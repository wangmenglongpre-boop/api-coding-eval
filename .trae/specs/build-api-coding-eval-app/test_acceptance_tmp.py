"""Independent acceptance probes; all candidate/task source stays inert."""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from coding_eval import container_check, judges
from coding_eval.api import APIClient
from coding_eval.container_check import DockerSandbox, DockerUnavailable
from coding_eval.datasets import import_task, export_task, list_tasks, load_builtin_tasks
from coding_eval.models import (
    AttemptResult, CheckResult, JudgeResult, ModelConfig, RunStatus, ScoringConfig,
    Snapshot, TaskDefinition, Weight,
)
from coding_eval.reports import build_report
from coding_eval.runner import BatchRunner, create_plan
from coding_eval.storage import AppPaths, Storage

# Existing trusted fixture factories, not dataset or generated code.
from test_reports import make_plan as report_plan, evidence


IMAGE_ID = "sha256:" + "a" * 64


def make_plan(**options):
    plan = report_plan(**options)
    snapshots = []
    for snapshot in plan.task_snapshots:
        task = snapshot.restore()
        task = task.model_copy(update={
            "runtime": task.runtime.model_copy(update={"image": IMAGE_ID}),
        })
        task = task.model_copy(update={"validated_content_hash": task.validation_hash})
        snapshots.append(Snapshot.capture(task))
    return plan.model_copy(update={"task_snapshots": tuple(snapshots)})


@pytest.fixture(autouse=True)
def offline_sandbox(monkeypatch):
    # Exercise real formal preflight; only the Docker boundary is simulated.
    monkeypatch.setattr(container_check, "run_command", Mock(
        side_effect=AssertionError("Independent acceptance must not invoke Docker or host task code"),
    ))

    def factory(runtime):
        sandbox = Mock(spec=DockerSandbox)
        sandbox.prepare.return_value = runtime.image
        sandbox.execute.side_effect = AssertionError("Task execution is forbidden in offline acceptance")
        return sandbox

    monkeypatch.setattr(judges, "DockerSandbox", factory)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    paths = AppPaths.from_env()
    return Storage(paths.database), paths


def widget(elements, label):
    return next(item for item in elements if item.label == label)


def dataset_page():
    from app.dataset_page import render
    from coding_eval.storage import AppPaths, Storage
    paths = AppPaths.from_env()
    render(Storage(paths.database), paths)


def report_page():
    from app.report_page import render
    from coding_eval.storage import AppPaths, Storage
    paths = AppPaths.from_env()
    render(Storage(paths.database), paths)


def test_dataset_ui_seed_edit_copy_and_blocked_validation(env, monkeypatch):
    storage, paths = env
    sandbox = Mock()
    sandbox.prepare.side_effect = DockerUnavailable("acceptance-no-daemon")
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    page = AppTest.from_function(dataset_page, default_timeout=20).run()
    assert not page.exception
    page.button(key="dataset_seed").click().run()
    assert not page.exception
    assert len(list_tasks(storage)) == 10
    original = storage.get_object("task", "C01")
    revised = dict(original, title="Acceptance edited title")
    widget(page.checkbox, "打开 JSON 编辑（含判分端参考解与隐藏测试）").check().run()
    widget(page.text_area, "TaskDefinition JSON").input(json.dumps(revised))
    widget(page.button, "保存新草稿版本").click().run()
    assert not page.exception
    assert storage.get_object("task", "C01")["title"] == "Acceptance edited title"
    assert len([r for r in storage.list_objects("task") if r["object_id"] == "C01"]) == 2
    widget(page.text_input, "新任务 ID").input("acceptance-copy")
    widget(page.text_input, "目标场景 ID").input("custom")
    widget(page.button, "复制为草稿").click().run()
    assert not page.exception
    assert storage.get_object("task", "acceptance-copy")["scenario_id"] == "custom"
    page.selectbox(key="dataset_selected").select("C01").run()
    page.button(key="dataset_validate").click().run()
    assert not page.exception
    assert any("blocked" in item.value for item in page.error)
    assert storage.get_object("task", "C01")["status"] == "draft"
    sandbox.execute.assert_not_called()
    assert import_task(export_task(list_tasks(storage)[0])).status == "draft"


