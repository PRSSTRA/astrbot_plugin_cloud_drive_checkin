"""持久化 FIFO、串行执行、通知结算及仅面向未来的 Cron 调度。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
import hashlib
import json
import time
import uuid

from .config import next_fire
from .config import validate_cron
from .credentials import CredentialCipher
from .credentials import validate_values
from .downloads import download_source
from .permissions import authorize
from .permissions import binding_key
from .permissions import Identity
from .permissions import owner_key
from .state import ACTIVE_PHASES
from .state import finish_run
from .state import InvalidStateError
from .state import MAX_ACTIVE_RUNS
from .state import unsettled
from .state import validate_snapshot
from .storage import StateUnavailableError

_UNSET = object()


class EngineError(ValueError):
    """携带稳定错误码的业务拒绝，不包含用户凭据。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _notification():
    return {'status': 'none', 'attempts': 0, 'next_attempt_at': 0.0}


class CheckinEngine:
    """用单 worker 执行任务，依赖存储串行提交协调三个后台循环。"""

    def __init__(self,
                 store,
                 cipher,
                 config,
                 executor,
                 *,
                 send,
                 platform_ready,
                 admin,
                 download=download_source):
        self.store = store
        self.cipher = cipher
        self._config = config
        self.executor = executor
        self._send = send
        self._platform_ready = platform_ready
        self._admin = admin
        self._download = download
        self._worker_event = asyncio.Event()
        self._notification_event = asyncio.Event()
        self._schedule_event = asyncio.Event()
        self._tasks = []
        self._closing = False
        self._due = {}

    def wake(self):
        """唤醒执行、通知和调度循环，重新评估已确认状态。"""
        self._worker_event.set()
        self._notification_event.set()
        self._schedule_event.set()

    def _allowed(self, identity, config):
        if not authorize(identity,
                         astrbot_admin=bool(self._admin(identity.qq_id)),
                         config=config):
            raise EngineError('permission_revoked')

    def _live_script(self, identity, script):
        config = self._config()
        if not config.enabled:
            raise EngineError('config_changed')
        self._allowed(identity, config)
        current = next((s for s in config.scripts if s.id == script.id), None)
        if (current is None or not current.enabled or
                current.fingerprint != script.fingerprint):
            raise EngineError('config_changed')
        return config, current

    async def save_binding(self, identity, origin, script, values, guard):
        """加密并确认保存绑定；提交锁内再次检查交互及脚本有效性。"""
        self._live_script(identity, script)
        env = validate_values(script, values)
        cipher = self.cipher or CredentialCipher.from_environment()
        ciphertext = cipher.encrypt(env,
                                    identity=identity,
                                    script_id=script.id,
                                    fields_hash=script.fields_hash)
        owner, key, now = owner_key(identity), binding_key(
            identity, script.id), time.time()

        def save(state):
            guard()
            self._live_script(identity, script)
            if owner not in state['users']:
                state['users'][owner] = {
                    'platform_id': identity.platform_id,
                    'qq_id': identity.qq_id,
                    'origin': origin,
                    'cron': None,
                    'auto_enabled': True,
                    'schedule_watermark': now,
                    'last_schedule_error': None,
                    'schedule_fingerprint': ''
                }
            else:
                state['users'][owner]['origin'] = origin
            state['bindings'][key] = {
                'owner': owner,
                'script_id': script.id,
                'script_name': script.name,
                'fields_hash': script.fields_hash,
                'ciphertext': ciphertext
            }

        try:
            # mutate 返回的是已确认快照；未确认写入不能宣称保存成功。
            result = await self.store.mutate(save)
        except InvalidStateError:
            raise EngineError('capacity') from None
        self.wake()
        return result['bindings'][key]

    def _admit(self, state, identity, origin, script, manual, now):
        """在同一提交内合并活动请求，或替换已结算的最新结果。"""
        self._live_script(identity, script)
        if self.executor is None:
            raise EngineError('runtime_unavailable')
        owner, key = owner_key(identity), binding_key(identity, script.id)
        binding = state['bindings'].get(key)
        if binding is None or binding['owner'] != owner or binding[
                'fields_hash'] != script.fields_hash:
            raise EngineError('config_changed')
        if not manual and not state['users'][owner]['auto_enabled']:
            raise EngineError('config_changed')
        previous = state['runs'].get(key)
        if previous is not None and previous['phase'] in ACTIVE_PHASES:
            # 手动请求提升同一活动任务，之后关闭自动开关也不应取消它。
            if manual:
                previous['manual_requested'] = True
                previous['origin'] = origin
            return previous
        if self.executor.blocked or state['cleanup_jobs']:
            raise EngineError('cleanup_pending')
        if previous is not None and unsettled(previous):
            raise EngineError('unsettled')
        if sum(run['phase'] in ACTIVE_PHASES
               for run in state['runs'].values()) >= MAX_ACTIVE_RUNS:
            raise EngineError('capacity')
        # 严格递增的入队时间让同一时刻的请求仍保持 FIFO 顺序。
        now = max(
            now,
            max((
                item['queued_at'] + .000001 for item in state['runs'].values()),
                default=now))
        record = {
            'owner': owner,
            'script_id': script.id,
            'script_name': script.name,
            'run_id': uuid.uuid4().hex,
            'source': 'manual' if manual else 'automatic',
            'manual_requested': manual,
            'origin': origin,
            'queued_at': now,
            'started_at': None,
            'finished_at': None,
            'phase': 'queued',
            'exit_code': None,
            'error_code': None,
            'duration_ms': 0,
            'sha256': None,
            'log': '',
            'log_truncated': False,
            'cleanup_status': 'none',
            'notification': _notification(),
            'fingerprint': script.fingerprint,
            'binding_digest': _digest(binding['ciphertext'])
        }
        state['runs'].pop(key, None)
        state['runs'][key] = record
        try:
            validate_snapshot(state, max_bytes=self.store._max_snapshot_bytes)
        except InvalidStateError:
            if previous is None:
                state['runs'].pop(key, None)
            else:
                state['runs'][key] = previous
            raise EngineError('capacity') from None
        return record

    async def enqueue(self, identity, origin, script, manual=True):
        """确认入队或合并到同一活动任务后，返回最新记录。"""
        if self._closing:
            raise EngineError('shutdown')
        key = binding_key(identity, script.id)
        try:
            await self.store.mutate(lambda state: self._admit(
                state, identity, origin, script, manual, time.time()))
        except InvalidStateError:
            raise EngineError('capacity') from None
        result = self.store.snapshot()['runs'][key]
        self.wake()
        return result

    async def delete_binding(self, identity, script_id):
        """删除绑定及最新结果，但不越过活动、清理或通知未结算状态。"""
        owner, key = owner_key(identity), binding_key(identity, script_id)

        def delete(state):
            record = state['runs'].get(key)
            if record is not None and (record['owner'] != owner or
                                       unsettled(record)):
                raise EngineError('unsettled')
            if any(job['owner'] == owner and job['script_id'] == script_id
                   for job in state['cleanup_jobs'].values()):
                raise EngineError('unsettled')
            state['bindings'].pop(key, None)
            state['runs'].pop(key, None)

        await self.store.mutate(delete)
        self.wake()

    async def set_preferences(self,
                              identity,
                              *,
                              cron=_UNSET,
                              auto_enabled=_UNSET):
        """保存个人计划，仅取消尚未开始且没有手动请求的自动任务。"""
        if cron is not _UNSET and cron is not None:
            cron = validate_cron(cron, self._config().timezone)
        if auto_enabled is not _UNSET and type(auto_enabled) is not bool:
            raise EngineError('config_changed')
        owner, now = owner_key(identity), time.time()

        def update(state):
            self._allowed(identity, self._config())
            if cron is not _UNSET and cron is not None:
                validate_cron(cron, self._config().timezone)
            if owner not in state['users'] or not any(
                    binding['owner'] == owner
                    for binding in state['bindings'].values()):
                raise EngineError('no_binding')
            user = state['users'][owner]
            if cron is not _UNSET:
                user['cron'] = cron
            if auto_enabled is not _UNSET:
                user['auto_enabled'] = auto_enabled
            # 推进水位并使缓存失效，新设置不补跑修改前的触发点。
            user['schedule_watermark'] = max(now, user['schedule_watermark'])
            user['schedule_fingerprint'] = ''
            if auto_enabled is False:
                for record in state['runs'].values():
                    if record['owner'] == owner and record[
                            'phase'] == 'queued' and not record[
                                'manual_requested']:
                        finish_run(record,
                                   phase='cancelled',
                                   error_code='config_changed',
                                   now=now)

        await self.store.mutate(update)
        self._due.pop(owner, None)
        self.wake()

    def describe_plan(self, identity):
        """说明当前生效的个人或全局未来计划。"""
        user = self.store.snapshot()['users'].get(owner_key(identity))
        config = self._config()
        if user is not None and not user['auto_enabled']:
            return '自动执行已关闭；可手动运行。'
        cron = user['cron'] if user is not None and user[
            'cron'] is not None else config.cron
        return f'参与未来定时：{cron}（{config.timezone}）；保存后不会立即执行。'

    def _guard(self, state, record, script):
        """在异步阶段之间核对任务代次、配置及凭据是否仍匹配。"""
        user = state['users'].get(record['owner'])
        if user is None:
            raise EngineError('config_changed')
        identity = Identity(user['platform_id'], user['qq_id'])
        self._live_script(identity, script)
        current = state['runs'].get(binding_key(identity, script.id))
        binding = state['bindings'].get(binding_key(identity, script.id))
        if (current is None or current['run_id'] != record['run_id'] or
                current['fingerprint'] != script.fingerprint or
                binding is None or
                binding['fields_hash'] != script.fields_hash or
                _digest(binding['ciphertext']) != record['binding_digest']):
            raise EngineError('config_changed')
        if not current['manual_requested'] and not user['auto_enabled']:
            raise EngineError('config_changed')
        if self._closing:
            raise EngineError('shutdown')

    async def start(self):
        """先恢复远端清理和本地状态，再启动三个后台循环。"""
        if self._tasks:
            return
        if not self.store.ready:
            await self.store.initialize()
        if self.executor is not None:
            await self.executor.recover()
        await self._recover_state()
        self._tasks = [
            asyncio.create_task(self._worker_loop()),
            asyncio.create_task(self._notification_loop()),
            asyncio.create_task(self._schedule_loop())
        ]
        self.wake()

    async def _recover_state(self):
        """终结遗留活动任务，不重放重启前的执行或不明通知。"""
        now = time.time()

        def recover(state):
            for record in state['runs'].values():
                if record['notification']['status'] == 'sending':
                    # 发送可能已被平台接收；未知结果不自动重发，避免重复通知。
                    record['notification']['status'] = 'unknown'
                if record['phase'] in ACTIVE_PHASES:
                    finish_run(record,
                               phase='cancelled' if record['phase'] == 'queued'
                               else 'interrupted',
                               error_code='shutdown',
                               now=now)
                elif record['cleanup_status'] != 'pending':
                    finish_run(record, now=now)
            for user in state['users'].values():
                error = user['last_schedule_error']
                if error is not None and error['notification'][
                        'status'] == 'sending':
                    error['notification']['status'] = 'unknown'

        await self.store.mutate(recover)
        self._due.clear()

    async def close(self):
        """停止后台循环并关闭执行器，再重试未确认写入、结算遗留状态。"""
        self._closing = True
        self.wake()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.executor is not None:
            await self.executor.close()
        if self.store.has_pending:
            # 只能重试同一待确认快照，不能用新状态覆盖可能已落盘的提交。
            await self.store.retry_pending()
        if self.store.ready:
            await self._recover_state()

    async def _wait(self, event, delay=None):
        try:
            if delay is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=max(.01, delay))
        except asyncio.TimeoutError:
            pass
        event.clear()

    async def _worker_loop(self):
        while not self._closing:
            try:
                did_work = await self._work_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                did_work = False
            if not did_work:
                await self._wait(self._worker_event)

    async def _fail(self, record, code, *, phase='failed'):

        def fail(state):
            current = state['runs'].get(self._record_key(state, record))
            if current is not None and current['run_id'] == record[
                    'run_id'] and current['phase'] in ACTIVE_PHASES:
                finish_run(current, phase=phase, error_code=code)

        await self.store.mutate(fail)
        self._notification_event.set()

    @staticmethod
    def _record_key(state, record):
        user = state['users'].get(record['owner'])
        return binding_key(Identity(user['platform_id'], user['qq_id']),
                           record['script_id']) if user else ''

    async def _work_once(self):
        """从已确认队列认领一项；每次外部等待后重新核对执行边界。"""
        if not await self.store.wait_ready(
        ) or self.executor is None or self.executor.blocked:
            return False
        state = self.store.snapshot()
        if state['cleanup_jobs']:
            return False
        queue = sorted((record for record in state['runs'].values()
                        if record['phase'] == 'queued'),
                       key=lambda record:
                       (record['queued_at'], record['run_id']))
        record = queue[0] if queue else None
        if record is None:
            return False
        if not self._platform_ready(
                state['users'][record['owner']]['platform_id']):
            return False
        script = next(
            (s for s in self._config().scripts if s.id == record['script_id']),
            None)
        try:
            if script is None:
                raise EngineError('config_changed')

            def claim(snapshot):
                self._guard(snapshot, record, script)
                current = snapshot['runs'][self._record_key(snapshot, record)]
                current.update(phase='downloading', started_at=time.time())

            # 先确认认领再下载；写入不明确时不开始新的外部操作。
            await self.store.mutate(claim)
            self._guard(self.store.snapshot(), record, script)
            source = await self._download(script.url)
            if not await self.store.wait_ready():
                return False
            self._guard(self.store.snapshot(), record, script)
            config = self._config()

            def credentials():
                # 延迟到执行器需要时才解密，并拒绝排队后被替换的凭据。
                snapshot = self.store.snapshot()
                self._guard(snapshot, record, script)
                user = snapshot['users'][record['owner']]
                identity = Identity(user['platform_id'], user['qq_id'])
                binding = snapshot['bindings'][binding_key(identity, script.id)]
                cipher = self.cipher or CredentialCipher.from_environment()
                return cipher.decrypt(binding['ciphertext'],
                                      identity=identity,
                                      script_id=script.id,
                                      fields_hash=script.fields_hash)

            await self.executor.execute(
                record,
                script,
                source,
                guard=lambda snapshot: self._guard(snapshot, record, script),
                credentials=credentials,
                timeout_seconds=config.timeout_seconds,
                install_timeout_seconds=config.install_timeout_seconds)
        except asyncio.CancelledError:
            if await self.store.wait_ready():
                await self._fail(record, 'shutdown', phase='interrupted')
            raise
        except EngineError as exc:
            if await self.store.wait_ready():
                await self._fail(
                    record,
                    exc.code,
                    phase='cancelled' if exc.code
                    in {'config_changed', 'permission_revoked'} else 'failed')
        except StateUnavailableError:
            return False
        except Exception:
            if await self.store.wait_ready():
                current = self.store.snapshot()['runs'].get(
                    self._record_key(state, record))
                if current is not None and current['run_id'] == record[
                        'run_id'] and current['phase'] == 'downloading':
                    await self._fail(record, 'download_error')
                else:
                    await self._fail(record,
                                     'execution_unknown',
                                     phase='interrupted')
        self._notification_event.set()
        return True

    async def _notification_loop(self):
        while not self._closing:
            try:
                delay = await self._notify_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = None
            await self._wait(self._notification_event, delay)

    async def _notify_once(self, *, now=None):
        """先确认发送意图；未获提交确认的返回值有限重试，异常不重发。"""
        if not await self.store.wait_ready():
            return None
        now = time.time() if now is None else now
        snapshot, delay = self.store.snapshot(), None
        targets = []
        for key, record in snapshot['runs'].items():
            if record['phase'] not in ACTIVE_PHASES and record[
                    'cleanup_status'] != 'pending':
                targets.append(('run', key, record['run_id'], record))
        for owner, user in snapshot['users'].items():
            if user['last_schedule_error'] is not None:
                targets.append(
                    ('schedule', owner, user['last_schedule_error']['at'],
                     user['last_schedule_error']))
        for kind, key, generation, record in targets:
            note = record['notification']
            if note['status'] != 'pending':
                continue
            owner = record['owner'] if kind == 'run' else key
            if not self._platform_ready(
                    snapshot['users'][owner]['platform_id']):
                continue
            if note['next_attempt_at'] > now:
                remaining = note['next_attempt_at'] - now
                delay = remaining if delay is None else min(delay, remaining)
                continue

            def locate(state):
                item = state['runs'].get(
                    key) if kind == 'run' else state['users'].get(
                        key, {}).get('last_schedule_error')
                if item is None or (item['run_id'] if kind == 'run' else
                                    item['at']) != generation:
                    raise EngineError('config_changed')
                return item

            def sending(state):
                current = locate(state)
                if current['notification']['status'] != 'pending':
                    raise EngineError('unsettled')
                current['notification']['status'] = 'sending'
                current['notification']['attempts'] += 1

            # sending 必须先落盘；崩溃后按 unknown 处理，而非再次发送。
            await self.store.mutate(sending)
            status = 'unknown'
            cancelled = False
            try:
                text = self._notification_text(
                    record) if kind == 'run' else '云盘签到：本次定时因容量不足未执行，请稍后手动运行。'
                submitted = await self._send(record['origin'], text)
                status = 'submitted' if submitted is True else (
                    'failed' if note['attempts'] + 1 >= 3 else 'pending')
            except asyncio.CancelledError:
                # 取消也不能推断平台未接收，仍尽可能持久化 unknown。
                cancelled = True
            except Exception:
                status = 'unknown'

            def settle(state):
                current = locate(state)
                if current['notification']['status'] == 'sending':
                    current['notification']['status'] = status
                    current['notification']['next_attempt_at'] = (
                        now + 30 if status == 'pending' else 0.0)

            if await self.store.wait_ready():
                # 外层取消不应中断结果提交，避免遗留未结算的发送状态。
                await asyncio.shield(self.store.mutate(settle))
            if cancelled:
                raise asyncio.CancelledError
            if status == 'pending':
                delay = 30 if delay is None else min(delay, 30)
        return delay

    @staticmethod
    def _notification_text(record):
        if record['phase'] == 'completed' and record[
                'exit_code'] == 0 and record['error_code'] is None:
            result = '进程正常结束，业务结果未核实'
        else:
            result = (f"阶段={record['phase']}，"
                      f"错误={record['error_code'] or 'script_error'}，"
                      f"退出码={record['exit_code']}")
        return (f"云盘签到：{record['script_name']}：{result}。"
                "可发送‘云盘签到 详情’或‘云盘签到 日志’查看。")

    async def _schedule_loop(self):
        while not self._closing:
            try:
                delay = await self._schedule_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = None
            await self._wait(self._schedule_event, delay)

    async def _schedule_once(self, *, now=None):
        """依据持久化水位推进未来触发点，入队与水位在同次提交中确认。"""
        if not await self.store.wait_ready():
            return None
        now = time.time() if now is None else now
        config = self._config()
        state = self.store.snapshot()
        delay = None
        for owner, user in state['users'].items():
            identity = Identity(user['platform_id'], user['qq_id'])
            expression = user['cron'] or config.cron
            fingerprint = hashlib.sha256(
                json.dumps([
                    expression, config.timezone, config.enabled,
                    user['auto_enabled']
                ]).encode()).hexdigest()

            def future(after):
                return next_fire(expression, config.timezone,
                                 datetime.fromtimestamp(
                                     after, timezone.utc)).timestamp()

            current = self._due.get(owner)
            if current is None or current[0] != fingerprint or user.get(
                    'schedule_fingerprint') != fingerprint:
                # 重启或计划变化均从当前时间之后计算，不补历史触发点。
                after = max(now, user['schedule_watermark'])
                due = future(after)

                def reset(snapshot):
                    target = snapshot['users'][owner]
                    target['schedule_watermark'] = max(
                        after, target['schedule_watermark'])
                    target['schedule_fingerprint'] = fingerprint

                await self.store.mutate(reset)
                self._due[owner] = (fingerprint, due)
                user = self.store.snapshot()['users'][owner]
            else:
                due = current[1]
            if due <= now:
                # 唤醒时若已错过完整 Cron 间隔则不补跑，水位同时防止回拨重入。
                trigger = future(due) > now and due > user['schedule_watermark']

                def tick(snapshot):
                    target = snapshot['users'][owner]
                    if due <= target['schedule_watermark']:
                        return
                    target['schedule_watermark'] = max(
                        now, target['schedule_watermark'])
                    # 即使禁用、无权限或容量不足，也消费本次触发，避免反复补跑。
                    if not trigger or not target[
                            'auto_enabled'] or not config.enabled:
                        return
                    try:
                        self._allowed(identity, self._config())
                    except EngineError:
                        return
                    for script in self._config().scripts:
                        key = binding_key(identity, script.id)
                        if (not script.enabled or
                                key not in snapshot['bindings']):
                            continue
                        try:
                            self._admit(snapshot, identity, target['origin'],
                                        script, False, now)
                        except EngineError as exc:
                            if exc.code == 'capacity':
                                previous = target['last_schedule_error']
                                if previous is None or previous['notification'][
                                        'status'] not in {'pending', 'sending'}:
                                    target['last_schedule_error'] = {
                                        'code': 'capacity',
                                        'at': now,
                                        'origin': target['origin'],
                                        'notification': {
                                            'status': 'pending',
                                            'attempts': 0,
                                            'next_attempt_at': 0.0
                                        }
                                    }

                await self.store.mutate(tick)
                due = future(max(now, user['schedule_watermark']))
                self._due[owner] = (fingerprint, due)
                self._worker_event.set()
                self._notification_event.set()
            remaining = due - now
            delay = remaining if delay is None else min(delay, remaining)
        return delay
