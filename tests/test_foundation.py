"""公共契约与 SQLite 回归；种子代码仅作为文本，不在宿主执行。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from coding_eval.datasets import list_tasks, load_builtin_tasks, save_task
from coding_eval.models import (
    AttemptMetrics, AttemptResult, JudgeResult, ModelConfig, ReportVersion,
    RunPlan, RunStatus, ScoringConfig, Snapshot, TaskDefinition, Weight,
    canonical_json, content_hash, reject_secret_fields, validate_relative_path,
    validate_transition,
)
from coding_eval.runner import latest_models
from coding_eval.storage import AppPaths, Storage


@pytest.fixture
def model():
    return ModelConfig(
        id="model", display_name="测试模型", protocol="openai",
        url="https://example.invalid/v1", model_id="exact-model",
        api_key_env="TEST_MODEL_KEY", parameters={"temperature": 0.2},
    )


@pytest.fixture
def plan(model):
    task = load_builtin_tasks()[0]
    return RunPlan(
        id="run", model_snapshots=(Snapshot.capture(model),),
        task_snapshots=(Snapshot.capture(task),), diagnostic=True,
        scoring=ScoringConfig(scenario_weights=(Weight(name="C01", weight=1),)),
        max_requests=3,
    )


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "eval.sqlite3")


def test_canonical_hash_and_snapshot_freeze(model):
    assert canonical_json({"b": 2, "a": "中文"}) == '{"a":"中文","b":2}'
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    snapshot = Snapshot.capture(model)
    model.parameters["temperature"] = 0.8
    assert snapshot.restore().parameters == {"temperature": 0.2}
    assert snapshot.version_hash != model.version_hash
    assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize("field,value", [
    ("object_id", "other"), ("version_hash", "forged"), ("object_type", "task"),
])
def test_snapshot_rejects_tampering(model, field, value):
    value_dict = Snapshot.capture(model).model_dump()
    value_dict[field] = value
    with pytest.raises(ValidationError):
        Snapshot.model_validate(value_dict)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_numbers_cannot_be_serialized(value):
    with pytest.raises(ValueError):
        canonical_json({"value": value})
    with pytest.raises(ValidationError):
        AttemptMetrics(total_seconds=value)


@pytest.mark.parametrize("weights", [
    (), (("functionality", 0.9),), (("functionality", 0.5), ("functionality", 0.5)),
    (("unknown", 1),), (("functionality", -0.1), ("security", 1.1)),
])
def test_scoring_rejects_invalid_weights(weights):
    with pytest.raises(ValidationError):
        ScoringConfig(dimension_weights=[{"name": n, "weight": w} for n, w in weights])


def test_scoring_supports_explicit_partial_template():
    scoring = ScoringConfig(
        template_id="custom", dimension_weights=(Weight(name="functionality", weight=1),),
        scenario_weights=(Weight(name="custom", weight=1),),
    )
    assert ScoringConfig.model_validate_json(scoring.model_dump_json()) == scoring


@pytest.mark.parametrize("url", [
    "file:///tmp/model", "https://", "https://user:password@example.com",
    "https://example.com?api_key=secret", "https://example.com#secret",
])
def test_model_rejects_invalid_or_secret_urls(model, url):
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({**model.model_dump(), "url": url})


@pytest.mark.parametrize("field", ["API-key", "Authorization", "password", "secret", "access_token", "headers"])
def test_nested_credentials_cannot_be_persisted(storage, field):
    payload = {"items": [{"options": {field: "do-not-store"}}]}
    with pytest.raises(ValueError, match="凭据"):
        reject_secret_fields(payload)
    with pytest.raises(ValueError, match="凭据"):
        storage.save_object("model", "id", payload)
    assert storage.list_objects("model") == []
    assert b"do-not-store" not in storage.database.read_bytes()


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../x", "a/../b", "a//b", "./x", "a\\b", "a:b", "a\x00b"])
def test_relative_paths_reject_escape(path):
    with pytest.raises(ValueError, match="相对路径"):
        validate_relative_path(path)


def test_app_paths_env_and_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path / "state"))
    paths = AppPaths.from_env()
    paths.ensure()
    assert paths.database == paths.root / "coding_eval.sqlite3"
    assert paths.artifact_path("run/output.txt") == paths.artifacts / "run/output.txt"
    (paths.artifacts / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="逃逸"):
        paths.artifact_path("escape/output.txt")
    monkeypatch.delenv("CODING_EVAL_DATA_DIR")
    monkeypatch.chdir(tmp_path)
    assert AppPaths.from_env().root == tmp_path / "data"


@pytest.mark.parametrize("changes", [
    {"model_snapshots": []}, {"task_snapshots": []}, {"max_requests": 2},
    {"max_retries": 1}, {"concurrency": 0}, {"repeats": 0}, {"diagnostic": False},
    {"scoring": ScoringConfig().model_dump()},
])
def test_plan_enforces_snapshot_budget_and_readiness(plan, changes):
    with pytest.raises(ValidationError):
        RunPlan.model_validate({**plan.model_dump(), **changes})


def test_plan_rejects_disabled_duplicate_and_wrong_kind(plan, model):
    disabled = Snapshot.capture(model.model_copy(update={"enabled": False}))
    for snapshots in ((disabled,), plan.model_snapshots * 2, plan.task_snapshots):
        with pytest.raises(ValidationError):
            RunPlan.model_validate({**plan.model_dump(), "model_snapshots": snapshots})
    assert plan.expected_requests == 3


def test_ready_task_binds_evidence_and_scoring():
    task = load_builtin_tasks()[0]
    # 合成证据仅用于公共契约测试，不能作为真实题库的验收记录。
    value = task.model_dump(mode="json")
    value.update(status="ready", validation_evidence=["synthetic-only"],
                 validated_content_hash=task.validation_hash)
    ready = TaskDefinition.model_validate(value)
    assert ready.ready
    for changes in (
        {"requirements": "changed"}, {"validation_evidence": []}, {"negative_files": {}},
        {"rubric": [*value["rubric"], value["rubric"][0]]},
        {"scoring": {"dimension_weights": [{"name": "functionality", "weight": 1}]}},
    ):
        with pytest.raises(ValidationError):
            TaskDefinition.model_validate({**value, **changes})


@pytest.mark.parametrize("current", list(RunStatus))
@pytest.mark.parametrize("target", list(RunStatus))
def test_run_transition_matrix(current, target):
    allowed = {
        "queued": {"running", "failed", "cancelled"},
        "running": {"completed", "failed", "cancelled"},
        "failed": {"queued"}, "cancelled": {"queued"}, "completed": set(),
    }
    if target in allowed[current]:
        validate_transition(current, target)
    else:
        with pytest.raises(ValueError, match="非法状态迁移"):
            validate_transition(current, target)


@pytest.mark.parametrize("kind", ["model", "task", "judge"])
def test_save_object_reactivates_old_hash_without_overwriting_history(storage, kind):
    first, second = {"value": 1}, {"value": 2}
    first_hash = storage.save_object(kind, "same", first)
    first_row = storage.list_objects(kind)[0]
    second_hash = storage.save_object(kind, "same", second)
    assert storage.save_object(kind, "same", first) == first_hash
    reopened = Storage(storage.database)
    assert reopened.get_object(kind, "same") == first
    assert reopened.get_object(kind, "same", second_hash) == second
    assert reopened.get_object(kind, "same", first_hash) == first
    rows = reopened.list_objects(kind)
    assert len(rows) == 2
    assert rows[-1] == first_row
    assert {row["version_hash"] for row in rows} == {first_hash, second_hash}
    assert reopened.get_object(kind, "absent") is None
    assert reopened.get_object(kind, "same", "absent") is None


def test_storage_latest_consumers_disable_enable_and_task_revert(storage, model):
    original = model.model_dump(mode="json")
    storage.save_object("model", model.id, original)
    storage.save_object("model", model.id, {**original, "enabled": False})
    assert not latest_models(storage)[0].enabled
    storage.save_object("model", model.id, original)
    assert latest_models(storage) == [model]
    task = load_builtin_tasks()[0]
    save_task(storage, task)
    save_task(storage, task.model_copy(update={"title": "edited"}))
    save_task(storage, task)
    assert list_tasks(storage) == [task]
    assert len(storage.list_objects("task")) == 2


def test_storage_migrates_legacy_versions_without_resetting_pointer(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE objects (kind TEXT, object_id TEXT, version_hash TEXT, "
            "payload_json TEXT, created_at TEXT, PRIMARY KEY(kind, object_id, version_hash))"
        )
        for index in (1, 2):
            payload = {"revision": index}
            connection.execute("INSERT INTO objects VALUES (?, ?, ?, ?, ?)",
                               ("model", "legacy", content_hash(payload), canonical_json(payload), "original"))
    storage = Storage(database)
    assert storage.get_object("model", "legacy") == {"revision": 2}
    storage.save_object("model", "legacy", {"revision": 1})
    assert Storage(database).get_object("model", "legacy") == {"revision": 1}
    assert {r["created_at"] for r in storage.list_objects("model")} == {"original"}


def test_concurrent_storage_writers_keep_versions_and_final_activation(storage):
    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(lambda i: storage.save_object("model", "shared", {"value": i}), range(12)))
    assert len(set(versions)) == len(storage.list_objects("model")) == 12
    storage.save_object("model", "shared", {"value": 0})
    assert storage.get_object("model", "shared") == {"value": 0}


def test_raw_records_append_only_and_run_compare_and_swap(storage, plan):
    storage.save_run(plan)
    assert storage.get_run(plan.id) == plan
    assert storage.get_run("missing") is None
    assert storage.list_runs()[0]["plan_hash"] == plan.version_hash
    assert storage.get_run_status(plan.id) == RunStatus.QUEUED
    with pytest.raises(KeyError):
        storage.get_run_status("missing")
    storage.transition_run(plan.id, RunStatus.QUEUED, RunStatus.RUNNING)
    with pytest.raises(ValueError, match="状态已发生变化"):
        storage.transition_run(plan.id, RunStatus.QUEUED, RunStatus.RUNNING)
    result = AttemptResult(
        id="attempt", run_id=plan.id, model_id="model", task_id=plan.task_snapshots[0].object_id,
        sample_index=0, status="completed", raw_output="原始代码\n<script>not executed</script>",
        raw_response={"usage": {"input": 0}}, metrics=AttemptMetrics(input_tokens=0),
        queue_seconds=0.1, logical_task_seconds=1.2,
    )
    storage.save_attempt(result)
    child = result.model_copy(update={"id": "retry", "attempt_index": 1})
    storage.save_attempt(child)
    report = ReportVersion(run_id=plan.id, source_plan_hash=plan.version_hash, scoring=plan.scoring)
    storage.save_report(report)
    for operation, value in ((storage.save_run, plan), (storage.save_attempt, result), (storage.save_report, report)):
        with pytest.raises(sqlite3.IntegrityError):
            operation(value)
    with pytest.raises(sqlite3.IntegrityError):
        storage.save_attempt(result.model_copy(update={"id": "foreign", "run_id": "missing"}))
    reopened = Storage(storage.database)
    assert reopened.list_attempts(plan.id) == [result, child]
    assert reopened.list_attempts(plan.id)[0].metrics.cache_read_tokens is None
    assert reopened.list_reports(plan.id) == [report]
    assert reopened.list_attempts("missing") == reopened.list_reports("missing") == []


def test_storage_revalidates_mutated_models(storage, plan):
    with pytest.raises(ValidationError):
        storage.save_run(plan.model_copy(update={"max_requests": 0}))
    assert storage.list_runs() == []
    with pytest.raises(ValueError, match="不支持"):
        Storage(":memory:")


@pytest.mark.parametrize("status,score,passed,gates", [
    ("blocked", 0, None, ()), ("judge_error", None, False, ()),
    ("completed", None, True, ()), ("completed", 50, False, ("gate",)),
])
def test_judge_failures_never_masquerade_as_scores(status, score, passed, gates):
    with pytest.raises(ValidationError):
        JudgeResult(attempt_id="attempt", task_version_hash="hash", judge_version="v1",
                    status=status, quality_score=score, all_required_passed=passed, gate_failures=gates)
