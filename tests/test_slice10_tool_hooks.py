"""Slice 3: tool lifecycle events — BeforeToolCallEvent/AfterToolCallEvent from run_calls.

See .specs/002-hooks-and-serve/spec.md AC-08 through AC-15a, AC-17a-c and
.specs/002-hooks-and-serve/code-design.md §5, §5a, §5b, §7-D4.
"""

from __future__ import annotations

import dataclasses
import json

from alloy._loop import run_calls
from alloy._schema import derive, tool
from alloy.contracts import ToolArgumentError, ToolCall, UnknownToolError
from alloy.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry


@tool
def add_one(x: int) -> int:
    """Add one to a number.

    Args:
        x: An input.
    """
    return x + 1


@tool
def explode(x: int) -> int:
    """Explode.

    Args:
        x: An input.
    """
    raise ValueError("boom")


@tool
def needs_team(team: str) -> str:
    """Needs team.

    Args:
        team: team name.
    """
    return team


def test_before_and_after_fire_once_for_successful_call() -> None:
    spec = derive(add_one)
    call = ToolCall(call_id="1", name="add_one", arguments='{"x": 1}')
    before_events: list[BeforeToolCallEvent] = []
    after_events: list[AfterToolCallEvent] = []
    registry = HookRegistry()
    registry.add_callback(BeforeToolCallEvent, before_events.append)
    registry.add_callback(AfterToolCallEvent, after_events.append)

    results = run_calls([call], {"add_one": spec}, hooks=registry)

    assert len(before_events) == 1
    assert before_events[0].tool_use == call
    assert len(after_events) == 1
    assert after_events[0].tool_use == call
    assert after_events[0].result == results[0]
    assert results[0].output == json.dumps(2)
    assert results[0].failure is None


def test_tool_raises_after_still_fires_with_failure() -> None:
    spec = derive(explode)
    call = ToolCall(call_id="1", name="explode", arguments='{"x": 1}')
    after_events: list[AfterToolCallEvent] = []
    registry = HookRegistry()
    registry.add_callback(AfterToolCallEvent, after_events.append)

    run_calls([call], {"explode": spec}, hooks=registry)

    assert len(after_events) == 1
    assert isinstance(after_events[0].result.failure, ValueError)
    assert str(after_events[0].result.failure) == "boom"


def test_unknown_tool_fires_both_events_with_unknown_tool_error() -> None:
    call = ToolCall(call_id="1", name="does_not_exist", arguments="{}")
    before_events: list[BeforeToolCallEvent] = []
    after_events: list[AfterToolCallEvent] = []
    registry = HookRegistry()
    registry.add_callback(BeforeToolCallEvent, before_events.append)
    registry.add_callback(AfterToolCallEvent, after_events.append)

    run_calls([call], {}, hooks=registry)

    assert len(before_events) == 1
    assert len(after_events) == 1
    assert isinstance(after_events[0].result.failure, UnknownToolError)


def test_cancel_tool_prevents_tool_execution() -> None:
    executed = False

    @tool
    def delete_prod(x: int) -> int:
        """Delete prod.

        Args:
            x: An input.
        """
        nonlocal executed
        executed = True
        return x

    spec = derive(delete_prod)
    call = ToolCall(call_id="1", name="delete_prod", arguments='{"x": 1}')
    registry = HookRegistry()

    def _cancel(event: BeforeToolCallEvent) -> None:
        event.cancel_tool = "blocked by policy"

    registry.add_callback(BeforeToolCallEvent, _cancel)

    run_calls([call], {"delete_prod": spec}, hooks=registry)

    assert executed is False


def test_cancelled_result_has_no_failure_and_reason_in_output() -> None:
    spec = derive(add_one)
    call = ToolCall(call_id="1", name="add_one", arguments='{"x": 1}')
    registry = HookRegistry()

    def _cancel(event: BeforeToolCallEvent) -> None:
        event.cancel_tool = "blocked by policy"

    registry.add_callback(BeforeToolCallEvent, _cancel)

    results = run_calls([call], {"add_one": spec}, hooks=registry)

    assert results[0].failure is None
    assert json.loads(results[0].output) == {"cancelled": "blocked by policy"}


def test_cancel_outranks_unknown_tool() -> None:
    call = ToolCall(call_id="1", name="does_not_exist", arguments="{}")
    registry = HookRegistry()

    def _cancel(event: BeforeToolCallEvent) -> None:
        event.cancel_tool = "blocked by policy"

    registry.add_callback(BeforeToolCallEvent, _cancel)

    results = run_calls([call], {}, hooks=registry)

    assert results[0].failure is None
    assert json.loads(results[0].output) == {"cancelled": "blocked by policy"}


def test_hook_rewrites_tool_use_arguments_before_dispatch() -> None:
    received: dict[str, int] = {}

    @tool
    def record(x: int) -> int:
        """Record x.

        Args:
            x: An input.
        """
        received["x"] = x
        return x

    spec = derive(record)
    call = ToolCall(call_id="1", name="record", arguments='{"x": 1}')
    registry = HookRegistry()

    def _rewrite(event: BeforeToolCallEvent) -> None:
        event.tool_use = dataclasses.replace(event.tool_use, arguments='{"x": 99}')

    registry.add_callback(BeforeToolCallEvent, _rewrite)

    run_calls([call], {"record": spec}, hooks=registry)

    assert received == {"x": 99}


def test_hook_replaces_result_on_after_event() -> None:
    spec = derive(add_one)
    call = ToolCall(call_id="1", name="add_one", arguments='{"x": 1}')
    registry = HookRegistry()

    def _replace(event: AfterToolCallEvent) -> None:
        event.result = dataclasses.replace(event.result, output=json.dumps("replaced"))

    registry.add_callback(AfterToolCallEvent, _replace)

    results = run_calls([call], {"add_one": spec}, hooks=registry)

    assert results[0].output == json.dumps("replaced")


def test_malformed_json_arguments_still_fires_both_events() -> None:
    spec = derive(add_one)
    call = ToolCall(call_id="1", name="add_one", arguments="not json")
    before_events: list[BeforeToolCallEvent] = []
    after_events: list[AfterToolCallEvent] = []
    registry = HookRegistry()
    registry.add_callback(BeforeToolCallEvent, before_events.append)
    registry.add_callback(AfterToolCallEvent, after_events.append)

    run_calls([call], {"add_one": spec}, hooks=registry)

    assert len(before_events) == 1
    assert len(after_events) == 1
    assert isinstance(after_events[0].result.failure, ToolArgumentError)


def test_missing_required_argument_still_fires_both_events() -> None:
    spec = derive(needs_team)
    call = ToolCall(call_id="1", name="needs_team", arguments="{}")
    before_events: list[BeforeToolCallEvent] = []
    after_events: list[AfterToolCallEvent] = []
    registry = HookRegistry()
    registry.add_callback(BeforeToolCallEvent, before_events.append)
    registry.add_callback(AfterToolCallEvent, after_events.append)

    run_calls([call], {"needs_team": spec}, hooks=registry)

    assert len(before_events) == 1
    assert len(after_events) == 1
    assert isinstance(after_events[0].result.failure, ToolArgumentError)


def test_hooks_none_matches_omitting_hooks_parameter() -> None:
    spec = derive(add_one)
    call = ToolCall(call_id="1", name="add_one", arguments='{"x": 1}')

    results_default = run_calls([call], {"add_one": spec})
    results_explicit_none = run_calls([call], {"add_one": spec}, hooks=None)

    assert results_default == results_explicit_none
    assert results_default[0].output == json.dumps(2)
    assert results_default[0].failure is None
