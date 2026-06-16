import os
import posixpath
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

import boto3


S3 = boto3.client("s3")
LEGACY_TARGETS = {
    "doc": "docx",
    "xls": "xlsx",
    "ppt": "pptx",
}


class LegacyOfficeConversionError(Exception):
    """Raised when a legacy Office document cannot be converted."""


def _require_str(d: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise LegacyOfficeConversionError(f"Missing required field: one of {keys}")


def _source_bucket_key(event: Dict[str, Any]) -> Tuple[str, str]:
    bucket = event.get("source_bucket")
    key = event.get("source_key")
    if isinstance(bucket, str) and bucket.strip() and isinstance(key, str) and key.strip():
        return bucket, key
    raise LegacyOfficeConversionError("Missing source_bucket/source_key")


def _converted_key(work_prefix: str, source_key: str, target_type: str) -> str:
    base_name = Path(source_key).stem or "source"
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base_name)
    safe_name = safe_name.strip("_") or "source"
    return posixpath.join(work_prefix.strip("/"), "converted", f"{safe_name}.{target_type}")


def _convert_with_soffice(input_path: Path, output_dir: Path, target_type: str) -> Path:
    soffice_bin = os.getenv("SOFFICE_BIN", "/opt/libreoffice/program/soffice").strip()
    if not soffice_bin:
        raise LegacyOfficeConversionError("SOFFICE_BIN is empty")
    if shutil.which(soffice_bin) is None and not Path(soffice_bin).exists():
        raise LegacyOfficeConversionError(
            "LibreOffice binary not found. Set SOFFICE_BIN or provide a Lambda layer/image "
            "that contains soffice."
        )

    cmd = [
        soffice_bin,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "--convert-to",
        target_type,
        "--outdir",
        str(output_dir),
        str(input_path),
    ]
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
        env={
            **os.environ,
            "HOME": str(output_dir.parent),
            "TMPDIR": str(output_dir.parent),
        },
    )
    if completed.returncode != 0:
        raise LegacyOfficeConversionError(
            f"LibreOffice conversion failed: {completed.stderr.strip() or completed.stdout.strip() or completed.returncode}"
        )

    candidates = sorted(output_dir.glob(f"*.{target_type}"))
    if not candidates:
        raise LegacyOfficeConversionError("LibreOffice did not produce a converted file")
    return candidates[0]


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    source_type = str(event.get("source_type") or "").strip().lower()
    target_type = event.get("legacy_office_target_type")

    if not event.get("legacy_office_source"):
        return event

    if not isinstance(target_type, str) or not target_type.strip():
        target_type = LEGACY_TARGETS.get(source_type)
    if not target_type:
        raise LegacyOfficeConversionError(f"Unsupported legacy source type: {source_type}")

    source_bucket, source_key = _source_bucket_key(event)
    processing_bucket = _require_str(event, "processing_bucket", "bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")

    converted_key = _converted_key(work_prefix, source_key, target_type)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_path = tmp_path / f"source.{source_type}"
        output_dir = tmp_path / "out"
        output_dir.mkdir(parents=True, exist_ok=True)

        S3.download_file(source_bucket, source_key, str(input_path))
        converted_path = _convert_with_soffice(input_path, output_dir, target_type)

        S3.upload_file(
            str(converted_path),
            processing_bucket,
            converted_key,
            ExtraArgs={"ContentType": "application/octet-stream"},
        )

    head = S3.head_object(Bucket=processing_bucket, Key=converted_key)
    converted_uri = f"s3://{processing_bucket}/{converted_key}"

    out = dict(event)
    out["original_source_bucket"] = source_bucket
    out["original_source_key"] = source_key
    out["original_source_s3_uri"] = event.get("source_s3_uri") or f"s3://{source_bucket}/{source_key}"
    out["original_source_type"] = source_type
    out["source_bucket"] = processing_bucket
    out["source_key"] = converted_key
    out["source_s3_uri"] = converted_uri
    out["source_type"] = target_type
    out["source_extension"] = target_type
    out["legacy_office_source"] = False
    out["source_size_bytes"] = int(head.get("ContentLength", 0))
    out["source_etag"] = head.get("ETag")
    out["source_last_modified"] = (
        head.get("LastModified").isoformat() if head.get("LastModified") else None
    )
    out["legacy_office_conversion"] = {
        "status": "converted",
        "source_type": source_type,
        "target_type": target_type,
        "converted_bucket": processing_bucket,
        "converted_key": converted_key,
        "converted_s3_uri": converted_uri,
    }
    return out
