import datetime
import json
import logging
import os
from typing import Any, Dict, Iterable, List

import boto3

import botocore

# No document content in logs.
logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_BEDROCK = boto3.client("bedrock-runtime")
_DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
_LEGACY_EMBEDDING_MODEL_IDS = {
    "amazon.titan-embed-text-v2": _DEFAULT_EMBEDDING_MODEL_ID,
}


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


def _normalize_embedding_model_id(model_id: str) -> str:
    return _LEGACY_EMBEDDING_MODEL_IDS.get(model_id, model_id)


def _s3_exists(bucket: str, key: str) -> bool:
    try:
        _S3.head_object(Bucket=bucket, Key=key)
        return True
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _titan_embed(text: str, model_id: str) -> Dict[str, Any]:
    # Titan Embed Text v2 expects {"inputText": "..."} and optionally accepts "dimensions".
    payload: Dict[str, Any] = {"inputText": text}
    dimensions = os.getenv("EMBEDDING_DIMENSIONS")
    if dimensions:
        payload["dimensions"] = int(dimensions)

    resp = _BEDROCK.invoke_model(
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
        body=json.dumps(payload).encode("utf-8"),
    )
    data = json.loads(resp["body"].read())

    embedding = data.get("embedding")
    if embedding is None and isinstance(data.get("embeddings"), list) and data["embeddings"]:
        embedding = data["embeddings"][0]

    if not isinstance(embedding, list):
        raise ValueError("Invalid embedding response")

    return {
        "embedding": embedding,
        "input_tokens": data.get("inputTextTokenCount"),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Embedding Lambda.

    Reads (in priority order):
      - search/chunks/enriched_chunks.jsonl
      - search/chunks/chunks.jsonl

    Uses `embedding_text` field if present, otherwise `text`.

    Writes:
      - vectors/embeddings.jsonl (JSONL): {chunk_id, document_id, vector, model_id, metadata}

    Env:
      - EMBEDDING_MODEL_ID (default: amazon.titan-embed-text-v2:0)
      - EMBEDDING_DIMENSIONS (optional)

    On any failure raises RuntimeError("EMBEDDING_FAILED").
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    model_id = _normalize_embedding_model_id(
        os.getenv("EMBEDDING_MODEL_ID", _DEFAULT_EMBEDDING_MODEL_ID)
    )

    enriched_key = _join_s3_key(work_prefix, "search/chunks/enriched_chunks.jsonl")
    plain_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")

    if _s3_exists(bucket, enriched_key):
        in_key = enriched_key
        input_source = "enriched"
    else:
        in_key = plain_key
        input_source = "plain"

    out_key = _join_s3_key(work_prefix, "vectors/embeddings.jsonl")

    embeddings_out: List[str] = []
    chunks_seen = 0

    try:
        for chunk in _s3_get_jsonl(bucket, in_key):
            chunks_seen += 1

            chunk_id = chunk.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                continue

            # embedding_text (from ChunkEnrichment) を優先、なければ text を使う
            text = chunk.get("embedding_text") or chunk.get("text")
            if not isinstance(text, str) or not text.strip():
                continue

            emb = _titan_embed(text, model_id)

            record = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "vector": emb["embedding"],
                "model_id": model_id,
                "metadata": {"input_tokens": emb.get("input_tokens")},
            }
            embeddings_out.append(json.dumps(record, ensure_ascii=False))

        _s3_put_jsonl(bucket, out_key, embeddings_out)

    except Exception:  # noqa: BLE001
        # Do not include underlying exception details to avoid leaking content into logs.
        raise RuntimeError("EMBEDDING_FAILED")

    out = dict(event)
    out["bucket"] = bucket
    out["work_prefix"] = work_prefix
    out["document_id"] = document_id
    out["embedding"] = {
        "input_source": input_source,
        "chunks_seen": chunks_seen,
        "embeddings_written": len(embeddings_out),
        "model_id": model_id,
        "dimensions": (
            int(os.getenv("EMBEDDING_DIMENSIONS"))
            if os.getenv("EMBEDDING_DIMENSIONS")
            else None
        ),
        "finished_at": _now_iso(),
    }
    return out
