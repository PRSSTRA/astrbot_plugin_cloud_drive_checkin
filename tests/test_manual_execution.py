import asyncio
import base64
import copy
import hashlib
import os
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    FakeEvent,
    patch_astrbot_modules,
)
from neo_support import FakeBay
from state_support import load_module
from test_artifact_service import MockPyPI, SyntheticArtifactRepository

_FAKE_PASSWORD = 'FAKE quote" semicolon; spaces  \nsecond line'


class ExecutionFixture(unittest.IsolatedAsyncioTestCase):
    host_platforms_ready = True

    @classmethod
    def setUpClass(cls):
        cls.astrbot = patch_astrbot_modules()
        cls.astrbot.start()
        package = "_manual_execution_tests"
        cls.main = load_module("main", package)
        cls.credentials = load_module("credentials", package)
        cls.permissions = load_module("permissions", package)
        cls.preparation = load_module("preparation", package)

    @classmethod
    def tearDownClass(cls):
        cls.astrbot.stop()

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="manual-execution-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = SyntheticArtifactRepository(self.base / "repository")
        entry = self.repository.root / "runners/tianyi_entry.py"
        entry.write_text(
            "import os,json,hashlib\n"
            "accounts=json.loads(os.environ['TY_ACCOUNTS'])\n"
            "assert len(accounts)==1\n"
            "account=accounts[0]\n"
            f"valid=hashlib.sha256(account['password'].encode()).hexdigest()=={hashlib.sha256(_FAKE_PASSWORD.encode()).hexdigest()!r}\n"
            "status=('already_signed' if account['username']=='second' else 'success') if valid else 'failed'\n"
            "print(json.dumps({'status':status}))\n",
            encoding="utf-8",
        )
        self.bay = FakeBay()
        self.addCleanup(self.bay.close)
        environment = mock.patch.dict(
            os.environ,
            {
                "CLOUD_DRIVE_CHECKIN_MASTER_KEY": base64.urlsafe_b64encode(
                    b"k" * 32
                ).decode(),
                "MANUAL_TEST_NEO_KEY": self.bay.token,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        root_patch = mock.patch.object(self.preparation, "_ROOT", self.repository.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        task = copy.deepcopy(self.repository.raw_task)
        task.update(enabled=True, timeout_seconds=2)
        self.config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001", "10002"],
            tasks=[task],
            manual_cooldown_seconds=0,
            user_interval={"mode": "none"},
            task_interval={"mode": "none"},
            neo_connection={
                "enabled": True,
                "docker_cleanup_confirmed": True,
                "endpoint": "http://fake.bay.invalid",
                "access_key_env": "MANUAL_TEST_NEO_KEY",
            },
        )
        self.context = AstrBotContext()
        self.context.config["admins_id"] = ["90001"]
        if self.host_platforms_ready:
            self.context.platforms.append(
                SimpleNamespace(
                    meta=lambda: SimpleNamespace(id="other-bot", name="aiocqhttp")
                )
            )
        else:
            self.context.platforms.clear()
        client_type = self.main.NeoClient
        client_patch = mock.patch.object(
            self.main,
            "NeoClient",
            side_effect=lambda connection: client_type(
                connection, transport=httpx.MockTransport(self.bay)
            ),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        directory_patch = mock.patch.object(
            self.main.StarTools, "get_data_dir", return_value=self.base / "cache"
        )
        directory_patch.start()
        self.addCleanup(directory_patch.stop)
        if not hasattr(self, "clock"):
            scheduler = load_module("scheduler", "_manual_execution_tests")
            clock_patch = mock.patch.object(
                scheduler, "_utc_now", return_value=datetime(2026, 1, 1, tzinfo=UTC)
            )
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        self.plugin = self.main.CloudDriveCheckin(self.context, self.config)
        self.plugin.plugin_id = "test/manual-execution"
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        remote = MockPyPI(self.repository)
        self.pypi_client = remote.client()
        self.addAsyncCleanup(self.pypi_client.aclose)
        self.plugin.preparation = self.main.PreparationService(
            self.plugin.state_store, self.base / "cache", client=self.pypi_client
        )
        self.addAsyncCleanup(self.plugin.preparation.close)
        self.task = self.main.parse_task_catalog(self.config).tasks[0]
        await self.plugin.preparation.prepare(self.task, guard=lambda: None)
        self.backend = self.context.kv_for(self.plugin.plugin_id)

    async def bind(
        self,
        qq="10001",
        platform="bot-instance",
        username="first",
        password=_FAKE_PASSWORD,
    ):
        identity = self.permissions.Identity(platform, qq)
        await self.credentials.save_binding(
            self.plugin.state_store,
            self.credentials.CredentialCipher.from_environment(),
            identity=identity,
            origin=f"{platform}:FriendMessage:{qq}",
            task=self.task,
            values={"username": username, "password": password},
            guard=lambda: None,
        )
        return identity

    async def submit(self, qq="10001", platform="bot-instance", **kwargs):
        event = FakeEvent(sender_id=qq, platform_id=platform, **kwargs)
        await self.plugin.checkin(event, "立即", self.task.task_id)
        match = re.search(r"[0-9a-f]{32}$", event.sent[-1].text)
        self.assertIsNotNone(match, event.sent[-1].text)
        return match.group(), event

    async def until(self, predicate):
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.01)

    def snapshot(self):
        return self.plugin.state_store.snapshot()

    async def finished(self, run_id):
        await self.until(
            lambda: self.snapshot()["batches"][run_id]["status"] == "finished"
        )
        await self.until(
            lambda: (
                self.snapshot()["outbox"][run_id]["phase"]
                in {"sent", "failed", "unknown"}
            )
        )
        return self.snapshot()["batches"][run_id]


class ColdStartReadinessTests(ExecutionFixture):
    host_platforms_ready = False

    async def test_queued_identity_waits_through_empty_and_partial_platforms(self):
        self.context.platforms.append(
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="bot-instance", name="aiocqhttp")
            )
        )
        await self.bind()
        run_id, _ = await self.submit()
        self.context.platforms.clear()
        self.plugin.queue.wake()
        await asyncio.sleep(0.05)

        self.assertEqual(self.snapshot()["batches"][run_id]["status"], "queued")
        self.assertEqual(self.snapshot()["outbox"][run_id]["phase"], "reserved")
        self.assertEqual(self.bay.execution_calls, 0)

        self.context.platforms.append(
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="bot-instance", name="aiocqhttp")
            )
        )
        self.plugin.queue.wake()
        await asyncio.sleep(0.05)

        self.assertEqual(self.snapshot()["batches"][run_id]["status"], "queued")
        self.assertEqual(self.bay.execution_calls, 0)

        await self.plugin.on_astrbot_loaded()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "success")
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_late_webchat_marker_is_cached_without_authorizing_qq(self):
        self.context.platforms.append(
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="bot-instance", name="aiocqhttp")
            )
        )
        await self.bind()
        run_id, _ = await self.submit()
        workers = (
            self.plugin.notifications._worker,
            self.plugin.queue._worker,
            self.plugin.scheduler._worker,
        )
        self.context.platforms.append(
            SimpleNamespace(meta=lambda: SimpleNamespace(id="webchat", name="webchat"))
        )
        self.assertTrue(self.plugin._startup_ready())
        self.context.platforms.clear()
        self.assertTrue(self.plugin._startup_ready())

        await self.plugin.on_astrbot_loaded()
        await self.plugin.on_astrbot_loaded()
        self.assertEqual(
            workers,
            (
                self.plugin.notifications._worker,
                self.plugin.queue._worker,
                self.plugin.scheduler._worker,
            ),
        )

        await self.until(
            lambda: self.snapshot()["batches"][run_id]["status"] == "finished"
        )
        self.assertEqual(
            self.snapshot()["batches"][run_id]["result"]["queue_reason"],
            "permission_revoked",
        )
        self.assertEqual(self.bay.execution_calls, 0)

    async def test_global_disablement_still_settles_after_startup_ready(self):
        self.context.platforms.append(
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="bot-instance", name="aiocqhttp")
            )
        )
        await self.bind()
        run_id, _ = await self.submit()
        self.config["enabled"] = False

        await self.plugin.on_astrbot_loaded()
        await self.until(
            lambda: self.snapshot()["batches"][run_id]["status"] == "finished"
        )

        self.assertEqual(
            self.snapshot()["batches"][run_id]["result"]["queue_reason"],
            "service_disabled",
        )
        self.assertEqual(self.bay.execution_calls, 0)

    async def test_ready_marker_survives_same_context_reload(self):
        await self.plugin.on_astrbot_loaded()
        await self.plugin.terminate()
        successor = self.main.CloudDriveCheckin(self.context, self.config)
        successor.plugin_id = self.plugin.plugin_id
        await successor.initialize()
        self.addAsyncCleanup(successor.terminate)

        self.assertTrue(successor._startup_ready())
        self.assertIsNotNone(successor.notifications._worker)
        self.assertIsNotNone(successor.queue._worker)
        self.assertIsNotNone(successor.scheduler._worker)

    async def test_loaded_hook_during_shutdown_cannot_start_cold_work(self):
        self.context.platforms.append(
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="bot-instance", name="aiocqhttp")
            )
        )
        await self.bind()
        run_id, _ = await self.submit()

        stopping = asyncio.create_task(self.plugin.terminate())
        await asyncio.sleep(0)
        self.assertTrue(self.plugin._terminating)
        await self.plugin.on_astrbot_loaded()
        await stopping

        self.assertEqual(self.bay.execution_calls, 0)
        self.assertNotEqual(self.snapshot()["batches"][run_id]["status"], "running")


