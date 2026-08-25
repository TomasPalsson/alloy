"""Slice 5: incremental tool-call arguments (B21-B23).

Azure streams a tool call's arguments as raw JSON fragments across many
`response.function_call_arguments.delta` events, not as one blob - see spec Appendix D's
live-measured 13 deltas for a two-argument tool. `_loop.extract_tool_argument_delta` pulls
`(call_id, delta)` off one such raw event; `Agent.stream_async` threads it through as a new
`{"tool_arguments_delta": (call_id, delta)}` shape (code-design.md Sec 9, pinned so this
slice cannot invent a second key for it).

Ordering chosen for `run_stream`: buffer each call's deltas by `call_id` as they arrive,
and flush them as TOOL_CALL_ARGS only once that call's COMPLETE `ToolCall` shows up via the
unchanged `{"current_tool_use": ...}` shape - the same point slice 4 already opens
TOOL_CALL_START from. Two things force this, not a coin flip:

1. AG-UI's TOOL_CALL_START carries `tool_call_name` up front, with no later "rename" event.
   alloy's own stream never knows a tool's name before the COMPLETE ToolCall arrives (see
   slice 4's B16, which pins TOOL_CALL_START.tool_call_name to that exact value) - so no
   earlier moment can legally open the bracket a delta's TOOL_CALL_ARGS would need to sit in.
2. AC-13 only requires more than one TOOL_CALL_ARGS whose deltas concatenate to the whole
   argument JSON - it does not require wire-clock realtime delivery relative to other run
   events. Buffer-then-flush satisfies the letter of AC-13 while satisfying conformance
   rule 6 (TOOL_CALL_ARGS needs an already-open TOOL_CALL_START) and leaving
   `current_tool_use`'s meaning - a COMPLETE tool call, is what TOOL_CALL_END is emitted
   from - exactly as slice 4 left it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import ag_ui.core as ag_ui_core
import pytest

import alloy.agui as agui
from alloy import Agent
from alloy._loop import extract_tool_argument_delta
from alloy._schema import tool
from alloy.contracts import AgentResult, StreamEvent, ToolCall, ToolResult
from alloy.hooks import AfterToolCallEvent, HookRegistry
from conftest import StubConversations

# Appendix D's REAL recorded fragments for a two-argument tool call - not tidy fakes.
# '{"city":' would hide exactly the "a single delta need not be valid JSON" trap B22 exists
# to document.
_ARGUMENT_FRAGMENTS = [
    '{"',
    "city",
    '":"',
    "Re",
    "yk",
    "jav",
    "ik",
    '","',
    "unit",
    '":"',
    "c",
    "elsius",
    '"}',
]
_FULL_ARGUMENTS_JSON = "".join(_ARGUMENT_FRAGMENTS)


# --- extract_tool_argument_delta: raw-event stubs ---------------------------------------


class _RawEvent:
    """A minimal raw-event stub carrying only `.type`.

    `extract_tool_argument_delta` must decide "is this mine?" from `.type` alone before
    touching any other attribute, exactly as `extract_tool_call_from_stream_item` already
    does (`getattr(raw_event, "type", None)`) - so Appendix D's other ten raw types need
    nothing more than a type tag to prove this extractor ignores them.
    """

    def __init__(self, type_: str) -> None:
        self.type = type_


class _RawArgsDeltaEvent:
    # Mirrors the real event: verified against live Azure it carries item_id and NOT
    # call_id. A stub with call_id would let a broken extractor pass.
    def __init__(self, item_id: str, delta: str) -> None:
        self.type = "response.function_call_arguments.delta"
        self.item_id = item_id
        self.delta = delta


# Every Appendix D raw type except the one this extractor handles.
_OTHER_APPENDIX_D_RAW_TYPES = [
    "response.created",
    "response.in_progress",
    "response.function_call_arguments.done",
    "response.output_item.done",
    "response.content_part.added",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.done",
    "response.completed",
]


@pytest.mark.parametrize("raw_type", _OTHER_APPENDIX_D_RAW_TYPES)
def test_extract_tool_argument_delta_returns_none_for_every_other_appendix_d_type(
    raw_type: str,
) -> None:
    assert extract_tool_argument_delta(_RawEvent(raw_type)) is None


def test_extract_tool_argument_delta_returns_none_for_event_with_no_type_attribute() -> None:
    assert extract_tool_argument_delta(object()) is None


def test_extract_tool_argument_delta_reads_item_id_and_delta_off_the_matching_event() -> None:
    event = _RawArgsDeltaEvent(item_id="fc_abc", delta='{"')
    assert extract_tool_argument_delta(event) == ("fc_abc", '{"')


def test_extract_tool_argument_delta_returns_item_id_since_call_id_is_absent() -> None:
    # Live Azure sends only ('delta', 'item_id', 'output_index', 'sequence_number', 'type').
    # An extractor reading call_id would return None forever and the feature would degrade
    # to one batched chunk with no error at all.
    event = _RawArgsDeltaEvent(item_id="fc_abc", delta="x")
    assert not hasattr(event, "call_id")
    assert extract_tool_argument_delta(event) == ("fc_abc", "x")


@pytest.mark.parametrize("fragment", _ARGUMENT_FRAGMENTS)
def test_extract_tool_argument_delta_passes_every_appendix_d_fragment_through_unparsed(
    fragment: str,
) -> None:
    # Each fragment round-trips byte-for-byte - the extractor must not trim, strip, or
    # otherwise "clean up" a delta that (correctly) isn't valid JSON on its own.
    event = _RawArgsDeltaEvent(item_id="fc_weather", delta=fragment)
    assert extract_tool_argument_delta(event) == ("fc_weather", fragment)


# --- B22: a single delta need not be valid JSON; only the concatenation is --------------


def test_b22_a_single_delta_is_not_required_to_be_valid_json() -> None:
    mid_fragment = _ARGUMENT_FRAGMENTS[1]
    assert mid_fragment == "city"

    with pytest.raises(json.JSONDecodeError):
        json.loads(mid_fragment)

    assert json.loads(_FULL_ARGUMENTS_JSON) == {"city": "Reykjavik", "unit": "celsius"}


def test_b22_tool_call_args_event_delta_carries_the_fragment_through_unvalidated() -> None:
    # Documents the trap at the AG-UI wire-event level too: ToolCallArgsEvent.delta is a
    # plain str field - nothing on it parses or validates JSON, so a mid-stream fragment
    # sails straight through construction.
    mid_fragment = _ARGUMENT_FRAGMENTS[1]
    event = ag_ui_core.ToolCallArgsEvent(tool_call_id="call-1", delta=mid_fragment)

    assert event.delta == "city"
    with pytest.raises(json.JSONDecodeError):
        json.loads(event.delta)


# --- B21: run_stream, real Appendix D fragments, through agui.run_stream ----------------


class _FakeAgentWithHooks:
    """Stands in for `alloy.Agent`, exposing a real `HookRegistry` at `.hooks`.

    Mirrors slice 4's `_FakeAgentWithHooks` (test_agui_slice4_tools.py): a scripted list of
    steps, either a `StreamEvent` to yield or an `AfterToolCallEvent` to fire through the
    same registry `run_stream` itself registers on.
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


