"""Shared test stubs for the Foundry conversations client."""

from __future__ import annotations

from typing import Any


class StubConversation:
    def __init__(self, id: str) -> None:
        self.id = id


class StubConversations:
    def create(self, **kwargs: Any) -> StubConversation:
        return StubConversation("conv_1")
