"""Deterministic, snapshot-based reports. No API calls or candidate execution."""

from __future__ import annotations

import csv
import html
import io
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from statistics import fmean
from typing import Any

from .models import (
    METRIC_NAMES, AttemptResult, JudgeResult, ReportVersion, RunPlan,
    RunStatus, ScoringConfig, TaskDefinition, canonical_json, content_hash,
)
from .storage import Storage

REPORT_ALGORITHM = "t6-v2"
NOTES = [
    "仅比较配置的 API 服务端点，不代表未经后训练的 base weights 或全行业能力。",
    "10 题及以下为探索性结果；同题重复不是独立题，小分差或区间重叠不能证明统计优势。",
    "样本均分形成任务分，任务等权形成场景分，最后按冻结场景权重汇总。",
    "重试不择优：按 attempt_index 选最后 completed 请求；无 completed 时最后 failed 计零。"
    "任何 uncertain 使该逻辑样本不可评分。所有实际请求均保留，费用与耗时不因重试消失。",
    "missing、cancelled、uncertain、blocked、judge_error 均不补分。完整分数为 null，"
    "observed_quality_score 仅为已知子集的描述统计，不能用于正式排名。",
    "API 执行失败计零；判分器故障 excluded，不计能力分并阻止正式排名。",
    "失败且尚有已声明重试的样本为 pending_retry，不形成正式分数；运行状态随报告冻结。",
    "首 token 是客户端观测到首个非空生成内容的近似时间，可能包含公开推理；"
    "首最终回答另列。非流式不可测时为 null，不用总时延替代。",
    "指标按实际 evaluation 请求统计，失败耗时另列；排队、判分、逻辑任务耗时不混入请求时延。"
    "缺失 token 为 null，不估算价格；不同 tokenizer、推理及缓存口径不能直接视为成本优势。",
    "区间按场景分层、以 task 为聚类单位重采样，保留同题所有重复；"
    "场景只有一题时无法估计该场景题间不确定性，退化区间不是确定性优势。",
    "仅同题库版本、任务覆盖、评分版本和预算组可比；参数相同不代表算力相同。",
]


