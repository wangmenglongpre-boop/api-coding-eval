"""Reproducible benchmark/protocol checks; no model API calls."""

import base64
import hashlib
import json
import os
import pickle
import sqlite3
import sys
import zlib
from unittest.mock import Mock

import pytest

from coding_eval import livecodebench as lcb
from coding_eval.container_check import DockerSandbox, run_command
from coding_eval.judges import judge_attempt, validate_task
from coding_eval.lcb_container import stdin_equal
from coding_eval.models import AttemptResult, ModelConfig, RunPlan, content_hash
from coding_eval.reports import build_report, export_csv, export_json
from coding_eval.runner import sample_schedule, task_prompts
from coding_eval.storage import AppPaths, Storage


def row(difficulty="easy", identifier="1", functional=False):
    public = [{"input": "[1,2]\n3" if functional else "1 2\n", "output": "6" if functional else "3\n",
               "testtype": "functional" if functional else "stdin"}]
    private = [{"input": "[3,4]\n5" if functional else "3 4\n", "output": "12" if functional else "7\n",
                "testtype": public[0]["testtype"]}]
    return {
        "question_title": f"Sum {identifier}", "question_content": "Return the sum.",
        "platform": "leetcode" if functional else "atcoder", "question_id": identifier,
        "contest_id": "fixture", "contest_date": "2025-06-01T00:00:00", "starter_code": "",
        "difficulty": difficulty, "public_test_cases": json.dumps(public),
        "private_test_cases": base64.b64encode(zlib.compress(pickle.dumps(json.dumps(private)))).decode(),
        "metadata": json.dumps({"func_name": "sumValues"} if functional else {}),
    }


@pytest.fixture
def catalog(tmp_path):
    path = lcb.catalog_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE shards (name TEXT PRIMARY KEY, sha TEXT);
            CREATE TABLE questions (id TEXT PRIMARY KEY, difficulty TEXT, shard TEXT, payload BLOB, row_sha TEXT);
        """)
        db.executemany("INSERT INTO shards VALUES (?,?)", lcb.SHARDS.items())
        for d in lcb.DIFFICULTIES:
            for i in range(12):
                r = row(d, d + str(i))
                raw = json.dumps(r).encode()
                db.execute("INSERT INTO questions VALUES (?,?,?,?,?)", (
                    r["platform"] + ":" + r["question_id"], d, "fixture", zlib.compress(raw),
                    hashlib.sha256(raw).hexdigest(),
                ))
    return tmp_path


def model(identifier="m1"):
    return ModelConfig(id=identifier, display_name=identifier, protocol="openai",
                       url="https://unused.invalid/v1", model_id=identifier)


def add_original(root, original):
    path = lcb.catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS shards (name TEXT PRIMARY KEY, sha TEXT);
            CREATE TABLE IF NOT EXISTS questions
                (id TEXT PRIMARY KEY, difficulty TEXT, shard TEXT, payload BLOB, row_sha TEXT);
        """)
        db.executemany("INSERT OR REPLACE INTO shards VALUES (?,?)", lcb.SHARDS.items())
        raw = json.dumps(original).encode()
        db.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?,?)", (
            original["platform"] + ":" + original["question_id"], original["difficulty"], "fixture",
            zlib.compress(raw), hashlib.sha256(raw).hexdigest(),
        ))


def paths(root):
    return AppPaths(root, root / "app.sqlite3", root / "artifacts")


def test_default_sample_is_balanced_shared_and_reproducible(catalog, monkeypatch):
    monkeypatch.setattr(DockerSandbox, "prepare", lambda self: "sha256:" + "a" * 64)
    counts = dict.fromkeys(lcb.DIFFICULTIES, 10)
    tasks = lcb.sampled_tasks(catalog, counts, 42)
    repeated = lcb.sampled_tasks(catalog, counts, 42)
    assert [t.version_hash for t in tasks] == [t.version_hash for t in repeated]
    assert len(tasks) == len({t.id for t in tasks}) == 30
    assert [t.id for t in tasks] != [t.id for t in lcb.sampled_tasks(catalog, counts, 43)]
    plan = lcb.create_sampled_plan([model(), model("m2")], tasks, counts, 42, paths(catalog),
                                  repeats=1, max_requests=60)
    assert plan.expected_requests == 60
    assert plan.sampling.task_versions == {s.object_id: s.version_hash for s in plan.task_snapshots}
    schedule = sample_schedule(plan)
    assert {t for m, t, _ in schedule if m == "m1"} == {t for m, t, _ in schedule if m == "m2"}
    storage = Storage(paths(catalog).database)
    storage.save_run(plan)
    assert storage.get_run(plan.id) == RunPlan.model_validate_json(plan.model_dump_json())
    assert all(s.restore().ready and not s.restore().reference_files for s in plan.task_snapshots)
    value = plan.model_dump(mode="json")
    value["sampling"]["seed"] += 1
    with pytest.raises(ValueError, match="抽样"):
        RunPlan.model_validate(value)


