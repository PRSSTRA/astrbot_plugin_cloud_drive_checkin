"""Durable FIFO admission and dispatch for manual and automatic check-ins."""

from __future__ import annotations

import asyncio
import inspect
import random
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum

from .config import PluginConfigError
from .credentials import binding_key, user_key
from .execution_config import ExecutionConfig, IntervalConfig, parse_execution_config
from .executors.neo import ExecutionBlocked
from .notifications import MAX_OUTBOX_ITEMS
from .permissions import Identity
from .runners.bootstrap import validate_result
from .storage import StateStore, StateUnavailableError
from .task_config import TaskConfig
from .task_control import task_is_disabling

MAX_ACTIVE_ITEMS = 256
MAX_BATCH_ITEMS = 1000
MAX_COOLDOWN_ITEMS = 2048
MAX_SCHEDULED_BATCHES = 1000
MAX_SCHEDULE_CURSORS = 256
_POLL_SECONDS = 0.25
_KIND = "checkin"
_COOLDOWN_KIND = "checkin_cooldown"
_SPACING_KEY = "checkin:global_spacing"
_SCHEDULED_KIND = "scheduled_batch"
_SCHEDULE_CURSOR_KIND = "schedule_cursor"
_MISSING = object()
_RESULT_KEYS = (
    "protocol",
    "process_status",
    "exit_code",
    "business_status",
    "reason_code",
    "duration_ms",
)
_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_SETTLED_NOTICE_PHASES = frozenset({"sent", "failed", "unknown", "suppressed"})


class QueueReason(str, Enum):
    PERMISSION_REVOKED = "permission_revoked"
    SERVICE_DISABLED = "service_disabled"
    TASK_DISABLED = "task_disabled"
    TASK_CHANGED = "task_changed"
    BINDING_MISSING = "binding_missing"
    BINDING_PAUSED = "binding_paused"
    ENROLLMENT_ACTIVE = "enrollment_active"
    CREDENTIALS_MISSING = "credentials_missing"
    CREDENTIALS_CHANGED = "credentials_changed"
    ARTIFACT_CHANGED = "artifact_changed"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    INVALID_ORIGIN = "invalid_origin"
    CONFIG_ERROR = "config_error"
    QUEUE_EXPIRED = "queue_expired"
    COOLDOWN_ACTIVE = "cooldown_active"
    QUEUE_CAPACITY = "queue_capacity"
    INTERRUPTED = "interrupted"
    EXECUTION_ERROR = "execution_error"
    ADMIN_CANCELLED = "admin_cancelled"


class QueueRejected(ExecutionBlocked):
    """A domain rejection safe to persist and render without caller text."""

    def __init__(
        self,
        reason: QueueReason | str,
        business_status: str = "cancelled",
    ) -> None:
        try:
            normalized_reason = QueueReason(reason)
        except (TypeError, ValueError):
            raise ValueError("queue rejection reason is invalid") from None
        if business_status not in {"cancelled", "config_error", "unknown"}:
            raise ValueError("queue rejection business status is invalid")
        self.reason = normalized_reason.value
        self.business_status = business_status
        super().__init__(self.reason)


class _DuplicateAdmission(Exception):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


class _DuplicateOccurrence(Exception):
    def __init__(self, run_ids: list[str]) -> None:
        self.run_ids = run_ids


class _CancellationUnchanged(Exception):
    def __init__(self, status: str) -> None:
        self.status = status


