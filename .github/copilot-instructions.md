# Copilot Instructions

このファイルは **What（何をするか）** を定義します。  
手順、コマンド、境界線は `AGENTS.md` に記載し、ここには重複して書かないでください。

## 基本方針

- このリポジトリは **AWS CDK v2 ベースの Python プロジェクト** として扱う。
- Python の依存管理は **`uv` + `pyproject.toml` + `uv.lock`** を正とする。
- ドキュメントは **日本語を基本** とし、コマンド、パス、AWS リソース名、コード識別子は英語のまま保持する。

## アーキテクチャ

- CDK アプリのエントリーポイントは `app.py`。
- インフラ定義の中核は `cdk_stack/stack.py` と責務別モジュール群。
- Lambda 実装は `deploy_code/<short-stage>/lambda_function.py` 単位で管理する。
- 補助ドキュメントは `docs/`、デプロイ設定は `config.yml`、サンプル入力は `sample-inputs/` に置く。

## 実装ルール

- 既存のディレクトリ分割を維持し、責務をまたいでファイルを混在させない。
- Lambda 追加・変更時は、CDK 側のアセットパスと実装ディレクトリを一致させる。
- `deploy_code` のディレクトリ名が短縮形でも、Lambda の function_name / resource 名は `cdk_stack_{stage}` を使う。
- 既存の **resource-scoped IAM** と **optional VPC attachment** の考え方を維持する。
- IAM 権限やネットワーク設定は、必要最小限を優先する。
- AWS アカウント ID、リージョン、VPC ID、Security Group ID などの環境依存値をコードへ固定しない。

## Python / CDK

- Python 依存の追加・更新は `pyproject.toml` に集約する。
- `requirements.txt` や `setup.py` を再導入しない。
- CDK 実行は `cdk.json` の `uv run python app.py` を前提とする。
- 生成物 (`cdk.out/`, `__pycache__/`, `.venv/`, `.DS_Store`) はソースとして扱わない。

## ドキュメント更新

- 構成変更があれば `docs/repository-guide.md` を更新する。
- デプロイ手順や設定値が変わる場合は `README.md` と `docs/production-configuration.md` を更新する。
- コード、設定、入出力契約、失敗分類、パイプライン順序、運用手順を変更したタスクでは、`.github/skills/docs-readme-sync/SKILL.md` の基準で `README.md` と `docs/` の更新要否を毎回確認する。
- docs 更新が不要だった場合も、最終回答で「変更不要」の判断を明示する。
- What/How の責務分離を維持し、方針はこのファイル、手順は `AGENTS.md` に置く。
