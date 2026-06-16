# Production Configuration

このリポジトリのデプロイ設定は、リポジトリ直下の `config.yml` を source of truth として読み込みます。`cdk deploy -c ...` は使いません。

## config.yml

初期状態の `config.yml` は、すべてのサポート対象キーを持つテンプレートです。必要な値だけ埋めて、`cdk synth` または `cdk deploy` を実行してください。

```yaml
pipelineVersion: pipeline_v1
lambdaVpcId: null
lambdaVpcName: shared-services-vpc
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
lambdaSecurityGroupIds:
  - sg-lambda-processing
createLambdaSecurityGroup: null
lambdaAllowAllOutbound: false
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

## Supported keys

| Key | Purpose |
| --- | --- |
| `lambdaVpcId` | VPC ID で既存 VPC を lookup して、すべての Lambda をその VPC にアタッチする。 |
| `lambdaVpcName` | VPC 名 (`Name` タグ) で既存 VPC を lookup して、すべての Lambda をその VPC にアタッチする。 |
| `lambdaSubnetIds` | Lambda を配置するサブネット一覧。未指定時は looked-up VPC の `PRIVATE_WITH_EGRESS` を使う。`lambdaVpcId` または `lambdaVpcName` が必要。 |
| `lambdaSecurityGroupIds` | Lambda にアタッチする既存 Security Group 一覧。 |
| `createLambdaSecurityGroup` | `true` の場合、スタック内で Lambda 用 Security Group を追加作成する。`null` の場合は「VPC 指定あり かつ Security Group 未指定」のときだけ自動で `true`。 |
| `lambdaAllowAllOutbound` | `false` の場合、作成した Security Group の送信先を絞る。既定値は `false`。 |
| `s3BucketName` | メイン S3 バケット名。指定名が既存ならそれを再利用し、未作成ならその名前で新規作成する。未指定時は CDK が自動生成する。 |
| `pipelineVersion` | 作業プレフィックスに使うパイプラインバージョン。既定値は `pipeline_v1`。 |
| `sofficeBinary` | 旧 Office 形式 (`.doc`, `.xls`, `.ppt`) を OOXML へ変換する LibreOffice バイナリパス。既定値は `/opt/libreoffice/program/soffice`。 |
| `bedrockImageModelId` | `bedrockenrichment` で使う Bedrock 画像モデル。 |
| `chunkEnrichmentModelId` | `chunkenrichment` で summary / aliases / entities を生成する Bedrock テキストモデル。未指定時も `chunkenrichment` は実行されるが、rule-based keywords のみ生成して LLM 要約はスキップする。 |
| `embeddingModelId` | `embedding` で使う Bedrock 埋め込みモデル。既定値は `amazon.titan-embed-text-v2:0`。従来の `amazon.titan-embed-text-v2` を指定しても自動で `:0` 付きに正規化する。 |
| `embeddingDimensions` | 埋め込みベクトル次元数。`amazon.titan-embed-text-v2:0` では `256` / `512` / `1024` を指定できる。 |
| `statusTableName` | `updatestatus` が更新する DynamoDB テーブル名。成功時は `INDEXED`、失敗時は失敗カテゴリを記録し、任意 enrichment の失敗は `optional_failures` として残せる。 |
| `searchBackend` | `none` / `aws-opensearch` / `elastic-cloud`。 |
| `searchEndpoint` | 検索書き込み先のエンドポイント。 |
| `searchIndex` | 検索インデックス名。 |
| `searchApiKeySecretArn` | `elastic-cloud` で使う API key を格納した Secrets Manager ARN。 |

## Default behavior

- `lambdaVpcId` も `lambdaVpcName` も指定しなければ、Lambda は VPC 非接続のままデプロイされる。
- `lambdaVpcId` または `lambdaVpcName` を指定した場合は、CDK の VPC lookup が必要になるため account/region 付きで synth/deploy される。
- `lambdaSubnetIds` や `lambdaSecurityGroupIds` は、`lambdaVpcId` または `lambdaVpcName` なしでは使えない。
- `lambdaVpcId` と `lambdaVpcName` を両方指定した場合、CDK lookup は両方の条件を満たす同じ VPC を探す。値が一致しない、または名前で複数 VPC が見つかる場合は synth が失敗する。
- VPC lookup を指定したのに Security Group を 1 つも与えず、`createLambdaSecurityGroup: false` にすると synth エラーになる。
- メイン S3 バケットには、`uploads/` プレフィックスの placeholder object と、S3 コンソールからのブラウザアップロード用 CORS ルールを自動で整備する。
- `chunkEnrichmentModelId` を省略した場合でも `chunkenrichment` step は残る。summary / aliases / entities はスキップされるが、rule-based keywords と `embedding_text` は生成される。
- 検索書き込みを有効化する場合は、`searchEndpoint` と `searchIndex` を必ずセットで指定する。
- `searchBackend: elastic-cloud` の場合は、`searchApiKeySecretArn` も必須。

## Managed security group mode

既存 Security Group を使わず、スタック側で Lambda 用 Security Group を作る場合:

```yaml
lambdaVpcName: shared-services-vpc
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
createLambdaSecurityGroup: true
lambdaAllowAllOutbound: false
```

このモードでは、作成される Security Group に以下の送信ルールを入れます。

- HTTPS (`tcp/443`) の外向き通信
- VPC 内 DNS (`udp/53`, `tcp/53`)

本番では、NAT Gateway や VPC Endpoint の設計と合わせて egress を詰める前提です。

## Legacy Office conversion

旧 Office 形式 (`.doc`, `.xls`, `.ppt`) は、既存 extractor の前段で `.docx`, `.xlsx`, `.pptx` へ変換します。

- 変換は `cdk_stack_legacyofficeconvert` Lambda が担当します。
- 実装は headless LibreOffice (`soffice`) を前提にしています。
- デフォルトのバイナリパスは `/opt/libreoffice/program/soffice` です。
- 別パスを使う場合は `config.yml` の `sofficeBinary` を変更します。
- 変換済みファイルは `work/<pipelineVersion>/<hash_prefix>/<source_filename>/<document_id>/converted/` 配下に S3 保存され、後続 extractor はそのオブジェクトを読む設計です。

このため、本番では次のいずれかが必要です。

1. LibreOffice を含む Lambda Layer を `/opt/libreoffice/...` に配置する
2. または LibreOffice を含む実行イメージへ切り替える

`soffice` が存在しない状態で旧 Office 形式を投入すると、変換ステップは明示的に失敗します。

## IAM scoping

POC 時の広い権限から、次のように絞っています。

### S3

- `legacyofficeconvert` は `uploads/*` を読み、`work/*` に変換済みファイルを書き込む。
- `inputvalidation` と各 extractor は `uploads/*` を読む。
- extractor、`bedrockenrichment`、`chunkbuild`、`chunkenrichment`、`embedding` は `work/*` を読む/書く。
- validation、index writer、status update は `work/*` を読む。

つまり、バケット全体への `GetObject` / `PutObject` ではなく、処理対象プレフィックス単位の grant にしています。

### SQS / Step Functions

- `kickoff` は対象 SQS の consume 権限だけを持つ。
- `kickoff` はこの stack が作る State Machine の `StartExecution` だけを持つ。
- S3 upload を受ける source queue (`cdk_stack_sf_sqs`) には DLQ (`cdk_stack_sf_dlq`) が付き、同一メッセージが 5 回受信されても処理できない場合は DLQ に移る。

### Bedrock

- `bedrockenrichment` は `bedrockImageModelId` で指定したモデルだけを invoke する。
- `chunkenrichment` は `chunkEnrichmentModelId` を指定した場合だけ、そのモデルを invoke する。
- `embedding` は `embeddingModelId` で指定したモデルだけを invoke する。

### DynamoDB

- `statusTableName` を指定した場合だけ `dynamodb:UpdateItem` を付与する。
- 対象はそのテーブル ARN のみ。

### Textract

Textract の `StartDocumentTextDetection` / `GetDocumentTextDetection` は、AWS 側のリソース指定制約のため `*` のままです。

## Typical deployment patterns

### 1. Existing VPC + existing security group

```yaml
lambdaVpcId: vpc-0123456789abcdef0
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
lambdaSecurityGroupIds:
  - sg-lambda-processing
```

その後に `cdk deploy` を実行します。

### 2. Existing VPC + stack-managed security group

```yaml
lambdaVpcName: shared-services-vpc
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
createLambdaSecurityGroup: true
lambdaAllowAllOutbound: false
```

### 3. Explicit processing bucket name

```yaml
s3BucketName: document-processing-prod-example
```

### 4. VPC + Bedrock + DynamoDB + Elastic Cloud

```yaml
lambdaVpcId: vpc-0123456789abcdef0
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
lambdaSecurityGroupIds:
  - sg-lambda-processing
s3BucketName: document-processing-prod-example
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
