"""真实主入口的离线页面集成；API 和容器结果均为测试夹具。"""

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from app import dataset_page, main, model_page, report_page, run_page
from coding_eval import judges, runner
from coding_eval.api import APIClient
from coding_eval.container_check import ContainerResult, DockerSandbox, DockerUnavailable
from coding_eval.datasets import list_tasks, load_builtin_tasks, save_task
from coding_eval.judges import render_answer
from coding_eval.models import ModelConfig, RunPlan, RunStatus
from coding_eval.reports import export_csv, export_html, export_json
from coding_eval.runner import WorkerRegistry, latest_models
from coding_eval.storage import AppPaths, Storage

ENTRY = Path(__file__).resolve().parents[1] / "app" / "main.py"


def widget(elements, label):
    return next(element for element in elements if element.label == label)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    paths = AppPaths.from_env()
    paths.ensure()
    return Storage(paths.database), paths


def test_main_routes_to_existing_renderers():
    assert main.render_models is model_page.render
    assert main.render_datasets is dataset_page.render
    assert main.render_runs is run_page.render
    assert main.render_reports is report_page.render


def test_main_navigation_never_dispatches_without_authorization(env, monkeypatch):
    request = Mock(side_effect=AssertionError("导航不允许发起请求"))
    monkeypatch.setattr(APIClient, "generate", request)
    page = AppTest.from_file(str(ENTRY), default_timeout=20).run()
    for title in main.PAGE_TITLES:
        page.sidebar.radio[0].set_value(title).run()
        assert not page.exception
        assert page.title[0].value == title
        assert not any("模块待接入" in item.value for item in page.info)
    request.assert_not_called()
    assert env[0].list_runs() == []


def test_main_model_disable_then_enable_reuses_history(env):
    storage, _ = env
    page = AppTest.from_file(str(ENTRY), default_timeout=20).run()
    for label, value in (
        ("配置 ID（保存后不可修改）", "gateway"), ("显示名称", "测试网关"),
        ("Base URL 或完整请求 URL", "https://gateway.invalid/v1"),
        ("精确 model ID", "exact-model"),
    ):
        widget(page.text_input, label).input(value)
    widget(page.button, "保存配置").click().run()
    assert not page.exception
    first = storage.get_object("model", "gateway")
    assert first is not None
    page.selectbox(key="model_editor_selection").select("gateway").run()
    widget(page.button, "禁用此模型").click().run()
    assert not page.exception
    assert not storage.get_object("model", "gateway")["enabled"]
    assert widget(page.button, "运行连接探针").disabled
    widget(page.checkbox, "启用此模型").check()
    widget(page.button, "保存配置").click().run()
    assert not page.exception
    assert not page.error
    assert storage.get_object("model", "gateway") == first
    assert len(storage.list_objects("model")) == 2
    assert latest_models(storage)[0].enabled
    assert not widget(page.button, "运行连接探针").disabled
    assert storage.list_objects("probe") == []


def test_main_seed_loading_is_idempotent_and_docker_blocked_stays_draft(env, monkeypatch):
    storage, _ = env
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.side_effect = DockerUnavailable("测试夹具：daemon unavailable")
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    page = AppTest.from_file(str(ENTRY), default_timeout=20).run()
    page.sidebar.radio[0].set_value("数据集管理").run()
    page.button(key="dataset_seed").click().run()
    assert not page.exception
    assert len(list_tasks(storage)) == 10
    assert all(not task.ready for task in list_tasks(storage))
    page.button(key="dataset_seed").click().run()
    assert len(storage.list_objects("task")) == 10
    page.button(key="dataset_validate").click().run()
    assert not page.exception
    assert any("阻塞" in item.value for item in page.error)
    assert all(not task.ready for task in list_tasks(storage))
    sandbox.prepare.assert_called_once()
    sandbox.execute.assert_not_called()
    assert len(page.get("download_button")) == 2


