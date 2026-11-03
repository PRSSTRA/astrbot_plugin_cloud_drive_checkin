import base64
import copy
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from state_support import load_module

_PACKAGE = "_checkin_credentials_tests"
credentials = load_module("credentials", _PACKAGE)
permissions = load_module("permissions", _PACKAGE)
storage = load_module("storage", _PACKAGE)
task_config = load_module("task_config", _PACKAGE)


class FakeKV:
    def __init__(self):
        self.value = None
        self.write_values = []
        self.fail_next_write = False

    async def get_kv_data(self, _key, default):
        return default if self.value is None else copy.deepcopy(self.value)

    async def put_kv_data(self, _key, value):
        detached = copy.deepcopy(value)
        self.write_values.append(detached)
        if self.fail_next_write:
            self.fail_next_write = False
            raise OSError("simulated storage detail")
        self.value = detached


def _field(
    field_id="account",
    *,
    value_type="string",
    required=True,
    secret=True,
):
    return task_config.UserField(
        field_id=field_id,
        label=field_id.title(),
        env_name=f"CHECKIN_{field_id.upper()}",
        value_type=value_type,
        required=required,
        secret=secret,
        hint="",
    )


def _task(*, task_id="example_task", version="task-version-1", fields=None):
    if fields is None:
        fields = (
            _field(),
            _field("attempts", value_type="int", required=False, secret=False),
            _field("enabled", value_type="bool", required=False, secret=False),
            _field("note", required=False, secret=False),
        )
    return task_config.TaskConfig(
        task_id=task_id,
        name="Example task",
        enabled=True,
        runtime="python",
        script_url="https://raw.githubusercontent.com/example/project/script.py",
        source_commit="1" * 40,
        sha256="2" * 64,
        cron="0 0 * * *",
        user_fields=tuple(fields),
        fixed_env=(),
        input_adapter="direct_env",
        result_adapter="json_protocol",
        dependency_bundle="none",
        timeout_seconds=0,
        experimental=False,
        version=version,
    )


class CredentialCipherTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.cipher = credentials.CredentialCipher(self.key)
        self.identity = permissions.Identity("bot-instance", "10001")
        self.binding = {
            "identity": self.identity,
            "task_id": "example_task",
            "task_version": "task-version-1",
        }

    def test_special_text_and_all_scalar_types_round_trip_without_normalization(self):
        values = {
            "account": "  中文; 'quoted' \"double\"\nline two\t\\tail  ",
            "attempts": -42,
            "enabled": False,
            "empty": "",
        }

        first = self.cipher.encrypt(values, **self.binding)
        second = self.cipher.encrypt(values, **self.binding)

        self.assertTrue(first.startswith("v1."))
        self.assertNotEqual(first, second)
        self.assertEqual(self.cipher.decrypt(first, **self.binding), values)
        self.assertEqual(self.cipher.decrypt(second, **self.binding), values)
        encoded = first.split(".", 1)[1]
        self.assertEqual(
            base64.urlsafe_b64encode(base64.urlsafe_b64decode(encoded)).decode(),
            encoded,
        )
        self.assertNotIn(values["account"], first)

    def test_corruption_wrong_key_and_binding_substitution_fail_closed(self):
        token = self.cipher.encrypt({"account": "do-not-disclose"}, **self.binding)
        encoded = token.split(".", 1)[1]
        corrupted_bytes = bytearray(base64.urlsafe_b64decode(encoded))
        corrupted_bytes[-1] ^= 1
        corrupted = "v1." + base64.urlsafe_b64encode(corrupted_bytes).decode("ascii")

        attempts = (
            (self.cipher, corrupted, self.binding),
            (credentials.CredentialCipher(b"z" * 32), token, self.binding),
            (
                self.cipher,
                token,
                {
                    **self.binding,
                    "identity": permissions.Identity("bot-instance", "10002"),
                },
            ),
            (self.cipher, token, {**self.binding, "task_id": "other_task"}),
            (self.cipher, token, {**self.binding, "task_version": "task-version-2"}),
        )
        for cipher, candidate, binding in attempts:
            with self.subTest(candidate=candidate[:20], binding=binding):
                with self.assertRaises(credentials.CredentialError) as raised:
                    cipher.decrypt(candidate, **binding)
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn("do-not-disclose", str(raised.exception))

    def test_malformed_envelopes_and_non_scalar_plaintext_are_rejected_safely(self):
        malformed = (
            "",
            "v1",
            "v2.AAAA",
            "v1.AAAA",
            "v1.@@@=",
            "v1.Zm8",
            "v1.Zm8=.extra",
            123,
        )
        for token in malformed:
            with self.subTest(token=token):
                with self.assertRaises(credentials.CredentialError) as raised:
                    self.cipher.decrypt(token, **self.binding)
                self.assertIsNone(raised.exception.__cause__)

        invalid_values = (
            {"field": None},
            {"field": 1.5},
            {"field": ["nested"]},
            {1: "non-string key"},
            {"field": "\ud800"},
            {"field": "has\0nul"},
        )
        for values in invalid_values:
            with self.subTest(values=repr(values)):
                with self.assertRaises(credentials.CredentialError) as raised:
                    self.cipher.encrypt(values, **self.binding)
                self.assertIsNone(raised.exception.__cause__)

    def test_authenticated_invalid_json_and_utf8_plaintext_fail_closed(self):
        nonce = b"n" * 12
        associated_data = credentials._associated_data(
            self.identity,
            self.binding["task_id"],
            self.binding["task_version"],
        )
        invalid_plaintexts = (
            b"\xff",
            b"{",
            b"[]",
            b'{"field":null}',
            b'{"field":1.25}',
            b'{"field":NaN}',
            b'{"field":"\\ud800"}',
            b'{"field":"\\u0000"}',
        )

        for plaintext in invalid_plaintexts:
            encrypted = AESGCM(self.key).encrypt(nonce, plaintext, associated_data)
            token = "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
            with self.subTest(plaintext=plaintext):
                with self.assertRaises(credentials.CredentialError) as raised:
                    self.cipher.decrypt(token, **self.binding)
                self.assertIsNone(raised.exception.__cause__)

    def test_plaintext_size_limit_is_enforced_after_json_encoding(self):
        at_limit = "x" * (512 * 1024 - len(b'{"field":""}'))
        token = self.cipher.encrypt({"field": at_limit}, **self.binding)

        self.assertEqual(
            self.cipher.decrypt(token, **self.binding),
            {"field": at_limit},
        )
        with self.assertRaises(credentials.CredentialError) as raised:
            self.cipher.encrypt({"field": at_limit + "x"}, **self.binding)
        self.assertIsNone(raised.exception.__cause__)

    def test_environment_key_requires_canonical_urlsafe_base64_of_32_bytes(self):
        raw_key = b"\xff" * 32
        encoded_key = base64.urlsafe_b64encode(raw_key).decode("ascii")
        with patch.dict(
            os.environ, {"CLOUD_DRIVE_CHECKIN_MASTER_KEY": encoded_key}, clear=True
        ):
            loaded = credentials.CredentialCipher.from_environment()
        token = loaded.encrypt({"account": "value"}, **self.binding)
        self.assertEqual(loaded.decrypt(token, **self.binding), {"account": "value"})
        self.assertNotIn(encoded_key, repr(loaded))
        self.assertNotIn(repr(raw_key), repr(loaded))

        invalid_keys = (
            "",
            encoded_key.rstrip("="),
            encoded_key.replace("_", "/"),
            f" {encoded_key}",
            base64.urlsafe_b64encode(b"x" * 31).decode("ascii"),
            "not-base64",
        )
        for invalid_key in invalid_keys:
            with self.subTest(invalid_key=invalid_key):
                with (
                    patch.dict(
                        os.environ,
                        {"CLOUD_DRIVE_CHECKIN_MASTER_KEY": invalid_key},
                        clear=True,
                    ),
                    self.assertRaises(credentials.CredentialError) as raised,
                ):
                    credentials.CredentialCipher.from_environment()
                self.assertIsNone(raised.exception.__cause__)
                if invalid_key:
                    self.assertNotIn(invalid_key, str(raised.exception))

    def test_missing_environment_key_and_invalid_constructor_key_are_safe(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(credentials.CredentialError) as raised,
        ):
            credentials.CredentialCipher.from_environment()
        self.assertIsNone(raised.exception.__cause__)

        for key in (b"short", bytearray(32), "x" * 32):
            with self.subTest(key_type=type(key)):
                with self.assertRaises(credentials.CredentialError) as raised:
                    credentials.CredentialCipher(key)
                self.assertIsNone(raised.exception.__cause__)

    def test_compact_tuple_keys_are_collision_free_across_platform_user_and_task(self):
        first = permissions.Identity("a", "bc")
        second = permissions.Identity("ab", "c")

        self.assertEqual(json.loads(credentials.user_key(first)), ["a", "bc"])
        self.assertNotEqual(credentials.user_key(first), credentials.user_key(second))
        self.assertEqual(
            json.loads(credentials.binding_key(first, "task")),
            ["a", "bc", "task"],
        )
        self.assertNotEqual(
            credentials.binding_key(first, "c/task"),
            credentials.binding_key(permissions.Identity("a", "bc/c"), "task"),
        )


