import json
import os
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import httpx

from executors.client import (
    NeoClient,
    NeoConfigError,
    NeoConnection,
    NeoError,
    resolve_connection,
)


class _UnreadableMapping(Mapping):
    def __getitem__(self, key):
        raise AssertionError("disabled configuration must not be read")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class NeoConnectionTests(unittest.TestCase):
    def test_connection_is_frozen_canonical_and_secret_safe(self):
        connection = NeoConnection(
            "HTTPS://Example.COM:443/bay/", "api-key-secret", "python-default", 900
        )

        self.assertEqual(connection.endpoint, "https://example.com/bay")
        self.assertNotIn("api-key-secret", repr(connection))
        self.assertRegex(connection.fingerprint, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(connection.fingerprint, connection.fingerprint)
        with self.assertRaises(FrozenInstanceError):
            connection.ttl = 901

    def test_fingerprint_changes_for_every_connection_identity_field(self):
        baseline = NeoConnection("https://neo.example", "key-a", "profile-a")
        alternatives = (
            NeoConnection("https://other.example", "key-a", "profile-a"),
            NeoConnection("https://neo.example", "key-b", "profile-a"),
            NeoConnection("https://neo.example", "key-a", "profile-b"),
        )

        for alternative in alternatives:
            with self.subTest(alternative=alternative.endpoint):
                self.assertNotEqual(baseline.fingerprint, alternative.fingerprint)

    def test_invalid_connection_values_raise_only_static_errors(self):
        secret = "credential-that-must-not-escape"
        invalid = (
            {"endpoint": f"https://user:{secret}@neo.example", "api_key": "key"},
            {"endpoint": f"https://neo.example/?token={secret}", "api_key": "key"},
            {"endpoint": "file:///tmp/neo", "api_key": "key"},
            {"endpoint": "https://neo.example", "api_key": ""},
            {"endpoint": "https://neo.example", "api_key": secret, "profile": ""},
            {"endpoint": "https://neo.example", "api_key": secret, "ttl": 299},
            {"endpoint": "https://neo.example", "api_key": secret, "ttl": True},
        )

        for values in invalid:
            with self.subTest(values=tuple(values)):
                with self.assertRaises(NeoConfigError) as raised:
                    NeoConnection(**values)
                self.assertNotIn(secret, str(raised.exception))


class ResolveConnectionTests(unittest.TestCase):
    def test_disabled_configuration_reads_no_framework_or_environment_token(self):
        with patch.dict(
            os.environ,
            {"CLOUD_DRIVE_CHECKIN_NEO_KEY": "environment-secret"},
            clear=False,
        ):
            resolved = resolve_connection(
                {"neo_connection": {"enabled": False}},
                _UnreadableMapping(),
            )

        self.assertIsNone(resolved)

    def test_complete_framework_connection_wins_as_one_fixed_source(self):
        plugin = {
            "neo_connection": {
                "enabled": True,
                "docker_cleanup_confirmed": True,
                "endpoint": "https://plugin.example",
                "profile": "plugin-profile",
                "access_key_env": "PLUGIN_NEO_KEY",
                "ttl": 1200,
            }
        }
        framework = {
            "provider_settings": {
                "sandbox": {
                    "booter": "shipyard_neo",
                    "shipyard_neo_endpoint": "https://framework.example/",
                    "shipyard_neo_access_token": "framework-key",
                    "shipyard_neo_profile": "framework-profile",
                }
            }
        }

        with patch.dict(os.environ, {"PLUGIN_NEO_KEY": "plugin-key"}, clear=False):
            resolved = resolve_connection(plugin, framework)

        self.assertEqual(
            resolved,
            NeoConnection(
                "https://framework.example",
                "framework-key",
                "framework-profile",
                1200,
            ),
        )

    def test_incomplete_framework_connection_falls_back_entirely_to_plugin(self):
        plugin = {
            "neo_connection": {
                "enabled": True,
                "docker_cleanup_confirmed": True,
                "endpoint": "https://plugin.example",
                "profile": "plugin-profile",
                "access_key_env": "PLUGIN_NEO_KEY",
            }
        }
        framework = {
            "provider_settings": {
                "sandbox": {
                    "booter": "shipyard_neo",
                    "shipyard_neo_endpoint": "https://framework.example",
                    "shipyard_neo_access_token": "",
                    "shipyard_neo_profile": "framework-profile",
                }
            }
        }

        with patch.dict(os.environ, {"PLUGIN_NEO_KEY": "plugin-key"}, clear=False):
            resolved = resolve_connection(plugin, framework)

        self.assertEqual(resolved.endpoint, "https://plugin.example")
        self.assertEqual(resolved.api_key, "plugin-key")
        self.assertEqual(resolved.profile, "plugin-profile")

    def test_framework_endpoint_is_never_combined_with_plugin_token(self):
        plugin = {
            "neo_connection": {
                "enabled": True,
                "docker_cleanup_confirmed": True,
                "endpoint": "",
                "access_key_env": "PLUGIN_NEO_KEY",
            }
        }
        framework = {
            "provider_settings": {
                "sandbox": {
                    "booter": "shipyard_neo",
                    "shipyard_neo_endpoint": "https://framework.example",
                    "shipyard_neo_access_token": "",
                }
            }
        }

        with (
            patch.dict(os.environ, {"PLUGIN_NEO_KEY": "plugin-key"}, clear=False),
            self.assertRaises(NeoConfigError) as raised,
        ):
            resolve_connection(plugin, framework)

        self.assertNotIn("plugin-key", str(raised.exception))
        self.assertNotIn("framework.example", str(raised.exception))

    def test_auto_framework_endpoint_does_not_capture_framework_token(self):
        plugin = {
            "neo_connection": {
                "enabled": True,
                "docker_cleanup_confirmed": True,
                "endpoint": "https://plugin.example",
                "access_key_env": "PLUGIN_NEO_KEY",
            }
        }
        framework = {
            "provider_settings": {
                "sandbox": {
                    "booter": "shipyard_neo",
                    "shipyard_neo_endpoint": "__auto__",
                    "shipyard_neo_access_token": "framework-key",
                }
            }
        }

        with patch.dict(os.environ, {"PLUGIN_NEO_KEY": "plugin-key"}, clear=False):
            resolved = resolve_connection(plugin, framework)

        self.assertEqual(resolved.endpoint, "https://plugin.example")
        self.assertEqual(resolved.api_key, "plugin-key")

    def test_cleanup_confirmation_is_required_before_token_resolution(self):
        config = {
            "neo_connection": {
                "enabled": True,
                "docker_cleanup_confirmed": False,
                "endpoint": "https://plugin.example",
            }
        }

        with self.assertRaises(NeoConfigError):
            resolve_connection(config, _UnreadableMapping())


class NeoClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = NeoConnection(
            "https://neo.example/api", "neo-api-secret", "python-default", 900
        )

    def make_client(self, handler):
        return NeoClient(self.connection, transport=httpx.MockTransport(handler))

    @staticmethod
    def profile_response():
        return {
            "items": [
                {
                    "id": "python-default",
                    "capabilities": ["python", "shell", "filesystem"],
                }
            ]
        }

    async def test_access_check_proves_anonymous_rejection_before_authenticated_access(
        self,
    ):
        requests = []

        def handler(request):
            requests.append(request)
            # The actual Bay profile route is public, even with anonymous mode off.
            if request.url.path == "/api/v1/profiles":
                return httpx.Response(200, json=self.profile_response())
            if "Authorization" not in request.headers:
                return httpx.Response(401, json={"error": {"code": "unauthorized"}})
            self.assertEqual(request.headers["Authorization"], "Bearer neo-api-secret")
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = self.make_client(handler)
        try:
            await client.check_access()
        finally:
            await client.close()

        self.assertEqual(len(requests), 3)
        self.assertEqual(
            requests[0].url.path, "/api/v1/sandboxes/cloud-drive-checkin-auth-probe"
        )
        self.assertNotIn("Authorization", requests[0].headers)

    async def test_access_check_rejects_anonymous_success_or_forbidden_without_retry(
        self,
    ):
        for status in (200, 403, 500):
            with self.subTest(status=status):
                calls = 0

                def handler(request, status=status):
                    nonlocal calls
                    calls += 1
                    return httpx.Response(status, json=self.profile_response())

                client = self.make_client(handler)
                try:
                    with self.assertRaises(NeoError) as raised:
                        await client.check_access()
                finally:
                    await client.close()

                self.assertEqual(raised.exception.code, "access_rejected")
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(calls, 1)

    async def test_access_check_rejects_bad_key_and_missing_capability(self):
        responses = iter(
            (
                httpx.Response(401),
                httpx.Response(401, json={"error": {"code": "unauthorized"}}),
            )
        )
        client = self.make_client(lambda _request: next(responses))
        try:
            with self.assertRaises(NeoError) as raised:
                await client.check_access()
        finally:
            await client.close()
        self.assertEqual(raised.exception.code, "access_rejected")

        responses = iter(
            (
                httpx.Response(401),
                httpx.Response(404, json={"error": {"code": "not_found"}}),
                httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "python-default",
                                "capabilities": ["shell"],
                            }
                        ]
                    },
                ),
            )
        )
        client = self.make_client(lambda _request: next(responses))
        try:
            with self.assertRaises(NeoError) as raised:
                await client.check_access()
        finally:
            await client.close()
        self.assertEqual(raised.exception.code, "access_rejected")

    async def test_create_uses_exact_protocol_and_keeps_ids_without_optional_metadata(
        self,
    ):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(201, json={"id": "sbx_1", "cargo_id": "crg_1"})

        client = self.make_client(handler)
        try:
            created = await client.create_sandbox("run_1")
        finally:
            await client.close()

        self.assertEqual(created, {"id": "sbx_1", "cargo_id": "crg_1"})
        request = captured[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/api/v1/sandboxes")
        self.assertEqual(request.headers["Idempotency-Key"], "run_1")
        self.assertEqual(
            json.loads(request.content),
            {"profile": "python-default", "cargo_id": None, "ttl": 900},
        )

    async def test_typed_404_is_absence_but_proxy_404_is_an_error(self):
        typed_client = self.make_client(
            lambda _request: httpx.Response(
                404,
                json={
                    "error": {
                        "code": "not_found",
                        "message": "resource does not exist",
                    }
                },
            )
        )
        try:
            self.assertIsNone(await typed_client.get_cargo("crg_1"))
            self.assertIsNone(await typed_client.get_sandbox("sbx_1"))
            await typed_client.delete_cargo("crg_1")
            await typed_client.delete_sandbox("sbx_1")
        finally:
            await typed_client.close()

        for body, content_type in (
            (b"<html>proxy not found</html>", "text/html"),
            (b'{"error":{"code":"upstream_not_found"}}', "application/json"),
            (b'{"code":"not_found"}', "application/json"),
        ):
            with self.subTest(body=body):
                proxy_client = self.make_client(
                    lambda _request, body=body, content_type=content_type: (
                        httpx.Response(
                            404, content=body, headers={"Content-Type": content_type}
                        )
                    )
                )
                try:
                    with self.assertRaises(NeoError) as raised:
                        await proxy_client.get_cargo("crg_1")
                finally:
                    await proxy_client.close()
                self.assertEqual(raised.exception.code, "unexpected_status")
                self.assertEqual(raised.exception.status, 404)

    async def test_cargo_response_is_typed_and_bound_to_requested_id(self):
        client = self.make_client(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "crg_1",
                    "managed": True,
                    "managed_by_sandbox_id": "sbx_1",
                    "backend": "docker-volume",
                    "untrusted": "ignored",
                },
            )
        )
        try:
            cargo = await client.get_cargo("crg_1")
        finally:
            await client.close()

        self.assertEqual(
            cargo,
            {
                "id": "crg_1",
                "managed": True,
                "managed_by_sandbox_id": "sbx_1",
            },
        )

        client = self.make_client(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "crg_other",
                    "managed": True,
                    "managed_by_sandbox_id": "sbx_1",
                },
            )
        )
        try:
            with self.assertRaises(NeoError) as raised:
                await client.get_cargo("crg_1")
        finally:
            await client.close()
        self.assertEqual(raised.exception.code, "protocol_error")

    async def test_upload_is_multipart_and_validates_exact_acknowledgement(self):
        captured = []
        secret_file = b'{"env":{"ACCOUNT_TOKEN":"business-secret"}}'

        def handler(request):
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "path": "checkin/credentials.json",
                    "size": len(secret_file),
                },
            )

        client = self.make_client(handler)
        try:
            await client.upload("sbx_1", "checkin/credentials.json", secret_file)
        finally:
            await client.close()

        request = captured[0]
        self.assertTrue(
            request.headers["Content-Type"].startswith("multipart/form-data")
        )
        self.assertIn(secret_file, request.content)
        self.assertIn(b"checkin/credentials.json", request.content)
        self.assertNotIn(b"neo-api-secret", request.content)

        for unsafe_path in (
            "/workspace/checkin/job.json",
            "../credentials.json",
            "checkin/../../credentials.json",
            "checkin\\credentials.json",
            "checkin//credentials.json",
        ):
            with self.subTest(path=unsafe_path):
                with self.assertRaises(NeoError) as raised:
                    await client.upload("sbx_1", unsafe_path, b"data")
                self.assertEqual(raised.exception.code, "invalid_request")

    async def test_exec_sends_no_secret_and_ignores_untrusted_error_field(self):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "exit_code": 7,
                    "output": '{"protocol":1}',
                    "error": "business-secret and server traceback",
                    "command": "server echoed command",
                },
            )

        client = self.make_client(handler)
        try:
            result = await client.exec(
                "sbx_1", "python checkin/bootstrap.py", timeout=240
            )
        finally:
            await client.close()

        self.assertEqual(
            result,
            {"success": False, "exit_code": 7, "output": '{"protocol":1}'},
        )
        body = json.loads(captured[0].content)
        self.assertEqual(
            body,
            {
                "command": "python checkin/bootstrap.py",
                "cwd": ".",
                "timeout": 240,
                "include_code": False,
            },
        )
        self.assertNotIn("business-secret", captured[0].content.decode())
        self.assertNotIn("neo-api-secret", captured[0].content.decode())

    async def test_only_exact_session_not_ready_error_gets_retryable_code(self):
        exact = self.make_client(
            lambda _request: httpx.Response(
                503, json={"error": {"code": "session_not_ready"}}
            )
        )
        try:
            with self.assertRaises(NeoError) as raised:
                await exact.exec("sbx_1", "python checkin/bootstrap.py", 30)
        finally:
            await exact.close()
        self.assertEqual(raised.exception.code, "session_not_ready")
        self.assertEqual(raised.exception.status, 503)

        other = self.make_client(
            lambda _request: httpx.Response(
                503, json={"error": {"code": "service_unavailable"}}
            )
        )
        try:
            with self.assertRaises(NeoError) as raised:
                await other.exec("sbx_1", "python checkin/bootstrap.py", 30)
        finally:
            await other.close()
        self.assertEqual(raised.exception.code, "unexpected_status")

    async def test_malformed_and_oversized_responses_are_static_protocol_errors(self):
        cases = (
            (httpx.Response(201, content=b"not-json"), "protocol_error"),
            (
                httpx.Response(201, json={"id": "../bad", "cargo_id": "crg_1"}),
                "protocol_error",
            ),
            (
                httpx.Response(201, json={"id": "sbx_1", "cargo_id": None}),
                "protocol_error",
            ),
            (
                httpx.Response(
                    201,
                    content=b'{"id":"sbx_1","id":"sbx_2","cargo_id":"crg_1"}',
                ),
                "protocol_error",
            ),
            (
                httpx.Response(201, content=b"x" * (256 * 1024 + 1)),
                "response_too_large",
            ),
        )

        for response, code in cases:
            with self.subTest(code=code, size=len(response.content)):
                calls = 0

                def handler(_request, response=response):
                    nonlocal calls
                    calls += 1
                    return response

                client = self.make_client(handler)
                try:
                    with self.assertRaises(NeoError) as raised:
                        await client.create_sandbox("run_1")
                finally:
                    await client.close()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(calls, 1)

    async def test_upload_and_exec_reject_malformed_success_responses(self):
        upload_client = self.make_client(
            lambda _request: httpx.Response(
                200,
                json={"status": "ok", "path": "checkin/job.json", "size": True},
            )
        )
        try:
            with self.assertRaises(NeoError) as raised:
                await upload_client.upload("sbx_1", "checkin/job.json", b"x")
        finally:
            await upload_client.close()
        self.assertEqual(raised.exception.code, "protocol_error")

        exec_client = self.make_client(
            lambda _request: httpx.Response(
                200,
                json={"success": True, "exit_code": False, "output": ""},
            )
        )
        try:
            with self.assertRaises(NeoError) as raised:
                await exec_client.exec("sbx_1", "python checkin/bootstrap.py", 30)
        finally:
            await exec_client.close()
        self.assertEqual(raised.exception.code, "protocol_error")

    async def test_server_and_transport_secrets_never_escape_and_requests_are_not_retried(
        self,
    ):
        leaked = "server-secret-that-must-not-escape"
        calls = 0

        def server_error(_request):
            nonlocal calls
            calls += 1
            return httpx.Response(
                500,
                json={"error": {"code": "internal", "message": leaked}},
            )

        client = self.make_client(server_error)
        try:
            with self.assertRaises(NeoError) as raised:
                await client.create_sandbox("run_1")
        finally:
            await client.close()
        self.assertEqual(calls, 1)
        self.assertNotIn(leaked, str(raised.exception))
        self.assertNotIn("neo-api-secret", str(raised.exception))

        calls = 0

        def transport_error(request):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError(leaked, request=request)

        client = self.make_client(transport_error)
        try:
            with self.assertRaises(NeoError) as raised:
                await client.create_sandbox("run_1")
        finally:
            await client.close()
        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.code, "transport_error")
        self.assertNotIn(leaked, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
