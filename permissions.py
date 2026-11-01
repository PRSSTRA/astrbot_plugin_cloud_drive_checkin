from collections.abc import Mapping
from dataclasses import dataclass

from .config import PermissionConfig, PluginConfigError, _normalize_qq


@dataclass(frozen=True)
class Identity:
    platform_id: str
    qq_id: str


@dataclass(frozen=True)
class Access:
    identity: Identity
    is_admin: bool


def get_private_identity(event) -> Identity | None:
    if event.get_platform_name() != "aiocqhttp":
        return None

    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    if (
        raw.get("post_type") != "message"
        or raw.get("message_type") != "private"
        or raw.get("sub_type") != "friend"
    ):
        return None
    if "temp_source" in raw or raw.get("group_id") is not None:
        return None

    platform_id = event.get_platform_id()
    if (
        not isinstance(platform_id, str)
        or not platform_id
        or platform_id.isspace()
    ):
        return None

    sender = raw.get("sender")
    if not isinstance(sender, Mapping):
        return None

    try:
        event_qq = _normalize_qq(event.get_sender_id())
        raw_qq = _normalize_qq(raw.get("user_id"))
        sender_qq = _normalize_qq(sender.get("user_id"))
    except PluginConfigError:
        return None
    if event_qq != raw_qq or event_qq != sender_qq:
        return None

    return Identity(platform_id=platform_id, qq_id=event_qq)


def authorize(
    identity: Identity,
    *,
    astrbot_admin: bool,
    config: PermissionConfig,
) -> Access | None:
    if astrbot_admin or identity.qq_id in config.plugin_admin_qqs:
        return Access(identity=identity, is_admin=True)
    if identity.qq_id in config.whitelist_qqs:
        return Access(identity=identity, is_admin=False)
    return None
