import copy
import unittest

from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    FakeEvent,
    PlainResult,
    patch_astrbot_modules,
)
from state_support import load_module

_PLUGIN_ID = "author/cloud-drive-checkin"


class CommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._astrbot_patch = patch_astrbot_modules()
        cls._astrbot_patch.start()
        package = "_checkin_commands"
        cls.main = load_module("main", package)
        cls.storage = load_module("storage", package)

    @classmethod
    def tearDownClass(cls):
        cls._astrbot_patch.stop()

    def make_plugin(self, config, context=None):
        context = AstrBotContext() if context is None else context
        plugin = self.main.CloudDriveCheckin(context, config)
        plugin.plugin_id = _PLUGIN_ID
        return plugin, context

    async def initialize_plugin(self, config, context=None):
        plugin, context = self.make_plugin(config, context)
        await plugin.initialize()
        self.addAsyncCleanup(plugin.terminate)
        return plugin, context, context.kv_for(_PLUGIN_ID)

    def assert_intercepted(self, event):
        self.assertFalse(event.llm_enabled)
        self.assertTrue(event.stopped)
        self.assertEqual(
            event.actions[:2],
            [("should_call_llm", False), ("stop_event",)],
        )

    async def test_constructor_rejects_invalid_config_before_accessing_kv(self):
        context = AstrBotContext()
        config = AstrBotConfig(enabled="not-a-boolean")

        with self.assertRaises(self.main.PluginConfigError):
            self.make_plugin(config, context)

        self.assertEqual(context._plugin_kv, {})

    async def test_constructor_rejects_invalid_task_config_before_accessing_kv(self):
        context = AstrBotContext()
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
            tasks="TOP_SECRET",
        )

        with self.assertRaises(self.main.PluginConfigError):
            self.make_plugin(config, context)

        self.assertEqual(context._plugin_kv, {})

    async def test_default_help_for_whitelisted_user_is_read_only(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        original_config = copy.deepcopy(config)
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event)

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0], PlainResult)
        self.assertEqual(backend.put_attempts, writes_before)
        self.assertEqual(config, original_config)

    async def test_disabled_service_still_shows_help_to_administrator(self):
        config = AstrBotConfig(
            enabled=False,
            plugin_admin_qqs=[],
            whitelist_qqs=[],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        event = FakeEvent(sender_id="10001", astrbot_admin=True)

        await plugin.checkin(event)

        self.assert_intercepted(event)
        self.assertEqual(event.admin_checks, 1)
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0], PlainResult)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_unauthorized_private_user_gets_fixed_denial(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=[],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event, "credential=TOP_SECRET")

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 1)
        self.assertNotIn("TOP_SECRET", event.sent[0].text)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_unauthorized_user_cannot_inspect_task_config(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=[],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        config["tasks"] = [{"task_id": "TOP_SECRET"}]
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event, "任务", "tianyi_cloud")

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 1)
        self.assertNotIn("tianyi_cloud", event.sent[0].text)
        self.assertEqual(event.sent[0].text, self.main._PERMISSION_DENIED_MESSAGE)
        self.assertNotIn("TOP_SECRET", event.sent[0].text)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_unknown_subcommand_returns_usage_without_echoing_input(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        original_config = copy.deepcopy(config)
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event, "绑定 TOP_SECRET")
        known_command = FakeEvent(sender_id="10001")
        await plugin.checkin(known_command)

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 1)
        self.assertNotEqual(event.sent, known_command.sent)
        self.assertNotIn("TOP_SECRET", event.sent[0].text)
        self.assertEqual(backend.put_attempts, writes_before)
        self.assertEqual(config, original_config)

    async def test_runtime_whitelist_revocation_takes_effect_immediately(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        allowed = FakeEvent(sender_id="10001")

        await plugin.checkin(allowed)
        config["whitelist_qqs"].clear()
        revoked = FakeEvent(sender_id="10001")
        await plugin.checkin(revoked)

        self.assertEqual(len(allowed.sent), 1)
        self.assertEqual(len(revoked.sent), 1)
        self.assertNotEqual(revoked.sent, allowed.sent)
        self.assertEqual(backend.put_attempts, writes_before)
        self.assertEqual(config["whitelist_qqs"], [])

    async def test_runtime_invalid_config_returns_safe_fixed_error(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        config["whitelist_qqs"] = ["10001", "TOP_SECRET"]
        event = FakeEvent(sender_id="10001", astrbot_admin=True)

        await plugin.checkin(event)

        self.assert_intercepted(event)
        self.assertEqual(event.admin_checks, 0)
        self.assertEqual(len(event.sent), 1)
        self.assertNotIn("TOP_SECRET", event.sent[0].text)
        self.assertEqual(backend.put_attempts, writes_before)
        self.assertEqual(config["whitelist_qqs"], ["10001", "TOP_SECRET"])

    async def test_runtime_invalid_task_config_returns_safe_fixed_error(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        config["tasks"] = [{"task_id": "TOP_SECRET"}]
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event, "任务")

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 1)
        self.assertNotIn("TOP_SECRET", event.sent[0].text)
        self.assertEqual(event.sent[0].text, self.main._CONFIG_ERROR_MESSAGE)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_task_detail_does_not_expose_execution_config_values(self):
        commit = "a" * 40
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
            tasks=[
                {
                    "task_id": "metadata_only",
                    "name": "元数据任务",
                    "enabled": False,
                    "runtime": "python",
                    "script_url": (
                        "https://raw.githubusercontent.com/example/repository/"
                        f"{commit}/script.py"
                    ),
                    "source_commit": commit,
                    "sha256": "b" * 64,
                    "cron": "0 8 * * *",
                    "user_fields": [
                        {
                            "field_id": "account",
                            "label": "账号",
                            "env_name": "ACCOUNT_EXECUTION_NAME",
                            "value_type": "string",
                            "required": True,
                            "secret": True,
                            "hint": "字段元数据",
                        }
                    ],
                    "fixed_env": {"SAFE_OPTION": "FIXED_EXECUTION_VALUE"},
                    "input_adapter": "direct_env",
                    "result_adapter": "process_only",
                    "dependency_bundle": "none",
                    "timeout_seconds": 0,
                    "experimental": False,
                }
            ],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event, "任务", "metadata_only")

        detail = event.sent[0].text
        self.assertIn("account", detail)
        self.assertNotIn("raw.githubusercontent.com", detail)
        self.assertNotIn("ACCOUNT_EXECUTION_NAME", detail)
        self.assertNotIn("FIXED_EXECUTION_VALUE", detail)
        self.assertNotIn("DEPENDENCY_EXECUTION_VALUE", detail)
        self.assertNotIn("b" * 64, detail)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_runtime_task_changes_and_explicit_empty_catalog_are_immediate(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        before = FakeEvent(sender_id="10001")

        await plugin.checkin(before, "任务")
        config["tasks"] = []
        after = FakeEvent(sender_id="10001")
        await plugin.checkin(after, "任务")

        self.assertIn("tianyi_cloud", before.sent[0].text)
        self.assertNotIn("tianyi_cloud", after.sent[0].text)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_task_query_does_not_echo_unknown_or_extra_input(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        unknown = FakeEvent(sender_id="10001")
        extra = FakeEvent(sender_id="10001")

        await plugin.checkin(unknown, "任务", "TOP_SECRET")
        await plugin.checkin(
            extra,
            "任务",
            "tianyi_cloud",
            "TOP_SECRET",
        )

        self.assertEqual(unknown.sent[0].text, self.main._TASK_NOT_FOUND_MESSAGE)
        self.assertEqual(extra.sent[0].text, self.main._USAGE_MESSAGE)
        self.assertNotIn("TOP_SECRET", unknown.sent[0].text)
        self.assertNotIn("TOP_SECRET", extra.sent[0].text)
        self.assertEqual(backend.put_attempts, writes_before)

    async def test_invalid_platform_session_and_identity_are_silent(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=["10001"],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        writes_before = backend.put_attempts
        private = {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "user_id": "10001",
            "sender": {"user_id": "10001"},
            "message": [],
        }
        cases = {
            "other platform": FakeEvent(
                platform_name="other",
                sender_id="10001",
                astrbot_admin=True,
            ),
            "malformed raw": FakeEvent(
                sender_id="10001",
                raw_message=None,
                astrbot_admin=True,
            ),
            "group": FakeEvent(
                sender_id="10001",
                raw_message={
                    **private,
                    "message_type": "group",
                    "sub_type": "normal",
                },
                astrbot_admin=True,
            ),
            "temporary": FakeEvent(
                sender_id="10001",
                raw_message={**private, "temp_source": 1},
                astrbot_admin=True,
            ),
            "contradictory group": FakeEvent(
                sender_id="10001",
                raw_message={**private, "group_id": "20001"},
                astrbot_admin=True,
            ),
            "malformed identity": FakeEvent(
                sender_id="10001",
                raw_message={**private, "user_id": "10002"},
                astrbot_admin=True,
            ),
        }

        for label, event in cases.items():
            with self.subTest(label=label):
                await plugin.checkin(event)
                self.assert_intercepted(event)
                self.assertEqual(event.admin_checks, 0)
                self.assertEqual(event.sent, [])

        self.assertEqual(backend.put_attempts, writes_before)

    async def test_uninitialized_closing_and_closed_instances_do_not_reply(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        context = AstrBotContext()
        old, _ = self.make_plugin(config, context)
        before_initialization = FakeEvent(sender_id="10001")

        await old.checkin(before_initialization)
        await old.initialize()
        await old.terminate()

        replacement, _, _ = await self.initialize_plugin(config, context)
        closed_event = FakeEvent(sender_id="10001")
        active_event = FakeEvent(sender_id="10001")
        await old.checkin(closed_event)
        await replacement.checkin(active_event)
        replacement.state_store.stop_accepting()
        closing_event = FakeEvent(sender_id="10001")
        await replacement.checkin(closing_event)

        self.assertEqual(before_initialization.sent, [])
        self.assertEqual(closed_event.sent, [])
        self.assertEqual(len(active_event.sent), 1)
        self.assertEqual(closing_event.sent, [])
        for event in (before_initialization, closed_event, closing_event):
            self.assert_intercepted(event)

    async def test_help_reports_unready_store_without_writing_state(self):
        config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001"],
        )
        plugin, _, backend = await self.initialize_plugin(config)
        ready_event = FakeEvent(sender_id="10001")
        await plugin.checkin(ready_event)
        backend.fail_next_put()
        with self.assertRaises(self.storage.CommitUnconfirmedError):
            await plugin.state_store.mutate(
                lambda snapshot: snapshot["users"].update({"user": {}})
            )
        writes_before = backend.put_attempts
        event = FakeEvent(sender_id="10001")

        await plugin.checkin(event)

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 1)
        self.assertNotEqual(event.sent, ready_event.sent)
        self.assertEqual(backend.put_attempts, writes_before)


if __name__ == "__main__":
    unittest.main()
