import datetime
import json
import logging
import os
from base64 import b64decode
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlparse

import boto3
import botocore
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# No document content in logs.
logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_SECRETS = boto3.client("secretsmanager")
_HTTP = urllib3.PoolManager(cert_reqs="CERT_REQUIRED")


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


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint
    return endpoint.rstrip("/")


def _sign_and_post(url: str, body: bytes, region: str) -> Tuple[int, bytes]:
    session = botocore.session.get_session()
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError("Missing AWS credentials")

    parsed = urlparse(url)
    headers = {"Host": parsed.netloc, "Content-Type": "application/x-ndjson"}

    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "es", region).add_auth(req)

    resp = _HTTP.request(
        "POST",
        url,
        body=body,
        headers=dict(req.headers),
        timeout=urllib3.Timeout(connect=3.0, read=30.0),
        retries=False,
    )
    return int(resp.status), resp.data


def _secret_text(secret_id: str) -> str:
    response = _SECRETS.get_secret_value(SecretId=secret_id)
    if "SecretString" in response and isinstance(response["SecretString"], str):
        return response["SecretString"]

    secret_binary = response.get("SecretBinary")
    if isinstance(secret_binary, (bytes, bytearray)):
        return b64decode(secret_binary).decode("utf-8")

    raise RuntimeError("INDEX_FAILED")


