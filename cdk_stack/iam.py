from typing import Any, Dict, Optional

import aws_cdk as cdk
from aws_cdk import aws_iam
from cdk_nag import NagSuppressions
from constructs import Construct


LAMBDA_ROLE_NAMES = [
    "kickoff",
    "inputvalidation",
    "legacyofficeconvert",
    "docxextract",
    "xlsxextract",
    "pptxextract",
    "pdfextract",
    "extractresultvalidation",
    "bedrockenrichment",
    "chunkbuild",
    "chunkenrichment",
    "embedding",
    "indexwriter",
    "updatestatus",
]

STATE_MACHINE_LAMBDA_NAMES = [
    "inputvalidation",
    "legacyofficeconvert",
    "docxextract",
    "xlsxextract",
    "pptxextract",
    "pdfextract",
    "extractresultvalidation",
    "bedrockenrichment",
    "chunkbuild",
    "chunkenrichment",
    "embedding",
    "indexwriter",
    "updatestatus",
]


FOUNDATION_MODEL_PROVIDERS = {
    "amazon",
    "anthropic",
    "cohere",
    "meta",
    "mistral",
    "ai21",
    "stability",
    "deepseek",
}


def _is_inference_profile_id(model_id: str) -> bool:
    prefix = model_id.split(".", 1)[0].lower()
    return prefix not in FOUNDATION_MODEL_PROVIDERS


def _foundation_model_arn(scope: Construct, model_id: str) -> str:
    return cdk.Stack.of(scope).format_arn(
        service="bedrock",
        region=cdk.Stack.of(scope).region,
        account="",
        resource="foundation-model",
        resource_name=model_id,
    )


def _foundation_model_wildcard_arn(scope: Construct, model_id: str) -> str:
    return (
        f"arn:{cdk.Stack.of(scope).partition}:bedrock:*::foundation-model/{model_id}"
    )


def bedrock_model_resources(scope: Construct, model_id: Optional[str]) -> list[str]:
    if not model_id:
        return []
    if model_id.startswith("arn:"):
        return [model_id]
    if _is_inference_profile_id(model_id):
        resources = [
            cdk.Stack.of(scope).format_arn(
                service="bedrock",
                region=cdk.Stack.of(scope).region,
                account=cdk.Stack.of(scope).account,
                resource="inference-profile",
                resource_name=model_id,
            )
        ]
        _prefix, _sep, base_model_id = model_id.partition(".")
        if base_model_id:
            resources.append(_foundation_model_arn(scope, base_model_id))
            resources.append(_foundation_model_wildcard_arn(scope, base_model_id))
        return resources
    return [_foundation_model_arn(scope, model_id)]


