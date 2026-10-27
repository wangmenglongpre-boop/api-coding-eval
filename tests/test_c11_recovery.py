"""Contract failures are scored in Docker; frozen check sources remain unchanged."""

import hashlib
import json
import os

import pytest

from coding_eval.check_migrations import LEGACY_C11_SHA256, migrate_check_source
from coding_eval.datasets import load_reference_tasks
from coding_eval.judges import judge_attempt, render_answer
from coding_eval.models import AttemptResult
from coding_eval.storage import AppPaths


def test_migration_is_exact_version_only_and_idempotent():
    task = load_reference_tasks()[0]
    original = task.test_files["checks.py"]
    effective, receipt = migrate_check_source(original)
    assert hashlib.sha256(original.encode()).hexdigest() == LEGACY_C11_SHA256
    assert receipt["original_sha256"] == LEGACY_C11_SHA256
    assert "checked_geometry(a.snapshot())" in effective
    assert task.test_files["checks.py"] == original
    assert migrate_check_source(effective) == (effective, None)
    edited = original + "\n# different task version\n"
    assert migrate_check_source(edited) == (edited, None)
    assert all(migrate_check_source(t.test_files["checks.py"])[1] is None
               for t in load_reference_tasks()[1:])


@pytest.mark.skipif(
    os.environ.get("CODING_EVAL_DOCKER_TESTS") != "1",
    reason="Candidate behavior executes only in Docker",
)
@pytest.mark.parametrize("snapshot", [
    None,
    "return {'viewport': self.viewport, 'offset': self.offset}",
    "return {'style': {}, 'move': 50}",
    "return {'style': None, 'move': 50}",
    "return []",
])
def test_snapshot_contract_and_genuine_judge_errors(snapshot, tmp_path):
    task = load_reference_tasks()[0]
    paths = AppPaths(tmp_path, tmp_path / "db.sqlite", tmp_path / "artifacts")
    source = task.reference_files["solution.py"]
    if snapshot is not None:
        old = "        return geometry(self.viewport, self.content, self.offset, self.horizontal)"
        # Change snapshot only, retaining reference geometry/scroll/update.
        position = source.index("    def snapshot(self):")
        source = source[:position] + source[position:].replace(old, "        " + snapshot, 1)
    attempt = AttemptResult(
        id="contract-case", run_id="offline", model_id="fixture", task_id=task.id,
        sample_index=0, attempt_index=0, status="completed",
        raw_output=render_answer(task, {"solution.py": source}),
    )
    result = judge_attempt(task, attempt, paths)
    assert result.status == "completed"
    state = next(c for c in result.checks if c.check == "check_state_integrity")
    assert state.passed is (snapshot is None)
    assert result.all_required_passed is (snapshot is None)
    receipt_path = next(p for p in result.artifact_paths if p.endswith("check_migration.json"))
    receipt = json.loads(open(receipt_path).read())
    assert receipt["original_sha256"] == LEGACY_C11_SHA256


@pytest.mark.skipif(
    os.environ.get("CODING_EVAL_DOCKER_TESTS") != "1",
    reason="Trusted check failure executes only in Docker",
)
def test_unrecognized_check_key_error_remains_judge_error(tmp_path):
    task = load_reference_tasks()[0]
    original = task.test_files["checks.py"]
    task.test_files["checks.py"] = original.replace(
        "def check_state_integrity():",
        "def check_state_integrity():\n    {}['trusted_fixture_missing_key']",
    )
    paths = AppPaths(tmp_path, tmp_path / "db.sqlite", tmp_path / "artifacts")
    attempt = AttemptResult(
        id="trusted-failure", run_id="offline", model_id="fixture", task_id=task.id,
        sample_index=0, attempt_index=0, status="completed",
        raw_output=render_answer(task, task.reference_files),
    )
    assert judge_attempt(task, attempt, paths).status == "judge_error"
