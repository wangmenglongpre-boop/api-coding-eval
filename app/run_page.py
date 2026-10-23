"""T4 page entry: render(storage, paths). Worker credentials stay in memory."""

from __future__ import annotations

import asyncio
import json

import streamlit as st
from pydantic import ValidationError

from coding_eval.agent import PlanLimits, PlanProposal, PlanningAgent
from coding_eval.credentials import CredentialError
from coding_eval.failure_diagnostics import describe_attempt
from coding_eval.datasets import list_tasks
from coding_eval.models import DIMENSION_LABELS, RunPlan, ScoringConfig, Weight, canonical_json, content_hash
from coding_eval.runner import BatchRunner, WorkerRegistry, latest_models, task_prompts
from coding_eval.plan_preview import PlanTaskError, create_plan, proposal_to_plan
from coding_eval.storage import AppPaths, Storage
from app.credentials import resolved_keys
from coding_eval.ui_queries import (
    attempt_detail, attempt_summaries, current_judges, current_models, current_tasks, run_summary,
)

PLAN_PREVIEW_REVISION = "ready-snapshot-v1"
PRODUCT_UI_REVISION = "product-v1"


@st.cache_resource
def worker_registry() -> WorkerRegistry:
    return WorkerRegistry()


def _weights(text: str) -> tuple[Weight, ...]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Weights must be an object")
    return tuple(Weight(name=name, weight=weight) for name, weight in value.items())


def _plan_error(exc: ValueError | TypeError) -> None:
    if isinstance(exc, PlanTaskError):
        st.error(f"任务 {exc.task_id} 容器验证失败：{exc}")
        st.dataframe([
            {"判分状态": result.status, "原因": result.error_message,
             "未通过检查": ", ".join(c.check for c in result.checks if not c.passed)}
            for result in exc.results
        ], use_container_width=True)
        with st.expander("验证证据与日志路径"):
            st.write(list(exc.artifact_paths))
        st.caption("Docker 不可用请先检查 Docker Desktop；参考解检查失败请查看对应日志。没有开始模型评测。")
    elif isinstance(exc, ValidationError):
        fields = {"max_requests": "请求预算上限", "repeats": "重复次数", "max_retries": "重试上限",
                  "concurrency": "并发上限", "dimension_weights": "维度权重", "scenario_weights": "场景权重"}
        messages = []
        for error in exc.errors(include_input=False, include_url=False):
            location = " / ".join(fields.get(str(key), str(key)) for key in error["loc"])
            messages.append(f"{location + '：' if location else ''}{error['msg']}")
        st.error("计划配置有误：" + "；".join(messages))
    else:
        messages = {
            "Control call not authorized": "请选择控制模型并勾选调用授权；手动预览无需授权",
            "An evaluation intent is required": "调用 agent 前请填写评测意图",
            "Control model unavailable; use the form": "控制模型未完成请求，请使用手动表单预览",
            "Control plan failed schema or authorization validation": "控制模型的计划未通过格式或授权范围校验，请使用手动表单",
            "Weights must be an object": "权重 JSON 必须是名称到权重的对象",
        }
        st.error("计划生成失败：" + messages.get(str(exc), str(exc))[:2000])


def _preview_progress():
    bar = st.progress(0, text="检查选择、权重和请求预算")

    def update(done, total, task_id, phase):
        bar.progress(done / total if total else 0, text=f"{done}/{total} · {task_id} · {phase}")

    return update


def _history(storage: Storage, paths: AppPaths) -> None:
    """Choose a batch outside the independently refreshing monitor."""
    rows = storage.list_runs()
    if not rows:
        return
    st.subheader("运行状态与恢复")
    by_id = {r["run_id"]: r for r in rows}
    run_id = st.selectbox("历史批次", list(by_id), key="history-run",
                         format_func=lambda i: f'{i[:12]} · {by_id[i]["status"]}')
    refresh = st.checkbox("自动刷新运行状态", value=by_id[run_id]["status"] in {"queued", "running"},
                          key=f"monitor-refresh-{run_id}")
    st.fragment(run_every="3s" if refresh and by_id[run_id]["status"] in {"queued", "running"} else None)(
        _monitor
    )(storage, paths, run_id)


