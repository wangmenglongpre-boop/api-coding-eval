"""Synthetic evidence only: no model API calls, Docker, or candidate execution."""

import csv
import io
import json
import random
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from coding_eval.models import (
    AttemptMetrics, AttemptResult, CheckResult, Dimension, JudgeResult, ModelConfig,
    OutputFormat, ReportVersion, RubricItem, RunPlan, ScoringConfig, Snapshot,
    TaskDefinition, Weight,
)
from coding_eval.reports import (
    build_report, explanation_summary, export_csv, export_html, export_json,
    group_reports, metric_summary, rescore_report,
)
from coding_eval.storage import AppPaths, Storage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_plan(scenarios=("S1",), repeats=1, models=("alpha",), **changes):
    names = sorted(set(scenarios))
    scoring = ScoringConfig(
        dimension_weights=(Weight(name="functionality", weight=.75), Weight(name="security", weight=.25)),
        scenario_weights=tuple(Weight(name=s, weight=1 / len(names)) for s in names),
    )
    tasks = []
    for index, scenario in enumerate(scenarios):
        task = TaskDefinition(
            id=f"t{index}", scenario_id=scenario, title=f"Synthetic {index}",
            description="Synthetic fixture, not a real API evaluation", system_prompt="Fixture",
            requirements="Synthetic requirement", public_files={"solution.py": "# PUBLIC"},
            reference_files={"solution.py": "# HIDDEN_REFERENCE"},
            test_files={"checks.py": "# HIDDEN_TESTS"},
            negative_files={"negative": {"solution.py": "# INERT_NEGATIVE"}},
            output_format=OutputFormat(kind="files", allowed_paths=("solution.py",), instructions="Fixture"),
            rubric=tuple(RubricItem(
                check=f"{dimension}{i}", dimension=dimension, description="Synthetic check",
            ) for dimension in ("functionality", "security") for i in range(4)),
            scoring=scoring,
        )
        # Structural ready fixture only; does not claim to have executed these files.
        task = task.model_copy(update={
            "status": "ready", "validation_evidence": ("SYNTHETIC_TEST_FIXTURE_NOT_EXECUTED",),
            "validated_content_hash": task.validation_hash,
        })
        tasks.append(task)
    values = dict(
        created_at=NOW, model_snapshots=tuple(Snapshot.capture(ModelConfig(
            id=m, display_name=m, protocol="openai", url="https://synthetic.invalid",
            model_id=m, visible_version="unknown",
        )) for m in models),
        task_snapshots=tuple(Snapshot.capture(t) for t in tasks), scoring=scoring,
        repeats=repeats, max_retries=2, max_requests=len(tasks) * repeats * len(models) * 3,
        random_seed=17,
    )
    values.update(changes)
    return RunPlan(**values)


def evidence(plan, values=None):
    attempts, judges = [], []
    for model in plan.model_snapshots:
        for task_snapshot in plan.task_snapshots:
            task = task_snapshot.restore()
            for index in range(plan.repeats):
                f, s = values.get((model.object_id, task.id, index), (100, 100)) if values else (100, 100)
                attempt = AttemptResult(
                    id=f"{model.object_id}-{task.id}-{index}", run_id=plan.id,
                    model_id=model.object_id, task_id=task.id, sample_index=index, status="completed",
                    created_at=NOW, raw_output="SYNTHETIC ANSWER",
                    raw_response={"synthetic": True, "content": "SYNTHETIC ANSWER"},
                    metrics=AttemptMetrics(
                        total_seconds=2 + index, output_tokens=10 + index, stream_complete=True,
                        usage_mapping_version="synthetic-v1", raw_usage={"output": 10 + index},
                    ),
                )
                scores = {"functionality": f, "security": s}
                checks = tuple(CheckResult(
                    check=r.check, passed=int(r.check[-1]) < scores[r.dimension.value] / 25,
                    evidence="SYNTHETIC_CHECK",
                ) for r in task.rubric)
                judge = JudgeResult(
                    id=f"judge-{attempt.id}", attempt_id=attempt.id, task_version_hash=task.version_hash,
                    judge_version="synthetic-judge-v1", status="completed", checks=checks,
                    dimension_scores=scores, quality_score=.75 * f + .25 * s,
                    all_required_passed=all(c.passed for c in checks),
                )
                attempts.append(attempt)
                judges.append(judge)
    return attempts, judges


