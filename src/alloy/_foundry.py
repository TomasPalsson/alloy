"""Azure adapter: the only module allowed to import `azure.*` or `openai` (see contract).

Maps SDK auth failures to `BackendAuthError` here, and only here, so a token or secret
can never reach a message (see B21, B22). The blocking-call-to-thread hop for
`Agent.invoke_async`/`Agent.stream_async` lives in `_agent.py`, alongside its two
`async def`s (see B30 and the "Async" decision in code-design.md) — this module only
opens the underlying (still blocking) SDK calls.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import DefaultAzureCredential
from openai import AuthenticationError as OpenAIAuthenticationError

from .contracts import AlloyError, BackendAuthError, StreamingUnsupportedError, VersionCapError

_ENDPOINT_ENV_VAR = "AZURE_AI_PROJECT_ENDPOINT"
_REAUTH_MESSAGE = (
    "Azure rejected the credential — it may be expired or invalid; "
    "re-authenticate (e.g. `az login`) and try again."
)


def resolve_endpoint(explicit: str | None) -> str:
    """Return the Foundry project endpoint: `explicit`, else the env var, else raise."""
    endpoint = explicit or os.environ.get(_ENDPOINT_ENV_VAR)
    if not endpoint:
        raise AlloyError(
            f"no Foundry endpoint: pass endpoint= or set the {_ENDPOINT_ENV_VAR} "
            "environment variable"
        )
    return endpoint


class FoundryClient:
    """Builds the Foundry project client and hands out an OpenAI-compatible client."""

    def __init__(self, *, endpoint: str | None = None, credential: object | None = None) -> None:
        resolved_endpoint = resolve_endpoint(endpoint)
        resolved_credential = credential if credential is not None else DefaultAzureCredential()
        try:
            # allow_preview is required server-side (a 403 `preview_feature_required`
            # without it) despite the SDK docstring's unrelated client-side claim — see
            # the verified-SDK note in this slice's contract.
            self._project_client = AIProjectClient(
                resolved_endpoint, cast(Any, resolved_credential), allow_preview=True
            )
        except (ClientAuthenticationError, OpenAIAuthenticationError) as exc:
            raise BackendAuthError(_REAUTH_MESSAGE) from exc

    @property
    def project(self) -> Any:
        """The `AIProjectClient` itself — the control plane, for `.agents.*` calls."""
        return self._project_client

    def get_openai_client(self, *, agent_name: str | None = None) -> Any:
        """Return a real `openai` client scoped to this project (and agent, if named)."""
        try:
            return self._project_client.get_openai_client(agent_name=agent_name)
        except (ClientAuthenticationError, OpenAIAuthenticationError) as exc:
            raise BackendAuthError(_REAUTH_MESSAGE) from exc
        except HttpResponseError as exc:
            if getattr(getattr(exc, "error", None), "code", None) == "preview_feature_required":
                raise AlloyError(
                    "Foundry rejected get_openai_client() as a preview feature even though "
                    "allow_preview=True was passed; check the project's preview enrollment"
                ) from exc
            raise


def create_completion(client: Any, **create_kwargs: Any) -> Any:
    """Return the coroutine for one non-streaming `client.responses.create` call.

    A plain function, not `async def`: it hands back the `asyncio.to_thread` coroutine for
    the caller to await, keeping the package's only two `async def`s on `Agent` (see B30).
    """
    return asyncio.to_thread(client.responses.create, **create_kwargs)


def open_stream(client: Any, **create_kwargs: Any) -> Any:
    """Start one streaming `client.responses.create(stream=True)` call.

    Blocking, and run on the caller's thread — `Agent.stream_async` is the one that hops
    this onto a worker thread, so this stays a plain function (see B30). A client whose
    `create` does not accept `stream` raises `StreamingUnsupportedError` (see B19).
    """
    try:
        return client.responses.create(**create_kwargs, stream=True)
    except TypeError as exc:
        raise StreamingUnsupportedError(f"backend does not support streaming: {exc}") from exc


def build_prompt_agent_definition(
    *, model: str, instructions: str, tools: list[dict[str, Any]]
) -> Any:
    """Build the real SDK agent definition (`_agent.py` may not import azure.* to do this).

    Maps alloy's `system_prompt` onto the SDK's `instructions` field — the field is named
    `system_prompt` nowhere in `PromptAgentDefinition`.
    """
    # alloy's tool schemas are plain JSON-Schema dicts, not typed `Tool` subclasses — the
    # SDK model accepts them at runtime; `cast` only satisfies the type checker.
    return PromptAgentDefinition(model=model, instructions=instructions, tools=cast(Any, tools))


def map_version_creation_error(error: Exception, *, agent_name: str) -> Exception | None:
    """Classify a `client.agents.create_version` failure.

    Returns the `AlloyError` to raise in its place, or `None` if `error` should propagate
    unchanged — a programming error (e.g. `AttributeError`) must never be swallowed into a
    domain error (see B17's honest counterpart: not every failure is a version cap).
    """
    if isinstance(error, (ClientAuthenticationError, OpenAIAuthenticationError)):
        return BackendAuthError(_REAUTH_MESSAGE)
    if isinstance(error, HttpResponseError):
        code = (getattr(getattr(error, "error", None), "code", None) or "").lower()
        if error.status_code == 429 or "quota" in code or "cap" in code:
            return VersionCapError(f"Agent {agent_name!r} cannot create a new version: {error}")
        return AlloyError(f"Foundry rejected the version request for agent {agent_name!r}: {error}")
    text = str(error).lower()
    if "cap" in text or "quota" in text:
        return VersionCapError(f"Agent {agent_name!r} cannot create a new version: {error}")
    return None


def create_conversation(client: Any) -> str:
    """Create a new Foundry conversation and return its id.

    Blocking, like the other Foundry calls in this module. Called lazily from `_agent.py`
    on an agent's first call, never from `__init__` (see B2) — the service, not a locally
    invented uuid4, owns conversation identity in the Responses API.
    """
    return client.conversations.create().id
