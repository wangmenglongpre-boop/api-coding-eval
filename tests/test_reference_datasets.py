"""Reference adaptations stay inert on the host; behavior runs only in Docker."""

import ast
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from coding_eval.api import APIClient
from coding_eval.datasets import (
    REFERENCE_PATH, export_task, import_task, list_tasks, load_builtin_tasks,
    load_reference_tasks, save_task, validation_issues,
)
from coding_eval.judges import extract_answer, render_answer, validate_task
from coding_eval.models import Dimension
from coding_eval.storage import AppPaths, Storage
from scripts.validate_reference_catalog import verify_receipts


@pytest.mark.parametrize("task", load_reference_tasks(), ids=lambda t: t.id)
def test_adaptation_provenance_contract_and_interchange(task):
    assert task.status == "draft" and not task.validation_evidence
    assert "Python 核心逻辑改编" in task.source and "https://github.com/" in task.source
    assert "不等同于原仓库" in task.description
    assert not validation_issues(task)
    assert {r.dimension for r in task.rubric} == set(Dimension)
    assert len(task.negative_files) == 2
    assert len({tuple(files.items()) for files in task.negative_files.values()}) == 2
    for files in [task.public_files, task.test_files, task.reference_files, *task.negative_files.values()]:
        for name, text in files.items():
            ast.parse(text, filename=f"{task.id}/{name}")
    for files in [task.reference_files, *task.negative_files.values()]:
        assert extract_answer(task, render_answer(task, files)) == files
    assert import_task(export_task(task)) == task


def test_reference_loading_is_fresh_separate_and_validates_membership(monkeypatch, tmp_path):
    assert [t.id for t in load_builtin_tasks()] == [f"C{i:02}" for i in range(1,11)]
    first = load_reference_tasks()
    first[0].public_files.clear()
    assert load_reference_tasks()[0].public_files
    assert [t.id for t in first] == [f"C{i:02}" for i in range(11,16)]
    values = json.loads(REFERENCE_PATH.read_text())
    values[1] = values[0]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(values))
    monkeypatch.setattr("coding_eval.datasets.REFERENCE_PATH", bad)
    with pytest.raises(ValueError, match="C11-C15"):
        load_reference_tasks()


def test_reference_page_addition_is_idempotent_preserves_originals_and_never_calls_api(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    storage = Storage(AppPaths.from_env().database)
    original = load_builtin_tasks()[0]
    save_task(storage, original)
    request = Mock(side_effect=AssertionError("dataset load must not request API"))
    monkeypatch.setattr(APIClient, "generate", request)
    page = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "app/main.py"), default_timeout=20,
    ).run()
    page.sidebar.radio[0].set_value("数据集管理").run()
    page.button(key="dataset_reference").click().run()
    assert not page.exception and len(list_tasks(storage)) == 6
    assert storage.get_object("task", original.id) == original.model_dump(mode="json")
    assert all(not t.ready for t in list_tasks(storage))
    page.button(key="dataset_reference").click().run()
    assert not page.exception and len(storage.list_objects("task")) == 6
    assert any("新增 0" in item.value for item in page.success)
    request.assert_not_called()


def test_page_imports_reference_catalog_with_pre_upgrade_dataset_module(tmp_path, monkeypatch):
    """An existing Streamlit worker may still hold the original dataset module."""
    import importlib
    import sys
    from coding_eval import datasets

    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.delattr(datasets, "load_reference_tasks")
    monkeypatch.delattr(datasets, "REFERENCE_PATH")
    # Force actual UI/catalog imports against the simulated cached old module.
    for name in ("app.dataset_page", "coding_eval.reference_datasets"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    page_module = importlib.import_module("app.dataset_page")
    assert len(page_module.load_reference_tasks()) == 5
    page = AppTest.from_string(
        "from app.dataset_page import render\n"
        "from coding_eval.storage import AppPaths, Storage\n"
        "paths = AppPaths.from_env()\n"
        "render(Storage(paths.database), paths)\n",
        default_timeout=20,
    ).run()
    assert not page.exception
    page.button(key="dataset_reference").click().run()
    assert not page.exception and not page.error
    assert len(list_tasks(Storage(AppPaths.from_env().database))) == 5


@pytest.mark.skipif(
    os.environ.get("CODING_EVAL_DOCKER_TESTS") != "1",
    reason="Embedded reference task behavior runs only in explicitly enabled Docker",
)
@pytest.mark.parametrize("task", load_reference_tasks(), ids=lambda t: t.id)
def test_reference_and_targeted_negatives_in_restricted_docker(task, tmp_path):
    paths = AppPaths(root=tmp_path, database=tmp_path / "db.sqlite", artifacts=tmp_path / "artifacts")
    paths.ensure()
    ready = validate_task(task, paths)
    assert ready.ready and ready.runtime.image.startswith("sha256:")
    assert ready.validated_content_hash == ready.validation_hash
    verify_receipts(ready)
    imported = import_task(export_task(ready))
    assert not imported.ready and not imported.validation_evidence
