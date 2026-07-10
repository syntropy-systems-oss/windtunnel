"""HttpInjectRuntime — built-in Contract C inject-protocol runtime.

Configuration is intentionally narrow: the base URL defaults to
``http://127.0.0.1:8647`` and may be overridden with ``WT_INJECT_URL``.
The per-request agent deadline defaults to ``120.0`` seconds and may be
overridden with ``WT_INJECT_TIMEOUT_S``. Inject transport calls use that
deadline plus a fixed five-second grace period, matching the protocol's
driver-deadline rule.

Agent-level failures are valid inject envelopes: when a response includes a
non-empty ``error`` string, ``send()`` returns the assistant message exactly
as supplied (possibly empty content) and attaches the raw error string as a
top-level ``worker_warnings`` list. ``windtunnel.api.runner`` copies those
warnings into ``Trace.worker_warnings``, so the run records the error without
fabricating reply text.

``provision(config, mcps)`` ignores ``mcps``. Contract C v1 has no route for
tool registration; the endpoint owns its own tool wiring and reports the
complete ordered tool-call transcript in each inject response.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from windtunnel.spi.agent_runtime import AgentConfig, AgentHandle, Message, Response

DEFAULT_BASE_URL = "http://127.0.0.1:8647"
DEFAULT_TIMEOUT_S = 120.0
GRACE_TIMEOUT_S = 5.0
PROTOCOL_VERSION = 1


def _resolve_timeout(timeout_s: float | None) -> float:
    if timeout_s is None:
        raw = os.environ.get("WT_INJECT_TIMEOUT_S")
        if raw is None:
            value = DEFAULT_TIMEOUT_S
        else:
            try:
                value = float(raw)
            except ValueError as exc:
                raise RuntimeError(
                    f"WT_INJECT_TIMEOUT_S must be a float, got {raw!r}"
                ) from exc
    else:
        value = float(timeout_s)
    if value <= 0:
        raise RuntimeError(f"http_inject timeout_s must be positive, got {value!r}")
    return value


def _decode_http_error_body(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _decode_json_body(body: bytes, operation: str) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"http_inject {operation}: response body is not valid UTF-8: {exc}"
        ) from exc


class _SurfaceRouteAbsent(Exception):
    """The optional route is not implemented on this endpoint (HTTP 404/501)."""


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    operation: str,
    absent_statuses: tuple[int, ...] = (),
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            status = int(getattr(response, "status", response.getcode()))
            response_body = response.read()
    except HTTPError as exc:
        if exc.code in absent_statuses:
            raise _SurfaceRouteAbsent(f"HTTP {exc.code}") from exc
        error_body = _decode_http_error_body(exc.read())
        raise RuntimeError(
            f"http_inject {operation}: expected HTTP 200, got {exc.code}: {error_body}"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"http_inject {operation}: transport timeout after {timeout_s:.1f}s"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"http_inject {operation}: transport error: {exc.reason}"
        ) from exc

    if status != 200:
        error_body = _decode_http_error_body(response_body)
        raise RuntimeError(
            f"http_inject {operation}: expected HTTP 200, got {status}: {error_body}"
        )

    text = _decode_json_body(response_body, operation)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"http_inject {operation}: response body is not valid JSON: {exc}; "
            f"body={text!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"http_inject {operation}: response JSON must be an object, "
            f"got {type(parsed).__name__}"
        )
    return parsed


def _require_version_echo(envelope: dict[str, Any], operation: str) -> None:
    observed = envelope.get("wt_inject")
    if observed != PROTOCOL_VERSION:
        raise RuntimeError(
            f"http_inject {operation}: response field 'wt_inject' must equal "
            f"{PROTOCOL_VERSION}, got {observed!r}"
        )


def _validate_inject_envelope(envelope: dict[str, Any]) -> None:
    _require_version_echo(envelope, "inject")

    if "reply" not in envelope:
        raise RuntimeError("http_inject inject: response missing required field 'reply'")
    if not isinstance(envelope["reply"], str):
        raise RuntimeError(
            "http_inject inject: response field 'reply' must be a string, "
            f"got {type(envelope['reply']).__name__}"
        )

    if "tool_calls" not in envelope:
        raise RuntimeError(
            "http_inject inject: response missing required field 'tool_calls'"
        )
    tool_calls = envelope["tool_calls"]
    if not isinstance(tool_calls, list):
        raise RuntimeError(
            "http_inject inject: response field 'tool_calls' must be a list, "
            f"got {type(tool_calls).__name__}"
        )

    for idx, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            raise RuntimeError(
                f"http_inject inject: tool_calls[{idx}] must be an object, "
                f"got {type(call).__name__}"
            )
        name = call.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(
                f"http_inject inject: tool_calls[{idx}].name must be a non-empty string"
            )
        if "arguments" not in call:
            raise RuntimeError(
                f"http_inject inject: tool_calls[{idx}] missing required field "
                "'arguments'"
            )
        arguments = call["arguments"]
        if not isinstance(arguments, dict):
            raise RuntimeError(
                f"http_inject inject: tool_calls[{idx}].arguments must be a JSON "
                f"object, got {type(arguments).__name__}"
            )

    if "error" in envelope:
        error = envelope["error"]
        if not isinstance(error, str):
            raise RuntimeError(
                "http_inject inject: response field 'error' must be a string, "
                f"got {type(error).__name__}"
            )
        if not error:
            raise RuntimeError(
                "http_inject inject: response field 'error' must be non-empty "
                "when present"
            )
    if envelope["reply"] == "" and not envelope.get("error"):
        raise RuntimeError(
            "http_inject inject: response field 'reply' may be empty only when "
            "non-empty 'error' is present"
        )


def _validate_reset_envelope(envelope: dict[str, Any]) -> None:
    _require_version_echo(envelope, "reset")


_SURFACE_SEGMENT_KEYS = ("system_instructions", "tool_definitions", "extra_segments")


def _validate_surface_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Validate a /wt/surface response; return the surface object.

    All three segment lists are required even when empty — `[]` is a
    conforming "there is nothing of this kind"; a missing key is a
    contract violation (the tool_calls rule again). Raises RuntimeError
    with a specific message on any violation; the caller converts that
    into an "invalid" block rather than failing the run.
    """
    _require_version_echo(envelope, "surface")

    surface = envelope.get("surface")
    if not isinstance(surface, dict):
        raise RuntimeError(
            "http_inject surface: response missing required object field 'surface'"
        )
    for key in _SURFACE_SEGMENT_KEYS:
        if key not in surface:
            raise RuntimeError(
                f"http_inject surface: surface missing required field {key!r} "
                "(required even when empty)"
            )
        if not isinstance(surface[key], list):
            raise RuntimeError(
                f"http_inject surface: surface.{key} must be a list, "
                f"got {type(surface[key]).__name__}"
            )

    for idx, part in enumerate(surface["system_instructions"]):
        if not isinstance(part, dict) or not isinstance(part.get("content"), str):
            raise RuntimeError(
                f"http_inject surface: system_instructions[{idx}] must be an object "
                "with string 'content'"
            )
    for idx, definition in enumerate(surface["tool_definitions"]):
        if not isinstance(definition, dict):
            raise RuntimeError(
                f"http_inject surface: tool_definitions[{idx}] must be an object"
            )
        name = definition.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(
                f"http_inject surface: tool_definitions[{idx}].name must be a "
                "non-empty string"
            )
    for idx, segment in enumerate(surface["extra_segments"]):
        if (
            not isinstance(segment, dict)
            or not isinstance(segment.get("name"), str)
            or not segment.get("name")
            or not isinstance(segment.get("content"), str)
        ):
            raise RuntimeError(
                f"http_inject surface: extra_segments[{idx}] must be an object with "
                "non-empty string 'name' and string 'content'"
            )
    return surface


