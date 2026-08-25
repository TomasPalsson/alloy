"""Importing an example must not touch the network.

`main()` stays behind `if __name__ == "__main__":` in every example, so loading the
module by path and constructing its classes is safe here — a broken example fails
this test instead of failing silently at demo time.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

from alloy.contracts import ToolCall

EXAMPLES_DIR = pathlib.Path(__file__).resolve().parents[1] / "examples"


def _load_example(filename: str) -> ModuleType:
    path = EXAMPLES_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hooks_example_guardrail_cancels_destructive_tool_and_audit_hook_wires_up() -> None:
    hooks_example = _load_example("hooks.py")
    registry = hooks_example.HookRegistry()
    hooks_example.AuditHook().register_hooks(registry)
    hooks_example.GuardrailHook().register_hooks(registry)

    harmless = ToolCall(call_id="1", name="check_disk_space", arguments="{}")
    destructive = ToolCall(call_id="2", name="wipe_database", arguments="{}")

    harmless_event = registry.emit(hooks_example.BeforeToolCallEvent(agent=None, tool_use=harmless))
    destructive_event = registry.emit(
        hooks_example.BeforeToolCallEvent(agent=None, tool_use=destructive)
    )

    assert harmless_event.cancel_tool is None
    assert destructive_event.cancel_tool is not None
