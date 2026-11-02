"""Isolated, identity-checked reload support for this plugin."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_RELOAD_ERROR_TEXT = "插件重载失败。"
_STATE_REGISTRY_ATTRIBUTE = "_cloud_drive_checkin_state_registry"


class ReloadError(RuntimeError):
    """The requested reload could not be completed safely."""

    def __init__(self) -> None:
        super().__init__(_RELOAD_ERROR_TEXT)


@dataclass(frozen=True)
class _ReloadTarget:
    context: Any
    manager: Any
    lock: asyncio.Lock
    metadata: Any
    name: str
    module_path: str
    plugin_id: Any


def _is_async_callable(value: Any) -> bool:
    return callable(value) and inspect.iscoroutinefunction(value)


def _resolve_target(plugin: Any) -> _ReloadTarget:
    context = plugin.context
    manager = context._star_manager
    lock = manager._pm_lock
    if not isinstance(lock, asyncio.Lock):
        raise ReloadError()
    for method_name in ("_terminate_plugin", "_unbind_plugin", "load"):
        if not _is_async_callable(getattr(manager, method_name, None)):
            raise ReloadError()

    get_all_stars = getattr(context, "get_all_stars", None)
    if not callable(get_all_stars) or inspect.iscoroutinefunction(get_all_stars):
        raise ReloadError()
    stars = tuple(get_all_stars())
    owned = tuple(metadata for metadata in stars if metadata.star_cls is plugin)
    if len(owned) != 1:
        raise ReloadError()

    metadata = owned[0]
    module_path = metadata.module_path
    name = metadata.name
    if (
        type(module_path) is not str
        or not module_path
        or module_path != type(plugin).__module__
        or type(name) is not str
        or not name
        or metadata.activated is not True
    ):
        raise ReloadError()

    same_name = tuple(candidate for candidate in stars if candidate.name == name)
    if len(same_name) != 1 or same_name[0] is not metadata:
        raise ReloadError()

    return _ReloadTarget(
        context=context,
        manager=manager,
        lock=lock,
        metadata=metadata,
        name=name,
        module_path=module_path,
        plugin_id=plugin.plugin_id,
    )


def _checked_target(plugin: Any) -> _ReloadTarget:
    try:
        return _resolve_target(plugin)
    except asyncio.CancelledError:
        raise
    except ReloadError:
        raise
    except Exception as error:
        raise ReloadError() from error


def _same_target(current: _ReloadTarget, expected: _ReloadTarget) -> bool:
    return (
        current.context is expected.context
        and current.manager is expected.manager
        and current.lock is expected.lock
        and current.metadata is expected.metadata
        and current.name == expected.name
        and current.module_path == expected.module_path
        and current.plugin_id == expected.plugin_id
    )


def _old_runtime_is_quiescent(plugin: Any) -> bool:
    store = plugin.state_store
    if store is None or store.closed is not True:
        return False

    for attribute in ("queue", "scheduler", "task_control"):
        service = getattr(plugin, attribute)
        if service is not None and service.closed is not True:
            return False

    executor = plugin.executor
    if executor is not None and executor.quiescent is not True:
        return False

    preparation = plugin.preparation
    if preparation is None:
        return True
    if preparation._closed is not True:
        return False
    if preparation._close_task is None or not preparation._close_task.done():
        return False
    if any(not operation.done() for operation in preparation._operations):
        return False
    return preparation._preparing is None or preparation._preparing.done()


def _reload_slot(plugin: Any) -> dict:
    slot = plugin._state_slot()
    if type(slot) is not dict:
        raise ReloadError()
    return slot


def _track_reload(plugin: Any, target: _ReloadTarget) -> dict:
    slot = _reload_slot(plugin)
    previous_owner = plugin.state_store
    if previous_owner is None or slot.get("owner") is not previous_owner:
        raise ReloadError()
    slot.pop("reload_candidate", None)
    slot["reload_target"] = target
    slot["reload_previous_owner"] = previous_owner
    return slot


def _tracked_candidate(plugin: Any, slot: dict, target: _ReloadTarget) -> Any | None:
    candidate = slot.get("reload_candidate")
    if candidate is None:
        return None
    if (
        slot.get("reload_target") is not target
        or slot.get("reload_previous_owner") is not plugin.state_store
        or target.context is not plugin.context
        or target.manager is not target.context._star_manager
        or target.manager._pm_lock is not target.lock
        or target.plugin_id != plugin.plugin_id
        or target.module_path != type(plugin).__module__
        or candidate is plugin
        or candidate.context is not target.context
        or candidate.plugin_id != target.plugin_id
        or type(candidate).__module__ != target.module_path
    ):
        raise ReloadError()

    store = candidate.state_store
    if store is None or store.closed is True:
        return None
    if slot.get("owner") is not store:
        raise ReloadError()

    stars = tuple(target.context.get_all_stars())
    related = tuple(
        metadata
        for metadata in stars
        if metadata.star_cls is candidate
        or metadata.name == target.name
        or metadata.module_path == target.module_path
    )
    if related and (
        len(related) != 1
        or related[0].star_cls is not candidate
        or related[0].name != target.name
        or related[0].module_path != target.module_path
    ):
        raise ReloadError()
    return candidate


def _replacement_is_ready(target: _ReloadTarget, old_plugin: Any, slot: dict) -> bool:
    candidate = _tracked_candidate(old_plugin, slot, target)
    if candidate is None:
        return False

    stars = tuple(target.context.get_all_stars())
    matches = tuple(
        metadata
        for metadata in stars
        if metadata.name == target.name and metadata.module_path == target.module_path
    )
    if len(matches) != 1:
        return False

    metadata = matches[0]
    same_name = tuple(candidate for candidate in stars if candidate.name == target.name)
    if (
        len(same_name) != 1
        or same_name[0] is not metadata
        or metadata.star_cls is not candidate
        or metadata.activated is not True
    ):
        return False

    store = candidate.state_store
    return store.ready is True and store.closed is False


async def _stop_tracked_candidate(
    plugin: Any, slot: dict, target: _ReloadTarget
) -> None:
    candidate = _tracked_candidate(plugin, slot, target)
    if candidate is None:
        return
    terminate = getattr(candidate, "terminate", None)
    if not _is_async_callable(terminate):
        raise ReloadError()
    await terminate()
    if not _old_runtime_is_quiescent(candidate):
        raise ReloadError()


def check_reload_target(plugin: Any) -> None:
    """Validate that ``plugin`` is the one exact reloadable registry target."""

    _checked_target(plugin)


async def reload_plugin(plugin: Any, *, guard: Callable[[], None]) -> None:
    """Terminate, unbind, and reload only the verified current plugin instance."""

    if not callable(guard) or inspect.iscoroutinefunction(guard):
        raise ReloadError()
    target = _checked_target(plugin)
    slot = _track_reload(plugin, target)

    try:
        async with target.lock:
            guard_result = guard()
            if guard_result is not None:
                if inspect.iscoroutine(guard_result):
                    guard_result.close()
                raise ReloadError()

            current = _checked_target(plugin)
            if not _same_target(current, target):
                raise ReloadError()

            await current.manager._terminate_plugin(target.metadata)

            current = _checked_target(plugin)
            if not _same_target(current, target):
                raise ReloadError()
            if not _old_runtime_is_quiescent(plugin):
                raise ReloadError()

            await current.manager._unbind_plugin(target.name, target.module_path)
            try:
                result = await current.manager.load(
                    specified_module_path=target.module_path
                )
                if (
                    type(result) is not tuple
                    or len(result) != 2
                    or result[0] is not True
                ):
                    raise ReloadError()
                if not _replacement_is_ready(target, plugin, slot):
                    raise ReloadError()
            except BaseException:
                await _stop_tracked_candidate(plugin, slot, target)
                raise
    except asyncio.CancelledError:
        raise
    except ReloadError:
        raise
    except Exception as error:
        raise ReloadError() from error


async def cleanup_reload_replacement(plugin: Any) -> None:
    """Stop only the replacement recorded for ``plugin``'s current reload job."""

    try:
        slot = _reload_slot(plugin)
        if slot.get("reload_candidate") is None:
            return
        target = slot.get("reload_target")
        if not isinstance(target, _ReloadTarget):
            raise ReloadError()

        # Only stop the captured object; do not acquire the manager lock.
        # A manager holding it may itself be joining the original reload job.
        await _stop_tracked_candidate(plugin, slot, target)
    except asyncio.CancelledError:
        raise
    except ReloadError:
        raise
    except Exception as error:
        raise ReloadError() from error
