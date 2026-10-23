"""Optional control-model planning; never an autonomous evaluation caller."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ConfigDict, Field

from .api import APIClient, sanitize
from .credentials import require_credential, resolve_credential
from .models import (
    AttemptResult, Contract, Identifier, ModelConfig, OutputFormat, ReportVersion,
    RunPlan, ScoringConfig, TaskDefinition, canonical_json, content_hash, new_id, utc_now,
)
from .runner import create_plan, task_prompts
from .reports import explanation_summary
from .storage import AppPaths, Storage


class PlanProposal(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    model_ids: tuple[Identifier, ...]
    task_ids: tuple[Identifier, ...]
    scoring: ScoringConfig
    repeats: int = Field(gt=0)
    concurrency: int = Field(gt=0)
    random_seed: int = 0
    max_retries: int = Field(ge=0, default=0)
    retry_delay_seconds: float = Field(ge=0, default=0)
    max_requests: int = Field(gt=0)
    budget_group: Identifier = "default"
    diagnostic: bool = False


class PlanLimits(Contract):
    max_requests: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    max_repeats: int = Field(gt=0)
    max_retries: int = Field(ge=0, default=0)
    diagnostic: bool = False
    budget_group: Identifier = "default"


def validate_proposal(
    proposal: PlanProposal, models: Sequence[ModelConfig], tasks: Sequence[TaskDefinition],
    scoring: ScoringConfig, limits: PlanLimits,
) -> None:
    """Treat model output as data, not authority over weights or request caps."""
    if (not proposal.model_ids or len(set(proposal.model_ids)) != len(proposal.model_ids)
            or not proposal.task_ids or len(set(proposal.task_ids)) != len(proposal.task_ids)):
        raise ValueError("Plan selections must be nonempty and unique")
    if not set(proposal.model_ids) <= {m.id for m in models if m.enabled}:
        raise ValueError("Plan selected an unavailable model")
    if not set(proposal.task_ids) <= {t.id for t in tasks if t.ready or limits.diagnostic}:
        raise ValueError("Plan selected an unavailable task")
    if proposal.scoring != scoring:
        raise ValueError("Control model cannot change approved scoring")
    if (proposal.max_requests > limits.max_requests or proposal.concurrency > limits.max_concurrency
            or proposal.repeats > limits.max_repeats or proposal.max_retries > limits.max_retries
            or proposal.diagnostic != limits.diagnostic or proposal.budget_group != limits.budget_group):
        raise ValueError("Plan exceeds approved limits")
    if proposal.max_requests < len(proposal.model_ids) * len(proposal.task_ids) * proposal.repeats * (1 + proposal.max_retries):
        raise ValueError("Plan request budget is insufficient")
    scenarios = {t.scenario_id for t in tasks if t.id in proposal.task_ids}
    if scenarios != {w.name for w in scoring.scenario_weights}:
        raise ValueError("Selected tasks do not cover approved scenario weights")


def proposal_to_plan(
    proposal: PlanProposal, models: Sequence[ModelConfig], tasks: Sequence[TaskDefinition],
    scoring: ScoringConfig, limits: PlanLimits, paths: AppPaths, **kwargs: Any,
) -> RunPlan:
    validate_proposal(proposal, models, tasks, scoring, limits)
    model_map, task_map = {m.id: m for m in models}, {t.id: t for t in tasks}
    return create_plan(
        [model_map[i] for i in proposal.model_ids], [task_map[i] for i in proposal.task_ids],
        scoring, paths, **proposal.model_dump(exclude={"schema_version", "model_ids", "task_ids", "scoring"}),
        **kwargs,
    )


def task_catalog(tasks: Sequence[TaskDefinition]) -> list[dict[str, Any]]:
    # Deliberate allowlist: no source files, hidden rubric, references or evidence.
    return [{"id": t.id, "scenario_id": t.scenario_id, "title": t.title, "status": t.status} for t in tasks]


class PlanningAgent:
    def __init__(self, storage: Storage, client: APIClient | None = None) -> None:
        self.storage, self.client = storage, client or APIClient()

    async def _call(
        self, config: ModelConfig, task: TaskDefinition, *, api_key: str | None,
        action: str, context_hash: str, report_id: str | None = None,
    ) -> AttemptResult:
        call_id = "control-" + new_id()
        secret = resolve_credential(config, api_key)
        require_credential(config, secret)
        self.storage.save_object("control_intent", call_id, {
            "call_id": call_id, "model_id": config.id, "model_hash": config.version_hash,
            "action": action, "context_hash": context_hash, "started_at": utc_now().isoformat(),
            "report_id": report_id,
        })
        metadata = dict(run_id=call_id, model_id=config.id, task_id=task.id,
                        sample_index=0, attempt_index=0, purpose="control")
        cancelled = None
        try:
            result = await self.client.generate(
                config, *task_prompts(task), api_key=secret, run_id=call_id,
                task_id=task.id, sample_index=0, attempt_index=0, purpose="control",
            )
            if any(getattr(result, name) != value for name, value in metadata.items()):
                raise ValueError("Control result identity mismatch")
        except asyncio.CancelledError as exc:
            cancelled = exc
            partial = getattr(exc, "result", None)
            if isinstance(partial, AttemptResult) and all(
                getattr(partial, name) == value for name, value in metadata.items()
            ):
                result = partial.model_copy(update={
                    "status": "uncertain", "error_code": "cancelled_in_flight",
                })
            else:
                result = AttemptResult(**metadata, status="uncertain", error_code="cancelled_in_flight")
        except Exception:
            result = AttemptResult(**metadata, status="uncertain", error_code="control_exception")
        result = AttemptResult.model_validate(sanitize(result.model_dump(mode="json"), secret))
        self.storage.save_object("control", call_id, {
            "action": action, "context_hash": context_hash, "model_hash": config.version_hash,
            "report_id": report_id,
            "result": result.model_dump(mode="json"),
        })
        if cancelled is not None:
            raise cancelled
        return result

    async def propose(
        self, intent: str, control_model: ModelConfig, models: Sequence[ModelConfig],
        tasks: Sequence[TaskDefinition], scoring: ScoringConfig, limits: PlanLimits, *,
        api_key: str | None = None,
    ) -> PlanProposal:
        if not intent.strip():
            raise ValueError("An evaluation intent is required")
        context = {
            "intent": intent,
            "models": [{"id": m.id, "display_name": m.display_name} for m in models if m.enabled],
            "tasks": task_catalog(tasks), "scoring": scoring.model_dump(mode="json"),
            "limits": limits.model_dump(mode="json"),
        }
        task = TaskDefinition(
            id="planning", scenario_id="control", title="Evaluation planning", description="",
            system_prompt="Convert the evaluation intent to the declared JSON plan. No tools or evaluation calls.",
            requirements=canonical_json(context),
            output_format=OutputFormat(
                kind="files", allowed_paths=("plan.json",),
                instructions=(
                    "Return exactly one JSON object, without Markdown. Select only supplied IDs. "
                    "Keep scoring, diagnostic and budget_group unchanged; respect all limits. Schema:\n"
                    + canonical_json(PlanProposal.model_json_schema())
                ),
            ),
        )
        result = await self._call(
            control_model, task, api_key=api_key, action="plan", context_hash=content_hash(context),
        )
        if result.status != "completed":
            raise ValueError("Control model unavailable; use the form")
        try:
            proposal = PlanProposal.model_validate_json(result.raw_output)
            validate_proposal(proposal, models, tasks, scoring, limits)
        except ValueError:
            raise ValueError("Control plan failed schema or authorization validation") from None
        return proposal

    async def explain(
        self, report: ReportVersion, control_model: ModelConfig, *, api_key: str | None = None,
    ) -> str:
        """Read only a persisted report; arbitrary report text never enters input."""
        persisted = next((r for r in self.storage.list_reports(report.run_id) if r.id == report.id), None)
        if persisted != report:
            raise ValueError("Explanation requires an unchanged persisted report")
        plan = self.storage.get_run(report.run_id)
        if plan is None or plan.version_hash != report.source_plan_hash:
            raise ValueError("Report does not match its frozen plan")

        summary = {
            "run_id": report.run_id, "report_id": report.id, "status": report.status,
            "task_ids": [s.object_id for s in plan.task_snapshots],
            "scoring": report.scoring.model_dump(mode="json"), "metrics": explanation_summary(report, plan),
        }
        task = TaskDefinition(
            id="explanation", scenario_id="control", title="Report explanation", description="",
            system_prompt="Explain only the supplied frozen report. Do not change scores or claim unreported tests passed.",
            requirements=canonical_json(summary),
            output_format=OutputFormat(
                kind="files", allowed_paths=("explanation.txt",),
                instructions="Cite the run ID and relevant task IDs. Explain missing evidence and small-sample uncertainty.",
            ),
        )
        result = await self._call(
            control_model, task, api_key=api_key, action="explain", context_hash=content_hash(summary),
            report_id=report.id,
        )
        if result.status != "completed":
            raise ValueError("Control explanation unavailable; deterministic report is unchanged")
        return result.raw_output
