"""Fresh, bounded UI reads; never cache mutable run state or alter evidence."""

import json

from .models import AttemptResult, ModelConfig, ReportVersion, TaskDefinition
from .storage import Storage


def current_objects(storage: Storage, kind: str) -> list[dict]:
    with storage._connect() as connection:
        rows = connection.execute("""
            SELECT o.object_id, o.version_hash, o.payload_json, o.created_at
            FROM object_heads h JOIN objects o
            ON o.kind=h.kind AND o.object_id=h.object_id AND o.version_hash=h.version_hash
            WHERE h.kind=? ORDER BY o.object_id
        """, (kind,)).fetchall()
    return [{"object_id": row["object_id"], "version_hash": row["version_hash"],
             "created_at": row["created_at"], "payload": json.loads(row["payload_json"])}
            for row in rows]


def current_models(storage: Storage) -> list[ModelConfig]:
    return [ModelConfig.model_validate(row["payload"]) for row in current_objects(storage, "model")]


def current_tasks(storage: Storage) -> list[TaskDefinition]:
    return [TaskDefinition.model_validate(row["payload"]) for row in current_objects(storage, "task")]


def attempt_summaries(storage: Storage, run_id: str) -> list[AttemptResult]:
    with storage._connect() as connection:
        rows = connection.execute("""
            SELECT json_remove(payload_json, '$.raw_output', '$.raw_response', '$.metrics.raw_usage')
            FROM attempts WHERE run_id=? ORDER BY rowid
        """, (run_id,)).fetchall()
    return [AttemptResult.model_validate_json(row[0]) for row in rows]


def attempt_detail(storage: Storage, run_id: str, attempt_id: str) -> AttemptResult | None:
    with storage._connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM attempts WHERE run_id=? AND attempt_id=?",
            (run_id, attempt_id),
        ).fetchone()
    return AttemptResult.model_validate_json(row[0]) if row else None


def current_judges(storage: Storage, run_id: str) -> dict[str, dict]:
    with storage._connect() as connection:
        rows = connection.execute("""
            SELECT o.object_id, o.payload_json FROM attempts a
            JOIN object_heads h ON h.kind='judge' AND h.object_id=a.attempt_id
            JOIN objects o ON o.kind=h.kind AND o.object_id=h.object_id AND o.version_hash=h.version_hash
            WHERE a.run_id=?
        """, (run_id,)).fetchall()
    return {row["object_id"]: json.loads(row["payload_json"]) for row in rows}


def report_index(storage: Storage, run_id: str | None = None) -> list[dict]:
    with storage._connect() as connection:
        rows = connection.execute("""
            SELECT report_id, run_id,
            json_extract(payload_json, '$.created_at') created_at,
            json_extract(payload_json, '$.status') status,
            json_extract(payload_json, '$.post_hoc') post_hoc,
            json_extract(payload_json, '$.payload.comparison_key') comparison_key
            FROM reports
        """ + (" WHERE run_id=?" if run_id is not None else "") + " ORDER BY rowid",
            (run_id,) if run_id is not None else (),
        ).fetchall()
    return [dict(row) for row in rows]


def report_detail(storage: Storage, run_id: str, report_id: str) -> ReportVersion | None:
    with storage._connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM reports WHERE run_id=? AND report_id=?",
            (run_id, report_id),
        ).fetchone()
    return ReportVersion.model_validate_json(row[0]) if row else None


def run_summary(storage: Storage, plan, attempts, judges) -> dict:
    observed = {(a.model_id, a.task_id, a.sample_index) for a in attempts}
    intents = [r for r in current_objects(storage, "request_intent")
               if r["payload"]["run_id"] == plan.id and r["payload"].get("purpose", "evaluation") == "evaluation"]
    epoch = storage.get_object("model_dispatch_epoch", plan.id)
    return {
        "run_id": plan.id, "status": storage.get_run_status(plan.id).value,
        "plan_hash": plan.version_hash, "expected_samples": plan.expected_requests,
        "observed_samples": len(observed), "remaining_samples": plan.expected_requests - len(observed),
        "attempts": len(attempts), "reserved_requests": len(intents), "max_requests": plan.max_requests,
        "uncertain": sum(a.status == "uncertain" for a in attempts),
        "judge_pending_or_invalid": sum(not judges.get(a.id) or judges[a.id]["status"] != "completed"
                                       for a in attempts),
        "diagnostic": plan.diagnostic,
        "blocked_models": sorted({r["payload"]["model_id"]
                                  for r in current_objects(storage, "model_dispatch_block")
                                  if r["payload"]["run_id"] == plan.id and r["payload"].get("epoch") == epoch}),
    }
