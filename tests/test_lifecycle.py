import asyncio
import unittest
from unittest import mock

from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    AstrBotStar,
    patch_astrbot_modules,
)
from state_support import load_module


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._astrbot_patch = patch_astrbot_modules()
        cls._astrbot_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._astrbot_patch.stop()

    def load_generation(self, generation):
        package = f"_checkin_lifecycle_{generation}"
        return load_module("main", package), load_module("storage", package)

    def make_plugin(self, module, context, plugin_id="author/cloud-drive-checkin"):
        plugin = module.CloudDriveCheckin(context, AstrBotConfig())
        plugin.plugin_id = plugin_id
        return plugin

    async def test_star_registers_subclass_without_explicit_registration(self):
        main, _ = self.load_generation("registration")
        self.assertIn(main.CloudDriveCheckin, AstrBotStar.registered)

    async def test_active_owner_rejects_reload(self):
        first_main, _ = self.load_generation("active_first")
        second_main, _ = self.load_generation("active_second")
        context = AstrBotContext()
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
        context = AstrBotContext()
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
        context = AstrBotContext()
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
        context = AstrBotContext()
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

    async def test_terminate_while_initialize_waits_for_owner_prevents_start(self):
        main, _ = self.load_generation("owner_wait_termination")
        context = AstrBotContext()
        plugin = self.make_plugin(main, context)
        self.addAsyncCleanup(plugin.terminate)
        entered = asyncio.Event()
        release = asyncio.Event()

        class ClosingOwner:
            closing = True
            closed = False

            async def close(self):
                entered.set()
                await release.wait()
                self.closed = True

        plugin._state_slot()["owner"] = ClosingOwner()
        initialization = asyncio.create_task(plugin.initialize())
        await asyncio.wait_for(entered.wait(), 1)
        await plugin.terminate()
        self.assertTrue(plugin._terminating)
        release.set()
        outcome = (await asyncio.gather(initialization, return_exceptions=True))[0]

        self.assertIsInstance(outcome, BaseException)
        self.assertTrue(plugin._terminating)
        self.assertIsNone(plugin.state_store)
        self.assertIsNone(plugin.notifications)
        self.assertIsNone(plugin.queue)
        self.assertIsNone(plugin.scheduler)

    async def test_terminate_during_recovery_prevents_later_services(self):
        main, _ = self.load_generation("recovery_termination")
        context = AstrBotContext()
        plugin = self.make_plugin(main, context)
        self.addAsyncCleanup(plugin.terminate)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_recovery(service):
            entered.set()
            await release.wait()

        with mock.patch.object(main.TaskControlService, "recover", gated_recovery):
            initialization = asyncio.create_task(plugin.initialize())
            await asyncio.wait_for(entered.wait(), 1)
            termination = asyncio.create_task(plugin.terminate())
            async with asyncio.timeout(1):
                while not plugin.state_store.closing:
                    await asyncio.sleep(0)
            release.set()
            outcomes = await asyncio.gather(
                initialization, termination, return_exceptions=True
            )

        self.assertIsInstance(outcomes[0], BaseException)
        self.assertTrue(plugin._terminating)
        self.assertIsNone(plugin.notifications)
        self.assertIsNone(plugin.queue)
        self.assertIsNone(plugin.scheduler)


if __name__ == "__main__":
    unittest.main()
