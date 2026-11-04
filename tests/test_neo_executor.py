"""验证两阶段执行、凭据边界与资源清理契约。"""

import copy
import importlib
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace
import unittest

import httpx
from neo_support import FakeBay

from executors.client import NeoClient
from executors.client import NeoConnection
from executors.neo import ExecutionBlocked
from executors.neo import NeoExecutor


class MemoryStore:

    def __init__(self, run):
        self.value = {'runs': {'binding': run}, 'cleanup_jobs': {}}
        self.ready = True
        self.writes = []

    def snapshot(self):
        return copy.deepcopy(self.value)

    async def mutate(self, change):
        value = self.snapshot()
        result = change(value)
        self.value = value
        self.writes.append(self.snapshot())
        return result

    async def wait_ready(self):
        return self.ready


def new_run():
    return {
        'owner': 'owner',
        'script_id': 'demo',
        'script_name': 'Demo',
        'run_id': 'a' * 32,
        'source': 'manual',
        'manual_requested': True,
        'origin': 'origin',
        'queued_at': 1.0,
        'started_at': None,
        'finished_at': None,
        'phase': 'downloading',
        'exit_code': None,
        'error_code': None,
        'duration_ms': 0,
        'sha256': None,
        'log': '',
        'log_truncated': False,
        'cleanup_status': 'none',
        'notification': {
            'status': 'none',
            'attempts': 0,
            'next_attempt_at': 0.0
        },
        'fingerprint': 'f' * 64,
        'binding_digest': 'b' * 64
    }


class ExecutorTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.bay = FakeBay()
        self.addAsyncCleanup(self.bay.close)
        self.store = MemoryStore(new_run())
        self.client = NeoClient(NeoConnection('https://neo.example',
                                              self.bay.token),
                                transport=httpx.MockTransport(self.bay))
        self.executor = NeoExecutor(self.store, self.client)
        self.addAsyncCleanup(self.executor.close)
        await self.executor.recover()
        self.script = SimpleNamespace(runtime='python',
                                      url='https://example.com/a.py',
                                      dependencies=())
        self.source = b'import os; print("run-ok",os.environ["TOKEN"])'
        self.credential_calls = 0

    def credentials(self):
        self.credential_calls += 1
        self.assertEqual(self.bay.install_calls, 1)
        return {'TOKEN': 'fake-user-secret'}

    async def execute(self, guard=lambda snapshot: None):
        return await self.executor.execute(
            self.store.snapshot()['runs']['binding'],
            self.script,
            self.source,
            guard=guard,
            credentials=self.credentials,
            timeout_seconds=2,
            install_timeout_seconds=60)

    async def test_two_stages_single_latest_run_and_safe_log(self):
        result = await self.execute()
        self.assertEqual(result['phase'], 'completed', result)
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(self.credential_calls, 1)
        self.assertEqual(list(self.store.value['runs']), ['binding'])
        self.assertEqual(self.store.value['cleanup_jobs'], {})
        self.assertEqual(result['notification']['status'], 'pending')
        self.assertNotIn('fake-user-secret', json.dumps(self.store.writes))
        self.assertIn('run-ok', result['log'])
        self.assertFalse(self.bay.cargos)

    async def test_install_failure_or_tamper_does_not_decrypt_credentials(self):

        def tamper(directory):
            (directory / 'checkin' / 'script.py').write_bytes(b'tampered')

        self.bay.after_install = tamper
        result = await self.execute()
        self.assertEqual(result['error_code'], 'execution_unknown')
        self.assertEqual(self.credential_calls, 0)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertNotIn('checkin/credentials.json', self.bay.upload_paths)

    async def test_revoke_during_credential_upload_never_starts_script(self):
        revoked = False

        def upload(path, directory):
            nonlocal revoked
            if path.endswith('credentials.json'):
                revoked = True

        self.bay.on_upload = upload

        def guard(snapshot):
            if revoked:
                raise ExecutionBlocked('permission_revoked')

        result = await self.execute(guard)
        self.assertEqual(result['error_code'], 'permission_revoked')
        self.assertEqual(self.bay.execution_calls, 0)

    async def test_ambiguous_create_is_not_replayed_or_forgotten(self):
        self.bay.ambiguous_create = True
        result = await self.execute()
        self.assertEqual(result['error_code'], 'execution_unknown')
        self.assertTrue(self.executor.blocked)
        self.assertTrue(self.store.value['cleanup_jobs'])
        await self.executor.recover()
        self.assertEqual(self.bay.create_calls, 1)
        self.assertTrue(self.executor.blocked)

    async def test_cleanup_failure_keeps_known_process_result_and_blocks(self):
        self.bay.keep_cargo = True
        result = await self.execute()
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['cleanup_status'], 'pending')
        self.assertTrue(self.executor.blocked)
        self.bay.keep_cargo = False
        await self.executor.recover()
        self.assertFalse(self.executor.blocked)
        self.assertEqual(self.store.value['runs']['binding']['phase'],
                         'completed')

    async def test_connection_change_cannot_delete_other_neo_resources(self):
        self.bay.keep_cargo = True
        await self.execute()
        self.client.connection = NeoConnection('https://different.example',
                                               self.bay.token)
        before = dict(self.bay.cargos)
        with self.assertRaises(ExecutionBlocked):
            await self.executor.recover()
        self.assertEqual(self.bay.cargos, before)

    async def test_known_result_waits_for_inflight_state_commit_before_cleanup(
            self):
        import asyncio
        self.store.value['cleanup_jobs']['a' * 32] = {
            'owner': 'owner',
            'script_id': 'demo',
            'connection': self.client.connection.fingerprint,
            'sandbox_id': None,
            'cargo_id': None,
            'allocation_started': False
        }
        release = asyncio.Event()
        self.store.ready = False

        async def wait_ready():
            await release.wait()
            return self.store.ready

        self.store.wait_ready = wait_ready
        finishing = asyncio.create_task(
            self.executor._finalize('binding', 'a' * 32, {
                'phase': 'completed',
                'exit_code': 0,
                'error_code': None
            }, self.store.value['cleanup_jobs']['a' * 32]))
        await asyncio.sleep(0)
        self.assertFalse(finishing.done())
        self.store.ready = True
        release.set()
        await finishing
        self.assertEqual(self.store.value['runs']['binding']['exit_code'], 0)
        self.assertEqual(self.store.value['cleanup_jobs'], {})

    async def test_maximum_encrypted_environment_reaches_runner(self):
        if '_checkin_tests' not in sys.modules:
            package = types.ModuleType('_checkin_tests')
            package.__path__ = [str(Path(__file__).resolve().parents[1])]
            sys.modules[package.__name__] = package
        credential_module = importlib.import_module(
            '_checkin_tests.credentials')
        permission_module = importlib.import_module(
            '_checkin_tests.permissions')
        config_module = importlib.import_module('_checkin_tests.config')
        names = tuple(
            'FIELD_' + str(index).zfill(2) + 'X' * 120 for index in range(64))
        environment = {name: '\x01' * 1024 for name in names}
        declaration = config_module.Script('demo', 'Demo', True,
                                           'https://example.com/a.py', 'python',
                                           names, ())
        identity = permission_module.Identity('qq-main', '123')
        cipher = credential_module.CredentialCipher(b'x' * 32)
        context = {
            'identity': identity,
            'script_id': 'demo',
            'fields_hash': declaration.fields_hash
        }
        token = cipher.encrypt(
            credential_module.validate_values(declaration, environment),
            **context)
        self.store.value['bindings'] = {'binding': {'ciphertext': token}}
        self.source = (
            'import os\nnames=' + repr(names) + '\n'
            'assert all(os.environ[name] == "\\x01"*1024 for name in names)\n'
            'print("all-64-fields-consumed")\n').encode()
        uploaded = []

        def upload(path, directory):
            if path.endswith('credentials.json'):
                data = (directory / path).read_bytes()
                uploaded.append(len(data))
                self.assertEqual(json.loads(data)['env'], environment)

        self.bay.on_upload = upload
        result = await self.executor.execute(
            self.store.snapshot()['runs']['binding'],
            declaration,
            self.source,
            guard=lambda snapshot: None,
            credentials=lambda: cipher.decrypt(token, **context),
            timeout_seconds=2,
            install_timeout_seconds=60)
        self.assertEqual(result['phase'], 'completed', result)
        self.assertGreater(uploaded[0], 256 * 1024)
        self.assertLessEqual(uploaded[0], 512 * 1024)
        self.assertIn('all-64-fields-consumed', result['log'])
        self.assertNotIn('\\u0001', json.dumps(self.store.writes))

    async def test_revoke_while_create_intent_is_committed_does_not_allocate(
            self):
        revoked = False
        mutate = self.store.mutate

        async def controlled(change):
            nonlocal revoked
            result = await mutate(change)
            if self.store.value['runs']['binding']['phase'] == 'creating':
                revoked = True
            return result

        self.store.mutate = controlled

        def guard(snapshot):
            if revoked:
                raise ExecutionBlocked('permission_revoked')

        result = await self.execute(guard)
        self.assertEqual(self.bay.create_calls, 0)
        self.assertEqual(result['error_code'], 'permission_revoked')
        self.assertEqual(result['cleanup_status'], 'cleaned')

    async def test_known_result_is_committed_before_cleanup_network_await(self):
        self.bay.hold_cleanup = True
        task = __import__('asyncio').create_task(self.execute())
        try:
            await self.bay.cleanup_started.wait()
            current = self.store.snapshot()['runs']['binding']
            self.assertEqual(current['phase'], 'completed')
            self.assertEqual(current['exit_code'], 0)
            self.assertEqual(current['cleanup_status'], 'pending')
            self.assertEqual(current['notification']['status'], 'none')
        finally:
            self.bay.release_cleanup.set()
            await task

    async def test_failed_install_never_requests_or_uploads_credentials(self):
        self.bay.install_result = {
            'protocol': 1,
            'stage': 'install',
            'process_status': 'exited',
            'exit_code': 1,
            'duration_ms': 1,
            'log': 'safe install failure',
            'log_truncated': False,
            'error_code': 'install_error'
        }
        result = await self.execute()
        self.assertEqual(result['error_code'], 'install_error')
        self.assertEqual(self.credential_calls, 0)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertNotIn('checkin/credentials.json', self.bay.upload_paths)