def _newest_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if not isinstance(content, str):
                raise RuntimeError(
                    "http_inject send: newest user message content must be a string, "
                    f"got {type(content).__name__}"
                )
            return content
    raise RuntimeError("http_inject send: no user message found")


def _openai_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for idx, call in enumerate(tool_calls):
        converted.append({
            "id": f"call_{idx}",
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call["arguments"]),
            },
        })
    return converted


def _to_response(envelope: dict[str, Any]) -> Response:
    tool_calls = _openai_tool_calls(envelope["tool_calls"])
    response: Response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": envelope["reply"],
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ]
    }
    error = envelope.get("error")
    if isinstance(error, str) and error:
        response["worker_warnings"] = [error]
    return response


class _HttpInjectHandle:
    """AgentHandle that POSTs Contract C requests to one endpoint."""

    # Contract C v1 transmits only the newest user text. The runner inspects
    # this private compatibility marker before attempting a history-shaped
    # perturbation, which this wire cannot faithfully deliver.
    _windtunnel_consumes_full_history = False

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def send(self, messages: list[Message], session_id: str) -> Response:
        text = _newest_user_text(messages)
        envelope = _post_json(
            f"{self._base_url}/wt/inject",
            {
                "wt_inject": PROTOCOL_VERSION,
                "session_id": session_id,
                "text": text,
                "timeout_s": self._timeout_s,
            },
            timeout_s=self._timeout_s + GRACE_TIMEOUT_S,
            operation="inject",
        )
        _validate_inject_envelope(envelope)
        return _to_response(envelope)

    def reset_state(self) -> None:
        envelope = _post_json(
            f"{self._base_url}/wt/reset",
            {"wt_inject": PROTOCOL_VERSION},
            timeout_s=self._timeout_s + GRACE_TIMEOUT_S,
            operation="reset",
        )
        _validate_reset_envelope(envelope)

    def describe_surface(self) -> dict[str, Any]:
        """Probe the optional /wt/surface route (Contract C, design 0002).

        404/501 = the endpoint doesn't implement the optional route —
        honest {"status": "unavailable"}, fully conforming. Anything else
        that fails (transport error, non-200, malformed envelope) becomes
        {"status": "invalid", "detail": ...}: the probe fails, never the
        run, and the malformed payload is never stored. A valid envelope
        is labeled "reported" — the endpoint's account of its configured
        surface, which a driver cannot verify through the inject boundary.
        """
        try:
            envelope = _post_json(
                f"{self._base_url}/wt/surface",
                {"wt_inject": PROTOCOL_VERSION},
                timeout_s=self._timeout_s + GRACE_TIMEOUT_S,
                operation="surface",
                absent_statuses=(404, 501),
            )
        except _SurfaceRouteAbsent:
            return {"status": "unavailable"}
        except Exception as exc:
            return {"status": "invalid", "detail": str(exc)}
        try:
            surface = _validate_surface_envelope(envelope)
        except Exception as exc:
            return {"status": "invalid", "detail": str(exc)}
        return {
            "status": "reported",
            "system_instructions": surface["system_instructions"],
            "tool_definitions": surface["tool_definitions"],
            "extra_segments": surface["extra_segments"],
        }

    def teardown(self) -> None:
        # urllib opens per-request connections; there is no client to close.
        pass


class HttpInjectRuntime:
    """Contract C AgentRuntime backed by the built-in HTTP inject wire."""

    def __init__(self, base_url: str | None = None, timeout_s: float | None = None) -> None:
        self._base_url = (base_url or os.environ.get("WT_INJECT_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout_s = _resolve_timeout(timeout_s)
        self.provisions: list[tuple[AgentConfig, _HttpInjectHandle]] = []

    def provision(self, config: AgentConfig, mcps: list[Any] | None = None) -> AgentHandle:
        # mcps: ignored — Contract C v1 has no tool-registration route.
        handle = _HttpInjectHandle(self._base_url, self._timeout_s)
        self.provisions.append((config, handle))
        return handle
