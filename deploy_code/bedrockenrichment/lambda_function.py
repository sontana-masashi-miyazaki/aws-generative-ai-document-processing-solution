import datetime
import json
import logging
import os
import uuid
from typing import Any, Dict, Iterable, List

import boto3
import botocore

# No document content in logs.
logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_BEDROCK = boto3.client("bedrock-runtime")
_BEDROCK_IMAGE_FORMATS = {
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
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


def _s3_get_json(bucket: str, key: str) -> Any:
    return json.loads(_s3_get_bytes(bucket, key).decode("utf-8"))


def _s3_get_jsonl_lines(bucket: str, key: str) -> List[str]:
    body = _s3_get_bytes(bucket, key).decode("utf-8")
    return [ln for ln in body.splitlines() if ln.strip()]


def _s3_put_json(bucket: str, key: str, obj: Any) -> None:
    _S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _s3_put_jsonl(bucket: str, key: str, json_lines: Iterable[str]) -> None:
    payload = "\n".join(json_lines) + "\n"
    _S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _s3_delete_if_exists(bucket: str, key: str) -> None:
    try:
        _S3.delete_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"NoSuchKey", "404"}:
            raise


def _extract_image_keys_from_manifest(manifest: Any) -> List[str]:
    # Supports common shapes:
    # - {"images": ["assets/images/...", ...]}
    # - {"images": [{"key": "assets/images/..."}, ...]}
    # - {"assets": {"images": [...]}}
    images = None
    if isinstance(manifest, dict):
        images = manifest.get("images")
        if images is None and isinstance(manifest.get("assets"), dict):
            images = manifest["assets"].get("images")

    if not isinstance(images, list):
        return []

    out: List[str] = []
    for item in images:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for k in ("key", "s3_key", "path"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v)
                    break
    return out


