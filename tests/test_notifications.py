import asyncio
import copy
import json
import unittest
from dataclasses import dataclass, field
from unittest import mock

from state_support import load_module

notifications = load_module("notifications", "_notification_tests")
state = load_module("state", "_notification_tests")
storage = load_module("storage", "_notification_tests")


def _item(run_id="a" * 32, **changes):
    item = {
        "kind": "manual_checkin",
        "run_id": run_id,
        "platform_id": "qq",
        "qq_id": "10001",
        "user_key": "qq:10001",
        "binding_key": "qq:10001:tianyi_cloud",
        "task_id": "tianyi_cloud",
        "task_name": "天翼云盘（配置显示名）",
        "task_version": "1" * 64,
        "artifact_sha256": "2" * 64,
        "inputs_sha256": "3" * 64,
        "origin": "qq:friend:10001",
        "queued_at": "2026-01-02T03:04:05+00:00",
        "expires_at": "2026-01-02T03:19:05+00:00",
        "sequence": 1,
        "status": "queued",
    }
    item.update(changes)
    return item


def _result(**changes):
    result = {
        "protocol": 1,
        "process_status": "exited",
        "exit_code": 0,
        "business_status": "success",
        "reason_code": "reported_result",
        "duration_ms": 1234,
    }
    result.update(changes)
    return result


class NotificationReservationTests(unittest.TestCase):
    def test_terminal_rendering_uses_only_fixed_result_labels(self):
        snapshot = state.new_snapshot()
        item = _item()
        notifications.reserve_terminal(snapshot, item)
        terminal = {
            **item,
            "status": "finished",
            "finished_at": "2026-01-02T03:04:07+00:00",
        }
        result = _result(
            process_status="start_error",
            exit_code=None,
            business_status="config_error",
            reason_code="runner_error",
            queue_reason="credentials_missing",
            exception="raw exception SECRET_VALUE",
            environment={"TOKEN": "RAW_ENV_SECRET"},
            upstream_text="untrusted upstream response",
        )

        notifications.enqueue_terminal(snapshot, terminal, result)

        record = snapshot["outbox"][item["run_id"]]
        self.assertEqual(record["phase"], "pending")
        self.assertNotIn("_reserve", record)
        self.assertEqual(
            record["result"],
            {
                "protocol": 1,
                "process_status": "start_error",
                "exit_code": None,
                "business_status": "config_error",
                "reason_code": "runner_error",
                "duration_ms": 1234,
                "queue_reason": "credentials_missing",
            },
        )
        self.assertIn("任务：天翼云盘（配置显示名）", record["message"])
        self.assertIn(f"运行 ID：{item['run_id']}", record["message"])
        self.assertIn("状态：配置错误", record["message"])
        self.assertIn("原因：本人凭据不存在", record["message"])
        self.assertIn("耗时：1234 毫秒", record["message"])
        self.assertIn("处理建议：请使用 /签到 录入 tianyi_cloud", record["message"])
        serialized = json.dumps(record, ensure_ascii=False)
        for unsafe in (
            "raw exception",
            "SECRET_VALUE",
            "RAW_ENV_SECRET",
            "untrusted upstream response",
        ):
            self.assertNotIn(unsafe, serialized)

    def test_invalid_result_text_is_replaced_not_rendered_or_persisted(self):
        snapshot = state.new_snapshot()
        item = _item()
        notifications.reserve_terminal(snapshot, item)
        raw = _result(
            business_status="upstream says account=SECRET_ACCOUNT",
            reason_code="response body SECRET_BODY",
            duration_ms=42,
            queue_reason="unrecognized SECRET_REASON",
        )

        notifications.enqueue_terminal(snapshot, item, raw)

        record = snapshot["outbox"][item["run_id"]]
        self.assertEqual(
            record["result"],
            {
                "protocol": 1,
                "process_status": "interrupted",
                "exit_code": None,
                "business_status": "unknown",
                "reason_code": "no_business_evidence",
                "duration_ms": 42,
            },
        )
        self.assertIn("状态：结果未知", record["message"])
        self.assertIn("原因：缺少可靠的业务结果", record["message"])
        self.assertNotIn("SECRET", json.dumps(record, ensure_ascii=False))

    def test_reservation_is_idempotent_but_rejects_conflicts_and_missing_slots(self):
        snapshot = state.new_snapshot()
        item = _item()
        notifications.reserve_terminal(snapshot, item)
        first = copy.deepcopy(snapshot["outbox"][item["run_id"]])

        notifications.reserve_terminal(snapshot, item)
        self.assertEqual(snapshot["outbox"][item["run_id"]], first)
        with self.assertRaises(notifications.NotificationReservationError):
            notifications.reserve_terminal(
                snapshot, {**item, "origin": "qq:friend:someone-else"}
            )
        with self.assertRaises(notifications.NotificationReservationError):
            notifications.enqueue_terminal(snapshot, _item("b" * 32), _result())

    def test_capacity_is_strict_until_queue_atomically_prunes_history(self):
        snapshot = state.new_snapshot()
        snapshot["outbox"] = {
            f"{index:032x}": {
                "kind": "manual_terminal_notification",
                "phase": "sent",
            }
            for index in range(notifications.MAX_OUTBOX_ITEMS)
        }
        before = copy.deepcopy(snapshot["outbox"])

        self.assertFalse(notifications.terminal_capacity(snapshot))
        with self.assertRaises(notifications.NotificationCapacityError):
            notifications.reserve_terminal(snapshot, _item("f" * 32))
        self.assertEqual(snapshot["outbox"], before)

        snapshot["outbox"].pop(next(iter(snapshot["outbox"])))
        self.assertTrue(notifications.terminal_capacity(snapshot))
        notifications.reserve_terminal(snapshot, _item("f" * 32))
        self.assertEqual(len(snapshot["outbox"]), notifications.MAX_OUTBOX_ITEMS)


