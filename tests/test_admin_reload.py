import asyncio
from unittest.mock import patch

from astrbot_support import FakeEvent, PutGate
from reload_support import ControlledReloadManager, StarMetadata
from test_manual_execution import ExecutionFixture

_ARGUMENT_SECRET = "SECRET_ARBITRARY_PLUGIN_TARGET"
_EVENT_SECRET = "SECRET_EVENT_PAYLOAD"


class BlockedFinalReply(FakeEvent):
    def __init__(self):
        super().__init__(sender_id="90001", astrbot_admin=True)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, result):
        if len(self.sent) == 1:
            self.started.set()
            await self.release.wait()
        await super().send(result)


class AdminReloadTests(ExecutionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager = ControlledReloadManager(self.context, self.plugin)
        self.addAsyncCleanup(self.manager.close)

    @staticmethod
    def admin_event(**changes):
        options = {"sender_id": "90001", "astrbot_admin": True}
        options.update(changes)
        return FakeEvent(**options)

    @staticmethod
    def feedback(event):
        return [result.text for result in event.sent]

    def assert_intercepted(self, event):
        self.assertFalse(event.llm_enabled)
        self.assertTrue(event.stopped)

    def assert_safe_feedback(self, event, *details):
        messages = self.feedback(event)
        self.assertTrue(messages)
        rendered = "\n".join(messages)
        for detail in details:
            self.assertNotIn(detail, rendered)

    async def reload(self, event=None, plugin=None, *, extra="", tail=""):
        event = event or self.admin_event()
        plugin = plugin or self.plugin
        await plugin.checkin(event, "管理", "重载", extra, tail)
        return event

    async def wait_for_acceptance(self, event):
        await self.until(lambda: bool(event.sent))

    async def test_success_replaces_exact_instance_and_preserves_owned_state(self):
        old = self.plugin
        old_store = old.state_store
        old_queue = old.queue
        old_scheduler = old.scheduler
        old_task_control = old.task_control
        old_executor = old.executor
        old_preparation = old.preparation
        backend = self.backend
        metadata = self.manager.current_metadata
        module_path = type(old).__module__
        self.config["enabled"] = False
        self.config["plugin_admin_qqs"] = ["10001"]
        event = FakeEvent(sender_id="10001")

        await self.reload(event)
        replacement = self.manager.current_plugin
        self.plugin = replacement
        await self.until(lambda: old._state_slot().get("reload_task") is None)

        self.assert_intercepted(event)
        self.assertEqual(len(event.sent), 2)
        self.assert_safe_feedback(event, _ARGUMENT_SECRET, _EVENT_SECRET)
        self.assertIsNot(replacement, old)
        self.assertIs(type(replacement), type(old))
        self.assertEqual(type(replacement).__module__, module_path)
        self.assertEqual(replacement.plugin_id, old.plugin_id)
        self.assertIs(replacement.config, old.config)
        self.assertIs(self.context.kv_for(replacement.plugin_id), backend)
        self.assertTrue(old_store.closed)
        self.assertTrue(old_queue.closed)
        self.assertTrue(old_scheduler.closed)
        self.assertTrue(old_task_control.closed)
        self.assertTrue(old_executor.quiescent)
        self.assertTrue(old_preparation._closed)
        self.assertTrue(old.notifications._closed)
        self.assertTrue(replacement.state_store.ready)
        self.assertFalse(replacement.state_store.closed)
        self.assertIs(replacement._state_slot()["owner"], replacement.state_store)
        self.assertIsNone(replacement._state_slot().get("reload_task"))
        self.assertEqual(
            [operation[0] for operation in self.manager.operations],
            ["terminate", "unbind", "load"],
        )
        self.assertEqual(self.manager.terminate_calls, [metadata])
        self.assertEqual(
            self.manager.unbind_calls,
            [(self.manager.plugin_name, module_path)],
        )
        self.assertEqual(self.manager.load_calls, [module_path])
        registered = self.context.get_registered_star(self.manager.plugin_name)
        self.assertIs(registered, self.manager.current_metadata)
        self.assertIs(registered.star_cls, replacement)
        self.assertEqual(registered.module_path, module_path)
        self.assertTrue(registered.activated)
        self.assertIn(self.manager.other_metadata, self.context.get_all_stars())
        self.assertIs(self.manager.other_metadata.star_cls, self.manager.other_plugin)

    async def test_only_current_ordinary_private_admin_and_no_arguments_are_accepted(
        self,
    ):
        rejected = [
            FakeEvent(sender_id="10001"),
            FakeEvent(sender_id="10001", astrbot_admin=True),
            self.admin_event(platform_name="telegram"),
            self.admin_event(
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": "90001",
                }
            ),
            self.admin_event(
                raw_message={
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "group",
                    "user_id": "90001",
                    "sender": {"user_id": "90001"},
                }
            ),
            self.admin_event(raw_message=None),
        ]
        for event in rejected:
            with self.subTest(
                platform=event.get_platform_name(),
                raw=event.message_obj.raw_message,
            ):
                await self.reload(event)
                self.assert_intercepted(event)
        self.context.config["admins_id"] = []
        stale = self.admin_event()
        await self.reload(stale)
        self.context.config["admins_id"] = ["90001"]

        extra = self.admin_event(text=_EVENT_SECRET)
        await self.reload(extra, extra=_ARGUMENT_SECRET)
        tail = self.admin_event(text=_EVENT_SECRET)
        await self.reload(tail, tail=_ARGUMENT_SECRET)

        self.assertEqual(self.manager.operations, [])
        self.assertIs(self.manager.current_plugin, self.plugin)
        self.assertTrue(self.plugin.state_store.ready)
        self.assertIsNone(self.plugin._state_slot().get("reload_task"))
        for event in [*rejected, stale, extra, tail]:
            rendered = "\n".join(self.feedback(event))
            self.assertNotIn(_ARGUMENT_SECRET, rendered)
            self.assertNotIn(_EVENT_SECRET, rendered)

    async def test_overlapping_requests_share_one_context_slot_and_one_lifecycle(self):
        await self.manager._pm_lock.acquire()
        first_event = self.admin_event()
        first_handler = asyncio.create_task(self.reload(first_event))
        try:
            await self.wait_for_acceptance(first_event)
            slot_task = self.plugin._state_slot().get("reload_task")
            self.assertIsNotNone(slot_task)
            self.assertFalse(slot_task.done())

            second_event = self.admin_event()
            await self.reload(second_event)
            self.assertEqual(len(second_event.sent), 1)
            self.assertIs(self.plugin._state_slot().get("reload_task"), slot_task)
            self.assertEqual(self.manager.operations, [])
        finally:
            self.manager._pm_lock.release()

        await first_handler
        self.plugin = self.manager.current_plugin
        await self.until(lambda: self.plugin._state_slot().get("reload_task") is None)
        self.assertEqual(len(first_event.sent), 2)
        self.assertEqual(len(self.manager.terminate_calls), 1)
        self.assertEqual(len(self.manager.unbind_calls), 1)
        self.assertEqual(len(self.manager.load_calls), 1)
        self.assertIs(
            self.context.get_registered_star(self.manager.plugin_name).star_cls,
            self.plugin,
        )

    async def test_revoked_admin_while_waiting_for_manager_lock_never_terminates(self):
        await self.manager._pm_lock.acquire()
        event = self.admin_event()
        handler = asyncio.create_task(self.reload(event))
        try:
            await self.wait_for_acceptance(event)
            self.context.config["admins_id"] = []
        finally:
            self.manager._pm_lock.release()
        await handler

        self.assertEqual(self.manager.operations, [])
        self.assertFalse(self.plugin._terminating)
        self.assertTrue(self.plugin.state_store.ready)
        self.assertIs(
            self.context.get_registered_star(self.manager.plugin_name).star_cls,
            self.plugin,
        )
        self.assertEqual(len(event.sent), 2)
        self.assert_safe_feedback(event, _EVENT_SECRET)

    async def test_registry_changes_while_waiting_never_touch_any_plugin(self):
        failures = []
        for change in ("gone", "renamed", "collision"):
            with self.subTest(change=change):
                target = self.manager.current_metadata
                await self.manager._pm_lock.acquire()
                event = self.admin_event()
                handler = asyncio.create_task(self.reload(event))
                try:
                    await self.wait_for_acceptance(event)
                    if change == "gone":
                        self.context.stars.remove(target)
                    elif change == "renamed":
                        target.name = "changed_plugin_name"
                    else:
                        self.context.stars.append(
                            StarMetadata(
                                name=self.manager.plugin_name,
                                module_path="tests.colliding_plugin.main",
                                star_cls=object(),
                            )
                        )
                finally:
                    self.manager._pm_lock.release()
                await handler
                failures.append(event.sent[-1].text)
                self.assertEqual(self.manager.operations, [])
                self.assertTrue(self.plugin.state_store.ready)
                self.assertIn(self.manager.other_metadata, self.context.stars)
                self.assertIs(
                    self.manager.other_metadata.star_cls, self.manager.other_plugin
                )
                self.manager.restore_target_metadata(self.plugin)
        self.assertEqual(len(set(failures)), 1)

    async def test_unconfirmed_kv_is_retried_before_replacement_owns_state(self):
        old = self.plugin
        self.backend.fail_next_put()
        with self.assertRaises(self.main.CommitUnconfirmedError):
            await old.state_store.mutate(
                lambda snapshot: snapshot["users"].update(
                    {"recoverable-user": {"enabled": True}}
                )
            )
        self.assertTrue(old.state_store.has_pending)

        event = await self.reload()
        self.plugin = self.manager.current_plugin

        self.assertEqual(len(event.sent), 2)
        self.assertTrue(old.state_store.closed)
        self.assertFalse(self.plugin.state_store.has_pending)
        self.assertTrue(self.plugin.state_store.ready)
        self.assertIs(self.plugin._state_slot()["owner"], self.plugin.state_store)
        self.assertIn("recoverable-user", self.plugin.state_store.snapshot()["users"])
        self.assertIs(self.context.kv_for(self.plugin.plugin_id), self.backend)

    async def test_second_kv_failure_stops_before_unbind_or_load(self):
        old = self.plugin
        metadata = self.manager.current_metadata
        self.backend.fail_next_put()
        with self.assertRaises(self.main.CommitUnconfirmedError):
            await old.state_store.mutate(
                lambda snapshot: snapshot["users"].update(
                    {"pending-user": {"enabled": True}}
                )
            )
        self.backend.fail_next_put()

        event = await self.reload()

        self.assertEqual(len(event.sent), 2)
        self.assertEqual(self.manager.terminate_calls, [metadata])
        self.assertEqual(self.manager.unbind_calls, [])
        self.assertEqual(self.manager.load_calls, [])
        self.assertFalse(old.state_store.closed)
        self.assertTrue(old.state_store.closing)
        self.assertIs(
            self.context.get_registered_star(self.manager.plugin_name), metadata
        )
        self.assert_safe_feedback(event, self.manager.loader_detail)

    async def test_active_unknown_is_not_replayed_and_fresh_queued_work_resumes(self):
        await self.bind()
        await self.bind("10002", "other-bot", "second")
        self.bay.hold_execution = True
        active, _ = await self.submit()
        await asyncio.wait_for(self.bay.executing.wait(), 5)
        queued, _ = await self.submit("10002", "other-bot")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(self.snapshot()["batches"][queued]["status"], "queued")

        event = await self.reload()
        self.plugin = self.manager.current_plugin
        self.bay.release_execution.set()
        active_result, queued_result = await asyncio.gather(
            self.finished(active), self.finished(queued)
        )

        self.assertEqual(len(event.sent), 2)
        self.assertEqual(active_result["result"]["business_status"], "unknown")
        self.assertEqual(queued_result["result"]["business_status"], "already_signed")
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(
            sum(active in text for _, text in self.context.sent),
            1,
        )
        self.assertEqual(
            sum(queued in text for _, text in self.context.sent),
            1,
        )
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.cargos, {})
        self.assertEqual(self.bay.sandboxes, {})

    async def test_known_business_result_finishes_cleanup_without_replay(self):
        await self.bind()
        self.bay.hold_cleanup = True
        run_id, _ = await self.submit()
        await asyncio.wait_for(self.bay.cleanup_started.wait(), 5)
        self.assertEqual(self.bay.execution_calls, 1)

        event = self.admin_event()
        handler = asyncio.create_task(self.reload(event))
        await self.manager.terminate_started.wait()
        self.assertFalse(handler.done())
        self.bay.release_cleanup.set()
        await handler
        self.plugin = self.manager.current_plugin
        result = await self.finished(run_id)

        self.assertEqual(result["result"]["business_status"], "success")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(sum(run_id in text for _, text in self.context.sent), 1)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertTrue(self.manager.terminate_finished.is_set())

    async def test_reload_waits_for_admitted_configuration_save_before_unbinding(self):
        gate = PutGate()
        self.config.save_actions.append(gate)
        disable_event = self.admin_event()
        disable = asyncio.create_task(
            self.plugin.checkin(disable_event, "管理", "停用", self.task.task_id)
        )
        await gate.started.wait()

        old = self.plugin
        reload_event = self.admin_event()
        reloading = asyncio.create_task(self.reload(reload_event))
        await self.manager.terminate_started.wait()
        self.assertEqual(self.manager.unbind_calls, [])
        self.assertEqual(self.manager.load_calls, [])
        self.assertFalse(old.task_control.closed)
        self.assertFalse(old.state_store.closed)

        gate.release.set()
        await asyncio.gather(disable, reloading)
        self.plugin = self.manager.current_plugin

        self.assertTrue(old.task_control.closed)
        self.assertTrue(old.state_store.closed)
        self.assertEqual(len(self.manager.unbind_calls), 1)
        self.assertEqual(len(self.manager.load_calls), 1)
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertFalse(self.config.persisted_config["tasks"][0]["enabled"])
        self.assertEqual(
            self.main.disabling_task_ids(self.plugin.state_store.snapshot()), set()
        )
        self.assertIs(self.plugin._state_slot()["owner"], self.plugin.state_store)

    async def test_replacement_recovers_pending_disable_intent(self):
        self.config.save_actions.append(False)
        disable_event = self.admin_event()
        await self.plugin.checkin(disable_event, "管理", "停用", self.task.task_id)
        self.assertEqual(
            self.main.disabling_task_ids(self.snapshot()), {self.task.task_id}
        )
        self.assertFalse(self.config["tasks"][0]["enabled"])
        self.assertIsNone(self.config.persisted_config)

        event = await self.reload()
        self.plugin = self.manager.current_plugin

        self.assertEqual(len(event.sent), 2)
        self.assertTrue(self.plugin.state_store.ready)
        self.assertEqual(self.main.disabling_task_ids(self.snapshot()), set())
        self.assertFalse(self.config.persisted_config["tasks"][0]["enabled"])
        self.assertFalse(self.plugin.task_control.closed)

    async def test_cancelled_incoming_handler_does_not_cancel_accepted_reload(self):
        await self.manager._pm_lock.acquire()
        event = self.admin_event()
        handler = asyncio.create_task(self.reload(event))
        try:
            await self.wait_for_acceptance(event)
            job = self.plugin._state_slot()["reload_task"]
            handler.cancel()
            outcome = await asyncio.gather(handler, return_exceptions=True)
            self.assertIsInstance(outcome[0], asyncio.CancelledError)
            self.assertFalse(job.done())
        finally:
            self.manager._pm_lock.release()

        await job
        self.plugin = self.manager.current_plugin
        await self.until(lambda: self.plugin._state_slot().get("reload_task") is None)
        self.assertEqual(len(event.sent), 2)
        self.assertTrue(self.manager.loaded.is_set())
        self.assertTrue(self.plugin.state_store.ready)
        self.assertEqual(len(self.manager.load_calls), 1)

    async def test_external_terminate_cancels_reload_waiting_for_manager_lock(self):
        await self.manager._pm_lock.acquire()
        old = self.plugin
        event = self.admin_event()
        handler = asyncio.create_task(self.reload(event))
        try:
            await self.wait_for_acceptance(event)
            job = old._state_slot()["reload_task"]
            termination = asyncio.create_task(old.terminate())
            await termination
            outcomes = await asyncio.gather(handler, return_exceptions=True)
            self.assertIsInstance(outcomes[0], asyncio.CancelledError)
            self.assertTrue(job.cancelled())
        finally:
            self.manager._pm_lock.release()

        await asyncio.sleep(0)
        self.assertTrue(old.state_store.closed)
        self.assertEqual(self.manager.operations, [])
        self.assertIsNone(old._state_slot().get("reload_task"))
        self.assertIs(
            self.context.get_registered_star(self.manager.plugin_name).star_cls, old
        )

    async def test_external_terminate_during_reload_shutdown_has_no_join_cycle(self):
        gate = PutGate()
        self.config.save_actions.append(gate)
        disable = asyncio.create_task(
            self.plugin.checkin(self.admin_event(), "管理", "停用", self.task.task_id)
        )
        await gate.started.wait()

        old = self.plugin
        event = self.admin_event()
        handler = asyncio.create_task(self.reload(event))
        await self.manager.terminate_started.wait()
        host_shutdown = asyncio.create_task(old.terminate())
        await asyncio.sleep(0)
        self.assertFalse(host_shutdown.done())
        gate.release.set()
        async with asyncio.timeout(5):
            await asyncio.gather(disable, host_shutdown)
        outcome = await asyncio.gather(handler, return_exceptions=True)

        self.assertIsInstance(outcome[0], asyncio.CancelledError)
        self.assertTrue(old.state_store.closed)
        self.assertTrue(old.task_control.closed)
        self.assertEqual(len(self.manager.terminate_calls), 1)
        self.assertEqual(self.manager.unbind_calls, [])
        self.assertEqual(self.manager.load_calls, [])
        self.assertIsNone(old._state_slot().get("reload_task"))

    async def test_external_terminate_during_final_reply_drains_replacement(self):
        event = BlockedFinalReply()

        old = self.plugin
        handler = asyncio.create_task(self.reload(event))
        try:
            await asyncio.wait_for(event.started.wait(), 5)
            replacement = self.manager.current_plugin
            self.assertTrue(replacement.state_store.ready)
            async with asyncio.timeout(5):
                await old.terminate()
            await asyncio.gather(handler, return_exceptions=True)
            self.assertTrue(replacement.state_store.closed)
            self.assertTrue(replacement.queue.closed)
            self.assertTrue(replacement.scheduler.closed)
            self.assertTrue(replacement.executor.quiescent)
            self.assertIn(self.manager.other_metadata, self.context.get_all_stars())
            self.assertEqual(len(self.manager.load_calls), 1)
        finally:
            event.release.set()

    async def test_cleanup_completes_while_manager_holds_lock_before_termination(self):
        old = self.plugin
        event = BlockedFinalReply()
        handler = asyncio.create_task(self.reload(event))
        await asyncio.wait_for(event.started.wait(), 5)
        replacement = self.manager.current_plugin
        cleanup_started = asyncio.Event()
        original_cleanup = self.main.cleanup_reload_replacement

        async def observed_cleanup(plugin):
            cleanup_started.set()
            await original_cleanup(plugin)

        self.enterContext(
            patch.object(self.main, "cleanup_reload_replacement", observed_cleanup)
        )
        await self.manager._pm_lock.acquire()
        host_shutdown = asyncio.create_task(old.terminate())
        try:
            await asyncio.wait_for(cleanup_started.wait(), 5)
            # Pinned install/update awaits dependency work under this lock
            # before it terminates the captured replacement.
            manager_stop = asyncio.create_task(replacement.terminate())
            _done, pending = await asyncio.wait(
                (host_shutdown, manager_stop), timeout=1
            )
        finally:
            self.manager._pm_lock.release()
            event.release.set()
        await asyncio.gather(
            host_shutdown, manager_stop, handler, return_exceptions=True
        )
        self.assertFalse(pending, "manager lock and reload cleanup formed a join cycle")
        self.assertTrue(replacement.state_store.closed)
        self.assertTrue(replacement.queue.closed)
        self.assertTrue(replacement.executor.quiescent)
        self.assertIn(self.manager.other_metadata, self.context.get_all_stars())

    async def test_external_terminate_reports_replacement_commit_failure(self):
        old = self.plugin
        event = BlockedFinalReply()
        handler = asyncio.create_task(self.reload(event))
        await asyncio.wait_for(event.started.wait(), 5)
        replacement = self.manager.current_plugin
        self.backend.fail_next_put()
        with self.assertRaises(self.main.CommitUnconfirmedError):
            await replacement.state_store.mutate(lambda snapshot: None)
        self.backend.fail_next_put()
        try:
            with self.assertRaises(self.main.ReloadError):
                await old.terminate()
        finally:
            event.release.set()
            await asyncio.gather(handler, return_exceptions=True)
        self.assertFalse(replacement.state_store.closed)
        self.assertTrue(replacement.state_store.closing)
        self.assertFalse(replacement.state_store.ready)
        self.assertTrue(replacement.queue.closed)

    async def test_loader_swallowed_cancellation_drains_unregistered_replacement(self):
        original_load = self.manager.load
        reached = asyncio.Event()
        never_release = asyncio.Event()

        async def swallowing_load(*, specified_module_path):
            await original_load(specified_module_path=specified_module_path)
            reached.set()
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                # AstrBot 4.29.4 load catches BaseException and purges metadata
                # after initialize, without terminating the initialized instance.
                self.manager.remove_target_metadata()
                return False, self.manager.loader_detail

        self.manager.load = swallowing_load
        old = self.plugin
        handler = asyncio.create_task(self.reload())
        await asyncio.wait_for(reached.wait(), 5)
        replacement = self.manager.current_plugin
        self.assertTrue(replacement.state_store.ready)
        async with asyncio.timeout(5):
            await old.terminate()
        await asyncio.gather(handler, return_exceptions=True)
        self.assertEqual(self.context.get_all_stars(), [self.manager.other_metadata])
        self.assertTrue(replacement.state_store.closed)
        self.assertTrue(replacement.queue.closed)
        self.assertTrue(replacement.scheduler.closed)
        self.assertTrue(replacement.task_control.closed)
        self.assertTrue(replacement.executor.quiescent)

    async def test_loader_failures_have_one_fixed_safe_terminal_feedback(self):
        terminal_feedback = []
        modes = ("false", "malformed", "raise", "unready")
        for index, mode in enumerate(modes):
            with self.subTest(mode=mode):
                current = self.plugin
                self.manager.load_mode = mode
                event = await self.reload(plugin=current)
                terminal_feedback.append(event.sent[-1].text)

                self.assertEqual(len(event.sent), 2)
                self.assertTrue(current.state_store.closed)
                self.assert_safe_feedback(
                    event,
                    self.manager.loader_detail,
                    _ARGUMENT_SECRET,
                    _EVENT_SECRET,
                )
                self.assertEqual(
                    [operation[0] for operation in self.manager.operations[-3:]],
                    ["terminate", "unbind", "load"],
                )
                if mode == "unready":
                    self.assertIsNone(self.manager.current_plugin.state_store)
                if index != len(modes) - 1:
                    self.plugin = await self.manager.recover()
        self.assertEqual(len(set(terminal_feedback)), 1)

    async def test_invalid_task_configuration_reaches_isolated_loader_safely(self):
        invalid = "SECRET_INVALID_TASK_CONFIGURATION"
        self.config["tasks"] = invalid

        event = await self.reload()

        self.assertEqual(len(event.sent), 2)
        self.assertEqual(
            [operation[0] for operation in self.manager.operations],
            ["terminate", "unbind", "load"],
        )
        self.assertTrue(self.plugin.state_store.closed)
        self.assertEqual(self.context.get_all_stars(), [self.manager.other_metadata])
        self.assert_safe_feedback(event, invalid, self.manager.loader_detail)

    async def test_missing_private_manager_fails_preflight_without_shutdown(self):
        manager = self.manager
        self.context._star_manager = None
        try:
            event = await self.reload()
        finally:
            self.context._star_manager = manager

        self.assertEqual(manager.operations, [])
        self.assertTrue(self.plugin.state_store.ready)
        self.assertFalse(self.plugin._terminating)
        self.assertEqual(len(event.sent), 1)
        self.assert_safe_feedback(event, manager.loader_detail)

    async def test_stopped_instance_reports_unavailable_without_reanimation(self):
        old = self.plugin
        await old.terminate()
        event = await self.reload(plugin=old)

        self.assertTrue(old.state_store.closed)
        self.assertEqual(self.manager.operations, [])
        self.assertEqual(len(event.sent), 1)
        self.assertIs(
            self.context.get_registered_star(self.manager.plugin_name).star_cls, old
        )
        self.assertIsNone(old._state_slot().get("reload_task"))
        self.assert_safe_feedback(event, self.manager.loader_detail)


if __name__ == "__main__":
    import unittest

    unittest.main()
