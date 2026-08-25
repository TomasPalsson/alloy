"""Agent orchestration: construction, tool derivation, and a single call."""

from __future__ import annotations

import asyncio
import threading
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

# Sentinel queued by stream_async's worker thread to signal "no more events this turn".
_STREAM_DONE = object()

# A model that keeps emitting tool calls (a quirk, or instructions smuggled in through a
# tool's own output text) must not loop forever — see F4.
_MAX_TOOL_TURNS = 10


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
        # Non-decorated tools carry no schema to derive and are never run locally (B11) —
        # kept as-is and forwarded untouched to the backend definition (FR-013, see F3).
        self._passthrough_tools = [t for t in tools if not hasattr(t, _TOOL_SPEC_ATTRIBUTE)]
        self._name = name
        self._endpoint = endpoint
        self._credential = credential
        self._client = client
        # Same object as `_client` when injected (a test stub may expose `.agents` on it
        # too); the real path below replaces this with the actual AIProjectClient.
        self._project_client = client
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
            foundry_client = _foundry.FoundryClient(
                endpoint=self._endpoint, credential=self._credential
            )
            self._project_client = foundry_client.project
            self._client = foundry_client.get_openai_client(agent_name=self._name)
        client = cast(Any, self._client)

        # Version tracking needs an agent_name on the backend, so it's opt-in via name=.
        if self._name is not None:
            self._ensure_version()

        # Conversation is created lazily on first call so __init__ stays call-free (see B2).
        # It's the service's id, never a locally invented one — the Responses API's
        # `conversation=` prepends that conversation's items automatically, so only the
        # new turn's items (the prompt, then tool outputs) go in `input=` below.
        if self._conversation_id is None:
            self._conversation_id = _foundry.create_conversation(client)

        self._messages.append(contracts.Message(role="user", content=prompt))

        tool_failures: list[Exception] = []
        next_input: str | list[dict[str, str]] = prompt
        for _turn in range(_MAX_TOOL_TURNS):
            response = client.responses.create(
                # No model/instructions/tools here: the client is scoped to an agent and
                # the agent VERSION already carries them. Passing them is rejected with
                # 'Not allowed when agent is specified.' (live, verified).
                input=next_input,
                conversation=self._conversation_id,
            )
            calls = extract_tool_calls(response)
            if not calls:
                text = response.output_text
                break

            next_input = []
            for result in run_calls(calls, self._tool_map):
                if result.failure is not None:
                    tool_failures.append(result.failure)
                self._messages.append(contracts.Message(role="tool", content=result.output))
                next_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": result.output,
                    }
                )
        else:
            raise contracts.AlloyError(
                f"tool-calling loop exceeded the max of {_MAX_TOOL_TURNS} turns"
            )

        self._messages.append(contracts.Message(role="assistant", content=text))
        return contracts.AgentResult(text=text, tool_failures=tuple(tool_failures))

    async def invoke_async(self, prompt: str) -> contracts.AgentResult:
        """Same as `__call__`, but the blocking backend call runs off the event loop.

        Args:
            prompt: The user prompt to send.
        """
        client = await _foundry.run_off_thread(self._prepare_call, prompt)

        tool_failures: list[Exception] = []
        next_input: str | list[dict[str, str]] = prompt
        for _turn in range(_MAX_TOOL_TURNS):
            response = await _foundry.create_completion(
                client,
                input=next_input,
                conversation=self._conversation_id,
            )
            calls = extract_tool_calls(response)
            if not calls:
                text = response.output_text
                break

            next_input = []
            for result in run_calls(calls, self._tool_map):
                if result.failure is not None:
                    tool_failures.append(result.failure)
                self._messages.append(contracts.Message(role="tool", content=result.output))
                next_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": result.output,
                    }
                )
        else:
            raise contracts.AlloyError(
                f"tool-calling loop exceeded the max of {_MAX_TOOL_TURNS} turns"
            )

        self._messages.append(contracts.Message(role="assistant", content=text))
        return contracts.AgentResult(text=text, tool_failures=tuple(tool_failures))

    async def stream_async(self, prompt: str) -> AsyncIterator[contracts.StreamEvent]:
        """Stream a prompt's response as it arrives, running any tool calls the model makes.

        Yields `{"data": ...}` for each text delta, `{"current_tool_use": ToolCall(...)}`
        before a tool call is run, and finally `{"result": AgentResult(...)}`.

        Runs the blocking stream iteration on a worker thread and relays events back
        through a queue, so the thread hop lives on this `async def` rather than a
        separate helper — this and `invoke_async` are the package's only two (see B30).

        Args:
            prompt: The user prompt to send.
        """
        client = await _foundry.run_off_thread(self._prepare_call, prompt)

        tool_failures: list[Exception] = []
        next_input: str | list[dict[str, str]] = prompt
        for _turn in range(_MAX_TOOL_TURNS):
            text_parts: list[str] = []
            final_text: str | None = None
            pending_calls: list[contracts.ToolCall] = []

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[Any] = asyncio.Queue()
            stop_requested = threading.Event()
            # See the note above: an agent-scoped client rejects model/instructions here.
            create_kwargs = {
                "input": next_input,
                "conversation": self._conversation_id,
            }

            def _pump(
                loop: asyncio.AbstractEventLoop = loop,
                queue: asyncio.Queue[Any] = queue,
                stop_requested: threading.Event = stop_requested,
                create_kwargs: dict[str, Any] = create_kwargs,
            ) -> None:
                # Default-arg bound: without it, every iteration's closure would read
                # the loop variables' final values instead of its own (ruff B023).
                # A single try/except/finally so _STREAM_DONE is queued no matter where
                # this fails — otherwise a consumer awaiting an empty queue would hang.
                try:
                    raw_stream = _foundry.open_stream(client, **create_kwargs)
                    for raw_event in raw_stream:
                        if stop_requested.is_set():
                            return
                        if getattr(raw_event, "type", None) == "error":
                            raise contracts.AlloyError(
                                getattr(raw_event, "message", "stream error")
                            )
                        loop.call_soon_threadsafe(queue.put_nowait, raw_event)
                except Exception as exc:  # the dual error path: an SSE error raised directly
                    loop.call_soon_threadsafe(queue.put_nowait, exc)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

            pump_task = asyncio.ensure_future(asyncio.to_thread(_pump))
            try:
                while True:
                    item = await queue.get()
                    if item is _STREAM_DONE:
                        break
                    if isinstance(item, Exception):
                        raise item
                    raw_event = item

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
            finally:
                stop_requested.set()
                await pump_task

            if not pending_calls:
                text = final_text if final_text is not None else "".join(text_parts)
                break

            next_input = []
            for result in run_calls(pending_calls, self._tool_map):
                if result.failure is not None:
                    tool_failures.append(result.failure)
                self._messages.append(contracts.Message(role="tool", content=result.output))
                next_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": result.output,
                    }
                )
        else:
            raise contracts.AlloyError(
                f"tool-calling loop exceeded the max of {_MAX_TOOL_TURNS} turns"
            )

        self._messages.append(contracts.Message(role="assistant", content=text))
        yield {"result": contracts.AgentResult(text=text, tool_failures=tuple(tool_failures))}

    def _prepare_call(self, prompt: str) -> Any:
        """Shared setup for `__call__`/`invoke_async`/`stream_async`.

        Validates the client, creates a backend version if needed, starts the
        conversation, and records the prompt.
        """
        if self._client is None:
            foundry_client = _foundry.FoundryClient(
                endpoint=self._endpoint, credential=self._credential
            )
            self._project_client = foundry_client.project
            self._client = foundry_client.get_openai_client(agent_name=self._name)
        client = cast(Any, self._client)

        if self._name is not None:
            self._ensure_version()

        if self._conversation_id is None:
            self._conversation_id = _foundry.create_conversation(client)

        self._messages.append(contracts.Message(role="user", content=prompt))
        return client

    def _ensure_version(self) -> None:
        """Create a backend version for the current config, unless one already matches.

        Version work is control-plane (`AIProjectClient.agents.*`), so it runs against
        `self._project_client`, never the openai data-plane client (see the two-client
        split in `_foundry.py`).

        See B14 (matched fingerprint, no-op), B15 (changed prompt, one new version),
        B16 (listing fails, still create and warn), B17 (creation fails, cap reached).
        """
        project_client = cast(Any, self._project_client)
        tool_schemas = [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in self._tool_specs
        ]
        current_fingerprint = fingerprint(self._model, self._system_prompt, tool_schemas)

        try:
            # Materializing the round trip here, not the `for` below, is what's genuinely
            # a backend failure — a bug reading an already-listed version's own fields
            # (e.g. malformed metadata) is a programming error and must not be swallowed
            # into "backend unavailable" (see F2; B16 must keep passing honestly).
            existing_versions = list(project_client.agents.list_versions(self._name))
        except Exception as list_error:
            warnings.warn(
                f"Could not list existing versions for agent {self._name!r}: {list_error}",
                stacklevel=2,
            )
        else:
            for version in existing_versions:
                if version.metadata.get("alloy_fingerprint") == current_fingerprint:
                    return

        definition = _foundry.build_prompt_agent_definition(
            model=self._model,
            instructions=self._system_prompt,
            tools=[*tool_schemas, *self._passthrough_tools],
        )
        try:
            project_client.agents.create_version(
                self._name,
                definition=definition,
                metadata={"alloy_fingerprint": current_fingerprint},
            )
        except Exception as create_error:
            mapped_error = _foundry.map_version_creation_error(
                create_error, agent_name=cast(str, self._name)
            )
            if mapped_error is not None:
                raise mapped_error from create_error
            raise
