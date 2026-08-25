"""Slice 2: the conformance checker (B8-B10).

`check_conformance` is alloy's only defence against emitting a stream that looks fine
in-process but is rejected by a real AG-UI frontend (the reference `verifyEvents` in
`@ag-ui/client` is strictly single-threaded). Every later slice's tests assert through it.
"""

from __future__ import annotations

import re

import ag_ui.core as ag_ui_core
import pytest

import alloy.agui as agui


def _run_input_ids() -> tuple[str, str]:
    return "thread-1", "run-1"


def _started() -> ag_ui_core.RunStartedEvent:
    thread_id, run_id = _run_input_ids()
    return ag_ui_core.RunStartedEvent(thread_id=thread_id, run_id=run_id)


def _finished() -> ag_ui_core.RunFinishedEvent:
    thread_id, run_id = _run_input_ids()
    return ag_ui_core.RunFinishedEvent(thread_id=thread_id, run_id=run_id)


def _error(message: str = "boom") -> ag_ui_core.RunErrorEvent:
    return ag_ui_core.RunErrorEvent(message=message)


# --- B8: legal sequences pass silently -------------------------------------------------


def test_b8_bare_bracket_is_legal() -> None:
    events: list[ag_ui_core.BaseEvent] = [_started(), _finished()]

    assert agui.check_conformance(events) is None


def test_b8_text_only_message_is_legal() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.TextMessageContentEvent(message_id="m1", delta="hel"),
        ag_ui_core.TextMessageContentEvent(message_id="m1", delta="lo"),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        _finished(),
    ]

    assert agui.check_conformance(events) is None


def test_b8_tool_call_only_is_legal() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        ag_ui_core.ToolCallArgsEvent(tool_call_id="tc1", delta='{"city":'),
        ag_ui_core.ToolCallArgsEvent(tool_call_id="tc1", delta='"nyc"}'),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc1"),
        ag_ui_core.ToolCallResultEvent(message_id="m1", tool_call_id="tc1", content="sunny"),
        _finished(),
    ]

    assert agui.check_conformance(events) is None


def test_b8_text_then_tool_call_then_state_is_legal() -> None:
    """Covers text + a tool call + state in one legal sequence, per the assignment."""
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.StateSnapshotEvent(snapshot={"activeToolCalls": {}}),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.TextMessageContentEvent(message_id="m1", delta="Let me check."),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        ag_ui_core.ToolCallArgsEvent(tool_call_id="tc1", delta='{"city":"nyc"}'),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc1"),
        ag_ui_core.ToolCallResultEvent(message_id="m1", tool_call_id="tc1", content="sunny"),
        ag_ui_core.StateDeltaEvent(delta=[{"op": "remove", "path": "/activeToolCalls/tc1"}]),
        _finished(),
    ]

    assert agui.check_conformance(events) is None


def test_b8_two_sequential_tool_calls_with_distinct_ids_is_legal() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        ag_ui_core.ToolCallArgsEvent(tool_call_id="tc1", delta="{}"),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc1"),
        ag_ui_core.ToolCallResultEvent(message_id="m1", tool_call_id="tc1", content="sunny"),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc2", tool_call_name="get_time"),
        ag_ui_core.ToolCallArgsEvent(tool_call_id="tc2", delta="{}"),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc2"),
        ag_ui_core.ToolCallResultEvent(message_id="m2", tool_call_id="tc2", content="noon"),
        _finished(),
    ]

    assert agui.check_conformance(events) is None


def test_b8_run_error_after_closed_text_message_is_legal() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.TextMessageContentEvent(message_id="m1", delta="partial"),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        _error("credential expired"),
    ]

    assert agui.check_conformance(events) is None


def test_b8_reopening_text_message_with_a_new_id_after_closing_is_legal() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        ag_ui_core.TextMessageStartEvent(message_id="m2"),
        ag_ui_core.TextMessageEndEvent(message_id="m2"),
        _finished(),
    ]

    assert agui.check_conformance(events) is None


# --- B9: each rule fails alone, naming itself and the offending event type -------------


def test_b9_rule1_run_started_must_be_first() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        _started(),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*1\b") as exc_info:
        agui.check_conformance(events)
    assert "TEXT_MESSAGE_START" in str(exc_info.value)


