import asyncio
import copy
import math
import unittest
from collections import deque

from state_support import load_module


state = load_module("state")
storage = load_module("storage")

_ABSENT = object()


class WriteControl:
    def __init__(self, *, blocked=False, error=None):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.error = error


class FakeKV:
    """A cache-first KV whose durable writer is explicitly controllable."""

    def __init__(self, initial=_ABSENT, *, get_error=None):
        self.cache = {}
        self.persisted = {}
        if initial is not _ABSENT:
            self.cache[state.STATE_KEY] = copy.deepcopy(initial)
            self.persisted[state.STATE_KEY] = copy.deepcopy(initial)
        self.get_error = get_error
        self.get_calls = 0
        self.write_values = []
        self._write_controls = deque()

    def queue_write(self, *, blocked=False, error=None):
        control = WriteControl(blocked=blocked, error=error)
        self._write_controls.append(control)
        return control

    async def get_kv_data(self, key, default):
        self.get_calls += 1
        if self.get_error is not None:
            raise self.get_error
        if key not in self.cache:
            return default
        return copy.deepcopy(self.cache[key])

    async def put_kv_data(self, key, value):
        control = self._write_controls.popleft() if self._write_controls else WriteControl()
        self.write_values.append(copy.deepcopy(value))

        # AstrBot's cache can retain the exact object before its writer finishes.
        self.cache[key] = value
        control.started.set()
        await control.release.wait()
        if control.error is not None:
            raise control.error
        self.persisted[key] = copy.deepcopy(value)


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def make_initialized_store(self, *, initial=_ABSENT, write_timeout=30.0):
        kv = FakeKV(initial)
        store = storage.StateStore(kv, write_timeout=write_timeout)
        await store.initialize()
        return kv, store

    def test_write_timeout_must_be_positive_and_finite(self):
        for invalid in (0, -1, math.inf, -math.inf, math.nan, True, "1", "not-a-number"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    storage.StateStore(FakeKV(), write_timeout=invalid)

    def test_snapshot_limit_cannot_be_disabled_by_invalid_value(self):
        for invalid in (0, -1, math.inf, math.nan, True, "1024"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    storage.StateStore(FakeKV(), max_snapshot_bytes=invalid)

    async def test_initialize_missing_state_saves_before_becoming_ready(self):
        kv = FakeKV()
        control = kv.queue_write(blocked=True)
        store = storage.StateStore(kv)

        initialize_task = asyncio.create_task(store.initialize())
        await control.started.wait()

        self.assertFalse(store.ready)
        self.assertTrue(store.has_pending)
        with self.assertRaises(storage.StateUnavailableError):
            store.snapshot()

        control.release.set()
        initialized = await initialize_task

        self.assertTrue(store.ready)
        self.assertFalse(store.has_pending)
        self.assertEqual(initialized, state.new_snapshot())
        self.assertEqual(kv.persisted[state.STATE_KEY], state.new_snapshot())
        self.assertEqual(len(kv.write_values), 1)

    async def test_initialize_existing_state_resaves_without_revision_change(self):
        initial = state.new_snapshot()
        initial["revision"] = 7
        initial["users"]["42"] = {"enabled": True}
        kv = FakeKV(initial)
        store = storage.StateStore(kv)

        initialized = await store.initialize()

        self.assertEqual(initialized, initial)
        self.assertEqual(kv.write_values, [initial])
        self.assertEqual(store.snapshot()["revision"], 7)
        self.assertTrue(store.ready)

        again = await store.initialize()
        self.assertEqual(again, initial)
        self.assertEqual(kv.get_calls, 1)
        self.assertEqual(len(kv.write_values), 1)

    async def test_initialize_honors_configured_snapshot_limit_before_writing(self):
        kv = FakeKV()
        store = storage.StateStore(kv, max_snapshot_bytes=1)

        with self.assertRaises(state.StateTooLargeError):
            await store.initialize()

        self.assertFalse(store.has_pending)
        self.assertEqual(kv.write_values, [])

    async def test_initialize_rejects_corrupt_or_unsupported_state_without_write(self):
        unsupported = state.new_snapshot()
        unsupported["schema_version"] = 999
        corrupt_values = (
            (None, state.InvalidStateError),
            (unsupported, state.UnsupportedSchemaError),
        )
        for value, expected_error in corrupt_values:
            with self.subTest(value=value):
                kv = FakeKV(value)
                store = storage.StateStore(kv)

                with self.assertRaises(expected_error):
                    await store.initialize()

                self.assertFalse(store.ready)
                self.assertFalse(store.has_pending)
                self.assertEqual(kv.write_values, [])
                self.assertEqual(kv.persisted[state.STATE_KEY], value)

    async def test_initialize_read_failure_is_not_treated_as_missing_state(self):
        read_error = OSError("read failed")
        kv = FakeKV(get_error=read_error)
        store = storage.StateStore(kv)

        with self.assertRaises(OSError) as raised:
            await store.initialize()

        self.assertIs(raised.exception, read_error)
        self.assertFalse(store.has_pending)
        self.assertEqual(kv.write_values, [])

    async def test_failed_initialize_keeps_pending_and_retries_without_readback(self):
        kv = FakeKV()
        kv.queue_write(error=OSError("secret writer detail"))
        store = storage.StateStore(kv)

        with self.assertRaises(storage.CommitUnconfirmedError) as raised:
            await store.initialize()

        self.assertNotIn("secret", str(raised.exception))
        self.assertTrue(store.has_pending)
        self.assertFalse(store.ready)
        self.assertEqual(kv.get_calls, 1)
        with self.assertRaises(storage.StateUnavailableError):
            store.snapshot()
        retried = await store.initialize()

        self.assertEqual(retried, state.new_snapshot())
        self.assertEqual(kv.get_calls, 1)
        self.assertEqual(kv.write_values[0], kv.write_values[1])
        self.assertTrue(store.ready)

        writes_before = len(kv.write_values)
        self.assertEqual(await store.retry_pending(), retried)
        self.assertEqual(len(kv.write_values), writes_before)

    async def test_cancelled_initialize_keeps_pending_for_identical_retry(self):
        kv = FakeKV()
        control = kv.queue_write(blocked=True)
        store = storage.StateStore(kv)

        initialize_task = asyncio.create_task(store.initialize())
        await control.started.wait()
        initialize_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await initialize_task

        self.assertTrue(store.has_pending)
        self.assertFalse(store.ready)
        with self.assertRaises(storage.StateUnavailableError):
            store.snapshot()

        initialized = await store.retry_pending()

        self.assertEqual(kv.write_values[0], kv.write_values[1])
        self.assertEqual(initialized, state.new_snapshot())
        self.assertTrue(store.ready)

    async def test_successful_mutation_increments_revision_and_returns_copies(self):
        kv, store = await self.make_initialized_store()

        committed = await store.mutate(
            lambda candidate: candidate["users"].update({"42": {"enabled": True}})
        )

        self.assertEqual(committed["revision"], 1)
        self.assertEqual(store.snapshot(), committed)
        self.assertEqual(kv.persisted[state.STATE_KEY], committed)

        committed["users"]["42"]["enabled"] = False
        snapshot = store.snapshot()
        snapshot["users"].clear()
        self.assertTrue(store.snapshot()["users"]["42"]["enabled"])

    async def test_callback_references_and_host_cache_cannot_mutate_internal_state(self):
        kv, store = await self.make_initialized_store()
        external = {"roles": []}
        retained = {}
        control = kv.queue_write(blocked=True)

        def mutate(candidate):
            candidate["users"]["42"] = external
            retained["candidate"] = candidate

        mutation_task = asyncio.create_task(store.mutate(mutate))
        await control.started.wait()
        external["roles"].append("external")
        retained["candidate"]["users"]["42"]["roles"].append("retained")
        self.assertEqual(store.snapshot()["users"], {})

        control.release.set()
        committed = await mutation_task
        self.assertEqual(committed["users"]["42"], {"roles": []})

        kv.cache[state.STATE_KEY]["users"]["42"]["roles"].append("cache")
        self.assertEqual(store.snapshot()["users"]["42"], {"roles": []})

    async def test_callback_exception_and_invalid_candidate_do_not_create_pending(self):
        _, store = await self.make_initialized_store()
        baseline = store.snapshot()

        class CallbackFailure(Exception):
            pass

        failure = CallbackFailure("callback detail")

        def raises(_candidate):
            raise failure

        with self.assertRaises(CallbackFailure) as raised:
            await store.mutate(raises)
        self.assertIs(raised.exception, failure)
        self.assertFalse(store.has_pending)
        self.assertEqual(store.snapshot(), baseline)

        with self.assertRaises(state.InvalidStateError):
            await store.mutate(lambda candidate: candidate["users"].update({"42": object()}))
        self.assertFalse(store.has_pending)
        self.assertEqual(store.snapshot(), baseline)

    async def test_callback_cannot_change_store_managed_fields(self):
        kv, store = await self.make_initialized_store()
        writes_before = len(kv.write_values)

        for field, value in (
            ("revision", 1),
            ("revision", False),
            ("schema_version", 2),
            ("schema_version", True),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(state.InvalidStateError):
                    await store.mutate(lambda candidate: candidate.__setitem__(field, value))
                self.assertFalse(store.has_pending)
                self.assertEqual(store.snapshot()["revision"], 0)

        self.assertEqual(len(kv.write_values), writes_before)

    async def test_async_callbacks_are_rejected_without_a_write(self):
        kv, store = await self.make_initialized_store()
        writes_before = len(kv.write_values)

        async def async_callback(candidate):
            candidate["users"]["42"] = {}

        with self.assertRaises(TypeError):
            await store.mutate(async_callback)
        with self.assertRaises(TypeError):
            await store.mutate(lambda candidate: async_callback(candidate))

        self.assertFalse(store.has_pending)
        self.assertEqual(len(kv.write_values), writes_before)
        self.assertEqual(store.snapshot()["revision"], 0)

    async def test_failed_mutation_never_reads_cache_and_retries_only_same_value(self):
        kv, store = await self.make_initialized_store()
        confirmed = store.snapshot()
        kv.queue_write(error=OSError("first failure"))

        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.mutate(lambda candidate: candidate["users"].update({"42": {}}))

        pending_value = copy.deepcopy(kv.write_values[-1])
        self.assertEqual(kv.cache[state.STATE_KEY], pending_value)
        self.assertEqual(kv.persisted[state.STATE_KEY], confirmed)
        self.assertEqual(store.snapshot(), confirmed)
        self.assertFalse(store.ready)
        self.assertTrue(store.has_pending)
        self.assertEqual(kv.get_calls, 1)

        callback_called = False

        def must_not_run(_candidate):
            nonlocal callback_called
            callback_called = True

        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.mutate(must_not_run)
        self.assertFalse(callback_called)
        kv.queue_write(error=OSError("second failure"))
        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.initialize()
        self.assertEqual(kv.get_calls, 1)
        self.assertTrue(store.has_pending)

        committed = await store.retry_pending()

        self.assertEqual(kv.write_values[-3:], [pending_value, pending_value, pending_value])
        self.assertEqual(committed, pending_value)
        self.assertEqual(committed["revision"], 1)
        self.assertEqual(kv.get_calls, 1)
        self.assertTrue(store.ready)

    async def test_cancelled_write_keeps_pending_for_identical_retry(self):
        kv, store = await self.make_initialized_store()
        confirmed = store.snapshot()
        control = kv.queue_write(blocked=True)

        mutation_task = asyncio.create_task(
            store.mutate(lambda candidate: candidate["users"].update({"42": {}}))
        )
        await control.started.wait()
        mutation_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await mutation_task

        pending_value = copy.deepcopy(kv.write_values[-1])
        self.assertTrue(store.has_pending)
        self.assertFalse(store.ready)
        self.assertEqual(store.snapshot(), confirmed)
        self.assertEqual(kv.persisted[state.STATE_KEY], confirmed)

        committed = await store.retry_pending()

        self.assertEqual(kv.write_values[-2:], [pending_value, pending_value])
        self.assertEqual(committed, pending_value)
        self.assertTrue(store.ready)

    async def test_timed_out_write_keeps_pending_for_identical_retry(self):
        kv, store = await self.make_initialized_store(write_timeout=0.01)
        confirmed = store.snapshot()
        kv.queue_write(blocked=True)

        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.mutate(lambda candidate: candidate["users"].update({"42": {}}))

        pending_value = copy.deepcopy(kv.write_values[-1])
        self.assertEqual(store.snapshot(), confirmed)
        self.assertTrue(store.has_pending)

        committed = await store.retry_pending()
        self.assertEqual(kv.write_values[-2:], [pending_value, pending_value])
        self.assertEqual(committed, pending_value)

    async def test_concurrent_mutations_are_serialized_without_lost_updates(self):
        kv, store = await self.make_initialized_store()
        first_write = kv.queue_write(blocked=True)

        first_task = asyncio.create_task(
            store.mutate(lambda candidate: candidate["users"].update({"first": {}}))
        )
        await first_write.started.wait()

        second_attempted = asyncio.Event()

        async def mutate_second():
            second_attempted.set()
            return await store.mutate(
                lambda candidate: candidate["users"].update({"second": {}})
            )

        second_task = asyncio.create_task(mutate_second())
        await second_attempted.wait()
        self.assertEqual(store.snapshot()["users"], {})

        first_write.release.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)

        self.assertEqual(first_result["revision"], 1)
        self.assertEqual(second_result["revision"], 2)
        self.assertEqual(set(second_result["users"]), {"first", "second"})
        self.assertEqual([value["revision"] for value in kv.write_values[-2:]], [1, 2])

    async def test_cancelling_lock_waiter_does_not_create_another_pending_value(self):
        kv, store = await self.make_initialized_store()
        first_write = kv.queue_write(blocked=True)
        first_task = asyncio.create_task(
            store.mutate(lambda candidate: candidate["users"].update({"first": {}}))
        )
        await first_write.started.wait()

        second_attempted = asyncio.Event()

        async def mutate_second():
            second_attempted.set()
            return await store.mutate(
                lambda candidate: candidate["users"].update({"second": {}})
            )

        second_task = asyncio.create_task(mutate_second())
        await second_attempted.wait()
        second_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second_task

        self.assertEqual(len(kv.write_values), 2)
        first_write.release.set()
        await first_task

        self.assertFalse(store.has_pending)
        self.assertEqual(set(store.snapshot()["users"]), {"first"})
        self.assertEqual(store.snapshot()["revision"], 1)
        self.assertEqual(len(kv.write_values), 2)

    async def test_stop_accepting_rejects_waiters_but_allows_active_commit(self):
        kv, store = await self.make_initialized_store()
        first_write = kv.queue_write(blocked=True)
        first_task = asyncio.create_task(
            store.mutate(lambda candidate: candidate["users"].update({"first": {}}))
        )
        await first_write.started.wait()

        second_attempted = asyncio.Event()
        second_callback_called = False

        async def mutate_second():
            nonlocal second_callback_called
            second_attempted.set()

            def callback(candidate):
                nonlocal second_callback_called
                second_callback_called = True
                candidate["users"]["second"] = {}

            return await store.mutate(callback)

        second_task = asyncio.create_task(mutate_second())
        await second_attempted.wait()
        store.stop_accepting()
        self.assertTrue(store.closing)
        self.assertFalse(store.ready)

        first_write.release.set()
        await first_task
        with self.assertRaises(storage.StateUnavailableError):
            await second_task

        self.assertFalse(second_callback_called)
        self.assertEqual(set(store.snapshot()["users"]), {"first"})
        with self.assertRaises(storage.StateUnavailableError):
            await store.initialize()
        with self.assertRaises(storage.StateUnavailableError):
            await store.retry_pending()
        with self.assertRaises(storage.StateUnavailableError):
            await store.mutate(lambda _candidate: None)

        await store.close()
        self.assertTrue(store.closed)

    async def test_close_waits_for_active_write_before_marking_closed(self):
        kv, store = await self.make_initialized_store()
        active_write = kv.queue_write(blocked=True)
        mutation_task = asyncio.create_task(
            store.mutate(lambda candidate: candidate["users"].update({"42": {}}))
        )
        await active_write.started.wait()

        close_attempted = asyncio.Event()

        async def close_store():
            close_attempted.set()
            await store.close()

        close_task = asyncio.create_task(close_store())
        await close_attempted.wait()
        self.assertTrue(store.closing)
        self.assertFalse(store.closed)

        active_write.release.set()
        await mutation_task
        await close_task

        self.assertTrue(store.closed)
        self.assertFalse(store.ready)
        self.assertEqual(store.snapshot()["revision"], 1)

    async def test_close_retries_value_left_by_failing_active_write(self):
        kv, store = await self.make_initialized_store()
        active_write = kv.queue_write(blocked=True, error=OSError("failed"))
        mutation_task = asyncio.create_task(
            store.mutate(lambda candidate: candidate["users"].update({"42": {}}))
        )
        await active_write.started.wait()

        close_attempted = asyncio.Event()

        async def close_store():
            close_attempted.set()
            await store.close()

        close_task = asyncio.create_task(close_store())
        await close_attempted.wait()
        active_write.release.set()

        with self.assertRaises(storage.CommitUnconfirmedError):
            await mutation_task
        await close_task

        self.assertEqual(kv.write_values[-2], kv.write_values[-1])
        self.assertTrue(store.closed)
        self.assertFalse(store.has_pending)
        self.assertEqual(store.snapshot()["revision"], 1)

    async def test_failed_close_stays_closing_and_later_close_retries_same_value(self):
        kv, store = await self.make_initialized_store()
        kv.queue_write(error=OSError("mutation failed"))
        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.mutate(lambda candidate: candidate["users"].update({"42": {}}))
        pending_value = copy.deepcopy(kv.write_values[-1])

        kv.queue_write(error=OSError("close failed"))
        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.close()

        self.assertTrue(store.closing)
        self.assertFalse(store.closed)
        self.assertTrue(store.has_pending)
        with self.assertRaises(storage.StateUnavailableError):
            await store.retry_pending()

        await store.close()
        self.assertEqual(kv.write_values[-3:], [pending_value, pending_value, pending_value])
        self.assertTrue(store.closed)
        self.assertFalse(store.has_pending)

        writes_before = len(kv.write_values)
        await store.close()
        self.assertEqual(len(kv.write_values), writes_before)

    async def test_cancelled_close_keeps_pending_and_can_be_retried(self):
        kv, store = await self.make_initialized_store()
        kv.queue_write(error=OSError("mutation failed"))
        with self.assertRaises(storage.CommitUnconfirmedError):
            await store.mutate(lambda candidate: candidate["users"].update({"42": {}}))
        pending_value = copy.deepcopy(kv.write_values[-1])

        close_write = kv.queue_write(blocked=True)
        close_task = asyncio.create_task(store.close())
        await close_write.started.wait()
        close_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await close_task

        self.assertTrue(store.closing)
        self.assertFalse(store.closed)
        self.assertTrue(store.has_pending)

        await store.close()
        self.assertEqual(kv.write_values[-3:], [pending_value, pending_value, pending_value])
        self.assertTrue(store.closed)
        self.assertFalse(store.has_pending)

    async def test_close_uninitialized_store_and_property_setters(self):
        store = storage.StateStore(FakeKV())

        await store.close()
        self.assertTrue(store.closing)
        self.assertTrue(store.closed)
        with self.assertRaises(storage.StateUnavailableError):
            store.snapshot()
        with self.assertRaises(AttributeError):
            store.ready = True
        with self.assertRaises(AttributeError):
            store.closing = False
        with self.assertRaises(AttributeError):
            store.closed = False
        with self.assertRaises(AttributeError):
            store.has_pending = True


if __name__ == "__main__":
    unittest.main()
