import asyncio
import copy
import sys
from collections import deque
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

_DEFAULT_RAW_MESSAGE = object()


class PutGate:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()


class FakeKV:
    def __init__(self):
        self.values = {}
        self.put_actions = deque()
        self.get_attempts = 0
        self.put_attempts = 0
        self.committed = []

    def fail_next_put(self):
        self.put_actions.append(OSError("simulated write failure"))

    def block_next_put(self):
        gate = PutGate()
        self.put_actions.append(gate)
        return gate

    async def get_kv_data(self, key, default):
        self.get_attempts += 1
        if key not in self.values:
            return default
        return copy.deepcopy(self.values[key])

    async def put_kv_data(self, key, value):
        self.put_attempts += 1
        action = self.put_actions.popleft() if self.put_actions else None
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, PutGate):
            action.started.set()
            await action.release.wait()

        detached = copy.deepcopy(value)
        self.values[key] = detached
        self.committed.append(copy.deepcopy(detached))


class AstrBotConfig(dict):
    pass


class AstrBotContext:
    def __init__(self):
        self._plugin_kv = {}

    def kv_for(self, plugin_id):
        return self._plugin_kv.setdefault(plugin_id, FakeKV())


class AstrBotStar:
    registered: ClassVar[list] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        AstrBotStar.registered.append(cls)

    def __init__(self, context):
        self.context = context

    async def get_kv_data(self, key, default):
        return await self.context.kv_for(self.plugin_id).get_kv_data(key, default)

    async def put_kv_data(self, key, value):
        await self.context.kv_for(self.plugin_id).put_kv_data(key, value)


class AstrBotStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        raise AssertionError("tests must supply an isolated plugin data directory")


class AstrMessageEvent:
    pass


class _Filter:
    EventMessageType = SimpleNamespace(PRIVATE_MESSAGE="private")

    @staticmethod
    def event_message_type(message_type, *, priority=0):
        def decorate(handler):
            handler.__astrbot_listener__ = (message_type, priority)
            return handler

        return decorate

    @staticmethod
    def command(name, *, priority=0):
        def decorate(handler):
            handler.__astrbot_command__ = (name, priority)
            return handler

        return decorate


filter = _Filter()


@dataclass(frozen=True)
class PlainResult:
    text: str


class FakeEvent(AstrMessageEvent):
    def __init__(
        self,
        *,
        platform_name="aiocqhttp",
        platform_id="bot-instance",
        sender_id="10001",
        raw_message=_DEFAULT_RAW_MESSAGE,
        astrbot_admin=False,
        text="",
    ):
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._sender_id = sender_id
        raw = (
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "user_id": sender_id,
                "sender": {"user_id": sender_id},
                "message": [{"type": "text", "data": {"text": text}}] if text else [],
            }
            if raw_message is _DEFAULT_RAW_MESSAGE
            else raw_message
        )
        self.message_obj = SimpleNamespace(raw_message=raw)
        self.unified_msg_origin = f"{platform_id}:FriendMessage:{sender_id}"
        self._astrbot_admin = astrbot_admin
        self.llm_enabled = True
        self.stopped = False
        self.admin_checks = 0
        self.actions = []
        self.sent = []

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id

    def is_admin(self):
        self.admin_checks += 1
        return self._astrbot_admin

    def should_call_llm(self, call_llm):
        self.llm_enabled = call_llm
        self.actions.append(("should_call_llm", call_llm))

    def stop_event(self):
        self.stopped = True
        self.actions.append(("stop_event",))

    def plain_result(self, text):
        return PlainResult(text)

    async def send(self, result):
        self.actions.append(("send",))
        self.sent.append(result)


def patch_astrbot_modules():
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    api = ModuleType("astrbot.api")
    api.__path__ = []
    event = ModuleType("astrbot.api.event")
    star = ModuleType("astrbot.api.star")

    api.AstrBotConfig = AstrBotConfig
    event.filter = filter
    event.AstrMessageEvent = AstrMessageEvent
    star.Context = AstrBotContext
    star.Star = AstrBotStar
    star.StarTools = AstrBotStarTools
    api.event = event
    api.star = star
    astrbot.api = api

    return patch.dict(
        sys.modules,
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        },
    )
