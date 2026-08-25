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
9. `STATE_DELTA` must be preceded by a `STATE_SNAPSHOT`.
10. At most one `STATE_SNAPSHOT` per run.
11. No text message or tool call may still be open at the terminal event.

Rules 1-7 and 9-11 come from the AG-UI protocol's own ordering and correlation rules:
https://docs.ag-ui.com/concepts/events. Rule 8 does not — it comes from the reference
client's actual behaviour: `verifyEvents` in `@ag-ui/client` is strictly single-threaded
and rejects any other event while a tool call is open, so a stream that interleaves text
and tool calls looks legal under rules 1-7 alone and is still refused by a real frontend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from . import contracts
from .hooks import AfterToolCallEvent

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
    "latest_user_prompt",
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
    # AG-UI state is bidirectional by design — a frontend writes state and expects it back
    # — so a client's own keys pass through untouched. What does NOT pass through are the
    # three collections alloy maintains: letting a client pre-populate those would let it
    # fake tool activity in the transcript a frontend renders. A non-dict `state` has no
    # keys to keep and is replaced rather than rejected.
    raw_state: Any = run_input.state
    seeded: dict[str, Any] = (
        {str(key): value for key, value in cast("dict[Any, Any]", raw_state).items()}
        if isinstance(raw_state, dict)
        else {}
    )
    if "messages" not in seeded:
        seeded["messages"] = [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content if isinstance(message.content, str) else "",
            }
            for message in run_input.messages
        ]
    seeded["activeToolCalls"] = {}
    seeded["completedToolCalls"] = []
    seeded["toolFailures"] = []
    return seeded


