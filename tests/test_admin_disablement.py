import asyncio
import copy
from datetime import UTC, datetime
from unittest import mock

from astrbot_support import AstrBotConfig, FakeEvent
from test_manual_execution import ExecutionFixture


class AdminDisablementTests(ExecutionFixture):
    async def disable(self, task_id=None, event=None):
        event = event or FakeEvent(sender_id="90001", astrbot_admin=True)
        await self.plugin.checkin(
            event,
            "管理",
            "停用",
            self.task.task_id if task_id is None else task_id,
        )
        return event

    def intent(self, task_id=None):
        task_id = self.task.task_id if task_id is None else task_id
        return self.snapshot()["dedupe"].get(f"task_disable:{task_id}")

    @staticmethod
    def raw_task(config, task_id):
        return next(task for task in config["tasks"] if task["task_id"] == task_id)

    async def bind_task(
        self,
        task,
        qq="10001",
        platform="bot-instance",
        username="first",
    ):
        original = self.task
        self.task = task
        try:
            return await self.bind(qq, platform, username)
        finally:
            self.task = original

    async def submit_task(self, task, qq="10001", platform="bot-instance"):
        original = self.task
        self.task = task
        try:
            return await self.submit(qq, platform)
        finally:
            self.task = original

    async def add_second_task(self):
        raw = copy.deepcopy(self.config["tasks"][0])
        raw.update(task_id="other_tianyi", name="另一个天翼任务")
        self.config["tasks"].append(raw)
        task = next(
            task
            for task in self.main.parse_task_catalog(self.config).tasks
            if task.task_id == raw["task_id"]
        )
        await self.plugin.preparation.prepare(task, guard=lambda: None)
        return task

    async def test_only_current_ordinary_private_administrators_may_disable(self):
        before = self.snapshot()
        rejected = [
            FakeEvent(sender_id="10002"),
            FakeEvent(sender_id="99999"),
            FakeEvent(sender_id="90001"),
            FakeEvent(
                sender_id="90001",
                astrbot_admin=True,
                platform_name="telegram",
            ),
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
        for event in rejected:
            await self.disable(event=event)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.config.saved_configs, [])
            self.assertTrue(self.config["tasks"][0]["enabled"])

        # A stale event role is insufficient once the framework list changes.
        self.context.config["admins_id"] = []
        await self.disable()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.config.saved_configs, [])

        self.config["plugin_admin_qqs"] = ["90002"]
        await self.disable(event=FakeEvent(sender_id="90002"))
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertFalse(self.config.persisted_config["tasks"][0]["enabled"])
        self.assertIsNone(self.intent())
        self.assertEqual(len(self.config.saved_configs), 1)

    async def test_revocation_while_waiting_for_state_lock_has_no_side_effect(self):
        before = self.snapshot()
        put_attempts = self.backend.put_attempts
        lock = self.plugin.state_store._lock
        async with lock:
            request = asyncio.create_task(self.disable())
            await self.until(lambda: bool(getattr(lock, "_waiters", ())))
            self.assertFalse(request.done())
            self.context.config["admins_id"] = []
        await request

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.put_attempts, put_attempts)
        self.assertEqual(self.config.saved_configs, [])
        self.assertTrue(self.config["tasks"][0]["enabled"])

    async def test_unconfirmed_kv_write_never_starts_config_save(self):
        unexpected_save = AssertionError("config save must not run after uncertain KV")
        self.config.save_actions.append(unexpected_save)
        self.backend.fail_next_put()

        await self.disable()

        self.assertTrue(self.plugin.state_store.has_pending)
        self.assertEqual(list(self.config.save_actions), [unexpected_save])
        self.assertEqual(self.config.saved_configs, [])
        self.assertIsNone(self.config.persisted_config)
        self.assertTrue(self.config["tasks"][0]["enabled"])
        self.assertIsNone(self.intent())

        await self.plugin.state_store.retry_pending()
        self.assertEqual(
            self.intent(),
            {
                "kind": "task_disable_intent",
                "task_id": self.task.task_id,
            },
        )
        self.assertTrue(self.config["tasks"][0]["enabled"])
        self.assertIs(self.config.save_actions.popleft(), unexpected_save)

    async def test_config_is_read_after_kv_commit_and_other_settings_are_preserved(
        self,
    ):
        gate = self.backend.block_next_put()
        self.addCleanup(gate.release.set)
        request = asyncio.create_task(self.disable())
        await asyncio.wait_for(gate.started.wait(), 3)

        self.config["operator_notes"] = {"nested": ["在 KV 等待期间修改"]}
        self.config["whitelist_qqs"].append("10003")
        self.config["tasks"][0]["__template_key"] = "keep-task-template"
        gate.release.set()
        await request

        expected = copy.deepcopy(dict(self.config))
        self.assertFalse(expected["tasks"][0]["enabled"])
        self.assertEqual(self.config.persisted_config, expected)
        self.assertEqual(self.config.saved_configs, [expected])
        self.assertIsNone(self.intent())

    async def test_disable_cancels_running_and_queued_target_but_other_task_runs(self):
        self.config["operator_notes"] = {"preserve": [1, 2, 3]}
        other_task = await self.add_second_task()
        await self.bind_task(self.task)
        await self.bind_task(
            self.task,
            qq="10002",
            platform="other-bot",
            username="second",
        )
        await self.bind_task(other_task)
        preserved_bindings = self.snapshot()["bindings"]
        preserved_credentials = self.snapshot()["credentials"]

        self.bay.hold_execution = True
        self.addCleanup(self.bay.release_execution.set)
        running, _ = await self.submit_task(self.task)
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        queued, _ = await self.submit_task(self.task, qq="10002", platform="other-bot")
        unrelated, _ = await self.submit_task(other_task)

        await self.disable()
        snapshot = self.snapshot()
        self.assertTrue(snapshot["batches"][running]["cancel_requested"])
        self.assertTrue(snapshot["batches"][queued]["cancel_requested"])
        self.assertNotIn("cancel_requested", snapshot["batches"][unrelated])
        self.assertIsNone(self.intent())

        running_result = await self.finished(running)
        self.bay.release_execution.set()
        queued_result, unrelated_result = await asyncio.gather(
            self.finished(queued), self.finished(unrelated)
        )

        self.assertEqual(running_result["result"]["queue_reason"], "admin_cancelled")
        self.assertEqual(
            (
                queued_result["result"]["business_status"],
                queued_result["result"]["queue_reason"],
            ),
            ("cancelled", "admin_cancelled"),
        )
        self.assertEqual(unrelated_result["result"]["business_status"], "success")
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(self.snapshot()["bindings"], preserved_bindings)
        self.assertEqual(self.snapshot()["credentials"], preserved_credentials)
        self.assertFalse(self.raw_task(self.config, self.task.task_id)["enabled"])
        self.assertTrue(self.raw_task(self.config, other_task.task_id)["enabled"])
        self.assertEqual(
            self.config.persisted_config["operator_notes"],
            {"preserve": [1, 2, 3]},
        )
        self.assertFalse(
            self.raw_task(self.config.persisted_config, self.task.task_id)["enabled"]
        )
        self.assertTrue(
            self.raw_task(self.config.persisted_config, other_task.task_id)["enabled"]
        )

    async def test_disable_during_preupload_blocks_credentials_and_business_request(
        self,
    ):
        await self.bind_task(self.task)
        preserved_bindings = self.snapshot()["bindings"]
        preserved_credentials = self.snapshot()["credentials"]
        reached = asyncio.Event()
        release = asyncio.Event()
        self.addCleanup(release.set)
        client = self.plugin.executor.client
        upload = client.upload

        async def gated_upload(sandbox, path, payload):
            await upload(sandbox, path, payload)
            if path == "checkin/script.py":
                reached.set()
                await release.wait()

        with mock.patch.object(client, "upload", new=gated_upload):
            run_id, _ = await self.submit_task(self.task)
            await asyncio.wait_for(reached.wait(), 5)
            await self.disable()
            result = await self.finished(run_id)

        self.assertEqual(
            (
                result["result"]["business_status"],
                result["result"]["queue_reason"],
            ),
            ("cancelled", "admin_cancelled"),
        )
        self.assertNotIn("checkin/credentials.json", self.bay.upload_paths)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.snapshot()["bindings"], preserved_bindings)
        self.assertEqual(self.snapshot()["credentials"], preserved_credentials)
        self.assertFalse(self.config.persisted_config["tasks"][0]["enabled"])
        self.assertIsNone(self.intent())

    async def test_failed_saves_recover_from_persisted_config_and_remain_fail_closed(
        self,
    ):
        await self.bind_task(self.task)
        self.config["operator_notes"] = {
            "preserve": {"credentials": False, "custom": ["值"]}
        }
        self.config["tasks"][0]["__template_key"] = "keep-task-template"
        await self.config.save_config_async()
        persisted_enabled = copy.deepcopy(self.config.persisted_config)
        preserved_bindings = self.snapshot()["bindings"]
        preserved_credentials = self.snapshot()["credentials"]
        expected_disabled = copy.deepcopy(persisted_enabled)
        expected_disabled["tasks"][0]["enabled"] = False

        self.config.save_actions.append(OSError("simulated config write failure"))
        await self.disable()
        self.assertEqual(
            self.intent(),
            {
                "kind": "task_disable_intent",
                "task_id": self.task.task_id,
            },
        )
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertEqual(self.config.persisted_config, persisted_enabled)

        # False is also unconfirmed, and a repeated command must retry the save.
        self.config["tasks"][0]["enabled"] = True
        self.config.save_actions.append(False)
        await self.disable()
        self.assertIsNotNone(self.intent())
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertEqual(self.config.persisted_config, persisted_enabled)
        self.assertEqual(self.config.saved_configs, [persisted_enabled])

        # Even an in-memory re-enable cannot bypass the durable pending intent.
        self.config["tasks"][0]["enabled"] = True
        before_rejections = self.snapshot()
        manual = FakeEvent(sender_id="10001")
        await self.plugin.checkin(manual, "立即", self.task.task_id)
        with self.assertRaises(self.main.QueueRejected) as caught:
            await self.plugin.queue.enqueue_scheduled(
                task=self.task,
                scheduled_at=datetime.now(UTC),
                requests=self.plugin.scheduler._requests(self.task, self.snapshot()),
            )
        self.assertEqual(caught.exception.reason, "task_disabled")
        self.assertEqual(self.snapshot(), before_rejections)
        self.assertEqual((self.bay.create_calls, self.bay.execution_calls), (0, 0))

        await self.plugin.terminate()
        successor_config = AstrBotConfig(**copy.deepcopy(self.config.persisted_config))
        successor = self.main.CloudDriveCheckin(self.context, successor_config)
        successor.plugin_id = self.plugin.plugin_id
        self.plugin = successor
        self.config = successor_config
        self.addAsyncCleanup(successor.terminate)
        await successor.initialize()
        self.task = self.main.parse_task_catalog(successor_config).tasks[0]

        self.assertEqual(dict(successor_config), expected_disabled)
        self.assertEqual(successor_config.persisted_config, expected_disabled)
        self.assertEqual(successor_config.saved_configs, [expected_disabled])
        self.assertIsNone(self.intent())
        self.assertEqual(self.snapshot()["bindings"], preserved_bindings)
        self.assertEqual(self.snapshot()["credentials"], preserved_credentials)

        recovered = self.snapshot()
        manual = FakeEvent(sender_id="10001")
        await successor.checkin(manual, "立即", self.task.task_id)
        with self.assertRaises(self.main.QueueRejected) as caught:
            await successor.queue.enqueue_scheduled(
                task=self.task,
                scheduled_at=datetime.now(UTC),
                requests=successor.scheduler._requests(self.task, self.snapshot()),
            )
        self.assertEqual(caught.exception.reason, "task_disabled")
        self.assertEqual(self.snapshot(), recovered)
        self.assertEqual((self.bay.create_calls, self.bay.execution_calls), (0, 0))


if __name__ == "__main__":
    import unittest

    unittest.main()
