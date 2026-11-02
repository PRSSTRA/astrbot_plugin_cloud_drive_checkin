"""Durable terminal-result notifications for check-in executions."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .credentials import CredentialError, binding_key, user_key
from .permissions import Identity
from .runners.bootstrap import validate_result
from .storage import StateStore, StateUnavailableError

MAX_OUTBOX_ITEMS = 1000
MAX_SEND_ATTEMPTS = 3
RETRY_DEADLINE_SECONDS = 30.0
RETRY_DELAY_SECONDS = 5.0
OUTBOX_RECHECK_SECONDS = 1.0
SEND_TIMEOUT_SECONDS = 30.0

_KIND = "terminal_notification"
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_TASK_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_QUEUE_REASONS = frozenset(
    {
        "permission_revoked",
        "service_disabled",
        "task_disabled",
        "task_changed",
        "binding_missing",
        "binding_paused",
        "enrollment_active",
        "credentials_missing",
        "credentials_changed",
        "artifact_changed",
        "executor_unavailable",
        "invalid_origin",
        "config_error",
        "queue_expired",
        "interrupted",
        "execution_error",
        "admin_cancelled",
    }
)
_STATUS_LABELS = {
    "success": "成功",
    "already_signed": "今日已签到",
    "failed": "失败",
    "needs_auth": "需要重新认证",
    "unknown": "结果未知",
    "cancelled": "已取消",
    "timeout": "执行超时",
    "config_error": "配置错误",
}
_REASON_LABELS = {
    "reported_result": "任务已返回结构化结果",
    "no_business_evidence": "缺少可靠的业务结果",
    "output_limit": "任务输出超过安全上限",
    "timeout": "执行超过时限",
    "runner_error": "执行器启动失败",
    "cancelled": "执行已取消",
}
_QUEUE_REASON_LABELS = {
    "permission_revoked": "使用权限已撤销",
    "service_disabled": "签到服务已停用",
    "task_disabled": "任务已停用",
    "task_changed": "任务配置已变更",
    "binding_missing": "本人绑定不存在",
    "binding_paused": "本人绑定已暂停",
    "enrollment_active": "凭据录入尚未完成",
    "credentials_missing": "本人凭据不存在",
    "credentials_changed": "本人凭据已变更",
    "artifact_changed": "任务制品已变更",
    "executor_unavailable": "执行服务不可用",
    "invalid_origin": "消息来源不受支持",
    "config_error": "任务配置不可用",
    "queue_expired": "排队等待已超时",
    "interrupted": "执行被中断",
    "execution_error": "执行服务发生错误",
    "admin_cancelled": "管理员已请求取消",
}
_DEFAULT_RESULT = {
    "protocol": 1,
    "process_status": "interrupted",
    "exit_code": None,
    "business_status": "unknown",
    "reason_code": "no_business_evidence",
    "duration_ms": 0,
}
_IDENTITY_FIELDS = (
    "run_id",
    "origin",
    "platform_id",
    "qq_id",
    "task_id",
    "task_name",
)
_PARTICIPATION_FIELDS = ("source", "manual_requested")
_TERMINAL_IDENTITY_FIELDS = (
    "run_id",
    "platform_id",
    "qq_id",
    "task_id",
    "task_name",
)


class NotificationError(RuntimeError):
    """Base class for notification admission errors."""


class NotificationCapacityError(NotificationError):
    """The durable outbox has no unreserved slot."""


class NotificationReservationError(NotificationError):
    """A terminal notification does not match a durable reservation."""


def _required_string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if type(value) is not str or not value:
        raise NotificationReservationError("notification identity is invalid")
    return value


def _participation(item: Mapping[str, Any]) -> tuple[str, bool]:
    source = item.get("source")
    manual_requested = item.get("manual_requested")
    if source not in {"manual", "automatic"} or type(manual_requested) is not bool:
        raise NotificationReservationError("notification participation is invalid")
    return source, manual_requested


def _reservation(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise NotificationReservationError("notification item is invalid")
    values = {key: _required_string(item, key) for key in _IDENTITY_FIELDS}
    source, manual_requested = _participation(item)
    if _RUN_ID.fullmatch(values["run_id"]) is None:
        raise NotificationReservationError("notification run identifier is invalid")
    if _TASK_ID.fullmatch(values["task_id"]) is None:
        raise NotificationReservationError("notification task identifier is invalid")
    queued_at = _required_string(item, "queued_at")
    return {
        "kind": _KIND,
        **values,
        "source": source,
        "manual_requested": manual_requested,
        "phase": "reserved",
        "message": None,
        "result": None,
        "attempts": 0,
        "next_attempt_at": None,
        "retry_deadline_at": None,
        "created_at": queued_at,
        "finished_at": None,
        # The message repeats task_name; reserve its worst JSON expansion plus
        # fixed labels, result fields, and delivery metadata before admission.
        "_reserve": " " * (len(values["task_name"]) * 6 + 4096),
    }


def _matches_reservation(record: object, expected: Mapping[str, Any]) -> bool:
    return (
        type(record) is dict
        and record.get("kind") == _KIND
        and all(
            record.get(key) == expected.get(key)
            for key in (*_IDENTITY_FIELDS, *_PARTICIPATION_FIELDS)
        )
    )


def _matches_terminal(record: object, item: Mapping[str, Any]) -> bool:
    return (
        type(record) is dict
        and record.get("kind") == _KIND
        and all(record.get(key) == item.get(key) for key in _TERMINAL_IDENTITY_FIELDS)
        and record.get("source") == item.get("source")
    )


def _record_order_key(entry: tuple[str, dict[str, Any]]) -> tuple[str, str]:
    run_id, record = entry
    timestamp = record.get("finished_at") or record.get("created_at")
    return (timestamp if type(timestamp) is str else "", run_id)


def terminal_capacity(snapshot: Mapping[str, Any]) -> bool:
    """Return whether queue pruning has left room for one reservation."""

    return (
        isinstance(snapshot, Mapping)
        and type(snapshot.get("outbox")) is dict
        and len(snapshot["outbox"]) < MAX_OUTBOX_ITEMS
    )


def reserve_terminal(snapshot: dict[str, Any], item: Mapping[str, Any]) -> None:
    """Reserve one outbox slot in the queue's atomic admission mutation."""

    expected = _reservation(item)
    outbox = snapshot.get("outbox")
    if type(outbox) is not dict:
        raise NotificationReservationError("notification outbox is invalid")
    run_id = expected["run_id"]
    existing = outbox.get(run_id)
    if existing is not None:
        if _matches_reservation(existing, expected):
            return
        raise NotificationReservationError("notification reservation conflicts")

    if len(outbox) >= MAX_OUTBOX_ITEMS:
        raise NotificationCapacityError("notification outbox is full")
    outbox[run_id] = expected


