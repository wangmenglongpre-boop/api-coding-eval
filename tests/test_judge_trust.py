"""Trusted framework tests run on the host; all task snippets stay Docker-only."""

import functools
import types
from unittest.mock import Mock

import pytest

from coding_eval.container_check import (
    _deferred_candidate_imports, _exception_outcome, _protect_check_dependencies,
)
from coding_eval.datasets import load_builtin_tasks
from coding_eval.judges import judge_attempt, render_answer
from test_judges import docker_only, make_attempt, make_task, paths  # noqa: F401


def test_trusted_dependencies_reject_module_writes_and_detect_suppressed_attempts():
    module = types.ModuleType("framework_fixture")
    module.wraps = functools.wraps
    verify = _protect_check_dependencies([module])
    verify()
    assert module.wraps is functools.wraps
    with pytest.raises(TypeError):
        module.__dict__["wraps"] = None
    with pytest.raises(PermissionError, match="read-only"):
        module.wraps = None
    with pytest.raises(PermissionError, match="mutate"):
        verify()


@pytest.mark.parametrize("mutation", ["class", "globals", "function", "delete"])
def test_trusted_dependencies_detect_indirect_mutations(mutation):
    module = types.ModuleType("framework_fixture")

    class Fixture:
        def assert_value(self):
            return True

    Fixture.__module__ = module.__name__
    module.Fixture = Fixture
    module.check = lambda: True
    namespace = module.__dict__
    verify = _protect_check_dependencies([module])
    verify()
    if mutation == "class":
        Fixture.assert_value = lambda self: None
    elif mutation == "globals":
        namespace["Fixture"] = None
    elif mutation == "function":
        module.check.__code__ = (lambda: False).__code__
    else:
        with pytest.raises(PermissionError):
            del module.Fixture
    with pytest.raises(PermissionError):
        verify()


def test_trusted_dependencies_allow_internal_cache_updates():
    module = types.ModuleType("framework_fixture")
    module._cache = {}
    verify = _protect_check_dependencies([module])
    module._cache["key"] = "value"
    module._cache = {"new": "value"}
    verify()


def test_deferred_import_loads_definitions_before_candidate_and_resolves_aliases():
    actual = types.ModuleType("solution")
    actual.value = 1
    importer = Mock(return_value=actual)
    import_for_checks, resolve = _deferred_candidate_imports({"solution"}, importer)
    proxy = import_for_checks("solution")
    namespace = {"solution": proxy, "alias": proxy, "check": lambda: None}
    importer.assert_not_called()
    resolve(namespace)
    importer.assert_called_once_with("solution")
    assert namespace["solution"] is namespace["alias"] is actual


def test_deferred_import_preserves_attribute_access_and_other_imports():
    actual = types.ModuleType("solution")
    actual.value = 7
    importer = Mock(return_value=actual)
    import_for_checks, _ = _deferred_candidate_imports({"solution"}, importer)
    assert import_for_checks("solution").value == 7
    importer.assert_called_once_with("solution")
    importer.reset_mock()
    assert import_for_checks("decimal", fromlist=("Decimal",)) is actual
    importer.assert_called_once_with("decimal", None, None, ("Decimal",), 0)


@pytest.mark.parametrize("kind,stage,expected", [
    (AssertionError, "check", "fail"),
    (AssertionError, "setup", "judge_error"),
    (NameError, "check", "judge_error"),
    (OSError, "check", "judge_error"),
    (ValueError, "check", "judge_error"),
])
def test_legacy_case_wrappers_preserve_judge_exception_origin(kind, stage, expected):
    try:
        try:
            raise kind("trusted fixture failure")
        except Exception as cause:
            raise AssertionError("legacy _case") from cause
    except AssertionError as exc:
        assert _exception_outcome(exc, stage, set()) == expected


def test_legacy_case_wrappers_preserve_candidate_exception_origin():
    # This is fixed framework test code, not task code or a host eval/exec.
    try:
        try:
            raise ValueError("framework fixture standing in for a candidate frame")
        except Exception as cause:
            raise AssertionError("legacy _case") from cause
    except AssertionError as exc:
        assert _exception_outcome(exc, "check", {__file__}) == "candidate_error"


def test_missing_candidate_api_and_exception_chain_cycles_are_bounded():
    module = types.ModuleType("solution")
    module.__file__ = "/input/work/solution.py"
    try:
        module.missing_api
    except AttributeError as exc:
        exc.__cause__ = exc
        assert _exception_outcome(exc, "check", {module.__file__}) == "candidate_error"


def test_exception_attribution_does_not_call_candidate_attribute_hooks():
    accessed = []

    class FixtureError(AssertionError):
        def __getattribute__(self, name):
            accessed.append(name)
            return super().__getattribute__(name)

    try:
        raise FixtureError("fixed framework fixture")
    except FixtureError as exc:
        assert _exception_outcome(exc, "check", set()) == "fail"
    assert accessed == []


