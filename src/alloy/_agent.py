"""Agent orchestration: construction, tool derivation, and a single call."""

from __future__ import annotations

import contextlib
import uuid
import warnings
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, cast

from . import _foundry, contracts
from ._loop import (
    extract_final_text_from_stream_event,
    extract_tool_call_from_stream_item,
    extract_tool_calls,
    run_calls,
    translate_stream_event,
)
from ._schema import derive
from ._versions import fingerprint

# Set by `tool()` in _schema.py; a plain, non-decorated tool lacks it and is forwarded
# to the backend unmodified rather than derived (see B7, B11 — it's never run locally).
_TOOL_SPEC_ATTRIBUTE = "__alloy_tool_spec__"


class _ToolNamespace:
    """Attribute access over an agent's own tools.

    `agent.tool.<name>(**kwargs)` runs it directly, with no model call.
    """

    def __init__(self, tool_map: Mapping[str, contracts.ToolSpec]) -> None:
        self._tool_map = tool_map

    def __getattr__(self, name: str) -> Callable[..., Any]:
        spec = self._tool_map.get(name)
        if spec is None:
            raise contracts.UnknownToolError(f"no tool named {name!r}")
        return spec.call


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
        self._tool_specs = [
            derive(cast(Callable[..., Any], t))
            for t in tools
            if hasattr(t, _TOOL_SPEC_ATTRIBUTE)
        ]
        self._tool_map = {spec.name: spec for spec in self._tool_specs}
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

    @property
    def tool(self) -> _ToolNamespace:
        """Direct, model-free invocation of this agent's own tools by name."""
        return _ToolNamespace(self._tool_map)

    def __call__(self, prompt: str) -> contracts.AgentResult:
        """Send a prompt to the model and return its result, running any tool calls it makes.

        Args:
            prompt: The user prompt to send.
        """
        if self._client is None:
            raise contracts.AlloyError(
                "Agent has no client configured; pass client= explicitly "
                "(building one from endpoint/credential is not yet supported)"
            )
        client = cast(Any, self._client)

        # Version tracking needs an agent_name on the backend, so it's opt-in via name=.
        if self._name is not None:
            self._ensure_version(client)

        # Conversation is created lazily on first call so __init__ stays call-free (see B2).
        if self._conversation_id is None:
            self._conversation_id = str(uuid.uuid4())

        self._messages.append(contracts.Message(role="user", content=prompt))

        tool_failures: list[Exception] = []
        while True:
            response = client.chat.completions.create(
                model=self._model,
                messages=self._request_messages(),
                conversation_id=self._conversation_id,
            )
            calls = extract_tool_calls(response)
            if not calls:
                text = response.choices[0].message.content
                break

            for result in run_calls(calls, self._tool_map):
                if result.failure is not None:
                    tool_failures.append(result.failure)
                self._messages.append(contracts.Message(role="tool", content=result.output))

        self._messages.append(contracts.Message(role="assistant", content=text))
        return contracts.AgentResult(text=text, tool_failures=tuple(tool_failures))

    async def invoke_async(self, prompt: str) -> contracts.AgentResult:
        """Same as `__call__`, but the blocking backend call runs off the event loop.

        Args:
            prompt: The user prompt to send.
        """
        client = self._prepare_call(prompt)

        tool_failures: list[Exception] = []
        while True:
            response = await _foundry.create_completion(
                client,
                model=self._model,
                messages=self._request_messages(),
                conversation_id=self._conversation_id,
            )
            calls = extract_tool_calls(response)
            if not calls:
                text = response.choices[0].message.content
                break

            for result in run_calls(calls, self._tool_map):
                if result.failure is not None:
                    tool_failures.append(result.failure)
                self._messages.append(contracts.Message(role="tool", content=result.output))

        self._messages.append(contracts.Message(role="assistant", content=text))
        return contracts.AgentResult(text=text, tool_failures=tuple(tool_failures))

    async def stream_async(self, prompt: str) -> AsyncIterator[contracts.StreamEvent]:
        """Stream a prompt's response as it arrives, running any tool calls the model makes.

        Yields `{"data": ...}` for each text delta, `{"current_tool_use": ToolCall(...)}`
        before a tool call is run, and finally `{"result": AgentResult(...)}`.

        Args:
            prompt: The user prompt to send.
        """
        client = self._prepare_call(prompt)

        tool_failures: list[Exception] = []
        while True:
            text_parts: list[str] = []
            final_text: str | None = None
            pending_calls: list[contracts.ToolCall] = []

            async with contextlib.aclosing(
                _foundry.stream_completion(
                    client,
                    model=self._model,
                    messages=self._request_messages(),
                    conversation_id=self._conversation_id,
                )
            ) as turn_events:
                async for raw_event in turn_events:
                    text_event = translate_stream_event(raw_event)
                    if text_event is not None:
                        text_parts.append(cast(str, text_event["data"]))
                        yield text_event

                    call = extract_tool_call_from_stream_item(raw_event)
                    if call is not None:
                        pending_calls.append(call)
                        yield {"current_tool_use": call}

                    done_text = extract_final_text_from_stream_event(raw_event)
                    if done_text is not None:
                        final_text = done_text

            if not pending_calls:
                text = final_text if final_text is not None else "".join(text_parts)
                break

            for result in run_calls(pending_calls, self._tool_map):
                if result.failure is not None:
                    tool_failures.append(result.failure)
                self._messages.append(contracts.Message(role="tool", content=result.output))

        self._messages.append(contracts.Message(role="assistant", content=text))
        yield {"result": contracts.AgentResult(text=text, tool_failures=tuple(tool_failures))}

    def _prepare_call(self, prompt: str) -> Any:
        """Shared setup for `__call__`/`invoke_async`/`stream_async`.

        Validates the client, creates a backend version if needed, starts the
        conversation, and records the prompt.
        """
        if self._client is None:
            raise contracts.AlloyError(
                "Agent has no client configured; pass client= explicitly "
                "(building one from endpoint/credential is not yet supported)"
            )
        client = cast(Any, self._client)

        if self._name is not None:
            self._ensure_version(client)

        if self._conversation_id is None:
            self._conversation_id = str(uuid.uuid4())

        self._messages.append(contracts.Message(role="user", content=prompt))
        return client

    def _request_messages(self) -> list[dict[str, str]]:
        """Build the request payload from the system prompt and conversation so far."""
        request_messages: list[dict[str, str]] = []
        if self._system_prompt:
            request_messages.append({"role": "system", "content": self._system_prompt})
        request_messages.extend({"role": m.role, "content": m.content} for m in self._messages)
        return request_messages

    def _ensure_version(self, client: Any) -> None:
        """Create a backend version for the current config, unless one already matches.

        See B14 (matched fingerprint, no-op), B15 (changed prompt, one new version),
        B16 (listing fails, still create and warn), B17 (creation fails, cap reached).
        """
        tool_schemas = [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in self._tool_specs
        ]
        current_fingerprint = fingerprint(self._model, self._system_prompt, tool_schemas)

        try:
            for version in client.agents.list_versions(self._name):
                if version.metadata.get("alloy_fingerprint") == current_fingerprint:
                    return
        except Exception as list_error:  # any backend failure here just skips the dedup check
            warnings.warn(
                f"Could not list existing versions for agent {self._name!r}: {list_error}",
                stacklevel=2,
            )

        definition = {
            "model": self._model,
            "system_prompt": self._system_prompt,
            "tools": tool_schemas,
        }
        try:
            client.agents.create_version(
                self._name,
                definition=definition,
                metadata={"alloy_fingerprint": current_fingerprint},
            )
        except Exception as create_error:  # re-raised below as the domain-specific cap error
            raise contracts.VersionCapError(
                f"Agent {self._name!r} cannot create a new version: {create_error}"
            ) from create_error
