"""Unit tests for selectable AWS credential sources (no network calls)."""
from __future__ import annotations

import json
import os

import pytest

from utils.aws_config import configure_aws_credentials, env_bool


class FakeSecretsClient:
    def __init__(self, payload: dict[str, str]):
        self.payload = payload
        self.requested_secret = None

    def get_secret_value(self, *, SecretId: str):
        self.requested_secret = SecretId
        return {"SecretString": json.dumps(self.payload)}


def test_env_credentials_are_left_untouched(monkeypatch):
    monkeypatch.setenv("USE_AWS_SECRETS_MANAGER", "false")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "env-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    monkeypatch.setenv("BEDROCK_JUDGE_MODEL_ID", "example-model")

    config = configure_aws_credentials(force=True)

    assert config.credential_source == "environment"
    assert config.model == "bedrock/example-model"
    assert config.region
    assert os.environ["AWS_ACCESS_KEY_ID"] == "env-access"


def test_secrets_manager_credentials_override_env(monkeypatch):
    monkeypatch.setenv("USE_AWS_SECRETS_MANAGER", "true")
    monkeypatch.setenv("AWS_SECRET_NAME", "argos/test/bedrock")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "old-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "old-secret")
    client = FakeSecretsClient(
        {
            "AWS_ACCESS_KEY_ID": "secret-access",
            "AWS_SECRET_ACCESS_KEY": "secret-key",
            "AWS_SESSION_TOKEN": "secret-token",
            "BEDROCK_JUDGE_REGION": "eu-west-1",
            "BEDROCK_JUDGE_MODEL_ID": "secret-model",
        }
    )

    config = configure_aws_credentials(secrets_client=client, force=True)

    assert client.requested_secret == "argos/test/bedrock"
    assert config.credential_source == "secrets_manager"
    assert config.model == "bedrock/secret-model"
    assert config.region == "eu-west-1"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "secret-access"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "secret-key"


def test_secrets_manager_requires_secret_name(monkeypatch):
    monkeypatch.setenv("USE_AWS_SECRETS_MANAGER", "true")
    monkeypatch.delenv("AWS_SECRET_NAME", raising=False)

    with pytest.raises(RuntimeError, match="AWS_SECRET_NAME"):
        configure_aws_credentials(force=True)


def test_boolean_rejects_ambiguous_values(monkeypatch):
    monkeypatch.setenv("FEATURE_FLAG", "sometimes")

    with pytest.raises(ValueError, match="FEATURE_FLAG"):
        env_bool("FEATURE_FLAG")
