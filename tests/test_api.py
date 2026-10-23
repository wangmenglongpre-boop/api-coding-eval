"""Offline protocol fixtures. No test sends a request to a real provider."""

import asyncio
import json

import httpx
import pytest

from coding_eval.api import APIClient, MAPPING_VERSIONS, build_request, normalize_usage, resolve_url
from coding_eval.models import ModelConfig, reject_secret_fields


def config(protocol="openai", **overrides):
    return ModelConfig(
        **{
            "id": "gateway", "display_name": "Test gateway",
            "protocol": protocol, "url": "https://gateway.invalid/v1", "model_id": "exact-model",
            **overrides,
        }
    )


def event(payload, name=""):
    prefix = f"event: {name}\n" if name else ""
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return (prefix + "data: " + data + "\n\n").encode()


def delta(content=None, **fields):
    return {"choices": [{"index": 0, "delta": {"content": content, **fields}, "finish_reason": None}]}


def finish(reason="stop"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def answer(text="OK", usage=None, reason="stop"):
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": reason}],
        "usage": usage,
    }


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, clock=None):
        self.chunks, self.clock, self.closed = chunks, clock, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.clock:
                self.clock.value += 1
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


def generate(chunks=None, *, model=None, payload=None, clock=None, status=200, **kwargs):
    stream = Stream(chunks or [], clock)
    seen = []

    def handler(request):
        seen.append(request)
        if chunks is not None:
            return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=stream)
        return httpx.Response(status, json=payload)

    client = APIClient(transport=httpx.MockTransport(handler), **({"clock": clock} if clock else {}))
    result = asyncio.run(client.generate(model or config(), "system exact", "user exact", **kwargs))
    assert len(seen) == 1
    return result, seen[0], stream


@pytest.mark.parametrize("protocol,url,mode,expected", [
    ("openai", "https://host.invalid", "base", "https://host.invalid/v1/chat/completions"),
    ("openai", "https://host.invalid/v1/", "base", "https://host.invalid/v1/chat/completions"),
    ("openai", "https://host.invalid/api/v4", "base", "https://host.invalid/api/v4/chat/completions"),
    ("openai", "https://host.invalid/v1/chat/completions/", "base", "https://host.invalid/v1/chat/completions"),
    ("openai", "https://host.invalid/custom/", "full", "https://host.invalid/custom/"),
    ("anthropic", "https://host.invalid", "base", "https://host.invalid/v1/messages"),
    ("anthropic", "https://host.invalid/v1", "base", "https://host.invalid/v1/messages"),
    ("anthropic", "https://host.invalid/v1/messages", "base", "https://host.invalid/v1/messages"),
    ("anthropic", "https://host.invalid/custom", "full", "https://host.invalid/custom"),
])
def test_resolve_url(protocol, url, mode, expected):
    assert resolve_url(config(protocol, url=url, url_mode=mode)) == expected


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_request_preserves_only_declared_inputs(protocol):
    model = config(protocol, max_output_tokens=123, parameters={"temperature": 0.2, "vendor_option": True})
    body = build_request(model, "system exact", "user exact")
    assert body["model"] == "exact-model"
    assert body["stream"] is True
    assert body["temperature"] == 0.2
    assert body["vendor_option"] is True
    if protocol == "openai":
        assert body["messages"] == [
            {"role": "system", "content": "system exact"},
            {"role": "user", "content": "user exact"},
        ]
        assert body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 123
        assert "max_completion_tokens" not in body
    else:
        assert body["system"] == "system exact"
        assert body["messages"] == [{"role": "user", "content": "user exact"}]
        assert body["max_tokens"] == 123
        assert "stream_options" not in body


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_output_limit_field_is_explicit_and_frozen(field, protocol):
    model = config(protocol, max_tokens_field=field, max_output_tokens=127)
    body = build_request(model, "", "")
    expected = field if protocol == "openai" else "max_tokens"
    assert body[expected] == 127
    assert ({"max_tokens", "max_completion_tokens"} & body.keys()) == {expected}
    assert ModelConfig.model_validate_json(model.model_dump_json()).max_tokens_field == field


