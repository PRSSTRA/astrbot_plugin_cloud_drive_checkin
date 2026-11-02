import asyncio
import copy
import dataclasses
import gzip
import hashlib
import io
import json
import stat
import tempfile
import threading
import unittest
import zipfile
from contextlib import suppress
from pathlib import Path
from unittest import mock

import httpx
from state_support import load_module

artifact_service = load_module("artifact_service", "_artifact_service_tests")
preparation = load_module("preparation", "_artifact_service_tests")
task_config = load_module("task_config", "_artifact_service_tests")

_DISTRIBUTIONS = (
    "certifi",
    "charset-normalizer",
    "idna",
    "pyasn1",
    "requests",
    "rsa",
    "urllib3",
)
_VERSION = "1.0"
_USER_FIELDS = (
    {
        "field_id": "username",
        "label": "用户名",
        "env_name": "TY_USERNAME",
        "value_type": "string",
        "required": True,
        "secret": True,
        "hint": "天翼云盘登录用户名",
    },
    {
        "field_id": "password",
        "label": "密码",
        "env_name": "TY_PASSWORD",
        "value_type": "string",
        "required": True,
        "secret": True,
        "hint": "天翼云盘登录密码",
    },
)


def _zip_member(name, data):
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 2, 3, 4, 6))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info, data