def json_safe(value: Any) -> Any:
    """Coerce `value` into something `json.dumps` can encode without raising.

    Args:
        value: Any Python value that may appear in agent state.

    Returns:
        A JSON-encodable equivalent of `value`.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in cast("dict[Any, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in cast("list[Any]", value)]
    # Stringified rather than raising: state is rendered, not executed, and a run that
    # dies at encode time is worse than one that shows a repr.
    return str(value)


_TERMINAL_TYPES = frozenset((ag_ui_core.EventType.RUN_FINISHED, ag_ui_core.EventType.RUN_ERROR))


def check_conformance(events: Sequence[ag_ui_core.BaseEvent]) -> None:
    """Assert `events` obeys AG-UI ordering rules; raise on the first violation.

    See the module docstring for the eleven rules this enforces.

    Args:
        events: The full ordered event sequence produced by one run.
    """
    if not events:
        raise AssertionError("rule 1 violated: RUN_STARTED must be the first event, got none")
    if events[0].type != ag_ui_core.EventType.RUN_STARTED:
        raise AssertionError(
            f"rule 1 violated: RUN_STARTED must be the first event, got {events[0].type.value}"
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
                    f"rule 10 violated: at most one STATE_SNAPSHOT is allowed per run, "
                    f"got a second {et.value}"
                )
            seen_state_snapshot = True
        elif isinstance(event, ag_ui_core.StateDeltaEvent) and not seen_state_snapshot:
            raise AssertionError(
                f"rule 9 violated: STATE_DELTA must be preceded by a STATE_SNAPSHOT, got {et.value}"
            )

        if et in _TERMINAL_TYPES:
            if open_message_id is not None:
                raise AssertionError(
                    f"rule 11 violated: TEXT_MESSAGE_START for "
                    f"message_id={open_message_id!r} was never closed with "
                    f"TEXT_MESSAGE_END before the terminal event {et.value}"
                )
            if open_tool_call_ids:
                raise AssertionError(
                    f"rule 11 violated: TOOL_CALL_START for tool_call_id(s) "
                    f"{sorted(open_tool_call_ids)} was never closed with "
                    f"TOOL_CALL_END before the terminal event {et.value}"
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


def latest_user_prompt(run_input: ag_ui_core.RunAgentInput) -> str:
    """Return the newest `role: "user"` message's text from `run_input`.

    Args:
        run_input: The parsed run input.

    Returns:
        The newest user message's text.

    Raises:
        ValueError: `run_input.messages` holds no user message, or the newest one carries
            no text this build can read.
    """
    for message in reversed(run_input.messages):
        if isinstance(message, ag_ui_core.UserMessage):
            content = message.content
            if isinstance(content, str):
                return content
            # Multimodal is a non-goal, but silently keeping only the text parts would ask
            # the model about an image it never received — a wrong answer that reads like a
            # working one. Reject the message and name what it carried.
            unsupported = sorted(
                {p.type for p in content if not isinstance(p, ag_ui_core.TextInputContent)}
            )
            if unsupported:
                raise ValueError(
                    f"only text content is supported; this message carries {', '.join(unsupported)}"
                )
            text = "".join(p.text for p in content if isinstance(p, ag_ui_core.TextInputContent))
            if not text:
                raise ValueError("no text content in the newest user message")
            return text
    raise ValueError("no user message in run_input.messages")


def _tool_result_event(result: contracts.ToolResult) -> ag_ui_core.ToolCallResultEvent:
    """Build one TOOL_CALL_RESULT, minting a fresh message_id (AC-09).

    `result.call_id` is the tool_call_id to report: every ToolResult _loop.py builds
    (success, failure, or cancellation) carries the originating call's id, so there is no
    separate id to thread through here.
    """
    return ag_ui_core.ToolCallResultEvent(
        message_id=uuid4().hex, tool_call_id=result.call_id, content=result.output
    )


def _completion_delta(
    run_state: _RunState, result: contracts.ToolResult
) -> ag_ui_core.StateDeltaEvent:
    """Move one tool call from active to completed as ONE delta event.

    Both ops travel together deliberately: split across two events, a frontend applying
    them in order renders a frame where the call sits in neither collection and its card
    blinks out. Every test that only replays to the FINAL state passes either way, which
    is why this is a rule and not a preference.
    """
    # Read the name off the active entry before removing it: a ToolResult carries only the
    # call id, and a frontend rendering "restart_service failed" needs the name.
    active = cast("dict[str, Any]", run_state.state["activeToolCalls"])
    name = cast("dict[str, Any]", active.get(result.call_id, {})).get("name", "")
    ops = [
        run_state.apply("remove", f"/activeToolCalls/{result.call_id}"),
        run_state.apply(
            "add",
            "/completedToolCalls/-",
            {
                "toolCallId": result.call_id,
                "name": name,
                "output": result.output,
                "failed": result.failure is not None,
            },
        ),
    ]
    if result.failure is not None:
        ops.append(
            run_state.apply(
                "add",
                "/toolFailures/-",
                {
                    "toolCallId": result.call_id,
                    "error": type(result.failure).__name__,
                    "message": str(result.failure),
                },
            )
        )
    return ag_ui_core.StateDeltaEvent(delta=ops)


async def run_stream(
    agent: Agent, run_input: ag_ui_core.RunAgentInput
) -> AsyncIterator[ag_ui_core.BaseEvent]:
    """Run `agent` against `run_input` and yield the AG-UI event stream.

    Every run is bracketed: `RunStartedEvent` first, then exactly one of
    `RunFinishedEvent`/`RunErrorEvent` last. Nothing follows the closing event.

    Tool calls bracket as TOOL_CALL_START/ARGS/END sharing the alloy `ToolCall.call_id`
    verbatim; an open text message closes first (rule 8). Results aren't on alloy's own
    stream (FR-06), so this registers on `agent.hooks` for the run and removes it in a
    `finally` - two calls on one agent must not double-emit a stale run's results.

    Args:
        agent: An already-built alloy agent, scoped to the right conversation.
        run_input: The parsed run request.

    Yields:
        AG-UI events in wire order.
    """
    yield ag_ui_core.RunStartedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)
    run_state = _RunState(seed_state(run_input))
    run_state.phase = "OPEN"
    yield ag_ui_core.StateSnapshotEvent(snapshot=json_safe(run_state.state))
    message_id: str | None = None
    # Populated by _record_result, which fires on agent.hooks - possibly nested inside
    # agent.stream_async's own frame, never inside this generator's own body, so it can't
    # yield. Drained (and cleared) at every point below that could otherwise let a result
    # go unreported: before handling each new stream event, after the loop, and on error.
    pending_results: list[contracts.ToolResult] = []
    # Tool calls whose TOOL_CALL_START already went out, so `current_tool_use` knows to
    # only close them rather than re-emit the whole bracket.
    opened_tool_call_ids: set[str] = set()

    def _record_result(event: AfterToolCallEvent) -> None:
        pending_results.append(event.result)

    def _drain() -> list[contracts.ToolResult]:
        drained = list(pending_results)
        pending_results.clear()
        return drained

    agent.hooks.add_callback(AfterToolCallEvent, _record_result)
    try:
        prompt = latest_user_prompt(run_input)
        async for event in agent.stream_async(prompt):
            for result in _drain():
                yield _tool_result_event(result)
                yield _completion_delta(run_state, result)

            if "tool_call_started" in event:
                started = cast(contracts.ToolCall, event["tool_call_started"])
                if message_id is not None:
                    yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
                    message_id = None
                opened_tool_call_ids.add(started.call_id)
                yield ag_ui_core.ToolCallStartEvent(
                    tool_call_id=started.call_id, tool_call_name=started.name
                )
                yield ag_ui_core.StateDeltaEvent(
                    delta=[
                        run_state.apply(
                            "add",
                            f"/activeToolCalls/{started.call_id}",
                            {"name": started.name, "arguments": ""},
                        )
                    ]
                )
                continue

            if "tool_arguments_delta" in event:
                streamed_call_id, fragment = cast("tuple[str, str]", event["tool_arguments_delta"])
                yield ag_ui_core.ToolCallArgsEvent(tool_call_id=streamed_call_id, delta=fragment)
                continue

            if "current_tool_use" in event:
                call = cast(contracts.ToolCall, event["current_tool_use"])
                if call.call_id in opened_tool_call_ids:
                    # Bracket already open and its arguments already streamed; this event
                    # only closes it. Re-emitting ARGS would repeat the whole payload after
                    # the fragments that already carried it.
                    yield ag_ui_core.ToolCallEndEvent(tool_call_id=call.call_id)
                    continue
                # No start event arrived, so the call was delivered whole rather than
                # streamed. Emit the complete bracket from this one event.
                if message_id is not None:
                    yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
                    message_id = None
                yield ag_ui_core.ToolCallStartEvent(
                    tool_call_id=call.call_id, tool_call_name=call.name
                )
                yield ag_ui_core.ToolCallArgsEvent(tool_call_id=call.call_id, delta=call.arguments)
                yield ag_ui_core.ToolCallEndEvent(tool_call_id=call.call_id)
                opened_tool_call_ids.add(call.call_id)
                yield ag_ui_core.StateDeltaEvent(
                    delta=[
                        run_state.apply(
                            "add",
                            f"/activeToolCalls/{call.call_id}",
                            {"name": call.name, "arguments": call.arguments},
                        )
                    ]
                )
                continue

            if "data" not in event:
                continue
            if message_id is None:
                message_id = uuid4().hex
                yield ag_ui_core.TextMessageStartEvent(message_id=message_id)
            yield ag_ui_core.TextMessageContentEvent(message_id=message_id, delta=event["data"])
    except Exception as exc:
        for result in _drain():
            yield _tool_result_event(result)
            yield _completion_delta(run_state, result)
        if message_id is not None:
            yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
        yield ag_ui_core.RunErrorEvent(message=str(exc))
        return
    finally:
        agent.hooks.remove_callback(AfterToolCallEvent, _record_result)
    for result in _drain():
        yield _tool_result_event(result)
        yield _completion_delta(run_state, result)
    if message_id is not None:
        yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
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


def _step_into(node: Any, token: str) -> Any:
    """Follow one JSON Pointer token into `node`, list index or dict key.

    A declared `Any` return keeps the walk honest: the shape being walked is genuinely
    unknown, and pretending otherwise is what makes strict mode complain.
    """
    if isinstance(node, list):
        return cast("list[Any]", node)[int(token)]
    return cast("dict[str, Any]", node)[token]


class _RunState:
    """One run's frontend-visible state, and the phase its brackets are in.

    Private. Every mutation goes through `apply`, which changes the state and returns the
    patch describing that change in one step — nothing diffs two dictionaries. A write
    that bypasses `apply` is silently missing from the stream, which is the whole cost of
    this approach and the reason it is the only door.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        """Start a run at `NOT_STARTED` over `state`.

        Args:
            state: The seeded state this run mutates.
        """
        self.state = state
        # Assigned, not merely annotated: an annotation alone leaves the attribute absent
        # and the first read raises AttributeError.
        self.phase: Literal["NOT_STARTED", "OPEN", "TEXT_OPEN", "FINISHED", "ERRORED"] = (
            "NOT_STARTED"
        )

    def apply(self, op: str, path: str, value: Any = None) -> dict[str, Any]:
        """Apply one JSON Patch operation to this run's state, returning the delta.

        Args:
            op: The JSON Patch op (`add`, `replace`, or `remove`).
            path: The JSON Pointer path the op targets.
            value: The value to write, for `add`/`replace`. Unused for `remove`.

        Returns:
            The JSON Patch operation as emitted on the wire.
        """
        tokens = [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]
        target: Any = self.state
        for token in tokens[:-1]:
            target = _step_into(target, token)
        last = tokens[-1]

        if op == "remove":
            # A list index must SPLICE. `del` is right for a dict key, but using it on a
            # list index would leave a hole and the client's replay would diverge.
            if isinstance(target, list):
                cast("list[Any]", target).pop(int(last))
            else:
                del cast("dict[str, Any]", target)[last]
            return {"op": op, "path": path}

        if isinstance(target, list):
            items = cast("list[Any]", target)
            if last == "-":
                items.append(value)
            elif op == "add":
                items.insert(int(last), value)
            else:
                items[int(last)] = value
        else:
            cast("dict[str, Any]", target)[last] = value
        return {"op": op, "path": path, "value": value}
