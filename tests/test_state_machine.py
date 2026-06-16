import unittest

import aws_cdk as cdk
from aws_cdk import assertions, aws_iam, aws_lambda, aws_logs

from cdk_stack.state_machine import create_state_machine


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


class StateMachineRegressionTest(unittest.TestCase):
    def test_optional_enrichment_failures_continue_to_next_required_step(self) -> None:
        app = cdk.App()
        stack = cdk.Stack(app, "StateMachineTestStack")

        role = aws_iam.Role(
            stack,
            "StateMachineRole",
            assumed_by=aws_iam.ServicePrincipal("states.amazonaws.com"),
        )
        log_group = aws_logs.LogGroup(stack, "StateMachineLogs")

        services = {
            "lambda": {
                name: aws_lambda.Function(
                    stack,
                    f"{name}Function",
                    runtime=aws_lambda.Runtime.PYTHON_3_12,
                    handler="index.handler",
                    code=aws_lambda.Code.from_inline("def handler(event, context): return event"),
                )
                for name in PIPELINE_LAMBDA_NAMES
            },
            "sf_iam_roles": {"sfunctions": role},
            "sf_log_group": log_group,
        }

        create_state_machine(stack, services)
        template = assertions.Template.from_stack(stack)
        definition = self._state_machine_definition_string(template)

        self.assertIn(
            '"BedrockEnrichment":{"Next":"ChunkBuild"',
            definition,
        )
        self.assertIn(
            '"ResultPath":"$.bedrock_enrichment_error","Next":"PrepareBedrockEnrichmentFailure"',
            definition,
        )
        self.assertIn(
            '"PrepareBedrockEnrichmentFailure":{"Type":"Pass","Result":{"failed_step":"bedrock_enrichment"',
            definition,
        )
        self.assertIn(
            '"ResultPath":"$.bedrock_enrichment_failure","Next":"ChunkBuild"',
            definition,
        )
        self.assertIn(
            '"ChunkEnrichment":{"Next":"Embedding"',
            definition,
        )
        self.assertIn(
            '"ResultPath":"$.chunk_enrichment_error","Next":"PrepareChunkEnrichmentFailure"',
            definition,
        )
        self.assertIn(
            '"PrepareChunkEnrichmentFailure":{"Type":"Pass","Result":{"failed_step":"chunk_enrichment"',
            definition,
        )
        self.assertIn(
            '"ResultPath":"$.chunk_enrichment_failure","Next":"Embedding"',
            definition,
        )

    def _state_machine_definition_string(self, template: assertions.Template) -> str:
        resources = template.to_json()["Resources"]
        for resource in resources.values():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue

            definition = resource["Properties"]["DefinitionString"]
            if isinstance(definition, dict) and "Fn::Join" in definition:
                _separator, parts = definition["Fn::Join"]
                return "".join(part if isinstance(part, str) else "<TOKEN>" for part in parts)

            self.fail("Unexpected DefinitionString format")

        self.fail("StateMachine resource not found")


if __name__ == "__main__":
    unittest.main()