"""Control calls are explicit, separately metered, and cannot alter authority."""

import asyncio
import json

import httpx
import pytest

from coding_eval.agent import (
    PlanLimits, PlanProposal, PlanningAgent, proposal_to_plan, task_catalog, validate_proposal,
)
from coding_eval.api import APIClient
from coding_eval.datasets import load_builtin_tasks
from coding_eval.models import (
    AttemptMetrics, AttemptResult, ModelConfig, ReportVersion, ScoringConfig, Weight,
)
from coding_eval.storage import AppPaths, Storage


@pytest.fixture
def setup(tmp_path):
    paths = AppPaths(tmp_path, tmp_path / "eval.sqlite3", tmp_path / "artifacts")
    storage = Storage(paths.database)
    model = ModelConfig(id="model", display_name="Model", protocol="openai",
                        url="https://control.invalid/v1", model_id="exact",
                        capabilities={"stream": False})
    tasks = load_builtin_tasks()[:1]
    scoring = ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),))
    limits = PlanLimits(max_requests=6, max_concurrency=2, max_repeats=3, max_retries=1, diagnostic=True)
    proposal = PlanProposal(
        model_ids=(model.id,), task_ids=(tasks[0].id,), scoring=scoring, repeats=3,
        concurrency=2, max_requests=6, diagnostic=True, max_retries=1,
    )
    return storage, paths, model, tasks, scoring, limits, proposal


class ControlAPI:
    def __init__(self, output, status="completed"):
        self.output, self.status, self.calls = output, status, []

    async def generate(self, config, system_prompt, user_prompt, *, api_key=None, **metadata):
        self.calls.append((system_prompt, user_prompt, metadata, api_key))
        return AttemptResult(model_id=config.id, **metadata, status=self.status, raw_output=self.output,
                             metrics=AttemptMetrics(input_tokens=13, output_tokens=8, total_seconds=0.2))


def test_agent_calls_once_catalog_only_and_records_control_metrics(setup):
    storage, paths, model, tasks, scoring, limits, proposal = setup
    client = ControlAPI(proposal.model_dump_json())
    result = asyncio.run(PlanningAgent(storage, client).propose(
        "Compare this task three times", model, [model], tasks, scoring, limits, api_key="private-control",
    ))
    assert result == proposal
    assert len(client.calls) == 1
    system, user, metadata, secret = client.calls[0]
    assert secret == "private-control"
    assert metadata["purpose"] == "control"
    assert "private-control" not in system + user
    assert tasks[0].title in user
    for private in [*tasks[0].test_files.values(), *tasks[0].reference_files.values(),
                    *(source for files in tasks[0].negative_files.values() for source in files.values())]:
        assert private not in user
    assert "test_files" not in user
    assert "reference_files" not in user
    assert set(task_catalog(tasks)[0]) == {"id", "scenario_id", "title", "status"}
    assert storage.list_runs() == []
    records = storage.list_objects("control")
    assert len(records) == len(storage.list_objects("control_intent")) == 1
    assert records[0]["payload"]["result"]["metrics"]["input_tokens"] == 13
    assert records[0]["payload"]["result"]["metrics"]["output_tokens"] == 8
    assert storage.list_attempts(metadata["run_id"]) == []
    assert b"private-control" not in paths.database.read_bytes()


@pytest.mark.parametrize("change,error", [
    ({"model_ids": []}, "nonempty"),
    ({"task_ids": []}, "nonempty"),
    ({"model_ids": ["model", "model"]}, "unique"),
    ({"model_ids": ["unknown"]}, "unavailable model"),
    ({"task_ids": ["unknown"]}, "unavailable task"),
    ({"max_requests": 7}, "limits"),
    ({"max_requests": 1}, "insufficient"),
    ({"concurrency": 3}, "limits"),
    ({"repeats": 4}, "limits"),
    ({"max_retries": 2}, "limits"),
    ({"diagnostic": False}, "limits"),
    ({"budget_group": "unapproved"}, "limits"),
    ({"scoring": {"template_id": "unapproved", "scenario_weights": [{"name": "C01", "weight": 1}]}}, "scoring"),
])
def test_server_rejects_unauthorized_proposals(setup, change, error):
    _, _, model, tasks, scoring, limits, proposal = setup
    candidate = PlanProposal.model_validate_json(json.dumps({**proposal.model_dump(mode="json"), **change}))
    with pytest.raises(ValueError, match=error):
        validate_proposal(candidate, [model], tasks, scoring, limits)


@pytest.mark.parametrize("change", [
    {"api_key": "not-allowed"}, {"test_files": {"checks.py": "pass"}}, {"tools": []},
    {"parameters": {"temperature": 1}}, {"concurrency": "2"}, {"repeats": True},
    {"max_retries": -1},
])
def test_schema_disallows_extra_fields_and_type_coercion(setup, change):
    proposal = setup[-1]
    with pytest.raises(ValueError):
        PlanProposal.model_validate_json(json.dumps({**proposal.model_dump(mode="json"), **change}))