class ControlledKV:
    def __init__(self):
        self.value = None
        self.confirmed = asyncio.Queue()
        self.next_gate = None

    async def get_kv_data(self, key, default):
        return default if self.value is None else copy.deepcopy(self.value)

    async def put_kv_data(self, key, value):
        gate = self.next_gate
        self.next_gate = None
        if gate is not None:
            gate.started.set()
            await gate.release.wait()
            if gate.error is not None:
                raise gate.error
        self.value = copy.deepcopy(value)
        self.confirmed.put_nowait(copy.deepcopy(value))


@dataclass
class WriteGate:
    error: BaseException | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class SleepCall:
    delay: float
    release: asyncio.Event = field(default_factory=asyncio.Event)


class ManualSleeper:
    def __init__(self):
        self.started = asyncio.Queue()

    async def __call__(self, delay):
        call = SleepCall(delay)
        self.started.put_nowait(call)
        await call.release.wait()


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@dataclass
class SendCall:
    origin: str
    message: str
    release: asyncio.Event = field(default_factory=asyncio.Event)


class GatedSender:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.started = asyncio.Queue()
        self.cancelled = asyncio.Event()

    async def __call__(self, origin, message):
        call = SendCall(origin, message)
        index = len(self.calls)
        self.calls.append(call)
        self.started.put_nowait(call)
        try:
            await call.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class NotificationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = ControlledKV()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        await self._write()
        self.clock = FakeClock()
        self.services = []
        self.senders = []

    async def asyncTearDown(self):
        for sender in self.senders:
            for call in sender.calls:
                call.release.set()
        for service in self.services:
            await service.close()

    async def _write(self):
        return await asyncio.wait_for(self.kv.confirmed.get(), 1)

    async def _terminal(self, *, item=None, result=None):
        item = item or _item()
        result = result or _result()
        await self.store.mutate(
            lambda snapshot: notifications.reserve_terminal(snapshot, item)
        )
        await self._write()
        terminal = {
            **item,
            "status": "finished",
            "finished_at": "2026-01-02T03:04:07+00:00",
            "result": result,
        }
        await self.store.mutate(
            lambda snapshot: notifications.enqueue_terminal(snapshot, terminal, result)
        )
        await self._write()
        return terminal

    def _service(self, outcomes, *, sleep=asyncio.sleep):
        sender = GatedSender(outcomes)
        service = notifications.NotificationService(
            self.store, sender, clock=self.clock, sleep=sleep
        )
        self.senders.append(sender)
        self.services.append(service)
        return service, sender

    async def _started(self, sender):
        call = await asyncio.wait_for(sender.started.get(), 1)
        sending = await self._write()
        return call, sending

    async def test_empty_initialize_does_not_write_state(self):
        sleeper = ManualSleeper()
        service, sender = self._service([], sleep=sleeper)

        await service.initialize()
        waiting = await asyncio.wait_for(sleeper.started.get(), 1)

        self.assertLessEqual(waiting.delay, notifications.OUTBOX_RECHECK_SECONDS)
        self.assertTrue(self.kv.confirmed.empty())
        self.assertEqual(sender.calls, [])

    async def test_precommit_wake_is_rechecked_after_terminal_commit(self):
        sleeper = ManualSleeper()
        service, sender = self._service([True], sleep=sleeper)
        await service.initialize()
        await asyncio.wait_for(sleeper.started.get(), 1)
        item = _item()
        terminal = {
            **item,
            "status": "finished",
            "finished_at": "2026-01-02T03:04:07+00:00",
        }
        commit_gate = WriteGate()
        self.kv.next_gate = commit_gate

        def commit_terminal(snapshot):
            notifications.reserve_terminal(snapshot, item)
            notifications.enqueue_terminal(snapshot, terminal, _result())
            service.wake()

        committing = asyncio.create_task(self.store.mutate(commit_terminal))
        await asyncio.wait_for(commit_gate.started.wait(), 1)
        periodic = await asyncio.wait_for(sleeper.started.get(), 1)
        self.assertLessEqual(periodic.delay, notifications.OUTBOX_RECHECK_SECONDS)
        self.assertEqual(sender.calls, [])

        commit_gate.release.set()
        await committing
        await self._write()
        periodic.release.set()
        call, sending = await self._started(sender)
        self.assertEqual(sending["outbox"][item["run_id"]]["phase"], "sending")
        call.release.set()
        sent = await self._write()
        self.assertEqual(sent["outbox"][item["run_id"]]["phase"], "sent")

    async def test_false_retries_three_times_then_marks_definite_failure(self):
        item = await self._terminal()
        service, sender = self._service([False, False, False])
        await service.initialize()

        for attempt in range(1, 4):
            call, sending = await self._started(sender)
            record = sending["outbox"][item["run_id"]]
            self.assertEqual(record["phase"], "sending")
            self.assertEqual(record["attempts"], attempt)
            self.assertEqual(call.origin, item["origin"])
            call.release.set()
            settled = await self._write()
            expected = "failed" if attempt == 3 else "pending"
            self.assertEqual(settled["outbox"][item["run_id"]]["phase"], expected)
            if attempt < 3:
                self.clock.advance(notifications.RETRY_DELAY_SECONDS)
                service.wake()

        self.assertEqual(len(sender.calls), notifications.MAX_SEND_ATTEMPTS)
        self.assertFalse(service.frozen)

    async def test_retry_deadline_stops_late_definite_retry(self):
        item = await self._terminal()
        service, sender = self._service([False, True])
        await service.initialize()
        call, _ = await self._started(sender)
        call.release.set()
        pending = await self._write()
        record = pending["outbox"][item["run_id"]]
        self.assertEqual(record["phase"], "pending")
        self.assertEqual(
            record["retry_deadline_at"],
            self.clock.value + notifications.RETRY_DEADLINE_SECONDS,
        )

        self.clock.advance(notifications.RETRY_DEADLINE_SECONDS)
        service.wake()
        expired = await self._write()
        self.assertEqual(expired["outbox"][item["run_id"]]["phase"], "failed")
        self.assertEqual(len(sender.calls), 1)

    async def test_exception_is_unknown_without_error_text_or_retry(self):
        item = await self._terminal()
        service, sender = self._service(
            [RuntimeError("upstream exception SECRET_TOKEN=raw")]
        )
        await service.initialize()
        call, _ = await self._started(sender)
        call.release.set()
        unknown = await self._write()
        record = unknown["outbox"][item["run_id"]]

        self.assertEqual(record["phase"], "unknown")
        self.assertEqual(record["attempts"], 1)
        self.assertNotIn("SECRET_TOKEN", json.dumps(record, ensure_ascii=False))
        self.clock.advance(100)
        service.wake()
        await service.close()
        self.assertEqual(len(sender.calls), 1)

    async def test_send_timeout_is_unknown_and_never_retried(self):
        item = await self._terminal()
        service, sender = self._service([True])

        with mock.patch.object(notifications, "SEND_TIMEOUT_SECONDS", 0.01):
            await service.initialize()
            await self._started(sender)
            unknown = await self._write()

        record = unknown["outbox"][item["run_id"]]
        self.assertEqual(record["phase"], "unknown")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(sender.cancelled.is_set())
        self.clock.advance(100)
        service.wake()
        await service.close()
        self.assertEqual(len(sender.calls), 1)

    async def test_non_boolean_send_result_is_ambiguous(self):
        item = await self._terminal()
        service, sender = self._service(["truthy but not confirmed"])
        await service.initialize()
        call, _ = await self._started(sender)
        call.release.set()
        unknown = await self._write()

        self.assertEqual(unknown["outbox"][item["run_id"]]["phase"], "unknown")
        self.assertEqual(len(sender.calls), 1)

    async def test_reload_changes_sending_to_unknown_without_blind_resend(self):
        item = await self._terminal()

        def interrupted(snapshot):
            snapshot["outbox"][item["run_id"]].update(
                phase="sending",
                attempts=1,
                next_attempt_at=None,
                retry_deadline_at=self.clock.value + 30,
            )

        await self.store.mutate(interrupted)
        await self._write()
        service, sender = self._service([True])

        await service.initialize()
        recovered = await self._write()

        self.assertEqual(recovered["outbox"][item["run_id"]]["phase"], "unknown")
        self.assertEqual(sender.calls, [])

    async def test_reserved_placeholder_is_never_sent(self):
        item = _item()
        await self.store.mutate(
            lambda snapshot: notifications.reserve_terminal(snapshot, item)
        )
        await self._write()
        service, sender = self._service([True])

        await service.initialize()
        await service.close()

        self.assertEqual(sender.calls, [])
        self.assertEqual(
            self.store.snapshot()["outbox"][item["run_id"]]["phase"],
            "reserved",
        )

    async def test_unconfirmed_sending_intent_freezes_without_external_send(self):
        await self._terminal()
        gate = WriteGate(error=OSError("unconfirmed write"))
        self.kv.next_gate = gate
        service, sender = self._service([True])

        await service.initialize()
        await asyncio.wait_for(gate.started.wait(), 1)
        self.assertEqual(sender.calls, [])
        gate.release.set()
        await service.close()

        self.assertTrue(service.frozen)
        self.assertTrue(self.store.has_pending)
        self.assertEqual(sender.calls, [])

    async def test_drain_close_sends_unclaimed_immediately_due_notice(self):
        sleeper = ManualSleeper()
        service, sender = self._service([True], sleep=sleeper)
        await service.initialize()
        await asyncio.wait_for(sleeper.started.get(), 1)
        item = await self._terminal()

        closing = asyncio.create_task(service.close(drain=True))
        call, sending = await self._started(sender)
        self.assertEqual(sending["outbox"][item["run_id"]]["phase"], "sending")
        self.assertFalse(closing.done())
        call.release.set()
        sent = await self._write()
        await closing

        self.assertEqual(sent["outbox"][item["run_id"]]["phase"], "sent")
        self.assertEqual(len(sender.calls), 1)

    async def test_drain_close_does_not_send_future_retry_early(self):
        item = await self._terminal()
        service, sender = self._service([False, True])
        await service.initialize()
        call, _ = await self._started(sender)
        call.release.set()
        pending = await self._write()
        self.assertEqual(pending["outbox"][item["run_id"]]["phase"], "pending")

        await service.close(drain=True)

        record = self.store.snapshot()["outbox"][item["run_id"]]
        self.assertEqual(record["phase"], "pending")
        self.assertEqual(record["attempts"], 1)
        self.assertGreater(record["next_attempt_at"], self.clock.value)
        self.assertEqual(len(sender.calls), 1)

    async def test_close_waits_for_in_flight_send_and_persists_result(self):
        item = await self._terminal()
        service, sender = self._service([True])
        await service.initialize()
        call, _ = await self._started(sender)

        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        call.release.set()
        sent = await self._write()
        await closing

        self.assertEqual(sent["outbox"][item["run_id"]]["phase"], "sent")

    async def test_cancelled_close_marks_in_flight_send_unknown(self):
        item = await self._terminal()
        service, sender = self._service([True])
        await service.initialize()
        await self._started(sender)

        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.wait_for(sender.cancelled.wait(), 1)
        unknown = await self._write()
        with self.assertRaises(asyncio.CancelledError):
            await closing

        self.assertEqual(unknown["outbox"][item["run_id"]]["phase"], "unknown")
        self.assertFalse(self.store.has_pending)

    async def test_cancelled_close_preserves_unconfirmed_unknown_write(self):
        item = await self._terminal()
        service, sender = self._service([True])
        await service.initialize()
        await self._started(sender)
        unknown_write = WriteGate()
        self.kv.next_gate = unknown_write

        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.wait_for(sender.cancelled.wait(), 1)
        await asyncio.wait_for(unknown_write.started.wait(), 1)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing

        self.assertTrue(self.store.has_pending)
        self.assertEqual(
            self.store.snapshot()["outbox"][item["run_id"]]["phase"],
            "sending",
        )
        recovered = await self.store.retry_pending()
        await self._write()
        self.assertEqual(recovered["outbox"][item["run_id"]]["phase"], "unknown")


if __name__ == "__main__":
    unittest.main()
