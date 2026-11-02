"""Durable, serial execution in plugin-owned Neo sandboxes only."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..credentials import user_key
from ..permissions import Identity
from ..runners.bootstrap import validate_environment, validate_result
from ..storage import StateStore
from .client import NeoClient, NeoError

_KIND = "neo_execution"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RESULT_KEYS = (
    "protocol",
    "process_status",
    "exit_code",
    "business_status",
    "reason_code",
    "duration_ms",
)
# No caller data, environment values or secrets are interpolated into commands.
_PREPARE = """python3 -c 'import os,stat
os.umask(0o077)
os.mkdir("checkin",0o700)
for name in ("bootstrap.py","job.json","credentials.json","script.py","script.js"):
 fd=os.open("checkin/"+name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 os.close(fd)
print("ready")'"""
_EXECUTE = "python3 checkin/bootstrap.py"


class ExecutionBlocked(RuntimeError):
    """Execution cannot proceed without a confirmed state and cleanup boundary."""


@dataclass(frozen=True)
class PreparedExecution:
    """Prepared bytes, not a URL or a command supplied by a chat user."""

    identity: Identity
    task_id: str
    task_version: str
    source_commit: str
    sha256: str
    runtime: str
    script: bytes = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    result_adapter: str = "json_protocol"
    timeout_seconds: int = 180


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _result(process="start_error", business="config_error", reason="runner_error"):
    return {
        "protocol": 1,
        "process_status": process,
        "exit_code": None,
        "business_status": business,
        "reason_code": reason,
        "duration_ms": 0,
    }


def _authorize(guard, snapshot) -> None:
    result = guard(snapshot)
    if result is not None:
        if inspect.iscoroutine(result):
            result.close()
        raise ExecutionBlocked(
            "execution guard must be synchronous and raise on denial"
        )


def _payload(request: PreparedExecution) -> tuple[bytes, bytes]:
    if (
        not isinstance(request, PreparedExecution)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", request.task_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", request.task_version) is None
        or re.fullmatch(r"[0-9a-f]{40}", request.source_commit) is None
        or re.fullmatch(r"[0-9a-f]{64}", request.sha256) is None
        or type(request.script) is not bytes
        or not 0 < len(request.script) <= 1024 * 1024
        or hashlib.sha256(request.script).hexdigest() != request.sha256
        or request.runtime not in {"python", "node"}
        or request.result_adapter not in {"json_protocol", "process_only"}
        or type(request.timeout_seconds) is not int
        or not 1 <= request.timeout_seconds <= 240
        or not isinstance(request.environment, Mapping)
    ):
        raise ExecutionBlocked("prepared execution is invalid")
    try:
        environment = validate_environment(dict(request.environment))
        credentials = json.dumps({"env": environment}, ensure_ascii=False).encode(
            "utf-8"
        )
    except (UnicodeError, ValueError):
        raise ExecutionBlocked("prepared environment is invalid") from None
    if len(credentials) > 256 * 1024:
        raise ExecutionBlocked("prepared environment is invalid")
    job = json.dumps(
        {
            "protocol": 1,
            "runtime": request.runtime,
            "sha256": request.sha256,
            "timeout_seconds": request.timeout_seconds,
            "result_adapter": request.result_adapter,
        }
    ).encode("utf-8")
    return job, credentials


