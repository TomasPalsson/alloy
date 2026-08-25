"""Tool-call loop: extract calls off a response, decode arguments, run tools locally."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ToolCall, ToolResult, ToolSpec


def extract_tool_calls(response: Any) -> list[ToolCall]:
    """Pull pending function-call items off a model response."""
    raise NotImplementedError


def decode_arguments(raw: str) -> dict[str, Any]:
    """Parse a tool call's raw JSON argument string into a dict."""
    raise NotImplementedError


def run_calls(calls: Sequence[ToolCall], tools: Mapping[str, ToolSpec]) -> list[ToolResult]:
    """Run each requested tool locally; every outcome becomes a ToolResult, nothing raises."""
    raise NotImplementedError
