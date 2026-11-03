import asyncio
import copy
import json
import unittest
from collections import deque
from datetime import datetime

from state_support import load_module

_PACKAGE = "_checkin_bindings_tests"
bindings = load_module("bindings", _PACKAGE)
credentials = load_module("credentials", _PACKAGE)
permissions = load_module("permissions", _PACKAGE)
state = load_module("state", _PACKAGE)
storage = load_module("storage", _PACKAGE)

_ABSENT = object()


class WriteControl:
    def __init__(self, *, blocked=False, error=None):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.error = error


class FakeKV:
    def __init__(self, initial=_ABSENT):
        self.values = {}
        if initial is not _ABSENT:
            self.values[state.STATE_KEY] = copy.deepcopy(initial)
        self.write_values = []
        self._write_controls = deque()

    def fail_next_write(self):
        self._write_controls.append(WriteControl(error=OSError("write failed")))

    def block_next_write(self):
        control = WriteControl(blocked=True)
        self._write_controls.append(control)
        return control

    async def get_kv_data(self, key, default):
        if key not in self.values:
            return default
        return copy.deepcopy(self.values[key])

    async def put_kv_data(self, key, value):
        detached = copy.deepcopy(value)
        self.write_values.append(detached)
        control = (
            self._write_controls.popleft() if self._write_controls else WriteControl()
        )
        control.started.set()
        await control.release.wait()
        if control.error is not None:
            raise control.error
        self.values[key] = detached