class ValueValidationTests(unittest.TestCase):
    def test_declared_types_required_presence_and_optional_absence_are_strict(self):
        task = _task()
        credentials.validate_values(task, {"account": " "})
        credentials.validate_values(
            task,
            {"account": "value", "attempts": -1, "enabled": True, "note": ""},
        )

        invalid = (
            {},
            {"account": ""},
            {"account": "value", "undeclared": "injected"},
            {"account": 1},
            {"account": "value", "attempts": True},
            {"account": "value", "enabled": 1},
            {"account": "has\0nul"},
            {"account": "\ud800"},
            [("account", "not a dict")],
        )
        for values in invalid:
            with self.subTest(values=repr(values)):
                with self.assertRaises(credentials.CredentialError) as raised:
                    credentials.validate_values(task, values)
                self.assertIsNone(raised.exception.__cause__)

    def test_per_field_and_aggregate_utf8_bounds_are_exact(self):
        fields = tuple(_field(f"field_{index}", required=False) for index in range(5))
        task = _task(fields=fields)
        at_limit = {f"field_{index}": "x" * (16 * 1024) for index in range(4)}

        credentials.validate_values(task, at_limit)

        with self.assertRaises(credentials.CredentialError):
            credentials.validate_values(task, {"field_0": "界" * 5462})
        with self.assertRaises(credentials.CredentialError):
            credentials.validate_values(task, {**at_limit, "field_4": "x"})

    def test_fixed_and_user_values_share_runner_total_budget(self):
        task = replace(
            _task(fields=(_field(),)),
            fixed_env=(
                ("FIXED_0", "x" * (16 * 1024)),
                ("FIXED_1", "x" * (16 * 1024)),
                ("FIXED_2", "x" * (16 * 1024)),
                ("FIXED_3", "x" * (16 * 1024 - 4)),
            ),
        )

        credentials.validate_values(task, {"account": "xxxx"})
        with self.assertRaises(credentials.CredentialError):
            credentials.validate_values(task, {"account": "xxxxx"})


class SaveBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = FakeKV()
        self.store = storage.StateStore(self.kv)
        await self.store.initialize()
        self.cipher = credentials.CredentialCipher(bytes(range(32)))
        self.identity = permissions.Identity("bot-instance", "10001")
        self.task = _task()

    async def test_atomic_commit_contains_only_owner_metadata_and_ciphertext(self):
        guard_calls = []
        values = {
            "account": "  secret;\nsecond line  ",
            "attempts": 3,
            "enabled": True,
            "note": "public-looking value is encrypted too",
        }

        await credentials.save_binding(
            self.store,
            self.cipher,
            identity=self.identity,
            origin="origin:10001",
            task=self.task,
            values=values,
            guard=lambda: guard_calls.append("checked"),
        )

        owner_key = credentials.user_key(self.identity)
        target_key = credentials.binding_key(self.identity, self.task.task_id)
        snapshot = self.store.snapshot()
        self.assertEqual(guard_calls, ["checked", "checked"])
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(
            snapshot["users"],
            {
                owner_key: {
                    "platform_id": "bot-instance",
                    "qq_id": "10001",
                    "unified_msg_origin": "origin:10001",
                }
            },
        )
        self.assertEqual(
            set(snapshot["bindings"][target_key]),
            {
                "user_key",
                "task_id",
                "task_version",
                "paused",
                "notify_all",
                "updated_at",
            },
        )
        self.assertEqual(snapshot["bindings"][target_key]["user_key"], owner_key)
        self.assertFalse(snapshot["bindings"][target_key]["paused"])
        self.assertFalse(snapshot["bindings"][target_key]["notify_all"])
        timestamp = datetime.fromisoformat(
            snapshot["bindings"][target_key]["updated_at"]
        )
        self.assertIsNotNone(timestamp.tzinfo)
        credential_record = snapshot["credentials"][target_key]
        self.assertEqual(
            self.cipher.decrypt(
                credential_record["ciphertext"],
                identity=self.identity,
                task_id=self.task.task_id,
                task_version=self.task.version,
            ),
            values,
        )
        serialized = json.dumps(snapshot, ensure_ascii=False)
        for value in (values["account"], values["note"]):
            self.assertNotIn(value, serialized)

    async def test_same_task_is_separated_by_platform_and_qq_owner(self):
        identities = (
            self.identity,
            permissions.Identity("bot-instance", "10002"),
            permissions.Identity("other-bot", "10001"),
        )
        for index, identity in enumerate(identities):
            await credentials.save_binding(
                self.store,
                self.cipher,
                identity=identity,
                origin=f"origin:{index}",
                task=self.task,
                values={"account": f"owner-{index}"},
                guard=lambda: None,
            )

        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["users"]), 3)
        self.assertEqual(len(snapshot["bindings"]), 3)
        self.assertEqual(len(snapshot["credentials"]), 3)
        for index, identity in enumerate(identities):
            target_key = credentials.binding_key(identity, self.task.task_id)
            token = snapshot["credentials"][target_key]["ciphertext"]
            self.assertEqual(
                self.cipher.decrypt(
                    token,
                    identity=identity,
                    task_id=self.task.task_id,
                    task_version=self.task.version,
                ),
                {"account": f"owner-{index}"},
            )
            other_identity = identities[(index + 1) % len(identities)]
            with self.assertRaises(credentials.CredentialError):
                self.cipher.decrypt(
                    token,
                    identity=other_identity,
                    task_id=self.task.task_id,
                    task_version=self.task.version,
                )

    async def test_update_preserves_preferences_and_replaces_origin_version_and_values(
        self,
    ):
        await credentials.save_binding(
            self.store,
            self.cipher,
            identity=self.identity,
            origin="old-origin",
            task=self.task,
            values={"account": "old-value"},
            guard=lambda: None,
        )
        target_key = credentials.binding_key(self.identity, self.task.task_id)

        def set_preferences(snapshot):
            snapshot["bindings"][target_key]["paused"] = True
            snapshot["bindings"][target_key]["notify_all"] = True
            snapshot["users"][credentials.user_key(self.identity)]["enrollment"] = {}

        await self.store.mutate(set_preferences)
        updated_task = _task(version="task-version-2")
        await credentials.save_binding(
            self.store,
            self.cipher,
            identity=self.identity,
            origin="new-origin",
            task=updated_task,
            values={"note": "new optional", "account": "new-value"},
            guard=lambda: None,
        )

        snapshot = self.store.snapshot()
        binding = snapshot["bindings"][target_key]
        record = snapshot["credentials"][target_key]
        self.assertTrue(binding["paused"])
        self.assertTrue(binding["notify_all"])
        self.assertEqual(binding["task_version"], "task-version-2")
        user = snapshot["users"][credentials.user_key(self.identity)]
        self.assertEqual(user["unified_msg_origin"], "new-origin")
        self.assertNotIn("enrollment", user)
        self.assertEqual(record["task_version"], "task-version-2")
        self.assertEqual(
            self.cipher.decrypt(
                record["ciphertext"],
                identity=self.identity,
                task_id=updated_task.task_id,
                task_version=updated_task.version,
            ),
            {"account": "new-value", "note": "new optional"},
        )

    async def test_guard_recheck_failure_leaves_all_three_collections_unchanged(self):
        baseline = self.store.snapshot()
        calls = 0

        class GuardRejected(Exception):
            pass

        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise GuardRejected

        with self.assertRaises(GuardRejected):
            await credentials.save_binding(
                self.store,
                self.cipher,
                identity=self.identity,
                origin="origin",
                task=self.task,
                values={"account": "value"},
                guard=guard,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(self.store.snapshot(), baseline)
        self.assertFalse(self.store.has_pending)
        self.assertEqual(len(self.kv.write_values), 1)

    async def test_malformed_record_claiming_another_owner_is_not_overwritten(self):
        target_key = credentials.binding_key(self.identity, self.task.task_id)
        await self.store.mutate(
            lambda snapshot: snapshot["bindings"].__setitem__(
                target_key,
                {
                    "user_key": credentials.user_key(
                        permissions.Identity("bot-instance", "other-owner")
                    ),
                    "task_id": self.task.task_id,
                    "paused": False,
                    "notify_all": False,
                },
            )
        )
        baseline = self.store.snapshot()

        with self.assertRaises(credentials.CredentialError) as raised:
            await credentials.save_binding(
                self.store,
                self.cipher,
                identity=self.identity,
                origin="origin",
                task=self.task,
                values={"account": "value"},
                guard=lambda: None,
            )

        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(self.store.snapshot(), baseline)
        self.assertFalse(self.store.has_pending)

    async def test_uncertain_write_retains_one_retryable_snapshot_with_ciphertext_only(
        self,
    ):
        secret_values = {
            "account": "unique-secret;明文-account",
            "note": "unique-public;明文-declaration",
        }
        self.kv.fail_next_write = True

        with self.assertRaises(storage.CommitUnconfirmedError) as raised:
            await credentials.save_binding(
                self.store,
                self.cipher,
                identity=self.identity,
                origin="origin",
                task=self.task,
                values=secret_values,
                guard=lambda: None,
            )

        self.assertTrue(raised.exception.__suppress_context__)
        self.assertTrue(self.store.has_pending)
        self.assertEqual(self.store.snapshot()["credentials"], {})
        pending = self.kv.write_values[-1]
        target_key = credentials.binding_key(self.identity, self.task.task_id)
        pending_text = json.dumps(pending, ensure_ascii=False)
        for value in secret_values.values():
            self.assertNotIn(value, pending_text)
        self.assertTrue(
            pending["credentials"][target_key]["ciphertext"].startswith("v1.")
        )
        self.assertEqual(len(self.kv.write_values), 2)

        committed = await self.store.retry_pending()
        self.assertEqual(committed, pending)
        self.assertEqual(self.kv.write_values[-2:], [pending, pending])
        self.assertEqual(
            self.cipher.decrypt(
                committed["credentials"][target_key]["ciphertext"],
                identity=self.identity,
                task_id=self.task.task_id,
                task_version=self.task.version,
            ),
            secret_values,
        )
