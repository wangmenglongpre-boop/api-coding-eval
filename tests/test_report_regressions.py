"""Run original report acceptance assertions under the frozen-runtime contract."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

from coding_eval import judges
from coding_eval.container_check import DockerSandbox
from coding_eval.models import Snapshot
from coding_eval.storage import AppPaths, Storage


def test_cancelled_retry_original_acceptance_with_frozen_runtime(tmp_path, monkeypatch):
    path = (Path(__file__).resolve().parents[1] / ".trae/specs/"
            "build-api-coding-eval-app/test_acceptance_tmp.py")
    spec = importlib.util.spec_from_file_location("report_acceptance", path)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    original_make_plan = acceptance.make_plan
    image = "sha256:" + "a" * 64

    def frozen_plan(**kwargs):
        plan = original_make_plan(**kwargs)
        snapshots = []
        for snapshot in plan.task_snapshots:
            task = snapshot.restore()
            task = task.model_copy(update={
                "runtime": task.runtime.model_copy(update={"image": image}),
            })
            task = task.model_copy(update={"validated_content_hash": task.validation_hash})
            snapshots.append(Snapshot.capture(task))
        return plan.model_copy(update={"task_snapshots": tuple(snapshots)})

    # Only the environment is simulated; the acceptance body/runner/report stay real.
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.return_value = image
    sandbox.execute.side_effect = AssertionError("Report regressions cannot execute task code")
    monkeypatch.setattr(acceptance, "make_plan", frozen_plan)
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    monkeypatch.setenv("CODING_EVAL_DATA_DIR", str(tmp_path))
    paths = AppPaths.from_env()
    storage = Storage(paths.database)
    acceptance.test_cancelled_retry_is_not_formally_ranked((storage, paths))
    sandbox.prepare.assert_called_once_with()
    sandbox.execute.assert_not_called()
    report = storage.list_reports(storage.list_runs()[0]["run_id"])[0]
    assert report.payload["run_status"] == "cancelled"
    assert report.payload["samples"][0]["status"] == "pending_retry"
    assert report.payload["ranking"] == []
