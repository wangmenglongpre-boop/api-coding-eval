"""Bounded plan preparation: validate cheap fields before any Docker work."""

from collections.abc import Callable, Sequence
import re
from typing import Any

from . import judges
from .models import ModelConfig, RunPlan, ScoringConfig, Snapshot, TaskDefinition, content_hash
from .storage import AppPaths


class PlanTaskError(judges.TaskValidationError):
    def __init__(self, task_id: str, cause: judges.TaskValidationError):
        super().__init__(f"{task_id}: {cause}", list(cause.results))
        self.task_id = task_id
        self.results = cause.results
        self.artifact_paths = cause.artifact_paths


def create_plan(
    models: Sequence[ModelConfig], tasks: Sequence[TaskDefinition],
    scoring: ScoringConfig, paths: AppPaths, *,
    validator: Callable[[TaskDefinition, AppPaths], TaskDefinition] | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
    **options: Any,
) -> RunPlan:
    from .runner import _require_frozen_runtime

    scoring = ScoringConfig.model_validate_json(scoring.model_dump_json())
    if any(task.scoring.dimension_weights != scoring.dimension_weights for task in tasks):
        value = scoring.model_dump(mode="json")
        value["template_id"] = "weights-" + content_hash(value["dimension_weights"])[:24]
        scoring = ScoringConfig.model_validate(value)
    frozen_models = tuple(Snapshot.capture(model) for model in models)
    # Draft-only copies validate counts, identities, enabled models, scenarios,
    # budget and option types. They never forge certificates or get persisted.
    draft_snapshots = []
    for task in tasks:
        value = task.model_dump(mode="json")
        value.update(scoring=scoring.model_dump(mode="json"), status="draft",
                     validated_content_hash=None, validation_evidence=[])
        draft_snapshots.append(Snapshot.capture(TaskDefinition.model_validate(value)))
    RunPlan(model_snapshots=frozen_models, task_snapshots=tuple(draft_snapshots),
            scoring=scoring, **{**options, "diagnostic": True})
    revised = []
    for index, original in enumerate(tasks):
        task = TaskDefinition.model_validate_json(original.model_dump_json())
        # RunPlan already binds scenario weights separately; changing them does
        # not change executable checks or require touching ready task snapshots.
        same_dimensions = (
            task.scoring.template_id == scoring.template_id
            and task.scoring.dimension_weights == scoring.dimension_weights
        )
        if (task.scoring != scoring and not (task.ready and same_dimensions)
                or not task.ready and not options.get("diagnostic", False)):
            if progress:
                progress(index, len(tasks), task.id, "容器验证参考解与负解")
            value = task.model_dump(mode="json")
            value.update(scoring=scoring.model_dump(mode="json"), status="draft",
                         validated_content_hash=None, validation_evidence=[])
            task = TaskDefinition.model_validate(value)
            if not options.get("diagnostic", False):
                try:
                    task = (validator or judges.validate_task)(task, paths)
                except judges.TaskValidationError as exc:
                    raise PlanTaskError(task.id, exc) from exc
                # The executable validator may pin an image, not alter content.
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", value["runtime"]["image"]):
                    value["runtime"]["image"] = task.runtime.image
                if task.scoring != scoring or task.validation_hash != TaskDefinition.model_validate(value).validation_hash:
                    raise ValueError(f"{task.id}: Validator changed task content")
        elif progress:
            progress(index, len(tasks), task.id, "复用已验证任务" if task.ready else "冻结诊断任务")
        if not options.get("diagnostic", False):
            _require_frozen_runtime(task)
        revised.append(Snapshot.capture(task))
        if progress:
            progress(index + 1, len(tasks), task.id, "已冻结")
    return RunPlan(model_snapshots=frozen_models, task_snapshots=tuple(revised), scoring=scoring, **options)


def proposal_to_plan(proposal, models, tasks, scoring, limits, paths, **options):
    from .agent import validate_proposal

    validate_proposal(proposal, models, tasks, scoring, limits)
    model_map, task_map = {m.id: m for m in models}, {t.id: t for t in tasks}
    return create_plan(
        [model_map[i] for i in proposal.model_ids], [task_map[i] for i in proposal.task_ids],
        scoring, paths, **proposal.model_dump(exclude={"schema_version", "model_ids", "task_ids", "scoring"}),
        **options,
    )
