"""Task catalogs and bounded ZIP interchange. Never execute dataset code."""

from __future__ import annotations

import io
import json
import stat
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import Any

from .models import TaskDefinition, canonical_json, validate_relative_path
from .storage import Storage

SEED_PATH = Path(__file__).resolve().parent.parent / "datasets" / "seed_tasks.json"
REFERENCE_PATH = SEED_PATH.with_name("reference_tasks.json")
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 256
MAX_COMPRESSION_RATIO = 200


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def parse_task_json(data: str | bytes, *, reset_validation: bool = False) -> TaskDefinition:
    """Parse data only, rejecting ambiguous duplicate JSON keys."""
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        if reset_validation:
            if not isinstance(value, dict):
                raise ValueError("任务 JSON 必须是对象")
            value.update(status="draft", validation_evidence=[], validated_content_hash=None)
        return TaskDefinition.model_validate(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("任务 JSON 无效或嵌套过深") from exc


def as_draft(task: TaskDefinition) -> TaskDefinition:
    value = task.model_dump(mode="json")
    value.update(status="draft", validation_evidence=[], validated_content_hash=None)
    return TaskDefinition.model_validate(value)


def load_builtin_tasks() -> list[TaskDefinition]:
    """Read fresh objects on each call; seed code remains inert text."""
    values = json.loads(SEED_PATH.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    tasks = [as_draft(TaskDefinition.model_validate(value)) for value in values]
    if len(tasks) != 10 or {task.scenario_id for task in tasks} != {
        f"C{i:02}" for i in range(1, 11)
    } or len({task.id for task in tasks}) != 10:
        raise ValueError("内置题库必须包含 C01-C10 各一个任务")
    return tasks


def load_reference_tasks() -> list[TaskDefinition]:
    """Compatibility entry point; UI imports the independent catalog directly."""
    from .reference_datasets import load_reference_tasks as load
    return load(REFERENCE_PATH)


def _archive_files(task: TaskDefinition) -> dict[str, bytes]:
    task = TaskDefinition.model_validate_json(task.model_dump_json())
    files = {"task.json": canonical_json(task).encode("utf-8")}
    groups = [
        ("public", task.public_files),
        ("reference", task.reference_files),
        ("tests", task.test_files),
    ]
    for name, mapping in task.negative_files.items():
        validate_relative_path(name)
        if "/" in name:
            raise ValueError("负解名称必须为单个路径段")
        groups.append((f"negative/{name}", mapping))
    for prefix, mapping in groups:
        for path, source in mapping.items():
            files[f"{prefix}/{path}"] = source.encode("utf-8")
    _check_file_limits(files)
    return files


def _check_file_limits(files: dict[str, bytes]) -> None:
    if len(files) > MAX_FILES:
        raise ValueError("ZIP 文件数超限")
    aliases: set[str] = set()
    for name, data in files.items():
        validate_relative_path(name)
        alias = unicodedata.normalize("NFC", name).casefold()
        if alias in aliases:
            raise ValueError("ZIP 路径重复或存在大小写别名")
        aliases.add(alias)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("ZIP 单文件大小超限")
    if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise ValueError("ZIP 展开总大小超限")


def export_task(task: TaskDefinition) -> bytes:
    """Export metadata plus exact UTF-8 source files without executing them."""
    files = _archive_files(task)
    stream = io.BytesIO()
    # Stored entries avoid compression-bomb ambiguity and produce stable exports.
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    result = stream.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP 压缩包大小超限")
    return result


def import_task(data: bytes) -> TaskDefinition:
    """Import an untrusted ZIP in memory, clearing all claimed validation."""
    if not isinstance(data, bytes):
        raise TypeError("ZIP 输入必须为 bytes")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP 压缩包大小超限")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise ValueError("ZIP 文件数超限")
            names: set[str] = set()
            total = 0
            for info in infos:
                validate_relative_path(info.orig_filename)
                if info.filename != info.orig_filename:
                    raise ValueError("ZIP 路径被截断")
                alias = unicodedata.normalize("NFC", info.filename).casefold()
                if alias in names:
                    raise ValueError("ZIP 路径重复或存在大小写别名")
                names.add(alias)
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError("ZIP 只允许普通文件，禁止符号链接和设备")
                if info.flag_bits & 1:
                    raise ValueError("不支持加密 ZIP")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("不支持此 ZIP 压缩算法")
                if info.file_size > MAX_FILE_BYTES:
                    raise ValueError("ZIP 单文件大小超限")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("ZIP 展开总大小超限")
                if info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO:
                    raise ValueError("ZIP 压缩比超限")
            files: dict[str, bytes] = {}
            for info in infos:
                with archive.open(info) as source:
                    body = source.read(MAX_FILE_BYTES + 1)
                if len(body) != info.file_size or len(body) > MAX_FILE_BYTES:
                    raise ValueError("ZIP 文件实际大小不匹配或超限")
                files[info.filename] = body
            if "task.json" not in files:
                raise ValueError("ZIP 缺少 task.json")
            # Evidence from another environment is never accepted as readiness.
            task = parse_task_json(files["task.json"].decode("utf-8"), reset_validation=True)
            expected = _archive_files(task)
            if set(files) != set(expected):
                raise ValueError("ZIP 代码文件缺失或包含未声明文件")
            if any(files[name] != body for name, body in expected.items() if name != "task.json"):
                raise ValueError("ZIP 代码文件与元数据原文不一致")
            return task
    except (zipfile.BadZipFile, zipfile.LargeZipFile, UnicodeError,
            json.JSONDecodeError, RecursionError, RuntimeError, NotImplementedError, EOFError,
            zlib.error) as exc:
        raise ValueError("无效或不受支持的任务 ZIP") from exc


def list_tasks(storage: Storage) -> list[TaskDefinition]:
    """Return one latest persisted snapshot per ID, retaining history in storage."""
    latest: dict[str, TaskDefinition] = {}
    for row in storage.list_objects("task"):
        latest[row["object_id"]] = TaskDefinition.model_validate(row["payload"])
    return sorted(latest.values(), key=lambda task: (task.scenario_id, task.id))


def save_task(storage: Storage, task: TaskDefinition) -> str:
    """Append a validated snapshot; ready evidence must come from the judge."""
    task = TaskDefinition.model_validate_json(task.model_dump_json())
    return storage.save_object("task", task.id, task.model_dump(mode="json"))


def validation_issues(task: TaskDefinition) -> list[str]:
    """Structural diagnostics only, never a claim of executable validation."""
    issues = []
    for name, value in (
        ("公开文件", task.public_files), ("参考解", task.reference_files),
        ("隐藏测试", task.test_files), ("负解", task.negative_files),
        ("评分项", task.rubric), ("系统提示词", task.system_prompt),
    ):
        if not value:
            issues.append(f"缺少{name}")
    if any(not files for files in task.negative_files.values()):
        issues.append("存在空负解")
    if {item.dimension.value for item in task.rubric} != {
        weight.name for weight in task.scoring.dimension_weights
    }:
        issues.append("评分项未完整覆盖评分模板维度")
    if set(task.reference_files) != set(task.output_format.allowed_paths):
        issues.append("参考解文件必须恰好覆盖允许输出路径")
    return issues
