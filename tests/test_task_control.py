import asyncio
import copy
import unittest

from astrbot_support import AstrBotConfig, FakeKV, PutGate
from state_support import load_module

state = load_module("state", "_task_control_tests")
storage = load_module("storage", "_task_control_tests")
task_config = load_module("task_config", "_task_control_tests")
task_control = load_module("task_control", "_task_control_tests")


def raw_task(task_id="task_one", *, enabled=True, name=None):
    commit = "a" * 40
    return {
        "__template_key": "task",
        "task_id": task_id,
        "name": name or task_id.replace("_", " ").title(),
        "enabled": enabled,
        "runtime": "python",
        "script_url": (
            f"https://raw.githubusercontent.com/example/checkin/{commit}/{task_id}.py"
        ),
        "source_commit": commit,
        "sha256": "b" * 64,
        "cron": "0 0 * * *",
        "user_fields": [],
        "fixed_env": {},
        "input_adapter": "direct_env",
        "result_adapter": "json_protocol",
        "dependency_bundle": "none",
        "timeout_seconds": 30,
        "experimental": False,
    }


def intent(task_id):
    return {"kind": "task_disable_intent", "task_id": task_id}


class TaskControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = FakeKV()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        self.service = None
        self.config = None

    async def asyncTearDown(self):
        if self.service is not None and not self.service.closed:
            await self.service.close()
        if not self.store.closed:
            if self.store.has_pending:
                await self.store.retry_pending()
            await self.store.close()

    def make_service(self, config=None):
        self.config = config or AstrBotConfig(
            enabled=True,
            timezone="UTC",
            tasks=[raw_task(), raw_task("task_two")],
            future_option={"preserve": [1, 2, 3]},
        )
        self.service = task_control.TaskControlService(self.store, self.config)
        return self.service

    async def restart(self, config):
        if self.service is not None and not self.service.closed:
            await self.service.close()
        await self.store.close()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        self.config = config
        self.service = task_control.TaskControlService(self.store, config)

    def test_marker_helpers_validate_only_task_disable_intents(self):
        snapshot = state.new_snapshot()
        snapshot["dedupe"]["unrelated"] = "an unrelated record shape"
        snapshot["dedupe"]["schedule:item"] = {
            "kind": "scheduled_checkin",
            "nested": [object.__name__],
        }
        self.assertEqual(task_control.disabling_task_ids(snapshot), set())
        self.assertFalse(task_control.task_is_disabling(snapshot, "task_one"))

        snapshot["dedupe"]["task_disable:task_one"] = intent("task_one")
        self.assertEqual(task_control.disabling_task_ids(snapshot), {"task_one"})
        self.assertTrue(task_control.task_is_disabling(snapshot, "task_one"))
        self.assertFalse(task_control.task_is_disabling(snapshot, "task_two"))

        corrupt_records = (
            {"task_disable:task_one": {"kind": "other", "task_id": "task_one"}},
            {"task_disable:task_one": {"kind": "task_disable_intent"}},
            {
                "task_disable:task_one": {
                    "kind": "task_disable_intent",
                    "task_id": "task_two",
                }
            },
            {
                "task_disable:task_one": {
                    "kind": "task_disable_intent",
                    "task_id": "task_one",
                    "extra": True,
                }
            },
            {"different:key": intent("task_one")},
        )
        for dedupe in corrupt_records:
            candidate = state.new_snapshot()
            candidate["dedupe"].update(dedupe)
            with (
                self.subTest(dedupe=dedupe),
                self.assertRaises(task_control.TaskControlError),
            ):
                task_control.disabling_task_ids(candidate)

        excess = state.new_snapshot()
        for index in range(task_control.MAX_PENDING_TASK_DISABLES + 1):
            task_id = f"excess_{index}"
            excess["dedupe"][f"task_disable:{task_id}"] = intent(task_id)
        with self.assertRaises(task_control.TaskControlError):
            task_control.disabling_task_ids(excess)

    async def test_missing_task_has_no_state_or_config_side_effect(self):
        service = self.make_service()
        writes_before = self.kv.put_attempts
        guard_calls = 0

        def guard():
            nonlocal guard_calls
            guard_calls += 1

        with self.assertRaises(task_control.TaskControlNotFound):
            await service.disable("missing_task", guard=guard)

        self.assertEqual(guard_calls, 1)
        self.assertEqual(self.kv.put_attempts, writes_before)
        self.assertEqual(self.config.saved_configs, [])
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())
        self.assertTrue(self.config["tasks"][0]["enabled"])

    async def test_guard_is_rechecked_after_waiting_for_state_lock(self):
        service = self.make_service()
        first_check = asyncio.Event()
        allowed = True
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            first_check.set()
            if not allowed:
                raise PermissionError("administrator permission was revoked")

        writes_before = self.kv.put_attempts
        async with self.store._lock:
            request = asyncio.create_task(service.disable("task_one", guard=guard))
            await first_check.wait()
            allowed = False
        with self.assertRaises(PermissionError):
            await request

        self.assertEqual(calls, 2)
        self.assertEqual(self.kv.put_attempts, writes_before)
        self.assertEqual(self.config.saved_configs, [])
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())

    async def test_intent_and_all_active_cancellations_commit_atomically(self):
        service = self.make_service()
        reserve = "r" * 160
        already_reserved = "a" * 96

        def seed(snapshot):
            snapshot["bindings"]["keep"] = {"secret": "untouched"}
            snapshot["batches"].update(
                {
                    "queued": {
                        "kind": "checkin",
                        "task_id": "task_one",
                        "status": "queued",
                        "_reserve": reserve,
                    },
                    "running": {
                        "kind": "checkin",
                        "task_id": "task_one",
                        "status": "running",
                        "_reserve": reserve,
                    },
                    "already": {
                        "kind": "checkin",
                        "task_id": "task_one",
                        "status": "running",
                        "cancel_requested": True,
                        "_reserve": already_reserved,
                    },
                    "finished": {
                        "kind": "checkin",
                        "task_id": "task_one",
                        "status": "finished",
                    },
                    "unrelated": {
                        "kind": "checkin",
                        "task_id": "task_two",
                        "status": "queued",
                        "_reserve": reserve,
                    },
                    "foreign": {
                        "kind": "cleanup",
                        "task_id": "task_one",
                        "status": "queued",
                        "_reserve": reserve,
                    },
                }
            )

        await self.store.mutate(seed)
        commits_before = len(self.kv.committed)
        guard_calls = 0

        def guard():
            nonlocal guard_calls
            guard_calls += 1

        await service.disable("task_one", guard=guard)

        self.assertEqual(guard_calls, 2)
        operation_commits = self.kv.committed[commits_before:]
        self.assertEqual(len(operation_commits), 2)
        intent_commit, retirement_commit = operation_commits
        self.assertEqual(
            intent_commit["dedupe"]["task_disable:task_one"], intent("task_one")
        )
        for run_id in ("queued", "running"):
            self.assertTrue(intent_commit["batches"][run_id]["cancel_requested"])
            self.assertEqual(
                intent_commit["batches"][run_id]["_reserve"], reserve[:-64]
            )
        self.assertEqual(
            intent_commit["batches"]["already"]["_reserve"], already_reserved
        )
        self.assertNotIn("cancel_requested", intent_commit["batches"]["finished"])
        self.assertNotIn("cancel_requested", intent_commit["batches"]["unrelated"])
        self.assertNotIn("cancel_requested", intent_commit["batches"]["foreign"])
        self.assertNotIn("task_disable:task_one", retirement_commit["dedupe"])
        self.assertEqual(
            retirement_commit["bindings"], {"keep": {"secret": "untouched"}}
        )
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertTrue(self.config["tasks"][1]["enabled"])
        self.assertEqual(
            self.config.persisted_config["future_option"], {"preserve": [1, 2, 3]}
        )

    async def test_invalid_target_batch_rolls_back_intent_and_flags_together(self):
        service = self.make_service()

        def seed(snapshot):
            snapshot["batches"]["broken"] = {
                "kind": "checkin",
                "task_id": "task_one",
                "status": "queued",
                "cancel_requested": "yes",
                "_reserve": "r" * 128,
            }

        await self.store.mutate(seed)
        writes_before = self.kv.put_attempts
        with self.assertRaises(task_control.TaskControlError):
            await service.disable("task_one", guard=lambda: None)

        self.assertEqual(self.kv.put_attempts, writes_before)
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())
        self.assertEqual(self.config.saved_configs, [])

    async def test_latest_config_edits_after_kv_barrier_are_preserved(self):
        service = self.make_service()
        gate = self.kv.block_next_put()
        request = asyncio.create_task(service.disable("task_one", guard=lambda: None))
        await gate.started.wait()

        self.assertEqual(self.config.saved_configs, [])
        self.assertTrue(self.config["tasks"][0]["enabled"])
        self.config["tasks"][0]["name"] = "Newest Task Name"
        self.config["tasks"][1]["cron"] = "5 1 * * *"
        self.config["future_option"] = {"latest": {"nested": True}}
        gate.release.set()
        await request

        saved = self.config.saved_configs[-1]
        self.assertEqual(saved["tasks"][0]["name"], "Newest Task Name")
        self.assertFalse(saved["tasks"][0]["enabled"])
        self.assertEqual(saved["tasks"][1]["cron"], "5 1 * * *")
        self.assertEqual(saved["future_option"], {"latest": {"nested": True}})
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())

    async def test_unconfirmed_kv_write_never_starts_config_save(self):
        service = self.make_service()
        self.kv.fail_next_put()

        with self.assertRaises(storage.CommitUnconfirmedError):
            await service.disable("task_one", guard=lambda: None)

        self.assertTrue(self.store.has_pending)
        self.assertFalse(self.store.ready)
        self.assertEqual(self.config.saved_configs, [])
        self.assertTrue(self.config["tasks"][0]["enabled"])
        confirmed = self.store.snapshot()
        self.assertEqual(task_control.disabling_task_ids(confirmed), set())

    async def test_false_save_keeps_intent_and_restart_recovery_uses_persisted_config(
        self,
    ):
        service = self.make_service()
        await self.config.save_config_async()
        original_persisted = copy.deepcopy(self.config.persisted_config)
        self.config.saved_configs.clear()
        self.config.save_actions.append(False)

        with self.assertRaises(task_control.TaskControlSaveUnconfirmed):
            await service.disable("task_one", guard=lambda: None)

        self.assertTrue(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertTrue(original_persisted["tasks"][0]["enabled"])
        self.assertEqual(self.config.persisted_config, original_persisted)
        self.assertEqual(self.config.saved_configs, [])

        successor_config = AstrBotConfig(copy.deepcopy(original_persisted))
        await self.restart(successor_config)
        await self.service.recover()

        self.assertFalse(successor_config["tasks"][0]["enabled"])
        self.assertFalse(successor_config.persisted_config["tasks"][0]["enabled"])
        self.assertEqual(len(successor_config.saved_configs), 1)
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())

    async def test_config_exception_is_save_unconfirmed_and_keeps_intent(self):
        service = self.make_service()
        self.config.save_actions.append(OSError("simulated config writer failure"))

        with self.assertRaises(task_control.TaskControlSaveUnconfirmed) as raised:
            await service.disable("task_one", guard=lambda: None)

        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertTrue(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertIsNone(self.config.persisted_config)
        self.assertEqual(self.config.saved_configs, [])

    async def test_removed_pending_target_saves_current_config_unchanged_then_retires(
        self,
    ):
        service = self.make_service()
        self.config.save_actions.append(False)
        with self.assertRaises(task_control.TaskControlSaveUnconfirmed):
            await service.disable("task_one", guard=lambda: None)

        self.config["tasks"] = [raw_task("task_two", name="Latest Other Task")]
        self.config["future_option"] = {"edited": ["after", "failure"]}
        expected = copy.deepcopy(dict(self.config))
        await service.recover()

        self.assertEqual(self.config.persisted_config, expected)
        self.assertEqual(self.config.saved_configs[-1], expected)
        self.assertEqual(self.config["tasks"], expected["tasks"])
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())

    async def test_schema_default_disabled_task_saves_config_without_materializing_tasks(
        self,
    ):
        config = AstrBotConfig(
            enabled=True,
            timezone="Asia/Shanghai",
            future_option={"keep": "exactly"},
        )
        service = self.make_service(config)
        default_catalog = task_config.parse_task_catalog(config)
        target = next(
            task for task in default_catalog.tasks if task.task_id == "tianyi_cloud"
        )
        self.assertFalse(target.enabled)
        self.assertNotIn("tasks", config)
        original = copy.deepcopy(dict(config))

        await service.disable("tianyi_cloud", guard=lambda: None)

        self.assertNotIn("tasks", config)
        self.assertEqual(dict(config), original)
        self.assertEqual(config.persisted_config, original)
        self.assertEqual(config.saved_configs, [original])
        self.assertEqual(task_control.disabling_task_ids(self.store.snapshot()), set())

    async def test_reenable_while_retirement_waits_for_kv_lock_keeps_intent(self):
        service = self.make_service()
        gate = PutGate()
        self.config.save_actions.append(gate)
        request = asyncio.create_task(service.disable("task_one", guard=lambda: None))
        await gate.started.wait()
        async with self.store._lock:
            gate.release.set()
            await asyncio.sleep(0)
            self.assertEqual(len(self.config.saved_configs), 1)
            self.assertFalse(request.done())
            self.config["tasks"][0]["enabled"] = True
            await self.config.save_config_async()
        with self.assertRaises(task_control.TaskControlSaveUnconfirmed):
            await request
        self.assertEqual(
            task_control.disabling_task_ids(self.store.snapshot()), {"task_one"}
        )
        self.assertEqual(
            self.kv.values[state.STATE_KEY]["dedupe"]["task_disable:task_one"],
            intent("task_one"),
        )
        self.assertTrue(self.config.persisted_config["tasks"][0]["enabled"])

    async def test_true_save_does_not_retire_if_current_config_reenables_target(self):
        service = self.make_service()
        gate = PutGate()
        self.config.save_actions.append(gate)
        request = asyncio.create_task(service.disable("task_one", guard=lambda: None))
        await gate.started.wait()

        self.assertTrue(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )
        self.config["tasks"][0]["enabled"] = True
        gate.release.set()
        with self.assertRaises(task_control.TaskControlSaveUnconfirmed):
            await request

        self.assertFalse(self.config.persisted_config["tasks"][0]["enabled"])
        self.assertTrue(self.config["tasks"][0]["enabled"])
        self.assertTrue(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )

    async def test_caller_cancellation_is_shielded_and_close_drains_save(self):
        service = self.make_service()
        gate = PutGate()
        self.config.save_actions.append(gate)
        request = asyncio.create_task(service.disable("task_one", guard=lambda: None))
        await gate.started.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request

        self.assertTrue(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        with self.assertRaises(task_control.TaskControlError):
            await service.disable("task_one", guard=lambda: None)

        gate.release.set()
        await closing
        self.assertTrue(service.closed)
        self.assertFalse(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )
        self.assertFalse(self.config.persisted_config["tasks"][0]["enabled"])
        self.assertEqual(len(self.config.saved_configs), 1)

    async def test_pending_intent_limit_rejects_new_task_without_overwrite(self):
        service = self.make_service()

        def seed(snapshot):
            for index in range(task_control.MAX_PENDING_TASK_DISABLES):
                task_id = f"pending_{index}"
                snapshot["dedupe"][f"task_disable:{task_id}"] = intent(task_id)

        await self.store.mutate(seed)
        before = self.store.snapshot()
        writes_before = self.kv.put_attempts
        with self.assertRaises(task_control.TaskControlError):
            await service.disable("task_one", guard=lambda: None)

        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.kv.put_attempts, writes_before)
        self.assertEqual(self.config.saved_configs, [])

    async def test_recovery_rejects_invalid_latest_config_without_retiring(self):
        service = self.make_service()

        def seed(snapshot):
            snapshot["dedupe"]["task_disable:task_one"] = intent("task_one")

        await self.store.mutate(seed)
        self.config["tasks"] = [{"task_id": "task_one", "enabled": True}]

        with self.assertRaises(task_control.TaskControlSaveUnconfirmed):
            await service.recover()

        self.assertTrue(
            task_control.task_is_disabling(self.store.snapshot(), "task_one")
        )
        self.assertEqual(self.config.saved_configs, [])
