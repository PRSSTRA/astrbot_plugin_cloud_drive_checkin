import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import bindings
from .config import PluginConfigError, parse_config
from .credentials import CredentialError
from .enrollment import Enrollment, raw_private_text
from .permissions import authorize, get_private_identity
from .state import InvalidStateError
from .storage import CommitUnconfirmedError, StateStore, StateUnavailableError
from .task_config import TaskConfig, next_run_times, parse_task_catalog

_STATE_REGISTRY_ATTRIBUTE = "_cloud_drive_checkin_state_registry"
_CONFIG_ERROR_MESSAGE = "插件配置无效，请联系管理员。"
_PERMISSION_DENIED_MESSAGE = "无权使用签到功能。"
_USAGE_MESSAGE = (
    "用法：/签到 帮助\n用法：/签到 任务 [任务ID]\n"
    "用法：/签到 录入 <任务ID>\n用法：/签到 我的\n用法：/签到 状态 [任务ID]"
    "\n用法：/签到 暂停/恢复 <任务ID>\n用法：/签到 通知 <任务ID> 开/关"
    "\n用法：/签到 删除 <任务ID> [确认/取消]"
)
_TASK_NOT_FOUND_MESSAGE = "未找到该签到任务。"
_DELETE_CONFIRM_SECONDS = 60
_MAX_DELETE_REQUESTS = 128


def _task_status(task: TaskConfig) -> str:
    if task.enabled:
        return "配置启用（当前版本仅预览，不执行）"
    return "配置禁用（仅预览，不执行）"


def _render_task_list(tasks: tuple[TaskConfig, ...]) -> str:
    if not tasks:
        return "暂无配置的签到任务。\n当前版本仅提供预览，不执行任务。"

    lines = ["签到任务（当前版本仅提供预览，不执行任务）："]
    lines.extend(
        f"- {task.task_id}｜{task.name}｜{_task_status(task)}" for task in tasks
    )
    return "\n".join(lines)


def _render_task_detail(task: TaskConfig, timezone: str) -> str:
    run_times = next_run_times(task, timezone)
    lines = [
        f"任务：{task.name}",
        f"ID：{task.task_id}",
        f"状态：{_task_status(task)}",
        f"Cron：{task.cron}",
        f"时区：{timezone}",
        "字段要求：",
    ]
    if task.user_fields:
        lines.extend(
            "- "
            f"{field.field_id}（{field.label}）：{field.value_type}，"
            f"{'必填' if field.required else '可选'}，"
            f"{'秘密字段' if field.secret else '非秘密字段'}"
            for field in task.user_fields
        )
    else:
        lines.append("- 无")

    lines.append("未来 3 次计划时间：")
    lines.extend(
        f"- {run_time.isoformat(timespec='minutes')}" for run_time in run_times
    )
    lines.append(f"配置版本：{task.version}")
    return "\n".join(lines)


