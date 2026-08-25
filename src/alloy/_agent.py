"""Agent orchestration: construction, tool derivation, and a single call."""

from __future__ import annotations

from collections.abc import Sequence

from . import contracts


class Agent:
    """Runs prompts against a model, using tools it derives schemas for."""

    def __init__(
        self,
        *,
        model: str,
        system_prompt: str = "",
        tools: Sequence[object] = (),
        name: str | None = None,
        endpoint: str | None = None,
        credential: object | None = None,
        client: object | None = None,
    ) -> None:
        raise NotImplementedError

    @property
    def messages(self) -> list[contracts.Message]:
        """Read-only view of the conversation history so far."""
        raise NotImplementedError

    def __call__(self, prompt: str) -> contracts.AgentResult:
        """Send a prompt to the model and return its result.

        Args:
            prompt: The user prompt to send.
        """
        raise NotImplementedError
