import unittest

from tests.helpers import load_lambda_module, set_default_aws_env


class ChunkBuildXlsxTests(unittest.TestCase):
    def test_prefers_xlsx_row_objects_over_cell_reconstruction(self):
        set_default_aws_env()
        module = load_lambda_module("chunkbuild", "test_chunkbuild_xlsx_module")

        records = [
            {
                "object_id": "sheet:1:table:primary:row:5",
                "object_type": "row",
                "title": "UI機能テスト",
                "text": "Table: UI機能テスト\nRow: 5\nNo: 1\nテスト項目: スタート画面で開始",
                "metadata": {
                    "source_type": "xlsx",
                    "table_id": "sheet:1:table:primary",
                    "table_title": "UI機能テスト",
                    "loc": {"xlsx": {"sheet": "UI機能テスト", "row": 5}},
                },
            },
            {
                "object_id": "sheet:1:table:primary:row:6",
                "object_type": "row",
                "title": "UI機能テスト",
                "text": "Table: UI機能テスト\nRow: 6\nNo: 1\nテスト項目: スタート画面で開始\n期待結果: 開始ボタンが押せなくなる",
                "metadata": {
                    "source_type": "xlsx",
                    "table_id": "sheet:1:table:primary",
                    "table_title": "UI機能テスト",
                    "loc": {"xlsx": {"sheet": "UI機能テスト", "row": 6}},
                },
            },
        ]

        objects, image_descriptions = module._normalize_records(records, "xlsx")
        segments = module._build_xlsx_segments(objects, image_descriptions, max_chars=2000)

        self.assertEqual(len(segments), 1)
        self.assertIn("Row: 6", segments[0]["text"])
        self.assertIn("No: 1", segments[0]["text"])
        self.assertEqual(
            segments[0]["source_object_ids"],
            ["sheet:1:table:primary:row:5", "sheet:1:table:primary:row:6"],
        )


if __name__ == "__main__":
    unittest.main()