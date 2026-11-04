"""验证脚本声明、配置校验与调度表达式边界。"""

from dataclasses import replace
from datetime import datetime
from datetime import UTC
import hashlib
import importlib
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if "_checkin_tests" not in sys.modules:
    package = types.ModuleType("_checkin_tests")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
config = importlib.import_module("_checkin_tests.config")


def declaration(**changes):
    raw = {
        "__template_key": "script",
        "id": "cloud",
        "name": "云盘",
        "enabled": True,
        "url": "https://example.com/main/checkin.py?download=1",
        "runtime": "python",
        "env_names": ["ACCOUNT", "PASSWORD"],
        "dependencies": []
    }
    raw.update(changes)
    return raw


class ConfigTests(unittest.TestCase):

    def parse(self, **changes):
        return config.parse_config({
            "scripts": [declaration(**changes)]
        }).scripts[0]

    def test_fields_hash_order_and_full_declaration_fingerprint(self):
        script = self.parse()
        expected = hashlib.sha256(b'["ACCOUNT","PASSWORD"]').hexdigest()
        self.assertEqual(script.fields_hash, expected)
        for changes in ({
                "id": "other"
        }, {
                "name": "other"
        }, {
                "enabled": False
        }, {
                "url": "https://example.com/other.py"
        }, {
                "runtime": "node"
        }, {
                "env_names": ("PASSWORD", "ACCOUNT")
        }, {
                "dependencies": ("requests",)
        }):
            changed = replace(script, **changes)
            self.assertNotEqual(script.fingerprint, changed.fingerprint)
            if "env_names" not in changes:
                self.assertEqual(script.fields_hash, changed.fields_hash)

    def test_configuration_boundaries(self):
        self.assertEqual(
            config.parse_config({
                "whitelist_qqs": [123, "456"]
            }).whitelist_qqs, frozenset({"123", "456"}))
        for raw in ({
                "enabled": 1
        }, {
                "timeout_seconds": 0
        }, {
                "timeout_seconds": 241
        }, {
                "install_timeout_seconds": True
        }, {
                "install_timeout_seconds": 241
        }, {
                "whitelist_qqs": ["01"]
        }, {
                "whitelist_qqs": [True]
        }, {
                "scripts": {}
        }, {
                "scripts": [declaration(), declaration()]
        }, {
                "scripts": [declaration(__template_key="other")]
        }, {
                "scripts": [declaration(fixed_env={})]
        }):
            with self.subTest(raw=raw), self.assertRaises(
                    config.PluginConfigError):
                config.parse_config(raw)
        for field in ("timeout_seconds", "install_timeout_seconds"):
            for boundary in (1, 240):
                self.assertEqual(
                    getattr(config.parse_config({field: boundary}), field),
                    boundary)
        for invalid in ("A", "a-b", "a" * 65, "a\n"):
            with self.subTest(invalid=invalid), self.assertRaises(
                    config.PluginConfigError):
                self.parse(id=invalid)

    def test_environment_name_and_value_limits(self):
        allowed = "A" * 128
        self.assertEqual(self.parse(env_names=[allowed]).env_names, (allowed,))
        for names in (["PATH"], ["pythonpath"], ["Node_OPTIONS"],
                      ["LD_PRELOAD"], ["DYLD_LIBRARY_PATH"], ["HOME"],
                      ["SHELL"], ["ENV"], ["BASH_ENV"], ["IFS"], ["A", "a"],
                      ["A" * 129], ["bad-name"
                                   ], ["中文"], [f"A{i}" for i in range(65)]):
            with self.subTest(names=names), self.assertRaises(
                    config.PluginConfigError):
                self.parse(env_names=names)
        full = {f"A{i}": "x" * 1024 for i in range(64)}
        self.assertEqual(config.validate_environment(full), full)
        for env in ({
                "A": ""
        }, {
                "A": "\0"
        }, {
                "A": "x" * 16385
        }, {
                "A": "\ud800"
        }, {
                "A": 1
        }, {
                **full, "A0": "x" * 1025
        }):
            with self.subTest(env_keys=list(env)), self.assertRaises(
                    config.PluginConfigError):
                config.validate_environment(env)

    def test_public_https_url_only(self):
        for url in ("https://example.com/main/a.py?branch=main",
                    "https://8.8.8.8/a.py",
                    "https://[2606:4700:4700::1111]:443/a.js"):
            self.assertEqual(config.validate_script_url(url), url)
        for url in ("http://example.com/a.py", "https://example.com:444/a.py",
                    "https://user:pass@example.com/a.py",
                    "https://example.com/a.py#x", "https://example.com/a.py#",
                    "https://example.com/a\n.py", "https://example.com/a%00.py",
                    "https://example.com/a%0d.py", "https://127.0.0.1/a.py",
                    "https://10.0.0.1/a.py", "https://[::1]/a.py",
                    "https://169.254.169.254/a.py", "https://example.com/",
                    "https://example.com", "https://example.com:/a.py",
                    "https://example.com\\@8.8.8.8/a.py",
                    "https://bad_host/a.py"):
            with self.subTest(url=url), self.assertRaises(
                    config.PluginConfigError):
                config.validate_script_url(url)

    def test_dependency_validation_normalization_and_limits(self):
        script = self.parse(dependencies=["Requests[socks]>=2", "foo_bar==1.0"])
        self.assertEqual(script.dependencies,
                         ("requests[socks]>=2", "foo-bar==1.0"))
        for deps in (["foo_bar",
                      "Foo-Bar>=1"], ["foo @ https://example.com/a.whl"],
                     ["foo; python_version>'3'"], ["--index-url=x"], ["../foo"],
                     ["-e foo"], ["foo\n"], ["foo"] * 129,
                     [f"package{i}[{'a' * 300}]" for i in range(128)]):
            with self.subTest(deps=deps[:2]), self.assertRaises(
                    config.PluginConfigError):
                self.parse(dependencies=deps)
        node = self.parse(
            runtime="node",
            dependencies=["@scope/pkg@^1.0 || >=2", "lodash", "other@latest"])
        self.assertEqual(node.dependencies,
                         ("@scope/pkg@^1.0 || >=2", "lodash", "other@latest"))
        for deps in (["foo",
                      "foo@1"], ["foo@git+https://example.com/a"
                                ], ["foo@file:../foo"], ["foo@npm:bar@1"
                                                        ], ["foo@workspace:*"],
                     ["foo@1;id"], ["foo@$(id)"], ["https://example.com/foo"],
                     ["../foo"], ["foo@"], ["Foo@1"], ["foo@1\n"]):
            with self.subTest(deps=deps), self.assertRaises(
                    config.PluginConfigError):
                self.parse(runtime="node", dependencies=deps)
        self.assertEqual(self.parse(dependencies=[]).dependencies, ())

    def test_cron_impossible_dates_sunday_or_and_lists(self):
        start = datetime(2025, 2, 1, tzinfo=UTC)
        for expression in ("0 0 31 2 *", "@daily", "0 0 0 * * *", "0 24 * * *",
                           "*/0 * * * *", "0 0 * * 8"):
            with self.subTest(expression=expression), self.assertRaises(
                    config.PluginConfigError):
                config.validate_cron(expression, "UTC")
        for sunday in ("0", "7", "SUN"):
            expression = f"0 0 31 2 {sunday}"
            self.assertEqual(config.next_fire(expression, "UTC", start),
                             datetime(2025, 2, 2, tzinfo=UTC))
            config.validate_cron(expression, "UTC")
        self.assertEqual(config.next_fire("0 0 29 2 *", "UTC", start),
                         datetime(2028, 2, 29, tzinfo=UTC))
        self.assertEqual(
            config.next_fire("*/15 1-3 * JAN,MAR MON-FRI", "UTC",
                             datetime(2025, 1, 1, 1, tzinfo=UTC)),
            datetime(2025, 1, 1, 1, 15, tzinfo=UTC))
        with self.assertRaises(config.PluginConfigError):
            config.next_fire("0 7 * * *", "UTC", datetime(2025, 1, 1))

    def test_default_timezone_is_shanghai_independent_of_server(self):
        self.addCleanup(time.tzset)
        environment = patch.dict(os.environ, {"TZ": "UTC0"})
        environment.start()
        self.addCleanup(environment.stop)
        time.tzset()
        parsed = config.parse_config({})
        self.assertEqual(parsed.timezone, "Asia/Shanghai")
        start = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.assertEqual(config.next_fire(parsed.cron, parsed.timezone, start),
                         datetime(2025, 1, 1, 23, tzinfo=UTC))
        for invalid in ("not/a-zone", "shanghai", "", " UTC", None, 8):
            with self.subTest(invalid=invalid), self.assertRaises(
                    config.PluginConfigError):
                config.parse_config({"timezone": invalid})

    def test_cron_uses_configured_timezone_in_winter_and_summer(self):
        for zone, hours in (("UTC", (7, 7)), ("Asia/Shanghai", (23, 23)),
                            ("America/New_York", (12, 11))):
            self.assertEqual(
                config.parse_config({
                    "timezone": zone
                }).timezone, zone)
            for month, hour in zip((1, 7), hours):
                with self.subTest(zone=zone, month=month):
                    start = datetime(2025, month, 1, 12, tzinfo=UTC)
                    day = 1 if zone == "Asia/Shanghai" else 2
                    expected = datetime(2025, month, day, hour, tzinfo=UTC)
                    result = config.next_fire("0 7 * * *", zone, start)
                    self.assertEqual(result.astimezone(UTC), expected)
                    self.assertEqual(result.hour, 7)
                    config.validate_cron("0 7 * * *", zone)

    def test_cron_skips_nonexistent_times_in_configured_timezone(self):
        zone = "America/New_York"
        start = datetime(2025, 3, 9, 0, tzinfo=UTC)
        self.assertEqual(config.next_fire("30 2 * * *", zone, start),
                         datetime(2025, 3, 10, 6, 30, tzinfo=UTC))
        before_jump = datetime(2025, 3, 9, 6, 59, tzinfo=UTC)
        self.assertEqual(config.next_fire("* * * * *", zone, before_jump),
                         datetime(2025, 3, 9, 7, tzinfo=UTC))
