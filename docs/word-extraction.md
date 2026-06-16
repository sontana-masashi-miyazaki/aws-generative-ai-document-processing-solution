# Word Extraction Details

対象コード: `deploy_code/docxextract/lambda_function.py`

このドキュメントは、`docxextract` の **入力イベント、S3 出力規約、抽出ロジック、後続 Lambda との契約、異常系、非対応要素の影響** を実装ベースで整理したものです。

## 1. Lambda の入力イベント

`docxextract` は Step Functions から、少なくとも次の情報を受け取る前提です。

### 必須

- `document_id`
- `source_type` (`docx` であること)
- `processing_bucket`
- `source_s3_uri` **または** `source_bucket` + `source_key`

### 条件付き必須

- `work_prefix` が未指定なら `pipeline_version` が必要
- `structured_prefix` / `assets_images_prefix` は未指定でもよく、その場合は `work_prefix` から自動計算される

### 実際に受ける入力例

```json
{
  "document_id": "sample-001",
  "source_type": "docx",
  "source_s3_uri": "s3://processing-bucket/uploads/sample.docx",
  "source_bucket": "processing-bucket",
  "source_key": "uploads/sample.docx",
  "processing_bucket": "processing-bucket",
  "bucket": "processing-bucket",
  "pipeline_version": "pipeline_v1",
  "work_prefix": "work/pipeline_v1/7f/sample-001",
  "structured_prefix": "work/pipeline_v1/7f/sample-001/structured",
  "assets_images_prefix": "work/pipeline_v1/7f/sample-001/assets/images"
}
```

補足:

- `bucket` は downstream 互換の alias で、extractor 自体は `processing_bucket` を主に使います
- `classification` が来た場合は、抽出 object の `metadata.classification` にそのまま引き継ぎます

## 2. Lambda の戻り値

`docxextract` 自体の戻り値は、**入力 event をベースに S3 出力先を追記したもの**です。

### `docxextract` の直接の戻り値

