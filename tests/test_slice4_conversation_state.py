"""Slice 4 conversation state and history: conversation id continuity, message history."""

from __future__ import annotations

from typing import Any

from alloy import Agent
from alloy.contracts import Message


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
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        return _StubResponse(f"response-{len(self.calls)}")


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubClient:
    def __init__(self) -> None:
        self.completions = _StubCompletions()
        self.chat = _StubChat(self.completions)


def test_b12_second_call_passes_same_conversation_id_to_client() -> None:
    client = _StubClient()
    agent = Agent(model="gpt-4o", client=client)

    agent("first prompt")
    agent("second prompt")

    first_conversation_id = client.completions.calls[0]["conversation_id"]
    second_conversation_id = client.completions.calls[1]["conversation_id"]

    assert first_conversation_id is not None
    assert first_conversation_id == second_conversation_id


def test_b13_messages_contains_both_prompts_and_responses_in_order() -> None:
    client = _StubClient()
    agent = Agent(model="gpt-4o", client=client)

    agent("first prompt")
    agent("second prompt")

    assert agent.messages == [
        Message(role="user", content="first prompt"),
        Message(role="assistant", content="response-1"),
        Message(role="user", content="second prompt"),
        Message(role="assistant", content="response-2"),
    ]

    mutated = agent.messages
    mutated.append(Message(role="user", content="corrupted"))
    assert len(agent.messages) == 4
