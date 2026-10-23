"""Frozen batch execution with durable request intents and explicit recovery."""

from __future__ import annotations

import asyncio
import fcntl
import random
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from . import judges
from .api import APIClient, build_request, sanitize
from .container_check import SandboxError
from .credentials import require_credential, resolve_credential
from .models import (
    AttemptResult, JudgeResult, ModelConfig, RunPlan, RunStatus, ScoringConfig,
    Snapshot, TaskDefinition, content_hash, utc_now,
)
from .storage import AppPaths, Storage


def latest_models(storage: Storage) -> list[ModelConfig]:
    latest = {}
    for row in storage.list_objects("model"):
        latest[row["object_id"]] = ModelConfig.model_validate(row["payload"])
    return sorted(latest.values(), key=lambda model: model.id)


def task_prompts(task: TaskDefinition) -> tuple[str, str]:
    """Only declared public inputs, shared by evaluation and control requests."""
    parts = [task.requirements]
    for path, source in sorted(task.public_files.items()):
        parts.append(f"File: {path}\n{source}")
    parts.append(task.output_format.instructions)
    return task.system_prompt, "\n\n".join(parts)


def _require_frozen_runtime(task: TaskDefinition) -> None:
    if not task.ready:
        raise ValueError("Formal plans require ready tasks")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", task.runtime.image):
        raise ValueError("Formal tasks require a validated runtime.image sha256 ID; revalidate the task")


def create_plan(
    models: Sequence[ModelConfig], tasks: Sequence[TaskDefinition],
    scoring: ScoringConfig, paths: AppPaths, *,
    validator: Callable[[TaskDefinition, AppPaths], TaskDefinition] | None = None,
    **options: Any,
) -> RunPlan:
    """Compatibility entry point for verified, bounded plan preparation."""
    from .plan_preview import create_plan as prepare_plan

    return prepare_plan(models, tasks, scoring, paths, validator=validator, **options)


def sample_schedule(plan: RunPlan) -> list[tuple[str, str, int]]:
    samples = [
        (model.object_id, task.object_id, sample)
        for sample in range(plan.repeats)
        for task in plan.task_snapshots for model in plan.model_snapshots
    ]
    random.Random(plan.random_seed).shuffle(samples)
    return samples


def intent_id(run_id: str, model_id: str, task_id: str, sample: int, attempt: int) -> str:
    return content_hash([run_id, model_id, task_id, sample, attempt, "evaluation"])


class RunBusyError(RuntimeError):
    pass


