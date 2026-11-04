"""事务式 Star KV 存储，改写自 dev@a662b766（同项目及许可证）。"""
from __future__ import annotations

import asyncio
import copy
import inspect
import math

from .state import InvalidStateError
from .state import MAX_SNAPSHOT_BYTES
from .state import new_snapshot
from .state import STATE_KEY
from .state import validate_snapshot


class StateUnavailableError(RuntimeError):
    """存储尚未就绪或已停止接收修改。"""


class CommitUnconfirmedError(StateUnavailableError):
    """写入未获确认；只能重试原待确认快照。"""


_MISSING = object()


class StateStore:
    """串行提交完整快照，仅在写入确认后替换可读状态。"""

    def __init__(self,
                 kv,
                 *,
                 write_timeout=30.0,
                 max_snapshot_bytes=MAX_SNAPSHOT_BYTES):
        if type(write_timeout) not in (int, float) or not math.isfinite(
                write_timeout) or write_timeout <= 0:
            raise ValueError('write_timeout must be positive and finite')
        if type(max_snapshot_bytes) is not int or max_snapshot_bytes <= 0:
            raise ValueError('max_snapshot_bytes must be positive')
        self._kv = kv
        self._write_timeout = write_timeout
        self._max_snapshot_bytes = max_snapshot_bytes
        self._lock = asyncio.Lock()
        self._confirmed = None
        self._pending = None
        self._closing = False
        self._closed = False

    @property
    def ready(self):
        """是否可以接受新的状态修改。"""
        return (self._confirmed is not None and self._pending is None and
                not self._closing and not self._closed)

    @property
    def closing(self):
        """是否已停止接收新修改。"""
        return self._closing

    @property
    def closed(self):
        """是否已完成关闭及待确认写入。"""
        return self._closed

    @property
    def has_pending(self):
        """是否存在结果未确认的完整写入。"""
        return self._pending is not None

    async def wait_ready(self):
        """等待当前写入结束，但不重试结果不明的提交。"""
        async with self._lock:
            return self.ready

    def stop_accepting(self):
        """阻止新修改，已有写入仍可完成确认。"""
        self._closing = True

    def snapshot(self):
        """返回最后已确认快照的深拷贝；待确认写入不影响此读取。"""
        if self._confirmed is None:
            raise StateUnavailableError('no state snapshot has been confirmed')
        return copy.deepcopy(self._confirmed)

    async def initialize(self):
        """读取并校验状态，确认首次写入后开放修改。"""
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
            self._pending = copy.deepcopy(candidate)
            return await self._commit_pending_locked()

    async def mutate(self, callback):
        """在副本上同步修改，校验后提交；禁止回调修改受管字段。

        Args:
            callback: 同步回调，原地修改候选快照；其返回值不作为结果。

        Returns:
            已确认新快照的深拷贝。

        Raises:
            CommitUnconfirmedError: 已有待确认提交，或本次写入未确认。
            StateUnavailableError: 存储未初始化或正在关闭。
            InvalidStateError: 回调修改受管字段或生成无效快照。
            TypeError: 回调或其返回值为异步对象。
        """
        self._ensure_accepting()
        async with self._lock:
            self._ensure_accepting()
            if self._pending is not None:
                raise CommitUnconfirmedError('a state commit is pending')
            if self._confirmed is None:
                raise StateUnavailableError('state store is not initialized')
            if inspect.iscoroutinefunction(
                    callback) or inspect.isasyncgenfunction(callback):
                raise TypeError('mutation callback must be synchronous')
            candidate = copy.deepcopy(self._confirmed)
            revision, schema = candidate['revision'], candidate[
                'schema_version']
            result = callback(candidate)
            if inspect.isawaitable(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError('mutation callback must be synchronous')
            if type(candidate.get('revision')
                   ) is not int or candidate['revision'] != revision or type(
                       candidate.get('schema_version')
                   ) is not int or candidate['schema_version'] != schema:
                raise InvalidStateError('mutation changed managed fields')
            candidate['revision'] += 1
            validate_snapshot(candidate, max_bytes=self._max_snapshot_bytes)
            self._pending = copy.deepcopy(candidate)
            return await self._commit_pending_locked()

    async def retry_pending(self):
        """仅重试原待确认快照，不合并后续修改。"""
        self._ensure_accepting()
        async with self._lock:
            self._ensure_accepting()
            if self._pending is not None:
                return await self._commit_pending_locked()
            if self._confirmed is None:
                raise StateUnavailableError('state store is not initialized')
            return copy.deepcopy(self._confirmed)

    async def close(self):
        """停止接收修改，等待写入并确认待提交快照后关闭。"""
        self.stop_accepting()
        async with self._lock:
            if self._closed:
                return
            if self._pending is not None:
                await self._commit_pending_locked()
            self._closed = True

    def _ensure_accepting(self):
        if self._closing or self._closed:
            raise StateUnavailableError('state store is not accepting requests')

    async def _commit_pending_locked(self):
        if self._pending is None:
            raise StateUnavailableError('no state commit is pending')
        pending = self._pending
        try:
            await asyncio.wait_for(self._kv.put_kv_data(STATE_KEY,
                                                        copy.deepcopy(pending)),
                                   timeout=self._write_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CommitUnconfirmedError(
                'state commit was not confirmed') from None
        self._confirmed = pending
        self._pending = None
        return copy.deepcopy(pending)