@pytest.mark.parametrize("capabilities,stream,stream_options", [
    ({}, True, True),
    ({"stream": None, "stream_usage": None}, True, True),
    ({"stream": False}, False, False),
    ({"stream_usage": False}, True, False),
])
def test_capability_defaults(capabilities, stream, stream_options):
    body = build_request(config(capabilities=capabilities), "", "")
    assert body["stream"] is stream
    assert ("stream_options" in body) is stream_options


@pytest.mark.parametrize("field", [
    "model", "messages", "system", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "max_output_tokens", "n", "tools", "tool_choice",
    "functions", "function_call", "prompt", "input", "previous_response_id", "extra_body",
])
def test_core_parameters_rejected_without_request(field):
    async def handler(request):
        pytest.fail("Invalid configurations must not send requests")

    result = asyncio.run(APIClient(transport=httpx.MockTransport(handler)).generate(
        config(parameters={field: "override"}), "s", "u",
    ))
    assert result.status == "failed"
    assert result.error_code == "configuration_error"
    assert field in result.error_message
    assert result.metrics.total_seconds is None


@pytest.mark.parametrize("overrides", [
    {"enabled": False},
    {"parameters": {"temperature": 0}, "capabilities": {"temperature": False}},
    {"usage_mapping_version": "unimplemented-v4"},
    {"api_key_env": "MISSING_TEST_KEY"},
])
def test_preflight_failure(overrides, monkeypatch):
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)
    client = APIClient(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected")))
    result = asyncio.run(client.generate(config(**overrides), "", ""))
    assert result.error_code == "configuration_error"
    assert result.status == "failed"


def test_mutated_config_validation_does_not_echo_embedded_credentials():
    model = config()
    model.parameters["headers"] = {"authorization": "a-different-secret"}
    client = APIClient(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected")))
    result = asyncio.run(client.generate(model, "", ""))
    assert result.error_code == "configuration_error"
    assert "a-different-secret" not in result.model_dump_json()


def test_openai_stream_timing_usage_and_no_reasoning_body():
    clock = Clock()
    usage = {
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 60},
        "completion_tokens_details": {"reasoning_tokens": 8},
        "vendor_counter": 99,
    }
    result, request, stream = generate([
        b": keepalive\n\n",
        event(delta(role="assistant")),
        event(delta("")),
        event(delta(reasoning_content="DO NOT STORE THIS REASONING")),
        event(delta("answer")),
        event(finish()),
        event({"choices": [], "usage": usage}),
        event("[DONE]"),
    ], clock=clock)
    assert result.status == "completed"
    assert result.raw_output == "answer"
    assert result.metrics.first_content_seconds == 4
    assert result.metrics.first_answer_seconds == 5
    assert result.metrics.total_seconds == 8
    assert result.metrics.stream_complete
    assert result.metrics.input_tokens == 100
    assert result.metrics.cache_read_tokens == 60
    assert result.metrics.output_tokens == 20
    assert result.metrics.reasoning_tokens == 8
    assert result.metrics.output_includes_reasoning is True
    assert result.metrics.raw_usage == usage
    assert result.metrics.usage_mapping_version == MAPPING_VERSIONS["openai"]
    assert result.request_hash and len(result.request_hash) == 64
    assert "DO NOT STORE" not in result.model_dump_json()
    assert "reasoning_content" not in result.model_dump_json()
    assert stream.closed
    assert json.loads(request.content)["stream_options"]["include_usage"]


def test_anthropic_stream_merges_nonoverlapping_cache_and_final_usage():
    clock = Clock()
    result, request, _ = generate([
        event({"message": {"content": [], "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30,
            "output_tokens": 1,
        }}}, "message_start"),
        event({"type": "ping"}, "ping"),
        event({"content_block": {"type": "thinking", "thinking": ""}}, "content_block_start"),
        event({"delta": {"type": "thinking_delta", "thinking": "PRIVATE THOUGHT"}}, "content_block_delta"),
        event({"delta": {"type": "signature_delta", "signature": "PRIVATE SIGNATURE"}}, "content_block_delta"),
        event({"delta": {"type": "text_delta", "text": "OK"}}, "content_block_delta"),
        event({"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}}, "message_delta"),
        event({}, "message_stop"),
    ], model=config("anthropic"), api_key="private-key", clock=clock)
    assert result.status == "completed"
    assert result.metrics.first_content_seconds == 4
    assert result.metrics.first_answer_seconds == 6
    assert result.metrics.input_tokens == 60
    assert result.metrics.cache_read_tokens == 20
    assert result.metrics.cache_creation_tokens == 30
    assert result.metrics.output_tokens == 40
    assert result.metrics.raw_usage["input_tokens"] == 10
    assert result.raw_output == "OK"
    assert "PRIVATE" not in result.model_dump_json()
    assert "private-key" not in result.model_dump_json()
    assert request.headers["x-api-key"] == "private-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_nonstream_timing_is_null_and_reasoning_not_collected(protocol):
    payload = answer()
    payload["choices"][0]["message"]["reasoning_content"] = "private-thought"
    if protocol == "anthropic":
        payload = {
            "content": [
                {"type": "thinking", "thinking": "private-thought", "signature": "private-signature"},
                {"type": "redacted_thinking", "data": "private-ciphertext"},
                {"type": "text", "text": "OK"},
            ],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    result, request, _ = generate(payload=payload, model=config(protocol, capabilities={"stream": False}))
    assert result.status == "completed"
    assert result.raw_output == "OK"
    assert result.metrics.first_content_seconds is None
    assert result.metrics.first_answer_seconds is None
    assert result.metrics.total_seconds is not None
    assert result.metrics.stream_complete
    assert result.metrics.cache_read_tokens is None
    assert "private-" not in result.model_dump_json()
    assert json.loads(request.content)["stream"] is False
    assert "stream_options" not in json.loads(request.content)


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": None, "completion_tokens": None}])
def test_missing_usage_stays_null(usage):
    result, _, _ = generate([event(delta("OK")), event(finish()), event({"usage": usage}), event("[DONE]")])
    assert result.status == "completed"
    assert result.metrics.input_tokens is None
    assert result.metrics.output_tokens is None
    assert result.metrics.cache_read_tokens is None


@pytest.mark.parametrize("value", [None, -1, 3.5, True, "42"])
def test_invalid_usage_is_unknown_not_coerced(value):
    result = normalize_usage(config(), {
        "prompt_tokens": value, "completion_tokens": value,
        "prompt_tokens_details": {"cached_tokens": value},
    })
    assert result["input_tokens"] is None
    assert result["output_tokens"] is None
    assert result["cache_read_tokens"] is None


def test_zero_cache_is_distinct_from_missing_and_input_not_double_counted():
    result = normalize_usage(config(), {
        "prompt_tokens": 100, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0},
    })
    assert result["input_tokens"] == 100
    assert result["cache_read_tokens"] == 0
    assert result["output_tokens"] == 0
    anthropic = normalize_usage(config("anthropic"), {"input_tokens": 10, "cache_read_input_tokens": None})
    assert anthropic["input_tokens"] is None
    assert anthropic["cache_read_tokens"] is None


@pytest.mark.parametrize("chunks,code,text", [
    ([event(delta("partial"))], "incomplete_stream", "partial"),
    ([event(delta("partial")), event(finish())], "incomplete_stream", "partial"),
    ([event(delta("partial")), b"data: [DONE]"], "incomplete_stream", "partial"),
    ([event(delta("partial")), b"data: invalid\n\n"], "invalid_response", "partial"),
    ([event(delta("partial")), event("[]")], "invalid_response", "partial"),
    ([event(delta("partial")), event(finish("length")), event("[DONE]")], "truncated", "partial"),
    ([event(delta("partial")), event(finish("content_filter")), event("[DONE]")], "invalid_finish", "partial"),
    ([event(delta("partial")), event("[DONE]")], "invalid_finish", "partial"),
    ([event(finish()), event("[DONE]")], "empty_response", ""),
])
def test_abnormal_end_is_failure_and_retains_partial_answer(chunks, code, text):
    result, _, stream = generate(chunks)
    assert result.status == "failed"
    assert result.error_code == code
    assert result.raw_output == text
    assert result.metrics.total_seconds is not None
    if code == "incomplete_stream":
        assert not result.metrics.stream_complete
    assert stream.closed


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_provider_error_before_content_has_no_ttft(protocol):
    result, _, _ = generate(
        [event({"type": "error", "error": {"message": "Bearer test-secret", "api_key": "test-secret"}})],
        model=config(protocol), api_key="test-secret",
    )
    assert result.status == "failed"
    assert result.metrics.first_content_seconds is None
    assert result.metrics.first_answer_seconds is None
    assert result.error_code == "provider_error"
    assert "test-secret" not in result.model_dump_json()
    reject_secret_fields(result.model_dump())


@pytest.mark.parametrize("status", [301, 400, 401, 429, 500])
def test_http_errors_no_ttft_no_retry_no_sensitive_response_body(status):
    result, _, _ = generate(status=status, payload={"error": "secret-response"}, api_key="session-secret")
    assert result.status == "failed"
    assert result.error_code == f"http_{status}"
    assert result.metrics.first_content_seconds is None
    assert result.metrics.first_answer_seconds is None
    assert "secret-response" not in result.model_dump_json()


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("sensitive"), httpx.ReadError("sensitive")])
@pytest.mark.parametrize("partial", [False, True])
def test_disconnect_and_timeout_preserve_evidence(exception, partial):
    chunks = [event(delta("partial"))] if partial else []
    result, _, stream = generate([*chunks, exception])
    assert result.status == ("failed" if partial else "uncertain")
    assert result.raw_output == ("partial" if partial else "")
    assert not result.metrics.stream_complete
    assert result.metrics.total_seconds is not None
    assert "sensitive" not in result.model_dump_json()
    assert stream.closed


