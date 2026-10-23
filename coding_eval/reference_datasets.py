"""Independent reference catalog, importable beside cached pre-upgrade datasets."""

from __future__ import annotations

import json
from pathlib import Path

from .datasets import _unique_object, as_draft
from .models import TaskDefinition

REFERENCE_PATH = Path(__file__).resolve().parent.parent / "datasets" / "reference_tasks.json"


def load_reference_tasks(path: Path | None = None) -> list[TaskDefinition]:
    """Read fresh inert drafts without requiring new exports in datasets.py."""
    values = json.loads(
        (REFERENCE_PATH if path is None else path).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
    )
    tasks = [as_draft(TaskDefinition.model_validate(value)) for value in values]
    if len(tasks) != 5 or {task.scenario_id for task in tasks} != {
        f"C{i:02}" for i in range(11, 16)
    } or len({task.id for task in tasks}) != 5:
        raise ValueError("参考改编题库必须包含 C11-C15 各一个任务")
    return tasks
