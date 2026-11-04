"""验证私聊身份、授权隔离与原始文本保留契约。"""

import importlib
from pathlib import Path
import sys
import types
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if "_checkin_tests" not in sys.modules:
    package = types.ModuleType("_checkin_tests")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
permissions = importlib.import_module("_checkin_tests.permissions")
config = importlib.import_module("_checkin_tests.config")


def event(**changes):
    raw = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": 123,
        "sender": {
            "user_id": 123
        },
        "message": [{
            "type": "text",
            "data": {
                "text": "  原文\n "
            }
        }]
    }
    raw.update(changes)
    return SimpleNamespace(message_obj=SimpleNamespace(raw_message=raw),
                           get_platform_name=lambda: "aiocqhttp",
                           get_platform_id=lambda: "qq-main",
                           get_sender_id=lambda: "123",
                           message_str="原文")


class PermissionTests(unittest.TestCase):

    def test_only_normal_friend_private_consistent_identity(self):
        value = permissions.get_private_identity(event())
        self.assertEqual(value, permissions.Identity("qq-main", "123"))
        for changes in ({
                "message_type": "group"
        }, {
                "sub_type": "group"
        }, {
                "sub_type": "other"
        }, {
                "temp_source": 0
        }, {
                "group_id": 5
        }, {
                "post_type": "notice"
        }, {
                "user_id": 456
        }, {
                "sender": {
                    "user_id": 456
                }
        }, {
                "user_id": True
        }):
            with self.subTest(changes=changes):
                self.assertIsNone(
                    permissions.get_private_identity(event(**changes)))
        for getter, result in (("get_platform_name", "other"),
                               ("get_platform_id", ""), ("get_sender_id",
                                                         "0123")):
            value = event()
            setattr(value, getter, lambda: result)
            self.assertIsNone(permissions.get_private_identity(value))

    def test_authorization_only_self_key_and_framework_admin(self):
        policy = config.parse_config({"whitelist_qqs": [123]})
        identity = permissions.Identity("a", "123")
        self.assertTrue(
            permissions.authorize(identity, astrbot_admin=False, config=policy))
        self.assertFalse(
            permissions.authorize(permissions.Identity("a", "456"),
                                  astrbot_admin=False,
                                  config=policy))
        self.assertTrue(
            permissions.authorize(permissions.Identity("a", "456"),
                                  astrbot_admin=True,
                                  config=policy))
        self.assertNotEqual(
            permissions.owner_key(identity),
            permissions.owner_key(permissions.Identity("b", "123")))
        self.assertNotEqual(permissions.binding_key(identity, "one"),
                            permissions.binding_key(identity, "two"))
        self.assertNotEqual(
            permissions.owner_key(permissions.Identity('a","b', "123")),
            permissions.owner_key(permissions.Identity("a", "123")))

    def test_raw_segments_preserve_all_whitespace_and_reject_nontext(self):
        self.assertEqual(permissions.raw_private_text(event()), "  原文\n ")
        value = event(message=[{
            "type": "text",
            "data": {
                "text": "  a"
            }
        }, {
            "type": "text",
            "data": {
                "text": "#b  "
            }
        }])
        self.assertEqual(permissions.raw_private_text(value), "  a#b  ")
        for message in ("stripped", [], [{
                "type": "image",
                "data": {}
        }], [{
                "type": "text",
                "data": {
                    "text": 1
                }
        }], [{
                "type": "text",
                "data": {
                    "text": "safe"
                }
        }, {
                "type": "reply",
                "data": {}
        }]):
            self.assertIsNone(
                permissions.raw_private_text(event(message=message)))
