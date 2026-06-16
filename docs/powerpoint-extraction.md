# PowerPoint Extraction Details

対象コード: `deploy_code/pptxextract/lambda_function.py`

このドキュメントは、`pptxextract` の **入力イベント、S3 出力規約、抽出ロジック、後続 Lambda との契約、異常系、非対応要素の影響** を実装ベースで整理したものです。

## 1. Lambda の入力イベント

`pptxextract` は Step Functions から、少なくとも次の情報を受け取る前提です。

### 必須

- `document_id`
- `source_type` (`pptx` であること)
- `processing_bucket`
- `source_s3_uri` **または** `source_bucket` + `source_key`

### 条件付き必須

- `work_prefix` が未指定なら `pipeline_version` が必要
- `structured_prefix` / `assets_images_prefix` は未指定でもよく、その場合は `work_prefix` から自動計算される

### 実際に受ける入力例

```json
{
  "document_id": "sample-001",
  "source_type": "pptx",
  "source_s3_uri": "s3://processing-bucket/uploads/sample.pptx",
  "source_bucket": "processing-bucket",
  "source_key": "uploads/sample.pptx",
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

`pptxextract` 自体の戻り値は、**入力 event をベースに S3 出力先を追記したもの**です。  
`objects_key` や `assets_images_count` をトップレベルで返しているわけではありません。

### `pptxextract` の直接の戻り値

```json
{
  "document_id": "sample-001",
  "source_type": "pptx",
  "source_s3_uri": "s3://processing-bucket/uploads/sample.pptx",
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
  "source_type": "pptx",
  "bucket": "processing-bucket",
  "work_prefix": "work/pipeline_v1/7f/sample-001/",
  "structured_prefix": "work/pipeline_v1/7f/sample-001/structured/",
  "assets_images_count": 3,
  "structured_outputs": {
    "objects": {
      "bucket": "processing-bucket",
      "key": "work/pipeline_v1/7f/sample-001/structured/objects.jsonl"
    }
  }
}
```

つまり、**`assets_images_count` は `pptxextract` の責務ではなく `extractresultvalidation` の責務**です。

## 3. S3 出力パス仕様

### 3.1 基本ルール

`pptxextract` は次の優先順位で出力 prefix を決めます。

1. `work_prefix` が event にあればそれを使う
2. なければ `pipeline_version`・`source_filename`・`document_id` から計算する

計算式はコード上こうです。

```text
hash_prefix = sha256(document_id).hexdigest()[:2]
work_prefix = work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}
```

**tenant_id は使っていません。**  
現状のパス仕様は次です。

- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/objects.jsonl`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/assets_manifest.json`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/structured/document_manifest.json`
- `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/assets/images/{filename}`

各 artifact の中身の共通仕様は [output-artifacts.md](output-artifacts.md) を参照してください。

### 3.2 `document_id` の扱い

- `document_id` は upstream が決めて渡す識別子です
- extractor 自身は `document_id` を再生成しません
- 同じ `document_id` と同じ `pipeline_version` で再実行すると、**同じ `work_prefix` に再出力して上書き**します

### 3.3 `pipeline_version` の扱い

- `pipeline_version` は prefix 名前空間の一部です
- 抽出仕様変更や再処理系統を分けたい場合は、`pipeline_version` を変えることで別 prefix にできます
- 逆に同じ `pipeline_version` のまま再処理すると、同じ場所に出力します

### 3.4 画像ファイル名の規則

`_upload_media()` は `ppt/media/*` の **basename** をそのまま使います。

例:

- `ppt/media/image1.png` → `assets/images/image1.png`
- `ppt/media/image3.jpeg` → `assets/images/image3.jpeg`

影響:

- 同じ deck 内で同じ media basename が再利用されれば、同じ S3 key を共有します
- 同じ `document_id` / `pipeline_version` で再実行すると、同じ key に上書きされます
- スライドごとの連番やハッシュ命名はしていません

## 4. slide 番号と object_id 採番ルール

### 4.1 slide 番号

`metadata.loc.pptx.slide` は、**PowerPoint 上の表示順に基づく 1 始まりのスライド番号**です。

これは `ppt/presentation.xml` の `sldId` 順と `presentation.xml.rels` から求めています。  
したがって:

- `slide:1` = 先頭スライド
- `slide:2` = 2 枚目スライド

**`ppt/slides/slide2.xml` と一致するとは限りません。**

### 4.2 object_id

各 object は slide 単位で次のような ID を持ちます。

- テキスト: `slide:{slide_index}:text:{n}`
- 画像: `slide:{slide_index}:image:{n}`

