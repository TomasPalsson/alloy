"""Slice 1 walking skeleton: schema derivation, agent construction, and a single call."""

from __future__ import annotations

from typing import Any

from alloy import Agent
from alloy._schema import derive, tool
from alloy.contracts import AgentResult


@tool
def get_weather(city: str, country: str = "US") -> str:
    """Look up the current weather for a city.

    Args:
        city: The city to look up.
        country: Two-letter country code.
    """
    return "sunny"


class _StubMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChoice:
    def __init__(self, content: str) -> None:
        self.message = _StubMessage(content)


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_StubChoice(content)]


class _StubCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.call_count = 0

    def create(self, **kwargs: Any) -> _StubResponse:
        self.call_count += 1
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubClient:
    def __init__(self, content: str = "hello") -> None:
        self.completions = _StubCompletions(content)
        self.chat = _StubChat(self.completions)


def test_b1_derives_schema_from_type_hints() -> None:
    spec = derive(get_weather)

    assert spec.name == "get_weather"
    assert spec.description == "Look up the current weather for a city."
    assert spec.parameters["properties"]["city"] == {
        "type": "string",
        "description": "The city to look up.",
    }
    assert spec.parameters["properties"]["country"] == {
        "type": "string",
        "description": "Two-letter country code.",
    }
    assert spec.parameters["required"] == ["city"]


def test_b2_construction_makes_zero_calls_against_client() -> None:
    client = _StubClient()

    Agent(model="gpt-4o", tools=[get_weather], client=client)

    assert client.completions.call_count == 0


def test_b3_plain_text_response_becomes_agent_result() -> None:
    client = _StubClient(content="The sky is blue.")
    agent = Agent(model="gpt-4o", client=client)

    result = agent("What color is the sky?")

    assert isinstance(result, AgentResult)
    assert result.text == "The sky is blue."
    assert str(result) == "The sky is blue."


def test_b4_never_called_agent_has_empty_messages() -> None:
    agent = Agent(model="gpt-4o", client=_StubClient())

    assert agent.messages == []