def test_b9_rule2_last_event_must_be_a_terminal_event() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.TextMessageContentEvent(message_id="m1", delta="hi"),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*2\b") as exc_info:
        agui.check_conformance(events)
    assert "TEXT_MESSAGE_END" in str(exc_info.value)


def test_b9_rule3_termination_is_exclusive() -> None:
    events: list[ag_ui_core.BaseEvent] = [_started(), _finished(), _error()]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*3\b") as exc_info:
        agui.check_conformance(events)
    assert "RUN_ERROR" in str(exc_info.value)


def test_b9_rule4_nothing_follows_the_terminal_event() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        _finished(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*4\b") as exc_info:
        agui.check_conformance(events)
    assert "TEXT_MESSAGE_START" in str(exc_info.value)


def test_b9_rule5_text_content_requires_an_open_start_with_same_message_id() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageContentEvent(message_id="m1", delta="hi"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*5\b") as exc_info:
        agui.check_conformance(events)
    assert "TEXT_MESSAGE_CONTENT" in str(exc_info.value)


def test_b9_rule6_tool_call_args_requires_an_open_start_with_same_tool_call_id() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.ToolCallArgsEvent(tool_call_id="tc1", delta="{}"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*6\b") as exc_info:
        agui.check_conformance(events)
    assert "TOOL_CALL_ARGS" in str(exc_info.value)


def test_b9_rule7_tool_call_result_must_follow_tool_call_end() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        ag_ui_core.ToolCallResultEvent(message_id="m1", tool_call_id="tc1", content="sunny"),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc1"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*7\b") as exc_info:
        agui.check_conformance(events)
    assert "TOOL_CALL_RESULT" in str(exc_info.value)


def test_b9_rule8_text_open_rejects_tool_call_start() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc1"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*8\b") as exc_info:
        agui.check_conformance(events)
    assert "TOOL_CALL_START" in str(exc_info.value)


def test_b9_rule8_tool_call_open_rejects_text_message_start() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        ag_ui_core.ToolCallEndEvent(tool_call_id="tc1"),
        ag_ui_core.TextMessageEndEvent(message_id="m1"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*8\b") as exc_info:
        agui.check_conformance(events)
    assert "TEXT_MESSAGE_START" in str(exc_info.value)


def test_b9_rule9_state_delta_before_any_snapshot_is_rejected() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.StateDeltaEvent(delta=[{"op": "add", "path": "/x", "value": 1}]),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*9\b") as exc_info:
        agui.check_conformance(events)
    assert "STATE_DELTA" in str(exc_info.value)


def test_b9_rule10_second_state_snapshot_is_rejected() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.StateSnapshotEvent(snapshot={}),
        ag_ui_core.StateSnapshotEvent(snapshot={}),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*10\b") as exc_info:
        agui.check_conformance(events)
    assert "STATE_SNAPSHOT" in str(exc_info.value)


def test_b9_rule11_unclosed_text_message_at_terminal_is_rejected() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.TextMessageStartEvent(message_id="m1"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*11\b") as exc_info:
        agui.check_conformance(events)
    assert "TEXT_MESSAGE_START" in str(exc_info.value)


def test_b9_rule11_unclosed_tool_call_at_terminal_is_rejected() -> None:
    events: list[ag_ui_core.BaseEvent] = [
        _started(),
        ag_ui_core.ToolCallStartEvent(tool_call_id="tc1", tool_call_name="get_weather"),
        _finished(),
    ]

    with pytest.raises(AssertionError, match=r"(?i)\brule\s*11\b") as exc_info:
        agui.check_conformance(events)
    assert "TOOL_CALL_START" in str(exc_info.value)


# --- B10: module docstring states ordering rules and cites the protocol ----------------


def test_b10_module_docstring_states_ordering_rules_and_cites_protocol() -> None:
    doc = agui.__doc__
    assert doc is not None

    assert "RUN_STARTED" in doc
    assert re.search(r"RUN_FINISHED|RUN_ERROR", doc)
    assert re.search(r"single[- ]open", doc, re.IGNORECASE)
    # cites the protocol source spec.md names for ordering/correlation rules
    assert "https://docs.ag-ui.com/concepts/events" in doc
