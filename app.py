#!/usr/bin/env python3
import os
from pathlib import Path

import aws_cdk as cdk

from cdk_stack import (
    CONFIG_FILE_NAME,
    DocumentProcessingStack,
    load_config_file,
    requires_cdk_environment,
)


config_path = Path(__file__).resolve().with_name(CONFIG_FILE_NAME)
deployment_settings = load_config_file(config_path)

app = cdk.App()
stack_kwargs = {}

if requires_cdk_environment(deployment_settings):
    stack_kwargs["env"] = cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    )

DocumentProcessingStack(
    app,
    "document-processing",
    deployment_settings=deployment_settings,
    # If you don't specify 'env', this stack will be environment-agnostic.
    # Account/Region-dependent features and context lookups will not work,
    # but a single synthesized template can be deployed anywhere.
    # Uncomment the next line to specialize this stack for the AWS Account
    # and Region that are implied by the current CLI configuration.
    # env=cdk.Environment(account=os.getenv('CDK_DEFAULT_ACCOUNT'), region=os.getenv('CDK_DEFAULT_REGION')),
    # Uncomment the next line if you know exactly what Account and Region you
    # want to deploy the stack to. */
    # env=cdk.Environment(account='123456789012', region='us-east-1'),
    # For more information, see https://docs.aws.amazon.com/cdk/latest/guide/environments.html
    **stack_kwargs,
)

app.synth()