def test_b21_thirteen_real_fragments_yield_more_than_one_tool_call_args_that_concatenate() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-1", name="get_weather", arguments=_FULL_ARGUMENTS_JSON)
    result = ToolResult(call_id="call-1", output=json.dumps({"temp": 5}))
    fake.steps = [
        # Azure names the tool at output_item.added, BEFORE any fragment, which is what
        # lets TOOL_CALL_START open the bracket the fragments then stream into. Buffering
        # them until the call completes would flush all 13 at one instant and render as
        # one blob — the behaviour this slice exists to replace.
        {"tool_call_started": ToolCall(call_id="call-1", name="get_weather", arguments="")},
        *({"tool_arguments_delta": ("call-1", fragment)} for fragment in _ARGUMENT_FRAGMENTS),
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="it is 5 degrees in Reykjavik")},
    ]
    run_input = _run_input(messages=[_user_message("weather in Reykjavik, celsius please")])

    events = asyncio.run(_collect(fake, run_input))

    starts = [e for e in events if isinstance(e, ag_ui_core.ToolCallStartEvent)]
    args = [e for e in events if isinstance(e, ag_ui_core.ToolCallArgsEvent)]
    ends = [e for e in events if isinstance(e, ag_ui_core.ToolCallEndEvent)]
    assert len(starts) == 1
    # Appendix D's exact measured count for this tool call, not just "more than one".
    assert len(args) == 13
    assert len(ends) == 1
    assert all(a.tool_call_id == "call-1" for a in args)
    assert [a.delta for a in args] == _ARGUMENT_FRAGMENTS
    assert "".join(a.delta for a in args) == '{"city":"Reykjavik","unit":"celsius"}'

    start_idx = events.index(starts[0])
    end_idx = events.index(ends[0])
    args_indices = [events.index(a) for a in args]
    assert start_idx < min(args_indices)
    assert max(args_indices) < end_idx
    # The completing event must only CLOSE the bracket; re-emitting ARGS there would
    # repeat the whole payload after the fragments that already carried it.
    assert len(args) == len(_ARGUMENT_FRAGMENTS)
    agui.check_conformance(events)


