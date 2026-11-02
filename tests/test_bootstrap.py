import hashlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from state_support import load_module

bootstrap = load_module("runners.bootstrap")


def _write_private(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    path.chmod(0o600)


def _prepare_job(
    directory,
    script,
    *,
    environment=None,
    timeout_seconds=3,
    result_adapter="json_protocol",
    runtime="python",
    digest=None,
    artifact_format="script",
):
    directory = Path(directory)
    directory.chmod(0o700)
    script_name = "script.py" if runtime == "python" else "script.js"
    artifact_name = "artifact.zip" if artifact_format == "bundle" else script_name
    script_bytes = script.encode("utf-8") if isinstance(script, str) else script
    _write_private(directory / artifact_name, script_bytes)
    job = {
        "protocol": 1,
        "runtime": runtime,
        "sha256": digest or hashlib.sha256(script_bytes).hexdigest(),
        "timeout_seconds": timeout_seconds,
        "result_adapter": result_adapter,
        "artifact_format": artifact_format,
    }
    _write_private(
        directory / "job.json",
        json.dumps(job, separators=(",", ":"), ensure_ascii=False),
    )
    _write_private(
        directory / "credentials.json",
        json.dumps(
            {"env": {} if environment is None else environment},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )
    return directory


def _bundle_artifact(runtime, source, members):
    task_name = "task.py" if runtime == "python" else "task.js"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in ((task_name, source), *members):
            name, data, *mode = member
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (mode[0] if mode else stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def _native_payloads():
    pe = bytearray(128)
    pe[:2] = b"MZ"
    pe[60:64] = (64).to_bytes(4, "little")
    pe[64:68] = b"PE\0\0"
    return (
        b"\x7fELF" + b"x" * 64,
        bytes(pe),
        b"\xfe\xed\xfa\xcf" + b"x" * 64,
        b"\xca\xfe\xba\xbe" + b"x" * 64,
        b"\0asm" + b"x" * 64,
    )


def _pid_is_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_until_pid_is_gone(pid, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_is_gone(pid):
            return True
        time.sleep(0.02)
    return _pid_is_gone(pid)


def _force_kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class BootstrapResultTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node runtime is not installed")
    def test_node_result_and_literal_credentials(self):
        node = shutil.which("node")
        real_popen = subprocess.Popen
        token = " FAKE_NODE_COOKIE;'\"\nsecond line "
        digest = hashlib.sha256(token.encode()).hexdigest()
        script = (
            "const crypto = require('crypto');\n"
            "const hash = crypto.createHash('sha256').update(process.env.TOKEN).digest('hex');\n"
            f"if (hash !== '{digest}') process.exit(9);\n"
            "console.error(process.env.TOKEN);\n"
            "console.log(JSON.stringify({status:'already_signed',reason:process.env.TOKEN}));\n"
            "process.exitCode = 3;\n"
        )

        def spawn(argv, **kwargs):
            # The test host may install Node outside the Docker image's fixed PATH.
            return real_popen(
                [node, *argv[1:]] if argv[0] == "node" else argv, **kwargs
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(bootstrap.subprocess, "Popen", side_effect=spawn),
        ):
            directory = _prepare_job(
                temporary, script, runtime="node", environment={"TOKEN": token}
            )
            result = bootstrap.run(directory)
            self.assertFalse((directory / "credentials.json").exists())
        self.assertEqual(result["business_status"], "already_signed")
        self.assertEqual(result["exit_code"], 3)
        self.assertNotIn("FAKE_NODE_COOKIE", json.dumps(result))

    def test_explicit_json_is_business_evidence_independent_of_exit_code(self):
        scenarios = (
            (
                'import json\nprint(json.dumps({"status": "success"}))\nraise SystemExit(7)\n',
                "success",
                "reported_result",
                7,
            ),
            (
                'print("ordinary log")\nprint("{\\"status\\":\\"failed\\"}")\n',
                "unknown",
                "no_business_evidence",
                0,
            ),
            (
                "raise SystemExit(9)\n",
                "unknown",
                "no_business_evidence",
                9,
            ),
            (
                'print("{\\"status\\":\\"failed\\",\\"detail\\":\\"ignored\\"}")\n',
                "failed",
                "reported_result",
                0,
            ),
        )
        for script, business_status, reason_code, exit_code in scenarios:
            with (
                self.subTest(
                    business_status=business_status,
                    reason_code=reason_code,
                    exit_code=exit_code,
                ),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = _prepare_job(temporary, script)

                result = bootstrap.run(directory)

                self.assertEqual(result["process_status"], "exited")
                self.assertEqual(result["exit_code"], exit_code)
                self.assertEqual(result["business_status"], business_status)
                self.assertEqual(result["reason_code"], reason_code)
                self.assertFalse((directory / "credentials.json").exists())

    def test_process_only_never_treats_stdout_as_business_evidence(self):
        script = 'print("{\\"status\\":\\"success\\"}")\n'
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(temporary, script, result_adapter="process_only")

            result = bootstrap.run(directory)

        self.assertEqual(result["process_status"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["business_status"], "unknown")
        self.assertEqual(result["reason_code"], "no_business_evidence")

    def test_python_bundle_imports_prebuilt_module_with_literal_typed_environment(self):
        token = " FAKE_PYTHON;'\"$()\n账户 "
        source = (
            b"import json, os, pinned_helper\n"
            b"valid = (pinned_helper.VALUE == 7 and os.environ['TOKEN'] == "
            + repr(token).encode("utf-8")
            + b" and os.environ['COUNT'] == '42' and os.environ['ENABLED'] == 'true' and os.environ['MODE'] == 'fixed')\n"
            b"print(json.dumps({'status': 'success' if valid else 'failed'}))\n"
        )
        artifact = _bundle_artifact(
            "python",
            source,
            (
                ("pinned_helper.py", b"VALUE = 7\n", stat.S_IFREG | 0o755),
                ("config.json", b'{"enabled":true}\n'),
                ("certificate.pem", b"-----BEGIN CERTIFICATE-----\nfixture\n"),
                ("data.bin", b"\x01\x02\x03binary-data"),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(
                temporary,
                artifact,
                artifact_format="bundle",
                environment={
                    "TOKEN": token,
                    "COUNT": "42",
                    "ENABLED": "true",
                    "MODE": "fixed",
                },
            )

            result = bootstrap.run(directory)

            runtime = directory / "runtime"
            self.assertTrue(runtime.is_dir())
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (runtime / "pinned_helper.py").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(
                (runtime / "data.bin").read_bytes(), b"\x01\x02\x03binary-data"
            )
            self.assertFalse((directory / "credentials.json").exists())
        self.assertEqual(result["business_status"], "success")
        self.assertNotIn(token, json.dumps(result))

    @unittest.skipUnless(shutil.which("node"), "Node runtime is not installed")
    def test_node_bundle_imports_prebuilt_module_with_literal_environment(self):
        node = shutil.which("node")
        real_popen = subprocess.Popen
        token = " FAKE_NODE_BUNDLE;'\"$()\n账户 "
        source = (
            "const helper = require('pinned-helper');\n"
            f"const valid = helper.value === 7 && process.env.TOKEN === {json.dumps(token)};\n"
            "console.log(JSON.stringify({status: valid ? 'already_signed' : 'failed'}));\n"
            "process.exitCode = 5;\n"
        ).encode()
        artifact = _bundle_artifact(
            "node",
            source,
            (
                (
                    "node_modules/pinned-helper/index.js",
                    b"module.exports = {value: 7};\n",
                ),
            ),
        )

        def spawn(argv, **kwargs):
            return real_popen(
                [node, *argv[1:]] if argv[0] == "node" else argv, **kwargs
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(bootstrap.subprocess, "Popen", side_effect=spawn),
        ):
            directory = _prepare_job(
                temporary,
                artifact,
                artifact_format="bundle",
                runtime="node",
                environment={"TOKEN": token},
            )
            result = bootstrap.run(directory)

        self.assertEqual(result["business_status"], "already_signed")
        self.assertEqual(result["exit_code"], 5)
        self.assertNotIn(token, json.dumps(result))

    def test_large_stdout_is_drained_but_never_returned(self):
        script = (
            "import sys\n"
            'sys.stdout.write("private-output-" * 20000)\n'
            "sys.stdout.flush()\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(temporary, script)

            result = bootstrap.run(directory)

        self.assertEqual(result["process_status"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["business_status"], "unknown")
        self.assertEqual(result["reason_code"], "output_limit")
        self.assertNotIn("private-output", json.dumps(result))

    def test_large_stderr_is_discarded_while_json_stdout_is_accepted(self):
        secret = "stderr-private-value"
        script = (
            "import sys\n"
            f"sys.stderr.write({secret!r} * 20000)\n"
            'sys.stdout.write("{\\"status\\":\\"already_signed\\"}")\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(temporary, script)

            result = bootstrap.run(directory)

        self.assertEqual(result["business_status"], "already_signed")
        self.assertEqual(result["reason_code"], "reported_result")
        self.assertNotIn(secret, json.dumps(result))

    def test_environment_values_are_literal_isolated_and_not_reused(self):
        marker_name = "shell-injection-marker"
        with (
            tempfile.TemporaryDirectory() as first_temporary,
            tempfile.TemporaryDirectory() as second_temporary,
        ):
            marker = Path(first_temporary) / marker_name
            first_value = f" first\n'\";$(touch {marker}) 账户 "
            second_value = "second-user-value"
            script_template = (
                "import json, os, sys\n"
                "expected = {expected!r}\n"
                "clean = 'HOST_ONLY_VALUE' not in os.environ and expected not in sys.argv\n"
                "status = 'success' if clean and os.environ.get('ACCOUNT') == expected else 'config_error'\n"
                "print(json.dumps({{'status': status}}))\n"
            )
            first = _prepare_job(
                first_temporary,
                script_template.format(expected=first_value),
                environment={"ACCOUNT": first_value},
            )
            second = _prepare_job(
                second_temporary,
                script_template.format(expected=second_value),
                environment={"ACCOUNT": second_value},
            )

            with mock.patch.dict(
                os.environ, {"HOST_ONLY_VALUE": "must-not-reach-child"}, clear=False
            ):
                first_result = bootstrap.run(first)
                second_result = bootstrap.run(second)
            marker_created = marker.exists()

        self.assertEqual(first_result["business_status"], "success")
        self.assertEqual(second_result["business_status"], "success")
        self.assertFalse(marker_created)
        encoded_results = json.dumps([first_result, second_result])
        self.assertNotIn(first_value, encoded_results)
        self.assertNotIn(second_value, encoded_results)

    def test_standalone_cli_emits_one_safe_envelope_for_child_traceback(self):
        secret = "traceback-secret-请勿输出"
        script = (
            "import os\n"
            "print(os.environ['ACCOUNT_SECRET'], flush=True)\n"
            "raise RuntimeError(os.environ['ACCOUNT_SECRET'])\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(
                temporary, script, environment={"ACCOUNT_SECRET": secret}
            )
            wrapper = directory / "bootstrap.py"
            shutil.copyfile(bootstrap.__file__, wrapper)
            wrapper.chmod(0o600)

            completed = subprocess.run(
                [sys.executable, str(wrapper)],
                cwd=directory,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        self.assertNotIn(secret.encode("utf-8"), completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["process_status"], "exited")
        self.assertEqual(result["business_status"], "unknown")
        self.assertEqual(result["reason_code"], "no_business_evidence")


class BootstrapEnvironmentValidationTests(unittest.TestCase):
    def test_full_64_kib_of_values_is_accepted_without_charging_field_names(self):
        values = {f"FIELD_{index}": "x" * (16 * 1024) for index in range(4)}
        self.assertEqual(bootstrap.validate_environment(values), values)
        with self.assertRaises(ValueError):
            bootstrap.validate_environment({**values, "EXTRA": "x"})

    def test_validate_environment_preserves_values_in_a_safe_copy(self):
        value = {
            "CHECKIN_ACCOUNT": " 账户\n'\"$() ",
            "LANG": "zh_CN.UTF-8",
        }

        copied = bootstrap.validate_environment(value)

        self.assertEqual(copied, value)
        self.assertIsNot(copied, value)

    def test_validate_environment_rejects_unsafe_values_with_static_errors(self):
        invalid_values = (
            {"PATH": "/untrusted"},
            {"pythonpath": "untrusted"},
            {"NODE_OPTIONS": "--require=x"},
            {"LD_PRELOAD": "x"},
            {"DYLD_LIBRARY_PATH": "x"},
            {"NOT-AN-ENV-NAME": "x"},
            {"FIELD": "contains\x00nul"},
            {"FIELD": 1},
            {"field": "one", "FIELD": "two"},
        )

        for value in invalid_values:
            with self.subTest(names=tuple(value)):
                with self.assertRaises(ValueError) as raised:
                    bootstrap.validate_environment(value)
                self.assertEqual(str(raised.exception), "runner environment is invalid")
                self.assertIsNone(raised.exception.__cause__)


class BootstrapGuardTests(unittest.TestCase):
    def test_hash_mismatch_refuses_to_start_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "started"
            script = (
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
            )
            directory = _prepare_job(temporary, script, digest="0" * 64)

            result = bootstrap.run(directory)

            self.assertFalse(marker.exists())
            self.assertFalse((directory / "credentials.json").exists())
        self.assertEqual(result["process_status"], "start_error")
        self.assertEqual(result["business_status"], "config_error")
        self.assertEqual(result["reason_code"], "runner_error")

    def test_unsafe_bundle_members_are_rejected_before_extraction_or_execution(self):
        native_scenarios = tuple(
            ((f"renamed_{index}.bin", payload),)
            for index, payload in enumerate(_native_payloads())
        )
        scenarios = (
            (("../escape.py", b"x"),),
            (("linked.py", b"target", stat.S_IFLNK | 0o777),),
            (("native.node", b"native"),),
            (("large.py", b"x" * (2 * 1024 * 1024 + 1)),),
            *native_scenarios,
        )
        for members in scenarios:
            with (
                self.subTest(path=members[0][0]),
                tempfile.TemporaryDirectory() as temporary,
            ):
                marker = Path(temporary) / "started"
                source = (
                    f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                ).encode()
                artifact = _bundle_artifact("python", source, members)
                directory = _prepare_job(temporary, artifact, artifact_format="bundle")

                result = bootstrap.run(directory)

                self.assertEqual(result["process_status"], "start_error")
                self.assertEqual(result["business_status"], "config_error")
                self.assertFalse(marker.exists())
                self.assertFalse((directory / "runtime").exists())
                self.assertFalse((directory / "credentials.json").exists())

    def test_reserved_execution_environment_is_rejected_without_starting(self):
        reserved_names = (
            "PATH",
            "home",
            "BASH_ENV",
            "IFS",
            "PYTHONPATH",
            "NODE_OPTIONS",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
        )
        for name in reserved_names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                marker = Path(temporary) / "started"
                script = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                directory = _prepare_job(
                    temporary, script, environment={name: "untrusted"}
                )

                result = bootstrap.run(directory)

                self.assertEqual(result["process_status"], "start_error")
                self.assertFalse(marker.exists())
                self.assertFalse((directory / "credentials.json").exists())

    def test_environment_count_field_and_total_limits_refuse_execution(self):
        environments = (
            {f"FIELD_{index}": "x" for index in range(65)},
            {"FIELD": "x" * (16 * 1024 + 1)},
            {f"FIELD_{index}": "x" * (16 * 1024) for index in range(5)},
        )
        for environment in environments:
            with (
                self.subTest(fields=len(environment)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                marker = Path(temporary) / "started"
                script = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                directory = _prepare_job(temporary, script, environment=environment)

                result = bootstrap.run(directory)

                self.assertEqual(result["process_status"], "start_error")
                self.assertFalse(marker.exists())
                self.assertFalse((directory / "credentials.json").exists())

    def test_invalid_credentials_are_removed_before_safe_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(temporary, "pass\n")
            _write_private(directory / "credentials.json", b'{"env":')

            result = bootstrap.run(directory)

            self.assertEqual(result["process_status"], "start_error")
            self.assertEqual(result["exit_code"], None)
            self.assertFalse((directory / "credentials.json").exists())

    def test_oversized_credentials_are_removed_without_starting(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "started"
            script = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            directory = _prepare_job(temporary, script)
            _write_private(directory / "credentials.json", b" " * (256 * 1024 + 1))

            result = bootstrap.run(directory)

            self.assertEqual(result["process_status"], "start_error")
            self.assertFalse(marker.exists())
            self.assertFalse((directory / "credentials.json").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX file identity checks")
    def test_credentials_symlink_is_not_followed_and_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = _prepare_job(temporary, "pass\n")
            credentials = directory / "credentials.json"
            target = directory / "credential-target.json"
            credentials.unlink()
            _write_private(target, '{"env":{"SECRET":"kept"}}')
            credentials.symlink_to(target.name)

            result = bootstrap.run(directory)

            self.assertEqual(result["process_status"], "start_error")
            self.assertFalse(credentials.exists())
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '{"env":{"SECRET":"kept"}}',
            )

    @unittest.skipUnless(os.name == "posix", "POSIX file identity checks")
    def test_script_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "started"
            script = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            directory = _prepare_job(temporary, script)
            script_path = directory / "script.py"
            target = directory / "script-target.py"
            script_path.rename(target)
            script_path.symlink_to(target.name)

            result = bootstrap.run(directory)

            self.assertEqual(result["process_status"], "start_error")
            self.assertFalse(marker.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), script)
            self.assertFalse((directory / "credentials.json").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX mode checks")
    def test_private_directory_and_file_modes_are_required(self):
        with tempfile.TemporaryDirectory() as first_temporary:
            first = _prepare_job(first_temporary, "pass\n")
            first.chmod(0o755)
            try:
                first_result = bootstrap.run(first)
            finally:
                first.chmod(0o700)
            self.assertEqual(first_result["process_status"], "start_error")
            self.assertTrue((first / "credentials.json").exists())

        with tempfile.TemporaryDirectory() as second_temporary:
            marker = Path(second_temporary) / "started"
            script = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            second = _prepare_job(second_temporary, script)
            (second / "script.py").chmod(0o644)

            second_result = bootstrap.run(second)

            self.assertEqual(second_result["process_status"], "start_error")
            self.assertFalse(marker.exists())
            self.assertFalse((second / "credentials.json").exists())

    def test_job_schema_types_and_file_size_guards_are_strict(self):
        invalid_jobs = (
            {"protocol": True},
            {"timeout_seconds": 0},
            {"timeout_seconds": 241},
            {"result_adapter": "other"},
            {"artifact_format": "other"},
            {"unexpected": "field"},
        )
        for update in invalid_jobs:
            with (
                self.subTest(update=update),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = _prepare_job(temporary, "pass\n")
                job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
                job.update(update)
                _write_private(directory / "job.json", json.dumps(job))

                result = bootstrap.run(directory)

                self.assertEqual(result["process_status"], "start_error")
                self.assertFalse((directory / "credentials.json").exists())

        with tempfile.TemporaryDirectory() as temporary:
            oversized_script = b"#" * (1024 * 1024 + 1)
            directory = _prepare_job(temporary, oversized_script)

            result = bootstrap.run(directory)

            self.assertEqual(result["process_status"], "start_error")
            self.assertFalse((directory / "credentials.json").exists())


@unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
class BootstrapProcessCleanupTests(unittest.TestCase):
    def test_timeout_kills_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "descendant.pid"
            script = (
                "import os, pathlib, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "payload = b'x' * 8192\n"
                "while True:\n"
                "    os.write(1, payload)\n"
                "    os.write(2, payload)\n"
            )
            directory = _prepare_job(temporary, script, timeout_seconds=1)

            result = bootstrap.run(directory)
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
            self.addCleanup(_force_kill, descendant_pid)

            self.assertEqual(result["process_status"], "timeout")
            self.assertEqual(result["exit_code"], None)
            self.assertEqual(result["business_status"], "timeout")
            self.assertEqual(result["reason_code"], "timeout")
            self.assertTrue(_wait_until_pid_is_gone(descendant_pid))

    def test_descendants_are_killed_after_leader_reports_and_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "descendant.pid"
            script = (
                "import pathlib, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                'print(\'{"status":"success"}\')\n'
            )
            directory = _prepare_job(temporary, script)

            result = bootstrap.run(directory)
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
            self.addCleanup(_force_kill, descendant_pid)

            self.assertEqual(result["process_status"], "exited")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["business_status"], "success")
            self.assertTrue(_wait_until_pid_is_gone(descendant_pid))

    def test_sigterm_cancels_wrapper_and_cleans_child_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "descendant.pid"
            script = (
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            directory = _prepare_job(temporary, script, timeout_seconds=30)
            wrapper = directory / "bootstrap.py"
            shutil.copyfile(bootstrap.__file__, wrapper)
            wrapper.chmod(0o600)
            process = subprocess.Popen(
                [sys.executable, str(wrapper)],
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 4
                while not pid_file.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                descendant_pid = int(pid_file.read_text(encoding="utf-8"))
                self.addCleanup(_force_kill, descendant_pid)

                process.terminate()
                stdout, stderr = process.communicate(timeout=4)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)

            self.assertEqual(process.returncode, 0)
            self.assertEqual(stderr, b"")
            result = json.loads(stdout)
            self.assertEqual(result["process_status"], "cancelled")
            self.assertEqual(result["business_status"], "cancelled")
            self.assertEqual(result["reason_code"], "cancelled")
            self.assertTrue(_wait_until_pid_is_gone(descendant_pid))


class BootstrapEnvelopeValidationTests(unittest.TestCase):
    def test_validate_result_returns_an_independent_safe_copy(self):
        value = {
            "protocol": 1,
            "process_status": "exited",
            "exit_code": 7,
            "business_status": "needs_auth",
            "reason_code": "reported_result",
            "duration_ms": 123,
        }

        copied = bootstrap.validate_result(value)

        self.assertEqual(copied, value)
        self.assertIsNot(copied, value)

    def test_validate_result_accepts_interrupted_recovery_without_evidence(self):
        value = {
            "protocol": 1,
            "process_status": "interrupted",
            "exit_code": None,
            "business_status": "unknown",
            "reason_code": "no_business_evidence",
            "duration_ms": 0,
        }

        copied = bootstrap.validate_result(value)

        self.assertEqual(copied, value)

    def test_validate_result_accepts_host_start_uncertainty(self):
        for business_status in ("unknown", "config_error"):
            with self.subTest(business_status=business_status):
                value = {
                    "protocol": 1,
                    "process_status": "start_error",
                    "exit_code": None,
                    "business_status": business_status,
                    "reason_code": "no_business_evidence",
                    "duration_ms": 5,
                }

                copied = bootstrap.validate_result(value)

                self.assertEqual(copied, value)

    def test_validate_result_enforces_reserved_integer_bounds(self):
        boundary = {
            "protocol": 1,
            "process_status": "exited",
            "exit_code": 2**31 - 1,
            "business_status": "success",
            "reason_code": "reported_result",
            "duration_ms": 300_000,
        }
        self.assertEqual(bootstrap.validate_result(boundary), boundary)

        for field, value in (
            ("exit_code", -(2**31) - 1),
            ("exit_code", 2**31),
            ("duration_ms", 300_001),
        ):
            with self.subTest(field=field, value=value):
                overflow = dict(boundary)
                overflow[field] = value

                with self.assertRaises(ValueError) as raised:
                    bootstrap.validate_result(overflow)

                self.assertEqual(str(raised.exception), "runner result is invalid")

    def test_validate_result_rejects_invalid_shapes_types_and_combinations(self):
        valid = {
            "protocol": 1,
            "process_status": "exited",
            "exit_code": 0,
            "business_status": "success",
            "reason_code": "reported_result",
            "duration_ms": 0,
        }
        invalid_values = []
        for key, replacement in (
            ("protocol", True),
            ("process_status", "other"),
            ("exit_code", True),
            ("business_status", "private-detail"),
            ("reason_code", "arbitrary reason"),
            ("duration_ms", -1),
            ("duration_ms", True),
        ):
            changed = dict(valid)
            changed[key] = replacement
            invalid_values.append(changed)
        extra = dict(valid)
        extra["private-secret-field"] = "must-not-appear-in-error"
        invalid_values.append(extra)
        mismatched_timeout = dict(valid)
        mismatched_timeout.update(
            process_status="timeout",
            exit_code=None,
            business_status="success",
            reason_code="timeout",
        )
        invalid_values.append(mismatched_timeout)
        invalid_values.extend((None, [], "result"))

        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as raised:
                    bootstrap.validate_result(value)
                self.assertEqual(str(raised.exception), "runner result is invalid")
                self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