```json
{
  "document_id": "sample-001",
  "source_type": "docx",
  "source_s3_uri": "s3://processing-bucket/uploads/sample.docx",
  "processing_bucket": "processing-bucket",
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

次段の `extractresultvalidation` を通ると、`assets_images_count` と `bucket` が補われます。

```json
{
  "document_id": "sample-001",
  "source_type": "docx",
  "bucket": "processing-bucket",
  "work_prefix": "work/pipeline_v1/7f/sample-001/",
  "structured_prefix": "work/pipeline_v1/7f/sample-001/structured/",
  "assets_images_count": 2,
  "structured_outputs": {
    "objects": {
      "bucket": "processing-bucket",
      "key": "work/pipeline_v1/7f/sample-001/structured/objects.jsonl"
    }
  }
}
```

つまり、**`assets_images_count` は `docxextract` ではなく `extractresultvalidation` が付与**します。

## 3. S3 出力パス仕様

### 3.1 基本ルール

`docxextract` は次の優先順位で出力 prefix を決めます。

1. `work_prefix` が event にあればそれを使う
2. なければ `pipeline_version`・`source_filename`・`document_id` から計算する

計算式:

```text
hash_prefix = sha256(document_id).hexdigest()[:2]
work_prefix = work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}
```

**tenant_id は使っていません。**  
現状の出力先は次です。

- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/objects.jsonl`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/assets_manifest.json`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/document_manifest.json`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/assets/images/{filename}`

各 artifact の中身の共通仕様は [output-artifacts.md](output-artifacts.md) を参照してください。

### 3.2 `document_id` の扱い

- `document_id` は upstream が決める
- extractor 自身は再生成しない
- 同じ `document_id` と `pipeline_version` で再実行すると、**同じ prefix に上書き**する

### 3.3 `pipeline_version` の扱い

- `pipeline_version` は prefix 名前空間の一部
- 仕様変更や再処理の系統分離には `pipeline_version` を変える
- 同じ `pipeline_version` なら同じ場所へ書く

### 3.4 画像ファイル名の規則

`_upload_media()` は `word/media/*` の **basename** をそのまま使います。

例:

- `word/media/image1.png` → `assets/images/image1.png`
- `word/media/image3.jpeg` → `assets/images/image3.jpeg`

影響:

- 同じ document 内で basename が重なれば同じ S3 key を共有する
- 同じ `document_id` / `pipeline_version` で再実行すると同じ key を上書きする
- 段落番号やテーブル番号を含む命名はしていない

## 4. object_id 採番ルール

Word は `part_id = doc:1` 固定で、要素ごとに次の ID を作ります。

- 段落 text: `doc:1:p:{paragraph_index}`
- 表: `doc:1:tbl:{table_index}`
- 段落内 image: `doc:1:p:{paragraph_index}:img:{n}`
- 表内 image: `doc:1:tbl:{table_index}:img:{n}`

### 4.1 paragraph / table 番号

- paragraph index は `word/body` 直下の `w:p` を見つけた順の **1 始まり**
- table index は `word/body` 直下の `w:tbl` を見つけた順の **1 始まり**

### 4.2 順序の安定性

抽出順は次の影響を受けます。

1. `word/body` の XML 出現順
2. `_build_elements()` による paragraph → table → image 展開順
3. `image_targets` の並び順

そのため、再保存や編集で XML 順が変われば `object_id` も変わりえます。  
ただし、**同じファイル内容・同じ XML 配列なら再現性は高い**です。

## 5. XML をどう解析しているか

`.docx` は ZIP として開き、`word/document.xml` の `body` を `xml.etree.ElementTree` で順番に走査します。  
relationship は `document.xml.rels` を `_rels_map()` で辞書化し、画像 `rel_id` を `word/media/*` に解決します。

## 6. 生データの抽出

`_parse_docx()` は `word/body` 直下から次を拾います。

1. **段落 (`w:p`)**
   - `.//{*}t` を連結してテキスト化
   - `.//{*}blip` / `.//{*}imagedata` から画像 `rel_id` を取得
2. **表 (`w:tbl`)**
   - `tr` / `tc` を走査して `rows: List[List[str]]` を構築
   - 表内画像も同様に relationship 解決

内部生データの例:

```json
{
  "type": "paragraph",
  "index": 4,
  "text": "住所: 東京都...",
  "image_targets": [
    {
      "rel_id": "rId7",
      "docx_path": "word/media/image1.png",
      "s3_key": "work/.../assets/images/image1.png"
    }
  ]
}
```

表の内部データ:

```json
{
  "type": "table",
  "index": 1,
  "rows": [
    ["項目", "値"],
    ["氏名", "山田太郎"]
  ]
}
```

## 7. テキスト連結ルール

段落・セル内テキストは `.//{*}t` をすべて拾って **単純連結**します。

実装上のルール:

- run 間に区切り文字は入れない
- 改行や段落境界は明示的に残さない
- style、bold、font size、色は保持しない
- 空文字は出力しない

影響:

- 見出し・本文・脚注の意味差は現在保持しない
- 改行や箇条書きの構造は落ちる

## 8. どう整形しているか

`_build_elements()` が内部データを順序付き `elements` に変換します。

- 段落 → `type: text`
- 表 → `type: table`
- 段落/表配下の画像 → `type: image`

位置情報は `loc.docx` に残します。

- 段落: `{"docx": {"paragraph": 4}}`
- 表: `{"docx": {"table": 1}}`

## 9. 最終出力

### `objects.jsonl`

テキスト段落:

```json
{
  "object_id": "doc:1:p:4",
  "object_type": "text",
  "text": "住所: 東京都...",
  "metadata": {
    "source_type": "docx",
    "loc": {
      "docx": {
        "paragraph": 4
      }
    }
  }
}
```

表:

```json
{
  "object_id": "doc:1:tbl:1",
  "object_type": "table",
  "metadata": {
    "source_type": "docx",
    "loc": {
      "docx": {
        "table": 1
      }
    },
    "rows": [
      ["項目", "値"],
      ["氏名", "山田太郎"]
    ]
  }
}
```

画像:

```json
{
  "object_id": "doc:1:p:4:img:1",
  "object_type": "image",
  "metadata": {
    "source_type": "docx",
    "loc": {
      "docx": {
        "paragraph": 4
      }
    },
    "s3_key": "work/.../assets/images/image1.png",
    "s3_uri": "s3://processing-bucket/work/.../assets/images/image1.png",
    "openxml_path": "word/media/image1.png",
    "rel_id": "rId7"
  }
}
```

### `document_manifest.json` / `assets_manifest.json`

`docxextract` は次も必ず出力します。

- `structured/document_manifest.json`
- `structured/assets_manifest.json`

主に入るもの:

- `document_id`
- source bucket/key/uri
- 出力先 key 群
- `counts.object_count`
- `counts.asset_count`

## 10. 後続 Lambda との契約

### 10.1 `extractresultvalidation`

次を期待します。

- `structured/objects.jsonl`
- `structured/assets_manifest.json`
- `structured/document_manifest.json`
- `document_manifest.counts.object_count > 0`

### 10.2 `bedrockenrichment`

次を参照します。

- `structured/assets_manifest.json`
- `structured/objects.jsonl`

主に必要なフィールド:

- `assets.images[*].s3_key`
- `assets.images[*].s3_uri`
- image object の `metadata.s3_key`
- image object の `metadata.openxml_path`

### 10.3 `chunkbuild`

主に使うのは次です。

- `object_id`
- `object_type`
- `text`
- `metadata.source_type`
- `metadata.loc`
- table の場合は `metadata.rows`

`chunkbuild` は DOCX table を `metadata.rows` からタブ区切りテキストに再整形します。

## 11. エラー時の挙動

### Lambda が失敗するケース

- 必須入力不足
- `source_type != "docx"`
- S3 から元ファイルを取得できない
- ZIP が壊れている
- XML parse error
- 画像 S3 保存失敗
- 最終 JSON / JSONL 書き込み失敗

### 継続するケース

- `word/document.xml` がない → warnings を返す内部データになるが、外部には warnings を出さず object 0 件になりうる
- `word/body` がない → 同上
- `document.xml.rels` がない → 画像 relationship が解決できず、画像 object の `s3_key` が取れないことがある

### 後続で失敗するケース

object 0 件でも `docxextract` 自体は成功しうるものの、`extractresultvalidation` が `object_count <= 0` で失敗にします。

## 12. ログ・メトリクス

現在出しているログは 1 件です。

```json
{
  "msg": "openxml-docx-extracted",
  "document_id": "sample-001",
  "object_count": 12,
  "asset_count": 2,
  "structured_prefix": "work/...",
  "assets_images_prefix": "work/.../assets/images"
}
```

現在出していないもの:

- paragraph_count
- table_count
- image_count
- warning_count
- 処理時間
- CloudWatch custom metrics

## 13. セキュリティ・権限

CDK 側の IAM では、`docxextract` は processing bucket に対して次を持ちます。

- 読み取り: `uploads/*`
- 読み書き: `work/*`

つまり通常運用では、入力 DOCX は `uploads/` 配下、出力は `work/` 配下です。  
別 bucket を読む場合は追加権限が必要です。

画像は後続で Bedrock へ送られうるため、機密文書では `assets/images/*` 複製と Bedrock 送信可能性を考慮してください。

## 14. 非対応要素と RAG への影響

| 非対応要素 | 影響 |
| --- | --- |
| 見出し階層 | タイトル/本文の意味差が落ちる |
| style 情報 | 強調や注記の重要度が落ちる |
| 脚注 | 補足説明が RAG 対象外になる |
| コメント | レビュー情報が落ちる |
| 変更履歴 | 差分意味を保持できない |
| 近接・親子構造 | 表と前後段落、画像とキャプション、見出しと本文の関係を独立 artifact としては保持しない |
