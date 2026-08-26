"""Security regressions found by an adversarial scan and reproduced before fixing.

Both were live-reproducible against the real code, not theoretical.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import ag_ui.core as ag_ui_core
import pytest

from alloy import agui
from alloy.hooks import HookRegistry


class _FakeAgent:
    def __init__(self, fail: Exception | None = None) -> None:
        self.hooks = HookRegistry()
        self.conversation_id: str | None = None
        self._fail = fail

    async def stream_async(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        if self._fail is not None:
            raise self._fail
        yield {"data": "ok"}


def _run_input(state: Any = None) -> ag_ui_core.RunAgentInput:
    return ag_ui_core.RunAgentInput(
        thread_id="t",
        run_id="r",
        state=state,
        messages=[ag_ui_core.UserMessage(id="u", content="hi")],
        tools=[],
        context=[],
        forwarded_props=None,
    )


async def _collect(agent: Any, run_input: ag_ui_core.RunAgentInput) -> list[Any]:
    return [event async for event in agui.run_stream(agent, run_input)]


def _deeply_nested(depth: int) -> dict[str, Any]:
    root: dict[str, Any] = {}
    node = root
    for _ in range(depth):
        node["n"] = {}
        node = cast("dict[str, Any]", node["n"])
    return root


def test_deeply_nested_client_state_does_not_crash_the_run() -> None:
    # ~6KB of body from any unauthenticated client. json_safe recursed with no depth
    # guard, and the snapshot was built OUTSIDE run_stream's try block, so RecursionError
    # escaped the generator entirely — no RUN_ERROR, no RUN_FINISHED, just a broken
    # connection. That violates the mandatory run bracketing.
    events = asyncio.run(_collect(_FakeAgent(), _run_input(state=_deeply_nested(3000))))
    assert events[0].type == ag_ui_core.EventType.RUN_STARTED
    assert events[-1].type in (
        ag_ui_core.EventType.RUN_FINISHED,
        ag_ui_core.EventType.RUN_ERROR,
    )
    agui.check_conformance(events)


def test_json_safe_truncates_rather_than_recursing_without_limit() -> None:
    safe = agui.json_safe(_deeply_nested(3000))
    assert isinstance(safe, dict)


@pytest.mark.parametrize(
    "secret_message",
    [
        "auth failed for Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.PAYLOAD.SIG",
        "connection refused: AccountKey=abc123def456ghi789jkl012mno345pqr678stu901==",
        "invalid api-key sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    ],
)
def test_run_error_never_carries_credential_material_to_the_browser(secret_message: str) -> None:
    # The previous test for this raised RuntimeError("backend rejected the request") — a
    # message that never contained a secret — so it could not tell "redaction works" from
    # "no redaction exists". These messages DO contain secrets, and failed before the fix.
    events = asyncio.run(_collect(_FakeAgent(fail=RuntimeError(secret_message)), _run_input()))
    errors = [e for e in events if e.type == ag_ui_core.EventType.RUN_ERROR]
    assert len(errors) == 1

    emitted = errors[0].message
    for marker in ("eyJ0eXAi", "AccountKey=", "sk-proj-", "Bearer "):
        assert marker not in emitted, f"{marker!r} reached a browser-readable event"


def test_run_error_still_says_something_useful_after_redaction() -> None:
    # Redaction that erases the whole message makes every failure indistinguishable.
    events = asyncio.run(
        _collect(_FakeAgent(fail=RuntimeError("service unavailable, try later")), _run_input())
    )
    errors = [e for e in events if e.type == ag_ui_core.EventType.RUN_ERROR]
    assert "service unavailable" in errors[0].message


def test_rule_8_holds_in_both_directions_not_just_one() -> None:
    # run_stream closed an open TEXT message before opening a tool call, but never closed
    # an open TOOL CALL before opening text. It therefore emitted a stream its own
    # conformance oracle rejects:
    #   rule 8 violated: a text message may not open while a tool call is open
    # The existing test only exercised the handled direction.
    from alloy import contracts

    class _Interleaving:
        def __init__(self) -> None:
            self.hooks = HookRegistry()
            self.conversation_id: str | None = None

        async def stream_async(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
            yield {
                "tool_call_started": contracts.ToolCall(call_id="c1", name="check", arguments="")
            }
            yield {"data": "thinking out loud"}
            yield {
                "current_tool_use": contracts.ToolCall(call_id="c1", name="check", arguments="{}")
            }
            yield {"result": contracts.AgentResult(text="done")}

    events = asyncio.run(_collect(_Interleaving(), _run_input()))
    agui.check_conformance(events)

    types = [e.type.value for e in events]
    assert types.index("TOOL_CALL_END") < types.index("TEXT_MESSAGE_START"), (
        "an open tool call must be closed before a text message opens, or a real client "
        "rejects the stream"
    )


def test_a_closed_tool_call_is_not_closed_twice() -> None:
    # Closing early to satisfy rule 8 must not then double-close when the completing
    # event arrives — that would be a rule 6 violation instead.
    from alloy import contracts

    class _Interleaving:
        def __init__(self) -> None:
            self.hooks = HookRegistry()
            self.conversation_id: str | None = None

        async def stream_async(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
            yield {
                "tool_call_started": contracts.ToolCall(call_id="c1", name="check", arguments="")
            }
            yield {"data": "text forces the close"}
            yield {
                "current_tool_use": contracts.ToolCall(call_id="c1", name="check", arguments="{}")
            }
            yield {"result": contracts.AgentResult(text="done")}

    events = asyncio.run(_collect(_Interleaving(), _run_input()))
    ends = [e for e in events if e.type == ag_ui_core.EventType.TOOL_CALL_END]
    assert len(ends) == 1, f"expected one TOOL_CALL_END, got {len(ends)}"


def test_abandoning_the_stream_closes_the_agents_own_iterator() -> None:
    # A client disconnect abandons run_stream part-way. `async for` does NOT close what it
    # iterates, so without an explicit aclose the agent's stream_async never runs its own
    # `finally` — and in the real Agent that finally is what stops a worker thread. The
    # leaked thread then spins for the life of the process. Reproduced live before the
    # fix: the repro hung until killed, with an `asyncio_0` thread still running.
    closed = {"inner": False}

    class _TracksClosure:
        def __init__(self) -> None:
            self.hooks = HookRegistry()
            self.conversation_id: str | None = None

        async def stream_async(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
            try:
                for index in range(1000):
                    yield {"data": f"chunk{index} "}
            finally:
                closed["inner"] = True

    async def abandon() -> None:
        stream = agui.run_stream(cast(Any, _TracksClosure()), _run_input())
        for _ in range(3):
            await stream.__anext__()
        await stream.aclose()

    asyncio.run(abandon())
    assert closed["inner"], (
        "the agent's own stream_async finally never ran, so whatever it cleans up — a "
        "worker thread, in the real Agent — was leaked"
    )
