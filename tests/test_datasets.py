"""Trusted framework tests; dataset source is never imported or executed here."""

import ast
import io
import json
import stat
import struct
import zipfile

import pytest

from coding_eval import datasets
from coding_eval.datasets import (
    as_draft, export_task, import_task, list_tasks, load_builtin_tasks,
    parse_task_json, save_task, validation_issues,
)
from coding_eval.models import DIMENSION_NAMES, TaskDefinition
from coding_eval.storage import Storage


@pytest.fixture
def task():
    return load_builtin_tasks()[0]


def _zip(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _entries(task):
    with zipfile.ZipFile(io.BytesIO(export_task(task))) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_builtin_catalog_contract_and_fresh_objects():
    tasks = load_builtin_tasks()
    assert [task.scenario_id for task in tasks] == [f"C{i:02}" for i in range(1, 11)]
    assert len({task.id for task in tasks}) == 10
    for task in tasks:
        assert task.status == "draft"
        assert not task.validation_evidence and task.validated_content_hash is None
        assert task.source == "自建企业场景模拟题"
        assert task.system_prompt and task.requirements and task.description
        assert task.reference_files and task.negative_files
        assert set(task.test_files) == {"checks.py"}
        assert {item.dimension.value for item in task.rubric} == set(DIMENSION_NAMES)
        assert len(task.rubric) == 5
        assert validation_issues(task) == []
        assert task.runtime.python_version == "3.11" and task.runtime.dependencies == ()
        assert task.output_format.allowed_paths == (
            ("test_solution.py",) if task.id == "C10" else ("solution.py",)
        )
        for files in task.negative_files.values():
            assert set(files) == set(task.output_format.allowed_paths)
            assert files != task.reference_files
    tasks[0].public_files["solution.py"] = "changed"
    assert load_builtin_tasks()[0].public_files["solution.py"] != "changed"


@pytest.mark.parametrize("task_index", range(10))
def test_sources_are_syntactically_valid_with_independent_checks(task_index):
    # AST inspection is inert: never compile, exec, import or call these sources.
    task = load_builtin_tasks()[task_index]
    groups = [task.public_files, task.reference_files, task.test_files, *task.negative_files.values()]
    for files in groups:
        for name, text in files.items():
            if name.endswith(".py"):
                ast.parse(text, filename=f"{task.id}/{name}")
    tree = ast.parse(task.test_files["checks.py"])
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert set(functions) >= {item.check for item in task.rubric}
    for item in task.rubric:
        args = functions[item.check].args
        assert not (args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg)
    assert all(item.kind == "test" for item in task.rubric)


def test_special_scenario_protocols():
    tasks = {task.id: task for task in load_builtin_tasks()}
    assert tasks["C04"].output_format.kind == "search_replace"
    assert "<<<<<<< SEARCH" in tasks["C04"].output_format.instructions
    assert "threading.Event()" in tasks["C02"].test_files["checks.py"]
    assert "release.set()" in tasks["C02"].test_files["checks.py"]
    task = tasks["C10"]
    assert task.output_format.kind == "tests"
    assert set(task.reference_files) == {"test_solution.py"}
    assert "shipping_fee" in task.public_files["solution.py"]
    tree = ast.parse(task.test_files["checks.py"])
    baseline = next(
        node.value.value for node in tree.body
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BASELINE" for target in node.targets
        )
    )
    assert baseline == task.public_files["solution.py"]
    assert "subprocess.run" in task.test_files["checks.py"]
    assert "TemporaryDirectory" in task.test_files["checks.py"]


@pytest.mark.parametrize("task_index", range(10))
def test_zip_roundtrip_exact_utf8_and_hash(task_index):
    task = load_builtin_tasks()[task_index]
    content = export_task(task)
    restored = import_task(content)
    assert restored == task
    assert restored.version_hash == task.version_hash
    assert content == export_task(task)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        metadata = json.loads(archive.read("task.json"))
        assert metadata == task.model_dump(mode="json")
        assert archive.read("tests/checks.py").decode("utf-8") == task.test_files["checks.py"]


def test_import_clears_untrusted_ready_claims(task):
    entries = _entries(task)
    metadata = json.loads(entries["task.json"])
    metadata.update(
        status="ready", validation_evidence=["UNTRUSTED-CLAIM"],
        validated_content_hash="not-a-validation",
    )
    entries["task.json"] = json.dumps(metadata).encode()
    restored = import_task(_zip(entries.items()))
    assert restored == task
    assert not restored.ready


