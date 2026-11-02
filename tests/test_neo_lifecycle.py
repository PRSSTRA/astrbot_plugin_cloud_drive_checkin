import asyncio
import hashlib
import os
import unittest
from unittest.mock import patch

import httpx
from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    FakeEvent,
    patch_astrbot_modules,
)
from neo_support import FakeBay
from state_support import load_module


class NeoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.modules = patch_astrbot_modules()
        self.modules.start()
        self.main = load_module("main", "_neo_lifecycle_tests")
        self.neo = load_module("executors.neo", "_neo_lifecycle_tests")
        self.client = load_module("executors.client", "_neo_lifecycle_tests")
        self.permissions = load_module("permissions", "_neo_lifecycle_tests")
        self.bay = FakeBay()
        self.env = patch.dict(os.environ, {"TEST_CHECKIN_NEO_KEY": self.bay.token})
        self.env.start()
        self.clients = patch.object(
            self.main,
            "NeoClient",
            lambda connection: self.client.NeoClient(
                connection, transport=httpx.MockTransport(self.bay)
            ),
        )
        self.clients.start()
        self.context = AstrBotContext()
        self.context.get_config = dict
        self.config = AstrBotConfig(
            {
                "neo_connection": {
                    "enabled": True,
                    "docker_cleanup_confirmed": True,
                    "endpoint": "http://fake-bay.invalid",
                    "access_key_env": "TEST_CHECKIN_NEO_KEY",
                }
            }
        )
        self.plugins = []

    def plugin(self):
        plugin = self.main.CloudDriveCheckin(self.context, self.config)
        plugin.plugin_id = "author/neo-lifecycle"
        self.plugins.append(plugin)
        return plugin

    def request(self):
        script = b'print(\'{"status":"success"}\')\n'
        return self.neo.PreparedExecution(
            self.permissions.Identity("qq", "10001"),
            "fake_checkin",
            "1" * 64,
            "2" * 40,
            hashlib.sha256(script).hexdigest(),
            "python",
            script,
            {"TOKEN": "FAKE_TOKEN"},
            timeout_seconds=5,
        )

    async def asyncTearDown(self):
        self.bay.release_cleanup.set()
        for plugin in self.plugins:
            await plugin.terminate()
        self.clients.stop()
        self.env.stop()
        self.modules.stop()
        await self.bay.close()

    async def test_reload_recovers_recorded_resources_without_replaying_script(self):
        first = self.plugin()
        await first.initialize()
        self.bay.keep_cargo = True
        run = await first.executor.execute(self.request(), guard=lambda snapshot: None)
        run_id = run["run_id"]

        def interrupted(snapshot):
            snapshot["runs"][run_id].update(finished_at=None, phase="executing")

        await first.state_store.mutate(interrupted)
        await first.terminate()
        self.bay.keep_cargo = False
        second = self.plugin()
        await second.initialize()
        recovered = second.state_store.snapshot()["runs"][run_id]
        self.assertEqual(recovered["process_status"], "interrupted")
        self.assertEqual(recovered["business_status"], "unknown")
        self.assertEqual(recovered["cleanup_status"], "cleaned")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertFalse(second.executor.blocked)

    async def test_old_executor_finishes_cleanup_before_new_writer_can_take_over(self):
        first = self.plugin()
        await first.initialize()
        self.bay.hold_execution = True
        self.bay.hold_cleanup = True
        running = asyncio.create_task(
            first.executor.execute(self.request(), guard=lambda snapshot: None)
        )
        await asyncio.wait_for(self.bay.executing.wait(), 10)
        stopping = asyncio.create_task(first.terminate())
        await asyncio.wait_for(self.bay.cleanup_started.wait(), 10)
        second = self.plugin()
        with self.assertRaises(RuntimeError):
            await second.initialize()
        event = FakeEvent(text="/签到 帮助")
        await first.checkin(event)
        self.assertEqual(event.sent, [])
        self.assertFalse(first.state_store.closed)
        self.bay.release_cleanup.set()
        await stopping
        self.assertTrue(running.cancelled())
        self.assertTrue(first.state_store.closed)
        await second.initialize()
        self.assertEqual(second.state_store.snapshot()["cleanup_jobs"], {})


if __name__ == "__main__":
    unittest.main()
