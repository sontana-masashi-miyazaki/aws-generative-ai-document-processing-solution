# /*
#  * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  * SPDX-License-Identifier: MIT-0
#  *
#  * Permission is hereby granted, free of charge, to any person obtaining a copy of this
#  * software and associated documentation files (the "Software"), to deal in the Software
#  * without restriction, including without limitation the rights to use, copy, modify,
#  * merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
#  * permit persons to whom the Software is furnished to do so.
#  *
#  * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
#  * INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
#  * PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
#  * HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
#  * OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
#  * SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#  */

import hashlib
import json
import os
from typing import Any, Dict, Iterable, Tuple
from urllib.parse import unquote, unquote_plus

import boto3

SFN = boto3.client("stepfunctions")
SQS = boto3.client("sqs")

SUPPORTED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"}


def _decode_key(raw_key: str) -> str:
    # S3 notifications may contain URL-encoded keys, so decode them.
    return unquote_plus(unquote(raw_key))


def _extract_s3_info(s3_record: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    s3 = s3_record["s3"]
    obj = s3.get("object") or {}
    bucket = s3["bucket"]["name"]
    key = _decode_key(obj["key"])
    etag = obj.get("eTag") or ""
    sequencer = obj.get("sequencer") or ""
    event_time = s3_record.get("eventTime") or ""
    return bucket, key, etag, sequencer, event_time


def _iter_s3_records_from_sqs_body(body: str) -> Iterable[Dict[str, Any]]:
    decoded = json.loads(body)
    for r in decoded.get("Records", []) or []:
        if isinstance(r, dict) and "s3" in r:
            yield r


def _stable_document_id(bucket: str, key: str, etag: str) -> str:
    # Deterministic ID prevents duplicate Step Functions executions on SQS retries.
    src = f"{bucket}/{key}:{etag}".encode("utf-8")
    return hashlib.sha256(src).hexdigest()[:32]


def _execution_id(bucket: str, key: str, etag: str, sequencer: str, event_time: str) -> str:
    # Keep Step Functions idempotent for duplicate delivery of the same S3 event,
    # but allow overwriting the same S3 key to start a fresh execution.
    src = f"{bucket}/{key}:{etag}:{sequencer}:{event_time}".encode("utf-8")
    return hashlib.sha256(src).hexdigest()[:32]


def _start_step_function(payload: Dict[str, Any]) -> None:
    try:
        SFN.start_execution(
            stateMachineArn=os.environ["state_machine_arn"],
            name=payload["execution_id"],
            input=json.dumps(payload, default=str),
        )
    except SFN.exceptions.ExecutionAlreadyExists:
        # Safe to ignore (at-least-once delivery).
        print(
            json.dumps(
                {
                    "msg": "execution-already-exists",
                    "execution_id": payload.get("execution_id"),
                    "document_id": payload.get("document_id"),
                }
            )
        )


def lambda_handler(event, context):
    # Triggered by SQS, where each message body contains an S3 Event Notification JSON.
    for msg in event.get("Records", []) or []:
        body = msg.get("body")
        if not isinstance(body, str) or not body:
            continue

        for s3_record in _iter_s3_records_from_sqs_body(body):
            bucket, key, etag, sequencer, event_time = _extract_s3_info(s3_record)

            _, dot, ext = key.rpartition(".")
            extension = ext.lower() if dot else ""
            if extension not in SUPPORTED_EXTENSIONS:
                continue

            document_id = _stable_document_id(bucket, key, etag)
            execution_id = _execution_id(bucket, key, etag, sequencer, event_time)
            payload = {
                # Kept for downstream compatibility as a document identifier alias.
                "id": document_id,
                # Expected by InputValidation lambda
                "document_id": document_id,
                # Used as Step Functions execution name so same-key overwrites rerun.
                "execution_id": execution_id,
                "source_s3_uri": f"s3://{bucket}/{key}",
                "source_type": extension,
                "processing_bucket": bucket,
                "source_etag": etag,
                "source_sequencer": sequencer,
                "source_event_time": event_time,
            }

            print(
                json.dumps(
                    {
                        "msg": "kickoff",
                        "execution_id": execution_id,
                        "document_id": document_id,
                        "source_s3_uri": payload["source_s3_uri"],
                        "source_type": extension,
                    }
                )
            )
            _start_step_function(payload)

        # Delete SQS message after processing all embedded S3 records.
        SQS.delete_message(
            QueueUrl=os.environ["sqs_url"],
            ReceiptHandle=msg["receiptHandle"],
        )