def _wheel_bytes(distribution):
    token = distribution.replace("-", "_")
    dist_info = f"{token}-{_VERSION}.dist-info"
    members = (
        _zip_member(f"{token}/__init__.py", b"VALUE = 1\n"),
        _zip_member(
            f"{dist_info}/METADATA",
            (
                f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {_VERSION}\n\n"
            ).encode("ascii"),
        ),
        _zip_member(
            f"{dist_info}/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        ),
        _zip_member(f"{dist_info}/licenses/LICENSE", b"fixture license\n"),
        _zip_member(f"{dist_info}/RECORD", b"fixture record\n"),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as wheel:
        for info, data in members:
            wheel.writestr(info, data)
    return output.getvalue()


class SyntheticArtifactRepository:
    """Complete fixed Tianyi repository inputs reusable by command tests."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "third_party/tianyi").mkdir(parents=True)
        (self.root / "runners").mkdir()
        self.source = b"def main_handler(event, context):\n    return {}\n"
        self.source_sha256 = hashlib.sha256(self.source).hexdigest()
        self.source_commit = "a" * 40
        self.script_url = (
            "https://raw.githubusercontent.com/example/tianyi/"
            f"{self.source_commit}/index.py"
        )
        (self.root / "third_party/tianyi/index.py").write_bytes(self.source)
        (self.root / "third_party/tianyi/LICENSE").write_bytes(b"vendor license\n")
        (self.root / "runners/tianyi_entry.py").write_bytes(
            b'import json\nprint(json.dumps({"status":"unknown"}))\n'
        )
        (self.root / "LICENSE").write_bytes(b"project license\n")

        self.wheels = {
            distribution: _wheel_bytes(distribution) for distribution in _DISTRIBUTIONS
        }
        lock_lines = ["# synthetic locked wheels"]
        for distribution, payload in self.wheels.items():
            lock_lines.append(
                f"{distribution}=={_VERSION} --hash=sha256:"
                f"{hashlib.sha256(payload).hexdigest()}"
            )
        (self.root / "third_party/tianyi/requirements.lock").write_text(
            "\n".join(lock_lines) + "\n", encoding="ascii"
        )
        task = {
            "task_id": "tianyi_cloud",
            "name": "天翼云盘",
            "enabled": False,
            "runtime": "python",
            "script_url": self.script_url,
            "source_commit": self.source_commit,
            "sha256": self.source_sha256,
            "cron": "10 7 * * *",
            "user_fields": json.dumps(
                _USER_FIELDS, ensure_ascii=False, separators=(",", ":")
            ),
            "fixed_env": {},
            "input_adapter": "tianyi_account",
            "result_adapter": "tianyi",
            "dependency_bundle": "",
            "timeout_seconds": 180,
            "experimental": False,
        }
        self.raw_task = copy.deepcopy(task)
        (self.root / "_conf_schema.json").write_text(
            json.dumps(
                {"tasks": {"default": [task]}},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def task(self, *, task_id="tianyi_cloud", version="1"):
        return task_config.TaskConfig(
            task_id=task_id,
            name="天翼云盘",
            enabled=False,
            runtime="python",
            script_url=self.script_url,
            source_commit=self.source_commit,
            sha256=self.source_sha256,
            cron="10 7 * * *",
            user_fields=tuple(task_config.UserField(**field) for field in _USER_FIELDS),
            fixed_env=(),
            input_adapter="tianyi_account",
            result_adapter="tianyi",
            dependency_bundle="",
            timeout_seconds=180,
            experimental=False,
            version=version,
        )


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload
        self.read = False
        self.closed = False
        self.yielded_bytes = 0

    async def __aiter__(self):
        self.read = True
        for start in range(0, len(self.payload), 64 * 1024):
            chunk = self.payload[start : start + 64 * 1024]
            self.yielded_bytes += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


class MockPyPI:
    """Strict two-host MockTransport serving one SyntheticArtifactRepository."""

    def __init__(self, repository):
        self.repository = repository
        self.requests = []
        self.metadata_status = 200
        self.file_status = 200
        self.file_host = "files.pythonhosted.org"
        self.file_query = ""
        self.corrupt_distribution = None
        self.error = None
        self.metadata_padding = 0
        self.started = asyncio.Event()
        self.release = None

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self), trust_env=False, follow_redirects=False
        )

    async def __call__(self, request):
        self.requests.append(request)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error

        if request.url.host == "pypi.org":
            if self.metadata_status != 200:
                return httpx.Response(self.metadata_status, request=request)
            parts = request.url.path.strip("/").split("/")
            if len(parts) != 4 or parts[0] != "pypi" or parts[3] != "json":
                return httpx.Response(404, request=request)
            distribution, version = parts[1], parts[2]
            payload = self.repository.wheels.get(distribution)
            if payload is None or version != _VERSION:
                return httpx.Response(404, request=request)
            token = distribution.replace("-", "_")
            filename = f"{token}-{_VERSION}-py3-none-any.whl"
            wheel_url = f"https://{self.file_host}/packages/{filename}{self.file_query}"
            metadata = {
                "info": {"name": distribution, "version": _VERSION},
                "urls": [
                    {
                        "filename": filename,
                        "packagetype": "bdist_wheel",
                        "digests": {"sha256": hashlib.sha256(payload).hexdigest()},
                        "url": wheel_url,
                    }
                ],
            }
            if self.metadata_padding:
                metadata["padding"] = "x" * self.metadata_padding
            body = json.dumps(metadata).encode()
            return httpx.Response(
                200,
                headers={"content-length": str(len(body))},
                stream=TrackedStream(body),
                request=request,
            )

        if request.url.host == "files.pythonhosted.org":
            if self.file_status != 200:
                return httpx.Response(self.file_status, request=request)
            filename = request.url.path.rsplit("/", 1)[-1]
            distribution = filename.split(f"-{_VERSION}-", 1)[0].replace("_", "-")
            payload = self.repository.wheels.get(distribution)
            if payload is None:
                return httpx.Response(404, request=request)
            if distribution == self.corrupt_distribution:
                payload += b"corrupt"
            return httpx.Response(
                200,
                headers={"content-length": str(len(payload))},
                stream=TrackedStream(payload),
                request=request,
            )

        return httpx.Response(599, request=request)


class ControlledStore:
    """Whole-snapshot store double with a retryable unconfirmed write."""

    def __init__(self):
        self.confirmed = {"revision": 0, "prepared_artifacts": {}}
        self.pending = None
        self.fail_next = None
        self.in_mutation = False

    @property
    def ready(self):
        return self.pending is None

    @property
    def has_pending(self):
        return self.pending is not None

    def snapshot(self):
        return copy.deepcopy(self.confirmed)

    async def mutate(self, callback):
        if self.pending is not None:
            raise artifact_service.CommitUnconfirmedError("a state commit is pending")
        candidate = copy.deepcopy(self.confirmed)
        self.in_mutation = True
        try:
            callback(candidate)
        finally:
            self.in_mutation = False
        candidate["revision"] += 1
        if self.fail_next is not None:
            self.pending = candidate
            self.fail_next = None
            raise artifact_service.CommitUnconfirmedError(
                "state commit was not confirmed"
            ) from None
        self.confirmed = candidate
        return copy.deepcopy(candidate)

    async def retry_pending(self):
        if self.pending is not None:
            self.confirmed = self.pending
            self.pending = None
        return copy.deepcopy(self.confirmed)


class ArtifactServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repository = SyntheticArtifactRepository(self.base / "repository")
        self.root_patch = mock.patch.object(preparation, "_ROOT", self.repository.root)
        self.root_patch.start()
        self.store = ControlledStore()
        self.pypi = MockPyPI(self.repository)
        self.clients = []
        self.services = []

    async def asyncTearDown(self):
        for service in reversed(self.services):
            with suppress(Exception, asyncio.CancelledError):
                await service.close()
        for client in reversed(self.clients):
            await client.aclose()
        self.root_patch.stop()
        self.temporary.cleanup()

    def service(self, *, pypi=None, store=None):
        pypi = self.pypi if pypi is None else pypi
        client = pypi.client()
        self.clients.append(client)
        service = artifact_service.PreparationService(
            self.store if store is None else store,
            self.base / "data",
            client=client,
        )
        self.services.append(service)
        return service

    async def test_prepare_publishes_exact_metadata_and_cache_hit_loads_offline(self):
        service = self.service()
        task = self.repository.task(task_id="custom_tianyi")

        prepared = await service.prepare(task, guard=lambda: None)

        self.assertEqual(len(self.pypi.requests), len(_DISTRIBUTIONS) * 2)
        self.assertEqual(
            {request.url.host for request in self.pypi.requests},
            {"pypi.org", "files.pythonhosted.org"},
        )
        self.assertTrue(
            all(
                request.url.scheme == "https"
                and request.url.port is None
                and not request.url.query
                and not request.url.fragment
                for request in self.pypi.requests
            )
        )
        self.assertEqual(hashlib.sha256(prepared.script).hexdigest(), prepared.sha256)
        record = self.store.snapshot()["prepared_artifacts"][task.task_id]
        self.assertEqual(
            set(record),
            {
                "task_version",
                "source_commit",
                "source_sha256",
                "sha256",
                "size",
                "inputs_sha256",
            },
        )
        self.assertEqual(record["task_version"], task.version)
        cache_file = self.base / "data/prepared_artifacts" / f"{prepared.sha256}.pyz"
        self.assertEqual(cache_file.read_bytes(), prepared.script)

        request_count = len(self.pypi.requests)
        self.assertEqual(await service.prepare(task, guard=lambda: None), prepared)
        self.assertEqual(await service.load(task), prepared)
        self.assertEqual(len(self.pypi.requests), request_count)

        await service.close()
        self.assertFalse(self.clients[0].is_closed)

    async def test_only_fixed_execution_declaration_is_accepted(self):
        original = self.repository.task()
        changed_field = dataclasses.replace(
            original.user_fields[0], env_name="OTHER_USERNAME"
        )
        cases = (
            dataclasses.replace(original, runtime="node"),
            dataclasses.replace(original, script_url="https://example.com/index.py"),
            dataclasses.replace(original, source_commit="b" * 40),
            dataclasses.replace(original, sha256="b" * 64),
            dataclasses.replace(original, input_adapter="direct_env"),
            dataclasses.replace(original, result_adapter="process_only"),
            dataclasses.replace(original, dependency_bundle="requests"),
            dataclasses.replace(original, fixed_env=(("EXTRA", "1"),)),
            dataclasses.replace(
                original,
                user_fields=(changed_field, *original.user_fields[1:]),
            ),
        )
        service = self.service()
        for task in cases:
            with (
                self.subTest(task=task),
                self.assertRaises(artifact_service.PreparationError),
            ):
                await service.prepare(task, guard=lambda: None)
        self.assertEqual(self.pypi.requests, [])
        self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})

    async def test_wheel_hash_redirect_and_source_are_rejected_without_detail_leak(
        self,
    ):
        scenarios = ("hash", "redirect", "source", "query", "transport")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                pypi = MockPyPI(self.repository)
                if scenario == "hash":
                    pypi.corrupt_distribution = "requests"
                elif scenario == "redirect":
                    pypi.metadata_status = 302
                elif scenario == "source":
                    pypi.file_host = "127.0.0.1"
                elif scenario == "query":
                    pypi.file_query = "?token=secret"
                else:
                    pypi.error = RuntimeError("remote secret detail")
                store = ControlledStore()
                service = self.service(pypi=pypi, store=store)

                with self.assertRaises(artifact_service.PreparationError) as raised:
                    await service.prepare(self.repository.task(), guard=lambda: None)

                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(store.snapshot()["prepared_artifacts"], {})

    async def test_private_dns_result_is_rejected(self):
        private_result = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with (
            mock.patch.object(
                artifact_service.socket, "getaddrinfo", return_value=private_result
            ),
            self.assertRaises(artifact_service.PreparationError),
        ):
            await artifact_service._resolve_public_addresses("pypi.org", 443)
        self.assertEqual(self.pypi.requests, [])

    async def test_network_backend_connects_to_verified_numeric_address(self):
        calls = []
        stream = object()

        class Delegate:
            async def connect_tcp(self, **kwargs):
                calls.append(kwargs)
                return stream

            async def sleep(self, _seconds):
                return None

        backend = artifact_service._PublicIPNetworkBackend(Delegate())
        public_result = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(
            artifact_service.socket, "getaddrinfo", return_value=public_result
        ):
            connected = await backend.connect_tcp("pypi.org", 443, timeout=1.0)

        self.assertIs(connected, stream)
        self.assertEqual(calls[0]["host"], "93.184.216.34")
        self.assertNotEqual(calls[0]["host"], "pypi.org")

        private_result = [(2, 1, 6, "", ("10.0.0.1", 443))]
        with (
            mock.patch.object(
                artifact_service.socket, "getaddrinfo", return_value=private_result
            ),
            self.assertRaises(artifact_service.PreparationError),
        ):
            await backend.connect_tcp("files.pythonhosted.org", 443)
        self.assertEqual(len(calls), 1)

    async def test_owned_transport_initialization_error_is_sanitized(self):
        service = artifact_service.PreparationService(self.store, self.base / "data")
        self.services.append(service)
        with (
            mock.patch.object(
                artifact_service,
                "_public_ip_transport",
                side_effect=OSError("secret TLS detail"),
            ),
            self.assertRaises(artifact_service.PreparationError) as raised,
        ):
            await service.prepare(self.repository.task(), guard=lambda: None)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})

    async def test_metadata_over_one_mebibyte_is_rejected(self):
        self.pypi.metadata_padding = 1024 * 1024
        service = self.service()

        with self.assertRaises(artifact_service.PreparationError):
            await service.prepare(self.repository.task(), guard=lambda: None)

        self.assertEqual(len(self.pypi.requests), 1)
        self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})

    async def test_encoded_responses_are_rejected_before_consuming_the_body(self):
        compressed = gzip.compress(b"x" * (4 * 1024 * 1024))
        for target_host in ("pypi.org", "files.pythonhosted.org"):
            with self.subTest(host=target_host):
                stream = TrackedStream(compressed)

                async def handler(request, host=target_host, body=stream):
                    self.assertEqual(request.headers.get("accept-encoding"), "identity")
                    if request.url.host == host:
                        return httpx.Response(
                            200,
                            headers={
                                "content-encoding": "gzip",
                                "content-length": str(len(compressed)),
                            },
                            stream=body,
                            request=request,
                        )
                    return await self.pypi(request)

                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), trust_env=False
                )
                self.clients.append(client)
                service = artifact_service.PreparationService(
                    self.store, self.base / "data", client=client
                )
                self.services.append(service)
                with self.assertRaises(artifact_service.PreparationError):
                    await service.prepare(self.repository.task(), guard=lambda: None)
                self.assertFalse(stream.read)
                self.assertTrue(stream.closed)
                self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})

    async def test_malformed_wheel_urls_are_rejected_without_a_file_request(self):
        for marker in ("\r\n\t", "\x01\x7f", "x" * 70000, "\ud800"):
            with self.subTest(marker_length=len(marker)):
                remote = MockPyPI(self.repository)
                remote.file_host = "files.pythonhosted.org/" + marker + "/boundary"
                service = self.service(pypi=remote)
                with self.assertRaises(artifact_service.PreparationError):
                    await service.prepare(self.repository.task(), guard=lambda: None)
                self.assertEqual(
                    [request.url.host for request in remote.requests], ["pypi.org"]
                )
                self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})

    async def test_raw_chunk_limits_apply_without_content_length(self):
        for host, limit in (
            ("pypi.org", 1024 * 1024),
            ("files.pythonhosted.org", 2 * 1024 * 1024),
        ):
            with self.subTest(host=host):
                stream = TrackedStream(b"x" * (limit + 128 * 1024))

                async def handler(request, target=host, body=stream):
                    if request.url.host == target:
                        return httpx.Response(200, stream=body, request=request)
                    return await self.pypi(request)

                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), trust_env=False
                )
                self.clients.append(client)
                service = artifact_service.PreparationService(
                    self.store, self.base / "data", client=client
                )
                self.services.append(service)
                with self.assertRaises(artifact_service.PreparationError):
                    await service.prepare(self.repository.task(), guard=lambda: None)
                self.assertEqual(stream.yielded_bytes, limit + 64 * 1024)
                self.assertTrue(stream.closed)
                self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})

    async def test_prospective_quota_rejects_before_file_or_kv_publication(self):
        records = self.store.confirmed["prepared_artifacts"]
        for index in range(1, 33):
            records[f"existing_{index}"] = {
                "task_version": "old",
                "source_commit": "a" * 40,
                "source_sha256": "b" * 64,
                "sha256": f"{index:064x}",
                "size": 1,
                "inputs_sha256": "c" * 64,
            }
        original = copy.deepcopy(self.store.confirmed)
        service = self.service()

        with self.assertRaises(artifact_service.PreparationError):
            await service.prepare(
                self.repository.task(task_id="new_tianyi"), guard=lambda: None
            )

        self.assertEqual(self.store.confirmed, original)
        self.assertFalse((self.base / "data/prepared_artifacts").exists())

    async def test_unconfirmed_metadata_keeps_file_for_exact_retry(self):
        service = self.service()
        task = self.repository.task()
        self.store.fail_next = OSError("secret durable writer detail")

        with self.assertRaises(artifact_service.CommitUnconfirmedError) as raised:
            await service.prepare(task, guard=lambda: None)

        self.assertNotIn("secret", str(raised.exception))
        self.assertTrue(self.store.has_pending)
        pending_record = self.store.pending["prepared_artifacts"][task.task_id]
        cache_file = (
            self.base / "data/prepared_artifacts" / f"{pending_record['sha256']}.pyz"
        )
        self.assertTrue(cache_file.is_file())
        with self.assertRaises(artifact_service.CommitUnconfirmedError):
            await service.load(task)

        await self.store.retry_pending()
        requests = len(self.pypi.requests)
        loaded = await service.load(task)
        self.assertEqual(loaded.sha256, pending_record["sha256"])
        self.assertEqual(len(self.pypi.requests), requests)

    async def test_restart_load_rejects_tamper_and_prepare_repairs_it(self):
        task = self.repository.task()
        first = self.service()
        prepared = await first.prepare(task, guard=lambda: None)
        await first.close()

        second_pypi = MockPyPI(self.repository)
        second = self.service(pypi=second_pypi)
        self.assertEqual(await second.load(task), prepared)
        cache_file = self.base / "data/prepared_artifacts" / f"{prepared.sha256}.pyz"
        cache_file.write_bytes(b"tampered")

        with self.assertRaises(artifact_service.PreparationError):
            await second.load(task)
        repaired = await second.prepare(task, guard=lambda: None)
        self.assertEqual(repaired.sha256, prepared.sha256)
        self.assertEqual(cache_file.read_bytes(), prepared.script)
        self.assertEqual(len(second_pypi.requests), len(_DISTRIBUTIONS) * 2)

    async def test_task_version_change_invalidates_load_and_republishes_latest(self):
        service = self.service()
        original = self.repository.task(version="one")
        await service.prepare(original, guard=lambda: None)
        changed = dataclasses.replace(
            original, name="重命名天翼", cron="20 8 * * *", version="two"
        )

        with self.assertRaises(artifact_service.PreparationError):
            await service.load(changed)
        prior_requests = len(self.pypi.requests)
        await service.prepare(changed, guard=lambda: None)

        self.assertEqual(len(self.pypi.requests), prior_requests + 14)
        records = self.store.snapshot()["prepared_artifacts"]
        self.assertEqual(set(records), {original.task_id})
        self.assertEqual(records[original.task_id]["task_version"], "two")

    async def test_input_change_rebuild_keeps_pending_file_then_reclaims_old_file(self):
        service = self.service()
        task = self.repository.task()
        original = await service.prepare(task, guard=lambda: None)
        entry = self.repository.root / "runners/tianyi_entry.py"
        entry.write_bytes(b'import json\nprint(json.dumps({"status":"failure"}))\n')
        self.store.fail_next = OSError("write failed")

        with self.assertRaises(artifact_service.CommitUnconfirmedError):
            await service.prepare(task, guard=lambda: None)

        pending = self.store.pending["prepared_artifacts"][task.task_id]
        self.assertNotEqual(pending["inputs_sha256"], original.inputs_sha256)
        self.assertNotEqual(pending["sha256"], original.sha256)
        cache = self.base / "data/prepared_artifacts"
        self.assertEqual(len(list(cache.glob("*.pyz"))), 2)

        await self.store.retry_pending()
        await service.prepare(task, guard=lambda: None)
        self.assertEqual(
            {path.stem for path in cache.glob("*.pyz")}, {pending["sha256"]}
        )

    async def test_concurrent_prepare_is_busy_and_close_cancels_owned_request(self):
        self.pypi.release = asyncio.Event()
        service = self.service()
        task = self.repository.task()
        preparing = asyncio.create_task(service.prepare(task, guard=lambda: None))
        await asyncio.wait_for(self.pypi.started.wait(), 1)

        with self.assertRaises(artifact_service.PreparationBusy):
            await service.prepare(task, guard=lambda: None)

        closing = asyncio.create_task(service.close())
        await asyncio.wait_for(closing, 1)
        with self.assertRaises(asyncio.CancelledError):
            await preparing
        with self.assertRaises(artifact_service.PreparationError):
            await service.prepare(task, guard=lambda: None)
        self.assertFalse(self.clients[0].is_closed)

    async def test_cancellation_waits_for_builder_thread_before_temp_cleanup(self):
        service = self.service()
        task = self.repository.task()
        started = asyncio.Event()
        release = threading.Event()
        wheelhouse_seen = []
        loop = asyncio.get_running_loop()
        original_build = artifact_service.build_tianyi

        def blocking_build(wheelhouse):
            wheelhouse_seen.append(wheelhouse)
            loop.call_soon_threadsafe(started.set)
            release.wait()
            return original_build(wheelhouse)

        with mock.patch.object(artifact_service, "build_tianyi", blocking_build):
            preparing = asyncio.create_task(service.prepare(task, guard=lambda: None))
            await asyncio.wait_for(started.wait(), 2)
            preparing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(preparing.done())
            self.assertTrue(wheelhouse_seen[0].is_dir())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await preparing
        self.assertFalse(wheelhouse_seen[0].exists())

    async def test_guard_is_rechecked_inside_state_mutation(self):
        service = self.service()

        class Revoked(RuntimeError):
            pass

        def guard():
            if self.store.in_mutation:
                raise Revoked("generation changed")

        with self.assertRaises(Revoked):
            await service.prepare(self.repository.task(), guard=guard)
        self.assertEqual(self.store.snapshot()["prepared_artifacts"], {})
        self.assertEqual(
            len(list((self.base / "data/prepared_artifacts").glob("*.pyz"))), 1
        )


if __name__ == "__main__":
    unittest.main()