def test_proposal_formal_mode_requires_ready_and_scenario_coverage(setup):
    _, _, model, tasks, scoring, limits, proposal = setup
    formal_limits = limits.model_copy(update={"diagnostic": False})
    with pytest.raises(ValueError, match="unavailable task"):
        validate_proposal(proposal, [model], tasks, scoring, formal_limits)
    other_tasks = load_builtin_tasks()[:2]
    other = proposal.model_copy(update={"task_ids": tuple(t.id for t in other_tasks), "repeats": 1})
    with pytest.raises(ValueError, match="scenario"):
        validate_proposal(other, [model], other_tasks, scoring, limits)
    with pytest.raises(ValueError, match="unavailable model"):
        validate_proposal(proposal, [model.model_copy(update={"enabled": False})], tasks, scoring, limits)


@pytest.mark.parametrize("output,status", [
    ("not json", "completed"), ('{"tools":[]}', "completed"),
    ("", "failed"), ("", "uncertain"),
])
def test_invalid_control_response_is_metered_but_never_starts_batch(setup, output, status):
    storage, _, model, tasks, scoring, limits, _ = setup
    client = ControlAPI(output, status)
    with pytest.raises(ValueError, match="Control"):
        asyncio.run(PlanningAgent(storage, client).propose("Compare", model, [model], tasks, scoring, limits))
    assert len(client.calls) == 1
    assert len(storage.list_objects("control")) == 1
    assert storage.list_runs() == []


def test_empty_intent_never_calls_control_model(setup):
    storage, _, model, tasks, scoring, limits, _ = setup
    client = ControlAPI("")
    with pytest.raises(ValueError, match="required"):
        asyncio.run(PlanningAgent(storage, client).propose("  ", model, [model], tasks, scoring, limits))
    assert client.calls == []
    assert storage.list_objects("control_intent") == []


def test_proposal_converts_to_frozen_plan_without_starting_requests(setup):
    storage, paths, model, tasks, scoring, limits, proposal = setup
    plan = proposal_to_plan(proposal, [model], tasks, scoring, limits, paths)
    assert plan.repeats == proposal.repeats
    assert plan.max_requests == proposal.max_requests
    assert plan.scoring == proposal.scoring
    assert plan.model_snapshots[0].restore() == model
    assert plan.task_snapshots[0].restore().scoring == plan.scoring
    assert plan.task_snapshots[0].restore().test_files == tasks[0].test_files
    assert storage.list_runs() == []


def test_control_cancellation_saves_uncertain_without_retry(setup):
    storage, _, model, tasks, scoring, limits, _ = setup

    async def scenario():
        entered = asyncio.Event()

        class BlockingControl:
            calls = 0

            async def generate(self, *args, **kwargs):
                self.calls += 1
                entered.set()
                await asyncio.Event().wait()

        client = BlockingControl()
        pending = asyncio.create_task(PlanningAgent(storage, client).propose(
            "Compare", model, [model], tasks, scoring, limits,
        ))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert client.calls == 1

    asyncio.run(scenario())
    record = storage.list_objects("control")[0]["payload"]["result"]
    assert record["status"] == "uncertain"
    assert record["error_code"] == "cancelled_in_flight"
    assert len(storage.list_objects("control_intent")) == 1


def test_control_client_exception_and_identity_mismatch_fail_closed(setup):
    storage, _, model, tasks, scoring, limits, _ = setup

    class BrokenControl:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("secret-in-error")

    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(PlanningAgent(storage, BrokenControl()).propose(
            "Compare", model, [model], tasks, scoring, limits,
        ))
    record = storage.list_objects("control")[0]["payload"]["result"]
    assert record["status"] == "uncertain"
    assert "secret-in-error" not in json.dumps(record)


def test_real_control_api_redacts_credential_and_preserves_metrics(setup):
    storage, paths, model, tasks, scoring, limits, _ = setup
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer control-secret"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "control-secret"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 3},
        })

    client = APIClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="validation"):
        asyncio.run(PlanningAgent(storage, client).propose(
            "Compare", model, [model], tasks, scoring, limits, api_key="control-secret",
        ))
    assert len(requests) == 1
    saved = storage.list_objects("control")[0]["payload"]["result"]
    assert saved["raw_output"] == "[REDACTED]"
    assert saved["metrics"]["input_tokens"] == 17
    assert b"control-secret" not in paths.database.read_bytes()