def test_import_export_never_execute_sources(task, tmp_path):
    marker = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    value = task.model_dump(mode="json")
    value.update(
        public_files={"solution.py": source},
        reference_files={"solution.py": source},
        test_files={"checks.py": source},
        negative_files={"trap": {"solution.py": source}},
    )
    restored = import_task(export_task(TaskDefinition.model_validate(value)))
    assert restored.public_files["solution.py"] == source
    assert not marker.exists()


@pytest.mark.parametrize("name", [
    "../escape", "/absolute", "C:/windows", "a\\b", "a/../b", "a//b",
    "./a", "a/./b", "folder/", "",
])
def test_import_rejects_unsafe_paths(name):
    if not name:
        with pytest.raises((ValueError, IndexError)):
            import_task(_zip([(name, b"x")]))
    else:
        with pytest.raises(ValueError):
            import_task(_zip([(name, b"x")]))


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o700])
def test_import_rejects_links_and_special_files(mode):
    info = zipfile.ZipInfo("unsafe")
    info.create_system = 3
    info.external_attr = mode << 16
    with pytest.raises(ValueError, match="普通文件"):
        import_task(_zip([(info, b"../outside")]))


@pytest.mark.parametrize("names", [
    ("task.json", "task.json"),
    ("Task.json", "task.json"),
    ("caf\u00e9.py", "cafe\u0301.py"),
])
def test_import_rejects_duplicate_and_aliased_paths(names):
    if names[0] == names[1]:
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive = _zip([(name, b"{}") for name in names])
    else:
        archive = _zip([(name, b"{}") for name in names])
    with pytest.raises(ValueError, match="重复"):
        import_task(archive)


def test_import_rejects_nul_truncation():
    content = _zip([("badXname", b"x")]).replace(b"badXname", b"bad\x00name")
    with pytest.raises(ValueError):
        import_task(content)


@pytest.mark.parametrize("mutation,match", [
    ("missing", "缺失"), ("extra", "未声明"), ("mismatch", "不一致"),
])
def test_import_rejects_metadata_file_disagreement(task, mutation, match):
    entries = _entries(task)
    if mutation == "missing":
        del entries["public/solution.py"]
    elif mutation == "extra":
        entries["extra.py"] = b""
    else:
        entries["public/solution.py"] += b"# changed\n"
    with pytest.raises(ValueError, match=match):
        import_task(_zip(entries.items()))


@pytest.mark.parametrize("content", [b"not a ZIP", b"", b"PK\x03\x04"])
def test_import_rejects_broken_zip(content):
    with pytest.raises(ValueError, match="ZIP"):
        import_task(content)


def test_import_rejects_corrupt_crc():
    content = _zip([("task.json", b'{"x":123}')])
    content = content.replace(b'{"x":123}', b'{"x":456}')
    with pytest.raises(ValueError, match="ZIP"):
        import_task(content)


def test_import_rejects_encryption():
    content = bytearray(_zip([("task.json", b"{}")]))
    local = content.index(b"PK\x03\x04")
    central = content.index(b"PK\x01\x02")
    struct.pack_into("<H", content, local + 6, 1)
    struct.pack_into("<H", content, central + 8, 1)
    with pytest.raises(ValueError, match="加密"):
        import_task(bytes(content))


def test_import_rejects_unsupported_compression():
    content = _zip([("task.json", b"{}")], zipfile.ZIP_BZIP2)
    with pytest.raises(ValueError, match="压缩算法"):
        import_task(content)


@pytest.mark.parametrize("metadata", [
    b'{"id":"a","id":"b"}', b'[]', b'\xff', b'{',
    b'{"schema_version":99}', b'{"public_files":{"../escape":"x"}}',
])
def test_import_rejects_invalid_metadata(metadata):
    with pytest.raises(ValueError):
        import_task(_zip([("task.json", metadata)]))


def test_import_rejects_missing_metadata():
    with pytest.raises(ValueError, match="task.json"):
        import_task(_zip([("public/solution.py", b"pass\n")]))


def test_import_type_check():
    with pytest.raises(TypeError, match="bytes"):
        import_task("not bytes")


