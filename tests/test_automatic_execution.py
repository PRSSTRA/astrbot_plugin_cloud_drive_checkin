import asyncio
import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from astrbot_support import FakeEvent
from state_support import load_module
from test_manual_execution import ExecutionFixture


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, 23, 9, 50, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class AutomaticExecutionTests(ExecutionFixture):
    async def asyncSetUp(self):
        self.clock = Clock()
        scheduler = load_module("scheduler", "_manual_execution_tests")
        queue = load_module("execution_queue", "_manual_execution_tests")
        for module in (scheduler, queue):
            patcher = mock.patch.object(module, "_utc_now", self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        await super().asyncSetUp()

    async def due(self):
        self.clock.advance(10)
        await self.plugin.scheduler.tick()

    def scheduled(self):
        return [
            record
            for record in self.snapshot()["dedupe"].values()
            if record.get("kind") == "scheduled_batch"
        ]

    async def bind_task(
        self,
        task,
        *,
        qq="10001",
        platform="bot-instance",
        username="status-user",
    ):
        identity = self.permissions.Identity(platform, qq)
        await self.credentials.save_binding(
            self.plugin.state_store,
            self.credentials.CredentialCipher.from_environment(),
            identity=identity,
            origin=f"{platform}:FriendMessage:{qq}",
            task=task,
            values={"username": username, "password": "STATUS_TEST_PASSWORD"},
            guard=lambda: None,
        )
        return identity

    async def personal_status(
        self, *, task_id=None, qq="10001", platform="bot-instance"
    ):
        event = FakeEvent(sender_id=qq, platform_id=platform)
        if task_id is None:
            await self.plugin.checkin(event, "我的")
        else:
            await self.plugin.checkin(event, "状态", task_id)
        self.assertTrue(event.sent)
        return event.sent[-1].text

    async def settled(self, run_ids):
        await self.until(
            lambda: all(
                self.snapshot()["outbox"][run_id]["phase"]
                in {"sent", "suppressed", "unknown", "failed"}
                for run_id in run_ids
            )
        )

    async def test_cron_batch_manual_merge_and_live_notify_preference(self):
        await self.bind()
        await self.bind("10002", "other-bot", "second")
        self.bay.hold_execution = True
        await self.due()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        items = sorted(
            self.snapshot()["batches"].values(), key=lambda item: item["sequence"]
        )
        self.assertEqual([item["qq_id"] for item in items], ["10001", "10002"])
        first, second = [item["run_id"] for item in items]
        duplicate, _ = await self.submit()
        self.assertEqual(duplicate, first)
        preference = FakeEvent(sender_id="10002", platform_id="other-bot")
        await self.plugin.checkin(preference, "通知", self.task.task_id, "开")
        self.bay.release_execution.set()
        await self.settled([first, second])
        snapshot = self.snapshot()
        self.assertTrue(snapshot["batches"][first]["manual_requested"])
        self.assertFalse(snapshot["batches"][second]["manual_requested"])
        self.assertEqual(
            [snapshot["outbox"][run]["phase"] for run in (first, second)],
            ["sent", "sent"],
        )
        self.assertEqual(
            [
                snapshot["batches"][run]["result"]["business_status"]
                for run in (first, second)
            ],
            ["success", "already_signed"],
        )
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(self.bay.max_executions, 1)
        self.assertEqual(
            [origin for origin, _ in self.context.sent],
            ["bot-instance:FriendMessage:10001", "other-bot:FriendMessage:10002"],
        )
        self.assertEqual(list(Path(self.bay.root.name).iterdir()), [])
        self.assertEqual(snapshot["cleanup_jobs"], {})
        await self.plugin.scheduler.tick()
        self.assertEqual(len(self.snapshot()["batches"]), 2)

    async def test_default_success_silent_but_failed_auto_run_notifies(self):
        await self.bind()
        await self.bind(
            "10002", "other-bot", "second", password="INVALID_FAKE_PASSWORD"
        )
        await self.due()
        runs = self.scheduled()[0]["run_ids"]
        await self.settled(runs)
        items = {item["qq_id"]: item for item in self.snapshot()["batches"].values()}
        self.assertEqual(items["10001"]["result"]["business_status"], "success")
        self.assertEqual(items["10002"]["result"]["business_status"], "failed")
        self.assertEqual(
            self.snapshot()["outbox"][items["10001"]["run_id"]]["phase"], "suppressed"
        )
        self.assertEqual(
            [origin for origin, _ in self.context.sent],
            ["other-bot:FriendMessage:10002"],
        )
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertFalse(
            any(key.startswith("manual:") for key in self.snapshot()["cooldowns"])
        )

    async def test_active_manual_task_skips_entire_automatic_batch(self):
        await self.bind()
        await self.bind("10002", "other-bot", "second")
        self.bay.hold_execution = True
        run, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        await self.due()
        self.assertEqual(self.scheduled()[0]["status"], "skipped_overlap")
        self.assertEqual(list(self.snapshot()["batches"]), [run])
        self.bay.release_execution.set()
        await self.finished(run)
        await self.plugin.scheduler.tick()
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_admission_commit_barrier_precedes_every_external_request(self):
        await self.bind()
        gate = self.backend.block_next_put()
        admission = asyncio.create_task(self.due())
        try:
            await asyncio.wait_for(gate.started.wait(), 3)
            self.assertFalse(self.plugin.state_store.ready)
            await self.plugin.scheduler.tick()
            self.assertEqual((self.bay.create_calls, self.bay.execution_calls), (0, 0))
            self.assertEqual(self.snapshot()["batches"], {})
        finally:
            gate.release.set()
            await admission
        await self.settled(self.scheduled()[0]["run_ids"])
        await self.plugin.scheduler.tick()
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_restart_inside_due_minute_does_not_catch_up(self):
        await self.bind()
        await self.due()
        await self.settled(self.scheduled()[0]["run_ids"])
        await self.plugin.terminate()
        self.clock.advance(2 * 86400 + 20)
        successor = self.main.CloudDriveCheckin(self.context, self.config)
        successor.plugin_id = self.plugin.plugin_id
        self.plugin = successor
        self.addAsyncCleanup(successor.terminate)
        await successor.initialize()
        await successor.scheduler.tick()
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(len(self.snapshot()["batches"]), 1)
        self.clock.now = self.main.next_run_times(
            self.task, "Asia/Shanghai", now=self.clock()
        )[0].astimezone(UTC)
        await successor.scheduler.tick()
        await self.settled(
            [item["run_id"] for item in self.snapshot()["batches"].values()]
        )
        self.assertEqual(self.bay.execution_calls, 2)

    async def test_disable_and_reenable_start_from_future_not_missed_slot(self):
        await self.bind()
        self.config["enabled"] = False
        await self.plugin.scheduler.tick()
        self.clock.advance(15)
        self.config["enabled"] = True
        await self.plugin.scheduler.tick()
        self.assertEqual(self.snapshot()["batches"], {})
        self.clock.now = self.main.next_run_times(
            self.task, "Asia/Shanghai", now=self.clock()
        )[0].astimezone(UTC)
        await self.plugin.scheduler.tick()
        await self.settled(self.scheduled()[0]["run_ids"])
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_paused_and_revoked_users_are_not_enqueued(self):
        await self.bind()
        await self.bind("10002", "other-bot", "second")
        await self.plugin.checkin(FakeEvent(), "暂停", self.task.task_id)
        self.config["whitelist_qqs"] = ["10001"]
        await self.due()
        self.assertEqual(self.scheduled()[0]["status"], "skipped_empty")
        self.assertEqual(self.snapshot()["batches"], {})
        self.assertEqual(self.context.sent, [])
        self.assertEqual(self.bay.create_calls, 0)

    async def test_missing_preparation_notifies_config_error_without_execution(self):
        await self.bind()
        await self.plugin.state_store.mutate(
            lambda snapshot: snapshot["prepared_artifacts"].clear()
        )
        await self.due()
        runs = self.scheduled()[0]["run_ids"]
        await self.settled(runs)
        self.assertEqual(
            self.snapshot()["batches"][runs[0]]["result"]["business_status"],
            "config_error",
        )
        self.assertEqual(
            [origin for origin, _ in self.context.sent],
            ["bot-instance:FriendMessage:10001"],
        )
        self.assertEqual(self.bay.create_calls, 0)

    async def test_clock_backtracking_does_not_replay_committed_occurrence(self):
        await self.bind()
        await self.due()
        await self.settled(self.scheduled()[0]["run_ids"])
        self.clock.advance(-30)
        await self.plugin.scheduler.tick()
        self.clock.advance(30)
        await self.plugin.scheduler.tick()
        self.assertEqual(len(self.snapshot()["batches"]), 1)
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_cron_sunday_alias_and_day_weekday_or_use_timezone(self):
        await self.bind()
        # Thursday Jan 1 is admitted by day-of-month, Sunday Jan 4 by weekday.
        self.clock.now = datetime(2025, 12, 31, 23, 9, 50, tzinfo=UTC)
        self.config["tasks"][0]["cron"] = "10 7 1 * 7"
        self.task = self.main.parse_task_catalog(self.config).tasks[0]
        await self.plugin.preparation.prepare(self.task, guard=lambda: None)
        await self.plugin.scheduler.tick()
        await self.due()
        await self.settled(self.scheduled()[0]["run_ids"])
        self.clock.now = datetime(2026, 1, 3, 23, 10, tzinfo=UTC)
        await self.plugin.scheduler.tick()
        await self.settled(
            [item["run_id"] for item in self.snapshot()["batches"].values()]
        )
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(
            sorted(record["scheduled_at"] for record in self.scheduled()),
            ["2025-12-31T23:10:00+00:00", "2026-01-03T23:10:00+00:00"],
        )

    async def test_schema_two_upgrade_resumes_only_unstarted_manual_work(self):
        await self.bind()
        await self.bind("10002", "other-bot", "second")
        self.bay.hold_execution = True
        first, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        second, _ = await self.submit("10002", "other-bot")
        await self.plugin.terminate()
        legacy = self.snapshot()
        legacy["schema_version"] = 2
        for item in legacy["batches"].values():
            item["kind"] = "manual_checkin"
            for field in ("source", "manual_requested", "scheduled_at", "batch_id"):
                item.pop(field)
        for record in legacy["outbox"].values():
            record["kind"] = "manual_terminal_notification"
            record.pop("source")
            record.pop("manual_requested")
        for key, record in tuple(legacy["dedupe"].items()):
            if record.get("kind") == "checkin":
                record["kind"] = "manual_checkin"
                legacy["dedupe"]["manual:" + key[len("binding:") :]] = legacy[
                    "dedupe"
                ].pop(key)
        for record in legacy["cooldowns"].values():
            record["kind"] = "manual_queue_cooldown"
        if "checkin:global_spacing" in legacy["cooldowns"]:
            legacy["cooldowns"]["manual:global_spacing"] = legacy["cooldowns"].pop(
                "checkin:global_spacing"
            )
        self.backend.values["checkin_state"] = legacy
        self.bay.hold_execution = False
        self.clock.advance(30)
        successor = self.main.CloudDriveCheckin(self.context, self.config)
        successor.plugin_id = self.plugin.plugin_id
        self.plugin = successor
        self.addAsyncCleanup(successor.terminate)
        await successor.initialize()
        await self.finished(second)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["schema_version"], 3)
        self.assertEqual(
            snapshot["batches"][first]["result"]["business_status"], "unknown"
        )
        self.assertEqual(
            snapshot["batches"][second]["result"]["business_status"], "already_signed"
        )
        self.assertTrue(snapshot["batches"][second]["manual_requested"])
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(len(self.context.sent), 2)
        self.assertEqual(snapshot["cleanup_jobs"], {})
        self.assertEqual(list(Path(self.bay.root.name).iterdir()), [])

    async def hourly_schedule(self):
        await self.bind()
        self.clock.now = datetime(2026, 11, 2, 9, 59, 50, tzinfo=UTC)
        self.config["tasks"][0]["cron"] = "0 * * * *"
        self.task = self.main.parse_task_catalog(self.config).tasks[0]
        await self.plugin.preparation.prepare(self.task, guard=lambda: None)
        await self.plugin.scheduler.tick()

    async def test_delayed_loop_admits_only_current_valid_cron_minute(self):
        await self.hourly_schedule()
        self.clock.now = datetime(2026, 11, 2, 11, 0, 20, tzinfo=UTC)
        await self.plugin.scheduler.tick()
        self.assertEqual(
            [record["scheduled_at"] for record in self.scheduled()],
            ["2026-11-02T11:00:00+00:00"],
        )
        await self.settled(self.scheduled()[0]["run_ids"])
        await self.plugin.scheduler.tick()
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_delayed_loop_outside_matching_minute_does_not_catch_up(self):
        await self.hourly_schedule()
        self.clock.now = datetime(2026, 11, 2, 11, 1, tzinfo=UTC)
        await self.plugin.scheduler.tick()
        self.assertEqual(self.snapshot()["batches"], {})
        self.clock.now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        await self.plugin.scheduler.tick()
        await self.settled(self.scheduled()[0]["run_ids"])
        self.assertEqual(
            [record["scheduled_at"] for record in self.scheduled()],
            ["2026-11-02T12:00:00+00:00"],
        )
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_personal_plan_view_is_owner_and_task_filtered_and_read_only(self):
        first_identity = await self.bind()
        secondary_raw = copy.deepcopy(self.config["tasks"][0])
        secondary_raw.update(
            task_id="secondary_cloud", name="第二云盘", cron="20 8 * * *"
        )
        self.config["tasks"].append(secondary_raw)
        secondary = next(
            task
            for task in self.main.parse_task_catalog(self.config).tasks
            if task.task_id == "secondary_cloud"
        )
        await self.bind_task(secondary)
        await self.bind("10002", "other-bot", "second")
        await self.plugin.scheduler.tick()

        before = self.snapshot()
        put_attempts = self.backend.put_attempts
        first_all = await self.personal_status()
        first_filtered = await self.personal_status(task_id="secondary_cloud")
        second_all = await self.personal_status(qq="10002", platform="other-bot")
        second_filtered = await self.personal_status(
            task_id="secondary_cloud", qq="10002", platform="other-bot"
        )

        self.assertIn("tianyi_cloud", first_all)
        self.assertIn("secondary_cloud", first_all)
        self.assertIn("2026-11-02T08:20+08:00", first_filtered)
        self.assertIn("secondary_cloud", first_filtered)
        self.assertNotIn("tianyi_cloud", first_filtered)
        self.assertIn("tianyi_cloud", second_all)
        self.assertNotIn("secondary_cloud", second_all)
        self.assertNotIn("secondary_cloud", second_filtered)
        self.assertEqual(
            [
                view.task_id
                for view in self.plugin.scheduler.binding_plan_views(first_identity)
            ],
            ["secondary_cloud", "tianyi_cloud"],
        )
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.put_attempts, put_attempts)
        self.assertEqual(self.snapshot()["batches"], {})

    async def test_plan_requires_current_version_clock_and_configured_timezone(self):
        identity = await self.bind()
        state = self.main.BindingPlanState

        initial = self.plugin.scheduler.binding_plan_views(identity)
        self.assertEqual([view.state for view in initial], [state.PLANNED])
        self.assertEqual(initial[0].timezone, "Asia/Shanghai")
        self.assertEqual(initial[0].planned_at.utcoffset(), timedelta(hours=8))
        initial_text = await self.personal_status(task_id=self.task.task_id)
        self.assertIn("Asia/Shanghai", initial_text)
        self.assertIn("+08:00", initial_text)

        self.config["timezone"] = "UTC"
        stale = self.plugin.scheduler.binding_plan_views(identity)
        self.assertEqual([view.state for view in stale], [state.PLAN_STALE])
        stale_text = await self.personal_status(task_id=self.task.task_id)
        self.assertNotRegex(stale_text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

        await self.plugin.scheduler.tick()
        current = self.plugin.scheduler.binding_plan_views(identity)
        self.assertEqual([view.state for view in current], [state.PLANNED])
        self.assertEqual(current[0].timezone, "UTC")
        self.assertEqual(current[0].planned_at.utcoffset(), timedelta(0))
        current_text = await self.personal_status(task_id=self.task.task_id)
        self.assertIn("UTC", current_text)
        self.assertIn("+00:00", current_text)

        self.clock.advance(-60)
        reversed_clock = self.plugin.scheduler.binding_plan_views(identity)
        self.assertEqual([view.state for view in reversed_clock], [state.PLAN_STALE])
        reversed_text = await self.personal_status(task_id=self.task.task_id)
        self.assertNotRegex(reversed_text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

    async def test_suppressed_disabled_absent_and_stopped_plans_are_not_runnable(self):
        identity = await self.bind()
        state = self.main.BindingPlanState

        async def assert_unrunnable(expected):
            views = self.plugin.scheduler.binding_plan_views(identity)
            self.assertEqual([view.state for view in views], [expected])
            text = await self.personal_status(task_id=self.task.task_id)
            self.assertIsNone(views[0].planned_at)
            self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

        paused = FakeEvent()
        await self.plugin.checkin(paused, "暂停", self.task.task_id)
        await assert_unrunnable(state.PAUSED)
        await self.plugin.checkin(FakeEvent(), "恢复", self.task.task_id)

        self.config["enabled"] = False
        await assert_unrunnable(state.GLOBAL_DISABLED)
        self.config["enabled"] = True

        self.config["tasks"][0]["enabled"] = False
        await assert_unrunnable(state.TASK_DISABLED)
        self.config["tasks"][0]["enabled"] = True

        configured_tasks = self.config["tasks"]
        self.config["tasks"] = []
        await assert_unrunnable(state.PLAN_ABSENT)
        self.config["tasks"] = configured_tasks

        intent_key = f"task_disable:{self.task.task_id}"
        await self.plugin.state_store.mutate(
            lambda snapshot: snapshot["dedupe"].update(
                {
                    intent_key: {
                        "kind": "task_disable_intent",
                        "task_id": self.task.task_id,
                    }
                }
            )
        )
        await assert_unrunnable(state.DISABLING)
        await self.plugin.state_store.mutate(
            lambda snapshot: snapshot["dedupe"].pop(intent_key)
        )

        await self.plugin.checkin(FakeEvent(), "录入", self.task.task_id)
        await assert_unrunnable(state.ENROLLMENT)
        await self.plugin.capture_enrollment(FakeEvent(text="/取消"))

        await self.plugin.scheduler.close()
        await assert_unrunnable(state.STOPPED)

    async def test_unready_plan_observation_keeps_confirmed_state_unchanged(self):
        identity = await self.bind()
        self.backend.fail_next_put()
        await self.plugin.checkin(FakeEvent(), "暂停", self.task.task_id)
        self.assertFalse(self.plugin.state_store.ready)

        before = self.snapshot()
        put_attempts = self.backend.put_attempts
        views = self.plugin.scheduler.binding_plan_views(identity)
        self.assertEqual(
            [view.state for view in views], [self.main.BindingPlanState.UNREADY]
        )
        text = await self.personal_status(task_id=self.task.task_id)
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.put_attempts, put_attempts)
