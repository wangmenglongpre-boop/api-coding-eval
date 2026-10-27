"""Strict answer extraction, Docker-only judging and task validation."""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .check_migrations import migrate_check_source
from .container_check import (
    JUDGE_VERSION, DockerSandbox, DockerUnavailable, SandboxError,
)
from .models import (
    AttemptResult, CheckResult, Dimension, JudgeResult, TaskDefinition,
    validate_relative_path,
)
from .storage import AppPaths

MAX_ANSWER_BYTES = 1024 * 1024
MAX_TASK_BYTES = 4 * 1024 * 1024
_FENCE = re.compile(r"\A[ \t\r\n]*```python\r?\n(.*?)\r?\n```[ \t\r\n]*\Z", re.DOTALL)
_PATCH = re.compile(
    r"(?:(?P<path>[^\n]+)\n)?<<<<<<< SEARCH\n"
    r"(?P<search>.*?)\n=======\n(?P<replacement>.*?)\n>>>>>>> REPLACE(?:\n|\Z)",
    re.DOTALL,
)


class AnswerFormatError(ValueError):
    """The original response cannot be applied without repair."""


class TaskValidationError(ValueError):
    def __init__(self, message: str, results: list[JudgeResult]):
        super().__init__(message)
        self.results = tuple(results)
        self.status = next(
            (r.status for r in results if r.status != "completed"), "judge_error",
        )
        self.artifact_paths = tuple(p for result in results for p in result.artifact_paths)


def _task_contract(task: TaskDefinition) -> None:
    allowed = task.output_format.allowed_paths
    if len(allowed) != 1 or allowed[0] not in {"solution.py", "test_solution.py"}:
        raise SandboxError("Exactly one allowed path, solution.py or test_solution.py, is required")
    if task.output_format.kind == "tests" and allowed != ("test_solution.py",):
        raise SandboxError("Tests answers must target test_solution.py")
    if set(task.test_files) != {"checks.py"} or not task.rubric:
        raise SandboxError("Task needs checks.py and a nonempty rubric")
    if {r.dimension.value for r in task.rubric} != {w.name for w in task.scoring.dimension_weights}:
        raise SandboxError("Rubric dimensions do not match scoring weights")
    if task.output_format.kind == "search_replace" and allowed[0] not in task.public_files:
        raise SandboxError("SEARCH/REPLACE requires an existing public file")
    files = {**task.public_files, **task.test_files}
    if len(files) > 128 or sum(len(v.encode("utf-8")) for v in files.values()) > MAX_TASK_BYTES:
        raise SandboxError("Task file budget exceeded")
    for path in task.public_files:
        validate_relative_path(path)
        if path == "checks.py":
            raise SandboxError("Public files cannot shadow checks.py")
        if any(other.startswith(path + "/") for other in task.public_files):
            raise SandboxError("Conflicting public file/directory paths")


