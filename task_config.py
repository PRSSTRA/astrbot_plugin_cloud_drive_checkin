from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as datetime_timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import (
    CroniterBadCronError,
    CroniterBadDateError,
    CroniterNotAlphaError,
    croniter,
)

from .config import PluginConfigError


@dataclass(frozen=True)
class UserField:
    field_id: str
    label: str
    env_name: str
    value_type: str
    required: bool
    secret: bool
    hint: str


@dataclass(frozen=True)
class DependencyBundle:
    url: str
    sha256: str


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    name: str
    enabled: bool
    runtime: str
    script_url: str
    source_commit: str
    sha256: str
    cron: str
    user_fields: tuple[UserField, ...]
    fixed_env: tuple[tuple[str, str], ...]
    input_adapter: str
    result_adapter: str
    dependency_bundle: str
    timeout_seconds: int
    experimental: bool
    version: str


@dataclass(frozen=True)
class TaskCatalog:
    timezone: str
    tasks: tuple[TaskConfig, ...]


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_HEX_40 = re.compile(r"[0-9a-fA-F]{40}\Z", re.ASCII)
_HEX_64 = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_CRON_PART = re.compile(
    r"(?P<base>\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)"
    r"(?:/(?P<step>[0-9]+))?\Z",
    re.ASCII,
)
_MONTH_NAMES = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_WEEKDAY_NAMES = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}
_CRON_BOUNDS = (
    (0, 59, None),
    (0, 23, None),
    (1, 31, None),
    (1, 12, _MONTH_NAMES),
    (0, 7, _WEEKDAY_NAMES),
)
_RESERVED_ENV_NAMES = frozenset({"PATH", "HOME", "SHELL", "ENV", "BASH_ENV", "IFS"})
_RESERVED_ENV_PREFIXES = ("PYTHON", "NODE_", "LD_", "DYLD_")
_ALLOWED_RUNTIMES = frozenset({"python", "node"})
_ALLOWED_INPUT_ADAPTERS = frozenset(
    {"direct_env", "tianyi_account", "mobile_single_account"}
)
_ALLOWED_RESULT_ADAPTERS = frozenset(
    {"json_protocol", "process_only", "tianyi", "mobile_unverified"}
)
_MOBILE_TASK_ID = "mobile_cloud_experimental"
_MISSING = object()
_SCHEMA_PATH = Path(__file__).with_name("_conf_schema.json")
_USER_FIELDS_JSON_MAX_BYTES = 64 * 1024
_DEPENDENCY_BUNDLE_JSON_MAX_BYTES = 4096
_MAX_ENV_FIELDS = 64
_MAX_ENV_FIELD_BYTES = 16 * 1024
_MAX_ENV_TOTAL_BYTES = 64 * 1024

_USER_FIELDS_JSON_MAX_DEPTH = 32
_CRON_ERRORS = (
    CroniterBadCronError,
    CroniterBadDateError,
    CroniterNotAlphaError,
    ValueError,
    OverflowError,
)


def _required(mapping: Mapping, key: str) -> object:
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        raise PluginConfigError("task configuration is missing a required field")
    return value


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise PluginConfigError("task configuration strings are invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise PluginConfigError("task configuration booleans are invalid")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PluginConfigError("task or field identifier is invalid")
    return value


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise PluginConfigError("task timeout is invalid")
    return value


def _normalize_timezone(value: object) -> tuple[str, ZoneInfo]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PluginConfigError("task timezone is invalid")
    try:
        timezone = ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise PluginConfigError("task timezone is invalid") from None
    return value, timezone


def _normalize_env_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or _ENV_NAME.fullmatch(value) is None
        or len(value.encode("ascii")) > _MAX_ENV_FIELD_BYTES
    ):
        raise PluginConfigError("task environment name is invalid")
    normalized = value.upper()
    if normalized in _RESERVED_ENV_NAMES or normalized.startswith(
        _RESERVED_ENV_PREFIXES
    ):
        raise PluginConfigError("task environment name is reserved")
    return value


