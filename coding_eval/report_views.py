"""Read-only report presentation, compatible with saved reports and cached models."""

from __future__ import annotations

import html
import json
import math

from .models import DIMENSION_LABELS, ReportVersion

COLORS = ("#2563eb", "#ea580c", "#16a34a", "#9333ea", "#0891b2", "#db2777")
TOKEN_LABELS = {"input_tokens": "平均输入 Token", "cache_read_tokens": "平均缓存命中 Token",
                "output_tokens": "平均输出 Token"}
POPULATIONS = {"all_attempts": "全部实际请求（含失败和重试）", "completed_attempts": "仅完成请求",
               "noncompleted_attempts": "仅未完成请求"}


def model_name(report: ReportVersion, model_id: str) -> str:
    name = report.payload.get("model_snapshots", {}).get(model_id, {}).get("config", {}).get("display_name")
    return f"{name} ({model_id})" if name and name != model_id else model_id


def quality_rows(report: ReportVersion) -> list[dict]:
    return [
        {"model_id": key, "模型": model_name(report, key), "质量总分": model.get("quality_score"),
         "已评分均分": model.get("observed_quality_score"), "已评分": model["scored"],
         "计划样本": model["expected"], "覆盖率": model["coverage"],
         "正式排名": report.status == "complete"}
        for key, model in report.payload["models"].items()
    ]


def token_rows(report: ReportVersion, population: str = "all_attempts") -> list[dict]:
    if population not in POPULATIONS:
        raise ValueError("Unknown request population")
    rows = []
    for key, model in report.payload["models"].items():
        for metric, label in TOKEN_LABELS.items():
            summary = model.get("metrics", {}).get(metric, {}).get(population, {})
            rows.append({"model_id": key, "模型": model_name(report, key), "指标": label,
                         "均值": summary.get("mean"), "有效请求": summary.get("n", 0),
                         "范围请求": summary.get("total", 0), "缺失率": summary.get("missing_rate"),
                         "metric": metric})
    return rows


def radar_rows(report: ReportVersion) -> list[dict]:
    dimensions = [w.name for w in report.scoring.dimension_weights]
    if len(dimensions) < 2:
        return []
    rows = []
    for key, model in report.payload["models"].items():
        values = [model.get("dimension_scores", {}).get(d) for d in dimensions]
        # Missing dimensions are not invented or treated as zero.
        if len(values) >= 2 and all(v is not None for v in values):
            rows.append({"model_id": key, "模型": model_name(report, key), "values": values,
                         "labels": [DIMENSION_LABELS.get(d, d) for d in dimensions]})
    return rows


def difficulty_rows(report: ReportVersion) -> list[dict]:
    labels = {"LCB-easy": "简单 Easy", "LCB-medium": "中等 Medium", "LCB-hard": "困难 Hard"}
    return [
        {"model_id": key, "模型": model_name(report, key), "难度": labels[scenario],
         "value": value.get("quality_score"), "color": index}
        for index, (key, model) in enumerate(report.payload["models"].items())
        for scenario, value in model.get("scenarios", {}).items() if scenario in labels
    ]


def number(value) -> str:
    return "未上报 / 不完整" if value is None else f"{value:,.1f}"


def _escape(value) -> str:
    return html.escape(str(value), quote=True)


def _svg(body: str, height: int, title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{_escape(title)}" '
            f'viewBox="0 0 900 {height}"><title>{_escape(title)}</title>{body}</svg>')