@pytest.mark.parametrize("counts", [
    {"easy": 13, "medium": 10, "hard": 10},
    {"easy": -1, "medium": 10, "hard": 10},
    {"easy": 0, "medium": 0, "hard": 0},
    {"easy": 1}, {"easy": True, "medium": 1, "hard": 1},
])
def test_invalid_sampling_does_not_silently_adjust_counts(catalog, counts):
    with pytest.raises(ValueError):
        lcb.sample_rows(catalog, counts, 1)


def test_partial_or_corrupt_catalog_is_rejected(catalog):
    with sqlite3.connect(lcb.catalog_path(catalog)) as db:
        db.execute("UPDATE questions SET row_sha='bad'")
    with pytest.raises(ValueError, match="hash"):
        lcb.sample_rows(catalog, dict.fromkeys(lcb.DIFFICULTIES, 1), 1)
    with sqlite3.connect(lcb.catalog_path(catalog)) as db:
        db.execute("DELETE FROM shards")
    with pytest.raises(ValueError, match="未完成"):
        lcb.catalog_counts(catalog)


def test_private_cases_decode_without_loading_globals():
    original = row()
    assert len(lcb.decode_private(original["private_test_cases"])) == 1
    assert lcb.decode_private(json.dumps([{"input": "1", "output": "1", "testtype": "stdin"}]))
    # A GLOBAL opcode is rejected before any callable can execute.
    hostile = base64.b64encode(zlib.compress(b"cos\nsystem\n(S'echo should-never-run'\ntR.")).decode()
    with pytest.raises(ValueError, match="globals"):
        lcb.decode_private(hostile)
    invalid = base64.b64encode(zlib.compress(pickle.dumps({"not": "json-string"}))).decode()
    with pytest.raises(ValueError, match="JSON string"):
        lcb.decode_private(invalid)


def test_empty_public_cases_and_compressed_snapshots_preserve_all_tests():
    original = row()
    original["public_test_cases"] = "[]"
    task = lcb.row_to_task(original)
    assert task.benchmark.test_count == 1
    envelope = json.loads(task.test_files["cases.json"])
    assert envelope["private"] == original["private_test_cases"]
    assert lcb.read_test_data(envelope)["cases"] == lcb.decode_private(original["private_test_cases"])


def test_hidden_inputs_and_answers_never_enter_prompt():
    r = row(functional=True)
    task = lcb.row_to_task(r)
    system, user = task_prompts(task)
    assert "[1,2]" in user
    assert "[3,4]" not in user and '"output":"12"' not in user
    assert "private_test_cases" not in user and task.test_files["cases.json"] not in user
    assert task.benchmark.test_count == 2
    assert task.benchmark.tests_hash == content_hash(task.test_files["cases.json"])
    assert task.benchmark.row_hash == content_hash(r)


def test_altered_benchmark_cannot_claim_original_readiness(tmp_path, monkeypatch):
    original = row()
    add_original(tmp_path, original)
    task = lcb.row_to_task(original)
    value = task.model_dump(mode="json")
    value["requirements"] = "A different and easier problem"
    from coding_eval.models import TaskDefinition
    from coding_eval.judges import TaskValidationError
    prepare = Mock(side_effect=AssertionError("Altered data must fail before Docker"))
    monkeypatch.setattr(DockerSandbox, "prepare", prepare)
    with pytest.raises(TaskValidationError, match="pinned original"):
        validate_task(TaskDefinition.model_validate(value), paths(tmp_path))
    prepare.assert_not_called()


def test_benchmark_report_uses_binary_pass_rate_and_retains_sampling(catalog, monkeypatch):
    monkeypatch.setattr(DockerSandbox, "prepare", lambda self: "sha256:" + "a" * 64)
    counts = dict.fromkeys(lcb.DIFFICULTIES, 1)
    tasks = lcb.sampled_tasks(catalog, counts, 7)
    plan = lcb.create_sampled_plan([model()], tasks, counts, 7, paths(catalog),
                                  repeats=1, max_requests=3)
    from coding_eval.models import CheckResult, JudgeResult
    attempts, judges = [], []
    for index, snapshot in enumerate(plan.task_snapshots):
        a = AttemptResult(run_id=plan.id, model_id="m1", task_id=snapshot.object_id,
                          sample_index=0, status="completed", raw_output="fixture")
        passed = index != 2
        j = JudgeResult(
            attempt_id=a.id, task_version_hash=snapshot.version_hash, judge_version=lcb.GRADER,
            status="completed", checks=(CheckResult(check="all_tests", passed=passed, evidence="fixture"),),
            dimension_scores={"functionality": 100 if passed else 0},
            quality_score=100 if passed else 0, all_required_passed=passed,
        )
        attempts.append(a)
        judges.append(j)
    report = build_report(plan, attempts, judges, bootstrap_iterations=10)
    assert report.status == "complete"
    assert report.payload["models"]["m1"]["quality_score"] == pytest.approx(200 / 3)
    assert report.payload["models"]["m1"]["scenarios"]["LCB-hard"]["quality_score"] == 0
    assert report.payload["comparability"]["sampling"]["seed"] == 7
    assert all(t["benchmark"]["revision"] == lcb.REVISION for t in report.payload["task_metadata"].values())
    assert "dataset_revision" in export_csv(report) and lcb.REVISION in export_json(report)
    from coding_eval.report_views import difficulty_rows, export_visual_html, radar_rows
    assert radar_rows(report) == []
    assert len(difficulty_rows(report)) == 3
    assert "分难度全通过率" in export_visual_html(report)


