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


def _optional_str(d: Dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


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


def _merge_failure_details(
    *,
    target_status: str,
    failure_context: Optional[Dict[str, Any]],
    error_detail: Optional[Dict[str, Any]],
    base_event: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if target_status == "INDEXED":
        return None

    merged: Dict[str, Any] = {}
    if isinstance(failure_context, dict):
        merged.update({k: v for k, v in failure_context.items() if v is not None})

    if isinstance(error_detail, dict) and error_detail:
        merged["error"] = error_detail
        merged.setdefault("error_type", error_detail.get("Error"))
        merged.setdefault("cause", error_detail.get("Cause"))

    unsupported_reason = _optional_str(base_event, "unsupported_reason")
    if unsupported_reason:
        merged.setdefault("cause", unsupported_reason)

    return merged or None


def _optional_failures(base_event: Dict[str, Any]) -> Dict[str, Any]:
    failures: Dict[str, Any] = {}
    for name in ("bedrock_enrichment", "chunk_enrichment"):
        failure_context = base_event.get(f"{name}_failure")
        error_detail = base_event.get(f"{name}_error")
        if not isinstance(failure_context, dict):
            continue

        merged = dict(failure_context)
        if isinstance(error_detail, dict) and error_detail:
            merged["error"] = error_detail
            merged.setdefault("error_type", error_detail.get("Error"))
            merged.setdefault("cause", error_detail.get("Cause"))
        failures[name] = merged

    return failures


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """UpdateStatus Lambda.

    Updates a DynamoDB item keyed by document_id (partition key) in STATUS_TABLE.

    If STATUS_TABLE is not set, this Lambda is a no-op and succeeds.

    Sets:
      - status = INDEXED on success path, or the requested failure status on failure paths
      - finished_at (ISO8601)
      - pipeline_version
      - counts (best-effort, from S3 artifacts when bucket/work_prefix are known)
      - last_error (on failure paths, when provided)
      - optional_failures (on success paths, when optional enrichment stages failed but workflow continued)

    Env:
      - STATUS_TABLE (optional)
      - PIPELINE_VERSION (optional)
    """

    state = event.get("state")
    base_event = state if isinstance(state, dict) else event
    failure_context = event.get("failure_context")
    failure_context = failure_context if isinstance(failure_context, dict) else None
    target_status = event.get("target_status")
    target_status = (
        target_status if isinstance(target_status, str) and target_status.strip() else "INDEXED"
    )

    bucket = _optional_str(base_event, "bucket", "Bucket", "processing_bucket")
    work_prefix = _optional_str(base_event, "work_prefix", "workPrefix", "prefix")
    document_id = base_event.get("document_id") or base_event.get("documentId") or base_event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    table_name = os.getenv("STATUS_TABLE")
    pipeline_version = (
        (
            base_event.get("pipeline_version")
            if isinstance(base_event.get("pipeline_version"), str)
            else None
        )
        or os.getenv("PIPELINE_VERSION")
        or "unknown"
    )

    counts: Dict[str, Any] = {}
    if bucket and work_prefix:
        chunks_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")
        embeds_key = _join_s3_key(work_prefix, "vectors/embeddings.jsonl")
        enrich_manifest_key = _join_s3_key(work_prefix, "structured/enrichment_manifest.json")

        counts = {
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
    error_detail = event.get("error")
    error_detail = error_detail if isinstance(error_detail, dict) else None
    last_error = _merge_failure_details(
        target_status=target_status,
        failure_context=failure_context,
        error_detail=error_detail,
        base_event=base_event,
    )
    optional_failures = _optional_failures(base_event)

    if not table_name:
        return {
            "bucket": bucket,
            "work_prefix": work_prefix,
            "document_id": document_id,
            "update_status": {
                "mode": "noop",
                "finished_at": finished_at,
                "pipeline_version": pipeline_version,
                "status": target_status,
                "optional_failures": optional_failures,
            },
        }

    table = _DDB.Table(table_name)
    set_expressions = ["#st = :st", "finished_at = :fa", "pipeline_version = :pv", "counts = :c"]
    remove_expressions = []
    expression_attribute_names = {"#st": "status"}
    expression_attribute_values: Dict[str, Any] = {
        ":st": target_status,
        ":fa": finished_at,
        ":pv": pipeline_version,
        ":c": counts,
    }

    if last_error:
        set_expressions.append("last_error = :e")
        expression_attribute_values[":e"] = last_error
    elif target_status == "INDEXED":
        remove_expressions.append("last_error")

    if optional_failures:
        set_expressions.append("optional_failures = :of")
        expression_attribute_values[":of"] = optional_failures
    elif target_status == "INDEXED":
        remove_expressions.append("optional_failures")

    update_expression = "SET " + ", ".join(set_expressions)
    if remove_expressions:
        update_expression += " REMOVE " + ", ".join(remove_expressions)

    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expression_attribute_names,
        ExpressionAttributeValues=expression_attribute_values,
    )

    return {
        "bucket": bucket,
        "work_prefix": work_prefix,
        "document_id": document_id,
        "update_status": {
            "table": table_name,
            "status": target_status,
            "finished_at": finished_at,
            "pipeline_version": pipeline_version,
            "counts": counts,
            "optional_failures": optional_failures,
        },
    }
