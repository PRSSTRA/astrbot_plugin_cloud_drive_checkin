import asyncio
import json
import os
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from neo_support import FakeBay


class FakeBayLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bay = FakeBay()
        self.addAsyncCleanup(self.bay.close)
        response = await self.request(
            "POST",
            "/v1/sandboxes",
            {"cargo_id": None, "ttl": 900, "profile": "python-default"},
        )
        self.sandbox = response.json()["id"]
        self.directory = Path(self.bay.root.name) / self.sandbox

    async def request(self, method, path, payload):
        return await self.bay(
            httpx.Request(
                method,
                "http://fixture.invalid" + path,
                headers={"Authorization": "Bearer " + self.bay.token},
                json=payload,
            )
        )

    async def command(self, source, timeout=1):
        return await self.request(
            "POST",
            f"/v1/sandboxes/{self.sandbox}/shell/exec",
            {"command": "python3 -c " + shlex.quote(source), "timeout": timeout},
        )

    async def hanging_command(self, timeout):
        marker = self.directory / "descendant.pid"
        source = (
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\n"
            f"Path({str(marker)!r}).write_text(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        task = asyncio.create_task(self.command(source, timeout))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        process = next(iter(self.bay.processes))
        return task, process, int(marker.read_text())

    async def assert_reaped(self, process, descendant):
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.bay.processes, {})
        self.assertEqual(self.bay.live_executions, 0)
        probe = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-p",
            str(descendant),
            "-o",
            "stat=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _error = await probe.communicate()
        # An orphan may briefly await the OS reaper, but must not still execute.
        self.assertTrue(not output.strip() or output.strip().startswith(b"Z"))

    async def test_timeout_stops_wrapper_and_separate_descendant_group(self):
        task, process, descendant = await self.hanging_command(timeout=1)
        with self.assertRaises(TimeoutError):
            await task
        await self.assert_reaped(process, descendant)

    async def test_request_cancellation_stops_wrapper_and_descendant(self):
        task, process, descendant = await self.hanging_command(timeout=30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_reaped(process, descendant)

    async def test_close_stops_owned_commands_before_removing_directory(self):
        task, process, descendant = await self.hanging_command(timeout=30)
        await self.bay.close()
        result = await task
        self.assertNotEqual(result.json()["exit_code"], 0)
        await self.assert_reaped(process, descendant)
        self.assertFalse(self.directory.exists())

    async def test_wrapper_does_not_inherit_host_credentials(self):
        names = (
            "UNRELATED_HOST_SECRET",
            "CLOUD_DRIVE_CHECKIN_MASTER_KEY",
            "CLOUD_DRIVE_CHECKIN_NEO_KEY",
        )
        with patch.dict(os.environ, dict.fromkeys(names, "FAKE_HOST_ONLY_SENTINEL")):
            response = await self.command(
                "import json,os,sys;print(json.dumps({'values':{key:os.environ.get(key) for key in "
                + repr(names)
                + "},'python':sys.executable}))"
            )
        result = json.loads(response.json()["output"])
        self.assertEqual(result["values"], dict.fromkeys(names))
        self.assertEqual(
            Path(result["python"]).resolve(), Path(sys.executable).resolve()
        )
