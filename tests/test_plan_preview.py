"""Preview regressions; no real model requests or task execution."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from coding_eval import judges
from coding_eval.models import JudgeResult, RunPlan, ScoringConfig, Snapshot, TaskDefinition, Weight
from coding_eval.plan_preview import PlanTaskError, create_plan
from coding_eval.storage import AppPaths, Storage
from test_runtime_regressions import ready_tasks, model


def test_batch_scenario_weights_reuse_byte_identical_ready_snapshot(tmp_path):
    tasks, _ = ready_tasks(2)
    task = tasks[0]
    scoring = ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),))
    forbidden = Mock(side_effect=AssertionError("Ready scenario-only preview must not run Docker"))
    paths = AppPaths(tmp_path, tmp_path / "test.sqlite3", tmp_path / "artifacts")
    progress = Mock()
    plan = create_plan([model()], [task], scoring, paths, validator=forbidden,
                       progress=progress, repeats=1, max_requests=1)
    forbidden.assert_not_called()
    assert plan.task_snapshots[0] == Snapshot.capture(task)
    assert plan.task_snapshots[0].restore().validated_content_hash == task.validated_content_hash
    assert plan.scoring.scenario_weights != task.scoring.scenario_weights
    assert progress.call_args.args[:2] == (1, 1)


@pytest.mark.parametrize("condition", ["budget", "scenarios"])
def test_invalid_preview_fails_before_revalidating_changed_dimensions(tmp_path, condition):
    tasks, old = ready_tasks(2)
    scoring = ScoringConfig(
        dimension_weights=tuple(Weight(name=w.name, weight=.2) for w in old.dimension_weights),
        scenario_weights=old.scenario_weights if condition == "budget" else (Weight(name="C01", weight=1),),
    )
    validator = Mock(side_effect=AssertionError("Invalid input must fail before validation"))
    paths = AppPaths(tmp_path, tmp_path / "test.sqlite3", tmp_path / "artifacts")
    with pytest.raises(ValidationError, match="预算|场景"):
        create_plan([model()], tasks, scoring, paths, validator=validator, repeats=1,
                    max_requests=1 if condition == "budget" else 2)
    validator.assert_not_called()


def test_changed_dimensions_require_new_certificate_and_retain_fixed_image(tmp_path):
    tasks, old = ready_tasks()
    scoring = ScoringConfig(
        dimension_weights=tuple(Weight(name=w.name, weight=.2) for w in old.dimension_weights),
        scenario_weights=old.scenario_weights,
    )
    seen = []

    def validator(task, paths):
        seen.append(task)
        assert not task.ready and task.validated_content_hash is None
        value = task.model_dump(mode="json")
        value.update(status="ready", validated_content_hash=task.validation_hash,
                     validation_evidence=["SYNTHETIC_NOT_EXECUTED"])
        return TaskDefinition.model_validate(value)

    paths = AppPaths(tmp_path, tmp_path / "test.sqlite3", tmp_path / "artifacts")
    plan = create_plan([model()], tasks, scoring, paths, validator=validator, repeats=1, max_requests=1)
    assert len(seen) == 1
    assert plan.task_snapshots[0].restore().runtime.image == tasks[0].runtime.image
    assert plan.task_snapshots[0].version_hash != tasks[0].version_hash


def test_validation_error_keeps_task_identity_and_evidence_for_ui(tmp_path):
    from streamlit.testing.v1 import AppTest

    tasks, old = ready_tasks()
    scoring = old.model_copy(update={
        "dimension_weights": tuple(Weight(name=w.name, weight=.2) for w in old.dimension_weights),
    })
    failure = JudgeResult(
        attempt_id="fixture", task_version_hash=tasks[0].version_hash, judge_version="fixture",
        status="blocked", error_message="Docker daemon unavailable", artifact_paths=("fixture/judge.log",),
    )
    paths = AppPaths(tmp_path, tmp_path / "test.sqlite3", tmp_path / "artifacts")
    validator = Mock(side_effect=judges.TaskValidationError("Docker daemon unavailable", [failure]))
    with pytest.raises(PlanTaskError) as caught:
        create_plan([model()], tasks, scoring, paths, validator=validator, repeats=1, max_requests=1)
    assert caught.value.task_id == "C01" and caught.value.results == (failure,)
    page = AppTest.from_string(
        "import streamlit as st\nfrom app.run_page import _plan_error\n_plan_error(st.session_state['failure'])",
        default_timeout=10,
    )
    page.session_state["failure"] = caught.value
    page.run()
    assert not page.exception
    assert "C01" in page.error[0].value and "Docker daemon unavailable" in page.error[0].value
    assert any("fixture/judge.log" in str(item.value) for item in page.json)


def test_pydantic_errors_do_not_echo_invalid_sensitive_input():
    from streamlit.testing.v1 import AppTest

    try:
        Weight(name="security", weight="SENSITIVE_INVALID_VALUE")
    except ValidationError as exc:
        failure = exc
    page = AppTest.from_string(
        "import streamlit as st\nfrom app.run_page import _plan_error\n_plan_error(st.session_state['failure'])"
    )
    page.session_state["failure"] = failure
    page.run()
    assert not page.exception and page.error
    assert "SENSITIVE_INVALID_VALUE" not in page.error[0].value


def test_ready_page_preview_does_not_validate_or_call_authorized_control(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding_eval.api import APIClient

    tasks, _ = ready_tasks(2)
    paths = AppPaths(tmp_path, tmp_path / "coding_eval.sqlite3", tmp_path / "artifacts")
    storage = Storage(paths.database)
    config = model()
    storage.save_object("model", config.id, config.model_dump(mode="json"))
    for task in tasks:
        storage.save_object("task", task.id, task.model_dump(mode="json"))
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    validator = Mock(side_effect=AssertionError("Scenario-only preview must not revalidate"))
    request = Mock(side_effect=AssertionError("Manual preview must not call any model"))
    monkeypatch.setattr(judges, "validate_task", validator)
    monkeypatch.setattr(APIClient, "generate", request)
    page = AppTest.from_string(
        "from app.run_page import render\nfrom coding_eval.storage import AppPaths, Storage\n"
        "p=AppPaths.from_env()\nrender(Storage(p.database),p)"
    ).run()
    next(w for w in page.multiselect if w.label == "评测任务").set_value(["C01"]).run()
    next(w for w in page.checkbox if w.label.startswith("允许一次控制模型调用")).check()
    next(b for b in page.button if b.label == "生成并验证表单预览").click().run()
    assert not page.exception and not page.error
    plan = RunPlan.model_validate_json(page.session_state["_run_preview"])
    assert plan.task_snapshots[0] == Snapshot.capture(tasks[0])
    validator.assert_not_called()
    request.assert_not_called()


def test_main_upgrades_cached_run_page_without_losing_worker_registry(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from app import run_page

    registry = run_page.worker_registry()
    # Restore exported function identities after simulating the old cache.
    monkeypatch.setattr(run_page, "render", run_page.render)
    monkeypatch.setattr(run_page, "worker_registry", run_page.worker_registry)
    monkeypatch.delattr(run_page, "PLAN_PREVIEW_REVISION")
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    entry = Path(__file__).resolve().parents[1] / "app" / "main.py"
    page = AppTest.from_string(
        f"import runpy\nrunpy.run_path({str(entry)!r},run_name='__main__')"
    )
    page.query_params["page"] = "评测运行"
    page.run()
    assert not page.exception
    assert run_page.PLAN_PREVIEW_REVISION == "ready-snapshot-v1"
    assert run_page.worker_registry() is registry
