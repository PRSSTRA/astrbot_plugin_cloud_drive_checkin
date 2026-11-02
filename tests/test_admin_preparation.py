import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from astrbot_support import (
    AstrBotConfig,
    AstrBotContext,
    FakeEvent,
    patch_astrbot_modules,
)
from state_support import load_module
from test_artifact_service import (
    GenericArtifactRepository,
    MockGitHub,
    MockPyPI,
    SyntheticArtifactRepository,
)


class AdminPreparationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.astrbot_patch = patch_astrbot_modules()
        cls.astrbot_patch.start()
        cls.main = load_module("main", "_admin_preparation_commands")
        cls.preparation_module = load_module(
            "preparation", "_admin_preparation_commands"
        )

    @classmethod
    def tearDownClass(cls):
        cls.astrbot_patch.stop()

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="checkin-admin-preparation-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.directory_patch = mock.patch.object(
            self.main.StarTools, "get_data_dir", return_value=self.data_dir
        )
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.context = AstrBotContext()
        self.config = AstrBotConfig(
            enabled=True, plugin_admin_qqs=["10001"], whitelist_qqs=["10002"]
        )
        self.plugin = self.main.CloudDriveCheckin(self.context, self.config)
        self.plugin.plugin_id = "author/admin-preparation"
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        self.backend = self.context.kv_for(self.plugin.plugin_id)

    async def test_only_ordinary_private_admin_can_start_preparation(self):
        original = self.plugin.state_store.snapshot()
        events = [
            FakeEvent(sender_id="10002"),
            FakeEvent(sender_id="10003"),
            FakeEvent(astrbot_admin=True, platform_name="telegram"),
            FakeEvent(
                astrbot_admin=True,
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": "10001",
                    "group_id": "20001",
                    "sender": {"user_id": "10001"},
                },
            ),
            FakeEvent(
                astrbot_admin=True,
                raw_message={
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "group",
                    "user_id": "10001",
                    "sender": {"user_id": "10001"},
                },
            ),
        ]
        for event in events:
            with self.subTest(
                platform=event.get_platform_name(), raw=event.message_obj.raw_message
            ):
                await self.plugin.checkin(event, "管理", "准备", "tianyi_cloud")
                self.assertFalse(event.llm_enabled)
                self.assertTrue(event.stopped)
                self.assertIsNone(self.plugin.preparation)
                self.assertEqual(self.plugin.state_store.snapshot(), original)
        self.assertEqual(list(self.data_dir.iterdir()), [])

    async def test_invalid_admin_requests_never_prepare_or_echo_arguments(self):
        original = copy.deepcopy(self.config)
        for arguments in (
            ("管理", "准备"),
            ("管理", "准备", "SECRET_UNKNOWN_TASK"),
            ("管理", "SECRET_ACTION", "tianyi_cloud"),
            ("管理", "准备", "tianyi_cloud", "SECRET_EXTRA"),
        ):
            event = FakeEvent()
            await self.plugin.checkin(event, *arguments)
            self.assertIsNone(self.plugin.preparation)
            self.assertNotIn("SECRET", "".join(result.text for result in event.sent))
        self.assertEqual(self.config, original)
        self.assertEqual(self.plugin.state_store.snapshot()["prepared_artifacts"], {})

    async def test_unconfirmed_store_rejects_preparation_before_network_or_cache(self):
        self.backend.fail_next_put()
        with self.assertRaises(self.main.CommitUnconfirmedError):
            await self.plugin.state_store.mutate(
                lambda snapshot: snapshot["users"].update({"x": {}})
            )
        event = FakeEvent()
        await self.plugin.checkin(event, "管理", "准备", "tianyi_cloud")
        self.assertIsNone(self.plugin.preparation)
        self.assertEqual(list(self.data_dir.iterdir()), [])
        self.assertEqual(self.plugin.state_store.snapshot()["prepared_artifacts"], {})

    def network_fixture(self):
        repository = SyntheticArtifactRepository(self.data_dir / "repository")
        self.config["tasks"] = [copy.deepcopy(repository.raw_task)]
        root_patch = mock.patch.object(
            self.preparation_module, "_ROOT", repository.root
        )
        root_patch.start()
        self.addCleanup(root_patch.stop)
        remote = MockPyPI(repository)
        client = remote.client()
        self.addAsyncCleanup(client.aclose)
        self.install_service(client)
        return remote, client

    def install_service(self, client):
        service = self.main.PreparationService(
            self.plugin.state_store, self.data_dir, client=client
        )
        self.plugin.preparation = service
        self.addAsyncCleanup(service.close)
        return service

    def current_task(self):
        return self.main.parse_task_catalog(self.config).tasks[0]

    async def test_disabled_template_prepares_and_survives_reload_without_network(self):
        remote, client = self.network_fixture()
        self.config["enabled"] = False
        original = copy.deepcopy(self.config)
        event = FakeEvent()
        await self.plugin.checkin(event, "管理", "准备", "tianyi_cloud")
        artifact = await self.plugin.preparation.load(self.current_task())
        record = self.plugin.state_store.snapshot()["prepared_artifacts"][
            "tianyi_cloud"
        ]
        self.assertEqual(record["sha256"], artifact.sha256)
        self.assertIn(artifact.sha256, event.sent[-1].text)
        self.assertEqual(self.config, original)
        self.assertEqual(self.plugin.state_store.snapshot()["credentials"], {})
        requests_before = len(remote.requests)
        await self.plugin.terminate()
        self.plugin = self.main.CloudDriveCheckin(self.context, self.config)
        self.plugin.plugin_id = "author/admin-preparation"
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        self.install_service(client)
        remote.error = RuntimeError("network must not be reached on cache hit")
        event = FakeEvent()
        await self.plugin.checkin(event, "管理", "准备", "tianyi_cloud")
        restored = await self.plugin.preparation.load(self.current_task())
        self.assertEqual(restored.script, artifact.script)
        self.assertIn(artifact.sha256, event.sent[-1].text)
        self.assertEqual(len(remote.requests), requests_before)

    async def test_administrator_prepares_generic_bundle_then_cache_is_offline(self):
        repository = GenericArtifactRepository("python")
        task = repository.task()
        self.config["tasks"] = [
            {
                "task_id": task.task_id,
                "name": task.name,
                "enabled": False,
                "runtime": task.runtime,
                "script_url": task.script_url,
                "source_commit": task.source_commit,
                "sha256": task.sha256,
                "cron": task.cron,
                "user_fields": [],
                "fixed_env": dict(task.fixed_env),
                "input_adapter": task.input_adapter,
                "result_adapter": task.result_adapter,
                "dependency_bundle": task.dependency_bundle,
                "timeout_seconds": task.timeout_seconds,
                "experimental": False,
            }
        ]
        remote = MockGitHub(repository)
        client = remote.client()
        self.addAsyncCleanup(client.aclose)
        self.install_service(client)

        event = FakeEvent()
        await self.plugin.checkin(event, "管理", "准备", task.task_id)

        prepared = await self.plugin.preparation.load(self.current_task())
        self.assertEqual(prepared.artifact_format, "bundle")
        self.assertEqual(len(remote.requests), 2)
        self.assertIn(prepared.sha256, event.sent[-1].text)
        requests_before = len(remote.requests)
        await self.plugin.checkin(FakeEvent(), "管理", "准备", task.task_id)
        self.assertEqual(len(remote.requests), requests_before)

    async def change_during_download(self, change):
        remote, _ = self.network_fixture()
        remote.release = asyncio.Event()
        command = asyncio.create_task(
            self.plugin.checkin(FakeEvent(), "管理", "准备", "tianyi_cloud")
        )
        await asyncio.wait_for(remote.started.wait(), 3)
        change()
        remote.release.set()
        await command
        self.assertEqual(len(remote.requests), 1)
        self.assertEqual(self.plugin.state_store.snapshot()["prepared_artifacts"], {})
        self.assertEqual(list(self.data_dir.glob("prepared_artifacts/*.pyz")), [])

    async def test_revocation_during_download_does_not_publish(self):
        await self.change_during_download(
            lambda: self.config["plugin_admin_qqs"].clear()
        )

    async def test_task_change_during_download_does_not_publish(self):
        await self.change_during_download(
            lambda: self.config["tasks"][0].update(name="新版任务")
        )

    async def test_malformed_remote_url_returns_a_safe_failure_reply(self):
        remote, _ = self.network_fixture()
        remote.file_host = "files.pythonhosted.org/\r\nPRIVATE_REMOTE_DETAIL"
        event = FakeEvent()
        await self.plugin.checkin(event, "管理", "准备", "tianyi_cloud")
        self.assertEqual(len(event.sent), 1)
        self.assertNotIn("PRIVATE_REMOTE_DETAIL", event.sent[0].text)
        self.assertEqual(
            [request.url.host for request in remote.requests], ["pypi.org"]
        )
        self.assertEqual(self.plugin.state_store.snapshot()["prepared_artifacts"], {})
        self.assertEqual(list(self.data_dir.glob("prepared_artifacts/*.pyz")), [])

    async def test_failed_publication_retains_artifact_for_exact_retry(self):
        remote, _ = self.network_fixture()
        self.backend.fail_next_put()
        event = FakeEvent()
        await self.plugin.checkin(event, "管理", "准备", "tianyi_cloud")
        self.assertFalse(self.plugin.state_store.ready)
        self.assertTrue(self.plugin.state_store.has_pending)
        self.assertEqual(self.plugin.state_store.snapshot()["prepared_artifacts"], {})
        files = list(self.data_dir.glob("prepared_artifacts/*.pyz"))
        self.assertEqual(len(files), 1)
        payload = files[0].read_bytes()
        requests_before = len(remote.requests)
        await self.plugin.state_store.retry_pending()
        artifact = await self.plugin.preparation.load(self.current_task())
        self.assertEqual(artifact.script, payload)
        self.assertNotIn(artifact.sha256, event.sent[-1].text)
        await self.plugin.checkin(FakeEvent(), "管理", "准备", "tianyi_cloud")
        self.assertEqual(len(remote.requests), requests_before)

    async def test_termination_cancels_preparation_before_writer_handoff(self):
        remote, _ = self.network_fixture()
        remote.release = asyncio.Event()
        command = asyncio.create_task(
            self.plugin.checkin(FakeEvent(), "管理", "准备", "tianyi_cloud")
        )
        await asyncio.wait_for(remote.started.wait(), 3)
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.assertTrue(self.plugin.state_store.closed)
        self.assertEqual(self.plugin.state_store.snapshot()["prepared_artifacts"], {})
        replacement = self.main.CloudDriveCheckin(self.context, self.config)
        replacement.plugin_id = self.plugin.plugin_id
        await replacement.initialize()
        self.addAsyncCleanup(replacement.terminate)
        self.assertEqual(replacement.state_store.snapshot()["prepared_artifacts"], {})

    async def test_client_close_failure_does_not_strand_the_kv_writer(self):
        class BrokenCloseTransport(httpx.AsyncBaseTransport):
            async def aclose(self):
                raise OSError("controlled client close failure")

        service = self.main.PreparationService(self.plugin.state_store, self.data_dir)
        client = httpx.AsyncClient(transport=BrokenCloseTransport(), trust_env=False)
        service._client = client
        self.plugin.preparation = service
        try:
            with self.assertRaises(self.main.PreparationError):
                await self.plugin.terminate()
            self.assertTrue(self.plugin.state_store.closed)
            replacement = self.main.CloudDriveCheckin(self.context, self.config)
            replacement.plugin_id = self.plugin.plugin_id
            await replacement.initialize()
            self.addAsyncCleanup(replacement.terminate)
            self.assertEqual(
                replacement.state_store.snapshot()["prepared_artifacts"], {}
            )
        finally:
            self.plugin.preparation = None


if __name__ == "__main__":
    unittest.main()
