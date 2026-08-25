"""Slice 2: schema derivation, complete — defaults, un-derivable types, mixed tool lists."""

from __future__ import annotations

import pytest

from alloy import ToolSchemaError
from alloy._schema import derive, tool
from alloy.contracts import ToolSpec


class _Coordinate:
    """A custom type with no JSON Schema mapping."""


def test_B5_defaulted_parameter_absent_from_required() -> None:
    @tool
    def search(query: str, limit: int = 10) -> str:
        """Search for something.

        Args:
            query: The search query.
            limit: Max number of results.
        """
        return "ok"

    spec = derive(search)

    assert spec.parameters["required"] == ["query"]
    assert "limit" not in spec.parameters["required"]


def test_B6_undecidable_type_raises_at_decoration_time() -> None:
    with pytest.raises(ToolSchemaError, match="location"):

        @tool
        def move(location: _Coordinate) -> str:
            """Move to a location.

            Args:
                location: Where to move to.
            """
            return "ok"


def test_B7_mixed_tools_list_derives_decorated_forwards_plain() -> None:
    @tool
    def get_weather(city: str) -> str:
        """Look up the weather.

        Args:
            city: The city to look up.
        """
        return "sunny"

    plain_tool = {"name": "raw_tool", "already": "a spec"}
    tools: list[object] = [get_weather, plain_tool]

    processed = [
        derive(t) if hasattr(t, "__alloy_tool_spec__") else t for t in tools
    ]

    assert isinstance(processed[0], ToolSpec)
    assert processed[0].name == "get_weather"
    assert processed[1] is plain_tool
