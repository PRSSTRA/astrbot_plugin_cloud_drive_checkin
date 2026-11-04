"""QQ 好友私聊身份与授权校验，复用 dev@a662b766（GPL-3.0）。"""
from collections.abc import Mapping
from dataclasses import dataclass
import json

from .config import _normalize_qq
from .config import PluginConfig
from .config import PluginConfigError


@dataclass(frozen=True)
class Identity:
    """平台实例与 QQ 号组成的独立身份。"""

    platform_id: str
    qq_id: str


def get_private_identity(event) -> Identity | None:
    """交叉校验适配器与原始事件身份；非普通好友私聊返回 None。"""
    if event.get_platform_name() != "aiocqhttp":
        return None
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    if (raw.get("post_type") != "message" or
            raw.get("message_type") != "private" or
            raw.get("sub_type") != "friend" or "temp_source" in raw or
            raw.get("group_id") is not None):
        return None
    platform_id = event.get_platform_id()
    if type(platform_id) is not str or not platform_id or platform_id.isspace():
        return None
    sender = raw.get("sender")
    if not isinstance(sender, Mapping):
        return None
    try:
        qq = _normalize_qq(event.get_sender_id())
        if qq != _normalize_qq(raw.get("user_id")) or qq != _normalize_qq(
                sender.get("user_id")):
            return None
        platform_id.encode("utf-8")
    except (PluginConfigError, UnicodeError):
        return None
    return Identity(platform_id, qq)


def authorize(identity: Identity, *, astrbot_admin: bool,
              config: PluginConfig) -> bool:
    """检查管理员或白名单授权；调用前必须已通过好友私聊校验。"""
    return bool(astrbot_admin or identity.qq_id in config.whitelist_qqs)


def owner_key(identity: Identity) -> str:
    """生成无分隔符歧义的跨平台用户键。"""
    return json.dumps((identity.platform_id, identity.qq_id),
                      ensure_ascii=False,
                      separators=(",", ":"))


def binding_key(identity: Identity, script_id: str) -> str:
    """生成用户与脚本的唯一绑定键。"""
    return json.dumps((identity.platform_id, identity.qq_id, script_id),
                      ensure_ascii=False,
                      separators=(",", ":"))


def raw_private_text(event) -> str | None:
    """仅接受纯文本片段，按原样拼接；混合消息返回 None。"""
    # 适配器会裁剪 message_str；凭据必须从原始片段读取以保留空白。
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("message"), list):
        return None
    parts = []
    for segment in raw["message"]:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            return None
        data = segment.get("data")
        if not isinstance(data, Mapping) or type(data.get("text")) is not str:
            return None
        parts.append(data["text"])
    return "".join(parts) if parts else None
