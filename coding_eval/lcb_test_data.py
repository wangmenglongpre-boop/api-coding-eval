"""Pure stdlib decoder shared by the host and Docker; no remote execution."""

import base64
import io
import json
import pickle
import zlib

MAX_CASE_BYTES = 256 * 1024 * 1024


class _JSONOnlyUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError("Private-test pickle may not load globals")

    def persistent_load(self, pid):
        raise ValueError("Private-test pickle may not load persistent objects")


def validate_cases(cases, *, allow_empty=False):
    if not isinstance(cases, list) or not cases and not allow_empty:
        raise ValueError("Benchmark needs original test cases")
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"input", "output", "testtype"}
                or not isinstance(case["input"], str) or not isinstance(case["output"], str)
                or case["testtype"] not in {"stdin", "functional"}):
            raise ValueError("Invalid original test-case schema")
    return cases


def decode_private(value):
    try:
        cases = json.loads(value)
    except json.JSONDecodeError:
        compressed = base64.b64decode(value, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_CASE_BYTES + 1)
        if len(raw) > MAX_CASE_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError("Private-test decompression limit or framing error")
        source = _JSONOnlyUnpickler(io.BytesIO(raw)).load()
        if not isinstance(source, str):
            raise ValueError("Private-test pickle must contain only a JSON string")
        del raw, compressed, decoder
        cases = json.loads(source)
    return validate_cases(cases, allow_empty=True)


def read_test_data(value):
    if (not isinstance(value, dict) or set(value) != {"func_name", "public", "private"}
            or not isinstance(value["private"], str)):
        raise ValueError("Invalid benchmark test envelope")
    cases = validate_cases(value["public"], allow_empty=True) + decode_private(value["private"])
    validate_cases(cases)
    function = value["func_name"]
    if any(case["testtype"] != ("functional" if function else "stdin") for case in cases):
        raise ValueError("Original test types disagree with func_name")
    if function:
        for case in cases:
            for line in case["input"].split("\n"):
                json.loads(line)
            json.loads(case["output"])
    return {"func_name": function, "cases": cases}
