"""Credential persistence and integration without accessing real keys or APIs."""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from coding_eval import credentials
from coding_eval.agent import PlanningAgent
from coding_eval.api import APIClient
from coding_eval.credentials import CredentialError, CredentialStore, resolve_credential
from coding_eval.datasets import load_builtin_tasks
from coding_eval.models import JudgeResult, ModelConfig, RunStatus, ScoringConfig, Weight
from coding_eval.reports import build_report, export_json
from coding_eval.runner import BatchRunner, create_plan
from coding_eval.storage import AppPaths, Storage


def model(**changes):
    return ModelConfig(**{
        "id": "persistent", "display_name": "持久密钥测试", "protocol": "openai",
        "url": "https://api.example.com/v1", "model_id": "exact",
        "capabilities": {"stream": False}, **changes,
    })


@pytest.fixture
def backend(monkeypatch):
    values = {}
    fake = Mock()
    fake.get_password.side_effect = lambda service, account: values.get((service, account))
    fake.set_password.side_effect = lambda service, account, value: values.__setitem__((service, account), value)
    fake.delete_password.side_effect = lambda service, account: values.pop((service, account))
    monkeypatch.setattr(credentials, "_secure_backend", lambda: fake)
    return fake


def test_restart_precedence_delete_and_destination_binding(backend, monkeypatch):
    config = model(api_key_env="TEST_PERSISTENT_KEY")
    monkeypatch.setenv("TEST_PERSISTENT_KEY", "environment-value")
    assert resolve_credential(config) == "environment-value"
    CredentialStore().save(config, "saved-value")
    assert resolve_credential(config) == "saved-value"
    assert resolve_credential(config, "session-value") == "session-value"
    assert CredentialStore().get(config.model_copy(update={"parameters": {"temperature": 0.2}})) == "saved-value"
    for changed in (
        config.model_copy(update={"url": "https://other.example.com/v1"}),
        config.model_copy(update={"url": "https://api.example.com/other"}),
        config.model_copy(update={"id": "different"}),
        config.model_copy(update={"protocol": "anthropic"}),
    ):
        assert CredentialStore().get(changed) is None
    CredentialStore().delete(config)
    CredentialStore().delete(config)
    assert resolve_credential(config) == "environment-value"


def test_unsupported_backend_does_not_save_plaintext(monkeypatch, tmp_path, native_secure_backend):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    monkeypatch.setattr(credentials, "_secure_backend", native_secure_backend)
    with pytest.raises(CredentialError):
        CredentialStore().save(model(), "never-on-disk")
    assert CredentialStore().get(model()) is None
    assert list(tmp_path.iterdir()) == []


def test_backend_errors_never_echo_secret(backend):
    backend.set_password.side_effect = RuntimeError("secret-in-error")
    with pytest.raises(CredentialError) as error:
        CredentialStore().save(model(), "secret-in-error")
    assert "secret-in-error" not in str(error.value)
    backend.get_password.side_effect = RuntimeError("secret-in-error")
    with pytest.raises(CredentialError) as error:
        resolve_credential(model())
    assert "secret-in-error" not in str(error.value)


def test_missing_credentials_block_before_any_batch_dispatch(tmp_path):
    paths = AppPaths(tmp_path, tmp_path / "eval.sqlite3", tmp_path / "artifacts")
    storage = Storage(paths.database)
    plan = create_plan(
        [model(url="http://127.0.0.1:8000/v1"), model(id="missing")],
        load_builtin_tasks()[:1], ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),)),
        paths, diagnostic=True, repeats=1, max_requests=2,
    )
    storage.save_run(plan)
    api = Mock()
    with pytest.raises(CredentialError, match="missing"):
        asyncio.run(BatchRunner(storage, paths, client=api).run(plan.id))
    assert storage.list_objects("request_intent") == []
    assert storage.list_attempts(plan.id) == []
    api.generate.assert_not_called()
    assert storage.get_run_status(plan.id) == RunStatus.QUEUED