def test_total_deadline_covers_slow_stream():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(delta("partial"))
            await asyncio.sleep(10)
            yield event("[DONE]")

    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=SlowStream(),
    ))
    result = asyncio.run(APIClient(transport=transport).generate(
        config(timeout_seconds=0.02), "", "",
    ))
    assert result.status == "failed"
    assert result.error_code == "timeout"
    assert result.raw_output == "partial"
    assert not result.metrics.stream_complete
    assert result.metrics.total_seconds < 2


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("observed", [False, True])
def test_cancellation_propagates_and_closes_response(protocol, observed):
    clock = Clock()
    if protocol == "openai":
        chunks = [
            event(delta("session-")), event(delta("secret partial")),
            event({"choices": [], "usage": {"prompt_tokens": 12}}),
        ]
    else:
        chunks = [
            event({"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
            event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "session-"}}),
            event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "secret partial"}}),
        ]
    stream = Stream([*(chunks if observed else []), asyncio.CancelledError()], clock)
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream,
    ))
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(APIClient(transport=transport, clock=clock).generate(
            config(protocol), "", "", api_key="session-secret",
        ))
    assert stream.closed
    result = getattr(caught.value, "result", None)
    assert result is not None
    assert result.status == "uncertain"
    assert result.error_code == "cancelled_in_flight"
    assert result.raw_output == ("[REDACTED] partial" if observed else "")
    assert result.metrics.input_tokens == (12 if observed else None)
    assert result.metrics.output_tokens is None
    assert result.metrics.total_seconds == (4 if observed else 1)
    assert (result.metrics.first_content_seconds is not None) is observed
    assert (result.metrics.first_answer_seconds is not None) is observed
    assert not result.metrics.stream_complete
    assert result.request_hash and len(result.request_hash) == 64
    assert result.protocol_mapping_version == MAPPING_VERSIONS[protocol]
    assert "session-" not in json.dumps(result.raw_response)


