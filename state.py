import copy
import json
import math

STATE_KEY = "checkin_state"
SCHEMA_VERSION = 3
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
    "prepared_artifacts",
)
_SNAPSHOT_FIELDS = frozenset(("schema_version", "revision", *_COLLECTION_FIELDS))
_V1_SNAPSHOT_FIELDS = _SNAPSHOT_FIELDS - {"prepared_artifacts"}


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


def upgrade_snapshot(value, *, max_bytes=MAX_SNAPSHOT_BYTES) -> dict:
    """Migrate confirmed manual-only snapshots before the startup save barrier."""
    if type(value) is not dict or type(value.get("schema_version")) is not int:
        validate_snapshot(value, max_bytes=max_bytes)
    version = value["schema_version"]
    if version == 1:
        if value.keys() != _V1_SNAPSHOT_FIELDS:
            raise InvalidStateError("snapshot fields are invalid")
        value = {**value, "schema_version": 2, "prepared_artifacts": {}}
        version = 2
    if version == 2:
        # Validate JSON and collection shapes before traversing business records.
        candidate = {**value, "schema_version": SCHEMA_VERSION}
        validate_snapshot(candidate, max_bytes=max_bytes)
        value = copy.deepcopy(candidate)
        for item in value["batches"].values():
            if type(item) is dict and item.get("kind") == "manual_checkin":
                item.update(
                    kind="checkin",
                    source="manual",
                    manual_requested=True,
                    scheduled_at=None,
                    batch_id=None,
                )
        for record in value["outbox"].values():
            if (
                type(record) is dict
                and record.get("kind") == "manual_terminal_notification"
            ):
                record.update(
                    kind="terminal_notification", source="manual", manual_requested=True
                )
        cooldowns = value["cooldowns"]
        for key, record in tuple(cooldowns.items()):
            if type(record) is dict and record.get("kind") == "manual_queue_cooldown":
                record["kind"] = "checkin_cooldown"
                if key == "manual:global_spacing":
                    _move_record(cooldowns, key, "checkin:global_spacing")
        indexes = value["dedupe"]
        for key, record in tuple(indexes.items()):
            if type(record) is dict and record.get("kind") == "manual_checkin":
                record["kind"] = "checkin"
                if key.startswith("manual:"):
                    _move_record(indexes, key, "binding:" + key[len("manual:") :])
    validate_snapshot(value, max_bytes=max_bytes)
    return value


def _move_record(records: dict, old: str, new: str) -> None:
    if new in records:
        raise InvalidStateError("snapshot migration index conflict")
    records[new] = records.pop(old)