def _encoded_size(snapshot):
    return len(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


class BindingControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = FakeKV()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        self.identity = permissions.Identity("bot-instance", "10001")
        self.other_qq = permissions.Identity("bot-instance", "10002")
        self.other_platform = permissions.Identity("other-bot", "10001")
        self.task_id = "example_task"

    async def add_binding(
        self,
        identity=None,
        *,
        task_id=None,
        paused=False,
        notify_all=False,
        credential=_ABSENT,
        extra_binding=None,
        extra_user=None,
    ):
        if identity is None:
            identity = self.identity
        if task_id is None:
            task_id = self.task_id
        owner_key = credentials.user_key(identity)
        target_key = credentials.binding_key(identity, task_id)
        if credential is _ABSENT:
            credential = {
                "ciphertext": f"ciphertext:{identity.platform_id}:{identity.qq_id}",
                "task_version": "task-version-1",
            }

        def mutate(snapshot):
            user = copy.deepcopy(snapshot["users"].get(owner_key, {}))
            user.update(
                platform_id=identity.platform_id,
                qq_id=identity.qq_id,
                unified_msg_origin=f"origin:{identity.platform_id}:{identity.qq_id}",
            )
            if extra_user:
                user.update(copy.deepcopy(extra_user))
            snapshot["users"][owner_key] = user
            record = {
                "user_key": owner_key,
                "task_id": task_id,
                "task_version": "task-version-1",
                "paused": paused,
                "notify_all": notify_all,
                "updated_at": "2026-11-02T00:00:00+00:00",
            }
            if extra_binding:
                record.update(copy.deepcopy(extra_binding))
            snapshot["bindings"][target_key] = record
            if credential is not None:
                snapshot["credentials"][target_key] = copy.deepcopy(credential)

        await self.store.mutate(mutate)
        return owner_key, target_key

    async def test_platform_and_qq_identity_isolation(self):
        identities = (self.identity, self.other_qq, self.other_platform)
        keys = {}
        for identity in identities:
            _, keys[identity] = await self.add_binding(identity)
        before = self.store.snapshot()

        await bindings.set_paused(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            paused=True,
            guard=lambda: None,
        )

        after = self.store.snapshot()
        self.assertTrue(after["bindings"][keys[self.identity]]["paused"])
        for identity in (self.other_qq, self.other_platform):
            self.assertEqual(
                after["bindings"][keys[identity]],
                before["bindings"][keys[identity]],
            )
            self.assertEqual(
                after["credentials"][keys[identity]],
                before["credentials"][keys[identity]],
            )

    async def test_preferences_change_only_requested_metadata(self):
        credential = {
            "ciphertext": "v1.private-ciphertext",
            "task_version": "task-version-1",
            "field_ids": ["account", "password"],
            "future_credential_metadata": {"keep": [1, 2]},
        }
        _, target_key = await self.add_binding(
            paused=False,
            notify_all=True,
            credential=credential,
            extra_binding={"future_binding_metadata": {"keep": ["value"]}},
        )
        before = self.store.snapshot()

        await bindings.set_paused(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            paused=True,
            guard=lambda: None,
        )

        paused_snapshot = self.store.snapshot()
        paused_record = paused_snapshot["bindings"][target_key]
        expected_record = copy.deepcopy(before["bindings"][target_key])
        expected_record["paused"] = True
        expected_record["updated_at"] = paused_record["updated_at"]
        self.assertEqual(paused_record, expected_record)
        self.assertTrue(paused_record["notify_all"])
        self.assertIsNotNone(datetime.fromisoformat(paused_record["updated_at"]).tzinfo)
        self.assertEqual(paused_snapshot["credentials"][target_key], credential)

        await bindings.set_notify_all(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            notify_all=False,
            guard=lambda: None,
        )

        notified_snapshot = self.store.snapshot()
        notified_record = notified_snapshot["bindings"][target_key]
        expected_record = copy.deepcopy(paused_record)
        expected_record["notify_all"] = False
        expected_record["updated_at"] = notified_record["updated_at"]
        self.assertEqual(notified_record, expected_record)
        self.assertTrue(notified_record["paused"])
        self.assertEqual(notified_snapshot["credentials"][target_key], credential)

    async def test_missing_flags_default_false_but_unrequested_flag_is_not_added(self):
        _, target_key = await self.add_binding()

        def remove_flags(snapshot):
            snapshot["bindings"][target_key].pop("paused")
            snapshot["bindings"][target_key].pop("notify_all")

        await self.store.mutate(remove_flags)

        await bindings.set_paused(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            paused=False,
            guard=lambda: None,
        )

        record = self.store.snapshot()["bindings"][target_key]
        self.assertIs(record["paused"], False)
        self.assertNotIn("notify_all", record)

    async def test_missing_and_malformed_owner_records_are_rejected(self):
        baseline = self.store.snapshot()
        with self.assertRaises(bindings.BindingNotFound):
            bindings.delete_version(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
            )
        with self.assertRaises(bindings.BindingNotFound):
            await bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=True,
                guard=lambda: None,
            )
        self.assertEqual(self.store.snapshot(), baseline)

        owner_key, target_key = await self.add_binding()
        other_owner = credentials.user_key(self.other_qq)

        def replace_owner(snapshot):
            snapshot["bindings"][target_key]["user_key"] = other_owner

        await self.store.mutate(replace_owner)
        malformed = self.store.snapshot()
        with self.assertRaises(bindings.BindingError):
            bindings.delete_version(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
            )
        with self.assertRaises(bindings.BindingError):
            await bindings.set_notify_all(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                notify_all=True,
                guard=lambda: None,
            )
        self.assertEqual(self.store.snapshot(), malformed)

        def restore_binding_and_remove_owner(snapshot):
            snapshot["bindings"][target_key]["user_key"] = owner_key
            del snapshot["users"][owner_key]

        await self.store.mutate(restore_binding_and_remove_owner)
        missing_owner = self.store.snapshot()
        with self.assertRaises(bindings.BindingError):
            await bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=True,
                guard=lambda: None,
            )
        self.assertEqual(self.store.snapshot(), missing_owner)

    async def test_missing_binding_never_deletes_orphan_ciphertext(self):
        target_key = credentials.binding_key(self.identity, self.task_id)

        def add_orphan_ciphertext(snapshot):
            snapshot["credentials"][target_key] = {
                "ciphertext": "orphan-ciphertext",
                "task_version": "task-version-1",
            }

        await self.store.mutate(add_orphan_ciphertext)
        baseline = self.store.snapshot()
        with self.assertRaises(bindings.BindingNotFound):
            await bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version="0" * 64,
                guard=lambda: None,
            )

        self.assertEqual(self.store.snapshot(), baseline)
        self.assertEqual(
            self.store.snapshot()["credentials"][target_key],
            baseline["credentials"][target_key],
        )

    async def test_task_mismatch_and_non_dictionary_binding_are_rejected(self):
        owner_key, target_key = await self.add_binding()
        invalid_records = (
            {
                "user_key": owner_key,
                "task_id": "different_task",
                "paused": False,
                "notify_all": False,
            },
            ["not", "a", "binding"],
        )

        for invalid_record in invalid_records:

            def replace(snapshot, invalid_record=invalid_record):
                snapshot["bindings"][target_key] = copy.deepcopy(invalid_record)

            await self.store.mutate(replace)
            baseline = self.store.snapshot()
            with self.assertRaises(bindings.BindingError):
                bindings.delete_version(
                    self.store,
                    identity=self.identity,
                    task_id=self.task_id,
                )
            with self.assertRaises(bindings.BindingError):
                await bindings.set_notify_all(
                    self.store,
                    identity=self.identity,
                    task_id=self.task_id,
                    notify_all=True,
                    guard=lambda: None,
                )
            self.assertEqual(self.store.snapshot(), baseline)

    async def test_requested_preferences_require_real_booleans(self):
        await self.add_binding()
        baseline = self.store.snapshot()

        with self.assertRaises(bindings.BindingError):
            await bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=1,
                guard=lambda: None,
            )
        with self.assertRaises(bindings.BindingError):
            await bindings.set_notify_all(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                notify_all="yes",
                guard=lambda: None,
            )

        self.assertEqual(self.store.snapshot(), baseline)

    async def test_non_boolean_stored_flags_are_rejected_without_writes(self):
        _, target_key = await self.add_binding()

        for field in ("paused", "notify_all"):

            def corrupt(snapshot, field=field):
                snapshot["bindings"][target_key][field] = 1

            await self.store.mutate(corrupt)
            baseline = self.store.snapshot()
            writes_before = list(self.kv.write_values)
            with self.assertRaises(bindings.BindingError):
                await bindings.set_paused(
                    self.store,
                    identity=self.identity,
                    task_id=self.task_id,
                    paused=True,
                    guard=lambda: None,
                )
            self.assertEqual(self.store.snapshot(), baseline)
            self.assertEqual(self.kv.write_values, writes_before)

            def restore(snapshot, field=field):
                snapshot["bindings"][target_key][field] = False

            await self.store.mutate(restore)

    async def test_persisted_enrollment_blocks_all_binding_management(self):
        owner_key, _ = await self.add_binding()
        version = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )

        def enroll(snapshot):
            snapshot["users"][owner_key]["enrollment"] = {}

        await self.store.mutate(enroll)
        baseline = self.store.snapshot()
        writes_before = list(self.kv.write_values)

        with self.assertRaises(bindings.BindingBusy):
            bindings.delete_version(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
            )
        with self.assertRaises(bindings.BindingBusy):
            await bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=True,
                guard=lambda: None,
            )
        with self.assertRaises(bindings.BindingBusy):
            await bindings.set_notify_all(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                notify_all=True,
                guard=lambda: None,
            )
        with self.assertRaises(bindings.BindingBusy):
            await bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version=version,
                guard=lambda: None,
            )

        self.assertEqual(self.store.snapshot(), baseline)
        self.assertEqual(self.kv.write_values, writes_before)

    async def test_fingerprint_is_canonical_and_covers_all_target_records(self):
        credential = {
            "ciphertext": "ciphertext",
            "task_version": "task-version-1",
            "field_ids": ["account"],
            "nested": {"z": 1, "a": [2, 3]},
        }
        _, target_key = await self.add_binding(
            credential=credential,
            extra_binding={"nested": {"second": 2, "first": 1}},
        )
        snapshot_before = self.store.snapshot()
        writes_before = list(self.kv.write_values)
        first = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        self.assertEqual(self.store.snapshot(), snapshot_before)
        self.assertEqual(self.kv.write_values, writes_before)
        self.assertEqual(len(first), 64)
        self.assertLessEqual(int(first, 16).bit_length(), 256)

        def reorder_only(snapshot):
            record = snapshot["bindings"].pop(target_key)
            snapshot["bindings"][target_key] = dict(reversed(tuple(record.items())))
            stored_credential = snapshot["credentials"].pop(target_key)
            snapshot["credentials"][target_key] = dict(
                reversed(tuple(stored_credential.items()))
            )

        await self.store.mutate(reorder_only)
        self.assertEqual(
            bindings.delete_version(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
            ),
            first,
        )

        def replace_future_credential_metadata(snapshot):
            snapshot["credentials"][target_key]["nested"]["z"] = 9

        await self.store.mutate(replace_future_credential_metadata)
        replaced = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        self.assertNotEqual(replaced, first)
        with self.assertRaises(bindings.BindingChanged):
            await bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version=first,
                guard=lambda: None,
            )
        self.assertIn(target_key, self.store.snapshot()["credentials"])

        await self.store.mutate(
            lambda snapshot: snapshot["credentials"].pop(target_key)
        )
        missing = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        self.assertNotEqual(missing, replaced)
        await bindings.delete_binding(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            expected_version=missing,
            guard=lambda: None,
        )
        self.assertNotIn(target_key, self.store.snapshot()["bindings"])

    async def test_stale_own_changes_fail_but_unrelated_owner_change_does_not(self):
        _, target_key = await self.add_binding()
        await self.add_binding(self.other_qq)
        original = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )

        def replace_credential(snapshot):
            snapshot["credentials"][target_key] = {
                "ciphertext": "replacement-ciphertext",
                "task_version": "task-version-2",
            }

        await self.store.mutate(replace_credential)
        replaced_snapshot = self.store.snapshot()
        with self.assertRaises(bindings.BindingChanged):
            await bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version=original,
                guard=lambda: None,
            )
        self.assertEqual(self.store.snapshot(), replaced_snapshot)

        replaced = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        await bindings.set_paused(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            paused=True,
            guard=lambda: None,
        )
        with self.assertRaises(bindings.BindingChanged):
            await bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version=replaced,
                guard=lambda: None,
            )

        current = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        await bindings.set_notify_all(
            self.store,
            identity=self.other_qq,
            task_id=self.task_id,
            notify_all=True,
            guard=lambda: None,
        )
        await bindings.delete_binding(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            expected_version=current,
            guard=lambda: None,
        )
        snapshot = self.store.snapshot()
        self.assertNotIn(target_key, snapshot["bindings"])
        self.assertIn(
            credentials.binding_key(self.other_qq, self.task_id),
            snapshot["bindings"],
        )

    async def test_delete_rechecks_version_and_guard_after_waiting_for_lock(self):
        _, target_key = await self.add_binding()
        version = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        gate = self.kv.block_next_write()

        def replace_credential(snapshot):
            snapshot["credentials"][target_key] = {
                "ciphertext": "replacement-while-waiting",
                "task_version": "task-version-2",
            }

        replacement = asyncio.create_task(self.store.mutate(replace_credential))
        await gate.started.wait()
        guard_called = asyncio.Event()
        deletion = asyncio.create_task(
            bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version=version,
                guard=guard_called.set,
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(guard_called.is_set())

        gate.release.set()
        await replacement
        with self.assertRaises(bindings.BindingChanged):
            await deletion

        self.assertTrue(guard_called.is_set())
        snapshot = self.store.snapshot()
        self.assertIn(target_key, snapshot["bindings"])
        self.assertEqual(
            snapshot["credentials"][target_key]["ciphertext"],
            "replacement-while-waiting",
        )

    async def test_guard_waits_for_lock_and_failure_leaves_state_unchanged(self):
        await self.add_binding()
        gate = self.kv.block_next_write()

        def hold_mutation(snapshot):
            snapshot["cooldowns"]["holding-write"] = {"value": 1}

        holding = asyncio.create_task(self.store.mutate(hold_mutation))
        await gate.started.wait()
        observed = []

        def guard():
            observed.append(copy.deepcopy(self.store.snapshot()["cooldowns"]))

        preference = asyncio.create_task(
            bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=True,
                guard=guard,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(observed, [])
        gate.release.set()
        await holding
        await preference
        self.assertEqual(observed, [{"holding-write": {"value": 1}}])

        baseline = self.store.snapshot()

        class GuardRejected(Exception):
            pass

        def reject():
            raise GuardRejected

        with self.assertRaises(GuardRejected):
            await bindings.set_notify_all(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                notify_all=True,
                guard=reject,
            )
        self.assertEqual(self.store.snapshot(), baseline)
        self.assertFalse(self.store.has_pending)

    async def test_uncertain_preference_retains_confirmed_and_exact_pending(self):
        _, target_key = await self.add_binding()
        baseline = self.store.snapshot()
        writes_before = list(self.kv.write_values)
        self.kv.fail_next_write()

        with self.assertRaises(storage.CommitUnconfirmedError):
            await bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=True,
                guard=lambda: None,
            )

        self.assertEqual(self.store.snapshot(), baseline)
        self.assertTrue(self.store.has_pending)
        pending = self.kv.write_values[-1]
        expected = copy.deepcopy(baseline)
        expected["revision"] += 1
        expected["bindings"][target_key]["paused"] = True
        expected["bindings"][target_key]["updated_at"] = pending["bindings"][
            target_key
        ]["updated_at"]
        self.assertEqual(pending, expected)
        self.assertEqual(self.store._pending, pending)
        self.assertEqual(self.kv.write_values, [*writes_before, pending])

        committed = await self.store.retry_pending()
        self.assertEqual(committed, pending)
        self.assertEqual(self.kv.write_values[-2:], [pending, pending])

    async def test_uncertain_delete_retains_confirmed_and_exact_pending(self):
        _, target_key = await self.add_binding()
        version = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        baseline = self.store.snapshot()
        writes_before = list(self.kv.write_values)
        self.kv.fail_next_write()

        with self.assertRaises(storage.CommitUnconfirmedError):
            await bindings.delete_binding(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                expected_version=version,
                guard=lambda: None,
            )

        self.assertEqual(self.store.snapshot(), baseline)
        self.assertTrue(self.store.has_pending)
        pending = self.kv.write_values[-1]
        expected = copy.deepcopy(baseline)
        expected["revision"] += 1
        del expected["bindings"][target_key]
        del expected["credentials"][target_key]
        self.assertEqual(pending, expected)
        self.assertEqual(self.store._pending, pending)
        self.assertEqual(self.kv.write_values, [*writes_before, pending])

        committed = await self.store.retry_pending()
        self.assertEqual(committed, pending)
        self.assertEqual(self.kv.write_values[-2:], [pending, pending])

    async def test_delete_preserves_user_metadata_and_every_unrelated_collection(self):
        owner_key, target_key = await self.add_binding(
            extra_user={"profile": {"keep": True}}
        )
        _, sibling_key = await self.add_binding(task_id="second_task")
        _, other_key = await self.add_binding(self.other_qq)

        def populate(snapshot):
            snapshot["batches"]["batch"] = {"keep": 1}
            snapshot["runs"]["run"] = {"keep": 2}
            snapshot["outbox"]["message"] = {"keep": 3}
            snapshot["cleanup_jobs"]["cleanup"] = {"keep": 4}
            snapshot["cooldowns"]["cooldown"] = {"keep": 5}
            snapshot["dedupe"]["dedupe"] = {"keep": 6}

        await self.store.mutate(populate)
        version = bindings.delete_version(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
        )
        baseline = self.store.snapshot()
        await bindings.delete_binding(
            self.store,
            identity=self.identity,
            task_id=self.task_id,
            expected_version=version,
            guard=lambda: None,
        )

        expected = copy.deepcopy(baseline)
        expected["revision"] += 1
        del expected["bindings"][target_key]
        del expected["credentials"][target_key]
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot, expected)
        self.assertIn(owner_key, snapshot["users"])
        self.assertEqual(snapshot["users"][owner_key]["profile"], {"keep": True})
        self.assertIn(sibling_key, snapshot["bindings"])
        self.assertIn(other_key, snapshot["bindings"])
        self.assertIn(state.STATE_KEY, self.kv.values)
        self.assertEqual(self.kv.values[state.STATE_KEY], snapshot)

    async def test_delete_version_rejects_unready_store_without_a_write(self):
        uninitialized_kv = FakeKV()
        uninitialized = storage.StateStore(uninitialized_kv)

        with self.assertRaises(storage.StateUnavailableError):
            bindings.delete_version(
                uninitialized,
                identity=self.identity,
                task_id=self.task_id,
            )
        self.assertEqual(uninitialized_kv.write_values, [])

        await self.add_binding()
        self.kv.fail_next_write()
        with self.assertRaises(storage.CommitUnconfirmedError):
            await bindings.set_paused(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
                paused=True,
                guard=lambda: None,
            )
        writes_before = list(self.kv.write_values)
        with self.assertRaises(storage.StateUnavailableError):
            bindings.delete_version(
                self.store,
                identity=self.identity,
                task_id=self.task_id,
            )
        self.assertEqual(self.kv.write_values, writes_before)

    async def test_invalid_resulting_snapshot_error_is_propagated(self):
        initial = state.new_snapshot()
        owner_key = credentials.user_key(self.identity)
        target_key = credentials.binding_key(self.identity, self.task_id)
        initial["users"][owner_key] = {
            "platform_id": self.identity.platform_id,
            "qq_id": self.identity.qq_id,
        }
        initial["bindings"][target_key] = {
            "user_key": owner_key,
            "task_id": self.task_id,
        }
        kv = FakeKV(initial)
        store = storage.StateStore(kv, max_snapshot_bytes=_encoded_size(initial))
        await store.initialize()
        baseline = store.snapshot()

        with self.assertRaises(state.StateTooLargeError):
            await bindings.set_paused(
                store,
                identity=self.identity,
                task_id=self.task_id,
                paused=False,
                guard=lambda: None,
            )

        self.assertEqual(store.snapshot(), baseline)
        self.assertFalse(store.has_pending)
        self.assertEqual(len(kv.write_values), 1)


if __name__ == "__main__":
    unittest.main()
