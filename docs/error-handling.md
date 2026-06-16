# Error Handling

このドキュメントは、現在のドキュメント処理パイプラインが **どこで失敗を検知し、どう伝播し、どこまで後続に残すか** を整理したものです。  
対象は `kickoff`、Step Functions、各 Lambda、および補助的な出力アーティファクトです。

## 全体方針

- **入力不整合や必須値不足は fail-fast** です。必要なフィールドが足りない場合は例外を送出します。
- **未対応ファイルは「異常」ではなく「対象外」** として扱います。`inputvalidation` が `unsupported_file=true` を付け、Step Functions が専用の失敗分類へ流します。
- **本文や画像そのものはログに出さない** 方針です。ログは `document_id`、件数、ステータスなどのメタデータ中心です。
- いくつかのステップは、失敗時でも **診断用アーティファクトを S3 に残してから失敗** します。
- `bedrockenrichment` と `chunkenrichment` は **任意の補助ステップ** として扱い、失敗しても raw artifact へフォールバックして後続へ進みます。
- Step Functions 側では **明示的な retry は未設定** です。必須ステップの Lambda 例外は Catch で失敗分類に変換して実行を終了します。

## Step Functions の失敗分類

現在の State Machine は、例外をそのまま見せず、次の失敗カテゴリにまとめます。

| Fail state | 主な発生箇所 | 意味 |
| --- | --- | --- |
| `UNSUPPORTED_FILE_TYPE` | `inputvalidation` → 共通 failed-status update | 形式未対応、暗号化Office、壊れたOOXMLなど「処理対象外」 |
| `EXTRACT_FAILED` | `inputvalidation` / `legacyofficeconvert` / 各 extractor / `extractresultvalidation` / `chunkbuild` | 入力正規化、変換、抽出、抽出結果検証、チャンク生成の失敗 |
| `EMBEDDING_FAILED` | `embedding` | 埋め込み生成の失敗 |
| `INDEX_FAILED` | `indexwriter` / `updatestatus` | 検索インデックス書き込みまたは最終状態更新の失敗 |

補足:

- 必須ステップの Catch は `failure_context` / `failure_error` を積んだうえで `updatestatus` を共通呼び出しし、その後 `Fail` 状態へ遷移します。**失敗時に回復フローへ戻す設計ではありません**。
- **失敗時の補償処理や後片付け処理は未実装** です。
- `statusTableName` が設定されている場合は、失敗分岐でも `updatestatus` を呼び、失敗カテゴリ (`UNSUPPORTED_FILE_TYPE` / `EXTRACT_FAILED` / `EMBEDDING_FAILED` / `INDEX_FAILED`) を DynamoDB に書きます。成功時は `optional_failures` に任意ステップの失敗内容も残せます。

## フロー別の現在の挙動

| フェーズ | 現在のエラー処理 |
| --- | --- |
| `kickoff` | SQS メッセージ内の対象外拡張子は無視します。`StartExecution` の重複 (`ExecutionAlreadyExists`) は、**同一 S3 通知の再配信** とみなして正常扱いで握りつぶします。S3 オブジェクトの同一 key 上書きは新しい実行になります。それ以外の例外は送出され、**その SQS メッセージは delete されず再試行対象** になります。 |
| `inputvalidation` | 必須入力不足は `InvalidInputError` で失敗します。一方、未対応拡張子・暗号化Office・壊れたOOXML・サイズ超過などは `unsupported_file` と `unsupported_reason` を返し、後続 Choice で unsupported 系統へ流します。 |
| `legacyofficeconvert` | State Machine は `legacy_office_source=true` のときだけこの Lambda を呼びます。`soffice` 未配置、変換コマンド失敗、出力ファイル未生成、必須 S3 情報不足は `LegacyOfficeConversionError` で失敗します。 |
| `docxextract` / `xlsxextract` / `pptxextract` | OOXML ZIP/XML の読み取りや S3 取得で失敗すると、基本的にそのまま例外が上がります。成功時は `objects.jsonl` などをまとめて出力します。**部分失敗を manifest に落として継続する設計ではありません**。 |
| `pdfextract` | Textract 呼び出し失敗・タイムアウト・Lambda 残時間不足は内部で `errors` に記録し、**manifest を `failed` で書き出して返却** します。次段の `extractresultvalidation` がそれを見て明示的に失敗へ変換します。 |
| `extractresultvalidation` | 必須アーティファクトの存在、JSON 妥当性、`status == succeeded`、`errors` 空、`object_count > 0` を検証します。どれかを満たさなければ失敗します。 |
| `bedrockenrichment` | 画像説明付与は任意ステップです。成功時だけ `enriched_objects.jsonl` を残します。画像単位の失敗やモデルID未設定 (`ENRICHMENT_NOOP=false`) では `enrichment_manifest.json` に `errors` を残しつつ `enriched_objects.jsonl` を削除し、State Machine は `optional_failures.bedrock_enrichment` を記録して `objects.jsonl` ベースで続行します。`ENRICHMENT_NOOP=true` なら placeholder object を含む `enriched_objects.jsonl` を成功扱いで残します。 |
| `chunkbuild` | 入力 JSONL 読み込みや S3 失敗は例外で停止します。個別オブジェクトのテキスト抽出に失敗した場合は、そのオブジェクトだけスキップする箇所があります。 |
| `chunkenrichment` | rule-based keyword 抽出は常に行います。LLM による summary / aliases / entities 生成は chunk 単位で失敗しても `chunk_enrichment_manifest.json` の `errors` と `summary_failed` に記録して続行します。S3 読み込み・出力書き込み・致命的例外では `chunk_enrichment_manifest.json` を `failed` で書き、`enriched_chunks.jsonl` を削除してから例外を送出します。State Machine は `optional_failures.chunk_enrichment` を記録し、`chunks.jsonl` ベースで続行します。 |
| `embedding` | `search/chunks/enriched_chunks.jsonl` を優先し、なければ `chunks.jsonl` を使います。各 chunk では `embedding_text` を優先し、なければ `text` を使います。チャンク読み込み、Bedrock 応答不正、S3 書き込みなど、**どの例外でも最終的に `RuntimeError("EMBEDDING_FAILED")`** に畳み込みます。 |
| `indexwriter` | 検索出力先未設定でも `SEARCH_NOOP=true` なら成功扱いです。Elastic Cloud API key 不足、AWS OpenSearch / Elastic Cloud の Bulk API エラー、認証不足、データ不整合は **`INDEX_FAILED` に統一** されます。 |
| `updatestatus` | `STATUS_TABLE` 未設定なら no-op 成功です。成功時は `INDEXED`、失敗分岐から呼ばれた場合は失敗カテゴリを status に書きます。成功時に任意ステップが失敗していれば `optional_failures` を追加し、クリーンな成功時は `last_error` / `optional_failures` を消します。チャンク数/埋め込み数の算出で 404 は `None` 扱いにします。`enrichment_manifest.json` は **欠落や JSON 不正を握りつぶして続行** します。DynamoDB 更新失敗は送出されます。 |