def report_for(plan, attempts=None, judges=None, **kwargs):
    if attempts is None:
        attempts, judges = evidence(plan)
    return build_report(plan, attempts, judges or [], bootstrap_iterations=200, **kwargs)


def test_hierarchical_scoring_dimensions_ranking_and_ties():
    plan = make_plan(("S1", "S1", "S2"), repeats=2, models=("alpha", "beta"))
    config = plan.scoring.model_copy(update={
        "scenario_weights": (Weight(name="S1", weight=.25), Weight(name="S2", weight=.75)),
    })
    plan = plan.model_copy(update={"scoring": config})
    attempts, judges = evidence(plan, {
        ("alpha", "t0", 0): (0, 0), ("alpha", "t0", 1): (100, 0),
        ("alpha", "t1", 0): (100, 100), ("alpha", "t1", 1): (100, 100),
        ("alpha", "t2", 0): (0, 100), ("alpha", "t2", 1): (0, 100),
    })
    report = report_for(plan, attempts, judges)
    model = report.payload["models"]["alpha"]
    assert report.status == "complete"
    assert model["tasks"]["t0"]["quality_score"] == 37.5
    assert model["scenarios"]["S1"]["quality_score"] == 68.75
    assert model["scenarios"]["S2"]["quality_score"] == 25
    assert model["quality_score"] == 35.9375
    assert model["dimension_scores"] == {"functionality": 18.75, "security": 87.5}
    assert model["all_required_passed_rate"] == pytest.approx(2 / 6)
    assert [r["model_id"] for r in report.payload["ranking"]] == ["beta", "alpha"]
    assert report.payload["coverage"]["expected"] == 12
    assert report.payload["exploratory"]
    tie = report_for(make_plan(models=("alpha", "beta")))
    assert [r["rank"] for r in tie.payload["ranking"]] == [1, 1]


@pytest.mark.parametrize("condition,row_status,report_status,score", [
    ("missing", "missing", "provisional", None),
    ("missing_judge", "missing_judge", "provisional", None),
    ("blocked", "blocked", "provisional", None),
    ("judge_error", "judge_error", "invalid", None),
    ("uncertain", "uncertain", "provisional", None),
    ("cancelled", "cancelled", "provisional", None),
    ("failed", "pending_retry", "provisional", None),
])
def test_completeness_and_error_taxonomy(condition, row_status, report_status, score):
    plan = make_plan()
    attempts, judges = evidence(plan)
    if condition == "missing":
        attempts, judges = [], []
    elif condition == "missing_judge":
        judges = []
    elif condition in {"blocked", "judge_error"}:
        judges = [judges[0].model_copy(update={
            "status": condition, "quality_score": None, "all_required_passed": None,
        })]
    else:
        attempts = [attempts[0].model_copy(update={"status": condition})]
    report = report_for(plan, attempts, judges)
    row = report.payload["samples"][0]
    model = report.payload["models"]["alpha"]
    assert (row["status"], report.status, row["quality_score"]) == (row_status, report_status, score)
    assert model["quality_score"] == score
    assert row["excluded"] is (score is None)
    assert model["missing_rate"] == (1 if score is None else 0)
    assert model["coverage"] == (0 if score is None else 1)
    assert model["all_required_passed_rate"] == (None if score is None else 0)
    assert bool(report.payload["ranking"]) is (report_status == "complete")


def test_missing_task_never_silently_reduces_full_score_denominator():
    plan = make_plan(("S1", "S2"), repeats=2)
    attempts, judges = evidence(plan)
    report = report_for(plan, attempts[:1], judges[:1])
    model = report.payload["models"]["alpha"]
    assert model["quality_score"] is None
    assert model["observed_quality_score"] == 100
    assert model["scenarios"]["S1"]["quality_score"] is None
    assert model["scenarios"]["S2"]["quality_score"] is None
    assert model["coverage"] == .25 and model["missing_rate"] == .75
    assert model["quality_interval"]["lower"] is None
    assert len(report.payload["samples"]) == 4 and report.payload["ranking"] == []


