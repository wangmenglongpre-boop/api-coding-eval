"""产品交互集成：大响应惰性渲染、版本切换、导航和授权保留。"""

from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from app import main, report_page, run_page
from coding_eval.api import APIClient
from coding_eval.models import RunStatus
from coding_eval.storage import Storage
from test_reports import evidence, make_plan, report_for

ENTRY = Path(__file__).resolve().parents[1] / "app/main.py"


@pytest.fixture
def product(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    storage = Storage(tmp_path / "coding_eval.sqlite3")
    plan = make_plan(models=("alpha", "beta"))
    storage.save_run(plan)
    for snapshot in (*plan.model_snapshots, *plan.task_snapshots):
        kind = "model" if snapshot in plan.model_snapshots else "task"
        storage.save_object(kind, snapshot.object_id, snapshot.restore().model_dump(mode="json"))
    attempts, judges = evidence(plan)
    attempts = [a.model_copy(update={
        "raw_output": "FULL_ANSWER_MARKER\n" + "answer" * 2000,
        "raw_response": {"events": ["RAW_STREAM_MARKER"] * 20000},
    }) for a in attempts]
    for a in attempts:
        storage.save_attempt(a)
    for j in judges:
        storage.save_object("judge", j.attempt_id, j.model_dump(mode="json"))
    storage.transition_run(plan.id, RunStatus.QUEUED, RunStatus.RUNNING)
    storage.transition_run(plan.id, RunStatus.RUNNING, RunStatus.COMPLETED)
    report = report_for(plan, attempts, judges, run_status="completed")
    storage.save_report(report)
    return storage, plan, report


def page_at(title):
    page = AppTest.from_file(str(ENTRY), default_timeout=20)
    page.query_params["page"] = title
    return page.run()


def rendered_bytes(page):
    return sum(n.proto.ByteSize() for n in page if getattr(n, "proto", None))


def test_default_report_never_exports_or_renders_streams(product, monkeypatch):
    storage, plan, report = product
    exporters = [Mock(side_effect=AssertionError("普通重跑不能生成导出")) for _ in range(3)]
    for name, exporter in zip(("export_json", "export_csv", "export_html"), exporters):
        monkeypatch.setattr(report_page, name, exporter)
    page = page_at("结果对比")
    assert not page.exception
    assert report_page.export_json is exporters[0]
    assert not any("FULL_ANSWER_MARKER" in c.value or "RAW_STREAM_MARKER" in c.value for c in page.code)
    assert rendered_bytes(page) < 150_000
    assert len(page.get("download_button")) == 3
    page.radio(key=f"report-section-{report.id}").set_value("详细证据").run()
    assert not page.exception and len(page.json) > 0
    for exporter in exporters:
        exporter.assert_not_called()
    assert len(storage.list_reports(plan.id)) == 1


def test_answers_load_only_on_selection_and_raw_response_on_request(product):
    _, _, report = product
    page = page_at("结果对比")
    page.radio(key=f"report-section-{report.id}").set_value("答案对比").run()
    assert not page.exception
    assert any("FULL_ANSWER_MARKER" in c.value for c in page.code)
    assert not any("RAW_STREAM_MARKER" in c.value for c in page.code)
    next(c for c in page.checkbox if c.label == "加载原始响应").check().run()
    assert not page.exception
    assert any("RAW_STREAM_MARKER" in c.value for c in page.code)


def test_run_monitor_summary_and_explicit_full_record(product, monkeypatch):
    storage, plan, _ = product
    request = Mock(side_effect=AssertionError("监控不能调用模型"))
    monkeypatch.setattr(APIClient, "generate", request)
    page = page_at("评测运行")
    assert not page.exception
    assert rendered_bytes(page) < 100_000
    assert not any("RAW_STREAM_MARKER" in str(j.value) for j in page.json)
    page.checkbox(key=f"run-details-{plan.id}").check().run()
    assert not page.exception and any("FULL_ANSWER_MARKER" in c.value for c in page.code)
    selected = page.selectbox(key=f"attempt-{plan.id}").value
    page.checkbox(key=f"raw-{selected}").check().run()
    assert not page.exception and any("RAW_STREAM_MARKER" in str(j.value) for j in page.json)
    request.assert_not_called()
    assert len(storage.list_attempts(plan.id)) == 2


def test_navigation_syncs_top_sidebar_and_url_without_requests(product, monkeypatch):
    request = Mock(side_effect=AssertionError("导航不能调用模型"))
    monkeypatch.setattr(APIClient, "generate", request)
    page = page_at("结果对比")
    for title in ("数据集管理", "模型配置", "评测运行", "结果对比"):
        page.radio(key="top-page").set_value(title).run()
        assert not page.exception and page.title[0].value == title
        assert page.sidebar.radio[0].value == title
        assert page.query_params["page"] == title
    page.sidebar.radio[0].set_value("数据集管理").run()
    assert page.radio(key="top-page").value == "数据集管理"
    request.assert_not_called()


def test_hot_ui_upgrade_preserves_worker_resource(product, monkeypatch):
    registry = run_page.worker_registry()
    monkeypatch.setattr(run_page, "_PRODUCT_LOADED_MTIME", -1, raising=False)
    monkeypatch.setattr(run_page, "render", run_page.render)
    monkeypatch.setattr(run_page, "worker_registry", run_page.worker_registry)
    page = page_at("评测运行")
    assert not page.exception
    assert run_page.worker_registry() is registry


def test_hot_ui_upgrade_uses_registered_module_after_package_alias_changes(product, monkeypatch):
    import app

    stale = ModuleType("app.dataset_page")
    stale.__file__ = str(ENTRY.parent / "dataset_page.py")
    monkeypatch.setattr(app, "dataset_page", stale)
    page = page_at("数据集管理")
    assert not page.exception
    assert page.button(key="dataset_validate")
