import hashlib
import io
import json
import os
import posixpath
import urllib.parse
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, Tuple

import boto3


S3 = boto3.client("s3")


LEGACY_SOURCE_TYPE_TARGETS = {
    "doc": "docx",
    "xls": "xlsx",
    "ppt": "pptx",
}
SUPPORTED_SOURCE_TYPES = {"docx", "xlsx", "pptx", "pdf", *LEGACY_SOURCE_TYPE_TARGETS.keys()}
OOXML_OFFICE_TYPES = {"docx", "xlsx", "pptx"}


class InvalidInputError(Exception):
    """Raised when the Step Functions input is missing required fields."""


class UNSUPPORTED_FILE_TYPE(Exception):
    """Raised for file types the pipeline cannot process (Step Functions maps this)."""


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        raise InvalidInputError("source_s3_uri must be an s3://bucket/key URI")

    rest = uri[5:]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise InvalidInputError("source_s3_uri must include bucket and key")

    # Decode any URL escaping that may exist from upstream systems
    key = urllib.parse.unquote_plus(key)
    return bucket, key


def _find_first_payload(event: Any) -> Dict[str, Any]:
    """Accept raw Step Functions input or an SQS event wrapper."""
    if isinstance(event, dict) and "source_s3_uri" in event:
        return event

    # Some integrations pass the Step Functions input as a JSON string.
    if isinstance(event, str):
        try:
            decoded = json.loads(event)
            if isinstance(decoded, dict):
                return _find_first_payload(decoded)
        except Exception:
            pass

    # SQS wrapper: { Records: [ { body: "{...}" } ] }
    if isinstance(event, dict) and isinstance(event.get("Records"), list) and event["Records"]:
        body = event["Records"][0].get("body")
        if isinstance(body, str):
            try:
                decoded = json.loads(body)
                if isinstance(decoded, dict):
                    return _find_first_payload(decoded)
            except Exception:
                pass

    if isinstance(event, dict):
        return event

    return {}


def _detect_office_encryption(source_bytes: bytes, source_type: str) -> None:
    expected_root = {
        "docx": "word/document.xml",
        "xlsx": "xl/workbook.xml",
        "pptx": "ppt/presentation.xml",
    }.get(source_type)

    if not expected_root:
        return

    try:
        with zipfile.ZipFile(io.BytesIO(source_bytes), "r") as z:
            names_lc = {n.lower() for n in z.namelist()}

            # Standard encrypted OOXML packages include these at the ZIP root.
            if any(n.endswith("encryptioninfo") for n in names_lc) or any(
                n.endswith("encryptedpackage") for n in names_lc
            ):
                raise UNSUPPORTED_FILE_TYPE("Encrypted Office document")

            if expected_root.lower() not in names_lc:
                # Often indicates an invalid or encrypted package.
                raise UNSUPPORTED_FILE_TYPE(f"Invalid {source_type} (missing expected root)")

            # Quick structural validation: ensure the expected root parses as XML.
            root_bytes = z.read(expected_root)
            if not root_bytes:
                raise UNSUPPORTED_FILE_TYPE(f"Invalid {source_type} (empty expected root)")
            ET.fromstring(root_bytes)

    except zipfile.BadZipFile as e:
        raise UNSUPPORTED_FILE_TYPE(f"Invalid {source_type} (not a ZIP container)") from e
    except ET.ParseError as e:
        raise UNSUPPORTED_FILE_TYPE(f"Invalid {source_type} (malformed XML)") from e


def _source_filename_segment(source_key: str) -> str:
    filename = posixpath.basename((source_key or "").rstrip("/")) or "source"
    filename = unicodedata.normalize("NFKC", filename)
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = "".join(ch if ch >= " " and ch != "\x7f" else "_" for ch in filename)
    filename = filename.strip() or "source"
    return filename