def test_retry_selects_last_completed_not_best_and_preserves_all_metrics():
    plan = make_plan()
    attempts, judges = evidence(plan)
    poor_attempts, poor_judges = evidence(plan, {("alpha", "t0", 0): (0, 0)})
    latest = poor_attempts[0].model_copy(update={"id": "retry", "attempt_index": 1})
    poor = poor_judges[0].model_copy(update={"id": "retry-judge", "attempt_id": latest.id})
    failed = latest.model_copy(update={"id": "failed-tail", "attempt_index": 2, "status": "failed"})
    report = report_for(plan, [failed, latest, *attempts], [poor, *judges])
    row = report.payload["samples"][0]
    assert row["attempt_id"] == "retry" and row["quality_score"] == 0
    assert row["attempt_ids"] == [attempts[0].id, "retry", "failed-tail"]
    assert report.payload["models"]["alpha"]["actual_requests"] == 3
    metrics = report.payload["models"]["alpha"]["metrics"]["output_tokens"]
    assert metrics["all_attempts"]["sum"] == 30
    assert metrics["completed_attempts"]["n"] == 2
    assert metrics["noncompleted_attempts"]["n"] == 1
    assert report_for(plan, [latest, *attempts, failed], [*judges, poor]).payload == report.payload


@pytest.mark.parametrize("uncertain_index", [0, 1])
def test_uncertain_anywhere_cannot_be_disguised_by_a_completed_retry(uncertain_index):
    plan = make_plan()
    attempts, judges = evidence(plan)
    completed = attempts[0].model_copy(update={"attempt_index": 1 - uncertain_index})
    uncertain = attempts[0].model_copy(update={"id": "uncertain", "attempt_index": uncertain_index, "status": "uncertain"})
    report = report_for(plan, [completed, uncertain], judges)
    assert report.payload["samples"][0]["status"] == "uncertain"
    assert report.payload["samples"][0]["quality_score"] is None
    assert report.payload["ranking"] == []


@pytest.mark.parametrize("mutation", ["version", "missing_dimension", "missing_checks", "duplicate_checks", "pass_flag"])
def test_invalid_judge_evidence_is_excluded(mutation):
    plan = make_plan()
    attempts, judges = evidence(plan)
    updates = {
        "version": {"task_version_hash": "old-task-version"},
        "missing_dimension": {"dimension_scores": {Dimension.FUNCTIONALITY: 100}},
        "missing_checks": {"checks": ()},
        "duplicate_checks": {"checks": (*judges[0].checks, judges[0].checks[0])},
        "pass_flag": {"all_required_passed": False},
    }
    judges = [judges[0].model_copy(update=updates[mutation])]
    report = report_for(plan, attempts, judges)
    assert report.status == "invalid"
    assert report.payload["samples"][0]["status"] == "judge_error"
    assert report.payload["models"]["alpha"]["quality_score"] is None
    assert report.payload["ranking"] == []


def test_gate_zero_recomputes_diagnostic_dimensions_in_posthoc_scoring():
    plan = make_plan()
    attempts, judges = evidence(plan)
    judges = [judges[0].model_copy(update={
        "checks": (), "gate_failures": ("answer_format",), "quality_score": 0,
        "all_required_passed": False,
    })]
    report = report_for(plan, attempts, judges, scoring=plan.scoring)
    assert report.post_hoc
    assert report.payload["samples"][0]["quality_score"] == 0
    assert report.payload["samples"][0]["dimension_scores"] == {"functionality": 0, "security": 0}
    assert report.payload["models"]["alpha"]["all_required_passed_rate"] == 0


def test_diagnostic_and_mixed_judge_versions_prevent_ranking():
    diagnostic = report_for(make_plan(diagnostic=True))
    assert diagnostic.status == "invalid" and diagnostic.payload["ranking"] == []
    plan = make_plan(repeats=2)
    attempts, judges = evidence(plan)
    judges[1] = judges[1].model_copy(update={"judge_version": "different"})
    mixed = report_for(plan, attempts, judges)
    assert mixed.status == "invalid" and mixed.payload["ranking"] == []


@pytest.mark.parametrize("field,value,message", [
    ("run_id", "foreign", "different runs"),
    ("task_id", "foreign", "coverage"),
    ("model_id", "foreign", "coverage"),
    ("sample_index", 1, "coverage"),
    ("attempt_index", 3, "retry budget"),
])
def test_frozen_plan_rejects_foreign_or_out_of_budget_attempts(field, value, message):
    plan = make_plan()
    attempts, judges = evidence(plan)
    with pytest.raises(ValueError, match=message):
        report_for(plan, [attempts[0].model_copy(update={field: value})], judges)


