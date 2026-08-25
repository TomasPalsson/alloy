"""Slice 7: Foundry adapter auth mapping, endpoint resolution, and the runnable example."""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

import alloy
from alloy import _foundry

EXAMPLE_PATH = pathlib.Path(__file__).resolve().parents[1] / "examples" / "oncall.py"


class _FakeProjectClient:
    """Stands in for `AIProjectClient`: a distinct object from what it hands out."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.agents = object()  # the real control-plane surface, unused directly here
        self._openai_client = object()  # sentinel: the real data-plane surface

    def get_openai_client(self, *, agent_name: str | None = None) -> Any:
        return self._openai_client


def test_b21_expired_credential_maps_to_backend_auth_error_naming_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_expired(*args: Any, **kwargs: Any) -> None:
        raise ClientAuthenticationError("credential expired")

    monkeypatch.setattr(_foundry, "AIProjectClient", _raise_expired)

    with pytest.raises(alloy.BackendAuthError) as exc_info:
        _foundry.FoundryClient(
            endpoint="https://example.services.ai.azure.com", credential=object()
        )

    assert "re-authenticate" in str(exc_info.value).lower()


def test_b22_backend_auth_error_never_leaks_token_or_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_token = "sekrit-bearer-token-abc123XYZ"

    def _raise_with_token(*args: Any, **kwargs: Any) -> None:
        raise ClientAuthenticationError(f"Bearer {secret_token} rejected")

    monkeypatch.setattr(_foundry, "AIProjectClient", _raise_with_token)

    with pytest.raises(alloy.BackendAuthError) as exc_info:
        _foundry.FoundryClient(
            endpoint="https://example.services.ai.azure.com", credential=object()
        )

    error = exc_info.value
    assert secret_token not in str(error)
    assert secret_token not in repr(error)


def test_b24_missing_endpoint_raises_alloy_error_naming_the_variable(
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


def test_project_and_openai_clients_are_distinct_and_separately_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.project` is the control plane (`.agents.*`); `get_openai_client()` is the data
    plane (`.responses.*`, `.conversations.*`) — the two conflated clients from the live bug.
    """
    monkeypatch.setattr(_foundry, "AIProjectClient", _FakeProjectClient)

    client = _foundry.FoundryClient(
        endpoint="https://example.services.ai.azure.com", credential=object()
    )

    project_client = client.project
    openai_client = client.get_openai_client(agent_name="weather-agent")

    assert hasattr(project_client, "agents")
    assert project_client is not openai_client


def test_build_prompt_agent_definition_maps_system_prompt_onto_instructions() -> None:
    definition = _foundry.build_prompt_agent_definition(
        model="gpt-4o", instructions="You are helpful.", tools=[]
    )

    assert definition.model == "gpt-4o"
    assert definition.instructions == "You are helpful."
    assert not hasattr(definition, "system_prompt")


def test_map_version_creation_error_maps_cap_condition_to_version_cap_error() -> None:
    mapped = _foundry.map_version_creation_error(
        RuntimeError("cap of 5 versions reached"), agent_name="weather-agent"
    )

    assert isinstance(mapped, alloy.VersionCapError)
    assert "weather-agent" in str(mapped)


def test_map_version_creation_error_maps_auth_failure_to_backend_auth_error() -> None:
    mapped = _foundry.map_version_creation_error(
        ClientAuthenticationError("credential expired"), agent_name="weather-agent"
    )

    assert isinstance(mapped, alloy.BackendAuthError)


def test_map_version_creation_error_maps_other_http_failure_to_alloy_error() -> None:
    http_error = HttpResponseError(message="internal server error")
    http_error.status_code = 500

    mapped = _foundry.map_version_creation_error(http_error, agent_name="weather-agent")

    assert type(mapped) is alloy.AlloyError
    assert not isinstance(mapped, alloy.VersionCapError)


def test_map_version_creation_error_leaves_programming_errors_unmapped() -> None:
    mapped = _foundry.map_version_creation_error(
        AttributeError("'OpenAI' object has no attribute 'agents'"), agent_name="weather-agent"
    )

    assert mapped is None
