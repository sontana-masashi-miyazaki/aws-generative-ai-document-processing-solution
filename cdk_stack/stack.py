from typing import Any, Dict, Mapping, Optional

import boto3
import botocore

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_iam,
    aws_lambda,
    aws_lambda_event_sources,
    aws_logs,
    aws_s3,
    aws_s3_notifications,
    aws_sqs,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .config import load_deployment_config
from .iam import (
    configure_lambda_permissions,
    create_iam_role_for_lambdas,
    create_iam_role_for_stepfunction,
)
from .lambdas import create_kickoff_lambda, create_lambda_functions
from .networking import resolve_lambda_network
from .state_machine import create_state_machine


class DocumentProcessingStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        deployment_settings: Mapping[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self._deployment_settings = deployment_settings

        services = self.create_services()
        self.create_events(services)

    def create_events(self, services: Dict[str, Any]) -> None:
        services["main_s3_bucket"].add_event_notification(
            aws_s3.EventType.OBJECT_CREATED,
            aws_s3_notifications.SqsDestination(services["sf_sqs"]),
            aws_s3.NotificationKeyFilter(prefix="uploads/"),
        )

        services["lambda"]["kickoff"].add_event_source(
            aws_lambda_event_sources.SqsEventSource(services["sf_sqs"], batch_size=1)
        )

    def create_services(self) -> Dict[str, Any]:
        services: Dict[str, Any] = {}
        services["config"] = load_deployment_config(self._deployment_settings)
        main_bucket_kwargs = {
            "removal_policy": cdk.RemovalPolicy.DESTROY,
            "encryption": aws_s3.BucketEncryption.S3_MANAGED,
            "access_control": aws_s3.BucketAccessControl.BUCKET_OWNER_FULL_CONTROL,
        }
        configured_bucket_name = services["config"].s3_bucket_name
        existing_stack_bucket_name = _existing_stack_bucket_name(self.stack_name)

        if configured_bucket_name:
            existing_bucket = _lookup_bucket(configured_bucket_name)
            if existing_bucket["exists"]:
                _validate_bucket_region(self, configured_bucket_name, existing_bucket["region"])
                if existing_stack_bucket_name == configured_bucket_name:
                    services["main_s3_bucket"] = aws_s3.Bucket(
                        self,
                        "cdk_stack",
                        bucket_name=configured_bucket_name,
                        **main_bucket_kwargs,
                    )
                    services["managed_s3_bucket"] = services["main_s3_bucket"]
                else:
                    if existing_stack_bucket_name:
                        services["managed_s3_bucket"] = aws_s3.Bucket(
                            self,
                            "cdk_stack",
                            **main_bucket_kwargs,
                        )
                    services["main_s3_bucket"] = aws_s3.Bucket.from_bucket_name(
                        self,
                        "cdk_stack_existing",
                        configured_bucket_name,
                    )
                services["reused_existing_s3_bucket"] = True
            else:
                services["main_s3_bucket"] = aws_s3.Bucket(
                    self,
                    "cdk_stack",
                    bucket_name=configured_bucket_name,
                    **main_bucket_kwargs,
                )
                services["managed_s3_bucket"] = services["main_s3_bucket"]
                services["reused_existing_s3_bucket"] = False
        else:
            services["main_s3_bucket"] = aws_s3.Bucket(
                self,
                "cdk_stack",
                **main_bucket_kwargs,
            )
            services["managed_s3_bucket"] = services["main_s3_bucket"]
            services["reused_existing_s3_bucket"] = False

        bucket_bootstrap_lambda = aws_lambda.Function(
            self,
            "cdk_stack_bucketbootstrap",
            function_name="cdk_stack_bucketbootstrap",
            code=aws_lambda.Code.from_asset("./deploy_code/bucketbootstrap/"),
            handler="lambda_function.lambda_handler",
            runtime=aws_lambda.Runtime.PYTHON_3_12,
            timeout=cdk.Duration.minutes(1),
            memory_size=256,
        )
        bucket_bootstrap_lambda.add_to_role_policy(
            statement=aws_iam.PolicyStatement(
                resources=[services["main_s3_bucket"].bucket_arn],
                actions=["s3:GetBucketCORS", "s3:PutBucketCORS"],
            )
        )
        bucket_bootstrap_lambda.add_to_role_policy(
            statement=aws_iam.PolicyStatement(
                resources=[services["main_s3_bucket"].arn_for_objects("uploads/")],
                actions=["s3:PutObject"],
            )
        )
        bucket_bootstrap_lambda.grant_invoke(
            aws_iam.ServicePrincipal("cloudformation.amazonaws.com")
        )

        bucket_bootstrap = cdk.CustomResource(
            self,
            "cdk_stack_bucket_bootstrap",
            service_token=bucket_bootstrap_lambda.function_arn,
            properties={"BucketName": services["main_s3_bucket"].bucket_name},
        )
        bucket_bootstrap.node.add_dependency(services["main_s3_bucket"])
        services["bucket_bootstrap_lambda"] = bucket_bootstrap_lambda
        services["bucket_bootstrap"] = bucket_bootstrap

        services["sf_sqs_dlq"] = aws_sqs.Queue(
            self,
            "cdk_stack_sf_dlq",
            queue_name="cdk_stack_sf_dlq",
            retention_period=cdk.Duration.days(14),
        )

        services["sf_sqs"] = aws_sqs.Queue(
            self,
            "cdk_stack_sf_sqs",
            queue_name="cdk_stack_sf_sqs",
            visibility_timeout=cdk.Duration.minutes(5),
            dead_letter_queue=aws_sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=services["sf_sqs_dlq"],
            ),
        )

        services["sf_log_group"] = aws_logs.LogGroup(
            self,
            "/aws/stepfunctions/cdk_stack_stepfunction_logs",
            log_group_name="/aws/stepfunctions/cdk_stack_stepfunction_logs",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            retention=aws_logs.RetentionDays.ONE_WEEK,
        )

        services["network"] = resolve_lambda_network(self, services["config"])
        services["iam_roles"] = create_iam_role_for_lambdas(self, services)
        services["lambda"] = create_lambda_functions(self, services)
        services["sf_iam_roles"] = create_iam_role_for_stepfunction(self, services)
        services["sf"] = create_state_machine(self, services)
        services["lambda"]["kickoff"] = create_kickoff_lambda(self, services)

        configure_lambda_permissions(self, services)

        NagSuppressions.add_resource_suppressions(
            [services["sf_log_group"]],
            [
                {
                    "id": "W84",
                    "reason": "This is created for a POC. Customer will enable encryption in production.",
                }
            ],
        )

        NagSuppressions.add_resource_suppressions(
            [services["sf_sqs"], services["sf_sqs_dlq"]],
            [
                {
                    "id": "W48",
                    "reason": "This is created for a POC. Customer will enable encryption in production.",
                }
            ],
        )

        managed_bucket = services.get("managed_s3_bucket")
        if managed_bucket is not None:
            NagSuppressions.add_resource_suppressions(
                [managed_bucket],
                [
                    {
                        "id": "W51",
                        "reason": "This is created for a POC. Customer will create the bucket policy in production.",
                    },
                    {
                        "id": "W35",
                        "reason": "This is created for a POC. Customer will have access logging configured in production.",
                    },
                ],
            )

        NagSuppressions.add_resource_suppressions(
            [bucket_bootstrap_lambda],
            [
                {
                    "id": "W58",
                    "reason": "The custom resource Lambda needs CloudWatch Logs permissions.",
                },
                {
                    "id": "W89",
                    "reason": "The bucket bootstrap custom resource only needs S3 control plane access and should stay outside the VPC.",
                },
                {
                    "id": "W92",
                    "reason": "The custom resource Lambda does not require reserved concurrency for this workflow.",
                },
            ],
        )

        return services