def test_duplicate_requests_rejected_and_control_probe_ignored():
    plan = make_plan()
    attempts, judges = evidence(plan)
    with pytest.raises(ValueError, match="Duplicate"):
        report_for(plan, attempts * 2, judges)
    same_slot = attempts[0].model_copy(update={"id": "other-id"})
    with pytest.raises(ValueError, match="Duplicate"):
        report_for(plan, [*attempts, same_slot], judges)
    controls = [attempts[0].model_copy(update={"purpose": purpose, "run_id": "control"})
                for purpose in ("control", "probe")]
    report = report_for(plan, [*attempts, *controls], judges)
    assert report.payload["ignored_requests"] == {"control": 1, "probe": 1}
    assert len(report.payload["attempts"]) == 1
    assert report.payload["models"]["alpha"]["actual_requests"] == 1


def test_metrics_missingness_percentiles_and_raw_fields():
    summary = metric_summary([1, 2, None, 5])
    assert summary == {
        "n": 3, "total": 4, "missing": 1, "missing_rate": .25,
        "mean": 8 / 3, "median": 2, "p95": pytest.approx(4.7), "sum": 8,
    }
    assert metric_summary([])["missing_rate"] is None
    assert metric_summary([None])["sum"] is None
    plan = make_plan(repeats=2)
    attempts, judges = evidence(plan)
    attempts[1] = attempts[1].model_copy(update={
        "status": "failed", "metrics": AttemptMetrics(total_seconds=9, stream_complete=False),
    })
    report = report_for(plan, attempts, judges)
    metrics = report.payload["models"]["alpha"]["metrics"]
    assert metrics["cache_read_tokens"]["all_attempts"]["missing_rate"] == 1
    assert metrics["first_content_seconds"]["all_attempts"]["mean"] is None
    assert metrics["total_seconds"]["all_attempts"]["mean"] == 5.5
    assert metrics["total_seconds"]["completed_attempts"]["mean"] == 2
    assert metrics["total_seconds"]["noncompleted_attempts"]["mean"] == 9
    assert report.payload["attempts"][0]["raw_response"] == attempts[0].raw_response
    assert report.payload["attempts"][0]["metrics"]["raw_usage"] == {"output": 10}


def test_logical_latency_uses_final_cumulative_request_once_per_planned_sample():
    plan = make_plan(repeats=2)
    attempts, judges = evidence(plan)
    first = attempts[0].model_copy(update={"logical_task_seconds": 2})
    last = first.model_copy(update={
        "id": "last", "attempt_index": 1, "status": "failed", "logical_task_seconds": 9,
    })
    report = report_for(plan, [last, first], judges)
    summary = report.payload["models"]["alpha"]["logical_task_seconds"]
    assert summary == {
        "n": 1, "total": 2, "missing": 1, "missing_rate": .5,
        "mean": 9, "median": 9, "p95": 9, "sum": 9,
    }
    assert report.payload["samples"][0]["attempt_id"] == first.id


def test_task_cluster_bootstrap_reproducible_and_not_repeat_pseudoreplication():
    plan = make_plan(("S1", "S1"), repeats=1)
    attempts, judges = evidence(plan, {("alpha", "t0", 0): (0, 0)})
    before = random.getstate()
    first = report_for(plan, attempts, judges)
    assert random.getstate() == before
    interval = first.payload["models"]["alpha"]["quality_interval"]
    assert interval["task_clusters"] == 2
    assert (interval["lower"], interval["upper"]) == (0, 100)
    assert interval["limited_task_diversity"] is False
    assert report_for(plan, list(reversed(attempts)), list(reversed(judges))).payload == first.payload
    repeated = make_plan(("S1", "S1"), repeats=20)
    a, j = evidence(repeated, {("alpha", "t0", index): (0, 0) for index in range(20)})
    assert report_for(repeated, a, j).payload["models"]["alpha"]["quality_interval"] == interval
    singleton = report_for(make_plan()).payload["models"]["alpha"]["quality_interval"]
    assert singleton["limited_task_diversity"] and singleton["lower"] == singleton["upper"] == 100


@pytest.mark.parametrize("kwargs,message", [
    ({"bootstrap_iterations": 0}, "bootstrap_iterations"),
    ({"bootstrap_iterations": True}, "bootstrap_iterations"),
    ({"bootstrap_iterations": 100001}, "bootstrap_iterations"),
    ({"bootstrap_seed": .5}, "bootstrap_seed"),
])
def test_bootstrap_argument_validation(kwargs, message):
    plan = make_plan()
    attempts, judges = evidence(plan)
    with pytest.raises(ValueError, match=message):
        build_report(plan, attempts, judges, **kwargs)


