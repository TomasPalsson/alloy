"""Derive JSON-Schema tool specs from typed, Google-docstring-documented callables."""

from __future__ import annotations

import inspect
import re
import types
from collections.abc import Callable
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from .contracts import JsonSchema, ToolSchemaError, ToolSpec

_JSON_TYPE_BY_ANNOTATION: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

_SECTION_HEADERS = ("Returns:", "Raises:", "Yields:")

# Set by `tool()` on the callables it decorates, so downstream code can tell a
# derived tool apart from an object that should be forwarded unmodified.
_TOOL_SPEC_ATTRIBUTE = "__alloy_tool_spec__"


def derive(fn: Callable[..., Any]) -> ToolSpec:
    """Derive a ToolSpec from a typed callable with a Google-style docstring.

    Args:
        fn: The callable to inspect.
    """
    signature = inspect.signature(fn)
    # `get_type_hints` resolves `from __future__ import annotations` string annotations
    # back to real types; `param.annotation` alone would stay a string in that case.
    resolved_annotations = get_type_hints(fn)
    summary, arg_descriptions = _parse_google_docstring(inspect.getdoc(fn) or "")

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in signature.parameters.items():
        if param_name not in resolved_annotations:
            raise ToolSchemaError(
                f"parameter {param_name!r} of tool {fn.__name__!r} has no type annotation"
            )
        annotation = resolved_annotations[param_name]
        property_schema = _json_schema_for_annotation(annotation, param_name, fn.__name__)
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


def _json_schema_for_annotation(annotation: Any, param_name: str, fn_name: str) -> JsonSchema:
    """Map a supported type annotation to its JSON Schema fragment.

    Supports str, int, float, bool, list[...], dict[...], Literal[...], and X | None.
    Anything else raises ToolSchemaError naming the parameter.
    """
    json_type = _JSON_TYPE_BY_ANNOTATION.get(annotation)
    if json_type is not None:
        return {"type": json_type}

    origin = get_origin(annotation)

    if origin is list:
        args = get_args(annotation)
        schema: JsonSchema = {"type": "array"}
        if args:
            schema["items"] = _json_schema_for_annotation(args[0], param_name, fn_name)
        return schema

    if origin is dict:
        args = get_args(annotation)
        schema = {"type": "object"}
        if len(args) == 2:
            schema["additionalProperties"] = _json_schema_for_annotation(
                args[1], param_name, fn_name
            )
        return schema

    if origin is Literal:
        args = get_args(annotation)
        schema = {"enum": list(args)}
        literal_types: set[type[Any]] = {type(value) for value in args}
        if len(literal_types) == 1:
            literal_json_type = _JSON_TYPE_BY_ANNOTATION.get(next(iter(literal_types)))
            if literal_json_type is not None:
                schema["type"] = literal_json_type
        return schema

    # `X | None`: exactly one non-None member, the other being NoneType.
    if origin is types.UnionType or origin is Union:
        args = get_args(annotation)
        non_none_args = [arg for arg in args if arg is not type(None)]
        if len(args) == 2 and len(non_none_args) == 1:
            inner_schema = _json_schema_for_annotation(non_none_args[0], param_name, fn_name)
            inner_type = inner_schema.get("type")
            if isinstance(inner_type, str):
                return {**inner_schema, "type": [inner_type, "null"]}
            return {"anyOf": [inner_schema, {"type": "null"}]}

    raise ToolSchemaError(
        f"parameter {param_name!r} of tool {fn_name!r} has unsupported type {annotation!r}"
    )


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


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable[..., Any]:
    """Validate a callable's schema at decoration time and mark it as a tool.

    Usable bare (`@tool`) or with overrides (`@tool(name=..., description=...)`).
    Returns the callable unchanged, with its derived ToolSpec attached so callers
    can tell a decorated tool apart from a plain, pass-through object.

    Args:
        fn: The callable to validate and register as a tool.
        name: Override the derived tool name.
        description: Override the derived tool description.
    """

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        spec = derive(target)
        if name is not None or description is not None:
            spec = ToolSpec(
                name=name if name is not None else spec.name,
                description=description if description is not None else spec.description,
                parameters=spec.parameters,
                call=spec.call,
            )
        setattr(target, _TOOL_SPEC_ATTRIBUTE, spec)
        return target

    if fn is None:
        return decorate
    return decorate(fn)
