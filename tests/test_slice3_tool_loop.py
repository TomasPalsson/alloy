"""Slice 3: tool-call loop and direct invocation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from alloy import Agent, AlloyError, ToolArgumentError, UnknownToolError
from alloy._loop import run_calls
from alloy._schema import derive, tool
from alloy.contracts import ToolCall
from conftest import _StubConversations


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


class _StubClient:
    def __init__(self, responses: list[_StubResponse]) -> None:
        self.responses = _StubResponses(responses)
        self.conversations = _StubConversations()


def test_b8_runs_matching_tool_and_submits_result_back() -> None:
    seen_teams: list[str] = []

    @tool
    def get_oncall(team: str) -> str:
        """Get the on-call engineer for a team.

        Args:
            team: The team name.
        """
        seen_teams.append(team)
        return "alice"

    call_item = _FunctionCallItem(call_id="call_1", name="get_oncall", arguments='{"team": "data"}')
    client = _StubClient(
        [_StubResponse(output=[call_item]), _StubResponse(content="alice is on call")]
    )
    agent = Agent(model="gpt-4o", tools=[get_oncall], client=client)

    result = agent("who is on call for data?")

    assert seen_teams == ["data"]
    assert result.text == "alice is on call"
    second_call_input = client.responses.calls[1]["input"]
    assert any(
        item["type"] == "function_call_output"
        and item["call_id"] == "call_1"
        and item["output"] == json.dumps("alice")
        for item in second_call_input
    )


def test_b9_tool_exception_is_returned_not_raised() -> None:
    @tool
    def explode(x: int) -> int:
        """Explode.

        Args:
            x: An input.
        """
        raise ValueError("boom")

    call_item = _FunctionCallItem(call_id="call_1", name="explode", arguments='{"x": 1}')
    client = _StubClient([_StubResponse(output=[call_item]), _StubResponse(content="handled")])
    agent = Agent(model="gpt-4o", tools=[explode], client=client)

    result = agent("go")

    assert result.text == "handled"
    assert len(result.tool_failures) == 1
    assert isinstance(result.tool_failures[0], ValueError)
    assert str(result.tool_failures[0]) == "boom"


def test_b10_unknown_tool_name_submits_named_error_not_raised() -> None:
    call_item = _FunctionCallItem(call_id="call_1", name="does_not_exist", arguments="{}")
    client = _StubClient([_StubResponse(output=[call_item]), _StubResponse(content="handled")])
    agent = Agent(model="gpt-4o", client=client)

    result = agent("go")

    assert result.text == "handled"
    assert len(result.tool_failures) == 1
    assert isinstance(result.tool_failures[0], UnknownToolError)
    assert "does_not_exist" in str(result.tool_failures[0])


def test_b11_passthrough_tool_requested_by_name_is_never_executed_locally() -> None:
    executed = False

    def spy_call(**kwargs: Any) -> str:
        nonlocal executed
        executed = True
        return "should not happen"

    passthrough_tool = {"name": "raw_tool", "already": "a spec", "call": spy_call}
    call_item = _FunctionCallItem(call_id="call_1", name="raw_tool", arguments="{}")
    client = _StubClient([_StubResponse(output=[call_item]), _StubResponse(content="handled")])
    agent = Agent(model="gpt-4o", tools=[passthrough_tool], client=client)

    result = agent("go")

    assert executed is False
    assert result.text == "handled"
    assert isinstance(result.tool_failures[0], UnknownToolError)


def test_b27_direct_invocation_runs_with_zero_client_calls() -> None:
    @tool
    def get_oncall(team: str) -> str:
        """Get the on-call engineer for a team.

        Args:
            team: The team name.
        """
        return f"{team}-oncall"

    client = _StubClient([])
    agent = Agent(model="gpt-4o", tools=[get_oncall], client=client)

    value = agent.tool.get_oncall(team="data")

    assert value == "data-oncall"
    assert client.responses.calls == []


def test_b28_unknown_direct_attribute_raises_unknown_tool_error() -> None:
    client = _StubClient([])
    agent = Agent(model="gpt-4o", client=client)

    with pytest.raises(UnknownToolError, match="nope"):
        agent.tool.nope()


def test_b29_missing_required_argument_raises_before_tool_runs() -> None:
    executed = False

    @tool
    def needs_team(team: str) -> str:
        """Needs a team.

        Args:
            team: The team name.
        """
        nonlocal executed
        executed = True
        return team

    spec = derive(needs_team)
    call = ToolCall(call_id="call_1", name="needs_team", arguments="{}")

    results = run_calls([call], {"needs_team": spec})

    assert executed is False
    assert isinstance(results[0].failure, ToolArgumentError)


def test_malformed_json_arguments_is_returned_as_a_tool_failure_not_raised() -> None:
    """A model-supplied argument string that isn't valid JSON must not escape the run as a
    bare json.JSONDecodeError — it becomes a ToolResult.failure, like a tool raising (F5)."""

    @tool
    def echo(x: int) -> int:
        """Echo a value back.

        Args:
            x: The value.
        """
        return x

    call_item = _FunctionCallItem(call_id="call_1", name="echo", arguments="{not valid json")
    client = _StubClient([_StubResponse(output=[call_item]), _StubResponse(content="handled")])
    agent = Agent(model="gpt-4o", tools=[echo], client=client)

    result = agent("go")

    assert result.text == "handled"
    assert len(result.tool_failures) == 1
    assert isinstance(result.tool_failures[0], ToolArgumentError)


def test_tool_loop_raises_alloy_error_after_max_turns_instead_of_looping_forever() -> None:
    """A model that keeps emitting tool calls must not loop forever (F4)."""

    @tool
    def echo(x: int) -> int:
        """Echo a value back.

        Args:
            x: The value.
        """
        return x

    # Every turn's response asks for another tool call — the loop never gets a plain-text
    # response on its own, so only the max-turns guard can end it.
    call_args = '{"x": 1}'
    responses = [
        _StubResponse(
            output=[_FunctionCallItem(call_id=f"call_{i}", name="echo", arguments=call_args)]
        )
        for i in range(50)
    ]
    client = _StubClient(responses)
    agent = Agent(model="gpt-4o", tools=[echo], client=client)

    with pytest.raises(AlloyError, match="10"):
        agent("go")
