import copy
import unittest
from dataclasses import FrozenInstanceError

from state_support import load_module




class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_module("config", "_checkin_config_tests")

    def test_missing_fields_use_deny_by_default_configuration(self):
        parsed = self.config.parse_config({"future_option": "ignored"})

        self.assertFalse(parsed.enabled)
        self.assertEqual(parsed.plugin_admin_qqs, frozenset())
        self.assertEqual(parsed.whitelist_qqs, frozenset())

    def test_valid_qq_values_are_normalized_and_deduplicated(self):
        raw = {
            "enabled": True,
            "plugin_admin_qqs": [1, "1", 98765432101234567890],
            "whitelist_qqs": [2, "2", "30004"],
        }
        original = copy.deepcopy(raw)

        parsed = self.config.parse_config(raw)

        self.assertTrue(parsed.enabled)
        self.assertEqual(
            parsed.plugin_admin_qqs,
            frozenset({"1", "98765432101234567890"}),
        )
        self.assertEqual(parsed.whitelist_qqs, frozenset({"2", "30004"}))
        self.assertEqual(raw, original)

    def test_permission_config_is_frozen(self):
        parsed = self.config.parse_config({})

        with self.assertRaises(FrozenInstanceError):
            parsed.enabled = True

    def test_configuration_root_must_be_a_mapping(self):
        for value in (None, [], (), "", 1, False):
            with self.subTest(value=value):
                with self.assertRaises(self.config.PluginConfigError):
                    self.config.parse_config(value)

    def test_enabled_requires_a_boolean(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(self.config.PluginConfigError):
                    self.config.parse_config({"enabled": value})

    def test_qq_collections_require_lists(self):
        invalid_collections = (None, (), set(), "12345", {}, False, 12345)
        for field in ("plugin_admin_qqs", "whitelist_qqs"):
            for value in invalid_collections:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(self.config.PluginConfigError):
                        self.config.parse_config({field: value})

    def test_invalid_qq_values_are_rejected(self):
        invalid_values = (
            True,
            False,
            0,
            -1,
            1.0,
            None,
            "",
            "0",
            "0123",
            " 123",
            "123 ",
            "+123",
            "-123",
            "1.0",
            "１２３",
            "١٢٣",
            "123\n",
            [],
            {},
        )
        for field in ("plugin_admin_qqs", "whitelist_qqs"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(self.config.PluginConfigError):
                        self.config.parse_config({field: [value]})

    def test_errors_do_not_echo_invalid_values(self):
        invalid_value = "not-an-ascii-decimal-identifier"
        invalid_configs = (
            {"enabled": invalid_value},
            {"plugin_admin_qqs": [invalid_value]},
            {"whitelist_qqs": invalid_value},
        )

        for raw in invalid_configs:
            with self.subTest(raw=raw):
                with self.assertRaises(self.config.PluginConfigError) as raised:
                    self.config.parse_config(raw)
                self.assertNotIn(invalid_value, str(raised.exception))



if __name__ == "__main__":
    unittest.main()
