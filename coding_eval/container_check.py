"""Docker-only execution and the trusted, container-side check harness.

No task source is imported, compiled or AST-parsed by the host. The harness is
also a script: Docker invokes this file with an isolated Python interpreter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import selectors
import subprocess
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

LOG_LIMIT = 64 * 1024
JUDGE_VERSION = "docker-python-v3"
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
_IMAGE = re.compile(
    r"(?:(?:docker\.io/)?library/)?python:3\.11(?:\.\d+)?(?:-slim(?:-bookworm|-trixie)?)?"
    r"(?:@sha256:[a-f0-9]{64})?\Z"
)


class DockerUnavailable(RuntimeError):
    """No usable daemon or approved image; never fall back to host execution."""


class SandboxError(RuntimeError):
    """The execution infrastructure, rather than the candidate, failed."""


def _protect_check_dependencies(modules):
    """Bounded monkeypatch protection, not a general Python security boundary.

    Freeze exported module bindings and retain identity snapshots of Python
    classes/functions. Internal mutable stdlib caches remain usable. Direct
    interpreter/object-model bypasses are outside this defense's guarantee.
    """
    module_type, mapping_proxy = types.ModuleType, types.MappingProxyType
    function_type, class_type = types.FunctionType, type
    get_vars, is_instance, as_tuple = vars, isinstance, tuple
    deny = PermissionError
    snapshots = []
    violated = False

    class ProtectedModule(module_type):
        def __getattribute__(self, name):
            value = module_type.__getattribute__(self, name)
            return mapping_proxy(value) if name == "__dict__" else value

        def __setattr__(self, name, value):
            nonlocal violated
            if not name.startswith("_") or name in {"__dict__", "__class__"}:
                violated = True
                raise deny("Trusted check dependency is read-only")
            module_type.__setattr__(self, name, value)

        def __delattr__(self, name):
            nonlocal violated
            violated = True
            raise deny("Trusted check dependency is read-only")

    for module in modules:
        namespace = module_type.__getattribute__(module, "__dict__")
        bindings = as_tuple((k, v) for k, v in namespace.items() if not k.startswith("_"))
        snapshots.append((namespace, bindings))
        for value in as_tuple(namespace.values()):
            if is_instance(value, class_type) and value.__module__ == module.__name__:
                members = get_vars(value)
                snapshots.append((members, as_tuple(members.items())))
        module_type.__setattr__(module, "__class__", ProtectedModule)
    # Capture identity checks before any candidate runs; don't invoke overloaded
    # equality, candidate repr, or mutable builtins while verifying integrity.
    functions = []
    for _, bindings in snapshots:
        for _, value in bindings:
            if is_instance(value, function_type):
                functions.append((value, value.__code__, value.__defaults__, value.__kwdefaults__))
    snapshots, functions = as_tuple(snapshots), as_tuple(functions)

    def verify():
        if violated:
            raise deny("Candidate tried to mutate a trusted dependency")
        for namespace, bindings in snapshots:
            for name, value in bindings:
                if namespace.get(name) is not value:
                    raise deny("Candidate mutated a trusted dependency")
        for function, code, defaults, kwdefaults in functions:
            if (
                function.__code__ is not code or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
            ):
                raise deny("Candidate mutated a trusted function")

    return verify


def _exception_outcome(
    exc, stage, candidate_paths, *,
    _isinstance=isinstance, _id=id, _set=set,
    _assertion=AssertionError, _attribute=AttributeError,
    _tb=types.TracebackType.tb_frame, _code=types.FrameType.f_code,
    _filename=types.CodeType.co_filename, _module=types.ModuleType,
    _traceback=BaseException.__traceback__, _cause=BaseException.__cause__,
    _context=BaseException.__context__, _obj=AttributeError.obj,
):
    """Attribute the innermost input frame, including legacy _case wrappers."""
    seen = _set()
    while _isinstance(exc, _assertion) and _id(exc) not in seen:
        seen.add(_id(exc))
        cause = _cause.__get__(exc)
        if cause is None:
            cause = _context.__get__(exc)
        if cause is None or _id(cause) in seen:
            break
        exc = cause
    origin = None
    # Attribution temporarily permits runner frame access; never dispatch an
    # exception subclass's attribute hooks in that privileged interval.
    tb = _traceback.__get__(exc)
    while tb is not None:
        filename = _filename.__get__(_code.__get__(_tb.__get__(tb)))
        if filename in candidate_paths or filename.startswith(("/input/tests/", "/input/work/")):
            origin = filename
        tb = tb.tb_next
    if origin in candidate_paths:
        return "candidate_error"
    if stage == "check" and _isinstance(exc, _assertion):
        return "fail"
    # Missing candidate APIs raise in checks.py, without a candidate frame.
    if _isinstance(exc, _attribute):
        obj = _obj.__get__(exc)
        if _isinstance(obj, _module):
            namespace = _module.__getattribute__(obj, "__dict__")
            if namespace.get("__file__") in candidate_paths:
                return "candidate_error"
    return "judge_error"


def _deferred_candidate_imports(candidate_names, importer):
    """Build trusted definitions before ordinary `import solution` executes."""
    module_type, get_attr = types.ModuleType, getattr
    pending = {}

    class DeferredModule(module_type):
        def __getattr__(self, name):
            return get_attr(importer(self.__name__), name)

    def import_for_checks(name, globals=None, locals=None, fromlist=(), level=0):
        if name in candidate_names and not fromlist and level == 0:
            if name not in pending:
                pending[name] = DeferredModule(name)
            return pending[name]
        return importer(name, globals, locals, fromlist, level)

    def resolve(namespace):
        for name, proxy in pending.items():
            actual = importer(name)
            for binding, value in tuple(namespace.items()):
                if value is proxy:
                    namespace[binding] = actual

    return import_for_checks, resolve


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


@dataclass(frozen=True)
class ContainerResult:
    outcome: str
    detail: str
    log: str = ""


def run_command(
    argv: list[str], *, timeout: float, input_data: bytes | None = None,
) -> CommandResult:
    """Drain both pipes with bounded storage, including when output is hostile."""
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated: set[str] = set()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        if input_data is not None:
            try:
                process.stdin.write(input_data)
                process.stdin.close()
            except BrokenPipeError:
                pass
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    process.kill()
                    break
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    remaining = LOG_LIMIT - len(buffer)
                    buffer.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated.add(key.data)
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    for name in truncated:
        marker = b"\n[log truncated]\n"
        buffers[name][-len(marker):] = marker
    return CommandResult(
        process.returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]), timed_out,
    )


class DockerSandbox:
    """One immutable local image ID, fresh restricted container for every call."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.image_id: str | None = None

    def prepare(self) -> str:
        if self.image_id is not None:
            return self.image_id
        pinned = _IMAGE_ID.fullmatch(self.runtime.image) is not None
        if not pinned and not _IMAGE.fullmatch(self.runtime.image):
            raise SandboxError("Only official Python 3.11 images are allowed")
        if self.runtime.dependencies:
            raise SandboxError("Dependencies need a prebuilt approved image; runtime installs are disabled")
        try:
            info = run_command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)
            if info.returncode or info.timed_out or not info.stdout.strip():
                raise DockerUnavailable("Docker daemon unavailable")
            inspect = run_command(
                ["docker", "image", "inspect", "--format", "{{.Id}}", self.runtime.image],
                timeout=8,
            )
            if inspect.returncode and not inspect.timed_out and not pinned:
                pull = run_command(["docker", "pull", self.runtime.image], timeout=180)
                if pull.returncode or pull.timed_out:
                    raise DockerUnavailable("Official Python 3.11 image unavailable")
                inspect = run_command(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", self.runtime.image],
                    timeout=8,
                )
            image_id = inspect.stdout.decode("ascii", errors="replace").strip()
            if inspect.returncode or inspect.timed_out or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                raise DockerUnavailable("Cannot resolve Docker image to an immutable local ID")
            if pinned and image_id != self.runtime.image:
                raise SandboxError("Resolved image does not match the frozen runtime")
            self.image_id = image_id
            return image_id
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerUnavailable("Docker CLI unavailable") from exc

    def command(self, directory: Path, name: str, phase: str, check: str) -> list[str]:
        if self.image_id is None:
            raise SandboxError("Image must be resolved before execution")
        memory = min(self.runtime.memory_mb, 512)
        return [
            "docker", "run", "--name", name, "--pull", "never", "-i",
            "--network", "none", "--ipc", "none", "--read-only",
            "--user", "65534:65534", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--cpus", str(min(self.runtime.cpus, 2)),
            "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
            "--pids-limit", str(min(self.runtime.pids_limit, 128)),
            "--ulimit", "core=0", "--ulimit", "nofile=128:128",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--log-driver", "none", "--workdir", "/tmp",
            "--mount", f"type=bind,src={directory.resolve()},dst=/input,readonly",
            "--env", "HOME=/tmp", "--env", "LANG=C.UTF-8",
            "--entrypoint", "/usr/local/bin/python",
            self.image_id, "-I", "-B", "/input/runner.py", phase, check,
        ]

    def execute(self, directory: Path, phase: str, check: str = "") -> ContainerResult:
        self.prepare()
        name = f"coding-eval-{uuid4().hex}"
        key = secrets.token_bytes(32)
        cleanup_error = None
        try:
            try:
                result = run_command(
                    self.command(directory, name, phase, check),
                    timeout=min(self.runtime.timeout_seconds, 120),
                    input_data=key.hex().encode("ascii") + b"\n",
                )
                state = run_command(
                    ["docker", "inspect", "--format", "{{json .State}}", name], timeout=8,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise DockerUnavailable("Docker execution unavailable") from exc
            if state.returncode or state.timed_out:
                raise DockerUnavailable("Container state unavailable")
            try:
                metadata = json.loads(state.stdout)
            except (ValueError, UnicodeError) as exc:
                raise SandboxError("Invalid Docker container state") from exc
            if not isinstance(metadata, dict) or metadata.get("Error"):
                raise SandboxError("Docker failed to start the sandbox")
            log = result.stderr.decode("utf-8", errors="replace")
            if result.timed_out or metadata.get("OOMKilled"):
                outcome = "judge_error" if phase == "preflight" else "candidate_error"
                return ContainerResult(outcome, "Container timeout or memory limit", log)
            if result.returncode in (125, 126, 127):
                raise SandboxError("Docker failed to invoke the runner")
            try:
                envelope = json.loads(result.stdout)
                payload = envelope["payload"]
                signature = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, envelope["signature"]):
                    raise ValueError("Invalid signature")
                verdict = json.loads(payload)
                if (
                    verdict["phase"] != phase or verdict["check"] != check
                    or verdict["outcome"] not in {"pass", "fail", "candidate_error", "judge_error"}
                    or not isinstance(verdict["detail"], str)
                    or result.returncode != 0
                ):
                    raise ValueError("Invalid verdict")
                return ContainerResult(verdict["outcome"], verdict["detail"], log)
            except (ValueError, KeyError, TypeError, UnicodeError):
                # Exit codes and candidate stdout never constitute a passing check.
                outcome = "judge_error" if phase == "preflight" else "candidate_error"
                return ContainerResult(outcome, "Runner did not produce an authenticated verdict", log)
        finally:
            try:
                removed = run_command(["docker", "rm", "--force", name], timeout=10)
                if removed.returncode or removed.timed_out:
                    cleanup_error = SandboxError(f"Sandbox cleanup failed: {name}")
            except (OSError, subprocess.SubprocessError):
                cleanup_error = SandboxError(f"Sandbox cleanup unavailable: {name}")
            if cleanup_error is not None and sys.exc_info()[0] is None:
                raise cleanup_error