def _mean(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return fmean(known) if known else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def metric_summary(values: Iterable[float | None]) -> dict[str, Any]:
    """Missingness uses all supplied observations, including nulls, as denominator."""
    values = list(values)
    known = [v for v in values if v is not None]
    return {
        "n": len(known), "total": len(values), "missing": len(values) - len(known),
        "missing_rate": (len(values) - len(known)) / len(values) if values else None,
        "mean": _mean(known), "median": _percentile(known, .5),
        "p95": _percentile(known, .95), "sum": sum(known) if known else None,
    }


def _validated_scoring(plan: RunPlan, scoring: ScoringConfig | None) -> ScoringConfig:
    value = ScoringConfig.model_validate_json((scoring or plan.scoring).model_dump_json())
    if value.template_id != plan.scoring.template_id:
        raise ValueError("Post-hoc scoring cannot change the scoring template")
    if {w.name for w in value.dimension_weights} != {w.name for w in plan.scoring.dimension_weights}:
        raise ValueError("Dimension weights must exactly cover frozen dimensions")
    if {w.name for w in value.scenario_weights} != {w.name for w in plan.scoring.scenario_weights}:
        raise ValueError("Scenario weights must exactly cover frozen scenarios")
    return value


def _judges_by_attempt(judges: Iterable[Any] | Mapping[str, Any]) -> dict[str, JudgeResult]:
    """Last supplied version wins; Storage.list_objects supplies insertion order."""
    latest = {}
    entries = judges.items() if isinstance(judges, Mapping) else ((None, j) for j in judges)
    for key, entry in entries:
        if isinstance(entry, JudgeResult):
            judge = JudgeResult.model_validate_json(entry.model_dump_json())
        else:
            if "payload" in entry:
                key = entry["object_id"]
                entry = entry["payload"]
            judge = JudgeResult.model_validate(entry)
        if key is not None and key != judge.attempt_id:
            raise ValueError("Judge object_id must equal attempt_id")
        latest[judge.attempt_id] = judge
    return latest


def _sample(
    task: TaskDefinition, model_id: str, index: int, attempts: list[AttemptResult],
    judges: dict[str, JudgeResult], scoring: ScoringConfig, max_retries: int,
) -> dict[str, Any]:
    attempts = sorted(attempts, key=lambda a: a.attempt_index)
    completed = [a for a in attempts if a.status == "completed"]
    selected = completed[-1] if completed else (attempts[-1] if attempts else None)
    judge = judges.get(selected.id) if selected else None
    row: dict[str, Any] = {
        "model_id": model_id, "task_id": task.id, "task_version_hash": task.version_hash,
        "scenario_id": task.scenario_id, "sample_index": index,
        "attempt_id": selected.id if selected else None,
        "attempt_ids": [a.id for a in attempts], "attempt_count": len(attempts),
        "status": "missing", "quality_score": None, "dimension_scores": {},
        "all_required_passed": None, "excluded": True, "reason": None,
        "judge_id": judge.id if judge else None,
        "gate_failures": list(judge.gate_failures) if judge else [],
    }
    if not selected:
        return row
    if any(a.status == "uncertain" for a in attempts):
        row.update(status="uncertain", reason="An actual request has uncertain completion")
    elif not completed and selected.status == "cancelled":
        row.update(status="cancelled")
    elif not completed and selected.status == "failed" and selected.attempt_index < max_retries:
        row.update(status="pending_retry", reason="Declared retries have not been exhausted")
    elif not completed and selected.status == "failed":
        row.update(
            status="api_failed", quality_score=0.0, excluded=False,
            dimension_scores={w.name: 0.0 for w in scoring.dimension_weights},
            all_required_passed=False,
        )
    elif judge is None:
        row.update(status="missing_judge")
    elif judge.task_version_hash != task.version_hash:
        row.update(status="judge_error", reason="Judge task version does not match frozen task")
    elif judge.status != "completed":
        row.update(status=judge.status, reason=judge.error_message)
    else:
        dimensions = {d.value: v for d, v in judge.dimension_scores.items()}
        required_dimensions = {w.name for w in scoring.dimension_weights}
        checks = {c.check: c.passed for c in judge.checks}
        rubric = {r.check: r for r in task.rubric}
        # Gates may short-circuit checks, but cannot turn diagnostic scores into a pass.
        malformed = (
            set(dimensions) != required_dimensions or len(checks) != len(judge.checks)
            or not set(checks) <= set(rubric)
            or (not judge.gate_failures and set(checks) != set(rubric))
            or judge.all_required_passed is None
        )
        if malformed:
            row.update(status="judge_error", reason="Incomplete or inconsistent judge evidence")
            return row
        # Persisted scores are a cache; only frozen rubric/check evidence earns points.
        dimensions = {}
        for name in required_dimensions:
            items = [r for r in task.rubric if r.dimension.value == name]
            scale = max(r.points for r in items)
            total = sum(r.points / scale for r in items)
            earned = sum(r.points / scale for r in items if checks.get(r.check, False))
            dimensions[name] = min(100.0, earned / total * 100)
        critical = [f"critical:{r.check}" for r in task.rubric if r.critical and not checks.get(r.check, False)]
        gates = list(dict.fromkeys([*judge.gate_failures, *critical]))
        required_passed = not gates and all(checks.get(r.check, False) for r in task.rubric if r.required)
        if required_passed != judge.all_required_passed:
            row.update(status="judge_error", reason="Required-check evidence contradicts pass flag")
            return row
        score = 0.0 if gates else sum(w.weight * dimensions[w.name] for w in scoring.dimension_weights)
        row.update(
            status="scored", quality_score=min(100.0, score), dimension_scores=dimensions,
            all_required_passed=required_passed, gate_failures=gates, excluded=False,
        )
    return row


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(r["status"] for r in rows)
    scored = sum(not r["excluded"] for r in rows)
    return {
        "expected": len(rows), "scored": scored, "excluded": len(rows) - scored,
        "coverage": scored / len(rows) if rows else 0.0,
        "missing_rate": (len(rows) - scored) / len(rows) if rows else 1.0,
        "absent": counts["missing"], "status_counts": dict(sorted(counts.items())),
        "api_failure_rate": counts["api_failed"] / len(rows) if rows else None,
        "all_required_passed_rate": (
            sum(r["all_required_passed"] is True for r in rows) / scored if scored else None
        ),
    }


def _task_summary(rows: list[dict[str, Any]], dimensions: list[str]) -> dict[str, Any]:
    coverage = _coverage(rows)
    observed = _mean(r["quality_score"] for r in rows)
    return {
        **coverage, "quality_score": observed if coverage["coverage"] == 1 else None,
        "observed_quality_score": observed,
        "dimension_scores": {
            d: _mean(r["dimension_scores"].get(d) for r in rows)
            if coverage["coverage"] == 1 else None for d in dimensions
        },
    }


def _bootstrap(
    tasks: dict[str, dict[str, Any]], scenarios: dict[str, list[str]],
    weights: dict[str, float], seed: int, iterations: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "method": "scenario-stratified task-cluster percentile bootstrap",
        "seed": seed, "iterations": iterations, "confidence": .95,
        "task_clusters": len(tasks), "lower": None, "upper": None,
        "limited_task_diversity": any(len(ids) < 2 for ids in scenarios.values()),
    }
    if any(t["quality_score"] is None for t in tasks.values()):
        return result
    rng = random.Random(seed)
    draws = []
    for _ in range(iterations):
        score = 0.0
        for scenario, ids in sorted(scenarios.items()):
            sample = rng.choices(sorted(ids), k=len(ids))
            score += weights[scenario] * fmean(tasks[t]["quality_score"] for t in sample)
        draws.append(score)
    result.update(lower=_percentile(draws, .025), upper=_percentile(draws, .975))
    return result


def build_report(
    plan: RunPlan, attempts: Iterable[AttemptResult], judges: Iterable[Any] | Mapping[str, Any],
    scoring: ScoringConfig | None = None, *, bootstrap_iterations: int = 2000,
    bootstrap_seed: int | None = None, parent_report_id: str | None = None,
    run_status: RunStatus | str | None = None,
) -> ReportVersion:
    """Build one run only; no persistence or network.

    Judges accept JudgeResult values, an attempt-id mapping, or Storage.list_objects
    rows in insertion order. Last supplied judge version wins, never highest score.
    Supplying scoring marks the report post-hoc, even if weights are unchanged.
    An omitted run_status never implies pending retries have finished.
    """
    plan = RunPlan.model_validate_json(plan.model_dump_json())
    state = RunStatus(run_status) if run_status is not None else None
    config = _validated_scoring(plan, scoring)
    if isinstance(bootstrap_iterations, bool) or not isinstance(bootstrap_iterations, int) or not 1 <= bootstrap_iterations <= 100000:
        raise ValueError("bootstrap_iterations must be an integer in [1, 100000]")
    seed = plan.random_seed if bootstrap_seed is None else bootstrap_seed
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("bootstrap_seed must be an integer")
    tasks = {s.object_id: TaskDefinition.model_validate_json(s.payload_json) for s in plan.task_snapshots}
    model_ids = sorted(s.object_id for s in plan.model_snapshots)
    dimensions = [w.name for w in config.dimension_weights]
    weights = {w.name: w.weight for w in config.scenario_weights}
    scenarios: dict[str, list[str]] = defaultdict(list)
    for task in tasks.values():
        scenarios[task.scenario_id].append(task.id)
    groups: dict[tuple[str, str, int], list[AttemptResult]] = defaultdict(list)
    included, ignored = [], Counter()
    seen_ids, seen_slots = set(), set()
    for value in attempts:
        attempt = AttemptResult.model_validate_json(value.model_dump_json())
        if attempt.purpose != "evaluation":
            ignored[attempt.purpose] += 1
            continue
        if attempt.run_id != plan.id:
            raise ValueError("Cannot mix attempts from different runs or budget groups")
        if attempt.model_id not in model_ids or attempt.task_id not in tasks or attempt.sample_index >= plan.repeats:
            raise ValueError("Attempt is outside the frozen model/task/sample coverage")
        if attempt.attempt_index > plan.max_retries:
            raise ValueError("Attempt exceeds frozen retry budget")
        key = (attempt.model_id, attempt.task_id, attempt.sample_index)
        slot = (*key, attempt.attempt_index)
        if attempt.id in seen_ids or slot in seen_slots:
            raise ValueError("Duplicate attempt ID or logical request slot")
        seen_ids.add(attempt.id)
        seen_slots.add(slot)
        groups[key].append(attempt)
        included.append(attempt)
    included.sort(key=lambda a: (a.model_id, a.task_id, a.sample_index, a.attempt_index))
    judge_map = {key: j for key, j in _judges_by_attempt(judges).items() if key in seen_ids}
    rows = [
        _sample(tasks[task_id], model_id, index, groups[(model_id, task_id, index)],
                judge_map, config, plan.max_retries)
        for model_id in model_ids for task_id in sorted(tasks) for index in range(plan.repeats)
    ]
    models = {}
    for model_id in model_ids:
        model_rows = [r for r in rows if r["model_id"] == model_id]
        task_scores = {
            task_id: _task_summary([r for r in model_rows if r["task_id"] == task_id], dimensions)
            for task_id in sorted(tasks)
        }
        scenario_scores = {}
        for scenario, ids in sorted(scenarios.items()):
            members = [task_scores[t] for t in ids]
            full = all(m["quality_score"] is not None for m in members)
            scenario_rows = [r for r in model_rows if r["scenario_id"] == scenario]
            scenario_scores[scenario] = {
                **_coverage(scenario_rows), "task_count": len(ids),
                "quality_score": _mean(m["quality_score"] for m in members) if full else None,
                "observed_quality_score": _mean(m["observed_quality_score"] for m in members),
                "dimension_scores": {
                    d: _mean(m["dimension_scores"][d] for m in members) if full else None
                    for d in dimensions
                },
            }
        full = all(s["quality_score"] is not None for s in scenario_scores.values())
        available = [s for s in scenario_scores if scenario_scores[s]["observed_quality_score"] is not None]
        request_rows = [a for a in included if a.model_id == model_id]
        # Runner stores cumulative prefixes; only the last request is a task total.
        logical_totals = {(a.task_id, a.sample_index): a.logical_task_seconds for a in request_rows}
        metrics = {}
        for name in METRIC_NAMES:
            metrics[name] = {
                "all_attempts": metric_summary(getattr(a.metrics, name) for a in request_rows),
                "completed_attempts": metric_summary(
                    getattr(a.metrics, name) for a in request_rows if a.status == "completed"
                ),
                "noncompleted_attempts": metric_summary(
                    getattr(a.metrics, name) for a in request_rows if a.status != "completed"
                ),
            }
        models[model_id] = {
            **_coverage(model_rows), "tasks": task_scores, "scenarios": scenario_scores,
            "quality_score": sum(weights[s] * scenario_scores[s]["quality_score"] for s in weights) if full else None,
            "observed_quality_score": (
                sum(weights[s] * scenario_scores[s]["observed_quality_score"] for s in available)
                / sum(weights[s] for s in available) if available else None
            ),
            "dimension_scores": {
                d: sum(weights[s] * scenario_scores[s]["dimension_scores"][d] for s in weights)
                if full else None for d in dimensions
            },
            "actual_requests": len(request_rows),
            "request_status_counts": dict(Counter(a.status for a in request_rows)),
            "request_failure_rate": (
                sum(a.status == "failed" for a in request_rows) / len(request_rows) if request_rows else None
            ),
            "metrics": metrics,
            "queue_seconds": metric_summary(a.queue_seconds for a in request_rows),
            "logical_task_seconds": metric_summary(
                logical_totals.get((r["task_id"], r["sample_index"])) for r in model_rows
            ),
            "judge_seconds": metric_summary(judge_map[a.id].judge_seconds for a in request_rows if a.id in judge_map),
            "quality_interval": _bootstrap(task_scores, scenarios, weights, seed, bootstrap_iterations),
        }
    coverage = _coverage(rows)
    judge_versions = sorted({
        judge_map[r["attempt_id"]].judge_version for r in rows
        if r["status"] == "scored"
    })
    invalid = plan.diagnostic or any(r["status"] == "judge_error" for r in rows) or len(judge_versions) > 1
    complete = coverage["coverage"] == 1 and state in {None, RunStatus.COMPLETED}
    status = "invalid" if invalid else ("complete" if complete else "provisional")
    comparability = {
        "algorithm": REPORT_ALGORITHM, "task_versions": {t: tasks[t].version_hash for t in sorted(tasks)},
        "scoring_hash": config.version_hash, "budget_group": plan.budget_group,
        "repeats": plan.repeats, "max_retries": plan.max_retries, "concurrency": plan.concurrency,
        "retry_delay_seconds": plan.retry_delay_seconds,
        "request_budget_per_model": plan.max_requests / len(model_ids),
        "judge_versions": judge_versions, "diagnostic": plan.diagnostic,
    }
    warnings = list(NOTES)
    if plan.sampling:
        comparability["sampling"] = plan.sampling.model_dump(mode="json")
        warnings.append("LiveCodeBench 固定 release_v6 分层抽样；功能分为整题原始测试全通过率×100。"
                        "仅 Python 3.11 标准库，每测试6秒、整题120秒、512MB；函数题逐测试重置实例。"
                        "这是本应用的 Docker 适配结果，不等同官方榜单；不同样本不能当作同题对比。")
    if plan.diagnostic:
        warnings.append("诊断运行不形成正式排名；任务 ready 状态以冻结快照为准。")
    if len(judge_versions) > 1:
        warnings.append("判分器版本混用，报告 invalid；请统一版本重判。")
    ranking = []
    if status == "complete":
        previous, rank = None, 0
        for position, model_id in enumerate(sorted(model_ids, key=lambda m: (-models[m]["quality_score"], m)), 1):
            score = models[model_id]["quality_score"]
            if previous is None or not math.isclose(previous, score, abs_tol=1e-9, rel_tol=0):
                rank = position
            ranking.append({"rank": rank, "model_id": model_id, "quality_score": score})
            previous = score
    payload = {
        "algorithm": REPORT_ALGORITHM, "comparability": comparability,
        "run_status": state.value if state is not None else None,
        "comparison_key": content_hash(comparability), "formal_ranking_eligible": status == "complete",
        "exploratory": len(tasks) <= 10, "warnings": warnings,
        "coverage": coverage, "models": models, "samples": rows, "ranking": ranking,
        "ignored_requests": dict(ignored),
        "model_snapshots": {
            s.object_id: {"version_hash": s.version_hash, "config": s.restore().model_dump(mode="json")}
            for s in plan.model_snapshots
        },
        "task_metadata": {
            t: {"title": task.title, "scenario_id": task.scenario_id, "version_hash": task.version_hash,
                "source": task.source, "ready": task.ready,
                **({"benchmark": task.benchmark.model_dump(mode="json")} if task.benchmark else {})}
            for t, task in sorted(tasks.items())
        },
        "attempts": [a.model_dump(mode="json") for a in included],
        "judges": [judge_map[key].model_dump(mode="json") for key in sorted(judge_map)],
        "bootstrap": {"iterations": bootstrap_iterations, "seed": seed},
    }
    return ReportVersion(
        run_id=plan.id, scoring=config, source_plan_hash=plan.version_hash,
        parent_report_id=parent_report_id, post_hoc=scoring is not None,
        status=status, payload=payload,
    )


def rescore_report(storage: Storage, report: ReportVersion, scoring: ScoringConfig) -> ReportVersion:
    """Append a child version using the parent's exact evidence, not live judges."""
    plan = storage.get_run(report.run_id)
    if plan is None or plan.version_hash != report.source_plan_hash:
        raise ValueError("Frozen source plan is unavailable or changed")
    if not any(saved == report for saved in storage.list_reports(report.run_id)):
        raise ValueError("Parent report must be saved and unchanged")
    child = build_report(
        plan, [AttemptResult.model_validate(a) for a in report.payload["attempts"]],
        report.payload["judges"], scoring,
        parent_report_id=report.id, bootstrap_iterations=report.payload["bootstrap"]["iterations"],
        bootstrap_seed=report.payload["bootstrap"]["seed"],
        run_status=report.payload.get("run_status"),
    )
    storage.save_report(child)
    return child


def group_reports(reports: Iterable[ReportVersion]) -> dict[str, list[ReportVersion]]:
    """Partition, never pool samples or ranks across incompatible versions."""
    groups: dict[str, list[ReportVersion]] = defaultdict(list)
    for report in reports:
        groups[content_hash(report.payload["comparability"])].append(report)
    return dict(groups)


def explanation_summary(report: ReportVersion, plan: RunPlan | None = None) -> dict[str, Any]:
    """Project the report schema, never recursively copy arbitrary evidence/text."""
    def mapping(value: Any) -> Mapping:
        return value if isinstance(value, Mapping) else {}

    def numbers(value: Any, fields: Iterable[str]) -> dict[str, Any]:
        source = mapping(value)
        return {
            name: source[name] if (
                source[name] is None or isinstance(source[name], (bool, int, float))
                and math.isfinite(source[name])
            ) else None
            for name in fields if name in source
        }

    coverage_fields = (
        "expected", "scored", "excluded", "coverage", "missing_rate", "absent",
        "api_failure_rate", "all_required_passed_rate",
    )
    score_fields = (*coverage_fields, "quality_score", "observed_quality_score", "task_count")
    statistic_fields = ("n", "total", "missing", "missing_rate", "mean", "median", "p95", "sum")
    statuses = (
        "missing", "missing_judge", "pending_retry", "api_failed", "scored",
        "completed", "failed", "cancelled", "uncertain", "blocked", "judge_error",
    )
    dimensions = [w.name for w in report.scoring.dimension_weights]
    scenario_ids = [w.name for w in report.scoring.scenario_weights]
    payload = report.payload
    model_ids = ([s.object_id for s in plan.model_snapshots] if plan else
                 list(mapping(payload.get("model_snapshots"))))
    task_ids = ([s.object_id for s in plan.task_snapshots] if plan else
                list(mapping(payload.get("task_metadata"))))

    def scores(value: Any) -> dict[str, Any]:
        source = mapping(value)
        return {
            **numbers(source, score_fields),
            "status_counts": numbers(source.get("status_counts"), statuses),
            "dimension_scores": numbers(source.get("dimension_scores"), dimensions),
        }

    models = {}
    for model_id in model_ids:
        value = mapping(mapping(payload.get("models")).get(model_id))
        metrics = mapping(value.get("metrics"))
        models[model_id] = {
            **scores(value),
            **numbers(value, ("actual_requests", "request_failure_rate")),
            "request_status_counts": numbers(value.get("request_status_counts"), statuses),
            "metrics": {
                metric: {
                    population: numbers(mapping(metrics[metric]).get(population), statistic_fields)
                    for population in ("all_attempts", "completed_attempts", "noncompleted_attempts")
                }
                for metric in METRIC_NAMES if metric in metrics
            },
            **{
                name: numbers(value.get(name), statistic_fields)
                for name in ("queue_seconds", "logical_task_seconds", "judge_seconds")
            },
            "quality_interval": {
                "method": "scenario-stratified task-cluster percentile bootstrap",
                **numbers(value.get("quality_interval"), (
                    "seed", "iterations", "confidence", "task_clusters", "lower", "upper",
                    "limited_task_diversity",
                )),
            },
            "tasks": {
                task_id: scores(mapping(value.get("tasks")).get(task_id))
                for task_id in task_ids
            },
            "scenarios": {
                scenario_id: scores(mapping(value.get("scenarios")).get(scenario_id))
                for scenario_id in scenario_ids
            },
        }
    return {
        "report_id": report.id, "run_id": report.run_id, "status": report.status,
        "post_hoc": report.post_hoc, "task_ids": task_ids, "model_ids": model_ids,
        "comparison_key": (
            content_hash(payload["comparability"]) if "comparability" in payload else None
        ),
        "run_status": (
            payload.get("run_status") if payload.get("run_status") in tuple(RunStatus) else None
        ),
        "scoring": report.scoring.model_dump(mode="json"),
        "warnings": list(NOTES),
        "coverage": {
            **numbers(payload.get("coverage"), coverage_fields),
            "status_counts": numbers(mapping(payload.get("coverage")).get("status_counts"), statuses),
        },
        **numbers(payload, ("formal_ranking_eligible", "exploratory")),
        "models": models,
    }


def export_json(report: ReportVersion) -> str:
    return canonical_json(report)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = canonical_json(value)
    if isinstance(value, str):
        stripped = value.lstrip("\ufeff \t\r\n\v\f")
        if stripped.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


def export_csv(report: ReportVersion) -> str:
    """One row per planned sample; nested evidence retains every retry and raw field."""
    output = io.StringIO(newline="")
    fields = [
        "report_id", "run_id", "report_status", "post_hoc", "comparison_key", "scoring",
        "model_id", "task_id", "task_version_hash", "scenario_id", "sample_index",
        "status", "quality_score", "excluded", "all_required_passed", "dimension_scores",
        "attempt_id", "raw_output", "raw_response", *METRIC_NAMES,
        "model_summary", "attempts", "judges", "warnings",
    ]
    is_benchmark = any(t.get("benchmark") for t in report.payload["task_metadata"].values())
    if is_benchmark:
        fields.extend(["difficulty", "dataset_revision", "sampling"])
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    attempts = {a["id"]: a for a in report.payload["attempts"]}
    judges = {j["attempt_id"]: j for j in report.payload["judges"]}
    for sample in report.payload["samples"]:
        selected = attempts.get(sample["attempt_id"], {})
        row = {name: sample.get(name) for name in fields}
        row.update(
            report_id=report.id, run_id=report.run_id, report_status=report.status,
            post_hoc=report.post_hoc, comparison_key=report.payload["comparison_key"],
            scoring=report.scoring.model_dump(mode="json"),
            raw_output=selected.get("raw_output"), raw_response=selected.get("raw_response"),
            model_summary=report.payload["models"][sample["model_id"]],
            attempts=[attempts[a] for a in sample["attempt_ids"]],
            judges=[judges[a] for a in sample["attempt_ids"] if a in judges],
            warnings=report.payload["warnings"],
        )
        row.update({name: selected.get("metrics", {}).get(name) for name in METRIC_NAMES})
        if is_benchmark:
            info = report.payload["task_metadata"][sample["task_id"]].get("benchmark", {})
            row.update(difficulty=info.get("difficulty"), dataset_revision=info.get("revision"),
                       sampling=report.payload["comparability"].get("sampling"))
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return output.getvalue()


def export_html(report: ReportVersion) -> str:
    """Standalone inert HTML; all dynamic content is escaped, including JSON."""
    from .report_views import export_visual_html

    return export_visual_html(report)