def _monitor(storage: Storage, paths: AppPaths, run_id: str) -> None:
    registry = worker_registry()
    plan = storage.get_run(run_id)
    if plan is None:
        st.info("批次不存在，请刷新列表。")
        return
    attempts = attempt_summaries(storage, run_id)
    judges = current_judges(storage, run_id)
    status = run_summary(storage, plan, attempts, judges)
    columns = st.columns(4)
    columns[0].metric("批次状态", status["status"])
    columns[1].metric("已返回样本", f'{status["observed_samples"]}/{status["expected_samples"]}')
    columns[2].metric("已预留请求 / 预算", f'{status["reserved_requests"]}/{status["max_requests"]}')
    columns[3].metric("待完成判分", status["judge_pending_or_invalid"])
    st.progress(min(1.0, status["observed_samples"] / max(1, status["expected_samples"])),
                text="已返回样本进度（包含失败，完整成绩以报告为准）")
    if st.checkbox("显示完整运行状态", key=f"run-status-{run_id}"):
        st.json(status)
    active = registry.active(storage, run_id)
    st.caption("uncertain 表示请求可能已计费，恢复不会重发；恢复只执行未派发样本、已声明失败重试及补判。")
    st.caption("正式批次每次启动或恢复均先检查 Docker、任务契约及冻结镜像 ID；失败时不派发 API。")
    left, middle, right = st.columns(3)
    if left.button("刷新结果"):
        st.rerun()
    if middle.button("停止派发并取消在途请求", disabled=status["status"] not in {"queued", "running"}):
        registry.cancel(storage, paths, run_id)
        st.rerun()
    if right.button("恢复未完成批次", disabled=active or status["status"] == "completed"):
        try:
            plan = storage.get_run(run_id)
            keys = resolved_keys([s.restore() for s in plan.model_snapshots])
            registry.start(storage, paths, run_id, api_keys=keys)
        except CredentialError as exc:
            st.error(str(exc))
        else:
            st.rerun()
    error = storage.get_object("run_error", run_id)
    if error:
        st.warning(error["message"])
    if status.get("blocked_models"):
        st.warning("鉴权失败已暂停这些模型的新请求：" + "、".join(status["blocked_models"])
                   + "。修复密钥/权限后点击恢复；仍按原预算和重试上限执行。")
    failures = [a for a in attempts if a.status != "completed"]
    judge_issues = []
    for attempt in attempts:
        judge = judges.get(attempt.id)
        if judge and judge.get("status") != "completed":
            judge_issues.append({"模型": attempt.model_id, "任务": attempt.task_id,
                                 "请求状态": attempt.status, "判分状态": judge["status"],
                                 "判分原因": judge.get("error_message"),
                                 "建议": "检查 Docker 后恢复批次，补判已保存答案；已完成请求不会重发"})
    if judge_issues:
        st.warning("有容器判分未完成，不能当作模型能力失败；请检查 Docker 并恢复补判。")
        st.dataframe(judge_issues, use_container_width=True)
    if failures:
        st.subheader("失败原因与处理建议")
        summary = {}
        for attempt in failures:
            diagnosis = describe_attempt(attempt)
            key = (attempt.model_id, diagnosis["原因"])
            row = summary.setdefault(key, {"模型": attempt.model_id, **diagnosis, "次数": 0})
            row["次数"] += 1
        st.dataframe(list(summary.values()), use_container_width=True)
        with st.expander("失败批次冻结配置与当前配置"):
            plan = storage.get_run(run_id)
            current = {m.id: m for m in latest_models(storage)}
            st.dataframe([
                {"模型": s.object_id, "批次超时秒": s.restore().timeout_seconds,
                 "批次最大输出 Token": s.restore().max_output_tokens,
                 "当前超时秒": current[s.object_id].timeout_seconds if s.object_id in current else None,
                 "当前最大输出 Token": current[s.object_id].max_output_tokens if s.object_id in current else None}
                for s in plan.model_snapshots
            ], use_container_width=True)
            st.caption("在模型配置页保存调整后重新生成计划；已有批次恢复仍使用冻结参数。未知用量不代表零费用。")
    st.dataframe([
        {
            "模型": a.model_id, "任务": a.task_id, "样本": a.sample_index,
            "尝试": a.attempt_index, "状态": a.status, "错误码": a.error_code,
            "原因": describe_attempt(a)["原因"],
            "排队秒": a.queue_seconds, "逻辑任务秒": a.logical_task_seconds,
            "请求秒": a.metrics.total_seconds, "首内容秒": a.metrics.first_content_seconds,
            "首回答秒": a.metrics.first_answer_seconds, "输入 token": a.metrics.input_tokens,
            "缓存 token": a.metrics.cache_read_tokens, "输出 token": a.metrics.output_tokens,
            "判分状态": (judges.get(a.id) or {}).get("status", "pending"),
        } for a in attempts
    ], use_container_width=True)
    if attempts and st.checkbox("查看原始结果与判分", key=f"run-details-{run_id}"):
        selected = st.selectbox("请求", [a.id for a in attempts], key=f"attempt-{run_id}",
                                format_func=lambda i: next(
                                    f"{a.model_id} · {a.task_id} · {a.status} · 尝试 {a.attempt_index}"
                                    for a in attempts if a.id == i))
        attempt = attempt_detail(storage, run_id, selected)
        if attempt:
            st.code(attempt.raw_output, language=None)
            st.json(judges.get(attempt.id) or {"status": "pending"})
            if st.checkbox("加载完整响应 JSON", key=f"raw-{selected}"):
                st.json(attempt.model_dump(mode="json"))
            st.download_button("下载完整请求记录", data=attempt.model_dump_json,
                               file_name=f"attempt-{selected}.json", on_click="ignore")
    if not st.checkbox("查看控制调用与用量", key=f"run-controls-{run_id}"):
        return
    controls = {row["object_id"]: row["payload"] for row in storage.list_objects("control")}
    with st.expander("控制调用与用量（全局单列，不计评测分数）"):
        st.dataframe([
            {
                "调用 ID": identifier, "用途": row["action"],
                "模型": row["result"]["model_id"], "状态": row["result"]["status"],
                "输入 token": row["result"]["metrics"]["input_tokens"],
                "输出 token": row["result"]["metrics"]["output_tokens"],
                "请求秒": row["result"]["metrics"]["total_seconds"],
            } for identifier, row in controls.items()
        ], use_container_width=True)
        unfinished = [
            row["object_id"] for row in storage.list_objects("control_intent")
            if row["object_id"] not in controls
        ]
        if unfinished:
            st.warning(f"{len(unfinished)} 次控制调用只有 intent，无响应记录，计费未知；不会自动重发。")


