from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NoReturn

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .permissions import Identity
from .storage import StateStore
from .task_config import TaskConfig, UserField

_MASTER_KEY_ENV = "CLOUD_DRIVE_CHECKIN_MASTER_KEY"
_FORMAT_VERSION = "v1"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_PLAINTEXT_BYTES = 512 * 1024
_MAX_FIELD_BYTES = 16 * 1024
_MAX_TOTAL_VALUE_BYTES = 64 * 1024
_MAX_ENV_FIELDS = 64
_MAX_ENVELOPE_BYTES = _NONCE_BYTES + _MAX_PLAINTEXT_BYTES + _TAG_BYTES
_MAX_ENVELOPE_TEXT = 4 * ((_MAX_ENVELOPE_BYTES + 2) // 3)
_MISSING = object()

_MISSING_KEY_MESSAGE = "credential master key is not configured"
_INVALID_KEY_MESSAGE = "credential master key is invalid"
_INVALID_VALUES_MESSAGE = "credential values are invalid"
_INVALID_BINDING_MESSAGE = "credential binding is invalid"
_ENCRYPTION_MESSAGE = "credentials could not be encrypted"
_DECRYPTION_MESSAGE = "credentials could not be decrypted"
_OWNERSHIP_MESSAGE = "credential binding ownership is invalid"


class CredentialError(ValueError):
    """Raised when credentials or their cryptographic binding are invalid."""


def _fail(message: str) -> NoReturn:
    raise CredentialError(message) from None


def _decode_canonical_base64(value: object) -> bytes:
    if type(value) is not str:
        raise ValueError
    encoded = value.encode("ascii")
    decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded) != encoded:
        raise ValueError
    return decoded


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_json_constant(_constant: str) -> NoReturn:
    raise ValueError


def _validate_plain_values(values: object) -> None:
    if type(values) is not dict:
        _fail(_INVALID_VALUES_MESSAGE)
    for field_id, value in values.items():
        if type(field_id) is not str or type(value) not in (str, int, bool):
            _fail(_INVALID_VALUES_MESSAGE)
        try:
            field_id.encode("utf-8")
            if type(value) is str:
                if "\0" in value:
                    _fail(_INVALID_VALUES_MESSAGE)
                value.encode("utf-8")
        except UnicodeEncodeError:
            _fail(_INVALID_VALUES_MESSAGE)


def _serialize_values(values: object) -> bytes:
    _validate_plain_values(values)
    try:
        plaintext = _json_bytes(values)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail(_INVALID_VALUES_MESSAGE)
    if len(plaintext) > _MAX_PLAINTEXT_BYTES:
        _fail(_INVALID_VALUES_MESSAGE)
    return plaintext


def _binding_parts(
    identity: object,
    task_id: object,
    task_version: object,
) -> tuple[str, str, str, str]:
    if not isinstance(identity, Identity):
        _fail(_INVALID_BINDING_MESSAGE)
    parts = (identity.platform_id, identity.qq_id, task_id, task_version)
    if any(type(part) is not str for part in parts):
        _fail(_INVALID_BINDING_MESSAGE)
    try:
        for part in parts:
            part.encode("utf-8")
    except UnicodeEncodeError:
        _fail(_INVALID_BINDING_MESSAGE)
    return parts


def _associated_data(identity: object, task_id: object, task_version: object) -> bytes:
    platform_id, qq_id, normalized_task_id, normalized_task_version = _binding_parts(
        identity, task_id, task_version
    )
    try:
        return _json_bytes(
            {
                "format": _FORMAT_VERSION,
                "platform_id": platform_id,
                "qq_id": qq_id,
                "task_id": normalized_task_id,
                "task_version": normalized_task_version,
            }
        )
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail(_INVALID_BINDING_MESSAGE)


