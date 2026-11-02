import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    FakeEvent,
    patch_astrbot_modules,
)
from neo_support import FakeBay
from state_support import load_module
from test_artifact_service import GenericArtifactRepository, TrackedStream

_TOKEN_ONE = " FAKE_GENERIC_ONE;'\"$()\n账户 "
_TOKEN_TWO = " FAKE_GENERIC_TWO;'\"$()\n第二账户 "
_FIXED_MODE = " fixed;'\"$()\n模式 "
_HOST_NODE = shutil.which("node")


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, 23, 9, 50, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _bundle(path, payload):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(path, date_time=(2020, 1, 2, 3, 4, 6))
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, payload)
    return output.getvalue()


def _locked_repository(runtime):
    repository = GenericArtifactRepository(runtime)
    if runtime == "python":
        repository.source = (
            b"import json, os, pinned_helper\n"
            b"valid = (os.environ['MODE'] == "
            + repr(_FIXED_MODE).encode("utf-8")
            + b" and os.environ['ENABLED'] == 'true' and os.environ['TOKEN'].startswith(' FAKE_GENERIC_'))\n"
            b"status = pinned_helper.status(int(os.environ['COUNT'])) if valid else 'failed'\n"
            b"print(json.dumps({'status': status}))\n"
        )
        repository.bundle = _bundle(
            "pinned_helper.py",
            b"def status(count):\n    return 'already_signed' if count == 2 else 'success'\n",
        )
    else:
        repository.source = (
            "const helper = require('pinned-helper');\n"
            f"const valid = process.env.MODE === {json.dumps(_FIXED_MODE)} && process.env.ENABLED === 'true' && process.env.TOKEN.startsWith(' FAKE_GENERIC_');\n"
            "const status = valid ? helper.status(Number(process.env.COUNT)) : 'failed';\n"
            "console.log(JSON.stringify({status}));\n"
            "process.exitCode = 7;\n"
        ).encode()
        repository.bundle = _bundle(
            "node_modules/pinned-helper/index.js",
            b"exports.status = count => count === 2 ? 'already_signed' : 'success';\n",
        )
    repository.dependency_bundle = json.dumps(
        {
            "format": "zip-v1",
            "sha256": hashlib.sha256(repository.bundle).hexdigest(),
            "url": repository.bundle_url,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return repository


def _field(field_id, env_name, value_type, *, secret):
    return {
        "field_id": field_id,
        "label": field_id,
        "env_name": env_name,
        "value_type": value_type,
        "required": True,
        "secret": secret,
        "hint": "fixture",
    }


def _raw_task(repository, *, task_id=None):
    return {
        "task_id": task_id or f"generic_{repository.runtime}",
        "name": f"Generic {repository.runtime}",
        "enabled": True,
        "runtime": repository.runtime,
        "script_url": repository.source_url,
        "source_commit": repository.source_commit,
        "sha256": hashlib.sha256(repository.source).hexdigest(),
        "cron": "10 7 * * *",
        "user_fields": [
            _field("token", "TOKEN", "string", secret=True),
            _field("count", "COUNT", "int", secret=False),
            _field("enabled", "ENABLED", "bool", secret=False),
        ],
        "fixed_env": {"MODE": _FIXED_MODE},
        "input_adapter": "direct_env",
        "result_adapter": "json_protocol",
        "dependency_bundle": repository.dependency_bundle,
        "timeout_seconds": 3,
        "experimental": False,
    }


class _CombinedGitHub:
    def __init__(self, repositories):
        self.payloads = {}
        self.requests = []
        for repository in repositories:
            self.payloads[repository.source_url] = repository.source
            if repository.bundle is not None:
                self.payloads[repository.bundle_url] = repository.bundle

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self), trust_env=False, follow_redirects=False
        )

    async def __call__(self, request):
        self.requests.append(request)
        payload = self.payloads.get(str(request.url))
        if payload is None:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={"content-length": str(len(payload))},
            stream=TrackedStream(payload),
            request=request,
        )


class GenericExecutionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.astrbot = patch_astrbot_modules()
        cls.astrbot.start()
        package = "_generic_execution_tests"
        cls.main = load_module("main", package)
        cls.credentials = load_module("credentials", package)
        cls.permissions = load_module("permissions", package)

    @classmethod
    def tearDownClass(cls):
        cls.astrbot.stop()

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="generic-execution-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.clock = _Clock()
        scheduler = load_module("scheduler", "_generic_execution_tests")
        queue = load_module("execution_queue", "_generic_execution_tests")
        for module in (scheduler, queue):
            patcher = mock.patch.object(module, "_utc_now", self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.repositories = {
            runtime: _locked_repository(runtime) for runtime in ("python", "node")
        }
        free_repository = GenericArtifactRepository("python", dependency=False)
        free_repository.source_commit = "e" * 40
        free_repository.source_url = (
            "https://raw.githubusercontent.com/example/tasks/"
            f"{free_repository.source_commit}/free.py"
        )
        free_repository.source = (
            b"import json, os\n"
            b"valid = os.environ['TOKEN'].startswith(' FAKE_GENERIC_') and os.environ['COUNT'] == '1' and os.environ['ENABLED'] == 'true'\n"
            b"print(json.dumps({'status': 'success' if valid else 'failed'}))\n"
        )
        self.repositories["free"] = free_repository
        self.remote = _CombinedGitHub(self.repositories.values())
        self.remote_client = self.remote.client()
        self.addAsyncCleanup(self.remote_client.aclose)
        self.bay = FakeBay()
        self.addCleanup(self.bay.close)
        environment = mock.patch.dict(
            os.environ,
            {
                "CLOUD_DRIVE_CHECKIN_MASTER_KEY": base64.urlsafe_b64encode(
                    b"g" * 32
                ).decode(),
                "GENERIC_TEST_NEO_KEY": self.bay.token,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.config = AstrBotConfig(
            enabled=True,
            plugin_admin_qqs=[],
            whitelist_qqs=["10001", "10002"],
            tasks=[
                _raw_task(self.repositories["python"]),
                _raw_task(self.repositories["node"]),
                _raw_task(self.repositories["free"], task_id="generic_python_free"),
            ],
            manual_cooldown_seconds=0,
            user_interval={"mode": "none"},
            task_interval={"mode": "none"},
            neo_connection={
                "enabled": True,
                "docker_cleanup_confirmed": True,
                "endpoint": "http://fake.bay.invalid",
                "access_key_env": "GENERIC_TEST_NEO_KEY",
            },
        )
        self.context = AstrBotContext()
        self.context.config["admins_id"] = ["90001"]
        self.context.platforms.append(
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="other-bot", name="aiocqhttp")
            )
        )
        client_type = self.main.NeoClient
        client_patch = mock.patch.object(
            self.main,
            "NeoClient",
            side_effect=lambda connection: client_type(
                connection, transport=httpx.MockTransport(self.bay)
            ),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        directory_patch = mock.patch.object(
            self.main.StarTools, "get_data_dir", return_value=self.base / "cache"
        )
        directory_patch.start()
        self.addCleanup(directory_patch.stop)
        self.plugin = self.main.CloudDriveCheckin(self.context, self.config)
        self.plugin.plugin_id = "test/generic-execution"
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        self.plugin.preparation = self.main.PreparationService(
            self.plugin.state_store,
            self.base / "cache",
            client=self.remote_client,
        )
        self.addAsyncCleanup(self.plugin.preparation.close)
        parsed_tasks = self.main.parse_task_catalog(self.config).tasks
        by_id = {task.task_id: task for task in parsed_tasks}
        self.tasks = {
            "python": by_id["generic_python"],
            "node": by_id["generic_node"],
            "free": by_id["generic_python_free"],
        }
        for task in parsed_tasks:
            await self.plugin.preparation.prepare(task, guard=lambda: None)
        self.backend = self.context.kv_for(self.plugin.plugin_id)

    def snapshot(self):
        return self.plugin.state_store.snapshot()

    def node_runtime_patch(self):
        original_upload = self.plugin.executor.client.upload
        runtime_path = f"{Path(_HOST_NODE).parent}:/usr/local/bin:/usr/bin:/bin"

        async def upload(sandbox_id, path, payload):
            if path == "checkin/bootstrap.py":
                fixed_path = b'"/usr/local/bin:/usr/bin:/bin"'
                self.assertIn(fixed_path, payload)
                payload = payload.replace(fixed_path, json.dumps(runtime_path).encode())
            await original_upload(sandbox_id, path, payload)

        return mock.patch.object(self.plugin.executor.client, "upload", new=upload)

    async def until(self, predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(0.01)

    async def bind(self, runtime, qq, platform, token, count):
        task = self.tasks[runtime]
        identity = self.permissions.Identity(platform, qq)
        await self.credentials.save_binding(
            self.plugin.state_store,
            self.credentials.CredentialCipher.from_environment(),
            identity=identity,
            origin=f"{platform}:FriendMessage:{qq}",
            task=task,
            values={"token": token, "count": count, "enabled": True},
            guard=lambda: None,
        )
        return identity

    async def submit(self, runtime, qq="10001", platform="bot-instance"):
        event = FakeEvent(sender_id=qq, platform_id=platform)
        await self.plugin.checkin(event, "立即", self.tasks[runtime].task_id)
        match = re.search(r"[0-9a-f]{32}$", event.sent[-1].text)
        self.assertIsNotNone(match, event.sent[-1].text)
        return match.group(), event

    async def finished(self, run_id):
        await self.until(
            lambda: self.snapshot()["batches"][run_id]["status"] == "finished"
        )
        await self.until(
            lambda: (
                self.snapshot()["outbox"][run_id]["phase"]
                in {"sent", "failed", "unknown", "suppressed"}
            )
        )
        return self.snapshot()["batches"][run_id]

    async def test_python_bundle_manual_queue_maps_typed_values_and_isolates_owners(
        self,
    ):
        await self.bind("python", "10001", "bot-instance", _TOKEN_ONE, 1)
        await self.bind("python", "10002", "other-bot", _TOKEN_TWO, 2)

        first, first_event = await self.submit("python")
        second, second_event = await self.submit("python", "10002", "other-bot")
        first_result, second_result = await asyncio.gather(
            self.finished(first), self.finished(second)
        )

        self.assertEqual(first_result["result"]["business_status"], "success")
        self.assertEqual(second_result["result"]["business_status"], "already_signed")
        self.assertEqual(self.bay.max_executions, 1)
        self.assertEqual(self.bay.upload_paths.count("checkin/artifact.zip"), 2)
        self.assertEqual(
            self.bay.credential_digests,
            [
                hashlib.sha256(_TOKEN_ONE.encode()).hexdigest(),
                hashlib.sha256(_TOKEN_TWO.encode()).hexdigest(),
            ],
        )
        messages = dict(self.context.sent)
        self.assertIn(first, messages["bot-instance:FriendMessage:10001"])
        self.assertNotIn(second, messages["bot-instance:FriendMessage:10001"])
        self.assertIn(second, messages["other-bot:FriendMessage:10002"])
        evidence = str(self.bay.history) + str(self.snapshot())
        evidence += str(first_event.sent) + str(second_event.sent)
        self.assertNotIn(_TOKEN_ONE, evidence)
        self.assertNotIn(_TOKEN_TWO, evidence)

    async def test_dependency_free_generic_task_executes_as_raw_script(self):
        await self.bind("free", "10001", "bot-instance", _TOKEN_ONE, 1)

        run_id, event = await self.submit("free")
        result = await self.finished(run_id)

        self.assertEqual(result["result"]["business_status"], "success")
        self.assertIn("checkin/script.py", self.bay.upload_paths)
        self.assertNotIn("checkin/artifact.zip", self.bay.upload_paths)
        self.assertNotIn(_TOKEN_ONE, str(self.bay.history) + str(event.sent))

    async def test_failed_queue_commit_prevents_generic_execution(self):
        await self.bind("free", "10001", "bot-instance", _TOKEN_ONE, 1)
        self.backend.fail_next_put()
        event = FakeEvent()

        await self.plugin.checkin(event, "立即", self.tasks["free"].task_id)

        self.assertFalse(self.plugin.state_store.ready)
        self.assertEqual(self.bay.create_calls, 0)
        self.assertEqual(self.bay.execution_calls, 0)
        self.assertNotIn(_TOKEN_ONE, str(event.sent))

    async def test_scheduler_automatically_queues_python_bundle(self):
        await self.bind("python", "10001", "bot-instance", _TOKEN_ONE, 1)

        self.clock.advance(10)
        await self.plugin.scheduler.tick()
        await self.until(lambda: len(self.snapshot()["batches"]) == 1)
        run_id = next(iter(self.snapshot()["batches"]))
        result = await self.finished(run_id)

        self.assertEqual(result["task_id"], self.tasks["python"].task_id)
        self.assertEqual(result["source"], "automatic")
        self.assertEqual(result["result"]["business_status"], "success")
        self.assertEqual(self.bay.execution_calls, 1)
        self.assertEqual(self.snapshot()["outbox"][run_id]["phase"], "suppressed")

    @unittest.skipUnless(_HOST_NODE, "Node runtime is not installed")
    async def test_node_bundle_manual_queue_executes_real_prebuilt_module(self):
        await self.bind("node", "10001", "bot-instance", _TOKEN_ONE, 1)

        with self.node_runtime_patch():
            run_id, event = await self.submit("node")
            result = await self.finished(run_id)

        self.assertEqual(result["result"]["business_status"], "success")
        self.assertEqual(result["result"]["exit_code"], 7)
        self.assertIn("checkin/artifact.zip", self.bay.upload_paths)
        self.assertNotIn(_TOKEN_ONE, str(self.bay.history) + str(event.sent))

    @unittest.skipUnless(_HOST_NODE, "Node runtime is not installed")
    async def test_scheduler_automatically_queues_and_runs_python_and_node_bundles(
        self,
    ):
        await self.bind("python", "10001", "bot-instance", _TOKEN_ONE, 1)
        await self.bind("node", "10002", "other-bot", _TOKEN_TWO, 1)

        self.clock.advance(10)
        with self.node_runtime_patch():
            await self.plugin.scheduler.tick()
            await self.until(lambda: len(self.snapshot()["batches"]) == 2)
            run_ids = list(self.snapshot()["batches"])
            results = await asyncio.gather(
                *(self.finished(run_id) for run_id in run_ids)
            )

        self.assertEqual(
            {result["task_id"] for result in results},
            {self.tasks["python"].task_id, self.tasks["node"].task_id},
        )
        self.assertEqual(
            {result["result"]["business_status"] for result in results}, {"success"}
        )
        self.assertTrue(all(result["source"] == "automatic" for result in results))
        self.assertEqual(self.bay.execution_calls, 2)
        self.assertEqual(self.bay.max_executions, 1)
        self.assertEqual(self.snapshot()["cleanup_jobs"], {})
        self.assertTrue(
            all(
                self.snapshot()["outbox"][run_id]["phase"] == "suppressed"
                for run_id in run_ids
            )
        )
        self.assertEqual(len(self.remote.requests), 5)


if __name__ == "__main__":
    unittest.main()
