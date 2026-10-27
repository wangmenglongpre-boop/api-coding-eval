"""Per-batch stratified benchmark planning; previews never call model APIs."""

from __future__ import annotations

import secrets

import streamlit as st

from app.credentials import resolved_keys
from coding_eval.credentials import CredentialError
from coding_eval.livecodebench import (
    DIFFICULTIES, RELEASE, REVISION, catalog_counts, create_sampled_plan,
    prepare_catalog, sampled_tasks,
)
from coding_eval.models import RunPlan, content_hash

LABELS = {"easy": "简单 Easy", "medium": "中等 Medium", "hard": "困难 Hard"}


def render(storage, paths, models, registry):
    st.caption(f"固定官方 {RELEASE} · revision {REVISION[:12]} · Python 3.11")
    st.caption("主指标：原始测试全部通过率。按难度统计；本应用 Docker 适配结果不等同于官方榜单。")
    try:
        population = catalog_counts(paths.root)
    except (ValueError, OSError):
        population = None
        st.info("请先准备固定版题库。首次下载约 4.5 GB，完成后只使用本地压缩缓存。")
    with st.expander("固定版题库与运行约定", expanded=population is None):
        if population:
            st.write({LABELS[d]: population[d] for d in DIFFICULTIES})
        st.write("保留全部原始测试。每测试最多 6 秒，整题最多 120 秒，512 MB；仅 Python 标准库。"
                 "函数题每测试使用新实例，调试输出不计答案；运行方式与官方 harness 有差异。")
        st.code(f"python -m scripts.prepare_livecodebench", language="bash")
        if st.button("准备 / 继续准备固定版 LiveCodeBench", key="lcb-prepare"):
            status = st.empty()
            try:
                prepare_catalog(paths.root, status.write)
            except Exception:
                st.error("题库准备未完成，请检查网络与本地空间后重试；已校验分片可复用。")
            else:
                st.rerun()
    if not population or not models:
        if not models:
            st.info("请先配置并启用评测模型。")
        return
    selected = st.multiselect(
        "评测模型", [m.id for m in models], default=[m.id for m in models], key="lcb-models",
        format_func=lambda i: next(m.display_name for m in models if m.id == i),
    )
    counts = {}
    for d, column in zip(DIFFICULTIES, st.columns(3)):
        counts[d] = int(column.number_input(
            LABELS[d] + " 抽样题数", min_value=0, max_value=population[d],
            value=min(10, population[d]), key="lcb-count-" + d,
        ))
    st.session_state.setdefault("lcb-seed", secrets.randbelow(2**31))
    if st.button("换一组随机样本", key="lcb-new-seed"):
        st.session_state["lcb-seed"] = secrets.randbelow(2**31)
        st.session_state.pop("_lcb_preview", None)
    seed = int(st.number_input("抽样种子（填写已有种子可复现）", step=1, key="lcb-seed"))
    total = sum(counts.values())
    chosen = [m for m in models if m.id in selected]
    form_key = content_hash([selected, counts, seed])[:16]
    with st.form("lcb-plan-" + form_key):
        left, middle, right = st.columns(3)
        repeats = int(left.number_input("每题重复次数", min_value=1, value=1))
        concurrency = int(middle.number_input("并发上限", min_value=1, max_value=128, value=1))
        retries = int(right.number_input("每样本失败重试上限", min_value=0, value=0))
        budget = int(st.number_input("评测请求预算上限", min_value=1,
                                     value=max(1, total * len(chosen))))
        st.caption(f"本批抽 {total} 题、{len(chosen)} 个模型，共用同一份题目；"
                   "预算需覆盖 题数 × 模型数 × 重复次数 × (1 + 重试上限)。")
        preview = st.form_submit_button("抽样并生成冻结预览", type="primary")
    if preview:
        st.session_state.pop("_lcb_preview", None)
        try:
            with st.spinner("本地抽样、验证原始测试结构并冻结容器环境"):
                # Cheap form/budget checks before decoding tests or touching Docker.
                if not chosen or total == 0:
                    raise ValueError("请至少选择一个模型和一道题")
                if budget < total * len(chosen) * repeats * (1 + retries):
                    raise ValueError("请求预算不足以覆盖抽样与重试")
                tasks = sampled_tasks(paths.root, counts, seed)
                plan = create_sampled_plan(
                    chosen, tasks, counts, seed, paths, repeats=repeats,
                    concurrency=concurrency, max_retries=retries, max_requests=budget,
                    budget_group="livecodebench-v6",
                )
            st.session_state["_lcb_preview"] = plan.model_dump_json()
        except Exception as exc:
            from app.run_page import _plan_error
            if isinstance(exc, (ValueError, TypeError)):
                _plan_error(exc)
            else:
                st.error("预览失败，请检查 Docker Desktop 与题库缓存；没有开始模型评测。")
    frozen = st.session_state.get("_lcb_preview")
    if not frozen:
        return
    plan = RunPlan.model_validate_json(frozen)
    st.subheader("已冻结 LiveCodeBench 计划")
    st.write(f"计划 {plan.id[:12]} · seed {plan.random_seed} · "
             f"{len(plan.task_snapshots)} 题 · {plan.expected_requests} 次生成 · 预算 {plan.max_requests}")
    st.caption("开始按钮执行这份冻结样本；修改上方参数后需重新生成预览。恢复批次不会重新抽题。")
    st.dataframe([
        {"题号": s.restore().benchmark.question_id, "平台": s.restore().benchmark.platform,
         "难度": LABELS[s.restore().benchmark.difficulty], "题目": s.restore().title,
         "原始测试数": s.restore().benchmark.test_count, "任务哈希": s.version_hash}
        for s in plan.task_snapshots
    ], use_container_width=True)
    st.download_button(
        "下载冻结计划 JSON（含抽样与完整测试）", data=plan.model_dump_json,
        file_name=f"livecodebench-plan-{plan.id}.json", on_click="ignore",
    )
    with st.expander("抽样来源与冻结模型参数"):
        st.json(plan.sampling.model_dump(mode="json"))
        st.json([s.restore().model_dump(mode="json") for s in plan.model_snapshots])
    confirmed = st.checkbox("我确认此冻结计划和可能产生的 API 费用", key="lcb-confirm-" + plan.id)
    if st.button("开始冻结计划", disabled=not confirmed, key="lcb-start-" + plan.id):
        try:
            keys = resolved_keys([s.restore() for s in plan.model_snapshots])
            if storage.get_run(plan.id) is None:
                storage.save_run(plan)
            registry.start(storage, paths, plan.id, api_keys=keys)
        except CredentialError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop("_lcb_preview", None)
            # Reset before the next page renders, avoiding mutation of an active widget.
            st.session_state["_lcb_reset_seed"] = True
            st.rerun()
