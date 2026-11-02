import contextlib
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from state_support import load_module

entry = load_module("runners.tianyi_entry", "_tianyi_entry_tests")


def _normal_envelope(
    status, reason_code, *, error=None, ok=None, notification="disabled"
):
    if ok is None:
        ok = status != "failed"
    counts = {
        "signed": 1 if status == "signed" else 0,
        "already_signed": 1 if status == "already_signed" else 0,
        "failed": 1 if status == "failed" else 0,
    }
    body = {
        "ok": ok,
        "counts": counts,
        "notification": notification,
        "results": [
            {
                "username": "masked-user",
                "signin": "business detail" if status != "failed" else "",
                "status": status,
                "error": error,
                "reason_code": reason_code,
            }
        ],
    }
    return {"statusCode": 200, "body": json.dumps(body, ensure_ascii=False)}


class StrictResultParserTests(unittest.TestCase):
    def test_closed_status_mapping_accepts_only_consistent_evidence(self):
        scenarios = (
            (_normal_envelope("signed", "signed"), "success"),
            (
                _normal_envelope("already_signed", "already_signed"),
                "already_signed",
            ),
            (
                _normal_envelope(
                    "failed", "captcha_required", error="arbitrary failure"
                ),
                "needs_auth",
            ),
            (
                _normal_envelope(
                    "failed", "device_verification_required", error="arbitrary"
                ),
                "needs_auth",
            ),
            (
                _normal_envelope("failed", "login_rejected", error="arbitrary"),
                "failed",
            ),
            (
                _normal_envelope("failed", "sign_rejected", error="arbitrary"),
                "failed",
            ),
            (
                _normal_envelope("failed", "unknown", error="arbitrary"),
                "unknown",
            ),
            (
                _normal_envelope("failed", "invalid_configuration", error="arbitrary"),
                "config_error",
            ),
        )
        for envelope, expected in scenarios:
            with self.subTest(expected=expected):
                self.assertEqual(entry.parse_handler_result(envelope), expected)

    def test_valid_top_level_configuration_error_is_config_error(self):
        envelope = {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "ok": False,
                    "config_error": "configuration rejected",
                    "notification": "not_attempted",
                    "reason_code": "invalid_configuration",
                }
            ),
        }
        self.assertEqual(entry.parse_handler_result(envelope), "config_error")

    def test_error_text_never_changes_classification(self):
        unknown = _normal_envelope(
            "failed",
            "unknown",
            error="captcha_required login_rejected signed success",
        )
        auth = _normal_envelope(
            "failed", "captcha_required", error="ordinary unrelated text"
        )
        self.assertEqual(entry.parse_handler_result(unknown), "unknown")
        self.assertEqual(entry.parse_handler_result(auth), "needs_auth")

    def test_malformed_mixed_and_contradictory_results_are_unknown(self):
        duplicate_body = (
            '{"ok":false,"ok":true,"counts":{"signed":0,'
            '"already_signed":0,"failed":1},"notification":"disabled",'
            '"results":[]}'
        )
        nonfinite_body = (
            '{"ok":false,"counts":{"signed":0,"already_signed":0,'
            '"failed":NaN},"notification":"disabled","results":[]}'
        )
        contradictory_count = _normal_envelope("signed", "signed")
        contradictory_count_body = json.loads(contradictory_count["body"])
        contradictory_count_body["counts"] = {
            "signed": 0,
            "already_signed": 1,
            "failed": 0,
        }
        contradictory_count["body"] = json.dumps(contradictory_count_body)

        bool_count = _normal_envelope("failed", "unknown", error="failure")
        bool_count_body = json.loads(bool_count["body"])
        bool_count_body["counts"]["failed"] = True
        bool_count["body"] = json.dumps(bool_count_body)

        mixed_config = {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "ok": False,
                    "config_error": "bad",
                    "notification": "not_attempted",
                    "reason_code": "invalid_configuration",
                    "results": [],
                }
            ),
        }
        scenarios = (
            {"statusCode": 200, "body": duplicate_body},
            {"statusCode": 200, "body": nonfinite_body},
            contradictory_count,
            bool_count,
            _normal_envelope("signed", "already_signed"),
            _normal_envelope("signed", "signed", error="not allowed"),
            _normal_envelope("failed", "unknown", error="failure", notification="sent"),
            _normal_envelope("failed", "unknown", error="failure", ok=True),
            mixed_config,
            {"statusCode": True, "body": "{}"},
            {"statusCode": 500, "body": "{}"},
            {"statusCode": 200, "body": "[]"},
            {"statusCode": 200, "body": "{}", "extra": True},
            None,
        )
        for number, envelope in enumerate(scenarios):
            with self.subTest(number=number):
                self.assertEqual(entry.parse_handler_result(envelope), "unknown")