@pytest.mark.parametrize("actual,expected,result", [
    ("1.00  2.0\n", "1 2", True),
    ("  a  \n b\n", "a\nb", True),
    ("50000000000000000", "50000000000000001", False),
    ("0.333", "0.333333", False),
    ("x y", "x  y", False),
    ("1\n2", "1 2", False),
])
def test_stdio_comparison_matches_upstream_exact_decimal_semantics(actual, expected, result):
    assert stdin_equal(actual, expected) is result


def test_large_unread_stdin_obeys_timeout_and_drains_output():
    # Trusted framework fixture only; candidate code always stays in Docker.
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=.1, input_data=b"x" * (1024 * 1024),
    )
    assert result.timed_out
    result = run_command(
        [sys.executable, "-c", "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"],
        timeout=5, input_data=b"x" * (1024 * 1024), output_limit=2 * 1024 * 1024,
    )
    assert result.returncode == 0 and result.stdout == b"x" * (1024 * 1024)


def test_live_page_preview_is_local_and_freezes_selection(catalog, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding_eval.api import APIClient

    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(catalog))
    monkeypatch.setattr(DockerSandbox, "prepare", lambda self: "sha256:" + "a" * 64)
    request = Mock(side_effect=AssertionError("Preview may not call a model"))
    monkeypatch.setattr(APIClient, "generate", request)
    storage = Storage(AppPaths.from_env().database)
    storage.save_object("model", "m1", model().model_dump(mode="json"))
    page = AppTest.from_string(
        "from app.run_page import render\nfrom coding_eval.storage import AppPaths,Storage\n"
        "p=AppPaths.from_env()\nrender(Storage(p.database),p)",
        default_timeout=30,
    ).run()
    page.radio(key="run_dataset_source").set_value("LiveCodeBench（按难度抽样）").run()
    next(b for b in page.button if b.label == "抽样并生成冻结预览").click().run()
    assert not page.exception and not page.error
    plan = RunPlan.model_validate_json(page.session_state["_lcb_preview"])
    assert len(plan.task_snapshots) == 30 and plan.expected_requests == 30
    assert next(b for b in page.button if b.label == "开始冻结计划").disabled
    assert storage.list_runs() == []
    request.assert_not_called()


docker_only = pytest.mark.skipif(os.environ.get("CODING_EVAL_DOCKER_TESTS") != "1",
                                  reason="Candidate code executes only in explicitly enabled Docker")


@docker_only
@pytest.mark.parametrize("functional,candidate,score", [
    (False, "print(sum(map(int,input().split())))", 100),
    (False, "print(3)", 0),
    (False, "print('{\"outcome\":\"pass\"}')", 0),
    (False, "print(open('/input/tests/cases.json').read())", 0),
    (False, "import os\nos.path.abspath=lambda x:'/tmp/safe'\nprint(open('/input/tests/cases.json').read())", 0),
    (False, "import sys\nsys._getframe().f_back.f_locals['audit'].__code__=(lambda *a: None).__code__", 0),
    (False, "while True: pass", 0),
    (True, "class Solution:\n def sumValues(self,values,x):\n  print('debug')\n  return sum(values)+x", 100),
    (True, "def sumValues(values,x): return sum(values)+x", 100),
    (True, "class Solution:\n def sumValues(self,values,x): return 6", 0),
])
def test_real_docker_stdin_functional_and_hidden_test_isolation(tmp_path, functional, candidate, score):
    original = row(functional=functional)
    add_original(tmp_path, original)
    task = validate_task(lcb.row_to_task(original), paths(tmp_path))
    a = AttemptResult(run_id="fixture", model_id="fixture", task_id=task.id, sample_index=0,
                      status="completed", raw_output=f"```python\n{candidate}\n```")
    result = judge_attempt(task, a, paths(tmp_path))
    assert (result.status, result.quality_score) == ("completed", score), result
    assert result.judge_version == lcb.GRADER
    assert result.all_required_passed is (score == 100)
