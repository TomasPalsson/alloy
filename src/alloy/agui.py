"""Translate one alloy `Agent` run into an AG-UI protocol event stream.

Requires the `agui` extra. Importing this module without it fails loudly, so alloy's
zero-dependency core never pulls in `ag-ui-protocol` unless a caller opts in.

What this module does NOT do, deliberately: human-in-the-loop (`Interrupt`/`ResumeEntry`),
reasoning or thinking events, multi-agent, and multimodal input. `capabilities()` declares
none of them — a client that trusts an overstated declaration renders an approval button
that hangs forever, or an empty reasoning pane, and the failure looks like the frontend's
bug rather than a false claim here.

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

import json
import re
from collections import OrderedDict
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from . import contracts
from .hooks import AfterToolCallEvent

if TYPE_CHECKING:
    from ._agent import Agent

AGUI_EXTRA_HINT = "install the AG-UI extra: pip install 'alloy-foundry[agui]'"

try:
    import ag_ui.core as ag_ui_core
    from ag_ui.core.capabilities import StateCapabilities, ToolsCapabilities
    from pydantic import ValidationError as PydanticValidationError
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
    "redact_secrets",
    "run_stream",
    "seed_state",
]


def parse_run_input(raw: bytes) -> ag_ui_core.RunAgentInput:
    """Parse an HTTP request body into a `RunAgentInput`.

    Args:
        raw: The raw request body bytes.

    Returns:
        The parsed run input.

    Raises:
        json.JSONDecodeError: `raw` is not JSON at all.
        ValueError: `raw` is JSON but not a valid `RunAgentInput`.
    """
    # Two distinct failures, deliberately not collapsed: a caller needs to tell "your JSON
    # is broken" (400) from "your JSON is fine, this field is wrong" (422), and only the
    # second is worth retrying with a corrected field.
    parsed: Any = json.loads(raw)
    try:
        return ag_ui_core.RunAgentInput.model_validate(parsed)
    except PydanticValidationError as exc:
        raise ValueError(str(exc)) from exc


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


# Deep enough for any state a frontend actually renders, shallow enough that a hostile
# body cannot exhaust the stack. A ~6KB request nesting 3000 levels used to raise
# RecursionError out of the generator before the run could emit a terminal event.
_MAX_STATE_DEPTH = 64


def json_safe(value: Any, _depth: int = 0) -> Any:
    """Coerce `value` into something `json.dumps` can encode without raising.

    Args:
        value: Any Python value that may appear in agent state.
        _depth: Recursion depth, bounded at `_MAX_STATE_DEPTH`. Internal.

    Returns:
        A JSON-encodable equivalent of `value`. Anything nested past the depth limit is
        replaced with a marker string rather than recursed into.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if _depth >= _MAX_STATE_DEPTH:
        # Truncated, not raised: this is client-supplied state being rendered, and a run
        # that dies here would break the mandatory RUN_STARTED/RUN_FINISHED bracketing.
        return f"<truncated at depth {_MAX_STATE_DEPTH}>"
    if isinstance(value, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in cast("dict[Any, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [json_safe(v, _depth + 1) for v in cast("list[Any]", value)]
    # Stringified rather than raising: state is rendered, not executed, and a run that
    # dies at encode time is worse than one that shows a repr.
    return str(value)


_SECRET_PATTERNS = (
    # JWTs (Azure AD tokens), storage account keys, and OpenAI-style keys. Matched on
    # shape rather than on a list of known field names, because the exception strings
    # these arrive in are written by upstream SDKs and change without notice.
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)\b(?:AccountKey|SharedAccessSignature|api[-_]?key|password)\s*=\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
)


def redact_secrets(message: str) -> str:
    """Strip credential-shaped substrings from text bound for a browser.

    `RUN_ERROR` and `toolFailures` are read by whoever opened the page, and the strings
    they carry come from upstream SDK exceptions — `azure-ai-projects`, `openai` — which
    are free to include a token, a connection string, or an account key. Redacting at the
    boundary is the only place that covers every source at once.

    Args:
        message: The raw exception text.

    Returns:
        The same text with credential-shaped runs replaced by `[redacted]`.
    """
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[redacted]", message)
    return message


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
    # Only what is actually built. A declaration that overstates is worse than none: a
    # client that trusts it renders affordances this server cannot honour — an approval
    # button that hangs forever, an empty reasoning pane — and the failure looks like the
    # frontend's bug rather than a false claim here.
    return ag_ui_core.AgentCapabilities(
        state=StateCapabilities(
            snapshots=True,
            deltas=True,
            # Conversation history lives in the backend, not in this process; the thread
            # map holds two strings and is lost on restart.
            memory=False,
            persistent_state=False,
        ),
        tools=ToolsCapabilities(),
    )


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
                    "message": redact_secrets(str(result.failure)),
                },
            )
        )
    return ag_ui_core.StateDeltaEvent(delta=ops)


