# Amazon Bedrock を使った PDF / Office ドキュメント処理

このソリューションの詳細は、以下のAWSブログ記事で公開されています。
https://aws.amazon.com/blogs/machine-learning/scalable-intelligent-document-processing-using-amazon-bedrock/

追加のリポジトリ内ドキュメント:

- [docs/README.md](docs/README.md)
- [docs/repository-guide.md](docs/repository-guide.md)
- [docs/production-configuration.md](docs/production-configuration.md)
- [docs/output-artifacts.md](docs/output-artifacts.md)
- [docs/word-extraction.md](docs/word-extraction.md)
- [docs/excel-extraction.md](docs/excel-extraction.md)
- [docs/powerpoint-extraction.md](docs/powerpoint-extraction.md)
- [docs/error-handling.md](docs/error-handling.md)

このリポジトリは、旧 Office 形式 (`.doc`, `.xls`, `.ppt`) を受け取った場合、抽出前に `.docx`, `.xlsx`, `.pptx` へ正規化する変換ステップを持ちます。  
変換は Lambda 内で **LibreOffice (`soffice`)** を使って実行するため、実運用では Lambda Layer またはコンテナイメージ側に LibreOffice を含めてください。

## 前提条件

1. Node.js
2. Python
3. `uv`: 手順は [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) を参照
4. AWS Command Line Interface (AWS CLI): 手順は [Installing the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-install.html) を参照

## デプロイ

以下のコードは、この参照実装を自分の AWS アカウントにデプロイします。ソリューションは AWS Cloud Development Kit (AWS CDK) を使って、S3 バケット、Step Functions、Amazon Simple Queue Service (Amazon SQS) キュー、AWS Lambda 関数などの各コンポーネントをデプロイします。AWS CDK は、使い慣れたプログラミング言語でクラウドリソースを定義・プロビジョニングできるオープンソースのフレームワークです。

1. AWS Cloud9 のターミナルで GitHub リポジトリを clone します:
	```
	git clone https://github.com/aws-samples/aws-generative-ai-document-processing-solution
	```
2. 現行構成では、追加の `sharp` レイヤー作成は不要です。
3. リポジトリのディレクトリへ移動します:
	```
	cd aws-generative-ai-document-processing-solution
	```
4. `uv` で依存関係を同期します:
	```
	uv sync
	```

特定の AWS アカウントとリージョンの組み合わせに対して AWS CDK アプリを初めてデプロイする場合は、bootstrap スタックのインストールが必要です。このスタックには、AWS CDK がデプロイを完了するために必要な各種リソースが含まれます。たとえば、デプロイ時にテンプレートやアセットを保存するための Amazon S3 バケットなどが作成されます。

5. bootstrap スタックをインストールするには、次のコマンドを実行します:
	```
   uv run cdk bootstrap
	```
