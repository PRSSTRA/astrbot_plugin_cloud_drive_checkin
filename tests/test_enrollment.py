import asyncio
import base64
import json
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


class EnrollmentTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = patch_astrbot_modules()
        cls.modules.start()
        cls.main = load_module("main", "_enrollment_tests")
        cls.crypto = load_module("credentials", "_enrollment_tests")
        cls.permissions = load_module("permissions", "_enrollment_tests")
        cls.catalog = load_module("task_config", "_enrollment_tests")

    @classmethod
    def tearDownClass(cls):
        cls.modules.stop()

    async def asyncSetUp(self):
        self.key = os.urandom(32)
        self.env = patch.dict(
            os.environ,
            {
                "CLOUD_DRIVE_CHECKIN_MASTER_KEY": base64.urlsafe_b64encode(
                    self.key
                ).decode()
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = AstrBotConfig(whitelist_qqs=["10001", "10002"])
        self.context = AstrBotContext()
        self.plugin = await self.new_plugin()
        self.identity = self.permissions.Identity("bot-instance", "10001")
        self.backend = self.context.kv_for("test/enrollment")

    async def new_plugin(self):
        plugin = self.main.CloudDriveCheckin(self.context, self.config)
        plugin.plugin_id = "test/enrollment"
        await plugin.initialize()
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def start(self, **identity):
        event = FakeEvent(**identity)
        await self.plugin.checkin(event, "录入", "tianyi_cloud")
        return event

    async def input(self, text, **identity):
        event = FakeEvent(text=text, **identity)
        await self.plugin.capture_enrollment(event)
        return event

    def decoded(self, identity=None):
        identity = self.identity if identity is None else identity
        snapshot = self.plugin.state_store.snapshot()
        record = snapshot["credentials"][
            self.crypto.binding_key(identity, "tianyi_cloud")
        ]
        return self.crypto.CredentialCipher(self.key).decrypt(
            record["ciphertext"],
            identity=identity,
            task_id="tianyi_cloud",
            task_version=record["task_version"],
        )

    async def test_raw_text_survives_confirmation_and_never_enters_state_or_reply(self):
        password = "  FAKE_ENROLLMENT_PASSWORD'\";\nsecond-line  "
        await self.start()
        for text in ("private-account", password):
            reply = await self.input(text)
            self.assertTrue(reply.stopped)
            self.assertFalse(reply.llm_enabled)
            self.assertNotIn("FAKE_ENROLLMENT_PASSWORD", repr(reply.sent))
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        await self.input("/确认")
        self.assertEqual(
            self.decoded(), {"username": "private-account", "password": password}
        )
        stored = json.dumps(self.backend.values, ensure_ascii=False)
        self.assertNotIn("FAKE_ENROLLMENT_PASSWORD", stored)
        self.assertNotIn("private-account", stored)
        self.assertFalse(self.plugin.enrollment.active(self.identity))
        status = FakeEvent()
        await self.plugin.checkin(status, "我的")
        self.assertIn("tianyi_cloud", status.sent[0].text)
        self.assertNotIn("FAKE_ENROLLMENT_PASSWORD", status.sent[0].text)
        self.assertNotIn("v1.", status.sent[0].text)

    async def test_same_qq_on_different_instances_is_isolated(self):
        await self.start()
        other = await self.input("not-our-input", platform_id="other-bot")
        self.assertFalse(other.stopped)
        await self.start(platform_id="other-bot")
        for platform, password in (
            ("bot-instance", "first-secret"),
            ("other-bot", "second-secret"),
        ):
            for text in ("account", password, "/确认"):
                await self.input(text, platform_id=platform)
        self.assertEqual(self.decoded()["password"], "first-secret")
        self.assertEqual(
            self.decoded(self.permissions.Identity("other-bot", "10001"))["password"],
            "second-secret",
        )
        status = FakeEvent(sender_id="10002")
        await self.plugin.checkin(status, "状态", "tianyi_cloud")
        self.assertNotIn("tianyi_cloud", status.sent[0].text)

    async def test_cancel_and_literal_control_values(self):
        await self.start()
        await self.input("/值 /取消")
        await self.input("/值 /值 password")
        await self.input("/确认")
        self.assertEqual(
            self.decoded(), {"username": "/取消", "password": "/值 password"}
        )
        await self.start()
        await self.input("discarded-account")
        await self.input("/取消")
        self.assertEqual(self.decoded()["username"], "/取消")
        self.assertFalse(self.plugin.enrollment.active(self.identity))
        normal = await self.input("ordinary-chat")
        self.assertFalse(normal.stopped)
        self.assertTrue(normal.llm_enabled)

    async def test_timeout_and_reload_keep_late_credentials_out_of_llm(self):
        self.plugin.enrollment.timeout_seconds = 0
        await self.start()
        owner_key = self.crypto.user_key(self.identity)
        self.assertEqual(
            self.plugin.state_store.snapshot()["users"][owner_key]["enrollment"], {}
        )
        late = await self.input("late-sensitive-value")
        self.assertTrue(late.stopped)
        self.assertFalse(late.llm_enabled)
        await self.plugin.terminate()
        self.plugin = await self.new_plugin()
        self.assertEqual(self.plugin.enrollment.sessions, {})
        self.assertEqual(
            self.plugin.state_store.snapshot()["users"][owner_key]["enrollment"], {}
        )
        self.assertTrue(self.plugin.enrollment.active(self.identity))
        after_reload = await self.input("another-late-sensitive-value")
        self.assertTrue(after_reload.stopped)
        self.assertFalse(after_reload.llm_enabled)
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        await self.input("/取消")
        self.assertFalse(self.plugin.enrollment.active(self.identity))
        self.assertNotIn(
            "enrollment", self.plugin.state_store.snapshot()["users"][owner_key]
        )
        normal = await self.input("ordinary-chat")
        self.assertFalse(normal.stopped)
        self.assertTrue(normal.llm_enabled)

    async def test_revocation_or_task_change_clears_uncommitted_values(self):
        await self.start()
        await self.input("account")
        self.config["whitelist_qqs"] = []
        denied = await self.input("revoked-password")
        self.assertTrue(denied.stopped)
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        await self.input("/取消")
        self.config["whitelist_qqs"] = ["10001"]
        await self.start()
        await self.input("account")
        self.config["timezone"] = "UTC"
        await self.input("stale-password")
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})

    async def test_confirmation_rechecks_task_after_waiting_for_state_lock(self):
        await self.start()
        await self.input("account")
        await self.input("password")
        async with self.plugin.state_store._lock:
            saving = asyncio.create_task(self.input("/确认"))
            await asyncio.sleep(0)
            self.config["timezone"] = "UTC"
        await saving
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        self.assertTrue(self.plugin.enrollment.active(self.identity))

    async def test_failed_intent_and_final_write_do_not_acknowledge_success(self):
        self.backend.fail_next_put()
        await self.start()
        blocked = await self.input("must-not-be-accepted")
        self.assertTrue(blocked.stopped)
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        await self.plugin.state_store.retry_pending()
        await self.input("/取消")
        await self.start()
        await self.input("private-account")
        await self.input("private-password")
        self.backend.fail_next_put()
        result = await self.input("/确认")
        self.assertEqual(
            result.sent[0].text,
            load_module("enrollment", "_enrollment_tests").UNCONFIRMED,
        )
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        await self.plugin.state_store.retry_pending()
        self.assertEqual(self.decoded()["password"], "private-password")

    async def test_missing_key_does_not_disable_help_and_creates_no_intent(self):
        os.environ.pop("CLOUD_DRIVE_CHECKIN_MASTER_KEY")
        writes = self.backend.put_attempts
        await self.start()
        self.assertFalse(self.plugin.enrollment.active(self.identity))
        help_event = FakeEvent()
        await self.plugin.checkin(help_event)
        self.assertEqual(len(help_event.sent), 1)
        self.assertEqual(self.backend.put_attempts, writes)

    async def test_non_text_or_oversize_input_does_not_advance_field(self):
        await self.start()
        image = FakeEvent()
        image.message_obj.raw_message["message"] = [
            {"type": "image", "data": {"url": "not-fetched"}}
        ]
        await self.plugin.capture_enrollment(image)
        self.assertTrue(image.stopped)
        await self.input("x" * (16 * 1024 + 1))
        self.assertEqual(self.plugin.enrollment.sessions[self.identity].index, 0)
        self.assertEqual(self.plugin.enrollment.sessions[self.identity].values, {})

    async def test_typed_optional_fields_and_whitespace_only_string(self):
        from pathlib import Path

        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text()
        )
        task = schema["tasks"]["default"][0]
        task.update(
            task_id="typed", input_adapter="direct_env", result_adapter="json_protocol"
        )
        task["dependency_bundle"] = "none"
        task["user_fields"] = [
            {
                "field_id": name,
                "label": name,
                "env_name": name.upper(),
                "value_type": kind,
                "required": required,
                "secret": True,
            }
            for name, kind, required in (
                ("account", "string", True),
                ("flag", "bool", True),
                ("count", "int", True),
                ("note", "string", False),
            )
        ]
        self.config["tasks"] = [task]
        await self.plugin.checkin(FakeEvent(), "录入", "typed")
        await self.input("   ")
        await self.input("yes")
        self.assertEqual(self.plugin.enrollment.sessions[self.identity].index, 1)
        await self.input("false")
        await self.input(" 2")
        self.assertEqual(self.plugin.enrollment.sessions[self.identity].index, 2)
        for text in ("-7", "/跳过", "/确认"):
            await self.input(text)
        record = self.plugin.state_store.snapshot()["credentials"][
            self.crypto.binding_key(self.identity, "typed")
        ]
        values = self.crypto.CredentialCipher(self.key).decrypt(
            record["ciphertext"],
            identity=self.identity,
            task_id="typed",
            task_version=record["task_version"],
        )
        self.assertEqual(values, {"account": "   ", "flag": False, "count": -7})

    async def test_another_pending_write_does_not_destroy_collected_values(self):
        await self.start()
        await self.input("preserved-account")
        gate = self.backend.block_next_put()
        other_write = asyncio.create_task(
            self.plugin.state_store.mutate(lambda snapshot: None)
        )
        await gate.started.wait()
        try:
            waiting = await self.input("not-yet-accepted-password")
            self.assertTrue(waiting.stopped)
            self.assertEqual(
                self.plugin.enrollment.sessions[self.identity].values,
                {"username": "preserved-account"},
            )
        finally:
            gate.release.set()
            await other_write
        await self.input("accepted-password")
        await self.input("/确认")
        self.assertEqual(
            self.decoded(),
            {"username": "preserved-account", "password": "accepted-password"},
        )

    async def test_capacity_rejection_is_not_an_unconfirmed_commit(self):
        await self.start()
        await self.input("account")
        await self.input("password")
        baseline = self.plugin.state_store.snapshot()
        self.plugin.state_store._max_snapshot_bytes = len(
            json.dumps(baseline, ensure_ascii=False, separators=(",", ":")).encode()
        )
        reply = await self.input("/确认")
        self.assertFalse(self.plugin.state_store.has_pending)
        self.assertEqual(self.plugin.state_store.snapshot(), baseline)
        self.assertNotEqual(
            reply.sent[0].text,
            load_module("enrollment", "_enrollment_tests").UNCONFIRMED,
        )
        await self.input("/取消")
        self.assertFalse(self.plugin.enrollment.active(self.identity))

    async def test_untrusted_context_cannot_start_or_feed_credentials(self):
        for change in (
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
            event = await self.start(astrbot_admin=True, **change)
            self.assertEqual(event.sent, [])
        self.assertEqual(self.plugin.state_store.snapshot()["users"], {})


if __name__ == "__main__":
    unittest.main()
