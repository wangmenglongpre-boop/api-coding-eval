"""SQLite 公共存储；输入必须已脱敏，不承担请求和判分业务。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    AttemptResult, ReportVersion, RunPlan, RunStatus, canonical_json,
    content_hash, reject_secret_fields, utc_now, validate_relative_path,
    validate_transition,
)


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    artifacts: Path

    @classmethod
    def from_env(cls) -> AppPaths:
        root = Path(os.environ.get("CODING_EVAL_DATA_DIR", "data")).expanduser().resolve()
        return cls(root=root, database=root / "coding_eval.sqlite3", artifacts=root / "artifacts")

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def artifact_path(self, relative_path: str) -> Path:
        """返回受限产物路径；具体文件的写入由各业务模块负责。"""
        validate_relative_path(relative_path)
        path = (self.artifacts / relative_path).resolve()
        if not path.is_relative_to(self.artifacts.resolve()):
            raise ValueError("产物路径不能逃逸产物目录")
        return path


class Storage:
    """每次操作独立连接；可跨 Streamlit 会话使用，不持有全局连接。"""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        if str(database) == ":memory:":
            raise ValueError("Storage 使用文件数据库，不支持 :memory:")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    version_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (kind, object_id, version_hash)
                );
                CREATE TABLE IF NOT EXISTS object_heads (
                    kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    version_hash TEXT NOT NULL,
                    PRIMARY KEY (kind, object_id),
                    FOREIGN KEY (kind, object_id, version_hash)
                        REFERENCES objects(kind, object_id, version_hash)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    plan_hash TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    model_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sample_index INTEGER NOT NULL,
                    attempt_index INTEGER NOT NULL,
                    purpose TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(run_id, model_id, task_id, sample_index, attempt_index, purpose)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_id);
                CREATE INDEX IF NOT EXISTS reports_run ON reports(run_id);
            """)
            # 旧库按原插入顺序补齐指针；重开数据库不得重置已回选的版本。
            connection.execute("""
                INSERT OR IGNORE INTO object_heads (kind, object_id, version_hash)
                SELECT kind, object_id, version_hash FROM objects
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM objects GROUP BY kind, object_id
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        # Connection 的上下文管理器只提交事务，不负责关闭连接。
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, *args: Any) -> Any:
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()

        connection = sqlite3.connect(self.database, timeout=10, factory=ClosingConnection)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def save_object(self, kind: str, object_id: str, payload: dict[str, Any]) -> str:
        """追加不可变版本并将其设为当前版本；重复内容不新增历史记录。"""
        reject_secret_fields(payload)
        encoded = canonical_json(payload)
        version = content_hash(payload)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO objects VALUES (?, ?, ?, ?, ?)",
                (kind, object_id, version, encoded, utc_now().isoformat()),
            )
            # 历史写入和指针更新同事务提交，包括重新启用已有 hash 的配置。
            connection.execute("""
                INSERT INTO object_heads (kind, object_id, version_hash) VALUES (?, ?, ?)
                ON CONFLICT (kind, object_id)
                DO UPDATE SET version_hash=excluded.version_hash
            """, (kind, object_id, version))
        return version

    def get_object(self, kind: str, object_id: str, version_hash: str | None = None) -> dict[str, Any] | None:
        import json

        query = "SELECT payload_json FROM objects WHERE kind=? AND object_id=?"
        params: tuple[Any, ...] = (kind, object_id)
        if version_hash is not None:
            query += " AND version_hash=?"
            params += (version_hash,)
        else:
            query += (
                " AND version_hash=(SELECT version_hash FROM object_heads "
                "WHERE kind=? AND object_id=?)"
            )
            params += (kind, object_id)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_objects(self, kind: str) -> list[dict[str, Any]]:
        """返回所有版本；历史在前、当前在后，各组内按首次插入顺序。

        按 object_id 顺序覆盖可得到当前版本；报告仍能读取全部 judge 版本。
        created_at 始终是版本首次写入时间，不表示最后激活时间。
        """
        import json

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.object_id, o.version_hash, o.payload_json, o.created_at
                FROM objects AS o
                JOIN object_heads AS h ON h.kind=o.kind AND h.object_id=o.object_id
                WHERE o.kind=?
                ORDER BY (o.version_hash=h.version_hash), o.rowid""",
                (kind,),
            ).fetchall()
        return [dict(object_id=row["object_id"], version_hash=row["version_hash"],
                     payload=json.loads(row["payload_json"]), created_at=row["created_at"]) for row in rows]

    def save_run(self, plan: RunPlan) -> None:
        # 再解析可防止通过 model_copy 或嵌套 dict 修改绕过模型校验。
        plan = RunPlan.model_validate_json(plan.model_dump_json())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (plan.id, plan.version_hash, canonical_json(plan), RunStatus.QUEUED, utc_now().isoformat()),
            )

    def get_run(self, run_id: str) -> RunPlan | None:
        with self._connect() as connection:
            row = connection.execute("SELECT plan_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunPlan.model_validate_json(row["plan_json"]) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, plan_hash, status, updated_at FROM runs ORDER BY rowid DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run_status(self, run_id: str) -> RunStatus:
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunStatus(row["status"])

    def transition_run(self, run_id: str, expected: RunStatus, target: RunStatus) -> None:
        """比较并交换状态，防止并发界面或 worker 覆盖彼此的迁移。"""
        validate_transition(expected, target)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=? AND status=?",
                (target, utc_now().isoformat(), run_id, expected),
            )
            if cursor.rowcount != 1:
                raise ValueError("运行不存在或当前状态已发生变化")

    def save_attempt(self, result: AttemptResult) -> None:
        """只追加实际请求记录；重试必须使用新的 ID 和 attempt_index。"""
        result = AttemptResult.model_validate_json(result.model_dump_json())
        payload = result.model_dump(mode="json")
        reject_secret_fields(payload)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (result.id, result.run_id, result.model_id, result.task_id,
                 result.sample_index, result.attempt_index, result.purpose, canonical_json(payload)),
            )

    def list_attempts(self, run_id: str) -> list[AttemptResult]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,),
            ).fetchall()
        return [AttemptResult.model_validate_json(row["payload_json"]) for row in rows]

    def save_report(self, report: ReportVersion) -> None:
        report = ReportVersion.model_validate_json(report.model_dump_json())
        reject_secret_fields(report.model_dump(mode="json"))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reports VALUES (?, ?, ?)", (report.id, report.run_id, canonical_json(report)),
            )

    def list_reports(self, run_id: str) -> list[ReportVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM reports WHERE run_id=? ORDER BY rowid", (run_id,),
            ).fetchall()
        return [ReportVersion.model_validate_json(row["payload_json"]) for row in rows]