def _existing_stack_bucket_name(stack_name: str) -> Optional[str]:
    client = boto3.client("cloudformation")
    try:
        resources = client.list_stack_resources(StackName=stack_name)["StackResourceSummaries"]
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ValidationError":
            return None
        raise

    for resource in resources:
        if resource.get("ResourceType") == "AWS::S3::Bucket":
            physical_id = resource.get("PhysicalResourceId")
            if isinstance(physical_id, str) and physical_id:
                return physical_id
    return None


def _lookup_bucket(bucket_name: str) -> Dict[str, Any]:
    client = boto3.client("s3")
    try:
        client.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as exc:
        status_code = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        code = exc.response.get("Error", {}).get("Code")
        if status_code == 404 or code in {"404", "NoSuchBucket", "NotFound"}:
            return {"exists": False, "region": None}
        if status_code == 403 or code in {"403", "AccessDenied"}:
            raise ValueError(
                f"S3 bucket '{bucket_name}' already exists but is not accessible with the current AWS credentials."
            ) from exc
        raise

    location = client.get_bucket_location(Bucket=bucket_name).get("LocationConstraint")
    region = "us-east-1" if location in (None, "") else location
    return {"exists": True, "region": region}


def _validate_bucket_region(scope: Construct, bucket_name: str, bucket_region: Optional[str]) -> None:
    stack_region = Stack.of(scope).region
    if not bucket_region or not isinstance(stack_region, str) or cdk.Token.is_unresolved(stack_region):
        return
    if bucket_region != stack_region:
        raise ValueError(
            f"S3 bucket '{bucket_name}' exists in region '{bucket_region}', but this stack is deploying to '{stack_region}'."
        )
