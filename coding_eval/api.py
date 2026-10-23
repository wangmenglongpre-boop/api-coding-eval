"""Direct, single-attempt API calls; never persist credentials or reasoning text."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from .credentials import require_credential, resolve_credential
from .models import AttemptMetrics, AttemptResult, ModelConfig, content_hash

MAPPING_VERSIONS = {
    "openai": "openai-chat-v1",
    "anthropic": "anthropic-messages-v1",
}
_CORE_FIELDS = {
    "model", "messages", "system", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "max_output_tokens", "n", "tools", "tool_choice",
    "functions", "function_call", "prompt", "input", "previous_response_id",
    "extra_body", "extra_headers", "headers", "api_key", "api_key_env",
}
_SECRET_FIELDS = {
    "apikey", "authorization", "password", "secret", "accesstoken", "headers",
    "xapikey", "cookie", "setcookie", "credential", "credentials",
}
_REASONING_FIELDS = {
    "reasoning", "reasoningcontent", "reasoningdetails", "thinking",
    "signature", "encryptedcontent", "redactedthinking",
}


def _normalized(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def sanitize(value: Any, api_key: str | None = None) -> Any:
    """Drop credential fields and reasoning bodies, redact exact active secrets."""
    if isinstance(value, dict):
        if value.get("type") in {"thinking", "redacted_thinking", "reasoning"}:
            return {"type": value["type"], "omitted": True}
        return {
            sanitize(str(key), api_key): sanitize(item, api_key)
            for key, item in value.items()
            if _normalized(str(key)) not in _SECRET_FIELDS | _REASONING_FIELDS
        }
    if isinstance(value, (tuple, list)):
        return [sanitize(item, api_key) for item in value]
    if isinstance(value, str):
        # Pattern-based replacement of "password = ..." would corrupt answers.
        return value.replace(api_key, "[REDACTED]") if api_key else value
    return value


def resolve_url(config: ModelConfig) -> str:
    """Full URLs are verbatim; base URLs get exactly one protocol suffix."""
    if config.url_mode == "full":
        return config.url
    parts = urlsplit(config.url)
    path = parts.path.rstrip("/")
    endpoint = "/chat/completions" if config.protocol == "openai" else "/messages"
    if not path.endswith(endpoint):
        if not path:
            path = "/v1"
        path += endpoint
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def build_request(config: ModelConfig, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Build the exact declared two-prompt input, without history or tools."""
    ModelConfig.model_validate(config.model_dump())
    if not config.enabled:
        raise ValueError("Model is disabled")
    conflicts = _CORE_FIELDS.intersection(config.parameters)
    if conflicts:
        raise ValueError("Parameters cannot override core request fields: " + ", ".join(sorted(conflicts)))
    unsupported = [name for name in config.parameters if config.capabilities.get(name) is False]
    if unsupported:
        raise ValueError("Parameters marked unsupported: " + ", ".join(sorted(unsupported)))
    if config.usage_mapping_version not in {"unknown", MAPPING_VERSIONS[config.protocol]}:
        raise ValueError("Unsupported usage mapping version")
    stream = config.capabilities.get("stream") is not False
    body: dict[str, Any] = dict(config.parameters)
    body.update(model=config.model_id, stream=stream)
    if config.protocol == "openai":
        body["messages"] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        body[config.max_tokens_field] = config.max_output_tokens
        if stream and config.capabilities.get("stream_usage") is not False:
            body["stream_options"] = {"include_usage": True}
    else:
        body["system"] = system_prompt
        body["messages"] = [{"role": "user", "content": user_prompt}]
        body["max_tokens"] = config.max_output_tokens
    return body


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(config: ModelConfig, usage: dict[str, Any]) -> dict[str, Any]:
    """Only map documented fields; missing values are not estimated."""
    if config.protocol == "openai":
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        cached = _token(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None
        reasoning = (
            _token(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict) else None
        )
        values = {
            "input_tokens": _token(usage.get("prompt_tokens")),
            "cache_read_tokens": cached,
            "output_tokens": _token(usage.get("completion_tokens")),
            "reasoning_tokens": reasoning,
            "output_includes_reasoning": (
                True if reasoning is not None else config.capabilities.get("output_includes_reasoning")
            ),
        }
    else:
        base = _token(usage.get("input_tokens"))
        read = _token(usage.get("cache_read_input_tokens"))
        created = _token(usage.get("cache_creation_input_tokens"))
        # Anthropic's input_tokens excludes both cache buckets. Explicit null
        # buckets make the total unknown; omitted buckets predate caching.
        unknown_bucket = any(
            name in usage and _token(usage[name]) is None
            for name in ("cache_read_input_tokens", "cache_creation_input_tokens")
        )
        values = {
            "input_tokens": None if base is None or unknown_bucket else base + (read or 0) + (created or 0),
            "cache_read_tokens": read,
            "cache_creation_tokens": created,
            "output_tokens": _token(usage.get("output_tokens")),
            "output_includes_reasoning": config.capabilities.get("output_includes_reasoning"),
        }
    return {
        **values,
        "raw_usage": usage,
        "usage_mapping_version": MAPPING_VERSIONS[config.protocol],
    }


class _APIError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    event, data = "", []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif not line.startswith(":"):
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
    if data:
        raise _APIError("incomplete_stream", "SSE ended inside an event")


def _json_object(data: str | bytes) -> dict[str, Any]:
    def invalid_constant(_: str) -> None:
        raise ValueError("Non-finite JSON number")

    def finite_float(text: str) -> float:
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    try:
        value = json.loads(data, parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, UnicodeError) as exc:
        raise _APIError("invalid_response", "Response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise _APIError("invalid_response", "Expected a JSON object")
    return value


def _merge_usage(target: dict[str, Any], incoming: Any) -> None:
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                _merge_usage(target[key], value)
            else:
                target[key] = value


class _ResponseState:
    def __init__(self, config: ModelConfig, secret: str | None, clock: Callable[[], float], start: float) -> None:
        self.config, self.secret, self.clock, self.start = config, secret, clock, start
        self.output: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.first_content: float | None = None
        self.first_answer: float | None = None
        self.finish_reason: str | None = None
        self.complete = False
        self.headers_seconds: float | None = None
        self.last_content_seconds: float | None = None
        self.reasoning_chunks = 0
        self.answer_chunks = 0

    def content(self, value: Any, *, answer: bool, stream: bool) -> None:
        if not isinstance(value, str) or not value:
            return
        self.last_content_seconds = self.clock() - self.start
        if answer:
            self.answer_chunks += 1
        else:
            self.reasoning_chunks += 1
        if stream:
            now = self.clock() - self.start
            if self.first_content is None:
                self.first_content = now
            if answer and self.first_answer is None:
                self.first_answer = now
        if answer:
            self.output.append(value)

    def consume(self, payload: dict[str, Any], *, stream: bool) -> None:
        if payload.get("error") or payload.get("type") == "error":
            # Never copy arbitrary error bodies (which may echo input/reasoning).
            self.events.append({"type": "error", "omitted": True})
            raise _APIError("provider_error", "Provider returned an error; adjust configuration or retry explicitly")
        self.events.append(sanitize(payload, self.secret))
        if self.config.protocol == "openai":
            _merge_usage(self.usage, sanitize(payload.get("usage"), self.secret))
            choices = payload.get("choices", [])
            if not isinstance(choices, list):
                raise _APIError("invalid_response", "Invalid choices field")
            if len(choices) > 1:
                raise _APIError("invalid_response", "Expected exactly one completion")
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    raise _APIError("invalid_response", "Invalid completion choice")
                message = choice.get("delta" if stream else "message") or {}
                if not isinstance(message, dict):
                    raise _APIError("invalid_response", "Invalid message field")
                for field in ("reasoning_content", "reasoning", "thinking"):
                    self.content(message.get(field), answer=False, stream=stream)
                self.content(message.get("content"), answer=True, stream=stream)
                if choice.get("finish_reason") is not None:
                    self.finish_reason = str(choice["finish_reason"])
        else:
            kind = payload.get("type")
            message = payload.get("message", {}) if kind == "message_start" else payload
            if not isinstance(message, dict):
                raise _APIError("invalid_response", "Invalid message field")
            _merge_usage(self.usage, sanitize(message.get("usage"), self.secret))
            if message.get("stop_reason") is not None:
                self.finish_reason = str(message["stop_reason"])
            blocks = message.get("content") or []
            if kind == "content_block_start":
                blocks = [payload.get("content_block", {})]
            elif kind == "content_block_delta":
                blocks = [payload.get("delta", {})]
            elif kind == "message_delta":
                delta = payload.get("delta") or {}
                if not isinstance(delta, dict):
                    raise _APIError("invalid_response", "Invalid message delta")
                if delta.get("stop_reason") is not None:
                    self.finish_reason = str(delta["stop_reason"])
            if not isinstance(blocks, list):
                raise _APIError("invalid_response", "Invalid content blocks")
            for block in blocks:
                if not isinstance(block, dict):
                    raise _APIError("invalid_response", "Invalid content block")
                if block.get("type") in {"text", "text_delta"}:
                    self.content(block.get("text"), answer=True, stream=stream)
                elif block.get("type") in {"thinking", "thinking_delta"}:
                    self.content(block.get("thinking"), answer=False, stream=stream)

    def check_finish(self) -> None:
        if not self.complete:
            raise _APIError("incomplete_stream", "Response ended without a completion marker")
        if self.finish_reason in {"length", "max_tokens", "model_context_window_exceeded"}:
            raise _APIError("truncated", "Response reached an output or context limit")
        accepted = {"stop"} if self.config.protocol == "openai" else {"end_turn", "stop_sequence"}
        if self.finish_reason not in accepted:
            raise _APIError("invalid_finish", "Missing or unsupported finish reason")
        if not self.output:
            raise _APIError("empty_response", "No final answer text was returned")


class APICancelledError(asyncio.CancelledError):
    """Local cancellation carrying sanitized observations, not final billing."""

    def __init__(self, result: AttemptResult) -> None:
        super().__init__("API request cancelled locally")
        self.result = result


class APIClient:
    """No retries. Inject AsyncClient/MockTransport for offline tests."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("Specify client or transport, not both")
        self._client, self._transport, self._clock = client, transport, clock

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(transport=self._transport, trust_env=False) as client:
                yield client

    async def generate(
        self, config: ModelConfig, system_prompt: str, user_prompt: str, *,
        api_key: str | None = None, run_id: str = "probe", task_id: str = "probe",
        sample_index: int = 0, attempt_index: int = 0, purpose: str = "evaluation",
    ) -> AttemptResult:
        metadata = dict(
            run_id=run_id, model_id=config.id, task_id=task_id, sample_index=sample_index,
            attempt_index=attempt_index, purpose=purpose,
            protocol_mapping_version=MAPPING_VERSIONS[config.protocol],
        )
        # Validate identifiers before any potentially billable request.
        AttemptResult(**metadata, status="failed")
        secret = api_key
        try:
            secret = resolve_credential(config, api_key)
            body = build_request(config, system_prompt, user_prompt)
            require_credential(config, secret)
            headers = {"Accept": "text/event-stream" if body["stream"] else "application/json"}
            if config.protocol == "anthropic":
                headers["anthropic-version"] = "2023-06-01"
                if secret:
                    headers["x-api-key"] = secret
            elif secret:
                headers["Authorization"] = "Bearer " + secret
            url = resolve_url(config)
            request_hash = content_hash({"url": url, "body": body, "mapping": MAPPING_VERSIONS[config.protocol]})
        except (ValueError, TypeError) as exc:
            return AttemptResult(
                **metadata, status="failed", error_code="configuration_error",
                error_message=(
                    "Invalid model configuration; credentials must use environment or session inputs"
                    if isinstance(exc, ValidationError) else sanitize(str(exc), secret)
                ),
            )
        state: _ResponseState | None = None
        error_code = error_message = None
        status = "completed"
        elapsed = None
        cancellation = None
        try:
            async with self._connection() as client:
                start = self._clock()
                state = _ResponseState(config, secret, self._clock, start)
                try:
                    # HTTPX's read timeout is per chunk; this also caps total time.
                    async with asyncio.timeout(config.timeout_seconds):
                        async with client.stream(
                            "POST", url, headers=headers, json=body,
                            timeout=config.timeout_seconds, follow_redirects=False,
                        ) as response:
                            state.headers_seconds = self._clock() - start
                            if not 200 <= response.status_code < 300:
                                raise _APIError(
                                    f"http_{response.status_code}",
                                    f"HTTP {response.status_code}; verify endpoint, credentials and supported parameters",
                                )
                            if body["stream"]:
                                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                                    raise _APIError("invalid_response", "Expected an SSE response")
                                async for event, data in _sse_events(response):
                                    if config.protocol == "openai" and data.strip() == "[DONE]":
                                        state.complete = True
                                        break
                                    payload = _json_object(data)
                                    if config.protocol == "anthropic" and event:
                                        payload.setdefault("type", event)
                                    state.consume(payload, stream=True)
                                    if config.protocol == "anthropic" and payload.get("type") == "message_stop":
                                        state.complete = True
                                        break
                            else:
                                state.consume(_json_object(await response.aread()), stream=False)
                                state.complete = True
                            state.check_finish()
                finally:
                    elapsed = self._clock() - start
        except asyncio.CancelledError as exc:
            cancellation = exc
            status, error_code = "uncertain", "cancelled_in_flight"
            error_message = "Cancelled locally; remote billing is unknown."
        except _APIError as exc:
            status, error_code, error_message = "failed", exc.code, str(exc)
        except httpx.ConnectTimeout:
            status, error_code, error_message = "failed", "timeout", "Connection timed out before a response"
        except (TimeoutError, httpx.TimeoutException):
            status = "failed" if state and state.events else "uncertain"
            error_code, error_message = "timeout", "Request timed out; remote usage may be incomplete"
        except httpx.TransportError as exc:
            status = "failed" if state and state.events else "uncertain"
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)):
                status = "failed"
            error_code, error_message = "transport_error", "Connection failed or interrupted; remote usage may be incomplete"
        except (ValueError, TypeError, httpx.HTTPError):
            status, error_code, error_message = "failed", "invalid_response", "Invalid request or response"
        usage = state.usage if state else {}
        metrics = AttemptMetrics(
            **normalize_usage(config, usage),
            first_content_seconds=state.first_content if state else None,
            first_answer_seconds=state.first_answer if state else None,
            total_seconds=elapsed,
            stream_complete=bool(state and state.complete and status == "completed"),
        )
        output = "".join(state.output) if state else ""
        evidence: Any = state.events if state else None
        if secret and secret in output:
            # An echoed key can span SSE events. Do not persist reconstructible
            # fragments; the sanitized answer and usage remain available.
            evidence = {"redacted": True, "reason": "Response echoed the active credential"}
        result = AttemptResult(
            **metadata, status=status, request_hash=request_hash,
            raw_output=sanitize(output, secret),
            raw_response=evidence, metrics=metrics,
            error_code=error_code, error_message=sanitize(error_message, secret),
            diagnostics={
                "timeout_seconds": config.timeout_seconds,
                "max_output_tokens": config.max_output_tokens,
                "headers_seconds": state.headers_seconds if state else None,
                "last_content_seconds": state.last_content_seconds if state else None,
                "reasoning_chunks": state.reasoning_chunks if state else 0,
                "answer_chunks": state.answer_chunks if state else 0,
                "event_count": len(state.events) if state else 0,
                "finish_reason": state.finish_reason if state else None,
            },
        )
        if cancellation is not None:
            raise APICancelledError(result) from cancellation
        return result
