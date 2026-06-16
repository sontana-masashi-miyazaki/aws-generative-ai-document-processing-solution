import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional

import yaml


@dataclass(frozen=True)
class DeploymentConfig:
    pipeline_version: str
    lambda_vpc_id: Optional[str]
    lambda_vpc_name: Optional[str]
    lambda_subnet_ids: List[str]
    lambda_security_group_ids: List[str]
    create_lambda_security_group: bool
    lambda_allow_all_outbound: bool
    s3_bucket_name: Optional[str]
    bedrock_image_model_id: Optional[str]
    embedding_model_id: str
    embedding_dimensions: Optional[int]
    soffice_binary: str
    status_table_name: Optional[str]
    search_backend: str
    search_endpoint: Optional[str]
    search_index: Optional[str]
    search_api_key_secret_arn: Optional[str]
    chunk_enrichment_model_id: Optional[str]


CONFIG_FILE_NAME = "config.yml"
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
LEGACY_EMBEDDING_MODEL_IDS = {
    "amazon.titan-embed-text-v2": DEFAULT_EMBEDDING_MODEL_ID,
}


def load_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as config_file:
            loaded = yaml.safe_load(config_file) or {}
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Configuration file '{path.name}' was not found. "
            f"Create {path.name} in the repository root before running CDK."
        ) from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Configuration file '{path.name}' is not valid YAML.") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration file '{path.name}' must contain a YAML mapping.")

    return loaded


def _config_string(config: Mapping[str, Any], key: str) -> Optional[str]:
    value = config.get(key)
    if value is None:
        return None

    if isinstance(value, str):
        value = value.strip()
        return value or None

    return str(value).strip() or None


def _config_bool(config: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False

    if isinstance(value, int) and value in {0, 1}:
        return bool(value)

    raise ValueError(f"Config '{key}' must be a boolean.")


def _config_list(config: Mapping[str, Any], key: str) -> List[str]:
    value = config.get(key)
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError(f"Config '{key}' must be a list.")
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]

    raise ValueError(f"Config '{key}' must be a list.")


def _config_int(config: Mapping[str, Any], key: str) -> Optional[int]:
    value = config.get(key)
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError(f"Config '{key}' must be an integer.")

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"Config '{key}' must be an integer.") from exc

    raise ValueError(f"Config '{key}' must be an integer.")


def _config_alias_string(config: Mapping[str, Any], *keys: str) -> Optional[str]:
    values: List[str] = []
    for key in keys:
        value = _config_string(config, key)
        if value is not None:
            values.append(value)

    if not values:
        return None

    if len(set(values)) > 1:
        raise ValueError(f"Config keys {keys} must not specify conflicting values.")

    return values[0]


def _normalize_embedding_model_id(model_id: str) -> str:
    return LEGACY_EMBEDDING_MODEL_IDS.get(model_id, model_id)


def requires_cdk_environment(config: Mapping[str, Any]) -> bool:
    return bool(
        _config_string(config, "lambdaVpcId") or _config_string(config, "lambdaVpcName")
    )


def load_deployment_config(config: Mapping[str, Any]) -> DeploymentConfig:
    vpc_id = _config_string(config, "lambdaVpcId")
    vpc_name = _config_string(config, "lambdaVpcName")
    subnet_ids = _config_list(config, "lambdaSubnetIds")
    security_group_ids = _config_list(config, "lambdaSecurityGroupIds")
    has_vpc_lookup = bool(vpc_id or vpc_name)

    if (subnet_ids or security_group_ids) and not has_vpc_lookup:
        raise ValueError(
            "lambdaSubnetIds and lambdaSecurityGroupIds require lambdaVpcId or "
            "lambdaVpcName to be set."
        )

    search_endpoint = _config_alias_string(config, "searchEndpoint", "opensearchEndpoint")
    search_index = _config_alias_string(config, "searchIndex", "opensearchIndex")
    if bool(search_endpoint) != bool(search_index):
        raise ValueError(
            "searchEndpoint and searchIndex must be provided together."
        )

    search_api_key_secret_arn = _config_string(config, "searchApiKeySecretArn")
    search_backend = (_config_string(config, "searchBackend") or "").strip().lower()
    if not search_backend:
        search_backend = "aws-opensearch" if search_endpoint else "none"
    if search_backend not in {"none", "aws-opensearch", "elastic-cloud"}:
        raise ValueError(
            "Config 'searchBackend' must be one of: none, aws-opensearch, elastic-cloud."
        )
    if search_backend == "none":
        if search_endpoint or search_index or search_api_key_secret_arn:
            raise ValueError(
                "searchBackend 'none' cannot be combined with searchEndpoint, searchIndex, "
                "or searchApiKeySecretArn."
            )
    else:
        if not search_endpoint or not search_index:
            raise ValueError(
                "searchEndpoint and searchIndex are required when searchBackend is enabled."
            )
        if search_backend == "elastic-cloud" and not search_api_key_secret_arn:
            raise ValueError(
                "Elastic Cloud requires searchApiKeySecretArn to be set."
            )
        if search_backend == "aws-opensearch" and search_api_key_secret_arn:
            raise ValueError(
                "searchApiKeySecretArn is only supported when searchBackend is elastic-cloud."
            )

    embedding_dimensions = _config_int(config, "embeddingDimensions")
    if embedding_dimensions is not None and embedding_dimensions <= 0:
        raise ValueError("Config 'embeddingDimensions' must be a positive integer.")

    embedding_model_id = _normalize_embedding_model_id(
        _config_string(config, "embeddingModelId") or DEFAULT_EMBEDDING_MODEL_ID
    )
    if (
        embedding_dimensions is not None
        and embedding_model_id == DEFAULT_EMBEDDING_MODEL_ID
        and embedding_dimensions not in {256, 512, 1024}
    ):
        raise ValueError(
            "Amazon Titan Text Embeddings V2 supports embeddingDimensions of 256, 512, or 1024."
        )

    create_lambda_security_group = _config_bool(
        config,
        "createLambdaSecurityGroup",
        default=bool(has_vpc_lookup and not security_group_ids),
    )
    if has_vpc_lookup and not create_lambda_security_group and not security_group_ids:
        raise ValueError(
            "When lambdaVpcId or lambdaVpcName is set, either "
            "createLambdaSecurityGroup must be true or lambdaSecurityGroupIds must "
            "be provided."
        )

    return DeploymentConfig(
        pipeline_version=_config_string(config, "pipelineVersion") or "pipeline_v1",
        lambda_vpc_id=vpc_id,
        lambda_vpc_name=vpc_name,
        lambda_subnet_ids=subnet_ids,
        lambda_security_group_ids=security_group_ids,
        create_lambda_security_group=create_lambda_security_group,
        lambda_allow_all_outbound=_config_bool(
            config,
            "lambdaAllowAllOutbound",
            default=False,
        ),
        s3_bucket_name=_config_string(config, "s3BucketName"),
        bedrock_image_model_id=_config_string(config, "bedrockImageModelId"),
        embedding_model_id=embedding_model_id,
        embedding_dimensions=embedding_dimensions,
        soffice_binary=_config_string(config, "sofficeBinary")
        or "/opt/libreoffice/program/soffice",
        status_table_name=_config_string(config, "statusTableName"),
        search_backend=search_backend,
        search_endpoint=search_endpoint,
        search_index=search_index,
        search_api_key_secret_arn=search_api_key_secret_arn,
        chunk_enrichment_model_id=_config_string(config, "chunkEnrichmentModelId"),
    )