class BatchRunner:
    """One owner per run across threads/processes; no credentials on the object.

    request_intent is append-only and consumes budget even without a response.
    Judges are stored under kind='judge', object_id=attempt.id for report readers.
    """

    def __init__(
        self, storage: Storage, paths: AppPaths, *,
        client: APIClient | None = None,
        judge: Callable[[TaskDefinition, AttemptResult, AppPaths], JudgeResult] | None = None,
    ) -> None:
        self.storage, self.paths = storage, paths
        self.client = client or APIClient()
        self.judge = judge or judges.judge_attempt
        self._cancel = threading.Event()

    @contextmanager
    def _ownership(self, run_id: str):
        folder = self.storage.database.resolve().parent / "run-locks"
        folder.mkdir(parents=True, exist_ok=True)
        name = content_hash([str(self.storage.database.resolve()), run_id])
        with (folder / (name + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunBusyError("Run already has an active worker") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def cancel(self, run_id: str) -> None:
        if self.storage.get_run(run_id) is None:
            raise KeyError(run_id)
        # Durable cancellation also reaches workers in another registry/process.
        self.storage.save_object("run_cancel", run_id, {"requested_at": utc_now().isoformat()})
        self._cancel.set()

    def _preflight(self, plan: RunPlan, keys: dict[str, str]) -> None:
        for snapshot in plan.model_snapshots:
            model = ModelConfig.model_validate_json(snapshot.payload_json)
            secret = resolve_credential(model, keys.get(model.id))
            require_credential(model, secret)
            if secret is not None:
                keys[model.id] = secret
            for task in plan.task_snapshots:
                build_request(model, *task_prompts(TaskDefinition.model_validate_json(task.payload_json)))
        if not plan.diagnostic:
            tasks = [TaskDefinition.model_validate_json(s.payload_json) for s in plan.task_snapshots]
            for task in tasks:
                _require_frozen_runtime(task)
                try:
                    judges._task_contract(task)
                except SandboxError as exc:
                    raise ValueError("Formal task runtime contract is invalid") from exc
            for task in tasks:
                try:
                    image_id = judges.DockerSandbox(task.runtime).prepare()
                except SandboxError as exc:
                    raise ValueError("Formal sandbox runtime is invalid") from exc
                if image_id != task.runtime.image:
                    raise ValueError("Prepared Docker image differs from the frozen runtime.image sha256 ID")

    def _intents(self, run_id: str) -> dict[str, dict[str, Any]]:
        return {
            row["object_id"]: row["payload"]
            for row in self.storage.list_objects("request_intent")
            if row["payload"]["run_id"] == run_id
        }

    async def _judge(self, task: TaskDefinition, attempt: AttemptResult) -> JudgeResult:
        try:
            result = await asyncio.to_thread(self.judge, task, attempt, self.paths)
            result = JudgeResult.model_validate_json(result.model_dump_json())
            if result.attempt_id != attempt.id or result.task_version_hash != task.version_hash:
                raise ValueError("Judge identity mismatch")
        except Exception:
            result = JudgeResult(
                attempt_id=attempt.id, task_version_hash=task.version_hash,
                judge_version="runner-error-v1", status="judge_error",
                error_message="Judge failed; inspect the judge environment and retry judging.",
            )
        self.storage.save_object("judge", attempt.id, result.model_dump(mode="json"))
        return result

    async def run(self, run_id: str, *, api_keys: Mapping[str, str] | None = None) -> RunStatus:
        """Start or explicitly resume. Never replay a possibly billed request."""
        keys = dict(api_keys or {})
        try:
            with self._ownership(run_id):
                plan = self.storage.get_run(run_id)
                if plan is None:
                    raise KeyError(run_id)
                status = self.storage.get_run_status(run_id)
                if status == RunStatus.COMPLETED:
                    return status
                self._cancel.clear()
                cancel_baseline = self.storage.get_object("run_cancel", run_id)
                if status == RunStatus.QUEUED and cancel_baseline is not None:
                    self.storage.transition_run(run_id, status, RunStatus.CANCELLED)
                    return RunStatus.CANCELLED
                self._preflight(plan, keys)
                # A running status without its OS lock is a crashed owner.
                if status == RunStatus.RUNNING:
                    self.storage.transition_run(run_id, status, RunStatus.FAILED)
                    status = RunStatus.FAILED
                if status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                    self.storage.transition_run(run_id, status, RunStatus.QUEUED)
                self.storage.transition_run(run_id, RunStatus.QUEUED, RunStatus.RUNNING)
                self.storage.save_object("model_dispatch_epoch", run_id, {
                    "started_at": utc_now().isoformat(),
                })
                execution = asyncio.create_task(self._execute(plan, keys, cancel_baseline))
                try:
                    # Cancelling the caller stops dispatch but lets persistence
                    # and already-running bounded judges drain before unlocking.
                    await asyncio.shield(execution)
                    result = RunStatus.CANCELLED if self._cancel.is_set() else self._completion(plan)
                except asyncio.CancelledError:
                    self._cancel.set()
                    await asyncio.gather(execution, return_exceptions=True)
                    result = RunStatus.CANCELLED
                except Exception:
                    self.storage.save_object("run_error", run_id, {
                        "time": utc_now().isoformat(), "message": "Worker failed; resume to reconcile saved intents.",
                    })
                    result = RunStatus.FAILED
                self.storage.transition_run(run_id, RunStatus.RUNNING, result)
                return result
        finally:
            keys.clear()

    def _completion(self, plan: RunPlan) -> RunStatus:
        attempts = self.storage.list_attempts(plan.id)
        observed = {(a.model_id, a.task_id, a.sample_index) for a in attempts}
        if observed != set(sample_schedule(plan)):
            return RunStatus.FAILED
        for attempt in attempts:
            judge = self.storage.get_object("judge", attempt.id)
            if judge is None or judge["status"] != "completed":
                return RunStatus.FAILED
        return RunStatus.COMPLETED

    async def _execute(self, plan: RunPlan, keys: Mapping[str, str], cancel_baseline: Any) -> None:
        tasks = {s.object_id: TaskDefinition.model_validate_json(s.payload_json) for s in plan.task_snapshots}
        models = {s.object_id: ModelConfig.model_validate_json(s.payload_json) for s in plan.model_snapshots}
        attempts = self.storage.list_attempts(plan.id)
        saved = {intent_id(a.run_id, a.model_id, a.task_id, a.sample_index, a.attempt_index): a for a in attempts}
        intents = self._intents(plan.id)
        for identifier, intent in intents.items():
            if identifier not in saved:
                attempt = AttemptResult(
                    id=identifier, run_id=plan.id, model_id=intent["model_id"], task_id=intent["task_id"],
                    sample_index=intent["sample_index"], attempt_index=intent["attempt_index"],
                    status="uncertain", error_code="response_lost",
                    error_message="Request intent survived without a result; never automatically resent.",
                    queue_seconds=intent["queue_seconds"],
                    logical_task_seconds=intent["logical_task_seconds"],
                )
                self.storage.save_attempt(attempt)
                attempts.append(attempt)
                saved[identifier] = attempt
        # Union also counts historical attempts written by older runners.
        used = len(set(intents) | set(saved))
        histories: dict[tuple[str, str, int], list[AttemptResult]] = {}
        for attempt in attempts:
            histories.setdefault((attempt.model_id, attempt.task_id, attempt.sample_index), []).append(attempt)
        for history in histories.values():
            history.sort(key=lambda attempt: attempt.attempt_index)
        queue = asyncio.Queue()
        queued_at = time.monotonic()
        for sample in sample_schedule(plan):
            queue.put_nowait(sample)
        in_flight: set[asyncio.Task] = set()
        # Reset only on explicit run()/resume. Historical failures remain scored.
        blocked_models: set[str] = set()

        async def watch_cancel() -> None:
            while True:
                if self.storage.get_object("run_cancel", plan.id) != cancel_baseline:
                    self._cancel.set()
                if self._cancel.is_set():
                    for request in tuple(in_flight):
                        request.cancel()
                    return
                await asyncio.sleep(0.03)

        async def sample_work(sample: tuple[str, str, int]) -> None:
            nonlocal used
            model_id, task_id, sample_index = sample
            task, model = tasks[task_id], models[model_id]
            history = histories.get(sample, [])
            for previous in history:
                existing = self.storage.get_object("judge", previous.id)
                if existing is None or existing["status"] != "completed":
                    await self._judge(task, previous)
            previous = history[-1] if history else None
            if previous and (previous.status != "failed" or previous.attempt_index >= plan.max_retries):
                return
            index = previous.attempt_index + 1 if previous else 0
            logical = (previous.logical_task_seconds or 0) if previous else 0
            queue_seconds = max(0, time.monotonic() - queued_at)
            while index <= plan.max_retries and not self._cancel.is_set():
                if index:
                    started = time.monotonic()
                    deadline = started + plan.retry_delay_seconds
                    while time.monotonic() < deadline and not self._cancel.is_set():
                        await asyncio.sleep(min(0.03, max(0, deadline - time.monotonic())))
                    logical += time.monotonic() - started
                if self.storage.get_object("run_cancel", plan.id) != cancel_baseline:
                    self._cancel.set()
                if self._cancel.is_set() or used >= plan.max_requests or model_id in blocked_models:
                    return
                identifier = intent_id(plan.id, model_id, task_id, sample_index, index)
                wait = queue_seconds
                queue_seconds = 0.0
                # No await between checking the budget, reserving it, and saving.
                self.storage.save_object("request_intent", identifier, {
                    "run_id": plan.id, "model_id": model_id, "task_id": task_id,
                    "sample_index": sample_index, "attempt_index": index,
                    "started_at": utc_now().isoformat(), "queue_seconds": wait,
                    "logical_task_seconds": logical, "plan_hash": plan.version_hash,
                })
                used += 1
                started = time.monotonic()
                request = asyncio.create_task(self.client.generate(
                    model, *task_prompts(task), api_key=keys.get(model_id),
                    run_id=plan.id, task_id=task_id, sample_index=sample_index,
                    attempt_index=index, purpose="evaluation",
                ))
                in_flight.add(request)
                metadata = dict(id=identifier, run_id=plan.id, model_id=model_id, task_id=task_id,
                                sample_index=sample_index, attempt_index=index, purpose="evaluation")
                try:
                    result = await request
                    if any(getattr(result, key) != value for key, value in metadata.items() if key != "id"):
                        raise ValueError("API result identity mismatch")
                except asyncio.CancelledError as exc:
                    partial = getattr(exc, "result", None)
                    if isinstance(partial, AttemptResult) and all(
                        getattr(partial, key) == value for key, value in metadata.items() if key != "id"
                    ):
                        result = partial.model_copy(update={
                            "status": "uncertain", "error_code": "cancelled_in_flight",
                            "metrics": partial.metrics.model_copy(update={"stream_complete": False}),
                        })
                    else:
                        result = AttemptResult(**metadata, status="uncertain", error_code="cancelled_in_flight",
                                               error_message="Cancelled locally; remote billing is unknown.")
                except Exception:
                    result = AttemptResult(**metadata, status="uncertain", error_code="client_exception",
                                           error_message="Client failed without a reliable response.")
                finally:
                    in_flight.discard(request)
                logical += time.monotonic() - started
                payload = result.model_dump(mode="json")
                payload.update(metadata, queue_seconds=wait, logical_task_seconds=logical)
                secret = keys.get(model_id)
                result = AttemptResult.model_validate(sanitize(payload, secret))
                self.storage.save_attempt(result)
                if result.error_code in {"http_401", "http_403"}:
                    blocked_models.add(model_id)
                    self.storage.save_object("model_dispatch_block", result.id, {
                        "run_id": plan.id, "model_id": model_id, "attempt_id": result.id,
                        "error_code": result.error_code, "blocked_at": utc_now().isoformat(),
                        "epoch": self.storage.get_object("model_dispatch_epoch", plan.id),
                    })
                await self._judge(task, result)
                if result.status != "failed":
                    return
                index += 1

        async def worker() -> None:
            while not queue.empty() and not self._cancel.is_set():
                await sample_work(queue.get_nowait())

        watcher = asyncio.create_task(watch_cancel())
        workers = [asyncio.create_task(worker()) for _ in range(min(plan.concurrency, queue.qsize()))]
        try:
            await asyncio.gather(*workers)
        finally:
            # Stop dispatch before draining; a persistence error must not leave
            # detached workers making requests after the run is marked failed.
            if any(w.done() and not w.cancelled() and w.exception() for w in workers):
                self._cancel.set()
            for request in tuple(in_flight):
                request.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def status(self, run_id: str) -> dict[str, Any]:
        """Report-facing progress; timing stays separate from API metrics."""
        plan = self.storage.get_run(run_id)
        if plan is None:
            raise KeyError(run_id)
        attempts = self.storage.list_attempts(run_id)
        observed = {(a.model_id, a.task_id, a.sample_index) for a in attempts}
        judges_by_attempt = {a.id: self.storage.get_object("judge", a.id) for a in attempts}
        return {
            "run_id": run_id, "status": self.storage.get_run_status(run_id).value,
            "plan_hash": plan.version_hash, "expected_samples": plan.expected_requests,
            "observed_samples": len(observed), "remaining_samples": plan.expected_requests - len(observed),
            "attempts": len(attempts), "reserved_requests": len(self._intents(run_id)),
            "max_requests": plan.max_requests,
            "uncertain": sum(a.status == "uncertain" for a in attempts),
            "judge_pending_or_invalid": sum(not j or j["status"] != "completed" for j in judges_by_attempt.values()),
            "diagnostic": plan.diagnostic,
            "blocked_models": sorted({
                row["payload"]["model_id"]
                for row in self.storage.list_objects("model_dispatch_block")
                if row["payload"]["run_id"] == run_id
                and row["payload"].get("epoch") == self.storage.get_object("model_dispatch_epoch", run_id)
            }),
        }


class WorkerRegistry:
    """Cache this resource, not a function whose arguments contain credentials."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[tuple[str, str], tuple[threading.Thread, BatchRunner]] = {}

    def start(
        self, storage: Storage, paths: AppPaths, run_id: str, *,
        api_keys: Mapping[str, str] | None = None, runner: BatchRunner | None = None,
    ) -> bool:
        key = (str(storage.database.resolve()), run_id)
        with self._lock:
            current = self._workers.get(key)
            if current and current[0].is_alive():
                return False
            engine = runner or BatchRunner(storage, paths)
            keys = dict(api_keys or {})

            def work() -> None:
                try:
                    asyncio.run(engine.run(run_id, api_keys=keys))
                except RunBusyError:
                    pass
                except Exception:
                    storage.save_object("run_error", run_id, {
                        "time": utc_now().isoformat(),
                        "message": "Preflight or worker failed; check model configuration, Docker availability, "
                                   "task contracts and validated runtime.image sha256 IDs.",
                    })
                finally:
                    keys.clear()

            thread = threading.Thread(target=work, name="eval-" + run_id, daemon=True)
            self._workers[key] = (thread, engine)
            thread.start()
            return True

    def active(self, storage: Storage, run_id: str) -> bool:
        with self._lock:
            worker = self._workers.get((str(storage.database.resolve()), run_id))
            return bool(worker and worker[0].is_alive())

    def cancel(self, storage: Storage, paths: AppPaths, run_id: str) -> None:
        with self._lock:
            worker = self._workers.get((str(storage.database.resolve()), run_id))
            (worker[1] if worker else BatchRunner(storage, paths)).cancel(run_id)

    def join(self, storage: Storage, run_id: str, timeout: float | None = None) -> None:
        with self._lock:
            worker = self._workers.get((str(storage.database.resolve()), run_id))
        if worker:
            worker[0].join(timeout)
