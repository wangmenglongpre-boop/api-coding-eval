"""Pinned upstream data as inert JSON, a compressed catalog and shared sampling."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import zlib
from pathlib import Path
from typing import Callable

import httpx
from .lcb_test_data import decode_private, read_test_data, validate_cases

DATASET = "livecodebench/code_generation_lite"
RELEASE = "release_v6"
REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
SHARDS = {
    "test.jsonl": "2bd02b38beb48e8c46b5b9987095d999ff38cd8efc255ea5d58974317c48f63f",
    "test2.jsonl": "095df7c5daf15f882c51a9deb84085cff1e073495a5dbcf95015a564d485f3a3",
    "test3.jsonl": "28ed26cc83363ce3f1fe2d5fad9f8393077beb1907b167a31bd3b32f80801b79",
    "test4.jsonl": "d711138ddaebfcf5f8ec6a4283ee677298c0f5c5d374a235af92aaf0584510da",
    "test5.jsonl": "7f77571c2a6df0c2a72a3277650309f67e01e0008e18117e624633df53f81214",
    "test6.jsonl": "bb4c364f71921c4495a6ad15abe1a927350b720009f4933e2e71f8af0f6fd1f5",
}
DIFFICULTIES = ("easy", "medium", "hard")
GRADER = "lcb-docker-v1"


def catalog_path(root: Path) -> Path:
    return root / "livecodebench" / f"{REVISION}.sqlite3"


def prepare_catalog(root: Path, progress: Callable[[str], None] | None = None) -> Path:
    """Resume only verified shards; failed imports never become a complete catalog."""
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=60) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS shards (name TEXT PRIMARY KEY, sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, difficulty TEXT NOT NULL, shard TEXT NOT NULL,
                payload BLOB NOT NULL, row_sha TEXT NOT NULL);
        """)
        for name, expected in SHARDS.items():
            if db.execute("SELECT sha FROM shards WHERE name=?", (name,)).fetchone() == (expected,):
                continue
            download = path.parent / (name + ".partial")
            digest = hashlib.sha256()
            if progress:
                progress(f"下载并校验 {name}（只在首次准备时下载约 4.5 GB 原始数据）")
            with httpx.stream(
                "GET", f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{name}",
                follow_redirects=True, timeout=httpx.Timeout(60, connect=30),
            ) as response, download.open("wb") as output:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                download.unlink(missing_ok=True)
                raise ValueError(f"{name}: upstream SHA256 mismatch")
            # Transaction covers both question rows and verified-shard certificate.
            with db:
                db.execute("DELETE FROM questions WHERE shard=?", (name,))
                with download.open("rb") as source:
                    for line in source:
                        row = json.loads(line)
                        difficulty = row["difficulty"].lower()
                        if difficulty not in DIFFICULTIES:
                            raise ValueError(f"{name}: unknown difficulty")
                        identifier = row["platform"] + ":" + row["question_id"]
                        db.execute("INSERT INTO questions VALUES (?,?,?,?,?)", (
                            identifier, difficulty, name, zlib.compress(line, 6),
                            hashlib.sha256(line).hexdigest(),
                        ))
                db.execute("INSERT OR REPLACE INTO shards VALUES (?,?)", (name, expected))
            download.unlink(missing_ok=True)
            if progress:
                progress(f"{name} 已校验并缓存")
    catalog_counts(root)
    return path


def catalog_counts(root: Path) -> dict[str, int]:
    path = catalog_path(root)
    if not path.exists():
        raise ValueError("尚未准备固定版 LiveCodeBench 题库")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60) as db:
        if dict(db.execute("SELECT name,sha FROM shards")) != SHARDS:
            raise ValueError("LiveCodeBench 题库准备未完成，请继续准备")
        return dict(db.execute("SELECT difficulty,count(*) FROM questions GROUP BY difficulty"))


def sample_rows(root: Path, counts: dict[str, int], seed: int) -> list[dict]:
    """Independent difficulty pools, stable ordering, no replacement."""
    available = catalog_counts(root)
    if (set(counts) != set(DIFFICULTIES)
            or any(type(n) is not int or n < 0 or n > available.get(d, 0) for d, n in counts.items())
            or not sum(counts.values())):
        raise ValueError("抽样数量必须在各难度题池范围内，且至少选择一道题")
    rng = random.Random(seed)
    rows = []
    with sqlite3.connect(f"file:{catalog_path(root)}?mode=ro", uri=True) as db:
        for difficulty in DIFFICULTIES:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM questions WHERE difficulty=? ORDER BY id", (difficulty,),
            )]
            for identifier in sorted(rng.sample(ids, counts[difficulty])):
                blob, expected = db.execute(
                    "SELECT payload,row_sha FROM questions WHERE id=?", (identifier,),
                ).fetchone()
                raw = zlib.decompress(blob)
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("Cached question hash mismatch")
                rows.append(json.loads(raw))
    return rows


