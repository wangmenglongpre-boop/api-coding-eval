"""公共数据契约；这里不执行请求、代码、判分或报告计算。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Mapping, Self
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
NonNegative = Annotated[float, Field(ge=0)]
Score = Annotated[float, Field(ge=0, le=100)]


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    """固定编码，禁止 NaN/Infinity，供快照和哈希共用。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1


class VersionedContract(Contract):
    @property
    def version_hash(self) -> str:
        # 哈希由内容计算，不能由调用方伪造；快照另存哈希。
        return content_hash(self)


class Dimension(StrEnum):
    FUNCTIONALITY = "functionality"
    ROBUSTNESS = "robustness"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    MAINTAINABILITY = "maintainability"


DIMENSION_NAMES = tuple(d.value for d in Dimension)
DIMENSION_LABELS = {
    "functionality": "功能正确性",
    "robustness": "健壮性与边界处理",
    "security": "安全与数据完整性",
    "compliance": "指令遵循与兼容性",
    "maintainability": "可维护性代理指标",
}
DEFAULT_DIMENSION_WEIGHTS = {
    "functionality": 0.50, "robustness": 0.20, "security": 0.15,
    "compliance": 0.10, "maintainability": 0.05,
}


class Weight(Contract):
    name: Identifier
    weight: Annotated[float, Field(gt=0, le=1)]


def validate_weights(weights: tuple[Weight, ...]) -> None:
    names = [item.name for item in weights]
    if not names or len(names) != len(set(names)):
        raise ValueError("权重名称不能为空或重复")
    if not math.isclose(sum(item.weight for item in weights), 1.0, abs_tol=1e-9, rel_tol=0):
        raise ValueError("权重之和必须为 1")


class ScoringConfig(VersionedContract):
    template_id: Identifier = "default-v1"
    dimension_weights: tuple[Weight, ...] = tuple(
        Weight(name=name, weight=value) for name, value in DEFAULT_DIMENSION_WEIGHTS.items()
    )
    scenario_weights: tuple[Weight, ...] = ()

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        validate_weights(self.dimension_weights)
        if not {w.name for w in self.dimension_weights} <= set(DIMENSION_NAMES):
            raise ValueError("未知评分维度")
        if self.scenario_weights:
            validate_weights(self.scenario_weights)
        return self


class ModelConfig(VersionedContract):
    id: Identifier
    display_name: Annotated[str, Field(min_length=1)]
    protocol: Literal["openai", "anthropic"]
    url: str
    url_mode: Literal["base", "full"] = "base"
    model_id: Annotated[str, Field(min_length=1)]
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = None
    provider: str = "unknown"
    visible_version: str = "unknown"
    enabled: bool = True
    timeout_seconds: Annotated[float, Field(gt=0)] = 120
    max_output_tokens: Annotated[int, Field(gt=0)] = 4096
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    capabilities: dict[str, bool | None] = Field(default_factory=dict)
    usage_mapping_version: str = "unknown"

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("模型 URL 必须为 HTTP(S) 地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("URL 不允许凭据、查询参数或片段")
        reject_secret_fields(self.parameters)
        return self


def reject_secret_fields(value: Any) -> None:
    """拒绝常见凭据字段；不能替代 API 层对响应、日志的脱敏。"""
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"apikey", "authorization", "password", "secret", "accesstoken", "headers"}:
                raise ValueError("持久化对象不允许凭据字段，请使用 api_key_env")
            reject_secret_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secret_fields(item)


class RubricItem(Contract):
    check: Identifier
    dimension: Dimension
    description: Annotated[str, Field(min_length=1)]
    points: Annotated[float, Field(gt=0)] = 1
    critical: bool = False
    required: bool = True
    kind: Literal["test", "static", "format"] = "test"


class OutputFormat(Contract):
    kind: Literal["files", "search_replace", "tests"]
    allowed_paths: tuple[str, ...]
    instructions: Annotated[str, Field(min_length=1)]
    extraction_version: str = "v1"

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        if not self.allowed_paths or len(self.allowed_paths) != len(set(self.allowed_paths)):
            raise ValueError("必须声明不重复的允许输出路径")
        for path in self.allowed_paths:
            validate_relative_path(path)
        return self


def validate_relative_path(path: str) -> None:
    parts = PurePosixPath(path)
    if (not path or parts.is_absolute() or "\\" in path or "\x00" in path
            or any(p in {"", ".", ".."} for p in path.split("/")) or ":" in path):
        raise ValueError("文件路径必须为安全的 POSIX 相对路径")