## 失敗時にも残るもの

現状、失敗時に S3 上へ情報を残す設計は次の通りです。

| ステップ | 失敗時の残存物 |
| --- | --- |
| `pdfextract` | `document_manifest.json`、`objects.jsonl`、`assets_manifest.json` を書きます。manifest の `status` と `errors` で失敗内容を判定します。 |
| `bedrockenrichment` | 成功時は `enriched_objects.jsonl` と `enrichment_manifest.json` を書きます。step 全体が失敗した場合でも `enrichment_manifest.json` は残しますが、`enriched_objects.jsonl` は削除して raw object へフォールバックさせます。 |
| `chunkenrichment` | chunk 単位の LLM 失敗だけなら `enriched_chunks.jsonl` と `chunk_enrichment_manifest.json` を書き、失敗 chunk は manifest の `errors` に残ります。step 全体が失敗した場合は `chunk_enrichment_manifest.json` を `failed` で残し、`enriched_chunks.jsonl` は削除します。 |

一方、`legacyofficeconvert`、`docxextract`、`xlsxextract`、`pptxextract`、`embedding`、`indexwriter` は、途中で例外になった場合に **途中成果を保証する設計ではありません**。

## ログ方針

- `inputvalidation`、`pdfextract`、`kickoff`、各 extractor は、基本的に `document_id`、件数、種別、状態だけを記録します。
- `embedding` と `indexwriter` は、内部例外の詳細を外へ出さず、固定エラー名に変換します。
- `pdfextract` も例外メッセージ全文ではなく、`exception:<クラス名>` だけを manifest に残します。
- `bedrockenrichment` は画像単位の失敗理由を **例外クラス名** までに留めています。

## 現状の運用上の注意

### 1. Step Functions の自動 retry はない

`add_retry()` は設定されていないため、一時的な API エラーでもその場で失敗分類へ落ちます。  
再試行したい場合は、State Machine 定義側の追加実装が必要です。

### 2. 失敗時も状態は書くが、回復はしない

`statusTableName` が設定されていれば、成功時は `INDEXED`、失敗時は失敗カテゴリを DynamoDB に書きます。任意ステップの失敗は `INDEXED` のまま `optional_failures` に残ります。  
ただし、状態を書いたあとでも実行自体は `Fail` で終了するため、**回復や再実行は別設計**です。

### 3. `kickoff` はメッセージ単位 delete

SQS レコード中の途中で例外が起きると、そのメッセージ全体が delete されません。  
`ExecutionAlreadyExists` で **同一 S3 通知の再配信** は抑えていますが、同じ S3 key への上書きイベントは別実行として流します。  
そのうえで、**同一メッセージ再配信時に先行レコードを再走査する設計** です。
`cdk_stack_sf_sqs` には DLQ (`cdk_stack_sf_dlq`) があり、同一メッセージが 5 回受信されても成功しない場合は source queue から退避されます。

### 4. 一部は best-effort / no-op

- `indexwriter` は `SEARCH_NOOP=true` なら書き込みなしで成功します。
- `updatestatus` は `STATUS_TABLE` 未設定なら no-op 成功します。
- `updatestatus` は enrichment manifest の欠落や JSON 不正を無視します。
- `chunkenrichment` は `CHUNK_ENRICHMENT_MODEL_ID` 未設定でも失敗せず、rule-based keywords のみ生成します。

## 障害調査の見方

1. Step Functions の失敗カテゴリを見る  
2. 失敗分岐なら `$.failure_error`、任意ステップなら `$.bedrock_enrichment_error` / `$.chunk_enrichment_error` を確認する  
3. `work/.../structured/` 配下の manifest を確認する  
4. `enrichment_manifest.json`、`chunk_enrichment_manifest.json`、`document_manifest.json` の `status` / `errors` を確認する  
5. 旧 Office の場合は `legacy_office_conversion` の有無と `source_type` の変化を確認する

## まだ docs 化していなかった点

これまで docs には VPC / IAM / リポジトリ構成の説明はありましたが、**エラー処理だけをまとめたページはありませんでした**。  
今回この `docs/error-handling.md` を追加し、`docs/README.md` とルート `README.md` から辿れるようにしています。
