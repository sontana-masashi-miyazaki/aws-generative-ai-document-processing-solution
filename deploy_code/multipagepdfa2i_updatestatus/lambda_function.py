import datetime
import json
import logging
import os
from typing import Any, Dict, Optional

import boto3
import botocore

# No document content in logs.
logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_DDB = boto3.resource("dynamodb")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _require_str(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    raise ValueError(f"Missing required field: one of {keys}")


def _join_s3_key(prefix: str, suffix: str) -> str:
    prefix = (prefix or "").lstrip("/")
    suffix = (suffix or "").lstrip("/")
    if prefix.endswith("/"):
        return prefix + suffix
    return f"{prefix}/{suffix}" if prefix else suffix


def _s3_get_bytes(bucket: str, key: str) -> bytes:
    resp = _S3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _s3_count_jsonl(bucket: str, key: str) -> Optional[int]:
    try:
        body = _s3_get_bytes(bucket, key).decode("utf-8")
    except botocore.exceptions.ClientError as e:  # noqa: BLE001
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise

    return sum(1 for ln in body.splitlines() if ln.strip())


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """UpdateStatusSucceeded Lambda.

    Updates a DynamoDB item keyed by document_id (partition key) in STATUS_TABLE.

    If STATUS_TABLE is not set, this Lambda is a no-op and succeeds.

    Sets:
      - status = INDEXED
      - finished_at (ISO8601)
      - pipeline_version
      - counts (best-effort, from S3 artifacts)

    Env:
      - STATUS_TABLE (optional)
      - PIPELINE_VERSION (optional)
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    table_name = os.getenv("STATUS_TABLE")
    pipeline_version = (
        (event.get("pipeline_version") if isinstance(event.get("pipeline_version"), str) else None)
        or os.getenv("PIPELINE_VERSION")
        or "unknown"
    )

    chunks_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")
    embeds_key = _join_s3_key(work_prefix, "vectors/embeddings.jsonl")
    enrich_manifest_key = _join_s3_key(work_prefix, "structured/enrichment_manifest.json")

    counts: Dict[str, Any] = {
        "chunks": _s3_count_jsonl(bucket, chunks_key),
        "embeddings": _s3_count_jsonl(bucket, embeds_key),
    }

    # Enrichment counts are optional
    try:
        enrich_manifest = json.loads(_s3_get_bytes(bucket, enrich_manifest_key).decode("utf-8"))
        if isinstance(enrich_manifest, dict):
            for k in ("images_total", "images_succeeded", "images_failed"):
                if k in enrich_manifest:
                    counts[k] = enrich_manifest.get(k)
    except botocore.exceptions.ClientError as e:  # noqa: BLE001
        code = e.response.get("Error", {}).get("Code")
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
    except Exception:
        pass

    finished_at = _now_iso()

    if not table_name:
        return {
            "bucket": bucket,
            "work_prefix": work_prefix,
            "document_id": document_id,
            "update_status": {"mode": "noop", "finished_at": finished_at, "pipeline_version": pipeline_version},
        }

    table = _DDB.Table(table_name)
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET #st = :st, finished_at = :fa, pipeline_version = :pv, counts = :c",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":st": "INDEXED",
            ":fa": finished_at,
            ":pv": pipeline_version,
            ":c": counts,
        },
    )

    return {
        "bucket": bucket,
        "work_prefix": work_prefix,
        "document_id": document_id,
        "update_status": {
            "table": table_name,
            "status": "INDEXED",
            "finished_at": finished_at,
            "pipeline_version": pipeline_version,
            "counts": counts,
        },
    }
