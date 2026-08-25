"""Translate one alloy `Agent` run into an AG-UI protocol event stream.

Requires the `agui` extra. Importing this module without it fails loudly, so alloy's
zero-dependency core never pulls in `ag-ui-protocol` unless a caller opts in.

`run_stream` and `check_conformance` are implemented; every other function below still
raises `NotImplementedError` until its slice lands — do not change a signature without
updating every later slice.

Ordering rules a produced stream must obey (enforced by `check_conformance`):

1. `RUN_STARTED` is the first event.
2. The last event is exactly one of `RUN_FINISHED` / `RUN_ERROR`.
3. `RUN_FINISHED` and `RUN_ERROR` never both appear — termination is exclusive.
4. Nothing follows the terminal event.
5. `TEXT_MESSAGE_CONTENT`/`TEXT_MESSAGE_END` require an open `TEXT_MESSAGE_START` with the
   same `message_id`; a second `START` for an already-open id is a violation.
6. `TOOL_CALL_ARGS`/`TOOL_CALL_END` require an open `TOOL_CALL_START` with the same
   `tool_call_id`.
7. `TOOL_CALL_RESULT` must follow that call's `TOOL_CALL_END`.
8. The single-open rule: a text message and a tool call are never open at the same time.

Rules 1-7 come from the AG-UI protocol's own ordering and correlation rules:
https://docs.ag-ui.com/concepts/events. Rule 8 does not — it comes from the reference
client's actual behaviour: `verifyEvents` in `@ag-ui/client` is strictly single-threaded
and rejects any other event while a tool call is open, so a stream that interleaves text
and tool calls looks legal under rules 1-7 alone and is still refused by a real frontend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ._agent import Agent

AGUI_EXTRA_HINT = "install the AG-UI extra: pip install 'alloy-foundry[agui]'"

try:
    import ag_ui.core as ag_ui_core
except ImportError:
    raise ImportError(AGUI_EXTRA_HINT) from None

__all__ = [
    "AGUI_EXTRA_HINT",
    "ThreadStore",
    "capabilities",
    "check_conformance",
    "json_safe",
    "parse_run_input",
    "run_stream",
    "seed_state",
]


def parse_run_input(raw: bytes) -> ag_ui_core.RunAgentInput:
    """Parse an HTTP request body into a `RunAgentInput`.

    Args:
        raw: The raw request body bytes.

    Returns:
        The parsed run input.
    """
    raise NotImplementedError


def seed_state(run_input: ag_ui_core.RunAgentInput) -> dict[str, Any]:
    """Derive a run's initial state from the client-supplied `RunAgentInput`.

    Args:
        run_input: The parsed run input.

    Returns:
        The seeded state dict.
    """
    raise NotImplementedError


def json_safe(value: Any) -> Any:
    """Coerce `value` into something `json.dumps` can encode without raising.

    Args:
        value: Any Python value that may appear in agent state.

    Returns:
        A JSON-encodable equivalent of `value`.
    """
    raise NotImplementedError


_TERMINAL_TYPES = frozenset((ag_ui_core.EventType.RUN_FINISHED, ag_ui_core.EventType.RUN_ERROR))


def check_conformance(events: Sequence[ag_ui_core.BaseEvent]) -> None:
    """Assert `events` obeys AG-UI ordering rules; raise on the first violation.

    See the module docstring for the eight rules this enforces.

    Args:
        events: The full ordered event sequence produced by one run.
    """
    if not events:
        raise AssertionError("rule 1 violated: RUN_STARTED must be the first event, got none")
    if events[0].type != ag_ui_core.EventType.RUN_STARTED:
        raise AssertionError(
            f"rule 1 violated: RUN_STARTED must be the first event, "
            f"got {events[0].type.value}"
        )

    terminal_type: ag_ui_core.EventType | None = None
    open_message_id: str | None = None
    open_tool_call_ids: set[str] = set()
    closed_tool_call_ids: set[str] = set()
    seen_state_snapshot = False

    for event in events:
        et = event.type

        if terminal_type is not None:
            if et in _TERMINAL_TYPES:
                raise AssertionError(
                    f"rule 3 violated: termination is exclusive, RUN_FINISHED and "
                    f"RUN_ERROR cannot both appear, got {et.value} after "
                    f"{terminal_type.value}"
                )
            raise AssertionError(
                f"rule 4 violated: nothing may follow the terminal event, got "
                f"{et.value} after {terminal_type.value}"
            )

        if isinstance(event, ag_ui_core.TextMessageStartEvent):
            if open_tool_call_ids:
                raise AssertionError(
                    f"rule 8 violated: a text message may not open while a tool call "
                    f"is open, got {et.value} while tool_call_id(s) "
                    f"{sorted(open_tool_call_ids)} open"
                )
            if open_message_id is not None:
                raise AssertionError(
                    f"rule 5 violated: TEXT_MESSAGE_START for "
                    f"message_id={event.message_id!r} while message_id="
                    f"{open_message_id!r} is already open, got {et.value}"
                )
            open_message_id = event.message_id
        elif isinstance(event, ag_ui_core.TextMessageContentEvent):
            if open_message_id != event.message_id:
                raise AssertionError(
                    f"rule 5 violated: TEXT_MESSAGE_CONTENT for "
                    f"message_id={event.message_id!r} requires an open "
                    f"TEXT_MESSAGE_START with that id, got {et.value}"
                )
        elif isinstance(event, ag_ui_core.TextMessageEndEvent):
            if open_message_id != event.message_id:
                raise AssertionError(
                    f"rule 5 violated: TEXT_MESSAGE_END for "
                    f"message_id={event.message_id!r} requires an open "
                    f"TEXT_MESSAGE_START with that id, got {et.value}"
                )
            open_message_id = None
        elif isinstance(event, ag_ui_core.ToolCallStartEvent):
            if open_message_id is not None:
                raise AssertionError(
                    f"rule 8 violated: a tool call may not open while a text message "
                    f"is open, got {et.value} while message_id={open_message_id!r} "
                    f"open"
                )
            open_tool_call_ids.add(event.tool_call_id)
        elif isinstance(event, ag_ui_core.ToolCallArgsEvent):
            if event.tool_call_id not in open_tool_call_ids:
                raise AssertionError(
                    f"rule 6 violated: TOOL_CALL_ARGS for "
                    f"tool_call_id={event.tool_call_id!r} requires an open "
                    f"TOOL_CALL_START with that id, got {et.value}"
                )
        elif isinstance(event, ag_ui_core.ToolCallEndEvent):
            if event.tool_call_id not in open_tool_call_ids:
                raise AssertionError(
                    f"rule 6 violated: TOOL_CALL_END for "
                    f"tool_call_id={event.tool_call_id!r} requires an open "
                    f"TOOL_CALL_START with that id, got {et.value}"
                )
            open_tool_call_ids.discard(event.tool_call_id)
            closed_tool_call_ids.add(event.tool_call_id)
        elif isinstance(event, ag_ui_core.ToolCallResultEvent):
            if event.tool_call_id not in closed_tool_call_ids:
                raise AssertionError(
                    f"rule 7 violated: TOOL_CALL_RESULT for "
                    f"tool_call_id={event.tool_call_id!r} must come after that call's "
                    f"TOOL_CALL_END, got {et.value}"
                )
        elif isinstance(event, ag_ui_core.StateSnapshotEvent):
            if seen_state_snapshot:
                raise AssertionError(
                    f"state rule violated: at most one STATE_SNAPSHOT is allowed per "
                    f"run, got a second {et.value}"
                )
            seen_state_snapshot = True
        elif isinstance(event, ag_ui_core.StateDeltaEvent) and not seen_state_snapshot:
            raise AssertionError(
                f"state rule violated: STATE_DELTA must be preceded by a "
                f"STATE_SNAPSHOT, got {et.value}"
            )

        if et in _TERMINAL_TYPES:
            if open_message_id is not None:
                raise AssertionError(
                    f"bracket rule violated: message_id={open_message_id!r} is still "
                    f"open at the terminal event, got {et.value}"
                )
            if open_tool_call_ids:
                raise AssertionError(
                    f"bracket rule violated: tool_call_id(s) "
                    f"{sorted(open_tool_call_ids)} still open at the terminal event, "
                    f"got {et.value}"
                )
            terminal_type = et

    if terminal_type is None:
        raise AssertionError(
            f"rule 2 violated: the last event must be RUN_FINISHED or RUN_ERROR, "
            f"got {events[-1].type.value}"
        )


def capabilities() -> ag_ui_core.AgentCapabilities:
    """Describe what this alloy AG-UI translator supports.

    Returns:
        The declared agent capabilities.
    """
    raise NotImplementedError


async def run_stream(
    agent: Agent, run_input: ag_ui_core.RunAgentInput
) -> AsyncIterator[ag_ui_core.BaseEvent]:
    """Run `agent` against `run_input` and yield the AG-UI event stream.

    Every run is bracketed: `RunStartedEvent` first, then exactly one of
    `RunFinishedEvent`/`RunErrorEvent` last. Nothing follows the closing event.

    Args:
        agent: An already-built alloy agent, scoped to the right conversation.
        run_input: The parsed run request.

    Yields:
        AG-UI events in wire order.
    """
    yield ag_ui_core.RunStartedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)
    try:
        # ponytail: message history -> prompt translation is a later slice's job (see
        # `seed_state`/`parse_run_input`, still NotImplementedError). This slice only
        # brackets the run, so it drives the agent without inspecting what it streams back.
        async for _event in agent.stream_async(""):
            pass
    except Exception as exc:
        yield ag_ui_core.RunErrorEvent(message=str(exc))
        return
    yield ag_ui_core.RunFinishedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)


class ThreadStore:
    """Maps an AG-UI `threadId` to the Foundry conversation id backing it."""

    def resolve(self, thread_id: str) -> str | None:
        """Return the conversation id remembered for `thread_id`, if any.

        Args:
            thread_id: The AG-UI thread id supplied by the client.

        Returns:
            The remembered conversation id, or None if `thread_id` is unseen.
        """
        raise NotImplementedError

    def remember(self, thread_id: str, conversation_id: str) -> None:
        """Record that `thread_id` maps to `conversation_id`.

        Args:
            thread_id: The AG-UI thread id supplied by the client.
            conversation_id: The Foundry conversation id to associate with it.
        """
        raise NotImplementedError


class _RunState:  # pyright: ignore[reportUnusedClass] - unused until a later slice wires it in
    """One run's bracketing/state-patch phase. Private; not part of the public surface."""

    phase: Literal["NOT_STARTED", "OPEN", "TEXT_OPEN", "FINISHED", "ERRORED"]

    def apply(self, op: str, path: str, value: Any = None) -> dict[str, Any]:
        """Apply one JSON Patch operation to this run's state, returning the delta.

        Args:
            op: The JSON Patch op (`add`, `replace`, or `remove`).
            path: The JSON Pointer path the op targets.
            value: The value to write, for `add`/`replace`. Unused for `remove`.

        Returns:
            The JSON Patch operation as emitted on the wire.
        """
        raise NotImplementedError
