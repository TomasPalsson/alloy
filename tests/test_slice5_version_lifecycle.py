"""Slice 5 version lifecycle: fingerprint-matched reuse, creation, and the version cap."""

from __future__ import annotations

from typing import Any, cast

import pytest

import alloy
from alloy import Agent, tool
from alloy._versions import fingerprint
from conftest import StubConversations


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.output_text = content
        self.output: list[Any] = []


class _StubResponses:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **kwargs: Any) -> _StubResponse:
        return _StubResponse(self._content)


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
        self.responses = _StubResponses(content)
        self.conversations = StubConversations()
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
    definition = agents.create_calls[0]["definition"]
    assert definition.instructions == "new system prompt"
    assert not hasattr(definition, "system_prompt")


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


def test_b17_programming_error_during_create_version_is_not_swallowed_as_version_cap() -> None:
    """The live bug: an `AttributeError` from a broken call must surface as itself, not
    get reported to the user as a misleading version-cap error."""
    original_error = AttributeError("'OpenAI' object has no attribute 'agents'")
    agents = _StubAgentsOperations(create_version_error=original_error)
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o", system_prompt="You are helpful.", name="weather-agent", client=client
    )

    with pytest.raises(AttributeError) as exc_info:
        agent("hi")

    assert exc_info.value is original_error


def test_malformed_version_metadata_is_not_swallowed_as_listing_failure() -> None:
    """A bug while reading an already-listed version's metadata (e.g. `metadata` is `None`)
    must surface as itself, not be reported as a listing/backend failure (see F2). Only the
    `list_versions()` round trip itself is a genuine backend failure — B16 must keep
    passing honestly."""
    malformed_version = _StubVersion(metadata=cast(Any, None))
    agents = _StubAgentsOperations(existing_versions=[malformed_version])
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o", system_prompt="You are helpful.", name="weather-agent", client=client
    )

    with pytest.raises(AttributeError):
        agent("hi")

    assert agents.create_calls == []


def test_passthrough_tool_is_forwarded_unmodified_to_the_backend_definition() -> None:
    """FR-013/AC-040: a non-decorated tool object must reach the backend agent definition
    untouched, not be silently dropped by `Agent.__init__`'s `tools=` filter (see F3)."""

    @tool
    def get_oncall(team: str) -> str:
        """Look up who is on call for a team.

        Args:
            team: Team name.
        """
        return f"{team}-oncall"

    passthrough = {"type": "code_interpreter"}
    agents = _StubAgentsOperations()
    client = _StubClient(agents=agents)
    agent = Agent(
        model="gpt-4o",
        tools=[get_oncall, passthrough],
        name="weather-agent",
        client=client,
    )

    agent("hi")

    assert len(agents.create_calls) == 1
    tools = agents.create_calls[0]["definition"].tools
    assert passthrough in tools
    assert any(getattr(t, "name", None) == "get_oncall" for t in tools)
