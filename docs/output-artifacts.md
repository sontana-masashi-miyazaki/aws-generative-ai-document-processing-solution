# Output Artifacts

このドキュメントは、抽出系 Lambda、`bedrockenrichment`、`chunkbuild`、`chunkenrichment`、`embedding` が S3 に書き出す中間成果物の **出力仕様** をまとめたものです。  
対象は DOCX / XLSX / PPTX / PDF extractor から、検索用 chunk / embedding 生成までを含みます。

## 1. 出力先

共通 prefix は次です。

```text
work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/
```

`source_filename` はアップロード元 S3 key の **basename** を使います。`/` や `\` など path 区切りに使えない文字だけ `_` に置き換え、それ以外の日本語や拡張子は可能な限り残します。

主な出力先:

- `structured/`
- `search/chunks/`
- `vectors/`

主なファイル:

- `objects.jsonl`
- `assets_manifest.json`
- `document_manifest.json`
- `enriched_objects.jsonl`
- `enrichment_manifest.json`
- `search/chunks/chunks.jsonl`
- `search/chunks/enriched_chunks.jsonl`
- `search/chunks/chunk_enrichment_manifest.json`
- `vectors/embeddings.jsonl`

画像アセット本体は次に保存されます。

```text
work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/assets/images/{filename}
```

同じ `document_id` と `pipeline_version` で再処理すると、これらの key は **上書き** されます。

## 2. ファイル一覧

| ファイル | 生成ステップ | 役割 | 後続の主な利用先 |
| --- | --- | --- | --- |
| `objects.jsonl` | 各 extractor | 抽出した cell / table / text / image object の本体 | `extractresultvalidation`, `bedrockenrichment`, `chunkbuild` |
| `assets_manifest.json` | 各 extractor | 画像アセット一覧 | `extractresultvalidation`, `bedrockenrichment` |
| `document_manifest.json` | 各 extractor | 抽出結果サマリ | `extractresultvalidation` |
| `enriched_objects.jsonl` | `bedrockenrichment` | `objects.jsonl` に画像説明 object を追加した JSONL | `chunkbuild` |
| `enrichment_manifest.json` | `bedrockenrichment` | enrichment の成功/失敗サマリ | 運用確認, `updatestatus` の件数集計 |
| `search/chunks/chunks.jsonl` | `chunkbuild` | 原文ベースの検索単位 | `chunkenrichment`, `embedding`, `updatestatus` |
| `search/chunks/enriched_chunks.jsonl` | `chunkenrichment` | summary / keywords / aliases / entities / `embedding_text` を付けた派生 chunk | `embedding` |
| `search/chunks/chunk_enrichment_manifest.json` | `chunkenrichment` | chunk enrichment の集計、version、LLM 失敗 chunk の記録 | 運用確認 |
| `vectors/embeddings.jsonl` | `embedding` | chunk ごとのベクトル | `indexwriter` |

## 3. `objects.jsonl`

1 行 1 object の JSONL です。
Office extractor（DOCX / XLSX / PPTX）は主に `object_id` / `object_type` / `metadata` ベースの shape を出し、PDF extractor は `id` / `type` / `loc` ベースの shape を出します。
後続の `chunkbuild` は両方を正規化して読めるため、厳密なキー名よりも、`text`、`rows`、`loc`、`source_type`、`s3_key` などの実質フィールドが契約上重要です。

### 3.1 cell object

```json
{
  "object_id": "sheet:1:cell:A1",
  "object_type": "cell",
  "text": "売上実績",
  "metadata": {
    "source_type": "xlsx",
    "loc": {
      "xlsx": {
        "sheet": "Sheet1",
        "cell": "A1",
        "row": 1,
        "col": 1
      }
    }
  }
}
```

### 3.2 table object

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

### 3.3 image object

XLSX では、cell / table に加えて、表の 1 行を検索向けに再構成した `row` object が追加されることがあります。

```json
{
  "object_id": "sheet:1:table:primary:row:5",
  "object_type": "row",
  "title": "UI機能テスト",
  "text": "Table: UI機能テスト\nRow: 5\nNo: 1\nテスト項目: スタート画面で開始",
  "metadata": {
    "source_type": "xlsx",
    "table_id": "sheet:1:table:primary",
    "table_title": "UI機能テスト",
    "table_headers": ["No", "テスト項目", "期待結果", "開発者チェック / 合否", "開発者チェック / 備考"],
    "source_cells": ["B5", "C5", "D5"],
    "source_object_ids": [
      "sheet:1:cell:B5",
      "sheet:1:cell:C5",
      "sheet:1:cell:D5"
    ],
    "fields": [
      {"header": "No", "value": "1", "cell": "B5", "source_cell": "B5"}
    ],
    "loc": {
      "xlsx": {
        "sheet": "UI機能テスト",
        "row": 5
      }
    }
  }
}
```

### 3.4 image object

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
          "from": {
            "col": 2,
            "row": 3
          },
          "to": {
            "col": 4,
            "row": 10
          }
        }
      }
    },
    "s3_key": "work/pipeline_v1/ab/sample-001/assets/images/image1.png",
    "s3_uri": "s3://processing-bucket/work/pipeline_v1/ab/sample-001/assets/images/image1.png",
    "openxml_path": "xl/media/image1.png",
    "content_type": "image/png",
    "name": "image1.png"
  }
}
```

