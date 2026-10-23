"""Offline regressions for scheduling, honest denominators and visual exports."""

import asyncio
import httpx
import pytest

from coding_eval.failure_diagnostics import describe_attempt
from coding_eval.models import AttemptMetrics, AttemptResult, ReportVersion, RunStatus
from coding_eval.report_views import export_visual_html, quality_rows, radar_rows, token_rows
from coding_eval.runner import BatchRunner
from test_api import delta, event, finish, generate
from test_reports import Tags, evidence, make_plan, report_for
from test_runner import FakeAPI, env, judged, plan_for


def test_reasoning_timeout_records_phase_without_reasoning_or_secret():
    result, _, _ = generate(
        [event(delta(reasoning_content="PRIVATE_THINKING")), httpx.ReadTimeout("private body")],
        api_key="private-key",
    )
    assert result.error_code == "timeout" and result.status == "failed"
    assert result.diagnostics["reasoning_chunks"] == 1
    assert result.diagnostics["answer_chunks"] == 0
    assert result.diagnostics["headers_seconds"] is not None
    assert describe_attempt(result)["原因"] == "思考阶段超时（尚无最终回答）"
    text = result.model_dump_json()
    assert all(s not in text for s in ("PRIVATE_THINKING", "private body", "private-key"))


def test_timeout_after_answer_and_old_record_inference():
    result, _, _ = generate([event(delta("partial")), httpx.ReadTimeout("interrupted")])
    assert result.raw_output == "partial" and not result.metrics.stream_complete
    assert describe_attempt(result)["原因"].startswith("回答生成中")
    old = result.model_dump(mode="json")
    old.pop("diagnostics")
    restored = AttemptResult.model_validate(old)
    assert restored.diagnostics == {}
    assert describe_attempt(restored)["原因"].startswith("回答生成中")


def test_truncation_is_not_success_and_diagnoses_thinking_limit():
    result, _, _ = generate([
        event(delta(reasoning_content="omitted")),
        event({**finish("length"), "usage": {"completion_tokens": 8192,
                                            "completion_tokens_details": {"reasoning_tokens": 8192}}}),
        event("[DONE]"),
    ])
    assert result.status == "failed"
    assert result.diagnostics["finish_reason"] == "length"
    assert describe_attempt(result)["原因"] == "输出额度被思考耗尽"


@pytest.mark.parametrize("code", ["http_401", "http_403"])
def test_auth_block_has_no_fake_requests_and_resume_obeys_budget(env, code):
    storage, paths = env
    plan = plan_for(paths, model_count=2, repeats=3, concurrency=1, max_retries=1, max_requests=12)
    storage.save_run(plan)

    class AuthAPI(FakeAPI):
        async def generate(self, config, *args, **kwargs):
            result = await super().generate(config, *args, **kwargs)
            if config.id == "model0":
                return result.model_copy(update={"status": "failed", "error_code": code})
            return result

    api = AuthAPI()
    runner = BatchRunner(storage, paths, client=api, judge=judged)
    assert asyncio.run(runner.run(plan.id)) == RunStatus.FAILED
    assert [c[0].id for c in api.calls].count("model0") == 1
    assert [c[0].id for c in api.calls].count("model1") == 3
    assert len(storage.list_attempts(plan.id)) == len(storage.list_objects("request_intent")) == 4
    assert runner.status(plan.id)["blocked_models"] == ["model0"]
    assert runner.status(plan.id)["remaining_samples"] == 2
    resumed = FakeAPI()
    engine = BatchRunner(storage, paths, client=resumed, judge=judged)
    assert asyncio.run(engine.run(plan.id)) == RunStatus.COMPLETED
    assert len(resumed.calls) == 3  # two undispatched samples, one declared retry
    assert all(c[0].id == "model0" for c in resumed.calls)
    assert engine.status(plan.id)["blocked_models"] == []
    assert len(storage.list_objects("request_intent")) == 7 <= plan.max_requests
    assert storage.get_run(plan.id).version_hash == plan.version_hash


