import io
import json
import os
import unittest
from unittest.mock import patch

from tests.helpers import load_lambda_module, set_default_aws_env


class _FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._objects[(Bucket, Key)])}


class IndexWriterRegressionTest(unittest.TestCase):
    def test_skips_chunks_without_embeddings_and_indexes_the_rest(self) -> None:
        set_default_aws_env()
        module = load_lambda_module("indexwriter", "test_indexwriter_lambda")

        bucket = "processing-bucket"
        work_prefix = "work/pipeline_v1/hash/source/document-1"
        chunks_key = f"{work_prefix}/search/chunks/chunks.jsonl"
        embeddings_key = f"{work_prefix}/vectors/embeddings.jsonl"

        chunks = "\n".join(
            [
                json.dumps(
                    {
                        "chunk_id": "chunk-1",
                        "document_id": "document-1",
                        "title": "A",
                        "text": "first chunk",
                        "metadata": {"kind": "body"},
                    }
                ),
                json.dumps(
                    {
                        "chunk_id": "chunk-2",
                        "document_id": "document-1",
                        "title": "B",
                        "text": "second chunk",
                        "metadata": {"kind": "body"},
                    }
                ),
                json.dumps(
                    {
                        "chunk_id": "chunk-3",
                        "document_id": "document-1",
                        "title": "C",
                        "text": "third chunk",
                        "metadata": {"kind": "body"},
                    }
                ),
            ]
        ) + "\n"
        embeddings = "\n".join(
            [
                json.dumps({"chunk_id": "chunk-1", "vector": [0.1, 0.2]}),
                json.dumps({"chunk_id": "chunk-3", "vector": [0.3, 0.4]}),
            ]
        ) + "\n"

        module._S3 = _FakeS3(
            {
                (bucket, chunks_key): chunks.encode("utf-8"),
                (bucket, embeddings_key): embeddings.encode("utf-8"),
            }
        )

        captured = {}

        def fake_sign_and_post(url, body, region):
            lines = body.decode("utf-8").splitlines()
            docs = [json.loads(lines[index + 1]) for index in range(0, len(lines), 2)]
            captured["url"] = url
            captured["region"] = region
            captured["docs"] = docs
            return 200, json.dumps({"errors": False, "items": [{} for _ in docs]}).encode("utf-8")

        event = {
            "bucket": bucket,
            "work_prefix": work_prefix,
            "document_id": "document-1",
            "pipeline_version": "pipeline_v1",
        }

        with patch.dict(
            os.environ,
            {
                "SEARCH_BACKEND": "aws-opensearch",
                "SEARCH_ENDPOINT": "search.example.com",
                "SEARCH_INDEX": "document-chunks",
                "BULK_MAX_DOCS": "100",
            },
            clear=False,
        ):
            with patch.object(module, "_sign_and_post", side_effect=fake_sign_and_post):
                result = module.lambda_handler(event, context=None)

        self.assertEqual(captured["url"], "https://search.example.com/_bulk")
        self.assertEqual(captured["region"], "ap-northeast-1")
        self.assertEqual([doc["chunk_id"] for doc in captured["docs"]], ["chunk-1", "chunk-3"])
        self.assertEqual(result["index_writer"]["docs_indexed"], 2)
        self.assertEqual(result["index_writer"]["docs_skipped_missing_embedding"], 1)


if __name__ == "__main__":
    unittest.main()