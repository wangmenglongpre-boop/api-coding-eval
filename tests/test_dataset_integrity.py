"""Inert dataset inspection on the host; behavioral checks are Docker-only.

Never exec/compile/import embedded checks, references, negatives or C10's runner
here. Static assertions protect the contract, not prove runtime correctness.
"""

import ast
import os
import textwrap
from unittest.mock import Mock

import pytest

from coding_eval import judges
from coding_eval.container_check import DockerSandbox, DockerUnavailable
from coding_eval.datasets import load_builtin_tasks
from coding_eval.judges import (
    TaskValidationError, extract_answer, judge_attempt, render_answer, validate_task,
)
from coding_eval.models import AttemptResult, ScoringConfig, TaskDefinition, Weight
from coding_eval.storage import AppPaths


NEGATIVE_CHECKS = {
    "C01": {"float_rounding": "check_totals"},
    "C02": {
        "unlocked_read_modify_write": "check_interleaving",
        "unlocked_read_then_recheck": "check_interleaving",
        "write_after_unlock": "check_interleaving",
    },
    "C03": {
        "duplicate_credit": "check_idempotency",
        "autocommit_compensation": "check_concurrent",
        "commit_before_credit": "check_rollback",
    },
    "C04": {"coercion_without_validation": "check_invalid"},
    "C05": {"shallow_copy": "check_no_alias"},
    "C06": {"no_retry": "check_retries"},
    "C07": {"n_plus_one": "check_query_budget"},
    "C08": {"request_admin_bypass": "check_isolation"},
    "C09": {"float_and_last_wins": "check_clean"},
    "C10": {
        "only_happy_path": "check_business_mutants",
        "forged_stdout_exit": "check_production_integrity",
        "tuple_monkeypatch": "check_production_integrity",
        "test_method_not_discovered": "check_production_integrity",
    },
}


@pytest.fixture
def catalog():
    return {task.id: task for task in load_builtin_tasks()}


@pytest.fixture
def paths(tmp_path):
    return AppPaths(
        root=tmp_path, database=tmp_path / "db.sqlite",
        artifacts=tmp_path / "artifacts",
    )


def _tree(task):
    return ast.parse(task.test_files["checks.py"])


def _function(tree, name):
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _value(tree, name):
    return next(
        node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    )


def _calls(node):
    return {
        ast.unparse(child.func) for child in ast.walk(node)
        if isinstance(child, ast.Call)
    }


@pytest.mark.parametrize("task_id", NEGATIVE_CHECKS)
def test_reference_and_negative_matrix_is_frozen_and_renderable(catalog, task_id):
    task = catalog[task_id]
    assert task.status == "draft" and not task.ready
    assert task.validated_content_hash is None and not task.validation_evidence
    assert set(task.negative_files) == set(NEGATIVE_CHECKS[task_id])
    assert set(NEGATIVE_CHECKS[task_id].values()) <= {item.check for item in task.rubric}
    cases = [task.reference_files, *task.negative_files.values()]
    for files in cases:
        assert extract_answer(task, render_answer(task, files)) == files
        for path, source in files.items():
            ast.parse(source, filename=f"{task_id}/{path}")
    assert len({tuple(sorted(files.items())) for files in cases}) == len(cases)


def test_c10_runner_uses_isolation_and_never_candidate_stdout(catalog):
    task = catalog["C10"]
    tree = _tree(task)
    run = _function(tree, "_run")
    validation = next(
        node for node in ast.walk(run)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_validate"
    )
    launch = next(
        node for node in ast.walk(run)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run"
    )
    assert validation.lineno < launch.lineno
    argv = launch.args[0]
    assert isinstance(argv, ast.List)
    assert [node.value for node in argv.elts if isinstance(node, ast.Constant)][:3] == [
        "-I", "-B", "-c",
    ]
    options = {kw.arg: ast.unparse(kw.value) for kw in launch.keywords}
    assert options["stdout"] == "subprocess.DEVNULL"
    assert options["stderr"] == "subprocess.PIPE" and options["timeout"] == "6"
    assert "capture_output" not in options
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "stdout"
        for node in ast.walk(tree)
    )
    runner = ast.parse(ast.literal_eval(_value(tree, "RUNNER")))
    calls = _calls(runner)
    assert {
        "unittest.TestLoader", "loader.loadTestsFromModule",
        "unittest.TextTestRunner", "SystemExit",
    } <= calls
    assert "print" not in calls and "json.loads" not in calls
    attributes = {node.attr for node in ast.walk(runner) if isinstance(node, ast.Attribute)}
    assert {
        "testsRun", "errors", "skipped", "expectedFailures", "unexpectedSuccesses", "failures",
    } <= attributes
    security = next(item for item in task.rubric if item.check == "check_production_integrity")
    assert security.critical
    assert "_validate" in _calls(_function(tree, security.check))