def extract_answer(task: TaskDefinition, answer: str) -> dict[str, str]:
    """Text-only parsing; never syntax-check or execute generated code here."""
    if len(answer.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise AnswerFormatError("Answer exceeds the extraction size limit")
    match = _FENCE.fullmatch(answer)
    if not match or re.search(r"(?m)^[ \t]*```", match.group(1)):
        raise AnswerFormatError("Expected exactly one python fenced block and no prose")
    body = match.group(1)
    allowed = task.output_format.allowed_paths
    if len(allowed) != 1:
        raise AnswerFormatError("The answer protocol requires a single allowed path")
    target = allowed[0]
    if task.output_format.kind in {"files", "tests"}:
        return {target: body}

    # Locate every SEARCH against the unchanged original; reject overlaps and
    # ambiguous matches rather than applying fuzzy or order-dependent repairs.
    originals = task.public_files
    patches: dict[str, list[tuple[int, int, str]]] = {}
    position = 0
    while position < len(body):
        patch = _PATCH.match(body, position)
        if patch is None:
            raise AnswerFormatError("Malformed SEARCH/REPLACE block")
        path = patch.group("path") or target
        if path not in allowed or path not in originals:
            raise AnswerFormatError("Unknown or unauthorized patch path")
        search, replacement = patch.group("search", "replacement")
        if not search:
            raise AnswerFormatError("SEARCH must not be empty")
        original = originals[path]
        start = original.find(search)
        if start < 0 or original.find(search, start + 1) >= 0:
            raise AnswerFormatError("SEARCH must match the original exactly once")
        end = start + len(search)
        edits = patches.setdefault(path, [])
        if any(start < old_end and old_start < end for old_start, old_end, _ in edits):
            raise AnswerFormatError("Overlapping SEARCH blocks are not allowed")
        edits.append((start, end, replacement))
        position = patch.end()
        while position < len(body) and body[position] == "\n":
            position += 1
    if not patches:
        raise AnswerFormatError("At least one SEARCH/REPLACE block is required")
    result = {}
    for path, edits in patches.items():
        source = originals[path]
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        result[path] = source
    return result


def render_answer(task: TaskDefinition, files: Mapping[str, str]) -> str:
    """Wrap a reference/negative file map in the exact declared answer protocol."""
    allowed = task.output_format.allowed_paths
    if len(allowed) != 1 or set(files) != set(allowed):
        raise AnswerFormatError("Reference/negative files must exactly match allowed_paths")
    path = allowed[0]
    body = files[path]
    if task.output_format.kind == "search_replace":
        if path not in task.public_files or not task.public_files[path]:
            raise AnswerFormatError("Cannot render a patch without a nonempty original")
        body = (
            f"{path}\n<<<<<<< SEARCH\n{task.public_files[path]}\n"
            f"=======\n{body}\n>>>>>>> REPLACE"
        )
    rendered = f"```python\n{body}\n```"
    if extract_answer(task, rendered) != dict(files):
        raise AnswerFormatError("File content cannot be represented by the answer protocol")
    return rendered


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _stage(directory: Path, task: TaskDefinition, files: dict[str, str]) -> None:
    directory.chmod(0o755)
    for folder, sources in (
        ("work", {**task.public_files, **files}), ("tests", task.test_files),
    ):
        for relative, source in sources.items():
            validate_relative_path(relative)
            if folder == "tests" and relative == "checks.py":
                source, _ = migrate_check_source(source)
            path = directory / folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            path.chmod(0o644)
    # Only the framework runner is read on the host; task files remain data.
    runner = Path(__file__).with_name("container_check.py").read_text(encoding="utf-8")
    (directory / "runner.py").write_text(runner, encoding="utf-8")
    (directory / "runner.py").chmod(0o644)
    _write_json(directory / "job.json", {
        "checks": [item.check for item in task.rubric], "candidate_paths": list(files),
    })
    (directory / "job.json").chmod(0o644)
    for path in directory.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)


def _scores(task: TaskDefinition, checks: list[CheckResult]) -> dict[Dimension, float]:
    passed = {check.check: check.passed for check in checks}
    scores = {}
    for weight in task.scoring.dimension_weights:
        items = [r for r in task.rubric if r.dimension.value == weight.name]
        scale = max(r.points for r in items)
        total = sum(r.points / scale for r in items)
        earned = sum(r.points / scale for r in items if passed.get(r.check, False))
        scores[Dimension(weight.name)] = min(100.0, earned / total * 100)
    return scores


def judge_attempt(task: TaskDefinition, attempt: AttemptResult, paths: AppPaths) -> JudgeResult:
    return _judge_attempt(task, attempt, paths, DockerSandbox(task.runtime))


