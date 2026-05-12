import json
from typing import Any, Dict

import boto3


S3 = boto3.client("s3")


class ExtractResultValidationError(Exception):
    """Raised when required extraction artifacts are missing or invalid."""


class MissingArtifactError(ExtractResultValidationError):
    """Raised when expected artifacts are not found in S3."""


def _head(bucket: str, key: str, label: str) -> Dict[str, Any]:
    try:
        return S3.head_object(Bucket=bucket, Key=key)
    except Exception as e:
        raise MissingArtifactError(f"Missing required artifact: {label}") from e


def _read_json(bucket: str, key: str) -> Dict[str, Any]:
    obj = S3.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("manifest is not a JSON object")
        return data
    except Exception as e:
        raise ExtractResultValidationError("document_manifest.json is not valid JSON") from e


def lambda_handler(event, context):
    processing_bucket = event.get("processing_bucket")
    structured_prefix = event.get("structured_prefix")
    document_id = event.get("document_id")

    if not processing_bucket or not structured_prefix:
        raise ExtractResultValidationError("Missing required fields: processing_bucket/structured_prefix")

    objects_key = event.get("structured_objects_key") or (structured_prefix + "objects.jsonl")
    manifest_key = event.get("document_manifest_key") or (structured_prefix + "document_manifest.json")

    objects_head = _head(processing_bucket, objects_key, "objects.jsonl")
    _head(processing_bucket, manifest_key, "document_manifest.json")

    manifest = _read_json(processing_bucket, manifest_key)

    stats = manifest.get("stats") if isinstance(manifest.get("stats"), dict) else {}
    object_count = stats.get("object_count") if isinstance(stats.get("object_count"), int) else manifest.get("object_count")

    status = manifest.get("status")
    errors = manifest.get("errors") or []

    print(
        f"ExtractResultValidation: document_id={document_id} objects_bytes={objects_head.get('ContentLength')} object_count={object_count} status={status}"
    )

    if status and status != "succeeded":
        raise ExtractResultValidationError(f"Extraction manifest reports failure: {status}")

    if errors:
        raise ExtractResultValidationError("Extraction manifest contains errors")

    if not isinstance(object_count, int) or object_count <= 0:
        raise ExtractResultValidationError("Extraction produced zero objects")

    return dict(event)
