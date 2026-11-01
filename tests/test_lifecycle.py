import asyncio
import copy
import sys
import unittest
from collections import deque
from types import ModuleType
from unittest.mock import patch

from state_support import load_module


class _PutGate:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()


class _FakeKV:
    def __init__(self):
        self.values = {}
        self.put_actions = deque()
        self.put_attempts = 0
        self.committed = []

    def fail_next_put(self):
        self.put_actions.append(OSError("simulated write failure"))

    def block_next_put(self):
        gate = _PutGate()
        self.put_actions.append(gate)
        return gate

    async def get_kv_data(self, key, default):
        if key not in self.values:
            return default
        return copy.deepcopy(self.values[key])

    async def put_kv_data(self, key, value):
        self.put_attempts += 1
        action = self.put_actions.popleft() if self.put_actions else None
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, _PutGate):
            action.started.set()
            await action.release.wait()

        detached = copy.deepcopy(value)
        self.values[key] = detached
        self.committed.append(copy.deepcopy(detached))


class _AstrBotContext:
    def __init__(self):
        self._plugin_kv = {}

    def kv_for(self, plugin_id):
        return self._plugin_kv.setdefault(plugin_id, _FakeKV())


class _AstrBotStar:
    registered = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _AstrBotStar.registered.append(cls)

    def __init__(self, context):
        self.context = context

    async def get_kv_data(self, key, default):
        return await self.context.kv_for(self.plugin_id).get_kv_data(key, default)

    async def put_kv_data(self, key, value):
        await self.context.kv_for(self.plugin_id).put_kv_data(key, value)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        astrbot = ModuleType("astrbot")
        astrbot.__path__ = []
        api = ModuleType("astrbot.api")
        api.__path__ = []
        star = ModuleType("astrbot.api.star")
        star.Context = _AstrBotContext
        star.Star = _AstrBotStar
        api.star = star
        astrbot.api = api
        cls._astrbot_patch = patch.dict(
            sys.modules,
            {
                "astrbot": astrbot,
                "astrbot.api": api,
                "astrbot.api.star": star,
            },
        )
        cls._astrbot_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._astrbot_patch.stop()

    def load_generation(self, generation):
        package = f"_checkin_lifecycle_{generation}"
        return load_module("main", package), load_module("storage", package)

    def make_plugin(self, module, context, plugin_id="author/cloud-drive-checkin"):
        plugin = module.CloudDriveCheckin(context)
        plugin.plugin_id = plugin_id
        return plugin

    async def test_star_registers_subclass_without_explicit_registration(self):
        main, _ = self.load_generation("registration")
        self.assertIn(main.CloudDriveCheckin, _AstrBotStar.registered)

    async def test_active_owner_rejects_reload(self):
        first_main, _ = self.load_generation("active_first")
        second_main, _ = self.load_generation("active_second")
        context = _AstrBotContext()
        first = self.make_plugin(first_main, context)
        second = self.make_plugin(second_main, context)

        await first.initialize()

        with self.assertRaises(RuntimeError):
            await second.initialize()

        self.assertTrue(first.state_store.ready)
        self.assertIsNone(second.state_store)
        await first.terminate()

    async def test_failed_close_blocks_reload_until_pending_write_recovers(self):
        first_main, first_storage = self.load_generation("failure_first")
        blocked_main, _ = self.load_generation("failure_blocked")
        recovered_main, _ = self.load_generation("failure_recovered")
        context = _AstrBotContext()
        backend = context.kv_for("author/cloud-drive-checkin")
        first = self.make_plugin(first_main, context)
        blocked = self.make_plugin(blocked_main, context)
        recovered = self.make_plugin(recovered_main, context)

        await first.initialize()
        backend.fail_next_put()
        with self.assertRaises(first_storage.CommitUnconfirmedError):
            await first.state_store.mutate(
                lambda snapshot: snapshot["users"].update({"user": {"enabled": True}})
            )

        backend.fail_next_put()
        with self.assertRaises(first_storage.CommitUnconfirmedError):
            await first.terminate()
        self.assertTrue(first.state_store.closing)
        self.assertFalse(first.state_store.closed)

        backend.fail_next_put()
        with self.assertRaises(first_storage.CommitUnconfirmedError):
            await blocked.initialize()
        self.assertIsNone(blocked.state_store)
        self.assertFalse(first.state_store.closed)

        await recovered.initialize()

        self.assertTrue(first.state_store.closed)
        self.assertTrue(recovered.state_store.ready)
        self.assertEqual(recovered.state_store.snapshot()["revision"], 1)
        attempts_before_stale_write = backend.put_attempts
        with self.assertRaises(first_storage.StateUnavailableError):
            await first.state_store.mutate(
                lambda snapshot: snapshot["users"].update({"late": {}})
            )
        self.assertEqual(backend.put_attempts, attempts_before_stale_write)
        await recovered.terminate()

    async def test_different_plugin_ids_have_independent_owners(self):
        first_main, _ = self.load_generation("isolated_first")
        second_main, _ = self.load_generation("isolated_second")
        context = _AstrBotContext()
        first = self.make_plugin(first_main, context, "author/first")
        second = self.make_plugin(second_main, context, "author/second")

        await first.initialize()
        await second.initialize()

        self.assertTrue(first.state_store.ready)
        self.assertTrue(second.state_store.ready)
        self.assertIsNot(first.state_store, second.state_store)
        await first.terminate()
        await second.terminate()

    async def test_cancelled_initialization_is_closed_and_recovered_by_reload(self):
        first_main, _ = self.load_generation("cancel_first")
        recovered_main, _ = self.load_generation("cancel_recovered")
        context = _AstrBotContext()
        backend = context.kv_for("author/cloud-drive-checkin")
        first = self.make_plugin(first_main, context)
        recovered = self.make_plugin(recovered_main, context)
        gate = backend.block_next_put()

        initialization = asyncio.create_task(first.initialize())
        await gate.started.wait()
        initialization.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await initialization

        self.assertTrue(first.state_store.closing)
        self.assertTrue(first.state_store.has_pending)
        self.assertFalse(first.state_store.closed)

        await recovered.initialize()

        self.assertTrue(first.state_store.closed)
        self.assertTrue(recovered.state_store.ready)
        self.assertFalse(recovered.state_store.has_pending)
        await recovered.terminate()


if __name__ == "__main__":
    unittest.main()
