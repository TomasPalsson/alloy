"""Public API for alloy: Strands-flavoured ergonomics for Azure AI Foundry agents."""

from __future__ import annotations

from ._agent import Agent
from ._schema import tool
from .contracts import (
    AlloyError,
    BackendAuthError,
    StreamingUnsupportedError,
    ToolArgumentError,
    ToolSchemaError,
    UnknownToolError,
    VersionCapError,
)

__all__ = [
    "Agent",
    "AlloyError",
    "BackendAuthError",
    "StreamingUnsupportedError",
    "ToolArgumentError",
    "ToolSchemaError",
    "UnknownToolError",
    "VersionCapError",
    "tool",
]