def test_c10_whitelist_and_reference_match_published_subset(catalog):
    task = catalog["C10"]
    tree = _tree(task)
    allowed_nodes = {node.attr for node in _value(tree, "NODES").elts}
    assert not allowed_nodes & {
        "Lambda", "Raise", "Try", "Delete", "NamedExpr", "AugAssign",
        "AnnAssign", "Global", "Nonlocal", "While", "ListComp", "AsyncFunctionDef",
    }
    reference = ast.parse(task.reference_files["test_solution.py"])
    assert {type(node).__name__ for node in ast.walk(reference)} <= allowed_nodes
    calls = ast.literal_eval(_value(tree, "CALLS"))
    assertions = ast.literal_eval(_value(tree, "ASSERTIONS"))
    assert not calls & {"print", "exit", "quit", "eval", "exec", "getattr", "setattr"}
    for name in calls | assertions:
        assert name in task.requirements
    assert {"_target", "_local", "_attribute"} <= _calls(_function(tree, "_validate"))
    assert "_target" in _calls(_function(tree, "_target"))
    assert not _calls(_function(tree, "_validate")) & {"exec", "compile", "eval"}
    assert ast.literal_eval(_value(tree, "BASELINE")) == task.public_files["solution.py"]
    mutants = _value(tree, "MUTANTS")
    assert {key.value for key in mutants.keys} == {"threshold", "rounding", "member", "validation"}
    assert "stdout" in task.requirements and "AST" in task.requirements
    assert "受限 unittest" in task.output_format.instructions


def test_c02_scheduler_observes_ownership_and_always_releases(catalog):
    task = catalog["C02"]
    tree = _tree(task)
    check = _function(tree, "check_interleaving")
    calls = _calls(check)
    assert {"threading.RLock", "lock._is_owned", "first_read.wait", "second_done.wait"} <= calls
    assert "lock.locked" not in calls and "sleep" not in calls
    assert any(
        isinstance(node, ast.Assert) and ast.unparse(node.test) == "held_at_read"
        for node in ast.walk(check)
    )
    cleanup = next(
        node.finalbody for node in ast.walk(check)
        if isinstance(node, ast.Try) and any(
            "release.set" in _calls(statement) for statement in node.finalbody
        )
    )
    assert {"release.set", "first.join", "second.join"} <= set().union(
        *(_calls(statement) for statement in cleanup)
    )
    for node in ast.walk(check):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"wait", "join"}:
                assert len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                assert node.args[0].value > 0
    assert "拥有共享锁" in task.requirements and "不得绕过子类访问器" in task.requirements
    assert "threading.Lock" in _calls(_function(tree, "check_reserve"))


