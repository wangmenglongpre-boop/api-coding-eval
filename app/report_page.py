"""Report UI. Rendering and reweighting never make paid model requests."""

from __future__ import annotations

import asyncio
import json

import streamlit as st

from coding_eval.agent import PlanningAgent
from coding_eval.credentials import CredentialError
from coding_eval.models import DIMENSION_LABELS, METRIC_NAMES, ReportVersion, ScoringConfig
from coding_eval.reports import (
    build_report, explanation_summary, export_csv, export_html, export_json,
    group_reports, rescore_report,
)
from coding_eval.storage import AppPaths, Storage
from coding_eval.runner import latest_models
from coding_eval.report_views import export_visual_html as export_html
from app.credentials import resolved_keys
from app.report_charts import render_overview
from coding_eval.ui_queries import report_detail, report_index

PRODUCT_UI_REVISION = "product-v1"


def _consume_explanation_authorization(auth_key: str, request_key: str, model_hash: str | None) -> None:
    st.session_state[request_key] = model_hash if st.session_state.get(auth_key) else None
    st.session_state[auth_key] = False


def _explanation(storage: Storage, report: ReportVersion) -> None:
    st.caption("仅发送报告数值摘要，不发送答案、参考解、测试或凭据；解释不能修改确定性评分。")
    models = {m.id: m for m in latest_models(storage) if m.enabled}
    model_id = st.selectbox(
        "解释控制模型", ["", *models], key=f"explain-model-{report.id}",
        format_func=lambda key: f"{models[key].display_name} ({key})" if key else "请选择控制模型",
    )
    auth_key, request_key = f"explain-authorize-{report.id}", f"explain-request-{report.id}"
    st.checkbox("允许一次解释调用（可能产生费用，与评测预算分开）", key=auth_key)
    chosen = models.get(model_id)
    if st.button(
        "授权并解释此报告", key=f"explain-report-{report.id}",
        on_click=_consume_explanation_authorization,
        args=(auth_key, request_key, chosen.version_hash if chosen else None),
    ):
        approved_hash = st.session_state.pop(request_key, None)
        if chosen is None or approved_hash != chosen.version_hash:
            st.error("请选择控制模型并明确授权本次解释调用。")
        else:
            try:
                with st.spinner("正在解释已保存报告"):
                    asyncio.run(PlanningAgent(storage).explain(
                        report, chosen, api_key=resolved_keys([chosen]).get(model_id),
                    ))
            except CredentialError as exc:
                st.error(str(exc))
            except (Exception, asyncio.CancelledError):
                st.error("解释不可用或已取消；确定性报告未改变，控制用量单列，不自动重试。")
    controls = [
        row for row in storage.list_objects("control")
        if row["payload"].get("action") == "explain" and row["payload"].get("report_id") == report.id
    ]
    st.caption("此报告的控制请求与用量（不计入评测请求、预算和排名）")
    st.dataframe([
        {
            "调用 ID": row["object_id"], "模型": row["payload"]["result"]["model_id"],
            "状态": row["payload"]["result"]["status"],
            **{name: row["payload"]["result"]["metrics"].get(name)
               for name in ("input_tokens", "output_tokens", "total_seconds")},
        } for row in controls
    ], use_container_width=True)
    completed_ids = {row["object_id"] for row in controls}
    pending = [
        row for row in storage.list_objects("control_intent")
        if row["payload"].get("report_id") == report.id and row["object_id"] not in completed_ids
    ]
    if pending:
        st.warning(f"{len(pending)} 次控制请求无最终记录，计费未知；不会自动重发。")
    for row in controls:
        result = row["payload"]["result"]
        with st.expander(f'解释记录 {row["object_id"]} ({result["status"]})'):
            st.caption("模型生成的解释，可能有误；评分和证据以确定性报告为准。")
            st.code(result["raw_output"], language=None)
    with st.expander("安全数值摘要"):
        st.json(explanation_summary(report))


def _metric_rows(report: ReportVersion) -> list[dict]:
    rows = []
    for model_id, model in report.payload["models"].items():
        for metric, summaries in model["metrics"].items():
            for population, summary in summaries.items():
                rows.append({"模型": model_id, "指标": metric, "请求范围": population, **summary})
    return rows