def _elastic_api_key(secret_text: str) -> str:
    text = secret_text.strip()
    if not text:
        raise RuntimeError("INDEX_FAILED")

    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("INDEX_FAILED")
        for key in ("api_key", "apiKey", "encoded", "token"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise RuntimeError("INDEX_FAILED")

    return text


def _post_with_headers(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
    resp = _HTTP.request(
        "POST",
        url,
        body=body,
        headers=headers,
        timeout=urllib3.Timeout(connect=3.0, read=30.0),
        retries=False,
    )
    return int(resp.status), resp.data


def _iter_bulk_batches(
    docs: Iterable[Tuple[str, Dict[str, Any]]],
    index: str,
    max_docs: int,
    max_bytes: int,
) -> Iterable[bytes]:
    batch_lines: List[str] = []
    batch_docs = 0
    batch_bytes = 0

    for doc_id, doc in docs:
        action_line = json.dumps({"index": {"_index": index, "_id": doc_id}}, separators=(",", ":"))
        doc_line = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
        add_bytes = len(action_line) + 1 + len(doc_line) + 1

        if batch_docs and (batch_docs >= max_docs or batch_bytes + add_bytes >= max_bytes):
            yield ("\n".join(batch_lines) + "\n").encode("utf-8")
            batch_lines = []
            batch_docs = 0
            batch_bytes = 0

        batch_lines.append(action_line)
        batch_lines.append(doc_line)
        batch_docs += 1
        batch_bytes += add_bytes

    if batch_docs:
        yield ("\n".join(batch_lines) + "\n").encode("utf-8")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """IndexWriter Lambda.

    Reads:
      - search/chunks/chunks.jsonl
      - vectors/embeddings.jsonl

    Writes:
      - AWS OpenSearch or Elastic Cloud index via Bulk API.

    Env:
      - SEARCH_BACKEND (aws-opensearch or elastic-cloud)
      - SEARCH_ENDPOINT (required unless SEARCH_NOOP=true)
      - SEARCH_INDEX (required unless SEARCH_NOOP=true)
      - SEARCH_API_KEY_SECRET_ARN (required for Elastic Cloud)
      - SEARCH_NOOP=true (optional): do nothing, succeed
      - EMBEDDING_DIMENSIONS (optional): fail if vector lengths do not match
      - BULK_MAX_DOCS (default 200)
      - BULK_MAX_BYTES (default 5MB)

    On any failure raises RuntimeError("INDEX_FAILED").
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    pipeline_version = event.get("pipeline_version") if isinstance(event.get("pipeline_version"), str) else None

    backend = (os.getenv("SEARCH_BACKEND") or "aws-opensearch").strip().lower()
    endpoint = os.getenv("SEARCH_ENDPOINT")
    index = os.getenv("SEARCH_INDEX")
    api_key_secret_arn = os.getenv("SEARCH_API_KEY_SECRET_ARN")
    noop = os.getenv("SEARCH_NOOP", "false").strip().lower() == "true"
    expected_dimensions = os.getenv("EMBEDDING_DIMENSIONS")

    if not endpoint or not index:
        if noop:
            out = dict(event)
            out["bucket"] = bucket
            out["work_prefix"] = work_prefix
            out["document_id"] = document_id
            out["index_writer"] = {"mode": "noop", "finished_at": _now_iso()}
            return out
        raise RuntimeError("INDEX_FAILED")

    if backend not in {"aws-opensearch", "elastic-cloud"}:
        raise RuntimeError("INDEX_FAILED")
    if backend == "elastic-cloud" and not api_key_secret_arn:
        raise RuntimeError("INDEX_FAILED")

    endpoint = _normalize_endpoint(endpoint)

    region = os.getenv("AWS_REGION") or boto3.session.Session().region_name or "us-east-1"
    max_docs = int(os.getenv("BULK_MAX_DOCS", "200"))
    max_bytes = int(os.getenv("BULK_MAX_BYTES", str(5 * 1024 * 1024)))
    expected_dimensions_int = int(expected_dimensions) if expected_dimensions else None
    elastic_api_key = None
    if backend == "elastic-cloud":
        elastic_api_key = _elastic_api_key(_secret_text(api_key_secret_arn))

    chunks_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")
    embeds_key = _join_s3_key(work_prefix, "vectors/embeddings.jsonl")

    embeddings: Dict[str, List[float]] = {}
    for rec in _s3_get_jsonl(bucket, embeds_key):
        cid = rec.get("chunk_id")
        vec = rec.get("vector")
        if isinstance(cid, str) and isinstance(vec, list):
            if expected_dimensions_int is not None and len(vec) != expected_dimensions_int:
                raise RuntimeError("INDEX_FAILED")
            embeddings[cid] = vec

    missing_embeddings = 0

    def docs_iter() -> Iterable[Tuple[str, Dict[str, Any]]]:
        nonlocal missing_embeddings
        for chunk in _s3_get_jsonl(bucket, chunks_key):
            cid = chunk.get("chunk_id")
            if not isinstance(cid, str) or not cid.strip():
                continue

            vec = embeddings.get(cid)
            if vec is None:
                missing_embeddings += 1
                continue

            meta = chunk.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            if isinstance(chunk.get("source_object_ids"), list):
                meta = dict(meta)
                meta.setdefault("source_object_ids", chunk.get("source_object_ids"))

            yield cid, {
                "chunk_id": cid,
                "document_id": chunk.get("document_id") or document_id,
                "pipeline_version": chunk.get("pipeline_version") or pipeline_version,
                "title": chunk.get("title"),
                "text": chunk.get("text"),
                "metadata": meta,
                "embedding": vec,
            }

    try:
        batches = 0
        docs_indexed = 0

        for payload in _iter_bulk_batches(docs_iter(), index=index, max_docs=max_docs, max_bytes=max_bytes):
            batches += 1
            if backend == "aws-opensearch":
                status, data = _sign_and_post(f"{endpoint}/_bulk", payload, region)
            else:
                status, data = _post_with_headers(
                    f"{endpoint}/_bulk",
                    payload,
                    {
                        "Content-Type": "application/x-ndjson",
                        "Authorization": f"ApiKey {elastic_api_key}",
                    },
                )
            if status < 200 or status >= 300:
                raise RuntimeError("INDEX_FAILED")

            resp = json.loads(data)
            if resp.get("errors"):
                raise RuntimeError("INDEX_FAILED")

            items = resp.get("items")
            if isinstance(items, list):
                docs_indexed += len(items)

    except Exception:  # noqa: BLE001
        # Do not include underlying exception details to avoid leaking content into logs.
        raise RuntimeError("INDEX_FAILED")

    out = dict(event)
    out["bucket"] = bucket
    out["work_prefix"] = work_prefix
    out["document_id"] = document_id
    out["index_writer"] = {
        "backend": backend,
        "endpoint": endpoint,
        "index": index,
        "batches": batches,
        "docs_indexed": docs_indexed,
        "docs_skipped_missing_embedding": missing_embeddings,
        "finished_at": _now_iso(),
    }
    return out