def test_sse_multiline_utf8_and_comments():
    data = 'data: {"choices": [\r\ndata: {"delta": {"content": "你好"}, "index": 0}\r\ndata: ]}\r\n\r\n'.encode()
    split = data.index("你".encode()) + 1
    result, _, _ = generate([
        b": ping\r\n\r\n", data[:split], data[split:],
        event(finish()), event("[DONE]"),
    ])
    assert result.status == "completed"
    assert result.raw_output == "你好"


def test_keys_are_ephemeral_env_fallback_and_request_hash_excludes_credentials(monkeypatch):
    monkeypatch.setenv("API_TEST_KEY", "environment-secret")
    model = config(api_key_env="API_TEST_KEY", capabilities={"stream": False})
    first, request, _ = generate(model=model, payload=answer(), api_key="session-secret")
    second, request2, _ = generate(model=model, payload=answer())
    assert request.headers["authorization"] == "Bearer session-secret"
    assert request2.headers["authorization"] == "Bearer environment-secret"
    assert first.request_hash == second.request_hash
    assert "secret" not in first.model_dump_json()
    assert "secret" not in second.model_dump_json()
    assert model.api_key_env == "API_TEST_KEY"


def test_redaction_also_handles_keys_split_across_text_deltas():
    result, _, _ = generate([
        event(delta("session-")), event(delta("secret")),
        event(finish()), event({"usage": {"prompt_tokens": 1, "headers": {"Authorization": "secret"}}}),
        event("[DONE]"),
    ], api_key="session-secret")
    assert result.raw_output == "[REDACTED]"
    assert "session-secret" not in result.model_dump_json()
    assert "session-" not in json.dumps(result.raw_response)
    reject_secret_fields(result.model_dump())


