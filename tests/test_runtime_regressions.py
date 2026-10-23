"""Offline runtime contracts; synthetic readiness is not task validation evidence."""

import asyncio
from unittest.mock import Mock

import httpx
import pytest

from coding_eval import container_check, judges
from coding_eval.api import APIClient
from coding_eval.container_check import DockerSandbox, DockerUnavailable
from coding_eval.datasets import load_builtin_tasks
from coding_eval.models import RunPlan, RunStatus, ScoringConfig, Snapshot, TaskDefinition, Weight
from coding_eval.runner import BatchRunner, create_plan
from coding_eval.storage import AppPaths, Storage
from test_api import delta, event
from test_runner import FakeAPI, judged, model

IMAGE = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def no_docker_commands(monkeypatch):
    monkeypatch.setattr(container_check, "run_command", Mock(
        side_effect=AssertionError("Runtime regressions must never invoke Docker or host task code"),
    ))


@pytest.fixture
def env(tmp_path):
    paths = AppPaths(tmp_path, tmp_path / "eval.sqlite3", tmp_path / "artifacts")
    return Storage(paths.database), paths


def ready_tasks(count=1, image=IMAGE):
    tasks = load_builtin_tasks()[:count]
    scoring = ScoringConfig(scenario_weights=tuple(
        Weight(name=t.scenario_id, weight=1 / count) for t in tasks
    ))
    result = []
    for task in tasks:
        payload = task.model_dump(mode="json")
        payload["scoring"] = scoring.model_dump(mode="json")
        payload["runtime"]["image"] = image
        draft = TaskDefinition.model_validate(payload)
        payload.update(status="ready", validated_content_hash=draft.validation_hash,
                       validation_evidence=["synthetic-offline-only"])
        result.append(TaskDefinition.model_validate(payload))
    return result, scoring


def formal_plan(count=1, image=IMAGE, **options):
    tasks, scoring = ready_tasks(count, image)
    return RunPlan(
        model_snapshots=(Snapshot.capture(model()),),
        task_snapshots=tuple(Snapshot.capture(t) for t in tasks),
        scoring=scoring, repeats=1, max_requests=count * (1 + options.get("max_retries", 0)),
        **options,
    )


@pytest.mark.parametrize("image", ["python:3.11-slim", "python:3.11-slim@sha256:" + "a" * 64,
                                  "sha256:bad", IMAGE])
def test_create_plan_requires_validated_image_id(env, image):
    _, paths = env
    tasks, scoring = ready_tasks(image=image)
    if image == IMAGE:
        plan = create_plan([model()], tasks, scoring, paths, repeats=1, max_requests=1)
        assert plan.task_snapshots[0].restore() == tasks[0]
        assert plan.task_snapshots[0].restore().runtime.image == IMAGE
    else:
        with pytest.raises(ValueError, match="sha256"):
            create_plan([model()], tasks, scoring, paths, repeats=1, max_requests=1)


@pytest.mark.parametrize("initial", [RunStatus.QUEUED, RunStatus.FAILED,
                                   RunStatus.CANCELLED, RunStatus.RUNNING])
@pytest.mark.parametrize("failure", ["unavailable", "mismatch"])
def test_formal_preflight_fails_before_intent_on_start_and_resume(env, monkeypatch, initial, failure):
    storage, paths = env
    plan = formal_plan()
    storage.save_run(plan)
    if initial != RunStatus.QUEUED:
        storage.transition_run(plan.id, RunStatus.QUEUED, RunStatus.RUNNING)
        if initial != RunStatus.RUNNING:
            storage.transition_run(plan.id, RunStatus.RUNNING, initial)
    sandbox = Mock(spec=DockerSandbox)
    if failure == "unavailable":
        sandbox.prepare.side_effect = DockerUnavailable("offline-no-daemon")
    else:
        sandbox.prepare.return_value = "sha256:" + "b" * 64
    factory = Mock(return_value=sandbox)
    monkeypatch.setattr(judges, "DockerSandbox", factory)
    api = FakeAPI()
    with pytest.raises((DockerUnavailable, ValueError)):
        asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id))
    assert api.calls == []
    assert storage.list_objects("request_intent") == []
    assert storage.list_attempts(plan.id) == []
    assert storage.get_run_status(plan.id) == initial
    sandbox.prepare.assert_called_once_with()
    sandbox.execute.assert_not_called()


