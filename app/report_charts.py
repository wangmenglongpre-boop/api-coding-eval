"""Focused overview, separate from long-lived report aggregation modules."""

import plotly.graph_objects as go
import streamlit as st

from coding_eval.report_views import (
    COLORS, POPULATIONS, TOKEN_LABELS, number, quality_rows, radar_rows, token_rows,
)


def render_overview(report) -> None:
    st.subheader("质量总分")
    rows = quality_rows(report)
    for start in range(0, len(rows), 4):
        for column, row in zip(st.columns(min(4, len(rows) - start)), rows[start:start + 4]):
            with column:
                score = row["质量总分"]
                st.metric(row["模型"], number(score if score is not None else row["已评分均分"]))
                st.caption(("质量总分" if score is not None else "已评分均分，非完整成绩")
                           + f' · 已评分 {row["已评分"]}/{row["计划样本"]}')
    if report.status != "complete":
        st.caption("当前为暂定/无效报告，不提供正式排名；已评分均分不能替代完整成绩。")
    quality = go.Figure()
    for i, row in enumerate(rows):
        value = row["质量总分"]
        observed = value is None
        value = row["已评分均分"] if observed else value
        if value is not None:
            quality.add_trace(go.Bar(
                x=[row["模型"]], y=[value], name=row["模型"],
                marker_color=COLORS[i % len(COLORS)], marker_opacity=.5 if observed else 1,
                text=[f'{value:.1f}' + ("（子集）" if observed else "")], textposition="auto",
                hovertemplate="%{x}<br>%{text}<extra></extra>",
            ))
    quality.update_layout(yaxis={"range": [0, 100], "title": "质量分（0–100）"},
                          showlegend=False, height=320, margin=dict(t=20, b=20))
    st.plotly_chart(quality, use_container_width=True, key=f"quality-chart-{report.id}")

    st.subheader("质量维度雷达图")
    radar = go.Figure()
    data = radar_rows(report)
    for i, row in enumerate(data):
        radar.add_trace(go.Scatterpolar(
            r=[*row["values"], row["values"][0]], theta=[*row["labels"], row["labels"][0]],
            mode="lines+markers", fill="toself", name=row["模型"],
            line_color=COLORS[list(report.payload["models"]).index(row["model_id"]) % len(COLORS)],
            hovertemplate="%{theta}: %{r:.1f}<extra>%{fullData.name}</extra>",
        ))
    if data:
        radar.update_layout(polar={"radialaxis": {"visible": True, "range": [0, 100]}},
                            height=440, margin=dict(t=35, b=35), legend={"orientation": "h"})
        st.plotly_chart(radar, use_container_width=True, key=f"radar-chart-{report.id}")
    else:
        st.info("暂无完整维度数据可绘制雷达图；缺失维度不补零。")
    if len(data) < len(rows):
        st.caption("部分模型维度不完整，待评分覆盖完整后显示；真实 0 分会保留。")

    st.subheader("各模型平均 Token 对比")
    population = st.radio(
        "Token 请求范围", list(POPULATIONS), format_func=POPULATIONS.get,
        horizontal=True, key=f"token-population-{report.id}",
    )
    tokens = token_rows(report, population)
    figure = go.Figure()
    for i, (metric, label) in enumerate(TOKEN_LABELS.items()):
        values = [row for row in tokens if row["metric"] == metric]
        figure.add_trace(go.Bar(
            x=[r["模型"] for r in values], y=[r["均值"] for r in values],
            name=label, marker_color=COLORS[i],
            customdata=[[r["有效请求"], r["范围请求"]] for r in values],
            hovertemplate="%{x}<br>均值 %{y:,.1f}<br>已上报 %{customdata[0]}/%{customdata[1]}<extra>%{fullData.name}</extra>",
        ))
    figure.update_layout(barmode="group", height=360, yaxis_title="Token / 请求",
                         legend={"orientation": "h"}, margin=dict(t=30, b=20))
    st.plotly_chart(figure, use_container_width=True, key=f"token-chart-{report.id}")
    st.caption("均值仅包含供应商已上报数据，失败和重试按所选范围计入；未上报不等于 0。"
               "缓存命中是输入的子集，不能与输入相加。输出按 usage 统计，可能包含思考。")
    missing = [r["模型"] for r in rows if all(
        token["均值"] is None for token in tokens if token["model_id"] == r["model_id"]
    )]
    if missing:
        st.info("以下模型在所选范围未上报 Token 用量，图中留空：" + "、".join(missing))
    st.dataframe([
        {key: row[key] for key in ("模型", "指标", "均值", "有效请求", "范围请求", "缺失率")}
        for row in tokens
    ], use_container_width=True)