def bars_svg(rows: list[dict], title: str, maximum: float | None = None) -> str:
    """Grouped horizontal bars; missing values still have explicit labels."""
    maximum = maximum or max([r["value"] for r in rows if r["value"] is not None] or [1]) or 1
    body = []
    for index, row in enumerate(rows):
        y = 22 + index * 36
        value = row["value"]
        width = 440 * value / maximum if value is not None else 0
        body.append(f'<text x="8" y="{y + 16}" font-size="12">{_escape(row["label"])}</text>')
        body.append(f'<rect x="340" y="{y}" width="{width:.2f}" height="23" rx="4" '
                    f'fill="{COLORS[row.get("color", index) % len(COLORS)]}"/>')
        body.append(f'<text x="{350 + width:.2f}" y="{y + 16}" font-size="12">'
                    f'{_escape(number(value))}</text>')
    return _svg("".join(body), max(85, len(rows) * 36 + 40), title)


def radar_svg(report: ReportVersion) -> str:
    rows = radar_rows(report)
    if not rows:
        if len(report.scoring.dimension_weights) == 1:
            return "<p>单一正确性评分，查看分难度全通过率。</p>"
        return "<p>暂无完整维度数据可绘制雷达图；缺失维度不补零。</p>"
    labels, size = rows[0]["labels"], len(rows[0]["values"])
    cx, cy, radius = 320, 235, 155

    def point(index, score):
        angle = -math.pi / 2 + 2 * math.pi * index / size
        return cx + radius * score / 100 * math.cos(angle), cy + radius * score / 100 * math.sin(angle)

    def polygon(values):
        return " ".join(f"{x:.2f},{y:.2f}" for x, y in (point(i, v) for i, v in enumerate(values)))

    body = []
    for score in (20, 40, 60, 80, 100):
        body.append(f'<polygon points="{polygon([score] * size)}" fill="none" stroke="#cbd5e1"/>')
        body.append(f'<text x="{cx+5}" y="{cy-radius*score/100}" font-size="11">{score}</text>')
    for i, label in enumerate(labels):
        x, y = point(i, 100)
        tx, ty = point(i, 120)
        body.append(f'<line x1="{cx}" y1="{cy}" x2="{x}" y2="{y}" stroke="#cbd5e1"/>')
        body.append(f'<text x="{tx}" y="{ty}" text-anchor="middle" font-size="14">{_escape(label)}</text>')
    for index, row in enumerate(rows):
        color = COLORS[list(report.payload["models"]).index(row["model_id"]) % len(COLORS)]
        body.append(f'<polygon points="{polygon(row["values"])}" fill="{color}" fill-opacity=".10" '
                    f'stroke="{color}" stroke-width="2"/>')
        for i, value in enumerate(row["values"]):
            x, y = point(i, value)
            body.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}">'
                        f'<title>{_escape(row["模型"])} / {_escape(labels[i])}: {number(value)}</title></circle>')
        body.append(f'<text x="590" y="{85+index*35}" fill="{color}" font-size="13">'
                    f'{_escape(row["模型"])}</text>')
    return _svg("".join(body), max(450, 130 + len(rows) * 35), "质量维度雷达图（0–100）")


