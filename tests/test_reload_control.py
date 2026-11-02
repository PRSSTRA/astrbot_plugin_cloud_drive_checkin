import asyncio
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from reload_control import (
    ReloadError,
    check_reload_target,
    cleanup_reload_replacement,
    reload_plugin,
)


@dataclass
class _Metadata:
    name: str
    module_path: str
    star_cls: object
    activated: bool = True


class _Context:
    def __init__(self):
        self.stars = []
        self._star_manager = None
        self._cloud_drive_checkin_state_registry = {}

    def get_all_stars(self):
        return self.stars


class _Preparation:
    def __init__(self):
        self._closed = False
        self._close_task = None
        self._operations = set()
        self._preparing = None


class _Plugin:
    def __init__(self, context, plugin_id="Prstra/astrbot_plugin_cloud_drive_checkin"):
        self.context = context
        self.plugin_id = plugin_id
        self.state_store = SimpleNamespace(ready=True, closed=False)
        self.queue = SimpleNamespace(closed=False)
        self.scheduler = SimpleNamespace(closed=False)
        self.task_control = SimpleNamespace(closed=False)
        self.executor = SimpleNamespace(quiescent=False)
        self.preparation = _Preparation()
        self.terminate_mode = "drain"
        self.terminate_calls = 0
        self._terminating = False
        self._shutdown_task = None

    async def terminate(self):
        self._terminating = True
        self.terminate_calls += 1
        if self.terminate_mode == "error":
            raise OSError("RAW_FRAMEWORK_CLOSE_SECRET")
        if self.terminate_mode == "cancel":
            raise asyncio.CancelledError
        if self.terminate_mode == "leave-running":
            return

        self.queue.closed = True
        self.scheduler.closed = True
        self.task_control.closed = True
        self.executor.quiescent = True
        close_task = asyncio.create_task(asyncio.sleep(0))
        await close_task
        self.preparation._closed = True
        self.preparation._close_task = close_task
        self.state_store.ready = False
        self.state_store.closed = True

    def _state_slot(self):
        return self.context._cloud_drive_checkin_state_registry[self.plugin_id]


class _OtherPlugin:
    def __init__(self):
        self.terminate_calls = 0


class _Manager:
    def __init__(self, context, target):
        self.context = context
        self.target = target
        self._pm_lock = asyncio.Lock()
        self.actions = []
        self.load_mode = "ready"
        self.after_terminate = None
        self.replacement = None

    async def reload(self, name=None):
        raise AssertionError("the broad framework reload API must not be called")

    async def _terminate_plugin(self, metadata):
        self.actions.append(("terminate", metadata))
        await metadata.star_cls.terminate()
        if self.after_terminate is not None:
            self.after_terminate()

    async def _unbind_plugin(self, name, module_path):
        self.actions.append(("unbind", name, module_path))
        for index, metadata in enumerate(self.context.stars):
            if metadata.name == name:
                del self.context.stars[index]
                return
        raise AssertionError("the requested plugin was not registered")

    def _install_candidate(self, specified_module_path, *, register):
        replacement = type(self.target)(self.context, self.target.plugin_id)
        slot = self.context._cloud_drive_checkin_state_registry[self.target.plugin_id]
        slot["reload_candidate"] = replacement
        slot["owner"] = replacement.state_store
        self.replacement = replacement
        if register:
            self.context.stars.append(
                _Metadata(
                    name="astrbot_plugin_cloud_drive_checkin",
                    module_path=specified_module_path,
                    star_cls=replacement,
                )
            )
        return replacement

    async def load(self, *, specified_module_path=None):
        self.actions.append(("load", specified_module_path))
        if self.load_mode == "false":
            return False, "RAW_FRAMEWORK_LOAD_SECRET"
        if self.load_mode == "malformed":
            return True
        if self.load_mode in {"native-failure", "cancel-after-candidate"}:
            self._install_candidate(specified_module_path, register=False)
            if self.load_mode == "cancel-after-candidate":
                raise asyncio.CancelledError
            return False, "RAW_FRAMEWORK_INITIALIZE_SECRET"

        replacement = self._install_candidate(specified_module_path, register=True)
        if self.load_mode == "unready":
            replacement.state_store.ready = False
        return True, None