async def run_stream(
    agent: Agent, run_input: ag_ui_core.RunAgentInput
) -> AsyncGenerator[ag_ui_core.BaseEvent, None]:
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

    Declared as an `AsyncGenerator`, not an `AsyncIterator`, so the type itself tells a
    caller `aclose()` exists. A consumer that abandons this stream — a disconnecting
    client — should close it, and one that cannot see the method will not.
    """
    yield ag_ui_core.RunStartedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)
    # Built defensively: seeding reads client-supplied state, so a hostile body must not
    # be able to raise between RUN_STARTED and the terminal event and leave the run
    # unbracketed. A failure here still produces a legal stream.
    try:
        run_state = _RunState(seed_state(run_input))
        snapshot = json_safe(run_state.state)
    except Exception as exc:
        yield ag_ui_core.RunErrorEvent(message=redact_secrets(str(exc)))
        return
    run_state.phase = "OPEN"
    yield ag_ui_core.StateSnapshotEvent(snapshot=snapshot)
    message_id: str | None = None
    # Populated by _record_result, which fires on agent.hooks - possibly nested inside
    # agent.stream_async's own frame, never inside this generator's own body, so it can't
    # yield. Drained (and cleared) at every point below that could otherwise let a result
    # go unreported: before handling each new stream event, after the loop, and on error.
    pending_results: list[contracts.ToolResult] = []
    # Tool calls whose TOOL_CALL_START already went out, so `current_tool_use` knows to
    # only close them rather than re-emit the whole bracket.
    opened_tool_call_ids: set[str] = set()
    # Still-open brackets. Rule 8 is symmetric: text must close a tool call just as a tool
    # call closes text. Enforcing only one direction emitted streams this module's own
    # check_conformance rejects.
    open_tool_call_ids: set[str] = set()

    def _record_result(event: AfterToolCallEvent) -> None:
        pending_results.append(event.result)

    def _drain() -> list[contracts.ToolResult]:
        drained = list(pending_results)
        pending_results.clear()
        return drained

    agent.hooks.add_callback(AfterToolCallEvent, _record_result)
    # Held so it can be closed explicitly below. `async for` does NOT close what it
    # iterates, so a consumer abandoning THIS generator would otherwise leave
    # stream_async's own `finally` unrun and its worker thread spinning for the life of
    # the process.
    inner = agent.stream_async(latest_user_prompt(run_input))
    try:
        async for event in inner:
            for result in _drain():
                yield _tool_result_event(result)
                yield _completion_delta(run_state, result)

            if "tool_call_started" in event:
                started = cast(contracts.ToolCall, event["tool_call_started"])
                if message_id is not None:
                    yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
                    message_id = None
                opened_tool_call_ids.add(started.call_id)
                open_tool_call_ids.add(started.call_id)
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
                    if call.call_id in open_tool_call_ids:
                        open_tool_call_ids.discard(call.call_id)
                        yield ag_ui_core.ToolCallEndEvent(tool_call_id=call.call_id)
                    # Already closed early to let text through — closing again would be a
                    # rule 6 violation instead.
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
            # Close any open tool call FIRST. The reference client rejects every event
            # while a tool call is open, so text arriving mid-call must end the bracket
            # rather than open a second one alongside it.
            for open_call_id in sorted(open_tool_call_ids):
                yield ag_ui_core.ToolCallEndEvent(tool_call_id=open_call_id)
            open_tool_call_ids.clear()
            if message_id is None:
                message_id = uuid4().hex
                yield ag_ui_core.TextMessageStartEvent(message_id=message_id)
            yield ag_ui_core.TextMessageContentEvent(message_id=message_id, delta=event["data"])
    except Exception as exc:
        for open_call_id in sorted(open_tool_call_ids):
            yield ag_ui_core.ToolCallEndEvent(tool_call_id=open_call_id)
        open_tool_call_ids.clear()
        for result in _drain():
            yield _tool_result_event(result)
            yield _completion_delta(run_state, result)
        if message_id is not None:
            yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
        yield ag_ui_core.RunErrorEvent(message=redact_secrets(str(exc)))
        return
    finally:
        agent.hooks.remove_callback(AfterToolCallEvent, _record_result)
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            await aclose()
    for open_call_id in sorted(open_tool_call_ids):
        yield ag_ui_core.ToolCallEndEvent(tool_call_id=open_call_id)
    open_tool_call_ids.clear()
    for result in _drain():
        yield _tool_result_event(result)
        yield _completion_delta(run_state, result)
    if message_id is not None:
        yield ag_ui_core.TextMessageEndEvent(message_id=message_id)
    yield ag_ui_core.RunFinishedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)


class ThreadStore:
    """Maps an AG-UI `threadId` to the Foundry conversation id backing it.

    An AG-UI thread and a Foundry conversation are the same concept — server-held history
    addressed by a client-supplied id — so this holds two strings per thread and never a
    transcript. The conversation itself lives in Azure, under Azure's retention.

    ponytail: process-local, bounded, lost on restart. Correct for one long-lived server;
    on any multi-instance or scale-to-zero host a caller's second run can land on an
    instance that never saw the first, and the agent silently forgets. Swap in a shared
    store (Redis, or a conversation id echoed back to the client) — three methods.
    """

    def __init__(self, max_threads: int = 100) -> None:
        """Create an empty store bounded at `max_threads` entries.

        Args:
            max_threads: How many threads to remember before evicting the least recently
                used. 100 is a bound, not a measurement — see code-design.md section 8.
        """
        self._max_threads = max_threads
        # Insertion-ordered and moved-to-end on every touch, so the first key is always
        # the least recently used.
        self._conversation_id_by_thread_id: OrderedDict[str, str] = OrderedDict()

    def __len__(self) -> int:
        """Return how many threads are currently remembered."""
        return len(self._conversation_id_by_thread_id)

    def resolve(self, thread_id: str) -> str | None:
        """Return the conversation id remembered for `thread_id`, if any.

        Args:
            thread_id: The AG-UI thread id supplied by the client.

        Returns:
            The remembered conversation id, or None if `thread_id` is unseen.

        Raises:
            ValueError: `thread_id` is blank.
        """
        self._reject_blank(thread_id)
        conversation_id = self._conversation_id_by_thread_id.get(thread_id)
        if conversation_id is not None:
            self._conversation_id_by_thread_id.move_to_end(thread_id)
        return conversation_id

    def remember(self, thread_id: str, conversation_id: str) -> None:
        """Record that `thread_id` maps to `conversation_id`, evicting if over the cap.

        Args:
            thread_id: The AG-UI thread id supplied by the client.
            conversation_id: The Foundry conversation id to associate with it.

        Raises:
            ValueError: `thread_id` is blank.
        """
        self._reject_blank(thread_id)
        self._conversation_id_by_thread_id[thread_id] = conversation_id
        self._conversation_id_by_thread_id.move_to_end(thread_id)
        while len(self._conversation_id_by_thread_id) > self._max_threads:
            # Silent: a caller whose thread aged out is indistinguishable from a
            # first-time caller, and both work.
            self._conversation_id_by_thread_id.popitem(last=False)

    def forget(self, thread_id: str) -> None:
        """Drop `thread_id`'s mapping; silent when it is not held.

        Called when the backend rejects a remembered conversation id, so the next run
        mints a fresh one and completes normally rather than failing (AC-40).

        Args:
            thread_id: The AG-UI thread id to forget.
        """
        self._conversation_id_by_thread_id.pop(thread_id, None)

    @staticmethod
    def _reject_blank(thread_id: str) -> None:
        """Refuse a blank thread id rather than letting every such caller share a thread.

        Args:
            thread_id: The id to check.

        Raises:
            ValueError: `thread_id` is empty or whitespace only.
        """
        if not thread_id.strip():
            raise ValueError("thread id must not be blank")


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