def test_c03_observes_transactions_before_writes_and_before_close(catalog):
    task = catalog["C03"]
    tree = _tree(task)
    check = _function(tree, "check_concurrent")
    worker = _function(check, "worker")
    authorize = _function(worker, "authorize")
    assert "writes.append" in _calls(authorize)
    attributes = {node.attr for node in ast.walk(authorize) if isinstance(node, ast.Attribute)}
    assert {"SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "in_transaction"} <= attributes
    calls = [
        node for node in ast.walk(worker) if isinstance(node, ast.Call)
    ]
    install = next(node for node in calls if ast.unparse(node.func) == "conn.set_authorizer")
    invoke = next(node for node in calls if ast.unparse(node.func) == "solution.apply_payment")
    assert install.lineno < invoke.lineno
    assert any(
        isinstance(node, ast.Assert) and node.lineno > invoke.lineno
        and ast.unparse(node.test) == "not conn.in_transaction"
        for node in ast.walk(worker)
    )
    assert "blocker.rollback" in _calls(check) and "blocker.close" in _calls(check)
    assert "显式事务" in task.requirements and "authorizer" in task.requirements


@pytest.mark.parametrize("task_id", ["C02", "C03", "C10"])
def test_validation_remains_blocked_without_docker(catalog, paths, monkeypatch, task_id):
    sandbox = Mock(spec=DockerSandbox)
    sandbox.prepare.side_effect = DockerUnavailable("Docker is not ready")
    monkeypatch.setattr(judges, "DockerSandbox", Mock(return_value=sandbox))
    with pytest.raises(TaskValidationError) as caught:
        validate_task(catalog[task_id], paths)
    assert caught.value.status == "blocked"
    sandbox.execute.assert_not_called()
    assert not catalog[task_id].ready


docker_only = pytest.mark.skipif(
    os.environ.get("CODING_EVAL_DOCKER_TESTS") != "1",
    reason="Dataset behavior must run only in explicitly enabled Docker",
)


def _single_check(task, name):
    payload = task.model_dump(mode="json")
    item = next(item for item in task.rubric if item.check == name)
    payload["rubric"] = [item.model_dump(mode="json")]
    payload["scoring"] = ScoringConfig(
        dimension_weights=(Weight(name=item.dimension.value, weight=1),),
    ).model_dump(mode="json")
    return TaskDefinition.model_validate(payload)


def _judge_files(task, files, paths):
    attempt = AttemptResult(
        run_id="dataset-integrity", model_id="validation", task_id=task.id,
        sample_index=0, status="completed", raw_output=render_answer(task, files),
    )
    return judge_attempt(task, attempt, paths)


@docker_only
@pytest.mark.parametrize("task_id,negative,check", [
    (task_id, negative, check)
    for task_id, cases in NEGATIVE_CHECKS.items() for negative, check in cases.items()
])
def test_targeted_negative_is_rejected_by_its_intended_check(
    catalog, paths, task_id, negative, check,
):
    task = _single_check(catalog[task_id], check)
    reference = _judge_files(task, task.reference_files, paths)
    assert reference.status == "completed" and reference.all_required_passed
    result = _judge_files(task, task.negative_files[negative], paths)
    assert result.status == "completed" and result.all_required_passed is False
    assert len(result.checks) == 1 and result.checks[0].passed is False
    assert "candidate_syntax" not in result.gate_failures


@docker_only
@pytest.mark.parametrize("statement", [
    "unittest.case.sys.exit(0)",
    "self.run()",
    "self.assertEqual = int",
    "(value, (solution.shipping_fee,)) = (1, (int,))",
    "for self.assertEqual in (int,):\n    pass",
    "self.__dict__.clear()",
    "unittest = solution",
    "fee = shipping_fee\nfee(1, 0)",
    "(type if True else int)('X', (), {})",
    "self.assertEqual.__func__",
    "import unittest",
    "with self.assertRaises(ValueError, shipping_fee, 0, 0):\n    pass",
    "with self.subTest() as value:\n    pass",
    "self.addCleanup(int)",
])
def test_c10_rejects_nested_escape_surfaces_before_execution(catalog, paths, statement):
    task = _single_check(catalog["C10"], "check_production_integrity")
    reference = task.reference_files["test_solution.py"]
    signature = "    def test_member(self):\n"
    candidate = reference.replace(signature, signature + textwrap.indent(statement, "        ") + "\n")
    result = _judge_files(task, {"test_solution.py": candidate}, paths)
    assert result.status == "completed"
    assert result.gate_failures == ("critical:check_production_integrity",)
    assert not result.checks[0].passed


@docker_only
@pytest.mark.parametrize("style", ["reference", "qualified", "local_table"])
def test_c10_accepts_documented_test_styles(catalog, paths, style):
    task = catalog["C10"]
    candidate = task.reference_files["test_solution.py"]
    if style == "qualified":
        candidate = candidate.replace("from solution import shipping_fee", "import solution")
        candidate = candidate.replace("shipping_fee(", "solution.shipping_fee(")
    elif style == "local_table":
        candidate += (
            "\n    def test_local_table(self):\n"
            "        cases = [(1000, 600), (1001, 800)]\n"
            "        for weight, expected in cases:\n"
            "            actual = shipping_fee(weight, 0)\n"
            "            assert actual == expected\n"
        )
    result = _judge_files(task, {"test_solution.py": candidate}, paths)
    assert result.status == "completed" and result.all_required_passed
    assert result.quality_score == 100