6. プロジェクトのルートディレクトリから、次のコマンドでスタックをデプロイします:
	```
   uv run cdk deploy
	```

   このリポジトリでは `cdk.json` の app 設定が `uv run python app.py` になっているため、`cdk synth` / `cdk deploy` 実行時には `uv` の仮想環境が使われます。

   デプロイ前にテンプレートだけ確認したい場合は、次を実行してください。
   ```
   uv run cdk synth
   ```

   AWS 上に作成される Lambda / Step Functions / SQS / CloudWatch Logs の主要リソース名は `cdk_stack_*` プレフィックスを使います。

   本番向けのデプロイ設定は、リポジトリ直下の `config.yml` で管理します。`cdk deploy -c ...` は使わず、必要な値を `config.yml` に書いてから `cdk deploy` / `cdk synth` を実行してください。

   例:
	```yaml
	lambdaVpcName: shared-services-vpc
	lambdaSubnetIds:
	  - subnet-aaa
	  - subnet-bbb
	lambdaSecurityGroupIds:
	  - sg-0123456789abcdef0
	s3BucketName: document-processing-prod-example
	sofficeBinary: /opt/libreoffice/program/soffice
	bedrockImageModelId: anthropic.claude-3-5-sonnet-20240620-v1:0
	chunkEnrichmentModelId: global.amazon.nova-2-lite-v1:0
	embeddingModelId: amazon.titan-embed-text-v2:0
	embeddingDimensions: 1024
	statusTableName: document-processing-status
	searchBackend: elastic-cloud
	searchEndpoint: https://cluster-id.region.aws.found.io
	searchIndex: document-chunks
	searchApiKeySecretArn: arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:elastic-cloud-api-key
	```

   `config.yml` で使える主なキー:

   | Key | 説明 |
   | --- | --- |
   | `lambdaVpcId` | VPC ID で既存 VPC を lookup し、すべての Lambda をその VPC にアタッチします。 |
   | `lambdaVpcName` | VPC 名 (`Name` タグ) で既存 VPC を lookup し、すべての Lambda をその VPC にアタッチします。 |
   | `lambdaSubnetIds` | Lambda 配置用のサブネット ID 一覧。未指定時は looked-up VPC の egress 付き private subnet を使います。`lambdaVpcId` または `lambdaVpcName` が必要です。 |
   | `lambdaSecurityGroupIds` | Lambda にアタッチする既存 Security Group ID 一覧です。 |
   | `createLambdaSecurityGroup` | `true` の場合、looked-up VPC 内に Lambda 用の Security Group を追加作成します。未設定時は「VPC 指定あり かつ Security Group 未指定」の場合だけ自動で `true` になります。 |
   | `lambdaAllowAllOutbound` | `false`（デフォルト）の場合、作成される Lambda 用 Security Group の送信先は VPC 内 DNS と HTTPS に限定されます。 |
   | `s3BucketName` | メイン S3 バケット名。指定名のバケットが既に存在すればそれを使い、存在しなければその名前で新規作成します。未指定時は従来どおり CDK の自動生成名です。 |
   | `pipelineVersion` | デフォルトのパイプラインバージョン (`pipeline_v1`) を上書きします。 |
   | `sofficeBinary` | 旧 Office 形式変換に使う LibreOffice バイナリパス。デフォルトは `/opt/libreoffice/program/soffice` です。 |
   | `bedrockImageModelId` | `bedrockenrichment` で使う Bedrock マルチモーダルモデル ID または ARN。IAM はこのモデルだけに限定されます。 |
   | `chunkEnrichmentModelId` | `chunkenrichment` で要約・別名・エンティティ抽出に使う Bedrock テキストモデル ID または ARN。未指定でも step 自体は動作し、その場合は rule-based keyword 抽出だけを行って LLM 要約はスキップします。 |
   | `embeddingModelId` | `embedding` で使う Bedrock 埋め込みモデル ID または ARN。デフォルトは `amazon.titan-embed-text-v2:0` です。従来の `amazon.titan-embed-text-v2` を指定しても自動で `:0` 付きに正規化します。 |
   | `embeddingDimensions` | 埋め込みベクトル次元数。`amazon.titan-embed-text-v2:0` を使う場合は `256` / `512` / `1024` を指定できます。 |
   | `statusTableName` | `updatestatus` が使う DynamoDB テーブル名。成功時は `INDEXED`、失敗時は失敗カテゴリを書き、任意 enrichment が落ちても `optional_failures` を残せます。IAM はこのテーブルだけに限定されます。 |
   | `searchBackend` | `none` / `aws-opensearch` / `elastic-cloud`。未設定時は、検索エンドポイント未指定なら `none`、指定済みなら `aws-opensearch` です。 |
   | `searchEndpoint` / `searchIndex` | 検索インデックス書き込み先を有効化します。2つとも必ずセットで指定してください。未指定時は no-op モードです。 |
   | `searchApiKeySecretArn` | `elastic-cloud` 利用時の API Key を格納した Secrets Manager ARN。secret 本文は plain text か `{ "api_key": "..." }` を受け付けます。 |

   検索向けの後段パイプラインは `chunkbuild` → `chunkenrichment` → `embedding` → `indexwriter` です。`chunkbuild` は原文ベースの `search/chunks/chunks.jsonl` を作り、`chunkenrichment` は summary / keywords / aliases / entities / `embedding_text` を持つ `search/chunks/enriched_chunks.jsonl` を派生生成します。`bedrockenrichment` または `chunkenrichment` が失敗した場合は stale artifact を削除したうえで raw `objects.jsonl` / `chunks.jsonl` にフォールバックし、`embedding` は `enriched_chunks.jsonl` があれば `embedding_text` を優先し、なければ `chunks.jsonl` の `text` を使います。現状の `indexwriter` は `chunks.jsonl` と `vectors/embeddings.jsonl` を入力に使うため、summary / keywords / aliases / entities は検索インデックスへそのままは書かれず、主に `embedding_text` 生成と運用確認に使われます。

   `lambdaVpcId` と `lambdaVpcName` を両方指定した場合、CDK lookup は **両方の条件を満たす同じ VPC** を探します。片方だけでも lookup できますが、両方を指定する場合は同じ VPC を指す値にしてください。

   このスタックでの IAM スコープ変更点:

   - Lambda の S3 アクセスは、バケット全体ではなく `uploads/*` と `work/*` のプレフィックス単位に制限しています。
   - `kickoff` は対象キューの消費と、このスタックの Step Functions 起動だけを行えます。
   - `kickoff` の source queue には DLQ (`cdk_stack_sf_dlq`) を付けており、同一メッセージが 5 回受信されても処理できない場合は退避されます。
   - `kickoff` は同一 S3 通知の再配信は抑止しつつ、同じ `uploads/` key への上書きは新しい Step Functions 実行として受け付けます。
   - Bedrock の権限は、`bedrockenrichment` / `chunkenrichment` / `embedding` それぞれで設定したモデル ID / ARN に限定しています。
   - DynamoDB の権限は、`statusTableName` を指定した場合のみ、そのテーブルに対して追加されます。

   旧 Office 形式の変換に関する補足:

   - 対応形式は `.doc`, `.xls`, `.ppt` です。
   - これらは Step Functions 内の変換ステップで `.docx`, `.xlsx`, `.pptx` に変換してから既存 extractor に渡します。
   - 実行環境に `soffice` が存在しない場合、変換ステップは明示的に失敗します。
   - 変換済みファイルは `s3://<processing-bucket>/work/<pipelineVersion>/<hash_prefix>/<source_filename>/<document_id>/converted/` に保存され、その後の extractor はその S3 オブジェクトを読みます。

