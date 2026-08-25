"""Agent orchestration: construction, tool derivation, and a single call."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any, cast

from . import contracts
from ._schema import derive


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
        self._model = model
        self._system_prompt = system_prompt
        self._tool_specs = [derive(cast(Callable[..., Any], t)) for t in tools]
        self._name = name
        self._endpoint = endpoint
        self._credential = credential
        self._client = client
        self._messages: list[contracts.Message] = []
        self._conversation_id: str | None = None

    @property
    def messages(self) -> list[contracts.Message]:
        """Read-only view of the conversation history so far."""
        return list(self._messages)

    def __call__(self, prompt: str) -> contracts.AgentResult:
        """Send a prompt to the model and return its result.

        Args:
            prompt: The user prompt to send.
        """
        if self._client is None:
            raise contracts.AlloyError(
                "Agent has no client configured; pass client= explicitly "
                "(building one from endpoint/credential is not yet supported)"
            )
        client = cast(Any, self._client)

        # Conversation is created lazily on first call so __init__ stays call-free (see B2).
        if self._conversation_id is None:
            self._conversation_id = str(uuid.uuid4())

        self._messages.append(contracts.Message(role="user", content=prompt))

        request_messages: list[dict[str, str]] = []
        if self._system_prompt:
            request_messages.append({"role": "system", "content": self._system_prompt})
        request_messages.extend({"role": m.role, "content": m.content} for m in self._messages)

        response = client.chat.completions.create(
            model=self._model,
            messages=request_messages,
            conversation_id=self._conversation_id,
        )
        text = response.choices[0].message.content

        self._messages.append(contracts.Message(role="assistant", content=text))
        return contracts.AgentResult(text=text)
