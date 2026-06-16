from typing import Any, Dict

from aws_cdk import aws_ssm, aws_stepfunctions, aws_stepfunctions_tasks
from constructs import Construct


def _failure_context_pass(
    scope: Construct,
    task_id: str,
    result_path: str,
    failed_step: str,
    target_status: str,
    retryable: bool,
) -> aws_stepfunctions.Pass:
    return aws_stepfunctions.Pass(
        scope,
        task_id,
        result=aws_stepfunctions.Result.from_object(
            {
                "failed_step": failed_step,
                "target_status": target_status,
                "retryable": retryable,
            }
        ),
        result_path=result_path,
    )


def create_state_machine(
    scope: Construct, services: Dict[str, Any]
) -> aws_stepfunctions.StateMachine:
    task_input_validation = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "InputValidation",
        lambda_function=services["lambda"]["inputvalidation"],
        payload_response_only=True,
        result_path="$",
    )
    task_legacy_office_convert = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "LegacyOfficeConvert",
        lambda_function=services["lambda"]["legacyofficeconvert"],
        payload_response_only=True,
        result_path="$",
    )

    task_docx_extract = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "DocxExtract",
        lambda_function=services["lambda"]["docxextract"],
        payload_response_only=True,
        result_path="$",
    )
    task_xlsx_extract = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "XlsxExtract",
        lambda_function=services["lambda"]["xlsxextract"],
        payload_response_only=True,
        result_path="$",
    )
    task_pptx_extract = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "PptxExtract",
        lambda_function=services["lambda"]["pptxextract"],
        payload_response_only=True,
        result_path="$",
    )
    task_pdf_extract = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "PdfExtract",
        lambda_function=services["lambda"]["pdfextract"],
        payload_response_only=True,
        result_path="$",
    )

    task_extract_result_validation = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "ExtractResultValidation",
        lambda_function=services["lambda"]["extractresultvalidation"],
        payload_response_only=True,
        result_path="$",
    )

    task_bedrock_enrichment = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "BedrockEnrichment",
        lambda_function=services["lambda"]["bedrockenrichment"],
        payload_response_only=True,
        result_path="$",
    )

    task_chunk_build = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "ChunkBuild",
        lambda_function=services["lambda"]["chunkbuild"],
        payload_response_only=True,
        result_path="$",
    )

    task_chunk_enrichment = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "ChunkEnrichment",
        lambda_function=services["lambda"]["chunkenrichment"],
        payload_response_only=True,
        result_path="$",
    )

    task_embedding = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "Embedding",
        lambda_function=services["lambda"]["embedding"],
        payload_response_only=True,
        result_path="$",
    )

    task_index_writer = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "IndexWriter",
        lambda_function=services["lambda"]["indexwriter"],
        payload_response_only=True,
        result_path="$",
    )

    task_update_status = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "UpdateStatusSucceeded",
        lambda_function=services["lambda"]["updatestatus"],
        payload_response_only=True,
        result_path="$",
    )
    task_update_status_failed = aws_stepfunctions_tasks.LambdaInvoke(
        scope,
        "UpdateStatusFailed",
        lambda_function=services["lambda"]["updatestatus"],
        payload=aws_stepfunctions.TaskInput.from_object(
            {
                "state": aws_stepfunctions.JsonPath.entire_payload,
                "target_status": aws_stepfunctions.JsonPath.string_at(
                    "$.failure_context.target_status"
                ),
                "failure_context": aws_stepfunctions.JsonPath.object_at("$.failure_context"),
                "error": aws_stepfunctions.JsonPath.object_at("$.failure_error"),
            }
        ),
        payload_response_only=True,
        result_path="$.status_update",
    )

    prepare_unsupported_failure_error = aws_stepfunctions.Pass(
        scope,
        "PrepareUnsupportedFailureError",
        result=aws_stepfunctions.Result.from_object({"Error": "UNSUPPORTED_FILE_TYPE"}),
        result_path="$.failure_error",
    )
    prepare_unsupported_failure_context = _failure_context_pass(
        scope,
        "PrepareUnsupportedFailureContext",
        "$.failure_context",
        "inputvalidation",
        "UNSUPPORTED_FILE_TYPE",
        False,
    )
    prepare_input_validation_failure_context = _failure_context_pass(
        scope,
        "PrepareInputValidationFailureContext",
        "$.failure_context",
        "inputvalidation",
        "EXTRACT_FAILED",
        False,
    )
    prepare_legacy_office_conversion_failure_context = _failure_context_pass(
        scope,
        "PrepareLegacyOfficeConversionFailureContext",
        "$.failure_context",
        "legacy_office_convert",
        "EXTRACT_FAILED",
        False,
    )
    prepare_extract_failure_context = _failure_context_pass(
        scope,
        "PrepareExtractFailureContext",
        "$.failure_context",
        "extract",
        "EXTRACT_FAILED",
        False,
    )
    prepare_embedding_failure_context = _failure_context_pass(
        scope,
        "PrepareEmbeddingFailureContext",
        "$.failure_context",
        "embedding",
        "EMBEDDING_FAILED",
        True,
    )
    prepare_index_failure_context = _failure_context_pass(
        scope,
        "PrepareIndexFailureContext",
        "$.failure_context",
        "index",
        "INDEX_FAILED",
        True,
    )
    prepare_bedrock_enrichment_failure = _failure_context_pass(
        scope,
        "PrepareBedrockEnrichmentFailure",
        "$.bedrock_enrichment_failure",
        "bedrock_enrichment",
        "ENRICHMENT_FAILED",
        True,
    )
    prepare_chunk_enrichment_failure = _failure_context_pass(
        scope,
        "PrepareChunkEnrichmentFailure",
        "$.chunk_enrichment_failure",
        "chunk_enrichment",
        "CHUNK_ENRICHMENT_FAILED",
        True,
    )

    fail_unsupported = aws_stepfunctions.Fail(
        scope,
        "UnsupportedFileTypeFail",
        error="UNSUPPORTED_FILE_TYPE",
    )

    fail_extract = aws_stepfunctions.Fail(
        scope,
        "ExtractFailed",
        error="EXTRACT_FAILED",
    )

    fail_embedding = aws_stepfunctions.Fail(
        scope,
        "EmbeddingFailed",
        error="EMBEDDING_FAILED",
    )

    fail_index = aws_stepfunctions.Fail(
        scope,
        "IndexFailed",
        error="INDEX_FAILED",
    )
    fail_workflow = aws_stepfunctions.Fail(
        scope,
        "WorkflowFailed",
        error="WORKFLOW_FAILED",
    )

    failure_choice = aws_stepfunctions.Choice(scope, "FailureStatusChoice")
    failure_choice.when(
        aws_stepfunctions.Condition.string_equals(
            "$.failure_context.target_status", "UNSUPPORTED_FILE_TYPE"
        ),
        fail_unsupported,
    )
    failure_choice.when(
        aws_stepfunctions.Condition.string_equals(
            "$.failure_context.target_status", "EXTRACT_FAILED"
        ),
        fail_extract,
    )
    failure_choice.when(
        aws_stepfunctions.Condition.string_equals(
            "$.failure_context.target_status", "EMBEDDING_FAILED"
        ),
        fail_embedding,
    )
    failure_choice.when(
        aws_stepfunctions.Condition.string_equals(
            "$.failure_context.target_status", "INDEX_FAILED"
        ),
        fail_index,
    )
    failure_choice.otherwise(fail_workflow)

    task_update_status_failed.next(failure_choice)

    input_validation_failed_chain = aws_stepfunctions.Chain.start(
        prepare_input_validation_failure_context
    ).next(task_update_status_failed)
    legacy_office_conversion_failed_chain = aws_stepfunctions.Chain.start(
        prepare_legacy_office_conversion_failure_context
    ).next(task_update_status_failed)
    extract_failed_chain = aws_stepfunctions.Chain.start(prepare_extract_failure_context).next(
        task_update_status_failed
    )
    embedding_failed_chain = aws_stepfunctions.Chain.start(
        prepare_embedding_failure_context
    ).next(task_update_status_failed)
    index_failed_chain = aws_stepfunctions.Chain.start(prepare_index_failure_context).next(
        task_update_status_failed
    )

    task_input_validation.add_catch(
        input_validation_failed_chain, errors=["States.ALL"], result_path="$.failure_error"
    )
    task_legacy_office_convert.add_catch(
        legacy_office_conversion_failed_chain,
        errors=["States.ALL"],
        result_path="$.failure_error",
    )
    for task in (
        task_docx_extract,
        task_xlsx_extract,
        task_pptx_extract,
        task_pdf_extract,
        task_extract_result_validation,
        task_chunk_build,
    ):
        task.add_catch(
            extract_failed_chain, errors=["States.ALL"], result_path="$.failure_error"
        )

    task_bedrock_enrichment.add_catch(
        prepare_bedrock_enrichment_failure,
        errors=["States.ALL"],
        result_path="$.bedrock_enrichment_error",
    )
    prepare_bedrock_enrichment_failure.next(task_chunk_build)
    task_chunk_enrichment.add_catch(
        prepare_chunk_enrichment_failure,
        errors=["States.ALL"],
        result_path="$.chunk_enrichment_error",
    )
    prepare_chunk_enrichment_failure.next(task_embedding)
    task_embedding.add_catch(
        embedding_failed_chain, errors=["States.ALL"], result_path="$.failure_error"
    )
    task_index_writer.add_catch(
        index_failed_chain, errors=["States.ALL"], result_path="$.failure_error"
    )
    task_update_status.add_catch(
        index_failed_chain, errors=["States.ALL"], result_path="$.failure_error"
    )

    unsupported_chain = (
        aws_stepfunctions.Chain.start(prepare_unsupported_failure_error)
        .next(prepare_unsupported_failure_context)
        .next(task_update_status_failed)
    )

    file_type_choice = aws_stepfunctions.Choice(scope, "FileTypeChoice")
    file_type_choice.when(
        aws_stepfunctions.Condition.and_(
            aws_stepfunctions.Condition.is_present("$.unsupported_file"),
            aws_stepfunctions.Condition.boolean_equals("$.unsupported_file", True),
        ),
        unsupported_chain,
    )
    file_type_choice.when(
        aws_stepfunctions.Condition.string_equals("$.source_type", "docx"),
        task_docx_extract,
    )
    file_type_choice.when(
        aws_stepfunctions.Condition.string_equals("$.source_type", "xlsx"),
        task_xlsx_extract,
    )
    file_type_choice.when(
        aws_stepfunctions.Condition.string_equals("$.source_type", "pptx"),
        task_pptx_extract,
    )
    file_type_choice.when(
        aws_stepfunctions.Condition.string_equals("$.source_type", "pdf"),
        task_pdf_extract,
    )
    file_type_choice.otherwise(unsupported_chain)

    skip_legacy_office_convert = aws_stepfunctions.Pass(scope, "SkipLegacyOfficeConvert")
    legacy_office_choice = aws_stepfunctions.Choice(scope, "LegacyOfficeChoice")
    legacy_office_choice.when(
        aws_stepfunctions.Condition.and_(
            aws_stepfunctions.Condition.is_present("$.legacy_office_source"),
            aws_stepfunctions.Condition.boolean_equals("$.legacy_office_source", True),
        ),
        task_legacy_office_convert,
    )
    legacy_office_choice.otherwise(skip_legacy_office_convert)

    skip_enrichment = aws_stepfunctions.Pass(scope, "SkipEnrichment")
    enrichment_choice = aws_stepfunctions.Choice(scope, "EnrichmentTargetChoice")
    enrichment_choice.when(
        aws_stepfunctions.Condition.number_greater_than("$.assets_images_count", 0),
        task_bedrock_enrichment,
    )
    enrichment_choice.otherwise(skip_enrichment)

    definition = (
        aws_stepfunctions.Chain.start(task_input_validation)
        .next(legacy_office_choice.afterwards())
        .next(file_type_choice.afterwards())
        .next(task_extract_result_validation)
        .next(enrichment_choice.afterwards())
        .next(task_chunk_build)
        .next(task_chunk_enrichment)
        .next(task_embedding)
        .next(task_index_writer)
        .next(task_update_status)
    )

    return aws_stepfunctions.StateMachine(
        scope=scope,
        id="cdk_stack_stepfunction",
        state_machine_name="cdk_stack_stepfunction",
        role=services["sf_iam_roles"]["sfunctions"],
        definition_body=aws_stepfunctions.DefinitionBody.from_chainable(definition),
        tracing_enabled=True,
        logs=aws_stepfunctions.LogOptions(
            destination=services["sf_log_group"],
            level=aws_stepfunctions.LogLevel.ALL,
        ),
    )
