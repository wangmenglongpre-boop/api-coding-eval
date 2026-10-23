"""Dataset editing UI. All executable validation belongs to the Docker judge."""

from __future__ import annotations

import json
from uuid import uuid4

import streamlit as st

from coding_eval.datasets import (
    as_draft, export_task, import_task, list_tasks, load_builtin_tasks,
    parse_task_json, save_task, validation_issues,
)
from coding_eval.models import OutputFormat, TaskDefinition
from coding_eval.reference_datasets import load_reference_tasks
from coding_eval.storage import AppPaths, Storage
from coding_eval.ui_queries import current_tasks

PRODUCT_UI_REVISION = "product-v1"


def _template() -> TaskDefinition:
    return TaskDefinition(
        id="custom-" + uuid4().hex[:12],
        scenario_id="custom",
        title="新建场景任务",
        description="自建模拟题，请补充业务背景。",
        system_prompt="只输出一个 python 围栏块，内容为完整 solution.py。",
        requirements="请填写明确的接口、输入、返回值、异常契约和评分规则。",
        public_files={"solution.py": "def solve():\n    raise NotImplementedError\n"},
        output_format=OutputFormat(
            kind="files", allowed_paths=("solution.py",),
            instructions="只输出一个 python 围栏块，内容为完整 solution.py。",
        ),
    )


def _json(task: TaskDefinition) -> str:
    return json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _store(storage: Storage, task: TaskDefinition) -> None:
    save_task(storage, task)
    st.session_state["dataset_pending_selection"] = task.id
    st.session_state["dataset_notice"] = "任务快照已保存；历史版本保持不变。"
    st.rerun()


def _validate(storage: Storage, paths: AppPaths, task: TaskDefinition) -> None:
    try:
        from coding_eval.judges import validate_task
    except ImportError:
        st.warning("判分模块尚未接入。任务仍为 draft，不会在宿主执行。")
        return
    try:
        with st.spinner("正在容器中验证参考解和负解，请勿关闭页面……"):
            validated = validate_task(task, paths)
        if not isinstance(validated, TaskDefinition) or not validated.ready:
            st.error("判分器未返回就绪任务，未保存就绪状态。")
            return
        save_task(storage, validated)
    except Exception as exc:
        st.error(f"验证未通过或已阻塞，未写入就绪状态：{exc}")
        for path in getattr(exc, "artifact_paths", ()):
            st.code(path, language=None)
        return
    st.success("参考解和负解已通过真实容器验证，就绪快照已保存。")
    st.rerun()


