"""Runnable stdlib HTTP server exposing an alloy Agent over /invoke.

Set up and run:

    export AZURE_AI_PROJECT_ENDPOINT=https://<your-project>.services.ai.azure.com/api/projects/<project>
    az login
    uv run examples/serve.py
    uv run examples/serve.py --host 0.0.0.0   # only inside a trusted, isolated network

`GET /ping` for health, `POST /invoke` (and the identical `POST /invocations`, for
AgentCore Runtime path parity) for a prompt, both as plain JSON and, with
`"stream": true` in the body, as Server-Sent Events.

State (see AC-25): one `Agent` is built per request, not once at module scope. An
`Agent` owns a conversation id and a growing message list, so a shared module-level
Agent would leak one caller's conversation into another's. The cost is a
`list_versions` round trip per request. A session-keyed alternative — a
`dict[session_id, Agent]`, keyed by a client-supplied id and evicted on a TTL — would
let a caller's own turns share state without leaking across callers. It is not built
here: a demo server has no notion of "the same caller" to key on.

Standard library only (AC-18) — no FastAPI, no uvicorn, no starlette. Parsing and
dispatch live in plain functions (`dispatch`, below) that take bytes/str and return
`(status, body)` or an iterator of SSE frame strings; `tests/test_serve_example.py`
calls them directly with a fake agent factory, binding no socket and reaching no
network. A thin `BaseHTTPRequestHandler` subclass reads the socket, calls `dispatch`,
and writes the socket — it holds no logic worth testing on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ag_ui.encoder import EventEncoder

from alloy import Agent, tool
from alloy.agui import (
    ThreadStore,
    latest_user_prompt,
    parse_run_input,
    run_stream,
)
from alloy.hooks import (
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

PORT = 8080

# The verification page loads from file://, so every request to this endpoint is
# cross-origin and `application/json` is not a simple content type — the browser sends an
# OPTIONS preflight first and will not send the real POST until it is answered. Withholding
# the header would not make a loopback-bound unauthenticated demo endpoint safer, only
# untestable in the one place the protocol is meant to be used. A real CORS POLICY (origin
# allow-lists, credentials, configurability) remains out of scope.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type, accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
}

# One per process: a thread's conversation must outlive the request that created it, or
# the agent forgets everything after the first message.
THREADS = ThreadStore()


def dispatch(
    method: str, path: str, raw_body: bytes, agent_factory: Callable[[], Agent]
) -> tuple[int, dict[str, Any]] | Iterator[str]:
    """Route one request; the only function the socket handler calls.

    Returns `(status, json_body)` for `/ping`, every error case, and a non-streaming
    `/invoke`; returns an iterator of SSE `data: ...` frame strings for a streaming
    `/invoke`. The handler tells the two apart with `isinstance(outcome, tuple)`.
    """
    if method == "OPTIONS":
        return 204, {}, CORS_HEADERS
    if method == "GET" and path == "/ping":
        return 200, {"status": "Healthy"}
    if method == "POST" and path == "/":
        return _handle_agui(raw_body, agent_factory)
    if method == "POST" and path in ("/invoke", "/invocations"):
        return _handle_invoke(raw_body, agent_factory)
    return 404, {"error": f"no such route: {method} {path}"}


def _handle_agui(
    raw_body: bytes, agent_factory: Callable[..., Agent]
) -> tuple[int, dict[str, Any]] | Iterator[str]:
    """Answer one AG-UI run: parse, validate, then stream protocol events.

    The 400/422 split is deliberate and is NOT shared with `/invoke`: a client needs to
    tell "your JSON is broken" from "your JSON is fine but the shape is wrong", and only
    the second is worth retrying with a corrected field.
    """
    try:
        run_input = parse_run_input(raw_body)
    except json.JSONDecodeError as exc:
        return 400, {"error": f"malformed JSON body: {exc}"}
    except ValueError as exc:
        return 422, {"error": f"not a valid RunAgentInput: {exc}"}

    try:
        # Validated here rather than mid-stream: a run that has already emitted
        # RUN_STARTED can only report this as RUN_ERROR inside a 200, which a client
        # cannot distinguish from a backend failure.
        latest_user_prompt(run_input)
    except ValueError as exc:
        return 422, {"error": str(exc)}

    return _agui_frames(run_input, agent_factory)


def _agui_frames(run_input: Any, agent_factory: Callable[..., Agent]) -> Iterator[str]:
    """Drive `run_stream` on a private event loop, encoding each event to the wire.

    Resolves the thread's conversation BEFORE building the agent, because an agent's
    conversation is fixed at construction. Without this the store is dead weight: every
    request gets a fresh conversation and the agent forgets everything after the first
    message.
    """
    encoder = EventEncoder()
    thread_id = run_input.thread_id
    agent = agent_factory(conversation_id=THREADS.resolve(thread_id))
    loop = asyncio.new_event_loop()
    stream = run_stream(agent, run_input)
    drained = False
    try:
        while True:
            try:
                event = loop.run_until_complete(stream.__anext__())
            except StopAsyncIteration:
                drained = True
                break
            yield encoder.encode(event)
        # Recorded after the run, not before: the id is minted lazily on the first backend
        # call, so before the stream drains there is nothing to remember.
        conversation_id = agent.conversation_id
        if conversation_id is not None:
            THREADS.remember(thread_id, conversation_id)
    finally:
        # A client that disconnects abandons this generator part-way. Python does NOT
        # auto-close what an `async for` iterates, so without this explicit aclose()
        # `stream_async`'s own `finally` never runs and its worker thread is left spinning
        # for the life of the process. Closing the loop first would make the cleanup
        # unrunnable, so the order here is load-bearing.
        if not drained:
            try:
                loop.run_until_complete(stream.aclose())
            except Exception:
                traceback.print_exc(file=sys.stderr)
        loop.close()


def _handle_invoke(
    raw_body: bytes, agent_factory: Callable[[], Agent]
) -> tuple[int, dict[str, Any]] | Iterator[str]:
    """Validate the request body, then run it once or stream it."""
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return 400, {"error": f"malformed JSON body: {exc}"}
    if not isinstance(parsed, dict) or not parsed.get("prompt"):
        return 400, {"error": "missing required field: prompt"}

    prompt = parsed["prompt"]
    if parsed.get("stream"):
        return _stream_frames(prompt, agent_factory)
    return _invoke_once(prompt, agent_factory)


def _invoke_once(prompt: str, agent_factory: Callable[[], Agent]) -> tuple[int, dict[str, Any]]:
    """Run one prompt to completion and return its text, or a 502 on backend failure."""
    agent = agent_factory()
    try:
        result = agent(prompt)
    except Exception as exc:  # the backend's failure, not a bug in this module (AC-26d)
        traceback.print_exc(file=sys.stderr)
        return 502, {"error": type(exc).__name__, "message": str(exc)}
    return 200, {"text": result.text}


def _stream_frames(prompt: str, agent_factory: Callable[[], Agent]) -> Iterator[str]:
    """Drive `stream_async` on a private event loop; yield one SSE frame per StreamEvent.

    A plain generator, not `async def` — the socket handler iterates it with a for-loop
    and writes each frame to the wire as it arrives, which is what makes this stream.
    """
    agent = agent_factory()
    loop = asyncio.new_event_loop()
    try:
        stream = agent.stream_async(prompt)
        while True:
            try:
                event = loop.run_until_complete(stream.__anext__())
            except StopAsyncIteration:
                return
            except Exception as exc:  # backend failure discovered mid-stream (AC-26d)
                traceback.print_exc(file=sys.stderr)
                yield _sse_frame({"error": type(exc).__name__, "message": str(exc)})
                return
            yield _sse_frame(_json_safe(event))
    finally:
        # ponytail: an abandoned stream (client disconnect) leaves stream_async's pump
        # thread running until GC closes the generator; call stream.aclose() first if
        # that matters for your traffic.
        loop.close()


def _sse_frame(payload: dict[str, Any]) -> str:
    """Format one Server-Sent Events `data:` frame."""
    return f"data: {json.dumps(payload)}\n\n"


def _json_safe(event: dict[str, Any]) -> dict[str, Any]:
    """Flatten a StreamEvent's dataclass payloads (`AgentResult`, `ToolCall`) to plain JSON."""
    if "result" in event:
        return {"result": {"text": event["result"].text}}
    if "current_tool_use" in event:
        call = event["current_tool_use"]
        return {"current_tool_use": {"call_id": call.call_id, "name": call.name}}
    return event