def test_cancelled_retry_is_not_formally_ranked(env):
    """One failed attempt is not a terminal sample when retries remain."""
    storage, paths = env
    plan = make_plan(max_retries=1, max_requests=2)
    storage.save_run(plan)
    calls = []

    class FailureAPI:
        async def generate(self, config, system, user, *, api_key=None, **metadata):
            calls.append(metadata)
            return AttemptResult(model_id=config.id, status="failed", **metadata)

    def cancel_after_failure(task, attempt, paths):
        engine.cancel(plan.id)
        return JudgeResult(attempt_id=attempt.id, task_version_hash=task.version_hash,
                           judge_version="acceptance", status="completed",
                           quality_score=0, all_required_passed=False)

    engine = BatchRunner(storage, paths, client=FailureAPI(), judge=cancel_after_failure)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.CANCELLED
    assert len(calls) == 1 and len(storage.list_attempts(plan.id)) == 1
    page = AppTest.from_function(report_page, default_timeout=20).run()
    page.button(key=f"build-report-{plan.id}").click().run()
    assert not page.exception
    report = storage.list_reports(plan.id)[0]
    assert report.status == "provisional", (
        report.status, report.payload["ranking"], storage.get_run_status(plan.id)
    )


def test_report_recomputes_dimension_scores_from_frozen_checks():
    plan = make_plan()
    attempts, results = evidence(plan)
    result = results[0]
    # A persisted judge accidentally reports all dimensions as 100 despite
    # every check failing. Neither a rank nor a 100-point score is valid.
    result = result.model_copy(update={
        "checks": tuple(c.model_copy(update={"passed": False}) for c in result.checks),
        "all_required_passed": False,
    })
    report = build_report(plan, attempts, [result], bootstrap_iterations=10)
    assert report.status == "invalid" or report.payload["models"]["alpha"]["quality_score"] == 0, (
        report.status, report.payload["models"]["alpha"]["quality_score"]
    )


def test_formal_preflight_blocks_before_requests_if_docker_unavailable(env, monkeypatch):
    storage, paths = env
    # Synthetic readiness fixture tests orchestration only, never a real score.
    plan = make_plan(max_retries=0, max_requests=1)
    storage.save_run(plan)
    sandbox = Mock()
    sandbox.prepare.side_effect = DockerUnavailable("acceptance-no-daemon")
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    calls = []

    class FakeAPI:
        async def generate(self, config, system, user, *, api_key=None, **metadata):
            calls.append(metadata)
            return AttemptResult(model_id=config.id, status="completed",
                                 raw_output="INERT SYNTHETIC TEXT", **metadata)

    try:
        asyncio.run(BatchRunner(storage, paths, client=FakeAPI()).run(plan.id))
    except (DockerUnavailable, ValueError):
        pass
    assert calls == [], {
        "requests": len(calls),
        "judges": [r["payload"]["status"] for r in storage.list_objects("judge")],
    }


def test_runtime_image_is_frozen_in_plan(env):
    _, paths = env
    source = make_plan()
    models = [s.restore() for s in source.model_snapshots]
    tasks = [s.restore() for s in source.task_snapshots]
    mutable_tasks = []
    for task in tasks:
        mutable = task.model_copy(update={
            "runtime": task.runtime.model_copy(update={"image": "python:3.11-slim"}),
        })
        mutable_tasks.append(mutable.model_copy(update={
            "validated_content_hash": mutable.validation_hash,
        }))
    with pytest.raises(ValueError, match="sha256"):
        create_plan(models, mutable_tasks, source.scoring, paths, repeats=1, max_requests=1)
    plan = create_plan(
        models, tasks, source.scoring, paths,
        repeats=1, max_requests=1,
    )
    assert all(
        snapshot.restore().runtime.image == IMAGE_ID
        for snapshot in plan.task_snapshots
    ), [s.restore().runtime.image for s in plan.task_snapshots]
    assert plan.task_snapshots == source.task_snapshots


