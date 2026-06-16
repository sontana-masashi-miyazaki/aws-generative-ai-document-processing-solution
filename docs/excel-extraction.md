# Excel Extraction Details

対象コード: `deploy_code/xlsxextract/lambda_function.py`

このドキュメントは、`xlsxextract` の **入力イベント、S3 出力規約、抽出ロジック、後続 Lambda との契約、異常系、非対応要素の影響** を実装ベースで整理したものです。

## 1. Lambda の入力イベント

`xlsxextract` は Step Functions から、少なくとも次の情報を受け取る前提です。

### 必須

- `document_id`
- `source_type` (`xlsx` であること)
- `processing_bucket`
- `source_s3_uri` **または** `source_bucket` + `source_key`

### 条件付き必須

- `work_prefix` が未指定なら `pipeline_version` が必要
- `structured_prefix` / `assets_images_prefix` は未指定でもよく、その場合は `work_prefix` から自動計算される

### 実際に受ける入力例

```json
{
  "document_id": "sample-001",
  "source_type": "xlsx",
  "source_s3_uri": "s3://processing-bucket/uploads/sample.xlsx",
  "source_bucket": "processing-bucket",
  "source_key": "uploads/sample.xlsx",
  "processing_bucket": "processing-bucket",
  "bucket": "processing-bucket",
  "pipeline_version": "pipeline_v1",
  "work_prefix": "work/pipeline_v1/7f/sample-001",
  "structured_prefix": "work/pipeline_v1/7f/sample-001/structured",
  "assets_images_prefix": "work/pipeline_v1/7f/sample-001/assets/images"
}
```

## 2. Lambda の戻り値

`xlsxextract` 自体の戻り値は、**入力 event に S3 出力先を追記したもの**です。

### `xlsxextract` の直接の戻り値

```json
{
  "document_id": "sample-001",
  "source_type": "xlsx",
  "work_prefix": "work/pipeline_v1/7f/sample-001/",
  "structured_prefix": "work/pipeline_v1/7f/sample-001/structured/",
  "assets_images_prefix": "work/pipeline_v1/7f/sample-001/assets/images/",
  "structured_outputs": {
    "document_manifest": {
      "bucket": "processing-bucket",
      "key": "work/pipeline_v1/7f/sample-001/structured/document_manifest.json"
    },
    "objects": {
      "bucket": "processing-bucket",
      "key": "work/pipeline_v1/7f/sample-001/structured/objects.jsonl"
    },
    "assets_manifest": {
      "bucket": "processing-bucket",
      "key": "work/pipeline_v1/7f/sample-001/structured/assets_manifest.json"
    }
  }
}
```

### `ExtractResultValidation` 後の event

次段で `assets_images_count` が補われます。

```json
{
  "document_id": "sample-001",
  "source_type": "xlsx",
  "bucket": "processing-bucket",
  "work_prefix": "work/pipeline_v1/7f/sample-001/",
  "structured_prefix": "work/pipeline_v1/7f/sample-001/structured/",
  "assets_images_count": 3
}
```

## 3. S3 出力パス仕様

### 3.1 基本ルール

`xlsxextract` は次の優先順位で出力 prefix を決めます。

1. `work_prefix` が event にあればそれを使う
2. なければ `pipeline_version`・`source_filename`・`document_id` から計算する

計算式:

```text
hash_prefix = sha256(document_id).hexdigest()[:2]
work_prefix = work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}
```

**tenant_id は使っていません。**

- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/objects.jsonl`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/assets_manifest.json`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/document_manifest.json`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/assets/images/{filename}`

各 artifact の中身の共通仕様は [output-artifacts.md](output-artifacts.md) を参照してください。

### 3.2 `document_id` / `pipeline_version`

- 同じ `document_id` と `pipeline_version` なら同じ場所に上書きする
- `pipeline_version` を変えると別 prefix に分離できる

### 3.3 画像ファイル名の規則

`_extract_images()` は `xl/media/*` の basename をそのまま使います。

例:

- `xl/media/image1.png` → `assets/images/image1.png`

したがって、同じ workbook を同じ prefix に再処理すると同じ key を上書きします。

## 4. sheet 番号と object_id 採番ルール

### 4.1 sheet 番号

sheet index は `workbook.xml` に並ぶ sheet 順の **1 始まり**です。

- `sheet:1` = workbook 先頭の sheet
- `sheet:2` = 2 番目の sheet