class EntryExecutionTests(unittest.TestCase):
    def _invoke(self, environment, importer):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(entry.importlib, "import_module", side_effect=importer),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            entry.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_single_literal_account_runs_once_with_telegram_forced_off(self):
        username = "  user ;'\"\n特殊字符  "
        password = "  pass $(not-shell) \\ \n秘密  "
        secret_log = "upstream-private-log"
        calls = []
        environment_seen = []

        def handler(event, context):
            calls.append((event, context))
            environment_seen.append(dict(os.environ))
            print(secret_log)
            sys.stderr.write(secret_log)
            return _normal_envelope("signed", "signed")

        source = SimpleNamespace(main_handler=handler)

        def importer(name):
            print(secret_log)
            sys.stderr.write(secret_log)
            return source

        stdout, stderr = self._invoke(
            {
                "TY_ACCOUNTS": json.dumps(
                    [{"username": username, "password": password}],
                    ensure_ascii=False,
                ),
                "TG_BOT_TOKEN": "must-not-be-used",
                "TG_CHAT_ID": "must-not-be-used",
            },
            importer,
        )

        self.assertEqual(json.loads(stdout), {"status": "success"})
        self.assertEqual(stderr, "")
        self.assertNotIn(secret_log, stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(environment_seen), 1)
        account = json.loads(environment_seen[0]["TY_ACCOUNTS"])[0]
        self.assertEqual(account, {"username": username, "password": password})
        self.assertEqual(environment_seen[0]["TG_BOT_TOKEN"], "")
        self.assertEqual(environment_seen[0]["TG_CHAT_ID"], "")

    def test_invalid_credentials_do_not_import_or_fall_back(self):
        invalid_values = (
            None,
            "[]",
            '[{"username":"user","password":""}]',
            '[{"username":"user","password":"pass","extra":true}]',
            '[{"username":"first","username":"second","password":"pass"}]',
            '[{"username":"user","password":"pass"},{"username":"two","password":"pass"}]',
            '[{"username":"user","password":NaN}]',
        )
        for raw in invalid_values:
            with self.subTest(raw=raw):
                environment = {} if raw is None else {"TY_ACCOUNTS": raw}
                importer = mock.Mock()
                stdout, stderr = self._invoke(environment, importer)
                self.assertEqual(json.loads(stdout), {"status": "config_error"})
                self.assertEqual(stderr, "")
                importer.assert_not_called()

    def test_import_failure_is_safe_config_error_without_secret_disclosure(self):
        secret = "import-private-secret"

        def importer(_name):
            print(secret)
            raise ImportError(secret)

        stdout, stderr = self._invoke(
            {"TY_ACCOUNTS": '[{"username":"user","password":"pass"}]'},
            importer,
        )
        self.assertEqual(json.loads(stdout), {"status": "config_error"})
        self.assertEqual(stderr, "")
        self.assertNotIn(secret, stdout)

    def test_handler_exception_is_unknown_and_never_retried_or_disclosed(self):
        secret = "handler-private-secret"
        calls = 0

        def handler(_event, _context):
            nonlocal calls
            calls += 1
            print(secret)
            sys.stderr.write(secret)
            raise RuntimeError(secret)

        stdout, stderr = self._invoke(
            {"TY_ACCOUNTS": '[{"username":"user","password":"pass"}]'},
            lambda _name: SimpleNamespace(main_handler=handler),
        )
        self.assertEqual(json.loads(stdout), {"status": "unknown"})
        self.assertEqual(stderr, "")
        self.assertNotIn(secret, stdout)
        self.assertEqual(calls, 1)

    def test_malformed_handler_output_is_unknown_without_duplicate_call(self):
        calls = 0

        def handler(_event, _context):
            nonlocal calls
            calls += 1
            return {
                "statusCode": 200,
                "body": '{"ok":false,"ok":true}',
            }

        stdout, stderr = self._invoke(
            {"TY_ACCOUNTS": '[{"username":"user","password":"pass"}]'},
            lambda _name: SimpleNamespace(main_handler=handler),
        )
        self.assertEqual(json.loads(stdout), {"status": "unknown"})
        self.assertEqual(stderr, "")
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