def test_api_cancellation_retains_observed_text_usage_and_timing(env):
    storage, paths = env
    plan = make_plan(max_retries=0, max_requests=1)
    storage.save_run(plan)
    entered = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                'data: {"choices":[{"delta":{"content":"partial-answer"},'
                '"index":0}],"usage":{"prompt_tokens":12}}\n\n'
            ).encode()
            entered.set()
            await asyncio.Event().wait()

    api = APIClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Stream(),
    )))

    def fake_judge(task, attempt, paths):
        return JudgeResult(attempt_id=attempt.id, task_version_hash=task.version_hash,
                           judge_version="acceptance", status="blocked")

    async def scenario():
        engine = BatchRunner(storage, paths, client=api, judge=fake_judge)
        pending = asyncio.create_task(engine.run(plan.id))
        await asyncio.wait_for(entered.wait(), 2)
        engine.cancel(plan.id)
        assert await asyncio.wait_for(pending, 3) == RunStatus.CANCELLED

    asyncio.run(scenario())
    attempt = storage.list_attempts(plan.id)[0]
    assert attempt.status == "uncertain"
    assert attempt.raw_output == "partial-answer", attempt.model_dump()
    assert attempt.metrics.input_tokens == 12
    assert attempt.metrics.first_content_seconds is not None
    assert attempt.metrics.total_seconds is not None


def test_key_redaction_and_exact_metrics_through_all_exports(env):
    from coding_eval.reports import export_json, export_csv, export_html
    storage, paths = env
    plan = make_plan(max_retries=0, max_requests=1)
    storage.save_run(plan)
    secret = "acceptance-fake-key-not-real"
    body = (
        'data: {"choices":[{"delta":{"content":"acceptance-fake-"},"index":0}]}\n\n'
        'data: {"choices":[{"delta":{"content":"key-not-real"},"index":0}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}],'
        '"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n'
        'data: [DONE]\n\n'
    )
    api = APIClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body,
    )))

    def blocked(task, attempt, paths):
        return JudgeResult(attempt_id=attempt.id, task_version_hash=task.version_hash,
                           judge_version="acceptance", status="blocked")

    asyncio.run(BatchRunner(storage, paths, client=api, judge=blocked).run(
        plan.id, api_keys={"alpha": secret},
    ))
    attempts = storage.list_attempts(plan.id)
    assert attempts[0].raw_output == "[REDACTED]"
    assert attempts[0].metrics.input_tokens == 12
    assert attempts[0].metrics.cache_read_tokens is None
    report = build_report(plan, attempts, storage.list_objects("judge"), bootstrap_iterations=10)
    storage.save_report(report)
    for exporter in (export_json, export_csv, export_html):
        assert secret not in exporter(report)
    assert secret.encode() not in paths.database.read_bytes()


def test_report_ui_explanation_has_an_authorized_action(env):
    storage, paths = env
    plan = make_plan()
    storage.save_run(plan)
    attempts, results = evidence(plan)
    storage.save_report(build_report(plan, attempts, results, bootstrap_iterations=10))
    model = plan.model_snapshots[0].restore()
    storage.save_object("model", model.id, model.model_dump(mode="json"))
    page = AppTest.from_function(report_page, default_timeout=20).run()
    assert not page.exception
    widget(page.checkbox, "打开可选 Agent 解释").check().run()
    assert any("解释" in item.label for item in page.button), [item.value for item in page.info]