def _tuple_key(parts: tuple[str, ...]) -> str:
    try:
        return json.dumps(
            parts,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail(_INVALID_BINDING_MESSAGE)


class CredentialCipher:
    """Encrypt and authenticate credential dictionaries for one binding context."""

    __slots__ = ("_cipher",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            _fail(_INVALID_KEY_MESSAGE)
        try:
            self._cipher = AESGCM(key)
        except (TypeError, ValueError, UnsupportedAlgorithm):
            _fail(_INVALID_KEY_MESSAGE)

    def __repr__(self) -> str:
        return "CredentialCipher()"

    @classmethod
    def from_environment(cls) -> CredentialCipher:
        encoded_key = os.environ.get(_MASTER_KEY_ENV)
        if encoded_key is None:
            _fail(_MISSING_KEY_MESSAGE)
        try:
            key = _decode_canonical_base64(encoded_key)
        except (TypeError, ValueError, UnicodeError):
            _fail(_INVALID_KEY_MESSAGE)
        if len(key) != 32:
            _fail(_INVALID_KEY_MESSAGE)
        return cls(key)

    def encrypt(
        self,
        values: dict[str, str | int | bool],
        *,
        identity: Identity,
        task_id: str,
        task_version: str,
    ) -> str:
        plaintext = _serialize_values(values)
        associated_data = _associated_data(identity, task_id, task_version)
        try:
            nonce = os.urandom(_NONCE_BYTES)
            encrypted = self._cipher.encrypt(nonce, plaintext, associated_data)
            envelope = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
        except (TypeError, ValueError, OverflowError, OSError, UnsupportedAlgorithm):
            _fail(_ENCRYPTION_MESSAGE)
        return f"{_FORMAT_VERSION}.{envelope}"

    def decrypt(
        self,
        token: str,
        *,
        identity: Identity,
        task_id: str,
        task_version: str,
    ) -> dict[str, str | int | bool]:
        try:
            if type(token) is not str:
                raise ValueError
            prefix, encoded = token.split(".")
            if (
                prefix != _FORMAT_VERSION
                or not encoded
                or len(encoded) > _MAX_ENVELOPE_TEXT
            ):
                raise ValueError
            envelope = _decode_canonical_base64(encoded)
            if not _NONCE_BYTES + _TAG_BYTES <= len(envelope) <= _MAX_ENVELOPE_BYTES:
                raise ValueError
            associated_data = _associated_data(identity, task_id, task_version)
            plaintext = self._cipher.decrypt(
                envelope[:_NONCE_BYTES],
                envelope[_NONCE_BYTES:],
                associated_data,
            )
            if len(plaintext) > _MAX_PLAINTEXT_BYTES:
                raise ValueError
            values = json.loads(plaintext, parse_constant=_reject_json_constant)
            _validate_plain_values(values)
        except CredentialError:
            _fail(_DECRYPTION_MESSAGE)
        except (InvalidTag, TypeError, ValueError, OverflowError, RecursionError):
            _fail(_DECRYPTION_MESSAGE)
        return values


def user_key(identity: Identity) -> str:
    platform_id, qq_id, _, _ = _binding_parts(identity, "", "")
    return _tuple_key((platform_id, qq_id))


def binding_key(identity: Identity, task_id: str) -> str:
    platform_id, qq_id, normalized_task_id, _ = _binding_parts(identity, task_id, "")
    return _tuple_key((platform_id, qq_id, normalized_task_id))


def _declared_fields(task: object) -> tuple[UserField, ...]:
    if not isinstance(task, TaskConfig) or type(task.user_fields) is not tuple:
        _fail(_INVALID_VALUES_MESSAGE)

    seen: set[str] = set()
    for field in task.user_fields:
        if (
            not isinstance(field, UserField)
            or type(field.field_id) is not str
            or field.field_id in seen
            or type(field.value_type) is not str
            or field.value_type not in {"string", "int", "bool"}
            or type(field.required) is not bool
        ):
            _fail(_INVALID_VALUES_MESSAGE)
        try:
            field.field_id.encode("utf-8")
        except UnicodeEncodeError:
            _fail(_INVALID_VALUES_MESSAGE)
        seen.add(field.field_id)
    return task.user_fields


def _fixed_environment_bytes(task: TaskConfig, user_field_count: int) -> int:
    if type(task.fixed_env) is not tuple:
        _fail(_INVALID_VALUES_MESSAGE)
    if len(task.fixed_env) + user_field_count > _MAX_ENV_FIELDS:
        _fail(_INVALID_VALUES_MESSAGE)
    total_bytes = 0
    for item in task.fixed_env:
        if type(item) is not tuple or len(item) != 2:
            _fail(_INVALID_VALUES_MESSAGE)
        name, value = item
        if type(name) is not str or type(value) is not str or "\0" in value:
            _fail(_INVALID_VALUES_MESSAGE)
        try:
            name_bytes = name.encode("utf-8")
            value_bytes = value.encode("utf-8")
        except UnicodeEncodeError:
            _fail(_INVALID_VALUES_MESSAGE)
        if (
            not name_bytes
            or len(name_bytes) > _MAX_FIELD_BYTES
            or len(value_bytes) > _MAX_FIELD_BYTES
        ):
            _fail(_INVALID_VALUES_MESSAGE)
        total_bytes += len(value_bytes)
        if total_bytes > _MAX_TOTAL_VALUE_BYTES:
            _fail(_INVALID_VALUES_MESSAGE)
    return total_bytes


def validate_values(task: TaskConfig, values: dict[str, str | int | bool]) -> None:
    """Validate direct-API values against the task's declared credential fields."""

    fields = _declared_fields(task)
    if type(values) is not dict:
        _fail(_INVALID_VALUES_MESSAGE)

    declared = {field.field_id: field for field in fields}
    if any(
        type(field_id) is not str or field_id not in declared for field_id in values
    ):
        _fail(_INVALID_VALUES_MESSAGE)
    if any(field.required and field.field_id not in values for field in fields):
        _fail(_INVALID_VALUES_MESSAGE)

    total_bytes = _fixed_environment_bytes(task, len(fields))
    expected_types = {"string": str, "int": int, "bool": bool}
    for field_id, value in values.items():
        expected_type = expected_types[declared[field_id].value_type]
        if type(value) is not expected_type:
            _fail(_INVALID_VALUES_MESSAGE)
        try:
            if type(value) is str:
                if (declared[field_id].required and value == "") or "\0" in value:
                    _fail(_INVALID_VALUES_MESSAGE)
                encoded_value = value.encode("utf-8")
            elif type(value) is bool:
                encoded_value = b"true" if value else b"false"
            else:
                encoded_value = str(value).encode("ascii")
        except (ValueError, OverflowError, UnicodeError):
            _fail(_INVALID_VALUES_MESSAGE)
        if len(encoded_value) > _MAX_FIELD_BYTES:
            _fail(_INVALID_VALUES_MESSAGE)
        total_bytes += len(encoded_value)
        if total_bytes > _MAX_TOTAL_VALUE_BYTES:
            _fail(_INVALID_VALUES_MESSAGE)

    _serialize_values(values)


def _existing_preferences(
    snapshot: dict,
    *,
    owner_key: str,
    target_binding_key: str,
    task_id: str,
) -> tuple[bool, bool]:
    existing = snapshot["bindings"].get(target_binding_key, _MISSING)
    if existing is _MISSING:
        return False, False
    if (
        type(existing) is not dict
        or existing.get("user_key") != owner_key
        or existing.get("task_id") != task_id
    ):
        _fail(_OWNERSHIP_MESSAGE)

    preferences: list[bool] = []
    for name in ("paused", "notify_all"):
        value = existing.get(name, False)
        if type(value) is not bool:
            _fail(_OWNERSHIP_MESSAGE)
        preferences.append(value)
    return preferences[0], preferences[1]


def _ensure_existing_user(
    snapshot: dict, *, owner_key: str, identity: Identity
) -> None:
    existing = snapshot["users"].get(owner_key, _MISSING)
    if existing is _MISSING:
        return
    if (
        type(existing) is not dict
        or existing.get("platform_id") != identity.platform_id
        or existing.get("qq_id") != identity.qq_id
    ):
        _fail(_OWNERSHIP_MESSAGE)


async def save_binding(
    store: StateStore,
    cipher: CredentialCipher,
    *,
    identity: Identity,
    origin: str,
    task: TaskConfig,
    values: dict[str, str | int | bool],
    guard: Callable[[], None],
) -> None:
    """Atomically save one user's binding and encrypted credential values."""

    validate_values(task, values)
    if type(origin) is not str or not callable(guard):
        _fail(_INVALID_BINDING_MESSAGE)

    owner_key = user_key(identity)
    target_binding_key = binding_key(identity, task.task_id)

    guard()
    ciphertext = cipher.encrypt(
        values,
        identity=identity,
        task_id=task.task_id,
        task_version=task.version,
    )

    def mutate(snapshot: dict) -> None:
        guard()
        _ensure_existing_user(snapshot, owner_key=owner_key, identity=identity)
        paused, notify_all = _existing_preferences(
            snapshot,
            owner_key=owner_key,
            target_binding_key=target_binding_key,
            task_id=task.task_id,
        )
        updated_at = datetime.now(UTC).isoformat()

        snapshot["users"][owner_key] = {
            "platform_id": identity.platform_id,
            "qq_id": identity.qq_id,
            "unified_msg_origin": origin,
        }
        snapshot["bindings"][target_binding_key] = {
            "user_key": owner_key,
            "task_id": task.task_id,
            "task_version": task.version,
            "paused": paused,
            "notify_all": notify_all,
            "updated_at": updated_at,
        }
        snapshot["credentials"][target_binding_key] = {
            "ciphertext": ciphertext,
            "task_version": task.version,
        }

    await store.mutate(mutate)
