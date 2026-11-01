"""Owner-scoped controls for persisted credential bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NoReturn

from .credentials import CredentialError, binding_key, user_key
from .permissions import Identity
from .storage import StateStore, StateUnavailableError

_MISSING = object()

_INVALID_MESSAGE = "binding record is invalid"
_NOT_FOUND_MESSAGE = "binding was not found"
_CHANGED_MESSAGE = "binding changed since confirmation"
_BUSY_MESSAGE = "credential enrollment is active"


class BindingError(ValueError):
    """Raised when a binding cannot safely be managed."""


class BindingNotFound(BindingError):
    """Raised when the owner's requested binding does not exist."""


class BindingChanged(BindingError):
    """Raised when a deletion confirmation no longer matches the binding."""


class BindingBusy(BindingError):
    """Raised while the owner has a persisted credential enrollment intent."""


def _fail(error_type: type[BindingError], message: str) -> NoReturn:
    raise error_type(message) from None


def _binding_keys(identity: Identity, task_id: str) -> tuple[str, str]:
    try:
        return user_key(identity), binding_key(identity, task_id)
    except CredentialError:
        _fail(BindingError, _INVALID_MESSAGE)


def _resolve_binding(
    snapshot: dict,
    *,
    owner_key: str,
    target_key: str,
    task_id: str,
) -> dict:
    record = snapshot["bindings"].get(target_key, _MISSING)
    if record is _MISSING:
        _fail(BindingNotFound, _NOT_FOUND_MESSAGE)
    if (
        type(record) is not dict
        or record.get("user_key", _MISSING) != owner_key
        or record.get("task_id", _MISSING) != task_id
    ):
        _fail(BindingError, _INVALID_MESSAGE)
    for field in ("paused", "notify_all"):
        if type(record.get(field, False)) is not bool:
            _fail(BindingError, _INVALID_MESSAGE)
    return record


def _ensure_owner_available(
    snapshot: dict,
    *,
    owner_key: str,
    identity: Identity,
) -> None:
    owner = snapshot["users"].get(owner_key, _MISSING)
    if (
        owner is _MISSING
        or type(owner) is not dict
        or owner.get("platform_id", _MISSING) != identity.platform_id
        or owner.get("qq_id", _MISSING) != identity.qq_id
    ):
        _fail(BindingError, _INVALID_MESSAGE)
    if "enrollment" in owner:
        _fail(BindingBusy, _BUSY_MESSAGE)


def _fingerprint(snapshot: dict, *, target_key: str, record: dict) -> str:
    credential = snapshot["credentials"].get(target_key, _MISSING)
    payload = {
        "binding": record,
        "credential": (
            {"present": False}
            if credential is _MISSING
            else {"present": True, "record": credential}
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_guard(guard: Callable[[], None]) -> None:
    if not callable(guard):
        _fail(BindingError, _INVALID_MESSAGE)


async def _set_preference(
    store: StateStore,
    *,
    identity: Identity,
    task_id: str,
    field: str,
    value: bool,
    guard: Callable[[], None],
) -> None:
    if type(value) is not bool:
        _fail(BindingError, _INVALID_MESSAGE)
    _validate_guard(guard)
    owner_key, target_key = _binding_keys(identity, task_id)

    def mutate(snapshot: dict) -> None:
        guard()
        record = _resolve_binding(
            snapshot,
            owner_key=owner_key,
            target_key=target_key,
            task_id=task_id,
        )
        _ensure_owner_available(snapshot, owner_key=owner_key, identity=identity)
        record[field] = value
        record["updated_at"] = datetime.now(UTC).isoformat()

    await store.mutate(mutate)


async def set_paused(
    store: StateStore,
    *,
    identity: Identity,
    task_id: str,
    paused: bool,
    guard: Callable[[], None],
) -> None:
    """Change only one owner's pause preference and update timestamp."""

    await _set_preference(
        store,
        identity=identity,
        task_id=task_id,
        field="paused",
        value=paused,
        guard=guard,
    )


async def set_notify_all(
    store: StateStore,
    *,
    identity: Identity,
    task_id: str,
    notify_all: bool,
    guard: Callable[[], None],
) -> None:
    """Change only one owner's automatic-result notification preference."""

    await _set_preference(
        store,
        identity=identity,
        task_id=task_id,
        field="notify_all",
        value=notify_all,
        guard=guard,
    )


def delete_version(
    store: StateStore,
    *,
    identity: Identity,
    task_id: str,
) -> str:
    """Return a stable owner-binding fingerprint without writing state."""

    if not store.ready:
        raise StateUnavailableError("state store is not ready")
    owner_key, target_key = _binding_keys(identity, task_id)
    snapshot = store.snapshot()
    record = _resolve_binding(
        snapshot,
        owner_key=owner_key,
        target_key=target_key,
        task_id=task_id,
    )
    _ensure_owner_available(snapshot, owner_key=owner_key, identity=identity)
    return _fingerprint(snapshot, target_key=target_key, record=record)


async def delete_binding(
    store: StateStore,
    *,
    identity: Identity,
    task_id: str,
    expected_version: str,
    guard: Callable[[], None],
) -> None:
    """Atomically delete one confirmed owner binding and its credential record."""

    if type(expected_version) is not str:
        _fail(BindingChanged, _CHANGED_MESSAGE)
    _validate_guard(guard)
    owner_key, target_key = _binding_keys(identity, task_id)

    def mutate(snapshot: dict) -> None:
        guard()
        record = _resolve_binding(
            snapshot,
            owner_key=owner_key,
            target_key=target_key,
            task_id=task_id,
        )
        _ensure_owner_available(snapshot, owner_key=owner_key, identity=identity)
        if (
            _fingerprint(snapshot, target_key=target_key, record=record)
            != expected_version
        ):
            _fail(BindingChanged, _CHANGED_MESSAGE)
        del snapshot["bindings"][target_key]
        snapshot["credentials"].pop(target_key, None)

    await store.mutate(mutate)
