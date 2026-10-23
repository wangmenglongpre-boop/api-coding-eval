"""Offline batches with real SQLite, process crashes and controlled interleaving."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from coding_eval import judges, runner
from coding_eval.api import APIClient
from coding_eval.datasets import load_builtin_tasks
from coding_eval.models import (
    AttemptMetrics, AttemptResult, JudgeResult, ModelConfig, RunPlan, RunStatus,
    ScoringConfig, Snapshot, TaskDefinition, Weight,
)
from coding_eval.runner import (
    BatchRunner, RunBusyError, WorkerRegistry, create_plan, intent_id, latest_models,
    sample_schedule, task_prompts,
)
from coding_eval.storage import AppPaths, Storage


@pytest.fixture
def env(tmp_path):
    paths = AppPaths(tmp_path, tmp_path / "eval.sqlite3", tmp_path / "artifacts")
    return Storage(paths.database), paths


def model(identifier="model", **kwargs):
    return ModelConfig(id=identifier, display_name=identifier, protocol="openai",
                       url="https://provider.invalid/v1", model_id="exact", **kwargs)


def plan_for(paths, *, model_count=1, task_count=1, **overrides):
    tasks = load_builtin_tasks()[:task_count]
    scoring = ScoringConfig(scenario_weights=tuple(
        Weight(name=t.scenario_id, weight=1 / task_count) for t in tasks
    ))
    options = dict(repeats=2, concurrency=2, max_retries=0, max_requests=100, diagnostic=True)
    options.update(overrides)
    return create_plan([model(f"model{i}") for i in range(model_count)], tasks, scoring, paths, **options)


def judged(task, attempt, paths):
    return JudgeResult(
        attempt_id=attempt.id, task_version_hash=task.version_hash, judge_version="test",
        status="completed", quality_score=100 if attempt.status == "completed" else 0,
        all_required_passed=attempt.status == "completed",
    )


class FakeAPI:
    def __init__(self, statuses=("completed",), hook=None):
        self.calls = []
        self.statuses = statuses
        self.hook = hook

    async def generate(self, config, system_prompt, user_prompt, *, api_key=None, **metadata):
        self.calls.append((config, system_prompt, user_prompt, metadata))
        if self.hook:
            await self.hook(config, metadata)
        return AttemptResult(
            model_id=config.id, **metadata,
            status=self.statuses[min(metadata["attempt_index"], len(self.statuses) - 1)],
            raw_output="test output", metrics=AttemptMetrics(total_seconds=0.01),
        )


def test_latest_models_uses_latest_even_when_disabled(env):
    storage, _ = env
    first = model()
    second = model(enabled=False)
    storage.save_object("model", first.id, first.model_dump(mode="json"))
    storage.save_object("model", second.id, second.model_dump(mode="json"))
    assert latest_models(storage) == [second]


def test_public_prompts_are_exact_and_do_not_include_private_fields():
    task = load_builtin_tasks()[0]
    value = task.model_dump(mode="json")
    value.update(system_prompt="exact system", requirements="exact requirement",
                 public_files={"b.py": "public b", "a.py": "public a"},
                 reference_files={"solution.py": "SECRET_REFERENCE"},
                 test_files={"checks.py": "SECRET_TEST"})
    value["output_format"]["instructions"] = "exact output instructions"
    task = TaskDefinition.model_validate(value)
    assert task_prompts(task) == (
        "exact system", "exact requirement\n\nFile: a.py\npublic a\n\nFile: b.py\npublic b\n\nexact output instructions",
    )


def test_plan_freezes_models_tasks_order_scoring_and_seed(env):
    storage, paths = env
    plan = plan_for(paths, model_count=2, task_count=2, repeats=3, random_seed=53)
    storage.save_run(plan)
    restored = storage.get_run(plan.id)
    assert restored == plan
    assert sample_schedule(restored) == sample_schedule(plan)
    assert len(set(sample_schedule(plan))) == 12
    other = RunPlan.model_validate({**plan.model_dump(), "random_seed": 99})
    assert sample_schedule(other) != sample_schedule(plan)
    assert all(s.restore().scoring == plan.scoring for s in plan.task_snapshots)
    model_value = plan.model_snapshots[0].restore()
    model_value.parameters["temperature"] = 0.7
    assert plan.model_snapshots[0].restore().parameters == {}


@pytest.mark.parametrize("changes", [{"max_requests": 1}, {"concurrency": 0}, {"repeats": 0}])
def test_invalid_plan_is_rejected_before_saving(env, changes):
    _, paths = env
    with pytest.raises(ValueError):
        plan_for(paths, **changes)


def test_formal_drafts_and_reweighted_validation_fail_closed(env):
    _, paths = env
    draft = load_builtin_tasks()[0]
    scoring = ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),))
    validate = Mock(side_effect=judges.TaskValidationError("blocked", []))
    with pytest.raises(judges.TaskValidationError, match="blocked"):
        create_plan([model()], [draft], scoring, paths, validator=validate, repeats=1, max_requests=1)
    candidate = validate.call_args.args[0]
    assert not candidate.ready
    assert candidate.validation_evidence == ()
    assert candidate.validated_content_hash is None
    assert draft.scoring != candidate.scoring
    diagnostic = create_plan([model()], [draft], scoring, paths, validator=validate,
                             diagnostic=True, repeats=1, max_requests=1)
    assert validate.call_count == 1
    assert not diagnostic.task_snapshots[0].restore().ready


def test_reweighting_gets_new_template_and_real_validator_evidence(env, monkeypatch):
    from coding_eval.container_check import ContainerResult, DockerSandbox

    _, paths = env
    task = load_builtin_tasks()[0]
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.return_value = "sha256:" + "1" * 64
    count = len(task.rubric)
    # Real validate_task runs its reference and all negative validation logic.
    sandbox.execute.side_effect = (
        [ContainerResult("pass", "", "")] * (count + 1)
        + [result for _ in task.negative_files
           for result in [ContainerResult("pass", "", ""), *[ContainerResult("fail", "", "")] * count]]
    )
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    weights = tuple(Weight(name=w.name, weight=0.2) for w in task.scoring.dimension_weights)
    scoring = ScoringConfig(dimension_weights=weights, scenario_weights=(Weight(name="C01", weight=1),))
    plan = create_plan([model()], [task], scoring, paths, repeats=1, max_requests=1)
    frozen = plan.task_snapshots[0].restore()
    assert frozen.ready
    assert frozen.runtime.image == "sha256:" + "1" * 64
    assert plan.scoring.template_id.startswith("weights-")
    assert frozen.scoring == plan.scoring
    assert frozen.validated_content_hash == frozen.validation_hash
    assert frozen.version_hash != task.version_hash
    assert sandbox.execute.call_count == (count + 1) * (1 + len(task.negative_files))
    assert all(os.path.isfile(p) for p in frozen.validation_evidence)


def test_batch_has_real_concurrency_and_persists_intent_before_send(env):
    storage, paths = env
    plan = plan_for(paths, model_count=2, task_count=2, concurrency=3)
    storage.save_run(plan)
    active = peak = 0
    gate = asyncio.Event()

    async def hook(config, metadata):
        nonlocal active, peak
        identifier = intent_id(plan.id, config.id, metadata["task_id"], metadata["sample_index"], 0)
        assert storage.get_object("request_intent", identifier) is not None
        active += 1
        peak = max(peak, active)
        if active == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        await asyncio.sleep(0)
        active -= 1

    api = FakeAPI(hook=hook)
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    assert peak == 3
    assert len(api.calls) == plan.expected_requests
    attempts = storage.list_attempts(plan.id)
    assert len(attempts) == len(storage.list_objects("request_intent")) == plan.expected_requests
    for attempt in attempts:
        assert storage.get_object("judge", attempt.id)["attempt_id"] == attempt.id
        assert attempt.queue_seconds >= 0
        assert attempt.logical_task_seconds >= 0
        assert attempt.metrics.total_seconds == 0.01
    per_task = {}
    for config, system, user, metadata in api.calls:
        per_task.setdefault(metadata["task_id"], set()).add((system, user))
    assert all(len(prompts) == 1 for prompts in per_task.values())
    assert engine.status(plan.id)["remaining_samples"] == 0
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    assert len(api.calls) == plan.expected_requests


@pytest.mark.parametrize("statuses,expected", [
    (("failed",), 6), (("failed", "completed"), 4),
    (("uncertain",), 2), (("cancelled",), 2), (("completed",), 2),
])
def test_retries_are_fixed_and_uncertain_is_never_retried(env, statuses, expected):
    storage, paths = env
    plan = plan_for(paths, max_retries=2, max_requests=6, retry_delay_seconds=0.01)
    storage.save_run(plan)
    api = FakeAPI(statuses)
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    attempts = storage.list_attempts(plan.id)
    assert len(attempts) == len(api.calls) == expected
    assert len({a.id for a in attempts}) == expected
    for sample in range(plan.repeats):
        history = sorted((a for a in attempts if a.sample_index == sample), key=lambda a: a.attempt_index)
        assert [a.attempt_index for a in history] == list(range(len(history)))
        if len(history) > 1:
            assert history[1].logical_task_seconds >= history[0].logical_task_seconds + 0.01
            assert history[1].queue_seconds == 0


def test_logical_time_excludes_judge_time(env, monkeypatch):
    storage, paths = env
    plan = plan_for(paths, repeats=1, max_retries=1, concurrency=1)
    storage.save_run(plan)
    clock = [0.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def hook(config, metadata):
        clock[0] += 2

    def slow_judge(task, attempt, paths):
        clock[0] += 100
        return judged(task, attempt, paths)

    api = FakeAPI(("failed", "completed"), hook)
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=slow_judge).run(plan.id)) == RunStatus.COMPLETED
    attempts = storage.list_attempts(plan.id)
    assert [a.logical_task_seconds for a in attempts] == [2, 4]
    assert clock[0] == 204


def test_process_crash_recovers_intent_without_reissuing(env):
    storage, paths = env
    plan = plan_for(paths, concurrency=1, max_retries=1)
    storage.save_run(plan)
    script = """