def _decode_url_segment(segment: str) -> str:
    if not segment or "\\" in segment:
        raise PluginConfigError("task script source is invalid")
    if "%" in segment:
        escapes = _PERCENT_ESCAPE.findall(segment)
        if len(escapes) != segment.count("%"):
            raise PluginConfigError("task script source is invalid")
    try:
        decoded = unquote(segment, errors="strict")
    except UnicodeDecodeError:
        raise PluginConfigError("task script source is invalid") from None
    if _PERCENT_ESCAPE.search(decoded) is not None:
        raise PluginConfigError("task script source is invalid")
    if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
        raise PluginConfigError("task script source is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise PluginConfigError("task script source is invalid")
    return decoded


def _normalize_source(script_url: object, source_commit: object) -> tuple[str, str]:
    if not isinstance(script_url, str) or not isinstance(source_commit, str):
        raise PluginConfigError("task script source is invalid")
    if (
        not script_url
        or "?" in script_url
        or "#" in script_url
        or any(character.isspace() for character in script_url)
        or any(ord(character) < 32 or ord(character) == 127 for character in script_url)
    ):
        raise PluginConfigError("task script source is invalid")
    if _HEX_40.fullmatch(source_commit) is None:
        raise PluginConfigError("task source commit is invalid")
    normalized_commit = source_commit.lower()

    try:
        parsed = urlsplit(script_url)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        raise PluginConfigError("task script source is invalid") from None

    if (
        parsed.scheme.lower() != "https"
        or username is not None
        or password is not None
        or parsed.query
        or parsed.fragment
        or hostname is None
        or port not in (None, 443)
        or parsed.netloc.endswith(":")
    ):
        raise PluginConfigError("task script source is invalid")

    hostname = hostname.lower()
    raw_segments = parsed.path.split("/")
    if (
        not raw_segments
        or raw_segments[0]
        or any(not item for item in raw_segments[1:])
    ):
        raise PluginConfigError("task script source is invalid")
    segments = tuple(_decode_url_segment(item) for item in raw_segments[1:])

    if hostname == "github.com":
        if len(segments) < 5 or segments[2] != "blob":
            raise PluginConfigError("task script source is invalid")
        owner, repository, _, url_commit, *script_path = segments
    elif hostname == "raw.githubusercontent.com":
        if len(segments) < 4:
            raise PluginConfigError("task script source is invalid")
        owner, repository, url_commit, *script_path = segments
    else:
        raise PluginConfigError("task script source is invalid")

    if (
        not owner
        or not repository
        or not script_path
        or _HEX_40.fullmatch(url_commit) is None
        or url_commit.lower() != normalized_commit
    ):
        raise PluginConfigError("task script source is invalid")

    canonical_segments = (owner, repository, normalized_commit, *script_path)
    canonical_path = "/".join(
        quote(segment, safe="-._~!$&'()*+,;=:@") for segment in canonical_segments
    )
    return f"https://raw.githubusercontent.com/{canonical_path}", normalized_commit


def _normalize_sha256(value: object) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise PluginConfigError("task script digest is invalid")
    return value.lower()


def _dependency_url_commit(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise PluginConfigError("task dependency bundle is invalid") from None
    segments = parsed.path.split("/")
    hostname = parsed.hostname.lower() if parsed.hostname is not None else ""
    if hostname == "github.com" and len(segments) >= 5:
        candidate = segments[4]
    elif hostname == "raw.githubusercontent.com" and len(segments) >= 4:
        candidate = segments[3]
    else:
        raise PluginConfigError("task dependency bundle is invalid")
    if _HEX_40.fullmatch(candidate) is None:
        raise PluginConfigError("task dependency bundle is invalid")
    return candidate


def _reject_dependency_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError
        result[key] = item
    return result


def dependency_bundle_reference(value: str) -> DependencyBundle | None:
    """Parse one canonical dependency declaration; ``none`` is dependency-free."""

    if value == "none":
        return None
    if not isinstance(value, str) or not value:
        raise PluginConfigError("task dependency bundle is invalid")
    try:
        encoded = value.encode("utf-8")
        if len(encoded) > _DEPENDENCY_BUNDLE_JSON_MAX_BYTES:
            raise ValueError
        declaration = json.loads(
            value,
            object_pairs_hook=_reject_dependency_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        raise PluginConfigError("task dependency bundle is invalid") from None
    if not isinstance(declaration, dict) or set(declaration) != {
        "format",
        "url",
        "sha256",
    }:
        raise PluginConfigError("task dependency bundle is invalid")
    if declaration.get("format") != "zip-v1":
        raise PluginConfigError("task dependency bundle is invalid")
    raw_url = declaration.get("url")
    raw_sha256 = declaration.get("sha256")
    if not isinstance(raw_url, str):
        raise PluginConfigError("task dependency bundle is invalid")
    try:
        normalized_url, _commit = _normalize_source(
            raw_url, _dependency_url_commit(raw_url)
        )
        normalized_sha256 = _normalize_sha256(raw_sha256)
    except PluginConfigError:
        raise PluginConfigError("task dependency bundle is invalid") from None
    return DependencyBundle(url=normalized_url, sha256=normalized_sha256)


def _normalize_dependency_bundle(value: object) -> str:
    if not isinstance(value, str):
        raise PluginConfigError("task dependency bundle is invalid")
    if value == "":
        return value
    reference = dependency_bundle_reference(value)
    if reference is None:
        return "none"
    return json.dumps(
        {"format": "zip-v1", "url": reference.url, "sha256": reference.sha256},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _cron_integer(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise PluginConfigError("task cron expression is invalid") from None


def _normalize_cron_value(
    value: str,
    minimum: int,
    maximum: int,
    names: Mapping[str, int] | None,
) -> tuple[str, int]:
    upper_value = value.upper()
    if names is not None and upper_value in names:
        return upper_value, names[upper_value]
    if re.fullmatch(r"[0-9]+", value, re.ASCII) is None:
        raise PluginConfigError("task cron expression is invalid")
    numeric_value = _cron_integer(value)
    if not minimum <= numeric_value <= maximum:
        raise PluginConfigError("task cron expression is invalid")
    return str(numeric_value), numeric_value


def _normalize_cron_field(
    field: str,
    minimum: int,
    maximum: int,
    names: Mapping[str, int] | None,
) -> str:
    normalized_parts: list[str] = []
    for part in field.split(","):
        match = _CRON_PART.fullmatch(part)
        if match is None:
            raise PluginConfigError("task cron expression is invalid")
        base = match.group("base")
        step = match.group("step")
        normalized_step = _cron_integer(step) if step is not None else None
        if normalized_step == 0:
            raise PluginConfigError("task cron expression is invalid")

        if base == "*":
            normalized_base = base
        elif "-" in base:
            start, end = base.split("-", 1)
            normalized_start, start_number = _normalize_cron_value(
                start, minimum, maximum, names
            )
            normalized_end, end_number = _normalize_cron_value(
                end, minimum, maximum, names
            )
            if start_number > end_number:
                raise PluginConfigError("task cron expression is invalid")
            normalized_base = f"{normalized_start}-{normalized_end}"
        else:
            normalized_base, _ = _normalize_cron_value(base, minimum, maximum, names)

        if normalized_step is not None:
            normalized_base = f"{normalized_base}/{normalized_step}"
        normalized_parts.append(normalized_base)
    return ",".join(normalized_parts)


def _normalize_cron(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PluginConfigError("task cron expression is invalid")
    if any(ord(character) < 32 and character not in {" ", "\t"} for character in value):
        raise PluginConfigError("task cron expression is invalid")
    fields = value.split()
    if len(fields) != 5:
        raise PluginConfigError("task cron expression must have five fields")
    return " ".join(
        _normalize_cron_field(field, minimum, maximum, names)
        for field, (minimum, maximum, names) in zip(fields, _CRON_BOUNDS)
    )


def _cron_branch_runs(cron_expression: str, now: datetime) -> tuple[datetime, ...]:
    try:
        iterator = croniter(
            cron_expression,
            now,
            day_or=True,
            max_years_between_matches=50,
        )
    except CroniterBadDateError:
        return ()
    except (CroniterBadCronError, CroniterNotAlphaError, ValueError, OverflowError):
        raise PluginConfigError("task cron expression is invalid") from None

    results: list[datetime] = []
    while len(results) < 3:
        try:
            results.append(iterator.get_next(datetime))
        except CroniterBadDateError:
            break
        except (CroniterBadCronError, CroniterNotAlphaError, ValueError, OverflowError):
            raise PluginConfigError("task cron expression is invalid") from None
    return tuple(results)


def _expanded_field_is_restricted(field: Sequence[object]) -> bool:
    return len(field) != 1 or field[0] != "*"


def _next_run_times_for_cron(
    cron_expression: str, timezone: ZoneInfo, now: datetime
) -> tuple[datetime, datetime, datetime]:
    try:
        localized_now = now.astimezone(timezone)
        expanded, _ = croniter.expand(cron_expression)
    except _CRON_ERRORS:
        raise PluginConfigError("task cron expression is invalid") from None

    fields = cron_expression.split()
    if (
        len(expanded) != 5
        or len(fields) != 5
        or not all(isinstance(field, Sequence) for field in expanded)
    ):
        raise PluginConfigError("task cron expression is invalid")

    if _expanded_field_is_restricted(expanded[2]) and _expanded_field_is_restricted(
        expanded[4]
    ):
        # croniter 6.2.4 aborts OR when the day branch has no date (e.g. Feb 31).
        # Merge both branches so an impossible day does not suppress valid weekdays.
        day_fields = fields.copy()
        day_fields[4] = "*"
        weekday_fields = fields.copy()
        weekday_fields[2] = "*"
        candidates = (
            *_cron_branch_runs(" ".join(day_fields), localized_now),
            *_cron_branch_runs(" ".join(weekday_fields), localized_now),
        )
    else:
        candidates = _cron_branch_runs(cron_expression, localized_now)

    localized_now_utc = localized_now.astimezone(datetime_timezone.utc)
    by_utc: dict[datetime, datetime] = {}
    for result in candidates:
        if (
            not isinstance(result, datetime)
            or result.tzinfo is None
            or result.utcoffset() is None
        ):
            raise PluginConfigError("task cron expression has no future run")
        result_utc = result.astimezone(datetime_timezone.utc)
        if result_utc > localized_now_utc:
            by_utc.setdefault(result_utc, result)

    ordered = tuple(by_utc[key] for key in sorted(by_utc))
    if len(ordered) < 3:
        raise PluginConfigError("task cron expression has no future run")
    return ordered[0], ordered[1], ordered[2]


def next_run_times(
    task: TaskConfig, timezone: str, *, now: datetime | None = None
) -> tuple[datetime, datetime, datetime]:
    _, timezone_info = _normalize_timezone(timezone)
    cron_expression = _normalize_cron(task.cron)
    if now is None:
        now = datetime.now(timezone_info)
    elif not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PluginConfigError("task schedule reference time must be timezone-aware")
    return _next_run_times_for_cron(cron_expression, timezone_info, now)


def _reject_json_constant(_: str) -> None:
    raise ValueError


def _json_depth_is_allowed(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, list):
            if depth > _USER_FIELDS_JSON_MAX_DEPTH:
                return False
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, Mapping):
            if depth > _USER_FIELDS_JSON_MAX_DEPTH:
                return False
            pending.extend((item, depth + 1) for item in current.values())
    return True


def _decode_user_fields_json(value: str) -> object:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise PluginConfigError("task user fields JSON is invalid") from None
    if len(encoded) > _USER_FIELDS_JSON_MAX_BYTES:
        raise PluginConfigError("task user fields JSON is invalid") from None
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise PluginConfigError("task user fields JSON is invalid") from None
    if not isinstance(decoded, list) or not _json_depth_is_allowed(decoded):
        raise PluginConfigError("task user fields JSON is invalid") from None
    return decoded


def _parse_user_fields(value: object) -> tuple[UserField, ...]:
    if isinstance(value, str):
        value = _decode_user_fields_json(value)
    if not isinstance(value, list):
        raise PluginConfigError("task user fields must be a list")
    if len(value) > 32:
        raise PluginConfigError("task has too many user fields")

    fields: list[UserField] = []
    field_ids: set[str] = set()
    env_names: set[str] = set()
    for raw_field in value:
        if not isinstance(raw_field, Mapping):
            raise PluginConfigError("task user field is invalid")
        field_id = _identifier(_required(raw_field, "field_id"))
        env_name = _normalize_env_name(_required(raw_field, "env_name"))
        folded_env_name = env_name.upper()
        if field_id in field_ids or folded_env_name in env_names:
            raise PluginConfigError("task user field or environment name is duplicated")
        field_ids.add(field_id)
        env_names.add(folded_env_name)

        value_type = _string(_required(raw_field, "value_type"))
        if value_type not in {"string", "int", "bool"}:
            raise PluginConfigError("task user field value type is invalid")
        fields.append(
            UserField(
                field_id=field_id,
                label=_string(_required(raw_field, "label")),
                env_name=env_name,
                value_type=value_type,
                required=_boolean(_required(raw_field, "required")),
                secret=_boolean(_required(raw_field, "secret")),
                hint=_string(raw_field.get("hint", ""), allow_empty=True),
            )
        )
    return tuple(fields)


def _parse_fixed_env(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise PluginConfigError("task fixed environment must be a mapping")
    if len(value) > _MAX_ENV_FIELDS:
        raise PluginConfigError("task has too many environment fields")
    normalized: dict[str, str] = {}
    seen_names: set[str] = set()
    total_bytes = 0
    for raw_name, raw_value in value.items():
        name = _normalize_env_name(raw_name)
        if name.upper() in seen_names:
            raise PluginConfigError("task fixed environment name is duplicated")
        seen_names.add(name.upper())
        if not isinstance(raw_value, str) or "\0" in raw_value:
            raise PluginConfigError("task fixed environment values must be strings")
        try:
            encoded_value = raw_value.encode("utf-8")
        except UnicodeEncodeError:
            raise PluginConfigError(
                "task fixed environment values are invalid"
            ) from None
        if len(encoded_value) > _MAX_ENV_FIELD_BYTES:
            raise PluginConfigError("task fixed environment value is too large")
        total_bytes += len(encoded_value)
        if total_bytes > _MAX_ENV_TOTAL_BYTES:
            raise PluginConfigError("task fixed environment is too large")
        normalized[name] = raw_value
    return tuple(sorted(normalized.items()))


def _validate_environment_declaration(
    user_fields: Sequence[UserField], fixed_env: Sequence[tuple[str, str]]
) -> None:
    if len(user_fields) + len(fixed_env) > _MAX_ENV_FIELDS:
        raise PluginConfigError("task has too many environment fields")
    fixed_bytes = sum(len(value.encode("utf-8")) for _, value in fixed_env)
    minimum_required_bytes = sum(
        {"string": 1, "int": 1, "bool": 4}[field.value_type]
        for field in user_fields
        if field.required
    )
    if fixed_bytes + minimum_required_bytes > _MAX_ENV_TOTAL_BYTES:
        raise PluginConfigError("task environment values exceed the size limit")


def _validate_adapter_contract(
    *,
    task_id: str,
    enabled: bool,
    runtime: str,
    input_adapter: str,
    result_adapter: str,
    dependency_bundle: str,
    user_fields: Sequence[UserField],
    experimental: bool,
) -> None:
    if input_adapter == "direct_env":
        if (
            result_adapter not in {"json_protocol", "process_only"}
            or not dependency_bundle
        ):
            raise PluginConfigError("task adapter combination is invalid")
    elif input_adapter == "tianyi_account":
        if runtime != "python" or result_adapter != "tianyi" or dependency_bundle != "":
            raise PluginConfigError("task adapter combination is invalid")
        _require_credential_fields(user_fields, ("username", "password"))
    elif input_adapter == "mobile_single_account":
        if (
            runtime != "node"
            or result_adapter != "mobile_unverified"
            or dependency_bundle != ""
        ):
            raise PluginConfigError("task adapter combination is invalid")
        _require_credential_fields(
            user_fields, ("authorization", "phone", "auth_token")
        )

    if result_adapter == "tianyi" and input_adapter != "tianyi_account":
        raise PluginConfigError("task adapter combination is invalid")
    if (
        result_adapter == "mobile_unverified"
        and input_adapter != "mobile_single_account"
    ):
        raise PluginConfigError("task adapter combination is invalid")

    is_mobile = (
        task_id == _MOBILE_TASK_ID
        or input_adapter == "mobile_single_account"
        or result_adapter == "mobile_unverified"
    )
    if is_mobile and (enabled or not experimental):
        raise PluginConfigError("mobile task configuration must remain disabled")


def _require_credential_fields(
    fields: Sequence[UserField], required_field_ids: Sequence[str]
) -> None:
    by_id = {field.field_id: field for field in fields}
    for field_id in required_field_ids:
        field = by_id.get(field_id)
        if (
            field is None
            or field.value_type != "string"
            or not field.required
            or not field.secret
        ):
            raise PluginConfigError("task credential field declaration is invalid")


def _task_version_payload(
    *,
    timezone: str,
    task_id: str,
    name: str,
    enabled: bool,
    runtime: str,
    script_url: str,
    source_commit: str,
    sha256: str,
    cron: str,
    user_fields: Sequence[UserField],
    fixed_env: Sequence[tuple[str, str]],
    input_adapter: str,
    result_adapter: str,
    dependency_bundle: str,
    timeout_seconds: int,
    experimental: bool,
) -> dict[str, object]:
    return {
        "timezone": timezone,
        "task": {
            "task_id": task_id,
            "name": name,
            "enabled": enabled,
            "runtime": runtime,
            "script_url": script_url,
            "source_commit": source_commit,
            "sha256": sha256,
            "cron": cron,
            "user_fields": [
                {
                    "field_id": field.field_id,
                    "label": field.label,
                    "env_name": field.env_name,
                    "value_type": field.value_type,
                    "required": field.required,
                    "secret": field.secret,
                    "hint": field.hint,
                }
                for field in user_fields
            ],
            "fixed_env": list(fixed_env),
            "input_adapter": input_adapter,
            "result_adapter": result_adapter,
            "dependency_bundle": dependency_bundle,
            "timeout_seconds": timeout_seconds,
            "experimental": experimental,
        },
    }


def _content_version(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_task(
    raw_task: object, *, timezone: str, timezone_info: ZoneInfo, global_timeout: int
) -> TaskConfig:
    if not isinstance(raw_task, Mapping):
        raise PluginConfigError("task entry must be a mapping")

    task_id = _identifier(_required(raw_task, "task_id"))
    name = _string(_required(raw_task, "name"))
    enabled = _boolean(raw_task.get("enabled", False))
    runtime = _string(_required(raw_task, "runtime"))
    if runtime not in _ALLOWED_RUNTIMES:
        raise PluginConfigError("task runtime is invalid")

    script_url, source_commit = _normalize_source(
        _required(raw_task, "script_url"), _required(raw_task, "source_commit")
    )
    sha256 = _normalize_sha256(_required(raw_task, "sha256"))
    cron_expression = _normalize_cron(_required(raw_task, "cron"))
    user_fields = _parse_user_fields(raw_task.get("user_fields", []))
    fixed_env = _parse_fixed_env(raw_task.get("fixed_env", {}))
    user_env_names = {field.env_name.upper() for field in user_fields}
    if user_env_names.intersection(name.upper() for name, _ in fixed_env):
        raise PluginConfigError("task user and fixed environment names overlap")
    _validate_environment_declaration(user_fields, fixed_env)

    input_adapter = _string(_required(raw_task, "input_adapter"))
    if input_adapter not in _ALLOWED_INPUT_ADAPTERS:
        raise PluginConfigError("task input adapter is invalid")
    result_adapter = _string(_required(raw_task, "result_adapter"))
    if result_adapter not in _ALLOWED_RESULT_ADAPTERS:
        raise PluginConfigError("task result adapter is invalid")
    dependency_bundle = _normalize_dependency_bundle(
        raw_task.get("dependency_bundle", "")
    )
    raw_timeout = raw_task.get("timeout_seconds", 0)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
        raise PluginConfigError("task timeout is invalid")
    timeout_seconds = (
        global_timeout if raw_timeout == 0 else _bounded_integer(raw_timeout, 1, 240)
    )
    experimental = _boolean(raw_task.get("experimental", False))

    _validate_adapter_contract(
        task_id=task_id,
        enabled=enabled,
        runtime=runtime,
        input_adapter=input_adapter,
        result_adapter=result_adapter,
        dependency_bundle=dependency_bundle,
        user_fields=user_fields,
        experimental=experimental,
    )
    _next_run_times_for_cron(
        cron_expression, timezone_info, datetime.now(timezone_info)
    )

    payload = _task_version_payload(
        timezone=timezone,
        task_id=task_id,
        name=name,
        enabled=enabled,
        runtime=runtime,
        script_url=script_url,
        source_commit=source_commit,
        sha256=sha256,
        cron=cron_expression,
        user_fields=user_fields,
        fixed_env=fixed_env,
        input_adapter=input_adapter,
        result_adapter=result_adapter,
        dependency_bundle=dependency_bundle,
        timeout_seconds=timeout_seconds,
        experimental=experimental,
    )
    return TaskConfig(
        task_id=task_id,
        name=name,
        enabled=enabled,
        runtime=runtime,
        script_url=script_url,
        source_commit=source_commit,
        sha256=sha256,
        cron=cron_expression,
        user_fields=user_fields,
        fixed_env=fixed_env,
        input_adapter=input_adapter,
        result_adapter=result_adapter,
        dependency_bundle=dependency_bundle,
        timeout_seconds=timeout_seconds,
        experimental=experimental,
        version=_content_version(payload),
    )


@lru_cache(maxsize=1)
def _schema_default_tasks() -> tuple[object, ...]:
    try:
        with _SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        tasks_schema = schema["tasks"]
        default_tasks = tasks_schema["default"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise PluginConfigError("task schema defaults are unavailable") from None
    if not isinstance(default_tasks, list):
        raise PluginConfigError("task schema defaults are invalid")
    return tuple(default_tasks)


def parse_task_catalog(raw: Mapping) -> TaskCatalog:
    if not isinstance(raw, Mapping):
        raise PluginConfigError("plugin task configuration must be a mapping")

    timezone, timezone_info = _normalize_timezone(raw.get("timezone", "Asia/Shanghai"))
    global_timeout = _bounded_integer(raw.get("timeout_seconds", 180), 1, 240)

    if "tasks" in raw:
        raw_tasks = raw["tasks"]
        if not isinstance(raw_tasks, list):
            raise PluginConfigError("tasks must be a list")
    else:
        raw_tasks = _schema_default_tasks()
    if len(raw_tasks) > 32:
        raise PluginConfigError("task catalog has too many tasks")

    tasks: list[TaskConfig] = []
    task_ids: set[str] = set()
    for raw_task in raw_tasks:
        try:
            task = _parse_task(
                raw_task,
                timezone=timezone,
                timezone_info=timezone_info,
                global_timeout=global_timeout,
            )
        except UnicodeError:
            raise PluginConfigError(
                "task configuration contains invalid Unicode"
            ) from None
        if task.task_id in task_ids:
            raise PluginConfigError("task identifier is duplicated")
        task_ids.add(task.task_id)
        tasks.append(task)
    return TaskCatalog(timezone=timezone, tasks=tuple(tasks))