@pytest.mark.parametrize("updates,message", [
    ({"template_id": "different"}, "template"),
    ({"dimension_weights": (Weight(name="functionality", weight=1),)}, "Dimension"),
    ({"scenario_weights": (Weight(name="other", weight=1),)}, "Scenario"),
    ({"dimension_weights": (Weight(name="functionality", weight=.8), Weight(name="security", weight=.3))}, "权重之和"),
])
def test_posthoc_weight_validation_revalidates_even_model_copy(updates, message):
    plan = make_plan()
    with pytest.raises(ValueError, match=message):
        report_for(plan, scoring=plan.scoring.model_copy(update=updates))


@pytest.mark.parametrize("weights", [
    [{"name": "functionality", "weight": -1}],
    [{"name": "functionality", "weight": float("nan")}],
    [{"name": "functionality", "weight": .5}, {"name": "functionality", "weight": .5}],
    [{"name": "not_a_dimension", "weight": 1}],
])
def test_scoring_rejects_negative_nan_duplicate_unknown_weights(weights):
    with pytest.raises(ValueError):
        ScoringConfig(dimension_weights=weights)


def test_storage_latest_judge_and_posthoc_append_preserve_parent_evidence(tmp_path):
    storage = Storage(tmp_path / "report.sqlite")
    plan = make_plan()
    storage.save_run(plan)
    attempts, judges = evidence(plan, {("alpha", "t0", 0): (100, 0)})
    for attempt in attempts:
        storage.save_attempt(attempt)
    original = judges[0].model_copy(update={"id": "old"})
    storage.save_object("judge", attempts[0].id, original.model_dump(mode="json"))
    storage.save_object("judge", attempts[0].id, judges[0].model_dump(mode="json"))
    storage.save_object("control", "separate-control", {"synthetic": True})
    parent = report_for(plan, storage.list_attempts(plan.id), storage.list_objects("judge"))
    storage.save_report(parent)
    original_json = export_json(parent)
    assert parent.payload["samples"][0]["judge_id"] == judges[0].id
    assert parent.payload["models"]["alpha"]["quality_score"] == 75
    _, new_judges = evidence(plan, {("alpha", "t0", 0): (0, 0)})
    storage.save_object("judge", attempts[0].id, new_judges[0].model_dump(mode="json"))
    scoring = plan.scoring.model_copy(update={"dimension_weights": (
        Weight(name="functionality", weight=.25), Weight(name="security", weight=.75),
    )})
    child = rescore_report(storage, parent, scoring)
    assert child.id != parent.id and child.parent_report_id == parent.id and child.post_hoc
    assert child.payload["models"]["alpha"]["quality_score"] == 25
    assert child.payload["judges"] == parent.payload["judges"]
    assert child.payload["attempts"] == parent.payload["attempts"]
    assert storage.list_reports(plan.id) == [parent, child]
    assert export_json(storage.list_reports(plan.id)[0]) == original_json
    assert plan.scoring.dimension_weights[0].weight == .75
    assert len(group_reports([parent, child])) == 2
    with pytest.raises(ValueError, match="saved"):
        rescore_report(storage, parent.model_copy(update={"id": "unsaved"}), scoring)
    with pytest.raises(ValueError, match="source plan"):
        rescore_report(storage, parent.model_copy(update={"source_plan_hash": "tampered"}), scoring)


def test_judge_object_key_mapping_and_latest_failed_judgement():
    plan = make_plan()
    attempts, judges = evidence(plan)
    assert report_for(plan, attempts, {attempts[0].id: judges[0]}).status == "complete"
    with pytest.raises(ValueError, match="object_id"):
        report_for(plan, attempts, {"wrong-key": judges[0]})
    newer = judges[0].model_copy(update={"status": "judge_error", "quality_score": None, "all_required_passed": None})
    report = report_for(plan, attempts, [judges[0], newer])
    assert report.status == "invalid" and report.payload["samples"][0]["quality_score"] is None


