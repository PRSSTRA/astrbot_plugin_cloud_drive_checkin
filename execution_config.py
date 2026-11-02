"""Validated scheduling settings for durable manual execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .config import PluginConfigError


@dataclass(frozen=True)
class IntervalConfig:
    """One persisted-completion-to-next-dispatch interval policy."""

    mode: str
    seconds: int = 0
    min_seconds: int = 0
    max_seconds: int = 0


@dataclass(frozen=True)
class ExecutionConfig:
    manual_cooldown_seconds: int
    queue_expiry_seconds: int
    history_retention_days: int
    user_interval: IntervalConfig
    task_interval: IntervalConfig


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PluginConfigError(f"{name} must be a non-negative integer")
    return value


def _interval(value: object, name: str) -> IntervalConfig:
    if not isinstance(value, Mapping):
        raise PluginConfigError(f"{name} must be an interval mapping")

    allowed = {"mode", "seconds", "min_seconds", "max_seconds"}
    if not set(value).issubset(allowed):
        raise PluginConfigError(f"{name} interval has invalid fields")

    mode = value.get("mode")
    if mode == "none":
        return IntervalConfig(mode="none")
    if mode == "fixed":
        if "seconds" not in value:
            raise PluginConfigError(f"{name} fixed interval has invalid fields")
        return IntervalConfig(
            mode="fixed",
            seconds=_nonnegative_integer(value["seconds"], f"{name}.seconds"),
        )
    if mode == "random":
        if "min_seconds" not in value or "max_seconds" not in value:
            raise PluginConfigError(f"{name} random interval has invalid fields")
        minimum = _nonnegative_integer(value["min_seconds"], f"{name}.min_seconds")
        maximum = _nonnegative_integer(value["max_seconds"], f"{name}.max_seconds")
        if minimum > maximum:
            raise PluginConfigError(f"{name} random interval bounds are invalid")
        return IntervalConfig(mode="random", min_seconds=minimum, max_seconds=maximum)
    raise PluginConfigError(f"{name} interval mode is invalid")


def parse_execution_config(raw: Mapping) -> ExecutionConfig:
    """Parse manual queue settings from the plugin's top-level configuration."""

    if not isinstance(raw, Mapping):
        raise PluginConfigError("plugin execution configuration must be a mapping")

    return ExecutionConfig(
        manual_cooldown_seconds=_nonnegative_integer(
            raw.get("manual_cooldown_seconds", 300), "manual_cooldown_seconds"
        ),
        queue_expiry_seconds=_nonnegative_integer(
            raw.get("queue_expiry_seconds", 900), "queue_expiry_seconds"
        ),
        history_retention_days=_nonnegative_integer(
            raw.get("history_retention_days", 30), "history_retention_days"
        ),
        user_interval=_interval(
            raw.get(
                "user_interval",
                {"mode": "random", "min_seconds": 30, "max_seconds": 60},
            ),
            "user_interval",
        ),
        task_interval=_interval(
            raw.get("task_interval", {"mode": "fixed", "seconds": 60}),
            "task_interval",
        ),
    )
