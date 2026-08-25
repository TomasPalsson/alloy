"""Slice 6: async streaming (priority) and async invocation."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from alloy import Agent, StreamingUnsupportedError
from alloy._schema import tool
from alloy.contracts import AgentResult, ToolCall


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.output_text = content
        self.output: list[Any] = []


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
    """A finite, in-memory stand-in for openai's `Stream[ResponseStreamEvent]`.

    `delay` pauses between items so a consumer that abandons iteration mid-way finds the
    worker thread still inside the for-loop, not already finished (see B26).
    """

    def __init__(self, events: list[Any], delay: float = 0.0) -> None:
        self._events = events
        self._delay = delay

    def __iter__(self) -> Any:
        for event in self._events:
            if self._delay:
                time.sleep(self._delay)
            yield event


class _StubStreamingResponses:
    """`create()` returns a plain response normally, or a `_StubStream` when `stream=True`."""

    def __init__(self, turns: list[list[Any]] | None = None, refuse_stream: bool = False) -> None:
        self._turns = turns or []
        self._refuse_stream = refuse_stream
        self.stream_call_count = 0

    def create(self, *, stream: bool = False, **kwargs: Any) -> Any:
        if not stream:
            return _StubResponse("unused")
        if self._refuse_stream:
            raise TypeError("create() got an unexpected keyword argument 'stream'")
        events = self._turns[self.stream_call_count]
        self.stream_call_count += 1
        return _StubStream(events)


class _StubDelayedStreamResponses:
    """`create()` always returns the same (deliberately slow) stream."""

    def __init__(self, stream: _StubStream) -> None:
        self._stream = stream

    def create(self, *, stream: bool = False, **kwargs: Any) -> Any:
        return self._stream if stream else _StubResponse("unused")


class _StubConversation:
    def __init__(self, id: str) -> None:
        self.id = id


class _StubConversations:
    def create(self, **kwargs: Any) -> _StubConversation:
        return _StubConversation("conv_1")


class _StubStreamingClient:
    def __init__(self, responses: Any) -> None:
        self.responses = responses
        self.conversations = _StubConversations()


class _StubBlockingResponses:
    """A non-streaming client used for invoke_async: create() blocks briefly."""

    def __init__(self, content: str, delay: float = 0.05) -> None:
        self._content = content
        self._delay = delay

    def create(self, **kwargs: Any) -> _StubResponse:
        time.sleep(self._delay)
        return _StubResponse(self._content)


class _StubBlockingClient:
    def __init__(self, content: str, delay: float = 0.05) -> None:
        self.responses = _StubBlockingResponses(content, delay)
        self.conversations = _StubConversations()


async def test_b18_text_deltas_yield_data_events_that_concatenate_to_full_text() -> None:
    turn = [
        _RawDeltaEvent("Hello"),
        _RawDeltaEvent(", "),
        _RawDeltaEvent("world"),
        _RawTextDoneEvent("Hello, world"),
        _RawCompletedEvent(),
    ]
    client = _StubStreamingClient(_StubStreamingResponses(turns=[turn]))
    agent = Agent(model="gpt-4o", client=client)

    data_events = [e async for e in agent.stream_async("hi") if "data" in e]
    concatenated = "".join(e["data"] for e in data_events)

    assert [e["data"] for e in data_events] == ["Hello", ", ", "world"]
    assert concatenated == "Hello, world"


async def test_b19_client_refusing_streaming_raises_streaming_unsupported_error() -> None:
    client = _StubStreamingClient(_StubStreamingResponses(refuse_stream=True))
    agent = Agent(model="gpt-4o", client=client)

    with pytest.raises(StreamingUnsupportedError, match="streaming"):
        async for _ in agent.stream_async("hi"):
            pass


async def test_b20_tool_call_emits_current_tool_use_before_result_submitted() -> None:
    call_item = _RawFunctionCallItem(
        call_id="call_1", name="get_oncall", arguments='{"team": "data"}'
    )
    first_turn = [_RawOutputItemDoneEvent(call_item), _RawCompletedEvent()]
    second_turn = [
        _RawDeltaEvent("alice is on call"),
        _RawTextDoneEvent("alice is on call"),
        _RawCompletedEvent(),
    ]

    seen_teams: list[str] = []

    @tool
    def get_oncall(team: str) -> str:
        """Get the on-call engineer for a team.

        Args:
            team: The team name.
        """
        seen_teams.append(team)
        return "alice"

    client = _StubStreamingClient(_StubStreamingResponses(turns=[first_turn, second_turn]))
    agent = Agent(model="gpt-4o", tools=[get_oncall], client=client)

    events = [e async for e in agent.stream_async("who is on call for data?")]
    event_kinds = [next(iter(e)) for e in events]

    assert "current_tool_use" in event_kinds
    tool_use_index = event_kinds.index("current_tool_use")
    result_index = event_kinds.index("result")
    assert tool_use_index < result_index
    # the tool actually ran, correlated to the announced call
    assert seen_teams == ["data"]
    tool_use_call = events[tool_use_index]["current_tool_use"]
    assert isinstance(tool_use_call, ToolCall)
    assert tool_use_call.name == "get_oncall"

    result = events[result_index]["result"]
    assert isinstance(result, AgentResult)
    assert result.text == "alice is on call"


async def test_b25_invoke_async_does_not_block_a_concurrent_task() -> None:
    client = _StubBlockingClient(content="hello", delay=0.05)
    agent = Agent(model="gpt-4o", client=client)
    ticks = {"count": 0}

    async def ticker() -> None:
        for _ in range(200):
            ticks["count"] += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.create_task(ticker())
    result = await agent.invoke_async("hello")
    await ticker_task

    assert isinstance(result, AgentResult)
    assert result.text == "hello"
    assert ticks["count"] > 0


async def test_b26_abandoned_stream_terminates_worker_thread() -> None:
    # warm the default thread-pool executor so its reused worker thread is in the baseline
    await asyncio.to_thread(lambda: None)
    baseline_thread_count = threading.active_count()

    many_events = [_RawDeltaEvent(str(i)) for i in range(50)]
    slow_stream = _StubStream(many_events, delay=0.02)
    client = _StubStreamingClient(_StubDelayedStreamResponses(slow_stream))
    agent = Agent(model="gpt-4o", client=client)

    seen = 0
    stream_events = cast(AsyncGenerator[Any, None], agent.stream_async("hi"))
    async with contextlib.aclosing(stream_events) as stream:
        async for _ in stream:
            seen += 1
            if seen >= 3:
                break

    assert seen == 3
    assert threading.active_count() <= baseline_thread_count