### 4.2 object_id

- セル: `sheet:{sheet_index}:cell:{cell_ref}`
- 表: `sheet:{sheet_index}:table:primary`
- 画像: `sheet:{sheet_index}:image:{n}`

セルは A1 参照をそのまま ID に使います。  
表 object は、sheet 内で header row を検出できた場合に 1 つ生成します。  
画像の `n` は `_drawing_images()` が返した画像配列の **1 始まり**です。

### 4.3 順序の安定性

抽出順は次の影響を受けます。

1. workbook 上の sheet 順
2. 各 sheet XML の cell 出現順
3. 画像はセル抽出の後に追加する

そのため、シート順の変更や再保存で XML 順が変わると、object 順や一部 ID の意味が変わる可能性があります。

## 5. XML をどう解析しているか

`.xlsx` は ZIP として開き、Workbook からシート一覧を解決して、各 sheet XML を `xml.etree.ElementTree` で走査します。  
shared strings は `xl/sharedStrings.xml` を先に読み、`t="s"` のセル参照を文字列へ戻します。  
画像は drawing relationship をたどって anchor 情報と `xl/media/*` を解決します。

## 6. 生データの抽出

### 6.1 シート一覧

`_workbook_sheets()` が workbook と relationship から sheet 名と path を決めます。

### 6.2 セル

`_sheet_cells()` は `c` 要素を走査し、次のルールで文字列化します。

- `t="s"`: shared strings 参照
- `t="inlineStr"`: inline string
- それ以外: `<v>` の生値

内部データ:

```json
{
  "ref": "B12",
  "text": "2026-05-16"
}
```

この生セル列から、extractor は後段で次も推定します。

- `row` / `col`
- header row があるか
- table とみなせる範囲
- noise 列（`index`, `Unnamed:*`, 空 header）かどうか

### 6.3 table 推定

`xlsxextract` は sheet ごとに **primary table** を 1 つだけ best-effort で推定します。

- 2 列以上の header 候補を持つ row を探す
- `index`, `Unnamed:*`, 空文字、数値だけの cell は header 候補から外す
- 同じ列帯に後続 row が続くかを見て、header row / data row 範囲を決める

この推定結果は canonical object に残しますが、複数 table の厳密検出まではまだ行いません。

### 6.4 画像

`_drawing_images()` が `drawing` relationship をたどり、`oneCellAnchor` / `twoCellAnchor` から位置を拾います。

```json
{
  "sheet": "Sheet1",
  "name": "image1.png",
  "s3_key": "work/.../assets/images/image1.png",
  "anchor": {
    "from": {"col": 2, "row": 4},
    "to": {"col": 5, "row": 12}
  },
  "xlsx_path": "xl/media/image1.png"
}
```

`anchor.from` / `anchor.to` の `col`, `row` はコード上で **1 加算**しているので、**1 始まり**です。

## 7. セル値変換ルール

現在の実装は「文字列化」を優先しています。

- 数式を評価しない
- セル書式から日付・通貨・パーセントを意味解釈しない
- `<v>` の値をそのまま文字列として扱う

そのため、Excel 上で見える表示文字列と extractor の text が一致しないことがあります。

## 8. どう整形しているか

- セル → `object_type: cell`
- 推定できた表 → `object_type: table`
- 画像 → `object_type: image`

位置情報は `loc.xlsx` に残します。

- セル: `{"xlsx": {"sheet": "Sheet1", "cell": "B12", "row": 12, "col": 2}}`
- 表: `{"xlsx": {"sheet": "Sheet1", "range": "B2:D13", "header_row": 2, "row_start": 3, "row_end": 13}}`
- 画像: `{"xlsx": {"sheet": "Sheet1", "anchor": {...}}}`

また、table 内の cell には次の補助 metadata が付くことがあります。

- `table_id`
- `table_role` (`header` / `data` / `noise`)
- `header`
- `search_excluded`

## 9. 最終出力

### `objects.jsonl`

セル:

```json
{
  "object_id": "sheet:1:cell:B12",
  "object_type": "cell",
  "text": "2026-05-16",
  "metadata": {
    "source_type": "xlsx",
    "loc": {
      "xlsx": {
        "sheet": "Sheet1",
        "cell": "B12",
        "row": 12,
        "col": 2
      }
    }
  }
}
```

表:

