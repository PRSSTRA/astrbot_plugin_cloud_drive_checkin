import asyncio
import base64
import copy
import os
import unittest
from unittest.mock import patch

from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    FakeEvent,
    patch_astrbot_modules,
)
from state_support import load_module


class BindingCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.astrbot_patch = patch_astrbot_modules()
        cls.astrbot_patch.start()
        package = "_binding_command_tests"
        cls.main = load_module("main", package)
        cls.crypto = load_module("credentials", package)
        cls.permissions = load_module("permissions", package)
        cls.catalog = load_module("task_config", package)

    @classmethod
    def tearDownClass(cls):
        cls.astrbot_patch.stop()

    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("CLOUD_DRIVE_CHECKIN_MASTER_KEY", None)
        self.config = AstrBotConfig(whitelist_qqs=["10001", "10002"])
        self.context = AstrBotContext()
        self.plugin = await self.new_plugin()
        self.backend = self.context.kv_for("test/binding-controls")
        self.identity = self.permissions.Identity("bot-instance", "10001")
        self.key = os.urandom(32)
        self.task = self.catalog.parse_task_catalog(self.config).tasks[0]
        await self.save(self.identity)

    async def new_plugin(self):
        plugin = self.main.CloudDriveCheckin(self.context, self.config)
        plugin.plugin_id = "test/binding-controls"
        await plugin.initialize()
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def save(self, identity):
        await self.crypto.save_binding(
            self.plugin.state_store,
            self.crypto.CredentialCipher(self.key),
            identity=identity,
            task=self.task,
            origin=f"{identity.platform_id}:FriendMessage:{identity.qq_id}",
            values={
                "username": "BINDING_TEST_ACCOUNT",
                "password": "BINDING_TEST_PASSWORD",
            },
            guard=lambda: None,
        )

    def record(self, identity=None):
        owner = self.identity if identity is None else identity
        key = self.crypto.binding_key(owner, self.task.task_id)
        return self.plugin.state_store.snapshot()["bindings"].get(key)

    async def command(
        self, command, option="", *, task_id="tianyi_cloud", tail="", **event_options
    ):
        event = FakeEvent(**event_options)
        await self.plugin.checkin(event, command, task_id, option, tail)
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm_enabled)
        self.assertNotIn("BINDING_TEST_PASSWORD", repr(event.sent))
        return event

    async def test_preferences_change_only_own_binding_without_master_key(self):
        other = self.permissions.Identity("other-instance", "10001")
        await self.save(other)
        snapshot = self.plugin.state_store.snapshot()
        config = copy.deepcopy(self.config)
        before = FakeEvent()
        await self.plugin.checkin(before, "我的")
        await self.command("暂停")
        await self.command("通知", "开")
        self.assertTrue(self.record()["paused"])
        self.assertTrue(self.record()["notify_all"])
        after = FakeEvent()
        await self.plugin.checkin(after, "我的")
        self.assertNotEqual(before.sent[0].text, after.sent[0].text)
        await self.command("恢复")
        await self.command("通知", "关")
        self.assertFalse(self.record()["paused"])
        self.assertFalse(self.record()["notify_all"])
        current = self.plugin.state_store.snapshot()
        self.assertEqual(current["credentials"], snapshot["credentials"])
        self.assertEqual(
            self.record(other),
            snapshot["bindings"][self.crypto.binding_key(other, self.task.task_id)],
        )
        self.assertEqual(self.config, config)

    async def test_delete_requires_confirm_and_cancel_is_read_only(self):
        snapshot = self.plugin.state_store.snapshot()
        writes = self.backend.put_attempts
        await self.command("删除", "确认")
        await self.command("删除")
        await self.command("删除", "取消")
        await self.command("删除", "确认")
        self.assertEqual(self.plugin.state_store.snapshot(), snapshot)
        self.assertEqual(self.backend.put_attempts, writes)
        await self.command("删除")
        await self.command("删除", "确认")
        self.assertIsNone(self.record())
        key = self.crypto.binding_key(self.identity, self.task.task_id)
        self.assertNotIn(key, self.plugin.state_store.snapshot()["credentials"])
        self.assertIn(
            self.crypto.user_key(self.identity),
            self.plugin.state_store.snapshot()["users"],
        )
        writes = self.backend.put_attempts
        await self.command("删除", "确认")
        self.assertEqual(self.backend.put_attempts, writes)

    async def test_other_identity_cannot_confirm_own_request_even_as_admin(self):
        await self.command("删除")
        for event_options in (
            {"sender_id": "10002", "astrbot_admin": True},
            {"platform_id": "other-instance"},
        ):
            await self.command("删除", "确认", **event_options)
            self.assertIsNotNone(self.record())
        await self.command("删除", "确认")
        self.assertIsNone(self.record())

    async def test_expired_or_reloaded_confirmation_cannot_delete(self):
        with patch.object(self.main, "_DELETE_CONFIRM_SECONDS", 0):
            await self.command("删除")
        await self.command("删除", "确认")
        self.assertIsNotNone(self.record())
        await self.command("删除")
        await self.plugin.terminate()
        self.plugin = await self.new_plugin()
        await self.command("删除", "确认")
        self.assertIsNotNone(self.record())

    async def test_own_record_replacement_invalidates_confirmation_but_other_user_does_not(
        self,
    ):
        await self.command("删除")
        await self.save(self.identity)
        await self.command("删除", "确认")
        self.assertIsNotNone(self.record())
        await self.command("删除")
        await self.save(self.permissions.Identity("bot-instance", "10002"))
        await self.command("删除", "确认")
        self.assertIsNone(self.record())
        self.assertIsNotNone(
            self.record(self.permissions.Identity("bot-instance", "10002"))
        )

    async def test_revocation_is_checked_after_waiting_for_state_lock(self):
        snapshot = self.plugin.state_store.snapshot()
        async with self.plugin.state_store._lock:
            operation = asyncio.create_task(self.command("暂停"))
            await asyncio.sleep(0)
            self.config["whitelist_qqs"] = []
        await operation
        self.assertEqual(self.plugin.state_store.snapshot(), snapshot)

    async def test_can_manage_binding_after_task_removed_from_catalog(self):
        self.config["tasks"] = []
        await self.command("暂停")
        self.assertTrue(self.record()["paused"])
        await self.command("删除")
        await self.command("删除", "确认")
        self.assertIsNone(self.record())

    async def test_invalid_parameters_and_extra_tokens_do_not_write_or_echo(self):
        snapshot = self.plugin.state_store.snapshot()
        for command, option, task_id, tail in (
            ("通知", "SECRET_VALUE", "tianyi_cloud", ""),
            ("暂停", "SECRET_VALUE", "tianyi_cloud", ""),
            ("恢复", "", "SECRET_VALUE", ""),
            ("删除", "确认", "tianyi_cloud", "SECRET_VALUE"),
            ("删除", "", "", ""),
        ):
            event = await self.command(command, option, task_id=task_id, tail=tail)
            self.assertNotIn("SECRET_VALUE", event.sent[0].text)
        self.assertEqual(self.plugin.state_store.snapshot(), snapshot)

    async def test_active_enrollment_blocks_binding_mutation(self):
        os.environ["CLOUD_DRIVE_CHECKIN_MASTER_KEY"] = base64.urlsafe_b64encode(
            self.key
        ).decode()
        await self.plugin.checkin(FakeEvent(), "录入", self.task.task_id)
        snapshot = self.plugin.state_store.snapshot()
        await self.command("暂停")
        await self.command("删除")
        await self.command("删除", "确认")
        self.assertEqual(self.plugin.state_store.snapshot(), snapshot)

    async def test_failed_deletion_keeps_confirmed_state_and_retries_only_original_write(
        self,
    ):
        await self.command("删除")
        baseline = self.plugin.state_store.snapshot()
        self.backend.fail_next_put()
        await self.command("删除", "确认")
        self.assertTrue(self.plugin.state_store.has_pending)
        self.assertEqual(self.plugin.state_store.snapshot(), baseline)
        await self.plugin.state_store.retry_pending()
        self.assertIsNone(self.record())
        writes = self.backend.put_attempts
        await self.command("删除", "确认")
        self.assertEqual(self.backend.put_attempts, writes)

    async def test_group_temporary_and_foreign_platform_admins_cannot_manage_binding(
        self,
    ):
        snapshot = self.plugin.state_store.snapshot()
        for options in (
            {"platform_name": "other"},
            {"raw_message": {"message_type": "group"}},
            {
                "raw_message": {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "group",
                }
            },
        ):
            event = await self.command("暂停", astrbot_admin=True, **options)
            self.assertEqual(event.sent, [])
        self.assertEqual(self.plugin.state_store.snapshot(), snapshot)


if __name__ == "__main__":
    unittest.main()
