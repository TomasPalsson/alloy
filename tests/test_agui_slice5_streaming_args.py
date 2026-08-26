"""Slice 5: incremental tool-call arguments (B21-B23).

Azure streams a tool call's arguments as raw JSON fragments across many
`response.function_call_arguments.delta` events, not as one blob - see spec Appendix D's
live-measured 13 deltas for a two-argument tool. Two extractors in `_loop.py` split the work:

- `extract_tool_call_start` reads `response.output_item.added` where `item.type ==
  "function_call"` and returns a `ToolCall` with empty `arguments` - Azure knows the tool's
  NAME before any argument fragment arrives, which is what lets `TOOL_CALL_START` open early
  instead of waiting for the complete call. The same raw event type also carries `reasoning`
  and `message` items, so the `function_call` filter is load-bearing, not decoration.
- `extract_tool_argument_delta` reads `response.function_call_arguments.delta` and returns
  `(item_id, delta)` - `item_id`, NOT `call_id`: verified live, this event carries only
  `delta`, `item_id`, `output_index`, `sequence_number`, `type`. `item_id` and the owning
  call's `call_id` are DIFFERENT values on the wire (e.g. `fc_...` vs `call_...`); resolving
  one to the other is `Agent.stream_async`'s job, using the mapping the `.added` event
  supplies. A delta whose `item_id` has no known mapping is dropped, not invented into a
  fabricated call.

`run_stream` (agui.py) brackets tool calls two ways depending on what it's fed: a call whose
START already went out via `tool_call_started` gets `TOOL_CALL_ARGS` streamed live, then
`current_tool_use` closes it with `TOOL_CALL_END` alone. A call delivered with no preceding
`tool_call_started` (the pre-slice-5 shape, still exercised by slice 4's own tests) still
gets its whole bracket - START/ARGS/END - from `current_tool_use` in one step, unchanged.
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
from alloy._loop import extract_tool_argument_delta, extract_tool_call_start
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

_ALL_APPENDIX_D_RAW_TYPES = [
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.output_item.done",
    "response.content_part.added",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.done",
    "response.completed",
]


def _other_appendix_d_types(target: str) -> list[str]:
    return [t for t in _ALL_APPENDIX_D_RAW_TYPES if t != target]


# --- raw-event stubs, shared by both extractors' tests -----------------------------------


class _RawEvent:
    """A minimal raw-event stub carrying only `.type`.

    Both extractors must decide "is this mine?" from `.type` alone before touching any
    other attribute, exactly as `extract_tool_call_from_stream_item` already does
    (`getattr(raw_event, "type", None)`) - so every other Appendix D raw type needs nothing
    more than a type tag to prove an extractor ignores it.
    """

    def __init__(self, type_: str) -> None:
        self.type = type_


class _RawArgsDeltaEvent:
    def __init__(self, item_id: str, delta: str) -> None:
        self.type = "response.function_call_arguments.delta"
        self.item_id = item_id
        self.delta = delta


class _RawFunctionCallItem:
    def __init__(
        self, call_id: str, name: str, arguments: str, item_id: str = "fc_default"
    ) -> None:
        self.type = "function_call"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments
        self.id = item_id


class _RawNonFunctionCallItem:
    def __init__(self, type_: str) -> None:
        self.type = type_


class _RawOutputItemAddedEvent:
    def __init__(self, item: Any) -> None:
        self.type = "response.output_item.added"
        self.item = item


class _RawOutputItemDoneEvent:
    def __init__(self, item: Any) -> None:
        self.type = "response.output_item.done"
        self.item = item


# --- extract_tool_call_start --------------------------------------------------------------


@pytest.mark.parametrize("raw_type", _other_appendix_d_types("response.output_item.added"))
def test_extract_tool_call_start_returns_none_for_every_other_appendix_d_type(
    raw_type: str,
) -> None:
    assert extract_tool_call_start(_RawEvent(raw_type)) is None


def test_extract_tool_call_start_returns_none_for_event_with_no_type_attribute() -> None:
    assert extract_tool_call_start(object()) is None


@pytest.mark.parametrize("item_type", ["reasoning", "message"])
def test_extract_tool_call_start_returns_none_for_non_function_call_items(
    item_type: str,
) -> None:
    # output_item.added also carries reasoning and message items - the Appendix D landmine:
    # filtering on the outer event.type alone, without also checking item.type, would map
    # the wrong item's id to a tool call.
    event = _RawOutputItemAddedEvent(_RawNonFunctionCallItem(item_type))
    assert extract_tool_call_start(event) is None


def test_extract_tool_call_start_returns_a_tool_call_with_empty_arguments() -> None:
    # `arguments="already here"` on the raw item proves the extractor ignores it - Azure's
    # own item.arguments is "" at this point anyway, but nothing should rely on that.
    item = _RawFunctionCallItem(
        call_id="call_RzvvHEgpiYHGC4TUFSn2PDnd",
        name="get_weather",
        arguments="already here",
        item_id="fc_a3a1691637524080006a8daa0314088190b90b0b91a40086e3",
    )
    event = _RawOutputItemAddedEvent(item)

    result = extract_tool_call_start(event)

    assert result == ToolCall(
        call_id="call_RzvvHEgpiYHGC4TUFSn2PDnd", name="get_weather", arguments=""
    )


# --- extract_tool_argument_delta ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw_type", _other_appendix_d_types("response.function_call_arguments.delta")
)
def test_extract_tool_argument_delta_returns_none_for_every_other_appendix_d_type(
    raw_type: str,
) -> None:
    assert extract_tool_argument_delta(_RawEvent(raw_type)) is None


def test_extract_tool_argument_delta_returns_none_for_event_with_no_type_attribute() -> None:
    assert extract_tool_argument_delta(object()) is None


def test_extract_tool_argument_delta_reads_item_id_and_delta_off_the_matching_event() -> None:
    # item_id, NOT call_id: verified live, this event carries no call_id field at all.
    event = _RawArgsDeltaEvent(item_id="fc_a3a1", delta='{"')
    assert extract_tool_argument_delta(event) == ("fc_a3a1", '{"')


@pytest.mark.parametrize("fragment", _ARGUMENT_FRAGMENTS)
def test_extract_tool_argument_delta_passes_every_appendix_d_fragment_through_unparsed(
    fragment: str,
) -> None:
    # Each fragment round-trips byte-for-byte - the extractor must not trim, strip, or
    # otherwise "clean up" a delta that (correctly) isn't valid JSON on its own.
    event = _RawArgsDeltaEvent(item_id="fc_a3a1", delta=fragment)
    assert extract_tool_argument_delta(event) == ("fc_a3a1", fragment)


# --- B22: a single delta need not be valid JSON; only the concatenation is ---------------


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
    started = ToolCall(call_id="call-1", name="get_weather", arguments="")
    complete_call = ToolCall(call_id="call-1", name="get_weather", arguments=_FULL_ARGUMENTS_JSON)
    result = ToolResult(call_id="call-1", output=json.dumps({"temp": 5}))
    fake.steps = [
        {"tool_call_started": started},
        *({"tool_arguments_delta": ("call-1", fragment)} for fragment in _ARGUMENT_FRAGMENTS),
        {"current_tool_use": complete_call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=complete_call, result=result),
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
    assert starts[0].tool_call_id == "call-1"
    assert starts[0].tool_call_name == "get_weather"
    assert all(a.tool_call_id == "call-1" for a in args)
    assert [a.delta for a in args] == _ARGUMENT_FRAGMENTS
    assert "".join(a.delta for a in args) == '{"city":"Reykjavik","unit":"celsius"}'
    assert ends[0].tool_call_id == "call-1"

    start_idx = events.index(starts[0])
    end_idx = events.index(ends[0])
    args_indices = [events.index(a) for a in args]
    assert start_idx < min(args_indices)
    assert max(args_indices) < end_idx
    agui.check_conformance(events)


# --- item_id -> call_id resolution, through the REAL Agent.stream_async ------------------


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


class _RawStream:
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
        return _RawStream(events)


class _StubStreamingClient:
    def __init__(self, responses: Any) -> None:
        self.responses = responses
        self.conversations = StubConversations()


async def test_tool_arguments_delta_resolves_item_id_to_the_tool_calls_call_id() -> None:
    # Mirrors the live probe: item.id ('fc_...') and item.call_id ('call_...') are DIFFERENT
    # values on Azure's wire. A delta event carries only item_id, so stream_async must
    # resolve it back to call_id using the output_item.added event seen earlier for that item.
    call_id = "call_RzvvHEgpiYHGC4TUFSn2PDnd"
    item_id = "fc_a3a1691637524080006a8daa0314088190b90b0b91a40086e3"
    call_item = _RawFunctionCallItem(
        call_id=call_id, name="get_weather", arguments=_FULL_ARGUMENTS_JSON, item_id=item_id
    )
    first_turn = [
        _RawOutputItemAddedEvent(call_item),
        *(_RawArgsDeltaEvent(item_id=item_id, delta=fragment) for fragment in _ARGUMENT_FRAGMENTS),
        _RawOutputItemDoneEvent(call_item),
        _RawCompletedEvent(),
    ]
    second_turn = [
        _RawDeltaEvent("it is 5 degrees"),
        _RawTextDoneEvent("it is 5 degrees"),
        _RawCompletedEvent(),
    ]

    @tool
    def get_weather(city: str, unit: str) -> str:
        """Get the weather for a city.

        Args:
            city: The city name.
            unit: The unit to report the temperature in.
        """
        return "5 degrees"

    client = _StubStreamingClient(_StubStreamingResponses(turns=[first_turn, second_turn]))
    agent = Agent(model="gpt-4o", tools=[get_weather], client=client)

    events = [e async for e in agent.stream_async("weather in Reykjavik, celsius please")]

    started = [e["tool_call_started"] for e in events if "tool_call_started" in e]
    deltas = [e["tool_arguments_delta"] for e in events if "tool_arguments_delta" in e]
    completed = [e["current_tool_use"] for e in events if "current_tool_use" in e]

    assert len(started) == 1
    assert started[0] == ToolCall(call_id=call_id, name="get_weather", arguments="")

    assert len(deltas) == 13
    # Every delta carries the RESOLVED call id - never the raw item id straight off the wire.
    assert all(resolved_id == call_id for resolved_id, _ in deltas)
    assert [fragment for _, fragment in deltas] == _ARGUMENT_FRAGMENTS

    assert len(completed) == 1
    assert completed[0].call_id == call_id
    assert completed[0].arguments == _FULL_ARGUMENTS_JSON


async def test_argument_delta_with_no_matching_start_event_is_dropped_not_invented() -> None:
    # No output_item.added precedes this delta - stream_async must not fabricate a call_id
    # for a fragment it cannot attribute; a wrongly-attributed fragment corrupts that call's
    # arguments silently, which is worse than dropping it.
    first_turn = [
        _RawArgsDeltaEvent(item_id="fc_orphan", delta='{"'),
        _RawCompletedEvent(),
    ]
    client = _StubStreamingClient(_StubStreamingResponses(turns=[first_turn]))
    agent = Agent(model="gpt-4o", client=client)

    events = [e async for e in agent.stream_async("hi")]

    assert not any("tool_arguments_delta" in e for e in events)


# --- B23: REGRESSION - stream_async's existing shapes are unchanged when no output_item -
# --- .added / argument-delta events occur, driven through the REAL Agent.stream_async ---


async def test_b23_stream_async_existing_shapes_unchanged_when_no_argument_deltas_occur() -> None:
    # A raw stream built exactly like Azure's OLD (pre-slice-5) tool-calling shape: no
    # output_item.added and no response.function_call_arguments.delta events anywhere, only
    # output_item.done - as if a caller (or an older Azure API) never emitted the new events.
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

    # A non-AG-UI caller sees exactly the three shapes it saw before this slice - never
    # "tool_call_started" or "tool_arguments_delta", since this raw stream carries neither
    # of the raw events those come from.
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
