"""Host tests exercise trusted orchestration only; task source stays inert data.

Set CODING_EVAL_DOCKER_TESTS=1 to additionally run the real container cases and
all ten built-in reference/negative validations. There is no host fallback.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from coding_eval import container_check, judges
from coding_eval.container_check import (
    CommandResult, ContainerResult, DockerSandbox, DockerUnavailable, SandboxError,
)
from coding_eval.judges import (
    AnswerFormatError, TaskValidationError, extract_answer, judge_attempt,
    render_answer, validate_task,
)
from coding_eval.models import (
    AttemptResult, Dimension, OutputFormat, RubricItem, RuntimeConfig,
    ScoringConfig, TaskDefinition, Weight,
)
from coding_eval.storage import AppPaths


IMAGE_ID = "sha256:" + "a" * 64


def make_task(kind="files", **changes):
    target = "test_solution.py" if kind == "tests" else "solution.py"
    values = dict(
        id="task", scenario_id="C01", title="Task", description="", system_prompt="",
        requirements="Return one fenced Python block",
        public_files={target: "value = 0\n"},
        reference_files={target: "value = 1\n"},
        negative_files={"wrong": {target: "value = 0\n"}},
        test_files={"checks.py": "import solution\n\ndef check_value():\n    assert solution.value == 1\n"},
        rubric=(RubricItem(check="check_value", dimension=Dimension.FUNCTIONALITY, description="value"),),
        scoring=ScoringConfig(dimension_weights=(Weight(name="functionality", weight=1),)),
        output_format=OutputFormat(kind=kind, allowed_paths=(target,), instructions="one block"),
    )
    values.update(changes)
    return TaskDefinition(**values)


def make_attempt(task, **changes):
    values = dict(
        run_id="run", model_id="model", task_id=task.id, sample_index=0,
        status="completed", raw_output=render_answer(task, task.reference_files),
    )
    values.update(changes)
    return AttemptResult(**values)


@pytest.fixture
def paths(tmp_path):
    return AppPaths(root=tmp_path, database=tmp_path / "db.sqlite3", artifacts=tmp_path / "artifacts")


def sandbox_mock(monkeypatch, outcomes=()):
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.return_value = IMAGE_ID
    sandbox.execute.side_effect = [ContainerResult(value, value, "bounded log") for value in outcomes]
    factory = Mock(return_value=sandbox)
    monkeypatch.setattr(judges, "DockerSandbox", factory)
    return sandbox, factory


@pytest.mark.parametrize("kind", ["files", "tests", "search_replace"])
@pytest.mark.parametrize("source", ["value = 1", "value = 1\n", "value = 1\n\n"])
def test_render_answer_round_trip(kind, source):
    task = make_task(kind)
    files = {task.output_format.allowed_paths[0]: source}
    assert extract_answer(task, render_answer(task, files)) == files


@pytest.mark.parametrize("answer", [
    "value = 1", "```py\nvalue = 1\n```", "Explanation\n```python\nvalue = 1\n```",
    "```python\nvalue = 1\n```\nMore text",
    "```python\nvalue = 1\n```\n```python\nvalue = 2\n```",
    '{"solution.py":"value=1"}',
])
def test_extract_answer_rejects_protocol_violations(answer):
    with pytest.raises(AnswerFormatError, match="exactly one"):
        extract_answer(make_task(), answer)


def test_extract_answer_does_not_execute_or_parse_python(tmp_path):
    sentinel = tmp_path / "not-created"
    text = f"this is invalid Python!!!\nopen({str(sentinel)!r}, 'w').write('bad')"
    assert extract_answer(make_task(), f"```python\n{text}\n```") == {"solution.py": text}
    assert not sentinel.exists()


def patch_block(search, replacement, path="solution.py"):
    prefix = path + "\n" if path else ""
    return f"{prefix}<<<<<<< SEARCH\n{search}\n=======\n{replacement}\n>>>>>>> REPLACE"


def test_search_replace_multiple_original_matches():
    task = make_task("search_replace", public_files={"solution.py": "alpha = 1\nbeta = 2\n"})
    answer = "```python\n" + patch_block("alpha = 1", "alpha = 3") + "\n\n"
    answer += patch_block("beta = 2", "beta = 4", path="") + "\n```"
    assert extract_answer(task, answer) == {"solution.py": "alpha = 3\nbeta = 4\n"}


@pytest.mark.parametrize("source,blocks,reason", [
    ("aaaa", [patch_block("aaa", "x")], "exactly once"),
    ("one one", [patch_block("one", "two")], "exactly once"),
    ("one", [patch_block("absent", "two")], "exactly once"),
    ("one", [patch_block("", "two")], "empty"),
    ("one", [patch_block("one", "two", "../solution.py")], "unauthorized"),
    ("one", [patch_block("one", "two", "other.py")], "unauthorized"),
    ("abcdef", [patch_block("abc", "x"), patch_block("bcd", "y")], "Overlapping"),
    ("one", [patch_block("one", "two"), patch_block("two", "three")], "exactly once"),
    ("one", ["<<<<<<< SEARCH\none\n===\ntwo\n>>>>>>> REPLACE"], "Malformed"),
])
def test_search_replace_rejects_ambiguous_or_unauthorized(source, blocks, reason):
    task = make_task("search_replace", public_files={"solution.py": source})
    with pytest.raises(AnswerFormatError, match=reason):
        extract_answer(task, "```python\n" + "\n".join(blocks) + "\n```")


def test_render_rejects_undeclared_files():
    with pytest.raises(AnswerFormatError, match="exactly match"):
        render_answer(make_task(), {"other.py": "value = 1"})


@pytest.mark.parametrize("outcomes,status,quality,gate", [
    (["pass", "pass"], "completed", 100, None),
    (["pass", "fail"], "completed", 0, None),
    (["candidate_error"], "completed", 0, "candidate_syntax"),
    (["pass", "candidate_error"], "completed", 0, "candidate_runtime"),
    (["judge_error"], "judge_error", None, None),
    (["pass", "judge_error"], "judge_error", None, None),
])
def test_judge_outcomes_and_artifacts(monkeypatch, paths, outcomes, status, quality, gate):
    task = make_task()
    sandbox, factory = sandbox_mock(monkeypatch, outcomes)
    attempt = make_attempt(task)
    result = judge_attempt(task, attempt, paths)
    assert (result.status, result.quality_score) == (status, quality)
    assert (gate in result.gate_failures) if gate else not result.gate_failures
    assert result.attempt_id == attempt.id
    assert result.task_version_hash == task.version_hash
    assert sandbox.execute.call_count == len(outcomes)
    factory.assert_called_once_with(task.runtime)
    for artifact in result.artifact_paths:
        assert Path(artifact).is_file()
    saved = json.loads(Path(result.artifact_paths[-1]).read_text())
    assert saved == result.model_dump(mode="json")
    assert not list(paths.artifacts.rglob("solution.py"))


def test_weighted_diagnostic_scores_and_critical_gate(monkeypatch, paths):
    task = make_task(
        rubric=(
            RubricItem(check="a", dimension="functionality", description="a", points=3),
            RubricItem(check="b", dimension="functionality", description="b", points=1, critical=True),
            RubricItem(check="c", dimension="security", description="c", required=False),
        ),
        scoring=ScoringConfig(dimension_weights=(
            Weight(name="functionality", weight=0.8), Weight(name="security", weight=0.2),
        )),
    )
    sandbox_mock(monkeypatch, ["pass", "pass", "fail", "pass"])
    result = judge_attempt(task, make_attempt(task), paths)
    assert result.dimension_scores == {Dimension.FUNCTIONALITY: 75, Dimension.SECURITY: 100}
    assert result.quality_score == 0
    assert result.gate_failures == ("critical:b",)
    assert result.all_required_passed is False


def test_format_error_is_zero_without_code_execution(monkeypatch, paths):
    task = make_task()
    sandbox, _ = sandbox_mock(monkeypatch)
    result = judge_attempt(task, make_attempt(task, raw_output="prose"), paths)
    assert result.status == "completed" and result.quality_score == 0
    assert result.gate_failures == ("answer_format",)
    sandbox.execute.assert_not_called()


@pytest.mark.parametrize("error,status", [
    (DockerUnavailable("No daemon"), "blocked"), (SandboxError("Bad setup"), "judge_error"),
])
def test_environment_failure_never_becomes_ability_score(monkeypatch, paths, error, status):
    task = make_task()
    sandbox, _ = sandbox_mock(monkeypatch)
    sandbox.prepare.side_effect = error
    result = judge_attempt(task, make_attempt(task), paths)
    assert result.status == status
    assert result.quality_score is None and result.all_required_passed is None
    assert error.args[0] in result.error_message
    sandbox.execute.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "cancelled", "uncertain"])
def test_api_failure_counts_zero_without_docker(monkeypatch, paths, status):
    task = make_task()
    sandbox, _ = sandbox_mock(monkeypatch)
    result = judge_attempt(task, make_attempt(task, status=status), paths)
    assert result.quality_score == 0 and result.gate_failures == ("api_" + status,)
    sandbox.prepare.assert_not_called()


def test_missing_task_checks_is_judge_error(monkeypatch, paths):
    task = make_task(test_files={})
    sandbox, _ = sandbox_mock(monkeypatch)
    result = judge_attempt(task, make_attempt(task), paths)
    assert result.status == "judge_error" and result.quality_score is None
    sandbox.prepare.assert_not_called()


def test_validation_binds_evidence_and_runs_every_negative(monkeypatch, paths):
    task = make_task(negative_files={
        "wrong": {"solution.py": "value = 0"}, "also_wrong": {"solution.py": "value = 2"},
    })
    sandbox, factory = sandbox_mock(monkeypatch, ["pass", "pass", "pass", "fail", "pass", "fail"])
    ready = validate_task(task, paths)
    assert ready.ready and ready.validated_content_hash == ready.validation_hash
    assert ready.runtime.image == IMAGE_ID
    assert ready.validation_hash != task.validation_hash
    assert task.runtime.image == "python:3.11-slim"
    assert not task.ready and not task.validation_evidence
    assert len(ready.validation_evidence) == 3
    assert all(Path(path).is_file() for path in ready.validation_evidence)
    assert sandbox.execute.call_count == 6
    factory.assert_called_once_with(task.runtime)
    pinned_draft = ready.model_copy(update={
        "status": "draft", "validation_evidence": (), "validated_content_hash": None,
    })
    for evidence in ready.validation_evidence:
        saved = json.loads(Path(evidence).read_text())
        assert saved["task_version_hash"] == pinned_draft.version_hash
        runtime = json.loads(Path(evidence).with_name("runtime.json").read_text())
        assert runtime["image"] == runtime["image_id"] == runtime["runtime"]["image"] == IMAGE_ID
    changed = ready.model_dump(mode="json")
    changed["requirements"] += " edited"
    with pytest.raises(ValueError, match="绑定"):
        TaskDefinition.model_validate(changed)


def test_validation_revalidates_a_ready_task_without_mutating_it(monkeypatch, paths):
    sandbox, _ = sandbox_mock(monkeypatch, ["pass", "pass", "pass", "fail"] * 2)
    first = validate_task(make_task(), paths)
    before = first.model_dump(mode="json")
    second = validate_task(first, paths)
    assert first.model_dump(mode="json") == before
    assert first.validation_hash == second.validation_hash
    assert first.validation_evidence != second.validation_evidence
    assert sandbox.execute.call_count == 8


@pytest.mark.parametrize("image", ["python:3.11-slim", "sha256:abc", ""])
def test_validation_rejects_unresolved_image_before_execution(monkeypatch, paths, image):
    sandbox, _ = sandbox_mock(monkeypatch)
    sandbox.prepare.return_value = image
    with pytest.raises(TaskValidationError, match="immutable") as caught:
        validate_task(make_task(), paths)
    assert caught.value.status == "judge_error"
    sandbox.execute.assert_not_called()


@pytest.mark.parametrize("outcomes,message", [
    (["pass", "fail"], "Reference"),
    (["pass", "pass", "pass", "pass"], "Negative"),
    (["pass", "pass", "candidate_error"], "Negative"),
    (["pass", "judge_error"], "judge_error"),
])
def test_validation_rejects_invalid_reference_or_negative(monkeypatch, paths, outcomes, message):
    task = make_task()
    sandbox_mock(monkeypatch, outcomes)
    with pytest.raises(TaskValidationError, match=message) as caught:
        validate_task(task, paths)
    assert caught.value.artifact_paths
    assert not task.ready


def test_validation_blocked_carries_evidence(monkeypatch, paths):
    sandbox, _ = sandbox_mock(monkeypatch)
    sandbox.prepare.side_effect = DockerUnavailable("No daemon")
    with pytest.raises(TaskValidationError, match="blocked") as caught:
        validate_task(make_task(), paths)
    assert caught.value.status == "blocked" and caught.value.artifact_paths


def test_staging_mount_contains_no_reference_or_credentials(monkeypatch, paths):
    task = make_task(reference_files={"solution.py": "reference_secret = 1"})
    sandbox, _ = sandbox_mock(monkeypatch)
    locations = []

    def execute(directory, phase, check=""):
        locations.append(directory)
        assert (directory / "work" / "solution.py").read_text() == "value = 9"
        assert (directory / "tests" / "checks.py").read_text() == task.test_files["checks.py"]
        assert json.loads((directory / "job.json").read_text())["candidate_paths"] == ["solution.py"]
        assert sorted(p.name for p in directory.iterdir()) == ["job.json", "runner.py", "tests", "work"]
        return ContainerResult("pass", "ok")

    sandbox.execute.side_effect = execute
    result = judge_attempt(task, make_attempt(task, raw_output="```python\nvalue = 9\n```"), paths)
    assert result.quality_score == 100
    assert all(not path.exists() for path in locations)


def test_docker_command_enforces_isolation_and_fixed_image(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "not-forwarded")
    sandbox = DockerSandbox(RuntimeConfig(cpus=9, memory_mb=4096, pids_limit=999))
    sandbox.image_id = IMAGE_ID
    command = sandbox.command(tmp_path, "owned-container", "check", "check_value")
    for option, value in [
        ("--network", "none"), ("--user", "65534:65534"), ("--cpus", "2"),
        ("--memory", "512m"), ("--memory-swap", "512m"), ("--pids-limit", "128"),
        ("--cap-drop", "ALL"), ("--pull", "never"), ("--log-driver", "none"),
        ("--security-opt", "no-new-privileges:true"),
    ]:
        assert command[command.index(option) + 1] == value
    assert "--read-only" in command
    assert command[command.index("--mount") + 1].endswith("dst=/input,readonly")
    assert "noexec,nosuid,nodev" in command[command.index("--tmpfs") + 1]
    assert IMAGE_ID in command and "python:3.11-slim" not in command
    assert not any("MODEL_API_KEY" in argument or "docker.sock" in argument for argument in command)


def test_docker_prepare_pulls_only_official_and_resolves_once(monkeypatch):
    command = Mock(side_effect=[
        CommandResult(0, b"27.0\n", b""), CommandResult(1, b"", b"missing"),
        CommandResult(0, b"pulled", b""), CommandResult(0, IMAGE_ID.encode(), b""),
    ])
    monkeypatch.setattr(container_check, "run_command", command)
    sandbox = DockerSandbox(RuntimeConfig())
    assert sandbox.prepare() == sandbox.prepare() == IMAGE_ID
    assert command.call_count == 4
    assert command.call_args_list[2].args[0] == ["docker", "pull", "python:3.11-slim"]


@pytest.mark.parametrize("resolved,error", [
    (IMAGE_ID, None),
    ("sha256:" + "b" * 64, SandboxError),
    ("missing", DockerUnavailable),
])
def test_docker_prepare_pinned_id_never_re_resolves_tag_or_pulls(monkeypatch, resolved, error):
    command = Mock(side_effect=[
        CommandResult(0, b"27.0\n", b""),
        CommandResult(1 if resolved == "missing" else 0, resolved.encode(), b""),
    ])
    monkeypatch.setattr(container_check, "run_command", command)
    sandbox = DockerSandbox(RuntimeConfig(image=IMAGE_ID))
    if error:
        with pytest.raises(error):
            sandbox.prepare()
        assert sandbox.image_id is None
    else:
        assert sandbox.prepare() == sandbox.prepare() == IMAGE_ID
    assert command.call_count == 2
    assert command.call_args.args[0] == ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE_ID]


@pytest.mark.parametrize("runtime", [
    RuntimeConfig(image="evil/python:3.11"), RuntimeConfig(dependencies=("requests==2.0",)),
])
def test_docker_rejects_unapproved_environment(monkeypatch, runtime):
    command = Mock()
    monkeypatch.setattr(container_check, "run_command", command)
    with pytest.raises(SandboxError):
        DockerSandbox(runtime).prepare()
    command.assert_not_called()


def test_docker_unavailable_is_bounded_and_never_runs_host_code(monkeypatch):
    command = Mock(side_effect=FileNotFoundError("docker"))
    monkeypatch.setattr(container_check, "run_command", command)
    with pytest.raises(DockerUnavailable, match="CLI unavailable"):
        DockerSandbox(RuntimeConfig()).prepare()
    command.assert_called_once_with(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)


@pytest.mark.parametrize("mode,expected", [
    ("signed", "pass"), ("forged", "candidate_error"),
    ("exit_zero", "candidate_error"), ("timeout", "candidate_error"),
    ("oom", "candidate_error"),
])
def test_docker_verdict_authentication_and_cleanup(monkeypatch, tmp_path, mode, expected):
    calls = []

    def command(argv, *, timeout, input_data=None):
        calls.append(argv)
        if argv[1] == "run":
            payload = json.dumps({
                "phase": "check", "check": "check_value", "outcome": "pass", "detail": "ok",
            })
            signature = hmac.new(bytes.fromhex(input_data.decode().strip()), payload.encode(), hashlib.sha256)
            body = json.dumps({
                "payload": payload, "signature": signature.hexdigest() if mode == "signed" else "fake",
            }).encode()
            return CommandResult(0, b"" if mode == "exit_zero" else body, b"candidate log", mode == "timeout")
        if argv[1] == "inspect":
            return CommandResult(0, json.dumps({"OOMKilled": mode == "oom", "Error": ""}).encode(), b"")
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(container_check, "run_command", command)
    sandbox = DockerSandbox(RuntimeConfig())
    sandbox.image_id = IMAGE_ID
    verdict = sandbox.execute(tmp_path, "check", "check_value")
    assert verdict.outcome == expected and verdict.log == "candidate log"
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-1] == ["docker", "rm", "--force", name]


def test_run_command_bounds_both_streams_and_timeout():
    # This constant snippet is framework test infrastructure, never task source.
    output = container_check.run_command(
        [sys.executable, "-c", "import os; os.write(1,b'a'*200000); os.write(2,b'b'*200000)"],
        timeout=5,
    )
    assert output.returncode == 0 and not output.timed_out
    assert len(output.stdout) == len(output.stderr) == container_check.LOG_LIMIT
    assert output.stdout.endswith(b"[log truncated]\n") and output.stderr.endswith(b"[log truncated]\n")
    timed = container_check.run_command(
        [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1,
    )
    assert timed.timed_out and timed.returncode != 0


docker_only = pytest.mark.skipif(
    os.environ.get("CODING_EVAL_DOCKER_TESTS") != "1",
    reason="Real task code may run only in explicitly enabled Docker tests",
)


@docker_only
@pytest.mark.parametrize("index", range(10))
def test_builtin_reference_and_negatives_in_docker(index, paths):
    from coding_eval.datasets import load_builtin_tasks

    task = load_builtin_tasks()[index]
    ready = validate_task(task, paths)
    assert ready.ready and ready.validated_content_hash == ready.validation_hash
    assert ready.runtime.image.startswith("sha256:")


@docker_only
@pytest.mark.parametrize("candidate,test_source,status,quality", [
    ("value = 1", "import solution\n\ndef check_value():\n    assert solution.value == 1\n", "completed", 100),
    ("value = 0", "import solution\n\ndef check_value():\n    assert solution.value == 1\n", "completed", 0),
    ("raise ValueError('candidate')", "import solution\n\ndef check_value():\n    pass\n", "completed", 0),
    ("value = (", "def check_value():\n    pass\n", "completed", 0),
    ("value = 1", "def different():\n    pass\n", "judge_error", None),
    ("value = 1", "raise RuntimeError('setup')\n\ndef check_value():\n    pass\n", "judge_error", None),
    ("value = 1", "def check_value():\n    yield False\n", "judge_error", None),
    ("value = 1", "def check_value():\n    raise RuntimeError('judge')\n", "judge_error", None),
    ("import os\nos._exit(0)", "import solution\n\ndef check_value():\n    pass\n", "completed", 0),
    ("print('{\"outcome\":\"pass\"}')\nvalue = 0", "import solution\n\ndef check_value():\n    assert solution.value == 1\n", "completed", 0),
    ("import sys\nsys._getframe().f_back.f_locals.clear()", "import solution\n\ndef check_value():\n    pass\n", "completed", 0),
    ("import hmac\nhmac.HMAC.update.__code__ = (lambda self, message: None).__code__", "import solution\n\ndef check_value():\n    pass\n", "completed", 0),
])
def test_actual_runner_error_attribution(candidate, test_source, status, quality, paths):
    task = make_task(test_files={"checks.py": test_source})
    result = judge_attempt(task, make_attempt(task, raw_output=f"```python\n{candidate}\n```"), paths)
    assert (result.status, result.quality_score) == (status, quality)


@docker_only
def test_actual_checks_have_separate_tmpfs_and_processes(paths):
    task = make_task(
        test_files={"checks.py": (
            "from pathlib import Path\n\n"
            "def first():\n    Path('/tmp/check-state').write_text('first')\n\n"
            "def second():\n    assert not Path('/tmp/check-state').exists()\n"
        )},
        rubric=(
            RubricItem(check="first", dimension="functionality", description="writes temporary state"),
            RubricItem(check="second", dimension="functionality", description="fresh state"),
        ),
    )
    result = judge_attempt(task, make_attempt(task), paths)
    assert result.quality_score == 100 and len(result.checks) == 2
