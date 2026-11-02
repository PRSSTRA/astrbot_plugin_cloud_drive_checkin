"""Durable administrative task-disable control."""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from .config import PluginConfigError
from .storage import StateStore
from .task_config import parse_task_catalog

MAX_PENDING_TASK_DISABLES = 32

_INTENT_KIND = "task_disable_intent"
_INTENT_PREFIX = "task_disable:"
_UNCHANGED = object()


class TaskControlError(RuntimeError):
    """Base error for administrative task control."""


class TaskControlSaveUnconfirmed(TaskControlError):
    """The durable task configuration save could not be confirmed."""


class TaskControlNotFound(TaskControlError):
    """The requested task does not exist and has no pending disable intent."""


def disabling_task_ids(snapshot: dict) -> set[str]:
    """Return valid pending task-disable intents without validating other indexes."""

    if type(snapshot) is not dict or type(snapshot.get("dedupe")) is not dict:
        raise TaskControlError("task disable state is invalid")

    task_ids: set[str] = set()
    for key, record in snapshot["dedupe"].items():
        key_is_ours = type(key) is str and key.startswith(_INTENT_PREFIX)
        record_is_ours = type(record) is dict and record.get("kind") == _INTENT_KIND
        if not key_is_ours and not record_is_ours:
            continue
        if (
            not key_is_ours
            or type(record) is not dict
            or record.keys() != {"kind", "task_id"}
            or record.get("kind") != _INTENT_KIND
            or type(record.get("task_id")) is not str
            or not record["task_id"]
            or key != _intent_key(record["task_id"])
        ):
            raise TaskControlError("task disable intent is invalid")
        task_ids.add(record["task_id"])
    if len(task_ids) > MAX_PENDING_TASK_DISABLES:
        raise TaskControlError("too many task disable intents")
    return task_ids


def task_is_disabling(snapshot: dict, task_id: str) -> bool:
    """Return whether an exact, valid disable intent exists for ``task_id``."""

    return task_id in disabling_task_ids(snapshot)


