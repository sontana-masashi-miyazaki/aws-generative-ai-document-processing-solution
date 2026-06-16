# Repository Guide

このドキュメントは、`aws-generative-ai-document-processing-solution` 配下の主要フォルダとファイルの目的をまとめたものです。

`cdk.out/asset.*` のような生成アセットは運用上の参照価値が低いため、ここでは省略しています。

## Top-level structure

| Path | Purpose |
| --- | --- |
| `.git/` | Git の管理情報。 |
| `__pycache__/` | Python のキャッシュ。 |
| `cdk.out/` | `cdk synth` の生成物。CloudFormation テンプレート、マニフェスト、アセット定義が入る。 |
| `deploy_code/` | 各 Lambda 関数の実装。 |
| `docs/` | 構成説明や運用設定の補足ドキュメント。 |
| `cdk_stack/` | CDK スタック定義の Python パッケージ。 |
| `sample-inputs/` | パイプライン投入用のサンプル入力ファイル置き場。 |
| `config.yml` | デプロイ設定の source of truth。VPC、S3、モデル ID などをここで指定する。 |
| `app.py` | CDK アプリのエントリーポイント。 |
| `cdk.json` | CDK の実行設定。`app` と feature flag を持つ。 |
| `.gitignore` | 生成物やワークステーション固有ファイルを Git 管理から除外する。 |
| `pyproject.toml` | `uv` が参照する Python プロジェクト定義と依存関係。 |
| `uv.lock` | `uv` が生成するロックファイル。依存バージョンを固定する。 |
| `README.md` | セットアップとデプロイ手順のメインドキュメント。 |
| `CODE_OF_CONDUCT.md` | 行動規範。 |
| `CONTRIBUTING.md` | コントリビューション手順。 |
| `LICENSE` | ライセンス。 |

## CDK application

### `app.py`

CDK アプリの起点です。`config.yml` を読み込み、必要に応じて account/region 付きで `DocumentProcessingStack` を synth します。VPC lookup が必要なときだけ `env` を付けます。

### `cdk_stack/`

| Path | Purpose |
| --- | --- |
| `__init__.py` | `DocumentProcessingStack` のパッケージ export。 |
| `stack.py` | S3、SQS、ログ、イベント連携などスタック全体の組み立てを担当する。`s3BucketName` があればメイン S3 バケット名もここで固定する。 |
| `config.py` | `config.yml` の読み込み、VPC lookup / バケット名などのデプロイ設定検証を担当する。 |
| `networking.py` | VPC ID または VPC 名での参照、Security Group、Lambda 用ネットワーク引数を解決する。 |
| `iam.py` | Lambda/Step Functions 用 IAM ロールと権限付与を定義する。 |
| `lambdas.py` | Lambda 環境変数、関数定義、kickoff Lambda の作成を担当する。 |
| `state_machine.py` | Step Functions のタスク、Choice、Fail state、状態機械本体を組み立てる。 |

## Support assets

| Path | Purpose |
| --- | --- |
| `sample-inputs/sample-birth-certificate-application.pdf` | 入力例として使えるサンプル PDF。 |

## Lambda implementation folders

`deploy_code/` には、1ディレクトリごとに 1 つの Lambda 関数が入っています。
ディレクトリ名は短縮形ですが、CDK で作成する Lambda 関数名・リソース名は `cdk_stack_*` を使います。

| Path | Purpose |
| --- | --- |
| `kickoff/` | S3 イベントを受けて Step Functions 実行を開始する入口。 |
| `inputvalidation/` | 入力ファイルの拡張子、サイズ、S3 URI、作業用プレフィックスを検証・正規化する。 |
| `legacyofficeconvert/` | 旧 Office 形式 (`.doc`, `.xls`, `.ppt`) を `.docx`, `.xlsx`, `.pptx` へ変換する。 |
| `docxextract/` | DOCX の本文・画像・構造情報を抽出する。 |
| `xlsxextract/` | XLSX のシート/セル/画像を抽出する。 |
| `pptxextract/` | PPTX のスライド内容や画像を抽出する。 |
| `pdfextract/` | Textract を使って PDF を解析し、構造化出力を作る。 |
| `extractresultvalidation/` | 抽出済みアーティファクトが揃っているかを検証する。 |
| `bedrockenrichment/` | 画像がある場合に Bedrock で説明文を付与する。 |
| `chunkbuild/` | 抽出テキストを検索用チャンクに分割する。 |
| `chunkenrichment/` | chunk に summary / keywords / aliases / entities / `embedding_text` を付与する。 |
| `embedding/` | チャンクを埋め込みベクトルに変換する。 |
| `indexwriter/` | AWS OpenSearch または Elastic Cloud へインデックスを書き込む。 |
| `updatestatus/` | 必要に応じて DynamoDB へ最終状態と optional failure 情報を書き戻す。 |

## Runtime flow

処理全体の流れは次のとおりです。

1. `kickoff` が S3 アップロードをトリガーに Step Functions を起動する。
2. `inputvalidation` が入力を正規化する。
3. 必要に応じて `legacyofficeconvert` が旧 Office 形式を OOXML に変換する。
4. ファイル種別ごとの extractor が構造化データを生成する。
5. `extractresultvalidation` が抽出結果を確認する。
6. 必要に応じて `bedrockenrichment` が画像説明を付与する。失敗時は raw object にフォールバックして続行する。
7. `chunkbuild` が検索用チャンクを作る。
8. `chunkenrichment` が検索補助情報と `embedding_text` を付与する。失敗時は `chunks.jsonl` にフォールバックして続行する。
9. `embedding` がベクトルを作る。
10. `indexwriter` が設定された検索バックエンドに書き込む。
11. `updatestatus` が外部状態を更新する。
