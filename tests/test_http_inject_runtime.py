"""HttpInjectRuntime Contract C wire tests."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from windtunnel.api import Scenario, run_reset_canary, run_scenario
from windtunnel.runtimes.http_inject import HttpInjectRuntime
from windtunnel.spi.agent_runtime import AgentConfig


class _EndpointState:
    def __init__(self) -> None:
        self.inject_responses: list[tuple[int, Any]] = []
        self.reset_responses: list[tuple[int, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def add_request(self, path: str, payload: Any) -> None:
        with self.lock:
            self.requests.append({"path": path, "payload": payload})

    def pop_inject(self) -> tuple[int, Any]:
        with self.lock:
            if self.inject_responses:
                return self.inject_responses.pop(0)
        return 200, {"wt_inject": 1, "reply": "ok", "tool_calls": []}

    def pop_reset(self) -> tuple[int, Any]:
        with self.lock:
            if self.reset_responses:
                return self.reset_responses.pop(0)
        return 200, {"wt_inject": 1}

    def requests_for(self, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return [request for request in self.requests if request["path"] == path]


class _FakeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: _EndpointState) -> None:
        self.state = state
        super().__init__(address, _Handler)


def _body_bytes(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, dict | list):
        return json.dumps(body).encode("utf-8")
    return str(body).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
        state = self.server.state  # type: ignore[attr-defined]
        state.add_request(self.path, payload)

        if self.path == "/wt/inject":
            status, body = state.pop_inject()
        elif self.path == "/wt/reset":
            status, body = state.pop_reset()
        else:
            status, body = 404, {"error": "not found"}

        response_body = _body_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _FakeEndpoint:
    def __init__(self) -> None:
        self.state = _EndpointState()
        self.server = _FakeHTTPServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def add_inject(self, body: Any, *, status: int = 200) -> None:
        self.state.inject_responses.append((status, body))

    def add_reset(self, body: Any, *, status: int = 200) -> None:
        self.state.reset_responses.append((status, body))

    def requests_for(self, path: str) -> list[dict[str, Any]]:
        return self.state.requests_for(path)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


@pytest.fixture
def endpoint() -> _FakeEndpoint:
    fake = _FakeEndpoint()
    try:
        yield fake
    finally:
        fake.close()


def _handle(endpoint: _FakeEndpoint, *, timeout_s: float = 3.0):
    runtime = HttpInjectRuntime(base_url=endpoint.url, timeout_s=timeout_s)
    return runtime.provision(AgentConfig())


def _message(response: dict[str, Any]) -> dict[str, Any]:
    return response["choices"][0]["message"]


def test_happy_path_converts_ordered_tool_calls(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({
        "wt_inject": 1,
        "reply": "Bluewing Logistics ordered pallet 4417.",
        "tool_calls": [
            {"name": "client_lookup", "arguments": {"query": "misrouted pallet"}},
            {"name": "order_status", "arguments": {"order_id": "4417"}},
        ],
    })

    response = _handle(endpoint).send(
        [{"role": "user", "content": "Which client ordered the misrouted pallet?"}],
        "sid-123",
    )

    message = _message(response)
    assert message["content"] == "Bluewing Logistics ordered pallet 4417."
    assert [call["id"] for call in message["tool_calls"]] == ["call_0", "call_1"]
    assert [call["type"] for call in message["tool_calls"]] == ["function", "function"]
    assert [
        call["function"]["name"] for call in message["tool_calls"]
    ] == ["client_lookup", "order_status"]
    assert [
        json.loads(call["function"]["arguments"]) for call in message["tool_calls"]
    ] == [{"query": "misrouted pallet"}, {"order_id": "4417"}]
    assert endpoint.requests_for("/wt/inject")[0]["payload"] == {
        "wt_inject": 1,
        "session_id": "sid-123",
        "text": "Which client ordered the misrouted pallet?",
        "timeout_s": 3.0,
    }


def test_empty_tool_calls_are_accepted(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "No lookup needed.", "tool_calls": []})

    response = _handle(endpoint).send(
        [{"role": "user", "content": "Say hello."}],
        "sid-empty",
    )

    assert _message(response)["content"] == "No lookup needed."
    assert _message(response)["tool_calls"] == []
    assert response["choices"][0]["finish_reason"] == "stop"


def test_missing_tool_calls_key_raises(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "Forgot the calls."})

    with pytest.raises(RuntimeError, match="missing required field 'tool_calls'"):
        _handle(endpoint).send([{"role": "user", "content": "Find the client."}], "sid")


def test_version_mismatch_raises(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({"wt_inject": 2, "reply": "stale", "tool_calls": []})

    with pytest.raises(RuntimeError, match="field 'wt_inject' must equal 1"):
        _handle(endpoint).send([{"role": "user", "content": "Find the client."}], "sid")


def test_non_200_raises(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject("upstream unavailable", status=503)

    with pytest.raises(RuntimeError, match="expected HTTP 200, got 503"):
        _handle(endpoint).send([{"role": "user", "content": "Find the client."}], "sid")


def test_stringified_arguments_are_rejected(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({
        "wt_inject": 1,
        "reply": "I looked it up.",
        "tool_calls": [
            {"name": "client_lookup", "arguments": '{"query": "Bluewing"}'},
        ],
    })

    with pytest.raises(RuntimeError, match="arguments must be a JSON object"):
        _handle(endpoint).send([{"role": "user", "content": "Find the client."}], "sid")


def test_empty_reply_without_error_raises(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "", "tool_calls": []})

    with pytest.raises(RuntimeError, match="reply.*empty only"):
        _handle(endpoint).send([{"role": "user", "content": "Find the client."}], "sid")


def test_error_envelope_surfaces_warning_without_fabricated_reply(
    endpoint: _FakeEndpoint,
) -> None:
    endpoint.add_reset({"wt_inject": 1})
    endpoint.add_inject({
        "wt_inject": 1,
        "reply": "",
        "tool_calls": [],
        "error": "agent timeout waiting for client_lookup",
    })
    runtime = HttpInjectRuntime(base_url=endpoint.url, timeout_s=3.0)
    scenario = Scenario(
        name="http_inject_error_surface",
        prompt="Which client ordered the pallet?",
        target_facts=[["Bluewing Logistics"]],
    )

    result = run_scenario(scenario, runtime)

    trace = result.runs[0].trace
    assert trace.turns[-1].content == ""
    assert trace.worker_warnings == ["agent timeout waiting for client_lookup"]
    assert result.aggregate.verdict == "FAIL"


def test_reset_happy_path(endpoint: _FakeEndpoint) -> None:
    endpoint.add_reset({"wt_inject": 1})

    _handle(endpoint).reset_state()

    assert endpoint.requests_for("/wt/reset")[0]["payload"] == {"wt_inject": 1}


def test_reset_non_200_raises(endpoint: _FakeEndpoint) -> None:
    endpoint.add_reset("reset failed", status=500)

    with pytest.raises(RuntimeError, match="expected HTTP 200, got 500"):
        _handle(endpoint).reset_state()


def test_env_url_override_is_used(
    endpoint: _FakeEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "ok", "tool_calls": []})
    monkeypatch.setenv("WT_INJECT_URL", endpoint.url)
    runtime = HttpInjectRuntime(timeout_s=2.0)

    runtime.provision(AgentConfig()).send(
        [{"role": "user", "content": "Use the configured endpoint."}],
        "sid-env",
    )

    assert endpoint.requests_for("/wt/inject")[0]["payload"]["session_id"] == "sid-env"


def test_env_timeout_override_is_sent(
    endpoint: _FakeEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "ok", "tool_calls": []})
    monkeypatch.setenv("WT_INJECT_TIMEOUT_S", "4.5")
    runtime = HttpInjectRuntime(base_url=endpoint.url)

    runtime.provision(AgentConfig()).send(
        [{"role": "user", "content": "Use the configured timeout."}],
        "sid-timeout",
    )

    assert endpoint.requests_for("/wt/inject")[0]["payload"]["timeout_s"] == 4.5


def test_newest_user_turn_is_sent(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "ok", "tool_calls": []})
    messages = [
        {"role": "system", "content": "You help with logistics."},
        {"role": "user", "content": "Remember client Bluewing."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Which client did I mention?"},
    ]

    _handle(endpoint).send(messages, "sid-history")

    assert endpoint.requests_for("/wt/inject")[0]["payload"]["text"] == (
        "Which client did I mention?"
    )


def test_reset_canary_composes_with_http_inject(endpoint: _FakeEndpoint) -> None:
    endpoint.add_inject({"wt_inject": 1, "reply": "stored", "tool_calls": []})
    endpoint.add_reset({"wt_inject": 1})
    endpoint.add_inject({"wt_inject": 1, "reply": "No stored code.", "tool_calls": []})
    endpoint.add_inject({"wt_inject": 1, "reply": "No prior code found.", "tool_calls": []})
    runtime = HttpInjectRuntime(base_url=endpoint.url, timeout_s=3.0)

    result = run_reset_canary(runtime, AgentConfig())

    assert result.passed
    assert not result.leaked
