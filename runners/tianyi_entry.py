"""Strict, quiet entry point for the prepared Tianyi zipapp."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys

_MAX_CREDENTIAL_JSON_CHARS = 32 * 1024
_MAX_BODY_JSON_CHARS = 64 * 1024
_NORMAL_FIELDS = frozenset({"ok", "counts", "notification", "results"})
_CONFIG_FIELDS = frozenset({"ok", "config_error", "notification", "reason_code"})
_RESULT_FIELDS = frozenset({"username", "signin", "status", "error", "reason_code"})
_COUNT_FIELDS = frozenset({"signed", "already_signed", "failed"})
_FAILED_STATUSES = {
    "captcha_required": "needs_auth",
    "device_verification_required": "needs_auth",
    "login_rejected": "failed",
    "sign_rejected": "failed",
    "unknown": "unknown",
    "invalid_configuration": "config_error",
}


class _InvalidResult(ValueError):
    pass


class _BusinessFailure(Exception):
    """The script may have run without returning reliable business evidence."""


def _strict_json(raw: str) -> object:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _InvalidResult
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise _InvalidResult

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError):
        raise _InvalidResult from None


def _credentials() -> str:
    raw = os.environ.get("TY_ACCOUNTS")
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_CREDENTIAL_JSON_CHARS:
        raise _InvalidResult
    value = _strict_json(raw)
    if not isinstance(value, list) or len(value) != 1:
        raise _InvalidResult
    account = value[0]
    if not isinstance(account, dict) or frozenset(account) != {"username", "password"}:
        raise _InvalidResult
    username = account["username"]
    password = account["password"]
    if (
        not isinstance(username, str)
        or not username.strip()
        or not isinstance(password, str)
        or not password.strip()
    ):
        raise _InvalidResult
    return json.dumps(
        [{"username": username, "password": password}],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _actual_nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _parse_normal_body(body: dict[str, object]) -> str:
    if frozenset(body) != _NORMAL_FIELDS:
        raise _InvalidResult
    ok = body["ok"]
    counts = body["counts"]
    notification = body["notification"]
    results = body["results"]
    if (
        type(ok) is not bool
        or notification != "disabled"
        or not isinstance(counts, dict)
        or frozenset(counts) != _COUNT_FIELDS
        or not all(_actual_nonnegative_integer(value) for value in counts.values())
        or sum(counts.values()) != 1
        or not isinstance(results, list)
        or len(results) != 1
    ):
        raise _InvalidResult

    result = results[0]
    if not isinstance(result, dict) or frozenset(result) != _RESULT_FIELDS:
        raise _InvalidResult
    username = result["username"]
    signin = result["signin"]
    status = result["status"]
    error = result["error"]
    reason_code = result["reason_code"]
    if not isinstance(username, str) or not isinstance(signin, str):
        raise _InvalidResult
    if status not in {"signed", "already_signed", "failed"}:
        raise _InvalidResult

    expected_counts = {
        "signed": 1 if status == "signed" else 0,
        "already_signed": 1 if status == "already_signed" else 0,
        "failed": 1 if status == "failed" else 0,
    }
    if counts != expected_counts or ok is (status == "failed"):
        raise _InvalidResult

    if status in {"signed", "already_signed"}:
        if reason_code != status or error is not None:
            raise _InvalidResult
        return "success" if status == "signed" else "already_signed"

    if not isinstance(error, str) or not isinstance(reason_code, str):
        raise _InvalidResult
    return _FAILED_STATUSES.get(reason_code, "unknown")


def _parse_handler_result(value: object) -> str:
    if not isinstance(value, dict) or frozenset(value) != {"statusCode", "body"}:
        raise _InvalidResult
    if type(value["statusCode"]) is not int or value["statusCode"] != 200:
        raise _InvalidResult
    raw_body = value["body"]
    if not isinstance(raw_body, str) or len(raw_body) > _MAX_BODY_JSON_CHARS:
        raise _InvalidResult
    body = _strict_json(raw_body)
    if not isinstance(body, dict):
        raise _InvalidResult

    if frozenset(body) == _CONFIG_FIELDS:
        if (
            body["ok"] is not False
            or body["notification"] != "not_attempted"
            or body["reason_code"] != "invalid_configuration"
            or not isinstance(body["config_error"], str)
        ):
            raise _InvalidResult
        return "config_error"
    return _parse_normal_body(body)


def parse_handler_result(value: object) -> str:
    """Collapse one upstream handler envelope to a closed business status."""

    try:
        return _parse_handler_result(value)
    except (KeyError, TypeError, ValueError, RecursionError):
        return "unknown"


def _run() -> str:
    os.environ["TY_ACCOUNTS"] = _credentials()
    os.environ["TG_BOT_TOKEN"] = ""
    os.environ["TG_CHAT_ID"] = ""
    with (
        open(os.devnull, "w", encoding="utf-8") as discarded,
        contextlib.redirect_stdout(discarded),
        contextlib.redirect_stderr(discarded),
    ):
        try:
            source = importlib.import_module("tianyi_source")
            handler = source.main_handler
            if not callable(handler):
                raise _InvalidResult
        except BaseException as error:
            raise _InvalidResult from error
        try:
            return parse_handler_result(handler({}, None))
        except BaseException as error:
            raise _BusinessFailure from error


def main() -> None:
    try:
        status = _run()
    except _BusinessFailure:
        status = "unknown"
    except (_InvalidResult, OSError):
        status = "config_error"
    sys.stdout.write(json.dumps({"status": status}, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
