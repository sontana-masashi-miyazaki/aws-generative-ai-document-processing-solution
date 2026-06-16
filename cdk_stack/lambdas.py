from typing import Any, Dict

import aws_cdk as cdk
from aws_cdk import aws_lambda
from cdk_nag import NagSuppressions
from constructs import Construct

from .networking import lambda_network_kwargs


PIPELINE_LAMBDA_NAMES = [
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


def lambda_environment(name: str, services: Dict[str, Any]) -> Dict[str, str]:
    config = services["config"]
    environment = {
        "PROCESSING_BUCKET": services["main_s3_bucket"].bucket_name,
        "DEFAULT_PIPELINE_VERSION": config.pipeline_version,
        "PIPELINE_VERSION": config.pipeline_version,
    }

    if name == "bedrockenrichment" and config.bedrock_image_model_id:
        environment["BEDROCK_IMAGE_MODEL_ID"] = config.bedrock_image_model_id

    if name == "embedding":
        environment["EMBEDDING_MODEL_ID"] = config.embedding_model_id
        if config.embedding_dimensions is not None:
            environment["EMBEDDING_DIMENSIONS"] = str(config.embedding_dimensions)

    if name == "chunkenrichment":
        if config.chunk_enrichment_model_id:
            environment["CHUNK_ENRICHMENT_MODEL_ID"] = config.chunk_enrichment_model_id

    if name == "legacyofficeconvert":
        environment["SOFFICE_BIN"] = config.soffice_binary

    if name == "indexwriter":
        if config.search_endpoint and config.search_index:
            environment["SEARCH_BACKEND"] = config.search_backend
            environment["SEARCH_ENDPOINT"] = config.search_endpoint
            environment["SEARCH_INDEX"] = config.search_index
            environment["SEARCH_NOOP"] = "false"
            if config.search_api_key_secret_arn:
                environment["SEARCH_API_KEY_SECRET_ARN"] = config.search_api_key_secret_arn
        else:
            environment["SEARCH_NOOP"] = "true"
        if config.embedding_dimensions is not None:
            environment["EMBEDDING_DIMENSIONS"] = str(config.embedding_dimensions)

    if name == "updatestatus" and config.status_table_name:
        environment["STATUS_TABLE"] = config.status_table_name

    return environment


def create_lambda_functions(
    scope: Construct, services: Dict[str, Any]
) -> Dict[str, aws_lambda.Function]:
    lambda_functions: Dict[str, aws_lambda.Function] = {}
    network_kwargs = lambda_network_kwargs(services["network"])

    for name in PIPELINE_LAMBDA_NAMES:
        lambda_functions[name] = aws_lambda.Function(
            scope=scope,
            id="cdk_stack_" + name,
            function_name="cdk_stack_" + name,
            code=aws_lambda.Code.from_asset(f"./deploy_code/{name}/"),
            handler="lambda_function.lambda_handler",
            runtime=aws_lambda.Runtime.PYTHON_3_12,
            timeout=cdk.Duration.minutes(15),
            memory_size=3000,
            role=services["iam_roles"][name],
            environment=lambda_environment(name, services),
            **network_kwargs,
        )

    lambda_suppressions = [
        {
            "id": "W58",
            "reason": "Lambda functions have permission to write CloudWatch Logs.",
        },
        {
            "id": "W92",
            "reason": "This is created for a POC. Customer will define ReservedConcurrentExecutions in production.",
        },
    ]
    if not services["network"]["enabled"]:
        lambda_suppressions.insert(
            0,
            {
                "id": "W89",
                "reason": "VPC attachment is optional and only enabled when production config is supplied.",
            },
        )

    NagSuppressions.add_resource_suppressions(
        list(lambda_functions.values()),
        lambda_suppressions,
    )

    return lambda_functions


def create_kickoff_lambda(
    scope: Construct, services: Dict[str, Any]
) -> aws_lambda.Function:
    kickoff_lambda = aws_lambda.Function(
        scope=scope,
        id="cdk_stack_kickoff",
        function_name="cdk_stack_kickoff",
        code=aws_lambda.Code.from_asset("./deploy_code/kickoff/"),
        handler="lambda_function.lambda_handler",
        runtime=aws_lambda.Runtime.PYTHON_3_12,
        timeout=cdk.Duration.minutes(5),
        memory_size=3000,
        role=services["iam_roles"]["kickoff"],
        environment={
            "sqs_url": services["sf_sqs"].queue_url,
            "state_machine_arn": services["sf"].state_machine_arn,
            "DEFAULT_PIPELINE_VERSION": services["config"].pipeline_version,
        },
        **lambda_network_kwargs(services["network"]),
    )

    kickoff_suppressions = [
        {
            "id": "W58",
            "reason": "Lambda functions have permission to write CloudWatch Logs.",
        },
        {
            "id": "W92",
            "reason": "This is created for a POC. Customer will define ReservedConcurrentExecutions in production.",
        },
        {
            "id": "W48",
            "reason": "This is created for a POC. Customer will enable encryption in production.",
        },
    ]
    if not services["network"]["enabled"]:
        kickoff_suppressions.insert(
            0,
            {
                "id": "W89",
                "reason": "VPC attachment is optional and only enabled when production config is supplied.",
            },
        )

    NagSuppressions.add_resource_suppressions([kickoff_lambda], kickoff_suppressions)

    return kickoff_lambda
