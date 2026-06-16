import json
import os
import unittest
from unittest.mock import patch

from tests.helpers import load_lambda_module, set_default_aws_env


class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _FailedTextract:
    def start_document_text_detection(self, DocumentLocation):
        return {"JobId": "job-123"}

    def get_document_text_detection(self, JobId, MaxResults=None, NextToken=None):
        return {"JobStatus": "FAILED"}


class _Context:
    def get_remaining_time_in_millis(self):
        return 60_000


class PdfExtractRegressionTest(unittest.TestCase):
    def test_writes_failure_artifacts_before_raising(self) -> None:
        set_default_aws_env()
        module = load_lambda_module("pdfextract", "test_pdfextract_lambda")
        fake_s3 = _FakeS3()
        module.S3 = fake_s3

        event = {
            "document_id": "document-1",
            "source_bucket": "source-bucket",
            "source_key": "uploads/sample.pdf",
            "source_s3_uri": "s3://source-bucket/uploads/sample.pdf",
            "processing_bucket": "processing-bucket",
            "structured_prefix": "work/pipeline_v1/hash/sample/structured",
            "structured_objects_key": "work/pipeline_v1/hash/sample/structured/objects.jsonl",
            "structured_assets_manifest_key": "work/pipeline_v1/hash/sample/structured/assets_manifest.json",
            "document_manifest_key": "work/pipeline_v1/hash/sample/structured/document_manifest.json",
            "assets_images_prefix": "work/pipeline_v1/hash/sample/assets/images",
        }

        with patch.dict(os.environ, {"TEXTRACT_PDF_POLL_SECONDS": "0"}, clear=False):
            with patch.object(module, "_get_textract_client", return_value=_FailedTextract()):
                with self.assertRaises(module.PdfExtractionError):
                    module.lambda_handler(event, _Context())

        written_by_key = {item["Key"]: item for item in fake_s3.puts}
        self.assertIn(event["structured_objects_key"], written_by_key)
        self.assertIn(event["document_manifest_key"], written_by_key)
        self.assertIn(event["structured_assets_manifest_key"], written_by_key)

        manifest = json.loads(written_by_key[event["document_manifest_key"]]["Body"].decode("utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("textract_failed", manifest["errors"])
        self.assertEqual(manifest["counts"]["object_count"], 0)

        assets_manifest = json.loads(
            written_by_key[event["structured_assets_manifest_key"]]["Body"].decode("utf-8")
        )
        self.assertEqual(assets_manifest["assets"]["images"], [])


if __name__ == "__main__":
    unittest.main()