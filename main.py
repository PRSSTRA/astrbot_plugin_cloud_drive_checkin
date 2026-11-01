import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .config import PluginConfigError, parse_config
from .permissions import authorize, get_private_identity
from .storage import StateStore


_STATE_REGISTRY_ATTRIBUTE = "_cloud_drive_checkin_state_registry"
_CONFIG_ERROR_MESSAGE = "插件配置无效，请联系管理员。"
_PERMISSION_DENIED_MESSAGE = "无权使用签到帮助。"
_USAGE_MESSAGE = "用法：/签到 帮助"


class CloudDriveCheckin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        parse_config(config)
        super().__init__(context)
        self.config = config
        self.state_store: StateStore | None = None

    def _state_slot(self) -> dict:
        registry = getattr(self.context, _STATE_REGISTRY_ATTRIBUTE, None)
        if registry is None:
            registry = {}
            setattr(self.context, _STATE_REGISTRY_ATTRIBUTE, registry)

        slot = registry.get(self.plugin_id)
        if slot is None:
            slot = {"lock": asyncio.Lock(), "owner": None}
            registry[self.plugin_id] = slot
        return slot

    @filter.command("签到", priority=100)
    async def checkin(
        self,
        event: AstrMessageEvent,
        subcommand: str = "帮助",
    ) -> None:
        event.should_call_llm(False)
        event.stop_event()

        identity = get_private_identity(event)
        if identity is None:
            return

        store = self.state_store
        if store is None or store.closing or store.closed:
            return

        try:
            permission_config = parse_config(self.config)
        except PluginConfigError:
            await event.send(event.plain_result(_CONFIG_ERROR_MESSAGE))
            return

        access = authorize(
            identity,
            astrbot_admin=event.is_admin(),
            config=permission_config,
        )
        if access is None:
            await event.send(event.plain_result(_PERMISSION_DENIED_MESSAGE))
            return

        if subcommand != "帮助":
            await event.send(event.plain_result(_USAGE_MESSAGE))
            return

        role = "管理员" if access.is_admin else "白名单用户"
        service = "开启" if permission_config.enabled else "关闭"
        readiness = "就绪" if store.ready else "未就绪"
        help_message = "\n".join(
            (
                _USAGE_MESSAGE,
                f"角色：{role}",
                f"服务开关：{service}",
                f"状态存储：{readiness}",
            )
        )
        await event.send(event.plain_result(help_message))

    async def initialize(self) -> None:
        slot = self._state_slot()
        async with slot["lock"]:
            owner = slot["owner"]
            if owner is not None and not owner.closed:
                if not owner.closing:
                    raise RuntimeError("the plugin state already has an active owner")
                await owner.close()
                if not owner.closed:
                    raise RuntimeError("the previous plugin state owner did not close")

            store = StateStore(self)
            self.state_store = store
            slot["owner"] = store
            try:
                await store.initialize()
            except asyncio.CancelledError:
                store.stop_accepting()
                raise
            except Exception:
                store.stop_accepting()
                raise

    async def terminate(self) -> None:
        store = self.state_store
        if store is None:
            return

        slot = self._state_slot()
        async with slot["lock"]:
            await store.close()