def _answers(report: ReportVersion) -> None:
    payload = report.payload
    tasks = payload["task_metadata"]
    task_id = st.selectbox(
        "任务", list(tasks), key=f"report-task-{report.id}",
        format_func=lambda key: f'{key}: {tasks[key]["title"]}',
    )
    indexes = sorted({s["sample_index"] for s in payload["samples"] if s["task_id"] == task_id})
    sample_index = st.selectbox("独立样本（从 0 开始）", indexes, key=f"report-sample-{report.id}")
    models = list(payload["models"])
    by_id = {a["id"]: a for a in payload["attempts"]}
    judges = {j["attempt_id"]: j for j in payload["judges"]}
    for side, column in enumerate(st.columns(2)):
        with column:
            model_id = st.selectbox(
                "左侧模型" if side == 0 else "右侧模型", models, index=min(side, len(models) - 1),
                key=f"report-model-{side}-{report.id}",
            )
            sample = next(
                s for s in payload["samples"] if s["model_id"] == model_id
                and s["task_id"] == task_id and s["sample_index"] == sample_index
            )
            st.write({
                "状态": sample["status"], "质量分": sample["quality_score"],
                "excluded": sample["excluded"], "全通过": sample["all_required_passed"],
                "固定选中请求": sample["attempt_id"], "原因": sample["reason"],
            })
            if not sample["attempt_ids"]:
                st.warning("此计划样本缺失，没有答案；不视为成功。")
                continue
            selected = st.selectbox(
                "查看请求（默认评分所用请求，可查看所有重试）", sample["attempt_ids"],
                index=sample["attempt_ids"].index(sample["attempt_id"]),
                key=f"report-attempt-{side}-{report.id}-{model_id}-{task_id}-{sample_index}",
                format_func=lambda key: f'{by_id[key]["attempt_index"]}: {by_id[key]["status"]} ({key})',
            )
            attempt = by_id[selected]
            if selected != sample["attempt_id"]:
                st.caption("正在查看历史请求，不改变逻辑样本的评分选择。")
            st.code(attempt["raw_output"], language=None)
            st.dataframe(
                [{"指标": name, "值": attempt["metrics"].get(name)} for name in METRIC_NAMES],
                use_container_width=True,
            )
            st.json({
                "request_status": attempt["status"], "error_code": attempt["error_code"],
                "error_message": attempt["error_message"], "request_hash": attempt["request_hash"],
                "protocol_mapping_version": attempt["protocol_mapping_version"],
                "queue_seconds": attempt["queue_seconds"], "logical_task_seconds": attempt["logical_task_seconds"],
                "metrics": attempt["metrics"], "artifact_paths": attempt["artifact_paths"],
            })
            if st.checkbox("加载原始响应", key=f"report-raw-{side}-{selected}"):
                st.code(json.dumps(attempt["raw_response"], ensure_ascii=False, indent=2), language="json")
            st.download_button("下载原始请求 JSON",
                               data=lambda item=attempt: json.dumps(item, ensure_ascii=False),
                               file_name=f"attempt-{selected}.json", on_click="ignore",
                               key=f"report-download-{side}-{selected}")
            with st.expander("判分、测试证据与日志路径", expanded=True):
                if selected in judges:
                    st.json(judges[selected])
                else:
                    st.info("此请求没有判分记录。API 执行失败仍计零；其他缺判分不计成功。")