def test_api_dependency_declared():
    import tomllib
    root = Path(__file__).resolve().parents[3]
    with (root / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    assert any(item.startswith("httpx") for item in config["project"]["dependencies"])


def test_explanation_receives_actual_report_metric_structure(env):
    from coding_eval.agent import PlanningAgent
    storage, paths = env
    plan = make_plan()
    storage.save_run(plan)
    attempts, results = evidence(plan)
    report = build_report(plan, attempts, results, bootstrap_iterations=10)
    storage.save_report(report)
    inputs = []

    class Control:
        async def generate(self, config, system, user, *, api_key=None, **metadata):
            inputs.append(json.loads(user.split("\n\n")[0]))
            return AttemptResult(model_id=config.id, status="completed",
                                 raw_output="Synthetic explanation", **metadata)

    asyncio.run(PlanningAgent(storage, Control()).explain(
        report, plan.model_snapshots[0].restore(),
    ))
    metrics = inputs[0]["metrics"]["models"]["alpha"]["metrics"]
    assert metrics["total_seconds"].get("completed_attempts", {}).get("median") == 2, metrics
    assert inputs[0]["metrics"]["models"]["alpha"].get("quality_interval"), inputs[0]


def test_budget_exhaustion_and_resume_never_issue_extra_requests(env):
    storage, paths = env
    with pytest.raises(ValueError):
        make_plan(repeats=3, models=("alpha", "beta"), max_retries=1, max_requests=11)
    plan = make_plan(repeats=3, models=("alpha", "beta"), max_retries=1,
                     max_requests=12, concurrency=4)
    storage.save_run(plan)
    calls = []

    class FailureAPI:
        async def generate(self, config, system, user, *, api_key=None, **metadata):
            calls.append(metadata)
            await asyncio.sleep(0)
            return AttemptResult(model_id=config.id, status="failed", **metadata)

    def zero(task, attempt, paths):
        return JudgeResult(attempt_id=attempt.id, task_version_hash=task.version_hash,
                           judge_version="acceptance", status="completed",
                           quality_score=0, all_required_passed=False)

    engine = BatchRunner(storage, paths, client=FailureAPI(), judge=zero)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    assert len(calls) == len(storage.list_objects("request_intent")) == 12
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    assert len(calls) == 12
    report = build_report(plan, storage.list_attempts(plan.id), storage.list_objects("judge"),
                          bootstrap_iterations=10)
    assert report.status == "complete" and report.payload["coverage"]["scored"] == 6
    assert all(m["quality_score"] == 0 for m in report.payload["models"].values())


def run_page():
    from app.run_page import render
    from coding_eval.storage import AppPaths, Storage
    paths = AppPaths.from_env()
    render(Storage(paths.database), paths)


def test_natural_language_plan_ui_authorization_and_edit_validation(env, monkeypatch):
    from app import run_page as page_module
    from coding_eval.agent import PlanningAgent, PlanProposal
    storage, paths = env
    config = make_plan().model_snapshots[0].restore()
    task = load_builtin_tasks()[0]
    storage.save_object("model", config.id, config.model_dump(mode="json"))
    storage.save_object("task", task.id, task.model_dump(mode="json"))
    calls = []
    scoring = ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),))
    proposal = PlanProposal(model_ids=(config.id,), task_ids=(task.id,), scoring=scoring,
                            repeats=3, concurrency=1, max_requests=3, diagnostic=True)

    class Control:
        async def generate(self, model, system, user, *, api_key=None, **metadata):
            calls.append(user)
            return AttemptResult(model_id=model.id, status="completed",
                                 raw_output=proposal.model_dump_json(), **metadata)

    monkeypatch.setattr(page_module, "PlanningAgent", lambda db: PlanningAgent(db, Control()))
    page = AppTest.from_function(run_page, default_timeout=20).run()
    page.checkbox(key="run_diagnostic").check().run()
    widget(page.selectbox, "可选控制模型").select(config.id).run()
    widget(page.text_area, "可选自然语言评测意图").input("Compare this task three times")
    widget(page.button, "调用 agent 生成可编辑计划").click().run()
    assert not calls
    widget(page.checkbox, "允许一次控制模型调用（可能产生费用，与评测预算分开）").check()
    widget(page.button, "调用 agent 生成可编辑计划").click().run()
    assert not page.exception and len(calls) == 1
    assert storage.list_runs() == []
    editor = widget(page.text_area, "编辑 agent 计划 JSON（仍受上述授权限制）")
    invalid = json.loads(editor.value)
    invalid["max_requests"] = 4
    editor.input(json.dumps(invalid))
    widget(page.button, "验证编辑结果并生成预览").click().run()
    assert not page.exception and page.error
    assert storage.list_runs() == [] and len(calls) == 1
    widget(page.text_area, "编辑 agent 计划 JSON（仍受上述授权限制）").input(
        proposal.model_dump_json(),
    )
    widget(page.button, "验证编辑结果并生成预览").click().run()
    assert not page.exception
    assert widget(page.button, "开始冻结计划").disabled
    assert storage.list_runs() == [] and len(calls) == 1
    assert len(storage.list_objects("control")) == 1
    assert all(source not in calls[0] for source in task.test_files.values())


def test_main_navigation_render_only_not_integration_acceptance(env):
    root = Path(__file__).resolve().parents[3]
    page = AppTest.from_file(str(root / "app/main.py"), default_timeout=20).run()
    for name in ("模型配置", "数据集管理", "评测运行", "结果对比"):
        page.sidebar.radio[0].set_value(name).run()
        assert not page.exception
        assert any(title.value == name for title in page.title)