def test_report_comparison_groups_separate_versions_budgets_and_coverage():
    base = make_plan()
    first = report_for(base)
    same = report_for(base.model_copy(update={"id": "another-run"}))
    budget = report_for(make_plan(budget_group="expensive"))
    repeats = report_for(make_plan(repeats=2))
    different_tasks = report_for(make_plan(("S1", "S1")))
    task = base.task_snapshots[0].restore()
    changed = task.model_copy(update={"title": "New version", "status": "draft"})
    changed = changed.model_copy(update={"status": "ready", "validated_content_hash": changed.validation_hash})
    version = report_for(base.model_copy(update={"task_snapshots": (Snapshot.capture(changed),)}))
    groups = group_reports([first, same, budget, repeats, different_tasks, version])
    assert len(groups) == 5
    assert groups[first.payload["comparison_key"]] == [first, same]


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


def test_json_html_exports_preserve_raw_and_escape_every_dynamic_surface():
    plan = make_plan(repeats=2)
    attempts, judges = evidence(plan)
    attack = '</pre><script>alert("x")</script><img src=x onerror=alert(1)> &'
    attempts[0] = attempts[0].model_copy(update={"raw_output": attack, "raw_response": {"body": attack}})
    report = report_for(plan, attempts[:1], judges[:1])
    restored = ReportVersion.model_validate_json(export_json(report))
    assert restored == report and restored.payload["attempts"][0]["raw_output"] == attack
    document = export_html(report)
    parser = Tags()
    parser.feed(document)
    assert "script" not in parser.tags and "img" not in parser.tags
    assert not any(name.startswith("on") for name, _ in parser.attributes)
    assert "&lt;script&gt;" in document and "Content-Security-Policy" in document
    assert attack not in document and "missing" in document
    assert "raw_response" in document and "SYNTHETIC_CHECK" in document


@pytest.mark.parametrize("attack", [
    '=HYPERLINK("https://bad")', "+SUM(1,1)", "-1+1", "@SUM(1,1)",
    " \t=1+1", "\r=1+1", "\n=1+1", "\ufeff=1+1", "\tplain text",
])
def test_csv_formula_injection_protection_and_roundtrip_all_retries(attack):
    plan = make_plan(repeats=2)
    attempts, judges = evidence(plan)
    attempts[0] = attempts[0].model_copy(update={"raw_output": attack, "raw_response": attack})
    retry = attempts[0].model_copy(update={"id": "retry", "attempt_index": 1, "status": "failed"})
    report = report_for(plan, [attempts[0], retry], judges[:1])
    rows = list(csv.DictReader(io.StringIO(export_csv(report))))
    assert len(rows) == 2
    assert rows[0]["raw_output"] == "'" + attack
    assert rows[0]["raw_response"] == "'" + attack
    assert json.loads(rows[0]["attempts"])[0]["raw_output"] == attack
    assert len(json.loads(rows[0]["attempts"])) == 2
    assert rows[1]["status"] == "missing" and rows[1]["quality_score"] == ""
    assert rows[0]["quality_score"] == "100.0"


def test_explanation_summary_allowlist_contains_ids_not_answers_or_hidden_data():
    report = report_for(make_plan())
    summary = explanation_summary(report)
    encoded = json.dumps(summary)
    assert summary["report_id"] == report.id and summary["run_id"] == report.run_id
    assert summary["comparison_key"] == report.payload["comparison_key"]
    assert "t0" in summary["models"]["alpha"]["tasks"]
    assert summary["models"]["alpha"]["quality_score"] == 100
    for secret in ("SYNTHETIC ANSWER", "HIDDEN_REFERENCE", "HIDDEN_TESTS", "raw_response", "api_key_env"):
        assert secret not in encoded


def _page_script(database, root):
    from pathlib import Path
    from app.report_page import render
    from coding_eval.storage import AppPaths, Storage
    render(Storage(database), AppPaths(Path(root), Path(database), Path(root) / "artifacts"))