def export_visual_html(report: ReportVersion) -> str:
    """Standalone CSS/SVG; dynamic text escaped, no scripts or external resources."""
    quality, tokens = quality_rows(report), token_rows(report)
    cards = "".join(
        '<article><h3>' + _escape(r["模型"]) + '</h3><strong>'
        + number(r["质量总分"] if r["质量总分"] is not None else r["已评分均分"])
        + '</strong><p>' + ("质量总分" if r["质量总分"] is not None else "已评分均分（非完整成绩）")
        + (" · 正式可比组" if r["正式排名"] else " · 暂定，不提供正式排名")
        + f'</p><p>已评分 {r["已评分"]}/{r["计划样本"]}</p></article>' for r in quality
    )
    score_bars = bars_svg([
        {"label": r["模型"] + ("（子集）" if r["质量总分"] is None else ""),
         "value": r["质量总分"] if r["质量总分"] is not None else r["已评分均分"],
         "color": i} for i, r in enumerate(quality)
    ], "质量总分（子集不作为完整成绩）", maximum=100)
    token_bars = bars_svg([
        {"label": f'{r["模型"]} · {r["指标"]} ({r["有效请求"]}/{r["范围请求"]})',
         "value": r["均值"], "color": list(TOKEN_LABELS).index(r["metric"])} for r in tokens
    ], "平均 Token（全部实际请求）")
    table = '<table><tr><th>模型</th><th>指标</th><th>均值</th><th>有效 / 范围请求</th><th>缺失率</th></tr>'
    table += "".join(
        f'<tr><td>{_escape(r["模型"])}</td><td>{r["指标"]}</td><td>{number(r["均值"])}</td>'
        f'<td>{r["有效请求"]}/{r["范围请求"]}</td><td>'
        + ("未知" if r["缺失率"] is None else f'{r["缺失率"]:.0%}') + '</td></tr>' for r in tokens
    ) + '</table>'
    warnings = "".join(f'<li>{_escape(note)}</li>' for note in report.payload["warnings"])
    difficulty = difficulty_rows(report)
    dimensions_chart = (
        '<section><h2>分难度全通过率（%）</h2>' + bars_svg([
            {**row, "label": f'{row["模型"]} · {row["难度"]}'} for row in difficulty
        ], "分难度原始测试全通过率（%）", maximum=100) + '</section>'
        if difficulty else
        '<section><h2>质量维度雷达图</h2>' + radar_svg(report)
        + '<p>沿用冻结权重与质量维度，缺失不补零；暂定报告不提供正式排名。</p></section>'
    )
    answers = []
    by_id = {a["id"]: a for a in report.payload["attempts"]}
    for sample in report.payload["samples"]:
        title = f'{sample["model_id"]} / {sample["task_id"]} / sample {sample["sample_index"]}'
        answers.append(f'<details><summary>{_escape(title)}</summary><pre>'
                       f'{_escape(json.dumps(sample, ensure_ascii=False, indent=2))}</pre>')
        for identifier in sample["attempt_ids"]:
            answers.append(f'<h4>{_escape(identifier)}</h4><pre>{_escape(by_id[identifier]["raw_output"])}</pre>')
        answers.append('</details>')
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; '
        'base-uri \'none\'; form-action \'none\'">'
        f'<title>API 编码评测 {_escape(report.id)}</title><style>'
        'body{font-family:system-ui,sans-serif;background:#f5f7fb;color:#172033;max-width:1200px;'
        'margin:auto;padding:30px}section,article,details{background:white;border:1px solid #e2e8f0;'
        'border-radius:12px;padding:20px;margin:16px 0}.cards{display:flex;flex-wrap:wrap;gap:16px}'
        'article{flex:1;min-width:200px}strong{font-size:38px;color:#2563eb}svg{width:100%;height:auto}'
        'table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}</style></head><body>'
        '<h1>模型编码能力对比</h1>'
        f'<p>run: {_escape(report.run_id)}; status: {_escape(report.status)}; post_hoc: {report.post_hoc}</p>'
        '<div class="cards">' + cards + '</div>'
        '<section><h2>质量总分（0–100）</h2>' + score_bars + '</section>'
        + dimensions_chart +
        '<section><h2>平均 Token 对比</h2><p>全部实际请求，含失败和重试。均值只含已上报数据，'
        '有效请求数按指标分别计算；未上报不等于零。缓存命中是输入的子集，不能相加。'
        '输出按供应商 usage 统计，可能包含思考。</p>' + token_bars + table + '</section>'
        '<details><summary>规则、覆盖与局限</summary><ul>' + warnings + '</ul><pre>'
        + _escape(json.dumps(report.payload["coverage"], ensure_ascii=False, indent=2)) + '</pre></details>'
        '<details><summary>逐任务答案与样本证据</summary>' + "".join(answers) + '</details>'
        '<details><summary>完整报告与原始响应</summary><pre>'
        + _escape(report.model_dump_json(indent=2)) + '</pre></details></body></html>'
    )
