"""Transactional persistence for the plugin's single state snapshot."""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from collections.abc import Callable
from typing import Any

from .state import MAX_SNAPSHOT_BYTES, STATE_KEY, InvalidStateError, new_snapshot, validate_snapshot


class StateUnavailableError(RuntimeError):
    """Raised when the store has no usable confirmed state."""


class CommitUnconfirmedError(StateUnavailableError):
    """Raised when a complete snapshot write was not confirmed."""


_MISSING = object()


class StateStore:
    """Serialize whole-snapshot changes through the Star asynchronous KV API."""

    def __init__(
        self,
        kv: Any,
        *,
        write_timeout: float = 30.0,
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        if isinstance(write_timeout, bool) or not isinstance(write_timeout, (int, float)):
            raise ValueError("write_timeout must be a positive finite number")
        normalized_timeout = float(write_timeout)
        if normalized_timeout <= 0 or not math.isfinite(normalized_timeout):
            raise ValueError("write_timeout must be a positive finite number")
        if type(max_snapshot_bytes) is not int or max_snapshot_bytes <= 0:
            raise ValueError("max_snapshot_bytes must be a positive integer")

        self._kv = kv
        self._write_timeout = normalized_timeout
        self._max_snapshot_bytes = max_snapshot_bytes
        self._lock = asyncio.Lock()
        self._confirmed: dict[str, Any] | None = None
        self._pending: dict[str, Any] | None = None
        self._closing = False
        self._closed = False

    @property
    def ready(self) -> bool:
        return (
            self._confirmed is not None
            and self._pending is None
            and not self._closing
            and not self._closed
        )

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def stop_accepting(self) -> None:
        """Synchronously prevent new operations from entering the write lock."""

        self._closing = True

    def snapshot(self) -> dict[str, Any]:
        """Return an isolated copy of the last confirmed snapshot."""

        if self._confirmed is None:
            raise StateUnavailableError("no state snapshot has been confirmed")
        return copy.deepcopy(self._confirmed)

    async def initialize(self) -> dict[str, Any]:
        """Load, validate, and durably re-save the complete snapshot."""

        self._ensure_accepting()
        async with self._lock:
            self._ensure_accepting()
            if self._pending is not None:
                return await self._commit_pending_locked()
            if self._confirmed is not None:
                return copy.deepcopy(self._confirmed)

            loaded = await self._kv.get_kv_data(STATE_KEY, _MISSING)
            candidate = new_snapshot() if loaded is _MISSING else loaded
            validate_snapshot(candidate, max_bytes=self._max_snapshot_bytes)

            # Detach from any object retained by the host KV cache.
            self._pending = copy.deepcopy(candidate)
            return await self._commit_pending_locked()

    async def mutate(self, callback: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
        """Apply a synchronous mutation and commit its complete resulting snapshot."""

        self._ensure_accepting()

        async with self._lock:
            self._ensure_accepting()
            if self._pending is not None:
                raise CommitUnconfirmedError("a state commit is pending")
            if self._confirmed is None:
                raise StateUnavailableError("state store is not initialized")
            if inspect.iscoroutinefunction(callback) or inspect.isasyncgenfunction(callback):
                raise TypeError("mutation callback must be synchronous")

            candidate = copy.deepcopy(self._confirmed)
            revision = self._confirmed["revision"]
            schema_version = self._confirmed["schema_version"]

            result = callback(candidate)
            if inspect.isawaitable(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("mutation callback must be synchronous")

            if (
                type(candidate.get("revision")) is not int
                or candidate.get("revision") != revision
                or type(candidate.get("schema_version")) is not int
                or candidate.get("schema_version") != schema_version
            ):
                raise InvalidStateError("mutation callback changed a managed field")

            candidate["revision"] = revision + 1
            validate_snapshot(candidate, max_bytes=self._max_snapshot_bytes)

            # The callback may retain its candidate or objects it inserted into it.
            self._pending = copy.deepcopy(candidate)
            return await self._commit_pending_locked()

    async def retry_pending(self) -> dict[str, Any]:
        """Retry exactly the unconfirmed snapshot, if one exists."""

        self._ensure_accepting()
        async with self._lock:
            self._ensure_accepting()
            if self._pending is not None:
                return await self._commit_pending_locked()
            if self._confirmed is None:
                raise StateUnavailableError("state store is not initialized")
            return copy.deepcopy(self._confirmed)

    async def close(self) -> None:
        """Stop new work, finish current work, and confirm one pending write."""

        self.stop_accepting()
        if self._closed:
            return

        async with self._lock:
            if self._closed:
                return
            if self._pending is not None:
                await self._commit_pending_locked()
            self._closed = True

    def _ensure_accepting(self) -> None:
        if self._closing or self._closed:
            raise StateUnavailableError("state store is not accepting requests")

    async def _commit_pending_locked(self) -> dict[str, Any]:
        pending = self._pending
        if pending is None:
            raise StateUnavailableError("no state commit is pending")

        try:
            await asyncio.wait_for(
                self._kv.put_kv_data(STATE_KEY, copy.deepcopy(pending)),
                timeout=self._write_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CommitUnconfirmedError("state commit was not confirmed") from None

        self._confirmed = pending
        self._pending = None
        return copy.deepcopy(self._confirmed)
