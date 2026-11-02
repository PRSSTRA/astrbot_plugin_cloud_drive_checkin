import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from neo_support import ExecutionKV
from state_support import load_module

execution_config = load_module("execution_config", "_manual_queue_tests")
execution_queue = load_module("execution_queue", "_manual_queue_tests")
notifications = load_module("notifications", "_manual_queue_tests")
permissions = load_module("permissions", "_manual_queue_tests")
storage = load_module("storage", "_manual_queue_tests")
task_config = load_module("task_config", "_manual_queue_tests")
config_module = load_module("config", "_manual_queue_tests")


_RESULT = {
    "protocol": 1,
    "process_status": "exited",
    "exit_code": 0,
    "business_status": "success",
    "reason_code": "reported_result",
    "duration_ms": 7,
}


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


async def wait_until(predicate, timeout=3.0):
    async def waiting():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(waiting(), timeout)


class ExecutionConfigTests(unittest.TestCase):
    def test_defaults_and_full_webui_objects(self):
        defaults = execution_config.parse_execution_config({})
        self.assertEqual(defaults.manual_cooldown_seconds, 300)
        self.assertEqual(defaults.queue_expiry_seconds, 900)
        self.assertEqual(defaults.history_retention_days, 30)
        self.assertEqual(defaults.user_interval.mode, "random")
        self.assertEqual(
            (defaults.user_interval.min_seconds, defaults.user_interval.max_seconds),
            (30, 60),
        )
        self.assertEqual(
            (defaults.task_interval.mode, defaults.task_interval.seconds),
            ("fixed", 60),
        )

        parsed = execution_config.parse_execution_config(
            {
                "user_interval": {
                    "mode": "random",
                    "seconds": 60,
                    "min_seconds": 30,
                    "max_seconds": 60,
                },
                "task_interval": {
                    "mode": "fixed",
                    "seconds": 60,
                    "min_seconds": 30,
                    "max_seconds": 60,
                },
            }
        )
        self.assertEqual(
            (parsed.user_interval.mode, parsed.user_interval.min_seconds),
            ("random", 30),
        )
        self.assertEqual(
            (parsed.task_interval.mode, parsed.task_interval.seconds), ("fixed", 60)
        )
        disabled = execution_config.parse_execution_config(
            {
                "user_interval": {
                    "mode": "none",
                    "seconds": 60,
                    "min_seconds": 30,
                    "max_seconds": 60,
                }
            }
        )
        self.assertEqual(disabled.user_interval.mode, "none")

    def test_rejects_unknown_and_invalid_active_values(self):
        invalid = (
            {"manual_cooldown_seconds": True},
            {"queue_expiry_seconds": -1},
            {"history_retention_days": False},
            {"user_interval": 0},
            {"user_interval": {"mode": "fixed"}},
            {"user_interval": {"mode": "fixed", "seconds": True}},
            {
                "user_interval": {
                    "mode": "random",
                    "min_seconds": 5,
                    "max_seconds": 4,
                }
            },
            {"task_interval": {"mode": "none", "extra": 1}},
            {"task_interval": {"mode": "other"}},
        )
        for raw in invalid:
            with (
                self.subTest(raw=raw),
                self.assertRaises(config_module.PluginConfigError),
            ):
                execution_config.parse_execution_config(raw)


class ExecutionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = ExecutionKV()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        self.clock = MutableClock()
        self.original_now = execution_queue._utc_now
        execution_queue._utc_now = self.clock
        self.blocked = False
        self.raw_config = {
            "manual_cooldown_seconds": 0,
            "queue_expiry_seconds": 900,
            "history_retention_days": 30,
            "user_interval": {"mode": "none"},
            "task_interval": {"mode": "none"},
        }
        self.guard = lambda item, snapshot: None
        self.execute_calls = []
        self.queues = []
        self.identity = permissions.Identity("qq-bot", "10001")
        self.base_task = task_config.TaskConfig(
            task_id="task_one",
            name="Task One",
            enabled=True,
            runtime="python",
            script_url="https://example.invalid/task.py",
            source_commit="1" * 40,
            sha256="2" * 64,
            cron="0 0 * * *",
            user_fields=(),
            fixed_env=(),
            input_adapter="direct_env",
            result_adapter="json_protocol",
            dependency_bundle="",
            timeout_seconds=30,
            experimental=False,
            version="3" * 64,
        )

    async def asyncTearDown(self):
        for queue in reversed(self.queues):
            await queue.close()
        execution_queue._utc_now = self.original_now
        if self.store.has_pending:
            self.kv.fail_at = None
            await self.store.retry_pending()
        await self.store.close()

    async def make_queue(self, execute=None, guard=None, startup_ready=None):
        if execute is None:

            async def execute(item):
                self.execute_calls.append(item["run_id"])

                def finish_run(snapshot):
                    snapshot["runs"][item["run_id"]] = {
                        "phase": "finished",
                        "finished_at": self.clock().isoformat(),
                        "cleanup_status": "cleaned",
                        "cleanup_finished_at": self.clock().isoformat(),
                        "business_attempted": True,
                        **_RESULT,
                    }

                confirmed = await self.store.mutate(finish_run)
                return confirmed["runs"][item["run_id"]]

        queue = execution_queue.ExecutionQueue(
            self.store,
            execute=execute,
            guard=guard or self.guard,
            config=lambda: self.raw_config,
            blocked=lambda: self.blocked,
            reserve=notifications.reserve_terminal,
            terminal=notifications.enqueue_terminal,
            startup_ready=startup_ready or (lambda: True),
        )
        await queue.initialize()
        self.queues.append(queue)
        return queue

    async def test_close_during_last_initialize_await_cannot_start_worker(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def execute(item):
            raise AssertionError("closed queue must not execute")

        async def gated_recovery():
            entered.set()
            await release.wait()

        queue = execution_queue.ExecutionQueue(
            self.store,
            execute=execute,
            guard=self.guard,
            config=lambda: self.raw_config,
            blocked=lambda: self.blocked,
            reserve=notifications.reserve_terminal,
            terminal=notifications.enqueue_terminal,
        )
        queue._recover_before_start = gated_recovery
        self.queues.append(queue)

        initialization = asyncio.create_task(queue.initialize())
        await asyncio.wait_for(entered.wait(), 1)
        await queue.close()
        release.set()
        outcome = (await asyncio.gather(initialization, return_exceptions=True))[0]

        self.assertIsInstance(outcome, execution_queue.ExecutionBlocked)
        self.assertTrue(queue.closed)
        self.assertFalse(queue._accepting)
        self.assertIsNone(queue._worker)

    async def enqueue(self, queue, *, identity=None, task=None):
        return await queue.enqueue(
            identity=identity or self.identity,
            task=task or self.base_task,
            origin="origin:test",
            artifact_sha256="4" * 64,
            inputs_sha256="5" * 64,
        )

    async def enqueue_scheduled(
        self,
        queue,
        *,
        scheduled_at=None,
        identities=None,
        task=None,
        artifact_sha256="4" * 64,
        inputs_sha256="5" * 64,
    ):
        selected = identities if identities is not None else [self.identity]
        return await queue.enqueue_scheduled(
            task=task or self.base_task,
            scheduled_at=scheduled_at or self.clock(),
            requests=[
                {
                    "identity": identity,
                    "origin": f"origin:auto:{identity.qq_id}",
                    "artifact_sha256": artifact_sha256,
                    "inputs_sha256": inputs_sha256,
                }
                for identity in selected
            ],
        )

    def batch(self, run_id):
        return self.store.snapshot()["batches"][run_id]

    async def test_duplicate_returns_existing_run_and_reserves_once(self):
        self.blocked = True
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        second = await self.enqueue(queue)
        self.assertEqual(second, first)
        snapshot = self.store.snapshot()
        self.assertEqual(list(snapshot["batches"]), [first])
        self.assertEqual(list(snapshot["outbox"]), [first])
        self.assertEqual(snapshot["outbox"][first]["phase"], "reserved")

    async def test_fifo_worker_is_serial_and_passes_stable_run_ids(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        live = 0
        maximum_live = 0
        seen = []

        async def execute(item):
            nonlocal live, maximum_live
            seen.append((item["run_id"], item["task_id"]))
            live += 1
            maximum_live = max(maximum_live, live)
            try:
                if len(seen) == 1:
                    first_started.set()
                    await release_first.wait()
                return dict(_RESULT)
            finally:
                live -= 1

        queue = await self.make_queue(execute=execute)
        first = await self.enqueue(queue)
        await asyncio.wait_for(first_started.wait(), 2)
        second_identity = permissions.Identity("qq-bot", "10002")
        second = await self.enqueue(queue, identity=second_identity)
        release_first.set()
        await wait_until(lambda: self.batch(second)["status"] == "finished")

        self.assertEqual([run_id for run_id, _ in seen], [first, second])
        self.assertEqual(maximum_live, 1)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["outbox"][first]["phase"], "pending")
        self.assertEqual(snapshot["outbox"][second]["phase"], "pending")

    async def test_blocked_executor_still_revalidates_every_queued_entry(self):
        self.blocked = True
        revoked = set()
        guarded = []

        def guard(item, snapshot):
            guarded.append(item["qq_id"])
            if item["qq_id"] in revoked:
                raise execution_queue.QueueRejected("permission_revoked")

        queue = await self.make_queue(guard=guard)
        first = await self.enqueue(queue)
        second_identity = permissions.Identity("qq-bot", "10002")
        second = await self.enqueue(queue, identity=second_identity)
        revoked.add(second_identity.qq_id)
        queue.wake()
        await wait_until(lambda: self.batch(second)["status"] == "finished")

        self.assertEqual(self.batch(first)["status"], "queued")
        self.assertEqual(
            self.batch(second)["result"]["queue_reason"], "permission_revoked"
        )
        self.assertIn(self.identity.qq_id, guarded)
        self.assertIn(second_identity.qq_id, guarded)
        self.assertEqual(self.execute_calls, [])

    async def test_startup_gate_precedes_guard_but_not_executor_blocking(self):
        self.blocked = True
        startup = {"ready": False}
        guarded = []

        def guard(item, snapshot):
            guarded.append(item["run_id"])
            raise execution_queue.QueueRejected("permission_revoked")

        queue = await self.make_queue(
            guard=guard, startup_ready=lambda: startup["ready"]
        )
        run_id = await self.enqueue(queue)
        queue.wake()
        await asyncio.sleep(0.05)

        self.assertEqual(self.batch(run_id)["status"], "queued")
        self.assertEqual(guarded, [])
        self.assertEqual(self.execute_calls, [])

        startup["ready"] = True
        queue.wake()
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")

        self.assertEqual(
            self.batch(run_id)["result"]["queue_reason"], "permission_revoked"
        )
        self.assertEqual(guarded, [run_id])
        self.assertEqual(self.execute_calls, [])

    async def test_persisted_queue_survives_cold_restart_until_platforms_complete(self):
        startup = {"ready": False}
        platforms = set()
        guarded = []

        def guard(item, snapshot):
            guarded.append(item["run_id"])
            if item["platform_id"] not in platforms:
                raise execution_queue.QueueRejected("permission_revoked")

        first = await self.make_queue(
            guard=guard, startup_ready=lambda: startup["ready"]
        )
        run_id = await self.enqueue(first)
        await first.close()

        successor = await self.make_queue(
            guard=guard, startup_ready=lambda: startup["ready"]
        )
        platforms.add("other-platform")
        successor.wake()
        await asyncio.sleep(0.05)

        self.assertEqual(self.batch(run_id)["status"], "queued")
        self.assertEqual(guarded, [])
        self.assertEqual(self.execute_calls, [])

        platforms.add(self.identity.platform_id)
        startup["ready"] = True
        successor.wake()
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")

        self.assertEqual(self.batch(run_id)["result"]["business_status"], "success")
        self.assertEqual(guarded, [run_id])
        self.assertEqual(self.execute_calls, [run_id])

    async def test_expiry_is_terminal_and_never_executes(self):
        self.blocked = True
        self.raw_config["queue_expiry_seconds"] = 10
        queue = await self.make_queue()
        run_id = await self.enqueue(queue)
        self.clock.advance(11)
        queue.wake()
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")

        result = self.batch(run_id)["result"]
        self.assertEqual(result["business_status"], "cancelled")
        self.assertEqual(result["queue_reason"], "queue_expired")
        self.assertEqual(self.execute_calls, [])
        self.assertEqual(self.store.snapshot()["outbox"][run_id]["phase"], "pending")

    async def test_cold_start_expires_only_expired_item_without_notice_attempt(self):
        startup = {"ready": False}
        self.raw_config["queue_expiry_seconds"] = 10
        queue = await self.make_queue(startup_ready=lambda: startup["ready"])
        expired = await self.enqueue(queue)
        self.raw_config["queue_expiry_seconds"] = 900
        unexpired = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10002")
        )

        self.clock.advance(11)
        await queue.close()
        await self.make_queue(startup_ready=lambda: startup["ready"])

        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["batches"][expired]["status"], "finished")
        self.assertEqual(
            snapshot["batches"][expired]["result"]["queue_reason"],
            "queue_expired",
        )
        self.assertEqual(snapshot["outbox"][expired]["phase"], "pending")
        self.assertEqual(snapshot["outbox"][expired]["attempts"], 0)
        self.assertEqual(snapshot["batches"][unexpired]["status"], "queued")
        self.assertEqual(snapshot["outbox"][unexpired]["phase"], "reserved")
        self.assertEqual(snapshot["outbox"][unexpired]["attempts"], 0)
        self.assertEqual(self.execute_calls, [])

    async def test_live_guard_cancels_owned_execute_and_finishes_safely(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        revoked = False

        async def execute(item):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        def guard(item, snapshot):
            if revoked:
                raise execution_queue.QueueRejected("binding_paused")

        queue = await self.make_queue(execute=execute, guard=guard)
        run_id = await self.enqueue(queue)
        await asyncio.wait_for(started.wait(), 2)
        revoked = True
        queue.wake()
        await asyncio.wait_for(cancelled.wait(), 2)
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        self.assertEqual(self.batch(run_id)["result"]["queue_reason"], "binding_paused")

    async def test_claim_commit_failure_is_never_replayed(self):
        self.blocked = True
        queue = await self.make_queue()
        run_id = await self.enqueue(queue)
        self.kv.fail_at = len(self.kv.writes) + 1
        self.blocked = False
        queue.wake()
        await wait_until(lambda: self.store.has_pending)

        pending = copy.deepcopy(self.kv.writes[-1])
        self.assertEqual(pending["batches"][run_id]["status"], "running")
        self.assertEqual(self.execute_calls, [])
        self.kv.fail_at = None
        await self.store.retry_pending()
        self.assertEqual(self.kv.writes[-1], pending)
        queue.wake()
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        self.assertEqual(self.execute_calls, [])
        self.assertEqual(self.batch(run_id)["result"]["queue_reason"], "interrupted")

    async def test_unconfirmed_terminal_freezes_dispatch_until_exact_retry(self):
        self.blocked = True
        first_returned = False

        async def execute(item):
            nonlocal first_returned
            self.execute_calls.append(item["run_id"])
            if not first_returned:
                first_returned = True
                self.blocked = True
            return dict(_RESULT)

        queue = await self.make_queue(execute=execute)
        first = await self.enqueue(queue)
        second_identity = permissions.Identity("qq-bot", "10002")
        second = await self.enqueue(queue, identity=second_identity)
        self.kv.fail_at = len(self.kv.writes) + 2  # Claim succeeds; terminal fails.
        self.blocked = False
        queue.wake()
        await wait_until(lambda: self.store.has_pending)

        pending = copy.deepcopy(self.kv.writes[-1])
        self.assertEqual(pending["batches"][first]["status"], "finished")
        self.assertEqual(self.execute_calls, [first])
        self.kv.fail_at = None
        await self.store.retry_pending()
        self.assertEqual(self.kv.writes[-1], pending)
        self.assertEqual(self.batch(second)["status"], "queued")

        self.blocked = False
        queue.wake()
        await wait_until(lambda: self.batch(second)["status"] == "finished")
        self.assertEqual(self.execute_calls, [first, second])

    async def test_manual_cooldown_starts_at_actual_completion(self):
        self.raw_config.update(
            manual_cooldown_seconds=300,
            user_interval={"mode": "none"},
            task_interval={"mode": "none"},
        )
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")

        with self.assertRaises(execution_queue.QueueRejected) as raised:
            await self.enqueue(queue)
        self.assertEqual(raised.exception.reason, "cooldown_active")

    async def test_same_task_user_change_uses_user_interval(self):
        self.raw_config.update(
            manual_cooldown_seconds=0,
            user_interval={"mode": "fixed", "seconds": 30},
            task_interval={"mode": "fixed", "seconds": 120},
        )
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")
        second = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10002")
        )
        await wait_until(
            lambda: (
                self.store.snapshot()["cooldowns"]
                .get("checkin:global_spacing", {})
                .get("target_run_id")
                == second
            )
        )
        spacing = self.store.snapshot()["cooldowns"]["checkin:global_spacing"]
        self.assertEqual(spacing["target_kind"], "user")
        self.clock.advance(29)
        queue.wake()
        await asyncio.sleep(0)
        self.assertEqual(self.batch(second)["status"], "queued")
        self.clock.advance(1)
        queue.wake()
        await wait_until(lambda: self.batch(second)["status"] == "finished")

    async def test_task_change_uses_only_task_interval(self):
        self.raw_config.update(
            manual_cooldown_seconds=0,
            user_interval={"mode": "fixed", "seconds": 120},
            task_interval={"mode": "fixed", "seconds": 60},
        )
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")
        second = await self.enqueue(
            queue,
            identity=permissions.Identity("qq-bot", "10002"),
            task=task_two,
        )
        await wait_until(
            lambda: (
                self.store.snapshot()["cooldowns"]
                .get("checkin:global_spacing", {})
                .get("target_run_id")
                == second
            )
        )
        spacing = self.store.snapshot()["cooldowns"]["checkin:global_spacing"]
        self.assertEqual(spacing["target_kind"], "task")
        self.clock.advance(60)
        queue.wake()
        await wait_until(lambda: self.batch(second)["status"] == "finished")

    async def test_spacing_uses_most_recent_global_completion(self):
        self.raw_config.update(
            manual_cooldown_seconds=0,
            user_interval={"mode": "fixed", "seconds": 30},
            task_interval={"mode": "fixed", "seconds": 60},
        )
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")
        second = await self.enqueue(
            queue,
            identity=permissions.Identity("qq-bot", "10002"),
            task=task_two,
        )
        self.clock.advance(60)
        queue.wake()
        await wait_until(lambda: self.batch(second)["status"] == "finished")
        third = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10003")
        )
        await wait_until(
            lambda: (
                self.store.snapshot()["cooldowns"]
                .get("checkin:global_spacing", {})
                .get("target_run_id")
                == third
            )
        )
        spacing = self.store.snapshot()["cooldowns"]["checkin:global_spacing"]
        self.assertEqual(spacing["task_id"], "task_two")
        self.assertEqual(spacing["target_kind"], "task")
        self.clock.advance(30)
        queue.wake()
        await asyncio.sleep(0)
        self.assertEqual(self.batch(third)["status"], "queued")
        self.clock.advance(30)
        queue.wake()
        await wait_until(lambda: self.batch(third)["status"] == "finished")

    async def test_terminal_survives_pending_cleanup_and_spacing_waits_for_cleanup(
        self,
    ):
        self.raw_config.update(
            manual_cooldown_seconds=300,
            user_interval={"mode": "none"},
            task_interval={"mode": "none"},
        )
        revoked = set()
        executed = []

        def guard(item, snapshot):
            if item["qq_id"] in revoked:
                raise execution_queue.QueueRejected("permission_revoked")

        async def execute(item):
            executed.append(item["run_id"])

            def record(snapshot):
                snapshot["runs"][item["run_id"]] = {
                    "phase": "finished",
                    "finished_at": self.clock().isoformat(),
                    "cleanup_finished_at": None,
                    "cleanup_status": "pending",
                    **_RESULT,
                }
                snapshot["cleanup_jobs"][item["run_id"]] = {"pending": True}

            confirmed = await self.store.mutate(record)
            return confirmed["runs"][item["run_id"]]

        queue = await self.make_queue(execute=execute, guard=guard)
        run_id = await self.enqueue(queue)
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        self.assertTrue(self.batch(run_id)["cooldown_pending"])
        self.assertEqual(self.store.snapshot()["outbox"][run_id]["phase"], "pending")
        self.assertEqual(self.store.snapshot()["cooldowns"], {})
        other_identity = permissions.Identity("qq-bot", "10002")
        revoked_run = await self.enqueue(queue, identity=other_identity)
        revoked.add(other_identity.qq_id)
        queue.wake()
        await wait_until(lambda: self.batch(revoked_run)["status"] == "finished")
        self.assertEqual(
            self.batch(revoked_run)["result"]["queue_reason"],
            "permission_revoked",
        )
        self.assertEqual(executed, [run_id])

        self.clock.advance(100)

        def cleaned(snapshot):
            snapshot["runs"][run_id].update(
                cleanup_status="cleaned",
                cleanup_finished_at=self.clock().isoformat(),
            )
            snapshot["cleanup_jobs"].pop(run_id)

        await self.store.mutate(cleaned)
        queue.wake()
        await wait_until(lambda: "cooldown_pending" not in self.batch(run_id))
        cooldown = next(
            record
            for record in self.store.snapshot()["cooldowns"].values()
            if record["scope"] == "manual"
        )
        self.assertEqual(
            datetime.fromisoformat(cooldown["expires_at"]),
            self.clock() + timedelta(seconds=300),
        )
        spacing = self.store.snapshot()["cooldowns"]["checkin:global_spacing"]
        self.assertEqual(datetime.fromisoformat(spacing["completed_at"]), self.clock())

    async def test_same_identity_and_zero_or_none_intervals_add_no_delay(self):
        self.raw_config.update(
            manual_cooldown_seconds=0,
            user_interval={"mode": "fixed", "seconds": 300},
            task_interval={"mode": "fixed", "seconds": 300},
        )
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")
        second = await self.enqueue(queue)
        await wait_until(lambda: self.batch(second)["status"] == "finished")

        self.raw_config.update(
            user_interval={"mode": "none"},
            task_interval={"mode": "fixed", "seconds": 0},
        )
        third = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10002")
        )
        await wait_until(lambda: self.batch(third)["status"] == "finished")
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        fourth = await self.enqueue(
            queue,
            identity=permissions.Identity("qq-bot", "10003"),
            task=task_two,
        )
        await wait_until(lambda: self.batch(fourth)["status"] == "finished")
        self.assertEqual(self.execute_calls, [first, second, third, fourth])

    async def test_skipped_entry_does_not_reset_last_global_completion(self):
        self.raw_config.update(
            manual_cooldown_seconds=0,
            user_interval={"mode": "fixed", "seconds": 30},
            task_interval={"mode": "fixed", "seconds": 60},
        )
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )

        def guard(item, snapshot):
            if item["task_id"] == "task_two":
                raise execution_queue.QueueRejected("task_disabled")

        queue = await self.make_queue(guard=guard)
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")
        skipped = await self.enqueue(
            queue,
            identity=permissions.Identity("qq-bot", "10002"),
            task=task_two,
        )
        await wait_until(lambda: self.batch(skipped)["status"] == "finished")
        next_run = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10003")
        )
        await wait_until(
            lambda: (
                self.store.snapshot()["cooldowns"]
                .get("checkin:global_spacing", {})
                .get("target_run_id")
                == next_run
            )
        )
        spacing = self.store.snapshot()["cooldowns"]["checkin:global_spacing"]
        self.assertEqual(spacing["task_id"], "task_one")
        self.assertEqual(spacing["target_kind"], "user")
        self.clock.advance(29)
        queue.wake()
        await asyncio.sleep(0)
        self.assertEqual(self.batch(next_run)["status"], "queued")
        self.clock.advance(1)
        queue.wake()
        await wait_until(lambda: self.batch(next_run)["status"] == "finished")
        self.assertEqual(self.batch(skipped)["result"]["queue_reason"], "task_disabled")
        self.assertEqual(self.execute_calls, [first, next_run])

    async def test_random_spacing_is_sampled_once_and_persisted(self):
        self.raw_config.update(
            manual_cooldown_seconds=0,
            user_interval={"mode": "random", "min_seconds": 5, "max_seconds": 9},
            task_interval={"mode": "none"},
        )
        samples = []
        original_randint = execution_queue.random.randint

        def sample(minimum, maximum):
            samples.append((minimum, maximum))
            return 7

        execution_queue.random.randint = sample
        self.addCleanup(setattr, execution_queue.random, "randint", original_randint)
        queue = await self.make_queue()
        first = await self.enqueue(queue)
        await wait_until(lambda: self.batch(first)["status"] == "finished")
        second = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10002")
        )
        await wait_until(
            lambda: (
                self.store.snapshot()["cooldowns"]
                .get("checkin:global_spacing", {})
                .get("target_run_id")
                == second
            )
        )
        self.assertEqual(samples, [(5, 9)])
        queue.wake()
        await asyncio.sleep(0)
        self.assertEqual(samples, [(5, 9)])
        self.clock.advance(7)
        queue.wake()
        await wait_until(lambda: self.batch(second)["status"] == "finished")

    async def test_recovery_terminals_started_batch_without_replay(self):
        self.blocked = True
        first_queue = await self.make_queue()
        run_id = await self.enqueue(first_queue)
        await self.store.mutate(
            lambda snapshot: snapshot["batches"][run_id].update(status="running")
        )
        await first_queue.close()
        self.queues.remove(first_queue)

        self.blocked = False
        second_queue = await self.make_queue()
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        self.assertEqual(self.execute_calls, [])
        self.assertEqual(self.batch(run_id)["result"]["queue_reason"], "interrupted")
        self.assertFalse(second_queue.closed)

    async def test_execute_callback_domain_rejection_is_safe_terminal(self):
        async def execute(item):
            raise execution_queue.QueueRejected(
                "credentials_missing", business_status="config_error"
            )

        queue = await self.make_queue(execute=execute)
        run_id = await self.enqueue(queue)
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        result = self.batch(run_id)["result"]
        self.assertEqual(result["queue_reason"], "credentials_missing")
        self.assertEqual(result["business_status"], "config_error")
        self.assertEqual(result["reason_code"], "runner_error")

    async def test_unexpected_execute_failure_does_not_kill_worker(self):
        calls = []

        async def execute(item):
            calls.append(item["task_id"])
            if item["task_id"] == "task_one":
                raise RuntimeError("unexpected callback failure")
            return dict(_RESULT)

        queue = await self.make_queue(execute=execute)
        failed = await self.enqueue(queue)
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        succeeded = await self.enqueue(queue, task=task_two)
        await wait_until(lambda: self.batch(succeeded)["status"] == "finished")
        self.assertEqual(calls, ["task_one", "task_two"])
        self.assertEqual(
            self.batch(failed)["result"]["queue_reason"], "execution_error"
        )
        self.assertEqual(self.batch(succeeded)["result"]["business_status"], "success")
        self.assertFalse(queue._worker.done())

    async def test_internal_worker_failure_joins_child_and_stops_admission(self):
        started = asyncio.Event()
        exited = asyncio.Event()

        async def execute(item):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        queue = await self.make_queue(execute=execute)

        async def broken_monitor(item, task):
            await started.wait()
            raise KeyError("simulated internal queue defect")

        queue._monitor_execution = broken_monitor
        run_id = await self.enqueue(queue)
        await asyncio.wait_for(exited.wait(), 2)
        await wait_until(lambda: queue._worker.done() and not queue._accepting)
        self.assertIsInstance(queue.last_error, KeyError)
        self.assertEqual(self.batch(run_id)["status"], "running")
        with self.assertRaises(execution_queue.ExecutionBlocked):
            await self.enqueue(queue, identity=permissions.Identity("qq-bot", "10002"))

    async def test_business_not_attempted_never_writes_cooldown(self):
        self.blocked = True
        self.raw_config.update(
            manual_cooldown_seconds=300,
            user_interval={"mode": "fixed", "seconds": 30},
            task_interval={"mode": "fixed", "seconds": 60},
        )
        queue = await self.make_queue()
        run_id = await self.enqueue(queue)
        await queue.close()

        def preparing(snapshot):
            snapshot["batches"][run_id]["status"] = "running"
            snapshot["runs"][run_id] = {
                "phase": "preparing",
                "finished_at": None,
                "cleanup_status": "pending",
                "cleanup_finished_at": None,
                "business_attempted": False,
                **_RESULT,
            }

        await self.store.mutate(preparing)
        await queue.reconcile_shutdown()
        self.assertEqual(self.batch(run_id)["result"]["business_status"], "cancelled")
        self.assertEqual(self.store.snapshot()["cooldowns"], {})
        self.assertEqual(self.store.snapshot()["outbox"][run_id]["phase"], "pending")

    async def test_shutdown_reconcile_preserves_finished_result_and_old_attempt(self):
        self.blocked = True
        self.raw_config["manual_cooldown_seconds"] = 300
        queue = await self.make_queue()
        run_id = await self.enqueue(queue)
        await queue.close()

        def finished(snapshot):
            snapshot["batches"][run_id]["status"] = "running"
            snapshot["runs"][run_id] = {
                "phase": "finished",
                "finished_at": self.clock().isoformat(),
                "cleanup_status": "cleaned",
                "cleanup_finished_at": self.clock().isoformat(),
                **_RESULT,
            }

        await self.store.mutate(finished)
        await queue.reconcile_shutdown()
        self.assertEqual(self.batch(run_id)["result"]["business_status"], "success")
        self.assertEqual(self.store.snapshot()["outbox"][run_id]["phase"], "pending")
        self.assertIn("checkin:global_spacing", self.store.snapshot()["cooldowns"])

    async def test_shutdown_reconcile_keeps_sent_unknown_and_expires_only_due_queue(
        self,
    ):
        self.blocked = True
        self.raw_config["queue_expiry_seconds"] = 10
        queue = await self.make_queue()
        running = await self.enqueue(queue)
        expired = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10002")
        )
        self.raw_config["queue_expiry_seconds"] = 100
        preserved = await self.enqueue(
            queue, identity=permissions.Identity("qq-bot", "10003")
        )
        await queue.close()

        def executing(snapshot):
            snapshot["batches"][running]["status"] = "running"
            snapshot["runs"][running] = {
                "phase": "executing",
                "finished_at": None,
                "cleanup_status": "pending",
                "cleanup_finished_at": None,
                "business_attempted": False,
                **_RESULT,
            }

        await self.store.mutate(executing)
        self.clock.advance(11)
        await queue.reconcile_shutdown()
        self.assertEqual(self.batch(running)["result"]["business_status"], "unknown")
        self.assertTrue(self.batch(running)["cooldown_pending"])
        self.assertEqual(self.batch(expired)["result"]["queue_reason"], "queue_expired")
        self.assertEqual(self.batch(preserved)["status"], "queued")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["outbox"][running]["phase"], "pending")
        self.assertEqual(snapshot["outbox"][expired]["phase"], "pending")
        self.assertEqual(snapshot["outbox"][preserved]["phase"], "reserved")

    async def test_retention_prunes_only_settled_triplet_on_next_admission(self):
        self.raw_config["history_retention_days"] = 0
        queue = await self.make_queue()
        old = await self.enqueue(queue)
        await wait_until(lambda: self.batch(old)["status"] == "finished")

        first_snapshot = self.store.snapshot()
        self.assertIn(old, first_snapshot["batches"])
        self.assertEqual(first_snapshot["outbox"][old]["phase"], "pending")
        await self.store.mutate(
            lambda snapshot: snapshot["outbox"][old].update(phase="sent")
        )
        newer_identity = permissions.Identity("qq-bot", "10002")
        newer = await self.enqueue(queue, identity=newer_identity)
        snapshot = self.store.snapshot()
        self.assertNotIn(old, snapshot["batches"])
        self.assertNotIn(old, snapshot["outbox"])
        self.assertIn(newer, snapshot["batches"])

    async def test_scheduled_duplicate_restart_and_backward_watermark_dedupe(self):
        self.blocked = True
        scheduled_at = self.clock()
        first_queue = await self.make_queue()
        admitted = await self.enqueue_scheduled(
            first_queue,
            scheduled_at=scheduled_at,
            artifact_sha256=None,
            inputs_sha256=None,
        )
        self.assertEqual(admitted["status"], "admitted")
        run_id = admitted["run_ids"][0]
        item = self.batch(run_id)
        self.assertEqual(item["kind"], "checkin")
        self.assertEqual(item["source"], "automatic")
        self.assertIs(item["manual_requested"], False)
        self.assertIsNone(item["artifact_sha256"])
        self.assertIsNone(item["inputs_sha256"])

        await first_queue.close()
        self.queues.remove(first_queue)
        second_queue = await self.make_queue()
        duplicate = await self.enqueue_scheduled(
            second_queue,
            scheduled_at=scheduled_at,
            artifact_sha256=None,
            inputs_sha256=None,
        )
        self.assertEqual(duplicate, {"status": "duplicate", "run_ids": [run_id]})

        later = scheduled_at + timedelta(minutes=1)
        overlap = await self.enqueue_scheduled(second_queue, scheduled_at=later)
        self.assertEqual(overlap, {"status": "skipped_overlap", "run_ids": []})
        backward = await self.enqueue_scheduled(
            second_queue, scheduled_at=scheduled_at + timedelta(seconds=30)
        )
        self.assertEqual(backward, {"status": "duplicate", "run_ids": []})
        cursor = self.store.snapshot()["dedupe"]["schedule_cursor:task_one"]
        self.assertEqual(cursor["kind"], "schedule_cursor")
        self.assertEqual(cursor["scheduled_at"], later.isoformat())

    async def test_scheduled_overlap_includes_cleanup_pending_and_empty_eligibility(
        self,
    ):
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )

        def guard(item, snapshot):
            if item["task_id"] == "task_two":
                raise execution_queue.QueueRejected("binding_missing")

        async def execute(item):
            self.execute_calls.append(item["run_id"])

            def finish_run(snapshot):
                snapshot["runs"][item["run_id"]] = {
                    "phase": "finished",
                    "finished_at": self.clock().isoformat(),
                    "cleanup_status": "pending",
                    "cleanup_finished_at": None,
                    "business_attempted": True,
                    **_RESULT,
                }
                snapshot["cleanup_jobs"][item["run_id"]] = {"pending": True}

            confirmed = await self.store.mutate(finish_run)
            return confirmed["runs"][item["run_id"]]

        queue = await self.make_queue(execute=execute, guard=guard)
        first = await self.enqueue_scheduled(queue, scheduled_at=self.clock())
        run_id = first["run_ids"][0]
        await wait_until(lambda: self.batch(run_id).get("cooldown_pending") is True)

        overlap = await self.enqueue_scheduled(
            queue, scheduled_at=self.clock() + timedelta(minutes=1)
        )
        empty = await self.enqueue_scheduled(
            queue,
            task=task_two,
            scheduled_at=self.clock() + timedelta(minutes=2),
            identities=[permissions.Identity("qq-bot", "10002")],
        )
        self.assertEqual(overlap, {"status": "skipped_overlap", "run_ids": []})
        self.assertEqual(empty, {"status": "skipped_empty", "run_ids": []})
        self.assertEqual(self.execute_calls, [run_id])

    async def test_scheduled_eligibility_skips_recipients_but_aborts_generic_config(
        self,
    ):
        self.blocked = True

        def guard(item, snapshot):
            if item["qq_id"] == "10001":
                raise execution_queue.QueueRejected("permission_revoked")
            if item["qq_id"] == "10002":
                raise execution_queue.QueueRejected(
                    "credentials_missing", business_status="config_error"
                )
            if item["qq_id"] == "10003":
                raise execution_queue.QueueRejected(
                    "config_error", business_status="config_error"
                )

        queue = await self.make_queue(guard=guard)
        admitted = await self.enqueue_scheduled(
            queue,
            scheduled_at=self.clock(),
            identities=[
                permissions.Identity("qq-bot", "10002"),
                permissions.Identity("qq-bot", "10001"),
            ],
        )
        self.assertEqual(admitted["status"], "admitted")
        self.assertEqual(self.batch(admitted["run_ids"][0])["qq_id"], "10002")

        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        scheduled_at = self.clock() + timedelta(minutes=1)
        with self.assertRaises(execution_queue.QueueRejected) as raised:
            await self.enqueue_scheduled(
                queue,
                task=task_two,
                scheduled_at=scheduled_at,
                identities=[permissions.Identity("qq-bot", "10003")],
            )
        self.assertEqual(raised.exception.reason, "config_error")
        self.assertNotIn(
            f"schedule:task_two:{scheduled_at.isoformat()}",
            self.store.snapshot()["dedupe"],
        )

    async def test_scheduled_occurrence_is_atomic_across_failure_and_capacity(self):
        self.blocked = True
        queue = await self.make_queue()
        scheduled_at = self.clock()
        identities = [
            permissions.Identity("qq-bot", "10002"),
            permissions.Identity("qq-bot", "10001"),
        ]
        self.kv.fail_at = len(self.kv.writes) + 1
        with self.assertRaises(storage.CommitUnconfirmedError):
            await self.enqueue_scheduled(
                queue, scheduled_at=scheduled_at, identities=identities
            )
        occurrence_key = f"schedule:task_one:{scheduled_at.isoformat()}"
        self.assertEqual(self.store.snapshot()["batches"], {})
        pending = copy.deepcopy(self.kv.writes[-1])
        self.assertEqual(len(pending["batches"]), 2)
        self.assertEqual(len(pending["outbox"]), 2)
        self.assertEqual(pending["dedupe"][occurrence_key]["status"], "admitted")

        self.kv.fail_at = None
        await self.store.retry_pending()
        duplicate = await self.enqueue_scheduled(
            queue, scheduled_at=scheduled_at, identities=identities
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(set(duplicate["run_ids"]), set(pending["batches"]))

        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        original_limit = execution_queue.MAX_ACTIVE_ITEMS
        execution_queue.MAX_ACTIVE_ITEMS = 2
        try:
            with self.assertRaises(execution_queue.QueueRejected) as raised:
                await self.enqueue_scheduled(
                    queue,
                    task=task_two,
                    scheduled_at=scheduled_at + timedelta(minutes=1),
                    identities=[permissions.Identity("qq-bot", "10003")],
                )
        finally:
            execution_queue.MAX_ACTIVE_ITEMS = original_limit
        self.assertEqual(raised.exception.reason, "queue_capacity")
        snapshot = self.store.snapshot()
        rejected_key = (
            f"schedule:task_two:{(scheduled_at + timedelta(minutes=1)).isoformat()}"
        )
        self.assertNotIn(rejected_key, snapshot["dedupe"])
        self.assertEqual(set(snapshot["batches"]), set(pending["batches"]))

    async def test_scheduled_group_is_sorted_contiguous_and_fifo_with_manual_work(self):
        self.blocked = True
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        queue = await self.make_queue()
        manual = await self.enqueue(queue, task=task_two)
        identities = [
            permissions.Identity("qq-bot", "10003"),
            permissions.Identity("qq-bot", "10001"),
            permissions.Identity("qq-bot", "10002"),
        ]
        admitted = await self.enqueue_scheduled(
            queue, scheduled_at=self.clock(), identities=identities
        )
        run_ids = admitted["run_ids"]
        snapshot = self.store.snapshot()
        self.assertEqual(
            [snapshot["batches"][run_id]["qq_id"] for run_id in run_ids],
            ["10001", "10002", "10003"],
        )
        self.assertEqual(
            [snapshot["batches"][run_id]["sequence"] for run_id in run_ids],
            [2, 3, 4],
        )
        self.assertEqual(
            len({snapshot["batches"][run_id]["batch_id"] for run_id in run_ids}),
            1,
        )

        self.blocked = False
        queue.wake()
        await wait_until(lambda: self.batch(run_ids[-1])["status"] == "finished")
        self.assertEqual(self.execute_calls, [manual, *run_ids])

    async def test_automatic_bypasses_and_does_not_rewrite_manual_cooldown(self):
        self.raw_config["manual_cooldown_seconds"] = 300
        queue = await self.make_queue()
        manual = await self.enqueue(queue)
        await wait_until(lambda: self.batch(manual)["status"] == "finished")
        manual_key = "manual:" + self.batch(manual)["binding_key"]
        original_cooldown = copy.deepcopy(
            self.store.snapshot()["cooldowns"][manual_key]
        )

        automatic = await self.enqueue_scheduled(
            queue, scheduled_at=self.clock() + timedelta(minutes=1)
        )
        run_id = automatic["run_ids"][0]
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["cooldowns"][manual_key], original_cooldown)
        self.assertEqual(snapshot["outbox"][run_id]["phase"], "suppressed")
        with self.assertRaises(execution_queue.QueueRejected) as raised:
            await self.enqueue(queue)
        self.assertEqual(raised.exception.reason, "cooldown_active")

        self.raw_config["history_retention_days"] = 0
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        await self.enqueue(
            queue,
            identity=permissions.Identity("qq-bot", "10002"),
            task=task_two,
        )
        self.assertNotIn(run_id, self.store.snapshot()["batches"])

    async def test_manual_request_merges_into_running_automatic_notice_and_cooldown(
        self,
    ):
        self.raw_config["manual_cooldown_seconds"] = 300
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def execute(item):
            calls.append(item["run_id"])
            started.set()
            await release.wait()

            def finish_run(snapshot):
                snapshot["runs"][item["run_id"]] = {
                    "phase": "finished",
                    "finished_at": self.clock().isoformat(),
                    "cleanup_status": "cleaned",
                    "cleanup_finished_at": self.clock().isoformat(),
                    "business_attempted": True,
                    **_RESULT,
                }

            confirmed = await self.store.mutate(finish_run)
            return confirmed["runs"][item["run_id"]]

        queue = await self.make_queue(execute=execute)
        automatic = await self.enqueue_scheduled(queue, scheduled_at=self.clock())
        run_id = automatic["run_ids"][0]
        await asyncio.wait_for(started.wait(), 2)
        merged = await self.enqueue(queue)
        self.assertEqual(merged, run_id)
        running = self.batch(run_id)
        self.assertEqual(running["source"], "automatic")
        self.assertIs(running["manual_requested"], True)
        self.assertEqual(running["origin"], "origin:test")
        self.assertEqual(list(self.store.snapshot()["outbox"]), [run_id])

        release.set()
        await wait_until(lambda: self.batch(run_id)["status"] == "finished")
        snapshot = self.store.snapshot()
        notice = snapshot["outbox"][run_id]
        self.assertEqual(calls, [run_id])
        self.assertEqual(notice["phase"], "pending")
        self.assertIs(notice["manual_requested"], True)
        self.assertTrue(notice["message"].startswith("手动签到结果\n"))
        self.assertIn(
            "manual:" + self.batch(run_id)["binding_key"], snapshot["cooldowns"]
        )

    async def test_manual_merge_at_batch_capacity_preserves_unexpired_history_and_reservation(
        self,
    ):
        self.blocked = True
        queue = await self.make_queue()
        automatic = await self.enqueue_scheduled(queue, scheduled_at=self.clock())
        active_run = automatic["run_ids"][0]

        def fill_history(snapshot):
            used = set(snapshot["batches"])
            for index in range(999):
                candidate = index + 1
                run_id = f"{candidate:032x}"
                while run_id in used:
                    candidate += 1000
                    run_id = f"{candidate:032x}"
                used.add(run_id)
                identity = permissions.Identity("history-bot", f"9{index:05d}")
                snapshot["batches"][run_id] = {
                    "kind": "checkin",
                    "run_id": run_id,
                    "platform_id": identity.platform_id,
                    "qq_id": identity.qq_id,
                    "user_key": execution_queue.user_key(identity),
                    "binding_key": execution_queue.binding_key(
                        identity, self.base_task.task_id
                    ),
                    "task_id": self.base_task.task_id,
                    "task_name": self.base_task.name,
                    "task_version": self.base_task.version,
                    "artifact_sha256": "4" * 64,
                    "inputs_sha256": "5" * 64,
                    "origin": f"origin:history:{index}",
                    "queued_at": self.clock().isoformat(),
                    "expires_at": (self.clock() + timedelta(minutes=15)).isoformat(),
                    "source": "manual",
                    "manual_requested": True,
                    "scheduled_at": None,
                    "batch_id": None,
                    "sequence": index + 2,
                    "status": "finished",
                    "finished_at": self.clock().isoformat(),
                    "result": dict(_RESULT),
                }
                snapshot["outbox"][run_id] = {
                    "kind": "terminal_notification",
                    "phase": "sent",
                }

        await self.store.mutate(fill_history)
        before = self.store.snapshot()
        before_batches = set(before["batches"])
        before_outbox = set(before["outbox"])
        reservation = copy.deepcopy(before["outbox"][active_run])
        self.assertEqual(len(before_batches), execution_queue.MAX_BATCH_ITEMS)
        self.assertEqual(len(before_outbox), notifications.MAX_OUTBOX_ITEMS)

        merged = await self.enqueue(queue)
        after = self.store.snapshot()
        self.assertEqual(merged, active_run)
        self.assertEqual(set(after["batches"]), before_batches)
        self.assertEqual(set(after["outbox"]), before_outbox)
        self.assertEqual(after["outbox"][active_run], reservation)
        self.assertEqual(after["outbox"][active_run]["phase"], "reserved")
        self.assertIs(after["batches"][active_run]["manual_requested"], True)

    async def test_schedule_capacity_retains_unsettled_occurrence_then_prunes_when_settled(
        self,
    ):
        failed_result = {
            **_RESULT,
            "exit_code": 1,
            "business_status": "failed",
        }

        async def execute(item):
            def finish_run(snapshot):
                snapshot["runs"][item["run_id"]] = {
                    "phase": "finished",
                    "finished_at": self.clock().isoformat(),
                    "cleanup_status": "cleaned",
                    "cleanup_finished_at": self.clock().isoformat(),
                    "business_attempted": True,
                    **failed_result,
                }

            confirmed = await self.store.mutate(finish_run)
            return confirmed["runs"][item["run_id"]]

        queue = await self.make_queue(execute=execute)
        first = await self.enqueue_scheduled(queue, scheduled_at=self.clock())
        first_run = first["run_ids"][0]
        await wait_until(lambda: self.batch(first_run)["status"] == "finished")
        snapshot = self.store.snapshot()
        occurrence_key = f"schedule:task_one:{self.clock().isoformat()}"
        self.assertIn(occurrence_key, snapshot["dedupe"])
        self.assertEqual(snapshot["outbox"][first_run]["phase"], "pending")

        self.blocked = True
        self.raw_config["history_retention_days"] = 0
        task_two = replace(
            self.base_task,
            task_id="task_two",
            name="Task Two",
            version="6" * 64,
        )
        next_at = self.clock() + timedelta(minutes=1)
        next_key = f"schedule:task_two:{next_at.isoformat()}"
        original_limit = execution_queue.MAX_SCHEDULED_BATCHES
        execution_queue.MAX_SCHEDULED_BATCHES = 1
        try:
            with self.assertRaises(execution_queue.QueueRejected) as raised:
                await self.enqueue_scheduled(
                    queue,
                    task=task_two,
                    scheduled_at=next_at,
                    identities=[],
                )
            self.assertEqual(raised.exception.reason, "queue_capacity")
            retained = self.store.snapshot()
            self.assertIn(occurrence_key, retained["dedupe"])
            self.assertNotIn(next_key, retained["dedupe"])
            self.assertEqual(retained["outbox"][first_run]["phase"], "pending")

            await self.store.mutate(
                lambda current: current["outbox"][first_run].update(phase="sent")
            )
            admitted = await self.enqueue_scheduled(
                queue,
                task=task_two,
                scheduled_at=next_at,
                identities=[],
            )
        finally:
            execution_queue.MAX_SCHEDULED_BATCHES = original_limit

        self.assertEqual(admitted, {"status": "skipped_empty", "run_ids": []})
        settled = self.store.snapshot()
        self.assertNotIn(occurrence_key, settled["dedupe"])
        self.assertIn(next_key, settled["dedupe"])
        self.assertNotIn(first_run, settled["batches"])
        self.assertNotIn(first_run, settled["outbox"])

    async def test_close_is_cancellation_safe_and_closed_waits_for_execute_exit(self):
        started = asyncio.Event()
        release = asyncio.Event()
        exited = asyncio.Event()

        async def execute(item):
            started.set()
            try:
                await release.wait()
            finally:
                exited.set()

        queue = await self.make_queue(execute=execute)
        await self.enqueue(queue)
        await asyncio.wait_for(started.wait(), 2)
        close_waiter = asyncio.create_task(queue.close())
        await asyncio.sleep(0)
        close_waiter.cancel()
        await asyncio.gather(close_waiter, return_exceptions=True)
        await asyncio.wait_for(exited.wait(), 2)
        await wait_until(lambda: queue.closed)
        self.assertTrue(queue.closing)