```json
{
  "object_id": "sheet:1:table:primary",
  "object_type": "table",
  "text": "売上サマリ",
  "metadata": {
    "source_type": "xlsx",
    "table_id": "sheet:1:table:primary",
    "headers": ["月", "売上", "前年差"],
    "source_object_ids": [
      "sheet:1:cell:B3",
      "sheet:1:cell:C3",
      "sheet:1:cell:D3"
    ],
    "loc": {
      "xlsx": {
        "sheet": "売上サマリ",
        "range": "B2:D13",
        "header_row": 2,
        "row_start": 3,
        "row_end": 13
      }
    }
  }
}
```

画像:

```json
{
  "object_id": "sheet:1:image:1",
  "object_type": "image",
  "metadata": {
    "source_type": "xlsx",
    "loc": {
      "xlsx": {
        "sheet": "Sheet1",
        "anchor": {
          "from": {"col": 2, "row": 4},
          "to": {"col": 5, "row": 12}
        }
      }
    },
    "s3_key": "work/.../assets/images/image1.png",
    "s3_uri": "s3://processing-bucket/work/.../assets/images/image1.png",
    "openxml_path": "xl/media/image1.png",
    "content_type": "image/png",
    "name": "image1.png"
  }
}
```

### `document_manifest.json` / `assets_manifest.json`

`xlsxextract` も必ず次を出します。

- `structured/document_manifest.json`
- `structured/assets_manifest.json`

## 10. 後続 Lambda との契約

### 10.1 `extractresultvalidation`

期待されるのは:

- `structured/objects.jsonl`
- `structured/assets_manifest.json`
- `structured/document_manifest.json`
- `document_manifest.counts.object_count > 0`

### 10.2 `bedrockenrichment`

主に必要なのは:

- `assets.images[*].s3_key`
- `assets.images[*].s3_uri`
- image object の `metadata.s3_key`

### 10.3 `chunkbuild`

主に参照するのは:

- `object_id`
- `object_type`
- `text`
- `metadata.source_type`
- `metadata.loc`
- `metadata.table_id`
- `metadata.table_role`
- `metadata.header`
- `metadata.search_excluded`

画像 object は enrichment なしでは chunk 化されません。  
xlsx では `table_role=data` の cell を使って `table_row_block` chunk を作り、`search_excluded=true` の列は検索 chunk から除外します。

## 11. エラー時の挙動

### Lambda が失敗するケース

- 必須入力不足
- `source_type != "xlsx"`
- S3 取得失敗
- ZIP 破損
- XML parse error
- 画像 S3 保存失敗
- 最終 JSON / JSONL 書き込み失敗

### 継続するケース

- `sharedStrings.xml` がない → shared strings 空配列で継続
- workbook に sheet がない / path 解決できない → object 0 件で継続しうる
- drawing rels がない → 画像だけ欠落しうる
- 空 sheet → object 0 件のまま継続

### 後続で失敗するケース

object 0 件だと `xlsxextract` 自体は成功しうるものの、`extractresultvalidation` が `object_count <= 0` で失敗にします。

## 12. ログ・メトリクス

現在出しているログは 1 件です。

```json
{
  "msg": "openxml-xlsx-extracted",
  "document_id": "sample-001",
  "object_count": 42,
  "asset_count": 3,
  "structured_prefix": "work/...",
  "assets_images_prefix": "work/.../assets/images"
}
```

現在出していないもの:

- sheet_count
- cell / table / image object 数
- truncated sheet 数
- warning 件数
- 処理時間
- CloudWatch custom metrics

## 13. セキュリティ・権限

CDK 側の IAM では、`xlsxextract` は processing bucket に対して次を持ちます。

- 読み取り: `uploads/*`
- 読み書き: `work/*`

画像は後続で Bedrock に渡る可能性があるため、機密 workbook では `assets/images/*` 複製と Bedrock 送信可能性を考慮してください。

## 14. 非対応要素と RAG への影響

| 非対応要素 | 影響 |
| --- | --- |
| 数式評価 | 計算結果ではなく生値ベースになる可能性 |
| セル書式意味 | 日付・通貨・パーセントの意味差が落ちる |
| merged cell 構造 | 見た目上のレイアウト意味が落ちる |
| 複数 table の厳密検出 | 1 sheet 内で primary table 以外は note row 扱いに寄る可能性 |
| シート間参照 | 関係性を保持しない |
| 近接・親子構造 | セルと画像の近接関係やシート内の論理ブロックを独立 artifact としては保持しない |
