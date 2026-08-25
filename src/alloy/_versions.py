"""Version identity: a stable fingerprint of a run's configuration. See B14-B17."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from .contracts import JsonSchema


def fingerprint(model: str, system_prompt: str, tool_schemas: Sequence[JsonSchema]) -> str:
    """A stable identity for (model, system_prompt, tools), independent of tool ordering."""
    ordered_tools = sorted(tool_schemas, key=lambda schema: schema["name"])
    canonical = json.dumps(
        {"model": model, "system_prompt": system_prompt, "tools": ordered_tools},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