def _show_report(report: ReportVersion) -> None:
    payload = report.payload
    sampling = payload["comparability"].get("sampling")
    if sampling:
        st.caption(f"LiveCodeBench {sampling['release']} · seed {sampling['seed']} · "
                   f"revision {sampling['dataset_revision'][:12]}")
        st.write("全通过率表示一份答案通过该题全部原始测试。功能分 / 100 为加权全通过率；"
                 "下方 LCB-easy / medium / hard 分别对应简单、中等、困难。")
    section = st.radio("报告视图", ("总览", "答案对比", "详细证据"), horizontal=True,
                       key=f"report-section-{report.id}")
    if section == "答案对比":
        st.subheader("同任务答案对比")
        _answers(report)
        return
    if section == "总览":
        render_overview(report)
    if report.status != "complete":
        st.warning("报告不完整或无效，不提供正式排名。已知子集统计不能替代完整计划成绩。")
    if payload["exploratory"]:
        st.warning("当前题目数不超过 10：仅为探索性结果，不能外推行业能力或宣称小分差为统计优势。")
    with st.expander("完整性与可比组"):
        st.write({
            "report_id": report.id, "run_id": report.run_id, "状态": report.status,
            "事后分析": report.post_hoc, "父版本": report.parent_report_id,
            "计划哈希": report.source_plan_hash, "可比组": payload["comparison_key"],
        })
        st.json(payload["coverage"])
    with st.expander("评分规则、指标字典与局限", expanded=False):
        for note in payload["warnings"]:
            st.write(note)
        st.json(report.scoring.model_dump(mode="json"))
    if section == "详细证据":
        with st.expander("冻结模型版本、参数、任务覆盖和预算组", expanded=True):
            st.json(payload["comparability"])
            st.json(payload["model_snapshots"])
            st.json(payload["task_metadata"])

    st.subheader("综合质量与完整性")
    st.dataframe([
        {
            "模型": model_id, "综合质量分": model["quality_score"],
            "已知子集均分（非排名）": model["observed_quality_score"],
            "coverage": model["coverage"], "缺失率": model["missing_rate"],
            "已评分样本": model["scored"], "计划样本": model["expected"],
            "实际请求": model["actual_requests"], "API 逻辑失败率": model["api_failure_rate"],
            "实际请求失败率": model["request_failure_rate"],
            "全通过率": model["all_required_passed_rate"],
            "区间下界": model["quality_interval"]["lower"], "区间上界": model["quality_interval"]["upper"],
        } for model_id, model in payload["models"].items()
    ], use_container_width=True)
    if payload["ranking"]:
        st.caption("排名只描述当前完整可比组的观测分数，不代表统计显著胜出。")
        st.dataframe(payload["ranking"], use_container_width=True)

    st.subheader("场景 × 模型矩阵")
    scenarios = sorted({t["scenario_id"] for t in payload["task_metadata"].values()})
    st.dataframe([
        {"场景": scenario, **{m: value["scenarios"][scenario]["quality_score"]
                             for m, value in payload["models"].items()}}
        for scenario in scenarios
    ], use_container_width=True)
    with st.expander("逐场景覆盖率与缺失"):
        st.dataframe([
            {"模型": m, "场景": s, **{key: value[key] for key in (
                "quality_score", "expected", "scored", "coverage", "missing_rate",
            )}}
            for m, model in payload["models"].items() for s, value in model["scenarios"].items()
        ], use_container_width=True)
    st.subheader("质量维度")
    st.dataframe([
        {"维度": DIMENSION_LABELS.get(weight.name, weight.name),
         **{m: value["dimension_scores"][weight.name] for m, value in payload["models"].items()}}
        for weight in report.scoring.dimension_weights
    ], use_container_width=True)
    with st.expander("完整任务与逻辑样本明细"):
        st.dataframe([
            {"模型": m, "任务": task_id, **{k: task[k] for k in (
                "quality_score", "observed_quality_score", "coverage", "missing_rate",
            )}} for m, model in payload["models"].items() for task_id, task in model["tasks"].items()
        ], use_container_width=True)
        st.dataframe(payload["samples"], use_container_width=True)

    st.subheader("质量与延迟")
    points = []
    for model_id, model in payload["models"].items():
        latency = model["metrics"]["total_seconds"]["completed_attempts"]["median"]
        if latency is not None and model["quality_score"] is not None:
            points.append({"模型": model_id, "完成请求时延中位数（秒）": latency, "质量分": model["quality_score"]})
    if points:
        st.scatter_chart(points, x="完成请求时延中位数（秒）", y="质量分", color="模型")
    else:
        st.info("没有同时具备完整质量分和完成请求时延的模型，不绘制虚构数据点。")
    st.caption("图中延迟仅含 completed 实际请求；所有重试和失败指标在下表分别统计，不混入质量分。")
    st.dataframe(_metric_rows(report), use_container_width=True)
    with st.expander("排队、逻辑任务、判分耗时（与请求时延分开）"):
        st.json({m: {key: value[key] for key in (
            "queue_seconds", "logical_task_seconds", "judge_seconds", "request_status_counts",
        )} for m, value in payload["models"].items()})


