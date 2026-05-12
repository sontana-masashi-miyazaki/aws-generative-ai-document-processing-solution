import datetime
import json
import logging
import os
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
      - OpenSearch/Elasticsearch index via SigV4 signed Bulk API.

    Env:
      - OPENSEARCH_ENDPOINT (required unless OPENSEARCH_NOOP=true)
      - OPENSEARCH_INDEX (required unless OPENSEARCH_NOOP=true)
      - OPENSEARCH_NOOP=true (optional): do nothing, succeed
      - BULK_MAX_DOCS (default 200)
      - BULK_MAX_BYTES (default 5MB)

    On any failure raises RuntimeError("INDEX_FAILED").
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    endpoint = os.getenv("OPENSEARCH_ENDPOINT")
    index = os.getenv("OPENSEARCH_INDEX")
    noop = os.getenv("OPENSEARCH_NOOP", "false").strip().lower() == "true"

    if not endpoint or not index:
        if noop:
            return {
                "bucket": bucket,
                "work_prefix": work_prefix,
                "document_id": document_id,
                "index_writer": {"mode": "noop", "finished_at": _now_iso()},
            }
        raise RuntimeError("INDEX_FAILED")

    endpoint = _normalize_endpoint(endpoint)

    region = os.getenv("AWS_REGION") or boto3.session.Session().region_name or "us-east-1"
    max_docs = int(os.getenv("BULK_MAX_DOCS", "200"))
    max_bytes = int(os.getenv("BULK_MAX_BYTES", str(5 * 1024 * 1024)))

    chunks_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")
    embeds_key = _join_s3_key(work_prefix, "vectors/embeddings.jsonl")

    embeddings: Dict[str, List[float]] = {}
    for rec in _s3_get_jsonl(bucket, embeds_key):
        cid = rec.get("chunk_id")
        vec = rec.get("vector")
        if isinstance(cid, str) and isinstance(vec, list):
            embeddings[cid] = vec

    def docs_iter() -> Iterable[Tuple[str, Dict[str, Any]]]:
        for chunk in _s3_get_jsonl(bucket, chunks_key):
            cid = chunk.get("chunk_id")
            if not isinstance(cid, str) or not cid.strip():
                continue

            vec = embeddings.get(cid)
            if vec is None:
                raise RuntimeError("INDEX_FAILED")

            yield cid, {
                "chunk_id": cid,
                "document_id": chunk.get("document_id") or document_id,
                "title": chunk.get("title"),
                "text": chunk.get("text"),
                "metadata": chunk.get("metadata") or {},
                "embedding": vec,
            }

    try:
        batches = 0
        docs_indexed = 0

        for payload in _iter_bulk_batches(docs_iter(), index=index, max_docs=max_docs, max_bytes=max_bytes):
            batches += 1
            status, data = _sign_and_post(f"{endpoint}/_bulk", payload, region)
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

    return {
        "bucket": bucket,
        "work_prefix": work_prefix,
        "document_id": document_id,
        "index_writer": {
            "endpoint": endpoint,
            "index": index,
            "batches": batches,
            "docs_indexed": docs_indexed,
            "finished_at": _now_iso(),
        },
    }
