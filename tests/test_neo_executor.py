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
        self.bay.close()

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
        self.assertIn(run["run_id"], self.store.snapshot()["cleanup_jobs"])
        with self.assertRaises(neo.ExecutionBlocked):
            await self.executor.execute(self.request(), guard=self.authorized)
        self.assertEqual(self.bay.create_calls, 1)
        self.bay.keep_cargo = False
        await self.executor.recover()
        recovered = self.store.snapshot()["runs"][run["run_id"]]
        self.assertEqual(recovered["business_status"], "needs_auth")
        self.assertEqual(recovered["cleanup_status"], "cleaned")
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
        self.kv.fail_at = len(self.kv.writes) + 2
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
        checks = 0

        def guard(snapshot):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise PermissionError("authorization changed")

        with self.assertRaises(PermissionError):
            await self.executor.execute(self.request(), guard=guard)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertEqual(self.store.snapshot()["cleanup_jobs"], {})
        self.assertEqual(self.bay.cargos, {})

    async def test_close_cancels_active_execution_and_waits_for_cleanup(self):
        self.bay.hold_execution = True
        task = asyncio.create_task(
            self.executor.execute(self.request(), guard=self.authorized)
        )
        await asyncio.wait_for(self.bay.executing.wait(), 10)
        await self.executor.close()
        self.assertTrue(task.cancelled())
        run = next(iter(self.store.snapshot()["runs"].values()))
        self.assertEqual(run["business_status"], "cancelled")
        self.assertEqual(run["cleanup_status"], "cleaned")
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


if __name__ == "__main__":
    unittest.main()
