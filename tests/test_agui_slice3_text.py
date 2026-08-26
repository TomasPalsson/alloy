"""Slice 3: text message lifecycle (B11-B14) + prompt extraction (B50, B51).

`run_stream` synthesizes TEXT_MESSAGE_START/CONTENT/END brackets around the raw
`{"data": ...}` deltas alloy's `Agent.stream_async` yields - there is no upstream
"text started"/"text ended" signal. `latest_user_prompt` pulls the newest
`role: "user"` message out of `RunAgentInput.messages` as the turn's prompt; the
backend conversation stays authoritative, so client-supplied history is never
replayed into the model (FR-26).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import ag_ui.core as ag_ui_core
import pytest

import alloy.agui as agui
from alloy import Agent
from alloy.contracts import AgentResult, StreamEvent
from alloy.hooks import HookRegistry


class _FakeAgent:
    """Stands in for `alloy.Agent`: no network, no Foundry client.

    Records every prompt `stream_async` was called with, so tests can assert on what
    the agent actually received instead of on output text.
    """

    def __init__(
        self, events: list[StreamEvent] | None = None, fail: Exception | None = None
    ) -> None:
        self._events = events if events is not None else []
        self._fail = fail
        self.recorded_prompts: list[str] = []
        # A real Agent always exposes .hooks; run_stream registers on it unconditionally
        # since slice 4, so a stand-in needs one too, not just the fakes that use it.
        self.hooks = HookRegistry()

    async def stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]:
        self.recorded_prompts.append(prompt)
        if self._fail is not None:
            raise self._fail
        for event in self._events:
            yield event


class _FakeAgentThatFailsAfterText:
    """Yields one text delta, then raises - the error twin of B14."""

    def __init__(self, fail: Exception) -> None:
        self._fail = fail
        self.recorded_prompts: list[str] = []
        self.hooks = HookRegistry()

    async def stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]:
        self.recorded_prompts.append(prompt)
        yield {"data": "partial"}
        raise self._fail


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


async def _collect(fake: object, run_input: ag_ui_core.RunAgentInput) -> list[ag_ui_core.BaseEvent]:
    return [event async for event in agui.run_stream(cast(Agent, fake), run_input)]


# --- B11: three deltas bracket into exactly one message ---------------------------------


def test_b11_three_text_deltas_bracket_into_one_message() -> None:
    fake = _FakeAgent(
        events=[
            {"data": "Hel"},
            {"data": "lo, "},
            {"data": "world"},
            {"result": AgentResult(text="Hello, world")},
        ]
    )
    run_input = _run_input(messages=[_user_message("hi")])

    events = asyncio.run(_collect(fake, run_input))

    starts = [e for e in events if isinstance(e, ag_ui_core.TextMessageStartEvent)]
    contents = [e for e in events if isinstance(e, ag_ui_core.TextMessageContentEvent)]
    ends = [e for e in events if isinstance(e, ag_ui_core.TextMessageEndEvent)]
    assert len(starts) == 1
    assert len(contents) == 3
    assert len(ends) == 1
    message_id = starts[0].message_id
    assert message_id != ""
    assert {c.message_id for c in contents} == {message_id}
    assert ends[0].message_id == message_id
    assert "".join(c.delta for c in contents) == "Hel" + "lo, " + "world"
    agui.check_conformance(events)


# --- B12: every CONTENT is preceded by an open START with its own message_id ------------


def test_b12_every_content_event_has_a_preceding_open_start() -> None:
    fake = _FakeAgent(
        events=[
            {"data": "a"},
            {"data": "b"},
            {"data": "c"},
            {"data": "d"},
            {"data": "e"},
            {"result": AgentResult(text="abcde")},
        ]
    )
    run_input = _run_input(messages=[_user_message("hi")])

    events = asyncio.run(_collect(fake, run_input))

    open_message_id: str | None = None
    content_count = 0
    for event in events:
        if isinstance(event, ag_ui_core.TextMessageStartEvent):
            assert open_message_id is None, "a second START opened before the first closed"
            open_message_id = event.message_id
        elif isinstance(event, ag_ui_core.TextMessageContentEvent):
            assert open_message_id is not None, "CONTENT arrived with no open START"
            assert event.message_id == open_message_id
            content_count += 1
        elif isinstance(event, ag_ui_core.TextMessageEndEvent):
            assert open_message_id is not None, "END arrived with no open START"
            assert event.message_id == open_message_id
            open_message_id = None

    assert content_count == 5
    assert open_message_id is None
    agui.check_conformance(events)


# --- B13: a textless run emits zero TEXT_MESSAGE_* events --------------------------------


def test_b13_no_text_deltas_emits_zero_text_events() -> None:
    fake = _FakeAgent(events=[{"result": AgentResult(text="")}])
    run_input = _run_input(messages=[_user_message("hi")])

    events = asyncio.run(_collect(fake, run_input))

    text_event_types = (
        ag_ui_core.TextMessageStartEvent,
        ag_ui_core.TextMessageContentEvent,
        ag_ui_core.TextMessageEndEvent,
    )
    assert not any(isinstance(event, text_event_types) for event in events)
    assert isinstance(events[0], ag_ui_core.RunStartedEvent)
    assert isinstance(events[-1], ag_ui_core.RunFinishedEvent)
    agui.check_conformance(events)


# --- B14: text still open at run end closes before the terminal event -------------------


def test_b14_open_text_closes_before_run_finished() -> None:
    fake = _FakeAgent(events=[{"data": "partial"}, {"result": AgentResult(text="partial")}])
    run_input = _run_input(messages=[_user_message("hi")])

    events = asyncio.run(_collect(fake, run_input))

    end_index = next(
        i for i, e in enumerate(events) if isinstance(e, ag_ui_core.TextMessageEndEvent)
    )
    finished_index = next(
        i for i, e in enumerate(events) if isinstance(e, ag_ui_core.RunFinishedEvent)
    )
    assert end_index < finished_index
    assert sum(1 for e in events if isinstance(e, ag_ui_core.TextMessageEndEvent)) == 1
    agui.check_conformance(events)


def test_b14_open_text_closes_before_run_error() -> None:
    fake = _FakeAgentThatFailsAfterText(fail=RuntimeError("boom"))
    run_input = _run_input(messages=[_user_message("hi")])

    events = asyncio.run(_collect(fake, run_input))

    end_index = next(
        i for i, e in enumerate(events) if isinstance(e, ag_ui_core.TextMessageEndEvent)
    )
    error_index = next(i for i, e in enumerate(events) if isinstance(e, ag_ui_core.RunErrorEvent))
    assert end_index < error_index
    assert sum(1 for e in events if isinstance(e, ag_ui_core.TextMessageEndEvent)) == 1
    agui.check_conformance(events)


# --- B50: the newest user message is what reaches Agent.stream_async --------------------


def test_b50_newest_user_message_reaches_agent_stream_async() -> None:
    fake = _FakeAgent(events=[{"result": AgentResult(text="ok")}])
    run_input = _run_input(
        messages=[
            _user_message("first turn", message_id="u0"),
            ag_ui_core.AssistantMessage(id="a0", content="first reply"),
            _user_message("what is the weather in Reykjavik?", message_id="u1"),
        ]
    )

    asyncio.run(_collect(fake, run_input))

    assert fake.recorded_prompts == ["what is the weather in Reykjavik?"]


# --- B51: no user message at all raises ValueError ---------------------------------------


def test_b51_no_user_message_raises_value_error() -> None:
    run_input = _run_input(
        messages=[
            ag_ui_core.SystemMessage(id="s0", content="be nice"),
            ag_ui_core.AssistantMessage(id="a0", content="hello"),
        ]
    )

    with pytest.raises(ValueError, match="no user message"):
        agui.latest_user_prompt(run_input)


def test_b51_list_content_with_non_text_part_is_rejected_not_silently_dropped() -> None:
    # Multimodal is a non-goal, but a non-goal is a promise about scope, not permission to
    # drop data. Joining only the text parts sends the model a question about an image it
    # never received, and that reads as a working answer.
    message = ag_ui_core.UserMessage(
        id="u1",
        content=[
            ag_ui_core.TextInputContent(text="what is in this picture?"),
            ag_ui_core.ImageInputContent(
                source=ag_ui_core.InputContentUrlSource(
                    value="https://example.invalid/a.png", mime_type="image/png"
                )
            ),
        ],
    )
    with pytest.raises(ValueError, match="only text content is supported"):
        agui.latest_user_prompt(_run_input(messages=[message]))


def test_b51_empty_list_content_is_rejected_not_turned_into_an_empty_prompt() -> None:
    # "".join([]) == "" — an empty prompt reaching the model is the exact failure this
    # slice exists to remove, so it must raise rather than return a falsy string.
    message = ag_ui_core.UserMessage(id="u1", content=[])
    with pytest.raises(ValueError, match="no text content"):
        agui.latest_user_prompt(_run_input(messages=[message]))


def test_b51_list_content_of_only_text_parts_is_joined() -> None:
    message = ag_ui_core.UserMessage(
        id="u1",
        content=[
            ag_ui_core.TextInputContent(text="hello "),
            ag_ui_core.TextInputContent(text="world"),
        ],
    )
    assert agui.latest_user_prompt(_run_input(messages=[message])) == "hello world"