@tool
def check_service_status(service_name: str) -> str:
    """Look up whether a service is currently healthy.

    Args:
        service_name: The service to check, e.g. "checkout-api".
    """
    known_incidents = {"checkout-api": "degraded - elevated latency since 09:12 UTC"}
    return known_incidents.get(service_name, "no known incidents")


class AuditLog(HookProvider):
    """Writes one stderr line per tool call, so the server log shows what the model did."""

    def register_hooks(self, registry: HookRegistry) -> None:
        """Listen for both ends of every tool call.

        Args:
            registry: The agent's hook registry.
        """
        registry.add_callback(BeforeToolCallEvent, self.on_before)
        registry.add_callback(AfterToolCallEvent, self.on_after)

    def on_before(self, event: BeforeToolCallEvent) -> None:
        """Record that a tool is about to run.

        Args:
            event: The pending tool call.
        """
        print(f"[audit] -> {event.tool_use.name}({event.tool_use.arguments})", file=sys.stderr)

    def on_after(self, event: AfterToolCallEvent) -> None:
        """Record how a tool call turned out.

        Args:
            event: The completed tool call and its result.
        """
        outcome = "failed" if event.result.failure is not None else "ok"
        print(f"[audit] <- {event.tool_use.name} {outcome}", file=sys.stderr)


