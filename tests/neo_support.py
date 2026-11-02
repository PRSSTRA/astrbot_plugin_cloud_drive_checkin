"""Controlled Bay transport: fake resources, real wrapper and fake child programs."""

import asyncio
import copy
import email.policy
import hashlib
import json
import shutil
import tempfile
from email.parser import BytesParser
from pathlib import Path

import httpx


class ExecutionKV:
    def __init__(self):
        self.value = None
        self.writes = []
        self.fail_at = None

    async def get_kv_data(self, key, default):
        return copy.deepcopy(self.value) if self.value is not None else default

    async def put_kv_data(self, key, value):
        self.writes.append(copy.deepcopy(value))
        self.value = copy.deepcopy(
            value
        )  # Model a cache updated before persistence fails.
        if len(self.writes) == self.fail_at:
            raise OSError("simulated KV failure")


class FakeBay:
    """No Docker/Neo deployment and no cloud account/network calls."""

    def __init__(self):
        self.root = tempfile.TemporaryDirectory(prefix="checkin-fake-bay-")
        self.sandboxes = {}
        self.cargos = {}
        self.create_calls = 0
        self.execution_calls = 0
        self.live_executions = 0
        self.max_executions = 0
        self.history = []
        self.upload_paths = []
        self.credential_digests = []
        self.ambiguous_create = False
        self.ambiguous_execution = False
        self.keep_cargo = False
        self.hold_execution = False
        self.executing = asyncio.Event()
        self.release_execution = asyncio.Event()
        self.hold_cleanup = False
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.token = "FAKE_NEO_ACCESS_KEY_NOT_A_REAL_CREDENTIAL"

    def close(self):
        self.root.cleanup()

    @staticmethod
    def missing():
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async def __call__(self, request):
        method, path = request.method, request.url.path
        if path == "/v1/profiles":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "python-default",
                            "capabilities": ["shell", "filesystem", "python"],
                        }
                    ]
                },
            )
        if request.headers.get("Authorization") != "Bearer " + self.token:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if path == "/v1/sandboxes" and method == "POST":
            self.create_calls += 1
            payload = json.loads(await request.aread())
            assert payload["cargo_id"] is None and payload["ttl"] > 0
            sandbox_id, cargo_id = (
                f"sandbox-{self.create_calls}",
                f"ws-{self.create_calls}",
            )
            resource = {
                "id": sandbox_id,
                "cargo_id": cargo_id,
                "profile": payload["profile"],
                "status": "ready",
                "capabilities": ["shell", "filesystem", "python"],
            }
            self.sandboxes[sandbox_id] = resource
            self.cargos[cargo_id] = {
                "id": cargo_id,
                "managed": True,
                "managed_by_sandbox_id": sandbox_id,
                "backend": "docker_volume",
            }
            (Path(self.root.name) / sandbox_id).mkdir()
            if self.ambiguous_create:
                raise httpx.ReadTimeout(
                    "simulated lost create response", request=request
                )
            return httpx.Response(201, json=resource)
        pieces = path.strip("/").split("/")
        if len(pieces) < 3:
            return self.missing()
        resource_id = pieces[2]
        if pieces[1] == "cargos":
            if resource_id not in self.cargos:
                return self.missing()
            if method == "GET":
                return httpx.Response(200, json=self.cargos[resource_id])
            if method == "DELETE":
                if self.keep_cargo:
                    return httpx.Response(
                        503, json={"error": {"code": "storage_unavailable"}}
                    )
                cargo = self.cargos.pop(resource_id)
                directory = Path(self.root.name) / cargo["managed_by_sandbox_id"]
                shutil.rmtree(directory, ignore_errors=True)
                return httpx.Response(204)
        if resource_id not in self.sandboxes:
            return self.missing()
        sandbox = self.sandboxes[resource_id]
        directory = Path(self.root.name) / resource_id
        if len(pieces) == 3:
            if method == "GET":
                return httpx.Response(200, json=sandbox)
            if method == "DELETE":
                self.cleanup_started.set()
                if self.hold_cleanup:
                    await self.release_cleanup.wait()
                del self.sandboxes[resource_id]
                if not self.keep_cargo:
                    self.cargos.pop(sandbox["cargo_id"], None)
                    shutil.rmtree(directory, ignore_errors=True)
                return httpx.Response(204)
        if pieces[3:] == ["filesystem", "upload"]:
            body = await request.aread()
            message = BytesParser(policy=email.policy.default).parsebytes(
                (
                    "Content-Type: "
                    + request.headers["content-type"]
                    + "\r\nMIME-Version: 1.0\r\n\r\n"
                ).encode()
                + body
            )
            parts = {
                part.get_param("name", header="content-disposition"): part.get_payload(
                    decode=True
                )
                for part in message.iter_parts()
            }
            relative = parts["path"].decode()
            (directory / relative).write_bytes(parts["file"])
            self.upload_paths.append(relative)
            if relative.endswith("credentials.json"):
                env = json.loads(parts["file"])["env"]
                self.credential_digests.append(
                    hashlib.sha256(env.get("TOKEN", "").encode()).hexdigest()
                )
            return httpx.Response(
                200, json={"status": "ok", "path": relative, "size": len(parts["file"])}
            )
        if pieces[3:] == ["shell", "exec"]:
            body = json.loads(await request.aread())
            is_business = body["command"] == "python3 checkin/bootstrap.py"
            if is_business:
                self.execution_calls += 1
                self.executing.set()
                if self.hold_execution:
                    await self.release_execution.wait()
            self.live_executions += 1
            self.max_executions = max(self.max_executions, self.live_executions)
            try:
                process = await asyncio.create_subprocess_shell(
                    body["command"],
                    cwd=directory,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), body["timeout"] + 5
                )
                response = {
                    "success": process.returncode == 0,
                    "exit_code": process.returncode,
                    "output": stdout.decode(),
                    "error": stderr.decode() or None,
                }
                self.history.append({"command": body["command"], **response})
            finally:
                self.live_executions -= 1
            if is_business and self.ambiguous_execution:
                raise httpx.ReadTimeout("simulated lost exec response", request=request)
            return httpx.Response(200, json=response)
        return self.missing()
