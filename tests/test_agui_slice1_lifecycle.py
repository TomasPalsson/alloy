"""Slice 1: run bracketing (B1-B4) + packaging/isolation (B5-B7).

`alloy.agui` implements only `run_stream`'s bracketing and the import guard this slice;
every other public function still raises `NotImplementedError` (later slices' job).
"""

from __future__ import annotations

import asyncio
import importlib
import re
import subprocess
import sys
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import ag_ui.core as ag_ui_core
import pytest

import alloy.agui as agui
from alloy import Agent
from alloy.contracts import AgentResult, StreamEvent

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeAgent:
    """Stands in for `alloy.Agent`: no network, no Foundry client."""

    def __init__(
        self, events: list[StreamEvent] | None = None, fail: Exception | None = None
    ) -> None:
        self._events = events if events is not None else []
        self._fail = fail

    async def stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]:
        if self._fail is not None:
            raise self._fail
        for event in self._events:
            yield event


def _run_input(thread_id: str = "thread-1", run_id: str = "run-1") -> ag_ui_core.RunAgentInput:
    return ag_ui_core.RunAgentInput(
        thread_id=thread_id,
        run_id=run_id,
        state=None,
        # Slice 3 made `run_stream` require a user message (`latest_user_prompt`); bracketing
        # itself doesn't care what the prompt is, so a fixed placeholder satisfies it.
        messages=[ag_ui_core.UserMessage(id="u1", content="hi")],
        tools=[],
        context=[],
        forwarded_props=None,
    )


async def _collect(
    fake: _FakeAgent, run_input: ag_ui_core.RunAgentInput
) -> list[ag_ui_core.BaseEvent]:
    return [event async for event in agui.run_stream(cast(Agent, fake), run_input)]


def test_b1_run_started_is_first_and_echoes_ids() -> None:
    fake = _FakeAgent(events=[{"result": AgentResult(text="hi")}])
    run_input = _run_input(thread_id="thread-abc", run_id="run-xyz")

    events = asyncio.run(_collect(fake, run_input))

    first = events[0]
    assert isinstance(first, ag_ui_core.RunStartedEvent)
    assert first.thread_id == "thread-abc"
    assert first.run_id == "run-xyz"


def test_b2_run_finished_is_last_and_echoes_ids() -> None:
    fake = _FakeAgent(
        events=[{"data": "hel"}, {"data": "lo"}, {"result": AgentResult(text="hello")}]
    )
    run_input = _run_input(thread_id="thread-2", run_id="run-2")

    events = asyncio.run(_collect(fake, run_input))

    last = events[-1]
    assert isinstance(last, ag_ui_core.RunFinishedEvent)
    assert last.thread_id == "thread-2"
    assert last.run_id == "run-2"


def test_b3_agent_raises_emits_run_error_and_nothing_else_follows() -> None:
    fake = _FakeAgent(fail=RuntimeError("boom"))
    run_input = _run_input(thread_id="thread-3", run_id="run-3")

    events = asyncio.run(_collect(fake, run_input))

    assert len(events) == 2  # RunStartedEvent, then RunErrorEvent, nothing more
    assert isinstance(events[-1], ag_ui_core.RunErrorEvent)
    assert events[-1].message == "boom"


def test_b4_run_finished_not_emitted_when_run_errors() -> None:
    fake = _FakeAgent(fail=RuntimeError("boom"))
    run_input = _run_input(thread_id="thread-4", run_id="run-4")

    events = asyncio.run(_collect(fake, run_input))

    assert not any(isinstance(event, ag_ui_core.RunFinishedEvent) for event in events)


def test_b5_import_without_extra_raises_actionable_import_error() -> None:
    # ag-ui-protocol is actually installed in this dev venv (it's a real optional
    # dependency), so "no extra" is simulated by poisoning sys.modules rather than by
    # uninstalling anything - a fresh `import ag_ui.core` then raises ImportError.
    sys.modules.pop("alloy.agui", None)
    try:
        with (
            patch.dict(sys.modules, {"ag_ui": None, "ag_ui.core": None, "ag_ui.encoder": None}),
            pytest.raises(ImportError) as exc_info,
        ):
            importlib.import_module("alloy.agui")
        assert "alloy-foundry[agui]" in str(exc_info.value)
    finally:
        sys.modules.pop("alloy.agui", None)
        importlib.import_module("alloy.agui")  # leave a clean import for later tests


def test_b6_core_package_imports_without_ag_ui() -> None:
    # pydantic is NOT asserted here: azure-ai-projects -> openai -> pydantic means it has
    # always been present in every alloy install, extra or not (AC-20 corrected for this).
    # A subprocess, not sys.modules inspection in-process: this test file already imports
    # ag_ui.core at module scope, which would poison an in-process check.
    script = (
        "import sys\n"
        "import alloy\n"
        "import alloy.hooks\n"
        "from alloy import Agent, tool\n"
        "assert 'ag_ui' not in sys.modules, sorted(sys.modules)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO_ROOT
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_b7_pyproject_declares_agui_extra_and_base_deps_are_exact() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

    assert "agui" in data["project"]["optional-dependencies"]
    base_dep_names = {
        re.split(r"[<>=!~\s]", dep, maxsplit=1)[0] for dep in data["project"]["dependencies"]
    }
    assert base_dep_names == {"azure-ai-projects", "azure-identity"}
