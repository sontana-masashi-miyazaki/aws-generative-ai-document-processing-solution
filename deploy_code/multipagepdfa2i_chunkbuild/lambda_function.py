import datetime
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional

import boto3
import botocore

# No document content in logs.
logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")


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


def _s3_exists(bucket: str, key: str) -> bool:
    try:
        _S3.head_object(Bucket=bucket, Key=key)
        return True
    except botocore.exceptions.ClientError as e:  # noqa: BLE001
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _s3_get_bytes(bucket: str, key: str) -> bytes:
    resp = _S3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _s3_get_jsonl(bucket: str, key: str) -> Iterable[Dict[str, Any]]:
    body = _s3_get_bytes(bucket, key).decode("utf-8")
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        yield json.loads(ln)


def _s3_put_jsonl(bucket: str, key: str, json_lines: Iterable[str]) -> None:
    payload = "\n".join(json_lines) + "\n"
    _S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _safe_id(s: str) -> str:
    s = s or ""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s)
    return s[:256] if len(s) > 256 else s


def _extract_text(obj: Dict[str, Any]) -> Optional[str]:
    for k in ("text", "content", "body", "value"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _extract_title(obj: Dict[str, Any]) -> Optional[str]:
    for k in ("title", "heading", "name"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:256]
    return None


def _split_text(text: str, max_chars: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(i + max_chars, n)
        if j < n:
            window_start = i + int(max_chars * 0.8)
            cut = max(text.rfind("\n", window_start, j), text.rfind(" ", window_start, j))
            if cut > i:
                j = cut
        part = text[i:j].strip()
        if part:
            parts.append(part)
        i = j
    return parts


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """ChunkBuild Lambda.

    Prefer structured/enriched_objects.jsonl if present, otherwise structured/objects.jsonl.

    Output:
      - search/chunks/chunks.jsonl (JSONL)
        {chunk_id, document_id, source_object_ids, title, text, metadata}

    Env:
      - CHUNK_MAX_CHARS (default 2000)
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    max_chars = int(os.getenv("CHUNK_MAX_CHARS", "2000"))

    enriched_objects_key = _join_s3_key(work_prefix, "structured/enriched_objects.jsonl")
    objects_key = _join_s3_key(work_prefix, "structured/objects.jsonl")

    if _s3_exists(bucket, enriched_objects_key):
        input_key = enriched_objects_key
        input_source = "enriched"
    else:
        input_key = objects_key
        input_source = "raw"

    out_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")

    chunks_out: List[str] = []
    chunk_count = 0
    object_count = 0

    for idx, obj in enumerate(_s3_get_jsonl(bucket, input_key)):
        object_count += 1
        obj_id = obj.get("id")
        obj_id = obj_id if isinstance(obj_id, str) and obj_id.strip() else f"obj_{idx}"

        text = _extract_text(obj)
        if not text:
            continue

        title = _extract_title(obj)
        obj_type = obj.get("type") if isinstance(obj.get("type"), str) else None

        for part_idx, part in enumerate(_split_text(text, max_chars)):
            chunk_id = _safe_id(f"{document_id}-{obj_id}-{part_idx}")
            chunk = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "source_object_ids": [obj_id],
                "title": title,
                "text": part,
                "metadata": {
                    "source": input_source,
                    "source_object_type": obj_type,
                    "source_object_id": obj_id,
                    "part_index": part_idx,
                },
            }
            chunks_out.append(json.dumps(chunk, ensure_ascii=False))
            chunk_count += 1

    _s3_put_jsonl(bucket, out_key, chunks_out)

    return {
        "bucket": bucket,
        "work_prefix": work_prefix,
        "document_id": document_id,
        "chunk_build": {
            "input": input_source,
            "objects_seen": object_count,
            "chunks_written": chunk_count,
            "max_chars": max_chars,
            "finished_at": _now_iso(),
        },
    }