def test_report_page_offline_generation_selectors_reweight_and_exports(tmp_path):
    from streamlit.testing.v1 import AppTest

    paths = AppPaths(tmp_path, tmp_path / "page.sqlite", tmp_path / "artifacts")
    storage = Storage(paths.database)
    app = AppTest.from_function(_page_script, args=(str(paths.database), str(tmp_path)))
    app.run()
    assert not app.exception
    assert "尚无运行" in app.info[0].value
    plan = make_plan(("S1", "S2"), repeats=2, models=("alpha", "beta"))
    storage.save_run(plan)
    attempts, judges = evidence(plan, {("alpha", "t0", 0): (100, 0)})
    for attempt in attempts:
        storage.save_attempt(attempt)
    for judge in judges:
        storage.save_object("judge", judge.attempt_id, judge.model_dump(mode="json"))
    app.run()
    assert not app.exception
    app.button(key=f"build-report-{plan.id}").click().run(timeout=30)
    assert not app.exception
    parent = storage.list_reports(plan.id)[0]
    assert len(app.get("download_button")) == 3
    assert len(app.dataframe) >= 7
    app.radio(key=f"report-section-{parent.id}").set_value("答案对比").run()
    app.selectbox(key=f"report-task-{parent.id}").select("t1").run()
    app.selectbox(key=f"report-sample-{parent.id}").select(1).run()
    assert not app.exception
    assert any("SYNTHETIC ANSWER" in code.value for code in app.code)
    config = parent.scoring.model_dump(mode="json")
    config["dimension_weights"] = [
        {"name": "functionality", "weight": .5}, {"name": "security", "weight": .5},
    ]
    app.text_area[0].input(json.dumps(config))
    next(button for button in app.button if button.label == "验证权重并保存事后分析新版本").click().run(timeout=30)
    assert not app.exception
    history = storage.list_reports(plan.id)
    assert len(history) == 2 and history[1].post_hoc
    assert history[1].parent_report_id == parent.id
    assert history[0] == parent


def test_report_page_missing_matrix_and_invalid_weights(tmp_path):
    from streamlit.testing.v1 import AppTest

    storage = Storage(tmp_path / "page.sqlite")
    plan = make_plan()
    storage.save_run(plan)
    app = AppTest.from_function(_page_script, args=(str(storage.database), str(tmp_path))).run()
    app.button(key=f"build-report-{plan.id}").click().run(timeout=30)
    assert not app.exception
    report = storage.list_reports(plan.id)[0]
    app.radio(key=f"report-section-{report.id}").set_value("答案对比").run()
    assert any("此计划样本缺失" in warning.value for warning in app.warning)
    assert storage.list_reports(plan.id)[0].status == "provisional"
    app.text_area[0].input('{"dimension_weights": []}')
    next(button for button in app.button if button.label == "验证权重并保存事后分析新版本").click().run()
    assert not app.exception and len(app.error) == 1
    assert len(storage.list_reports(plan.id)) == 1


@pytest.mark.parametrize("run_status", [None, "queued", "running", "cancelled", "failed", "completed"])
def test_pending_retry_stays_provisional_in_every_run_state_and_export(run_status):
    plan = make_plan(max_retries=1, max_requests=2)
    attempts, judges = evidence(plan)
    failed = attempts[0].model_copy(update={"status": "failed"})
    report = report_for(plan, [failed], judges, run_status=run_status)
    assert report.status == "provisional"
    assert report.payload["samples"][0]["status"] == "pending_retry"
    assert report.payload["coverage"]["scored"] == 0
    assert report.payload["models"]["alpha"]["quality_score"] is None
    assert report.payload["ranking"] == []
    assert json.loads(export_json(report))["status"] == "provisional"
    assert list(csv.DictReader(io.StringIO(export_csv(report))))[0]["report_status"] == "provisional"
    assert "status: provisional" in export_html(report)


@pytest.mark.parametrize("outcome", ["failed", "completed"])
@pytest.mark.parametrize("run_status", [None, "completed"])
def test_terminal_retry_can_be_ranked(outcome, run_status):
    plan = make_plan(max_retries=1, max_requests=2)
    attempts, judges = evidence(plan)
    failed = attempts[0].model_copy(update={"id": "first-failure", "status": "failed"})
    last = attempts[0].model_copy(update={"attempt_index": 1, "status": outcome})
    report = report_for(plan, [failed, last], judges, run_status=run_status)
    assert report.status == "complete"
    assert report.payload["models"]["alpha"]["quality_score"] == (100 if outcome == "completed" else 0)
    assert len(report.payload["ranking"]) == 1
    assert report.payload["models"]["alpha"]["actual_requests"] == 2


def test_rescore_keeps_frozen_run_state_after_live_run_completes(tmp_path):
    storage = Storage(tmp_path / "reports.sqlite")
    plan = make_plan()
    storage.save_run(plan)
    parent = report_for(plan, run_status="cancelled")
    storage.save_report(parent)
    storage.transition_run(plan.id, "queued", "running")
    storage.transition_run(plan.id, "running", "completed")
    before = export_json(parent)
    child = rescore_report(storage, parent, plan.scoring)
    assert child.status == parent.status == "provisional"
    assert child.payload["run_status"] == "cancelled"
    assert child.payload["ranking"] == []
    assert export_json(storage.list_reports(plan.id)[0]) == before