### 3.5 契約

- Office extractor の object は主に `object_id` / `object_type` / `metadata` を持つ
- PDF extractor の object は現在 `id` / `type` / `loc` / `text` ベースで、Office extractor と完全な同一 shape ではない
- `text` は text 系 object に入るが、DOCX の table object は本文を `metadata.rows` で持つ
- `chunkbuild` は上記の差分を吸収して正規化する
- `metadata.source_type` は Office extractor では各 object に入る
- `metadata.loc` の中身はフォーマットごとに異なる
- xlsx の cell object は `loc.xlsx.row` / `loc.xlsx.col` を持つ
- xlsx の table object は `metadata.headers` と `loc.xlsx.range` / `header_row` / `row_start` / `row_end` を持つ
- xlsx の table object は複数 header row を検出した場合、cell 側に `metadata.header` と `metadata.table_role=header|data|noise` が付く
- xlsx の row object は merged cell を補完した検索向け表現で、`metadata.source_cells` / `metadata.source_object_ids` / `metadata.fields` を持つ
- xlsx の noise 列（`index`, `Unnamed:*`, 空 header）は `objects.jsonl` には残すが、対象 cell に `metadata.search_excluded=true` が付きうる
- xlsx の image object の `loc.xlsx.anchor` は A1 形式文字列ではなく `from` / `to` を持つ辞書
- image object は `metadata.s3_key` / `metadata.s3_uri` / `metadata.openxml_path` を持つ

`chunkbuild` はこの JSONL から text を取り出します。  
`bedrockenrichment` は image object 自体ではなく、主に `assets_manifest.json` から画像一覧を引きます。

## 4. `assets_manifest.json`

抽出した画像アセットの一覧です。

```json
{
  "schema": "structured-assets-manifest@1",
  "document_id": "sample-001",
  "assets": {
    "images": [
      {
        "name": "image1.png",
        "s3_bucket": "processing-bucket",
        "s3_key": "work/pipeline_v1/ab/sample-001/assets/images/image1.png",
        "s3_uri": "s3://processing-bucket/work/pipeline_v1/ab/sample-001/assets/images/image1.png",
        "content_type": "image/png",
        "openxml_path": "xl/media/image1.png"
      }
    ]
  }
}
```

### 4.1 契約

- `schema` は現在 `structured-assets-manifest@1`
- `assets.images` は配列
- 各 image は少なくとも `s3_key` を持つ想定
- `bedrockenrichment` は `assets.images[*].s3_key` を画像入力として使う
- `extractresultvalidation` は `assets.images` の件数を見て `assets_images_count` を計算する

## 5. `document_manifest.json`

抽出処理全体のサマリです。

