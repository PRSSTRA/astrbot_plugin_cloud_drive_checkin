import copy
import unittest
from dataclasses import FrozenInstanceError

from state_support import load_module


_BASE_RAW = {
    "post_type": "message",
    "message_type": "private",
    "sub_type": "friend",
    "user_id": 12345,
    "sender": {"user_id": "12345"},
}
_UNSET = object()


def _raw(**updates):
    raw = copy.deepcopy(_BASE_RAW)
    raw.update(updates)
    return raw


class _MessageObject:
    def __init__(self, raw_message):
        self.raw_message = raw_message


class _Event:
    def __init__(
        self,
        *,
        platform_name="aiocqhttp",
        platform_id="account-one",
        sender_id="12345",
        raw=_UNSET,
    ):
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._sender_id = sender_id
        if raw is _UNSET:
            raw = _BASE_RAW
        self.message_obj = _MessageObject(copy.deepcopy(raw))

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id


class _ForeignPlatformEvent:
    def get_platform_name(self):
        return "other-platform"

    def get_platform_id(self):
        raise AssertionError("platform identity was read before the platform gate")

    def get_sender_id(self):
        raise AssertionError("sender identity was read before the platform gate")


class _RawRejectedEvent(_Event):
    def get_platform_id(self):
        raise AssertionError("platform identity was read before the raw event gate")

    def get_sender_id(self):
        raise AssertionError("sender identity was read before the raw event gate")


class _BrokenSenderEvent(_Event):
    def get_sender_id(self):
        raise RuntimeError("program failure")


class PermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = "_checkin_permission_tests"
        cls.config = load_module("config", package)
        cls.permissions = load_module("permissions", package)

    def test_regular_friend_private_message_produces_identity(self):
        identity = self.permissions.get_private_identity(_Event())

        self.assertEqual(
            identity,
            self.permissions.Identity(platform_id="account-one", qq_id="12345"),
        )

    def test_numeric_identity_sources_are_normalized_consistently(self):
        event = _Event(
            sender_id=7,
            raw=_raw(user_id="7", sender={"user_id": 7}),
        )

        identity = self.permissions.get_private_identity(event)

        self.assertEqual(identity.qq_id, "7")

    def test_platform_instance_is_preserved_without_trimming(self):
        identity = self.permissions.get_private_identity(
            _Event(platform_id="  account-one  ")
        )

        self.assertEqual(identity.platform_id, "  account-one  ")

    def test_other_platform_is_rejected_before_other_event_fields_are_read(self):
        self.assertIsNone(
            self.permissions.get_private_identity(_ForeignPlatformEvent())
        )

    def test_raw_event_gate_precedes_identity_fields(self):
        event = _RawRejectedEvent(raw=_raw(message_type="group", sub_type="normal"))

        self.assertIsNone(self.permissions.get_private_identity(event))

    def test_non_friend_private_contexts_are_rejected(self):
        rejected_raw_messages = (
            None,
            [],
            _raw(post_type="message_sent"),
            _raw(post_type="request"),
            _raw(message_type="group", sub_type="normal"),
            _raw(sub_type="group"),
            _raw(sub_type="other"),
            _raw(temp_source=None),
            _raw(temp_source=0),
            _raw(group_id=0),
            _raw(group_id=67890),
        )

        for raw in rejected_raw_messages:
            with self.subTest(raw=raw):
                self.assertIsNone(
                    self.permissions.get_private_identity(_Event(raw=raw))
                )

    def test_target_id_does_not_turn_friend_private_into_temporary_session(self):
        identity = self.permissions.get_private_identity(
            _Event(raw=_raw(target_id=12345))
        )

        self.assertIsNotNone(identity)
        self.assertEqual(identity.qq_id, "12345")

    def test_blank_or_non_string_platform_instances_are_rejected(self):
        for platform_id in (None, "", " ", "\t\n", 12345):
            with self.subTest(platform_id=platform_id):
                self.assertIsNone(
                    self.permissions.get_private_identity(
                        _Event(platform_id=platform_id)
                    )
                )

    def test_missing_or_malformed_raw_identity_fields_are_rejected(self):
        missing_user = _raw()
        missing_user.pop("user_id")
        missing_sender = _raw()
        missing_sender.pop("sender")
        missing_sender_user = _raw(sender={})
        malformed_raw_messages = (
            missing_user,
            missing_sender,
            missing_sender_user,
            _raw(sender=None),
            _raw(sender=[]),
            _raw(user_id="012345"),
            _raw(sender={"user_id": "１２３４５"}),
        )

        for raw in malformed_raw_messages:
            with self.subTest(raw=raw):
                self.assertIsNone(
                    self.permissions.get_private_identity(_Event(raw=raw))
                )

    def test_all_three_qq_sources_must_match(self):
        mismatched_events = (
            _Event(sender_id="54321"),
            _Event(raw=_raw(user_id="54321")),
            _Event(raw=_raw(sender={"user_id": "54321"})),
        )

        for event in mismatched_events:
            with self.subTest(event=event):
                self.assertIsNone(self.permissions.get_private_identity(event))

    def test_invalid_event_sender_id_is_rejected(self):
        for sender_id in (None, True, 0, "012345", " 12345", "１２３４５"):
            with self.subTest(sender_id=sender_id):
                self.assertIsNone(
                    self.permissions.get_private_identity(
                        _Event(sender_id=sender_id)
                    )
                )

    def test_identity_distinguishes_platform_instances_for_same_qq(self):
        first = self.permissions.get_private_identity(
            _Event(platform_id="account-one")
        )
        second = self.permissions.get_private_identity(
            _Event(platform_id="account-two")
        )

        self.assertNotEqual(first, second)
        self.assertEqual(first.qq_id, second.qq_id)

    def test_identity_and_access_are_frozen(self):
        identity = self.permissions.Identity("account-one", "12345")
        access = self.permissions.Access(identity, False)

        with self.assertRaises(FrozenInstanceError):
            identity.qq_id = "54321"
        with self.assertRaises(FrozenInstanceError):
            access.is_admin = True

    def test_framework_admin_does_not_need_whitelist_or_enabled_service(self):
        identity = self.permissions.Identity("account-one", "12345")
        config = self.config.parse_config({"enabled": False})

        access = self.permissions.authorize(
            identity,
            astrbot_admin=True,
            config=config,
        )

        self.assertEqual(access.identity, identity)
        self.assertTrue(access.is_admin)

    def test_plugin_admin_does_not_need_whitelist_or_enabled_service(self):
        identity = self.permissions.Identity("account-one", "12345")
        config = self.config.parse_config(
            {"enabled": False, "plugin_admin_qqs": [12345]}
        )

        access = self.permissions.authorize(
            identity,
            astrbot_admin=False,
            config=config,
        )

        self.assertEqual(access.identity, identity)
        self.assertTrue(access.is_admin)

    def test_whitelisted_user_gets_non_admin_access_when_service_is_disabled(self):
        identity = self.permissions.Identity("account-one", "12345")
        config = self.config.parse_config(
            {"enabled": False, "whitelist_qqs": [12345]}
        )

        access = self.permissions.authorize(
            identity,
            astrbot_admin=False,
            config=config,
        )

        self.assertEqual(access.identity, identity)
        self.assertFalse(access.is_admin)

    def test_unlisted_user_is_rejected_regardless_of_service_switch(self):
        identity = self.permissions.Identity("account-one", "12345")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                config = self.config.parse_config({"enabled": enabled})
                self.assertIsNone(
                    self.permissions.authorize(
                        identity,
                        astrbot_admin=False,
                        config=config,
                    )
                )

    def test_removing_whitelist_does_not_revoke_admin_permissions(self):
        identity = self.permissions.Identity("account-one", "12345")
        plugin_admin_config = self.config.parse_config(
            {"plugin_admin_qqs": [12345], "whitelist_qqs": []}
        )
        empty_config = self.config.parse_config({})

        plugin_access = self.permissions.authorize(
            identity,
            astrbot_admin=False,
            config=plugin_admin_config,
        )
        framework_access = self.permissions.authorize(
            identity,
            astrbot_admin=True,
            config=empty_config,
        )

        self.assertTrue(plugin_access.is_admin)
        self.assertTrue(framework_access.is_admin)

    def test_qq_lists_apply_to_each_instance_without_collapsing_identity(self):
        first = self.permissions.Identity("account-one", "12345")
        second = self.permissions.Identity("account-two", "12345")
        config = self.config.parse_config({"whitelist_qqs": [12345]})

        first_access = self.permissions.authorize(
            first,
            astrbot_admin=False,
            config=config,
        )
        second_access = self.permissions.authorize(
            second,
            astrbot_admin=False,
            config=config,
        )

        self.assertEqual(first_access.identity, first)
        self.assertEqual(second_access.identity, second)
        self.assertNotEqual(first_access.identity, second_access.identity)

    def test_raw_sender_role_does_not_grant_admin_access(self):
        event = _Event(raw=_raw(sender={"user_id": 12345, "role": "owner"}))
        identity = self.permissions.get_private_identity(event)
        config = self.config.parse_config({})

        self.assertIsNotNone(identity)
        self.assertIsNone(
            self.permissions.authorize(
                identity,
                astrbot_admin=False,
                config=config,
            )
        )

    def test_identity_extraction_does_not_swallow_program_failures(self):
        with self.assertRaisesRegex(RuntimeError, "program failure"):
            self.permissions.get_private_identity(_BrokenSenderEvent())


if __name__ == "__main__":
    unittest.main()
