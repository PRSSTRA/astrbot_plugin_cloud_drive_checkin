import asyncio
import copy
import hashlib
import json
import unittest
from dataclasses import replace

import httpx
from neo_support import ExecutionKV, FakeBay
from state_support import load_module

neo = load_module("executors.neo", "_neo_execution_tests")
client_module = load_module("executors.client", "_neo_execution_tests")
storage = load_module("storage", "_neo_execution_tests")
permissions = load_module("permissions", "_neo_execution_tests")


class NeoExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = ExecutionKV()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        self.bay = FakeBay()
        self.client = client_module.NeoClient(
            client_module.NeoConnection("http://fake-bay.invalid", self.bay.token),
            transport=httpx.MockTransport(self.bay),
        )
        self.executor = neo.NeoExecutor(self.store, self.client)
        await self.executor.recover()
        self.identity = permissions.Identity("qq-bot", "10001")

    async def asyncTearDown(self):
        await self.executor.close()
        await self.store.close()
        await self.bay.close()

    def request(self, script=None, token=" FAKE_COOKIE;'\"\nsecond line "):
        if script is None:
            script = b'import json,sys\nprint(json.dumps({"status":"needs_auth","reason":"do not relay"}))\nsys.exit(0)\n'
        return neo.PreparedExecution(
            identity=self.identity,
            task_id="fake_checkin",
            task_version="1" * 64,
            source_commit="2" * 40,
            sha256=hashlib.sha256(script).hexdigest(),
            runtime="python",
            script=script,
            environment={"TOKEN": token},
            timeout_seconds=5,
        )

    @staticmethod
    def authorized(snapshot):
        return None

    async def test_recovery_rejects_admitted_record_that_has_resource_ids(self):
        self.bay.keep_cargo = True
        run = await self.executor.execute(self.request(), guard=self.authorized)

        def corrupt_phase(snapshot):
            snapshot["runs"][run["run_id"]].update(phase="admitted", finished_at=None)

        await self.store.mutate(corrupt_phase)
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.recover()
        self.assertIn(run["run_id"], self.store.snapshot()["cleanup_jobs"])
        self.assertIn(run["cargo_id"], self.bay.cargos)
        self.assertTrue(self.executor.blocked)

    async def test_no_allocation_proof_survives_unrelated_unconfirmed_write(self):
        proceed, failed = asyncio.Event(), asyncio.Event()
        original_put = self.kv.put_kv_data
        original_finalize = self.executor._finalize
        fail_once = True

        async def fail_unrelated_write(key, value):
            nonlocal fail_once
            if fail_once and "concurrent_probe" in value["users"]:
                fail_once = False
                self.kv.fail_at = len(self.kv.writes) + 1
            await original_put(key, value)

        async def concurrent_mutation():
            await proceed.wait()
            try:
                await self.store.mutate(
                    lambda snapshot: snapshot["users"].update(
                        {"concurrent_probe": {"value": 1}}
                    )
                )
            except storage.CommitUnconfirmedError:
                failed.set()

        async def delayed_finalize(*args, **kwargs):
            await asyncio.wait_for(failed.wait(), 10)
            await original_finalize(*args, **kwargs)

        def revoke_after_admission(snapshot):
            if snapshot["runs"]:
                proceed.set()
                raise PermissionError("authorization revoked")

        self.kv.put_kv_data = fail_unrelated_write
        self.executor._finalize = delayed_finalize
        other = asyncio.create_task(concurrent_mutation())
        with self.assertRaises(PermissionError):
            await self.executor.execute(self.request(), guard=revoke_after_admission)
        await other
        self.assertEqual(self.bay.create_calls, 0)
        pending = copy.deepcopy(self.kv.writes[-1])
        self.kv.fail_at = None
        await self.store.retry_pending()
        self.assertEqual(self.kv.writes[-1], pending)
        await self.executor.recover()
        self.assertEqual(
            self.store.snapshot()["users"]["concurrent_probe"], {"value": 1}
        )
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertFalse(self.executor.blocked)

    async def test_revocation_during_executing_commit_prevents_business_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        allowed = True
        original_put = self.kv.put_kv_data

        async def delayed_put(key, value):
            if any(run["phase"] == "executing" for run in value["runs"].values()):
                entered.set()
                await release.wait()
            await original_put(key, value)

        def guard(snapshot):
            if not allowed:
                raise PermissionError("authorization revoked")

        self.kv.put_kv_data = delayed_put
        task = asyncio.create_task(self.executor.execute(self.request(), guard=guard))
        await asyncio.wait_for(entered.wait(), 10)
        allowed = False
        release.set()
        with self.assertRaises(PermissionError):
            await task
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.cargos, {})

    async def test_revocation_after_script_upload_prevents_credential_upload(self):
        allowed = True
        original_upload = self.client.upload

        async def revoke_after_script(sandbox_id, path, content):
            nonlocal allowed
            await original_upload(sandbox_id, path, content)
            if path.endswith("script.py"):
                allowed = False

        def guard(snapshot):
            if not allowed:
                raise PermissionError("authorization changed")

        self.client.upload = revoke_after_script
        with self.assertRaises(PermissionError):
            await self.executor.execute(self.request(), guard=guard)
        self.assertNotIn("checkin/credentials.json", self.bay.upload_paths)
        self.assertEqual(self.bay.execution_calls, 0)
        run = next(iter(self.store.snapshot()["runs"].values()))
        self.assertFalse(run["business_attempted"])

    async def test_pending_state_before_credentials_fails_closed(self):
        original_upload = self.client.upload

        async def make_state_pending(sandbox_id, path, content):
            await original_upload(sandbox_id, path, content)
            if path.endswith("script.py"):
                self.kv.fail_at = len(self.kv.writes) + 1
                try:
                    await self.store.mutate(
                        lambda snapshot: snapshot["users"].update(
                            {"pending_guard_probe": {"value": 1}}
                        )
                    )
                except storage.CommitUnconfirmedError:
                    pass
                finally:
                    self.kv.fail_at = None

        self.client.upload = make_state_pending
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(self.request(), guard=self.authorized)
        self.assertTrue(self.store.has_pending)
        self.assertNotIn("checkin/credentials.json", self.bay.upload_paths)
        self.assertEqual(self.bay.execution_calls, 0)

    def test_recovery_attempt_fact_is_conservative_by_phase(self):
        for phase in ("admitted", "creating", "preparing"):
            self.assertFalse(
                self.executor._recovery_business_attempted(
                    {"phase": phase, "business_attempted": True}
                )
            )
        self.assertTrue(
            self.executor._recovery_business_attempted(
                {"phase": "executing", "business_attempted": False}
            )
        )
        self.assertTrue(
            self.executor._recovery_business_attempted({"phase": "finished"})
        )
        self.assertFalse(
            self.executor._recovery_business_attempted(
                {"phase": "finished", "business_attempted": False}
            )
        )

    async def test_revocation_during_creation_intent_commit_prevents_allocation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        allowed = True
        original_put = self.kv.put_kv_data

        async def delayed_put(key, value):
            if any(run["phase"] == "creating" for run in value["runs"].values()):
                entered.set()
                await release.wait()
            await original_put(key, value)

        def guard(snapshot):
            if not allowed:
                raise PermissionError("authorization revoked")

        self.kv.put_kv_data = delayed_put
        task = asyncio.create_task(self.executor.execute(self.request(), guard=guard))
        await asyncio.wait_for(entered.wait(), 10)
        allowed = False
        release.set()
        with self.assertRaises(PermissionError):
            await task
        self.assertEqual(self.bay.create_calls, 0)
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertFalse(self.executor.blocked)

    async def test_cancelling_response_after_remote_effect_preserves_unknown_result(
        self,
    ):
        effect_applied = asyncio.Event()
        original_exec = self.client.exec

        async def delayed_response(sandbox_id, command, timeout):
            if command == "python3 checkin/bootstrap.py":
                effect_applied.set()
                await asyncio.Event().wait()
            return await original_exec(sandbox_id, command, timeout)

        self.client.exec = delayed_response
        task = asyncio.create_task(
            self.executor.execute(self.request(), guard=self.authorized)
        )
        await asyncio.wait_for(effect_applied.wait(), 10)
        await self.executor.close()
        self.assertTrue(task.cancelled())
        run = next(iter(self.store.snapshot()["runs"].values()))
        self.assertEqual(run["business_status"], "unknown")
        self.assertEqual(run["process_status"], "interrupted")
        self.assertEqual(run["cleanup_status"], "cleaned")
        self.assertTrue(run["business_attempted"])
        self.assertEqual(self.bay.cargos, {})

    async def test_actual_wrapper_result_and_cleaned_state_without_secret_disclosure(
        self,
    ):
        token = " FAKE_PRIVATE_COOKIE;'\"\nsecond line "
        script = b'import json,os,sys\nprint(os.environ["TOKEN"],file=sys.stderr)\nprint(json.dumps({"status":"needs_auth","reason":os.environ["TOKEN"]}))\n'
        run = await self.executor.execute(
            self.request(script, token), guard=self.authorized
        )
        self.assertEqual(run["business_status"], "needs_auth")
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["cleanup_status"], "cleaned")
        self.assertTrue(run["business_attempted"])
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertEqual((self.bay.sandboxes, self.bay.cargos), ({}, {}))
        self.assertNotIn("FAKE_PRIVATE_COOKIE", json.dumps(self.kv.writes))
        self.assertNotIn("FAKE_PRIVATE_COOKIE", json.dumps(self.bay.history))
        self.assertNotIn("do not relay", json.dumps(run))

    async def test_two_users_share_one_serial_executor_not_resources_or_credentials(
        self,
    ):
        first = self.request(token="FAKE_USER_ONE")
        second = replace(
            first,
            identity=permissions.Identity("qq-bot", "10002"),
            environment={"TOKEN": "FAKE_USER_TWO"},
        )
        runs = await asyncio.gather(
            self.executor.execute(first, guard=self.authorized),
            self.executor.execute(second, guard=self.authorized),
        )
        self.assertNotEqual(runs[0]["sandbox_id"], runs[1]["sandbox_id"])
        self.assertNotEqual(runs[0]["cargo_id"], runs[1]["cargo_id"])
        self.assertNotEqual(runs[0]["user_key"], runs[1]["user_key"])
        self.assertEqual(self.bay.max_executions, 1)
        self.assertEqual(
            self.bay.credential_digests,
            [
                hashlib.sha256(value.encode()).hexdigest()
                for value in ("FAKE_USER_ONE", "FAKE_USER_TWO")
            ],
        )

    async def test_cleanup_failure_blocks_new_execution_and_recovery_never_reexecutes(
        self,
    ):
        self.bay.keep_cargo = True
        run = await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(run["cleanup_status"], "pending")
        self.assertIsNone(run["cleanup_finished_at"])
        self.assertIn(run["run_id"], self.store.snapshot()["cleanup_jobs"])
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(self.bay.create_calls, 1)
        self.bay.keep_cargo = False
        await self.executor.recover()
        recovered = self.store.snapshot()["runs"][run["run_id"]]
        self.assertEqual(recovered["business_status"], "needs_auth")
        self.assertEqual(recovered["cleanup_status"], "cleaned")
        self.assertIsInstance(recovered["cleanup_finished_at"], str)
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertFalse(self.executor.blocked)

    async def test_intent_write_failure_never_creates_resources(self):
        self.kv.fail_at = len(self.kv.writes) + 1
        with self.assertRaises(storage.CommitUnconfirmedError):
            await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(self.bay.create_calls, 0)
        self.assertFalse(self.store.ready)
        self.kv.fail_at = None

    async def test_resource_id_write_failure_cleans_locally_without_overwriting_pending(
        self,
    ):
        original_put = self.kv.put_kv_data
        fail_once = True

        async def fail_allocated_write(key, value):
            nonlocal fail_once
            if fail_once and any(
                run["phase"] == "preparing" for run in value["runs"].values()
            ):
                fail_once = False
                self.kv.fail_at = len(self.kv.writes) + 1
            await original_put(key, value)

        self.kv.put_kv_data = fail_allocated_write
        with self.assertRaises(storage.CommitUnconfirmedError):
            await self.executor.execute(self.request(), guard=self.authorized)
        pending = copy.deepcopy(self.kv.writes[-1])
        run_id = next(iter(pending["runs"]))
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual((self.bay.sandboxes, self.bay.cargos), ({}, {}))
        self.assertIsNone(self.store.snapshot()["runs"][run_id]["sandbox_id"])
        self.kv.fail_at = None
        await self.store.retry_pending()
        self.assertEqual(self.kv.writes[-1], pending)
        await self.executor.recover()
        self.assertEqual(
            self.store.snapshot()["runs"][run_id]["business_status"], "unknown"
        )
        self.assertFalse(self.store.snapshot()["runs"][run_id]["business_attempted"])
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.create_calls, 1)

    async def test_ambiguous_create_retains_blocker_without_guessing_or_replaying(self):
        self.bay.ambiguous_create = True
        run = await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(run["cleanup_status"], "pending")
        self.assertIsNone(run["sandbox_id"])
        await self.executor.recover()
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(self.bay.create_calls, 1)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.bay.upload_paths, [])
        self.assertEqual(len(self.bay.sandboxes), 1)

    async def test_lost_business_response_is_unknown_and_not_retried(self):
        self.bay.ambiguous_execution = True
        run = await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(run["business_status"], "unknown")
        self.assertEqual(run["cleanup_status"], "cleaned")
        await self.executor.recover()
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_hash_mismatch_rejected_before_network_side_effects(self):
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(
                replace(self.request(), sha256="0" * 64), guard=self.authorized
            )
        self.assertEqual(self.bay.create_calls, 0)
        self.assertEqual(self.store.snapshot()["runs"], {})

    async def test_authorization_change_before_execution_cleans_without_running(self):
        def guard(snapshot):
            if any(run["phase"] == "preparing" for run in snapshot["runs"].values()):
                raise PermissionError("authorization changed")

        with self.assertRaises(PermissionError):
            await self.executor.execute(self.request(), guard=guard)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.bay.create_calls, 1)
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.cargos, {})
        run = next(iter(self.store.snapshot()["runs"].values()))
        self.assertFalse(run["business_attempted"])

    async def test_close_cancels_active_execution_and_waits_for_cleanup(self):
        self.bay.hold_execution = True
        task = asyncio.create_task(
            self.executor.execute(self.request(), guard=self.authorized)
        )
        await asyncio.wait_for(self.bay.executing.wait(), 10)
        await self.executor.close()
        self.assertTrue(task.cancelled())
        run = next(iter(self.store.snapshot()["runs"].values()))
        self.assertEqual(run["process_status"], "interrupted")
        self.assertEqual(run["business_status"], "unknown")
        self.assertEqual(run["cleanup_status"], "cleaned")
        self.assertTrue(run["business_attempted"])
        self.assertEqual(self.bay.cargos, {})

    async def test_pending_cleanup_cannot_move_to_a_different_connection(self):
        self.bay.keep_cargo = True
        await self.executor.execute(self.request(), guard=self.authorized)
        other_client = client_module.NeoClient(
            client_module.NeoConnection("http://another-bay.invalid", self.bay.token),
            transport=httpx.MockTransport(self.bay),
        )
        other = neo.NeoExecutor(self.store, other_client)
        try:
            with self.assertRaises(neo.ExecutionBlocked):
                await other.recover()
            self.assertEqual(len(self.bay.cargos), 1)
        finally:
            await other.close()

    async def test_wrapper_timeout_is_distinct_from_business_evidence(self):
        request = replace(
            self.request(b"import time\ntime.sleep(20)\n"), timeout_seconds=1
        )
        run = await self.executor.execute(request, guard=self.authorized)
        self.assertEqual(run["process_status"], "timeout")
        self.assertEqual(run["business_status"], "timeout")
        self.assertEqual(run["cleanup_status"], "cleaned")
        self.assertTrue(run["business_attempted"])

    async def test_caller_supplied_run_id_is_stable_and_cannot_replay(self):
        run_id = "a" * 32
        run = await self.executor.execute(
            self.request(), guard=self.authorized, run_id=run_id
        )
        self.assertEqual(run["run_id"], run_id)
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(
                self.request(), guard=self.authorized, run_id=run_id
            )
        self.assertEqual(self.bay.execution_calls, 1)

    async def test_invalid_supplied_run_id_has_no_network_side_effect(self):
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(
                self.request(), guard=self.authorized, run_id="not-a-uuidhex"
            )
        self.assertEqual(self.bay.create_calls, 0)

    async def test_close_continues_after_waiter_cancel_and_marks_quiescent(self):
        client_close_entered = asyncio.Event()
        release_client_close = asyncio.Event()
        original_close = self.client.close

        async def delayed_client_close():
            client_close_entered.set()
            await release_client_close.wait()
            await original_close()

        self.client.close = delayed_client_close
        close_waiter = asyncio.create_task(self.executor.close())
        await asyncio.wait_for(client_close_entered.wait(), 10)
        self.assertTrue(self.executor.quiescent)
        close_waiter.cancel()
        await asyncio.gather(close_waiter, return_exceptions=True)
        release_client_close.set()
        await self.executor.close()
        self.assertTrue(self.executor.quiescent)


if __name__ == "__main__":
    unittest.main()
