#!/usr/bin/env python3
"""Opt-in AstrBot 4.29.4 acceptance with real KV/OneBot and controlled fake services.

Run in a disposable environment containing AstrBot==4.29.4 and requirements.txt:
    python scripts/verify-native.py --archive /tmp/plugin.zip

The archive must contain the integrated generic-task and personal-plan features.
Generated fake programs make no cloud requests. FakeBay runs trusted wrappers
locally; it is not an OS or network sandbox for untrusted plugin archives.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

PLUGIN = "astrbot_plugin_cloud_drive_checkin"
CONTROL = "NATIVE_CONTROL "
CANARIES = ("FAKE_NATIVE_A;'\"$()\n凭据", "FAKE_NATIVE_B", "FAKE_NATIVE_C")
ROOT = Path(__file__).resolve().parents[1]


def fixture():
    tasks, sources = [], {}
    for runtime in ("python", "node"):
        if runtime == "python":
            source = (
                b"import json, os, sys, locked_helper\n"
                b"if not os.environ['TOKEN'].startswith('FAKE_NATIVE_'): sys.exit(9)\n"
                b"if os.environ['MODE'] != 'native' or os.environ['ENABLED'] != 'true': sys.exit(9)\n"
                b"if 'NATIVE_ACCEPTANCE_HOST_SENTINEL' in os.environ: sys.exit(9)\n"
                b"print(json.dumps({'status': locked_helper.status(int(os.environ['COUNT']))}))\n"
                b"sys.exit(7)\n"
            )
            module, body = (
                "locked_helper.py",
                (
                    b"def status(count):\n    return 'already_signed' if count == 2 else 'success'\n"
                ),
            )
            suffix = "py"
        else:
            source = (
                b"const helper = require('locked-helper');\n"
                b"if (!process.env.TOKEN.startsWith('FAKE_NATIVE_') || process.env.MODE !== 'native' || process.env.ENABLED !== 'true') process.exit(9);\n"
                b"if (process.env.NATIVE_ACCEPTANCE_HOST_SENTINEL !== undefined) process.exit(9);\n"
                b"console.log(JSON.stringify({status: helper.status(Number(process.env.COUNT))}));\n"
                b"process.exitCode = 7;\n"
            )
            module, body = (
                "node_modules/locked-helper/index.js",
                (b"exports.status = n => n === 2 ? 'already_signed' : 'success';\n"),
            )
            suffix = "js"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            member = zipfile.ZipInfo(module, date_time=(2020, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(member, body)
        bundle = output.getvalue()
        source_url = f"https://raw.githubusercontent.com/acceptance/fixtures/{'a' * 40}/task.{suffix}"
        bundle_url = f"https://raw.githubusercontent.com/acceptance/fixtures/{'b' * 40}/{runtime}.zip"
        sources.update({source_url: source, bundle_url: bundle})
        tasks.append(
            {
                "task_id": f"native_{runtime}",
                "name": f"Native {runtime}",
                "enabled": True,
                "runtime": runtime,
                "script_url": source_url,
                "source_commit": "a" * 40,
                "sha256": hashlib.sha256(source).hexdigest(),
                "cron": "0 0 1 1 *",
                "user_fields": [
                    {
                        "field_id": name,
                        "label": name,
                        "env_name": name.upper(),
                        "value_type": kind,
                        "required": True,
                        "secret": secret,
                        "hint": "acceptance fake input",
                    }
                    for name, kind, secret in (
                        ("token", "string", True),
                        ("count", "int", False),
                        ("enabled", "bool", False),
                    )
                ],
                "fixed_env": {"MODE": "native"},
                "input_adapter": "direct_env",
                "result_adapter": "json_protocol",
                "dependency_bundle": json.dumps(
                    {
                        "format": "zip-v1",
                        "url": bundle_url,
                        "sha256": hashlib.sha256(bundle).hexdigest(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "timeout_seconds": 30,
                "experimental": False,
            }
        )
    return tasks, sources


def isolate_child(root):
    os.environ["ASTRBOT_ROOT"] = str(root)
    os.environ["ASTRBOT_CLI"] = "1"
    temporary = root / "tmp"
    temporary.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(temporary)
    for key in tuple(os.environ):
        if key.lower().endswith("_proxy"):
            del os.environ[key]
    sys.path.insert(0, str(root))

    def audit(event, args):
        if event in {"socket.connect", "socket.bind"} and isinstance(args[1], tuple):
            host = args[1][0]
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == "localhost"
            if not allowed:
                raise PermissionError("acceptance permits loopback sockets only")

    sys.addaudithook(audit)


async def child(args):
    isolate_child(args.root)
    import httpx
    from astrbot.core import LogBroker, astrbot_config, db_helper
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

    astrbot_config["dashboard"]["enable"] = False
    astrbot_config["admins_id"] = ["90001"]
    astrbot_config["provider"] = []
    astrbot_config["provider_sources"] = []
    astrbot_config["wake_prefix"] = ["/"]
    astrbot_config["platform_settings"]["enable_id_white_list"] = False
    astrbot_config["platform"] = [
        {
            "id": f"native-bot-{index}",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": port,
            "ws_reverse_token": "",
        }
        for index, port in enumerate(args.ports)
    ]
    astrbot_config.save_config()
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    await core.initialize()
    running = None
    source_requests = []

    def current():
        metadata = core.star_context.get_registered_star(PLUGIN)
        if metadata is None:
            raise RuntimeError("the real framework did not load the plugin")
        return metadata.star_cls

    def emit(kind, **payload):
        print(CONTROL + json.dumps({"kind": kind, **payload}), flush=True)

    class Stream(httpx.AsyncByteStream):
        def __init__(self, value):
            self.value = value

        async def __aiter__(self):
            yield self.value

    try:
        if args.archive:
            await core.plugin_manager.install_plugin_from_file(str(args.archive))
            plugin = current()
            tasks, _sources = fixture()
            plugin.config.update(
                enabled=True,
                whitelist_qqs=["10001", "10002"],
                tasks=tasks,
                manual_cooldown_seconds=0,
                user_interval={"mode": "none"},
                task_interval={"mode": "none"},
                neo_connection={
                    "enabled": True,
                    "docker_cleanup_confirmed": True,
                    "endpoint": args.bay,
                    "access_key_env": "CLOUD_DRIVE_CHECKIN_NEO_KEY",
                },
            )
            assert await plugin.config.save_config_async() is True
        running = asyncio.create_task(core.start())
        emit("ready", pid=os.getpid())
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            operation = request["op"]
            plugin = current()
            if operation == "stop":
                break
            if operation == "observe":
                emit(
                    "reply",
                    state=plugin.state_store.snapshot(),
                    source_requests=len(source_requests),
                    ready=plugin.state_store.ready,
                )
            elif operation == "source":
                _tasks, sources = fixture()

                async def download(http_request, payloads=sources):
                    source_requests.append(str(http_request.url))
                    assert http_request.method == "GET"
                    payload = payloads[str(http_request.url)]
                    return httpx.Response(
                        200,
                        headers={"content-length": str(len(payload))},
                        stream=Stream(payload),
                    )

                service = plugin._preparation_service()
                assert service._client is None
                service._client = httpx.AsyncClient(
                    transport=httpx.MockTransport(download), trust_env=False
                )
                emit("reply")
            elif operation == "expire":
                run_id = request["run_id"]

                def expire(snapshot, selected=run_id):
                    item = snapshot["batches"][selected]
                    assert item["status"] == "queued"
                    item["queued_at"] = (
                        datetime.now(UTC) - timedelta(hours=2)
                    ).isoformat()
                    item["expires_at"] = (
                        datetime.now(UTC) - timedelta(hours=1)
                    ).isoformat()

                await plugin.state_store.mutate(expire)
                emit("reply")
            elif operation == "arm_notice_commit":
                already_sent = {
                    key
                    for key, value in plugin.state_store.snapshot()["outbox"].items()
                    if value["phase"] == "sent"
                }
                real_put = plugin.put_kv_data
                gate = asyncio.Event()

                async def gated_put(
                    key, value, sent=already_sent, wait_gate=gate, put=real_put
                ):
                    newly_sent = [
                        key
                        for key, record in value["outbox"].items()
                        if record["phase"] == "sent" and key not in sent
                    ]
                    if newly_sent:
                        emit("gate", run_id=newly_sent[0])
                        await wait_gate.wait()
                    await put(key, value)

                plugin.put_kv_data = gated_put
                emit("reply")
            else:
                raise AssertionError(operation)
    finally:
        await core.stop()
        if running is not None:
            await running


class Process:
    def __init__(self, root, archive, bay, ports, environment):
        self.root, self.archive, self.bay, self.ports = root, archive, bay, ports
        self.environment = environment
        self.events = asyncio.Queue()
        self.logs = []
        self.process = None
        self.killed = False

    async def start(self):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--root",
            str(self.root),
            "--bay",
            self.bay,
            "--ports",
            *(str(port) for port in self.ports),
        ]
        if self.archive:
            command.extend(("--archive", str(self.archive)))
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.root,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def output():
            async for line in self.process.stdout:
                text = line.decode(errors="replace").rstrip()
                if text.startswith(CONTROL):
                    await self.events.put(json.loads(text[len(CONTROL) :]))
                else:
                    self.logs.append(text)
            await self.events.put({"kind": "exit"})

        self.reader = asyncio.create_task(output())
        assert (await self.event())["kind"] == "ready", "\n".join(self.logs)
        return self

    async def event(self):
        return await asyncio.wait_for(self.events.get(), 60)

    async def request(self, operation, **values):
        self.process.stdin.write(
            (json.dumps({"op": operation, **values}) + "\n").encode()
        )
        await self.process.stdin.drain()
        response = await self.event()
        assert response["kind"] == "reply", (response, "\n".join(self.logs))
        return response

    async def kill(self):
        self.killed = True
        self.process.kill()
        await self.process.wait()
        await self.reader

    async def close(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.stdin.write(b'{"op":"stop"}\n')
            await self.process.stdin.drain()
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 30)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                raise
        await self.reader
        expected = -9 if self.killed else 0
        assert self.process.returncode == expected, "\n".join(self.logs)


class Peer:
    def __init__(self, port, index):
        self.port, self.index = port, index
        self.inbox = asyncio.Queue()
        self.messages = []
        self.sequence = 0
        self.closed = False
        self.last_response = None

    async def start(self):
        from websockets.asyncio.client import connect

        async with asyncio.timeout(15):
            while True:
                try:
                    self.ws = await connect(
                        f"ws://127.0.0.1:{self.port}/ws",
                        additional_headers={
                            "X-Self-ID": str(12345 + self.index),
                            "X-Client-Role": "Universal",
                        },
                        proxy=None,
                    )
                    break
                except OSError:
                    await asyncio.sleep(0.05)

        async def receive():
            async for raw in self.ws:
                request = json.loads(raw)
                name, params = request["action"], request.get("params", {})
                if name in {"send_msg", "send_private_msg", "send_group_msg"}:
                    message = params.get("message", "")
                    text = (
                        message
                        if isinstance(message, str)
                        else "".join(
                            part.get("data", {}).get("text", "") for part in message
                        )
                    )
                    record = (params, text)
                    self.messages.append(record)
                    await self.inbox.put(record)
                    data = {"message_id": 20000 + len(self.messages)}
                elif name == "get_login_info":
                    data = {"user_id": 12345 + self.index, "nickname": "NativeBot"}
                elif name == "get_stranger_info":
                    data = {"user_id": params["user_id"], "nickname": "NativeUser"}
                elif name in {"get_group_list", "get_friend_list"}:
                    data = []
                else:
                    raise AssertionError(f"Unexpected OneBot action: {name}")
                await self.ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": data,
                            "echo": request.get("echo"),
                        }
                    )
                )

        self.reader = asyncio.create_task(receive())
        return self

    async def command(self, text, qq=10001):
        self.sequence += 1
        await self.ws.send(
            json.dumps(
                {
                    "time": int(time.time()),
                    "self_id": 12345 + self.index,
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "message_id": self.sequence,
                    "user_id": qq,
                    "sender": {"user_id": qq, "nickname": "NativeUser"},
                    "message": [{"type": "text", "data": {"text": text}}],
                    "raw_message": text,
                    "font": 0,
                }
            )
        )
        self.last_response = await asyncio.wait_for(self.inbox.get(), 15)
        params, reply = self.last_response
        assert int(params["user_id"]) == qq, (params, reply)
        return reply

    async def close(self, *, crashed=False):
        from websockets.exceptions import ConnectionClosed

        if self.closed:
            return
        self.closed = True
        await self.ws.close()
        try:
            await self.reader
        except ConnectionClosed:
            if not crashed:
                raise


def deliveries(run_id, peers, *, exclude=None):
    return [
        (peer.index, int(record[0]["user_id"]))
        for peer in peers
        for record in peer.messages
        if record is not exclude and run_id in record[1]
    ]


async def wait_state(process, predicate):
    async with asyncio.timeout(45):
        while True:
            state = (await process.request("observe"))["state"]
            if predicate(state):
                return state
            await asyncio.sleep(0.05)


async def run_acceptance(archive):
    assert importlib.metadata.version("AstrBot") == "4.29.4"
    assert shutil.which("node"), "Node is required for the controlled local Bay"
    import httpx
    from aiohttp import web

    sys.path.insert(0, str(ROOT / "tests"))
    from neo_support import FakeBay

    class Bay(FakeBay):
        def __init__(self):
            super().__init__()
            self.block_response = False
            self.business_finished = asyncio.Event()
            self.release_response = asyncio.Event()
            self.node_path_adjusted = False
            self.credential_owners = {}

        async def __call__(self, request):
            result = await super().__call__(request)
            if request.url.path.endswith("/filesystem/upload"):
                sandbox = request.url.path.split("/")[3]
                if self.upload_paths[-1] == "checkin/credentials.json":
                    credentials = (
                        Path(self.root.name) / sandbox / "checkin" / "credentials.json"
                    )
                    token = json.loads(credentials.read_text())["env"]["TOKEN"]
                    self.credential_owners[sandbox] = hashlib.sha256(
                        token.encode()
                    ).hexdigest()
                wrapper = Path(self.root.name) / sandbox / "checkin" / "bootstrap.py"
                if wrapper.exists():
                    original = wrapper.read_bytes()
                    node_dir = str(Path(shutil.which("node")).parent)
                    if node_dir not in ("/usr/local/bin", "/usr/bin", "/bin"):
                        adjusted = original.replace(
                            b'"/usr/local/bin:/usr/bin:/bin"',
                            json.dumps(
                                node_dir + ":/usr/local/bin:/usr/bin:/bin"
                            ).encode(),
                        )
                        if adjusted != original:
                            wrapper.write_bytes(adjusted)
                            self.node_path_adjusted = True
            if request.url.path.endswith("/shell/exec"):
                body = json.loads(await request.aread())
                if (
                    body["command"] == "python3 checkin/bootstrap.py"
                    and self.block_response
                ):
                    self.business_finished.set()
                    await self.release_response.wait()
            return result

    bay = Bay()

    def assert_credential_owner(state, run_id, token):
        sandbox_id = state["runs"][run_id]["sandbox_id"]
        assert (
            bay.credential_owners[sandbox_id]
            == hashlib.sha256(token.encode()).hexdigest()
        )

    requests = []

    async def handle(request):
        requests.append((request.method, request.path))
        response = await bay(
            httpx.Request(
                request.method,
                "http://127.0.0.1" + request.path_qs,
                headers=request.headers,
                content=await request.read(),
            )
        )
        return web.Response(
            status=response.status_code,
            body=await response.aread(),
            headers={
                "Content-Type": response.headers.get("content-type", "application/json")
            },
        )

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_route("*", "/{path:.*}", handle)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    ports = []
    for _ in range(2):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            ports.append(listener.getsockname()[1])
    host_sentinel = "FAKE_NATIVE_HOST_ENVIRONMENT_ONLY"
    os.environ["NATIVE_ACCEPTANCE_HOST_SENTINEL"] = host_sentinel
    environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    environment["CLOUD_DRIVE_CHECKIN_MASTER_KEY"] = base64.urlsafe_b64encode(
        os.urandom(32)
    ).decode()
    environment["CLOUD_DRIVE_CHECKIN_NEO_KEY"] = bay.token
    environment["PYTHONUNBUFFERED"] = "1"
    processes, peers = [], []
    with tempfile.TemporaryDirectory(prefix="checkin-native-acceptance-") as temporary:
        try:
            root = Path(temporary)
            upload = root / "plugin.zip"
            environment["HOME"] = str(root)
            shutil.copyfile(archive, upload)

            async def start(upload=None):
                process = Process(root, upload, endpoint, ports, environment)
                processes.append(process)
                await process.start()
                connected = [
                    await Peer(port, index).start() for index, port in enumerate(ports)
                ]
                peers.extend(connected)
                return process, connected

            process, (first, second) = await start(upload)
            await first.command("/签到 管理 重载", 90001)
            _params, terminal = await asyncio.wait_for(first.inbox.get(), 15)
            assert "重载完成" in terminal
            await process.request("source")
            for task in ("native_python", "native_node"):
                reply = await first.command(f"/签到 管理 准备 {task}", 90001)
                assert re.search(r"[0-9a-f]{64}", reply), reply

            async def enroll(peer, task, token, count, qq=10001):
                for text in (f"/签到 录入 {task}", token, str(count), "true", "/确认"):
                    await peer.command(text, qq)

            await enroll(first, "native_python", CANARIES[0], 1)
            await enroll(second, "native_python", CANARIES[1], 2)
            await enroll(first, "native_python", CANARIES[2], 3, 10002)
            before = (await process.request("observe"))["state"]
            assert len(before["bindings"]) == 3
            status = await first.command("/签到 我的")
            assert "Asia/Shanghai" in status and "+08:00" in status
            assert (await process.request("observe"))["state"] == before
            assert not any(
                token in json.dumps(before, ensure_ascii=False) for token in CANARIES
            )
            credentials = before["credentials"]

            bay.block_response = True
            accepted = await first.command("/签到 立即 native_python")
            first_run = re.search(r"\b[0-9a-f]{32}\b", accepted).group()
            await asyncio.wait_for(bay.business_finished.wait(), 20)
            accepted = await second.command("/签到 立即 native_python")
            queued_run = re.search(r"\b[0-9a-f]{32}\b", accepted).group()
            accepted = await first.command("/签到 立即 native_python", 10002)
            expired_run = re.search(r"\b[0-9a-f]{32}\b", accepted).group()
            await process.request("expire", run_id=expired_run)
            calls_before_crash = bay.execution_calls
            await process.kill()
            await first.close(crashed=True)
            await second.close(crashed=True)
            bay.block_response = False
            bay.release_response.set()
            process, (first, second) = await start()
            recovered = await wait_state(
                process,
                lambda state: all(
                    state["batches"][key]["status"] == "finished"
                    and state["outbox"][key]["phase"] == "sent"
                    for key in (first_run, queued_run, expired_run)
                ),
            )
            assert recovered["credentials"] == credentials
            assert (
                recovered["batches"][first_run]["result"]["business_status"]
                == "unknown"
            )
            assert (
                recovered["batches"][queued_run]["result"]["business_status"]
                == "already_signed"
            ), {
                "queued_result": recovered["batches"][queued_run]["result"],
                "bay_history": bay.history,
            }
            assert (
                recovered["batches"][expired_run]["result"]["business_status"]
                == "cancelled"
            )
            assert bay.execution_calls == calls_before_crash + 1
            assert not recovered["cleanup_jobs"]
            assert_credential_owner(recovered, first_run, CANARIES[0])
            assert_credential_owner(recovered, queued_run, CANARIES[1])
            assert expired_run not in recovered["runs"]
            assert deliveries(first_run, (first, second)) == [(0, 10001)]
            assert deliveries(queued_run, (first, second)) == [(1, 10001)]
            assert deliveries(expired_run, (first, second)) == [(0, 10002)]
            for peer in (first, second):
                while not peer.inbox.empty():
                    peer.inbox.get_nowait()
            await process.request("arm_notice_commit")
            accepted = await first.command("/签到 立即 native_python")
            uncertain_run = re.search(r"\b[0-9a-f]{32}\b", accepted).group()
            gate = await process.event()
            assert gate == {"kind": "gate", "run_id": uncertain_run}
            calls_before_notice_crash = bay.execution_calls
            assert deliveries(uncertain_run, (first, second)) == [
                (0, 10001),
                (0, 10001),
            ]
            await process.kill()
            await first.close(crashed=True)
            await second.close(crashed=True)
            process, (first, second) = await start()
            after_notice = (await process.request("observe"))["state"]
            assert after_notice["outbox"][uncertain_run]["phase"] == "unknown"
            assert bay.execution_calls == calls_before_notice_crash
            assert_credential_owner(after_notice, uncertain_run, CANARIES[0])
            status_reply = await first.command("/签到 状态 native_python")
            status_record = first.last_response
            assert "Asia/Shanghai" in status_reply
            assert not deliveries(uncertain_run, (first, second), exclude=status_record)
            await enroll(first, "native_node", CANARIES[0], 2)
            accepted = await first.command("/签到 立即 native_node")
            node_run = re.search(r"\b[0-9a-f]{32}\b", accepted).group()
            final = await wait_state(
                process, lambda state: state["outbox"][node_run]["phase"] == "sent"
            )
            assert (
                final["batches"][node_run]["result"]["business_status"]
                == "already_signed"
            )
            assert final["batches"][node_run]["result"]["exit_code"] == 7
            assert_credential_owner(final, node_run, CANARIES[0])
            assert deliveries(node_run, (first, second)) == [(0, 10001), (0, 10001)]
            assert not deliveries(uncertain_run, (first, second), exclude=status_record)
            assert len(bay.credential_owners) == 4
            assert host_sentinel not in json.dumps(bay.history)
            assert bay.max_executions == 1
            assert not final["cleanup_jobs"] and not bay.sandboxes and not bay.cargos
            assert not list(Path(bay.root.name).iterdir())
            outgoing = json.dumps([peer.messages for peer in peers], ensure_ascii=False)
            assert not any(token in outgoing for token in CANARIES)
            summary = {
                "framework": "AstrBot 4.29.4",
                "native_loader_and_KV": True,
                "onebot": "real aiocqhttp with two controlled loopback peers",
                "artifact_source": "controlled pinned-source HTTP transport",
                "neo": "loopback FakeBay, real wrapper and fake Python/Node programs",
                "node_test_path_adjusted": bay.node_path_adjusted,
                "source_installs": 1,
                "real_process_kills": 2,
                "encrypted_bindings_survived": True,
                "started_business_not_replayed": True,
                "queued_resumed": True,
                "expired_cancelled": True,
                "uncertain_notification_not_resent": True,
                "personal_plan_read_only": True,
                "http_requests": len(requests),
                "business_requests": bay.execution_calls,
                "max_concurrent": bay.max_executions,
                "residual_resources": 0,
                "outgoing_canary_free": True,
            }
        finally:
            bay.release_response.set()
            outcomes = await asyncio.gather(
                *(peer.close() for peer in reversed(peers)),
                *(process.close() for process in reversed(processes)),
                return_exceptions=True,
            )
            await server.cleanup()
            await bay.close()
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
    print(json.dumps(summary, ensure_ascii=False))


def main():
    if not __debug__:
        raise RuntimeError(
            "native acceptance requires assertions; remove -O or PYTHONOPTIMIZE"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--bay", help=argparse.SUPPRESS)
    parser.add_argument("--ports", nargs=2, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        asyncio.run(child(args))
    else:
        if args.archive is None or not args.archive.is_file():
            parser.error("--archive must name the plugin source ZIP")
        asyncio.run(run_acceptance(args.archive.resolve()))


if __name__ == "__main__":
    main()
