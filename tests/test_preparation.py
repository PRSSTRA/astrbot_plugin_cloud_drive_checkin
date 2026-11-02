import contextlib
import hashlib
import importlib.util
import io
import json
import stat
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
