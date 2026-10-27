"""启动：python -m streamlit run app/main.py --server.address 127.0.0.1"""

from collections.abc import Callable
import streamlit as st

from coding_eval.storage import AppPaths, Storage
from coding_eval.ui_bootstrap import load_ui_modules

ui_modules = load_ui_modules()
ui_queries = ui_modules["coding_eval.ui_queries"]
product_style = ui_modules["app.product_style"]
dataset_page = ui_modules["app.dataset_page"]
model_page = ui_modules["app.model_page"]
report_views = ui_modules["coding_eval.report_views"]
report_charts = ui_modules["app.report_charts"]
report_page = ui_modules["app.report_page"]
run_module = ui_modules["app.run_page"]
render_models, render_datasets = model_page.render, dataset_page.render
render_reports, render_runs = report_page.render, run_module.render
apply_style = product_style.apply_style

PageRenderer = Callable[[Storage, AppPaths], None]
PAGE_TITLES = ("模型配置", "数据集管理", "评测运行", "结果对比")


def _navigate(source: str) -> None:
    page = st.session_state[source]
    st.session_state["navigation_page"] = page
    for key in ("sidebar-page", "top-page"):
        if key != source:
            st.session_state[key] = page
    st.query_params["page"] = page


def main() -> None:
    st.set_page_config(page_title="API 编码评测", layout="wide", initial_sidebar_state="collapsed")
    apply_style()
    paths = AppPaths.from_env()
    paths.ensure()
    storage = Storage(paths.database)
    st.sidebar.title("API 编码评测")
    requested_page = st.session_state.get("navigation_page", st.query_params.get("page", PAGE_TITLES[0]))
    requested_page = requested_page if requested_page in PAGE_TITLES else PAGE_TITLES[0]
    st.session_state.setdefault("sidebar-page", requested_page)
    st.session_state.setdefault("top-page", requested_page)
    page = st.sidebar.radio(
        "导航", PAGE_TITLES,
        index=None, key="sidebar-page",
        on_change=_navigate, args=("sidebar-page",),
    )
    st.sidebar.caption("单用户本地应用 · 原始 API 编码评测")
    st.sidebar.divider()
    st.sidebar.caption("使用流程")
    st.sidebar.markdown("1. 配置模型与密钥\n2. 验证评测题目\n3. 预览并运行\n4. 对比质量与用量")
    st.radio("页面", PAGE_TITLES, index=None, horizontal=True,
             label_visibility="collapsed", key="top-page", on_change=_navigate, args=("top-page",))
    renderers: dict[str, PageRenderer] = dict(zip(
        PAGE_TITLES, (render_models, render_datasets, render_runs, render_reports), strict=True,
    ))
    st.title(page)
    renderers[page](storage, paths)


if __name__ == "__main__":
    main()