def test_missing_token_is_not_zero_and_each_population_has_own_denominator():
    plan = make_plan(repeats=2, max_retries=0)
    attempts, judges = evidence(plan)
    attempts[0] = attempts[0].model_copy(update={
        "metrics": AttemptMetrics(input_tokens=100, cache_read_tokens=0, output_tokens=20),
    })
    attempts[1] = attempts[1].model_copy(update={
        "status": "failed", "error_code": "timeout", "metrics": AttemptMetrics(output_tokens=40),
    })
    report = report_for(plan, attempts, judges)
    rows = {r["metric"]: r for r in token_rows(report)}
    assert rows["input_tokens"]["均值"] == 100 and rows["input_tokens"]["有效请求"] == 1
    assert rows["cache_read_tokens"]["均值"] == 0
    assert rows["cache_read_tokens"]["范围请求"] == 2
    assert rows["cache_read_tokens"]["缺失率"] == .5
    assert rows["output_tokens"]["均值"] == 30
    completed = {r["metric"]: r for r in token_rows(report, "completed_attempts")}
    failed = {r["metric"]: r for r in token_rows(report, "noncompleted_attempts")}
    assert completed["output_tokens"]["均值"] == 20
    assert failed["output_tokens"]["均值"] == 40
    assert failed["cache_read_tokens"]["均值"] is None
    with pytest.raises(ValueError):
        token_rows(report, "unknown")


def test_radar_keeps_real_zero_and_skips_incomplete_models_old_report():
    plan = make_plan(models=("alpha", "beta"), max_retries=0)
    attempts, judges = evidence(plan, {("alpha", "t0", 0): (0, 0)})
    report = report_for(plan, attempts[:1], judges[:1])
    old = report.model_dump(mode="json")
    for attempt in old["payload"]["attempts"]:
        attempt.pop("diagnostics", None)
    restored = ReportVersion.model_validate(old)
    data = radar_rows(restored)
    assert len(data) == 1 and data[0]["values"] == [0, 0]
    assert quality_rows(restored)[1]["质量总分"] is None
    assert not any(r["正式排名"] for r in quality_rows(restored))


def test_visual_html_escapes_names_and_contains_standalone_charts():
    plan = make_plan(max_retries=0)
    report = report_for(plan)
    data = report.model_dump(mode="json")
    attack = '</text><script>bad()</script><img src=x onerror=bad()>'
    data["payload"]["model_snapshots"]["alpha"]["config"]["display_name"] = attack
    report = ReportVersion.model_validate(data)
    text = export_visual_html(report)
    parser = Tags()
    parser.feed(text)
    assert parser.tags.count("svg") == 3
    assert "script" not in parser.tags and "img" not in parser.tags
    assert not any(name.startswith("on") for name, _ in parser.attributes)
    assert attack not in text and "&lt;script&gt;" in text
    assert all(label in text for label in ("质量总分", "雷达图", "平均输入 Token", "平均缓存命中 Token", "平均输出 Token"))
    assert "default-src &#" not in text and "default-src 'none'" in text


def test_overview_radio_changes_token_population_without_mutating_report():
    from streamlit.testing.v1 import AppTest

    report = report_for(make_plan(max_retries=0))
    original = report.model_dump_json()
    source = """
import streamlit as st
from app.report_charts import render_overview
from coding_eval.models import ReportVersion
render_overview(ReportVersion.model_validate_json(st.session_state["report_json"]))
"""
    page = AppTest.from_string(source)
    page.session_state["report_json"] = original
    page.run()
    assert not page.exception and len(page.get("plotly_chart")) == 3
    page.radio(key=f"token-population-{report.id}").set_value("noncompleted_attempts").run()
    assert not page.exception
    assert page.dataframe[0].value["范围请求"].tolist() == [0, 0, 0]
    assert page.session_state["report_json"] == original