# --- B23: REGRESSION - stream_async's existing shapes are unchanged when no argument ----
# --- deltas occur, driven through the REAL Agent.stream_async (not the agui fake) -------


class _RawDeltaEvent:
    def __init__(self, delta: str) -> None:
        self.type = "response.output_text.delta"
        self.delta = delta


class _RawTextDoneEvent:
    def __init__(self, text: str) -> None:
        self.type = "response.output_text.done"
        self.text = text


class _RawCompletedEvent:
    def __init__(self) -> None:
        self.type = "response.completed"


class _RawFunctionCallItem:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.type = "function_call"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class _RawOutputItemDoneEvent:
    def __init__(self, item: Any) -> None:
        self.type = "response.output_item.done"
        self.item = item


class _StubStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __iter__(self) -> Any:
        yield from self._events


class _StubStreamingResponses:
    """Same shape as test_slice6_async_streaming.py's stub: one stream per `create()` call."""

    def __init__(self, turns: list[list[Any]]) -> None:
        self._turns = turns
        self._call_count = 0

    def create(self, *, stream: bool = False, **kwargs: Any) -> Any:
        events = self._turns[self._call_count]
        self._call_count += 1
        return _StubStream(events)


class _StubStreamingClient:
    def __init__(self, responses: Any) -> None:
        self.responses = responses
        self.conversations = StubConversations()


async def test_b23_stream_async_existing_shapes_unchanged_when_no_argument_deltas_occur() -> None:
    # A raw stream built exactly like Azure's OLD (pre-slice-5) tool-calling shape: no
    # response.function_call_arguments.delta events anywhere, only output_item.done.
    call_item = _RawFunctionCallItem(
        call_id="call_1", name="get_oncall", arguments='{"team": "data"}'
    )
    first_turn = [_RawOutputItemDoneEvent(call_item), _RawCompletedEvent()]
    second_turn = [
        _RawDeltaEvent("alice is on call"),
        _RawTextDoneEvent("alice is on call"),
        _RawCompletedEvent(),
    ]

    @tool
    def get_oncall(team: str) -> str:
        """Get the on-call engineer for a team.

        Args:
            team: The team name.
        """
        return "alice"

    client = _StubStreamingClient(_StubStreamingResponses(turns=[first_turn, second_turn]))
    agent = Agent(model="gpt-4o", tools=[get_oncall], client=client)

    events = [e async for e in agent.stream_async("who is on call for data?")]
    event_kinds = {next(iter(e)) for e in events}

    # A non-AG-UI caller sees exactly the three shapes it saw before this slice - never the
    # new "tool_arguments_delta" key, since this raw stream carries no delta events at all.
    assert event_kinds == {"data", "current_tool_use", "result"}

    tool_use_index = next(i for i, e in enumerate(events) if "current_tool_use" in e)
    result_index = next(i for i, e in enumerate(events) if "result" in e)
    assert tool_use_index < result_index
    tool_use_call = events[tool_use_index]["current_tool_use"]
    assert isinstance(tool_use_call, ToolCall)
    assert tool_use_call.name == "get_oncall"
    assert tool_use_call.call_id == "call_1"

    result = events[result_index]["result"]
    assert isinstance(result, AgentResult)
    assert result.text == "alice is on call"
