---
name: docs-readme-sync
description: Use this skill whenever code, config, infrastructure, runtime behavior, output artifacts, or failure handling changes and README.md or docs/ may need to be updated.
argument-hint: "[changed files or task summary]"
---

# Docs / README Sync Skill

## Purpose

コード変更のたびに、`README.md` と `docs/` の更新要否を必ず確認し、必要なら同じタスク内で反映する。

## Mandatory rule

- コード、設定、構成、入出力契約、失敗分類、運用手順のいずれかが変わったら、**必ず** `README.md` と `docs/` の更新要否を確認する。
- 必要な変更があれば、ユーザーに別途言われる前に **同じタスク内で更新する**。
- 変更が不要だった場合も、最終回答で **docs 変更不要の理由を明示する**。

## When to use this skill

Use this skill when the task includes any of the following:

- Lambda / CDK / Step Functions / S3 / SQS / IAM / VPC の変更
- `config.yml` のキー、環境変数、モデル ID、S3 prefix、ファイルパスの変更
- パイプライン順序や処理フローの変更
- 中間成果物、JSON schema、manifest、metadata、output field の変更
- 失敗分類、retry/no-op/best-effort 挙動の変更
- デプロイ手順、実行コマンド、運用手順の変更
- ディレクトリ構成や主要ファイルの追加・削除・改名

## Required workflow

1. まず、今回の変更で **何が変わったか** を列挙する。
2. 次に、変更内容を下の対応表へ当てはめて、更新対象ファイルを決める。
3. 対象ファイルがあれば、**コード変更と同じタスクで** 更新する。
4. 最終回答では、更新した docs を明示する。不要だった場合は不要理由を明示する。

## Mapping guide

| Change type | Files to review/update |
| --- | --- |
| 概要、主要フロー、導入説明、主要設定例 | `README.md` |
| デプロイ設定、`config.yml`、VPC、IAM、Secrets、検索バックエンド | `docs/production-configuration.md` |
| 中間成果物、S3 key、manifest、chunk schema、embedding input/output | `docs/output-artifacts.md` |
| failure state、error category、retry/no-op/best-effort、状態更新 | `docs/error-handling.md` |
| リポジトリ構成、処理段階、ディレクトリ追加 | `docs/repository-guide.md` |
| 作業ルールや恒久運用ルール | `AGENTS.md`, `.github/copilot-instructions.md` |

## Strong triggers

The following changes almost always require doc updates:

- 新しい config key / env var を追加した
- 新しい Lambda / Step Functions state / failure state を追加した
- `uploads/`, `work/`, `search/chunks/`, `vectors/` などのパスや artifacts を変えた
- `text`, `embedding_text`, `summary`, `keywords_*` のような field を追加・変更した
- 既存 no-op 挙動や fallback 挙動を変えた
- ユーザーや運用者が知るべき前提条件を変えた

## Output requirement

When finishing a code-changing task, always include one of these in the final response:

- `Updated docs: ...`
- `No docs changes were needed because ...`

Do not leave documentation status implicit.