def render(storage: Storage, paths: AppPaths) -> None:
    st.caption("独立原始 API 请求，无工具、历史或全局优化提示。预览后才开始评测。")
    models = [m for m in current_models(storage) if m.enabled]
    diagnostic = st.checkbox("诊断模式（允许 draft，不进入正式排行）", key="run_diagnostic")
    if diagnostic:
        st.warning("诊断模式允许在 Docker 不可用时调用 API，可能产生费用且无法判分；仍需确认冻结计划费用。")
    tasks = [t for t in current_tasks(storage) if t.ready or diagnostic]
    if not models or not tasks:
        st.info("请先配置启用模型并验证任务；未验证题目仅可选择诊断模式。")
        _history(storage, paths)
        return
    selected_models = st.multiselect(
        "评测模型", [m.id for m in models], default=[m.id for m in models],
        format_func=lambda i: next(m.display_name for m in models if m.id == i),
    )
    selected_tasks = st.multiselect(
        "评测任务", [t.id for t in tasks], default=[t.id for t in tasks],
        format_func=lambda i: next(f"{t.scenario_id} · {t.title} ({t.status})" for t in tasks if t.id == i),
    )
    chosen_models = [m for m in models if m.id in selected_models]
    chosen_tasks = [t for t in tasks if t.id in selected_tasks]
    scenarios = sorted({t.scenario_id for t in chosen_tasks})
    defaults = chosen_tasks[0].scoring if chosen_tasks else ScoringConfig()
    selection_hash = content_hash([selected_models, selected_tasks, diagnostic])[:16]
    st.caption("不同模型的温度、思考参数不代表相同算力；实际配置及参数随计划冻结。")
    control_id = st.selectbox("可选控制模型", ["", *[m.id for m in models]],
                              format_func=lambda i: i or "不使用 agent，完整表单运行")
    with st.form("run_form_" + selection_hash):
        weights_left, weights_right = st.columns(2)
        dimension_text = weights_left.text_area(
            "维度权重 JSON（合计 1）",
            value=json.dumps({w.name: w.weight for w in defaults.dimension_weights}, indent=2),
            help="可用维度：" + "、".join(DIMENSION_LABELS),
        )
        scenario_text = weights_right.text_area(
            "场景权重 JSON（合计 1）",
            value=json.dumps({s: 1 / len(scenarios) for s in scenarios}, indent=2),
        )
        first, second, third = st.columns(3)
        repeats = first.number_input("每题重复次数", min_value=1, value=3)
        concurrency = second.number_input("并发上限", min_value=1, max_value=128, value=1)
        retries = third.number_input("每样本失败重试上限（uncertain 不重试）", min_value=0, value=0)
        delay = first.number_input("固定重试间隔（秒）", min_value=0.0, value=0.0)
        budget = second.number_input(
            "评测请求预算上限", min_value=1,
            value=max(1, len(chosen_models) * len(chosen_tasks) * 3),
        )
        seed = third.number_input("随机种子", value=0, step=1)
        group = st.text_input("预算组", value="default")
        intent = st.text_area("可选自然语言评测意图")
        allow_control = st.checkbox("允许一次控制模型调用（可能产生费用，与评测预算分开）")
        st.caption("已验证题目且维度权重不变时直接复用证据；修改维度权重才重新容器验证。开始运行前检查 Docker。")
        preview = st.form_submit_button("生成并验证表单预览", type="primary")
        propose = st.form_submit_button("调用 agent 生成可编辑计划")
    if preview or propose:
        st.session_state.pop("_run_preview", None)
        st.session_state.pop("_run_proposal", None)
        try:
            scoring = ScoringConfig(
                template_id=defaults.template_id, dimension_weights=_weights(dimension_text),
                scenario_weights=_weights(scenario_text),
            )
            options = dict(
                repeats=int(repeats), concurrency=int(concurrency), random_seed=int(seed),
                max_retries=int(retries), retry_delay_seconds=float(delay), max_requests=int(budget),
                budget_group=group, diagnostic=diagnostic,
            )
            if propose:
                if not control_id or not allow_control or not chosen_models or not chosen_tasks:
                    raise ValueError("Control call not authorized")
                limits = PlanLimits(
                    max_requests=int(budget), max_concurrency=int(concurrency), max_repeats=int(repeats),
                    max_retries=int(retries), diagnostic=diagnostic, budget_group=group,
                )
                proposal = asyncio.run(PlanningAgent(storage).propose(
                    intent, next(m for m in models if m.id == control_id),
                    chosen_models, chosen_tasks, scoring, limits,
                    api_key=resolved_keys([next(m for m in models if m.id == control_id)]).get(control_id),
                ))
                st.session_state["_run_proposal"] = (
                    proposal.model_dump_json(indent=2), chosen_models, chosen_tasks, scoring, limits,
                )
            else:
                with st.spinner("验证并冻结任务与评分配置"):
                    plan = create_plan(chosen_models, chosen_tasks, scoring, paths,
                                       progress=_preview_progress(), **options)
                st.session_state["_run_preview"] = plan.model_dump_json()
        except CredentialError as exc:
            st.error(str(exc))
        except (ValueError, TypeError) as exc:
            _plan_error(exc)

    pending = st.session_state.get("_run_proposal")
    if pending:
        text, frozen_models, frozen_tasks, scoring, limits = pending
        with st.form("edit_proposal_" + content_hash(text)[:16]):
            edited = st.text_area("编辑 agent 计划 JSON（仍受上述授权限制）", text, height=350)
            validate = st.form_submit_button("验证编辑结果并生成预览")
        if validate:
            st.session_state.pop("_run_preview", None)
            try:
                proposal = PlanProposal.model_validate_json(edited)
                with st.spinner("验证计划与任务"):
                    plan = proposal_to_plan(proposal, frozen_models, frozen_tasks, scoring, limits, paths,
                                            progress=_preview_progress())
                st.session_state["_run_preview"] = plan.model_dump_json()
            except (ValueError, TypeError) as exc:
                _plan_error(exc)

    preview_json = st.session_state.get("_run_preview")
    if preview_json:
        plan = RunPlan.model_validate_json(preview_json)
        st.subheader("已冻结计划预览")
        st.write(f"计划 {plan.id}；样本 {plan.expected_requests}；最多请求 {plan.max_requests}；诊断：{plan.diagnostic}")
        st.caption("开始按钮执行下方冻结预览。修改上方表单后，需重新生成预览。")
        st.json({
            "scoring": plan.scoring.model_dump(mode="json"),
            "repeats": plan.repeats, "concurrency": plan.concurrency, "random_seed": plan.random_seed,
            "max_retries": plan.max_retries, "retry_delay_seconds": plan.retry_delay_seconds,
            "budget_group": plan.budget_group,
            "models": [s.restore().model_dump(mode="json") for s in plan.model_snapshots],
            "tasks": [
                {"id": s.object_id, "version_hash": s.version_hash, "runtime_image": s.restore().runtime.image}
                for s in plan.task_snapshots
            ],
        })
        if st.checkbox("查看冻结计划的实际模型输入", key=f"preview-input-{plan.id}"):
            selected_task = st.selectbox("预览任务", [s.object_id for s in plan.task_snapshots])
            snapshot = next(s for s in plan.task_snapshots if s.object_id == selected_task)
            system, user = task_prompts(snapshot.restore())
            st.code(system, language=None)
            st.code(user, language=None)
        confirmed = st.checkbox("我确认此冻结计划、评分权重和可能产生的 API 费用", key="run_cost_confirm_" + plan.id)
        if st.button("开始冻结计划", disabled=not confirmed):
            try:
                keys = resolved_keys([s.restore() for s in plan.model_snapshots])
                if storage.get_run(plan.id) is None:
                    storage.save_run(plan)
                worker_registry().start(storage, paths, plan.id, api_keys=keys)
                st.session_state.pop("_run_preview", None)
                st.session_state.pop("_run_proposal", None)
                st.rerun()
            except CredentialError as exc:
                st.error(str(exc))
            except (ValueError, TypeError):
                st.error("启动失败，请检查计划及模型配置。")
    _history(storage, paths)


render_runs = render
