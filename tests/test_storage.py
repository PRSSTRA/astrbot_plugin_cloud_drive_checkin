"""验证结果不明的 KV 写入冻结原候选值，不回滚或重算。"""

import asyncio
import copy
import importlib
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if '_checkin_tests' not in sys.modules:
    package = types.ModuleType('_checkin_tests')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
state_module = importlib.import_module('_checkin_tests.state')
InvalidStateError = state_module.InvalidStateError
new_snapshot = state_module.new_snapshot
storage_module = importlib.import_module('_checkin_tests.storage')
CommitUnconfirmedError = storage_module.CommitUnconfirmedError
StateStore = storage_module.StateStore
StateUnavailableError = storage_module.StateUnavailableError


class KV:

    def __init__(self):
        self.value = None
        self.writes = []
        self.error = None
        self.block = None

    async def get_kv_data(self, key, default=None):
        return default if self.value is None else self.value

    async def put_kv_data(self, key, value):
        self.value = copy.deepcopy(value)
        self.writes.append(copy.deepcopy(value))
        if self.block is not None:
            await self.block.wait()
        if self.error:
            raise self.error


class StorageTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.kv = KV()
        self.store = StateStore(self.kv, write_timeout=.02)
        await self.store.initialize()

    async def test_applied_but_raised_write_retries_exact_candidate(self):
        self.kv.error = OSError('not confirmed')
        with self.assertRaises(CommitUnconfirmedError):
            await self.store.mutate(lambda state: None)
        self.assertEqual(self.store.snapshot()['revision'], 0)
        pending = copy.deepcopy(self.kv.writes[-1])
        self.assertFalse(self.store.ready)
        with self.assertRaises(CommitUnconfirmedError):
            await self.store.mutate(
                lambda state: self.fail('must not recompute'))
        self.kv.error = None
        await self.store.retry_pending()
        self.assertEqual(self.kv.writes[-1], pending)
        self.assertEqual(self.store.snapshot()['revision'], 1)

    async def test_timeout_and_cancel_freeze_original_pending(self):
        self.kv.block = asyncio.Event()
        with self.assertRaises(CommitUnconfirmedError):
            await self.store.mutate(lambda state: None)
        expected = copy.deepcopy(self.kv.writes[-1])
        self.kv.block.set()
        await self.store.retry_pending()
        self.assertEqual(self.kv.writes[-1], expected)
        self.kv.block = asyncio.Event()
        task = asyncio.create_task(self.store.mutate(lambda state: None))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.store.has_pending)
        self.kv.block.set()
        await self.store.close()
        self.assertTrue(self.store.closed)

    async def test_load_always_rewrites_and_unknown_schema_stays_untouched(
            self):
        kv = KV()
        kv.value = new_snapshot()
        await StateStore(kv).initialize()
        self.assertEqual(len(kv.writes), 1)
        kv.value['schema_version'] = 999
        with self.assertRaises(InvalidStateError):
            await StateStore(kv).initialize()
        self.assertEqual(len(kv.writes), 1)
        self.assertEqual(kv.value['schema_version'], 999)

    async def test_callback_aliases_and_managed_fields_cannot_corrupt(self):
        captured = []
        await self.store.mutate(lambda state: captured.append(state))
        captured[0]['users']['bad'] = {}
        self.assertEqual(self.store.snapshot()['users'], {})
        with self.assertRaises(InvalidStateError):
            await self.store.mutate(lambda state: state.update(revision=99))
        self.store.stop_accepting()
        with self.assertRaises(StateUnavailableError):
            await self.store.mutate(lambda state: None)

    async def test_wait_ready_waits_normal_write_but_never_confirms_ambiguous(
            self):
        self.kv.block = asyncio.Event()
        writing = asyncio.create_task(self.store.mutate(lambda state: None))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(self.store.wait_ready())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.kv.block.set()
        await writing
        self.assertTrue(await waiter)
        self.kv.error = OSError('ambiguous')
        with self.assertRaises(CommitUnconfirmedError):
            await self.store.mutate(lambda state: None)
        writes = len(self.kv.writes)
        self.assertFalse(await self.store.wait_ready())
        self.assertEqual(len(self.kv.writes), writes)