def create_iam_role_for_lambdas(
    scope: Construct, services: Dict[str, Any]
) -> Dict[str, aws_iam.Role]:
    iam_roles: Dict[str, aws_iam.Role] = {}

    for name in LAMBDA_ROLE_NAMES:
        iam_roles[name] = aws_iam.Role(
            scope=scope,
            id="cdk_stack_lam_role_" + name,
            assumed_by=aws_iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        iam_roles[name].add_managed_policy(
            aws_iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        )
        if services["network"]["enabled"]:
            iam_roles[name].add_managed_policy(
                aws_iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            )

    return iam_roles


def create_iam_role_for_stepfunction(
    scope: Construct, services: Dict[str, Any]
) -> Dict[str, aws_iam.Role]:
    iam_roles: Dict[str, aws_iam.Role] = {}

    iam_roles["sfunctions"] = aws_iam.Role(
        scope=scope,
        id="cdk_stack_lam_role_sfunctions",
        assumed_by=aws_iam.ServicePrincipal("states.amazonaws.com"),
    )

    for name in STATE_MACHINE_LAMBDA_NAMES:
        services["lambda"][name].grant_invoke(iam_roles["sfunctions"])

    iam_roles["sfunctions"].add_to_policy(
        statement=aws_iam.PolicyStatement(
            resources=[
                f"arn:aws:logs:{cdk.Stack.of(scope).region}:{cdk.Stack.of(scope).account}:log-group:/aws/stepfunctions/cdk_stack_stepfunction_logs:*"
            ],
            actions=[
                "logs:CreateLogDelivery",
                "logs:DeleteLogDelivery",
                "logs:DescribeLogGroups",
                "logs:DescribeResourcePolicies",
                "logs:GetLogDelivery",
                "logs:ListLogDeliveries",
                "logs:PutResourcePolicy",
                "logs:UpdateLogDelivery",
            ],
        )
    )

    iam_roles["sfunctions"].add_to_policy(
        statement=aws_iam.PolicyStatement(
            resources=["*"],
            actions=[
                "xray:GetSamplingRules",
                "xray:GetSamplingTargets",
                "xray:PutTelemetryRecords",
                "xray:PutTraceSegments",
            ],
        )
    )

    NagSuppressions.add_resource_suppressions(
        [iam_roles["sfunctions"]],
        [
            {
                "id": "W12",
                "reason": "This is created for a POC. Customer will restrict resources for X-Ray in production.",
            }
        ],
    )

    return iam_roles


def configure_lambda_permissions(scope: Construct, services: Dict[str, Any]) -> None:
    roles = services["iam_roles"]
    bucket = services["main_s3_bucket"]

    services["sf_sqs"].grant_consume_messages(roles["kickoff"])
    services["sf"].grant_start_execution(roles["kickoff"])

    for name in [
        "inputvalidation",
        "legacyofficeconvert",
        "docxextract",
        "xlsxextract",
        "pptxextract",
        "pdfextract",
    ]:
        bucket.grant_read(roles[name], "uploads/*")

    for name in [
        "legacyofficeconvert",
        "docxextract",
        "xlsxextract",
        "pptxextract",
        "pdfextract",
        "bedrockenrichment",
        "chunkbuild",
        "chunkenrichment",
        "embedding",
    ]:
        bucket.grant_read_write(roles[name], "work/*")

    for name in ["extractresultvalidation", "indexwriter", "updatestatus"]:
        bucket.grant_read(roles[name], "work/*")

    roles["pdfextract"].add_to_policy(
        statement=aws_iam.PolicyStatement(
            resources=["*"],
            actions=[
                "textract:StartDocumentTextDetection",
                "textract:GetDocumentTextDetection",
            ],
        )
    )

    image_model_resources = bedrock_model_resources(
        scope, services["config"].bedrock_image_model_id
    )
    if image_model_resources:
        roles["bedrockenrichment"].add_to_policy(
            statement=aws_iam.PolicyStatement(
                resources=image_model_resources,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
            )
        )

    embedding_model_resources = bedrock_model_resources(
        scope, services["config"].embedding_model_id
    )
    roles["embedding"].add_to_policy(
        statement=aws_iam.PolicyStatement(
            resources=embedding_model_resources,
            actions=["bedrock:InvokeModel"],
        )
    )

    chunk_enrichment_model_resources = bedrock_model_resources(
        scope, services["config"].chunk_enrichment_model_id
    )
    if chunk_enrichment_model_resources:
        roles["chunkenrichment"].add_to_policy(
            statement=aws_iam.PolicyStatement(
                resources=chunk_enrichment_model_resources,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
            )
        )

    if services["config"].status_table_name:
        roles["updatestatus"].add_to_policy(
            statement=aws_iam.PolicyStatement(
                resources=[
                    cdk.Stack.of(scope).format_arn(
                        service="dynamodb",
                        resource="table",
                        resource_name=services["config"].status_table_name,
                    )
                ],
                actions=["dynamodb:UpdateItem"],
            )
        )

    if services["config"].search_api_key_secret_arn:
        roles["indexwriter"].add_to_policy(
            statement=aws_iam.PolicyStatement(
                resources=[services["config"].search_api_key_secret_arn],
                actions=["secretsmanager:GetSecretValue"],
            )
        )