def _safe_result(result: object) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return dict(_DEFAULT_RESULT)
    core = {
        key: result.get(key)
        for key in (
            "protocol",
            "process_status",
            "exit_code",
            "business_status",
            "reason_code",
            "duration_ms",
        )
    }
    try:
        safe = validate_result(core)
    except (TypeError, ValueError, RecursionError):
        duration = result.get("duration_ms")
        safe = dict(_DEFAULT_RESULT)
        if type(duration) is int and 0 <= duration <= 300_000:
            safe["duration_ms"] = duration
    queue_reason = result.get("queue_reason")
    if type(queue_reason) is str and queue_reason in _QUEUE_REASONS:
        safe["queue_reason"] = queue_reason
    return safe


def _guidance(result: Mapping[str, Any], task_id: str) -> str:
    queue_reason = result.get("queue_reason")
    if queue_reason == "admin_cancelled":
        return "请查询 /签到 状态 并联系管理员；结果未知时不要重复执行。"
    if queue_reason in {"binding_missing", "credentials_missing"}:
        return f"请使用 /签到 录入 {task_id} 保存本人凭据后再执行。"
    if queue_reason == "binding_paused":
        return f"请使用 /签到 恢复 {task_id} 恢复本人绑定后再执行。"
    if queue_reason == "enrollment_active":
        return "请先完成当前凭据录入，或发送 /取消 退出录入后再执行。"
    if queue_reason == "invalid_origin":
        return "请在受支持的 QQ 普通私聊中重新发起签到。"
    if queue_reason in {
        "permission_revoked",
        "service_disabled",
        "task_disabled",
        "config_error",
    }:
        return "请联系管理员确认权限、服务与任务配置后再执行。"
    if queue_reason in {
        "task_changed",
        "credentials_changed",
        "artifact_changed",
        "queue_expired",
    }:
        return "请先查询 /签到 状态，确认最新状态后再执行。"
    if queue_reason in {
        "executor_unavailable",
        "interrupted",
        "execution_error",
    }:
        return "请先查询 /签到 状态；持续异常请联系管理员。"

    business_status = result["business_status"]
    if business_status in {"success", "already_signed"}:
        return "无需操作。"
    if business_status == "needs_auth":
        return f"请使用 /签到 录入 {task_id} 更新本人凭据后再执行。"
    if business_status == "cancelled":
        return "请检查本人绑定、任务与权限状态后再执行。"
    if business_status == "unknown":
        return "请先查询 /签到 状态，勿重复执行；持续未知请联系管理员。"
    if business_status == "timeout":
        return "请先查询 /签到 状态；持续超时请联系管理员。"
    return "请先查询 /签到 状态；持续失败请联系管理员检查任务配置。"