def test_saved_key_flows_through_api_batch_control_and_export(backend, tmp_path):
    config = model()
    secret = "fake-persistent-secret"
    CredentialStore().save(config, secret)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer " + secret
        return httpx.Response(200, json={
            "choices": [{"message": {"content": secret}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })

    api = APIClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(api.generate(config, "system", "user"))
    assert result.raw_output == "[REDACTED]"
    paths = AppPaths(tmp_path, tmp_path / "eval.sqlite3", tmp_path / "artifacts")
    storage = Storage(paths.database)
    task = load_builtin_tasks()[0]
    plan = create_plan(
        [config], [task], ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),)),
        paths, diagnostic=True, repeats=1, max_requests=1,
    )
    storage.save_run(plan)

    def judge(task, attempt, paths):
        return JudgeResult(
            attempt_id=attempt.id, task_version_hash=task.version_hash, judge_version="fixture",
            status="completed", quality_score=0, all_required_passed=False,
        )

    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=judge).run(plan.id)) == RunStatus.COMPLETED
    control = asyncio.run(PlanningAgent(storage, api)._call(
        config, task, api_key=None, action="explain", context_hash="fixture",
    ))
    assert control.raw_output == "[REDACTED]"
    assert len(seen) == 3
    report = build_report(
        plan, storage.list_attempts(plan.id), storage.list_objects("judge"),
        run_status=storage.get_run_status(plan.id),
    )
    assert secret not in export_json(report)
    assert secret not in json.dumps(storage.list_objects("control"))
    assert secret.encode() not in paths.database.read_bytes()


def widget(elements, label):
    return next(item for item in elements if item.label == label)


def test_page_saves_existing_session_recovers_new_session_and_deletes(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    paths = AppPaths.from_env()
    paths.ensure()
    storage = Storage(paths.database)
    config = model()
    storage.save_object("model", config.id, config.model_dump(mode="json"))
    entry = str(Path(__file__).resolve().parents[1] / "app" / "main.py")
    page = AppTest.from_file(entry).run()
    page.selectbox(key="model_editor_selection").select(config.id).run()
    widget(page.text_input, "当前会话 API key").input("ui-saved-secret")
    widget(page.button, "设置会话密钥").click().run()
    widget(page.button, "保存密钥到系统钥匙串").click().run()
    assert not page.exception
    assert page.session_state["api_keys"] == {}
    assert widget(page.text_input, "当前会话 API key").value == ""
    # A fresh Streamlit session has none of the original widget/session state.
    fresh = AppTest.from_file(entry).run()
    fresh.selectbox(key="model_editor_selection").select(config.id).run()
    assert not fresh.exception
    assert fresh.session_state["api_keys"] == {}
    assert any("系统钥匙串已保存" in item.value for item in fresh.success)
    assert "ui-saved-secret" not in json.dumps(storage.list_objects("model"))
    # Reject pasting the persisted secret into ordinary configuration fields.
    widget(fresh.text_input, "显示名称").input("ui-saved-secret")
    widget(fresh.button, "保存配置").click().run()
    assert fresh.error
    assert storage.get_object("model", config.id)["display_name"] == config.display_name
    widget(fresh.button, "删除系统钥匙串密钥").click().run()
    assert not fresh.exception
    assert CredentialStore().get(config) is None


def test_native_selector_ignores_configurable_plaintext_fallback(monkeypatch, native_secure_backend):
    import keyring
    import keyring.backends.macOS

    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    fake = Mock()
    monkeypatch.setattr(keyring.backends.macOS, "Keyring", lambda: fake)
    fallback = Mock(side_effect=AssertionError("Do not use configurable backends"))
    monkeypatch.setattr(keyring, "get_keyring", fallback)
    assert native_secure_backend() is fake
    fallback.assert_not_called()


def test_api_missing_credential_returns_configuration_error_without_http():
    handler = Mock(side_effect=AssertionError("Missing key must not send HTTP"))
    api = APIClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(api.generate(model(), "system", "user"))
    assert result.error_code == "configuration_error"
    handler.assert_not_called()


def test_session_key_not_reused_after_destination_change(tmp_path, monkeypatch):
    from app.credentials import session_key
    from app import credentials as ui_credentials

    state = {"api_keys": {"persistent": "fake-scoped-key"}}
    monkeypatch.setattr(ui_credentials.st, "session_state", state)
    assert session_key(model()) == "fake-scoped-key"
    assert session_key(model(url="https://other.example.com/v1")) is None


def test_save_failure_retains_session_input_for_retry(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    paths = AppPaths.from_env()
    paths.ensure()
    storage = Storage(paths.database)
    config = model()
    storage.save_object("model", config.id, config.model_dump(mode="json"))
    backend.set_password.side_effect = RuntimeError("do-not-echo-this-secret")
    page = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "app" / "main.py"), default_timeout=10,
    ).run()
    page.selectbox(key="model_editor_selection").select(config.id).run()
    widget(page.text_input, "当前会话 API key").input("do-not-echo-this-secret")
    widget(page.button, "保存密钥到系统钥匙串").click().run()
    assert not page.exception
    assert page.session_state["api_keys"][config.id] == "do-not-echo-this-secret"
    assert page.error
    assert all("do-not-echo-this-secret" not in item.value for item in page.error)
    assert CredentialStore().get(config) is None
