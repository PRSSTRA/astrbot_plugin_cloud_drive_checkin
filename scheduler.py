"""Future-only Cron admission; execution remains owned by the single queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from .config import PluginConfigError, parse_config
from .credentials import CredentialError, binding_key, user_key
from .execution_queue import ExecutionQueue
from .executors.neo import ExecutionBlocked
from .permissions import Identity
from .state import InvalidStateError
from .storage import StateStore, StateUnavailableError
from .task_config import TaskConfig, next_run_times, parse_task_catalog
from .task_control import disabling_task_ids

_POLL_SECONDS = 1.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CronScheduler:
    """One timer, no business retries and no replay of time spent offline."""

    def __init__(
        self,
        store: StateStore,
        queue: ExecutionQueue,
        config: Callable[[], Mapping],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.queue = queue
        self._config = config
        self._clock = clock or _utc_now
        self._plans: dict[str, tuple[str, datetime]] = {}
        self._errors: dict[str, Exception] = {}
        self._last_error: Exception | None = None
        self._last_observed: datetime | None = None
        self._worker: asyncio.Task | None = None
        self._closing = False
        self._closed = False
        self._wake = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_error(self) -> Exception | None:
        return self._last_error or next(iter(self._errors.values()), None)

    async def initialize(self) -> None:
        if self._closed:
            raise ExecutionBlocked("cron scheduler is closed")
        if self._worker is not None:
            return
        # Establish the future baseline before returning control to the caller.
        await self.tick()
        self._worker = asyncio.create_task(self._loop(), name="checkin-cron-admission")
        self._worker.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._last_error = task.exception()

    async def _loop(self) -> None:
        while not self._closing:
            try:
                await self.tick()
            except (
                PluginConfigError,
                StateUnavailableError,
                InvalidStateError,
                ExecutionBlocked,
            ) as error:
                self._last_error = error
                self._plans.clear()
            self._wake.clear()
            if not self._closing:
                try:
                    await asyncio.wait_for(self._wake.wait(), _POLL_SECONDS)
                except TimeoutError:
                    pass

    async def tick(self) -> None:
        """Observe current configuration and admit only the currently due minute."""
        if self._closing or self._closed:
            return
        now = self._clock().astimezone(UTC)
        if not self.store.ready:
            self._plans.clear()
            self._last_observed = now
            return
        raw = self._config()
        permission = parse_config(raw)
        catalog = parse_task_catalog(raw)
        if not permission.enabled:
            self._plans.clear()
            self._errors.clear()
            self._last_error = None
            self._last_observed = now
            return
        if self._last_observed is not None and now < self._last_observed:
            self._plans.clear()
        self._last_observed = now
        disabling = disabling_task_ids(self.store.snapshot())
        enabled = {
            task.task_id: task
            for task in catalog.tasks
            if task.enabled and task.task_id not in disabling
        }
        self._plans = {key: plan for key, plan in self._plans.items() if key in enabled}
        self._errors = {
            key: error for key, error in self._errors.items() if key in enabled
        }
        due: list[tuple[datetime, TaskConfig]] = []
        for task in enabled.values():
            plan = self._plans.get(task.task_id)
            if plan is None or plan[0] != task.version:
                self._plans[task.task_id] = (
                    task.version,
                    next_run_times(task, catalog.timezone, now=now)[0].astimezone(UTC),
                )
                self._errors.pop(task.task_id, None)
                continue
            scheduled_at = plan[1]
            if scheduled_at > now:
                continue
            self._plans[task.task_id] = (
                task.version,
                next_run_times(task, catalog.timezone, now=now)[0].astimezone(UTC),
            )
            # After an in-process delay, inspect just the current minute, not
            # the missed backlog. New/changed plans still take the branch above.
            minute = now.replace(second=0, microsecond=0)
            if scheduled_at < minute:
                scheduled_at = next_run_times(
                    task, catalog.timezone, now=minute - timedelta(microseconds=1)
                )[0].astimezone(UTC)
            if scheduled_at == minute:
                due.append((scheduled_at, task))
        self._last_error = None
        for scheduled_at, task in sorted(
            due, key=lambda pair: (pair[0], pair[1].task_id)
        ):
            if self._closing or not self.store.ready:
                self._plans.clear()
                return
            try:
                requests = self._requests(task, self.store.snapshot())
                await self.queue.enqueue_scheduled(
                    task=task, scheduled_at=scheduled_at, requests=requests
                )
            except StateUnavailableError as error:
                self._last_error = error
                self._plans.clear()
                return
            except (PluginConfigError, InvalidStateError, ExecutionBlocked) as error:
                self._errors[task.task_id] = error
            else:
                self._errors.pop(task.task_id, None)

    @staticmethod
    def _requests(task: TaskConfig, snapshot: dict) -> list[dict]:
        artifact = snapshot["prepared_artifacts"].get(task.task_id)
        if (
            not isinstance(artifact, dict)
            or artifact.get("task_version") != task.version
        ):
            artifact = {}
        requests = []
        for key, binding in snapshot["bindings"].items():
            if not isinstance(binding, dict) or binding.get("task_id") != task.task_id:
                continue
            if binding.get("paused", False) is not False:
                continue
            owner = snapshot["users"].get(binding.get("user_key"))
            if not isinstance(owner, dict):
                raise InvalidStateError("scheduled binding owner is unavailable")
            if "enrollment" in owner:
                continue
            identity = Identity(owner.get("platform_id"), owner.get("qq_id"))
            try:
                if (
                    user_key(identity) != binding.get("user_key")
                    or binding_key(identity, task.task_id) != key
                ):
                    raise InvalidStateError("scheduled binding identity is invalid")
            except CredentialError:
                raise InvalidStateError(
                    "scheduled binding identity is invalid"
                ) from None
            origin = owner.get("unified_msg_origin")
            if origin != f"{identity.platform_id}:FriendMessage:{identity.qq_id}":
                raise InvalidStateError("scheduled binding origin is invalid")
            requests.append(
                {
                    "identity": identity,
                    "origin": origin,
                    "artifact_sha256": artifact.get("sha256"),
                    "inputs_sha256": artifact.get("inputs_sha256"),
                }
            )
        return sorted(
            requests,
            key=lambda item: (item["identity"].platform_id, item["identity"].qq_id),
        )

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        worker = self._worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._worker = None
        self._closed = True