def test_formal_preflight_checks_every_task_before_api(env, monkeypatch):
    storage, paths = env
    plan = formal_plan(count=2)
    storage.save_run(plan)
    prepared = []

    def factory(runtime):
        sandbox = Mock(spec=DockerSandbox)

        def prepare():
            prepared.append(runtime)
            return runtime.image

        sandbox.prepare.side_effect = prepare
        sandbox.execute.side_effect = AssertionError("No task execution in preflight")
        return sandbox

    monkeypatch.setattr(judges, "DockerSandbox", factory)

    async def hook(config, metadata):
        assert len(prepared) == 2

    api = FakeAPI(hook=hook)
    assert asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id)) == RunStatus.COMPLETED
    assert len(api.calls) == 2
    assert len(prepared) == 2
    assert all(a.status == "completed" for a in storage.list_attempts(plan.id))
    assert all(runtime.image == IMAGE for runtime in prepared)


def test_second_task_prepare_failure_still_sends_zero_requests(env, monkeypatch):
    storage, paths = env
    plan = formal_plan(count=2)
    storage.save_run(plan)
    first, second = Mock(spec=DockerSandbox), Mock(spec=DockerSandbox)
    first.prepare.return_value = IMAGE
    second.prepare.side_effect = DockerUnavailable("second runtime unavailable")
    monkeypatch.setattr(judges, "DockerSandbox", Mock(side_effect=[first, second]))
    api = FakeAPI()
    with pytest.raises(DockerUnavailable, match="second runtime"):
        asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id))
    assert api.calls == []
    assert storage.list_objects("request_intent") == []
    first.prepare.assert_called_once_with()
    second.prepare.assert_called_once_with()


@pytest.mark.parametrize("mutation", ["image", "requirements"])
def test_revalidation_cannot_replace_frozen_runtime_or_task_content(env, mutation):
    _, paths = env
    tasks, _ = ready_tasks()
    scoring = ScoringConfig(
        dimension_weights=tuple(Weight(name=w.name, weight=0.2) for w in tasks[0].scoring.dimension_weights),
        scenario_weights=(Weight(name="C01", weight=1),),
    )

    def validator(task, paths):
        payload = task.model_dump(mode="json")
        if mutation == "image":
            payload["runtime"]["image"] = "sha256:" + "b" * 64
        else:
            payload["requirements"] = "Changed by validator"
        draft = TaskDefinition.model_validate(payload)
        payload.update(status="ready", validation_evidence=["synthetic-only"],
                       validated_content_hash=draft.validation_hash)
        return TaskDefinition.model_validate(payload)

    with pytest.raises(ValueError, match="Validator changed task content"):
        create_plan([model()], tasks, scoring, paths, validator=validator, repeats=1, max_requests=1)


@pytest.mark.parametrize("diagnostic", [False, True])
def test_legacy_tag_is_blocked_unless_diagnostic_is_explicit(env, monkeypatch, diagnostic):
    storage, paths = env
    plan = formal_plan(image="python:3.11-slim", diagnostic=diagnostic)
    storage.save_run(plan)
    factory = Mock(side_effect=AssertionError("Unpinned formal task must fail before Docker"))
    monkeypatch.setattr(judges, "DockerSandbox", factory)
    api = FakeAPI()
    engine = BatchRunner(storage, paths, client=api, judge=judged)
    if diagnostic:
        assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
        assert len(api.calls) == 1
    else:
        with pytest.raises(ValueError, match="sha256"):
            asyncio.run(engine.run(plan.id))
        assert api.calls == []
        assert storage.list_objects("request_intent") == []
    factory.assert_not_called()


