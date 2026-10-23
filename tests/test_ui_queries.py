"""精简 UI 查询保留版本、缺失语义和完整证据；不接触 API/容器。"""

import pytest

from coding_eval.models import AttemptResult, ReportVersion, RunPlan, ScoringConfig, Snapshot
from coding_eval.storage import Storage
from coding_eval.ui_queries import (
    attempt_detail, attempt_summaries, current_judges, current_models, current_objects,
    current_tasks, report_detail, report_index, run_summary,
)
from test_runtime_regressions import model, ready_tasks


@pytest.fixture
def records(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    tasks, scoring = ready_tasks()
    config = model()
    plan = RunPlan(
        model_snapshots=(Snapshot.capture(config),), task_snapshots=(Snapshot.capture(tasks[0]),),
        scoring=scoring, repeats=1, max_requests=3,
    )
    storage.save_run(plan)
    return storage, config, tasks[0], plan


def test_current_objects_reactivation_and_live_writes(records):
    storage, config, task, _ = records
    assert current_objects(storage, "model") == []
    first = config.model_dump(mode="json")
    storage.save_object("model", config.id, first)
    storage.save_object("model", config.id, {**first, "enabled": False})
    assert current_models(storage)[0].enabled is False
    storage.save_object("model", config.id, first)
    assert current_models(storage) == [config]
    assert len(storage.list_objects("model")) == 2
    storage.save_object("task", task.id, task.model_dump(mode="json"))
    assert current_tasks(storage) == [task]
    row = current_objects(storage, "model")[0]
    assert row["version_hash"] == config.version_hash
    assert "payload_json" not in row


@pytest.mark.parametrize("status", ["completed", "failed", "uncertain", "cancelled"])
def test_attempt_summaries_preserve_metrics_without_bulk_response(records, status):
    storage, config, task, plan = records
    result = AttemptResult(
        run_id=plan.id, model_id=config.id, task_id=task.id, sample_index=0, status=status,
        raw_output="COMPLETE_ANSWER" * 1000, raw_response={"events": ["STREAM_DATA"] * 1000},
        metrics={"input_tokens": 23, "output_tokens": 0, "cache_read_tokens": None,
                 "raw_usage": {"provider": 23}},
        diagnostics={"failure_category": "response_truncated"} if status == "failed" else {},
    )
    storage.save_attempt(result)
    summary = attempt_summaries(storage, plan.id)[0]
    assert summary.id == result.id and summary.status == status
    assert summary.raw_output == "" and summary.raw_response is None
    assert summary.metrics.input_tokens == 23 and summary.metrics.output_tokens == 0
    assert summary.metrics.cache_read_tokens is None
    assert summary.diagnostics == result.diagnostics
    assert attempt_detail(storage, plan.id, result.id) == result
    assert attempt_detail(storage, "another-run", result.id) is None
    assert attempt_detail(storage, plan.id, "missing") is None
    assert attempt_summaries(storage, "missing") == []


def test_current_judges_batched_heads_and_run_isolation(records):
    storage, config, task, plan = records
    attempts = [AttemptResult(run_id=plan.id, model_id=config.id, task_id=task.id,
                              sample_index=0, attempt_index=i, status="completed") for i in range(2)]
    for attempt in attempts:
        storage.save_attempt(attempt)
    first = {"status": "blocked", "quality_score": None}
    storage.save_object("judge", attempts[0].id, first)
    storage.save_object("judge", attempts[0].id, {"status": "completed", "quality_score": 100})
    storage.save_object("judge", attempts[0].id, first)
    storage.save_object("judge", "outside", {"status": "completed", "quality_score": 99})
    assert current_judges(storage, plan.id) == {attempts[0].id: first}
    assert current_judges(storage, "missing") == {}


def test_report_index_and_detail_preserve_all_versions_without_answers(records, tmp_path):
    storage, _, _, plan = records
    reports = [ReportVersion(
        run_id=plan.id, scoring=ScoringConfig(), source_plan_hash=plan.version_hash,
        post_hoc=bool(i), payload={"comparison_key": "group-A", "answer": "COMPLETE" * 1000},
    ) for i in range(2)]
    for report in reports:
        storage.save_report(report)
    rows = report_index(storage, plan.id)
    assert [r["report_id"] for r in rows] == [r.id for r in reports]
    assert rows[1]["post_hoc"] == 1 and rows[0]["comparison_key"] == "group-A"
    assert all("payload" not in r and "answer" not in r for r in rows)
    assert report_index(storage) == rows
    assert report_detail(storage, plan.id, reports[0].id) == reports[0]
    assert report_detail(storage, "other", reports[0].id) is None
    assert report_index(storage, "missing") == []
    assert report_detail(Storage(tmp_path / "other.sqlite3"), plan.id, reports[0].id) is None


def test_run_summary_counts_logical_samples_and_epoch_blocks(records):
    storage, config, task, plan = records
    assert run_summary(storage, plan, [], {})["remaining_samples"] == 1
    attempts = [AttemptResult(run_id=plan.id, model_id=config.id, task_id=task.id,
                              sample_index=0, attempt_index=i,
                              status="uncertain" if i else "failed") for i in range(2)]
    for index, attempt in enumerate(attempts):
        storage.save_attempt(attempt)
        # 真实 runner intent 未存 purpose；历史记录也必须能读取。
        storage.save_object("request_intent", f"intent-{index}", {"run_id": plan.id})
    storage.save_object("request_intent", "probe", {"run_id": plan.id, "purpose": "probe"})
    storage.save_object("request_intent", "other", {"run_id": "other", "purpose": "evaluation"})
    epoch = {"epoch": 2}
    storage.save_object("model_dispatch_epoch", plan.id, epoch)
    storage.save_object("model_dispatch_block", "active",
                        {"run_id": plan.id, "model_id": config.id, "epoch": epoch})
    storage.save_object("model_dispatch_block", "old",
                        {"run_id": plan.id, "model_id": "old-model", "epoch": {"epoch": 1}})
    summary = run_summary(storage, plan, attempts, {attempts[0].id: {"status": "completed"}})
    assert summary["observed_samples"] == 1 and summary["remaining_samples"] == 0
    assert summary["attempts"] == 2 and summary["reserved_requests"] == 2
    assert summary["uncertain"] == 1 and summary["judge_pending_or_invalid"] == 1
    assert summary["blocked_models"] == [config.id] and summary["status"] == "queued"
