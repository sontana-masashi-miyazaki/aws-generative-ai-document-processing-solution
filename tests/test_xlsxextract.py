import hashlib
import io
import json
import os
import unittest
import zipfile
from unittest.mock import patch

from tests.helpers import load_lambda_module, set_default_aws_env


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def __init__(self, workbook_bytes):
        self._workbook_bytes = workbook_bytes
        self.puts = {}

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(self._workbook_bytes)}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.puts[(Bucket, Key)] = Body


def _build_test_workbook():
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">
  <sheets>
    <sheet name=\"UI機能テスト\" sheetId=\"1\" r:id=\"rId1\"/>
  </sheets>
</workbook>""",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">
  <Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/>
</Relationships>""",
        )
        z.writestr(
            "xl/sharedStrings.xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" count=\"2\" uniqueCount=\"2\">
  <si><t>合格</t><rPh sb=\"0\" eb=\"2\"><t>ゴウカク</t></rPh></si>
  <si><t>保存しない</t><rPh sb=\"0\" eb=\"5\"><t>ホゾンシナイ</t></rPh></si>
</sst>""",
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">
  <sheetData>
    <row r=\"3\">
      <c r=\"B3\" t=\"inlineStr\"><is><t>No</t></is></c>
      <c r=\"C3\" t=\"inlineStr\"><is><t>テスト項目</t></is></c>
      <c r=\"D3\" t=\"inlineStr\"><is><t>期待結果</t></is></c>
      <c r=\"E3\" t=\"inlineStr\"><is><t>開発者チェック</t></is></c>
    </row>
    <row r=\"4\">
      <c r=\"E4\" t=\"inlineStr\"><is><t>合否</t></is></c>
      <c r=\"F4\" t=\"inlineStr\"><is><t>備考</t></is></c>
    </row>
    <row r=\"5\">
      <c r=\"B5\" t=\"inlineStr\"><is><t>1</t></is></c>
      <c r=\"C5\" t=\"inlineStr\"><is><t>スタート画面で開始</t></is></c>
      <c r=\"D5\" t=\"inlineStr\"><is><t>トレイが起動する</t></is></c>
      <c r=\"E5\" t=\"s\"><v>0</v></c>
      <c r=\"F5\" t=\"s\"><v>1</v></c>
    </row>
    <row r=\"6\">
      <c r=\"D6\" t=\"inlineStr\"><is><t>開始ボタンが押せなくなる</t></is></c>
    </row>
  </sheetData>
  <mergeCells count=\"3\">
    <mergeCell ref=\"E3:F3\"/>
    <mergeCell ref=\"B5:B6\"/>
    <mergeCell ref=\"C5:C6\"/>
  </mergeCells>
</worksheet>""",
        )
    return workbook.getvalue()


class XlsxExtractTests(unittest.TestCase):
    def test_ignores_phonetic_text_and_emits_filled_row_objects(self):
        set_default_aws_env()
        module = load_lambda_module("xlsxextract", "test_xlsxextract_module")
        fake_s3 = _FakeS3(_build_test_workbook())
        module.S3 = fake_s3

        event = {
            "document_id": "doc-001",
            "pipeline_version": "pipeline_v1",
            "source_type": "xlsx",
            "processing_bucket": "processing-bucket",
            "source_s3_uri": "s3://processing-bucket/uploads/テストケース.xlsx",
        }

        with patch.dict(os.environ, {}, clear=False):
            module.lambda_handler(event, context=None)

        hash_prefix = hashlib.sha256("doc-001".encode("utf-8")).hexdigest()[:2]
        objects_key = f"work/pipeline_v1/{hash_prefix}/テストケース.xlsx/doc-001/structured/objects.jsonl"
        body = fake_s3.puts[("processing-bucket", objects_key)].decode("utf-8")
        records = [json.loads(line) for line in body.splitlines() if line.strip()]

        text_values = [record.get("text") for record in records if isinstance(record.get("text"), str)]
        self.assertIn("合格", text_values)
        self.assertIn("保存しない", text_values)
        self.assertFalse(any("ゴウカク" in text for text in text_values))
        self.assertFalse(any("ホゾンシナイ" in text for text in text_values))

        row_record = next(
            record
            for record in records
            if record.get("object_type") == "row" and record["metadata"]["loc"]["xlsx"]["row"] == 6
        )
        self.assertIn("No: 1", row_record["text"])
        self.assertIn("テスト項目: スタート画面で開始", row_record["text"])
        self.assertIn("期待結果: 開始ボタンが押せなくなる", row_record["text"])
        self.assertEqual(row_record["metadata"]["source_cells"], ["B5", "C5", "D6"])

        note_cell = next(record for record in records if record.get("object_id") == "sheet:1:cell:F5")
        self.assertEqual(note_cell["metadata"]["header"], "開発者チェック / 備考")

        table_record = next(record for record in records if record.get("object_type") == "table")
        self.assertEqual(
          table_record["metadata"]["source_object_ids"],
          [
            "sheet:1:cell:B5",
            "sheet:1:cell:C5",
            "sheet:1:cell:D5",
            "sheet:1:cell:E5",
            "sheet:1:cell:F5",
            "sheet:1:cell:D6",
          ],
        )


if __name__ == "__main__":
    unittest.main()