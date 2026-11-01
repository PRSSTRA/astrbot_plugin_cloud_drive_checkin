import json
import math


STATE_KEY = "checkin_state"
SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024

_COLLECTION_FIELDS = (
    "users",
    "bindings",
    "credentials",
    "batches",
    "runs",
    "outbox",
    "cleanup_jobs",
    "cooldowns",
    "dedupe",
)
_SNAPSHOT_FIELDS = frozenset(("schema_version", "revision", *_COLLECTION_FIELDS))


class InvalidStateError(ValueError):
    """The persisted state is not a valid snapshot."""


class UnsupportedSchemaError(InvalidStateError):
    """The persisted state uses an unsupported schema version."""


class StateTooLargeError(InvalidStateError):
    """The serialized snapshot exceeds its configured size limit."""


def new_snapshot() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        **{field: {} for field in _COLLECTION_FIELDS},
    }


def _validate_json_value(value, active_containers) -> None:
    value_type = type(value)
    if value is None or value_type in (str, int, bool):
        return
    if value_type is float:
        if not math.isfinite(value):
            raise InvalidStateError("snapshot contains invalid JSON data")
        return
    if value_type not in (dict, list):
        raise InvalidStateError("snapshot contains invalid JSON data")

    identity = id(value)
    if identity in active_containers:
        raise InvalidStateError("snapshot contains invalid JSON data")

    active_containers.add(identity)
    try:
        if value_type is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise InvalidStateError("snapshot contains invalid JSON data")
                _validate_json_value(child, active_containers)
        else:
            for child in value:
                _validate_json_value(child, active_containers)
    finally:
        active_containers.remove(identity)


def validate_snapshot(value, *, max_bytes=MAX_SNAPSHOT_BYTES) -> None:
    if type(value) is not dict:
        raise InvalidStateError("snapshot must be a dictionary")
    if value.keys() != _SNAPSHOT_FIELDS:
        raise InvalidStateError("snapshot fields are invalid")

    schema_version = value["schema_version"]
    if type(schema_version) is not int:
        raise InvalidStateError("schema version is invalid")
    if schema_version != SCHEMA_VERSION:
        raise UnsupportedSchemaError("unsupported schema version")

    revision = value["revision"]
    if type(revision) is not int or revision < 0:
        raise InvalidStateError("revision is invalid")

    for field in _COLLECTION_FIELDS:
        if type(value[field]) is not dict:
            raise InvalidStateError("snapshot collection is invalid")

    try:
        _validate_json_value(value, set())
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except InvalidStateError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise InvalidStateError("snapshot contains invalid JSON data") from None

    if len(encoded) > max_bytes:
        raise StateTooLargeError("snapshot exceeds the size limit")
