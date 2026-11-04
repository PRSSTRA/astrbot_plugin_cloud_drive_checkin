"""解析原生配置并校验输入边界。

权限、环境变量和 Cron 校验选择性复用本项目 GPL-3.0 开发版
dev@a662b7662f301ee8dfbeb5cf9a059fcf9af113c2。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import UTC
import hashlib
import ipaddress
import json
import re
from urllib.parse import unquote
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from croniter import croniter
from croniter import CroniterBadCronError
from croniter import CroniterBadDateError
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

MAX_CREDENTIAL_BYTES = 512 * 1024
MAX_ENV_FIELDS = 64
MAX_FIELD_BYTES = 16 * 1024
MAX_TOTAL_VALUE_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z", re.ASCII)
_RESERVED_NAMES = frozenset({"PATH", "HOME", "SHELL", "ENV", "BASH_ENV", "IFS"})
_RESERVED_PREFIXES = ("PYTHON", "NODE_", "LD_", "DYLD_")
_CRON_FIELD = re.compile(
    r"(?:\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/[0-9]+)?"
    r"(?:,(?:\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/[0-9]+)?)*\Z", re.ASCII)
_NODE_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z",
                        re.ASCII)
_NODE_VERSION = re.compile(r"[A-Za-z0-9._+*~^<>=| -]+\Z", re.ASCII)


class PluginConfigError(ValueError):
    """配置无效；错误消息不包含原始值。"""


def json_bytes(value: object) -> bytes:
    """生成稳定的 UTF-8 JSON，供指纹和认证上下文使用。"""
    return json.dumps(value,
                      ensure_ascii=False,
                      allow_nan=False,
                      separators=(",", ":"),
                      sort_keys=True).encode("utf-8")


@dataclass(frozen=True)
class Script:
    """不可变脚本声明；字段顺序参与凭据认证。"""

    id: str
    name: str
    enabled: bool
    url: str
    runtime: str
    env_names: tuple[str, ...]
    dependencies: tuple[str, ...]

    @property
    def fields_hash(self) -> str:
        """有序字段名的摘要；变更后旧凭据不可复用。"""
        return hashlib.sha256(json_bytes(self.env_names)).hexdigest()

    @property
    def fingerprint(self) -> str:
        """完整声明的摘要，用于执行前复核配置。"""
        return hashlib.sha256(json_bytes(asdict(self))).hexdigest()


@dataclass(frozen=True)
class PluginConfig:
    """经过校验的插件配置快照。"""

    enabled: bool
    whitelist_qqs: frozenset[str]
    timezone: str
    cron: str
    timeout_seconds: int
    install_timeout_seconds: int
    scripts: tuple[Script, ...]


def _normalize_qq(value: object) -> str:
    if type(value) is int and value > 0:
        return str(value)
    if type(value) is str and re.fullmatch(r"[1-9][0-9]*", value, re.ASCII):
        return value
    raise PluginConfigError(
        "QQ identifiers must be positive ASCII decimal numbers")


def validate_env_name(value: object) -> str:
    """校验 ASCII 环境变量名，拒绝影响运行时的保留名称。"""
    if type(value) is not str or _ENV_NAME.fullmatch(value) is None:
        raise PluginConfigError(
            "environment name must be an ASCII identifier of at most 128 bytes")
    upper = value.upper()
    if upper in _RESERVED_NAMES or upper.startswith(_RESERVED_PREFIXES):
        raise PluginConfigError("environment name is reserved")
    return value


def validate_environment(env: dict) -> dict[str, str]:
    """校验凭据字段及 UTF-8 字节上限，返回独立副本。

    Raises:
        PluginConfigError: 名称、值、重复字段或大小不符合约束。
    """
    if type(env) is not dict or len(env) > MAX_ENV_FIELDS:
        raise PluginConfigError("environment must contain at most 64 fields")
    seen = set()
    total = 0
    for name, value in env.items():
        validate_env_name(name)
        if name.upper() in seen:
            raise PluginConfigError("environment names are duplicated")
        seen.add(name.upper())
        if type(value) is not str or not value or "\0" in value:
            raise PluginConfigError(
                "all credential fields require nonempty text without NUL")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise PluginConfigError(
                "credential text is not valid UTF-8") from None
        total += size
        if size > MAX_FIELD_BYTES or total > MAX_TOTAL_VALUE_BYTES:
            raise PluginConfigError(
                "credentials exceed the 16 KiB field or 64 KiB total limit")
    try:
        if len(json_bytes({"env": env})) > MAX_CREDENTIAL_BYTES:
            raise PluginConfigError("credential JSON exceeds 512 KiB")
    except UnicodeError:
        raise PluginConfigError("credential text is not valid UTF-8") from None
    return dict(env)


def validate_script_url(url: object) -> str:
    """校验公开 HTTPS 单文件地址；连接时仍需检查 DNS 解析结果。"""
    error = "script URL must be a public HTTPS single-file URL on port 443"
    if type(url) is not str or not url or "#" in url or "\\" in url:
        raise PluginConfigError(error)
    try:
        url.encode("utf-8")
        decoded = unquote(url, errors="strict")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or any(
                ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise ValueError
        if re.search(r"%(?![0-9A-Fa-f]{2})", url):
            raise ValueError
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if (parsed.scheme != "https" or not hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.port not in (None, 443) or parsed.netloc.endswith(":") or
                not parsed.path or parsed.path.endswith("/")):
            raise ValueError
        if "%" in hostname:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            ascii_host = hostname.encode("idna").decode("ascii")
            if len(ascii_host) > 253 or any(
                    not re.fullmatch(
                        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in ascii_host.rstrip(".").split(".")):
                raise ValueError
        else:
            if (not address.is_global or address.is_multicast or
                    address.is_unspecified):
                raise ValueError
    except (ValueError, UnicodeError):
        raise PluginConfigError(error) from None
    return url


def _timezone(name: object) -> ZoneInfo:
    if type(name) is not str or not name or name != name.strip():
        raise PluginConfigError("timezone is invalid")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise PluginConfigError("timezone is invalid") from None


def _cron(expression: object) -> str:
    """校验五段常规 Cron 语法，拒绝扩展字段和无效控制字符。"""
    if type(expression) is not str or any(
            ord(c) < 32 and c != "\t" for c in expression):
        raise PluginConfigError("cron expression is invalid")
    fields = expression.upper().split()
    if len(fields) != 5 or any(
            _CRON_FIELD.fullmatch(field) is None for field in fields):
        raise PluginConfigError("cron must have five list/range/step fields")
    result = " ".join(fields)
    try:
        expanded, _ = croniter.expand(result)
        if len(expanded) != 5:
            raise ValueError
    except (CroniterBadCronError, ValueError, OverflowError):
        raise PluginConfigError("cron expression is invalid") from None
    return result


def _next_fire(expression: str, zone: ZoneInfo, after: datetime) -> datetime:
    """保留日期与星期的 OR 语义，按 UTC 比较下一次有效触发。"""
    fields = expression.split()
    local_after = after.astimezone(zone).replace(tzinfo=None)
    expanded, _ = croniter.expand(expression)
    branches = [expression]
    if expanded[2] != ["*"] and expanded[4] != ["*"]:
        # 拆开日期与星期分支，避免 croniter 因 2 月 31 日等无解日期中止 OR。
        branches = [
            " ".join([*fields[:4], "*"]),
            " ".join([*fields[:2], "*", *fields[3:]])
        ]
    candidates = []
    for branch in branches:
        try:
            iterator = croniter(branch,
                                local_after,
                                day_or=True,
                                max_years_between_matches=50)
            while True:
                local_result = iterator.get_next(datetime)
                result = local_result.replace(tzinfo=zone)
                if result.astimezone(UTC).astimezone(zone).replace(
                        tzinfo=None) != local_result:
                    continue
                if result.astimezone(UTC) > after.astimezone(UTC):
                    candidates.append(result)
                    break
        except CroniterBadDateError:
            continue
        except (CroniterBadCronError, ValueError, OverflowError):
            raise PluginConfigError("cron expression is invalid") from None
    if not candidates:
        raise PluginConfigError("cron expression has no future run")
    return min(candidates, key=lambda value: value.astimezone(UTC))


def validate_cron(expression: str, timezone: str = "Asia/Shanghai") -> str:
    """规范化五段 Cron，并确认指定时区内存在未来触发时间。"""
    zone = _timezone(timezone)
    normalized = _cron(expression)
    _next_fire(normalized, zone, datetime.now(UTC))
    return normalized


def next_fire(expression: str, timezone: str, after: datetime) -> datetime:
    """按指定时区返回严格晚于参考时间的下一次触发时间。

    Args:
        expression: 五段 Cron 表达式，日期和星期采用 OR 语义。
        timezone: IANA 时区名称。
        after: 带时区的参考时间，不补跑此时间之前的任务。

    Raises:
        PluginConfigError: 表达式、时区或参考时间无效，或未来无解。
    """
    if not isinstance(
            after,
            datetime) or after.tzinfo is None or after.utcoffset() is None:
        raise PluginConfigError("schedule reference must be timezone-aware")
    return _next_fire(_cron(expression), _timezone(timezone), after)


def _dependencies(value: object, runtime: str) -> tuple[str, ...]:
    """规范化包声明，拒绝重复包、外部地址及 Python 条件标记。"""
    if type(value) is not list or len(value) > 128:
        raise PluginConfigError(
            "dependencies must be a list with at most 128 entries")
    result = []
    seen = set()
    for item in value:
        if type(item) is not str or not item or item != item.strip() or any(
                ord(c) < 32 or ord(c) == 127 for c in item):
            raise PluginConfigError("dependency declaration is invalid")
        if runtime == "python":
            try:
                requirement = Requirement(item)
            except InvalidRequirement:
                raise PluginConfigError(
                    "Python dependency declaration is invalid") from None
            if requirement.url is not None or requirement.marker is not None:
                raise PluginConfigError(
                    "Python dependency URLs and markers are forbidden")
            name = canonicalize_name(requirement.name)
            extras = sorted(
                canonicalize_name(extra) for extra in requirement.extras)
            normalized = name + ("[" + ",".join(extras) + "]"
                                 if extras else "") + str(requirement.specifier)
        else:
            split_at = item.find("@",
                                 1) if item.startswith("@") else item.find("@")
            name = item if split_at < 0 else item[:split_at]
            version = None if split_at < 0 else item[split_at + 1:]
            if len(name) > 214 or _NODE_NAME.fullmatch(
                    name) is None or name in {"node_modules", "favicon.ico"}:
                raise PluginConfigError("Node dependency name is invalid")
            if version is not None and (
                    not version or version != version.strip() or
                    _NODE_VERSION.fullmatch(version) is None):
                raise PluginConfigError("Node dependency version is invalid")
            normalized = name + ("@" + version if version is not None else "")
        if name in seen:
            raise PluginConfigError("dependency names are duplicated")
        seen.add(name)
        result.append(normalized)
    if len(json_bytes(result)) > 32 * 1024:
        raise PluginConfigError("dependency JSON exceeds 32 KiB")
    return tuple(result)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise PluginConfigError("configuration switch must be boolean")
    return value


def _timeout(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 240:
        raise PluginConfigError(
            "timeout must be an integer from 1 to 240 seconds")
    return value


def _script(raw: object) -> Script:
    """校验单个脚本声明，忽略原生配置模板的标记字段。"""
    fields = {
        "id", "name", "enabled", "url", "runtime", "env_names", "dependencies"
    }
    if not isinstance(
            raw, Mapping) or set(raw) - fields - {"__template_key"
                                                 } or not fields.issubset(raw):
        raise PluginConfigError(
            "script must contain exactly its seven declaration fields")
    if "__template_key" in raw and raw["__template_key"] != "script":
        raise PluginConfigError("script template is invalid")
    identifier = raw["id"]
    if type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None:
        raise PluginConfigError("script ID is invalid")
    name = raw["name"]
    if type(name) is not str or not name.strip() or any(
            ord(c) < 32 or ord(c) == 127 for c in name):
        raise PluginConfigError("script name is invalid")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise PluginConfigError("script name is not valid UTF-8") from None
    runtime = raw["runtime"]
    if runtime not in ("python", "node"):
        raise PluginConfigError("script runtime must be python or node")
    names = raw["env_names"]
    if type(names) is not list or len(names) > MAX_ENV_FIELDS:
        raise PluginConfigError(
            "script fields must be a list of at most 64 names")
    normalized = tuple(validate_env_name(name) for name in names)
    if len({name.upper() for name in normalized}) != len(normalized):
        raise PluginConfigError("script field names are duplicated")
    return Script(identifier, name, _boolean(raw["enabled"]),
                  validate_script_url(raw["url"]), runtime, normalized,
                  _dependencies(raw["dependencies"], runtime))


def parse_config(raw: Mapping) -> PluginConfig:
    """将原生配置解析为不可变快照，拒绝无效字段和重复脚本 ID。

    Raises:
        PluginConfigError: 配置不符合声明约束；错误消息不含凭据值。
    """
    if not isinstance(raw, Mapping):
        raise PluginConfigError("plugin configuration must be a mapping")
    qqs = raw.get("whitelist_qqs", [])
    if type(qqs) is not list:
        raise PluginConfigError("QQ whitelist must be a list")
    timezone = raw.get("timezone", "Asia/Shanghai")
    _timezone(timezone)
    scripts = raw.get("scripts", [])
    if type(scripts) is not list:
        raise PluginConfigError("scripts must be a list")
    declarations = tuple(_script(script) for script in scripts)
    if len({script.id for script in declarations}) != len(declarations):
        raise PluginConfigError("script IDs are duplicated")
    return PluginConfig(_boolean(raw.get("enabled", False)),
                        frozenset(_normalize_qq(qq) for qq in qqs), timezone,
                        validate_cron(raw.get("cron", "0 7 * * *"), timezone),
                        _timeout(raw.get("timeout_seconds", 180)),
                        _timeout(raw.get("install_timeout_seconds", 240)),
                        declarations)