def _render(record: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    queue_reason = result.get("queue_reason")
    reason_label = (
        _QUEUE_REASON_LABELS[queue_reason]
        if queue_reason in _QUEUE_REASON_LABELS
        else _REASON_LABELS.get(result["reason_code"], "执行结果无法确认")
    )
    participation = (
        "手动"
        if record.get("source") == "manual" or record.get("manual_requested") is True
        else "自动"
    )
    return "\n".join(
        (
            f"{participation}签到结果",
            f"任务：{record['task_name']}",
            f"运行 ID：{record['run_id']}",
            f"状态：{_STATUS_LABELS.get(result['business_status'], '结果未知')}",
            f"原因：{reason_label}",
            f"耗时：{result['duration_ms']} 毫秒",
            f"处理建议：{_guidance(result, record['task_id'])}",
        )
    )


def _current_notify_all(snapshot: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    try:
        identity = Identity(
            _required_string(item, "platform_id"),
            _required_string(item, "qq_id"),
        )
        task_id = _required_string(item, "task_id")
        owner_key = user_key(identity)
        target_key = binding_key(identity, task_id)
    except (CredentialError, NotificationReservationError):
        return False
    if item.get("user_key") != owner_key or item.get("binding_key") != target_key:
        return False
    bindings = snapshot.get("bindings")
    binding = bindings.get(target_key) if type(bindings) is dict else None
    return (
        type(binding) is dict
        and binding.get("user_key") == owner_key
        and binding.get("task_id") == task_id
        and binding.get("notify_all") is True
    )


def enqueue_terminal(
    snapshot: dict[str, Any], item: Mapping[str, Any], result: object
) -> None:
    """Fill a matching reservation in the queue's atomic terminal mutation."""

    outbox = snapshot.get("outbox")
    if type(outbox) is not dict or not isinstance(item, Mapping):
        raise NotificationReservationError("notification reservation is unavailable")
    source, manual_requested = _participation(item)
    run_id = item.get("run_id")
    record = outbox.get(run_id) if type(run_id) is str else None
    if record is None or not _matches_terminal(record, item):
        raise NotificationReservationError("notification reservation is unavailable")
    if record.get("phase") != "reserved":
        return

    origin = _required_string(item, "origin")
    safe_result = _safe_result(result)
    finished_at = item.get("finished_at")
    if type(finished_at) is not str or not finished_at:
        finished_at = record["created_at"]
    mandatory = source == "manual" or manual_requested
    should_send = (
        mandatory
        or safe_result["business_status"] not in {"success", "already_signed"}
        or _current_notify_all(snapshot, item)
    )
    record.update(
        origin=origin,
        source=source,
        manual_requested=manual_requested,
        phase="pending" if should_send else "suppressed",
        result=safe_result,
        attempts=0,
        next_attempt_at=0.0 if should_send else None,
        retry_deadline_at=None,
        finished_at=finished_at,
    )
    record["message"] = _render(record, safe_result)
    record.pop("_reserve", None)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


class NotificationService:
    """Single durable outbox sender; ambiguous sends are never replayed."""

    def __init__(
        self,
        store: StateStore,
        send: Callable[[str, str], Awaitable[bool]],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        startup_ready: Callable[[], bool] = lambda: True,
    ) -> None:
        if not callable(send):
            raise TypeError("send must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        if (
            not callable(startup_ready)
            or inspect.iscoroutinefunction(startup_ready)
            or inspect.isasyncgenfunction(startup_ready)
        ):
            raise TypeError(
                "notification startup readiness callback must be synchronous"
            )
        self._store = store
        self._send = send
        self._clock = clock
        self._sleep = sleep
        self._startup_ready = startup_ready
        self._wake_event = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._draining = False
        self._closed = False
        self._frozen = False

    @property
    def frozen(self) -> bool:
        """Whether an unconfirmed or invalid store operation stopped delivery."""

        return self._frozen

    def _now(self) -> float:
        now = _number(self._clock())
        if now is None:
            raise RuntimeError("notification clock is invalid")
        return now

    def _startup_is_ready(self) -> bool:
        ready = self._startup_ready()
        if inspect.iscoroutine(ready):
            ready.close()
            raise RuntimeError(
                "notification startup readiness callback must be synchronous"
            )
        if type(ready) is not bool:
            raise RuntimeError(
                "notification startup readiness callback must return a boolean"
            )
        return ready

    async def initialize(self) -> None:
        """Resolve crash-ambiguous sends, then start the outbox worker."""

        if self._closed or self._stopping:
            raise RuntimeError("notification service is closed")
        if self._worker is not None:
            return
        if not self._store.ready:
            raise StateUnavailableError("notification state is unavailable")

        snapshot = self._store.snapshot()
        sending = [
            run_id
            for run_id, record in snapshot["outbox"].items()
            if type(record) is dict
            and record.get("kind") == _KIND
            and record.get("phase") == "sending"
        ]
        if sending:
            unknown_at = self._now()

            def recover(candidate: dict[str, Any]) -> None:
                for run_id in sending:
                    record = candidate["outbox"].get(run_id)
                    if (
                        type(record) is dict
                        and record.get("kind") == _KIND
                        and record.get("phase") == "sending"
                    ):
                        record.update(
                            phase="unknown",
                            next_attempt_at=None,
                            unknown_at=unknown_at,
                        )

            await self._store.mutate(recover)
        if self._closed or self._stopping:
            raise RuntimeError("notification service is closed")
        self._worker = asyncio.create_task(self._run())

    def wake(self) -> None:
        """Wake the sender after an atomic queue terminal commit."""

        if not self._closed:
            self._wake_event.set()

    async def _run(self) -> None:
        work = asyncio.create_task(self._work_loop())
        try:
            outcome = (await asyncio.gather(work, return_exceptions=True))[0]
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return
        if isinstance(outcome, asyncio.CancelledError):
            return
        if isinstance(outcome, BaseException):
            self._frozen = True

    async def _work_loop(self) -> None:
        while not self._stopping:
            self._wake_event.clear()
            claimed, delay = await self._claim()
            if claimed is not None:
                should_continue = await self._deliver(claimed)
                if not should_continue:
                    return
                continue
            if self._draining:
                if delay == 0:
                    continue
                return
            if self._stopping or self._wake_event.is_set():
                continue
            timeout = OUTBOX_RECHECK_SECONDS
            if delay is not None:
                timeout = min(timeout, max(0.0, delay))
            if timeout > 0:
                await self._wait(timeout)

    async def _wait(self, timeout: float) -> None:
        wake = asyncio.create_task(self._wake_event.wait())
        recheck = asyncio.create_task(self._sleep(timeout))
        tasks = (wake, recheck)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _claim(self) -> tuple[dict[str, Any] | None, float | None]:
        if not self._store.ready:
            return None, OUTBOX_RECHECK_SECONDS
        if not self._startup_is_ready():
            return None, OUTBOX_RECHECK_SECONDS
        now = self._now()
        snapshot = self._store.snapshot()
        pending: list[tuple[str, dict[str, Any]]] = []
        for run_id, record in snapshot["outbox"].items():
            if (
                type(record) is dict
                and record.get("kind") == _KIND
                and record.get("phase") == "pending"
            ):
                pending.append((run_id, record))
        pending.sort(key=_record_order_key)

        delay: float | None = None
        for run_id, record in pending:
            attempts = record.get("attempts")
            next_attempt = _number(record.get("next_attempt_at"))
            deadline = _number(record.get("retry_deadline_at"))
            valid = (
                type(attempts) is int
                and 0 <= attempts <= MAX_SEND_ATTEMPTS
                and next_attempt is not None
                and (deadline is None or deadline >= 0)
                and type(record.get("origin")) is str
                and bool(record.get("origin"))
                and type(record.get("message")) is str
                and bool(record.get("message"))
            )
            if not valid:
                await self._settle(run_id, "unknown", now)
                return None, 0.0
            if attempts >= MAX_SEND_ATTEMPTS or (
                deadline is not None and now >= deadline
            ):
                await self._settle(run_id, "failed", now)
                return None, 0.0
            due_at = next_attempt
            if due_at <= now:
                first_deadline = (
                    now + RETRY_DEADLINE_SECONDS if deadline is None else deadline
                )

                def claim(
                    candidate: dict[str, Any],
                    claim_run_id: str = run_id,
                    claim_attempts: int = attempts,
                    claim_deadline: float = first_deadline,
                    claim_now: float = now,
                ) -> None:
                    current = candidate["outbox"].get(claim_run_id)
                    if (
                        type(current) is dict
                        and current.get("kind") == _KIND
                        and current.get("phase") == "pending"
                        and current.get("attempts") == claim_attempts
                        and self._startup_is_ready()
                    ):
                        current.update(
                            phase="sending",
                            attempts=claim_attempts + 1,
                            next_attempt_at=None,
                            retry_deadline_at=claim_deadline,
                            sending_at=claim_now,
                        )

                committed = await self._store.mutate(claim)
                current = committed["outbox"].get(run_id)
                if (
                    type(current) is dict
                    and current.get("kind") == _KIND
                    and current.get("phase") == "sending"
                    and current.get("attempts") == attempts + 1
                ):
                    return current, 0.0
                return None, 0.0
            wait_until = due_at if deadline is None else min(due_at, deadline)
            remaining = max(0.0, wait_until - now)
            delay = remaining if delay is None else min(delay, remaining)
        return None, delay

    async def _settle(self, run_id: str, phase: str, now: float) -> None:
        def settle(candidate: dict[str, Any]) -> None:
            record = candidate["outbox"].get(run_id)
            if type(record) is not dict or record.get("kind") != _KIND:
                return
            if phase == "unknown" and record.get("phase") not in {
                "pending",
                "sending",
            }:
                return
            if phase == "failed" and record.get("phase") not in {
                "pending",
                "sending",
            }:
                return
            record.update(phase=phase, next_attempt_at=None)
            record[f"{phase}_at"] = now

        await self._store.mutate(settle)

    async def _invoke_send(self, origin: str, message: str) -> bool:
        return await self._send(origin, message)

    async def _deliver(self, record: Mapping[str, Any]) -> bool:
        run_id = record["run_id"]
        try:
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                delivered = (
                    await asyncio.gather(
                        self._invoke_send(record["origin"], record["message"]),
                        return_exceptions=True,
                    )
                )[0]
        except TimeoutError:
            await self._settle(run_id, "unknown", self._now())
            return True
        except asyncio.CancelledError:
            await self._settle(run_id, "unknown", self._now())
            task = asyncio.current_task()
            return not self._stopping and task is not None and not task.cancelling()
        if isinstance(delivered, BaseException):
            await self._settle(run_id, "unknown", self._now())
            return True

        now = self._now()
        if delivered is True:
            await self._settle(run_id, "sent", now)
            return True
        if delivered is not False:
            await self._settle(run_id, "unknown", now)
            return True

        attempts = record["attempts"]
        deadline = record["retry_deadline_at"]
        if attempts >= MAX_SEND_ATTEMPTS or now >= deadline:
            await self._settle(run_id, "failed", now)
            return True

        next_attempt = min(now + RETRY_DELAY_SECONDS, deadline)

        def retry(candidate: dict[str, Any]) -> None:
            current = candidate["outbox"].get(run_id)
            if (
                type(current) is dict
                and current.get("kind") == _KIND
                and current.get("phase") == "sending"
                and current.get("attempts") == attempts
            ):
                current.update(
                    phase="pending",
                    next_attempt_at=next_attempt,
                    last_not_delivered_at=now,
                )

        await self._store.mutate(retry)
        return True

    async def close(self, *, drain: bool = False) -> None:
        """Stop delivery, optionally draining notices that are immediately due."""

        if self._closed:
            return
        worker = self._worker
        self._draining = bool(
            drain
            and worker is not None
            and not worker.done()
            and not self._frozen
            and self._store.ready
            and self._startup_is_ready()
        )
        if not self._draining:
            self._stopping = True
        self._wake_event.set()
        if worker is not None:
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                self._draining = False
                self._stopping = True
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                self._closed = True
                raise
        self._draining = False
        self._stopping = True
        self._closed = True