class ManualExecutionTests(ExecutionFixture):
    async def test_two_users_share_fifo_deduplicate_and_receive_isolated_terminals(
        self,
    ):
        await self.bind()
        await self.bind("10002", "other-bot", "second")
        self.bay.hold_execution = True
        first, first_event = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        duplicate, _ = await self.submit()
        second, _ = await self.submit("10002", "other-bot")
        self.assertEqual(first, duplicate)
        self.assertNotEqual(first, second)
        self.assertEqual(self.bay.execution_calls, 1)
        self.bay.release_execution.set()
        result1, result2 = await asyncio.gather(
            self.finished(first), self.finished(second)
        )
        self.assertEqual(result1["result"]["business_status"], "success")
        self.assertEqual(result2["result"]["business_status"], "already_signed")
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(self.bay.max_executions, 1)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.cargos, {})
        self.assertEqual(self.bay.sandboxes, {})
        messages = dict(self.context.sent)
        self.assertIn(first, messages["bot-instance:FriendMessage:10001"])
        self.assertNotIn(second, messages["bot-instance:FriendMessage:10001"])
        self.assertIn(second, messages["other-bot:FriendMessage:10002"])
        self.assertNotIn(_FAKE_PASSWORD, str(self.snapshot()))
        self.assertNotIn(_FAKE_PASSWORD, str(self.bay.history))
        self.assertNotIn(_FAKE_PASSWORD, str(self.context.sent) + str(first_event.sent))
        status = FakeEvent()
        await self.plugin.checkin(status, "状态", self.task.task_id)
        self.assertIn(first, status.sent[-1].text)
        self.assertNotIn(second, status.sent[-1].text)

    async def test_latest_committed_credentials_are_used_when_queue_starts(self):
        await self.bind()
        await self.bind("10002", username="second", password="OLD_FAKE_PASSWORD")
        self.bay.hold_execution = True
        first, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        second, _ = await self.submit("10002")
        await self.bind("10002", username="second")
        self.bay.release_execution.set()
        await self.finished(first)
        result = await self.finished(second)
        self.assertEqual(result["result"]["business_status"], "already_signed")

    async def test_pause_cancels_queued_item_and_revocation_interrupts_active_without_replay(
        self,
    ):
        await self.bind()
        await self.bind("10002", username="second")
        self.bay.hold_execution = True
        first, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        second, _ = await self.submit("10002")
        await self.plugin.checkin(
            FakeEvent(sender_id="10002"), "暂停", self.task.task_id
        )
        self.config["whitelist_qqs"] = ["10002"]
        self.plugin.queue.wake()
        result1 = await self.finished(first)
        result2 = await self.finished(second)
        self.assertEqual(result1["result"]["business_status"], "unknown")
        self.assertEqual(result2["result"]["business_status"], "cancelled")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})

    async def test_kv_failure_during_admission_never_allocates(self):
        await self.bind()
        self.backend.fail_next_put()
        event = FakeEvent()
        await self.plugin.checkin(event, "立即", self.task.task_id)
        self.assertFalse(self.plugin.state_store.ready)
        self.assertEqual(self.bay.create_calls, 0)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertNotIn(_FAKE_PASSWORD, event.sent[-1].text)

    async def test_delivery_exception_does_not_repeat_the_business(self):
        await self.bind()
        sent = []

        async def uncertain_send(origin, message):
            sent.append((origin, message.get_plain_text()))
            raise OSError("FAKE uncertain OneBot response")

        self.context.send_message = uncertain_send
        run_id, _ = await self.submit()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "success")
        self.assertEqual(self.snapshot()["outbox"][run_id]["phase"], "unknown")
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_shutdown_interruption_is_terminal_and_reload_does_not_rerun(self):
        await self.bind()
        self.bay.hold_execution = True
        run_id, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        await self.plugin.terminate()
        self.assertTrue(self.plugin.state_store.closed)
        replacement = self.main.CloudDriveCheckin(self.context, self.config)
        replacement.plugin_id = self.plugin.plugin_id
        await replacement.initialize()
        self.addAsyncCleanup(replacement.terminate)
        self.plugin = replacement
        self.bay.release_execution.set()
        self.plugin.queue.wake()
        await self.finished(run_id)
        self.assertEqual(
            self.snapshot()["batches"][run_id]["result"]["business_status"], "unknown"
        )
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_nonprivate_or_unbound_users_cannot_execute(self):
        await self.bind()
        events = [
            FakeEvent(platform_name="telegram", astrbot_admin=True),
            FakeEvent(raw_message={"message_type": "group"}, astrbot_admin=True),
            FakeEvent(sender_id="10002"),
        ]
        for event in events:
            await self.plugin.checkin(event, "立即", self.task.task_id)
        self.assertEqual(self.bay.create_calls, 0)
        self.assertEqual(self.snapshot()["batches"], {})

    async def before_credential_transfer(self, change):
        identity = await self.bind()
        self.config["manual_cooldown_seconds"] = 60
        reached, release = asyncio.Event(), asyncio.Event()
        client = self.plugin.executor.client
        original_upload = client.upload

        async def gated_upload(sandbox, path, payload):
            await original_upload(sandbox, path, payload)
            if path == "checkin/script.py":
                reached.set()
                await release.wait()

        queue_module = load_module("execution_queue", "_manual_execution_tests")
        with (
            mock.patch.object(client, "upload", new=gated_upload),
            mock.patch.object(queue_module, "_POLL_SECONDS", 5.0),
        ):
            run_id, _ = await self.submit()
            await asyncio.wait_for(reached.wait(), 5)
            await change()
            release.set()
            result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "cancelled")
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.cargos, {})
        self.assertEqual(self.bay.sandboxes, {})
        self.assertEqual(
            (
                "checkin/credentials.json" in self.bay.upload_paths,
                "checkin:global_spacing" in self.snapshot()["cooldowns"],
                "manual:" + self.credentials.binding_key(identity, self.task.task_id)
                in self.snapshot()["cooldowns"],
            ),
            (False, False, False),
        )

    async def test_pause_before_secret_upload_neither_transfers_nor_consumes_cooldown(
        self,
    ):
        async def pause():
            await self.plugin.checkin(FakeEvent(), "暂停", self.task.task_id)

        await self.before_credential_transfer(pause)

    async def test_replacement_before_secret_upload_neither_transfers_nor_consumes_cooldown(
        self,
    ):
        async def replace():
            await self.bind(password="NEW_FAKE_PASSWORD")

        await self.before_credential_transfer(replace)

    async def test_shutdown_without_reload_delivers_active_run_terminal(self):
        await self.bind()
        self.bay.hold_execution = True
        run_id, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        await self.plugin.terminate()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["batches"][run_id]["status"], "finished")
        self.assertEqual(
            snapshot["batches"][run_id]["result"]["business_status"], "unknown"
        )
        self.assertEqual(snapshot["outbox"][run_id]["phase"], "sent")
        self.assertEqual(sum(run_id in message for _, message in self.context.sent), 1)
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertTrue(self.plugin.executor.quiescent)
        self.assertTrue(self.plugin.state_store.closed)

    async def test_unconfirmed_pause_stops_secret_upload_before_poll(self):
        await self.bind()
        reached, release = asyncio.Event(), asyncio.Event()
        client = self.plugin.executor.client
        original_upload = client.upload

        async def gated_upload(sandbox, path, payload):
            await original_upload(sandbox, path, payload)
            if path == "checkin/script.py":
                reached.set()
                await release.wait()

        queue_module = load_module("execution_queue", "_manual_execution_tests")
        with (
            mock.patch.object(client, "upload", new=gated_upload),
            mock.patch.object(queue_module, "_POLL_SECONDS", 5.0),
        ):
            await self.submit()
            await asyncio.wait_for(reached.wait(), 5)
            self.backend.fail_next_put()
            await self.plugin.checkin(FakeEvent(), "暂停", self.task.task_id)
            self.assertTrue(self.plugin.state_store.has_pending)
            release.set()
            await self.until(lambda: not self.bay.cargos)
            self.assertNotIn("checkin/credentials.json", self.bay.upload_paths)
            self.assertEqual(self.bay.execution_calls, 0)
            self.assertEqual(self.bay.sandboxes, {})
            self.assertTrue(self.plugin.state_store.has_pending)


if __name__ == "__main__":
    unittest.main()
