import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from state_support import load_module

preparation = load_module("preparation", "_tianyi_preparation_tests")

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


def _zip_member(name, data, mode=stat.S_IFREG | 0o644):
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 2, 3, 4, 6))
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info, data


def _wheel_bytes(distribution, *, extra_members=()):
    token = distribution.replace("-", "_")
    dist_info = f"{token}-{_VERSION}.dist-info"
    members = [
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
        *extra_members,
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as wheel:
        for info, data in members:
            wheel.writestr(info, data)
    return output.getvalue()


def _bundle_bytes(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, *mode in members:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 2, 3, 4, 6))
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


def _bundle_declaration(payload):
    return json.dumps(
        {
            "format": "zip-v1",
            "url": (
                "https://raw.githubusercontent.com/example/dependencies/"
                + "b" * 40
                + "/bundle.zip"
            ),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class SyntheticTianyiInputs:
    def __init__(self, base):
        self.root = Path(base)
        self.wheelhouse = self.root / "wheelhouse"
        (self.root / "third_party/tianyi").mkdir(parents=True)
        (self.root / "runners").mkdir()
        self.wheelhouse.mkdir()

        self.source = b"def main_handler(event, context):\n    return {}\n"
        self.source_sha256 = hashlib.sha256(self.source).hexdigest()
        self.source_commit = "a" * 40
        (self.root / "third_party/tianyi/index.py").write_bytes(self.source)
        (self.root / "third_party/tianyi/LICENSE").write_bytes(b"vendor license\n")
        (self.root / "LICENSE").write_bytes(b"project license\n")
        (self.root / "runners/tianyi_entry.py").write_bytes(
            b'import json\nprint(json.dumps({"status":"unknown"}))\n'
        )
        (self.root / "_conf_schema.json").write_text(
            json.dumps(
                {
                    "tasks": {
                        "default": [
                            {
                                "task_id": "tianyi_cloud",
                                "source_commit": self.source_commit,
                                "sha256": self.source_sha256,
                            }
                        ]
                    }
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        self.wheels = {
            distribution: _wheel_bytes(distribution) for distribution in _DISTRIBUTIONS
        }
        self._write_wheels_and_lock()

    def wheel_path(self, distribution):
        return self.wheelhouse / (
            f"{distribution.replace('-', '_')}-{_VERSION}-py3-none-any.whl"
        )

    def _write_wheels_and_lock(self):
        lines = ["# synthetic locked wheels"]
        for distribution in _DISTRIBUTIONS:
            payload = self.wheels[distribution]
            self.wheel_path(distribution).write_bytes(payload)
            lines.append(
                f"{distribution}=={_VERSION} --hash=sha256:"
                f"{hashlib.sha256(payload).hexdigest()}"
            )
        (self.root / "third_party/tianyi/requirements.lock").write_text(
            "\n".join(lines) + "\n", encoding="ascii"
        )

    def replace_wheel(self, distribution, payload, *, update_lock=True):
        self.wheels[distribution] = payload
        self.wheel_path(distribution).write_bytes(payload)
        if update_lock:
            self._write_wheels_and_lock()

    def build(self):
        with mock.patch.object(preparation, "_ROOT", self.root):
            return preparation.build_tianyi(self.wheelhouse)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.inputs = SyntheticTianyiInputs(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_repeat_build_is_deterministic_complete_and_bounded(self):
        first = self.inputs.build()
        second = self.inputs.build()

        self.assertEqual(first, second)
        self.assertEqual(first.sha256, hashlib.sha256(first.script).hexdigest())
        self.assertRegex(first.inputs_sha256, r"[0-9a-f]{64}\Z")
        self.assertLessEqual(len(first.script), 1024 * 1024)

        with zipfile.ZipFile(io.BytesIO(first.script)) as archive:
            names = archive.namelist()
            self.assertIn("__main__.py", names)
            self.assertEqual(archive.read("tianyi_source.py"), self.inputs.source)
            self.assertEqual(
                archive.read("licenses/tianyi/LICENSE"), b"vendor license\n"
            )
            self.assertEqual(
                archive.read("licenses/project/LICENSE"), b"project license\n"
            )
            for distribution in _DISTRIBUTIONS:
                token = distribution.replace("-", "_")
                self.assertIn(f"{token}-{_VERSION}.dist-info/licenses/LICENSE", names)

    def test_hash_mismatch_fails_before_archive_consumption(self):
        self.inputs.replace_wheel(
            "requests", self.inputs.wheels["requests"] + b"changed", update_lock=False
        )
        with self.assertRaises(preparation.PreparationError):
            self.inputs.build()

    def test_invalid_archive_and_member_path_are_rejected(self):
        original = self.inputs.wheels["requests"]
        scenarios = (
            (b"not a wheel archive", "valid archive"),
            (
                _wheel_bytes(
                    "requests", extra_members=(_zip_member("../escape.py", b"bad"),)
                ),
                "invalid member path",
            ),
        )
        for payload, message in scenarios:
            with self.subTest(message=message):
                self.inputs.replace_wheel("requests", payload)
                with self.assertRaises(preparation.PreparationError):
                    self.inputs.build()
                self.inputs.replace_wheel("requests", original)

    def test_unsupported_and_unsafe_wheel_layouts_are_rejected(self):
        original = self.inputs.wheels["requests"]
        symlink = _zip_member("requests/link", b"target", mode=stat.S_IFLNK | 0o777)
        scenarios = (
            (
                _wheel_bytes(
                    "requests",
                    extra_members=(_zip_member("requests-1.0.data/purelib/x.py", b""),),
                ),
                "unsupported \\.data layout",
            ),
            (
                _wheel_bytes(
                    "requests", extra_members=(_zip_member("requests/native.so", b""),)
                ),
                "native library",
            ),
            (_wheel_bytes("requests", extra_members=(symlink,)), "symlink"),
            (
                _wheel_bytes(
                    "requests", extra_members=(_zip_member("__main__.py", b"bad"),)
                ),
                "reserved entry",
            ),
        )
        for payload, message in scenarios:
            with self.subTest(message=message):
                self.inputs.replace_wheel("requests", payload)
                with self.assertRaises(preparation.PreparationError):
                    self.inputs.build()
                self.inputs.replace_wheel("requests", original)

    def test_source_pin_and_exact_wheel_identity_fail_closed(self):
        (self.inputs.root / "third_party/tianyi/index.py").write_bytes(b"modified")
        with self.assertRaises(preparation.PreparationError):
            self.inputs.build()

        (self.inputs.root / "third_party/tianyi/index.py").write_bytes(
            self.inputs.source
        )
        requests_path = self.inputs.wheel_path("requests")
        requests_path.rename(
            self.inputs.wheelhouse / "requests-1.0-cp312-cp312-macosx.whl"
        )
        with self.assertRaises(preparation.PreparationError):
            self.inputs.build()

    def test_cross_wheel_module_collision_is_rejected(self):
        self.inputs.replace_wheel(
            "requests",
            _wheel_bytes(
                "requests",
                extra_members=(
                    _zip_member("rsa/__init__.py", b"conflicting module\n"),
                ),
            ),
        )
        with self.assertRaises(preparation.PreparationError):
            self.inputs.build()

    def test_artifact_over_executor_limit_is_rejected(self):
        import random

        payload = random.Random(0).randbytes(1024 * 1024 + 32)
        self.inputs.replace_wheel(
            "requests",
            _wheel_bytes(
                "requests",
                extra_members=(_zip_member("requests/large_resource.bin", payload),),
            ),
        )
        with self.assertRaises(preparation.PreparationError):
            self.inputs.build()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_named_pipe_wheel_is_rejected_without_waiting_for_a_writer(self):
        wheel = self.inputs.wheel_path("certifi")
        wheel.unlink()
        os.mkfifo(wheel)
        code = """from pathlib import Path
import preparation, sys
preparation._ROOT = Path(sys.argv[1])
try:
    preparation.build_tianyi(Path(sys.argv[2]))
except preparation.PreparationError:
    raise SystemExit(0)
raise SystemExit(1)
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(self.inputs.root),
                str(self.inputs.wheelhouse),
            ],
            cwd=Path(preparation.__file__).parent,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)


class GenericPreparationTests(unittest.TestCase):
    def build(self, runtime, source, bundle, declaration=None):
        dependency_bundle = (
            _bundle_declaration(bundle) if declaration is None else declaration
        )
        dependency_sha256 = json.loads(dependency_bundle)["sha256"]
        return preparation.build_generic(
            runtime=runtime,
            source=source,
            source_commit="a" * 40,
            source_sha256=hashlib.sha256(source).hexdigest(),
            dependency_bundle=dependency_bundle,
            dependency_sha256=dependency_sha256,
            bundle=bundle,
        )

    def test_dependency_free_artifact_remains_a_raw_script(self):
        source = b'print("{\\"status\\":\\"unknown\\"}")\n'

        artifact = preparation.build_generic(
            runtime="python",
            source=source,
            source_commit="a" * 40,
            source_sha256=hashlib.sha256(source).hexdigest(),
            dependency_bundle="none",
            dependency_sha256=None,
            bundle=None,
        )

        self.assertEqual(artifact.script, source)
        self.assertEqual(artifact.artifact_format, "script")

    def test_python_and_node_prebuilt_modules_are_packaged_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "host-executed"
            cases = (
                (
                    "python",
                    b"import pinned_helper\n",
                    "pinned_helper.py",
                    f"from pathlib import Path\nPath({str(marker)!r}).touch()\nVALUE=7\n".encode(),
                    "task.py",
                ),
                (
                    "node",
                    b"require('pinned-helper')\n",
                    "node_modules/pinned-helper/index.js",
                    f"require('fs').writeFileSync({str(marker)!r}, 'x'); module.exports=7;\n".encode(),
                    "task.js",
                ),
            )
            for runtime, source, module_path, module, task_path in cases:
                with self.subTest(runtime=runtime):
                    bundle = _bundle_bytes(((module_path, module),))
                    artifact = self.build(runtime, source, bundle)
                    self.assertEqual(artifact.artifact_format, "bundle")
                    with zipfile.ZipFile(io.BytesIO(artifact.script)) as archive:
                        self.assertEqual(
                            set(archive.namelist()), {task_path, module_path}
                        )
                        self.assertEqual(archive.read(module_path), module)
            self.assertFalse(marker.exists())

    def test_text_helpers_and_non_native_auxiliary_binary_remain_supported(self):
        source = b"import helper\n"
        bundle = _bundle_bytes(
            (
                ("helper.py", b"VALUE = 1\n", stat.S_IFREG | 0o755),
                ("config.json", b'{"enabled":true}\n'),
                ("certificate.pem", b"-----BEGIN CERTIFICATE-----\nfixture\n"),
                ("data.bin", b"\x01\x02\x03binary-data"),
            )
        )

        artifact = self.build("python", source, bundle)

        self.assertEqual(artifact.artifact_format, "bundle")

    def test_unsafe_dependency_archives_and_hashes_fail_before_publication(self):
        source = b"print('not executed')\n"
        large = b"x" * (2 * 1024 * 1024 + 1)
        bomb = b"x" * (2 * 1024 * 1024)
        native_bundles = tuple(
            _bundle_bytes(((f"renamed_{index}.bin", payload),))
            for index, payload in enumerate(_native_payloads())
        )
        scenarios = (
            _bundle_bytes((("../escape.py", b"x"),)),
            _bundle_bytes((("task.py", b"collision"),)),
            _bundle_bytes((("native.so", b"native"),)),
            _bundle_bytes((("linked.py", b"target", stat.S_IFLNK | 0o777),)),
            _bundle_bytes((("large.py", large),)),
            _bundle_bytes(tuple((f"part{index}.bin", bomb) for index in range(5))),
            *native_bundles,
        )
        for bundle in scenarios:
            with (
                self.subTest(bundle_sha256=hashlib.sha256(bundle).hexdigest()),
                self.assertRaises(preparation.PreparationError),
            ):
                self.build("python", source, bundle)

        bundle = _bundle_bytes((("helper.py", b"VALUE=1\n"),))
        declaration = json.loads(_bundle_declaration(bundle))
        declaration["sha256"] = "0" * 64
        with self.assertRaises(preparation.PreparationError):
            self.build(
                "python",
                source,
                bundle,
                json.dumps(declaration, sort_keys=True, separators=(",", ":")),
            )


class PreparationCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).resolve().parents[1] / "scripts/prepare-tianyi.py"
        spec = importlib.util.spec_from_file_location(
            "_prepare_tianyi_cli_tests", script
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load prepare-tianyi CLI")
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def test_cli_refuses_existing_output_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.pyz"
            output.write_bytes(b"keep")
            stderr = io.StringIO()
            with (
                mock.patch.object(self.cli, "build_tianyi") as build,
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                self.cli.main(["--wheelhouse", temporary, "--output", str(output)])
            self.assertEqual(raised.exception.code, 2)
            build.assert_not_called()
            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
