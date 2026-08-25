"""Translate one alloy `Agent` run into an AG-UI protocol event stream.

Requires the `agui` extra. Importing this module without it fails loudly, so alloy's
zero-dependency core never pulls in `ag-ui-protocol` unless a caller opts in.

Most functions below still raise `NotImplementedError`: this slice implements only run
bracketing (`run_stream`) and the import guard. Slices 2-9 fill in the rest, against the
exact signatures declared here — do not change a signature without updating every later
slice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ._agent import Agent

AGUI_EXTRA_HINT = "install the AG-UI extra: pip install 'alloy-foundry[agui]'"

try:
    import ag_ui.core as ag_ui_core
except ImportError:
    raise ImportError(AGUI_EXTRA_HINT) from None

__all__ = [
    "AGUI_EXTRA_HINT",
    "ThreadStore",
    "capabilities",
    "check_conformance",
    "json_safe",
    "parse_run_input",
    "run_stream",
    "seed_state",
]


def parse_run_input(raw: bytes) -> ag_ui_core.RunAgentInput:
    """Parse an HTTP request body into a `RunAgentInput`.

    Args:
        raw: The raw request body bytes.

    Returns:
        The parsed run input.
    """
    raise NotImplementedError


def seed_state(run_input: ag_ui_core.RunAgentInput) -> dict[str, Any]:
    """Derive a run's initial state from the client-supplied `RunAgentInput`.

    Args:
        run_input: The parsed run input.

    Returns:
        The seeded state dict.
    """
    raise NotImplementedError


def json_safe(value: Any) -> Any:
    """Coerce `value` into something `json.dumps` can encode without raising.

    Args:
        value: Any Python value that may appear in agent state.

    Returns:
        A JSON-encodable equivalent of `value`.
    """
    raise NotImplementedError


def check_conformance(events: Sequence[ag_ui_core.BaseEvent]) -> None:
    """Assert `events` obeys AG-UI ordering rules; raise on the first violation.

    Args:
        events: The full ordered event sequence produced by one run.
    """
    raise NotImplementedError


def capabilities() -> ag_ui_core.AgentCapabilities:
    """Describe what this alloy AG-UI translator supports.

    Returns:
        The declared agent capabilities.
    """
    raise NotImplementedError


async def run_stream(
    agent: Agent, run_input: ag_ui_core.RunAgentInput
) -> AsyncIterator[ag_ui_core.BaseEvent]:
    """Run `agent` against `run_input` and yield the AG-UI event stream.

    Every run is bracketed: `RunStartedEvent` first, then exactly one of
    `RunFinishedEvent`/`RunErrorEvent` last. Nothing follows the closing event.

    Args:
        agent: An already-built alloy agent, scoped to the right conversation.
        run_input: The parsed run request.

    Yields:
        AG-UI events in wire order.
    """
    yield ag_ui_core.RunStartedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)
    try:
        # ponytail: message history -> prompt translation is a later slice's job (see
        # `seed_state`/`parse_run_input`, still NotImplementedError). This slice only
        # brackets the run, so it drives the agent without inspecting what it streams back.
        async for _event in agent.stream_async(""):
            pass
    except Exception as exc:
        yield ag_ui_core.RunErrorEvent(message=str(exc))
        return
    yield ag_ui_core.RunFinishedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)


class ThreadStore:
    """Maps an AG-UI `threadId` to the Foundry conversation id backing it."""

    def resolve(self, thread_id: str) -> str | None:
        """Return the conversation id remembered for `thread_id`, if any.

        Args:
            thread_id: The AG-UI thread id supplied by the client.

        Returns:
            The remembered conversation id, or None if `thread_id` is unseen.
        """
        raise NotImplementedError

    def remember(self, thread_id: str, conversation_id: str) -> None:
        """Record that `thread_id` maps to `conversation_id`.

        Args:
            thread_id: The AG-UI thread id supplied by the client.
            conversation_id: The Foundry conversation id to associate with it.
        """
        raise NotImplementedError


class _RunState:  # pyright: ignore[reportUnusedClass] - unused until a later slice wires it in
    """One run's bracketing/state-patch phase. Private; not part of the public surface."""

    phase: Literal["NOT_STARTED", "OPEN", "TEXT_OPEN", "FINISHED", "ERRORED"]

    def apply(self, op: str, path: str, value: Any = None) -> dict[str, Any]:
        """Apply one JSON Patch operation to this run's state, returning the delta.

        Args:
            op: The JSON Patch op (`add`, `replace`, or `remove`).
            path: The JSON Pointer path the op targets.
            value: The value to write, for `add`/`replace`. Unused for `remove`.

        Returns:
            The JSON Patch operation as emitted on the wire.
        """
        raise NotImplementedError
