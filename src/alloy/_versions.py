"""Version identity: a stable fingerprint of a run's configuration. See B14-B17."""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import JsonSchema


def fingerprint(model: str, system_prompt: str, tool_schemas: Sequence[JsonSchema]) -> str:
    """A stable identity for (model, system_prompt, tools), independent of tool ordering."""
    raise NotImplementedError