def test_judge_error_while_handling_an_assertion_is_not_a_model_failure():
    try:
        try:
            raise AssertionError("check failure")
        except AssertionError:
            raise OSError("judge failed to process failure")
    except OSError as exc:
        assert _exception_outcome(exc, "check", set()) == "judge_error"


@pytest.mark.parametrize("outer_file,inner_file,expected", [
    ("/input/work/solution.py", "/input/runner.py", "candidate_error"),
    ("/input/work/solution.py", "/input/tests/checks.py", "judge_error"),
    ("/input/tests/checks.py", "/input/runner.py", "judge_error"),
])
def test_attribution_ignores_framework_frames(outer_file, inner_file, expected):
    def inner():
        raise PermissionError("fixed framework fixture")

    def outer():
        inner()

    filenames = {
        "test_attribution_ignores_framework_frames": "/input/runner.py",
        "outer": outer_file, "inner": inner_file,
    }
    # Only filename metadata is simulated; no task code is compiled or run.
    descriptor = types.SimpleNamespace(__get__=lambda code: filenames[code.co_name])
    try:
        outer()
    except PermissionError as exc:
        assert _exception_outcome(
            exc, "check", {"/input/work/solution.py"}, _filename=descriptor,
        ) == expected


@docker_only
@pytest.mark.parametrize("scenario", range(9))
def test_builtin_checks_cannot_be_disabled_by_wraps_patch(scenario, paths):
    task = load_builtin_tasks()[scenario]
    candidate = (
        "import functools\n"
        "def skip(fn):\n"
        "    def decorate(wrapper):\n"
        "        wrapper.__closure__[0].cell_contents = lambda: None\n"
        "        return wrapper\n"
        "    return decorate\n"
        "functools.wraps = skip\n"
    )
    result = judge_attempt(
        task, make_attempt(task, raw_output=render_answer(task, {"solution.py": candidate})), paths,
    )
    assert result.status == "completed"
    assert result.quality_score == 0 and result.all_required_passed is False
    assert result.checks and not any(check.passed for check in result.checks)
    assert "candidate_runtime" in result.gate_failures


@docker_only
@pytest.mark.parametrize("mutation", [
    "import functools\nfunctools.wraps = lambda fn: lambda wrapper: wrapper",
    "import builtins\nbuiltins.all = lambda values: True",
    "import ast\nast.walk = lambda tree: ()",
    "import inspect\ninspect.signature = lambda fn: None",
    "import unittest\nunittest.TestCase.assertRaises = lambda *a: None",
    "import unittest\nunittest.case._AssertRaisesContext.__exit__ = lambda *a: True",
    "import functools\nfunctools.wraps.__globals__['wraps'] = lambda *a: None",
    "import ast\ntry:\n    ast.walk = lambda tree: ()\nexcept PermissionError:\n    pass",
])
def test_candidate_dependency_mutation_is_never_a_passing_check(mutation, paths):
    checks = (
        "import solution\n\n"
        "def check_value():\n"
        "    solution.value()\n"
        "    assert True\n"
    )
    # Mutate during the check, not only during module import.
    candidate = "def value():\n" + "\n".join("    " + line for line in mutation.splitlines())
    task = make_task(test_files={"checks.py": checks})
    result = judge_attempt(task, make_attempt(task, raw_output=f"```python\n{candidate}\n```"), paths)
    assert result.status == "completed" and result.quality_score == 0
    assert result.gate_failures == ("candidate_runtime",)


@docker_only
@pytest.mark.parametrize("body,candidate,expected", [
    ("raise OSError('judge helper')", "value = 1", "judge_error"),
    ("missing_judge_name()", "value = 1", "judge_error"),
    ("assert solution.value == 1", "value = 0", "completed"),
    ("solution.value()", "def value():\n    raise ValueError('candidate')", "completed"),
    ("solution.value()", "pass", "completed"),
    ("solution.value(helper)", "def value(helper):\n    helper()", "judge_error"),
])
def test_docker_legacy_case_exception_attribution(body, candidate, expected, paths):
    checks = (
        "import functools\nimport solution\n\n"
        "def _case(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def run():\n"
        "        try:\n"
        "            fn()\n"
        "        except Exception as exc:\n"
        "            raise AssertionError(str(exc)) from exc\n"
        "    return run\n\n"
        "def helper():\n"
        "    raise OSError('trusted callback failed')\n\n"
        "@_case\n"
        "def check_value():\n"
        f"    {body}\n"
    )
    task = make_task(test_files={"checks.py": checks})
    result = judge_attempt(task, make_attempt(task, raw_output=f"```python\n{candidate}\n```"), paths)
    assert result.status == expected
    assert result.quality_score == (None if expected == "judge_error" else 0)
