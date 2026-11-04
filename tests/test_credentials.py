"""验证凭据解析、加密上下文与环境变量载荷边界。"""

import base64
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if "_checkin_tests" not in sys.modules:
    package = types.ModuleType("_checkin_tests")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
credentials = importlib.import_module("_checkin_tests.credentials")
config = importlib.import_module("_checkin_tests.config")
permissions = importlib.import_module("_checkin_tests.permissions")


def script(names=("ACCOUNT", "PASSWORD")):
    return config.Script("cloud", "云盘", True, "https://example.com/a.py",
                         "python", tuple(names), ())


class CredentialTests(unittest.TestCase):

    def setUp(self):
        self.identity = permissions.Identity("qq-main", "123")
        self.script = script()
        self.cipher = credentials.CredentialCipher(b"x" * 32)
        self.context = {
            "identity": self.identity,
            "script_id": self.script.id,
            "fields_hash": self.script.fields_hash
        }

    def test_single_scan_escape_parser_keeps_raw_spaces(self):
        self.assertEqual(credentials.parse_values("  a\\#b#c\\\\d\n ", 2),
                         ["  a#b", "c\\d\n "])
        self.assertEqual(credentials.parse_values("取消", 1), ["取消"])
        for text, count in (("a\\x#b", 2), ("a#b\\", 2), ("a#b#c", 2), ("a", 2),
                            ("", 0)):
            with self.subTest(text=text), self.assertRaises(
                    credentials.CredentialError):
                credentials.parse_values(text, count)

    def test_all_fields_required_exact_keys_and_ordered_result(self):
        values = {"PASSWORD": " b ", "ACCOUNT": " a "}
        result = credentials.validate_values(self.script, values)
        self.assertEqual(list(result), ["ACCOUNT", "PASSWORD"])
        self.assertEqual(result["PASSWORD"], " b ")
        self.assertEqual(credentials.validate_values(script(()), {}), {})
        for values in ({
                "ACCOUNT": "a"
        }, {
                "ACCOUNT": "a",
                "PASSWORD": ""
        }, {
                "ACCOUNT": "a",
                "PASSWORD": "b",
                "EXTRA": "c"
        }, {
                "ACCOUNT": "a",
                "PASSWORD": 1
        }, {
                "ACCOUNT": "a",
                "PASSWORD": "\0"
        }):
            with self.subTest(values_keys=list(values)), self.assertRaises(
                    credentials.CredentialError):
                credentials.validate_values(self.script, values)

    def test_cipher_context_mutations_and_authentication(self):
        env = {"ACCOUNT": "秘密", "PASSWORD": " pass "}
        token = self.cipher.encrypt(env, **self.context)
        self.assertNotIn("秘密", token)
        self.assertEqual(self.cipher.decrypt(token, **self.context), env)
        self.assertNotEqual(token, self.cipher.encrypt(env, **self.context))
        for changes in ({
                "identity": permissions.Identity("other", "123")
        }, {
                "identity": permissions.Identity("qq-main", "456")
        }, {
                "script_id": "other"
        }, {
                "fields_hash": script(("PASSWORD", "ACCOUNT")).fields_hash
        }):
            context = {**self.context, **changes}
            with self.subTest(changes=changes), self.assertRaises(
                    credentials.CredentialError):
                self.cipher.decrypt(token, **context)
        for changed in ("v2." + token.split(".")[1], token[:-4] + "AAAA",
                        "garbage", token + "="):
            with self.assertRaises(credentials.CredentialError):
                self.cipher.decrypt(changed, **self.context)
        renamed = replace(self.script,
                          name="new",
                          url="https://example.com/b.py",
                          dependencies=("requests",))
        self.assertEqual(
            self.cipher.decrypt(token,
                                identity=self.identity,
                                script_id=renamed.id,
                                fields_hash=renamed.fields_hash), env)

    def test_environment_master_key_is_strict_and_not_printed(self):
        key = base64.urlsafe_b64encode(b"k" * 32).decode()
        with patch.dict(os.environ, {"CLOUD_DRIVE_CHECKIN_MASTER_KEY": key}):
            self.assertEqual(
                repr(credentials.CredentialCipher.from_environment()),
                "CredentialCipher()")
        for invalid in ("", "not a key", key.rstrip("="),
                        base64.urlsafe_b64encode(b"a" * 16).decode()):
            with patch.dict(os.environ,
                            {"CLOUD_DRIVE_CHECKIN_MASTER_KEY": invalid
                            }), self.assertRaises(credentials.CredentialError):
                credentials.CredentialCipher.from_environment()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(
                credentials.CredentialError):
            credentials.CredentialCipher.from_environment()

    def test_full_control_character_payload_is_endpoint_safe(self):
        names = ["A" + str(i).zfill(2) + "X" * 125 for i in range(64)]
        declaration = script(names)
        values = {name: "\x01" * 1024 for name in names}
        env = credentials.validate_values(declaration, values)
        payload = json.dumps({
            "env": env
        },
                             ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        self.assertGreater(len(payload), 256 * 1024)
        self.assertLessEqual(len(payload), 512 * 1024)
        context = {**self.context, "fields_hash": declaration.fields_hash}
        restored = self.cipher.decrypt(self.cipher.encrypt(env, **context),
                                       **context)
        self.assertEqual(json.loads(payload)["env"], restored)
        # 独立加载 runner，以验证真实上传端点的载荷契约。
        runner = importlib.import_module("_checkin_tests.runners.bootstrap")
        self.assertEqual(
            runner.validate_environment(json.loads(payload)["env"]), env)
        for values in ({
                **env, names[0]: "\x01" * 1025
        }, {
                names[0]: "x" * 16385
        }):
            with self.assertRaises(credentials.CredentialError):
                self.cipher.encrypt(values, **context)

    def test_unicode_limits_use_actual_utf8(self):
        env = {"ACCOUNT": "中" * 5461, "PASSWORD": "b"}
        self.assertEqual(credentials.validate_values(self.script, env), env)
        with self.assertRaises(credentials.CredentialError):
            credentials.validate_values(self.script, {
                **env, "ACCOUNT": "中" * 5462
            })
