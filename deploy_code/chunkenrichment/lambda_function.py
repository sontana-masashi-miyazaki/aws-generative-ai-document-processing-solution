import datetime
import json
import logging
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

import boto3
import botocore

logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_BEDROCK = boto3.client("bedrock-runtime")

CHUNK_ENRICHMENT_VERSION = "chunk_enrich_v1"
KEYWORD_EXTRACTOR_VERSION = "keyword_rule_v1"

# group_kind ごとの summary 対象判定
_SUMMARY_PRIORITY_KINDS = {
    "sheet_rows",
    "table_row_block",
    "sheet_images",
    "sheet_cells",
    "slide",
    "slide_images",
    "document_flow",
    "page",
}

_MIN_TEXT_LENGTH_FOR_SUMMARY = 100


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def _s3_get_jsonl(bucket: str, key: str) -> List[Dict[str, Any]]:
    resp = _S3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read().decode("utf-8")
    return [json.loads(ln) for ln in body.splitlines() if ln.strip()]


def _s3_put_jsonl(bucket: str, key: str, json_lines: Iterable[str]) -> None:
    payload = "\n".join(json_lines) + "\n"
    _S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _s3_put_json(bucket: str, key: str, data: Any) -> None:
    _S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _s3_delete_if_exists(bucket: str, key: str) -> None:
    try:
        _S3.delete_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"NoSuchKey", "404"}:
            raise


# ---------------------------------------------------------------------------
# rule-based keyword extraction
# ---------------------------------------------------------------------------

_JP_STOPWORDS = frozenset(
    "の に は を た が で て と し れ さ ある いる する も な い か "
    "こと これ それ よう から まで など ため として について おり "
    "および ただし なお また さらに ここ どの その".split()
)

_EN_STOPWORDS = frozenset(
    "the a an is are was were be been being have has had do does did "
    "will would shall should may might must can could of in to for with "
    "on at from by about as into through during before after above below "
    "between out off over under again further then once and but or nor "
    "not so very just than too also this that these those it its".split()
)

_WORD_RE = re.compile(r"[\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]+", re.UNICODE)
_NUMERIC_RE = re.compile(r"^\d+$")
_CODE_RE = re.compile(r"[A-Z]{2,}[-_]?\d{2,}", re.IGNORECASE)


def _normalize_keyword(keyword: str) -> str:
    kw = unicodedata.normalize("NFKC", keyword).lower().strip()
    kw = re.sub(r"[\s\-_・]+", "_", kw)
    return kw.strip("_")


def _extract_keywords_raw(text: str, max_keywords: int = 20) -> List[str]:
    if not text:
        return []

    codes = _CODE_RE.findall(text)

    freq: Dict[str, int] = {}
    for w in _WORD_RE.findall(text):
        if len(w) < 2 or _NUMERIC_RE.match(w):
            continue
        w_lower = w.lower()
        if w_lower in _EN_STOPWORDS or w_lower in _JP_STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    keywords = [w for w, _ in sorted_words[:max_keywords]]

    seen = {kw.lower() for kw in keywords}
    for code in codes:
        if code.lower() not in seen:
            keywords.append(code)
            seen.add(code.lower())

    return keywords[:max_keywords]


def _normalize_keywords(keywords_raw: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for kw in keywords_raw:
        n = _normalize_keyword(kw)
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    return result


# ---------------------------------------------------------------------------
# summary / LLM enrichment
# ---------------------------------------------------------------------------

def _needs_summary(chunk: Dict[str, Any]) -> bool:
    text = chunk.get("text", "")
    if len(text) < _MIN_TEXT_LENGTH_FOR_SUMMARY:
        return False
    group_kind = (chunk.get("metadata") or {}).get("group_kind", "")
    if group_kind in _SUMMARY_PRIORITY_KINDS:
        return True
    return len(text) > 500


_ENRICH_PROMPT = """\
以下のテキストを分析し、JSON のみで回答してください。

テキスト:
{text}

回答フォーマット（JSON のみ、他の文章は不要）:
{{
  "summary": "このテキストの内容を1〜3文で要約",
  "keywords": ["重要な固有名詞・専門用語を最大10個"],
  "entities": {{
    "product_names": ["製品名・型番"],
    "org_names": ["部署名・組織名"],
    "system_names": ["システム名・ツール名"],
    "dates": ["日付・期間"],
    "amounts": ["金額・数量"]
  }},
  "aliases": ["上記キーワードの別名・略称・表記揺れがあれば記載"]
}}"""


def _parse_json_response(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _llm_enrich(text: str, title: Optional[str], model_id: str) -> Dict[str, Any]:
    input_text = text
    if title:
        input_text = f"タイトル: {title}\n\n{text}"
    if len(input_text) > 6000:
        input_text = input_text[:6000] + "\n\n[以降省略]"

    prompt = _ENRICH_PROMPT.format(text=input_text)

    resp = _BEDROCK.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
    )

    parts = resp.get("output", {}).get("message", {}).get("content", [])
    output_text = "".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    )
    result = _parse_json_response(output_text)
    return {
        "summary": result.get("summary") or None,
        "keywords": [kw for kw in result.get("keywords", []) if isinstance(kw, str)],
        "entities": result.get("entities") if isinstance(result.get("entities"), dict) else {},
        "aliases": [a for a in result.get("aliases", []) if isinstance(a, str)],
    }


# ---------------------------------------------------------------------------
# embedding_text composition
# ---------------------------------------------------------------------------