def _build_agent(conversation_id: str | None = None) -> Agent:
    """Build one fresh Agent for a single request (see the module docstring on why).

    Carries a tool and an audit hook so the server demonstrates both — a bare agent
    would exercise the HTTP plumbing without exercising anything alloy adds.
    """
    return Agent(
        conversation_id=conversation_id,
        model="gpt-5-mini",
        system_prompt=(
            "You are an on-call assistant reachable over HTTP. "
            "Use your tools to check real service status; never guess."
        ),
        tools=[check_service_status],
        name="http-agent",
        hooks=[AuditLog()],
    )


def _make_handler(agent_factory: Callable[[], Agent]) -> type[BaseHTTPRequestHandler]:
    """Bind `agent_factory` onto a fresh handler class; `HTTPServer` instantiates handlers."""

    class Handler(BaseHTTPRequestHandler):
        """Thin socket adapter: reads the request, calls `dispatch`, writes the response."""

        def do_GET(self) -> None:
            """Handle a GET request by delegating to `dispatch`."""
            self._respond()

        def do_POST(self) -> None:
            """Handle a POST request by delegating to `dispatch`."""
            self._respond()

        def do_OPTIONS(self) -> None:
            """Answer a CORS preflight.

            This method must exist even though `dispatch` already handles OPTIONS:
            `BaseHTTPRequestHandler` answers 501 for any verb it has no `do_<VERB>` for,
            and never reaches `dispatch` at all. A preflight that 501s means the browser
            never sends the real POST.
            """
            self._respond()

        def _respond(self) -> None:
            """Read the body, call `dispatch`, and write back a JSON or SSE response."""
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            outcome = dispatch(self.command, self.path, raw_body, agent_factory)
            if isinstance(outcome, tuple):
                status, body = outcome[0], outcome[1]
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self._send_cors_headers()
                self.end_headers()
                for frame in outcome:
                    self.wfile.write(frame.encode())
                    self.wfile.flush()

        def _send_cors_headers(self) -> None:
            """Attach the CORS headers to whatever response is being written.

            On every response, not only the preflight: a browser rejects the actual
            response too if the origin header is missing from it.
            """
            for name, value in CORS_HEADERS.items():
                self.send_header(name, value)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default per-request access log; startup already prints the bind."""

    return Handler


def main() -> None:
    """Parse `--host`, bind the server, and serve forever."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind. Defaults to loopback only; pass 0.0.0.0 to accept "
        "connections from other hosts, e.g. inside a container (see AC-26a/b).",
    )
    args = parser.parse_args()

    if args.host != "127.0.0.1":
        print(
            f"WARNING: binding {args.host} exposes this unauthenticated agent endpoint "
            "to any host that can reach this port. Do not do this on an untrusted network.",
            file=sys.stderr,
        )
    # flush=True: stdout is block-buffered when piped to a file or a log collector, so
    # without it the operator sees nothing until the buffer fills — which for a server
    # that then blocks in serve_forever() is effectively never.
    print(f"alloy serve: listening on {args.host}:{PORT}", flush=True)

    server = ThreadingHTTPServer((args.host, PORT), _make_handler(_build_agent))
    server.serve_forever()


if __name__ == "__main__":
    main()
