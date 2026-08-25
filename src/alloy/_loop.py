"""Tool-call loop: extract calls off a response, decode arguments, run tools locally."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ToolArgumentError, ToolCall, ToolResult, ToolSpec, UnknownToolError


def extract_tool_calls(response: Any) -> list[ToolCall]:
    """Pull pending function-call items off a model response.

    Looks for a `response.output` list holding items with `type == "function_call"`
    (the shape observed for openai's ResponseFunctionToolCall). A response with no
    such list — e.g. a plain-text completion — yields no calls.
    """
    items = getattr(response, "output", None) or []
    return [
        ToolCall(call_id=item.call_id, name=item.name, arguments=decode_arguments(item.arguments))
        for item in items
        if getattr(item, "type", None) == "function_call"
    ]


def decode_arguments(raw: str) -> dict[str, Any]:
    """Parse a tool call's raw JSON argument string into a dict."""
    return json.loads(raw)


def run_calls(calls: Sequence[ToolCall], tools: Mapping[str, ToolSpec]) -> list[ToolResult]:
    """Run each requested tool locally; every outcome becomes a ToolResult, nothing raises."""
    results: list[ToolResult] = []
    for call in calls:
        spec = tools.get(call.name)
        if spec is None:
            results.append(_failure(call, UnknownToolError(f"no tool named {call.name!r}")))
            continue

        missing = [
            name for name in spec.parameters.get("required", []) if name not in call.arguments
        ]
        if missing:
            failure = ToolArgumentError(
                f"tool {call.name!r} is missing required argument(s): {', '.join(missing)}"
            )
            results.append(_failure(call, failure))
            continue

        try:
            value = spec.call(**call.arguments)
        except Exception as exc:  # the tool's own failure: returned, never raised (see contract)
            results.append(_failure(call, exc))
            continue

        results.append(ToolResult(call_id=call.call_id, output=json.dumps(value)))
    return results


def _failure(call: ToolCall, error: Exception) -> ToolResult:
    """Build the ToolResult for a call that never ran its tool."""
    return ToolResult(call_id=call.call_id, output=json.dumps({"error": str(error)}), failure=error)
