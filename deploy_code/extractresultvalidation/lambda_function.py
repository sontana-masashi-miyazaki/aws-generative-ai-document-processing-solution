import json
from typing import Any, Dict, Optional

import boto3


S3 = boto3.client("s3")


class ExtractResultValidationError(Exception):
    """Raised when required extraction artifacts are missing or invalid."""


class MissingArtifactError(ExtractResultValidationError):
    """Raised when expected artifacts are not found in S3."""


def _head(bucket: str, key: str, label: str) -> Dict[str, Any]:
    try:
        return S3.head_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        raise MissingArtifactError(f"Missing required artifact: {label}") from e


def _read_json(bucket: str, key: str, label: str) -> Dict[str, Any]:
    obj = S3.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except Exception as e:  # noqa: BLE001
        raise ExtractResultValidationError(f"{label} is not valid JSON") from e


def _get_object_count(manifest: Dict[str, Any]) -> Optional[int]:
    for path in (
        ("stats", "object_count"),
        ("counts", "object_count"),
    ):
        parent = manifest.get(path[0])
        if isinstance(parent, dict) and isinstance(parent.get(path[1]), int):
            return int(parent[path[1]])

    if isinstance(manifest.get("object_count"), int):
        return int(manifest["object_count"])

    return None


def _get_status(manifest: Dict[str, Any]) -> Optional[str]:
    v = manifest.get("status")
    return v if isinstance(v, str) and v.strip() else None


def _get_errors(manifest: Dict[str, Any]) -> list:
    v = manifest.get("errors")
    return v if isinstance(v, list) else []


def _assets_images_count(assets_manifest: Dict[str, Any]) -> int:
    assets = assets_manifest.get("assets")
    if isinstance(assets, dict):
        images = assets.get("images")
        if isinstance(images, list):
            return len(images)

    images = assets_manifest.get("images")
    if isinstance(images, list):
        return len(images)

    return 0


def lambda_handler(event, context):
    # Bucket where extraction artifacts are written.
    processing_bucket = event.get("processing_bucket") or event.get("bucket")
    structured_prefix = event.get("structured_prefix")
    work_prefix = event.get("work_prefix")

    document_id = event.get("document_id")

    if not processing_bucket:
        raise ExtractResultValidationError("Missing required field: processing_bucket")

    if not structured_prefix:
        if isinstance(work_prefix, str) and work_prefix.strip():
            wp = work_prefix.strip("/")
            structured_prefix = f"{wp}/structured/"
        else:
            raise ExtractResultValidationError("Missing required fields: structured_prefix/work_prefix")

    if not structured_prefix.endswith("/"):
        structured_prefix += "/"

    objects_key = event.get("structured_objects_key") or (structured_prefix + "objects.jsonl")
    assets_manifest_key = event.get("structured_assets_manifest_key") or (structured_prefix + "assets_manifest.json")
    manifest_key = event.get("document_manifest_key") or (structured_prefix + "document_manifest.json")

    objects_head = _head(processing_bucket, objects_key, "objects.jsonl")
    _head(processing_bucket, assets_manifest_key, "assets_manifest.json")
    _head(processing_bucket, manifest_key, "document_manifest.json")

    manifest = _read_json(processing_bucket, manifest_key, "document_manifest.json")

    object_count = _get_object_count(manifest)
    status = _get_status(manifest)
    errors = _get_errors(manifest)

    # Metadata-only log.
    print(
        json.dumps(
            {
                "msg": "extract-result-validation",
                "document_id": document_id,
                "objects_bytes": objects_head.get("ContentLength"),
                "object_count": object_count,
                "status": status,
            }
        )
    )

    if status and status != "succeeded":
        raise ExtractResultValidationError("Extraction manifest reports failure")

    if errors:
        raise ExtractResultValidationError("Extraction manifest contains errors")

    if not isinstance(object_count, int) or object_count <= 0:
        raise ExtractResultValidationError("Extraction produced zero objects")

    assets_manifest = _read_json(processing_bucket, assets_manifest_key, "assets_manifest.json")
    images_count = _assets_images_count(assets_manifest)

    out = dict(event)
    out["bucket"] = processing_bucket
    out["assets_images_count"] = images_count
    return out
