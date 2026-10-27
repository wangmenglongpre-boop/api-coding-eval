"""Offline pinned-data verification and optional Docker smoke; never calls APIs."""

import argparse
import hashlib
import json
import sqlite3
import zlib

from coding_eval.judges import _task_contract, judge_attempt, validate_task
from coding_eval.livecodebench import (
    DIFFICULTIES, REVISION, catalog_counts, catalog_path, row_to_task, sampled_tasks,
)
from coding_eval.models import AttemptResult
from coding_eval.storage import AppPaths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Verify every original row and full test schema")
    parser.add_argument("--docker-smoke", action="store_true", help="Grade handwritten fixtures on official tests")
    options = parser.parse_args()
    paths = AppPaths.from_env()
    population = catalog_counts(paths.root)
    print({"revision": REVISION, "population": population}, flush=True)
    if options.all:
        checked = 0
        with sqlite3.connect(catalog_path(paths.root)) as db:
            for identifier, payload, expected in db.execute("SELECT id,payload,row_sha FROM questions ORDER BY id"):
                raw = zlib.decompress(payload)
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError(f"{identifier}: cached row hash mismatch")
                try:
                    task = row_to_task(json.loads(raw))
                    _task_contract(task)
                except Exception as exc:
                    raise ValueError(f"{identifier}: original test contract invalid ({type(exc).__name__})") from None
                checked += 1
                if checked % 100 == 0:
                    print(f"validated {checked} original questions", flush=True)
        print(f"ALL ORIGINAL QUESTIONS VALIDATED: {checked}", flush=True)
    tasks = sampled_tasks(paths.root, dict.fromkeys(DIFFICULTIES, 10), 20261027)
    for task in tasks:
        _task_contract(task)
    print({"sample_seed": 20261027, "sample_count": len(tasks),
           "unique_questions": len({t.id for t in tasks})}, flush=True)
    if options.docker_smoke:
        candidates = {
            "codeforces:1873_A": (
                "import sys\nlines=sys.stdin.read().split()\n"
                "for s in lines[1:]: print('YES' if sum(a!=b for a,b in zip(s,'abc'))<=2 else 'NO')",
                "print('NO')",
            ),
            "leetcode:2727": (
                "class Solution:\n def countSeniors(self,details: List[str])->int:\n"
                "  return sum(int(s[11:13])>60 for s in details)",
                "class Solution:\n def countSeniors(self,details): return 0",
            ),
        }
        with sqlite3.connect(catalog_path(paths.root)) as db:
            for identifier, sources in candidates.items():
                blob = db.execute("SELECT payload FROM questions WHERE id=?", (identifier,)).fetchone()[0]
                task = validate_task(row_to_task(json.loads(zlib.decompress(blob))), paths)
                for index, source in enumerate(sources):
                    attempt = AttemptResult(
                        run_id="lcb-offline-smoke", model_id="handwritten-fixture", task_id=task.id,
                        sample_index=index, status="completed", raw_output=f"```python\n{source}\n```",
                    )
                    verdict = judge_attempt(task, attempt, paths)
                    expected = 100 if index == 0 else 0
                    print({"question": identifier, "original_tests": task.benchmark.test_count,
                           "fixture": index, "status": verdict.status, "score": verdict.quality_score},
                          flush=True)
                    if (verdict.status, verdict.quality_score) != ("completed", expected):
                        raise ValueError("Official-test Docker smoke failed; inspect judge artifacts")


if __name__ == "__main__":
    main()