@pytest.mark.parametrize("limit,match", [
    ("MAX_ARCHIVE_BYTES", "压缩包"), ("MAX_FILE_BYTES", "单文件"),
    ("MAX_TOTAL_BYTES", "总大小"), ("MAX_FILES", "文件数"),
])
def test_archive_limits_apply_to_import_and_export(task, monkeypatch, limit, match):
    archive = export_task(task)
    monkeypatch.setattr(datasets, limit, 1)
    with pytest.raises(ValueError, match=match):
        import_task(archive)
    with pytest.raises(ValueError, match=match):
        export_task(task)


def test_compression_bomb_ratio_rejected():
    content = _zip([("large.txt", b"a" * 500000)], zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="压缩比"):
        import_task(content)


def test_deflated_zip_is_supported(task):
    assert import_task(_zip(_entries(task).items(), zipfile.ZIP_DEFLATED)) == task


@pytest.mark.parametrize("names", [("a.py", "A.py"), ("a/../b.py", "x.py")])
def test_export_checks_paths_even_after_nested_mutation(task, names):
    task.public_files.clear()
    task.public_files.update(dict.fromkeys(names, "pass\n"))
    with pytest.raises(ValueError):
        export_task(task)


def test_export_rejects_nested_negative_name(task):
    task.negative_files["nested/name"] = {"solution.py": "pass\n"}
    with pytest.raises(ValueError, match="负解名称"):
        export_task(task)


def test_json_parse_and_edit_reset(task):
    assert parse_task_json(task.model_dump_json()) == task
    value = task.model_dump(mode="json")
    value.update(status="ready", validation_evidence=["UNTRUSTED-CLAIM"], validated_content_hash="invalid")
    value["requirements"] += " 新需求"
    with pytest.raises(ValueError):
        parse_task_json(json.dumps(value))
    edited = parse_task_json(json.dumps(value), reset_validation=True)
    assert edited.requirements.endswith("新需求") and not edited.ready
    assert edited.validation_evidence == () and edited.validated_content_hash is None
    assert as_draft(edited) == edited
    with pytest.raises(ValueError, match="重复"):
        parse_task_json('{"id":"a","id":"b"}')
    with pytest.raises(ValueError):
        parse_task_json(b"\xff")
    with pytest.raises(ValueError):
        parse_task_json("[]", reset_validation=True)


def test_storage_latest_version_history_and_idempotency(task, tmp_path):
    storage = Storage(tmp_path / "tasks.sqlite")
    assert list_tasks(storage) == []
    original_hash = save_task(storage, task)
    assert original_hash == task.version_hash
    assert save_task(storage, task) == original_hash
    assert len(storage.list_objects("task")) == 1
    payload = task.model_dump(mode="json")
    payload["requirements"] += " 新版要求"
    changed = TaskDefinition.model_validate(payload)
    changed_hash = save_task(storage, changed)
    assert changed_hash != original_hash
    other = load_builtin_tasks()[1]
    save_task(storage, other)
    assert list_tasks(storage) == [changed, other]
    assert storage.get_object("task", task.id, original_hash) == task.model_dump(mode="json")
    assert storage.get_object("task", task.id, changed_hash) == changed.model_dump(mode="json")
    assert len(storage.list_objects("task")) == 3


def test_save_revalidates_nested_mutations(task, tmp_path):
    storage = Storage(tmp_path / "tasks.sqlite")
    task.public_files["../unsafe"] = "pass\n"
    with pytest.raises(ValueError):
        save_task(storage, task)
    assert list_tasks(storage) == []


def test_structural_diagnostics_report_incomplete_draft(task):
    value = task.model_dump(mode="json")
    value.update(
        public_files={}, reference_files={}, test_files={},
        negative_files={"empty": {}}, rubric=[], system_prompt="",
    )
    draft = TaskDefinition.model_validate(value)
    issues = validation_issues(draft)
    assert {"缺少公开文件", "缺少参考解", "缺少隐藏测试", "缺少评分项", "缺少系统提示词", "存在空负解"} <= set(issues)
    assert any("维度" in issue for issue in issues)
    assert any("允许输出路径" in issue for issue in issues)
    value["negative_files"] = {}
    assert "缺少负解" in validation_issues(TaskDefinition.model_validate(value))


def test_seed_loader_checks_catalog_completeness(task, tmp_path, monkeypatch):
    path = tmp_path / "seed_tasks.json"
    path.write_text(json.dumps([task.model_dump(mode="json")]), encoding="utf-8")
    monkeypatch.setattr(datasets, "SEED_PATH", path)
    with pytest.raises(ValueError, match="C01-C10"):
        load_builtin_tasks()
