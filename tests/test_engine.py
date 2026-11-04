"""验证引擎的共享准入、生命周期、调度与通知契约。"""

import asyncio
from dataclasses import replace
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if '_checkin_tests' not in sys.modules:
    package = types.ModuleType('_checkin_tests')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
config_module = importlib.import_module('_checkin_tests.config')
parse_config = config_module.parse_config
CredentialCipher = importlib.import_module(
    '_checkin_tests.credentials').CredentialCipher
engine_module = importlib.import_module('_checkin_tests.engine')
CheckinEngine = engine_module.CheckinEngine
EngineError = engine_module.EngineError
permissions = importlib.import_module('_checkin_tests.permissions')
Identity = permissions.Identity
binding_key = permissions.binding_key
owner_key = permissions.owner_key
finish_run = importlib.import_module('_checkin_tests.state').finish_run
StateStore = importlib.import_module('_checkin_tests.storage').StateStore
from test_storage import KV


class EngineTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.config = parse_config({
            'enabled':
                True,
            'whitelist_qqs': ['123'],
            'cron':
                '* * * * *',
            'scripts': [{
                'id': 'demo',
                'name': 'Demo',
                'enabled': True,
                'url': 'https://example.com/check.py',
                'runtime': 'python',
                'env_names': ['TOKEN'],
                'dependencies': []
            }]
        })
        self.identity = Identity('qq', '123')
        self.script = self.config.scripts[0]
        self.kv = KV()
        self.store = StateStore(self.kv)
        await self.store.initialize()
        self.messages = []
        self.send_result = True

        async def send(origin, text):
            self.messages.append((origin, text))
            if isinstance(self.send_result, BaseException):
                raise self.send_result
            return self.send_result

        self.engine = CheckinEngine(self.store,
                                    CredentialCipher(b'k' * 32),
                                    lambda: self.config,
                                    types.SimpleNamespace(blocked=False),
                                    send=send,
                                    platform_ready=lambda _: True,
                                    admin=lambda _: False)
        with patch('time.time', return_value=100.0):
            await self.engine.save_binding(self.identity,
                                           'origin-123',
                                           self.script, {'TOKEN': 'secret'},
                                           guard=lambda: None)

    async def test_reconfigure_and_delete_preserve_preferences_and_owner(self):
        await self.engine.set_preferences(self.identity,
                                          cron='0 9 * * *',
                                          auto_enabled=False)
        await self.engine.save_binding(self.identity,
                                       'new-origin',
                                       self.script, {'TOKEN': 'new'},
                                       guard=lambda: None)
        u = self.store.snapshot()['users'][owner_key(self.identity)]
        self.assertFalse(u['auto_enabled'])
        self.assertEqual(u['cron'], '0 9 * * *')
        self.config = replace(self.config, enabled=False)
        await self.engine.delete_binding(self.identity, 'demo')
        self.assertEqual(self.store.snapshot()['bindings'], {})
        self.assertIn(owner_key(self.identity), self.store.snapshot()['users'])

    async def test_dedupe_manual_joins_auto_and_settings_keep_manual(self):
        automatic = await self.engine.enqueue(self.identity,
                                              'scheduled-origin',
                                              self.script,
                                              manual=False)
        manual = await self.engine.enqueue(self.identity, 'manual-origin',
                                           self.script)
        self.assertEqual(automatic['run_id'], manual['run_id'])
        self.assertTrue(manual['manual_requested'])
        self.assertEqual(manual['origin'], 'manual-origin')
        await self.engine.set_preferences(self.identity, auto_enabled=False)
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(self.identity,
                                                      'demo')]['phase'],
            'queued')

    async def test_pure_automatic_cancel_and_unsettled_delete_rejected(self):
        await self.engine.enqueue(self.identity, 'o', self.script, manual=False)
        with self.assertRaises(EngineError):
            await self.engine.delete_binding(self.identity, 'demo')
        await self.engine.set_preferences(self.identity, auto_enabled=False)
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(self.identity,
                                                      'demo')]['phase'],
            'cancelled')
        await self.engine.delete_binding(self.identity, 'demo')

    async def test_terminal_pending_obligation_cannot_be_replaced(self):
        await self.engine.enqueue(self.identity, 'o', self.script)

        def complete(state):
            record = state['runs'][binding_key(self.identity, 'demo')]
            finish_run(record, phase='completed', now=2)

        await self.store.mutate(complete)
        with self.assertRaises(EngineError):
            await self.engine.enqueue(self.identity, 'o', self.script)
        with self.assertRaises(EngineError):
            await self.engine.delete_binding(self.identity, 'demo')
        await self.engine._notify_once(now=3)
        newer = await self.engine.enqueue(self.identity, 'o', self.script)
        self.assertEqual(newer['phase'], 'queued')
        self.assertEqual(newer['log'], '')

    async def test_live_guard_rejects_binding_generation_and_permission(self):
        record = await self.engine.enqueue(self.identity, 'o', self.script)
        await self.engine.save_binding(self.identity,
                                       'o',
                                       self.script, {'TOKEN': 'changed'},
                                       guard=lambda: None)
        with self.assertRaises(EngineError) as error:
            self.engine._guard(self.store.snapshot(), record, self.script)
        self.assertEqual(error.exception.code, 'config_changed')
        self.config = replace(self.config, whitelist_qqs=frozenset())
        with self.assertRaises(EngineError) as error:
            self.engine._guard(self.store.snapshot(), record, self.script)
        self.assertEqual(error.exception.code, 'permission_revoked')

    async def test_false_retries_only_notifications_and_exception_is_unknown(
            self):
        await self.engine.enqueue(self.identity, 'o', self.script)
        await self.store.mutate(lambda state: finish_run(
            state['runs'][binding_key(self.identity, 'demo')],
            phase='failed',
            error_code='script_error',
            now=2))
        self.send_result = False
        for now in (3, 33, 63):
            await self.engine._notify_once(now=now)
        record = self.store.snapshot()['runs'][binding_key(
            self.identity, 'demo')]
        self.assertEqual(record['notification']['status'], 'failed')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(record['phase'], 'failed')
        await self.engine.enqueue(self.identity, 'o', self.script)
        await self.store.mutate(lambda state: finish_run(
            state['runs'][binding_key(self.identity, 'demo')],
            phase='failed',
            error_code='script_error',
            now=2))
        self.send_result = RuntimeError('do not expose')
        await self.engine._notify_once(now=100)
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(
                self.identity, 'demo')]['notification']['status'], 'unknown')
        await self.engine._notify_once(now=200)
        self.assertEqual(len(self.messages), 4)

    async def test_restart_cancels_queue_and_marks_sending_unknown_no_replay(
            self):
        await self.engine.enqueue(self.identity, 'o', self.script)
        await self.engine._recover_state()
        record = self.store.snapshot()['runs'][binding_key(
            self.identity, 'demo')]
        self.assertEqual(record['phase'], 'cancelled')
        await self.store.mutate(lambda state: state['runs'][binding_key(
            self.identity, 'demo')]['notification'].update(status='sending'))
        await self.engine._recover_state()
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(
                self.identity, 'demo')]['notification']['status'], 'unknown')

    async def test_future_only_cron_and_override_no_catchup(self):
        with patch('time.time', return_value=120.0):
            await self.engine._schedule_once(now=120)
            self.assertEqual(self.store.snapshot()['runs'], {})
            await self.engine._schedule_once(now=179)
            self.assertEqual(self.store.snapshot()['runs'], {})
            await self.engine._schedule_once(now=180)
            self.assertEqual(len(self.store.snapshot()['runs']), 1)
            run_id = next(iter(
                self.store.snapshot()['runs'].values()))['run_id']
            await self.engine._schedule_once(now=180)
            await self.engine._schedule_once(now=100)
            self.assertEqual(
                next(iter(self.store.snapshot()['runs'].values()))['run_id'],
                run_id)
        await self.engine.set_preferences(self.identity, cron='0 9 * * *')
        self.assertEqual(
            self.store.snapshot()['users'][owner_key(self.identity)]['cron'],
            '0 9 * * *')
        await self.engine.set_preferences(self.identity, cron=None)
        self.assertIsNone(self.store.snapshot()['users'][owner_key(
            self.identity)]['cron'])

    async def test_capacity_schedule_error_is_durable_and_not_overwritten(self):
        await self.engine._schedule_once(now=120)
        with patch('{}.MAX_ACTIVE_RUNS'.format(CheckinEngine.__module__), 0):
            await self.engine._schedule_once(now=180)
            error = self.store.snapshot()['users'][owner_key(
                self.identity)]['last_schedule_error']
            self.assertEqual(error['code'], 'capacity')
            await self.engine._schedule_once(now=240)
            self.assertEqual(
                self.store.snapshot()['users'][owner_key(
                    self.identity)]['last_schedule_error'], error)
        await self.engine._notify_once(now=300)
        self.assertEqual(
            self.store.snapshot()['users'][owner_key(self.identity)]
            ['last_schedule_error']['notification']['status'], 'submitted')

    async def test_disabled_and_missing_executor_refuse_admission(self):
        self.config = replace(self.config, enabled=False)
        with self.assertRaises(EngineError):
            await self.engine.enqueue(self.identity, 'o', self.script)
        with self.assertRaises(EngineError):
            await self.engine.save_binding(self.identity,
                                           'o',
                                           self.script, {'TOKEN': 'x'},
                                           guard=lambda: None)
        self.config = replace(self.config, enabled=True)
        self.engine.executor = None
        with self.assertRaises(EngineError) as error:
            await self.engine.enqueue(self.identity, 'o', self.script)
        self.assertEqual(error.exception.code, 'runtime_unavailable')
        self.engine.executor = types.SimpleNamespace(blocked=True)
        await self.engine._schedule_once(now=120)
        await self.engine._schedule_once(now=180)
        self.assertEqual(self.store.snapshot()['runs'], {})
        self.assertEqual(
            self.store.snapshot()['users'][owner_key(
                self.identity)]['schedule_watermark'], 180)

    async def test_stale_failure_cannot_touch_later_generation(self):
        old = await self.engine.enqueue(self.identity, 'o', self.script)
        await self.store.mutate(lambda state: state['runs'][binding_key(
            self.identity, 'demo')].update(run_id='new-generation'))
        await self.engine._fail(old, 'download_error')
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(self.identity,
                                                      'demo')]['phase'],
            'queued')

    async def test_worker_rechecks_after_download_and_never_runs_revoked(self):
        calls = []

        async def execute(*args, **kwargs):
            calls.append(args)

        async def download(url):
            self.config = replace(self.config, whitelist_qqs=frozenset())
            return b'print(1)'

        self.engine.executor = types.SimpleNamespace(blocked=False,
                                                     execute=execute)
        self.engine._download = download
        await self.engine.enqueue(self.identity, 'o', self.script)
        await self.engine._work_once()
        self.assertEqual(calls, [])
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(self.identity,
                                                      'demo')]['error_code'],
            'permission_revoked')

    async def test_awaited_write_freezes_worker_and_claim_rechecks_permission(
            self):
        calls = []

        async def execute(*args, **kwargs):
            calls.append(args)

        self.engine.executor = types.SimpleNamespace(blocked=False,
                                                     execute=execute)
        self.kv.error = OSError('ambiguous')
        with self.assertRaises(Exception):
            await self.engine.enqueue(self.identity, 'o', self.script)
        self.assertFalse(await self.engine._work_once())
        self.assertEqual(calls, [])
        self.kv.error = None
        await self.store.retry_pending()
        self.config = replace(self.config, whitelist_qqs=frozenset())
        await self.engine._work_once()
        self.assertEqual(calls, [])
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(self.identity,
                                                      'demo')]['error_code'],
            'permission_revoked')

    async def test_future_schedule_after_late_wakeup_does_not_replay(self):
        await self.engine._schedule_once(now=120)
        await self.engine._schedule_once(now=301)
        self.assertEqual(self.store.snapshot()['runs'], {})
        await self.engine._schedule_once(now=360)
        self.assertEqual(len(self.store.snapshot()['runs']), 1)

    async def test_worker_downloads_latest_and_fifo_survives_clock_rollback(
            self):
        seen = []
        contents = iter((b'old source', b'new source'))

        async def download(url):
            return next(contents)

        async def execute(record, script, source, **kwargs):
            seen.append(source)
            kwargs['guard'](self.store.snapshot())

            def complete(state):
                current = state['runs'][binding_key(self.identity, 'demo')]
                current['exit_code'] = 0
                finish_run(current, phase='completed')

            await self.store.mutate(complete)

        self.engine.executor = types.SimpleNamespace(blocked=False,
                                                     execute=execute)
        self.engine._download = download
        await self.engine.enqueue(self.identity, 'o', self.script)
        await self.engine._work_once()
        await self.engine._notify_once()
        await self.engine.enqueue(self.identity, 'o', self.script)
        await self.engine._work_once()
        self.assertEqual(seen, [b'old source', b'new source'])

    async def test_claim_permission_change_during_kv_await_prevents_download(
            self):
        downloaded = []

        async def download(url):
            downloaded.append(url)
            return b'print(1)'

        await self.engine.enqueue(self.identity, 'o', self.script)
        original = self.kv.put_kv_data

        async def put(key, value):
            await original(key, value)
            if any(run['phase'] == 'downloading'
                   for run in value['runs'].values()):
                self.config = replace(self.config, whitelist_qqs=frozenset())

        self.kv.put_kv_data = put
        self.engine._download = download
        await self.engine._work_once()
        self.assertEqual(downloaded, [])
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(self.identity,
                                                      'demo')]['error_code'],
            'permission_revoked')

    async def test_known_result_is_not_changed_by_late_shutdown(self):
        record = await self.engine.enqueue(self.identity, 'o', self.script)

        def complete(state):
            current = state['runs'][binding_key(self.identity, 'demo')]
            current['exit_code'] = 0
            finish_run(current, phase='completed')

        await self.store.mutate(complete)
        await self.engine._fail(record, 'shutdown', phase='interrupted')
        current = self.store.snapshot()['runs'][binding_key(
            self.identity, 'demo')]
        self.assertEqual(current['phase'], 'completed')
        self.assertIsNone(current['error_code'])

    async def test_sending_cancel_is_unknown_and_never_retried(self):
        record = await self.engine.enqueue(self.identity, 'o', self.script)
        await self.store.mutate(lambda state: finish_run(
            state['runs'][binding_key(self.identity, 'demo')],
            phase='failed',
            error_code='script_error'))
        entered = asyncio.Event()

        async def send(origin, text):
            entered.set()
            await asyncio.Event().wait()

        self.engine._send = send
        task = asyncio.create_task(self.engine._notify_once())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            self.store.snapshot()['runs'][binding_key(
                self.identity, 'demo')]['notification']['status'], 'unknown')

    async def test_owner_delete_never_removes_another_identity(self):
        foreign = Identity('other-platform', '123')
        await self.engine.delete_binding(foreign, 'demo')
        self.assertIn(binding_key(self.identity, 'demo'),
                      self.store.snapshot()['bindings'])

    async def test_fifo_queued_times_do_not_reverse_after_clock_rollback(self):
        with patch('time.time', return_value=200):
            first = await self.engine.enqueue(self.identity, 'o', self.script)
        other = replace(self.script, id='other')
        self.config = replace(self.config, scripts=(self.script, other))
        await self.engine.save_binding(self.identity,
                                       'o',
                                       other, {'TOKEN': 'other'},
                                       guard=lambda: None)
        with patch('time.time', return_value=100):
            second = await self.engine.enqueue(self.identity, 'o', other)
        self.assertGreater(second['queued_at'], first['queued_at'])

    async def test_binding_write_failure_keeps_last_confirmed_ciphertext(self):
        key = binding_key(self.identity, 'demo')
        previous = self.store.snapshot()['bindings'][key]['ciphertext']
        self.kv.error = OSError('ambiguous')
        with self.assertRaises(Exception):
            await self.engine.save_binding(self.identity,
                                           'new-origin',
                                           self.script, {'TOKEN': 'changed'},
                                           guard=lambda: None)
        self.assertEqual(self.store.snapshot()['bindings'][key]['ciphertext'],
                         previous)
        self.assertEqual(
            self.store.snapshot()['users'][owner_key(self.identity)]['origin'],
            'origin-123')

    async def test_deletion_refuses_terminal_unconfirmed_cleanup(self):
        record = await self.engine.enqueue(self.identity, 'o', self.script)

        def complete(state):
            current = state['runs'][binding_key(self.identity, 'demo')]
            current.update(phase='completed',
                           exit_code=0,
                           cleanup_status='pending')
            finish_run(current)
            state['cleanup_jobs'][record['run_id']] = {
                'owner': owner_key(self.identity),
                'script_id': 'demo',
                'connection': 'a' * 64,
                'sandbox_id': 's',
                'cargo_id': 'c',
                'allocation_started': True
            }

        await self.store.mutate(complete)
        with self.assertRaises(EngineError):
            await self.engine.delete_binding(self.identity, 'demo')
        self.assertEqual(self.messages, [])
        await self.engine._notify_once()
        self.assertEqual(self.messages, [])

    async def test_ambiguous_schedule_commit_freezes_watermark_and_error(self):
        await self.engine._schedule_once(now=120)
        self.kv.error = OSError('ambiguous')
        with patch('{}.MAX_ACTIVE_RUNS'.format(CheckinEngine.__module__), 0):
            with self.assertRaises(Exception):
                await self.engine._schedule_once(now=180)
        user = self.store.snapshot()['users'][owner_key(self.identity)]
        self.assertEqual(user['schedule_watermark'], 120)
        self.assertIsNone(user['last_schedule_error'])
        self.kv.error = None
        await self.store.retry_pending()
        user = self.store.snapshot()['users'][owner_key(self.identity)]
        self.assertEqual(user['schedule_watermark'], 180)
        self.assertEqual(user['last_schedule_error']['code'], 'capacity')

    async def test_global_schedule_update_does_not_override_personal_cron(self):
        with patch('time.time', return_value=100):
            await self.engine.set_preferences(self.identity, cron='0 9 * * *')
        await self.engine._schedule_once(now=120)
        personal_due = self.engine._due[owner_key(self.identity)][1]
        self.config = replace(self.config, cron='0 4 * * *')
        await self.engine._schedule_once(now=121)
        self.assertEqual(self.engine._due[owner_key(self.identity)][1],
                         personal_due)
        self.assertEqual(
            self.store.snapshot()['users'][owner_key(self.identity)]['cron'],
            '0 9 * * *')

    async def test_timezone_change_resets_future_schedule_without_replay(self):
        self.config = replace(self.config, cron='0 9 * * *')
        await self.engine._schedule_once(now=120)
        self.assertEqual(self.engine._due[owner_key(self.identity)][1], 3600)
        self.config = replace(self.config, timezone='UTC')
        await self.engine._schedule_once(now=3601)
        self.assertEqual(self.engine._due[owner_key(self.identity)][1], 32400)
        self.assertEqual(self.store.snapshot()['runs'], {})

    async def test_automatic_success_silent_manual_join_still_notifies(self):
        record = await self.engine.enqueue(self.identity,
                                           'auto-origin',
                                           self.script,
                                           manual=False)

        def complete(state):
            current = state['runs'][binding_key(self.identity, 'demo')]
            current['exit_code'] = 0
            finish_run(current, phase='completed')

        await self.store.mutate(complete)
        await self.engine._notify_once()
        self.assertEqual(self.messages, [])
        record = await self.engine.enqueue(self.identity,
                                           'auto-origin',
                                           self.script,
                                           manual=False)
        await self.engine.enqueue(self.identity, 'manual-origin', self.script)
        await self.store.mutate(complete)
        await self.engine._notify_once()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0][0], 'manual-origin')
