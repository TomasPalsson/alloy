"""Slice 5 version lifecycle: fingerprint-matched reuse, creation, and the version cap."""

from __future__ import annotations

from typing import Any

import pytest

import alloy
from alloy import Agent
from alloy._versions import fingerprint


class _StubMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChoice:
    def __init__(self, content: str) -> None:
        self.message = _StubMessage(content)


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_StubChoice(content)]


class _StubCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **kwargs: Any) -> _StubResponse:
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubVersion:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.metadata = metadata


class _StubAgentsOperations:
    def __init__(
        self,
        existing_versions: list[_StubVersion] | None = None,
        list_versions_error: Exception | None = None,
        create_version_error: Exception | None = None,
    ) -> None:
        self._existing_versions = existing_versions or []
        self._list_versions_error = list_versions_error
        self._create_version_error = create_version_error
        self.create_calls: list[dict[str, Any]] = []

    def list_versions(self, agent_name: str, **kwargs: Any) -> list[_StubVersion]:
        if self._list_versions_error is not None:
            raise self._list_versions_error
        return self._existing_versions

    def create_version(self, agent_name: str, **kwargs: Any) -> _StubVersion:
        if self._create_version_error is not None:
            raise self._create_version_error
        self.create_calls.append({"agent_name": agent_name, **kwargs})
        return _StubVersion(metadata=kwargs.get("metadata", {}))


class _StubClient:
    def __init__(self, agents: _StubAgentsOperations, content: str = "hello") -> None:
        self.chat = _StubChat(_StubCompletions(content))
        self.agents = agents


def test_b14_matching_fingerprint_creates_no_new_version() -> None:
    matching_fingerprint = fingerprint("gpt-4o", "You are helpful.", [])
    agents = _StubAgentsOperations(
        existing_versions=[_StubVersion(metadata={"alloy_fingerprint": matching_fingerprint})]
    )
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o", system_prompt="You are helpful.", name="weather-agent", client=client
    )

    agent("hi")

    assert agents.create_calls == []


def test_b15_changed_system_prompt_creates_exactly_one_new_version() -> None:
    stale_fingerprint = fingerprint("gpt-4o", "old system prompt", [])
    agents = _StubAgentsOperations(
        existing_versions=[_StubVersion(metadata={"alloy_fingerprint": stale_fingerprint})]
    )
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o", system_prompt="new system prompt", name="weather-agent", client=client
    )

    agent("hi")

    assert len(agents.create_calls) == 1
    new_fingerprint = fingerprint("gpt-4o", "new system prompt", [])
    assert agents.create_calls[0]["metadata"]["alloy_fingerprint"] == new_fingerprint


def test_b16_listing_failure_still_creates_version_and_warns() -> None:
    agents = _StubAgentsOperations(list_versions_error=RuntimeError("backend unavailable"))
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o", system_prompt="You are helpful.", name="weather-agent", client=client
    )

    with pytest.warns(UserWarning):
        result = agent("hi")

    assert len(agents.create_calls) == 1
    assert result.text == "hello"


def test_b17_version_cap_reached_raises_version_cap_error_with_cause() -> None:
    original_error = RuntimeError("cap of 5 versions reached")
    agents = _StubAgentsOperations(create_version_error=original_error)
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o", system_prompt="You are helpful.", name="weather-agent", client=client
    )

    with pytest.raises(alloy.VersionCapError) as exc_info:
        agent("hi")

    error = exc_info.value
    assert "weather-agent" in str(error)
    assert "5" in str(error)
    assert error.__cause__ is original_error
