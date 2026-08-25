"""Slice 7: Foundry adapter auth mapping, endpoint resolution, and the runnable example."""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest
from azure.core.exceptions import ClientAuthenticationError

import alloy
from alloy import _foundry

EXAMPLE_PATH = pathlib.Path(__file__).resolve().parents[1] / "examples" / "oncall.py"


def test_B21_expired_credential_maps_to_backend_auth_error_naming_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_expired(*args: Any, **kwargs: Any) -> None:
        raise ClientAuthenticationError("credential expired")

    monkeypatch.setattr(_foundry, "AIProjectClient", _raise_expired)

    with pytest.raises(alloy.BackendAuthError) as exc_info:
        _foundry.FoundryClient(endpoint="https://example.services.ai.azure.com", credential=object())

    assert "re-authenticate" in str(exc_info.value).lower()


def test_B22_backend_auth_error_never_leaks_token_or_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_token = "sekrit-bearer-token-abc123XYZ"

    def _raise_with_token(*args: Any, **kwargs: Any) -> None:
        raise ClientAuthenticationError(f"Bearer {secret_token} rejected")

    monkeypatch.setattr(_foundry, "AIProjectClient", _raise_with_token)

    with pytest.raises(alloy.BackendAuthError) as exc_info:
        _foundry.FoundryClient(endpoint="https://example.services.ai.azure.com", credential=object())

    error = exc_info.value
    assert secret_token not in str(error)
    assert secret_token not in repr(error)


def test_B24_missing_endpoint_raises_alloy_error_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_AI_PROJECT_ENDPOINT", raising=False)

    with pytest.raises(alloy.AlloyError) as exc_info:
        _foundry.resolve_endpoint(None)

    assert "AZURE_AI_PROJECT_ENDPOINT" in str(exc_info.value)


def test_example_oncall_imports_without_calling_azure() -> None:
    spec = importlib.util.spec_from_file_location("oncall_example", EXAMPLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # import-time only: must not construct an Agent

    assert hasattr(module, "main")
