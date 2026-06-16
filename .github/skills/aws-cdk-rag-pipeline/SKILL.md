---
name: aws-cdk-rag-pipeline
description: Use this skill when creating, reviewing, or modifying an AWS CDK Python stack for a RAG document-processing pipeline using S3, SQS, Lambda, Step Functions, Bedrock, embeddings, and a search backend.
argument-hint: "[task or file path]"
---

# AWS CDK RAG Pipeline Skill

## Purpose

Create, review, and modify AWS CDK Python code for a document-processing RAG pipeline.

Target architecture:

S3 uploads/
→ S3 Event Notification
→ SQS
→ kickoff Lambda
→ Step Functions
→ docx/xlsx/pptx/pdf extraction Lambdas
→ extract result validation
→ optional Bedrock enrichment
→ chunk build
→ embedding
→ index writer
→ status update

## When to use this skill

Use this skill when the user asks to:

- create AWS CDK Python infrastructure
- review AWS CDK code
- modify Lambda / Step Functions / S3 / SQS / IAM definitions
- improve Bedrock or embedding integration
- improve search indexing configuration
- design or refactor the RAG document-processing pipeline

## Project assumptions

- CDK language is Python.
- Runtime is Python 3.12 unless otherwise specified.
- Supported source file types are docx, xlsx, pptx, and pdf.
- Raw uploads are stored under `uploads/`.
- Working data is stored under `work/{pipeline_version}/`.
- Search backend integration must be optional.
- If search endpoint and index are not configured, `SEARCH_NOOP=true`.
- Bedrock enrichment must be optional.
- Embedding model and dimensions must be configurable by environment variable.

## Review checklist

When reviewing CDK code, check:

1. CDK construct structure
2. Resource naming consistency
3. S3 event notification configuration
4. SQS visibility timeout vs Lambda timeout
5. Lambda memory and timeout settings
6. Step Functions state transitions
7. Step Functions input/output contract
8. IAM least privilege
9. S3 prefix-scoped permissions
10. Bedrock model ARN generation
11. Textract permissions for PDF extraction
12. Search backend configuration
13. CloudWatch Logs and X-Ray settings
14. cdk-nag suppressions
15. POC-only settings that must not go to production

## Security rules

- Prefer least-privilege IAM.
- Avoid `resources=["*"]` unless AWS service constraints require it.
- If `resources=["*"]` is used, explain why.
- Treat `RemovalPolicy.DESTROY` as POC-only.
- Treat unencrypted SQS, logs, or buckets as POC-only.
- Do not hard-code production VPC IDs, subnet IDs, security group IDs, endpoints, or account IDs.
- Do not expose secrets in environment variables.

## Step Functions rules

- Keep each Lambda task output contract explicit.
- Do not silently change `result_path` without explaining downstream impact.
- Unsupported file types must short-circuit to the common failed-status update path; do not add a dedicated handler Lambda unless it performs real work.
- `docx`, `xlsx`, `pptx`, and `pdf` must each route to their own extraction Lambda.
- Errors should map to meaningful failure states.

## Lambda rules

- Lambda names should follow `cdk_stack_{stage}` for this project.
- Lambda code should be loaded from `./deploy_code/{stage}/`, while AWS resource names remain `cdk_stack_{stage}`.
- Each Lambda should receive only the environment variables it needs.
- VPC configuration should be optional and driven by `config.yml`.

## Output format

When modifying code:

1. Explain the problem briefly.
2. Show the exact changed section.
3. Include surrounding code before and after the changed lines.
4. Explain deployment or testing commands if needed.

Use Japanese for explanations.
Keep the answer concise.