def test_attempt_identity_and_external_client_lifetime():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json=answer(),
        ))) as http:
            result = await APIClient(http).generate(
                config(capabilities={"stream": False}), "s", "u",
                run_id="run-7", task_id="C02", sample_index=2, attempt_index=3, purpose="control",
            )
            assert not http.is_closed
            return result

    result = asyncio.run(run())
    assert (result.run_id, result.task_id, result.sample_index, result.attempt_index, result.purpose) == (
        "run-7", "C02", 2, 3, "control",
    )


@pytest.mark.parametrize("protocol,payload", [
    ("openai", {"choices": {}}),
    ("openai", {"choices": [{}, {}]}),
    ("openai", {"choices": [None]}),
    ("openai", {"choices": [{"index": 1}]}),
    ("openai", {"choices": [{"delta": "wrong"}]}),
    ("anthropic", {"type": "message_start", "message": "wrong"}),
    ("anthropic", {"type": "message_delta", "delta": "wrong"}),
    ("anthropic", {"content": "wrong"}),
    ("anthropic", {"content": ["wrong"]}),
])
def test_malformed_protocol_fields_are_retained_failures(protocol, payload):
    result, _, _ = generate([event(payload)], model=config(protocol))
    assert result.status == "failed"
    assert result.error_code == "invalid_response"
    assert not result.metrics.stream_complete


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e9999"])
def test_nonfinite_json_never_breaks_attempt_serialization(number):
    result, _, _ = generate([event('{"usage": {"vendor": ' + number + '}}')])
    assert result.status == "failed"
    assert result.error_code == "invalid_response"
    result.model_dump_json()


def test_json_instead_of_sse_is_not_silently_downgraded():
    result, _, _ = generate(payload=answer())
    assert result.status == "failed"
    assert result.error_code == "invalid_response"
    assert result.metrics.first_content_seconds is None


@pytest.mark.parametrize("error,code", [
    (httpx.ConnectTimeout("private-info"), "timeout"),
    (httpx.ConnectError("private-info"), "transport_error"),
])
def test_connect_failure_is_not_a_completed_or_uncertain_attempt(error, code):
    def handler(request):
        raise error

    result = asyncio.run(APIClient(transport=httpx.MockTransport(handler)).generate(config(), "", ""))
    assert result.status == "failed"
    assert result.error_code == code
    assert "private-info" not in result.model_dump_json()


@pytest.mark.parametrize("stop,terminal,expected,code", [
    ("end_turn", True, "completed", None),
    ("stop_sequence", True, "completed", None),
    ("max_tokens", True, "failed", "truncated"),
    ("end_turn", False, "failed", "incomplete_stream"),
    ("stop", True, "failed", "invalid_finish"),
])
def test_anthropic_completion_markers(stop, terminal, expected, code):
    chunks = [
        event({"type": "content_block_start", "content_block": {"type": "text", "text": "answer"}}),
        event({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 3}}),
    ]
    if terminal:
        chunks.append(event({"type": "message_stop"}))
    result, _, _ = generate(chunks, model=config("anthropic"))
    assert result.status == expected
    assert result.error_code == code
    assert result.raw_output == "answer"
    assert result.metrics.output_tokens == 3
    assert result.metrics.first_content_seconds is not None