def _compose_embedding_text(chunk: Dict[str, Any], summary: Optional[str]) -> str:
    parts: List[str] = []
    title = chunk.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    text = chunk.get("text", "")
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """ChunkEnrichment Lambda.

    Reads:
      - search/chunks/chunks.jsonl

    Writes:
      - search/chunks/enriched_chunks.jsonl
      - search/chunks/chunk_enrichment_manifest.json

    Env:
      - CHUNK_ENRICHMENT_MODEL_ID  (未設定なら LLM summary はスキップ)
      - CHUNK_ENRICHMENT_NOOP      ("true" なら LLM 全スキップ)

    失敗時は RuntimeError("CHUNK_ENRICHMENT_FAILED") を送出。
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or "unknown"

    model_id = os.getenv("CHUNK_ENRICHMENT_MODEL_ID", "")
    noop = os.getenv("CHUNK_ENRICHMENT_NOOP", "false").lower() in ("true", "1", "yes")

    in_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")
    out_key = _join_s3_key(work_prefix, "search/chunks/enriched_chunks.jsonl")
    manifest_key = _join_s3_key(work_prefix, "search/chunks/chunk_enrichment_manifest.json")
    _s3_delete_if_exists(bucket, out_key)
    _s3_delete_if_exists(bucket, manifest_key)

    stats = {
        "summary_generated": 0,
        "summary_skipped": 0,
        "summary_failed": 0,
        "keywords_extracted": 0,
        "llm_errors": [],
    }

    try:
        chunks = _s3_get_jsonl(bucket, in_key)
        stats["total_chunks"] = len(chunks)

        enriched_out: List[str] = []

        for chunk in chunks:
            text = chunk.get("text", "")
            title = chunk.get("title")

            # --- rule-based keywords (always) ---
            keywords_raw = _extract_keywords_raw(text)
            keywords_normalized = _normalize_keywords(keywords_raw)
            stats["keywords_extracted"] += 1

            summary: Optional[str] = None
            aliases: List[str] = []
            entities: Dict[str, Any] = {}
            summary_status = "skipped"

            # --- LLM enrichment (conditional) ---
            if not noop and model_id and _needs_summary(chunk):
                try:
                    llm = _llm_enrich(text, title, model_id)
                    summary = llm.get("summary")
                    aliases = llm.get("aliases", [])
                    entities = llm.get("entities", {})

                    # merge LLM keywords into rule-based (deduplicated)
                    existing = {kw.lower() for kw in keywords_raw}
                    for kw in llm.get("keywords", []):
                        if kw and kw.lower() not in existing:
                            keywords_raw.append(kw)
                            existing.add(kw.lower())
                    keywords_normalized = _normalize_keywords(keywords_raw)

                    summary_status = "generated" if summary else "failed"
                    if summary:
                        stats["summary_generated"] += 1
                    else:
                        stats["summary_failed"] += 1
                except Exception:  # noqa: BLE001
                    summary_status = "failed"
                    stats["summary_failed"] += 1
                    stats["llm_errors"].append(chunk.get("chunk_id", "unknown"))
            else:
                stats["summary_skipped"] += 1

            embedding_text = _compose_embedding_text(chunk, summary)

            enriched_chunk = {
                **chunk,
                "summary": summary,
                "summary_status": summary_status,
                "keywords_raw": keywords_raw,
                "keywords_normalized": keywords_normalized,
                "aliases": aliases,
                "entities": entities,
                "embedding_text": embedding_text,
            }

            metadata = dict(enriched_chunk.get("metadata") or {})
            metadata["chunk_enrichment_version"] = CHUNK_ENRICHMENT_VERSION
            metadata["keyword_extractor_version"] = KEYWORD_EXTRACTOR_VERSION
            if model_id:
                metadata["chunk_enrichment_model"] = model_id
            enriched_chunk["metadata"] = metadata

            enriched_out.append(json.dumps(enriched_chunk, ensure_ascii=False))

        _s3_put_jsonl(bucket, out_key, enriched_out)

        manifest = {
            "status": "succeeded",
            "document_id": document_id,
            "chunk_enrichment_version": CHUNK_ENRICHMENT_VERSION,
            "keyword_extractor_version": KEYWORD_EXTRACTOR_VERSION,
            "model_id": model_id or None,
            "noop_mode": noop,
            "total_chunks": stats["total_chunks"],
            "summary_generated": stats["summary_generated"],
            "summary_skipped": stats["summary_skipped"],
            "summary_failed": stats["summary_failed"],
            "keywords_extracted": stats["keywords_extracted"],
            "errors": stats["llm_errors"],
            "finished_at": _now_iso(),
        }
        _s3_put_json(bucket, manifest_key, manifest)

    except Exception as exc:  # noqa: BLE001
        _s3_delete_if_exists(bucket, out_key)
        _s3_put_json(
            bucket,
            manifest_key,
            {
                "status": "failed",
                "document_id": document_id,
                "chunk_enrichment_version": CHUNK_ENRICHMENT_VERSION,
                "keyword_extractor_version": KEYWORD_EXTRACTOR_VERSION,
                "model_id": model_id or None,
                "noop_mode": noop,
                "error_type": type(exc).__name__,
                "finished_at": _now_iso(),
            },
        )
        raise RuntimeError("CHUNK_ENRICHMENT_FAILED")

    out = dict(event)
    out["bucket"] = bucket
    out["work_prefix"] = work_prefix
    out["document_id"] = document_id
    out["chunk_enrichment"] = {
        "total_chunks": stats.get("total_chunks", 0),
        "summary_generated": stats["summary_generated"],
        "summary_skipped": stats["summary_skipped"],
        "summary_failed": stats["summary_failed"],
        "model_id": model_id or None,
        "noop_mode": noop,
        "finished_at": _now_iso(),
    }
    return out
