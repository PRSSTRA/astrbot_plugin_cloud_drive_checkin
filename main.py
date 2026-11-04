"""AstrBot 原生命令入口及短时私聊问答。"""
import asyncio
import contextlib
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.event import MessageChain
from astrbot.api.star import Context
from astrbot.api.star import Star
from astrbot.api.util import session_waiter
from astrbot.core.utils.session_waiter import SessionFilter

from .config import parse_config
from .config import PluginConfigError
from .config import validate_cron
from .credentials import CredentialCipher
from .credentials import CredentialError
from .credentials import parse_values
from .credentials import validate_values
from .engine import CheckinEngine
from .engine import EngineError
from .executors.client import NeoClient
from .executors.client import resolve_connection
from .executors.neo import NeoExecutor
from .permissions import authorize
from .permissions import binding_key
from .permissions import get_private_identity
from .permissions import owner_key
from .permissions import raw_private_text
from .state import InvalidStateError
from .storage import StateStore
from .storage import StateUnavailableError

HELP = ('云盘签到：显示帮助\n'
        '云盘签到 列表：可用脚本、本人配置与最新状态\n'
        '云盘签到 设置：脚本参数、个人定时、自动开关、删除配置\n'
        '云盘签到 运行：选择本人脚本入队\n'
        '云盘签到 详情：选择本人脚本查看最新进程与清理通知状态\n'
        '云盘签到 日志：选择本人脚本查看最新已结束脱敏日志')
_OPERATIONS = {'列表', '设置', '运行', '详情', '日志'}
_ACTIVE = {
    'queued', 'downloading', 'creating', 'installing', 'running', 'cleaning'
}
_REGISTRY = '_cloud_drive_checkin_writer'


class _NewEntry(Exception):
    """让新入口接管当前问答，不把命令当作参数。"""

    def __init__(self, event):
        self.event = event


def log_blocks(log, truncated):
    """分块展示日志，仅在末块附加截断提示。"""
    suffix = '\n日志已截断。' if truncated else ''
    text = log or '本次没有日志输出。'
    parts = [text[index:index + 1400] for index in range(0, len(text), 1400)]
    return [
        f'日志 {index + 1}/{len(parts)}\n{part}' +
        (suffix if index == len(parts) - 1 else '')
        for index, part in enumerate(parts)
    ]


