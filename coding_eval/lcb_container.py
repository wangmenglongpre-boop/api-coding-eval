"""Docker-only benchmark controller; candidates never receive expected outputs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def stdin_equal(prediction: str, expected: str) -> bool:
    actual_lines = [line.strip() for line in prediction.strip().split("\n")]
    expected_lines = [line.strip() for line in expected.strip().split("\n")]
    if len(actual_lines) != len(expected_lines):
        return False
    for actual, target in zip(actual_lines, expected_lines):
        if actual == target:
            continue
        try:
            if [Decimal(token) for token in actual.split()] == [Decimal(token) for token in target.split()]:
                continue
        except (InvalidOperation, ValueError):
            pass
        return False
    return True


def main():
    sys.path.insert(0, "/input")
    from container_check import _install_process_filter, run_command
    from lcb_test_data import read_test_data

    phase, check = sys.argv[1:3]
    key = bytes.fromhex(sys.stdin.readline().strip())
    sys.stdin.close()
    _install_process_filter()
    outcome, detail = "judge_error", "Benchmark setup failure"
    try:
        source = Path("/input/work/solution.py").read_text(encoding="utf-8")
        job = read_test_data(json.loads(Path("/input/tests/cases.json").read_text(encoding="utf-8")))
        try:
            compile(source, "solution.py", "exec")
        except SyntaxError:
            outcome, detail = "candidate_error", "Candidate syntax failure"
        else:
            if phase == "preflight":
                outcome, detail = "pass", "Benchmark syntax and data verified"
            elif phase == "check" and check == "all_tests":
                outcome, detail = "pass", f"All {len(job['cases'])} original tests passed"
                for index, case in enumerate(job["cases"]):
                    verdict = run_command(
                        [sys.executable, "-I", "-B", "/input/candidate.py", job["func_name"] or ""],
                        timeout=6, input_data=case["input"].encode("utf-8"),
                        output_limit=min(16 * 1024 * 1024, max(
                            65536, len(case["output"].encode("utf-8")) * 2 + 1024,
                        )),
                    )
                    if verdict.timed_out or verdict.returncode:
                        outcome = "candidate_error"
                        detail = f"Original test {index}: candidate timeout/runtime failure"
                        os.write(2, verdict.stderr[:2048])
                        break
                    try:
                        prediction = verdict.stdout.decode("utf-8")
                        matched = (json.loads(prediction) == json.loads(case["output"])
                                   if job["func_name"] else stdin_equal(prediction, case["output"]))
                    except (ValueError, UnicodeError, RecursionError):
                        matched = False
                    if not matched:
                        outcome, detail = "fail", f"Original test {index}: wrong answer"
                        break
    except Exception:
        outcome, detail = "judge_error", "Benchmark controller setup/execution failure"
    payload = json.dumps(
        {"phase": phase, "check": check, "outcome": outcome, "detail": detail},
        sort_keys=True, separators=(",", ":"),
    )
    envelope = json.dumps({
        "payload": payload,
        "signature": hmac.new(key, payload.encode(), hashlib.sha256).hexdigest(),
    })
    os.write(1, envelope.encode())


if __name__ == "__main__":
    main()