import asyncio, os, sys
from pathlib import Path
from coding_eval.runner import BatchRunner
from coding_eval.storage import AppPaths, Storage
class CrashAPI:
    async def generate(self, *args, **kwargs):
        os._exit(17)
root = Path(sys.argv[1])
paths = AppPaths(root, root / 'eval.sqlite3', root / 'artifacts')
asyncio.run(BatchRunner(Storage(paths.database), paths, client=CrashAPI()).run(sys.argv[2]))
"""
    process = subprocess.run([sys.executable, "-c", script, str(paths.root), plan.id],
                             capture_output=True, timeout=10)
    assert process.returncode == 17, process.stderr.decode()
    assert storage.get_run_status(plan.id) == RunStatus.RUNNING
    assert storage.list_attempts(plan.id) == []
    assert len(storage.list_objects("request_intent")) == 1
    api = FakeAPI()
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id)) == RunStatus.COMPLETED
    attempts = storage.list_attempts(plan.id)
    lost = next(a for a in attempts if a.status == "uncertain")
    assert lost.error_code == "response_lost"
    assert len(api.calls) == 1
    assert all(call[3]["sample_index"] != lost.sample_index for call in api.calls)
    assert len(storage.list_objects("request_intent")) == 2


def test_cancel_before_start_never_dispatches_and_can_resume(env):
    storage, paths = env
    plan = plan_for(paths)
    storage.save_run(plan)
    api = FakeAPI()
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    engine.cancel(plan.id)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.CANCELLED
    assert api.calls == []
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    assert len(api.calls) == 2


@pytest.mark.parametrize("external", [False, True])
def test_cancel_in_flight_and_resume_only_unstarted(env, external):
    storage, paths = env
    plan = plan_for(paths, repeats=4, concurrency=2, max_retries=1)
    storage.save_run(plan)

    async def scenario():
        entered = asyncio.Event()
        count = 0

        async def hook(config, metadata):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await asyncio.Event().wait()

        api = FakeAPI(hook=hook)
        engine = BatchRunner(storage, paths, client=api, judge=judged)
        running = asyncio.create_task(engine.run(plan.id))
        await asyncio.wait_for(entered.wait(), 3)
        if external:
            BatchRunner(storage, paths).cancel(plan.id)
        else:
            running.cancel()
        assert await asyncio.wait_for(running, 3) == RunStatus.CANCELLED
        attempts = storage.list_attempts(plan.id)
        assert len(api.calls) == len(attempts) == 2
        assert all(a.status == "uncertain" for a in attempts)
        assert all(storage.get_object("judge", a.id) is not None for a in attempts)
        resumed_api = FakeAPI()
        assert await BatchRunner(storage, paths, client=resumed_api, judge=judged).run(plan.id) == RunStatus.COMPLETED
        assert len(resumed_api.calls) == 2
        assert {c[3]["sample_index"] for c in resumed_api.calls}.isdisjoint(a.sample_index for a in attempts)

    asyncio.run(scenario())


def test_registry_thread_status_duplicate_start_and_cancellation(env):
    storage, paths = env
    plan = plan_for(paths, repeats=3, concurrency=1)
    storage.save_run(plan)
    entered = threading.Event()

    async def hold(config, metadata):
        entered.set()
        await asyncio.Event().wait()

    api = FakeAPI(hook=hold)
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    registry = WorkerRegistry()
    keys = {"model0": "session-key"}
    assert registry.start(storage, paths, plan.id, runner=engine, api_keys=keys)
    try:
        assert entered.wait(3)
        with ThreadPoolExecutor(max_workers=6) as pool:
            started = list(pool.map(lambda _: registry.start(storage, paths, plan.id), range(12)))
            states = list(pool.map(lambda _: engine.status(plan.id), range(12)))
        assert not any(started)
        assert all(s["status"] == "running" and s["reserved_requests"] == 1 for s in states)
        contender = BatchRunner(storage, paths, client=FakeAPI(), judge=judged)
        with pytest.raises(RunBusyError, match="active worker"):
            asyncio.run(contender.run(plan.id))
    finally:
        registry.cancel(storage, paths, plan.id)
        registry.join(storage, plan.id, timeout=5)
    assert not registry.active(storage, plan.id)
    assert keys == {"model0": "session-key"}
    assert storage.get_run_status(plan.id) == RunStatus.CANCELLED
    assert len(api.calls) == 1
    assert b"session-key" not in paths.database.read_bytes()


def test_judge_error_resume_rejudges_without_api_request(env):
    storage, paths = env
    plan = plan_for(paths, repeats=1)
    storage.save_run(plan)
    api = FakeAPI()
    judge = Mock(side_effect=RuntimeError("do not persist arbitrary exceptions"))
    engine = BatchRunner(storage, paths, client=api, judge=judge)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.FAILED
    attempt = storage.list_attempts(plan.id)[0]
    assert storage.get_object("judge", attempt.id)["quality_score"] is None
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id)) == RunStatus.COMPLETED
    assert len(api.calls) == 1
    assert len(storage.list_objects("judge")) == 2
    assert storage.get_object("judge", attempt.id)["quality_score"] == 100


def test_preflight_all_models_before_any_request(env):
    storage, paths = env
    base = plan_for(paths)
    invalid = model("bad", parameters={"messages": []})
    plan = RunPlan.model_validate({
        **base.model_dump(), "model_snapshots": [*base.model_snapshots, Snapshot.capture(invalid)],
    })
    storage.save_run(plan)
    api = FakeAPI()
    with pytest.raises(ValueError, match="core request"):
        asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id))
    assert api.calls == []
    assert storage.list_objects("request_intent") == []
    assert storage.get_run_status(plan.id) == RunStatus.QUEUED


def test_api_integration_saves_hash_metrics_and_redacts_session_secret(env):
    storage, paths = env
    plan = plan_for(paths, repeats=1)
    storage.save_run(plan)
    observed = []

    def handler(request):
        observed.append(request)
        assert request.headers["authorization"] == "Bearer session-secret"
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "session-secret"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 12, "completion_tokens": 4}},
        ]
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    api = APIClient(transport=httpx.MockTransport(handler))
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    assert asyncio.run(engine.run(plan.id, api_keys={"model0": "session-secret"})) == RunStatus.COMPLETED
    attempt = storage.list_attempts(plan.id)[0]
    assert attempt.request_hash
    assert attempt.raw_output == "[REDACTED]"
    assert attempt.metrics.input_tokens == 12
    assert attempt.metrics.output_tokens == 4
    assert attempt.metrics.cache_read_tokens is None
    assert len(observed) == 1
    assert b"session-secret" not in paths.database.read_bytes()


def test_result_persistence_failure_recovers_as_uncertain(env, monkeypatch):
    storage, paths = env
    plan = plan_for(paths, repeats=1, max_retries=2)
    storage.save_run(plan)
    save = storage.save_attempt
    monkeypatch.setattr(storage, "save_attempt", Mock(side_effect=OSError("disk unavailable")))
    api = FakeAPI()
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id)) == RunStatus.FAILED
    assert len(api.calls) == 1
    assert storage.list_attempts(plan.id) == []
    monkeypatch.setattr(storage, "save_attempt", save)
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id)) == RunStatus.COMPLETED
    assert len(api.calls) == 1
    assert storage.list_attempts(plan.id)[0].status == "uncertain"


def test_cancel_during_preflight_is_not_overwritten(env, monkeypatch):
    storage, paths = env
    plan = plan_for(paths, repeats=1)
    storage.save_run(plan)
    api = FakeAPI()
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    build = runner.build_request

    def stop_then_build(*args):
        engine.cancel(plan.id)
        return build(*args)

    monkeypatch.setattr(runner, "build_request", stop_then_build)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.CANCELLED
    assert api.calls == []
    assert storage.list_objects("request_intent") == []


def test_cancel_waits_for_in_progress_judge_without_dispatching_more(env):
    storage, paths = env
    plan = plan_for(paths, repeats=3, concurrency=1)
    storage.save_run(plan)
    entered, release = threading.Event(), threading.Event()

    def hold_judge(task, attempt, paths):
        entered.set()
        assert release.wait(4)
        return judged(task, attempt, paths)

    api = FakeAPI()
    engine = BatchRunner(storage, paths, client=api, judge=hold_judge)
    registry = WorkerRegistry()
    registry.start(storage, paths, plan.id, runner=engine)
    try:
        assert entered.wait(3)
        registry.cancel(storage, paths, plan.id)
        assert registry.active(storage, plan.id)
        assert len(storage.list_attempts(plan.id)) == 1
    finally:
        release.set()
        registry.join(storage, plan.id, timeout=5)
    assert not registry.active(storage, plan.id)
    assert storage.get_run_status(plan.id) == RunStatus.CANCELLED
    assert len(api.calls) == 1
    assert storage.get_object("judge", storage.list_attempts(plan.id)[0].id)["status"] == "completed"


def test_runner_results_feed_report_with_exact_scoring_and_parameters(env):
    from coding_eval.models import CheckResult, Dimension
    from coding_eval.reports import build_report

    storage, paths = env
    plan = plan_for(paths, model_count=2, task_count=2)
    storage.save_run(plan)

    def full_judge(task, attempt, paths):
        dimensions = {Dimension(w.name): 100.0 for w in task.scoring.dimension_weights}
        return JudgeResult(
            attempt_id=attempt.id, task_version_hash=task.version_hash, judge_version="test",
            status="completed", quality_score=100, all_required_passed=True,
            dimension_scores=dimensions,
            checks=tuple(CheckResult(check=r.check, passed=True, evidence="offline fixture") for r in task.rubric),
        )

    api = FakeAPI()
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=full_judge).run(plan.id)) == RunStatus.COMPLETED
    report = build_report(plan, storage.list_attempts(plan.id), storage.list_objects("judge"), bootstrap_iterations=10)
    assert report.scoring == plan.scoring
    assert report.source_plan_hash == plan.version_hash
    assert report.payload["coverage"]["coverage"] == 1
    assert report.status == "invalid"  # Diagnostic results never become a formal rank.
    for snapshot in plan.model_snapshots:
        assert report.payload["model_snapshots"][snapshot.object_id]["config"] == snapshot.restore().model_dump(mode="json")
        assert report.payload["models"][snapshot.object_id]["quality_score"] == 100
    storage.save_report(report)
    assert storage.list_reports(plan.id) == [report]


def test_page_form_preview_start_and_refresh_without_control_model(env, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app import run_page

    storage, paths = env
    config = model("model0")
    task = load_builtin_tasks()[0]
    storage.save_object("model", config.id, config.model_dump(mode="json"))
    storage.save_object("task", task.id, task.model_dump(mode="json"))
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(paths.root))
    registry = WorkerRegistry()
    monkeypatch.setattr(run_page, "worker_registry", lambda: registry)
    monkeypatch.setattr(runner, "APIClient", FakeAPI)
    monkeypatch.setattr(judges, "judge_attempt", judged)
    source = """
from app.run_page import render
from coding_eval.storage import AppPaths, Storage
paths = AppPaths.from_env()
storage = Storage(paths.root / "eval.sqlite3")
render(storage, paths)
"""
    page = AppTest.from_string(source, default_timeout=10).run()
    assert not page.exception
    page.checkbox(key="run_diagnostic").check().run()
    assert not page.exception
    page.button[0].click().run()
    assert not page.exception
    frozen = RunPlan.model_validate_json(page.session_state["_run_preview"])
    assert frozen.diagnostic
    assert frozen.expected_requests == 3
    assert storage.list_runs() == []
    page.checkbox(key="run_cost_confirm_" + frozen.id).check().run()
    page.button[2].click().run()
    registry.join(storage, frozen.id, timeout=5)
    assert not registry.active(storage, frozen.id)
    page.run()
    assert not page.exception
    assert storage.get_run_status(frozen.id) == RunStatus.COMPLETED
    assert len(storage.list_attempts(frozen.id)) == 3
    assert storage.list_objects("control") == []
