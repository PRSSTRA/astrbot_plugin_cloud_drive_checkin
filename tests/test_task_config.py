import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from state_support import load_module

_COMMIT = "1a2b3c4d5e6f78900123456789abcdef01234567"
_SHA256 = "0123456789abcdef" * 4


def _field(field_id="account", env_name="CHECKIN_ACCOUNT", **updates):
    raw = {
        "field_id": field_id,
        "label": "Account",
        "env_name": env_name,
        "value_type": "string",
        "required": True,
        "secret": True,
        "hint": "Configured by the user",
    }
    raw.update(updates)
    return raw


def _task(**updates):
    raw = {
        "task_id": "example_task",
        "name": "Example task",
        "enabled": False,
        "runtime": "python",
        "script_url": (
            "https://raw.githubusercontent.com/example/project/"
            f"{_COMMIT}/scripts/checkin.py"
        ),
        "source_commit": _COMMIT,
        "sha256": _SHA256,
        "cron": "15 7 * * *",
        "user_fields": [_field()],
        "fixed_env": {"CHECKIN_MODE": "safe"},
        "input_adapter": "direct_env",
        "result_adapter": "json_protocol",
        "dependency_bundle": "none",
        "timeout_seconds": 0,
        "experimental": False,
    }
    raw.update(updates)
    return raw


def _mobile_fields():
    return [
        _field("authorization", "MOBILE_AUTHORIZATION"),
        _field("phone", "MOBILE_PHONE"),
        _field("auth_token", "MOBILE_AUTH_TOKEN"),
    ]


class TaskConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = "_checkin_task_config_tests"
        cls.config = load_module("config", package)
        cls.task_config = load_module("task_config", package)

    def parse_one(self, task=None, **catalog_updates):
        raw = {"tasks": [_task() if task is None else task]}
        raw.update(catalog_updates)
        return self.task_config.parse_task_catalog(raw).tasks[0]

    def test_catalog_and_entries_are_frozen_and_input_is_not_mutated(self):
        raw = {"timezone": "Asia/Shanghai", "tasks": [_task()]}
        original = copy.deepcopy(raw)

        catalog = self.task_config.parse_task_catalog(raw)

        self.assertEqual(raw, original)
        with self.assertRaises(FrozenInstanceError):
            catalog.timezone = "UTC"
        with self.assertRaises(FrozenInstanceError):
            catalog.tasks[0].enabled = True
        with self.assertRaises(FrozenInstanceError):
            catalog.tasks[0].user_fields[0].secret = False

    def test_json_user_fields_match_direct_list_and_version(self):
        fields = [
            _field("first", "CHECKIN_FIRST"),
            _field("second", "CHECKIN_SECOND", value_type="bool", secret=False),
        ]

        direct = self.parse_one(_task(user_fields=fields))
        encoded = self.parse_one(
            _task(user_fields=json.dumps(fields, ensure_ascii=False))
        )

        self.assertEqual(direct.user_fields, encoded.user_fields)
        self.assertEqual(direct.version, encoded.version)

    def test_json_user_fields_utf8_size_boundary(self):
        at_limit = "[]" + " " * (64 * 1024 - 2)

        parsed = self.parse_one(_task(user_fields=at_limit))

        self.assertEqual(parsed.user_fields, ())
        self.assertEqual(
            parsed.version,
            self.parse_one(_task(user_fields=[])).version,
        )
        with self.assertRaises(self.config.PluginConfigError) as raised:
            self.parse_one(_task(user_fields=at_limit + " "))
        self.assertIsNone(raised.exception.__cause__)

    def test_invalid_non_array_and_overdeep_json_use_safe_errors(self):
        invalid_json_values = (
            "{",
            "{}",
            "[NaN]",
            "[" * 33 + "]" * 33,
            "\ud800",
        )

        for user_fields in invalid_json_values:
            with self.subTest(user_fields=repr(user_fields)):
                with self.assertRaises(self.config.PluginConfigError) as raised:
                    self.parse_one(_task(user_fields=user_fields))
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(user_fields, str(raised.exception))

    def test_sunday_zero_and_seven_produce_the_same_runs(self):
        sunday_zero = self.parse_one(_task(cron="0 0 * * 0"))
        sunday_seven = self.parse_one(_task(cron="0 0 * * 7"))
        now = datetime(2024, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

        zero_runs = self.task_config.next_run_times(
            sunday_zero, "Asia/Shanghai", now=now
        )
        seven_runs = self.task_config.next_run_times(
            sunday_seven, "Asia/Shanghai", now=now
        )

        self.assertEqual(zero_runs, seven_runs)
        self.assertEqual(
            [run.date().isoformat() for run in zero_runs],
            ["2024-01-07", "2024-01-14", "2024-01-21"],
        )
        self.assertTrue(all(run.utcoffset() is not None for run in zero_runs))

    def test_day_of_month_and_weekday_use_or_semantics(self):
        task = self.parse_one(_task(cron="0 9 13 * MON"))
        now = datetime(2024, 4, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

        runs = self.task_config.next_run_times(task, "Asia/Shanghai", now=now)

        self.assertEqual(
            [run.isoformat() for run in runs],
            [
                "2024-04-08T09:00:00+08:00",
                "2024-04-13T09:00:00+08:00",
                "2024-04-15T09:00:00+08:00",
            ],
        )

    def test_day_or_merge_deduplicates_matching_instants(self):
        task = self.parse_one(_task(cron="0 9 1 * MON"))
        now = datetime(2024, 3, 31, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

        runs = self.task_config.next_run_times(task, "Asia/Shanghai", now=now)

        self.assertEqual(
            [run.date().isoformat() for run in runs],
            ["2024-04-01", "2024-04-08", "2024-04-15"],
        )

    def test_impossible_month_day_is_valid_when_weekday_or_can_run(self):
        task = self.parse_one(_task(cron="0 9 31 2 MON"))
        now = datetime(2026, 11, 2, tzinfo=ZoneInfo("Asia/Shanghai"))

        runs = self.task_config.next_run_times(task, "Asia/Shanghai", now=now)

        self.assertEqual(
            [run.isoformat() for run in runs],
            [
                "2027-02-01T09:00:00+08:00",
                "2027-02-08T09:00:00+08:00",
                "2027-02-15T09:00:00+08:00",
            ],
        )

    def test_expression_without_any_future_date_is_rejected(self):
        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(_task(cron="0 9 31 2 *"))

    def test_next_runs_convert_reference_time_to_catalog_timezone(self):
        task = self.parse_one(_task(cron="0 9 * * *"))
        now = datetime(2024, 1, 1, 2, tzinfo=timezone.utc)

        runs = self.task_config.next_run_times(task, "Asia/Shanghai", now=now)

        self.assertEqual(runs[0].isoformat(), "2024-01-02T09:00:00+08:00")
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(run.tzinfo is not None for run in runs))

    def test_next_runs_require_an_aware_reference_time(self):
        task = self.parse_one()

        with self.assertRaises(self.config.PluginConfigError):
            self.task_config.next_run_times(
                task, "Asia/Shanghai", now=datetime(2024, 1, 1)
            )

    def test_only_regular_five_field_cron_is_accepted(self):
        invalid_expressions = (
            "@daily",
            "0 0 0 * * *",
            "0 0 * * * 2026",
            "H 0 * * *",
            "R 0 * * *",
            "0 0 L * *",
            "0 0 1W * *",
            "0 0 * * MON#2",
            "0 0 32 * MON",
            "0 0 1 13 MON",
        )

        for cron_expression in invalid_expressions:
            with (
                self.subTest(cron_expression=cron_expression),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(cron=cron_expression))

    def test_mobile_identity_or_adapters_can_never_be_enabled(self):
        enabled_mobile_variants = (
            _task(
                task_id="mobile_cloud_experimental",
                enabled=True,
                experimental=True,
            ),
            _task(
                enabled=True,
                runtime="node",
                input_adapter="mobile_single_account",
                result_adapter="mobile_unverified",
                user_fields=_mobile_fields(),
                dependency_bundle="",
                experimental=True,
            ),
        )

        for task in enabled_mobile_variants:
            with (
                self.subTest(task_id=task["task_id"]),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(task)

    def test_mobile_identity_or_adapters_must_be_experimental(self):
        disabled_mobile_variants = (
            _task(task_id="mobile_cloud_experimental", experimental=False),
            _task(
                runtime="node",
                input_adapter="mobile_single_account",
                result_adapter="mobile_unverified",
                user_fields=_mobile_fields(),
                dependency_bundle="",
                experimental=False,
            ),
        )

        for task in disabled_mobile_variants:
            with (
                self.subTest(task_id=task["task_id"]),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(task)

    def test_adapter_specific_credentials_are_required_and_secret(self):
        tianyi = _task(
            input_adapter="tianyi_account",
            result_adapter="tianyi",
            dependency_bundle="",
            user_fields=[
                _field("username", "TY_USERNAME"),
                _field("password", "TY_PASSWORD", secret=False),
            ],
        )
        mobile = _task(
            runtime="node",
            input_adapter="mobile_single_account",
            result_adapter="mobile_unverified",
            dependency_bundle="",
            user_fields=_mobile_fields()[:-1],
            experimental=True,
        )

        for task in (tianyi, mobile):
            with (
                self.subTest(adapter=task["input_adapter"]),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(task)

    def test_blob_source_is_normalized_to_raw_source(self):
        blob_url = (
            "https://github.com:443/example/project/blob/"
            f"{_COMMIT.upper()}/scripts/checkin.py"
        )

        task = self.parse_one(
            _task(
                script_url=blob_url,
                source_commit=_COMMIT.upper(),
                sha256=_SHA256.upper(),
            )
        )

        self.assertEqual(
            task.script_url,
            "https://raw.githubusercontent.com/example/project/"
            f"{_COMMIT}/scripts/checkin.py",
        )
        self.assertEqual(task.source_commit, _COMMIT)
        self.assertEqual(task.sha256, _SHA256)

    def test_dependency_bundle_is_fixed_and_canonical_in_task_version(self):
        bundle_commit = "2" * 40
        bundle_sha256 = "ABCDEF0123456789" * 4
        blob_url = (
            "https://github.com/example/dependencies/blob/"
            f"{bundle_commit.upper()}/bundles/python.zip"
        )
        raw_url = (
            "https://raw.githubusercontent.com/example/dependencies/"
            f"{bundle_commit}/bundles/python.zip"
        )
        first_declaration = json.dumps(
            {"sha256": bundle_sha256, "url": blob_url, "format": "zip-v1"},
            indent=2,
        )
        second_declaration = json.dumps(
            {"format": "zip-v1", "url": raw_url, "sha256": bundle_sha256.lower()},
            separators=(",", ":"),
        )

        first = self.parse_one(_task(dependency_bundle=first_declaration))
        second = self.parse_one(_task(dependency_bundle=second_declaration))

        expected = json.dumps(
            {"format": "zip-v1", "sha256": bundle_sha256.lower(), "url": raw_url},
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(first.dependency_bundle, expected)
        self.assertEqual(first.version, second.version)

    def test_generic_dependency_mode_is_explicit_and_strict(self):
        bundle_commit = "2" * 40
        url = (
            "https://raw.githubusercontent.com/example/dependencies/"
            f"{bundle_commit}/bundle.zip"
        )
        valid = {"format": "zip-v1", "url": url, "sha256": "3" * 64}
        invalid_declarations = (
            "",
            "requests",
            json.dumps({**valid, "format": "zip-v2"}),
            json.dumps({**valid, "url": url.replace(bundle_commit, "main")}),
            json.dumps({**valid, "url": url + "?token=private"}),
            json.dumps({**valid, "url": url.replace("https://", "http://")}),
            json.dumps({**valid, "sha256": "3" * 63}),
            '{"format":"zip-v1","format":"zip-v1","url":"'
            + url
            + '","sha256":"'
            + "3" * 64
            + '"}',
        )

        self.assertEqual(
            self.parse_one(_task(dependency_bundle="none")).dependency_bundle, "none"
        )
        for declaration in invalid_declarations:
            with (
                self.subTest(declaration=declaration),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(dependency_bundle=declaration))

    def test_unsafe_or_unpinned_sources_are_rejected_without_echoing_them(self):
        raw_path = f"example/project/{_COMMIT}/scripts/checkin.py"
        github_path = f"example/project/blob/{_COMMIT}/scripts/checkin.py"
        invalid_sources = (
            f"http://raw.githubusercontent.com/{raw_path}",
            f"https://user@raw.githubusercontent.com/{raw_path}",
            f"https://raw.githubusercontent.com:444/{raw_path}",
            f"https://raw.githubusercontent.com/{raw_path}?private-token",
            f"https://raw.githubusercontent.com/{raw_path}#private-token",
            f"https://raw.githubusercontent.com/{raw_path}?",
            f"https://raw.githubusercontent.com/{raw_path}#",
            f"https://github.com/{github_path}/../other.py",
            f"https://github.com/example/project/blob/{_COMMIT}/%2e%2e/other.py",
            f"https://github.com/example/project/blob/{_COMMIT}/%252e%252e/other.py",
            f"https://github.com/example/project/blob/{_COMMIT}/%00/other.py",
            f"https://github.com/example/project/blob/{_COMMIT}/%ZZ/other.py",
            f"https://example.com/{raw_path}",
        )

        for script_url in invalid_sources:
            with self.subTest(script_url=script_url):
                with self.assertRaises(self.config.PluginConfigError) as raised:
                    self.parse_one(_task(script_url=script_url))
                self.assertNotIn("private-token", str(raised.exception))
                self.assertNotIn(script_url, str(raised.exception))

        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(_task(source_commit="f" * 40))

    def test_content_version_uses_normalized_values_and_ignores_mapping_order(self):
        original = _task(
            cron="5 7 * JAN MON",
            user_fields=[_field(env_name="CHECKIN_ACCOUNT", __template_key="field")],
            fixed_env={"CHECKIN_MODE": "safe", "CHECKIN_REGION": "east"},
            __template_key="task",
        )
        reordered = dict(reversed(list(original.items())))
        reordered["script_url"] = (
            "https://github.com/example/project/blob/"
            f"{_COMMIT.upper()}/scripts/checkin.py"
        )
        reordered["source_commit"] = _COMMIT.upper()
        reordered["sha256"] = _SHA256.upper()
        reordered["cron"] = "5\t7 * jan mon"
        reordered["user_fields"] = [
            dict(
                reversed(
                    list(
                        _field(
                            env_name="CHECKIN_ACCOUNT",
                            __template_key="different-field-metadata",
                        ).items()
                    )
                )
            )
        ]
        reordered["fixed_env"] = {
            "CHECKIN_REGION": "east",
            "CHECKIN_MODE": "safe",
        }
        reordered["__template_key"] = "different-task-metadata"

        first = self.parse_one(original)
        second = self.parse_one(reordered)

        self.assertEqual(first.version, second.version)
        self.assertEqual(first, second)

    def test_content_version_changes_for_business_values_and_field_order(self):
        first_field = _field("first", "CHECKIN_FIRST")
        second_field = _field("second", "CHECKIN_SECOND")
        original = self.parse_one(_task(user_fields=[first_field, second_field]))
        renamed = self.parse_one(
            _task(name="Renamed task", user_fields=[first_field, second_field])
        )
        reordered_fields = self.parse_one(
            _task(user_fields=[second_field, first_field])
        )

        self.assertEqual(original.task_id, renamed.task_id)
        self.assertNotEqual(original.version, renamed.version)
        self.assertNotEqual(original.version, reordered_fields.version)

    def test_versions_use_effective_timeout(self):
        inherited_zero = self.parse_one(_task(timeout_seconds=0), timeout_seconds=120)
        missing_timeout_raw = _task()
        missing_timeout_raw.pop("timeout_seconds")
        inherited_missing = self.parse_one(missing_timeout_raw, timeout_seconds=120)
        inherited_other_global = self.parse_one(
            _task(timeout_seconds=0), timeout_seconds=121
        )
        explicit = self.parse_one(_task(timeout_seconds=120), timeout_seconds=121)

        self.assertEqual(inherited_zero.timeout_seconds, 120)
        self.assertEqual(inherited_zero.version, inherited_missing.version)
        self.assertNotEqual(inherited_zero.version, inherited_other_global.version)
        self.assertEqual(inherited_zero.version, explicit.version)

    def test_timezone_is_part_of_content_version(self):
        shanghai = self.parse_one(timezone="Asia/Shanghai")
        utc = self.parse_one(timezone="UTC")

        self.assertNotEqual(shanghai.version, utc.version)

    def test_duplicate_field_ids_or_user_environment_names_are_rejected(self):
        conflicts = (
            [
                _field("duplicate", "CHECKIN_ONE"),
                _field("duplicate", "CHECKIN_TWO"),
            ],
            [
                _field("first", "checkin_shared"),
                _field("second", "CHECKIN_SHARED"),
            ],
        )

        for fields in conflicts:
            with (
                self.subTest(fields=fields),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(user_fields=fields))

    def test_fixed_and_user_environment_names_cannot_overlap(self):
        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(
                _task(
                    user_fields=[_field(env_name="checkin_shared")],
                    fixed_env={"CHECKIN_SHARED": "constant"},
                )
            )

    def test_declared_environment_spelling_and_values_are_preserved(self):
        task = self.parse_one(
            _task(
                user_fields=[_field(env_name="account_token")],
                fixed_env={"custom_option": "  quoted ';\nvalue  "},
            )
        )
        self.assertEqual(task.user_fields[0].env_name, "account_token")
        self.assertEqual(task.fixed_env, (("custom_option", "  quoted ';\nvalue  "),))
        uppercase = self.parse_one(
            _task(
                user_fields=[_field(env_name="ACCOUNT_TOKEN")],
                fixed_env={"custom_option": "  quoted ';\nvalue  "},
            )
        )
        self.assertNotEqual(task.version, uppercase.version)
        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(_task(user_fields=[_field(env_name="paß")]))

    def test_environment_declaration_matches_runner_capacity(self):
        required_fields = [
            _field("token", "TOKEN"),
            _field("count", "COUNT", value_type="int"),
            _field("enabled", "ENABLED", value_type="bool"),
        ]
        fixed_at_combined_limit = {
            "FIXED_0": "x" * (16 * 1024),
            "FIXED_1": "x" * (16 * 1024),
            "FIXED_2": "x" * (16 * 1024),
            "FIXED_3": "x" * (16 * 1024 - 6),
        }
        parsed = self.parse_one(
            _task(user_fields=required_fields, fixed_env=fixed_at_combined_limit)
        )
        self.assertEqual(len(parsed.fixed_env), 4)

        over_combined_limit = dict(fixed_at_combined_limit)
        over_combined_limit["FIXED_3"] += "x"
        invalid_tasks = (
            _task(user_fields=required_fields, fixed_env=over_combined_limit),
            _task(fixed_env={"FIXED": "x" * (16 * 1024 + 1)}),
            _task(fixed_env={"FIXED": "contains\0nul"}),
            _task(fixed_env={"FIXED": "汉" * 5462}),
            _task(user_fields=[_field(env_name="A" * (16 * 1024 + 1))]),
            _task(
                user_fields=[
                    _field(f"field_{index}", f"USER_{index}") for index in range(32)
                ],
                fixed_env={f"FIXED_{index}": "" for index in range(33)},
            ),
        )
        for task in invalid_tasks:
            with (
                self.subTest(names=tuple(task.get("fixed_env", {}))),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(task)

        longest_name = "A" * (16 * 1024)
        unicode_value = "汉" * 5461
        bounded = self.parse_one(
            _task(
                user_fields=[_field(env_name=longest_name)],
                fixed_env={"FIXED": unicode_value},
            )
        )
        self.assertEqual(bounded.user_fields[0].env_name, longest_name)
        self.assertEqual(dict(bounded.fixed_env)["FIXED"], unicode_value)

    def test_invalid_unicode_is_rejected_at_configuration_boundary(self):
        for change in (
            {"name": "bad\ud800"},
            {"fixed_env": {"CHECKIN_MODE": "bad\ud800"}},
            {"script_url": _task()["script_url"] + "\ud800"},
        ):
            with self.subTest(field=next(iter(change))):
                with self.assertRaises(self.config.PluginConfigError) as raised:
                    self.parse_one(_task(**change))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_executor_environment_names_are_rejected_case_insensitively(self):
        forbidden_names = (
            "path",
            "HOME",
            "Shell",
            "env",
            "bash_env",
            "ifs",
            "pythonpath",
            "node_options",
            "ld_preload",
            "dyld_library_path",
        )

        for env_name in forbidden_names:
            with (
                self.subTest(env_name=env_name),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(user_fields=[_field(env_name=env_name)]))

    def test_catalog_root_task_collection_and_timezone_are_validated(self):
        for raw in (None, [], (), "", 1, False):
            with (
                self.subTest(raw=raw),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.task_config.parse_task_catalog(raw)

        with self.assertRaises(self.config.PluginConfigError):
            self.task_config.parse_task_catalog({"tasks": ()})
        with self.assertRaises(self.config.PluginConfigError):
            self.task_config.parse_task_catalog(
                {"tasks": [], "timezone": "Not/A_Timezone"}
            )

    def test_task_enums_and_boolean_flags_are_strict(self):
        invalid_tasks = (
            _task(enabled=1),
            _task(experimental="false"),
            _task(runtime="shell"),
            _task(input_adapter="unknown"),
            _task(result_adapter="unknown"),
            _task(user_fields=[_field(value_type="number")]),
        )

        for task in invalid_tasks:
            with (
                self.subTest(task=task),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(task)

    def test_fixed_environment_names_are_unique_after_normalization(self):
        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(
                _task(fixed_env={"checkin_mode": "one", "CHECKIN_MODE": "two"})
            )

    def test_script_digest_requires_exactly_64_hex_characters(self):
        for digest in ("f" * 63, "f" * 65, "g" * 64):
            with (
                self.subTest(digest=digest),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(sha256=digest))

    def test_timeout_boundaries_and_strict_integer_types(self):
        for timeout_seconds in (1, 240):
            with self.subTest(scope="catalog", timeout_seconds=timeout_seconds):
                task = self.parse_one(timeout_seconds=timeout_seconds)
                self.assertEqual(task.timeout_seconds, timeout_seconds)
            with self.subTest(scope="task", timeout_seconds=timeout_seconds):
                task = self.parse_one(_task(timeout_seconds=timeout_seconds))
                self.assertEqual(task.timeout_seconds, timeout_seconds)

        for invalid_timeout in (0, 241, True, 1.5, "180"):
            with (
                self.subTest(scope="catalog", timeout_seconds=invalid_timeout),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.task_config.parse_task_catalog(
                    {"tasks": [], "timeout_seconds": invalid_timeout}
                )
        for invalid_timeout in (-1, 241, True, 1.5, "180"):
            with (
                self.subTest(scope="task", timeout_seconds=invalid_timeout),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(timeout_seconds=invalid_timeout))

    def test_task_and_field_count_boundaries(self):
        thirty_two_tasks = [_task(task_id=f"task_{index}") for index in range(32)]
        parsed = self.task_config.parse_task_catalog({"tasks": thirty_two_tasks})
        self.assertEqual(len(parsed.tasks), 32)

        with self.assertRaises(self.config.PluginConfigError):
            self.task_config.parse_task_catalog(
                {"tasks": thirty_two_tasks + [_task(task_id="task_overflow")]}
            )

        thirty_two_fields = [
            _field(f"field_{index}", f"CHECKIN_FIELD_{index}") for index in range(32)
        ]
        parsed_task = self.parse_one(_task(user_fields=thirty_two_fields))
        self.assertEqual(len(parsed_task.user_fields), 32)

        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(
                _task(
                    user_fields=thirty_two_fields
                    + [_field("field_overflow", "CHECKIN_FIELD_OVERFLOW")]
                )
            )

    def test_identifier_length_boundaries_and_duplicates(self):
        longest_identifier = "a" + "1" * 63
        self.assertEqual(
            self.parse_one(_task(task_id=longest_identifier)).task_id,
            longest_identifier,
        )
        self.assertEqual(
            self.parse_one(
                _task(user_fields=[_field(longest_identifier, "CHECKIN_LONG")])
            )
            .user_fields[0]
            .field_id,
            longest_identifier,
        )

        for task_id in ("1task", "Task", "a" + "1" * 64):
            with (
                self.subTest(task_id=task_id),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(task_id=task_id))

        duplicate = _task(task_id="duplicate")
        with self.assertRaises(self.config.PluginConfigError):
            self.task_config.parse_task_catalog(
                {"tasks": [duplicate, copy.deepcopy(duplicate)]}
            )

    def test_field_flags_are_strict_booleans_and_fixed_values_are_strings(self):
        for flag in ("required", "secret"):
            with (
                self.subTest(flag=flag),
                self.assertRaises(self.config.PluginConfigError),
            ):
                self.parse_one(_task(user_fields=[_field(**{flag: 1})]))

        with self.assertRaises(self.config.PluginConfigError):
            self.parse_one(_task(fixed_env={"CHECKIN_MODE": 1}))


if __name__ == "__main__":
    unittest.main()