class CloudDriveCheckin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        parse_config(config)
        parse_task_catalog(config)
        super().__init__(context)
        self.config = config
        self.state_store: StateStore | None = None
        self.enrollment: Enrollment | None = None
        self._delete_requests: dict[tuple[str, str, str], tuple[str, float]] = {}

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

    def _enrollment_guard(self, event, task_id: str, version: str) -> None:
        identity = get_private_identity(event)
        try:
            permissions = parse_config(self.config)
            if (
                identity is None
                or authorize(
                    identity, astrbot_admin=event.is_admin(), config=permissions
                )
                is None
            ):
                raise CredentialError("enrollment authorization changed")
            catalog = parse_task_catalog(self.config)
        except PluginConfigError:
            raise CredentialError("enrollment configuration changed") from None
        if not any(
            task.task_id == task_id and task.version == version
            for task in catalog.tasks
        ):
            raise CredentialError("enrollment task changed")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1000)
    async def capture_enrollment(self, event: AstrMessageEvent) -> None:
        identity = get_private_identity(event)
        enrollment = self.enrollment
        if identity is None or enrollment is None or enrollment.closed:
            return
        try:
            active = enrollment.active(identity)
        except StateUnavailableError:
            return
        if not active:
            return
        event.should_call_llm(False)
        event.stop_event()
        message = await enrollment.consume(
            identity,
            raw_private_text(event),
            lambda task_id, version: self._enrollment_guard(event, task_id, version),
        )
        await event.send(event.plain_result(message))

    def _binding_guard(self, event, identity) -> None:
        try:
            permissions = parse_config(self.config)
        except PluginConfigError:
            raise bindings.BindingError("permission configuration changed") from None
        if (
            get_private_identity(event) != identity
            or authorize(identity, astrbot_admin=event.is_admin(), config=permissions)
            is None
        ):
            raise bindings.BindingError("binding authorization changed")
        if self.enrollment is not None and self.enrollment.active(identity):
            raise bindings.BindingBusy("credential enrollment is active")

    async def _binding_command(self, event, identity, command, task_id, option) -> str:
        if not task_id:
            return _USAGE_MESSAGE
        if command in {"暂停", "恢复"} and option:
            return _USAGE_MESSAGE
        if command == "通知" and option not in {"开", "关"}:
            return _USAGE_MESSAGE
        if command == "删除" and option not in {"", "确认", "取消"}:
            return _USAGE_MESSAGE

        def guard():
            self._binding_guard(event, identity)

        try:
            guard()
            store = self.state_store
            if store is None:
                raise StateUnavailableError("state store is unavailable")
            if command in {"暂停", "恢复"}:
                await bindings.set_paused(
                    store,
                    identity=identity,
                    task_id=task_id,
                    paused=command == "暂停",
                    guard=guard,
                )
                return (
                    "本人绑定已暂停，全局任务配置未改变。"
                    if command == "暂停"
                    else "本人绑定已恢复；当前仍不执行签到。"
                )
            if command == "通知":
                await bindings.set_notify_all(
                    store,
                    identity=identity,
                    task_id=task_id,
                    notify_all=option == "开",
                    guard=guard,
                )
                preference = "每次结果" if option == "开" else "仅失败"
                return f"已保存自动通知偏好：{preference}；当前尚不发送签到通知。"

            now = asyncio.get_running_loop().time()
            self._delete_requests = {
                key: request
                for key, request in self._delete_requests.items()
                if request[1] > now
            }
            key = (identity.platform_id, identity.qq_id, task_id)
            if option == "取消":
                removed = self._delete_requests.pop(key, None)
                return "删除请求已取消。" if removed else "没有待确认的删除请求。"
            if option == "确认":
                request = self._delete_requests.pop(key, None)
                if request is None:
                    return "没有有效的删除请求，请重新发起删除。"
                await bindings.delete_binding(
                    store,
                    identity=identity,
                    task_id=task_id,
                    expected_version=request[0],
                    guard=guard,
                )
                return "本人绑定和加密凭据已删除。"
            if (
                key not in self._delete_requests
                and len(self._delete_requests) >= _MAX_DELETE_REQUESTS
            ):
                return "待确认请求已满，请稍后重试。"
            version = bindings.delete_version(store, identity=identity, task_id=task_id)
            self._delete_requests[key] = (version, now + _DELETE_CONFIRM_SECONDS)
            return (
                f"将删除本人任务 {task_id} 的绑定和加密凭据。\n"
                f"请在 60 秒内发送 /签到 删除 {task_id} 确认；"
                f"放弃请发送 /签到 删除 {task_id} 取消。\n"
                "不会清除 QQ、框架入站记录或备份。"
            )
        except bindings.BindingNotFound:
            return "没有此任务的本人绑定。"
        except bindings.BindingChanged:
            return "绑定内容已变化，请重新发起删除。"
        except bindings.BindingBusy:
            return "尚有未结束的凭据录入，请先发送 /取消 退出向导。"
        except bindings.BindingError:
            return "绑定数据或权限已变化，未执行操作。"
        except CommitUnconfirmedError:
            return "状态写入有待确认结果，请管理员恢复存储后查询本人状态，勿重复提交。"
        except StateUnavailableError:
            return "状态存储未就绪，暂不能修改绑定。"
        except InvalidStateError:
            return "状态容量或结构校验失败，未保存本次变更。"

    @filter.command("签到", priority=100)
    async def checkin(
        self,
        event: AstrMessageEvent,
        subcommand: str = "帮助",
        task_id: str = "",
        extra: str = "",
        tail: str = "",
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

        try:
            task_catalog = parse_task_catalog(self.config)
        except PluginConfigError:
            await event.send(event.plain_result(_CONFIG_ERROR_MESSAGE))
            return

        if tail:
            await event.send(event.plain_result(_USAGE_MESSAGE))
            return
        if subcommand in {"暂停", "恢复", "通知", "删除"}:
            message = await self._binding_command(
                event, identity, subcommand, task_id, extra
            )
            await event.send(event.plain_result(message))
            return

        if extra:
            await event.send(event.plain_result(_USAGE_MESSAGE))
            return
        enrollment = self.enrollment
        if subcommand in {"录入", "我的", "状态"}:
            if enrollment is None or enrollment.closed:
                await event.send(event.plain_result("状态存储未就绪。"))
                return
            if subcommand == "录入":
                task = next(
                    (task for task in task_catalog.tasks if task.task_id == task_id),
                    None,
                )
                if task is None:
                    await event.send(event.plain_result(_TASK_NOT_FOUND_MESSAGE))
                    return
                message = await enrollment.begin(
                    identity,
                    task,
                    event.unified_msg_origin,
                    lambda task_id, version: self._enrollment_guard(
                        event, task_id, version
                    ),
                )
            elif subcommand == "我的" and task_id:
                message = _USAGE_MESSAGE
            else:
                try:
                    message = enrollment.describe(identity, task_id)
                except (CredentialError, StateUnavailableError):
                    message = "绑定状态不可用，请联系管理员。"
            await event.send(event.plain_result(message))
            return

        if subcommand == "任务":
            if not task_id:
                message = _render_task_list(task_catalog.tasks)
            else:
                task = next(
                    (
                        candidate
                        for candidate in task_catalog.tasks
                        if candidate.task_id == task_id
                    ),
                    None,
                )
                if task is None:
                    await event.send(event.plain_result(_TASK_NOT_FOUND_MESSAGE))
                    return
                try:
                    message = _render_task_detail(task, task_catalog.timezone)
                except PluginConfigError:
                    await event.send(event.plain_result(_CONFIG_ERROR_MESSAGE))
                    return
            await event.send(event.plain_result(message))
            return

        if subcommand != "帮助" or task_id:
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
                self.enrollment = Enrollment(store)
            except asyncio.CancelledError:
                store.stop_accepting()
                raise
            except Exception:
                store.stop_accepting()
                raise

    async def terminate(self) -> None:
        self._delete_requests.clear()
        store = self.state_store
        if store is None:
            return
        store.stop_accepting()
        if self.enrollment is not None:
            self.enrollment.close()

        slot = self._state_slot()
        async with slot["lock"]:
            await store.close()
