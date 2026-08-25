"""Shared test stubs for the Foundry conversations client."""

from __future__ import annotations

from typing import Any


class _StubConversation:
    def __init__(self, id: str) -> None:
        self.id = id


class _StubConversations:
    def create(self, **kwargs: Any) -> _StubConversation:
        return _StubConversation("conv_1")
