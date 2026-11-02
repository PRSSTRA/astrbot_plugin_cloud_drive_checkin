import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from astrbot_support import FakeEvent
from state_support import load_module
from test_manual_execution import ExecutionFixture


class AdminCancellationTests(ExecutionFixture):
    async def cancel(self, run_id, event=None):
        event = event or FakeEvent(sender_id="90001", astrbot_admin=True)
        await self.plugin.checkin(event, "管理", "取消", run_id)
        return event

    async def parked(self):
        await self.bind()
        reached, release = asyncio.Event(), asyncio.Event()
        client = self.plugin.executor.client
        upload = client.upload

        async def gated_upload(sandbox, path, payload):
            await upload(sandbox, path, payload)
            if path == "checkin/script.py":
                reached.set()
                await release.wait()

        patcher = mock.patch.object(client, "upload", new=gated_upload)
        patcher.start()
        self.addCleanup(patcher.stop)
        queue = load_module("execution_queue", "_manual_execution_tests")
        polling = mock.patch.object(queue, "_POLL_SECONDS", 5.0)
        polling.start()
        self.addCleanup(polling.stop)
        self.addCleanup(release.set)
        run_id, _ = await self.submit()
        await asyncio.wait_for(reached.wait(), 5)
        return run_id, release

    async def test_only_current_ordinary_private_administrators_may_cancel(self):
        run_id, release = await self.parked()
        events = [
            FakeEvent(sender_id="10002"),
            FakeEvent(sender_id="99999"),
            FakeEvent(sender_id="90001", astrbot_admin=True, platform_name="telegram"),
            FakeEvent(
                sender_id="90001",
                astrbot_admin=True,
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": "90001",
                    "group_id": "20001",
                    "sender": {"user_id": "90001"},
                },
            ),
            FakeEvent(
                sender_id="90001",
                astrbot_admin=True,
                raw_message={
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "group",
                    "user_id": "90001",
                    "sender": {"user_id": "90001"},
                },
            ),
        ]
        for event in events:
            await self.cancel(run_id, event)
            self.assertNotIn("cancel_requested", self.snapshot()["batches"][run_id])
        # Even a stale event role must not bypass the current framework admin list.
        self.context.config["admins_id"] = []
        await self.cancel(run_id)
        self.assertNotIn("cancel_requested", self.snapshot()["batches"][run_id])
        # Additional plugin administrators work without the framework role.
        self.config["plugin_admin_qqs"] = ["90002"]
        await self.cancel(run_id, FakeEvent(sender_id="90002"))
        self.assertTrue(self.snapshot()["batches"][run_id]["cancel_requested"])
        release.set()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "cancelled")
        self.assertEqual(self.bay.execution_calls, 0)

    async def test_queued_cancel_only_removes_target_execution_not_binding(self):
        first_identity = await self.bind()
        second_identity = await self.bind("10002", "other-bot", "second")
        self.bay.hold_execution = True
        first, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        second, _ = await self.submit("10002", "other-bot")
        original_bindings = self.snapshot()["bindings"]
        await self.cancel(second)
        self.assertTrue(self.snapshot()["batches"][second]["cancel_requested"])
        self.bay.release_execution.set()
        result1, result2 = await asyncio.gather(
            self.finished(first), self.finished(second)
        )
        self.assertEqual(result1["result"]["business_status"], "success")
        self.assertEqual(result2["result"]["business_status"], "cancelled")
        self.assertEqual(result2["result"]["queue_reason"], "admin_cancelled")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(self.snapshot()["bindings"], original_bindings)
        self.assertEqual(
            [origin for origin, _ in self.context.sent],
            [
                f"{first_identity.platform_id}:FriendMessage:{first_identity.qq_id}",
                f"{second_identity.platform_id}:FriendMessage:{second_identity.qq_id}",
            ],
        )
        self.assertNotIn(second, self.snapshot()["runs"])

    async def test_cancel_before_secret_upload_works_with_disabled_invalid_task_config(
        self,
    ):
        run_id, release = await self.parked()
        self.config["enabled"] = False
        self.config["tasks"][0]["cron"] = "invalid cron"
        await self.cancel(run_id)
        self.assertTrue(self.snapshot()["batches"][run_id]["cancel_requested"])
        release.set()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "cancelled")
        self.assertNotIn("checkin/credentials.json", self.bay.upload_paths)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.snapshot()["cooldowns"], {})
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertEqual(list(Path(self.bay.root.name).iterdir()), [])

    async def test_cancel_after_business_request_keeps_unknown_and_cleans(self):
        await self.bind()
        self.bay.hold_execution = True
        run_id, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        await self.cancel(run_id)
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "unknown")
        self.assertEqual(result["result"]["queue_reason"], "admin_cancelled")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertEqual(list(Path(self.bay.root.name).iterdir()), [])
        self.assertEqual(len(self.context.sent), 1)

    async def test_known_success_during_cleanup_is_not_relabelled_cancelled(self):
        await self.bind()
        self.bay.hold_cleanup = True
        run_id, _ = await self.submit()
        await asyncio.wait_for(self.bay.cleanup_started.wait(), 5)
        await self.cancel(run_id)
        self.bay.release_cleanup.set()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "success")
        self.assertEqual(result["result"]["queue_reason"], "admin_cancelled")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(len(self.context.sent), 1)

    async def test_known_wrapper_config_error_survives_cancel_during_cleanup(self):
        await self.bind()
        self.bay.hold_cleanup = True
        client = self.plugin.executor.client
        upload = client.upload

        async def tampered_upload(sandbox, path, payload):
            await upload(sandbox, path, payload)
            if path == "checkin/script.py":
                script = Path(self.bay.root.name) / sandbox / path
                script.write_bytes(
                    script.read_bytes() + b"\n# simulated transfer corruption\n"
                )

        with mock.patch.object(client, "upload", new=tampered_upload):
            run_id, _ = await self.submit()
            await asyncio.wait_for(self.bay.cleanup_started.wait(), 5)
            await self.cancel(run_id)
            self.bay.release_cleanup.set()
            item = await self.finished(run_id)
        self.assertEqual(
            tuple(
                item["result"][key]
                for key in ("process_status", "business_status", "reason_code")
            ),
            ("start_error", "config_error", "runner_error"),
        )
        self.assertEqual(item["result"]["queue_reason"], "admin_cancelled")
        self.assertEqual(
            self.snapshot()["outbox"][run_id]["result"]["business_status"],
            "config_error",
        )
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(list(Path(self.bay.root.name).iterdir()), [])

    async def test_duplicate_unknown_and_finished_requests_do_not_write_or_echo_input(
        self,
    ):
        run_id, release = await self.parked()
        before = self.snapshot()
        event = await self.cancel("SECRET_INVALID_RUN_ID")
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("SECRET_INVALID_RUN_ID", str(event.sent))
        await self.cancel("f" * 32)
        self.assertEqual(self.snapshot(), before)
        await self.cancel(run_id)
        cancelled = self.snapshot()
        await self.cancel(run_id)
        self.assertEqual(self.snapshot(), cancelled)
        release.set()
        await self.finished(run_id)
        finished = self.snapshot()
        await self.cancel(run_id)
        self.assertEqual(self.snapshot(), finished)
        self.assertEqual(len(self.context.sent), 1)

    async def test_unconfirmed_cancel_blocks_upload_and_survives_recovery(self):
        run_id, release = await self.parked()
        self.backend.fail_next_put()
        await self.cancel(run_id)
        self.assertTrue(self.plugin.state_store.has_pending)
        self.assertNotIn("cancel_requested", self.snapshot()["batches"][run_id])
        release.set()
        await self.until(lambda: not self.bay.cargos)
        await self.plugin.executor.close()
        self.assertTrue(self.plugin.executor.quiescent)
        self.assertNotIn("checkin/credentials.json", self.bay.upload_paths)
        self.assertEqual(self.bay.execution_calls, 0)
        await self.plugin.state_store.retry_pending()
        self.assertTrue(self.snapshot()["batches"][run_id]["cancel_requested"])
        await self.plugin.terminate()
        successor = self.main.CloudDriveCheckin(self.context, self.config)
        successor.plugin_id = self.plugin.plugin_id
        self.plugin = successor
        self.addAsyncCleanup(successor.terminate)
        await successor.initialize()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "cancelled")
        self.assertEqual(result["result"]["queue_reason"], "admin_cancelled")
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertEqual(len(self.context.sent), 1)

    async def test_admin_revocation_while_waiting_for_state_lock_rejects_cancel(self):
        run_id, release = await self.parked()
        async with self.plugin.state_store._lock:
            request = asyncio.create_task(self.cancel(run_id))
            await asyncio.sleep(0)
            self.assertFalse(request.done())
            self.context.config["admins_id"] = []
        await request
        self.assertNotIn("cancel_requested", self.snapshot()["batches"][run_id])
        release.set()
        result = await self.finished(run_id)
        self.assertEqual(result["result"]["business_status"], "success")

    async def test_automatic_running_cancel_notifies_owner_without_manual_participation(
        self,
    ):
        await self.bind()
        self.bay.hold_execution = True
        result = await self.plugin.queue.enqueue_scheduled(
            task=self.task,
            scheduled_at=datetime.now(UTC),
            requests=self.plugin.scheduler._requests(self.task, self.snapshot()),
        )
        run_id = result["run_ids"][0]
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        await self.cancel(run_id)
        item = await self.finished(run_id)
        self.assertEqual(item["result"]["business_status"], "unknown")
        self.assertFalse(item["manual_requested"])
        self.assertEqual(self.snapshot()["outbox"][run_id]["source"], "automatic")
        self.assertEqual(
            [origin for origin, _ in self.context.sent],
            ["bot-instance:FriendMessage:10001"],
        )
        self.assertEqual(self.bay.execution_calls, 1)
