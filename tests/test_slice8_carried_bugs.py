"""Slice 1: carried bug fixes — @tool overrides discarded (FR-B1), non-JSON tool
return escaping run_calls (FR-B2). See .specs/002-hooks-and-serve/spec.md §7."""

from __future__ import annotations

import datetime
import json
from typing import Any

from alloy import Agent
from alloy._loop import run_calls
from alloy._schema import derive, tool
from alloy.contracts import ToolCall


class _FunctionCallItem:
    """Mimics openai's ResponseFunctionToolCall: call_id, name, raw arguments string."""

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.type = "function_call"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class _StubResponse:
    def __init__(self, content: str | None = None, output: list[Any] = []) -> None:  # noqa: B006
        self.output_text = content
        self.output = list(output)


class _StubResponses:
    def __init__(self, responses: list[_StubResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        return self._responses[len(self.calls) - 1]


class _StubConversation:
    def __init__(self, id: str) -> None:
        self.id = id


class _StubConversations:
    def create(self, **kwargs: Any) -> _StubConversation:
        return _StubConversation("conv_1")


class _StubClient:
    def __init__(self, responses: list[_StubResponse]) -> None:
        self.responses = _StubResponses(responses)
        self.conversations = _StubConversations()


def test_renamed_tool_is_reachable_by_its_override_name() -> None:
    @tool(name="renamed_lookup")
    def get_oncall(team: str) -> str:
        """Get the on-call engineer for a team.

        Args:
            team: The team name.
        """
        return f"{team}-oncall"

    agent = Agent(model="gpt-4o", tools=[get_oncall], client=_StubClient([]))

    assert agent.tool.renamed_lookup(team="data") == "data-oncall"


def test_renamed_tool_schema_carries_override_name_not_function_name() -> None:
    @tool(name="renamed_lookup")
    def get_oncall(team: str) -> str:
        """Get the on-call engineer for a team.

        Args:
            team: The team name.
        """
        return f"{team}-oncall"

    agent = Agent(model="gpt-4o", tools=[get_oncall], client=_StubClient([]))

    assert "renamed_lookup" in agent._tool_map
    assert "get_oncall" not in agent._tool_map
    assert [spec.name for spec in agent._tool_specs] == ["renamed_lookup"]


def test_overridden_description_survives_into_the_agent_tool_spec() -> None:
    @tool(description="overridden text")
    def get_oncall(team: str) -> str:
        """Original summary.

        Args:
            team: The team name.
        """
        return f"{team}-oncall"

    agent = Agent(model="gpt-4o", tools=[get_oncall], client=_StubClient([]))

    assert agent._tool_map["get_oncall"].description == "overridden text"


def test_non_serializable_tool_return_becomes_a_type_error_failure() -> None:
    @tool
    def now() -> datetime.datetime:
        """Return the current time.

        Returns:
            The current time.
        """
        return datetime.datetime.now()

    spec = derive(now)
    call = ToolCall(call_id="call_1", name="now", arguments="{}")

    results = run_calls([call], {"now": spec})

    assert results[0].failure is not None
    assert isinstance(results[0].failure, TypeError)
    parsed = json.loads(results[0].output)
    assert "error" in parsed


def test_agent_call_completes_normally_when_a_tool_returns_non_serializable_value() -> None:
    @tool
    def now() -> datetime.datetime:
        """Return the current time.

        Returns:
            The current time.
        """
        return datetime.datetime.now()

    call_item = _FunctionCallItem(call_id="call_1", name="now", arguments="{}")
    client = _StubClient([_StubResponse(output=[call_item]), _StubResponse(content="handled")])
    agent = Agent(model="gpt-4o", tools=[now], client=client)

    result = agent("what time is it?")

    assert result.text == "handled"
    assert len(result.tool_failures) == 1
    assert isinstance(result.tool_failures[0], TypeError)