```json
{
  "schema": "structured-document-manifest@1",
  "document_id": "sample-001",
  "source": {
    "source_type": "xlsx",
    "bucket": "processing-bucket",
    "key": "uploads/sample.xlsx",
    "s3_uri": "s3://processing-bucket/uploads/sample.xlsx"
  },
  "output": {
    "processing_bucket": "processing-bucket",
    "structured": {
      "document_manifest": {
        "bucket": "processing-bucket",
        "key": "work/pipeline_v1/ab/sample-001/structured/document_manifest.json"
      },
      "objects": {
        "bucket": "processing-bucket",
        "key": "work/pipeline_v1/ab/sample-001/structured/objects.jsonl"
      },
      "assets_manifest": {
        "bucket": "processing-bucket",
        "key": "work/pipeline_v1/ab/sample-001/structured/assets_manifest.json"
      }
    },
    "assets": {
      "images_prefix": "work/pipeline_v1/ab/sample-001/assets/images"
    }
  },
  "counts": {
    "object_count": 42,
    "asset_count": 3
  },
  "pointers": {
    "objects_s3_uri": "s3://processing-bucket/work/pipeline_v1/ab/sample-001/structured/objects.jsonl",
    "assets_manifest_s3_uri": "s3://processing-bucket/work/pipeline_v1/ab/sample-001/structured/assets_manifest.json"
  }
}
```

### 5.1 契約

- `schema` は現在 `structured-document-manifest@1`
- `counts.object_count` は必須に近い扱いで、`extractresultvalidation` が 1 以上を要求する
- `status` と `errors` は PDF 抽出では使われるが、Office extractor では通常出さない
- `extractresultvalidation` は `status != succeeded` や `errors` 配列ありを失敗扱いにする

## 6. `enriched_objects.jsonl`

`bedrockenrichment` が書く JSONL です。  
元の `objects.jsonl` をそのまま残し、末尾に **image_enrichment object** を追加します。上書きではありません。

```json
{
  "id": "img_enrich_0123456789abcdef",
  "type": "image_enrichment",
  "document_id": "sample-001",
  "source": {
    "s3_bucket": "processing-bucket",
    "s3_key": "work/pipeline_v1/ab/sample-001/assets/images/image1.png"
  },
  "text": "A spreadsheet screenshot showing monthly sales totals.",
  "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "metadata": {
    "image_index": 0
  }
}
```

画像ごとの推論に失敗した場合も placeholder object が追記されます。

```json
{
  "id": "img_enrich_deadbeef",
  "type": "image_enrichment",
  "document_id": "sample-001",
  "source": {
    "s3_bucket": "processing-bucket",
    "s3_key": "work/pipeline_v1/ab/sample-001/assets/images/image1.png"
  },
  "text": "Image description unavailable.",
  "model_id": null,
  "metadata": {
    "image_index": 0,
    "enrichment_error": true
  }
}
```

### 6.1 契約

- 元の `objects.jsonl` の各 row はそのまま維持される
- 追加 row は `id` / `type` ベースの shape で、extractor object (`object_id` / `object_type`) とは少し異なる
- `chunkbuild` は `enriched_objects.jsonl` が存在すれば **こちらを優先** して読む

## 7. `enrichment_manifest.json`

enrichment 処理の実行サマリです。

```json
{
  "document_id": "sample-001",
  "status": "partial",
  "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "images_total": 3,
  "images_succeeded": 2,
  "images_failed": 1,
  "errors": [
    {
      "image_key": "assets/images/image3.png",
      "error": "ValueError"
    }
  ],
  "started_at": "2026-05-16T05:32:30.123456+00:00",
  "finished_at": "2026-05-16T05:32:32.456789+00:00",
  "noop_mode": false
}
```

### 7.1 契約