class RuntimeConfig(Contract):
    language: Literal["python"] = "python"
    python_version: Literal["3.11"] = "3.11"
    image: str = "python:3.11-slim"
    dependencies: tuple[str, ...] = ()
    timeout_seconds: Annotated[float, Field(gt=0)] = 30
    memory_mb: Annotated[int, Field(gt=0)] = 256
    cpus: Annotated[float, Field(gt=0)] = 1
    pids_limit: Annotated[int, Field(gt=0)] = 64


class TaskDefinition(VersionedContract):
    id: Identifier
    scenario_id: Identifier
    title: Annotated[str, Field(min_length=1)]
    description: str
    system_prompt: str
    requirements: Annotated[str, Field(min_length=1)]
    # 文件映射为相对路径 -> UTF-8 原文；仅 public_files 可进入模型请求。
    public_files: dict[str, str] = Field(default_factory=dict)
    reference_files: dict[str, str] = Field(default_factory=dict)
    test_files: dict[str, str] = Field(default_factory=dict)
    negative_files: dict[str, dict[str, str]] = Field(default_factory=dict)
    rubric: tuple[RubricItem, ...] = ()
    output_format: OutputFormat
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    source: str = "自建企业场景模拟题"
    status: Literal["draft", "ready"] = "draft"
    validation_evidence: tuple[str, ...] = ()
    validated_content_hash: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def validation_hash(self) -> str:
        return content_hash(self.model_dump(mode="json", exclude={
            "status", "validation_evidence", "validated_content_hash",
        }))

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        for files in (self.public_files, self.reference_files, self.test_files, *self.negative_files.values()):
            for path in files:
                validate_relative_path(path)
        checks = [item.check for item in self.rubric]
        if len(checks) != len(set(checks)):
            raise ValueError("每个 check 只能归属一个维度且只能计分一次")
        if self.ready:
            if not (self.reference_files and self.test_files and self.negative_files
                    and all(self.negative_files.values()) and self.rubric and self.validation_evidence):
                raise ValueError("ready 任务必须有参考解、测试、错误解、评分项与验证证据")
            if {item.dimension.value for item in self.rubric} != {w.name for w in self.scoring.dimension_weights}:
                raise ValueError("评分模板维度必须与实际评分项完全一致")
            if self.validated_content_hash != self.validation_hash:
                raise ValueError("验证证据未绑定当前任务内容")
        return self


class DatasetDefinition(VersionedContract):
    id: Identifier
    title: str
    description: str = ""
    task_versions: dict[str, str] = Field(default_factory=dict)


class Snapshot(Contract):
    object_type: Literal["model", "task"]
    object_id: Identifier
    version_hash: str
    payload_json: str

    @classmethod
    def capture(cls, value: ModelConfig | TaskDefinition) -> Snapshot:
        kind = "model" if isinstance(value, ModelConfig) else "task"
        return cls(object_type=kind, object_id=value.id,
                   version_hash=value.version_hash, payload_json=canonical_json(value))

    def restore(self) -> ModelConfig | TaskDefinition:
        contract = ModelConfig if self.object_type == "model" else TaskDefinition
        return contract.model_validate_json(self.payload_json)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        restored = self.restore()
        if restored.id != self.object_id or restored.version_hash != self.version_hash:
            raise ValueError("快照 ID 或内容哈希不匹配")
        return self