def test_raw_code_not_rewritten_by_credential_like_identifiers():
    code = "password = get_password()\nheaders = {'Authorization': 'Bearer example'}\n"
    result, _, _ = generate(payload=answer(code), model=config(capabilities={"stream": False}))
    assert result.status == "completed"
    assert result.raw_output == code


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_nonstream_truncation_is_a_failed_attempt(protocol):
    payload = answer("partial", reason="length") if protocol == "openai" else {
        "content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens",
    }
    result, _, _ = generate(payload=payload, model=config(protocol, capabilities={"stream": False}))
    assert result.status == "failed"
    assert result.error_code == "truncated"
    assert result.raw_output == "partial"
    assert result.metrics.first_content_seconds is None
    assert not result.metrics.stream_complete


def _page():
    from app.model_page import render
    from coding_eval.storage import AppPaths, Storage

    paths = AppPaths.from_env()
    render(Storage(paths.database), paths)


def _widget(widgets, label):
    return next(widget for widget in widgets if widget.label == label)


def test_model_page_configuration_credentials_probe_and_disable(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app import model_page
    from coding_eval.storage import Storage

    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        if body["stream"]:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream([
                event(delta("OK")), event(finish()), event({"usage": {"prompt_tokens": 2, "completion_tokens": 1}}),
                event("[DONE]"),
            ]))
        return httpx.Response(200, json=answer(usage={"prompt_tokens": 2, "completion_tokens": 1}))

    monkeypatch.setattr(model_page, "APIClient", lambda: APIClient(transport=httpx.MockTransport(handler)))
    page = AppTest.from_function(_page, default_timeout=20).run()
    assert not page.exception
    assert not requests
    _widget(page.text_input, "配置 ID（保存后不可修改）").input("my-model")
    _widget(page.text_input, "显示名称").input("My Gateway")
    _widget(page.text_input, "Base URL 或完整请求 URL").input("https://gateway.invalid")
    _widget(page.text_input, "精确 model ID").input("exact-model")
    _widget(page.text_input, "API key 环境变量名称（不是密钥值）").input("TEST_PAGE_API_KEY")
    _widget(page.selectbox, "OpenAI 输出限制字段").select("max_completion_tokens")
    _widget(page.button, "保存配置").click().run()
    assert not page.exception
    storage = Storage(tmp_path / "coding_eval.sqlite3")
    assert storage.get_object("model", "my-model")["api_key_env"] == "TEST_PAGE_API_KEY"
    assert storage.get_object("model", "my-model")["max_tokens_field"] == "max_completion_tokens"
    page.selectbox(key="model_editor_selection").select("my-model").run()
    assert not page.exception
    assert _widget(page.selectbox, "OpenAI 输出限制字段").value == "max_completion_tokens"
    _widget(page.text_input, "显示名称").input("Edited Gateway")
    _widget(page.button, "保存配置").click().run()
    versions = storage.list_objects("model")
    assert len(versions) == 2
    assert versions[0]["payload"]["display_name"] == "My Gateway"
    assert versions[1]["payload"]["display_name"] == "Edited Gateway"
    assert _widget(page.text_input, "配置 ID（保存后不可修改）").disabled
    _widget(page.text_input, "当前会话 API key").input("page-session-secret")
    _widget(page.button, "设置会话密钥").click().run()
    assert page.session_state["api_keys"] == {"my-model": "page-session-secret"}
    assert _widget(page.text_input, "当前会话 API key").value == ""
    assert not requests
    _widget(page.button, "运行连接探针").click().run()
    assert not requests
    _widget(page.checkbox, "我确认允许所选探针请求及可能产生的费用").check()
    _widget(page.button, "运行连接探针").click().run()
    assert not page.exception
    assert len(requests) == 2
    assert all(json.loads(request.content)["max_completion_tokens"] == 4096 for request in requests)
    assert all("max_tokens" not in json.loads(request.content) for request in requests)
    assert all(request.headers["authorization"] == "Bearer page-session-secret" for request in requests)
    records = storage.list_objects("probe")
    assert len(records) == 2
    assert all(row["payload"]["result"]["purpose"] == "probe" for row in records)
    assert storage.list_runs() == []
    assert "page-session-secret" not in json.dumps(storage.list_objects("model") + records)
    _widget(page.button, "清除会话密钥").click().run()
    assert page.session_state["api_keys"] == {}
    _widget(page.button, "禁用此模型").click().run()
    assert not page.exception
    assert storage.get_object("model", "my-model")["enabled"] is False
    assert _widget(page.button, "运行连接探针").disabled
