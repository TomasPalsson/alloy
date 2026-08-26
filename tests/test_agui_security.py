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