class CloudDriveCheckin(Star):
    """管理命令会话，并在热重载时串行交接持久化 writer。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        parse_config(config)
        self.state_store = None
        self.engine = None
        self._terminating = False
        self._sessions = {}
        self._entry_lock = asyncio.Lock()
        self._shutdown_task = None
        self._client = None

    def _slot(self):
        # 注册槽存于共用 Context，避免新旧插件实例同时写入。
        slot = getattr(self.context, _REGISTRY, None)
        if slot is None:
            slot = {'lock': asyncio.Lock(), 'owner': None, 'loaded': False}
            setattr(self.context, _REGISTRY, slot)
        return slot

    def _config(self):
        return parse_config(self.config)

    def _admin(self, qq):
        return str(qq) in {
            str(value)
            for value in self.context.get_config().get('admins_id', [])
        }

    def _platform_ready(self, platform_id):
        platform = self.context.get_platform_inst(platform_id)
        return platform is not None and platform.meta().name == 'aiocqhttp'

    async def initialize(self):
        """等待旧实例确认关闭，再恢复状态并启动后台任务。"""
        slot = self._slot()
        async with slot['lock']:
            previous = slot['owner']
            if previous is not None and previous is not self:
                await previous.terminate()
                # 未确认的旧写入不能靠新实例重读或覆盖来消除歧义。
                if previous.state_store and not previous.state_store.closed:
                    raise StateUnavailableError(
                        'previous writer has unconfirmed state')
            slot['owner'] = self
            self.state_store = StateStore(self)
            await self.state_store.initialize()
            try:
                cipher = CredentialCipher.from_environment()
            except CredentialError:
                cipher = None
            connection = resolve_connection(self.config,
                                            self.context.get_config())
            executor = None
            if connection is not None:
                self._client = NeoClient(connection)
                executor = NeoExecutor(self.state_store, self._client)
            self.engine = CheckinEngine(
                self.state_store,
                cipher,
                self._config,
                executor,
                send=self._notice,
                platform_ready=self._platform_ready,
                admin=self._admin,
            )
            await self.engine.start()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """在平台加载完成后唤醒此前等待连接的任务。"""
        self._slot()['loaded'] = True
        if self.engine:
            self.engine.wake()

    async def _send(self, event, text):
        result = event.plain_result(text)
        result.use_t2i(False).use_markdown(False)
        await asyncio.wait_for(event.send(result), 15)

    async def _notice(self, origin, text):
        """返回平台的提交结果，不将其视为用户已收到消息。"""
        platform, marker, qq = origin.rpartition(':FriendMessage:')
        if not marker or not qq.isascii() or not qq.isdecimal(
        ) or not self._platform_ready(platform):
            return False
        return await self.context.send_message(
            origin,
            MessageChain().message(text).use_t2i(False).use_markdown(False))

    def _access(self, event):
        identity = get_private_identity(event)
        if identity is None or not authorize(identity,
                                             astrbot_admin=event.is_admin(),
                                             config=self._config()):
            raise EngineError('permission_revoked')
        if self._terminating or self.engine is None or self.state_store is None:
            raise StateUnavailableError('state unavailable')
        self.state_store.snapshot()
        return identity

    @staticmethod
    def _operation(event):
        words = event.get_message_str().split()
        return words[1] if len(words) == 2 and words[0] == '云盘签到' and words[
            1] in _OPERATIONS else None

    @staticmethod
    def _is_entry(event):
        # 唤醒预处理和原生命令过滤先于 waiter，需识别被其接收的新入口。
        words = event.get_message_str().split()
        return bool(
            getattr(event, 'is_at_or_wake_command', False) and words and
            words[0] == '云盘签到')

    async def _cancel_session(self, key):
        task = self._sessions.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @filter.command('云盘签到')
    async def cloud_drive_checkin(self, event: AstrMessageEvent):
        """云盘签到：列表、设置、运行、详情、日志。"""
        event.should_call_llm(False)
        event.stop_event()
        # 入口只负责接管会话，耗时问答在独立任务中运行。
        try:
            identity = self._access(event)
            key = owner_key(identity)
            async with self._entry_lock:
                # 先等旧 waiter 注销，再创建新会话，避免同一回复被竞争接收。
                await self._cancel_session(key)
                task = asyncio.create_task(self._flow(event, identity),
                                           name='checkin-question')
                self._sessions[key] = task
        except (EngineError, PluginConfigError, StateUnavailableError):
            await self._send(event, '仅允许已获授权的 QQ 好友私聊；插件与存储必须已就绪。')

    async def _flow(self, event, identity):
        key = owner_key(identity)
        try:
            while not self._terminating:
                operation = self._operation(event)
                try:
                    self._access(event)
                    await self._dispatch(event, identity, operation)
                    return
                except _NewEntry as entry:
                    event = entry.event
                except TimeoutError:
                    name = operation or '设置'
                    await self._send(
                        event,
                        f'本次{name}已超时结束，请发送“云盘签到 {name}”重新进入{name}后再发送参数。'
                        '超时后裸发文字可能进入普通聊天；QQ及框架入站记录不由插件控制。')
                    return
                except CredentialError:
                    await self._send(event,
                                     '凭据主密钥或字段格式不可用，未保存本次参数；请联系管理员或重新设置。')
                    return
                except (EngineError, PluginConfigError):
                    await self._send(
                        event,
                        '权限、配置或任务状态已变化，未完成操作。请重新进入；活动任务、清理或通知未结算时不能删除或覆盖。')
                    return
                except (InvalidStateError, StateUnavailableError):
                    await self._send(
                        event, '状态容量不足或写入未确认，本次操作不保证已保存；请管理员恢复存储并原生重载后查询。')
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # 输入和传输异常可能含凭据，不拼入框架日志。
            with contextlib.suppress(Exception):
                await self._send(event, '操作未完成，请检查插件加载、存储与消息连接。')
        finally:
            # 旧会话退出时不能移除已替换它的新会话。
            if self._sessions.get(key) is asyncio.current_task():
                self._sessions.pop(key, None)

    async def _ask(self, event, identity, operation, prompt):
        """先注册隔离的私聊 waiter，再提示输入；退出时始终回收。"""
        ready = asyncio.Event()
        answer = []

        class PrivateFilter(SessionFilter):
            """按平台和用户隔离 waiter，避免跨私聊接收参数。"""

            def filter(inner, candidate):
                ready.set()
                found = get_private_identity(candidate)
                return 'checkin:' + owner_key(
                    found) if found is not None else 'checkin:invalid'

        @session_waiter(timeout=60, record_history_chains=False)
        async def receive(controller, incoming):
            if get_private_identity(incoming) != identity:
                return
            incoming.should_call_llm(False)
            incoming.stop_event()
            answer.append((raw_private_text(incoming), incoming))
            controller.stop()

        waiting = asyncio.create_task(
            receive(event, session_filter=PrivateFilter()))
        try:
            # 原生注册会调用过滤器；收到信号后再提示，避免快速回复丢失。
            await ready.wait()
            if waiting.done():
                await waiting
            await self._send(event, prompt)
            await waiting
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        text, incoming = answer[0]
        if self._is_entry(incoming):
            raise _NewEntry(incoming)
        # 等待期间授权或状态可能变化，收到回复后重新检查。
        self._access(incoming)
        return text, incoming

    async def _choose(self, event, identity, operation, choices):
        if not choices:
            await self._send(event, '没有可选的本人脚本或配置；请先设置参数，可执行项还需管理员启用。')
            return None
        menu = '\n'.join(
            f'{index + 1}. {item.name}' for index, item in enumerate(choices))
        while True:
            text, _incoming = await self._ask(event, identity, operation,
                                              f'{menu}\n请在60秒内回复编号。')
            if text is not None and text.isascii() and text.isdecimal(
            ) and 1 <= int(text) <= len(choices):
                return choices[int(text) - 1]

    def _owned(self, identity):
        from types import SimpleNamespace
        owner = owner_key(identity)
        snapshot = self.state_store.snapshot()
        combined = {}
        for collection in ('bindings', 'runs'):
            for value in snapshot[collection].values():
                if value['owner'] == owner:
                    combined[value['script_id']] = SimpleNamespace(
                        id=value['script_id'], name=value['script_name'])
        return list(combined.values())

    def _script_guard(self, event, script):
        self._access(event)
        config = self._config()
        if not config.enabled or not any(
                item.id == script.id and item.enabled and
                item.fingerprint == script.fingerprint
                for item in config.scripts):
            raise EngineError('config_changed')

    async def _dispatch(self, event, identity, operation):
        if operation is None:
            await self._send(event, HELP)
            return
        if operation == '列表':
            snapshot = self.state_store.snapshot()
            lines = []
            config = self._config()
            for script in config.scripts:
                if not script.enabled:
                    continue
                key = binding_key(identity, script.id)
                binding = snapshot['bindings'].get(key)
                status = '已配置' if binding and binding[
                    'fields_hash'] == script.fields_hash else '待配置'
                run = snapshot['runs'].get(key)
                lines.append(
                    f'{script.name}｜{status}｜{run["phase"] if run else "未运行"}')
            await self._send(event, '\n'.join(lines) if lines else '暂无可用脚本。')
            return
        if operation == '设置':
            await self._settings(event, identity)
            return
        choices = self._owned(identity)
        if operation == '运行':
            snapshot = self.state_store.snapshot()
            choices = [
                script for script in self._config().scripts
                if script.enabled and (binding := snapshot['bindings'].get(
                    binding_key(identity, script.id))) and
                binding['fields_hash'] == script.fields_hash
            ]
        selected = await self._choose(event, identity, operation, choices)
        if selected is None:
            return
        if operation == '运行':
            self._script_guard(event, selected)
            run = await self.engine.enqueue(identity, event.unified_msg_origin,
                                            selected)
            await self._send(
                event, f'{selected.name}：{run["phase"]}；已确认入队或返回同一活动任务。'
                '只报告进程结果，不核实业务签到。')
            return
        run = self.state_store.snapshot()['runs'].get(
            binding_key(identity, selected.id))
        if run is None:
            await self._send(event, '本人此脚本尚无最新执行记录。')
        elif operation == '详情':
            timezone = ZoneInfo(self._config().timezone)

            def time_text(value):
                return datetime.fromtimestamp(value, timezone).isoformat(
                    timespec='seconds') if value else '尚无'

            await self._send(
                event, f'{run["script_name"]}\n阶段：{run["phase"]}'
                f'\n开始：{time_text(run.get("started_at"))}'
                f'\n结束：{time_text(run.get("finished_at"))}'
                f'\n耗时：{run.get("duration_ms", 0)}ms'
                f'\n退出码：{run.get("exit_code")}'
                f'\n异常：{run.get("error_code") or "无"}'
                f'\n清理：{run["cleanup_status"]}'
                f'\n通知：{run["notification"]["status"]}'
                '\n退出0只代表进程正常结束，业务结果未核实。')
        elif run['phase'] in _ACTIVE:
            await self._send(event, '最新任务尚未结束，日志尚未就绪；不回退到旧日志。')
        else:
            for block in log_blocks(run.get('log', ''),
                                    run.get('log_truncated', False)):
                await self._send(event, block)

    async def _settings(self, event, identity):
        while True:
            text, _ = await self._ask(
                event, identity, '设置',
                '1. 脚本参数\n2. 个人定时\n3. 自动开关\n4. 删除配置\n请在60秒内回复编号。')
            if text in {'1', '2', '3', '4'}:
                break
        if text == '1':
            choices = [
                script for script in self._config().scripts if script.enabled
            ]
            script = await self._choose(event, identity, '设置', choices)
            if script is None:
                return
            self._script_guard(event, script)
            values = {}
            while script.env_names:
                prompt = (f'{script.name}\n字段顺序：{"、".join(script.env_names)}\n'
                          '直接回复全部秘密值，用#分隔，例如AAA#BBB#123；\\#表示#，\\\\表示反斜杠。'
                          '\n保留空白与换行。格式正确立即加密保存，不试跑；按个人或全局计划参与未来定时，不立即执行。'
                          '\n每问等待60秒；超时后裸发凭据可能进入普通聊天。QQ及框架入站记录不由插件控制。')
                raw, _ = await self._ask(event, identity, '设置', prompt)
                self._script_guard(event, script)
                try:
                    if raw is None:
                        raise CredentialError('plain text required')
                    parsed = parse_values(raw, len(script.env_names))
                    values = validate_values(
                        script, dict(zip(script.env_names, parsed)))
                    break
                except CredentialError:
                    await self._send(
                        event, '仅接收纯文本；字段数量、转义或大小格式错误，旧值未改变。请重新输入，不要发送图片或引用。')
            await self.engine.save_binding(
                identity,
                event.unified_msg_origin,
                script,
                values,
                guard=lambda: self._script_guard(event, script))
            await self._send(
                event, f'{script.name}：{len(script.env_names)}个字段已加密保存。\n' +
                self.engine.describe_plan(identity))
        elif text == '2':
            while True:
                raw, _ = await self._ask(
                    event, identity, '设置',
                    '请输入五段Cron；回复“默认”恢复全局定时。只影响本人全部脚本的未来计划。')
                try:
                    cron = None if raw == '默认' else validate_cron(
                        raw,
                        self._config().timezone)
                    break
                except (PluginConfigError, TypeError):
                    await self._send(event, 'Cron格式无效或永远不触发，请重新输入。')
            await self.engine.set_preferences(identity, cron=cron)
            await self._send(event,
                             '个人定时已保存。\n' + self.engine.describe_plan(identity))
        elif text == '3':
            while True:
                raw, _ = await self._ask(
                    event, identity, '设置',
                    '回复“开”或“关”设置本人自动签到；关闭只取消未开始的纯自动请求，不停止远端任务。')
                if raw in {'开', '关'}:
                    break
            await self.engine.set_preferences(identity, auto_enabled=raw == '开')
            await self._send(event,
                             '自动开关已保存。\n' + self.engine.describe_plan(identity))
        else:
            script = await self._choose(event, identity, '设置',
                                        self._owned(identity))
            if script is None:
                return
            raw, _ = await self._ask(
                event, identity, '设置',
                f'将删除本人{script.name}参数及唯一最新结果/日志，保留个人定时和自动设置。\n'
                '回复“确认”删除；其它文字结束，不删除。'
                '不会停止远端任务，也不能清除QQ/框架/备份记录。')
            if raw != '确认':
                await self._send(event, '本次未删除。')
                return
            await self.engine.delete_binding(identity, script.id)
            await self._send(event, '本人该脚本参数及唯一最新结果/日志已删除。')

    async def terminate(self):
        """复用关闭任务，避免调用方取消打断 writer 交接。"""
        if self._shutdown_task is None or (
                self._shutdown_task.done() and
            (self._shutdown_task.cancelled() or
             self._shutdown_task.exception() is not None)):
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self):
        # 先停止问答和后台写入，最后确认存储关闭，供下一实例接管。
        self._terminating = True
        for key in tuple(self._sessions):
            await self._cancel_session(key)
        if self.engine is not None:
            await self.engine.close()
        elif self._client is not None:
            await self._client.close()
        if self.state_store is not None:
            await self.state_store.close()