def _judge_attempt(
    task: TaskDefinition, attempt: AttemptResult, paths: AppPaths, sandbox: DockerSandbox,
    *, preparation_error: Exception | None = None,
) -> JudgeResult:
    started = time.monotonic()
    result_id = uuid4().hex
    folder = paths.artifact_path(f"judges/{attempt.id}/{result_id}")
    folder.mkdir(parents=True, exist_ok=False)
    raw = folder / "raw_output.txt"
    raw.write_text(attempt.raw_output, encoding="utf-8")
    log_path = folder / "judge.log"
    log_path.write_text("", encoding="utf-8")
    artifacts = [str(raw), str(log_path)]
    checks: list[CheckResult] = []
    gates: list[str] = []

    def finish(status: str, error: str | None = None) -> JudgeResult:
        scores = _scores(task, checks) if status == "completed" and task.rubric else {}
        passed = {check.check: check.passed for check in checks}
        quality = None
        required = None
        if status == "completed":
            quality = 0.0 if gates else min(
                100.0, sum(scores.get(Dimension(w.name), 0) * w.weight for w in task.scoring.dimension_weights),
            )
            required = not gates and all(passed.get(r.check, False) for r in task.rubric if r.required)
        result_path = folder / "result.json"
        result = JudgeResult(
            id=result_id, attempt_id=attempt.id, task_version_hash=task.version_hash,
            judge_version=JUDGE_VERSION, status=status, checks=tuple(checks),
            dimension_scores=scores, quality_score=quality, all_required_passed=required,
            gate_failures=tuple(dict.fromkeys(gates)),
            artifact_paths=tuple([*artifacts, str(result_path)]),
            judge_seconds=time.monotonic() - started, error_message=error,
        )
        if error:
            log_path.write_text(error[:8192] + "\n", encoding="utf-8")
        _write_json(result_path, result.model_dump(mode="json"))
        return result

    try:
        _task_contract(task)
        if attempt.task_id != task.id:
            raise SandboxError("Attempt task_id does not match the task")
        if attempt.status != "completed":
            gates.append("api_" + attempt.status)
            return finish("completed")
        if preparation_error is not None:
            raise preparation_error
        # Environment failure remains an invalid observation even for bad output.
        image_id = sandbox.prepare()
        runtime_path = folder / "runtime.json"
        _write_json(runtime_path, {
            "image": task.runtime.image, "image_id": image_id,
            "judge_version": JUDGE_VERSION,
            "runtime": task.runtime.model_dump(mode="json"),
            "effective_limits": {
                "memory_mb": min(task.runtime.memory_mb, 512),
                "cpus": min(task.runtime.cpus, 2),
                "pids": min(task.runtime.pids_limit, 128),
                "timeout_seconds": min(task.runtime.timeout_seconds, 120),
                "log_bytes_per_stream": 65536,
            },
            "isolation": "nonroot,network=none,read-only,cap-drop=ALL,no-new-privileges,tmpfs",
        })
        artifacts.append(str(runtime_path))
        try:
            files = extract_answer(task, attempt.raw_output)
        except AnswerFormatError as exc:
            gates.append("answer_format")
            return finish("completed", str(exc))
        extracted = folder / "extracted_files.json"
        _write_json(extracted, files)
        artifacts.append(str(extracted))
        with tempfile.TemporaryDirectory(prefix="coding-eval-") as temporary:
            directory = Path(temporary)
            _stage(directory, task, files)
            _, migration = migrate_check_source(task.test_files["checks.py"])
            if migration:
                effective_path = folder / "effective_checks.py"
                effective_path.write_text(
                    (directory / "tests" / "checks.py").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                migration_path = folder / "check_migration.json"
                _write_json(migration_path, migration)
                artifacts.extend((str(effective_path), str(migration_path)))
            preflight = sandbox.execute(directory, "preflight")
            preflight_log = folder / "preflight.log"
            preflight_log.write_text(preflight.detail + "\n" + preflight.log, encoding="utf-8")
            artifacts.append(str(preflight_log))
            if preflight.outcome == "judge_error":
                return finish("judge_error", preflight.detail)
            if preflight.outcome != "pass":
                gates.append("candidate_syntax")
                return finish("completed")
            for index, item in enumerate(task.rubric):
                verdict = sandbox.execute(directory, "check", item.check)
                check_log = folder / f"check-{index:03d}-{item.check}.log"
                check_log.write_text(verdict.detail + "\n" + verdict.log, encoding="utf-8")
                artifacts.append(str(check_log))
                checks.append(CheckResult(
                    check=item.check, passed=verdict.outcome == "pass", evidence=str(check_log),
                ))
                if verdict.outcome == "judge_error":
                    return finish("judge_error", f"{item.check}: {verdict.detail}")
                if verdict.outcome == "candidate_error":
                    gates.append("candidate_runtime")
                if item.critical and verdict.outcome != "pass":
                    gates.append("critical:" + item.check)
        return finish("completed")
    except DockerUnavailable as exc:
        return finish("blocked", str(exc))
    except (SandboxError, OSError, ValueError) as exc:
        return finish("judge_error", str(exc))


def validate_task(task: TaskDefinition, paths: AppPaths) -> TaskDefinition:
    """Run reference and all declared negatives; publish ready only on success.

    A failed/blocked validation raises TaskValidationError with results and
    artifact_paths, and never modifies the caller's task or readiness evidence.
    """
    results: list[JudgeResult] = []
    try:
        _task_contract(task)
        if not task.reference_files or not task.negative_files:
            raise SandboxError("Validation needs a reference and at least one negative")
        sandbox = DockerSandbox(task.runtime)
        try:
            image_id = sandbox.prepare()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                raise SandboxError("Validation requires an immutable Docker image ID")
        except (DockerUnavailable, SandboxError, OSError, ValueError) as exc:
            attempt = AttemptResult(
                run_id="validation-" + uuid4().hex, model_id="validation",
                task_id=task.id, sample_index=0, status="completed", raw_output="",
            )
            result = _judge_attempt(task, attempt, paths, sandbox, preparation_error=exc)
            raise TaskValidationError(
                f"Image preparation: {result.status}: {result.error_message}", [result],
            ) from exc
        # Clear old readiness before changing the runtime and hashing the exact
        # environment used by every reference/negative execution.
        payload = task.model_dump(mode="json")
        payload["runtime"]["image"] = image_id
        payload.update(status="draft", validation_evidence=[], validated_content_hash=None)
        task = TaskDefinition.model_validate(payload)
        cases = [("reference", task.reference_files), *task.negative_files.items()]
        for index, (name, files) in enumerate(cases):
            attempt = AttemptResult(
                run_id="validation-" + uuid4().hex, model_id="validation",
                task_id=task.id, sample_index=index, status="completed",
                raw_output=render_answer(task, files),
            )
            result = _judge_attempt(task, attempt, paths, sandbox)
            results.append(result)
            if result.status != "completed":
                raise TaskValidationError(f"{name}: {result.status}: {result.error_message}", results)
            if index == 0:
                if result.gate_failures or len(result.checks) != len(task.rubric) or not all(
                    c.passed for c in result.checks
                ):
                    raise TaskValidationError("Reference must pass every check", results)
            elif (
                not result.checks
                or not any(not check.passed for check in result.checks)
                or "answer_format" in result.gate_failures
            ):
                raise TaskValidationError(f"Negative {name} must be rejected by an executed check", results)
        payload = task.model_dump(mode="json")
        payload.update(
            status="ready",
            validation_evidence=[p for result in results for p in result.artifact_paths if p.endswith("result.json")],
            validated_content_hash=task.validation_hash,
        )
        return TaskDefinition.model_validate(payload)
    except TaskValidationError:
        raise
    except (SandboxError, AnswerFormatError, ValueError) as exc:
        raise TaskValidationError(str(exc), results) from exc