def guard_cancellation(item: Mapping[str, object], snapshot: dict) -> None:
    """Reject a committed cancellation at queue and credential-transfer boundaries."""
    current = snapshot["batches"].get(item.get("run_id"))
    if (
        isinstance(current, dict)
        and current.get("kind") == _KIND
        and current.get("cancel_requested", False) is True
    ):
        raise QueueRejected(QueueReason.ADMIN_CANCELLED)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ExecutionBlocked("check-in execution timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ExecutionBlocked("check-in execution timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionBlocked("check-in execution timestamp is invalid")
    return parsed.astimezone(UTC)


def _add_seconds(value: datetime, seconds: int) -> datetime:
    try:
        return value + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


def _add_days(value: datetime, days: int) -> datetime:
    try:
        return value + timedelta(days=days)
    except OverflowError:
        boundary = datetime.min if days < 0 else datetime.max
        return boundary.replace(tzinfo=UTC)


def _queue_result(rejection: QueueRejected) -> dict[str, object]:
    if rejection.business_status == "cancelled":
        result: dict[str, object] = {
            "protocol": 1,
            "process_status": "cancelled",
            "exit_code": None,
            "business_status": "cancelled",
            "reason_code": "cancelled",
            "duration_ms": 0,
        }
    elif rejection.business_status == "unknown":
        result = {
            "protocol": 1,
            "process_status": "interrupted",
            "exit_code": None,
            "business_status": "unknown",
            "reason_code": "no_business_evidence",
            "duration_ms": 0,
        }
    else:
        result = {
            "protocol": 1,
            "process_status": "start_error",
            "exit_code": None,
            "business_status": "config_error",
            "reason_code": "runner_error",
            "duration_ms": 0,
        }
    result["queue_reason"] = rejection.reason
    return result


def _normalize_result(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ExecutionBlocked("manual execution result is invalid")
    try:
        normalized = validate_result({key: value[key] for key in _RESULT_KEYS})
    except (KeyError, TypeError, ValueError):
        raise ExecutionBlocked("manual execution result is invalid") from None
    queue_reason = value.get("queue_reason")
    if queue_reason is not None:
        try:
            normalized["queue_reason"] = QueueReason(queue_reason).value
        except (TypeError, ValueError):
            raise ExecutionBlocked("manual execution result is invalid") from None
    return normalized


def _call_sync(callback: Callable, *args: object) -> object:
    try:
        result = callback(*args)
    except (ExecutionBlocked, PluginConfigError, StateUnavailableError):
        raise
    except Exception as error:
        raise ExecutionBlocked("execution queue callback failed") from error
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        if inspect.iscoroutine(result):
            result.close()
        raise ExecutionBlocked("execution queue callback must be synchronous")
    return result


def _manual_cooldown_key(item: Mapping[str, object]) -> str:
    return "manual:" + str(item["binding_key"])


def _dedupe_key(item: Mapping[str, object]) -> str:
    return "binding:" + str(item["binding_key"])


def _schedule_key(task_id: str, scheduled_at: str) -> str:
    return f"schedule:{task_id}:{scheduled_at}"


def _schedule_cursor_key(task_id: str) -> str:
    return f"schedule_cursor:{task_id}"


def _has_manual_participation(item: Mapping[str, object]) -> bool:
    return item.get("source") == "manual" or item.get("manual_requested") is True


def _sample_interval(interval: IntervalConfig) -> int:
    if interval.mode == "none":
        return 0
    if interval.mode == "fixed":
        return interval.seconds
    return random.randint(interval.min_seconds, interval.max_seconds)


def _interval_enabled(interval: IntervalConfig) -> bool:
    if interval.mode == "none":
        return False
    if interval.mode == "fixed":
        return interval.seconds > 0
    return interval.max_seconds > 0


def _cooldown_reserve(item: Mapping[str, object]) -> str:
    repeated_text = sum(
        len(str(item[key])) for key in ("binding_key", "user_key", "task_id")
    )
    return " " * (repeated_text * 6 + 2048)


class ExecutionQueue:
    """One durable FIFO worker; queue claims and terminal notices commit atomically."""

    def __init__(
        self,
        store: StateStore,
        *,
        execute: Callable[[dict], object],
        guard: Callable[[dict, dict], None],
        config: Callable[[], Mapping],
        blocked: Callable[[], bool],
        reserve: Callable[[dict, dict], None],
        terminal: Callable[[dict, dict, dict], None],
        startup_ready: Callable[[], bool] = lambda: True,
    ) -> None:
        callbacks = (
            execute,
            guard,
            config,
            blocked,
            reserve,
            terminal,
            startup_ready,
        )
        if not isinstance(store, StateStore) or any(
            not callable(callback) for callback in callbacks
        ):
            raise TypeError("execution queue dependencies are invalid")
        for callback in (guard, config, blocked, reserve, terminal, startup_ready):
            if inspect.iscoroutinefunction(callback) or inspect.isasyncgenfunction(
                callback
            ):
                raise TypeError("execution queue callback must be synchronous")

        self.store = store
        self._execute = execute
        self._guard = guard
        self._config = config
        self._blocked = blocked
        self._reserve = reserve
        self._terminal = terminal
        self._startup_ready = startup_ready
        self._wake_event = asyncio.Event()
        self._admission_lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._accepting = False
        self._closing = False
        self._closed = False
        self._last_error: BaseException | None = None

    @property
    def closed(self) -> bool:
        """True only after the worker and its owned execute call have exited."""

        return self._closed

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def last_error(self) -> BaseException | None:
        """Most recent unexpected worker failure, cleared by a successful step."""

        return self._last_error

    def wake(self) -> None:
        self._wake_event.set()

    def _startup_is_ready(self) -> bool:
        ready = _call_sync(self._startup_ready)
        if type(ready) is not bool:
            raise ExecutionBlocked(
                "execution startup readiness callback must return a boolean"
            )
        return ready

    async def initialize(self) -> None:
        if self._closed:
            raise ExecutionBlocked("manual execution queue is closed")
        if self._worker is not None:
            return
        if not self.store.ready:
            raise ExecutionBlocked("manual execution state is unavailable")

        settings = self._settings()
        snapshot = self.store.snapshot()
        self._validate_snapshot(snapshot)
        await self._complete_pending_cooldowns(settings)
        await self._repair_indexes_and_prune(settings, _utc_now())
        await self._recover_before_start()
        if self._closing or self._closed:
            raise ExecutionBlocked("manual execution queue is closed")

        self._accepting = True
        self._worker = asyncio.create_task(
            self._worker_loop(), name="manual-checkin-execution-queue"
        )
        self._worker.add_done_callback(self._worker_done)
        self.wake()

    def _worker_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._last_error = error
            self._accepting = False
            self.wake()

    async def enqueue(
        self,
        *,
        identity: Identity,
        task: TaskConfig,
        origin: str,
        artifact_sha256: str,
        inputs_sha256: str,
    ) -> str:
        if not self._accepting or self._closed or not self.store.ready:
            raise ExecutionBlocked("manual execution queue is unavailable")
        if not isinstance(identity, Identity) or not isinstance(task, TaskConfig):
            raise ExecutionBlocked("manual execution request is invalid")
        if type(origin) is not str or not origin:
            raise QueueRejected(QueueReason.INVALID_ORIGIN)
        if (
            type(artifact_sha256) is not str
            or _HEX_64.fullmatch(artifact_sha256) is None
            or type(inputs_sha256) is not str
            or _HEX_64.fullmatch(inputs_sha256) is None
        ):
            raise ExecutionBlocked("manual execution request is invalid")

        settings = self._settings()
        now = _utc_now()
        run_id = uuid.uuid4().hex
        item = self._new_item(
            identity=identity,
            task=task,
            origin=origin,
            artifact_sha256=artifact_sha256,
            inputs_sha256=inputs_sha256,
            queued_at=_timestamp(now),
            expires_at=_timestamp(_add_seconds(now, settings.queue_expiry_seconds)),
            run_id=run_id,
            source="manual",
            manual_requested=True,
            scheduled_at=None,
            batch_id=None,
        )

        def admit(snapshot: dict) -> None:
            self._validate_snapshot(snapshot)
            if task_is_disabling(snapshot, task.task_id):
                raise QueueRejected(QueueReason.TASK_DISABLED)
            self._prune(
                snapshot,
                settings,
                now,
                for_admission=True,
                batch_slots=0,
            )
            active = self._active_items(snapshot)
            pending_cooldowns = self._pending_cooldown_items(snapshot)
            for existing in active:
                if existing["binding_key"] != item["binding_key"]:
                    continue
                if existing["source"] == "automatic":
                    existing["manual_requested"] = True
                    existing["origin"] = origin
                    self._ensure_cooldown_capacity(
                        snapshot, (*active, *pending_cooldowns)
                    )
                    snapshot["dedupe"][_dedupe_key(existing)] = {
                        "kind": _KIND,
                        "run_id": existing["run_id"],
                    }
                    return
                raise _DuplicateAdmission(existing["run_id"])
            if any(
                existing["binding_key"] == item["binding_key"]
                for existing in pending_cooldowns
            ):
                raise QueueRejected(
                    QueueReason.EXECUTOR_UNAVAILABLE,
                    business_status="config_error",
                )
            self._prune(
                snapshot,
                settings,
                now,
                for_admission=True,
                batch_slots=1,
            )
            if (
                len(active) >= MAX_ACTIVE_ITEMS
                or len(snapshot["batches"]) >= MAX_BATCH_ITEMS
                or len(snapshot["outbox"]) >= MAX_OUTBOX_ITEMS
            ):
                raise QueueRejected(QueueReason.QUEUE_CAPACITY)

            cooldown = snapshot["cooldowns"].get(_manual_cooldown_key(item))
            if (
                settings.manual_cooldown_seconds
                and cooldown is not None
                and self._cooldown_expiry(cooldown) > now
            ):
                raise QueueRejected(QueueReason.COOLDOWN_ACTIVE)

            item["sequence"] = self._next_sequence(snapshot)
            self._validate_item(run_id, item)
            self._ensure_cooldown_capacity(
                snapshot, (*active, *pending_cooldowns, item)
            )
            snapshot["batches"][run_id] = item
            snapshot["dedupe"][_dedupe_key(item)] = {
                "kind": _KIND,
                "run_id": run_id,
            }
            result = _call_sync(self._reserve, snapshot, item)
            if result is not None:
                raise ExecutionBlocked("terminal reservation callback must return None")

        try:
            async with self._admission_lock:
                if not self._accepting or self._closed or not self.store.ready:
                    raise ExecutionBlocked("manual execution queue is unavailable")
                await self._complete_pending_cooldowns(settings)
                confirmed = await self.store.mutate(admit)
        except _DuplicateAdmission as duplicate:
            return duplicate.run_id
        self.wake()
        active_item = confirmed["batches"].get(run_id)
        if type(active_item) is dict:
            return run_id
        merged = next(
            existing
            for existing in self._active_items(confirmed)
            if existing["binding_key"] == item["binding_key"]
        )
        return merged["run_id"]

    async def enqueue_scheduled(
        self,
        *,
        task: TaskConfig,
        scheduled_at: datetime,
        requests: list[dict],
    ) -> dict[str, object]:
        if not self._accepting or self._closed or not self.store.ready:
            raise ExecutionBlocked("automatic execution queue is unavailable")
        if (
            not isinstance(task, TaskConfig)
            or not isinstance(scheduled_at, datetime)
            or scheduled_at.tzinfo is None
            or scheduled_at.utcoffset() is None
            or type(requests) is not list
        ):
            raise ExecutionBlocked("automatic execution request is invalid")

        scheduled_text = _timestamp(scheduled_at)
        occurrence_key = _schedule_key(task.task_id, scheduled_text)
        batch_id = uuid.uuid4().hex
        settings = self._settings()
        now = _utc_now()
        queued_at = _timestamp(now)
        expires_at = _timestamp(_add_seconds(now, settings.queue_expiry_seconds))
        candidates: list[dict] = []
        seen_bindings: set[str] = set()
        normalized_requests: list[tuple[Identity, str, str | None, str | None]] = []
        for request in requests:
            if type(request) is not dict:
                raise ExecutionBlocked("automatic execution request is invalid")
            identity = request.get("identity")
            origin = request.get("origin")
            artifact_sha256 = request.get("artifact_sha256")
            inputs_sha256 = request.get("inputs_sha256")
            if (
                not isinstance(identity, Identity)
                or type(identity.platform_id) is not str
                or not identity.platform_id
                or type(identity.qq_id) is not str
                or not identity.qq_id
                or type(origin) is not str
                or not origin
                or (
                    artifact_sha256 is not None
                    and (
                        type(artifact_sha256) is not str
                        or _HEX_64.fullmatch(artifact_sha256) is None
                    )
                )
                or (
                    inputs_sha256 is not None
                    and (
                        type(inputs_sha256) is not str
                        or _HEX_64.fullmatch(inputs_sha256) is None
                    )
                )
            ):
                raise ExecutionBlocked("automatic execution request is invalid")
            normalized_requests.append(
                (identity, origin, artifact_sha256, inputs_sha256)
            )
        normalized_requests.sort(
            key=lambda value: (value[0].platform_id, value[0].qq_id)
        )
        for identity, origin, artifact_sha256, inputs_sha256 in normalized_requests:
            target_binding = binding_key(identity, task.task_id)
            if target_binding in seen_bindings:
                raise ExecutionBlocked("automatic execution request is invalid")
            seen_bindings.add(target_binding)
            candidate = self._new_item(
                identity=identity,
                task=task,
                origin=origin,
                artifact_sha256=artifact_sha256,
                inputs_sha256=inputs_sha256,
                queued_at=queued_at,
                expires_at=expires_at,
                run_id=uuid.uuid4().hex,
                source="automatic",
                manual_requested=False,
                scheduled_at=scheduled_text,
                batch_id=batch_id,
            )
            candidates.append(candidate)

        def admit(snapshot: dict) -> None:
            self._validate_snapshot(snapshot)
            if task_is_disabling(snapshot, task.task_id):
                raise QueueRejected(QueueReason.TASK_DISABLED)
            self._prune(
                snapshot,
                settings,
                now,
                for_admission=True,
                batch_slots=0,
                schedule_slots=1,
            )
            occurrence = snapshot["dedupe"].get(occurrence_key)
            if occurrence is not None:
                if (
                    type(occurrence) is not dict
                    or occurrence.get("kind") != _SCHEDULED_KIND
                    or occurrence.get("task_id") != task.task_id
                    or occurrence.get("scheduled_at") != scheduled_text
                    or type(occurrence.get("run_ids")) is not list
                    or any(type(run_id) is not str for run_id in occurrence["run_ids"])
                ):
                    raise ExecutionBlocked("automatic execution occurrence is invalid")
                raise _DuplicateOccurrence(list(occurrence["run_ids"]))
            cursor_key = _schedule_cursor_key(task.task_id)
            cursor = snapshot["dedupe"].get(cursor_key)
            if cursor is not None:
                if (
                    type(cursor) is not dict
                    or cursor.get("kind") != _SCHEDULE_CURSOR_KIND
                    or cursor.get("task_id") != task.task_id
                ):
                    raise ExecutionBlocked("automatic execution cursor is invalid")
                if _parse_timestamp(
                    cursor.get("scheduled_at")
                ) >= scheduled_at.astimezone(UTC):
                    raise _DuplicateOccurrence([])
            elif self._schedule_cursor_count(snapshot) >= MAX_SCHEDULE_CURSORS:
                raise QueueRejected(QueueReason.QUEUE_CAPACITY)
            if (
                sum(
                    1
                    for record in snapshot["dedupe"].values()
                    if type(record) is dict and record.get("kind") == _SCHEDULED_KIND
                )
                >= MAX_SCHEDULED_BATCHES
            ):
                raise QueueRejected(QueueReason.QUEUE_CAPACITY)

            unfinished = [
                item
                for item in snapshot["batches"].values()
                if type(item) is dict
                and item.get("kind") == _KIND
                and item.get("task_id") == task.task_id
                and self._item_is_unfinished(item)
            ]
            if unfinished:
                self._record_occurrence(
                    snapshot,
                    occurrence_key=occurrence_key,
                    task_id=task.task_id,
                    scheduled_at=scheduled_text,
                    status="skipped_overlap",
                    run_ids=[],
                )
                return

            eligible: list[dict] = []
            for candidate in candidates:
                try:
                    self._guard_item(candidate, snapshot)
                except QueueRejected as rejection:
                    if rejection.reason in {
                        QueueReason.PERMISSION_REVOKED.value,
                        QueueReason.BINDING_MISSING.value,
                        QueueReason.BINDING_PAUSED.value,
                        QueueReason.ENROLLMENT_ACTIVE.value,
                        QueueReason.INVALID_ORIGIN.value,
                    }:
                        continue
                    if rejection.reason not in {
                        QueueReason.EXECUTOR_UNAVAILABLE.value,
                        QueueReason.ARTIFACT_CHANGED.value,
                        QueueReason.CREDENTIALS_MISSING.value,
                    }:
                        raise
                eligible.append(candidate)

            if not eligible:
                self._record_occurrence(
                    snapshot,
                    occurrence_key=occurrence_key,
                    task_id=task.task_id,
                    scheduled_at=scheduled_text,
                    status="skipped_empty",
                    run_ids=[],
                )
                return

            self._prune(
                snapshot,
                settings,
                now,
                for_admission=True,
                batch_slots=len(eligible),
            )
            active = self._active_items(snapshot)
            pending_cooldowns = self._pending_cooldown_items(snapshot)
            if (
                len(active) + len(eligible) > MAX_ACTIVE_ITEMS
                or len(snapshot["batches"]) + len(eligible) > MAX_BATCH_ITEMS
                or len(snapshot["outbox"]) + len(eligible) > MAX_OUTBOX_ITEMS
            ):
                raise QueueRejected(QueueReason.QUEUE_CAPACITY)
            sequence = self._next_sequence(snapshot)
            for offset, candidate in enumerate(eligible):
                candidate["sequence"] = sequence + offset
                self._validate_item(candidate["run_id"], candidate)
            self._ensure_cooldown_capacity(
                snapshot, (*active, *pending_cooldowns, *eligible)
            )
            for candidate in eligible:
                run_id = candidate["run_id"]
                snapshot["batches"][run_id] = candidate
                snapshot["dedupe"][_dedupe_key(candidate)] = {
                    "kind": _KIND,
                    "run_id": run_id,
                }
                result = _call_sync(self._reserve, snapshot, candidate)
                if result is not None:
                    raise ExecutionBlocked(
                        "terminal reservation callback must return None"
                    )
            self._record_occurrence(
                snapshot,
                occurrence_key=occurrence_key,
                task_id=task.task_id,
                scheduled_at=scheduled_text,
                status="admitted",
                run_ids=[candidate["run_id"] for candidate in eligible],
            )

        try:
            async with self._admission_lock:
                if not self._accepting or self._closed or not self.store.ready:
                    raise ExecutionBlocked("automatic execution queue is unavailable")
                await self._complete_pending_cooldowns(settings)
                confirmed = await self.store.mutate(admit)
        except _DuplicateOccurrence as duplicate:
            return {"status": "duplicate", "run_ids": duplicate.run_ids}
        record = confirmed["dedupe"][occurrence_key]
        if record["status"] == "admitted":
            self.wake()
        return {"status": record["status"], "run_ids": list(record["run_ids"])}

    @staticmethod
    def _new_item(
        *,
        identity: Identity,
        task: TaskConfig,
        origin: str,
        artifact_sha256: str | None,
        inputs_sha256: str | None,
        queued_at: str,
        expires_at: str,
        run_id: str,
        source: str,
        manual_requested: bool,
        scheduled_at: str | None,
        batch_id: str | None,
    ) -> dict:
        owner = user_key(identity)
        target_binding = binding_key(identity, task.task_id)
        return {
            "kind": _KIND,
            "run_id": run_id,
            "platform_id": identity.platform_id,
            "qq_id": identity.qq_id,
            "user_key": owner,
            "binding_key": target_binding,
            "task_id": task.task_id,
            "task_name": task.name,
            "task_version": task.version,
            "artifact_sha256": artifact_sha256,
            "inputs_sha256": inputs_sha256,
            "origin": origin,
            "queued_at": queued_at,
            "expires_at": expires_at,
            "source": source,
            "manual_requested": manual_requested,
            "scheduled_at": scheduled_at,
            "batch_id": batch_id,
            "status": "queued",
            "_reserve": _cooldown_reserve(
                {
                    "binding_key": target_binding,
                    "user_key": owner,
                    "task_id": task.task_id,
                }
            ),
        }

    @staticmethod
    def _next_sequence(snapshot: dict) -> int:
        return 1 + max(
            (
                item.get("sequence", 0)
                for item in snapshot["batches"].values()
                if type(item) is dict and item.get("kind") == _KIND
            ),
            default=0,
        )

    @staticmethod
    def _pending_cooldown_items(snapshot: dict) -> list[dict]:
        return [
            item
            for item in snapshot["batches"].values()
            if type(item) is dict
            and item.get("kind") == _KIND
            and item.get("cooldown_pending") is True
        ]

    def _ensure_cooldown_capacity(
        self, snapshot: dict, items: tuple[dict, ...]
    ) -> None:
        needed = set(snapshot["cooldowns"])
        for item in items:
            needed.update(self._cooldown_keys(item))
        if len(needed) > MAX_COOLDOWN_ITEMS:
            raise QueueRejected(QueueReason.QUEUE_CAPACITY)

    @staticmethod
    def _item_is_unfinished(item: Mapping[str, object]) -> bool:
        return (
            item.get("status") in {"queued", "running"}
            or item.get("cooldown_pending") is True
        )

    @staticmethod
    def _schedule_cursor_count(snapshot: dict) -> int:
        return sum(
            1
            for record in snapshot["dedupe"].values()
            if type(record) is dict and record.get("kind") == _SCHEDULE_CURSOR_KIND
        )

    @staticmethod
    def _record_occurrence(
        snapshot: dict,
        *,
        occurrence_key: str,
        task_id: str,
        scheduled_at: str,
        status: str,
        run_ids: list[str],
    ) -> None:
        snapshot["dedupe"][occurrence_key] = {
            "kind": _SCHEDULED_KIND,
            "task_id": task_id,
            "scheduled_at": scheduled_at,
            "status": status,
            "run_ids": list(run_ids),
        }
        snapshot["dedupe"][_schedule_cursor_key(task_id)] = {
            "kind": _SCHEDULE_CURSOR_KIND,
            "task_id": task_id,
            "scheduled_at": scheduled_at,
        }

    async def request_cancel(self, run_id: str, *, guard: Callable[[], None]) -> str:
        """Persist intent only; the worker owns stopping, cleanup and final results."""
        if self._closing or self._closed or not self.store.ready:
            raise ExecutionBlocked("execution cancellation is unavailable")
        if _call_sync(guard) is not None:
            raise ExecutionBlocked("cancellation guard must return None")
        if type(run_id) is not str or _HEX_32.fullmatch(run_id) is None:
            return "not_found"

        def cancel(snapshot: dict) -> None:
            if _call_sync(guard) is not None:
                raise ExecutionBlocked("cancellation guard must return None")
            if self._closing or self._closed:
                raise ExecutionBlocked("execution cancellation is unavailable")
            item = snapshot["batches"].get(run_id)
            if type(item) is not dict or item.get("kind") != _KIND:
                raise _CancellationUnchanged("not_found")
            self._validate_item(run_id, item)
            if item["status"] == "finished":
                raise _CancellationUnchanged("finished")
            if item.get("cancel_requested") is True:
                raise _CancellationUnchanged("requested")
            item["cancel_requested"] = True
            # Admission reserved room for terminal/control state, including at quota.
            item["_reserve"] = item["_reserve"][:-64]

        try:
            await self.store.mutate(cancel)
        except _CancellationUnchanged as unchanged:
            return unchanged.status
        self.wake()
        return "requested"

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="manual-checkin-execution-queue-close"
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self._accepting = False
        self._closing = True
        self.wake()
        worker = self._worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._worker = None
        async with self._admission_lock:
            pass
        self._closed = True

    async def reconcile_shutdown(self) -> None:
        """Terminalize stopped work after the worker and Neo are quiescent."""

        if not self._closed or not self.store.ready:
            raise ExecutionBlocked("manual execution shutdown state is unavailable")
        settings = self._settings()
        await self._complete_pending_cooldowns(settings)
        while self.store.ready:
            snapshot = self.store.snapshot()
            self._validate_snapshot(snapshot)
            running = sorted(
                (
                    item
                    for item in snapshot["batches"].values()
                    if type(item) is dict
                    and item.get("kind") == _KIND
                    and item.get("status") == "running"
                ),
                key=lambda item: item["sequence"],
            )
            if running:
                await self._finish(
                    running[0], self._shutdown_result(snapshot, running[0])
                )
                continue
            now = _utc_now()
            expired = next(
                (
                    item
                    for item in snapshot["batches"].values()
                    if type(item) is dict
                    and item.get("kind") == _KIND
                    and item.get("status") == "queued"
                    and _parse_timestamp(item["expires_at"]) <= now
                ),
                None,
            )
            if expired is None:
                return
            await self._finish(
                expired,
                _queue_result(QueueRejected(QueueReason.QUEUE_EXPIRED)),
            )

    def _settings(self) -> ExecutionConfig:
        raw = _call_sync(self._config)
        return parse_execution_config(raw)

    def _guard_item(self, item: dict, snapshot: dict) -> None:
        guard_cancellation(item, snapshot)
        if task_is_disabling(snapshot, item["task_id"]):
            raise QueueRejected(QueueReason.TASK_DISABLED)
        result = _call_sync(self._guard, dict(item), snapshot)
        if result is not None:
            raise ExecutionBlocked("execution queue guard must return None")

    @staticmethod
    def _cooldown_keys(item: Mapping[str, object]) -> tuple[str, ...]:
        if _has_manual_participation(item):
            return (_manual_cooldown_key(item), _SPACING_KEY)
        return (_SPACING_KEY,)

    def _validate_snapshot(self, snapshot: object) -> None:
        if type(snapshot) is not dict:
            raise ExecutionBlocked("check-in execution state is invalid")
        active_bindings: set[str] = set()
        run_ids: set[str] = set()
        for key, item in snapshot["batches"].items():
            if type(item) is not dict or item.get("kind") != _KIND:
                continue
            self._validate_item(key, item)
            if item["run_id"] in run_ids:
                raise ExecutionBlocked("check-in execution state is invalid")
            run_ids.add(item["run_id"])
            if item["status"] in {"queued", "running"}:
                if item["binding_key"] in active_bindings:
                    raise ExecutionBlocked("check-in execution state is invalid")
                active_bindings.add(item["binding_key"])
        self._validate_schedule_records(snapshot)

    @staticmethod
    def _validate_item(key: object, item: dict) -> None:
        string_fields = (
            "platform_id",
            "qq_id",
            "user_key",
            "binding_key",
            "task_id",
            "task_name",
            "task_version",
            "origin",
            "queued_at",
            "expires_at",
        )
        run_id = item.get("run_id")
        status = item.get("status")
        source = item.get("source")
        artifact_sha256 = item.get("artifact_sha256")
        inputs_sha256 = item.get("inputs_sha256")
        scheduled_at = item.get("scheduled_at")
        batch_id = item.get("batch_id")
        if (
            type(key) is not str
            or type(run_id) is not str
            or key != run_id
            or _HEX_32.fullmatch(run_id) is None
            or any(type(item.get(name)) is not str for name in string_fields)
            or not item["platform_id"]
            or not item["qq_id"]
            or not item["task_id"]
            or not item["origin"]
            or _HEX_64.fullmatch(item["task_version"]) is None
            or (
                artifact_sha256 is not None
                and (
                    type(artifact_sha256) is not str
                    or _HEX_64.fullmatch(artifact_sha256) is None
                )
            )
            or (
                inputs_sha256 is not None
                and (
                    type(inputs_sha256) is not str
                    or _HEX_64.fullmatch(inputs_sha256) is None
                )
            )
            or type(item.get("sequence")) is not int
            or item["sequence"] < 1
            or status not in {"queued", "running", "finished"}
            or source not in {"manual", "automatic"}
            or type(item.get("manual_requested")) is not bool
            or type(item.get("cancel_requested", False)) is not bool
        ):
            raise ExecutionBlocked("check-in execution state is invalid")
        if source == "manual":
            if (
                item["manual_requested"] is not True
                or scheduled_at is not None
                or batch_id is not None
                or artifact_sha256 is None
                or inputs_sha256 is None
            ):
                raise ExecutionBlocked("check-in execution state is invalid")
        elif (
            type(scheduled_at) is not str
            or type(batch_id) is not str
            or _HEX_32.fullmatch(batch_id) is None
        ):
            raise ExecutionBlocked("check-in execution state is invalid")
        else:
            parsed_scheduled = _parse_timestamp(scheduled_at)
            if _timestamp(parsed_scheduled) != scheduled_at:
                raise ExecutionBlocked("check-in execution state is invalid")

        _parse_timestamp(item["queued_at"])
        _parse_timestamp(item["expires_at"])
        expected_user = user_key(Identity(item["platform_id"], item["qq_id"]))
        expected_binding = binding_key(
            Identity(item["platform_id"], item["qq_id"]), item["task_id"]
        )
        if item["user_key"] != expected_user or item["binding_key"] != expected_binding:
            raise ExecutionBlocked("check-in execution state is invalid")
        if status == "finished":
            _parse_timestamp(item.get("finished_at"))
            _normalize_result(item.get("result"))
        elif "finished_at" in item or "result" in item:
            raise ExecutionBlocked("check-in execution state is invalid")
        if "cooldown_pending" in item and (
            status != "finished"
            or item["cooldown_pending"] is not True
            or type(item.get("_cooldown_reserve")) is not str
        ):
            raise ExecutionBlocked("check-in execution state is invalid")
        if "_cooldown_reserve" in item and item.get("cooldown_pending") is not True:
            raise ExecutionBlocked("check-in execution state is invalid")
        if status in {"queued", "running"} and type(item.get("_reserve")) is not str:
            raise ExecutionBlocked("check-in execution state is invalid")
        if status == "finished" and "_reserve" in item:
            raise ExecutionBlocked("check-in execution state is invalid")

    @staticmethod
    def _validate_schedule_records(snapshot: dict) -> None:
        occurrences: dict[str, datetime] = {}
        cursors: dict[str, datetime] = {}
        occurrence_count = 0
        cursor_count = 0
        for key, record in snapshot["dedupe"].items():
            if type(record) is not dict:
                continue
            kind = record.get("kind")
            if kind == _SCHEDULED_KIND:
                occurrence_count += 1
                task_id = record.get("task_id")
                scheduled_at = record.get("scheduled_at")
                run_ids = record.get("run_ids")
                if (
                    type(task_id) is not str
                    or not task_id
                    or type(scheduled_at) is not str
                    or key != _schedule_key(task_id, scheduled_at)
                    or record.get("status")
                    not in {"admitted", "skipped_overlap", "skipped_empty"}
                    or type(run_ids) is not list
                    or any(
                        type(run_id) is not str or _HEX_32.fullmatch(run_id) is None
                        for run_id in run_ids
                    )
                    or len(set(run_ids)) != len(run_ids)
                    or (record["status"] == "admitted") != bool(run_ids)
                ):
                    raise ExecutionBlocked("automatic execution occurrence is invalid")
                parsed = _parse_timestamp(scheduled_at)
                if _timestamp(parsed) != scheduled_at:
                    raise ExecutionBlocked("automatic execution occurrence is invalid")
                latest = occurrences.get(task_id)
                occurrences[task_id] = parsed if latest is None else max(latest, parsed)
            elif kind == _SCHEDULE_CURSOR_KIND:
                cursor_count += 1
                task_id = record.get("task_id")
                scheduled_at = record.get("scheduled_at")
                if (
                    type(task_id) is not str
                    or not task_id
                    or type(scheduled_at) is not str
                    or key != _schedule_cursor_key(task_id)
                ):
                    raise ExecutionBlocked("automatic execution cursor is invalid")
                parsed = _parse_timestamp(scheduled_at)
                if _timestamp(parsed) != scheduled_at:
                    raise ExecutionBlocked("automatic execution cursor is invalid")
                cursors[task_id] = parsed
        if (
            occurrence_count > MAX_SCHEDULED_BATCHES
            or cursor_count > MAX_SCHEDULE_CURSORS
        ):
            raise ExecutionBlocked("automatic execution history capacity is invalid")
        for task_id, latest in occurrences.items():
            if task_id not in cursors or cursors[task_id] < latest:
                raise ExecutionBlocked("automatic execution cursor is invalid")

    @staticmethod
    def _active_items(snapshot: dict) -> list[dict]:
        return [
            item
            for item in snapshot["batches"].values()
            if type(item) is dict
            and item.get("kind") == _KIND
            and item.get("status") in {"queued", "running"}
        ]

    @staticmethod
    def _cooldown_expiry(record: object) -> datetime:
        if (
            type(record) is not dict
            or record.get("kind") != _COOLDOWN_KIND
            or record.get("scope") != "manual"
        ):
            raise ExecutionBlocked("manual execution cooldown state is invalid")
        return _parse_timestamp(record.get("expires_at"))

    @staticmethod
    def _spacing_record(snapshot: dict) -> dict | None:
        record = snapshot["cooldowns"].get(_SPACING_KEY)
        if record is None:
            return None
        if (
            type(record) is not dict
            or record.get("kind") != _COOLDOWN_KIND
            or record.get("scope") != "spacing"
            or type(record.get("task_id")) is not str
            or type(record.get("user_key")) is not str
        ):
            raise ExecutionBlocked("manual execution spacing state is invalid")
        _parse_timestamp(record.get("completed_at"))
        target_run_id = record.get("target_run_id")
        if target_run_id is None:
            if "target_kind" in record or "target_expires_at" in record:
                raise ExecutionBlocked("manual execution spacing state is invalid")
        elif (
            type(target_run_id) is not str
            or _HEX_32.fullmatch(target_run_id) is None
            or record.get("target_kind") not in {"user", "task"}
        ):
            raise ExecutionBlocked("manual execution spacing state is invalid")
        else:
            _parse_timestamp(record.get("target_expires_at"))
        return record

    def _complete_cooldowns(self, snapshot: dict, settings: ExecutionConfig) -> bool:
        changed = False
        for item in snapshot["batches"].values():
            if (
                type(item) is not dict
                or item.get("kind") != _KIND
                or item.get("cooldown_pending") is not True
            ):
                continue
            run_id = item["run_id"]
            run = snapshot["runs"].get(run_id)
            if not self._run_is_terminal_clean(snapshot, run_id, run):
                continue
            self._write_cooldowns(
                snapshot,
                item,
                settings,
                _parse_timestamp(run["cleanup_finished_at"]),
            )
            item.pop("cooldown_pending")
            item.pop("_cooldown_reserve", None)
            changed = True
        return changed

    async def _complete_pending_cooldowns(self, settings: ExecutionConfig) -> bool:
        snapshot = self.store.snapshot()
        self._validate_snapshot(snapshot)
        ready = any(
            type(item) is dict
            and item.get("kind") == _KIND
            and item.get("cooldown_pending") is True
            and self._run_is_terminal_clean(
                snapshot,
                item["run_id"],
                snapshot["runs"].get(item["run_id"]),
            )
            for item in snapshot["batches"].values()
        )
        if not ready:
            return False

        def complete(candidate: dict) -> None:
            if not self._complete_cooldowns(candidate, settings):
                raise ExecutionBlocked("manual execution cleanup state changed")

        await self.store.mutate(complete)
        return True

    def _prune_cooldowns(self, snapshot: dict, now: datetime) -> bool:
        self._spacing_record(snapshot)
        changed = False
        for key, record in tuple(snapshot["cooldowns"].items()):
            if type(record) is not dict or record.get("kind") != _COOLDOWN_KIND:
                continue
            if key == _SPACING_KEY:
                continue
            if record.get("scope") in {"user", "task"}:
                del snapshot["cooldowns"][key]
                changed = True
                continue
            if self._cooldown_expiry(record) <= now:
                del snapshot["cooldowns"][key]
                changed = True
        return changed

    @staticmethod
    def _settled_history(snapshot: dict, item: dict) -> bool:
        if item.get("status") != "finished" or item.get("cooldown_pending") is True:
            return False
        run_id = item["run_id"]
        notice = snapshot["outbox"].get(run_id)
        if (
            type(notice) is not dict
            or notice.get("phase") not in _SETTLED_NOTICE_PHASES
        ):
            return False
        if run_id in snapshot["cleanup_jobs"]:
            return False
        run = snapshot["runs"].get(run_id)
        return run is None or (
            type(run) is dict
            and run.get("phase") == "finished"
            and run.get("cleanup_status") == "cleaned"
        )

    def _prune_scheduled_occurrences(
        self,
        snapshot: dict,
        settings: ExecutionConfig,
        now: datetime,
        *,
        for_admission: bool,
        schedule_slots: int,
    ) -> bool:
        cutoff = _add_days(now, -settings.history_retention_days)
        records = sorted(
            (
                (key, record)
                for key, record in snapshot["dedupe"].items()
                if type(record) is dict and record.get("kind") == _SCHEDULED_KIND
            ),
            key=lambda entry: (_parse_timestamp(entry[1]["scheduled_at"]), entry[0]),
        )
        removable = [
            (key, record)
            for key, record in records
            if all(
                item is None
                or (
                    type(item) is dict
                    and item.get("kind") == _KIND
                    and self._settled_history(snapshot, item)
                )
                for run_id in record["run_ids"]
                for item in (snapshot["batches"].get(run_id),)
            )
        ]
        remove = [
            (key, record)
            for key, record in removable
            if _parse_timestamp(record["scheduled_at"]) <= cutoff
        ]
        remaining_count = len(records) - len(remove)
        if for_admission and remaining_count + schedule_slots > MAX_SCHEDULED_BATCHES:
            already = {key for key, _ in remove}
            for key, record in removable:
                if key in already:
                    continue
                remove.append((key, record))
                remaining_count -= 1
                if remaining_count + schedule_slots <= MAX_SCHEDULED_BATCHES:
                    break
        for key, _ in remove:
            snapshot["dedupe"].pop(key, None)
        return bool(remove)

    def _prune(
        self,
        snapshot: dict,
        settings: ExecutionConfig,
        now: datetime,
        *,
        for_admission: bool,
        batch_slots: int = 0,
        schedule_slots: int = 0,
    ) -> bool:
        changed = self._prune_cooldowns(snapshot, now)
        changed = (
            self._prune_scheduled_occurrences(
                snapshot,
                settings,
                now,
                for_admission=for_admission,
                schedule_slots=schedule_slots,
            )
            or changed
        )
        cutoff = _add_days(now, -settings.history_retention_days)
        eligible = sorted(
            (
                item
                for item in snapshot["batches"].values()
                if type(item) is dict
                and item.get("kind") == _KIND
                and self._settled_history(snapshot, item)
            ),
            key=lambda item: (_parse_timestamp(item["finished_at"]), item["sequence"]),
        )
        remove = [
            item for item in eligible if _parse_timestamp(item["finished_at"]) <= cutoff
        ]
        remaining_batches = len(snapshot["batches"]) - len(remove)
        remaining_outbox = len(snapshot["outbox"]) - sum(
            1 for item in remove if item["run_id"] in snapshot["outbox"]
        )
        if for_admission and (
            remaining_batches + batch_slots > MAX_BATCH_ITEMS
            or remaining_outbox + batch_slots > MAX_OUTBOX_ITEMS
        ):
            already = {item["run_id"] for item in remove}
            for item in eligible:
                if item["run_id"] in already:
                    continue
                remove.append(item)
                remaining_batches -= 1
                if item["run_id"] in snapshot["outbox"]:
                    remaining_outbox -= 1
                if (
                    remaining_batches + batch_slots <= MAX_BATCH_ITEMS
                    and remaining_outbox + batch_slots <= MAX_OUTBOX_ITEMS
                ):
                    break

        for item in remove:
            run_id = item["run_id"]
            snapshot["batches"].pop(run_id, None)
            snapshot["runs"].pop(run_id, None)
            notice = snapshot["outbox"].get(run_id)
            if type(notice) is dict and notice.get("phase") in _SETTLED_NOTICE_PHASES:
                snapshot["outbox"].pop(run_id, None)
            dedupe = snapshot["dedupe"].get(_dedupe_key(item))
            if type(dedupe) is dict and dedupe.get("run_id") == run_id:
                snapshot["dedupe"].pop(_dedupe_key(item), None)
            changed = True
        return changed

    async def _repair_indexes_and_prune(
        self, settings: ExecutionConfig, now: datetime
    ) -> None:
        snapshot = self.store.snapshot()
        expected = {
            _dedupe_key(item): {"kind": _KIND, "run_id": item["run_id"]}
            for item in self._active_items(snapshot)
        }
        current = {
            key: value
            for key, value in snapshot["dedupe"].items()
            if type(value) is dict and value.get("kind") == _KIND
        }
        probe = {
            name: dict(values) if type(values) is dict else values
            for name, values in snapshot.items()
        }
        needs_prune = self._prune(probe, settings, now, for_admission=False)
        if current == expected and not needs_prune:
            return

        def repair(candidate: dict) -> None:
            for key, value in tuple(candidate["dedupe"].items()):
                if type(value) is dict and value.get("kind") == _KIND:
                    candidate["dedupe"].pop(key)
            candidate["dedupe"].update(expected)
            self._prune(candidate, settings, now, for_admission=False)

        await self.store.mutate(repair)

    async def _recover_before_start(self) -> None:
        while self.store.ready:
            snapshot = self.store.snapshot()
            running = sorted(
                (
                    item
                    for item in snapshot["batches"].values()
                    if type(item) is dict
                    and item.get("kind") == _KIND
                    and item.get("status") == "running"
                ),
                key=lambda item: item["sequence"],
            )
            progressed = False
            for item in running:
                if await self._recover_running(item):
                    progressed = True
                    break
            if progressed:
                continue
            queued = sorted(
                (
                    item
                    for item in snapshot["batches"].values()
                    if type(item) is dict
                    and item.get("kind") == _KIND
                    and item.get("status") == "queued"
                ),
                key=lambda item: item["sequence"],
            )
            now = _utc_now()
            expired = next(
                (
                    item
                    for item in queued
                    if _parse_timestamp(item["expires_at"]) <= now
                ),
                None,
            )
            if expired is None:
                return
            await self._finish(
                expired,
                _queue_result(QueueRejected(QueueReason.QUEUE_EXPIRED)),
            )

    async def _worker_loop(self) -> None:
        while self._accepting and not self._closed:
            delay = _POLL_SECONDS
            try:
                if not self.store.ready:
                    raise ExecutionBlocked("manual execution state is unavailable")
                delay = await self._step()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except (
                ExecutionBlocked,
                PluginConfigError,
                StateUnavailableError,
            ) as error:
                self._last_error = error
                delay = _POLL_SECONDS
            if not self._accepting or self._closed:
                return
            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=max(0.0, min(delay, _POLL_SECONDS))
                )
            except TimeoutError:
                pass

    async def _step(self) -> float:
        settings = self._settings()
        if await self._complete_pending_cooldowns(settings):
            return 0.0
        snapshot = self.store.snapshot()
        self._validate_snapshot(snapshot)
        running = sorted(
            (
                item
                for item in snapshot["batches"].values()
                if type(item) is dict
                and item.get("kind") == _KIND
                and item.get("status") == "running"
            ),
            key=lambda item: item["sequence"],
        )
        running_pending = False
        if running:
            if await self._recover_running(running[0]):
                return 0.0
            running_pending = True

        queued = sorted(
            (
                item
                for item in snapshot["batches"].values()
                if type(item) is dict
                and item.get("kind") == _KIND
                and item.get("status") == "queued"
            ),
            key=lambda item: item["sequence"],
        )
        if not queued:
            return _POLL_SECONDS

        if not self._startup_is_ready():
            return _POLL_SECONDS

        now = _utc_now()
        for item in queued:
            if _parse_timestamp(item["expires_at"]) <= now:
                await self._finish(
                    item,
                    _queue_result(QueueRejected(QueueReason.QUEUE_EXPIRED)),
                )
                return 0.0
            try:
                self._guard_item(item, snapshot)
            except QueueRejected as rejection:
                await self._finish(item, _queue_result(rejection))
                return 0.0
            except (ExecutionBlocked, PluginConfigError, StateUnavailableError):
                rejection = QueueRejected(
                    QueueReason.EXECUTION_ERROR, business_status="config_error"
                )
                await self._finish(item, _queue_result(rejection))
                return 0.0

        if running_pending or any(
            type(batch) is dict
            and batch.get("kind") == _KIND
            and batch.get("cooldown_pending") is True
            for batch in snapshot["batches"].values()
        ):
            return _POLL_SECONDS

        item = queued[0]
        delay = await self._spacing_delay(snapshot, item, now, settings)
        if delay is None:
            return 0.0
        if delay > 0:
            return delay
        blocked = _call_sync(self._blocked)
        if type(blocked) is not bool:
            raise ExecutionBlocked("execution blocked callback must return a boolean")
        if blocked:
            return _POLL_SECONDS

        claimed = await self._claim(item["run_id"])
        await self._execute_claimed(claimed)
        return 0.0

    async def _spacing_delay(
        self,
        snapshot: dict,
        item: dict,
        now: datetime,
        settings: ExecutionConfig,
    ) -> float | None:
        record = self._spacing_record(snapshot)
        if record is None:
            return 0.0
        if item["task_id"] != record["task_id"]:
            spacing_kind = "task"
            interval = settings.task_interval
        elif item["user_key"] != record["user_key"]:
            spacing_kind = "user"
            interval = settings.user_interval
        else:
            return 0.0
        if not _interval_enabled(interval):
            return 0.0
        if (
            record.get("target_run_id") == item["run_id"]
            and record.get("target_kind") == spacing_kind
        ):
            expiry = _parse_timestamp(record["target_expires_at"])
            return max(0.0, (expiry - now).total_seconds())

        base = (record["task_id"], record["user_key"], record["completed_at"])

        def reserve_spacing(candidate: dict) -> None:
            current = self._spacing_record(candidate)
            if (
                current is None
                or (
                    current["task_id"],
                    current["user_key"],
                    current["completed_at"],
                )
                != base
            ):
                raise ExecutionBlocked("manual execution spacing state changed")
            queued = candidate["batches"].get(item["run_id"])
            if (
                type(queued) is not dict
                or queued.get("kind") != _KIND
                or queued.get("status") != "queued"
            ):
                raise ExecutionBlocked("manual execution queue item changed")
            delay = _sample_interval(interval)
            current.update(
                target_run_id=item["run_id"],
                target_kind=spacing_kind,
                target_expires_at=_timestamp(
                    _add_seconds(_parse_timestamp(current["completed_at"]), delay)
                ),
            )

        await self.store.mutate(reserve_spacing)
        return None

    async def _claim(self, run_id: str) -> dict:
        def claim(snapshot: dict) -> None:
            item = snapshot["batches"].get(run_id)
            if type(item) is not dict or item.get("kind") != _KIND:
                raise ExecutionBlocked("manual execution queue item is unavailable")
            if item.get("status") != "queued":
                raise ExecutionBlocked(
                    "manual execution queue item was already claimed"
                )
            guard_cancellation(item, snapshot)
            item["status"] = "running"

        confirmed = await self.store.mutate(claim)
        return confirmed["batches"][run_id]

    async def _execute_claimed(self, item: dict) -> None:
        async def invoke_execute() -> object:
            try:
                return await self._execute(dict(item))
            except asyncio.CancelledError:
                raise
            except (ExecutionBlocked, PluginConfigError, StateUnavailableError):
                raise
            except Exception as error:
                raise ExecutionBlocked("manual execution callback failed") from error

        task = asyncio.create_task(invoke_execute())
        try:
            await self._monitor_execution(item, task)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _monitor_execution(self, item: dict, task: asyncio.Task) -> None:
        rejection: QueueRejected | None = None
        returned: object = None
        failure: BaseException | None = None
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=_POLL_SECONDS)
            if done:
                break
            if not self.store.ready:
                task.cancel()
                return
            try:
                self._guard_item(item, self.store.snapshot())
            except QueueRejected as error:
                rejection = error
                task.cancel()
                break
            except (ExecutionBlocked, PluginConfigError, StateUnavailableError):
                rejection = QueueRejected(
                    QueueReason.EXECUTION_ERROR, business_status="config_error"
                )
                task.cancel()
                break
        try:
            returned = await task
        except QueueRejected as error:
            rejection = error
            failure = error
        except asyncio.CancelledError as error:
            failure = error
        except (ExecutionBlocked, PluginConfigError, StateUnavailableError) as error:
            failure = error

        if not self.store.ready:
            return
        snapshot = self.store.snapshot()
        run = snapshot["runs"].get(item["run_id"])
        if run is not None and not self._run_has_terminal_result(run):
            if rejection is not None:
                await self._remember_rejection(item["run_id"], rejection)
            return

        if run is not None:
            result = self._run_result(run)
            if rejection is not None:
                result = self._merge_rejection(result, rejection)
            await self._finish(item, result)
            return
        if rejection is not None:
            await self._finish(item, _queue_result(rejection))
            return
        if failure is not None:
            error_result = _queue_result(
                QueueRejected(
                    QueueReason.EXECUTION_ERROR, business_status="config_error"
                )
            )
            await self._finish(item, error_result)
            return
        try:
            result = _normalize_result(returned)
        except ExecutionBlocked:
            result = _queue_result(
                QueueRejected(
                    QueueReason.EXECUTION_ERROR, business_status="config_error"
                )
            )
        await self._finish(item, result)

    @staticmethod
    def _run_has_terminal_result(run: object) -> bool:
        return (
            type(run) is dict
            and run.get("phase") == "finished"
            and type(run.get("finished_at")) is str
        )

    @staticmethod
    def _run_is_terminal_clean(snapshot: dict, run_id: str, run: object) -> bool:
        return (
            type(run) is dict
            and run.get("phase") == "finished"
            and type(run.get("finished_at")) is str
            and run.get("cleanup_status") == "cleaned"
            and type(run.get("cleanup_finished_at")) is str
            and run_id not in snapshot["cleanup_jobs"]
        )

    @staticmethod
    def _run_result(run: object) -> dict[str, object]:
        if type(run) is not dict:
            raise ExecutionBlocked("manual execution run state is invalid")
        return _normalize_result(run)

    @staticmethod
    def _run_business_attempted(run: object) -> bool:
        if run is None:
            return False
        if type(run) is not dict:
            raise ExecutionBlocked("manual execution run state is invalid")
        value = run.get("business_attempted", _MISSING)
        if value is not _MISSING and type(value) is not bool:
            raise ExecutionBlocked("manual execution attempt state is invalid")
        phase = run.get("phase")
        if phase in {"admitted", "creating", "preparing"}:
            return False
        if phase == "executing":
            return True
        if phase == "finished":
            return True if value is _MISSING else value
        raise ExecutionBlocked("manual execution phase is invalid")

    def _shutdown_result(self, snapshot: dict, item: dict) -> dict[str, object]:
        run = snapshot["runs"].get(item["run_id"])
        attempted = self._run_business_attempted(run)
        if run is None:
            result = _queue_result(
                QueueRejected(QueueReason.INTERRUPTED, business_status="unknown")
            )
        elif self._run_has_terminal_result(run):
            result = self._run_result(run)
        elif attempted:
            result = _queue_result(
                QueueRejected(QueueReason.INTERRUPTED, business_status="unknown")
            )
        else:
            result = _queue_result(QueueRejected(QueueReason.INTERRUPTED))

        pending_reason = item.get("pending_queue_reason")
        if pending_reason is None:
            return result
        try:
            rejection = QueueRejected(
                pending_reason,
                item.get("pending_business_status", "cancelled"),
            )
        except (TypeError, ValueError):
            raise ExecutionBlocked("manual execution state is invalid") from None
        if run is not None and not attempted and not self._run_has_terminal_result(run):
            return _queue_result(rejection)
        return self._merge_rejection(result, rejection)

    @staticmethod
    def _merge_rejection(
        result: dict[str, object], rejection: QueueRejected
    ) -> dict[str, object]:
        if (
            result.get("process_status") == "start_error"
            and result.get("business_status") == "config_error"
            and result.get("reason_code") == "runner_error"
        ):
            return _queue_result(rejection)
        result["queue_reason"] = rejection.reason
        return result

    async def _remember_rejection(self, run_id: str, rejection: QueueRejected) -> None:
        def remember(snapshot: dict) -> None:
            item = snapshot["batches"].get(run_id)
            if (
                type(item) is dict
                and item.get("kind") == _KIND
                and item.get("status") == "running"
            ):
                item["pending_queue_reason"] = rejection.reason
                item["pending_business_status"] = rejection.business_status

        await self.store.mutate(remember)

    async def _recover_running(self, item: dict) -> bool:
        snapshot = self.store.snapshot()
        current = snapshot["batches"].get(item["run_id"])
        if (
            type(current) is not dict
            or current.get("kind") != _KIND
            or current.get("status") != "running"
        ):
            return False
        run = snapshot["runs"].get(item["run_id"])
        if run is None:
            result = _queue_result(
                QueueRejected(QueueReason.INTERRUPTED, business_status="unknown")
            )
            await self._finish(current, result)
            return True
        if not self._run_has_terminal_result(run):
            return False
        result = self._run_result(run)
        pending_reason = current.get("pending_queue_reason")
        if pending_reason is not None:
            try:
                rejection = QueueRejected(
                    pending_reason,
                    current.get("pending_business_status", "cancelled"),
                )
            except (TypeError, ValueError):
                raise ExecutionBlocked("manual execution state is invalid") from None
            result = self._merge_rejection(result, rejection)
        await self._finish(current, result)
        return True

    async def _finish(self, item: dict, result: object) -> None:
        normalized = _normalize_result(result)
        finished = _utc_now()
        settings = self._settings()

        def finish(snapshot: dict) -> None:
            current = snapshot["batches"].get(item["run_id"])
            if type(current) is not dict or current.get("kind") != _KIND:
                raise ExecutionBlocked("manual execution queue item is unavailable")
            if current.get("status") == "finished":
                return
            if current.get("status") not in {"queued", "running"}:
                raise ExecutionBlocked("manual execution queue item is invalid")
            run = snapshot["runs"].get(current["run_id"])
            final_result = dict(normalized)
            if current.get("cancel_requested") is True:
                rejection = QueueRejected(QueueReason.ADMIN_CANCELLED)
                if self._run_business_attempted(run):
                    # Use the authoritative executor result, not an earlier guard
                    # rejection merged while cancellation/cleanup was in flight.
                    if self._run_has_terminal_result(run):
                        final_result = self._run_result(run)
                    final_result["queue_reason"] = rejection.reason
                else:
                    final_result = _queue_result(rejection)

            current["status"] = "finished"
            current["finished_at"] = _timestamp(finished)
            current["result"] = final_result
            current.pop("pending_queue_reason", None)
            current.pop("pending_business_status", None)
            current.pop("_reserve", None)

            dedupe = snapshot["dedupe"].get(_dedupe_key(current))
            if type(dedupe) is dict and dedupe.get("run_id") == current["run_id"]:
                snapshot["dedupe"].pop(_dedupe_key(current), None)
            if self._run_business_attempted(run):
                if self._run_is_terminal_clean(snapshot, current["run_id"], run):
                    self._write_cooldowns(
                        snapshot,
                        current,
                        settings,
                        _parse_timestamp(run["cleanup_finished_at"]),
                    )
                else:
                    current["cooldown_pending"] = True
                    current["_cooldown_reserve"] = _cooldown_reserve(current)
            else:
                spacing = self._spacing_record(snapshot)
                if (
                    spacing is not None
                    and spacing.get("target_run_id") == current["run_id"]
                ):
                    spacing.pop("target_run_id", None)
                    spacing.pop("target_kind", None)
                    spacing.pop("target_expires_at", None)
            callback_result = _call_sync(
                self._terminal, snapshot, current, dict(final_result)
            )
            if callback_result is not None:
                raise ExecutionBlocked("terminal callback must return None")

        await self.store.mutate(finish)
        self.wake()

    def _write_cooldowns(
        self,
        snapshot: dict,
        item: dict,
        settings: ExecutionConfig,
        finished: datetime,
    ) -> None:
        if _has_manual_participation(item):
            manual_key = _manual_cooldown_key(item)
            if settings.manual_cooldown_seconds == 0:
                snapshot["cooldowns"].pop(manual_key, None)
            else:
                snapshot["cooldowns"][manual_key] = {
                    "kind": _COOLDOWN_KIND,
                    "scope": "manual",
                    "expires_at": _timestamp(
                        _add_seconds(finished, settings.manual_cooldown_seconds)
                    ),
                }
        snapshot["cooldowns"][_SPACING_KEY] = {
            "kind": _COOLDOWN_KIND,
            "scope": "spacing",
            "task_id": item["task_id"],
            "user_key": item["user_key"],
            "completed_at": _timestamp(finished),
        }
        if len(snapshot["cooldowns"]) > MAX_COOLDOWN_ITEMS:
            raise ExecutionBlocked(
                "check-in execution cooldown capacity is unavailable"
            )
