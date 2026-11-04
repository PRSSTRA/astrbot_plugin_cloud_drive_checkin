"""受控 Bay 替身，使用真实独立 runner；改编自 dev a662b766（仓库许可证）。"""

import asyncio
from contextlib import suppress
import copy
from email.parser import BytesParser
import email.policy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile

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
        self.value = copy.deepcopy(value)  # 模拟持久化失败前已更新的缓存。
        if len(self.writes) == self.fail_at:
            raise OSError("simulated KV failure")


class FakeBay:
    """不部署 Docker/Neo，也不访问云盘账号或外部网络。"""

    def __init__(self):
        self.root = tempfile.TemporaryDirectory(prefix="checkin-fake-bay-")
        self.sandboxes = {}
        self.cargos = {}
        self.create_calls = 0
        self.execution_calls = 0
        self.install_calls = 0
        self.on_upload = None
        self.after_install = None
        self.live_executions = 0
        self.max_executions = 0
        self.install_result = None
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
        self.processes = {}

    async def close(self):
        for process in tuple(self.processes):
            await self._stop(process)
        self.root.cleanup()

    async def _stop(self, process):
        """只终止受控命令派生的进程组。"""
        groups = {process.pid}
        try:
            if process.returncode is None:
                inspector = await asyncio.create_subprocess_exec(
                    "/bin/ps",
                    "-e",
                    "-o",
                    "pid=",
                    "-o",
                    "ppid=",
                    "-o",
                    "pgid=",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, _error = await inspector.communicate()
                if inspector.returncode:
                    raise RuntimeError(
                        "cannot inspect controlled subprocess descendants")
                rows = [
                    tuple(map(int, line.split()))
                    for line in output.splitlines()
                ]
                descendants = {process.pid}
                while True:
                    found = {
                        pid for pid, parent, _group in rows
                        if parent in descendants
                    }
                    if found <= descendants:
                        break
                    descendants.update(found)
                groups.update(
                    group for pid, _parent, group in rows if pid in descendants)
        finally:
            for group in groups:
                if group > 0 and group != os.getpgrp():
                    with suppress(ProcessLookupError):
                        os.killpg(group, signal.SIGKILL)
            await process.wait()
            self.processes.pop(process, None)

    async def _stop_sandbox(self, sandbox_id):
        for process, owner in tuple(self.processes.items()):
            if owner == sandbox_id:
                await self._stop(process)

    @staticmethod
    def missing():
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async def __call__(self, request):
        method, path = request.method, request.url.path
        if path == "/v1/profiles":
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "python-default",
                        "capabilities": ["shell", "filesystem", "python"],
                    }]
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
                raise httpx.ReadTimeout("simulated lost create response",
                                        request=request)
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
                        503, json={"error": {
                            "code": "storage_unavailable"
                        }})
                cargo = self.cargos.pop(resource_id)
                await self._stop_sandbox(cargo["managed_by_sandbox_id"])
                directory = Path(
                    self.root.name) / cargo["managed_by_sandbox_id"]
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
                await self._stop_sandbox(resource_id)
                del self.sandboxes[resource_id]
                if not self.keep_cargo:
                    self.cargos.pop(sandbox["cargo_id"], None)
                    shutil.rmtree(directory, ignore_errors=True)
                return httpx.Response(204)
        if pieces[3:] == ["filesystem", "upload"]:
            body = await request.aread()
            message = BytesParser(policy=email.policy.default).parsebytes(
                ("Content-Type: " + request.headers["content-type"] +
                 "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            parts = {
                part.get_param("name", header="content-disposition"):
                    part.get_payload(decode=True)
                for part in message.iter_parts()
            }
            relative = parts["path"].decode()
            (directory / relative).write_bytes(parts["file"])
            self.upload_paths.append(relative)
            if self.on_upload:
                self.on_upload(relative, directory)
            if relative.endswith("credentials.json"):
                env = json.loads(parts["file"])["env"]
                self.credential_digests.append(
                    hashlib.sha256(env.get("TOKEN", "").encode()).hexdigest())
            return httpx.Response(200,
                                  json={
                                      "status": "ok",
                                      "path": relative,
                                      "size": len(parts["file"])
                                  })
        if pieces[3:] == ["shell", "exec"]:
            body = json.loads(await request.aread())
            if body["command"] == "python3 checkin/bootstrap.py install":
                self.install_calls += 1
            is_business = body["command"] == "python3 checkin/bootstrap.py run"
            if is_business:
                self.execution_calls += 1
                self.executing.set()
                if self.hold_execution:
                    await self.release_execution.wait()
            self.live_executions += 1
            self.max_executions = max(self.max_executions, self.live_executions)
            process = None
            try:
                arguments = shlex.split(body["command"])
                if arguments[0] == "python3":
                    arguments[0] = sys.executable
                if body['command'] in ('python3 checkin/bootstrap.py install',
                                       'python3 checkin/bootstrap.py run'):
                    # 仅在受控子进程边界模拟沙箱内安装的运行时。
                    node, npm = shutil.which('node'), shutil.which('npm')
                    launcher = '''import json,os,runpy
from pathlib import Path
namespace=runpy.run_path('checkin/bootstrap.py')
scope=namespace['run'].__globals__
spawn=scope['_spawn']
which=scope['shutil'].which
node,npm=''' + repr((node, npm)) + '''
def controlled(argv,**kwargs):
 if node:
  kwargs['env']={**kwargs['env'],'PATH':str(Path(node).parent)+':'+kwargs['env']['PATH']}
  if argv[0] in ('node','npm'): argv=[node if argv[0]=='node' else npm,*argv[1:]]
 return spawn(argv,**kwargs)
scope['_spawn']=controlled
scope['shutil'].which=lambda name,**kwargs: {'node':node,'npm':npm}.get(name) or which(name,**kwargs)
result=namespace['run'](Path('checkin'),''' + repr(arguments[-1]) + ''')
print(json.dumps(result,ensure_ascii=False))'''
                    arguments = [sys.executable, '-c', launcher]
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    cwd=directory,
                    env={
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "HOME": str(directory),
                        "TMPDIR": str(directory),
                    },
                    start_new_session=True,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.processes[process] = resource_id
                stdout, stderr = await asyncio.wait_for(process.communicate(),
                                                        body["timeout"] + 5)
                response = {
                    "success": process.returncode == 0,
                    "exit_code": process.returncode,
                    "output": stdout.decode(),
                    "error": stderr.decode() or None,
                }
                self.history.append({"command": body["command"], **response})
                if (body['command'] == 'python3 checkin/bootstrap.py install'
                        and self.install_result is not None):
                    response['output'] = json.dumps(self.install_result)
                if (body["command"] == "python3 checkin/bootstrap.py install"
                        and self.after_install):
                    self.after_install(directory)
            finally:
                if process is not None:
                    await self._stop(process)
                self.live_executions -= 1
            if is_business and self.ambiguous_execution:
                raise httpx.ReadTimeout("simulated lost exec response",
                                        request=request)
            return httpx.Response(200, json=response)
        return self.missing()
