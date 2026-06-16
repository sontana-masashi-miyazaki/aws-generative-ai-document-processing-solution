# AGENTS.md

このファイルは **How（どうやるか）** を定義します。  
方針や恒久ルールは `.github/copilot-instructions.md` を参照し、ここにはコマンド、手順、境界線だけを書きます。

## 基本コマンド

```bash
uv sync
uv lock
uv run python -m compileall app.py cdk_stack deploy_code
cdk synth
cdk bootstrap
cdk deploy
```

## よく使う実行例

### 本番寄りの VPC 付きデプロイ

```bash
# config.yml を編集
lambdaVpcId: vpc-0123456789abcdef0
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
lambdaSecurityGroupIds:
  - sg-lambda-processing

# その後にデプロイ
cdk deploy
```

### Elastic Cloud / Bedrock / DynamoDB を含むデプロイ

```bash
# config.yml を編集
lambdaVpcId: vpc-0123456789abcdef0
lambdaSubnetIds:
  - subnet-private-a
  - subnet-private-c
lambdaSecurityGroupIds:
  - sg-lambda-processing
bedrockImageModelId: anthropic.claude-3-5-sonnet-20240620-v1:0
embeddingModelId: amazon.titan-embed-text-v2:0
embeddingDimensions: 1024
statusTableName: document-processing-status
searchBackend: elastic-cloud
searchEndpoint: https://cluster-id.region.aws.found.io
searchIndex: document-chunks
searchApiKeySecretArn: arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:elastic-cloud-api-key

# その後にデプロイ
cdk deploy
```

## 作業手順

1. Python / CDK 変更前に `uv sync` を実行する。
2. 依存変更時は `pyproject.toml` を更新し、`uv lock` を実行する。
3. CDK、Lambda、設定変更後は `uv run python -m compileall ...` を実行する。
4. インフラや `cdk.json` を変更したら `cdk synth` を実行する。
5. パス変更や構成変更をしたら `README.md` と `docs/` の参照を更新する。
6. 作業後は `cdk.out/` や `__pycache__/` などの生成物を残さない。

## Always Do
- `uv` ベースの運用を維持する。
- コード変更後は、変更範囲に応じた検証を必ず実行する。
  検証できない場合は、理由と代替確認方法を明記する。
- ファイル移動時は `cdk.json`、`README`、`docs`、`CDK` アセット参照を一緒に直す。
- `deploy_code/<short-name>/lambda_function.py` と `cdk_stack/lambdas.py` / `cdk_stack/stack.py` の対応関係を壊さない。
- `deploy_code` の短いディレクトリ名を変える場合でも、AWS 上の Lambda 名・リソース名 (`cdk_stack_*`) を使う。
- デプロイ設定は `config.yml` を source of truth にして、`cdk deploy -c ...` を再導入しない。
- IAM / VPC 関連の変更後は `cdk synth` でテンプレート生成まで確認する。
- 完了報告では、変更ファイル・実行した検証・検証結果・未実行の検証理由を明記する。

## Ask First
- 新しい Python 依存や CLI ツールを追加するとき
- IAM の権限を広げるとき
- VPC / Security Group / サブネットのデフォルト動作を変えるとき
- AWS リソース構成を大きく変更するとき
- `cdk bootstrap` を実行するとき
- `cdk deploy` を実行するとき
- 既存リソース名、S3 prefix、DynamoDB table、OpenSearch index 名を変更するとき
- 本文・画像・機密情報がログに出る可能性のある変更を行うとき

## Never Do
- `requirements.txt` や `setup.py` を再導入しない。
- `cdk.out/`, `__pycache__/`, `.venv/`, `.DS_Store` をコミットしない。
- `.env` や認証情報をコミットしない。
- 無関係なファイルの整理やリネームをついでに行わない。
- `deploy_code` 配下のディレクトリ名だけを単独で変更しない。
- `config.yml` の代わりに deployment setting を `cdk deploy -c ...` へ戻さない。
- 検証未実行のまま「動作確認済み」「完了」と言わない。
- `cdk deploy` を明示的な依頼なしに実行しない。
