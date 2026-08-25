"""Slice 2: HookRegistry dispatch order, mutation semantics, and propagation."""

from __future__ import annotations

from typing import Any

import pytest

from alloy.contracts import AgentResult, ToolCall, ToolResult
from alloy.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

# Registry-level tests need no real Agent; the field is never read here.
_AGENT: Any = None


def test_registered_callback_receives_the_event() -> None:
    registry = HookRegistry()
    received: list[BeforeToolCallEvent] = []
    registry.add_callback(BeforeToolCallEvent, received.append)

    call = ToolCall(call_id="1", name="get_weather", arguments="{}")
    registry.emit(BeforeToolCallEvent(agent=_AGENT, tool_use=call))

    assert len(received) == 1
    assert received[0].tool_use.name == "get_weather"


def test_before_callbacks_run_in_registration_order() -> None:
    registry = HookRegistry()
    order: list[str] = []
    registry.add_callback(BeforeInvocationEvent, lambda e: order.append("first"))
    registry.add_callback(BeforeInvocationEvent, lambda e: order.append("second"))

    registry.emit(BeforeInvocationEvent(agent=_AGENT, prompt="hi"))

    assert order == ["first", "second"]


def test_after_callbacks_run_in_reverse_registration_order() -> None:
    registry = HookRegistry()
    order: list[str] = []
    registry.add_callback(AfterInvocationEvent, lambda e: order.append("first"))
    registry.add_callback(AfterInvocationEvent, lambda e: order.append("second"))

    registry.emit(AfterInvocationEvent(agent=_AGENT, result=AgentResult(text="done")))

    assert order == ["second", "first"]


def test_add_hook_registers_everything_the_provider_adds() -> None:
    order: list[str] = []

    class _LoggingProvider(HookProvider):
        def register_hooks(self, registry: HookRegistry) -> None:
            registry.add_callback(BeforeInvocationEvent, lambda e: order.append("before"))
            registry.add_callback(AfterInvocationEvent, lambda e: order.append("after"))

    registry = HookRegistry()
    registry.add_hook(_LoggingProvider())

    registry.emit(BeforeInvocationEvent(agent=_AGENT, prompt="hi"))
    registry.emit(AfterInvocationEvent(agent=_AGENT, result=AgentResult(text="done")))

    assert order == ["before", "after"]


def test_emit_returns_the_event_with_mutations_visible() -> None:
    registry = HookRegistry()

    def _cancel(event: BeforeToolCallEvent) -> None:
        event.cancel_tool = "blocked by policy"

    registry.add_callback(BeforeToolCallEvent, _cancel)

    call = ToolCall(call_id="1", name="delete_prod", arguments="{}")
    returned = registry.emit(BeforeToolCallEvent(agent=_AGENT, tool_use=call))

    assert returned.cancel_tool == "blocked by policy"


def test_last_writer_wins_and_each_callback_sees_the_previous_value() -> None:
    registry = HookRegistry()
    seen: list[str | None] = []

    def _first(event: BeforeToolCallEvent) -> None:
        seen.append(event.cancel_tool)
        event.cancel_tool = "first"

    def _second(event: BeforeToolCallEvent) -> None:
        seen.append(event.cancel_tool)
        event.cancel_tool = "second"

    registry.add_callback(BeforeToolCallEvent, _first)
    registry.add_callback(BeforeToolCallEvent, _second)

    call = ToolCall(call_id="1", name="delete_prod", arguments="{}")
    returned = registry.emit(BeforeToolCallEvent(agent=_AGENT, tool_use=call))

    assert seen == [None, "first"]
    assert returned.cancel_tool == "second"


def test_raising_callback_propagates_and_stops_remaining_callbacks() -> None:
    registry = HookRegistry()
    ran: list[str] = []

    def _boom(event: BeforeInvocationEvent) -> None:
        raise ValueError("nope")

    def _never(event: BeforeInvocationEvent) -> None:
        ran.append("never")

    registry.add_callback(BeforeInvocationEvent, _boom)
    registry.add_callback(BeforeInvocationEvent, _never)

    with pytest.raises(ValueError, match="nope"):
        registry.emit(BeforeInvocationEvent(agent=_AGENT, prompt="hi"))

    assert ran == []


def test_emit_with_no_registered_callbacks_is_harmless() -> None:
    registry = HookRegistry()
    call = ToolCall(call_id="1", name="get_weather", arguments="{}")
    result = ToolResult(call_id="1", output='{"temp": "72F"}')
    event = AfterToolCallEvent(agent=_AGENT, tool_use=call, result=result)

    returned = registry.emit(event)

    assert returned is event
