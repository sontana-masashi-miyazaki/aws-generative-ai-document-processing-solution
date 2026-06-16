import json
import os
import unittest
from unittest.mock import patch

from tests.helpers import load_lambda_module, set_default_aws_env


class _FakeTable:
    def __init__(self):
        self.calls = []

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _FakeDynamoResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):
        self._table.name = name
        return self._table


class UpdateStatusRegressionTest(unittest.TestCase):
    def test_success_status_keeps_optional_failures_and_removes_last_error(self) -> None:
        set_default_aws_env()
        module = load_lambda_module("updatestatus", "test_updatestatus_lambda")
        fake_table = _FakeTable()
        module._DDB = _FakeDynamoResource(fake_table)

        event = {
            "document_id": "document-1",
            "pipeline_version": "pipeline_v1",
            "bucket": "processing-bucket",
            "work_prefix": "work/pipeline_v1/hash/source/document-1",
            "bedrock_enrichment_failure": {
                "failed_step": "bedrock_enrichment",
                "target_status": "ENRICHMENT_FAILED",
                "retryable": True,
            },
            "bedrock_enrichment_error": {
                "Error": "RuntimeError",
                "Cause": "ENRICHMENT_FAILED",
            },
        }

        with patch.dict(os.environ, {"STATUS_TABLE": "document-status"}, clear=False):
            with patch.object(module, "_s3_count_jsonl", side_effect=[3, 2]):
                with patch.object(
                    module,
                    "_s3_get_bytes",
                    return_value=json.dumps(
                        {
                            "images_total": 1,
                            "images_succeeded": 0,
                            "images_failed": 1,
                        }
                    ).encode("utf-8"),
                ):
                    result = module.lambda_handler(event, context=None)

        self.assertEqual(len(fake_table.calls), 1)
        call = fake_table.calls[0]
        self.assertEqual(call["Key"], {"document_id": "document-1"})
        self.assertIn("optional_failures = :of", call["UpdateExpression"])
        self.assertIn("REMOVE last_error", call["UpdateExpression"])
        self.assertEqual(call["ExpressionAttributeValues"][":st"], "INDEXED")
        self.assertEqual(call["ExpressionAttributeValues"][":c"]["chunks"], 3)
        self.assertEqual(call["ExpressionAttributeValues"][":c"]["embeddings"], 2)

        optional_failures = call["ExpressionAttributeValues"][":of"]
        self.assertIn("bedrock_enrichment", optional_failures)
        self.assertEqual(
            optional_failures["bedrock_enrichment"]["error_type"],
            "RuntimeError",
        )
        self.assertEqual(result["update_status"]["status"], "INDEXED")
        self.assertIn("bedrock_enrichment", result["update_status"]["optional_failures"])


if __name__ == "__main__":
    unittest.main()