class NeoExecutor:
    """One serial execution service; every external intent is committed first.

    Cleanup records deliberately survive ambiguous creates and uncertain KV writes.
    No create replay, business retry, host fallback or global Neo GC is provided.
    """

    def __init__(self, store: StateStore, client: NeoClient):
        self.store = store
        self.client = client
        self._serial = asyncio.Lock()
        self._active: set[asyncio.Task] = set()
        self._cleanup: set[asyncio.Task] = set()
        self._accepting = True
        self._recovered = False

    @property
    def blocked(self) -> bool:
        return (
            not self._accepting
            or not self._recovered
            or not self.store.ready
            or bool(self.store.snapshot()["cleanup_jobs"])
        )

    async def recover(self) -> None:
        """Clean recorded resources and interrupt unfinished runs; never replay scripts."""
        async with self._serial:
            if not self._accepting or not self.store.ready:
                raise ExecutionBlocked("execution state is unavailable")
            await self.client.check_access()
            snapshot = self.store.snapshot()
            for run_id, job in snapshot["cleanup_jobs"].items():
                run = snapshot["runs"].get(run_id)
                self._validate_job(run_id, job, run)
                admitted = run.get("phase") == "admitted"
                result = {key: run[key] for key in _RESULT_KEYS}
                if admitted:
                    result = _result("cancelled", "cancelled", "cancelled")
                elif not run.get("finished_at"):
                    result = _result("interrupted", "unknown", "no_business_evidence")
                await self._finalize(
                    run_id,
                    job.get("sandbox_id"),
                    job.get("cargo_id"),
                    result,
                    allocation_started=not admitted,
                )
            self._recovered = True

    def _validate_job(self, run_id, job, run) -> None:
        if (
            type(job) is not dict
            or type(run) is not dict
            or job.get("kind") != _KIND
            or run.get("kind") != _KIND
            or job.get("run_id") != run_id
            or run.get("run_id") != run_id
            or job.get("connection") != self.client.connection.fingerprint
            or run.get("connection") != job.get("connection")
        ):
            raise ExecutionBlocked(
                "cleanup record or connection requires operator attention"
            )
        for key in ("sandbox_id", "cargo_id"):
            value = job.get(key)
            if value != run.get(key) or (
                value is not None
                and (type(value) is not str or not _ID.fullmatch(value))
            ):
                raise ExecutionBlocked("cleanup resource identity is invalid")
        if run.get("phase") == "admitted" and (
            run.get("sandbox_id") is not None
            or run.get("cargo_id") is not None
            or run.get("finished_at") is not None
        ):
            raise ExecutionBlocked("admitted execution record is invalid")
        try:
            validate_result({key: run[key] for key in _RESULT_KEYS})
        except (KeyError, ValueError):
            raise ExecutionBlocked("execution result record is invalid") from None

    async def execute(
        self, request: PreparedExecution, *, guard: Callable[[dict], None]
    ) -> dict:
        job_payload, credential_payload = _payload(request)
        owner = user_key(request.identity)
        if (
            not callable(guard)
            or inspect.iscoroutinefunction(guard)
            or inspect.isasyncgenfunction(guard)
        ):
            raise ExecutionBlocked("execution authorization guard is required")
        current = asyncio.current_task()
        self._active.add(current)
        try:
            async with self._serial:
                if self.blocked:
                    raise ExecutionBlocked(
                        "execution is blocked by state or pending cleanup"
                    )
                await self.client.check_access()
                run_id = uuid.uuid4().hex
                connection = self.client.connection.fingerprint

                def begin(snapshot):
                    _authorize(guard, snapshot)
                    if (
                        snapshot["cleanup_jobs"]
                        or run_id in snapshot["runs"]
                        or len(snapshot["runs"]) >= 1000
                    ):
                        raise ExecutionBlocked(
                            "execution state capacity or cleanup blocks admission"
                        )
                    snapshot["runs"][run_id] = {
                        "kind": _KIND,
                        "run_id": run_id,
                        "user_key": owner,
                        "task_id": request.task_id,
                        "task_version": request.task_version,
                        "source_commit": request.source_commit,
                        "sha256": request.sha256,
                        "connection": connection,
                        "sandbox_id": None,
                        "cargo_id": None,
                        "started_at": _now(),
                        "finished_at": None,
                        "phase": "admitted",
                        "cleanup_status": "pending",
                        **_result(),
                        # Counted by StateStore's byte limit; released as IDs/results grow.
                        "_reserve": " " * 8192,
                    }
                    snapshot["cleanup_jobs"][run_id] = {
                        "kind": _KIND,
                        "run_id": run_id,
                        "connection": connection,
                        "sandbox_id": None,
                        "cargo_id": None,
                    }

                await self.store.mutate(begin)
                sandbox_id = cargo_id = None
                result = _result()
                sent = False
                allocation_started = False
                try:
                    # External permission/config data can change while KV awaits.
                    _authorize(guard, self.store.snapshot())

                    def creating(snapshot):
                        _authorize(guard, snapshot)
                        snapshot["runs"][run_id]["phase"] = "creating"

                    # A confirmed creating intent must precede every allocation.
                    await self.store.mutate(creating)
                    _authorize(guard, self.store.snapshot())
                    allocation_started = True
                    allocated = await self.client.create_sandbox(run_id)
                    sandbox_id, cargo_id = allocated["id"], allocated["cargo_id"]

                    def allocated_state(snapshot):
                        snapshot["runs"][run_id].update(
                            sandbox_id=sandbox_id,
                            cargo_id=cargo_id,
                            phase="preparing",
                            _reserve=" " * 4096,
                        )
                        snapshot["cleanup_jobs"][run_id].update(
                            sandbox_id=sandbox_id, cargo_id=cargo_id
                        )

                    await self.store.mutate(allocated_state)
                    cargo = await self.client.get_cargo(cargo_id)
                    if not self._owned(cargo, sandbox_id, cargo_id):
                        raise ExecutionBlocked("allocated cargo ownership is invalid")
                    # Session startup 503 is known to precede execution. No other retry.
                    for attempt in range(20):
                        try:
                            prepared = await self.client.exec(sandbox_id, _PREPARE, 15)
                            break
                        except NeoError as error:
                            if error.code != "session_not_ready" or attempt == 19:
                                raise
                            await asyncio.sleep(1)
                    if (
                        not prepared["success"]
                        or prepared["exit_code"] != 0
                        or prepared["output"].strip() != "ready"
                    ):
                        raise ExecutionBlocked("sandbox private directory setup failed")
                    bootstrap = Path(__file__).parents[1] / "runners" / "bootstrap.py"
                    await self.client.upload(
                        sandbox_id, "checkin/bootstrap.py", bootstrap.read_bytes()
                    )
                    await self.client.upload(
                        sandbox_id, "checkin/job.json", job_payload
                    )
                    script_name = (
                        "script.py" if request.runtime == "python" else "script.js"
                    )
                    await self.client.upload(
                        sandbox_id, "checkin/" + script_name, request.script
                    )
                    await self.client.upload(
                        sandbox_id, "checkin/credentials.json", credential_payload
                    )

                    def executing(snapshot):
                        _authorize(guard, snapshot)
                        snapshot["runs"][run_id]["phase"] = "executing"

                    await self.store.mutate(executing)
                    _authorize(guard, self.store.snapshot())
                    sent = True
                    response = await self.client.exec(
                        sandbox_id, _EXECUTE, request.timeout_seconds + 10
                    )
                    if response["success"] and response["exit_code"] == 0:
                        try:
                            result = validate_result(json.loads(response["output"]))
                        except (ValueError, TypeError, RecursionError):
                            result = _result(
                                "start_error", "unknown", "no_business_evidence"
                            )
                    else:
                        result = _result(
                            "start_error", "unknown", "no_business_evidence"
                        )
                except NeoError:
                    result = _result(
                        "start_error",
                        "unknown" if sent else "config_error",
                        "no_business_evidence",
                    )
                except asyncio.CancelledError:
                    result = (
                        _result("interrupted", "unknown", "no_business_evidence")
                        if sent
                        else _result("cancelled", "cancelled", "cancelled")
                    )
                    raise
                finally:
                    cleanup = asyncio.create_task(
                        self._finalize(
                            run_id,
                            sandbox_id,
                            cargo_id,
                            result,
                            allocation_started=allocation_started,
                        )
                    )
                    self._cleanup.add(cleanup)
                    try:
                        await asyncio.shield(cleanup)
                    finally:
                        # An interrupted waiter leaves the task tracked for close().
                        if cleanup.done():
                            self._cleanup.discard(cleanup)
                return self.store.snapshot()["runs"][run_id]
        finally:
            self._active.discard(current)

    @staticmethod
    def _owned(cargo, sandbox_id, cargo_id) -> bool:
        return (
            type(cargo) is dict
            and cargo.get("id") == cargo_id
            and cargo.get("managed") is True
            and cargo.get("managed_by_sandbox_id") == sandbox_id
        )

    async def _clean_resources(self, sandbox_id, cargo_id) -> bool:
        if not sandbox_id or not cargo_id:
            return False
        try:
            sandbox = await self.client.get_sandbox(sandbox_id)
            cargo = await self.client.get_cargo(cargo_id)
            if sandbox is not None and (
                sandbox.get("id") != sandbox_id or sandbox.get("cargo_id") != cargo_id
            ):
                return False
            if cargo is not None and not self._owned(cargo, sandbox_id, cargo_id):
                return False
            await self.client.delete_sandbox(sandbox_id)
            if await self.client.get_sandbox(sandbox_id) is not None:
                return False
            cargo = await self.client.get_cargo(cargo_id)
            if cargo is not None:
                if not self._owned(cargo, sandbox_id, cargo_id):
                    return False
                await self.client.delete_cargo(cargo_id)
            return await self.client.get_cargo(cargo_id) is None
        except NeoError:
            return False

    async def _finalize(
        self, run_id, sandbox_id, cargo_id, result, *, allocation_started=True
    ) -> None:
        # Only durable admission or the live caller can prove no allocation.
        # A creating intent without resource IDs remains ambiguous on recovery.
        cleaned = not allocation_started or await self._clean_resources(
            sandbox_id, cargo_id
        )
        # Never replace or mutate an unconfirmed snapshot, even after physical cleanup.
        if not self.store.ready:
            return

        def finish(snapshot):
            run = snapshot["runs"][run_id]
            run.update(validate_result(result))
            run.update(
                phase="finished",
                finished_at=run.get("finished_at") or _now(),
                cleanup_status="cleaned" if cleaned else "pending",
            )
            run.pop("_reserve", None)
            if cleaned:
                snapshot["cleanup_jobs"].pop(run_id)

        await self.store.mutate(finish)

    async def close(self) -> None:
        self._accepting = False
        active = tuple(self._active)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._cleanup:
            await asyncio.gather(*self._cleanup, return_exceptions=True)
            self._cleanup.clear()
        await self.client.close()
