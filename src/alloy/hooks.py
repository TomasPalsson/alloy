"""Lifecycle hooks: user-registered callbacks fired around agent invocations and tool calls.

Event dataclasses live here, not in contracts.py. contracts.py keeps the cross-cutting
vocabulary (errors, Message, ToolCall, ToolResult); this module owns the hook vocabulary,
so the two do not drift apart on a shared type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from .contracts import AgentResult, ToolCall, ToolResult

if TYPE_CHECKING:
    from ._agent import Agent

TEvent = TypeVar("TEvent")


@dataclass
class BeforeInvocationEvent:
    """Fires once per `__call__`/`invoke_async`/`stream_async`, before any backend call.

    "Any" is literal: before the version lookup, before the conversation is created, and
    before the model is called — so a hook here genuinely precedes all Azure traffic.
    Writing to `prompt` does NOT change what is sent; only the tool events are mutable in
    a way the loop honours.
    """

    agent: Agent
    prompt: str


@dataclass
class AfterInvocationEvent:
    """Fires once per call, after the final text is known."""

    agent: Agent
    result: AgentResult


@dataclass
class BeforeToolCallEvent:
    """Fires once per tool call, before the tool runs.

    A callback may change what happens next, by assigning to this event:

    * `cancel_tool = "<reason>"` blocks the call without running the tool. The reason
      reaches the model as `{"cancelled": "<reason>"}`, and the resulting `ToolResult`
      has `failure is None` — a guardrail firing correctly is a decision, not an error,
      and never appears in `AgentResult.tool_failures`. Cancellation outranks an unknown
      tool name: blocking `delete_prod` blocks it whether or not that tool exists.
    * `tool_use = replace(event.tool_use, arguments=...)` rewrites what the tool receives.
      `ToolCall` is frozen, so build a copy with `dataclasses.replace` and assign it back;
      the loop re-reads this field after every callback has run.

    With several callbacks registered, they share one event object and run in
    registration order, so the last writer wins and each sees the previous one's value.
    """

    agent: Agent
    tool_use: ToolCall
    cancel_tool: str | None = None


@dataclass
class AfterToolCallEvent:
    """Fires once per tool call, after its `ToolResult` exists.

    Fires on every outcome — success, a tool that raised, an unknown tool, malformed or
    missing arguments, and a call a hook cancelled. There is no path that fires
    `BeforeToolCallEvent` without this one following it.

    Assigning `result = ToolResult(...)` replaces what is submitted back to the model.
    Callbacks run in REVERSE registration order (LIFO), so the FIRST-registered hook has
    the last word on the result.
    """

    agent: Agent
    tool_use: ToolCall
    result: ToolResult


# The only two events dispatched in reverse (LIFO) order; see HookRegistry.emit.
_REVERSE_DISPATCH_EVENTS = (AfterInvocationEvent, AfterToolCallEvent)


class HookProvider(ABC):
    """Base class for a bundle of related hook callbacks a user supplies to an agent."""

    @abstractmethod
    def register_hooks(self, registry: HookRegistry) -> None:
        """Add this provider's callbacks to `registry`.

        Args:
            registry: The registry to register callbacks on.
        """


class HookRegistry:
    """Holds callbacks per event type and dispatches events to them."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._callbacks: dict[type[Any], list[Callable[[Any], None]]] = {}

    def add_callback(self, event_type: type[TEvent], callback: Callable[[TEvent], None]) -> None:
        """Register one callback for one event type.

        Args:
            event_type: The event class to listen for.
            callback: Called with the event instance when it fires.
        """
        self._callbacks.setdefault(event_type, []).append(callback)

    def add_hook(self, provider: HookProvider) -> None:
        """Let `provider` register its callbacks on this registry.

        Args:
            provider: The hook provider to register.
        """
        provider.register_hooks(self)

    def emit(self, event: TEvent) -> TEvent:
        """Dispatch `event` to every callback registered for its type, and return it.

        Before-events fire in registration order; After-events fire in reverse
        registration order (LIFO), matching Strands. A raising callback propagates
        immediately, unchanged, and stops any remaining callbacks for this event.

        Args:
            event: The event instance to dispatch. Its type determines which
                callbacks run.
        """
        callbacks = self._callbacks.get(type(event), [])
        if isinstance(event, _REVERSE_DISPATCH_EVENTS):
            callbacks = list(reversed(callbacks))
        for callback in callbacks:
            callback(event)
        return event
