"""Derive JSON-Schema tool specs from typed, Google-docstring-documented callables."""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable
from typing import Any

from .contracts import JsonSchema, ToolSchemaError, ToolSpec

_JSON_TYPE_BY_ANNOTATION: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

_SECTION_HEADERS = ("Returns:", "Raises:", "Yields:")


def derive(fn: Callable[..., Any]) -> ToolSpec:
    """Derive a ToolSpec from a typed callable with a Google-style docstring.

    Args:
        fn: The callable to inspect.
    """
    signature = inspect.signature(fn)
    # `get_type_hints` resolves `from __future__ import annotations` string annotations
    # back to real types; `param.annotation` alone would stay a string in that case.
    resolved_annotations = typing.get_type_hints(fn)
    summary, arg_descriptions = _parse_google_docstring(inspect.getdoc(fn) or "")

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in signature.parameters.items():
        if param_name not in resolved_annotations:
            raise ToolSchemaError(
                f"parameter {param_name!r} of tool {fn.__name__!r} has no type annotation"
            )
        annotation = resolved_annotations[param_name]
        json_type = _JSON_TYPE_BY_ANNOTATION.get(annotation)
        if json_type is None:
            raise ToolSchemaError(
                f"parameter {param_name!r} of tool {fn.__name__!r} has unsupported "
                f"type {annotation!r}"
            )
        property_schema: dict[str, Any] = {"type": json_type}
        if param_name in arg_descriptions:
            property_schema["description"] = arg_descriptions[param_name]
        properties[param_name] = property_schema
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    parameters: JsonSchema = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    return ToolSpec(name=fn.__name__, description=summary, parameters=parameters, call=fn)


def _parse_google_docstring(docstring: str) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into its summary and per-argument descriptions."""
    body, _, args_section = docstring.partition("Args:")
    summary = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())

    descriptions: dict[str, str] = {}
    current_name: str | None = None
    for line in args_section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in _SECTION_HEADERS:
            break
        match = re.match(r"(\w+)\s*(?:\([^)]*\))?:\s*(.*)", stripped)
        if match:
            name = match.group(1)
            current_name = name
            descriptions[name] = match.group(2)
        elif current_name is not None:
            descriptions[current_name] += " " + stripped
    return summary, descriptions


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Validate a callable's schema at decoration time and return it unchanged.

    Args:
        fn: The callable to validate and register as a tool.
    """
    derive(fn)
    return fn
