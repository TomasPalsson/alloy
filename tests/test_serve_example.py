"""examples/serve.py: drives `dispatch` directly with a fake agent factory.

No socket bound, no network reached (AC-26). Covers AC-19 through AC-26, plus the
502 backend-failure path and the empty-prompt 400.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from collections.abc import AsyncIterator
from types import ModuleType

from alloy.contracts import AgentResult, BackendAuthError, StreamEvent


def _load_example(filename: str) -> ModuleType:
    path = pathlib.Path(__file__).resolve().parents[1] / "examples" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serve = _load_example("serve.py")


class _FakeAgent:
    """Stands in for `alloy.Agent`: no network, no Foundry client."""

    def __init__(self, text: str = "hello", fail: Exception | None = None) -> None:
        self._text = text
        self._fail = fail

    def __call__(self, prompt: str) -> AgentResult:
        if self._fail is not None:
            raise self._fail
        return AgentResult(text=self._text)

    async def stream_async(self, prompt: str) -> AsyncIterator[StreamEvent]:
        if self._fail is not None:
            raise self._fail
        yield {"data": "hel"}
        yield {"data": "lo"}
        yield {"result": AgentResult(text=self._text)}


def test_ping_returns_200_and_healthy_body() -> None:
    outcome = serve.dispatch("GET", "/ping", b"", lambda: _FakeAgent())

    assert outcome == (200, {"status": "Healthy"})


def test_invoke_returns_200_and_agent_text() -> None:
    outcome = serve.dispatch(
        "POST", "/invoke", b'{"prompt": "hello"}', lambda: _FakeAgent(text="hi there")
    )

    assert outcome == (200, {"text": "hi there"})


def test_invocations_path_behaves_identically_to_invoke() -> None:
    outcome = serve.dispatch(
        "POST", "/invocations", b'{"prompt": "hello"}', lambda: _FakeAgent(text="hi there")
    )

    assert outcome == (200, {"text": "hi there"})


def test_invoke_streaming_emits_one_frame_per_stream_event_ending_with_result() -> None:
    outcome = serve.dispatch(
        "POST",
        "/invoke",
        b'{"prompt": "hello", "stream": true}',
        lambda: _FakeAgent(text="hi there"),
    )

    assert not isinstance(outcome, tuple)
    frames = [json.loads(frame.removeprefix("data: ").rstrip("\n")) for frame in outcome]

    assert frames == [{"data": "hel"}, {"data": "lo"}, {"result": {"text": "hi there"}}]


def test_malformed_json_body_returns_400_and_never_builds_an_agent() -> None:
    built: list[_FakeAgent] = []

    def factory() -> _FakeAgent:
        agent = _FakeAgent()
        built.append(agent)
        return agent

    status, body = serve.dispatch("POST", "/invoke", b"{not json", factory)

    assert status == 400
    assert "error" in body
    assert built == []


def test_missing_prompt_key_returns_400() -> None:
    status, body = serve.dispatch("POST", "/invoke", b"{}", lambda: _FakeAgent())

    assert status == 400
    assert "error" in body


def test_empty_prompt_returns_400() -> None:
    status, body = serve.dispatch("POST", "/invoke", b'{"prompt": ""}', lambda: _FakeAgent())

    assert status == 400
    assert "error" in body


def test_agent_failure_returns_502_with_only_class_name_and_message() -> None:
    outcome = serve.dispatch(
        "POST",
        "/invoke",
        b'{"prompt": "hello"}',
        lambda: _FakeAgent(fail=BackendAuthError("token rejected")),
    )

    assert outcome == (
        502,
        {"error": "BackendAuthError", "message": "token rejected"},
    )


def test_unknown_path_returns_404() -> None:
    status, body = serve.dispatch("GET", "/nope", b"", lambda: _FakeAgent())

    assert status == 404
    assert "error" in body


def test_two_requests_build_independent_agents() -> None:
    built: list[_FakeAgent] = []

    def factory() -> _FakeAgent:
        agent = _FakeAgent(text="ok")
        built.append(agent)
        return agent

    serve.dispatch("POST", "/invoke", b'{"prompt": "first"}', factory)
    serve.dispatch("POST", "/invoke", b'{"prompt": "second"}', factory)

    assert len(built) == 2
    assert built[0] is not built[1]