def _install_process_filter() -> None:
    """Protect the verdict process from its subprocesses, without disabling them."""
    import ctypes
    import platform

    architecture = platform.machine()
    if architecture == "x86_64":
        arch, denied = 0xC000003E, (86, 88, 101, 265, 266, 310, 311)
    elif architecture == "aarch64":
        arch, denied = 0xC00000B7, (36, 37, 117, 270, 271)
    else:
        raise RuntimeError("Unsupported sandbox architecture")

    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    # BPF loads seccomp_data.arch/nr; deny x32 as well as cross-process memory
    # access. PR_SET_DUMPABLE also denies /proc/<runner>/mem to child processes.
    instructions = [
        (0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x00050001),
    ]
    for number in denied:
        instructions.extend(((0x15, 0, 1, number), (0x06, 0, 0, 0x00050001)))
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    array = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), array)
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(4, 0, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0)
        or libc.prctl(22, 2, ctypes.byref(program), 0, 0)
    ):
        raise RuntimeError("Cannot install the sandbox process filter")


def _container_main() -> None:
    """Only called inside Docker; stdout carries an authenticated control record.

    The key and bound verdict writer are kept off module globals. Audit guards
    deny frame/GC/native-memory introspection and process replacement, which
    otherwise let Python code steal the verdict key or replace the runner.
    These guards are defense in depth, not a replacement for the OS sandbox.
    """
    import ast
    import builtins
    import importlib
    import sys
    import threading
    import types

    phase, check = sys.argv[1:3]
    key = bytes.fromhex(sys.stdin.readline().strip())
    sys.stdin.close()
    control = os.dup(1)
    os.dup2(2, 1)
    writer = os.write
    # Bind the immutable OpenSSL methods, not mutable Python HMAC functions.
    signer = hmac.new(key, digestmod=hashlib.sha256)._hmac
    if signer is None:
        raise RuntimeError("OpenSSL HMAC is required by the trusted runner")
    sign_update, sign_digest = signer.update, signer.hexdigest
    dumps = json.dumps
    base_exception = BaseException
    syntax_error, type_error = SyntaxError, TypeError
    string, object_type = str, type
    exec_code, compile_code = exec, compile
    function_type = types.FunctionType
    get_ident, runner_thread = threading.get_ident, threading.get_ident()
    getcwd = os.getcwd
    protect_dependencies = _protect_check_dependencies
    classify_exception = _exception_outcome
    deferred_imports = _deferred_candidate_imports
    attributing = False
    verdicts = {}
    for status, description in (
        ("pass", "Check completed successfully"),
        ("fail", "Check raised AssertionError"),
        ("candidate_error", "Candidate syntax or execution failure"),
        ("judge_error", "Check missing or judge setup/execution failure"),
    ):
        payload = dumps(
            {"phase": phase, "check": check, "outcome": status, "detail": description},
            sort_keys=True, separators=(",", ":"),
        )
        verdicts[status] = (payload.encode("utf-8"), dumps(payload).encode("utf-8"))
    task = json.loads(Path("/input/job.json").read_text(encoding="utf-8"))
    candidate_paths = {"/input/work/" + path for path in task["candidate_paths"]}
    work = Path("/input/work")
    sys.path.insert(0, str(work))
    sys.path.insert(1, "/input/tests")
    outcome, detail = "judge_error", "Runner setup failed"

    # Captures only immutable locals; it cannot be disabled by replacing sys.audit.
    def audit(event, args, _deny=PermissionError, _isinstance=isinstance, _str=str, _bytes=bytes):
        if event == "open" and _isinstance(args[0], (_str, _bytes)):
            path = args[0].decode("utf-8") if _isinstance(args[0], _bytes) else args[0]
            if not path.startswith("/"):
                # open's audit event omits dir_fd. Forbid upward relative paths
                # so directory descriptors cannot bypass the path boundary.
                if ".." in path.split("/"):
                    raise _deny("Relative parent traversal is forbidden")
                path = getcwd() + "/" + path
            parts = []
            for part in path.split("/"):
                if part == "..":
                    if parts:
                        parts.pop()
                elif part and part != ".":
                    parts.append(part)
            normalized = "/" + "/".join(parts)
            if not (
                # DEVNULL is a data sink used by the trusted C10 subprocess
                # runner, not a host resource or an introspection surface.
                normalized in {"/tmp", "/input", "/usr/local/lib", "/usr/lib", "/dev/null"}
                or normalized.startswith(("/tmp/", "/input/", "/usr/local/lib/", "/usr/lib/"))
            ):
                raise _deny("Sandbox file access is restricted to input, tmpfs and libraries")
        if (
            event in {
                "sys._getframe", "sys._current_frames", "sys._current_exceptions",
                "gc.get_objects", "gc.get_referrers", "gc.get_referents",
                "sys.settrace", "sys.setprofile", "sys.addaudithook",
                "os.fork", "os.forkpty", "os.exec", "os.posix_spawn",
                "os.system", "os.symlink", "os.link", "os.fwalk", "socket.__new__",
                "code.__new__", "function.__new__",
            }
            or event.startswith("ctypes.")
            or (event == "object.__setattr__" and args[1] in {
                "__code__", "__defaults__", "__kwdefaults__",
            })
            or (event == "object.__getattr__" and args[1] in {
                "f_code", "tb_frame", "gi_frame", "cr_frame", "ag_frame",
            }
                and not (attributing and get_ident() == runner_thread))
        ):
            raise _deny("Sandbox runner introspection/process access is forbidden")

    # Preflight executes no source. It reports test/setup syntax independently.
    try:
        if sys.version_info[:2] != (3, 11):
            raise RuntimeError("Python 3.11 required")
        test_source = Path("/input/tests/checks.py").read_text(encoding="utf-8")
        test_tree = ast.parse(test_source, filename="/input/tests/checks.py")
        definitions = {
            node.name: node for node in test_tree.body if isinstance(node, ast.FunctionDef)
        }
        for name in task["checks"]:
            if name not in definitions:
                raise ValueError("Missing top-level check: " + name)
            args = definitions[name].args
            if args.posonlyargs or args.args or args.kwonlyargs or args.vararg or args.kwarg:
                raise ValueError("Check must take no arguments: " + name)
        test_code = compile_code(test_tree, "/input/tests/checks.py", "exec")
        for path in work.rglob("*.py"):
            if str(path) not in candidate_paths:
                compile_code(path.read_text(encoding="utf-8"), str(path), "exec")
        try:
            for path in candidate_paths:
                compile_code(Path(path).read_text(encoding="utf-8"), path, "exec")
        except (syntax_error, ValueError) as exc:
            outcome, detail = "candidate_error", "Candidate syntax error: " + string(exc)[:2000]
        else:
            if phase == "preflight":
                outcome, detail = "pass", "Syntax and check definitions verified"
            elif phase == "check":
                # Warm common stdlib modules before frame guards: their own import
                # machinery may legitimately inspect frames (e.g. namedtuple).
                for module in (
                    "ast", "copy", "datetime", "decimal", "functools", "inspect",
                    "sqlite3", "sqlite3.dump", "tempfile", "threading", "unittest",
                    "unittest.mock", "json", "re", "math",
                ):
                    importlib.import_module(module)
                # tempfile lazily replaces its public `tempdir` binding on first
                # use. Resolve it before taking identity snapshots so legitimate
                # TemporaryDirectory checks do not look like candidate mutation.
                import tempfile
                tempfile.gettempdir()
                # unittest.assertRaises clears traceback frames as an optional
                # memory optimization. Each check exits immediately, so avoid
                # that introspection without altering its assertion semantics.
                import traceback
                traceback.clear_frames = lambda tb: None
                _install_process_filter()
                os.chdir(work)
                trusted_builtins = dict(vars(builtins))
                import_for_checks, resolve_candidates = deferred_imports(
                    {Path(path).stem for path in candidate_paths},
                    trusted_builtins["__import__"],
                )
                trusted_builtins["__import__"] = import_for_checks
                verify_dependencies = protect_dependencies([
                    importlib.import_module(name) for name in (
                        "builtins", "ast", "copy", "decimal", "functools", "inspect",
                        "sqlite3", "tempfile", "threading", "unittest", "unittest.case",
                        "unittest.util", "pathlib", "json", "re", "math",
                    )
                ])
                sys.addaudithook(audit)
                namespace = {
                    "__name__": "checks", "__file__": "/input/tests/checks.py",
                    "__builtins__": trusted_builtins,
                }
                stage = "setup"
                try:
                    exec_code(test_code, namespace)
                    function = namespace.get(check)
                    # Avoid inspect.signature after installing the frame guard.
                    if object_type(function) is not function_type:
                        raise type_error("Check is not a top-level function")
                    if function.__code__.co_argcount or function.__code__.co_kwonlyargcount:
                        raise type_error("Check must take no arguments")
                    if function.__code__.co_flags & (0x20 | 0x80 | 0x200):
                        raise type_error("Check must be synchronous, not a generator/coroutine")
                    if function.__code__.co_filename != "/input/tests/checks.py":
                        raise type_error("Check must be defined in checks.py")
                    resolve_candidates(namespace)
                    verify_dependencies()
                    stage = "check"
                    function()
                    outcome, detail = "pass", "Check returned without exception"
                except base_exception as exc:
                    attributing = True
                    try:
                        outcome = classify_exception(exc, stage, candidate_paths)
                    finally:
                        attributing = False
                    detail = stage + ": " + object_type(exc).__name__
                finally:
                    try:
                        verify_dependencies()
                    except base_exception:
                        outcome, detail = "candidate_error", "Trusted dependency mutation"
            else:
                raise ValueError("Unknown runner phase")
    except base_exception as exc:
        outcome, detail = "judge_error", "Setup: " + object_type(exc).__name__
    payload, encoded_payload = verdicts[outcome]
    writer(2, (detail[:2000] + "\n").encode("utf-8"))
    sign_update(payload)
    envelope = (
        b'{"payload":' + encoded_payload + b',"signature":"'
        + sign_digest().encode("ascii") + b'"}'
    )
    writer(control, envelope)


if __name__ == "__main__":
    # Do not leave the runner function reachable through __main__ during a check.
    globals().pop("_container_main")()