class ReloadControlTests(unittest.IsolatedAsyncioTestCase):
    def environment(self):
        context = _Context()
        plugin = _Plugin(context)
        metadata = _Metadata(
            name="astrbot_plugin_cloud_drive_checkin",
            module_path=type(plugin).__module__,
            star_cls=plugin,
        )
        context.stars.append(metadata)
        context._cloud_drive_checkin_state_registry[plugin.plugin_id] = {
            "lock": asyncio.Lock(),
            "owner": plugin.state_store,
        }
        manager = _Manager(context, plugin)
        context._star_manager = manager
        return context, plugin, metadata, manager

    @staticmethod
    def assert_safe_error(caught):
        assert str(caught.exception) == "插件重载失败。"
        assert "RAW_" not in str(caught.exception)

    async def test_correct_target_only_is_reloaded_and_new_owner_is_ready(self):
        context, plugin, metadata, manager = self.environment()
        other = _OtherPlugin()
        other_metadata = _Metadata("unrelated", "plugins.unrelated", other)
        context.stars.append(other_metadata)
        guard_observations = []

        self.assertIsNone(check_reload_target(plugin))
        self.assertEqual(manager.actions, [])
        self.assertEqual(context.stars, [metadata, other_metadata])

        await reload_plugin(
            plugin,
            guard=lambda: guard_observations.append(manager._pm_lock.locked()),
        )

        self.assertEqual(guard_observations, [True])
        self.assertEqual(
            manager.actions,
            [
                ("terminate", metadata),
                (
                    "unbind",
                    "astrbot_plugin_cloud_drive_checkin",
                    type(plugin).__module__,
                ),
                ("load", type(plugin).__module__),
            ],
        )
        self.assertEqual(plugin.terminate_calls, 1)
        self.assertEqual(other.terminate_calls, 0)
        self.assertIn(other_metadata, context.stars)
        self.assertNotIn(metadata, context.stars)
        self.assertIsNotNone(manager.replacement)
        self.assertTrue(manager.replacement.state_store.ready)
        self.assertFalse(manager.replacement.state_store.closed)
        self.assertIs(
            context._cloud_drive_checkin_state_registry[plugin.plugin_id]["owner"],
            manager.replacement.state_store,
        )
        self.assertTrue(plugin.state_store.closed)
        self.assertTrue(plugin.queue.closed)
        self.assertTrue(plugin.scheduler.closed)
        self.assertTrue(plugin.task_control.closed)
        self.assertTrue(plugin.executor.quiescent)
        self.assertTrue(plugin.preparation._closed)
        slot = context._cloud_drive_checkin_state_registry[plugin.plugin_id]
        self.assertIs(slot["reload_previous_owner"], plugin.state_store)
        self.assertIs(slot["reload_candidate"], manager.replacement)
        self.assertEqual(slot["reload_target"].module_path, type(plugin).__module__)

    async def test_registry_identity_change_after_termination_never_unbinds(self):
        context, plugin, metadata, manager = self.environment()
        substituted = _Metadata(metadata.name, metadata.module_path, plugin)

        def replace_metadata():
            context.stars[:] = [substituted]

        manager.after_terminate = replace_metadata

        with self.assertRaises(ReloadError) as caught:
            await reload_plugin(plugin, guard=lambda: None)

        self.assert_safe_error(caught)
        self.assertEqual(manager.actions, [("terminate", metadata)])
        self.assertEqual(context.stars, [substituted])
        self.assertTrue(plugin.state_store.closed)
        self.assertIs(
            context._cloud_drive_checkin_state_registry[plugin.plugin_id]["owner"],
            plugin.state_store,
        )

    async def test_name_collision_is_rejected_without_touching_either_plugin(self):
        context, plugin, metadata, manager = self.environment()
        other = _OtherPlugin()
        collision = _Metadata(metadata.name, "plugins.name_collision", other)
        context.stars.append(collision)

        with self.assertRaises(ReloadError) as preflight:
            check_reload_target(plugin)
        with self.assertRaises(ReloadError) as execution:
            await reload_plugin(plugin, guard=lambda: None)

        self.assert_safe_error(preflight)
        self.assert_safe_error(execution)
        self.assertEqual(manager.actions, [])
        self.assertEqual(plugin.terminate_calls, 0)
        self.assertEqual(other.terminate_calls, 0)
        self.assertEqual(context.stars, [metadata, collision])

    async def test_close_failure_keeps_the_old_writer_bound(self):
        context, plugin, metadata, manager = self.environment()
        plugin.terminate_mode = "error"

        with self.assertRaises(ReloadError) as caught:
            await reload_plugin(plugin, guard=lambda: None)

        self.assert_safe_error(caught)
        self.assertEqual(manager.actions, [("terminate", metadata)])
        self.assertEqual(context.stars, [metadata])
        self.assertFalse(plugin.state_store.closed)
        self.assertIs(
            context._cloud_drive_checkin_state_registry[plugin.plugin_id]["owner"],
            plugin.state_store,
        )

    async def test_returning_from_terminate_without_draining_never_unbinds(self):
        context, plugin, metadata, manager = self.environment()
        plugin.terminate_mode = "leave-running"

        with self.assertRaises(ReloadError) as caught:
            await reload_plugin(plugin, guard=lambda: None)

        self.assert_safe_error(caught)
        self.assertEqual(manager.actions, [("terminate", metadata)])
        self.assertEqual(context.stars, [metadata])
        self.assertFalse(plugin.state_store.closed)
        self.assertFalse(plugin.queue.closed)

    async def test_false_and_unready_load_results_never_report_success(self):
        for mode in ("false", "unready"):
            with self.subTest(mode=mode):
                context, plugin, metadata, manager = self.environment()
                manager.load_mode = mode

                with self.assertRaises(ReloadError) as caught:
                    await reload_plugin(plugin, guard=lambda: None)

                self.assert_safe_error(caught)
                self.assertEqual(
                    manager.actions,
                    [
                        ("terminate", metadata),
                        (
                            "unbind",
                            "astrbot_plugin_cloud_drive_checkin",
                            type(plugin).__module__,
                        ),
                        ("load", type(plugin).__module__),
                    ],
                )
                self.assertTrue(plugin.state_store.closed)
                self.assertNotIn(metadata, context.stars)
                if mode == "false":
                    self.assertIsNone(manager.replacement)
                    self.assertEqual(context.stars, [])
                    self.assertIs(
                        context._cloud_drive_checkin_state_registry[plugin.plugin_id][
                            "owner"
                        ],
                        plugin.state_store,
                    )
                else:
                    self.assertIsNotNone(manager.replacement)
                    self.assertEqual(manager.replacement.terminate_calls, 1)
                    self.assertTrue(manager.replacement.state_store.closed)
                    self.assertIs(
                        context._cloud_drive_checkin_state_registry[plugin.plugin_id][
                            "owner"
                        ],
                        manager.replacement.state_store,
                    )

    async def test_native_load_failure_cleans_candidate_without_metadata(self):
        context, plugin, _metadata, manager = self.environment()
        manager.load_mode = "native-failure"

        with self.assertRaises(ReloadError) as caught:
            await reload_plugin(plugin, guard=lambda: None)

        self.assert_safe_error(caught)
        self.assertIsNotNone(manager.replacement)
        self.assertEqual(manager.replacement.terminate_calls, 1)
        self.assertTrue(manager.replacement.state_store.closed)
        self.assertEqual(context.stars, [])
        self.assertIs(
            context._cloud_drive_checkin_state_registry[plugin.plugin_id]["owner"],
            manager.replacement.state_store,
        )

    async def test_cancelled_load_cleans_candidate_and_preserves_cancellation(self):
        context, plugin, _metadata, manager = self.environment()
        manager.load_mode = "cancel-after-candidate"

        with self.assertRaises(asyncio.CancelledError):
            await reload_plugin(plugin, guard=lambda: None)

        self.assertIsNotNone(manager.replacement)
        self.assertEqual(manager.replacement.terminate_calls, 1)
        self.assertTrue(manager.replacement.state_store.closed)
        self.assertEqual(context.stars, [])

    async def test_cleanup_stops_only_the_recorded_ready_replacement(self):
        context, plugin, _metadata, manager = self.environment()
        await reload_plugin(plugin, guard=lambda: None)
        replacement = manager.replacement
        replacement_metadata = context.stars[0]

        await cleanup_reload_replacement(plugin)

        self.assertEqual(replacement.terminate_calls, 1)
        self.assertTrue(replacement.state_store.closed)
        self.assertEqual(context.stars, [replacement_metadata])

    async def test_cleanup_does_not_stop_a_later_independent_generation(self):
        context, plugin, metadata, manager = self.environment()
        await reload_plugin(plugin, guard=lambda: None)
        recorded = manager.replacement
        await recorded.terminate()
        third = _Plugin(context, plugin.plugin_id)
        third_metadata = _Metadata(metadata.name, metadata.module_path, third)
        context.stars[:] = [third_metadata]
        slot = context._cloud_drive_checkin_state_registry[plugin.plugin_id]
        slot["owner"] = third.state_store

        await cleanup_reload_replacement(plugin)

        self.assertEqual(recorded.terminate_calls, 1)
        self.assertEqual(third.terminate_calls, 0)
        self.assertFalse(third.state_store.closed)
        self.assertEqual(context.stars, [third_metadata])

    async def test_cancelled_error_is_not_converted_to_reload_error(self):
        context, plugin, metadata, manager = self.environment()
        plugin.terminate_mode = "cancel"

        with self.assertRaises(asyncio.CancelledError):
            await reload_plugin(plugin, guard=lambda: None)

        self.assertEqual(manager.actions, [("terminate", metadata)])
        self.assertEqual(context.stars, [metadata])
        self.assertIs(
            context._cloud_drive_checkin_state_registry[plugin.plugin_id]["owner"],
            plugin.state_store,
        )


if __name__ == "__main__":
    unittest.main()
