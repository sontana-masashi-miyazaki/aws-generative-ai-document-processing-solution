import json
import logging
import urllib.request
from typing import Any, Dict, List

import boto3
import botocore

logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_UPLOADS_PLACEHOLDER_KEY = "uploads/"
_DESIRED_CORS_RULE = {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "HEAD", "PUT", "POST"],
    "AllowedOrigins": [
        "https://console.aws.amazon.com",
        "https://*.console.aws.amazon.com",
    ],
    "ExposeHeaders": ["ETag", "x-amz-request-id", "x-amz-id-2"],
}


def _send_response(
    event: Dict[str, Any],
    context: Any,
    status: str,
    *,
    data: Dict[str, Any] | None = None,
    physical_resource_id: str,
    reason: str | None = None,
) -> None:
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch Logs: {context.log_stream_name}",
            "PhysicalResourceId": physical_resource_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": data or {},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    with urllib.request.urlopen(request):
        return


def _ensure_uploads_placeholder(bucket_name: str) -> None:
    _S3.put_object(
        Bucket=bucket_name,
        Key=_UPLOADS_PLACEHOLDER_KEY,
        Body=b"",
        ContentType="application/x-directory",
    )


def _rules_equivalent(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    for key in ("AllowedHeaders", "AllowedMethods", "AllowedOrigins", "ExposeHeaders"):
        left_values = sorted(v for v in left.get(key, []) if isinstance(v, str))
        right_values = sorted(v for v in right.get(key, []) if isinstance(v, str))
        if left_values != right_values:
            return False
    return True


def _get_existing_cors_rules(bucket_name: str) -> List[Dict[str, Any]]:
    try:
        resp = _S3.get_bucket_cors(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "NoSuchCORSConfiguration":
            return []
        raise
    return resp.get("CORSRules", [])


def _ensure_cors_rule(bucket_name: str) -> Dict[str, Any]:
    rules = _get_existing_cors_rules(bucket_name)
    if any(_rules_equivalent(rule, _DESIRED_CORS_RULE) for rule in rules):
        return {"mode": "noop", "cors_rule_count": len(rules)}

    rules.append(_DESIRED_CORS_RULE)
    _S3.put_bucket_cors(
        Bucket=bucket_name,
        CORSConfiguration={"CORSRules": rules},
    )
    return {"mode": "updated", "cors_rule_count": len(rules)}


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    properties = event.get("ResourceProperties") or {}
    bucket_name = properties.get("BucketName")
    physical_resource_id = f"bucket-bootstrap:{bucket_name or 'unknown'}"

    try:
        request_type = event["RequestType"]
        if not isinstance(bucket_name, str) or not bucket_name.strip():
            raise ValueError("BucketName is required")

        if request_type in ("Create", "Update"):
            _ensure_uploads_placeholder(bucket_name)
            cors_result = _ensure_cors_rule(bucket_name)
            data = {
                "bucket_name": bucket_name,
                "uploads_placeholder_key": _UPLOADS_PLACEHOLDER_KEY,
                **cors_result,
            }
            _send_response(
                event,
                context,
                "SUCCESS",
                data=data,
                physical_resource_id=physical_resource_id,
            )
            return data

        _send_response(
            event,
            context,
            "SUCCESS",
            data={"bucket_name": bucket_name, "mode": "delete-noop"},
            physical_resource_id=physical_resource_id,
        )
        return {"bucket_name": bucket_name, "mode": "delete-noop"}
    except Exception as e:  # noqa: BLE001
        _send_response(
            event,
            context,
            "FAILED",
            data={"bucket_name": bucket_name or "unknown"},
            physical_resource_id=physical_resource_id,
            reason=str(e),
        )
        raise
