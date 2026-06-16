import importlib.util
import os
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_lambda_module(stage: str, module_name: str) -> Any:
    module_path = REPO_ROOT / "deploy_code" / stage / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def set_default_aws_env() -> None:
    os.environ.setdefault("AWS_REGION", "ap-northeast-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")