- `status` は `succeeded` / `partial` / `failed`
- 画像が 0 件のときは `status=failed` と `error=no_images`
- `errors` は image ごとの失敗一覧
- `images_failed > 0` の場合、manifest は書かれても Lambda 自体は **`ENRICHMENT_FAILED` を送出** し、`enriched_objects.jsonl` は削除される
- Step Functions はその失敗を `optional_failures.bedrock_enrichment` に残し、後続は `objects.jsonl` ベースで続行する
- `ENRICHMENT_NOOP=true` かつモデル未設定時は placeholder object を含む `enriched_objects.jsonl` を残して成功扱いにする

## 8. `search/chunks/chunks.jsonl`

`chunkbuild` が生成する、**埋め込み直前の検索単位 JSONL** です。  
ここで extractor の object 群が、検索しやすい知識単位へ再構成されます。

```json
{
  "chunk_id": "chunk_000001",
  "document_id": "sample-001",
  "chunk_type": "table_row_block",
  "source_object_ids": [
    "sheet:1:cell:B3",
    "sheet:1:cell:C3",
    "sheet:1:cell:D3",
    "sheet:1:cell:B4",
    "sheet:1:cell:C4",
    "sheet:1:cell:D4"
  ],
  "title": "売上サマリ",
  "text": "Sheet: 売上サマリ\n月: 1月\n売上: 120\n前年差: +10\n\n月: 2月\n売上: 140\n前年差: +12",
  "metadata": {
    "source_type": "xlsx",
    "loc": {
      "xlsx": {
        "sheet": "売上サマリ",
        "row_start": 3,
        "row_end": 4
      }
    },
    "group_kind": "table_row_block",
    "group_key": "sheet:売上サマリ:sheet:1:table:primary:1-2",
    "table_id": "sheet:1:table:primary",
    "source": "enriched",
    "object_type": "cell",
    "object_types": [
      "cell"
    ],
    "source_object_id": "sheet:1:cell:B3",
    "source_object_ids": [
      "sheet:1:cell:B3",
      "sheet:1:cell:C3",
      "sheet:1:cell:D3",
      "sheet:1:cell:B4",
      "sheet:1:cell:C4",
      "sheet:1:cell:D4"
    ],
    "part_index": 0
  }
}
```

### 8.1 整形ルール

現行 `chunkbuild` は、いきなり 1 object = 1 chunk にはしません。  
まず **検索に使いやすい単位へ再編集した segment** を作り、そのあと **長すぎる segment だけ** 追加で分割します。

#### 8.1.1 全体の流れ

1. 入力は `structured/enriched_objects.jsonl` を優先し、なければ `structured/objects.jsonl` を使う
2. `source_type` ごとに、文書の意味に近い単位へまとめ直す
3. できた segment が長すぎる場合だけ、`CHUNK_MAX_CHARS`（既定値 2000）を上限に再分割する
4. 再分割時は、できるだけ文の途中で切らないように、末尾寄りの改行または空白を優先して切る

つまり、chunk の切れ目は **フォーマットごとのまとまり方** と **文字数上限** の両方で決まります。

#### 8.1.2 フォーマット別の分割基準

| source_type | 最初に作るまとまり | どこを区切りにするか | 補足 |
| --- | --- | --- | --- |
| `xlsx` | table row block / `sheet_cells` / `sheet_images` | まず **sheet ごと** に分ける。表らしい header row を検出できた部分は **header: value** 形式の row block に再構成し、表以外の行は `sheet_cells` にまとめる。長くなりすぎたら row block 単位で分割する | `index`, `Unnamed:*`, 空 header の列は canonical object には残すが、検索 chunk からは落とす。画像説明は同じ sheet の `sheet_images` chunk に寄せる |
| `pptx` | slide | **slide ごと** に 1 segment | スライド内の text を結合し、画像説明があれば同じ slide に追記する。テキストがない画像-only slide は画像説明ベースで chunk 化する |
| `docx` | document flow block | 文書順に paragraph / table / image description を足していき、**文字数上限** で切る。加えて、次の block が短い見出しっぽい場合は、ある程度たまった時点でそこで切る | Word の正式な heading style を読んでいるわけではなく、短く 1 行の text を見出しらしいものとして heuristic 判定している |
| `pdf` | page | **page ごと** に 1 segment | 同じ page の text line を順番に連結する |
| その他 | generic block | object の出現順に text を連結し、**文字数上限** で切る | source_type が混在している場合や未知フォーマットの fallback |

