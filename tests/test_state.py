import datetime
import json
import math
import unittest

from state_support import load_module


state = load_module("state")

COLLECTION_FIELDS = (
    "users",
    "bindings",
    "credentials",
    "batches",
    "runs",
    "outbox",
    "cleanup_jobs",
    "cooldowns",
    "dedupe",
)
EXPECTED_FIELDS = frozenset(("schema_version", "revision", *COLLECTION_FIELDS))


def encoded_size(snapshot):
    return len(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


class StateSchemaTests(unittest.TestCase):
    def test_public_constants_and_error_hierarchy(self):
        self.assertEqual(state.STATE_KEY, "checkin_state")
        self.assertEqual(state.SCHEMA_VERSION, 1)
        self.assertEqual(state.MAX_SNAPSHOT_BYTES, 8 * 1024 * 1024)
        self.assertTrue(issubclass(state.InvalidStateError, ValueError))
        self.assertTrue(
            issubclass(state.UnsupportedSchemaError, state.InvalidStateError)
        )
        self.assertTrue(issubclass(state.StateTooLargeError, state.InvalidStateError))

    def test_new_snapshot_has_fresh_independent_collections(self):
        first = state.new_snapshot()
        second = state.new_snapshot()

        self.assertEqual(first.keys(), EXPECTED_FIELDS)
        self.assertEqual(first["schema_version"], state.SCHEMA_VERSION)
        self.assertEqual(first["revision"], 0)
        self.assertEqual(len({id(first[field]) for field in COLLECTION_FIELDS}), 9)
        for field in COLLECTION_FIELDS:
            self.assertEqual(first[field], {})
            self.assertIsNot(first[field], second[field])

        first["users"]["user-1"] = {"arbitrary": "data"}
        self.assertEqual(second["users"], {})

    def test_validate_accepts_all_json_values_without_changing_business_data(self):
        snapshot = state.new_snapshot()
        shared = {
            "text": "签到数据",
            "integer": 7,
            "float": 2.5,
            "enabled": True,
            "missing": None,
            "items": [False, 0, "值", {"nested": [1, 2, 3]}],
        }
        for index, field in enumerate(COLLECTION_FIELDS):
            snapshot[field][f"业务键-{index}"] = {
                "unconstrained": shared,
                "field_specific_data": [field, index],
            }

        state.validate_snapshot(snapshot)

        for index, field in enumerate(COLLECTION_FIELDS):
            record = snapshot[field][f"业务键-{index}"]
            self.assertIs(record["unconstrained"], shared)
            self.assertEqual(record["field_specific_data"], [field, index])

    def test_top_level_type_and_exact_fields_are_required(self):
        for invalid in (None, [], "state"):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(state.InvalidStateError):
                    state.validate_snapshot(invalid)

        missing = state.new_snapshot()
        missing.pop("users")
        with self.assertRaises(state.InvalidStateError):
            state.validate_snapshot(missing)

        unexpected = state.new_snapshot()
        secret_key = "PRIVATE-UNKNOWN-FIELD"
        unexpected[secret_key] = {}
        with self.assertRaises(state.InvalidStateError) as caught:
            state.validate_snapshot(unexpected)
        self.assertNotIn(secret_key, str(caught.exception))

    def test_schema_version_must_be_a_supported_non_boolean_integer(self):
        for invalid in (True, False, 1.0, "1", None):
            snapshot = state.new_snapshot()
            snapshot["schema_version"] = invalid
            with self.subTest(value_type=type(invalid).__name__, value=invalid):
                with self.assertRaises(state.InvalidStateError) as caught:
                    state.validate_snapshot(snapshot)
                self.assertNotIsInstance(caught.exception, state.UnsupportedSchemaError)

        for version in (-1, 0, 2, 999999):
            snapshot = state.new_snapshot()
            snapshot["schema_version"] = version
            with self.subTest(version=version):
                with self.assertRaises(state.UnsupportedSchemaError) as caught:
                    state.validate_snapshot(snapshot)
                self.assertNotIn(str(version), str(caught.exception))
                self.assertEqual(snapshot["schema_version"], version)

    def test_revision_must_be_a_non_negative_non_boolean_integer(self):
        for invalid in (-1, True, False, 1.0, "1", None):
            snapshot = state.new_snapshot()
            snapshot["revision"] = invalid
            with self.subTest(value_type=type(invalid).__name__, value=invalid):
                with self.assertRaises(state.InvalidStateError):
                    state.validate_snapshot(snapshot)

        for valid in (0, 1, 10**30):
            snapshot = state.new_snapshot()
            snapshot["revision"] = valid
            with self.subTest(valid=valid):
                state.validate_snapshot(snapshot)

    def test_each_collection_must_be_a_dictionary(self):
        for field in COLLECTION_FIELDS:
            snapshot = state.new_snapshot()
            snapshot[field] = []
            with self.subTest(field=field):
                with self.assertRaises(state.InvalidStateError):
                    state.validate_snapshot(snapshot)
                self.assertEqual(snapshot[field], [])

    def test_non_json_values_and_non_string_keys_are_rejected(self):
        invalid_values = (
            b"bytes",
            ("tuple",),
            datetime.datetime(2026, 1, 1),
            object(),
        )
        for invalid in invalid_values:
            snapshot = state.new_snapshot()
            snapshot["users"]["record"] = invalid
            with self.subTest(value_type=type(invalid).__name__):
                with self.assertRaises(state.InvalidStateError):
                    state.validate_snapshot(snapshot)

        snapshot = state.new_snapshot()
        snapshot["users"][7] = "value"
        with self.assertRaises(state.InvalidStateError):
            state.validate_snapshot(snapshot)

    def test_non_finite_floats_are_rejected_at_any_depth(self):
        for invalid in (math.nan, math.inf, -math.inf):
            snapshot = state.new_snapshot()
            snapshot["runs"]["run"] = {"nested": [invalid]}
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(state.InvalidStateError):
                    state.validate_snapshot(snapshot)

    def test_circular_containers_are_rejected_but_shared_values_are_allowed(self):
        circular_list = []
        circular_list.append(circular_list)
        snapshot = state.new_snapshot()
        snapshot["users"]["cycle"] = circular_list
        with self.assertRaises(state.InvalidStateError):
            state.validate_snapshot(snapshot)

        circular_dict = {}
        circular_dict["self"] = circular_dict
        snapshot = state.new_snapshot()
        snapshot["users"]["cycle"] = circular_dict
        with self.assertRaises(state.InvalidStateError):
            state.validate_snapshot(snapshot)

        shared = {"valid": [1, 2, 3]}
        snapshot = state.new_snapshot()
        snapshot["users"]["first"] = shared
        snapshot["bindings"]["second"] = shared
        state.validate_snapshot(snapshot)

    def test_invalid_unicode_is_rejected(self):
        invalid_unicode = "\ud800"

        snapshot = state.new_snapshot()
        snapshot["users"]["record"] = invalid_unicode
        with self.assertRaises(state.InvalidStateError):
            state.validate_snapshot(snapshot)

        snapshot = state.new_snapshot()
        snapshot["users"][invalid_unicode] = "record"
        with self.assertRaises(state.InvalidStateError):
            state.validate_snapshot(snapshot)

    def test_validation_errors_do_not_echo_user_content(self):
        marker = "DO-NOT-ECHO-PRIVATE-CONTENT"
        snapshot = state.new_snapshot()
        snapshot["users"][marker] = object()

        with self.assertRaises(state.InvalidStateError) as caught:
            state.validate_snapshot(snapshot)

        self.assertNotIn(marker, str(caught.exception))

    def test_utf8_size_limit_allows_exact_boundary_and_rejects_one_more_byte(self):
        snapshot = state.new_snapshot()
        snapshot["users"]["payload"] = ""
        remaining_bytes = state.MAX_SNAPSHOT_BYTES - encoded_size(snapshot)
        multibyte_count, ascii_count = divmod(remaining_bytes, len("界".encode()))
        snapshot["users"]["payload"] = "界" * multibyte_count + "x" * ascii_count

        self.assertEqual(encoded_size(snapshot), state.MAX_SNAPSHOT_BYTES)
        state.validate_snapshot(snapshot)

        snapshot["users"]["payload"] += "x"
        with self.assertRaises(state.StateTooLargeError):
            state.validate_snapshot(snapshot)
        self.assertTrue(snapshot["users"]["payload"].endswith("x"))

    def test_custom_size_limit_uses_utf8_bytes_not_character_count(self):
        snapshot = state.new_snapshot()
        snapshot["users"]["payload"] = "云"
        exact_size = encoded_size(snapshot)

        state.validate_snapshot(snapshot, max_bytes=exact_size)
        with self.assertRaises(state.StateTooLargeError):
            state.validate_snapshot(snapshot, max_bytes=exact_size - 1)


if __name__ == "__main__":
    unittest.main()
