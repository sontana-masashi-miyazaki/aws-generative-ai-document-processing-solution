# Docs

このフォルダには、このリポジトリの構成とデプロイ設定を整理した補足ドキュメントを置きます。

- [Repository Guide](repository-guide.md): フォルダ/ファイル構成と各役割
- [Production Configuration](production-configuration.md): VPC接続、`config.yml`、IAMスコープの考え方
- [Output Artifacts](output-artifacts.md): `objects.jsonl`、`chunks.jsonl`、`enriched_chunks.jsonl`、`embeddings.jsonl` まで含めた中間成果物の共通仕様
- [Word Extraction Details](word-extraction.md): DOCX の XML 解析、正規化、出力アーティファクト
- [Excel Extraction Details](excel-extraction.md): XLSX の XML 解析、正規化、出力アーティファクト
- [PowerPoint Extraction Details](powerpoint-extraction.md): PPTX の XML 解析、正規化、出力アーティファクト
- [Error Handling](error-handling.md): Step Functions と各 Lambda の失敗分類、伝播、no-op / best-effort の挙動