def test_explain_uses_persisted_numeric_summary_and_never_mutates_report(setup):
    from coding_eval.reports import build_report
    from test_reports import make_plan, evidence

    storage, _, model, _, _, _, _ = setup
    plan = make_plan(models=(model.id,), repeats=2)
    storage.save_run(plan)
    attempts, judges = evidence(plan)
    report = build_report(plan, attempts[:1], judges[:1], bootstrap_iterations=10)
    # Sources at both known and unknown nesting levels must never enter the prompt.
    report.payload["test_files"] = {"checks.py": "HIDDEN_TESTS"}
    report.payload["warnings"] = ["HIDDEN_TEXT"]
    value = report.payload["models"]["model"]
    value["raw_output"] = "HIDDEN_SOURCE"
    value["metrics"]["total_seconds"]["all_attempts"]["evidence"] = "HIDDEN_EVIDENCE"
    value["quality_interval"]["method"] = "HIDDEN_METHOD"
    value["tasks"]["HIDDEN_TASK"] = {"quality_score": 100}
    report.payload["models"]["HIDDEN_MODEL"] = {"quality_score": 100}
    storage.save_report(report)
    client = ControlAPI(f"Report for {plan.id}; task t0: evidence is limited.")
    result = asyncio.run(PlanningAgent(storage, client).explain(report, model))
    assert plan.id in result
    prompt = client.calls[0][1]
    summary = json.loads(prompt.split("\n\n")[0])
    actual = summary["metrics"]["models"]["model"]
    assert actual["quality_score"] is None
    assert actual["observed_quality_score"] == 100
    assert actual["coverage"] == .5 and actual["expected"] == 2 and actual["scored"] == 1
    assert actual["quality_interval"]["lower"] is None
    assert actual["quality_interval"]["task_clusters"] == 1
    assert actual["quality_interval"]["confidence"] == .95
    assert actual["metrics"]["total_seconds"]["completed_attempts"]["median"] == 2
    assert actual["metrics"]["output_tokens"]["all_attempts"]["sum"] == 10
    assert actual["metrics"]["input_tokens"]["all_attempts"]["missing_rate"] == 1
    assert actual["metrics"]["input_tokens"]["noncompleted_attempts"]["total"] == 0
    assert summary["metrics"]["coverage"]["status_counts"] == {"missing": 1, "scored": 1}
    assert summary["report_id"] == report.id and summary["run_id"] == plan.id
    assert summary["task_ids"] == ["t0"]
    assert set(actual["scenarios"]) == {"S1"}
    assert set(actual["tasks"]) == {"t0"}
    assert "HIDDEN_" not in prompt and "SYNTHETIC ANSWER" not in prompt
    assert storage.list_reports(plan.id) == [report]
    assert storage.list_attempts(plan.id) == []
    assert storage.list_objects("control")[0]["payload"]["action"] == "explain"
    with pytest.raises(ValueError, match="unchanged persisted"):
        asyncio.run(PlanningAgent(storage, client).explain(report.model_copy(update={"payload": {}}), model))
    assert len(client.calls) == 1


def test_unavailable_explanation_does_not_block_deterministic_report(setup):
    storage, paths, model, tasks, scoring, limits, proposal = setup
    plan = proposal_to_plan(proposal, [model], tasks, scoring, limits, paths)
    storage.save_run(plan)
    report = ReportVersion(run_id=plan.id, scoring=scoring, source_plan_hash=plan.version_hash)
    storage.save_report(report)
    with pytest.raises(ValueError, match="unchanged"):
        asyncio.run(PlanningAgent(storage, ControlAPI("", "failed")).explain(report, model))
    assert storage.list_reports(plan.id) == [report]


@pytest.mark.parametrize("matching_identity", [True, False])
def test_control_cancellation_keeps_only_matching_sanitized_partial(setup, matching_identity):
    storage, paths, model, tasks, scoring, limits, _ = setup

    class PartialCancellation(asyncio.CancelledError):
        def __init__(self, result):
            self.result = result

    class PartialControl:
        calls = 0

        async def generate(self, config, system, user, *, api_key=None, **metadata):
            self.calls += 1
            if not matching_identity:
                metadata["run_id"] = "foreign-control"
            raise PartialCancellation(AttemptResult(
                model_id=config.id, **metadata, status="uncertain",
                raw_output="partial control-secret", raw_response={"content": "control-secret"},
                request_hash="observed-hash",
                metrics=AttemptMetrics(
                    input_tokens=12, first_content_seconds=.1, total_seconds=.3,
                    raw_usage={"prompt_tokens": 12, "debug": "control-secret"},
                ),
            ))

    client = PartialControl()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(PlanningAgent(storage, client).propose(
            "Compare", model, [model], tasks, scoring, limits, api_key="control-secret",
        ))
    assert client.calls == 1
    records = storage.list_objects("control")
    assert len(records) == len(storage.list_objects("control_intent")) == 1
    result = records[0]["payload"]["result"]
    assert result["status"] == "uncertain" and result["purpose"] == "control"
    assert result["error_code"] == "cancelled_in_flight"
    assert result["run_id"] == records[0]["object_id"]
    if matching_identity:
        assert result["raw_output"] == "partial [REDACTED]"
        assert result["raw_response"] == {"content": "[REDACTED]"}
        assert result["metrics"]["input_tokens"] == 12
        assert result["metrics"]["first_content_seconds"] == .1
        assert result["metrics"]["total_seconds"] == .3
        assert result["request_hash"] == "observed-hash"
    else:
        assert result["raw_output"] == "" and result["metrics"]["input_tokens"] is None
        assert result["request_hash"] is None
    assert b"control-secret" not in paths.database.read_bytes()
    assert storage.list_runs() == [] and storage.list_attempts(result["run_id"]) == []