ただし `n` は **テキストだけの連番 / 画像だけの連番ではありません**。  
実装上は `s.get("elements")` 全体を `enumerate(..., start=1)` しているので、同一 slide 内の **element 列全体に対する通し番号**です。

例:

- 1 番目の要素が text → `slide:2:text:1`
- 2 番目の要素が image → `slide:2:image:2`

### 4.3 順序の安定性

抽出順は次の影響を受けます。

1. `.//{*}sp` の XML 出現順
2. `.//{*}pic` の XML 出現順
3. ただし実装は **text shape を先に全部集め、その後に picture shape を全部集める**

そのため、**PowerPoint の z-order や前面/背面順を厳密には反映していません。**  
また、再保存・編集・group 化の変更で XML 順や relationship ID が変われば、`object_id` が変わる可能性があります。

## 5. bbox_emu の座標仕様

`bbox_emu` は shape の OpenXML transform から取る座標です。

```json
{
  "x": 1200000,
  "y": 900000,
  "cx": 3400000,
  "cy": 1800000
}
```

意味:

| Field | 意味 |
| --- | --- |
| `x` | スライド左上からの X 座標 |
| `y` | スライド左上からの Y 座標 |
| `cx` | 幅 |
| `cy` | 高さ |

単位:

- `1 inch = 914400 EMU`
- `1 cm = 360000 EMU`

注意:

- 現在の extractor は **スライド全体サイズ (`slide_size_emu`) を出力していません**
- そのため、相対座標への変換は downstream では直接できません
- `xfrm/off/ext` がない shape は `bbox_emu = null` になります

## 6. テキスト連結ルール

テキスト shape は `_shape_text()` が `.//{*}txBody//{*}t` をすべて拾って **単純連結**します。

実装上のルール:

- run 間に区切り文字は入れない
- 段落境界でも明示的な改行は入れない
- 箇条書き記号や段落レベルは保持しない
- 前後空白は `.strip()` で除去する
- 空文字になった shape は出力しない

つまり、次の情報は現在落ちます。

- タイトル / 本文 / フッターの役割
- 箇条書き階層
- 改行位置
- フォントサイズ、太字、色などの style
- プレースホルダ種別

RAG 観点では、「タイトル」「注記」「本文」を別 object type として扱っていないため、**意味差は平坦化**されます。

## 7. 画像保存・rels 解決ルール

### 7.1 media 保存

`_upload_media()` は `ppt/media/*` 配下の全ファイルを S3 にそのまま保存します。

保持する情報:

- basename
- `content_type`
- `s3_key`
- `browser_supported`
- `openxml_path`

対応 MIME 推定対象:

- `png`
- `jpg` / `jpeg`
- `gif`
- `webp`
- `bmp`
- `tif` / `tiff`
- `emf`
- `wmf`

### 7.2 relationship 解決

slide 内の picture は `.rels` の `Target` を `_resolve_rel()` で解決します。  
相対 path の場合は、**slide XML のある `ppt/slides/` を基準に正規化**します。

例:

- slide path: `ppt/slides/slide2.xml`
- target: `../media/image3.jpeg`
- 解決結果: `ppt/media/image3.jpeg`

### 7.3 どの画像が Bedrock に渡るか

Bedrock enrichment は `assets_manifest.json` の `assets.images[*].s3_key` を参照します。  
したがって、**`ppt/media/*` に保存された画像だけが enrichment 対象**です。

### 7.4 表示見た目との差

現在は **元の `ppt/media/*` バイナリをそのまま保存**します。  
PowerPoint 上の表示変形は反映しません。

未反映:

- crop
- rotation
- transparency
- mask
- flip
- 各種エフェクト

そのため、S3 上の画像と PowerPoint 画面上の見た目は一致しない場合があります。

## 8. 非対応要素と RAG への影響

現状 `pptxextract` が主に扱うのは **text shape** と **picture shape** だけです。

| 非対応要素 | 影響 |
| --- | --- |
| ノート | 発表者メモ内の説明は RAG 対象外 |
| SmartArt | 図解内テキストや階層関係が落ちる可能性 |
| 表 | セル構造・行列関係を保持できない |
| アニメーション | 段階表示や説明順の意味が落ちる |
| グループ化の意味 | 図とテキストのまとまりを relation として残せない |
| コネクタ / 矢印 | 因果・関係性を抽出できない |

補足:

- `.//{*}sp` / `.//{*}pic` の再帰探索なので、group 内部の text/picture 自体は拾える場合があります
- ただし **group 構造そのもの** は保存しません

## 9. 後続 Lambda との契約

### 9.1 `extractresultvalidation`

この Lambda は次を期待します。

- `structured/objects.jsonl`
- `structured/assets_manifest.json`
- `structured/document_manifest.json`
- `document_manifest.counts.object_count > 0`

