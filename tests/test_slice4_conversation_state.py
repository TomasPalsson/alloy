"""Slice 4 conversation state and history: conversation id continuity, message history."""

from __future__ import annotations

from typing import Any

from alloy import Agent
from alloy.contracts import Message
from conftest import _StubConversations


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.output_text = content
        self.output: list[Any] = []


class _StubResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        return _StubResponse(f"response-{len(self.calls)}")


class _StubClient:
    def __init__(self) -> None:
        self.responses = _StubResponses()
        self.conversations = _StubConversations()


def test_b12_second_call_passes_same_conversation_id_to_client() -> None:
    client = _StubClient()
    agent = Agent(model="gpt-4o", client=client)

    agent("first prompt")
    agent("second prompt")

    first_conversation_id = client.responses.calls[0]["conversation"]
    second_conversation_id = client.responses.calls[1]["conversation"]

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