def render(storage: Storage, paths: AppPaths) -> None:
    """Page entry point, compatible with app.main.PageRenderer."""
    st.caption("题库代码仅作数据存储；只有受限容器验证通过后，任务才可用于正式评分。")
    notice = st.session_state.pop("dataset_notice", None)
    if notice:
        st.success(notice)
    pending = st.session_state.pop("dataset_pending_selection", None)
    if pending is not None:
        st.session_state["dataset_selected"] = pending
    tasks = current_tasks(storage)
    by_id = {task.id: task for task in tasks}
    create_col, seed_col, reference_col, template_col = st.columns(4)
    with create_col:
        if st.button("创建任务", key="dataset_create"):
            _store(storage, _template())
    with seed_col:
        if st.button("加载缺失内置题", key="dataset_seed"):
            added = 0
            for task in load_builtin_tasks():
                if task.id not in by_id:
                    save_task(storage, task)
                    added += 1
            st.session_state["dataset_notice"] = f"新增 {added} 题；已有任务未覆盖，全部新增任务为 draft。"
            st.rerun()
    with reference_col:
        if st.button("加载缺失参考题", key="dataset_reference"):
            added = 0
            for task in load_reference_tasks():
                if task.id not in by_id:
                    save_task(storage, task)
                    added += 1
            st.session_state["dataset_notice"] = f"新增 {added} 道 C11–C15 Python 核心逻辑改编题；新增为 draft，需容器验证。"
            st.rerun()
    with template_col:
        st.download_button(
            "下载任务 ZIP 模板", data=lambda: export_task(_template()),
            file_name="task-template.zip", mime="application/zip", key="dataset_template", on_click="ignore",
        )

    with st.expander("导入任务 ZIP"):
        upload = st.file_uploader("JSON 元数据加代码 ZIP", type=["zip"], key="dataset_upload")
        st.caption("限制：压缩包 8 MiB、展开 16 MiB、单文件 2 MiB、256 个文件；导入后必须重新验证。")
        if upload is not None:
            try:
                imported = import_task(upload.getvalue())
            except (ValueError, TypeError) as exc:
                st.error(str(exc))
            else:
                st.write(f"{imported.id} · {imported.title} · {imported.scenario_id}")
                for issue in validation_issues(imported):
                    st.warning(issue)
                replace = imported.id not in by_id or st.checkbox(
                    "同 ID 已存在：确认追加导入版本（保留历史）", key="dataset_import_existing",
                )
                if st.button("保存导入版本", disabled=not replace, key="dataset_import"):
                    _store(storage, imported)
    if not tasks:
        st.info("暂无任务。可创建任务、加载内置题与五道参考改编题，或导入 ZIP。")
        return

    selected = st.session_state.get("dataset_selected")
    if selected not in by_id:
        st.session_state["dataset_selected"] = tasks[0].id
    task_id = st.selectbox(
        "场景 / 任务", options=list(by_id), key="dataset_selected",
        format_func=lambda key: f"{by_id[key].scenario_id} · {by_id[key].title} [{by_id[key].status}]",
    )
    task = by_id[task_id]
    st.caption(f"任务 ID：{task.id} · 版本 SHA256：{task.version_hash}")
    st.caption(f"内容 SHA256：{task.validation_hash} · 来源：{task.source}")
    issues = validation_issues(task)
    for issue in issues:
        st.warning(issue)

    with st.expander("实际模型输入", expanded=True):
        st.markdown("**系统提示词**")
        st.code(task.system_prompt, language=None)
        st.markdown("**需求**")
        st.code(task.requirements, language=None)
        st.markdown("**输出协议**")
        st.code(task.output_format.instructions, language=None)
        for path, source in task.public_files.items():
            st.text(path)
            st.code(source, language="python" if path.endswith(".py") else None)
    with st.expander("评分项、运行预算及验证证据"):
        st.json({
            "rubric": [item.model_dump(mode="json") for item in task.rubric],
            "scoring": task.scoring.model_dump(mode="json"),
            "runtime": task.runtime.model_dump(mode="json"),
            "validation_evidence": task.validation_evidence,
            "validated_content_hash": task.validated_content_hash,
        })
    if st.checkbox("打开 JSON 编辑（含判分端参考解与隐藏测试）", key=f"task-edit-{task.id}"):
        st.caption("保存编辑会清除验证证据并追加 draft 版本。可修改 scenario_id 来创建或复制场景归属。")
        with st.form(f"dataset_edit_{task.id}_{task.version_hash}"):
            text = st.text_area("TaskDefinition JSON", value=_json(task), height=600)
            submitted = st.form_submit_button("保存新草稿版本")
        if submitted:
            try:
                # Ignore stale ready metadata before validation, not after it.
                edited = parse_task_json(text, reset_validation=True)
                if edited.id != task.id:
                    raise ValueError("编辑不能更改任务 ID，请使用复制任务")
            except (ValueError, TypeError, RecursionError) as exc:
                st.error(str(exc))
            else:
                _store(storage, edited)
    with st.expander("复制任务 / 场景"):
        with st.form(f"dataset_copy_{task.id}"):
            new_id = st.text_input("新任务 ID", value=task.id + "-copy")
            scenario = st.text_input("目标场景 ID", value=task.scenario_id)
            title = st.text_input("新标题", value=task.title + "（副本）")
            copied = st.form_submit_button("复制为草稿")
        if copied:
            try:
                if new_id in by_id:
                    raise ValueError("新任务 ID 已存在")
                value = as_draft(task).model_dump(mode="json")
                value.update(id=new_id, scenario_id=scenario, title=title)
                clone = TaskDefinition.model_validate(value)
            except ValueError as exc:
                st.error(str(exc))
            else:
                _store(storage, clone)

    export_col, validate_col = st.columns(2)
    with export_col:
        st.download_button(
            "导出当前 ZIP 快照", data=lambda selected=task: export_task(selected),
            file_name=f"{task.id}-{task.version_hash[:12]}.zip",
            mime="application/zip", key="dataset_export", on_click="ignore",
        )
    with validate_col:
        if st.button("容器验证参考解与负解", disabled=bool(issues), key="dataset_validate"):
            _validate(storage, paths, task)


render_datasets = render
