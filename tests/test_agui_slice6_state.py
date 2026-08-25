"""Slice 6: state the frontend can render (B24-B29).

`run_stream` synthesizes a `STATE_SNAPSHOT` from `seed_state(run_input)` right after
`RUN_STARTED` (design §9's only state write that skips `_RunState.apply`), then emits a
`STATE_DELTA` for every later state mutation - one when a tool call opens
(`activeToolCalls` gains an entry), one when it finishes (removed from `activeToolCalls`,
appended to `completedToolCalls`, and on failure also to `toolFailures`). Every op comes
from `_RunState.apply`, which mutates the state and returns the wire patch together
(decision 3): there is no diff step, so a mutation that bypasses `apply` is silently
missing from the stream. B25 proves the mechanism end to end by replaying every emitted
patch onto the snapshot with a small local RFC 6902 (add/replace/remove subset) applier
and deep-comparing the result to the state the server actually held at `RUN_FINISHED`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import ag_ui.core as ag_ui_core
import pytest

import alloy.agui as agui
from alloy import Agent
from alloy.contracts import AgentResult, StreamEvent, ToolCall, ToolResult
from alloy.hooks import AfterToolCallEvent, HookRegistry


class _FakeAgentWithHooks:
    """Stands in for `alloy.Agent`, exposing a real `HookRegistry` at `.hooks`.

    Same pattern as `tests/test_agui_slice4_tools.py` / `test_agui_slice5_streaming_args.py`:
    `run_stream` registers on this exact registry, and a test's own `AfterToolCallEvent`
    steps fire through it, so the tool-result signal is exercised against the real
    `HookRegistry`, not a mock of it.
    """

    def __init__(self) -> None:
        self.hooks = HookRegistry()
        self.steps: list[StreamEvent | AfterToolCallEvent] = []
        self.recorded_prompts: list[str] = []

    async def stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]:
        self.recorded_prompts.append(prompt)
        for step in self.steps:
            if isinstance(step, AfterToolCallEvent):
                self.hooks.emit(step)
            else:
                yield step


def _user_message(text: str, message_id: str = "m-1") -> ag_ui_core.UserMessage:
    return ag_ui_core.UserMessage(id=message_id, content=text)


def _run_input(
    messages: list[ag_ui_core.Message],
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    state: Any = None,
) -> ag_ui_core.RunAgentInput:
    return ag_ui_core.RunAgentInput(
        thread_id=thread_id,
        run_id=run_id,
        state=state,
        messages=messages,
        tools=[],
        context=[],
        forwarded_props=None,
    )


async def _collect(
    fake: _FakeAgentWithHooks, run_input: ag_ui_core.RunAgentInput
) -> list[ag_ui_core.BaseEvent]:
    return [event async for event in agui.run_stream(cast(Agent, fake), run_input)]


def _json_pointer_parts(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON Pointer: {pointer!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]


def _apply_patch(doc: Any, ops: list[dict[str, Any]]) -> Any:
    """Apply an RFC 6902 add/replace/remove subset to `doc` in place, returning it.

    A small local applier - this build has no `jsonpatch` dependency (decision 3). `remove`
    on a list index SPLICES (`del list[i]`), it does not null the slot, or a later `/-`
    append would land at the wrong index and the replayed state would silently diverge.
    """
    for op in ops:
        kind, path = op["op"], op["path"]
        parts = _json_pointer_parts(path)
        parent: Any = doc
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = parts[-1]
        if kind == "add":
            if isinstance(parent, list):
                parent.insert(len(parent) if key == "-" else int(key), op["value"])
            else:
                parent[key] = op["value"]
        elif kind == "replace":
            if isinstance(parent, list):
                parent[int(key)] = op["value"]
            else:
                parent[key] = op["value"]
        elif kind == "remove":
            if isinstance(parent, list):
                del parent[int(key)]
            else:
                del parent[key]
        else:
            raise ValueError(f"unsupported JSON Patch op: {kind!r}")
    return doc


# --- B24: exactly one STATE_SNAPSHOT, right after RUN_STARTED, before any STATE_DELTA ---


def test_b24_exactly_one_state_snapshot_after_run_started_before_any_state_delta() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(
        call_id="call_abc", name="check_service_status", arguments='{"service": "checkout-api"}'
    )
    result = ToolResult(call_id="call_abc", output="degraded - elevated latency")
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="checkout-api is degraded")},
    ]
    run_input = _run_input(messages=[_user_message("is checkout-api ok?")])

    events = asyncio.run(_collect(fake, run_input))

    assert isinstance(events[0], ag_ui_core.RunStartedEvent)
    assert isinstance(events[1], ag_ui_core.StateSnapshotEvent)
    snapshots = [e for e in events if isinstance(e, ag_ui_core.StateSnapshotEvent)]
    deltas = [e for e in events if isinstance(e, ag_ui_core.StateDeltaEvent)]
    assert len(snapshots) == 1
    assert deltas  # this run's tool call must have produced at least one
    snapshot_idx = events.index(snapshots[0])
    assert all(events.index(d) > snapshot_idx for d in deltas)
    # The exact worked shape from spec.md §4.2's "Initial STATE_SNAPSHOT.snapshot".
    assert snapshots[0].snapshot == {
        "messages": [{"id": "m-1", "role": "user", "content": "is checkout-api ok?"}],
        "activeToolCalls": {},
        "completedToolCalls": [],
        "toolFailures": [],
    }
    agui.check_conformance(events)


# --- B25 (load-bearing): snapshot + ordered deltas replay to the RUN_FINISHED state -----


def test_b25_snapshot_plus_ordered_deltas_replay_to_the_finished_state() -> None:
    fake = _FakeAgentWithHooks()
    call_a = ToolCall(
        call_id="call-a", name="check_service_status", arguments='{"service": "checkout-api"}'
    )
    result_a = ToolResult(call_id="call-a", output="degraded - elevated latency")
    failure = ValueError("timeout after 30s")
    call_b = ToolCall(
        call_id="call-b", name="restart_service", arguments='{"service": "checkout-api"}'
    )
    result_b = ToolResult(
        call_id="call-b", output=json.dumps({"error": str(failure)}), failure=failure
    )
    fake.steps = [
        {"data": "Checking checkout-api..."},
        {"current_tool_use": call_a},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call_a, result=result_a),
        {"current_tool_use": call_b},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call_b, result=result_b),
        {"data": "Restarted after a timeout."},
        {"result": AgentResult(text="...", tool_failures=(failure,))},
    ]
    run_input = _run_input(messages=[_user_message("is checkout-api ok?")])

    events = asyncio.run(_collect(fake, run_input))
    agui.check_conformance(events)

    snapshots = [e for e in events if isinstance(e, ag_ui_core.StateSnapshotEvent)]
    deltas = [e for e in events if isinstance(e, ag_ui_core.StateDeltaEvent)]
    assert len(snapshots) == 1
    assert len(deltas) >= 2  # at least the two tool-finish deltas

    # A real JSON Patch replay, not a re-derivation: every op from the wire, applied in
    # the order the stream carried them, onto a deep copy of the snapshot.
    replayed = json.loads(json.dumps(snapshots[0].snapshot))
    for delta_event in deltas:
        replayed = _apply_patch(replayed, delta_event.delta)

    expected_final_state = {
        # No transition in design §9's table mutates `messages`; it stays exactly what
        # seeding produced.
        "messages": snapshots[0].snapshot["messages"],
        "activeToolCalls": {},
        "completedToolCalls": [
            {
                "toolCallId": "call-a",
                "name": "check_service_status",
                "output": "degraded - elevated latency",
                "failed": False,
            },
            {
                "toolCallId": "call-b",
                "name": "restart_service",
                "output": json.dumps({"error": "timeout after 30s"}),
                "failed": True,
            },
        ],
        "toolFailures": [
            {"toolCallId": "call-b", "error": "ValueError", "message": "timeout after 30s"}
        ],
    }
    assert replayed == expected_final_state
    assert isinstance(events[-1], ag_ui_core.RunFinishedEvent)


# --- B26: every STATE_DELTA.delta is a list of add/replace/remove ops -------------------


def test_b26_every_state_delta_is_a_list_of_add_replace_or_remove_ops() -> None:
    fake = _FakeAgentWithHooks()
    call_a = ToolCall(call_id="call-x", name="check_service_status", arguments="{}")
    result_a = ToolResult(call_id="call-x", output="ok")
    failure = ValueError("boom")
    call_b = ToolCall(call_id="call-y", name="restart_service", arguments="{}")
    result_b = ToolResult(
        call_id="call-y", output=json.dumps({"error": str(failure)}), failure=failure
    )
    fake.steps = [
        {"current_tool_use": call_a},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call_a, result=result_a),
        {"current_tool_use": call_b},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call_b, result=result_b),
        {"result": AgentResult(text="done", tool_failures=(failure,))},
    ]
    run_input = _run_input(messages=[_user_message("run both")])

    events = asyncio.run(_collect(fake, run_input))
    agui.check_conformance(events)

    deltas = [e for e in events if isinstance(e, ag_ui_core.StateDeltaEvent)]
    assert deltas
    allowed_ops = {"add", "replace", "remove"}
    for delta_event in deltas:
        assert isinstance(delta_event.delta, list)
        assert delta_event.delta
        for op in delta_event.delta:
            assert op["op"] in allowed_ops


# --- B27: a tool's outcome reaches state; a failure lands in BOTH collections -----------


def test_b27_tool_outcome_reaches_state_and_a_failure_lands_in_both_collections() -> None:
    fake = _FakeAgentWithHooks()
    call = ToolCall(call_id="call-fail", name="restart_service", arguments='{"service": "auth-api"}')
    failure = RuntimeError("connection refused")
    result = ToolResult(
        call_id="call-fail", output=json.dumps({"error": str(failure)}), failure=failure
    )
    fake.steps = [
        {"current_tool_use": call},
        AfterToolCallEvent(agent=cast(Agent, fake), tool_use=call, result=result),
        {"result": AgentResult(text="", tool_failures=(failure,))},
    ]
    run_input = _run_input(messages=[_user_message("restart auth-api")])

    events = asyncio.run(_collect(fake, run_input))
    agui.check_conformance(events)

    snapshots = [e for e in events if isinstance(e, ag_ui_core.StateSnapshotEvent)]
    deltas = [e for e in events if isinstance(e, ag_ui_core.StateDeltaEvent)]
    assert deltas  # the tool's outcome must have produced at least one delta

    replayed = json.loads(json.dumps(snapshots[0].snapshot))
    for delta_event in deltas:
        replayed = _apply_patch(replayed, delta_event.delta)

    assert replayed["activeToolCalls"] == {}
    completed = [c for c in replayed["completedToolCalls"] if c["toolCallId"] == "call-fail"]
    assert len(completed) == 1
    assert completed[0]["failed"] is True
    assert completed[0]["output"] == result.output
    failures = [f for f in replayed["toolFailures"] if f["toolCallId"] == "call-fail"]
    assert len(failures) == 1
    assert failures[0]["error"] == "RuntimeError"
    assert failures[0]["message"] == "connection refused"


# --- B28: run_input.state is not a JSON object -> no raise, snapshot = what was used ----


@pytest.mark.parametrize("bad_state", ["not-an-object", ["also", "not"], 42])
def test_b28_non_object_run_input_state_is_replaced_not_rejected(bad_state: object) -> None:
    fake = _FakeAgentWithHooks()
    fake.steps = [{"result": AgentResult(text="ok")}]
    run_input = _run_input(messages=[_user_message("status?")], state=bad_state)
    expected_state = agui.seed_state(run_input)
    assert isinstance(expected_state, dict)
    assert set(expected_state) == {"messages", "activeToolCalls", "completedToolCalls", "toolFailures"}

    events = asyncio.run(_collect(fake, run_input))
    agui.check_conformance(events)

    snapshots = [e for e in events if isinstance(e, ag_ui_core.StateSnapshotEvent)]
    assert len(snapshots) == 1
    assert snapshots[0].snapshot == expected_state


# --- B29: a non-JSON-serialisable state value is coerced, never raised ------------------


class _Unserializable:
    def __str__(self) -> str:
        return "<sensor-reading:47.2>"


def test_b29_non_serialisable_state_value_is_coerced_not_raised() -> None:
    weird = _Unserializable()
    assert agui.json_safe(weird) == str(weird)
    assert agui.json_safe("already-safe") == "already-safe"
    assert agui.json_safe(42) == 42

    fake = _FakeAgentWithHooks()
    fake.steps = [{"result": AgentResult(text="ok")}]
    run_input = _run_input(
        messages=[_user_message("status?")],
        state={
            "messages": [{"id": "m-1", "role": "user", "content": weird}],
            "activeToolCalls": {},
            "completedToolCalls": [],
            "toolFailures": [],
        },
    )

    events = asyncio.run(_collect(fake, run_input))
    agui.check_conformance(events)

    snapshots = [e for e in events if isinstance(e, ag_ui_core.StateSnapshotEvent)]
    assert len(snapshots) == 1
    # Without json_safe coercion this raises PydanticSerializationError - verified live
    # against a raw unserialisable object in `snapshot` before writing this assertion.
    encoded = snapshots[0].model_dump_json()
    assert str(weird) in encoded
