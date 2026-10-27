"""Trusted child wrapper, invoked only inside Docker with a single test input."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Mirrors the upstream standard-library prelude, without numpy or remote code.
PRELUDE = "\n".join(
    f"from {name} import *\nimport {name}" for name in (
        "string", "re", "datetime", "collections", "heapq", "bisect", "copy", "math",
        "random", "statistics", "itertools", "functools", "operator", "io", "sys",
        "json", "builtins", "typing",
    )
)


def main():
    # Read trusted inputs before the candidate audit hook becomes active.
    function = sys.argv[1]
    source = Path("/input/work/solution.py").read_text(encoding="utf-8")
    compile_code, execute = compile, exec
    output_json = json.dumps
    if function:
        arguments = [json.loads(line) for line in sys.stdin.read().split("\n")]
        answer_fd = os.dup(1)
        os.dup2(2, 1)
        write_output = os.write
    else:
        arguments = None
    sys.setrecursionlimit(50000)
    sys.path.insert(0, "/input")
    from container_check import _install_process_filter
    _install_process_filter()
    os.chdir("/tmp")
    namespace = {"__name__": "__main__"}
    execute(compile_code(PRELUDE, "<standard-library-prelude>", "exec"), namespace)

    def audit(
        event, args, _deny=PermissionError, _isinstance=isinstance, _str=str, _bytes=bytes,
        _split=str.split, _join=str.join, _startswith=str.startswith, _cwd=os.getcwd,
        _decode=bytes.decode,
    ):
        if event == "open":
            path = args[0]
            # File descriptors could expose inherited runner/pipe handles.
            if not _isinstance(path, (_str, _bytes)):
                raise _deny("Candidate file-descriptor access denied")
            path = _decode(path, "utf-8") if _isinstance(path, _bytes) else path
            if not _startswith(path, "/"):
                path = _cwd() + "/" + path
            parts = []
            for part in _split(path, "/"):
                if part == "..":
                    if parts:
                        parts.pop()
                elif part and part != ".":
                    parts.append(part)
            normalized = "/" + _join("/", parts)
            if not (_startswith(normalized, ("/tmp/", "/usr/local/lib/", "/usr/lib/"))
                    or normalized == "/dev/null"):
                raise _deny("Candidate cannot read test or process files")
        if (event in {
            "os.fork", "os.forkpty", "os.exec", "os.posix_spawn", "os.system",
            "subprocess.Popen", "os.symlink", "os.link", "os.kill", "os.killpg",
            "socket.__new__", "sys.addaudithook", "sys._getframe", "sys._current_frames",
            "gc.get_objects", "gc.get_referrers", "gc.get_referents", "sys.settrace", "sys.setprofile",
        } or _startswith(event, "ctypes.")
                or (event == "object.__setattr__" and args[1] in {"__code__", "__defaults__", "__kwdefaults__"})
                or (event == "object.__getattr__" and args[1] in {
                    "f_code", "tb_frame", "gi_frame", "cr_frame", "ag_frame",
                })):
            raise _deny("Candidate process/introspection access denied")

    sys.addaudithook(audit)
    execute(compile_code(source, "solution.py", "exec"), namespace)
    if function:
        target = namespace["Solution"]() if "Solution" in namespace else None
        method = getattr(target, function) if target is not None else namespace[function]
        prediction = method(*arguments)
        if isinstance(prediction, tuple):
            prediction = list(prediction)
        # Parent independently compares output; no expected answer is in this process.
        write_output(answer_fd, output_json(
            prediction, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))


if __name__ == "__main__":
    main()