def _bedrock_image_format_from_key(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    image_format = _BEDROCK_IMAGE_FORMATS.get(ext)
    if not image_format:
        raise ValueError("UNSUPPORTED_IMAGE_FORMAT")
    return image_format


def _bedrock_describe_image(image_bytes: bytes, model_id: str, image_format: str) -> str:
    # Uses Bedrock Runtime Converse API (multimodal). The configured model must support image input.
    # image_format must be a Bedrock-supported value such as "png" or "jpeg".
    prompt = (
        "Describe this image in 1-2 sentences. "
        "If there is text, provide a brief OCR-style transcription summary."
    )

    resp = _BEDROCK.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": prompt},
                    {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 256, "temperature": 0.2},
    )

    parts = resp.get("output", {}).get("message", {}).get("content", [])
    texts = [
        p.get("text")
        for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), str) and p.get("text").strip()
    ]
    return "\n".join(t.strip() for t in texts)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """BedrockEnrichment Lambda.

    Inputs (under work_prefix):
      - structured/assets_manifest.json
      - structured/objects.jsonl

    Outputs:
      - structured/enriched_objects.jsonl (never overwrites objects.jsonl)
      - structured/enrichment_manifest.json

    Env:
      - BEDROCK_IMAGE_MODEL_ID (recommended) / BEDROCK_MODEL_ID: Bedrock Runtime model ID.
        This MUST be an image-capable (multimodal) model because this Lambda sends image bytes
        via the Converse API.
        If not set, this Lambda writes placeholder enrichment objects + a manifest and then
        raises ENRICHMENT_FAILED (unless ENRICHMENT_NOOP=true).
      - ENRICHMENT_NOOP=true: allow missing model id (placeholder-only) and succeed.

    NOTE: This Lambda assumes it is invoked only when assets.images >= 1.
    """

    processing_bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    # Image enrichment requires an image-capable Bedrock model.
    model_id = os.getenv("BEDROCK_IMAGE_MODEL_ID") or os.getenv("BEDROCK_MODEL_ID")
    noop_ok = os.getenv("ENRICHMENT_NOOP", "false").strip().lower() == "true"

    started_at = _now_iso()
    errors: List[Dict[str, str]] = []
    image_keys: List[str] = []
    allow_placeholder_only = (not model_id) and noop_ok

    manifest_key = _join_s3_key(work_prefix, "structured/assets_manifest.json")
    objects_key = _join_s3_key(work_prefix, "structured/objects.jsonl")
    enriched_key = _join_s3_key(work_prefix, "structured/enriched_objects.jsonl")
    out_manifest_key = _join_s3_key(work_prefix, "structured/enrichment_manifest.json")
    _s3_delete_if_exists(processing_bucket, enriched_key)
    _s3_delete_if_exists(processing_bucket, out_manifest_key)

    try:
        assets_manifest = _s3_get_json(processing_bucket, manifest_key)
        image_keys = _extract_image_keys_from_manifest(assets_manifest)

        if len(image_keys) < 1:
            _s3_put_json(
                processing_bucket,
                out_manifest_key,
                {
                    "document_id": document_id,
                    "status": "failed",
                    "error": "no_images",
                    "model_id": model_id,
                    "started_at": started_at,
                    "finished_at": _now_iso(),
                },
            )
            raise RuntimeError("ENRICHMENT_FAILED")

        original_lines = _s3_get_jsonl_lines(processing_bucket, objects_key)
        enriched_lines: List[str] = list(original_lines)

        succeeded = 0
        wp = (work_prefix or "").strip("/")

        for idx, rel_key in enumerate(image_keys):
            image_bucket = processing_bucket
            img_key = (rel_key or "").lstrip("/")

            # assets_manifest may contain either a relative key (assets/images/...), or an absolute key
            # under work_prefix (work/.../assets/images/...), or an s3:// URI.
            if img_key.startswith("s3://"):
                try:
                    no_scheme = img_key[5:]
                    bkt, _, k = no_scheme.partition("/")
                    if bkt and k:
                        image_bucket = bkt
                        img_key = k
                except Exception:
                    pass

            if image_bucket == processing_bucket:
                if wp and img_key.startswith(wp + "/"):
                    pass
                elif wp:
                    img_key = _join_s3_key(wp, img_key)

            try:
                if not model_id:
                    raise ValueError("BEDROCK_MODEL_ID_NOT_SET")

                img_bytes = _s3_get_bytes(image_bucket, img_key)
                image_format = _bedrock_image_format_from_key(img_key)
                desc = _bedrock_describe_image(img_bytes, model_id, image_format)
                if not desc:
                    raise ValueError("EMPTY_MODEL_RESPONSE")

                enriched_obj = {
                    "id": f"img_enrich_{uuid.uuid4().hex}",
                    "type": "image_enrichment",
                    "document_id": document_id,
                    "source": {"s3_bucket": image_bucket, "s3_key": img_key},
                    "text": desc,
                    "model_id": model_id,
                    "metadata": {"image_index": idx},
                }
                enriched_lines.append(json.dumps(enriched_obj, ensure_ascii=False))
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"image_key": rel_key, "error": type(exc).__name__})
                placeholder_obj = {
                    "id": f"img_enrich_{uuid.uuid4().hex}",
                    "type": "image_enrichment",
                    "document_id": document_id,
                    "source": {"s3_bucket": image_bucket, "s3_key": img_key},
                    "text": "Image description unavailable.",
                    "model_id": model_id,
                    "metadata": {"image_index": idx, "enrichment_error": True},
                }
                enriched_lines.append(json.dumps(placeholder_obj, ensure_ascii=False))

        finished_at = _now_iso()
        status = "succeeded" if not errors else "partial"

        if not errors or allow_placeholder_only:
            _s3_put_jsonl(processing_bucket, enriched_key, enriched_lines)
        _s3_put_json(
            processing_bucket,
            out_manifest_key,
            {
                "document_id": document_id,
                "status": status,
                "model_id": model_id,
                "images_total": len(image_keys),
                "images_succeeded": succeeded,
                "images_failed": len(errors),
                "errors": errors,
                "started_at": started_at,
                "finished_at": finished_at,
                "noop_mode": allow_placeholder_only,
            },
        )

        # Default behavior: fail if model_id missing (unless noop) OR any per-image error.
        if (not model_id and not noop_ok) or (errors and not allow_placeholder_only):
            _s3_delete_if_exists(processing_bucket, enriched_key)
            raise RuntimeError("ENRICHMENT_FAILED")

        out = dict(event)
        out["bucket"] = processing_bucket
        out["work_prefix"] = work_prefix
        out["document_id"] = document_id
        out["enrichment"] = {
            "images_total": len(image_keys),
            "images_succeeded": succeeded,
            "images_failed": len(errors),
            "model_id": model_id,
        }
        return out
    except Exception as exc:  # noqa: BLE001
        if str(exc) != "ENRICHMENT_FAILED":
            _s3_delete_if_exists(processing_bucket, enriched_key)
            _s3_put_json(
                processing_bucket,
                out_manifest_key,
                {
                    "document_id": document_id,
                    "status": "failed",
                    "model_id": model_id,
                    "images_total": len(image_keys),
                    "images_succeeded": 0,
                    "images_failed": len(image_keys),
                    "errors": [{"error": type(exc).__name__}],
                    "started_at": started_at,
                    "finished_at": _now_iso(),
                    "noop_mode": allow_placeholder_only,
                },
            )
        raise RuntimeError("ENRICHMENT_FAILED")
