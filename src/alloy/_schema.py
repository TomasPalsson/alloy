"""Derive JSON-Schema tool specs from typed, Google-docstring-documented callables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contracts import ToolSpec


def derive(fn: Callable[..., Any]) -> ToolSpec:
    """Derive a ToolSpec from a typed callable with a Google-style docstring.

    Args:
        fn: The callable to inspect.
    """
    raise NotImplementedError


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Validate a callable's schema at decoration time and return it unchanged.

    Args:
        fn: The callable to validate and register as a tool.
    """
    return fn