def lambda_handler(event, context):
    payload = _find_first_payload(event)

    source_s3_uri = payload.get("source_s3_uri")
    document_id = payload.get("document_id")

    if not source_s3_uri:
        raise InvalidInputError("Missing required field: source_s3_uri")
    if not document_id:
        raise InvalidInputError("Missing required field: document_id")

    source_bucket, source_key = _parse_s3_uri(source_s3_uri)

    ext = os.path.splitext(source_key)[1].lower().lstrip(".")

    st = payload.get("source_type")
    if isinstance(st, str) and st.strip():
        source_type = st.strip().lower().lstrip(".")
    else:
        source_type = ext

    unsupported_reason = None
    if not source_type:
        unsupported_reason = "Could not determine file extension"
    elif source_type not in SUPPORTED_SOURCE_TYPES:
        unsupported_reason = f"Unsupported file type: {source_type}"

    max_source_bytes = int(os.environ.get("MAX_SOURCE_BYTES", str(50 * 1024 * 1024)))

    head = S3.head_object(Bucket=source_bucket, Key=source_key)
    content_length = int(head.get("ContentLength", 0))

    # Metadata-only log.
    # (Do not log file contents.)
    print(
        json.dumps(
            {
                "msg": "input-validation",
                "document_id": document_id,
                "source_type": source_type,
                "size_bytes": content_length,
            }
        )
    )

    if content_length <= 0:
        unsupported_reason = unsupported_reason or "Source object has no content"

    if content_length > max_source_bytes:
        unsupported_reason = unsupported_reason or "Source object exceeds MAX_SOURCE_BYTES"

    if source_type in OOXML_OFFICE_TYPES and unsupported_reason is None:
        obj = S3.get_object(Bucket=source_bucket, Key=source_key)
        src_bytes = obj["Body"].read()
        try:
            _detect_office_encryption(src_bytes, source_type)
        except UNSUPPORTED_FILE_TYPE as e:
            unsupported_reason = str(e) or "Encrypted/invalid Office document"

    legacy_office_target_type = LEGACY_SOURCE_TYPE_TARGETS.get(source_type)

    pipeline_version = (
        payload.get("pipeline_version")
        if isinstance(payload.get("pipeline_version"), str) and payload.get("pipeline_version").strip()
        else None
    )
    pipeline_version = (
        pipeline_version
        or (os.environ.get("PIPELINE_VERSION") or "").strip()
        or (os.environ.get("DEFAULT_PIPELINE_VERSION") or "").strip()
        or "pipeline_v1"
    )

    hash_prefix = hashlib.sha256(str(document_id).encode("utf-8")).hexdigest()[:2]
    source_filename = _source_filename_segment(source_key)

    # Use prefixes WITHOUT a trailing slash. Downstream code should join paths.
    work_prefix = posixpath.join(
        "work", pipeline_version, hash_prefix, source_filename, str(document_id)
    )
    structured_prefix = work_prefix + "/structured"
    assets_images_prefix = work_prefix + "/assets/images"

    processing_bucket = (
        (os.environ.get("PROCESSING_BUCKET") or "").strip()
        or payload.get("processing_bucket")
        or payload.get("bucket")
        or source_bucket
    )
    if not processing_bucket:
        raise InvalidInputError("Missing processing bucket")

    normalized: Dict[str, Any] = {
        "document_id": str(document_id),
        "source_s3_uri": source_s3_uri,
        "source_bucket": source_bucket,
        "source_key": source_key,
        "source_filename": source_filename,
        "source_type": source_type,
        "source_extension": ext,
        "legacy_office_source": legacy_office_target_type is not None,
        "legacy_office_target_type": legacy_office_target_type,
        "source_size_bytes": content_length,
        "source_etag": head.get("ETag"),
        "source_last_modified": head.get("LastModified").isoformat() if head.get("LastModified") else None,
        "processing_bucket": processing_bucket,
        # Compatibility alias used by downstream lambdas
        "bucket": processing_bucket,
        "pipeline_version": pipeline_version,
        "hash_prefix": hash_prefix,
        "work_prefix": work_prefix,
        "structured_prefix": structured_prefix,
        "assets_images_prefix": assets_images_prefix,
        "structured_objects_key": structured_prefix + "/objects.jsonl",
        "structured_assets_manifest_key": structured_prefix + "/assets_manifest.json",
        "document_manifest_key": structured_prefix + "/document_manifest.json",
    }

    if unsupported_reason:
        normalized["unsupported_file"] = True
        normalized["unsupported_reason"] = unsupported_reason

    # Preserve upstream metadata (do not log it).
    for k in ("trace_id", "request_id", "ingest_timestamp"):
        if k in payload:
            normalized[k] = payload[k]

    return normalized
