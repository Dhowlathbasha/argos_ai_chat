"""Resolve the AWS credential source used by all Bedrock-backed LLM calls."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}
_RESOLVED_MARKER = "ARGOS_AWS_CREDENTIALS_RESOLVED"
_DEFAULT_MODEL_ID = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"


@dataclass(frozen=True)
class AwsLlmConfig:
    credential_source: str
    model: str
    region: str


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a strict, human-friendly boolean environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off; got {raw!r}"
    )


def _decode_secret(response: Mapping[str, Any]) -> dict[str, Any]:
    if response.get("SecretString"):
        raw = str(response["SecretString"])
    elif response.get("SecretBinary") is not None:
        binary = response["SecretBinary"]
        if isinstance(binary, str):
            binary = binary.encode("utf-8")
        try:
            raw = bytes(binary).decode("utf-8")
            json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raw = base64.b64decode(binary).decode("utf-8")
    else:
        raise ValueError("AWS Secrets Manager returned neither SecretString nor SecretBinary")

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("AWS secret must contain a JSON object")
    nested = payload.get("credentials")
    return nested if isinstance(nested, dict) else payload


def _normalise_secret(payload: Mapping[str, Any]) -> dict[str, str]:
    aliases = {
        "AWS_ACCESS_KEY_ID": ("AWS_ACCESS_KEY_ID", "AccessKeyId", "access_key_id"),
        "AWS_SECRET_ACCESS_KEY": (
            "AWS_SECRET_ACCESS_KEY",
            "SecretAccessKey",
            "secret_access_key",
        ),
        "AWS_SESSION_TOKEN": ("AWS_SESSION_TOKEN", "SessionToken", "session_token"),
        "AWS_DEFAULT_REGION": ("AWS_DEFAULT_REGION", "Region", "region"),
        "BEDROCK_JUDGE_REGION": ("BEDROCK_JUDGE_REGION", "bedrock_region"),
        "BEDROCK_JUDGE_MODEL_ID": ("BEDROCK_JUDGE_MODEL_ID", "model_id"),
        "FOLLOW_UP_AI_MODEL": ("FOLLOW_UP_AI_MODEL", "follow_up_model"),
    }
    values: dict[str, str] = {}
    for env_name, candidates in aliases.items():
        for candidate in candidates:
            value = payload.get(candidate)
            if value is not None and str(value).strip():
                values[env_name] = str(value).strip()
                break
    return values


def resolve_bedrock_model(model: Optional[str] = None) -> str:
    """Return a LiteLLM-compatible Bedrock model name."""
    raw = model or os.getenv("BEDROCK_JUDGE_MODEL_ID") or _DEFAULT_MODEL_ID
    return raw if raw.startswith("bedrock/") else f"bedrock/{raw}"


def configure_aws_credentials(
    *,
    secrets_client: Any = None,
    force: bool = False,
) -> AwsLlmConfig:
    """Select .env credentials or replace them with a Secrets Manager JSON secret.

    ``USE_AWS_SECRETS_MANAGER=false`` leaves the AWS SDK/LiteLLM environment
    credential chain untouched. When true, ``AWS_SECRET_NAME`` is fetched once
    and its AWS/Bedrock values replace the process environment before any LLM
    client is created.
    """
    use_secrets = env_bool("USE_AWS_SECRETS_MANAGER", False)
    requested_source = "secrets_manager" if use_secrets else "environment"

    if not force and os.getenv(_RESOLVED_MARKER) == requested_source:
        region = os.getenv("BEDROCK_JUDGE_REGION") or os.getenv(
            "AWS_DEFAULT_REGION", "eu-central-1"
        )
        return AwsLlmConfig(requested_source, resolve_bedrock_model(), region)

    if use_secrets:
        secret_name = os.getenv("AWS_SECRET_NAME", "").strip()
        if not secret_name:
            raise RuntimeError(
                "USE_AWS_SECRETS_MANAGER=true requires AWS_SECRET_NAME to be set"
            )

        if secrets_client is None:
            import boto3

            secret_region = os.getenv("AWS_SECRETS_MANAGER_REGION") or os.getenv(
                "AWS_DEFAULT_REGION", "eu-central-1"
            )
            profile = os.getenv("AWS_SECRETS_PROFILE") or None
            session = boto3.Session(profile_name=profile, region_name=secret_region)
            secrets_client = session.client("secretsmanager")

        try:
            response = secrets_client.get_secret_value(SecretId=secret_name)
            secret_values = _normalise_secret(_decode_secret(response))
        except Exception as exc:
            raise RuntimeError(
                f"Unable to load AWS LLM credentials from secret {secret_name!r}: {exc}"
            ) from exc

        missing = [
            key
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
            if not secret_values.get(key)
        ]
        if missing:
            raise RuntimeError(
                f"AWS secret {secret_name!r} is missing required keys: {', '.join(missing)}"
            )

        for key, value in secret_values.items():
            os.environ[key] = value

    os.environ[_RESOLVED_MARKER] = requested_source
    region = os.getenv("BEDROCK_JUDGE_REGION") or os.getenv(
        "AWS_DEFAULT_REGION", "eu-central-1"
    )
    return AwsLlmConfig(requested_source, resolve_bedrock_model(), region)


__all__ = [
    "AwsLlmConfig",
    "configure_aws_credentials",
    "env_bool",
    "resolve_bedrock_model",
]
