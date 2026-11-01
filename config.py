from collections.abc import Mapping
from dataclasses import dataclass


class PluginConfigError(ValueError):
    """Raised when the plugin permission configuration is malformed."""


@dataclass(frozen=True)
class PermissionConfig:
    enabled: bool
    plugin_admin_qqs: frozenset[str]
    whitelist_qqs: frozenset[str]


def _normalize_qq(value: object) -> str:
    if isinstance(value, bool):
        raise PluginConfigError("QQ identifiers must be positive ASCII decimal numbers")
    if isinstance(value, int):
        if value > 0:
            return str(value)
        raise PluginConfigError("QQ identifiers must be positive ASCII decimal numbers")
    if isinstance(value, str):
        if (
            value
            and value[0] in "123456789"
            and all(character in "0123456789" for character in value)
        ):
            return value
    raise PluginConfigError("QQ identifiers must be positive ASCII decimal numbers")


def _parse_qq_list(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise PluginConfigError("QQ identifier collections must be lists")
    return frozenset(_normalize_qq(item) for item in value)


def parse_config(raw: Mapping) -> PermissionConfig:
    if not isinstance(raw, Mapping):
        raise PluginConfigError("plugin configuration must be a mapping")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise PluginConfigError("enabled must be a boolean")

    return PermissionConfig(
        enabled=enabled,
        plugin_admin_qqs=_parse_qq_list(raw.get("plugin_admin_qqs", [])),
        whitelist_qqs=_parse_qq_list(raw.get("whitelist_qqs", [])),
    )
