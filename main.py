import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from . import bindings
from .artifact_service import PreparationBusy, PreparationService
from .config import PluginConfigError, parse_config
from .credentials import CredentialError
from .enrollment import Enrollment, raw_private_text
from .execution_bridge import ExecutionBridge, describe_execution
from .execution_config import parse_execution_config
from .execution_queue import ExecutionQueue, QueueRejected
from .executors.client import NeoClient, resolve_connection
from .executors.neo import ExecutionBlocked, NeoExecutor
from .notifications import NotificationService, enqueue_terminal, reserve_terminal
from .permissions import authorize, get_private_identity
from .preparation import PreparationError
from .scheduler import CronScheduler
from .state import InvalidStateError
from .storage import CommitUnconfirmedError, StateStore, StateUnavailableError
from .task_config import TaskConfig, next_run_times, parse_task_catalog

_STATE_REGISTRY_ATTRIBUTE = "_cloud_drive_checkin_state_registry"
_CONFIG_ERROR_MESSAGE = "插件配置无效，请联系管理员。"
_PERMISSION_DENIED_MESSAGE = "无权使用签到功能。"
_USAGE_MESSAGE = (
    "用法：/签到 帮助\n用法：/签到 任务 [任务ID]\n"
    "用法：/签到 录入 <任务ID>\n用法：/签到 我的\n用法：/签到 状态 [任务ID]"
    "\n用法：/签到 立即 <任务ID>"
    "\n用法：/签到 暂停/恢复 <任务ID>\n用法：/签到 通知 <任务ID> 开/关"
    "\n用法：/签到 删除 <任务ID> [确认/取消]"
    "\n用法：/签到 管理 准备 <任务ID>"
    "\n用法：/签到 管理 取消 <run_id>"
)
_TASK_NOT_FOUND_MESSAGE = "未找到该签到任务。"
_DELETE_CONFIRM_SECONDS = 60
_MAX_DELETE_REQUESTS = 128


class _PreparationAccessChanged(PreparationError):
    """The administrator or task changed while preparing an artifact."""


def _task_status(task: TaskConfig) -> str:
    if task.enabled:
        return "配置启用"
    return "配置禁用"


