import datetime
import json
import os
import posixpath
import time
from typing import Any, Dict, List, Optional

import boto3


S3 = boto3.client("s3")


class PdfExtractionError(Exception):
    """Raised when the PDF extraction step cannot produce usable artifacts."""


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _get_textract_client():
    region = os.environ.get("TEXTRACT_REGION") or os.environ.get("AWS_REGION")
    return boto3.client("textract", region_name=region) if region else boto3.client("textract")


def _remaining_seconds(context) -> Optional[float]:
    try:
        return context.get_remaining_time_in_millis() / 1000.0
    except Exception:
        return None


def _write_artifacts(
    bucket: str,
    objects_key: str,
    manifest_key: str,
    objects: List[Dict[str, Any]],
    manifest: Dict[str, Any],
) -> None:
    body_lines = "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objects)
    S3.put_object(
        Bucket=bucket,
        Key=objects_key,
        Body=body_lines.encode("utf-8"),
        ContentType="application/json",
    )

    S3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def lambda_handler(event, context):
    document_id = event.get("document_id")
    if not document_id:
        raise PdfExtractionError("Missing required field: document_id")

    source_bucket = event.get("source_bucket")
    source_key = event.get("source_key")
    if not source_bucket or not source_key:
        raise PdfExtractionError("Missing required fields: source_bucket/source_key")

    processing_bucket = event.get("processing_bucket")
    structured_prefix = event.get("structured_prefix")
    if not processing_bucket or not structured_prefix:
        raise PdfExtractionError("Missing required fields: processing_bucket/structured_prefix")

    objects_key = event.get("structured_objects_key") or posixpath.join(structured_prefix, "objects.jsonl")
    manifest_key = event.get("document_manifest_key") or posixpath.join(structured_prefix, "document_manifest.json")

    max_wait_seconds = float(os.environ.get("TEXTRACT_PDF_MAX_WAIT_SECONDS", "240"))
    poll_seconds = float(os.environ.get("TEXTRACT_PDF_POLL_SECONDS", "5"))
    max_lines = int(os.environ.get("TEXTRACT_PDF_MAX_LINES", "20000"))

    textract = _get_textract_client()

    started_at = time.time()
    errors: List[str] = []
    status: str = "UNKNOWN"
    job_id: Optional[str] = None
    objects: List[Dict[str, Any]] = []
    truncated = False

    try:
        start_resp = textract.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": source_bucket, "Name": source_key}}
        )
        job_id = start_resp.get("JobId")
        if not job_id:
            raise PdfExtractionError("Textract StartDocumentTextDetection returned no JobId")

        # Poll for completion.
        while True:
            rem = _remaining_seconds(context)
            if rem is not None and rem < 10:
                status = "TIMED_OUT"
                errors.append("lambda_near_timeout")
                break

            if time.time() - started_at > max_wait_seconds:
                status = "TIMED_OUT"
                errors.append("textract_timeout")
                break

            resp = textract.get_document_text_detection(JobId=job_id)
            status = resp.get("JobStatus") or "UNKNOWN"

            if status in {"SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"}:
                break

            time.sleep(max(0.0, poll_seconds))

        if status in {"SUCCEEDED", "PARTIAL_SUCCESS"}:
            next_token = None
            line_index = 0

            while True:
                kwargs = {"JobId": job_id, "MaxResults": 1000}
                if next_token:
                    kwargs["NextToken"] = next_token
                page_resp = textract.get_document_text_detection(**kwargs)

                for b in page_resp.get("Blocks", []) or []:
                    if b.get("BlockType") != "LINE":
                        continue

                    text = (b.get("Text") or "").strip()
                    if not text:
                        continue

                    line_index += 1
                    if line_index > max_lines:
                        truncated = True
                        break

                    page_num = b.get("Page")
                    objects.append(
                        {
                            "id": f"pdf:{document_id}:line:{line_index}",
                            "type": "text",
                            "part_id": f"page:{page_num}" if page_num else None,
                            "order": line_index,
                            "page": page_num,
                            "confidence": b.get("Confidence"),
                            "loc": {"pdf": {"page": page_num}} if page_num else {"pdf": {}},
                            "text": text,
                        }
                    )

                if truncated:
                    break

                next_token = page_resp.get("NextToken")
                if not next_token:
                    break

        if status == "FAILED":
            errors.append("textract_failed")

    except Exception as e:
        status = "FAILED"
        # Do not include exception message (may contain sensitive info).
        errors.append(f"exception:{type(e).__name__}")

    manifest = {
        "schema": "document-manifest@1",
        "document_id": str(document_id),
        "source": {
            "bucket": source_bucket,
            "key": source_key,
            "s3_uri": event.get("source_s3_uri"),
            "type": "pdf",
        },
        "output": {
            "bucket": processing_bucket,
            "structured_prefix": structured_prefix,
            "objects_key": objects_key,
            "manifest_key": manifest_key,
        },
        "textract": {
            "job_id": job_id,
            "status": status,
            "region": os.environ.get("TEXTRACT_REGION") or os.environ.get("AWS_REGION"),
        },
        "stats": {
            "object_count": len(objects),
            "object_type_counts": {"text": len(objects)},
            "truncated": truncated,
        },
        "status": "succeeded" if (len(objects) > 0 and status in {"SUCCEEDED", "PARTIAL_SUCCESS"}) else "failed",
        "errors": errors,
        "created_at": _utc_now_iso(),
    }

    # Always write artifacts so downstream validation can make a deterministic decision.
    _write_artifacts(
        bucket=processing_bucket,
        objects_key=objects_key,
        manifest_key=manifest_key,
        objects=objects,
        manifest=manifest,
    )

    print(
        f"PdfExtract: document_id={document_id} textract_status={status} objects={len(objects)} truncated={truncated}"
    )

    out = dict(event)
    out["pdf_extract"] = {
        "structured_objects": {"bucket": processing_bucket, "key": objects_key},
        "document_manifest": {"bucket": processing_bucket, "key": manifest_key},
        "object_count": len(objects),
        "status": manifest["status"],
    }
    return out