#### 8.1.3 長文分割のルール

segment の本文が `CHUNK_MAX_CHARS` を超える場合だけ、1つの segment から複数 chunk を作ります。

- 既定値は `2000` 文字
- なるべく後ろ側（おおむね 80% 以降）で改行または空白を探して切る
- 分割後は同じ `source_object_ids` を引き継ぎ、`metadata.part_index` だけが `0`, `1`, `2` ... と増える

#### 8.1.4 画像説明の扱い

- `bedrockenrichment` 済みの画像説明があれば、元の image object と `s3_key` で突き合わせる
- 一致した場合は、その画像が属する sheet / slide / document flow に寄せて chunk に入れる
- どこにも結び付けられなかった画像説明は、捨てずに **unmatched image chunk** として単独で残す

### 8.2 契約

- `source_object_ids` はこの chunk の元になった object 一覧
- `chunk_type` は top-level でも持ち、現在は `metadata.group_kind` と同じ値を入れる
- `title` は heading / sheet 名 / page 名 / slide 先頭 text などから best-effort で付く
- `text` は chunk の原文ベース本文
- `chunkenrichment` 後は `embedding` が `embedding_text` を優先し、存在しない場合だけ `text` を使う
- `metadata.group_kind` / `metadata.group_key` で chunk のまとまり方を表す
- `metadata.source` は `raw` または `enriched`
- xlsx では table row block が優先され、表以外の行は `sheet_cells` として別 chunk になる
- `metadata.object_type` は単一種別ならその値、複合なら `mixed`
- `part_index` は 1 chunk がさらに長文分割されたときの添字

### 8.3 非対応・制約

- 見出し判定は heuristic で、Word の正式な heading style はまだ使っていない
- Excel の表構造は **row ベース再構成** であり、header 推定や key-value 正規化まではしていない
- PowerPoint の図形間関係、SmartArt、矢印関係などは chunk へ昇格していない
- 画像説明は image object の `s3_key` で突き合わせており、近傍 text との厳密 relation 推定は未実装

### 8.4 `enriched_chunks.jsonl`

`chunkenrichment` は `chunks.jsonl` を上書きせず、検索補助情報を足した派生物を `enriched_chunks.jsonl` に書きます。

```json
{
  "chunk_id": "chunk_000001",
  "document_id": "sample-001",
  "chunk_type": "table_row_block",
  "title": "売上サマリ",
  "text": "Sheet: 売上サマリ\n月: 1月\n売上: 120\n前年差: +10\n\n月: 2月\n売上: 140\n前年差: +12",
  "summary": "売上サマリシートの月別売上と前年差をまとめた表。",
  "summary_status": "generated",
  "keywords_raw": ["売上サマリ", "月", "売上", "前年差"],
  "keywords_normalized": ["売上サマリ", "月", "売上", "前年差"],
  "aliases": [],
  "entities": {
    "product_names": [],
    "org_names": [],
    "system_names": [],
    "dates": [],
    "amounts": []
  },
  "embedding_text": "売上サマリ\n売上サマリシートの月別売上と前年差をまとめた表。\nSheet: 売上サマリ\n月: 1月\n売上: 120\n前年差: +10\n\n月: 2月\n売上: 140\n前年差: +12",
  "metadata": {
    "source_type": "xlsx",
    "group_kind": "table_row_block",
    "chunk_enrichment_version": "chunk_enrich_v1",
    "keyword_extractor_version": "keyword_rule_v1",
    "chunk_enrichment_model": "global.amazon.nova-2-lite-v1:0"
  }
}
```

- `summary_status` は `generated` / `skipped` / `failed`
- `keywords_raw` は rule-based 抽出が常に走り、LLM が返した追加語は重複排除して同じ配列へマージされる
- `keywords_normalized` は `keywords_raw` を正規化した検索用キー
- `aliases` と `entities` は検索補助用で、`embedding_text` には入れない
- `embedding_text` は `title + summary + text` の順で連結した、埋め込み専用テキスト