def render(storage: Storage, paths: AppPaths) -> None:
    """Public page entry point; AppPaths is reserved for host integration."""
    st.caption("只读取冻结运行与判分证据；刷新报告、重评分、导出均不调用模型 API。")
    runs = storage.list_runs()
    if not runs:
        st.info("尚无运行记录。完成或部分执行的运行都可以生成报告。")
        return
    run_id = st.selectbox(
        "运行", [run["run_id"] for run in runs],
        format_func=lambda key: next(f'{r["run_id"]} ({r["status"]})' for r in runs if r["run_id"] == key),
        key="report-run",
    )
    plan = storage.get_run(run_id)
    if plan is None:
        st.error("冻结计划不存在。")
        return
    if st.button("根据当前证据生成并保存新报告", key=f"build-report-{run_id}"):
        try:
            report = build_report(
                plan, storage.list_attempts(run_id), storage.list_objects("judge"),
                run_status=storage.get_run_status(run_id),
            )
            storage.save_report(report)
        except (ValueError, TypeError) as exc:
            st.error(f"报告生成失败：{exc}")
        else:
            st.success(f"已保存新版本 {report.id}，历史版本未覆盖。")
    history = report_index(storage, run_id)
    if not history:
        st.info("此运行尚无报告，点击上方按钮生成。没有数据时仍展示完整缺失矩阵。")
        return
    report_id = st.selectbox(
        "报告版本", [r["report_id"] for r in history], index=len(history) - 1,
        key=f"report-version-{run_id}-{len(history)}",
        format_func=lambda key: next(
            f'{r["created_at"]} | {r["status"]} | {"事后分析" if r["post_hoc"] else "冻结评分"} | {r["report_id"][:12]}'
            for r in history if r["report_id"] == key
        ),
    )
    report = report_detail(storage, run_id, report_id)
    if report is None:
        st.error("报告版本不存在，请刷新列表。")
        return
    _show_report(report)
    if st.checkbox("查看所有报告的可比组", key="report-groups"):
        st.dataframe(report_index(storage), use_container_width=True)

    st.subheader("事后重评分")
    st.caption("仅调整已有维度及场景的权重，各组合计必须为 1；生成新报告，沿用所选版本的原始证据。")
    with st.form(f"rescore-{report.id}"):
        text = st.text_area("ScoringConfig JSON", report.scoring.model_dump_json(indent=2), height=260)
        submit = st.form_submit_button("验证权重并保存事后分析新版本")
    if submit:
        try:
            scoring = ScoringConfig.model_validate_json(text)
            child = rescore_report(storage, report, scoring)
        except (ValueError, TypeError) as exc:
            st.error(f"权重或报告验证失败：{exc}")
        else:
            st.success(f"已保存新版本 {child.id}，父版本 {report.id} 未变。")
            st.rerun()

    st.subheader("导出")
    for column, (extension, mime, exporter) in zip(st.columns(3), (
        ("json", "application/json", export_json), ("csv", "text/csv", export_csv),
        ("html", "text/html", export_html),
    )):
        with column:
            st.download_button(
                extension.upper(), data=lambda export=exporter, selected=report: export(selected),
                file_name=f"report-{report.id}.{extension}",
                mime=mime, key=f"export-{extension}-{report.id}", on_click="ignore",
            )
    st.caption("JSON/HTML 保留原文，HTML 已转义；CSV 含每个计划样本及所有重试，危险公式字符串加单引号。")
    if st.checkbox("打开可选 Agent 解释", key=f"explanation-open-{report.id}"):
        _explanation(storage, report)