7. `uploads/` プレフィックスと、S3 コンソールから `uploads/` 配下へブラウザアップロードするための最小 CORS ルールは、スタックが自動で整備します。追加の手動設定は不要です。

## 動作方法

### 1. デプロイ後の基本確認

まず、スタックが見えることを確認します。

```bash
uv run cdk ls
```

このリポジトリの標準スタック名は `document-processing` です。

次に、メイン S3 バケット名を確認します。`config.yml` で `s3BucketName` を固定していない場合は、CloudFormation から物理名を取得するのが確実です。

```bash
aws cloudformation describe-stack-resources \
   --stack-name document-processing \
   --query "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
   --output text
```

以降、この値を `<processing-bucket>` として使います。

### 2. サンプル入力を投入する

このリポジトリには、すぐに試せるサンプル入力が入っています。

- `sample-inputs/sample-birth-certificate-application.pdf`
- `sample-inputs/word_extract_test_v1.docx`
- `sample-inputs/テストケース.xlsx`
- `sample-inputs/P6A2説明.pptx`

S3 の `uploads/` プレフィックスへ配置すると、自動でパイプラインが起動します。

```bash
aws s3 cp sample-inputs/sample-birth-certificate-application.pdf \
   s3://<processing-bucket>/uploads/sample-birth-certificate-application.pdf
```

Word / Excel / PowerPoint を試す場合も同様です。

```bash
aws s3 cp sample-inputs/word_extract_test_v1.docx s3://<processing-bucket>/uploads/word_extract_test_v1.docx
aws s3 cp sample-inputs/テストケース.xlsx s3://<processing-bucket>/uploads/テストケース.xlsx
aws s3 cp sample-inputs/P6A2説明.pptx s3://<processing-bucket>/uploads/P6A2説明.pptx
```

補足:

- 同じ `uploads/` key へ再アップロードすると、新しい Step Functions 実行として再処理されます。
- 対応拡張子は `pdf`, `doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx` です。

### 3. 実行状態を確認する

主な確認先は次の 3 つです。

1. Step Functions
    - ステートマシン名は `cdk_stack_stepfunction` です。
    - 失敗時の分類は `UNSUPPORTED_FILE_TYPE`, `EXTRACT_FAILED`, `EMBEDDING_FAILED`, `INDEX_FAILED` です。
2. CloudWatch Logs
    - Lambda 関数名は `cdk_stack_*` 形式です。
    - ログには本文ではなく `document_id`、件数、状態などのメタデータだけが出ます。
3. S3 の `work/` 配下
    - 抽出・enrichment・chunk・embedding の中間成果物が出力されます。

実行履歴の一覧は AWS CLI からも見られます。

```bash
aws stepfunctions list-executions \
   --state-machine-arn $(aws stepfunctions list-state-machines --query "stateMachines[?name=='cdk_stack_stepfunction'].stateMachineArn | [0]" --output text) \
   --max-results 10
```

### 4. 出力アーティファクトを確認する

正常時の主な出力は `work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}/` 配下に作られます。