### 8.5 `chunk_enrichment_manifest.json`

`chunkenrichment` の実行結果は次の manifest にまとまります。

```json
{
  "status": "succeeded",
  "document_id": "sample-001",
  "chunk_enrichment_version": "chunk_enrich_v1",
  "keyword_extractor_version": "keyword_rule_v1",
  "model_id": "global.amazon.nova-2-lite-v1:0",
  "noop_mode": false,
  "total_chunks": 12,
  "summary_generated": 5,
  "summary_skipped": 7,
  "summary_failed": 0,
  "keywords_extracted": 12,
  "errors": [],
  "finished_at": "2026-05-16T12:40:00+00:00"
}
```

- `errors` には **step 全体を落とさず継続した LLM 失敗 chunk の `chunk_id`** が入る
- `status: succeeded` は「`enriched_chunks.jsonl` と manifest の書き出しまで完了した」ことを表す
- S3 読み込み失敗や manifest 書き込み失敗などの致命的エラーでは `status: failed` の manifest を残し、`enriched_chunks.jsonl` を削除して `CHUNK_ENRICHMENT_FAILED` を送出する

## 9. 後続処理との接続

### 9.1 `extractresultvalidation`

次を必須として見ます。

- `objects.jsonl`
- `assets_manifest.json`
- `document_manifest.json`

加えて、`document_manifest.json` から次を検証します。

- `counts.object_count > 0`
- `status` があれば `succeeded`
- `errors` があれば空配列

### 9.2 `bedrockenrichment`

次を入力として使います。

- `assets_manifest.json`
- `objects.jsonl`

出力:

- `enriched_objects.jsonl`
- `enrichment_manifest.json`

### 9.3 `chunkbuild`

入力優先順位:

1. `enriched_objects.jsonl`
2. `objects.jsonl`

そのうえで `search/chunks/chunks.jsonl` を作ります。  
この段階で **object 単位から検索単位への再構成** を行います。

### 9.4 `chunkenrichment`

入力:

- `search/chunks/chunks.jsonl`

出力:

- `search/chunks/enriched_chunks.jsonl`
- `search/chunks/chunk_enrichment_manifest.json`

挙動:

- rule-based keywords は全 chunk で必ず生成する
- LLM summary / aliases / entities は、`CHUNK_ENRICHMENT_MODEL_ID` が設定され、かつ chunk が summary 対象のときだけ生成する
- 対象判定は `group_kind`（`table_row_block`, `sheet_cells`, `sheet_images`, `slide`, `slide_images`, `document_flow`, `page`）と本文長の heuristic で行う
- LLM が失敗した chunk は `summary_status=failed` と manifest の `errors` に残し、step 全体は続行する

### 9.5 `embedding`

入力優先順位:

1. `search/chunks/enriched_chunks.jsonl`
2. `search/chunks/chunks.jsonl`

埋め込み対象フィールド:

1. `embedding_text`
2. `text`

出力:

- `vectors/embeddings.jsonl`

つまり、`chunkenrichment` が有効なときは summary を含んだ `embedding_text` を使い、未実行または旧データでは `text` に自動でフォールバックします。

## 10. 運用上の読み方

- `objects.jsonl` があれば、抽出本体は成功している
- `document_manifest.json` があれば、抽出結果の件数と出力先を確認できる
- `assets_manifest.json` があれば、画像アセットの有無を確認できる
- `enriched_objects.jsonl` があれば、画像説明付与処理は少なくとも実行された
- `enrichment_manifest.json` の `errors` を見れば、enrichment 失敗理由を追える
- `search/chunks/chunks.jsonl` があれば、検索単位への再構成までは完了している
- `search/chunks/enriched_chunks.jsonl` があれば、summary / keywords / `embedding_text` 付きの派生 chunk まで生成されている
- `search/chunks/chunk_enrichment_manifest.json` の `summary_failed` / `errors` を見れば、chunk enrichment の部分失敗を追える
