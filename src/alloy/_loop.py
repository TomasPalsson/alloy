"""Tool-call loop: extract calls off a response, decode arguments, run tools locally."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import (
    StreamEvent,
    ToolArgumentError,
    ToolCall,
    ToolResult,
    ToolSpec,
    UnknownToolError,
)


def extract_tool_calls(response: Any) -> list[ToolCall]:
    """Pull pending function-call items off a model response.

    Looks for a `response.output` list holding items with `type == "function_call"`
    (the shape observed for openai's ResponseFunctionToolCall). A response with no
    such list — e.g. a plain-text completion — yields no calls.

    `arguments` is kept as the raw JSON string here, undecoded — `run_calls` decodes it,
    so malformed JSON becomes a `ToolResult.failure` like any other tool failure, rather
    than a bare `json.JSONDecodeError` escaping the run (see F5).
    """
    items: list[Any] = getattr(response, "output", None) or []
    return [
        ToolCall(call_id=str(item.call_id), name=str(item.name), arguments=str(item.arguments))
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

        try:
            arguments = decode_arguments(call.arguments)
        except json.JSONDecodeError as exc:
            failure = ToolArgumentError(
                f"tool {call.name!r} received malformed JSON arguments: {exc}"
            )
            results.append(_failure(call, failure))
            continue

        missing = [name for name in spec.parameters.get("required", []) if name not in arguments]
        if missing:
            failure = ToolArgumentError(
                f"tool {call.name!r} is missing required argument(s): {', '.join(missing)}"
            )
            results.append(_failure(call, failure))
            continue

        try:
            value = spec.call(**arguments)
            output = json.dumps(value)
        except Exception as exc:  # the tool's own failure: returned, never raised (see contract)
            results.append(_failure(call, exc))
            continue

        results.append(ToolResult(call_id=call.call_id, output=output))
    return results


def _failure(call: ToolCall, error: Exception) -> ToolResult:
    """Build the ToolResult for a call that never ran its tool."""
    output = json.dumps({"error": str(error)})
    return ToolResult(call_id=call.call_id, output=output, failure=error)


def translate_stream_event(raw_event: Any) -> StreamEvent | None:
    """Translate one raw streaming SDK event into our StreamEvent shape.

    Only text deltas ("response.output_text.delta") carry something to surface; any other
    event type (lifecycle, tool-call structure, completion) yields None here — those are
    handled by `extract_tool_call_from_stream_item` and `extract_final_text_from_stream_event`.
    """
    if getattr(raw_event, "type", None) == "response.output_text.delta":
        return {"data": raw_event.delta}
    return None


def extract_tool_call_from_stream_item(raw_event: Any) -> ToolCall | None:
    """Pull a finished tool call off a `response.output_item.done` event, if it holds one."""
    if getattr(raw_event, "type", None) != "response.output_item.done":
        return None
    item = raw_event.item
    if getattr(item, "type", None) != "function_call":
        return None
    return ToolCall(call_id=str(item.call_id), name=str(item.name), arguments=str(item.arguments))


def extract_final_text_from_stream_event(raw_event: Any) -> str | None:
    """Pull the finalized assistant text off a `response.output_text.done` event."""
    if getattr(raw_event, "type", None) == "response.output_text.done":
        return str(raw_event.text)
    return None