@pytest.mark.parametrize("passed", [False, True])
def test_scores_use_frozen_weighted_checks_and_not_cache_including_rescore(tmp_path, passed):
    storage = Storage(tmp_path / "reports.sqlite")
    plan = make_plan()
    task = plan.task_snapshots[0].restore()
    rubric = tuple(r.model_copy(update={"points": 3 if r.check == "functionality0" else 1})
                   for r in task.rubric)
    task = task.model_copy(update={"rubric": rubric})
    task = task.model_copy(update={"validated_content_hash": task.validation_hash})
    plan = plan.model_copy(update={"task_snapshots": (Snapshot.capture(task),)})
    storage.save_run(plan)
    attempts, judges = evidence(plan)
    judge = judges[0].model_copy(update={
        "checks": tuple(c.model_copy(update={"passed": passed and c.check.endswith("0")})
                        for c in judges[0].checks),
        "all_required_passed": False,
    })
    parent = report_for(plan, attempts, [judge])
    expected = {"functionality": 50 if passed else 0, "security": 25 if passed else 0}
    assert parent.payload["samples"][0]["dimension_scores"] == pytest.approx(expected)
    assert parent.payload["models"]["alpha"]["quality_score"] == pytest.approx(43.75 if passed else 0)
    storage.save_report(parent)
    before = export_json(parent)
    scoring = plan.scoring.model_copy(update={"dimension_weights": (
        Weight(name="functionality", weight=.5), Weight(name="security", weight=.5),
    )})
    child = rescore_report(storage, parent, scoring)
    assert child.payload["models"]["alpha"]["quality_score"] == pytest.approx(37.5 if passed else 0)
    assert child.payload["judges"][0]["dimension_scores"] == {"functionality": 100, "security": 100}
    assert export_json(storage.list_reports(plan.id)[0]) == before


@pytest.mark.parametrize("response_status", ["completed", "failed"])
def test_report_explanation_requires_selection_and_single_use_authorization(tmp_path, monkeypatch, response_status):
    from streamlit.testing.v1 import AppTest
    from app import report_page
    from coding_eval.agent import PlanningAgent
    from test_agent import ControlAPI

    storage = Storage(tmp_path / "report.sqlite")
    plan = make_plan(models=("alpha", "beta"))
    storage.save_run(plan)
    report = report_for(plan)
    storage.save_report(report)
    before = export_json(report)
    for snapshot in plan.model_snapshots:
        storage.save_object("model", snapshot.object_id, snapshot.restore().model_dump(mode="json"))
    client = ControlAPI("Synthetic explanation", response_status)
    monkeypatch.setattr(report_page, "PlanningAgent", lambda db: PlanningAgent(db, client))
    page = AppTest.from_function(_page_script, args=(str(storage.database), str(tmp_path))).run()
    button_key, auth_key = f"explain-report-{report.id}", f"explain-authorize-{report.id}"
    assert not page.exception and not client.calls
    page.checkbox(key=f"explanation-open-{report.id}").check().run()
    page.checkbox(key=auth_key).check()
    page.button(key=button_key).click().run()
    assert not client.calls and page.error
    page.selectbox(key=f"explain-model-{report.id}").select("beta").run()
    page.button(key=button_key).click().run()
    assert not client.calls
    page.checkbox(key=auth_key).check()
    page.button(key=button_key).click().run()
    assert not page.exception and len(client.calls) == 1
    assert page.checkbox(key=auth_key).value is False
    assert bool(page.error) is (response_status == "failed")
    record = storage.list_objects("control")[0]["payload"]
    intent = storage.list_objects("control_intent")[0]["payload"]
    assert record["report_id"] == intent["report_id"] == report.id
    assert record["result"]["model_id"] == "beta"
    assert record["result"]["purpose"] == "control"
    assert record["result"]["metrics"]["input_tokens"] == 13
    assert record["result"]["metrics"]["output_tokens"] == 8
    assert record["result"]["status"] == response_status
    page.run()
    page.button(key=button_key).click().run()
    assert not page.exception and len(client.calls) == 1
    assert export_json(storage.list_reports(plan.id)[0]) == before
    assert storage.list_attempts(plan.id) == []
    assert storage.list_objects("request_intent") == []