class RunPlan(VersionedContract):
    id: Identifier = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    model_snapshots: tuple[Snapshot, ...]
    task_snapshots: tuple[Snapshot, ...]
    scoring: ScoringConfig
    repeats: Annotated[int, Field(gt=0)] = 3
    concurrency: Annotated[int, Field(gt=0)] = 1
    random_seed: int = 0
    max_retries: Annotated[int, Field(ge=0)] = 0
    retry_delay_seconds: NonNegative = 0
    max_requests: Annotated[int, Field(gt=0)]
    budget_group: Identifier = "default"
    diagnostic: bool = False

    @property
    def expected_requests(self) -> int:
        return len(self.model_snapshots) * len(self.task_snapshots) * self.repeats

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        for kind, snapshots in (("model", self.model_snapshots), ("task", self.task_snapshots)):
            ids = [s.object_id for s in snapshots]
            if not ids or len(ids) != len(set(ids)) or any(s.object_type != kind for s in snapshots):
                raise ValueError("计划需要类型正确、非空且 ID 不重复的快照")
        tasks = [TaskDefinition.model_validate_json(s.payload_json) for s in self.task_snapshots]
        if any(not ModelConfig.model_validate_json(s.payload_json).enabled for s in self.model_snapshots):
            raise ValueError("禁用模型不能进入计划")
        if not self.diagnostic and any(not t.ready for t in tasks):
            raise ValueError("正式计划只能包含 ready 任务")
        if any(t.scoring.template_id != self.scoring.template_id
               or t.scoring.dimension_weights != self.scoring.dimension_weights for t in tasks):
            raise ValueError("任务与计划必须使用相同评分模板和维度权重")
        if {w.name for w in self.scoring.scenario_weights} != {t.scenario_id for t in tasks}:
            raise ValueError("场景权重必须完整覆盖计划中的场景")
        if self.max_requests < self.expected_requests * (1 + self.max_retries):
            raise ValueError("请求预算不足以覆盖样本及声明的重试上限")
        return self


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    allowed = {
        RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
        RunStatus.RUNNING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
        RunStatus.FAILED: {RunStatus.QUEUED},
        RunStatus.CANCELLED: {RunStatus.QUEUED},
        RunStatus.COMPLETED: set(),
    }
    if target not in allowed[current]:
        raise ValueError(f"非法状态迁移：{current} -> {target}")


class AttemptMetrics(Contract):
    first_content_seconds: NonNegative | None = None
    first_answer_seconds: NonNegative | None = None
    total_seconds: NonNegative | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_read_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_creation_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    reasoning_tokens: Annotated[int, Field(ge=0)] | None = None
    output_includes_reasoning: bool | None = None
    stream_complete: bool = False
    usage_mapping_version: str = "unknown"
    raw_usage: dict[str, JsonValue] = Field(default_factory=dict)


METRIC_NAMES = (
    "first_content_seconds", "first_answer_seconds", "total_seconds",
    "input_tokens", "cache_read_tokens", "cache_creation_tokens",
    "output_tokens", "reasoning_tokens",
)


class AttemptResult(Contract):
    id: Identifier = Field(default_factory=new_id)
    run_id: Identifier
    model_id: Identifier
    task_id: Identifier
    sample_index: Annotated[int, Field(ge=0)]
    attempt_index: Annotated[int, Field(ge=0)] = 0
    purpose: Literal["evaluation", "probe", "control"] = "evaluation"
    status: Literal["completed", "failed", "cancelled", "uncertain"]
    created_at: datetime = Field(default_factory=utc_now)
    request_hash: str | None = None
    protocol_mapping_version: str = "unknown"
    raw_output: str = ""
    raw_response: JsonValue = None
    metrics: AttemptMetrics = Field(default_factory=AttemptMetrics)
    error_code: str | None = None
    error_message: str | None = None
    diagnostics: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_paths: tuple[str, ...] = ()
    queue_seconds: NonNegative | None = None
    logical_task_seconds: NonNegative | None = None


class CheckResult(Contract):
    check: Identifier
    passed: bool
    evidence: str


class JudgeResult(Contract):
    id: Identifier = Field(default_factory=new_id)
    attempt_id: Identifier
    task_version_hash: str
    judge_version: str
    status: Literal["completed", "judge_error", "blocked"]
    checks: tuple[CheckResult, ...] = ()
    dimension_scores: dict[Dimension, Score] = Field(default_factory=dict)
    quality_score: Score | None = None
    all_required_passed: bool | None = None
    gate_failures: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    judge_seconds: NonNegative | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status != "completed" and (self.quality_score is not None or self.all_required_passed is not None):
            raise ValueError("评测器故障或 blocked 不是能力分")
        if self.status == "completed" and self.quality_score is None:
            raise ValueError("完成的判分必须有质量分")
        if self.gate_failures and self.quality_score not in (None, 0):
            raise ValueError("强制门槛失败时质量分必须为零")
        return self


class ReportVersion(Contract):
    id: Identifier = Field(default_factory=new_id)
    run_id: Identifier
    created_at: datetime = Field(default_factory=utc_now)
    scoring: ScoringConfig
    source_plan_hash: str
    parent_report_id: Identifier | None = None
    post_hoc: bool = False
    status: Literal["provisional", "complete", "invalid"] = "provisional"
    payload: dict[str, JsonValue] = Field(default_factory=dict)
