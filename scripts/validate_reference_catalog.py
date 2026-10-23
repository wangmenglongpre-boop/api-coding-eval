"""Validate and publish five adaptations without invoking any model API."""

import json
from pathlib import Path

from coding_eval.datasets import export_task, list_tasks, load_reference_tasks, save_task
from coding_eval.judges import validate_task
from coding_eval.storage import AppPaths, Storage


NEGATIVE_CHECKS = {
    "C11": {"wrong_move_denominator": "check_geometry", "no_resize_clamp": "check_geometry"},
    "C12": {"truncate_subseconds": "check_precision", "windows_uniform": "check_precision"},
    "C13": {"int32_k_wrap": "check_large_values", "int32_histogram_wrap": "check_large_values"},
    "C14": {"null_alias_not_filtered": "check_grouping", "truthiness_alias_filter": "check_grouping"},
    "C15": {"minor_version_only": "check_version_policy", "wrong_warning_stacklevel": "check_warning_contract"},
}


def verify_receipts(task):
    results = [
        json.loads(Path(path).read_text(encoding="utf-8"))
        for path in task.validation_evidence
    ]
    assert len(results) == 3 and all(r["status"] == "completed" for r in results)
    assert all(check["passed"] for check in results[0]["checks"])
    for name, result in zip(task.negative_files, results[1:]):
        checks = {item["check"]: item["passed"] for item in result["checks"]}
        assert checks[NEGATIVE_CHECKS[task.id][name]] is False, (task.id, name, checks)
        assert "candidate_syntax" not in result["gate_failures"]
    return results


def main():
    paths = AppPaths.from_env()
    paths.ensure()
    storage = Storage(paths.database)
    before = {t.id: t.version_hash for t in list_tasks(storage)}
    validated = []
    receipts = {}
    for task in load_reference_tasks():
        print(f"Validating {task.id} ...", flush=True)
        ready = validate_task(task, paths)
        receipts[task.id] = verify_receipts(ready)
        validated.append(ready)
        print(f"{task.id}: reference 5/5, targeted negatives 2/2 rejected", flush=True)
    output = paths.root / "reference_tasks"
    output.mkdir(parents=True, exist_ok=True)
    for task in validated:
        # Preserve a user-edited existing head; adding missing tasks is idempotent.
        if task.id not in before:
            save_task(storage, task)
        (output / f"{task.id}.json").write_text(task.model_dump_json(indent=2), encoding="utf-8")
        (output / f"{task.id}.zip").write_bytes(export_task(task))
    (output / "validation-evidence.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    after = {t.id: t for t in list_tasks(storage)}
    assert all(after[key].version_hash == version for key, version in before.items())
    assert all(task.id in after for task in validated)
    print(f"Published {len(validated)} validated exports to {output}", flush=True)
    print("Dataset heads: " + ", ".join(f"{t.id}={t.status}" for t in after.values()), flush=True)


if __name__ == "__main__":
    main()