### 4.1 出力結果の概要

このパイプラインを 1 回実行すると、最終的には次の 3 層の結果が得られます。

1. **構造化抽出結果**
    - 元の PDF / Word / Excel / PowerPoint から、本文、表、画像参照などを `structured/objects.jsonl` に正規化して保存します。
    - 画像がある場合は `structured/assets_manifest.json` に一覧を残します。
    - 抽出処理全体のサマリは `structured/document_manifest.json` に残ります。
2. **検索用の中間結果**
    - `chunkbuild` が、抽出結果を検索しやすい単位へ再構成して `search/chunks/chunks.jsonl` を作ります。
    - `chunkenrichment` が有効なら、summary / keywords / aliases / entities / `embedding_text` を追加した `search/chunks/enriched_chunks.jsonl` を派生生成します。
    - 画像がある場合は `bedrockenrichment` が画像説明を追加し、`structured/enriched_objects.jsonl` を生成します。
3. **ベクトル化・索引化結果**
    - `embedding` が chunk ごとの埋め込みベクトルを `vectors/embeddings.jsonl` に保存します。
    - `indexwriter` を有効化していれば、そのベクトルと chunk 本文が OpenSearch / Elastic Cloud に送られます。

言い換えると、このリポジトリの出力は「抽出しただけの生データ」で終わらず、検索・RAG に使える chunk と embedding まで作る構成です。

### 4.2 利用者視点で何が見えるか

ファイル種別ごとに、概ね次のような結果が得られます。

- PDF
   - ページごとの本文テキストが抽出されます。
   - 画像アセットは基本的に生成されず、主に本文中心の chunk / embedding が作られます。
- Word (`.docx`, `.doc`)
   - 段落、表、本文中画像が抽出されます。
   - 表は row 配列として保持され、検索時にはテキスト化されます。
- Excel (`.xlsx`, `.xls`)
   - セル、推定された主テーブル、シート内画像が抽出されます。
   - 検索時には table row block や sheet 単位の chunk に再構成されます。
- PowerPoint (`.pptx`, `.ppt`)
   - スライドごとのテキストと画像が抽出されます。
   - 検索時には slide 単位の chunk にまとまります。

### 4.3 成功時と部分成功時の違い

- 必須ステップが成功した場合は、`objects.jsonl`、`chunks.jsonl`、`embeddings.jsonl` まで進みます。
- `bedrockenrichment` や `chunkenrichment` は任意ステップなので、失敗しても raw artifact にフォールバックして後続へ進む場合があります。
- その場合でも、`enrichment_manifest.json` や `search/chunks/chunk_enrichment_manifest.json` に失敗内容が残るため、「全文失敗」なのか「部分成功」なのかを判別できます。

よく見るファイル:

- `structured/objects.jsonl`
- `structured/assets_manifest.json`
- `structured/document_manifest.json`
- `structured/enriched_objects.jsonl`
- `search/chunks/chunks.jsonl`
- `search/chunks/enriched_chunks.jsonl`
- `vectors/embeddings.jsonl`

一覧確認例:

```bash
aws s3 ls s3://<processing-bucket>/work/ --recursive
```

artifact の意味は [docs/output-artifacts.md](docs/output-artifacts.md) を参照してください。

### 5. 失敗時の見方

失敗時は、まず Step Functions の失敗カテゴリを見てから、`work/.../structured/` 配下の manifest を確認します。

- `document_manifest.json`
- `enrichment_manifest.json`
- `search/chunks/chunk_enrichment_manifest.json`

PDF は失敗時でも manifest を残します。`bedrockenrichment` と `chunkenrichment` は任意ステップなので、失敗しても raw artifact にフォールバックして後続へ進む場合があります。詳細は [docs/error-handling.md](docs/error-handling.md) を参照してください。

### 6. ローカル検証コマンド

コード変更後の最低限の確認コマンドは次です。

```bash
uv run python -m compileall app.py cdk_stack deploy_code
uv run python -m unittest discover -s tests -t . -p "test_*.py"
uv run cdk synth
```

テストは重要回帰だけを `tests/` に置いています。CDK / Lambda / 設定を変えたときは、少なくとも `compileall` と `cdk synth` を実行してください。
## クリーンアップ

1. まず、作成された S3 バケットを完全に空にします。
2. 次に、以下を実行します:
   ```
   cdk destroy
   ```

## セキュリティ

詳細は [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) を参照してください。

## ライセンス

このライブラリは MIT-0 ライセンスで提供されています。詳細は [LICENSE](LICENSE) を参照してください。