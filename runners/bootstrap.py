"""Standalone, dependency-free child process wrapper for Neo sandboxes."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

_PROTOCOL = 1
_MAX_JOB_BYTES = 64 * 1024
_MAX_CREDENTIAL_BYTES = 256 * 1024
_MAX_SCRIPT_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_BUNDLE_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
_MAX_BUNDLE_MEMBERS = 4096
_MAX_ENV_FIELDS = 64
_MAX_ENV_FIELD_BYTES = 16 * 1024
_MAX_ENV_TOTAL_BYTES = 64 * 1024
_MIN_EXIT_CODE = -(2**31)
_MAX_EXIT_CODE = 2**31 - 1
_MAX_DURATION_MS = 300_000

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_HEX_64 = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_RESERVED_ENV_NAMES = frozenset({"PATH", "HOME", "SHELL", "ENV", "BASH_ENV", "IFS"})
_RESERVED_ENV_PREFIXES = ("PYTHON", "NODE_", "LD_", "DYLD_")
_NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib", ".node")
_NATIVE_MAGICS = (
    b"\x7fELF",
    b"\x00asm",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
)
_BUNDLE_RESERVED_TOP_LEVEL = frozenset(
    {
        "artifact.zip",
        "bootstrap.py",
        "credentials.json",
        "job.json",
        "payload",
        "script.js",
        "script.py",
    }
)
_BUSINESS_STATUSES = frozenset(
    {
        "success",
        "already_signed",
        "failed",
        "needs_auth",
        "unknown",
        "cancelled",
        "timeout",
        "config_error",
    }
)
_PROCESS_STATUSES = frozenset(
    {"exited", "timeout", "start_error", "cancelled", "interrupted"}
)
_REASON_CODES = frozenset(
    {
        "reported_result",
        "no_business_evidence",
        "output_limit",
        "timeout",
        "runner_error",
        "cancelled",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "protocol",
        "process_status",
        "exit_code",
        "business_status",
        "reason_code",
        "duration_ms",
    }
)
_JOB_FIELDS = frozenset(
    {
        "protocol",
        "runtime",
        "sha256",
        "timeout_seconds",
        "result_adapter",
        "artifact_format",
    }
)


class _RunnerError(Exception):
    """A deliberately detail-free error safe to collapse into an envelope."""


class _OutputCollector:
    def __init__(self) -> None:
        self.data = bytearray()
        self.exceeded = False

    def add(self, chunk: bytes) -> None:
        remaining = _MAX_OUTPUT_BYTES - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.exceeded = True


class _Subreaper:
    """Adopt and reap orphaned descendants where Linux makes that possible."""

    _PR_SET_CHILD_SUBREAPER = 36
    _PR_GET_CHILD_SUBREAPER = 37

    def __init__(self) -> None:
        self._libc: Any | None = None
        self._previous = 0
        self._changed = False

    def enable(self) -> None:
        if not sys.platform.startswith("linux"):
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            previous = ctypes.c_int()
            if (
                libc.prctl(
                    self._PR_GET_CHILD_SUBREAPER,
                    ctypes.byref(previous),
                    0,
                    0,
                    0,
                )
                != 0
            ):
                return
            self._libc = libc
            self._previous = previous.value
            if (
                previous.value == 0
                and libc.prctl(self._PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
            ):
                self._changed = True
        except (AttributeError, OSError, TypeError, ValueError):
            self._libc = None

    def restore(self) -> None:
        if self._changed and self._libc is not None:
            try:
                self._libc.prctl(
                    self._PR_SET_CHILD_SUBREAPER,
                    self._previous,
                    0,
                    0,
                    0,
                )
            except (AttributeError, OSError, TypeError, ValueError):
                pass


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _envelope(
    process_status: str,
    exit_code: int | None,
    business_status: str,
    reason_code: str,
    duration_ms: int,
) -> dict[str, object]:
    return {
        "protocol": _PROTOCOL,
        "process_status": process_status,
        "exit_code": exit_code,
        "business_status": business_status,
        "reason_code": reason_code,
        "duration_ms": max(0, duration_ms),
    }


def validate_result(value: object) -> dict[str, object]:
    """Validate and safely copy one runner result envelope."""

    if not isinstance(value, dict) or frozenset(value) != _RESULT_FIELDS:
        raise ValueError("runner result is invalid")

    protocol = value.get("protocol")
    process_status = value.get("process_status")
    exit_code = value.get("exit_code")
    business_status = value.get("business_status")
    reason_code = value.get("reason_code")
    duration_ms = value.get("duration_ms")

    if not _is_int(protocol) or protocol != _PROTOCOL:
        raise ValueError("runner result is invalid")
    if not isinstance(process_status, str) or process_status not in _PROCESS_STATUSES:
        raise ValueError("runner result is invalid")
    if exit_code is not None and (
        not _is_int(exit_code)
        or exit_code < _MIN_EXIT_CODE
        or exit_code > _MAX_EXIT_CODE
    ):
        raise ValueError("runner result is invalid")
    if (
        not isinstance(business_status, str)
        or business_status not in _BUSINESS_STATUSES
    ):
        raise ValueError("runner result is invalid")
    if not isinstance(reason_code, str) or reason_code not in _REASON_CODES:
        raise ValueError("runner result is invalid")
    if not _is_int(duration_ms) or duration_ms < 0 or duration_ms > _MAX_DURATION_MS:
        raise ValueError("runner result is invalid")

    if process_status == "exited":
        if exit_code is None or reason_code not in {
            "reported_result",
            "no_business_evidence",
            "output_limit",
        }:
            raise ValueError("runner result is invalid")
        if (
            reason_code in {"no_business_evidence", "output_limit"}
            and business_status != "unknown"
        ):
            raise ValueError("runner result is invalid")
    elif process_status == "timeout":
        if (
            exit_code is not None
            or business_status != "timeout"
            or reason_code != "timeout"
        ):
            raise ValueError("runner result is invalid")
    elif process_status == "cancelled":
        if (
            exit_code is not None
            or business_status != "cancelled"
            or reason_code != "cancelled"
        ):
            raise ValueError("runner result is invalid")
    elif process_status == "interrupted":
        if (
            exit_code is not None
            or business_status != "unknown"
            or reason_code != "no_business_evidence"
        ):
            raise ValueError("runner result is invalid")
    elif exit_code is not None or (business_status, reason_code) not in {
        ("config_error", "runner_error"),
        ("config_error", "no_business_evidence"),
        ("unknown", "no_business_evidence"),
    }:
        raise ValueError("runner result is invalid")

    return {
        "protocol": protocol,
        "process_status": process_status,
        "exit_code": exit_code,
        "business_status": business_status,
        "reason_code": reason_code,
        "duration_ms": duration_ms,
    }


def _json_object(raw: bytes) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError):
        raise _RunnerError from None
    if not isinstance(value, dict):
        raise _RunnerError
    return value


def _check_directory(directory: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.lstat(directory)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise _RunnerError
        directory_fd = os.open(directory, flags)
        opened = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            os.close(directory_fd)
            raise _RunnerError
        return directory_fd
    except _RunnerError:
        raise
    except (OSError, ValueError):
        raise _RunnerError from None


def _open_checked_file(directory_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_nlink != 1
        ):
            os.close(file_fd)
            raise _RunnerError
        return file_fd
    except _RunnerError:
        raise
    except (OSError, ValueError):
        raise _RunnerError from None


def _read_checked_file(directory_fd: int, name: str, maximum: int) -> bytes:
    file_fd = _open_checked_file(directory_fd, name)
    data = bytearray()
    try:
        file_stat = os.fstat(file_fd)
        if file_stat.st_size > maximum:
            raise _RunnerError
        while len(data) <= maximum:
            chunk = os.read(file_fd, min(64 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise _RunnerError
        return bytes(data)
    except _RunnerError:
        raise
    except (OSError, OverflowError, ValueError):
        raise _RunnerError from None
    finally:
        try:
            os.close(file_fd)
        except OSError:
            pass


def _consume_credentials(directory_fd: int) -> bytes:
    try:
        return _read_checked_file(
            directory_fd, "credentials.json", _MAX_CREDENTIAL_BYTES
        )
    finally:
        try:
            os.unlink("credentials.json", dir_fd=directory_fd)
        except OSError:
            raise _RunnerError from None


def _parse_job(raw: bytes) -> dict[str, object]:
    job = _json_object(raw)
    if frozenset(job) != _JOB_FIELDS:
        raise _RunnerError

    protocol = job.get("protocol")
    runtime = job.get("runtime")
    digest = job.get("sha256")
    timeout_seconds = job.get("timeout_seconds")
    result_adapter = job.get("result_adapter")
    artifact_format = job.get("artifact_format")
    if not _is_int(protocol) or protocol != _PROTOCOL:
        raise _RunnerError
    if runtime not in {"python", "node"}:
        raise _RunnerError
    if not isinstance(digest, str) or _HEX_64.fullmatch(digest) is None:
        raise _RunnerError
    if not _is_int(timeout_seconds) or timeout_seconds < 1 or timeout_seconds > 240:
        raise _RunnerError
    if result_adapter not in {"json_protocol", "process_only"}:
        raise _RunnerError
    if artifact_format not in {"script", "bundle"}:
        raise _RunnerError
    return job


def validate_environment(value: object) -> dict[str, str]:
    """Validate and safely copy the environment passed to a child script."""

    if not isinstance(value, dict) or len(value) > _MAX_ENV_FIELDS:
        raise ValueError("runner environment is invalid")

    environment: dict[str, str] = {}
    folded_names: set[str] = set()
    total_bytes = 0
    try:
        for name, item in value.items():
            if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
                raise ValueError("runner environment is invalid")
            if not isinstance(item, str) or "\x00" in item:
                raise ValueError("runner environment is invalid")
            folded = name.upper()
            if (
                folded in folded_names
                or folded in _RESERVED_ENV_NAMES
                or folded.startswith(_RESERVED_ENV_PREFIXES)
            ):
                raise ValueError("runner environment is invalid")
            name_bytes = name.encode("utf-8")
            value_bytes = item.encode("utf-8")
            if (
                len(name_bytes) > _MAX_ENV_FIELD_BYTES
                or len(value_bytes) > _MAX_ENV_FIELD_BYTES
            ):
                raise ValueError("runner environment is invalid")
            total_bytes += len(value_bytes)
            if total_bytes > _MAX_ENV_TOTAL_BYTES:
                raise ValueError("runner environment is invalid")
            folded_names.add(folded)
            environment[name] = item
    except (UnicodeEncodeError, AttributeError):
        raise ValueError("runner environment is invalid") from None
    return environment


def _parse_environment(raw: bytes) -> dict[str, str]:
    credentials = _json_object(raw)
    if frozenset(credentials) != {"env"}:
        raise _RunnerError
    try:
        return validate_environment(credentials.get("env"))
    except ValueError:
        raise _RunnerError from None


def _verify_script(directory_fd: int, name: str, expected_digest: str) -> None:
    script_fd = _open_checked_file(directory_fd, name)
    digest = hashlib.sha256()
    total = 0
    try:
        script_stat = os.fstat(script_fd)
        if script_stat.st_size > _MAX_SCRIPT_BYTES:
            raise _RunnerError
        while total <= _MAX_SCRIPT_BYTES:
            chunk = os.read(script_fd, min(64 * 1024, _MAX_SCRIPT_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        if total > _MAX_SCRIPT_BYTES or digest.hexdigest() != expected_digest.lower():
            raise _RunnerError
    except _RunnerError:
        raise
    except (OSError, OverflowError, ValueError):
        raise _RunnerError from None
    finally:
        try:
            os.close(script_fd)
        except OSError:
            pass


def _bundle_member_path(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or name.endswith("/")
        or any(ord(character) < 32 for character in name)
    ):
        raise _RunnerError
    parts = name.split("/")
    if any(not part or part in {".", ".."} or ":" in part for part in parts):
        raise _RunnerError
    return name


def _has_native_signature(data: bytes) -> bool:
    if data.startswith(_NATIVE_MAGICS):
        return True
    if len(data) < 64 or not data.startswith(b"MZ"):
        return False
    pe_offset = int.from_bytes(data[60:64], "little")
    return pe_offset <= len(data) - 4 and data[pe_offset : pe_offset + 4] == b"PE\0\0"


def _bundle_files(raw: bytes, runtime: str) -> dict[str, bytes]:
    if not raw or len(raw) > _MAX_SCRIPT_BYTES:
        raise _RunnerError
    expected_task = "task.py" if runtime == "python" else "task.js"
    files: dict[str, bytes] = {}
    casefolded: set[str] = set()
    parents: set[str] = set()
    uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as bundle:
            members = bundle.infolist()
            if not members or len(members) > _MAX_BUNDLE_MEMBERS:
                raise _RunnerError
            for member in members:
                path = _bundle_member_path(member.filename)
                folded = path.casefold()
                parts = path.split("/")
                parent_keys = {
                    "/".join(parts[:index]).casefold() for index in range(1, len(parts))
                }
                if (
                    folded in casefolded
                    or folded in parents
                    or any(parent in casefolded for parent in parent_keys)
                ):
                    raise _RunnerError
                first = parts[0].casefold()
                if first in _BUNDLE_RESERVED_TOP_LEVEL:
                    raise _RunnerError
                if first in {"task.py", "task.js"} and path != expected_task:
                    raise _RunnerError
                if folded.endswith(_NATIVE_SUFFIXES):
                    raise _RunnerError

                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(mode) != stat.S_IFREG or member.flag_bits & 0x1:
                    raise _RunnerError
                if member.compress_type != zipfile.ZIP_DEFLATED:
                    raise _RunnerError
                if member.file_size < 0 or member.file_size > _MAX_BUNDLE_MEMBER_BYTES:
                    raise _RunnerError
                uncompressed += member.file_size
                if uncompressed > _MAX_BUNDLE_UNCOMPRESSED_BYTES:
                    raise _RunnerError
                data = bundle.read(member)
                if len(data) != member.file_size:
                    raise _RunnerError
                if _has_native_signature(data):
                    raise _RunnerError
                files[path] = data
                casefolded.add(folded)
                parents.update(parent_keys)
    except _RunnerError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise _RunnerError from None
    if expected_task not in files:
        raise _RunnerError
    return files


def _open_private_directory(parent_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if create:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
        metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            os.close(directory_fd)
            raise _RunnerError
        return directory_fd
    except _RunnerError:
        raise
    except (OSError, ValueError):
        raise _RunnerError from None


def _write_private_file(parent_fd: int, name: str, data: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_fd: int | None = None
    try:
        file_fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(file_fd, view[written:])
            if count <= 0:
                raise _RunnerError
            written += count
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise _RunnerError
    except _RunnerError:
        raise
    except (OSError, OverflowError, ValueError):
        raise _RunnerError from None
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass


def _write_bundle_tree(directory_fd: int, files: dict[str, bytes]) -> None:
    runtime_fd = _open_private_directory(directory_fd, "runtime", create=True)
    try:
        for path in sorted(files):
            parts = path.split("/")
            parent_fd = os.dup(runtime_fd)
            try:
                for part in parts[:-1]:
                    try:
                        child_fd = _open_private_directory(parent_fd, part, create=True)
                    except _RunnerError:
                        child_fd = _open_private_directory(
                            parent_fd, part, create=False
                        )
                    os.close(parent_fd)
                    parent_fd = child_fd
                _write_private_file(parent_fd, parts[-1], files[path])
            finally:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
    finally:
        try:
            os.close(runtime_fd)
        except OSError:
            pass


def _prepare_bundle(
    directory_fd: int, directory: Path, runtime: str, expected_digest: str
) -> tuple[Path, Path]:
    raw = _read_checked_file(directory_fd, "artifact.zip", _MAX_SCRIPT_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected_digest.lower():
        raise _RunnerError
    files = _bundle_files(raw, runtime)
    _write_bundle_tree(directory_fd, files)
    runtime_directory = directory / "runtime"
    task_name = "task.py" if runtime == "python" else "task.js"
    return runtime_directory / task_name, runtime_directory


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return
    except PermissionError:
        raise _RunnerError from None


def _reap_group(process_group: int) -> None:
    while True:
        try:
            child, _status = os.waitpid(-process_group, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            return
        except InterruptedError:
            continue
        if child == 0:
            return


def _drain(
    selector: selectors.BaseSelector,
    collector: _OutputCollector,
    timeout: float,
) -> None:
    try:
        ready = selector.select(max(0.0, timeout))
    except (OSError, ValueError):
        return
    for key, _events in ready:
        pipe = key.fileobj
        is_stdout = bool(key.data)
        for _read_count in range(32):
            try:
                chunk = os.read(pipe.fileno(), 8192)
            except BlockingIOError:
                break
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    selector.unregister(pipe)
                except (KeyError, ValueError):
                    pass
                try:
                    pipe.close()
                except OSError:
                    pass
                break
            if is_stdout:
                collector.add(chunk)


def _cleanup_process_group(
    process: subprocess.Popen[bytes],
    process_group: int,
    selector: selectors.BaseSelector,
    collector: _OutputCollector,
) -> None:
    if _group_exists(process_group):
        _signal_group(process_group, signal.SIGTERM)

    graceful_deadline = time.monotonic() + 0.25
    while time.monotonic() < graceful_deadline:
        _drain(selector, collector, 0.01)
        if process.poll() is not None:
            _reap_group(process_group)
        if not _group_exists(process_group):
            break

    if _group_exists(process_group):
        _signal_group(process_group, signal.SIGKILL)

    kill_deadline = time.monotonic() + 1.0
    while time.monotonic() < kill_deadline:
        _drain(selector, collector, 0.01)
        if process.poll() is not None:
            _reap_group(process_group)
        if process.returncode is not None and not _group_exists(process_group):
            break

    if process.poll() is None:
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            raise _RunnerError from None
    _reap_group(process_group)

    drain_deadline = time.monotonic() + 0.1
    while selector.get_map() and time.monotonic() < drain_deadline:
        _drain(selector, collector, 0.01)
    if _group_exists(process_group):
        raise _RunnerError


def _reported_business_status(raw_output: bytes) -> str | None:
    try:
        result = _json_object(raw_output)
    except _RunnerError:
        return None
    status = result.get("status")
    if isinstance(status, str) and status in _BUSINESS_STATUSES:
        return status
    return None


def _execute(
    script_path: Path,
    working_directory: Path,
    job: dict[str, object],
    declared_environment: dict[str, str],
    cancelled: list[bool],
) -> tuple[str, int | None, str, str]:
    runtime = job["runtime"]
    script_argument = str(script_path)
    argv = (
        [sys.executable, script_argument]
        if runtime == "python"
        else ["node", script_argument]
    )
    child_environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(working_directory),
    }
    child_environment.update(declared_environment)

    selector = selectors.DefaultSelector()
    collector = _OutputCollector()
    subreaper = _Subreaper()
    process: subprocess.Popen[bytes] | None = None
    subreaper.enable()
    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(working_directory),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise _RunnerError from None

        if process.stdout is None or process.stderr is None:
            raise _RunnerError
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, True)
        selector.register(process.stderr, selectors.EVENT_READ, False)

        process_group = process.pid
        deadline = time.monotonic() + int(job["timeout_seconds"])
        outcome = "exited"
        exit_code: int | None = None
        while True:
            polled = process.poll()
            if polled is not None:
                exit_code = polled
                break
            if cancelled[0]:
                outcome = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            _drain(selector, collector, min(0.05, remaining))

        _cleanup_process_group(process, process_group, selector, collector)
        _drain(selector, collector, 0.0)

        if outcome == "timeout":
            return "timeout", None, "timeout", "timeout"
        if outcome == "cancelled":
            return "cancelled", None, "cancelled", "cancelled"
        if exit_code is None:
            raise _RunnerError
        if collector.exceeded:
            return "exited", exit_code, "unknown", "output_limit"
        if job["result_adapter"] == "json_protocol":
            business_status = _reported_business_status(bytes(collector.data))
            if business_status is not None:
                return "exited", exit_code, business_status, "reported_result"
        return "exited", exit_code, "unknown", "no_business_evidence"
    finally:
        try:
            if process is not None and _group_exists(process.pid):
                _cleanup_process_group(process, process.pid, selector, collector)
        finally:
            for key in list(selector.get_map().values()):
                try:
                    selector.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
                try:
                    key.fileobj.close()
                except (AttributeError, OSError):
                    pass
            selector.close()
            subreaper.restore()


def _install_sigterm_handler(cancelled: list[bool]) -> object | None:
    def handle_sigterm(_signal_number: int, _frame: object) -> None:
        cancelled[0] = True

    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, handle_sigterm)
        return previous
    except (OSError, ValueError):
        return None


def run(directory: Path) -> dict[str, object]:
    """Execute one prepared job directory and return only its safe envelope."""

    started = time.monotonic()
    cancelled = [False]
    previous_sigterm = _install_sigterm_handler(cancelled)
    directory_fd: int | None = None
    try:
        directory = Path(os.path.abspath(Path(directory)))
        directory_fd = _check_directory(directory)
        credentials_raw = _consume_credentials(directory_fd)
        if cancelled[0]:
            return _envelope(
                "cancelled",
                None,
                "cancelled",
                "cancelled",
                int((time.monotonic() - started) * 1000),
            )
        job = _parse_job(_read_checked_file(directory_fd, "job.json", _MAX_JOB_BYTES))
        declared_environment = _parse_environment(credentials_raw)
        runtime = str(job["runtime"])
        if job["artifact_format"] == "bundle":
            script_path, working_directory = _prepare_bundle(
                directory_fd, directory, runtime, str(job["sha256"])
            )
        else:
            script_name = "script.py" if runtime == "python" else "script.js"
            _verify_script(directory_fd, script_name, str(job["sha256"]))
            script_path = directory / script_name
            working_directory = directory
        process_status, exit_code, business_status, reason_code = _execute(
            script_path, working_directory, job, declared_environment, cancelled
        )
        return _envelope(
            process_status,
            exit_code,
            business_status,
            reason_code,
            int((time.monotonic() - started) * 1000),
        )
    except (
        _RunnerError,
        OSError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
        SystemExit,
    ):
        if cancelled[0]:
            return _envelope(
                "cancelled",
                None,
                "cancelled",
                "cancelled",
                int((time.monotonic() - started) * 1000),
            )
        return _envelope(
            "start_error",
            None,
            "config_error",
            "runner_error",
            int((time.monotonic() - started) * 1000),
        )
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if previous_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, previous_sigterm)
            except (OSError, ValueError):
                pass


def _main() -> None:
    try:
        result = validate_result(run(Path(__file__).resolve().parent))
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    except (
        OSError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        KeyboardInterrupt,
        SystemExit,
    ):
        encoded = json.dumps(
            _envelope("start_error", None, "config_error", "runner_error", 0),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    try:
        os.write(1, encoded.encode("ascii") + b"\n")
    except OSError:
        return


if __name__ == "__main__":
    _main()
