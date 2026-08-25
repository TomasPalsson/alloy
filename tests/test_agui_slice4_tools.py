"""Slice 4: tool call bracketing (B15-B20) + text/tool interleave (B49).

`run_stream` must translate one alloy `{"current_tool_use": ToolCall}` event into
TOOL_CALL_START/ARGS/END sharing that call's `call_id` verbatim, then a TOOL_CALL_RESULT
sourced from `AfterToolCallEvent` - the only place a tool's result is observable, since
alloy's own stream never yields one (FR-05, FR-06). A text message open when a tool call
starts must close first (AC-42, rule 8): `_FakeAgentWithHooks` fires `AfterToolCallEvent`
itself, at the point in its script the real `_loop.run_calls` would - after the owning
turn's `current_tool_use` events, not interleaved with them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import cast

import ag_ui.core as ag_ui_core

import alloy.agui as agui
from alloy import Agent
from alloy.contracts import AgentResult, StreamEvent, ToolCall, ToolResult
from alloy.hooks import AfterToolCallEvent, HookRegistry


class _FakeAgentWithHooks:
    """Stands in for `alloy.Agent`, exposing a real `HookRegistry` at `.hooks`.

    `run_stream` registers on this exact registry, the same object a test's own
    `AfterToolCallEvent` steps fire through - so the callback-removal contract (B20) is
    exercised against the real `HookRegistry`, not a mock of it.

    `steps` is set after construction (not passed to `__init__`) so a test can build an
    `AfterToolCallEvent` referencing `agent=cast(Agent, fake)` before the steps exist.
    """

    def __init__(self) -> None:
        self.hooks = HookRegistry()
        self.steps: list[StreamEvent | AfterToolCallEvent] = []
        self.recorded_prompts: list[str] = []

    async def stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]:
        self.recorded_prompts.append(prompt)
        for step in self.steps:
            if isinstance(step, AfterToolCallEvent):
                self.hooks.emit(step)
            else:
                yield step


def _user_message(text: str, message_id: str = "u1") -> ag_ui_core.UserMessage:
    return ag_ui_core.UserMessage(id=message_id, content=text)


def _run_input(
    messages: list[ag_ui_core.Message],
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> ag_ui_core.RunAgentInput:
    return ag_ui_core.RunAgentInput(
        thread_id=thread_id,
        run_id=run_id,
        state=None,
        messages=messages,
        tools=[],
        context=[],
        forwarded_props=None,
    )


async def _collect(
    fake: _FakeAgentWithHooks, run_input: ag_ui_core.RunAgentInput
) -> list[ag_ui_core.BaseEvent]:
    return [event async for event in agui.run_stream(cast(Agent, fake), run_input)]


# --- B15: one tool call -> START before every ARGS before END, one shared id ------------


def test_b15_one_tool_call_brackets_start_before_args_before_end() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-1", name="get_weather", arguments='{"city": "Reykjavik"}')
    result = ToolResult(call_id="call-1", output=json.dumps({"temp": 5}))
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="it is 5 degrees")},
    ]
    run_input = _run_input(messages=[_user_message("weather please")])

    events = asyncio.run(_collect(fake, run_input))

    starts = [e for e in events if isinstance(e, ag_ui_core.ToolCallStartEvent)]
    args = [e for e in events if isinstance(e, ag_ui_core.ToolCallArgsEvent)]
    ends = [e for e in events if isinstance(e, ag_ui_core.ToolCallEndEvent)]
    assert len(starts) == 1
    assert len(args) == 1
    assert len(ends) == 1
    start_idx = events.index(starts[0])
    args_idx = events.index(args[0])
    end_idx = events.index(ends[0])
    assert start_idx < args_idx < end_idx
    assert starts[0].tool_call_id == "call-1"
    assert args[0].tool_call_id == "call-1"
    assert ends[0].tool_call_id == "call-1"
    assert args[0].delta == call.arguments
    agui.check_conformance(events)


# --- B16: TOOL_CALL_START.tool_call_name/tool_call_id match the alloy ToolCall exactly ---


def test_b16_tool_call_start_name_and_id_match_the_alloy_tool_call_literally() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(
        call_id="tc-42-xyz", name="lookup_flight_status", arguments='{"flight": "FI415"}'
    )
    result = ToolResult(call_id="tc-42-xyz", output=json.dumps({"status": "on time"}))
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="FI415 is on time")},
    ]
    run_input = _run_input(messages=[_user_message("is FI415 on time?")])

    events = asyncio.run(_collect(fake, run_input))

    starts = [e for e in events if isinstance(e, ag_ui_core.ToolCallStartEvent)]
    assert len(starts) == 1
    assert starts[0].tool_call_name == "lookup_flight_status"
    assert starts[0].tool_call_id == "tc-42-xyz"
    agui.check_conformance(events)


# --- B17: TOOL_CALL_RESULT follows that call's END, same id, non-empty message_id -------


def test_b17_tool_call_result_follows_end_with_same_id_and_nonempty_message_id() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-9", name="get_weather", arguments='{"city": "Akureyri"}')
    result = ToolResult(call_id="call-9", output=json.dumps({"temp": -2}))
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="it is -2 degrees in Akureyri")},
    ]
    run_input = _run_input(messages=[_user_message("weather in Akureyri?")])

    events = asyncio.run(_collect(fake, run_input))

    ends = [e for e in events if isinstance(e, ag_ui_core.ToolCallEndEvent)]
    results = [e for e in events if isinstance(e, ag_ui_core.ToolCallResultEvent)]
    assert len(ends) == 1
    assert len(results) == 1
    end_idx = events.index(ends[0])
    result_idx = events.index(results[0])
    assert end_idx < result_idx
    assert results[0].tool_call_id == "call-9"
    assert results[0].message_id != ""
    assert results[0].content == result.output
    agui.check_conformance(events)


# --- B18: a tool that raises still produces TOOL_CALL_RESULT; run still finishes --------


def test_b18_tool_that_raises_still_produces_tool_call_result_and_run_finishes() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-boom", name="explode", arguments="{}")
    failure = ValueError("boom: division by zero")
    # Mirrors _loop._failure's shape: the tool never ran to a real output, so the ToolResult
    # carries the error, JSON-encoded, with .failure set - never a bare exception escaping.
    result = ToolResult(
        call_id="call-boom", output=json.dumps({"error": str(failure)}), failure=failure
    )
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="", tool_failures=(failure,))},
    ]
    run_input = _run_input(messages=[_user_message("run the exploding tool")])

    events = asyncio.run(_collect(fake, run_input))

    tool_results = [e for e in events if isinstance(e, ag_ui_core.ToolCallResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "call-boom"
    assert "boom: division by zero" in tool_results[0].content
    assert isinstance(events[-1], ag_ui_core.RunFinishedEvent)
    assert not any(isinstance(e, ag_ui_core.RunErrorEvent) for e in events)
    agui.check_conformance(events)


# --- B19: two tool calls in one turn -> distinct ids, non-nested brackets ---------------


def test_b19_two_tool_calls_have_distinct_ids_and_non_nested_brackets() -> None:
    fake = _FakeAgentWithHooks()
    call_a = ToolCall(call_id="call-a", name="get_weather", arguments='{"city": "Reykjavik"}')
    call_b = ToolCall(call_id="call-b", name="get_time", arguments='{"city": "Reykjavik"}')
    result_a = ToolResult(call_id="call-a", output=json.dumps({"temp": 5}))
    result_b = ToolResult(call_id="call-b", output=json.dumps({"time": "14:00"}))
    fake.steps = [
        {"current_tool_use": call_a},
        {"current_tool_use": call_b},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call_a, result=result_a),
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call_b, result=result_b),
        {"result": AgentResult(text="it is 5 degrees and 14:00")},
    ]
    run_input = _run_input(messages=[_user_message("weather and time please")])

    events = asyncio.run(_collect(fake, run_input))

    starts = [e for e in events if isinstance(e, ag_ui_core.ToolCallStartEvent)]
    ends = [e for e in events if isinstance(e, ag_ui_core.ToolCallEndEvent)]
    assert {e.tool_call_id for e in starts} == {"call-a", "call-b"}
    assert {e.tool_call_id for e in ends} == {"call-a", "call-b"}
    start_idx = {e.tool_call_id: events.index(e) for e in starts}
    end_idx = {e.tool_call_id: events.index(e) for e in ends}
    assert start_idx["call-a"] != start_idx["call-b"]
    # Neither bracket contains the other: whichever call started first must also have
    # ended before the other call's bracket opens (sequential, not interleaved).
    first_id, second_id = sorted(start_idx, key=lambda call_id: start_idx[call_id])
    assert end_idx[first_id] < start_idx[second_id]
    results = [e for e in events if isinstance(e, ag_ui_core.ToolCallResultEvent)]
    assert {e.tool_call_id for e in results} == {"call-a", "call-b"}
    agui.check_conformance(events)


# --- B20: run_stream called twice on one Agent -> TOOL_CALL_RESULT does not double ------


def test_b20_calling_run_stream_twice_does_not_double_tool_call_result() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-once", name="get_weather", arguments='{"city": "Reykjavik"}')
    result = ToolResult(call_id="call-once", output=json.dumps({"temp": 5}))
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="it is 5 degrees")},
    ]
    run_input = _run_input(messages=[_user_message("weather please")])

    first_events = asyncio.run(_collect(fake, run_input))
    second_events = asyncio.run(_collect(fake, run_input))

    first_results = [e for e in first_events if isinstance(e, ag_ui_core.ToolCallResultEvent)]
    second_results = [e for e in second_events if isinstance(e, ag_ui_core.ToolCallResultEvent)]
    assert len(first_results) == 1
    assert len(second_results) == 1
    agui.check_conformance(first_events)
    agui.check_conformance(second_events)


# --- B49: text, then a tool call, then more text - END precedes START, new message_id ---


def test_b49_text_then_tool_call_then_text_closes_message_before_tool_opens() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-mid", name="lookup", arguments='{"q": "iceland"}')
    result = ToolResult(call_id="call-mid", output=json.dumps({"answer": "Reykjavik"}))
    fake.steps = [
        {"data": "Let me check "},
        {"data": "that for you."},
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"data": "The answer is "},
        {"data": "Reykjavik."},
        {"result": AgentResult(text="Let me check that for you.The answer is Reykjavik.")},
    ]
    run_input = _run_input(messages=[_user_message("what is the capital of iceland?")])

    events = asyncio.run(_collect(fake, run_input))

    text_starts = [e for e in events if isinstance(e, ag_ui_core.TextMessageStartEvent)]
    text_ends = [e for e in events if isinstance(e, ag_ui_core.TextMessageEndEvent)]
    tool_starts = [e for e in events if isinstance(e, ag_ui_core.ToolCallStartEvent)]
    assert len(text_starts) == 2
    assert len(text_ends) == 2
    assert len(tool_starts) == 1
    assert text_starts[0].message_id != text_starts[1].message_id
    end_idx = events.index(text_ends[0])
    tool_start_idx = events.index(tool_starts[0])
    second_start_idx = events.index(text_starts[1])
    assert end_idx < tool_start_idx < second_start_idx
    agui.check_conformance(events)
