"""Shared contract for every alloy module. Import from here; never redeclare.

Adding an error variant is a design change — escalate, do not add locally.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

JsonSchema: TypeAlias = dict[str, Any]
Role: TypeAlias = Literal["user", "assistant", "tool"]

# One streaming event. Keys mirror Strands: "data" (text delta), "message",
# "current_tool_use", "result". Absent keys mean "nothing of that kind happened".
StreamEvent: TypeAlias = dict[str, Any]


class AlloyError(Exception):
    """Base for every error this package raises. Callers may catch this alone."""


class ToolSchemaError(AlloyError):
    """A tool's signature cannot be turned into a JSON Schema. Raised at decoration time."""


class UnknownToolError(AlloyError):
    """The backend asked for a tool this agent does not hold."""


class ToolArgumentError(AlloyError):
    """Arguments do not match the tool's schema. Raised before the tool runs."""


class BackendAuthError(AlloyError):
    """The backend rejected the credential, or its token expired.

    Never carries a token, secret, or bearer value in its message or repr.
    """


class VersionCapError(AlloyError):
    """The agent has exhausted its available versions on the backend."""


class StreamingUnsupportedError(AlloyError):
    """The backend does not support streaming responses."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A callable tool, its derived schema, and the function that runs it."""

    name: str
    description: str
    parameters: JsonSchema
    call: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of conversation history."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A backend request to run one tool."""

    call_id: str
    name: str
    # Raw JSON string, undecoded — decoded lazily by run_calls, so malformed JSON becomes
    # a ToolResult.failure rather than a bare json.JSONDecodeError escaping the run (F5).
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of running one tool, ready to submit back to the backend."""

    call_id: str
    output: str
    failure: Exception | None = None


@dataclass(frozen=True, slots=True)
class AgentResult:
    """What calling an agent returns. Stringifies to its text."""

    text: str
    tool_failures: tuple[Exception, ...] = field(default=())

    def __str__(self) -> str:
        """Return the response text, so `print(result)` reads naturally."""
        return self.text
