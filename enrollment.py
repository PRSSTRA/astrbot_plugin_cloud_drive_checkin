"""Private credential enrollment; only encrypted terminal values enter KV."""

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .credentials import (
    CredentialCipher,
    CredentialError,
    binding_key,
    save_binding,
    user_key,
    validate_values,
)
from .permissions import Identity
from .state import InvalidStateError
from .storage import StateStore, StateUnavailableError
from .task_config import TaskConfig

EXPIRED = "录入已停止或过期，未提交内容已清除。发送 /取消 退出，再重新开始。"
UNCONFIRMED = (
    "存储提交未确认，已停止录入。请管理员恢复存储后查询本人状态，不要重复提交。"
)


def raw_private_text(event) -> str | None:
    # AstrBot's OneBot adapter strips text; credential bytes must come from raw segments.
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("message"), list):
        return None
    parts = []
    for segment in raw["message"]:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            return None
        data = segment.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("text"), str):
            return None
        parts.append(data["text"])
    return "".join(parts) if parts else None


@dataclass(repr=False)
class _Session:
    task: TaskConfig
    cipher: CredentialCipher
    origin: str
    deadline: float
    values: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    index: int = 0
    busy: bool = False
    expired: bool = False
    timer: asyncio.TimerHandle | None = None


class Enrollment:
    def __init__(self, store: StateStore, *, timeout_seconds: float = 300):
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.sessions: dict[Identity, _Session] = {}
        self.closed = False

    def active(self, identity: Identity) -> bool:
        if identity in self.sessions:
            return True
        record = self.store.snapshot()["users"].get(user_key(identity))
        return isinstance(record, dict) and "enrollment" in record

    def _expire(self, session: _Session) -> None:
        session.values.clear()
        session.sizes.clear()
        session.expired = True
        if session.timer is not None:
            session.timer.cancel()
        # Keep the non-secret KV intent: late input after expiry/reload must not reach LLM.

    def _discard(self, identity: Identity) -> None:
        session = self.sessions.pop(identity, None)
        if session is not None:
            self._expire(session)

    def close(self) -> None:
        self.closed = True
        for identity in tuple(self.sessions):
            self._discard(identity)

    @staticmethod
    def _prompt(session: _Session) -> str:
        if session.index == len(session.task.user_fields):
            return "字段已接收，不回显内容。发送 /确认 保存，或 /取消 放弃。"
        current = session.task.user_fields[session.index]
        optional = "必填" if current.required else "可选，可发送 /跳过"
        return f"请输入 {current.label}（{current.field_id}，{current.value_type}，{optional}）。"

    async def begin(
        self,
        identity: Identity,
        task: TaskConfig,
        origin: str,
        guard: Callable[[str, str], None],
    ) -> str:
        if self.closed or not self.store.ready:
            return "状态存储未就绪，不能开始录入。"
        if self.active(identity):
            return "已有未结束的录入，请先发送 /取消。"
        if len(self.sessions) >= 128:
            return "录入会话已满，请稍后重试。"
        if not isinstance(origin, str) or not origin:
            return "无法确认通知会话，不能开始录入。"
        try:
            cipher = CredentialCipher.from_environment()
        except CredentialError:
            return "凭据主密钥未配置或无效，请联系管理员。不要发送凭据。"
        session = _Session(task, cipher, origin, 0, busy=True)
        self.sessions[identity] = session

        def mark_intent(snapshot):
            if self.closed:
                raise CredentialError("enrollment is closed")
            guard(task.task_id, task.version)
            key = user_key(identity)
            previous = snapshot["users"].get(key, {})
            if not isinstance(previous, dict) or (
                previous
                and (
                    previous.get("platform_id") != identity.platform_id
                    or previous.get("qq_id") != identity.qq_id
                )
            ):
                raise CredentialError("invalid user record")
            record = dict(previous)
            record.update(
                platform_id=identity.platform_id,
                qq_id=identity.qq_id,
                unified_msg_origin=origin,
                enrollment={
                    "task_id": task.task_id,
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )
            snapshot["users"][key] = record

        try:
            await self.store.mutate(mark_intent)
        except StateUnavailableError:
            self._expire(session)
            session.busy = False
            return UNCONFIRMED
        except InvalidStateError:
            self._discard(identity)
            return "状态容量或结构校验失败，未开始录入，请联系管理员。"
        except CredentialError:
            self._discard(identity)
            return "权限或任务配置已变化，不能开始录入。"
        except asyncio.CancelledError:
            self._expire(session)
            session.busy = False
            raise
        if self.closed:
            self._discard(identity)
            return EXPIRED
        loop = asyncio.get_running_loop()
        session.deadline = loop.time() + self.timeout_seconds
        session.timer = loop.call_later(self.timeout_seconds, self._discard, identity)
        session.busy = False
        return (
            "录入限时 5 分钟；QQ 与框架入站记录可能保留消息，本插件无法清除。\n"
            "向导中每条纯文本均作为字段输入，不交给 LLM；/取消 退出，/确认 最终保存。\n"
            "值恰为控制词或以 /值 开头时，使用 /值 原文。\n" + self._prompt(session)
        )

    async def _cancel(self, identity: Identity) -> str:
        self._discard(identity)

        def clear_intent(snapshot):
            record = snapshot["users"].get(user_key(identity))
            if isinstance(record, dict):
                record.pop("enrollment", None)

        try:
            await self.store.mutate(clear_intent)
        except StateUnavailableError:
            return UNCONFIRMED
        except InvalidStateError:
            return "状态容量或结构校验失败，暂不能退出保护，请联系管理员。"
        return "录入已取消，未提交内容已清除。"

    async def consume(
        self,
        identity: Identity,
        text: str | None,
        guard: Callable[[str, str], None],
    ) -> str:
        session = self.sessions.get(identity)
        if session is not None and session.busy:
            return "上一条消息仍在处理，请稍后再发；本条内容未保存。"
        if text == "/取消":
            return await self._cancel(identity)
        if self.closed or session is None or session.expired:
            return EXPIRED
        if asyncio.get_running_loop().time() >= session.deadline:
            self._discard(identity)
            return EXPIRED
        try:
            guard(session.task.task_id, session.task.version)
        except CredentialError:
            self._discard(identity)
            return EXPIRED
        if not self.store.ready:
            return "存储正在等待确认，请恢复后重发；本条内容未保存。"
        if text is None:
            return "只接收纯文本消息，请勿使用图片、引用或文件。"
        session.busy = True
        try:
            if text == "/确认":
                if session.index != len(session.task.user_fields):
                    return "字段尚未完成。" + self._prompt(session)

                def final_guard():
                    if (
                        self.closed
                        or session.expired
                        or asyncio.get_running_loop().time() >= session.deadline
                    ):
                        raise CredentialError("enrollment has expired")
                    guard(session.task.task_id, session.task.version)
                    validate_values(session.task, session.values)

                try:
                    final_guard()
                    await save_binding(
                        self.store,
                        session.cipher,
                        identity=identity,
                        origin=session.origin,
                        task=session.task,
                        values=session.values,
                        guard=final_guard,
                    )
                    return "本人绑定和凭据已加密保存。"
                except CredentialError:
                    return "权限、任务或字段已变化，未保存本次凭据。发送 /取消 退出。"
                except StateUnavailableError:
                    return UNCONFIRMED
                except InvalidStateError:
                    return "状态容量或结构校验失败，未保存本次凭据。发送 /取消 退出。"
                finally:
                    self._discard(identity)
            if session.index == len(session.task.user_fields):
                return "字段已完成，请发送 /确认 或 /取消；本条内容未保存。"
            current = session.task.user_fields[session.index]
            if text == "/跳过":
                if current.required:
                    return "此字段必填，不能跳过。"
            else:
                value_text = text.removeprefix("/值 ")
                try:
                    size = len(value_text.encode("utf-8"))
                    if (
                        size > 16 * 1024
                        or sum(session.sizes.values()) + size > 64 * 1024
                    ):
                        return (
                            "字段超过大小限制：单字段 16 KiB，总计 64 KiB；请重新输入。"
                        )
                    if "\x00" in value_text or (current.required and not value_text):
                        return "字段值无效，请重新输入。"
                    if current.value_type == "string":
                        value = value_text
                    elif current.value_type == "int":
                        if re.fullmatch(r"[+-]?[0-9]+", value_text) is None:
                            return "请输入十进制整数，不要添加空白。"
                        value = int(value_text)
                    else:
                        if value_text.lower() not in {"true", "false"}:
                            return "请输入 true 或 false。"
                        value = value_text.lower() == "true"
                except (UnicodeError, ValueError):
                    return "字段值无效，请重新输入。"
                session.values[current.field_id] = value
                session.sizes[current.field_id] = size
            session.index += 1
            return self._prompt(session)
        finally:
            session.busy = False

    def describe(self, identity: Identity, task_id: str = "") -> str:
        snapshot = self.store.snapshot()
        owner = user_key(identity)
        lines = []
        for key, record in snapshot["bindings"].items():
            if not isinstance(record, dict) or record.get("user_key") != owner:
                continue
            bound_task = record.get("task_id")
            if (
                not isinstance(bound_task, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", bound_task) is None
                or key != binding_key(identity, bound_task)
            ):
                raise CredentialError("invalid binding record")
            if task_id and task_id != bound_task:
                continue
            configured = key in snapshot["credentials"]
            paused = record.get("paused", False)
            notify_all = record.get("notify_all", False)
            if type(paused) is not bool or type(notify_all) is not bool:
                raise CredentialError("invalid binding preferences")
            lines.append(
                f"- {bound_task}：凭据{'已配置' if configured else '未配置'}；"
                f"订阅{'已暂停' if paused else '未暂停'}；"
                f"自动通知{'每次结果' if notify_all else '仅失败'}"
            )
        result = (
            "本人绑定：\n" + "\n".join(lines) if lines else "没有符合条件的本人绑定。"
        )
        if not self.store.ready:
            result += "\n存储未就绪，仅显示最后确认保存的状态。"
        return result