つまり `pptxextract` が最低保証すべきのは:

- 必須 3 ファイルが存在すること
- `object_count` が正しいこと

### 9.2 `bedrockenrichment`

この Lambda は次を参照します。

- `structured/assets_manifest.json`
- `structured/objects.jsonl`

期待する主なフィールド:

- `assets.images[*].s3_key`
- `assets.images[*].s3_uri` または同等の画像位置
- object 群そのもの

したがって `pptxextract` で画像 object の `metadata.s3_key` / `metadata.s3_uri` / `metadata.openxml_path` を壊すと、画像の追跡や説明付与の整合が落ちます。

### 9.3 `chunkbuild`

`chunkbuild` は object JSONL を上から順に読み、主に次を使います。

- `object_id`
- `object_type`
- `text`
- `metadata.source_type`
- `metadata.loc`

PPTX text object では `text` が必須です。  
image object は `text` を持たないので、enrichment なしでは chunk 化されません。

## 10. エラー時の挙動

### extractor 自体が失敗するケース

次は原則として例外になり、Lambda 失敗になります。

- 入力 event に必須フィールド不足
- `source_type != "pptx"`
- S3 から元 PPTX を取得できない
- ZIP が壊れていて `zipfile.ZipFile(...)` が失敗
- XML parse error (`ElementTree.fromstring`)
- `ppt/media/*` の読み出し失敗
- S3 への画像保存失敗
- JSON / JSONL の最終書き込み失敗

### 失敗ではなく継続するケース

次は **警告相当だが Lambda 自体は継続**です。

- `presentation.xml` がない → slide list が空になる
- slide XML が欠けている → その slide は `warnings=["slide not found"]` になるが、最終的に warnings は外へ出していない
- slide rels が欠けている → 画像 `target_path` / `s3_key` が取れないことがある
- 空スライド → object 0 件でその slide はスキップ
- テキストも画像もない slide → object 0 件で継続

### 後続で失敗に変わるケース

deck 全体で object が 0 件だと `pptxextract` 自体は成功しうるものの、  
次段の `extractresultvalidation` が `counts.object_count <= 0` を見て失敗にします。

つまり **抽出 Lambda の成功 = RAG で十分な情報が取れた、ではありません。**

## 11. ログ・メトリクス

`pptxextract` が現在出しているログは 1 件だけです。

```json
{
  "msg": "openxml-pptx-extracted",
  "document_id": "sample-001",
  "object_count": 12,
  "asset_count": 3,
  "structured_prefix": "work/...",
  "assets_images_prefix": "work/.../assets/images"
}
```

現在 **出していない**もの:

- slide_count
- text object 数
- image object 数
- warning 件数
- スキップした shape 数
- 処理時間
- CloudWatch custom metrics

そのため、品質監視は現状 `object_count` / `asset_count` と downstream の失敗有無に依存します。

## 12. セキュリティ・権限

CDK 側の IAM では、`pptxextract` はメイン processing bucket に対して次を持ちます。

- 読み取り: `uploads/*`
- 読み書き: `work/*`

つまり通常運用では、入力 PPTX は `uploads/` 配下、出力は `work/` 配下に置く前提です。  
`source_bucket` が別 bucket だと、追加権限がなければ `S3.get_object` が失敗します。

また、画像は後続で Bedrock に渡る可能性があります。  
機密資料や社外秘スライドを扱う場合は、**画像アセットが `assets/images/*` に複製されること**と **Bedrock 送信対象になりうること** を前提にデータ分類を検討してください。

## 13. document_manifest / assets_manifest の実際

`pptxextract` は `objects.jsonl` だけでなく、次も必ず出します。

- `structured/document_manifest.json`
- `structured/assets_manifest.json`

ただし、現状の `document_manifest.json` に **入っていない**ものもあります。

未記録:

- `slide_count`
- `created_at`
- `warnings`
- `text_object_count`
- `image_object_count`

入っているのは主に次です。

- `document_id`
- `source.source_type`
- source bucket/key/uri
- 出力先 key 群
- `counts.object_count`
- `counts.asset_count`

## 14. 実装の再現性に関する要点

再現性に関して重要なのは次です。

1. 同じ `document_id` と `pipeline_version` は同じ prefix を使う
2. 同じ prefix では既存出力を上書きする
3. `slide` 番号は表示順ベースの 1 始まり
4. `object_id` は XML 順 + 実装の text-first / image-second 探索順に依存する
5. shape 間の近接や親子関係は別 artifact としては保存しない

そのため、この extractor は **再処理時の key 安定性は高い一方、編集後の PPTX に対する object_id の意味安定性までは保証しません。**