def row_to_task(row: dict, *, test_count: int | None = None):
    from .models import (
        BenchmarkInfo, Dimension, OutputFormat, RubricItem, RuntimeConfig,
        ScoringConfig, TaskDefinition, Weight, canonical_json, content_hash,
    )

    public = validate_cases(json.loads(row["public_test_cases"]), allow_empty=True)
    function = json.loads(row["metadata"]).get("func_name")
    envelope = {"func_name": function, "public": public, "private": row["private_test_cases"]}
    if test_count is None:
        test_count = len(read_test_data(envelope)["cases"])
    # Keep upstream compressed private payload in the snapshot; expand only to
    # validate or grade, never repeatedly during serialization and plan hashing.
    case_source = canonical_json(envelope)
    difficulty = row["difficulty"].lower()
    requirements = row["question_content"]
    if row["starter_code"]:
        requirements += "\n\nPython starter code:\n" + row["starter_code"]
    requirements += "\n\nPublic examples:\n" + canonical_json(public)
    requirements += (
        "\n\nUse Python 3.11 standard library. "
        + (f"Implement Solution.{function} (or a top-level {function} function)."
           if function else "Read standard input and write the answer to standard output.")
    )
    return TaskDefinition(
        id="LCB-" + content_hash([row["platform"], row["question_id"]])[:20],
        scenario_id="LCB-" + difficulty, title=row["question_title"],
        description="LiveCodeBench 原始题与全部原始测试；数据/运行环境验证不代表参考解验证。",
        system_prompt="You solve programming contest problems. Return exactly one python fenced code block.",
        requirements=requirements, test_files={"cases.json": case_source},
        rubric=(RubricItem(check="all_tests", dimension=Dimension.FUNCTIONALITY,
                          description="全部原始公开与隐藏测试通过"),),
        output_format=OutputFormat(
            kind="files", allowed_paths=("solution.py",),
            instructions="Return solution.py as exactly one ```python code block, with no extra prose.",
        ),
        runtime=RuntimeConfig(timeout_seconds=120, memory_mb=512),
        scoring=ScoringConfig(template_id="livecodebench-pass-v1",
                              dimension_weights=(Weight(name="functionality", weight=1),)),
        source=f"{DATASET}@{REVISION}/{RELEASE}",
        benchmark=BenchmarkInfo(
            dataset=DATASET, release=RELEASE, revision=REVISION, platform=row["platform"],
            question_id=row["question_id"], contest_date=row["contest_date"], difficulty=difficulty,
            row_hash=content_hash(row), tests_hash=content_hash(case_source),
            test_count=test_count, func_name=function,
        ),
    )


def sampled_tasks(root: Path, counts: dict[str, int], seed: int):
    return [row_to_task(row) for row in sample_rows(root, counts, seed)]


def validate_benchmark(task, paths):
    """Certify data/schema and immutable runtime, not a nonexistent reference solution."""
    from .container_check import DockerSandbox
    from .judges import _task_contract
    from .models import TaskDefinition, canonical_json

    task = TaskDefinition.model_validate_json(task.model_dump_json())
    _task_contract(task)
    verify_original_task(task, paths.root)
    image = DockerSandbox(task.runtime).prepare()
    payload = task.model_dump(mode="json")
    payload["runtime"]["image"] = image
    payload.update(status="draft", validation_evidence=[], validated_content_hash=None)
    draft = TaskDefinition.model_validate(payload)
    receipt = paths.artifact_path(f"livecodebench/validation/{draft.validation_hash}.json")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(canonical_json({
        "kind": "original-test-schema-and-runtime", "validation_hash": draft.validation_hash,
        "benchmark": draft.benchmark.model_dump(mode="json"), "runtime_image": image,
        "reference_solution_validated": False,
    }), encoding="utf-8")
    payload.update(status="ready", validation_evidence=[str(receipt)],
                   validated_content_hash=draft.validation_hash)
    return TaskDefinition.model_validate(payload)


def verify_original_task(task, root):
    """A copied/imported benchmark cannot claim the pin for altered task content."""
    catalog_counts(root)
    identifier = task.benchmark.platform + ":" + task.benchmark.question_id
    with sqlite3.connect(f"file:{catalog_path(root)}?mode=ro", uri=True) as db:
        record = db.execute("SELECT payload,row_sha FROM questions WHERE id=?", (identifier,)).fetchone()
    if record is None:
        raise ValueError(f"{task.id}: original question missing from pinned catalog")
    raw = zlib.decompress(record[0])
    if hashlib.sha256(raw).hexdigest() != record[1]:
        raise ValueError(f"{task.id}: cached original question hash mismatch")
    original = row_to_task(json.loads(raw), test_count=task.benchmark.test_count)
    exclude = {"status", "validation_evidence", "validated_content_hash"}
    expected = original.model_dump(mode="json", exclude=exclude)
    actual = task.model_dump(mode="json", exclude=exclude)
    for value in (expected, actual):
        value["runtime"].pop("image")
        value["scoring"].pop("scenario_weights")
    if actual != expected:
        raise ValueError(f"{task.id}: benchmark content differs from the pinned original")


def create_sampled_plan(models, tasks, counts, seed, paths, **options):
    from .models import RunPlan, SamplingInfo, ScoringConfig, Weight
    from .plan_preview import create_plan

    total = sum(counts.values())
    scoring = ScoringConfig(
        template_id="livecodebench-pass-v1", dimension_weights=(Weight(name="functionality", weight=1),),
        scenario_weights=tuple(Weight(name="LCB-" + d, weight=n / total)
                               for d, n in counts.items() if n),
    )
    plan = create_plan(models, tasks, scoring, paths, random_seed=seed, **options)
    sampling = SamplingInfo(
        dataset_revision=REVISION, seed=seed, counts=counts, population=catalog_counts(paths.root),
        task_versions={s.object_id: s.version_hash for s in plan.task_snapshots},
    )
    return RunPlan.model_validate({**plan.model_dump(mode="json"), "sampling": sampling.model_dump(mode="json")})