def test_formal_preflight_checks_task_contract_before_api(env, monkeypatch):
    storage, paths = env
    tasks, scoring = ready_tasks()
    payload = tasks[0].model_dump(mode="json")
    payload.update(status="draft", validation_evidence=[], validated_content_hash=None,
                   test_files={"unsupported.py": "INERT"})
    draft = TaskDefinition.model_validate(payload)
    payload.update(status="ready", validation_evidence=["synthetic-only"],
                   validated_content_hash=draft.validation_hash)
    task = TaskDefinition.model_validate(payload)
    plan = create_plan([model()], [task], scoring, paths, repeats=1, max_requests=1)
    storage.save_run(plan)
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.return_value = IMAGE
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    api = FakeAPI()
    with pytest.raises(ValueError):
        asyncio.run(BatchRunner(storage, paths, client=api, judge=judged).run(plan.id))
    assert api.calls == []
    assert storage.list_objects("request_intent") == []
    sandbox.execute.assert_not_called()


@pytest.mark.parametrize("external", [False, True])
def test_stream_cancel_persists_partial_evidence_and_resume_never_resends(env, monkeypatch, external):
    storage, paths = env
    plan = formal_plan(max_retries=1)
    storage.save_run(plan)
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.return_value = IMAGE
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    entered = asyncio.Event()
    calls = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(delta("session-"))
            yield event(delta("secret partial-answer"))
            yield event({"choices": [], "usage": {"prompt_tokens": 12}})
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    async def scenario():
        api = APIClient(transport=httpx.MockTransport(handler))
        engine = BatchRunner(storage, paths, client=api, judge=judged)
        running = asyncio.create_task(engine.run(plan.id, api_keys={"model": "session-secret"}))
        await asyncio.wait_for(entered.wait(), 2)
        if external:
            running.cancel()
        else:
            engine.cancel(plan.id)
        assert await asyncio.wait_for(running, 3) == RunStatus.CANCELLED
        assert await engine.run(plan.id) == RunStatus.COMPLETED

    asyncio.run(scenario())
    assert len(calls) == len(closed) == 1
    attempt, = storage.list_attempts(plan.id)
    assert attempt.status == "uncertain"
    assert attempt.raw_output == "[REDACTED] partial-answer"
    assert attempt.metrics.input_tokens == 12
    assert attempt.metrics.output_tokens is None
    assert attempt.metrics.first_content_seconds is not None
    assert attempt.metrics.first_answer_seconds is not None
    assert attempt.metrics.total_seconds is not None
    assert not attempt.metrics.stream_complete
    assert attempt.request_hash and len(attempt.request_hash) == 64
    assert attempt.protocol_mapping_version == "openai-chat-v1"
    assert attempt.raw_response["redacted"] is True
    assert b"session-secret" not in paths.database.read_bytes()
    assert len(storage.list_objects("request_intent")) == 1


def test_control_cancellation_uses_the_same_partial_result_protocol(env):
    from coding_eval.agent import PlanLimits, PlanningAgent

    storage, _ = env
    tasks, scoring = ready_tasks()
    entered = asyncio.Event()
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(delta("partial-plan"))
            yield event({"choices": [], "usage": {"prompt_tokens": 12}})
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    api = APIClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Stream(),
    )))

    async def scenario():
        pending = asyncio.create_task(PlanningAgent(storage, api).propose(
            "Compare", model(), [model()], tasks, scoring,
            PlanLimits(max_requests=1, max_concurrency=1, max_repeats=1),
        ))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())
    row, = storage.list_objects("control")
    result = row["payload"]["result"]
    assert result["status"] == "uncertain"
    assert result["raw_output"] == "partial-plan"
    assert result["request_hash"]
    assert result["metrics"]["input_tokens"] == 12
    assert result["metrics"]["total_seconds"] is not None
    assert result["metrics"]["first_content_seconds"] is not None
    assert result["metrics"]["stream_complete"] is False
    assert closed == [True]