class TaskControlService:
    """Persist task-disable intent before saving the latest framework config."""

    def __init__(self, store: StateStore, config: Mapping) -> None:
        save = getattr(config, "save_config_async", None)
        if (
            not isinstance(store, StateStore)
            or not isinstance(config, Mapping)
            or not callable(save)
        ):
            raise TypeError("task control dependencies are invalid")

        self.store = store
        self.config = config
        self._operation_lock = asyncio.Lock()
        self._jobs: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def disable(self, task_id: str, *, guard: Callable[[], None]) -> None:
        """Durably cancel and disable one current task."""

        self._ensure_admission()
        if not callable(guard):
            raise TaskControlError("task disable guard is invalid")
        job = self._track(self._disable(task_id, guard=guard), "task-control-disable")
        await asyncio.shield(job)

    async def recover(self) -> None:
        """Finish every disable intent left by an unconfirmed config operation."""

        self._ensure_admission()
        job = self._track(self._recover(), "task-control-recover")
        await asyncio.shield(job)

    async def close(self) -> None:
        """Stop admission and drain every admitted config save and retirement."""

        self._closing = True
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._drain(), name="task-control-close"
            )
        await asyncio.shield(self._close_task)

    def _ensure_admission(self) -> None:
        if self._closing or self._closed or not self.store.ready:
            raise TaskControlError("task control is unavailable")

    def _track(self, operation: Any, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(operation, name=name)
        self._jobs.add(task)
        task.add_done_callback(self._job_done)
        return task

    def _job_done(self, task: asyncio.Task[None]) -> None:
        self._jobs.discard(task)
        if not task.cancelled():
            task.exception()

    async def _drain(self) -> None:
        while self._jobs:
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
        self._closed = True

    async def _disable(self, task_id: str, *, guard: Callable[[], None]) -> None:
        async with self._operation_lock:
            if not self.store.ready:
                raise TaskControlError("task control state is unavailable")

            _call_guard(guard)
            catalog = parse_task_catalog(self.config)
            current_intents = disabling_task_ids(self.store.snapshot())
            if (
                not any(task.task_id == task_id for task in catalog.tasks)
                and task_id not in current_intents
            ):
                raise TaskControlNotFound("task was not found")

            def record_intent(snapshot: dict) -> None:
                _call_guard(guard)
                latest_catalog = parse_task_catalog(self.config)
                pending = disabling_task_ids(snapshot)
                if (
                    not any(task.task_id == task_id for task in latest_catalog.tasks)
                    and task_id not in pending
                ):
                    raise TaskControlNotFound("task was not found")
                if task_id not in pending:
                    if len(pending) >= MAX_PENDING_TASK_DISABLES:
                        raise TaskControlError("too many task disable intents")
                    snapshot["dedupe"][_intent_key(task_id)] = _intent(task_id)
                _cancel_task_batches(snapshot, task_id)

            await self.store.mutate(record_intent)
            await self._save_and_retire(task_id)

    async def _recover(self) -> None:
        async with self._operation_lock:
            if not self.store.ready:
                raise TaskControlError("task control state is unavailable")
            for task_id in sorted(disabling_task_ids(self.store.snapshot())):
                await self._save_and_retire(task_id)

    async def _save_and_retire(self, task_id: str) -> None:
        replacement = self._latest_tasks_replacement(task_id)
        save = self.config.save_config_async
        try:
            operation = save() if replacement is _UNCHANGED else save(replacement)
            if not inspect.isawaitable(operation):
                raise TypeError("configuration save is not awaitable")
            confirmed = await operation
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise TaskControlSaveUnconfirmed(
                "task configuration save was not confirmed"
            ) from error

        if confirmed is not True or not self._target_is_disabled_or_absent(task_id):
            raise TaskControlSaveUnconfirmed(
                "task configuration save was not confirmed"
            )

        expected = _intent(task_id)
        key = _intent_key(task_id)

        def retire(snapshot: dict) -> None:
            disabling_task_ids(snapshot)
            if snapshot["dedupe"].get(key) != expected:
                raise TaskControlError("task disable intent changed unexpectedly")
            if not self._target_is_disabled_or_absent(task_id):
                raise TaskControlSaveUnconfirmed(
                    "task configuration changed before disable intent retirement"
                )
            del snapshot["dedupe"][key]

        await self.store.mutate(retire)

    def _latest_tasks_replacement(self, task_id: str) -> object:
        try:
            catalog = parse_task_catalog(self.config)
        except PluginConfigError:
            raise TaskControlSaveUnconfirmed("task configuration is invalid") from None

        target = next((task for task in catalog.tasks if task.task_id == task_id), None)
        if target is None:
            return _UNCHANGED
        if "tasks" not in self.config:
            if target.enabled is False:
                return _UNCHANGED
            raise TaskControlSaveUnconfirmed(
                "enabled default task has no raw configuration"
            )

        raw_tasks = self.config["tasks"]
        try:
            tasks = copy.deepcopy(raw_tasks)
            matched = False
            for raw_task in tasks:
                if isinstance(raw_task, Mapping) and raw_task.get("task_id") == task_id:
                    raw_task["enabled"] = False
                    matched = True
                    break
        except Exception as error:
            raise TaskControlSaveUnconfirmed(
                "task configuration could not be copied"
            ) from error
        if not matched:
            raise TaskControlSaveUnconfirmed("task configuration changed unexpectedly")
        return {"tasks": tasks}

    def _target_is_disabled_or_absent(self, task_id: str) -> bool:
        try:
            catalog = parse_task_catalog(self.config)
        except PluginConfigError:
            return False
        target = next((task for task in catalog.tasks if task.task_id == task_id), None)
        return target is None or target.enabled is False


def _intent_key(task_id: str) -> str:
    return _INTENT_PREFIX + task_id


def _intent(task_id: str) -> dict[str, str]:
    return {"kind": _INTENT_KIND, "task_id": task_id}


def _call_guard(guard: Callable[[], None]) -> None:
    result = guard()
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TaskControlError("task disable guard must be synchronous")
    if result is not None:
        raise TaskControlError("task disable guard must return None")


def _cancel_task_batches(snapshot: dict, task_id: str) -> None:
    for item in snapshot["batches"].values():
        if (
            type(item) is not dict
            or item.get("kind") != "checkin"
            or item.get("task_id") != task_id
            or item.get("status") not in {"queued", "running"}
        ):
            continue
        cancel_requested = item.get("cancel_requested", False)
        if type(cancel_requested) is not bool:
            raise TaskControlError("task execution cancellation state is invalid")
        if cancel_requested:
            continue
        reserve = item.get("_reserve")
        if type(reserve) is not str:
            raise TaskControlError("task execution cancellation state is invalid")
        item["cancel_requested"] = True
        item["_reserve"] = reserve[:-64]
