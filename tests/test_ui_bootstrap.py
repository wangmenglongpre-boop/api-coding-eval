"""Reproduce cached-export ImportError, not merely a clean-process import."""

from importlib import import_module
from pathlib import Path
import sys
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from coding_eval.api import APIClient
from coding_eval.ui_bootstrap import load_ui_modules


def test_refresh_dependency_before_fresh_consumer_import(monkeypatch):
    from app import run_page
    from coding_eval import models, runner

    views = import_module("coding_eval.report_views")
    # Simulate the exact running-process state: cached old dependency, consumers
    # removed after their import failed. Stamp alone must not hide a missing export.
    monkeypatch.delattr(views, "difficulty_rows")
    monkeypatch.setattr(views, "_PRODUCT_LOADED_MTIME",
                        Path(views.__file__).stat().st_mtime_ns, raising=False)
    monkeypatch.delitem(sys.modules, "app.report_page", raising=False)
    monkeypatch.delitem(sys.modules, "app.report_charts", raising=False)
    old_registry = run_page.worker_registry
    old_plan, old_runner = models.RunPlan, runner.BatchRunner
    loaded = load_ui_modules()
    assert callable(loaded["coding_eval.report_views"].difficulty_rows)
    assert loaded["app.report_charts"].difficulty_rows is views.difficulty_rows
    assert loaded["app.report_page"].render_overview is loaded["app.report_charts"].render_overview
    assert run_page.worker_registry is old_registry
    assert models.RunPlan is old_plan and runner.BatchRunner is old_runner


def test_all_main_pages_render_after_cached_dependency_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    forbidden = Mock(side_effect=AssertionError("Page recovery must not call any model"))
    monkeypatch.setattr(APIClient, "generate", forbidden)
    views = import_module("coding_eval.report_views")
    monkeypatch.delattr(views, "difficulty_rows")
    monkeypatch.delitem(sys.modules, "app.report_page", raising=False)
    monkeypatch.delitem(sys.modules, "app.report_charts", raising=False)
    entry = Path(__file__).resolve().parents[1] / "app" / "main.py"
    page = AppTest.from_file(str(entry), default_timeout=20).run()
    for title in ("模型配置", "数据集管理", "评测运行", "结果对比"):
        page.sidebar.radio[0].set_value(title).run()
        assert not page.exception
        assert page.title[0].value == title
    forbidden.assert_not_called()
