"""Slice 4: invocation lifecycle events and cross-path event parity.

See .specs/002-hooks-and-serve/spec.md AC-04, AC-06, AC-07, AC-16, AC-17 and
.specs/002-hooks-and-serve/code-design.md §5, §7 ("Contract for slice 4").
"""

from __future__ import annotations

from typing import Any

import pytest

from alloy import Agent
from alloy._schema import tool
from alloy.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from conftest import StubConversations

# --- stubs for __call__/invoke_async, matching tests/test_slice3_tool_loop.py exactly ---


class _FunctionCallItem:
    """Mimics openai's ResponseFunctionToolCall: call_id, name, raw arguments string."""

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.type = "function_call"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class _StubResponse:
    def __init__(self, content: str | None = None, output: list[Any] | None = None) -> None:
        self.output_text = content
        self.output = list(output or [])


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
        self.conversations = StubConversations()


# --- stubs for stream_async, matching tests/test_slice6_async_streaming.py exactly ---


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
    def __init__(self, turns: list[list[Any]]) -> None:
        self._turns = turns
        self.stream_call_count = 0

    def create(self, *, stream: bool = False, **kwargs: Any) -> Any:
        events = self._turns[self.stream_call_count]
        self.stream_call_count += 1
        return _StubStream(events)


class _StubStreamingClient:
    def __init__(self, responses: _StubStreamingResponses) -> None:
        self.responses = responses
        self.conversations = StubConversations()


@tool
def add_one(x: int) -> int:
    """Add one to a number.

    Args:
        x: The input number.
    """
    return x + 1


class _EventLogger(HookProvider):
    """Records the class name of every event it sees, in firing order."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def register_hooks(self, registry: HookRegistry) -> None:
        for event_type in (
            BeforeInvocationEvent,
            AfterInvocationEvent,
            BeforeToolCallEvent,
            AfterToolCallEvent,
        ):
            registry.add_callback(event_type, lambda e: self._log.append(type(e).__name__))


def _raise_boom(event: Any) -> None:
    raise RuntimeError("boom")


def test_before_invocation_fires_once_before_any_backend_call() -> None:
    call_counts_at_emit: list[int] = []
    seen_prompts: list[str] = []
    client = _StubClient([_StubResponse(content="hi")])

    class _Recorder(HookProvider):
        def register_hooks(self, registry: HookRegistry) -> None:
            def _record(event: BeforeInvocationEvent) -> None:
                call_counts_at_emit.append(len(client.responses.calls))
                seen_prompts.append(event.prompt)

            registry.add_callback(BeforeInvocationEvent, _record)

    agent = Agent(model="gpt-4o", client=client, hooks=[_Recorder()])
    result = agent("hi there")

    assert call_counts_at_emit == [0]
    assert seen_prompts == ["hi there"]
    assert result.text == "hi"


def test_after_invocation_fires_once_with_matching_result() -> None:
    events: list[AfterInvocationEvent] = []
    client = _StubClient([_StubResponse(content="done")])

    class _Recorder(HookProvider):
        def register_hooks(self, registry: HookRegistry) -> None:
            registry.add_callback(AfterInvocationEvent, events.append)

    agent = Agent(model="gpt-4o", client=client, hooks=[_Recorder()])
    result = agent("hi")

    assert len(events) == 1
    assert events[0].agent is agent
    assert events[0].result.text == result.text


def test_add_hook_after_construction_registers_and_fires_on_next_run() -> None:
    events: list[str] = []
    client = _StubClient([_StubResponse(content="hi")])
    agent = Agent(model="gpt-4o", client=client)

    class _Recorder(HookProvider):
        def register_hooks(self, registry: HookRegistry) -> None:
            registry.add_callback(BeforeInvocationEvent, lambda e: events.append("before"))

    agent.hooks.add_hook(_Recorder())
    agent("hi")

    assert events == ["before"]


def test_hook_raising_in_before_invocation_propagates() -> None:
    client = _StubClient([_StubResponse(content="hi")])

    class _Boom(HookProvider):
        def register_hooks(self, registry: HookRegistry) -> None:
            registry.add_callback(BeforeInvocationEvent, _raise_boom)

    agent = Agent(model="gpt-4o", client=client, hooks=[_Boom()])

    with pytest.raises(RuntimeError, match="boom"):
        agent("hi")


def test_no_hooks_registered_behaves_exactly_as_before() -> None:
    client = _StubClient([_StubResponse(content="hi")])
    agent = Agent(model="gpt-4o", client=client)

    result = agent("hi")

    assert result.text == "hi"
    assert isinstance(agent.hooks, HookRegistry)


async def test_ac17_all_three_call_paths_fire_the_same_event_sequence() -> None:
    call_item = _FunctionCallItem(call_id="call_1", name="add_one", arguments='{"x": 1}')

    sync_log: list[str] = []
    sync_client = _StubClient(
        [_StubResponse(output=[call_item]), _StubResponse(content="handled")]
    )
    sync_agent = Agent(
        model="gpt-4o", tools=[add_one], client=sync_client, hooks=[_EventLogger(sync_log)]
    )

    async_log: list[str] = []
    async_client = _StubClient(
        [_StubResponse(output=[call_item]), _StubResponse(content="handled")]
    )
    async_agent = Agent(
        model="gpt-4o", tools=[add_one], client=async_client, hooks=[_EventLogger(async_log)]
    )

    stream_log: list[str] = []
    first_turn = [_RawOutputItemDoneEvent(call_item), _RawCompletedEvent()]
    second_turn = [
        _RawDeltaEvent("handled"),
        _RawTextDoneEvent("handled"),
        _RawCompletedEvent(),
    ]
    stream_client = _StubStreamingClient(_StubStreamingResponses(turns=[first_turn, second_turn]))
    stream_agent = Agent(
        model="gpt-4o", tools=[add_one], client=stream_client, hooks=[_EventLogger(stream_log)]
    )

    sync_result = sync_agent("go")
    async_result = await async_agent.invoke_async("go")
    async for _ in stream_agent.stream_async("go"):
        pass

    expected = [
        "BeforeInvocationEvent",
        "BeforeToolCallEvent",
        "AfterToolCallEvent",
        "AfterInvocationEvent",
    ]

    assert sync_result.text == "handled"
    assert async_result.text == "handled"
    assert sync_log == expected
    assert async_log == expected
    assert stream_log == expected