def _render_task_list(tasks: tuple[TaskConfig, ...]) -> str:
    if not tasks:
        return "暂无配置的签到任务。"

    lines = ["签到任务（启用后按 Cron 入队，也可使用 /签到 立即）："]
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
        parse_execution_config(config)
        super().__init__(context)
        self.config = config
        self.state_store: StateStore | None = None
        self.enrollment: Enrollment | None = None
        self._delete_requests: dict[tuple[str, str, str], tuple[str, float]] = {}
        self.executor: NeoExecutor | None = None
        self.preparation: PreparationService | None = None
        self.queue: ExecutionQueue | None = None
        self.notifications: NotificationService | None = None
        self.scheduler: CronScheduler | None = None
        self.execution_bridge = ExecutionBridge(self)
        self._shutdown_task: asyncio.Task | None = None
        self._terminating = False

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
                self._terminating
                or identity is None
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
            self._terminating
            or get_private_identity(event) != identity
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
                    else "本人绑定已恢复。"
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
                return f"已保存自动通知偏好：{preference}；手动签到始终反馈终态。"

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

    def _preparation_guard(self, event, identity, task: TaskConfig) -> None:
        try:
            permissions = parse_config(self.config)
            access = authorize(
                identity, astrbot_admin=event.is_admin(), config=permissions
            )
            catalog = parse_task_catalog(self.config)
        except PluginConfigError:
            raise _PreparationAccessChanged(
                "preparation configuration changed"
            ) from None
        if (
            self._terminating
            or get_private_identity(event) != identity
            or access is None
            or not access.is_admin
            or not any(
                item.task_id == task.task_id and item.version == task.version
                for item in catalog.tasks
            )
        ):
            raise _PreparationAccessChanged("preparation authorization changed")

    def _preparation_service(self) -> PreparationService:
        store = self.state_store
        if store is None or not store.ready:
            raise StateUnavailableError("state store is unavailable")
        if self.preparation is None:
            try:
                data_dir = StarTools.get_data_dir()
            except (OSError, RuntimeError, ValueError):
                raise PreparationError("plugin data directory is unavailable") from None
            self.preparation = PreparationService(store, data_dir)
        return self.preparation

    async def _send_notice(self, origin: str, text: str) -> bool:
        platform_id, separator, qq_id = origin.rpartition(":FriendMessage:")
        if not separator or not qq_id.isascii() or not qq_id.isdecimal():
            return False
        if not any(
            platform.meta().id == platform_id and platform.meta().name == "aiocqhttp"
            for platform in self.context.platform_manager.get_insts()
        ):
            return False
        message = MessageChain().message(text).use_t2i(False).use_markdown(False)
        return await self.context.send_message(origin, message)

    def _terminal_notice(self, snapshot, item, result):
        enqueue_terminal(snapshot, item, result)
        if self.notifications is not None:
            self.notifications.wake()

    async def _manual_command(self, event, identity, task) -> str:
        try:
            queue = self.queue
            if queue is None or self._terminating:
                raise StateUnavailableError("queue is unavailable")
            artifact = await self.execution_bridge.admission(
                identity, task, event.unified_msg_origin
            )
            run_id = await queue.enqueue(
                identity=identity,
                task=task,
                origin=event.unified_msg_origin,
                artifact_sha256=artifact.sha256,
                inputs_sha256=artifact.inputs_sha256,
            )
            return f"签到已入队（重复请求返回同一编号）：{run_id}"
        except QueueRejected as error:
            messages = {
                "permission_revoked": "执行权限已撤销。",
                "service_disabled": "签到服务未启用。",
                "task_disabled": "该任务未启用。",
                "task_changed": "任务配置已变化，请重新请求。",
                "binding_missing": "请先录入本人任务凭据。",
                "binding_paused": "本人绑定已暂停，请先恢复。",
                "enrollment_active": "请先完成或取消凭据录入。",
                "credentials_missing": "凭据不可用，请检查主密钥并重新录入。",
                "artifact_changed": "制品未准备或版本已变化，请管理员重新准备。",
                "executor_unavailable": "Neo 执行器未就绪或资源清理未确认。",
                "cooldown_active": "该任务仍在手动冷却期，请稍后重试。",
                "queue_capacity": "执行队列或历史容量已满，请稍后重试。",
            }
            return messages.get(error.reason, "当前配置或状态不能执行该任务。")
        except ExecutionBlocked:
            return "执行队列暂不可用，请查询 /签到 状态 后重试。"
        except PreparationError:
            return "制品缓存不可用，请管理员检查数据目录并重新准备。"
        except CommitUnconfirmedError:
            return "状态写入未确认，请管理员重载后通过 /签到 状态 查询，勿重复提交。"
        except StateUnavailableError:
            return "状态存储未就绪，请管理员恢复后重试。"
        except (PluginConfigError, InvalidStateError):
            return "执行配置、状态结构或容量校验失败，未接受本次请求。"

    async def _prepare_command(self, event, identity, task: TaskConfig) -> str:
        def guard():
            self._preparation_guard(event, identity, task)

        try:
            guard()
            artifact = await self._preparation_service().prepare(task, guard=guard)
            guard()
            return f"制品已准备：{task.task_id}\nSHA256：{artifact.sha256}\n大小：{len(artifact.script)} 字节"
        except PreparationBusy:
            return "已有制品正在准备，请稍后重试。"
        except _PreparationAccessChanged:
            return "管理员权限或任务配置已变化，请重新发起准备。"
        except PreparationError:
            return "制品准备失败，请检查任务配置、PyPI 网络及插件数据目录后重试。"
        except CommitUnconfirmedError:
            return "状态写入未确认，请在插件页重载后重试。"
        except StateUnavailableError:
            return "状态存储未就绪，请在插件页重载后重试。"
        except InvalidStateError:
            return "状态容量或结构校验失败，未发布本次制品。"

    async def _cancel_command(self, event, identity, run_id: str) -> str:
        def guard():
            permissions = parse_config(self.config)
            admins = self.context.get_config(umo=event.unified_msg_origin).get(
                "admins_id", []
            )
            if not isinstance(admins, list) or any(
                type(value) is not str for value in admins
            ):
                raise PluginConfigError(
                    "framework administrator configuration is invalid"
                )
            access = authorize(
                identity,
                astrbot_admin=event.is_admin() and identity.qq_id in admins,
                config=permissions,
            )
            if (
                self._terminating
                or get_private_identity(event) != identity
                or access is None
                or not access.is_admin
            ):
                raise QueueRejected("permission_revoked")

        try:
            guard()
            if self.queue is None:
                raise StateUnavailableError("execution queue is unavailable")
            status = await self.queue.request_cancel(run_id, guard=guard)
            return {
                "requested": "取消请求已记录；最终结果将通知运行所属用户，请通过本人状态查询。",
                "finished": "该运行已结束，未修改结果或重新发送通知。",
                "not_found": "未找到该运行，请检查 run_id。",
            }[status]
        except QueueRejected:
            return "管理员权限已变化，未接受取消请求。"
        except CommitUnconfirmedError:
            return "取消写入未确认，新执行已暂停；请管理员恢复存储后查询，勿重复提交。"
        except StateUnavailableError:
            return "状态存储未就绪，未接受取消请求。"
        except ExecutionBlocked:
            return "执行队列暂不可用，未接受取消请求。"
        except (PluginConfigError, InvalidStateError):
            return "权限配置或状态校验失败，未接受取消请求。"

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
        if self._terminating or store is None or store.closing or store.closed:
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

        if tail:
            await event.send(event.plain_result(_USAGE_MESSAGE))
            return
        if subcommand == "管理":
            if not access.is_admin:
                await event.send(event.plain_result("仅管理员可执行此管理操作。"))
                return
            if task_id == "取消":
                message = (
                    await self._cancel_command(event, identity, extra)
                    if extra
                    else _USAGE_MESSAGE
                )
                await event.send(event.plain_result(message))
                return

        try:
            task_catalog = parse_task_catalog(self.config)
        except PluginConfigError:
            await event.send(event.plain_result(_CONFIG_ERROR_MESSAGE))
            return

        if subcommand == "管理":
            if task_id != "准备" or not extra:
                await event.send(event.plain_result(_USAGE_MESSAGE))
                return
            task = next(
                (task for task in task_catalog.tasks if task.task_id == extra), None
            )
            if task is None:
                await event.send(event.plain_result(_TASK_NOT_FOUND_MESSAGE))
                return
            message = await self._prepare_command(event, identity, task)
            await event.send(event.plain_result(message))
            return
        if subcommand in {"暂停", "恢复", "通知", "删除"}:
            message = await self._binding_command(
                event, identity, subcommand, task_id, extra
            )
            await event.send(event.plain_result(message))
            if self.queue is not None:
                self.queue.wake()
            return

        if extra:
            await event.send(event.plain_result(_USAGE_MESSAGE))
            return
        if subcommand == "立即":
            if not task_id:
                await event.send(event.plain_result(_USAGE_MESSAGE))
                return
            task = next(
                (task for task in task_catalog.tasks if task.task_id == task_id), None
            )
            if task is None:
                await event.send(event.plain_result(_TASK_NOT_FOUND_MESSAGE))
                return
            message = await self._manual_command(event, identity, task)
            await event.send(event.plain_result(message))
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
                    execution = describe_execution(store.snapshot(), identity, task_id)
                    if execution:
                        message += "\n" + execution
                    if self.queue is not None and self.queue.last_error is not None:
                        message += "\n队列暂不可用，请管理员检查执行配置和状态。"
                    if self.notifications is not None and self.notifications.frozen:
                        message += "\n通知发送器已暂停，请管理员重载。"
                    if (
                        self.scheduler is not None
                        and self.scheduler.last_error is not None
                    ):
                        message += "\n自动调度存在未处理错误，请管理员检查配置和状态。"
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
                neo_config = self.config.get("neo_connection", {})
                framework = (
                    self.context.get_config()
                    if isinstance(neo_config, dict)
                    and neo_config.get("enabled", False) is True
                    else {}
                )
                connection = resolve_connection(self.config, framework)
                if connection is not None:
                    self.executor = NeoExecutor(store, NeoClient(connection))
                    await self.executor.recover()
                self._terminating = False
                self.enrollment = Enrollment(store)
                self.notifications = NotificationService(store, self._send_notice)
                await self.notifications.initialize()
                self.queue = ExecutionQueue(
                    store,
                    execute=self.execution_bridge.execute,
                    guard=self.execution_bridge.guard,
                    config=lambda: self.config,
                    blocked=lambda: self.executor is None or self.executor.blocked,
                    reserve=reserve_terminal,
                    terminal=self._terminal_notice,
                )
                await self.queue.initialize()
                self.scheduler = CronScheduler(store, self.queue, lambda: self.config)
                await self.scheduler.initialize()
            except asyncio.CancelledError:
                if self.scheduler is not None:
                    await self.scheduler.close()
                if self.queue is not None:
                    await self.queue.close()
                if self.notifications is not None:
                    await self.notifications.close()
                if self.executor is not None:
                    await self.executor.close()
                store.stop_accepting()
                raise
            except Exception:
                if self.scheduler is not None:
                    await self.scheduler.close()
                if self.queue is not None:
                    await self.queue.close()
                if self.notifications is not None:
                    await self.notifications.close()
                if self.executor is not None:
                    await self.executor.close()
                store.stop_accepting()
                raise

    async def terminate(self) -> None:
        self._terminating = True
        self._delete_requests.clear()
        store = self.state_store
        if store is None or store.closed:
            return
        if self.enrollment is not None:
            self.enrollment.close()
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            settled = asyncio.gather(self._shutdown_task, return_exceptions=True)
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    continue
            raise

    async def _shutdown(self) -> None:
        store = self.state_store
        already_closing = store.closing
        try:
            try:
                if self.scheduler is not None:
                    await self.scheduler.close()
            finally:
                if self.queue is not None:
                    await self.queue.close()
        finally:
            try:
                if self.preparation is not None:
                    await self.preparation.close()
            finally:
                try:
                    if self.executor is not None:
                        await self.executor.close()
                finally:
                    if (
                        self.queue is not None
                        and not self.queue.closed
                        or self.executor is not None
                        and not self.executor.quiescent
                        or self.scheduler is not None
                        and not self.scheduler.closed
                    ):
                        if self.notifications is not None:
                            await self.notifications.close()
                        raise RuntimeError("execution workers have not stopped")
                    try:
                        if store.has_pending and not already_closing:
                            await store.retry_pending()
                        if self.queue is not None and store.ready:
                            await self.queue.reconcile_shutdown()
                    finally:
                        if self.notifications is not None:
                            await self.notifications.close(drain=store.ready)
                        store.stop_accepting()
                        if store.has_pending and not already_closing:
                            raise CommitUnconfirmedError(
                                "shutdown state commit is unconfirmed"
                            )
                        slot = self._state_slot()
                        async with slot["lock"]:
                            await store.close()
