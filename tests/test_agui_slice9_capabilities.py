"""Slice 9: alloy declares what it actually supports, and nothing more.

Behaviour B44. A capabilities declaration that overstates is worse than none — a client
that trusts it renders affordances the server cannot honour.
"""

from __future__ import annotations

from alloy import agui


def test_b44_state_capabilities_are_claimed_because_they_are_built() -> None:
    declared = agui.capabilities()
    assert declared.state is not None
    assert declared.state.snapshots is True
    assert declared.state.deltas is True


def test_b44_reasoning_is_not_claimed() -> None:
    # Azure does not expose reasoning content through the path alloy uses, so the events
    # would be structurally empty. Claiming it would have a client render an empty pane.
    declared = agui.capabilities()
    assert declared.reasoning is None or declared.reasoning.model_dump(exclude_none=True) == {}


def test_b44_human_in_the_loop_is_not_claimed() -> None:
    # alloy has no pause-and-resume anywhere; a client offering an approval affordance
    # would hang forever waiting for a resume this server cannot honour.
    declared = agui.capabilities()
    assert (
        declared.human_in_the_loop is None
        or declared.human_in_the_loop.model_dump(exclude_none=True) == {}
    )


def test_b44_multi_agent_is_not_claimed() -> None:
    declared = agui.capabilities()
    assert declared.multi_agent is None or declared.multi_agent.model_dump(exclude_none=True) == {}


def test_b44_multimodal_is_not_claimed() -> None:
    # latest_user_prompt rejects non-text content outright, so claiming multimodal would
    # invite exactly the request the parser refuses.
    declared = agui.capabilities()
    assert declared.multimodal is None or declared.multimodal.model_dump(exclude_none=True) == {}


def test_b44_tools_are_claimed_because_tool_calls_stream() -> None:
    declared = agui.capabilities()
    assert declared.tools is not None


def test_b44_capabilities_serialise_to_camel_case_for_the_wire() -> None:
    encoded = agui.capabilities().model_dump_json(by_alias=True, exclude_none=True)
    assert "persistentState" in encoded or "snapshots" in encoded
    assert "human_in_the_loop" not in encoded