def test_main_mock_protocols_to_judge_report_reweight_and_exports(env, monkeypatch):
    storage, paths = env
    task = load_builtin_tasks()[0]
    save_task(storage, task)
    for protocol in ("openai", "anthropic"):
        config = ModelConfig(
            id=protocol, display_name=protocol, protocol=protocol,
            url="https://mock.invalid/v1", model_id=protocol,
            capabilities={"stream": False},
        )
        storage.save_object("model", config.id, config.model_dump(mode="json"))
    calls = []
    answer = render_answer(task, task.reference_files)

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert task.system_prompt in request.content.decode()
        assert "test_files" not in request.content.decode()
        assert "reference_files" not in request.content.decode()
        if body["model"] == "openai":
            return httpx.Response(200, json={
                "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        return httpx.Response(200, json={
            "type": "message", "content": [{"type": "text", "text": answer}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    monkeypatch.setattr(runner, "APIClient", lambda: APIClient(transport=httpx.MockTransport(handler)))
    # 仅模拟容器边界；使用真实编排、提取、判分和报告逻辑，不执行题目代码。
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.return_value = "sha256:" + "1" * 64
    sandbox.execute.return_value = ContainerResult("pass", "离线合成证据", "")
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    registry = WorkerRegistry()
    monkeypatch.setattr(run_page, "worker_registry", lambda: registry)
    page = AppTest.from_file(str(ENTRY), default_timeout=20).run()
    page.sidebar.radio[0].set_value("评测运行").run()
    page.checkbox(key="run_diagnostic").check().run()
    widget(page.number_input, "每题重复次数").set_value(1)
    widget(page.button, "生成并验证表单预览").click().run()
    assert not page.exception
    frozen = RunPlan.model_validate_json(page.session_state["_run_preview"])
    assert frozen.expected_requests == 2
    assert not calls
    assert storage.list_runs() == []
    assert widget(page.button, "开始冻结计划").disabled
    page.checkbox(key="run_cost_confirm_" + frozen.id).check().run()
    try:
        widget(page.button, "开始冻结计划").click().run()
        registry.join(storage, frozen.id, timeout=10)
        assert not registry.active(storage, frozen.id)
    finally:
        if registry.active(storage, frozen.id):
            registry.cancel(storage, paths, frozen.id)
            registry.join(storage, frozen.id, timeout=10)
    assert not page.exception
    assert len(calls) == 2
    assert storage.get_run_status(frozen.id) == RunStatus.COMPLETED
    assert len(storage.list_attempts(frozen.id)) == len(storage.list_objects("judge")) == 2
    assert sandbox.execute.call_count == 2 * (1 + len(task.rubric))

    page.sidebar.radio[0].set_value("结果对比").run()
    page.button(key="build-report-" + frozen.id).click().run()
    assert not page.exception
    parent = storage.list_reports(frozen.id)[0]
    assert parent.status == "invalid"
    assert parent.payload["ranking"] == []
    assert parent.payload["coverage"]["coverage"] == 1
    assert len(page.get("download_button")) == 3
    assert len(json.loads(export_json(parent))["payload"]["attempts"]) == 2
    assert "openai" in export_csv(parent)
    assert "anthropic" in export_html(parent)
    for attempt in storage.list_attempts(frozen.id):
        assert attempt.metrics.first_content_seconds is None
        assert attempt.metrics.input_tokens == 10
        assert attempt.metrics.cache_read_tokens is None
    scoring = parent.scoring.model_dump(mode="json")
    for weight in scoring["dimension_weights"]:
        weight["weight"] = 0.2
    widget(page.text_area, "ScoringConfig JSON").input(json.dumps(scoring))
    widget(page.button, "验证权重并保存事后分析新版本").click().run()
    assert not page.exception
    versions = storage.list_reports(frozen.id)
    assert len(versions) == 2
    assert versions[0] == parent
    assert versions[1].parent_report_id == parent.id
    assert versions[1].post_hoc
    assert len(calls) == 2
    assert storage.list_objects("control") == []
