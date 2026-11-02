"""Join confirmed bindings and prepared bytes to the independent Neo executor."""

from __future__ import annotations

import copy
import json

from .config import PluginConfigError, parse_config
from .credentials import (
    CredentialCipher,
    CredentialError,
    binding_key,
    user_key,
    validate_values,
)
from .execution_queue import QueueRejected, guard_cancellation
from .executors.client import resolve_connection
from .executors.neo import PreparedExecution
from .permissions import Identity, authorize
from .preparation import PreparationError
from .storage import StateUnavailableError
from .task_config import parse_task_catalog
from .task_control import task_is_disabling


class ExecutionBridge:
    def __init__(self, plugin):
        self.plugin = plugin

    def _task(self, task_id):
        try:
            catalog = parse_task_catalog(self.plugin.config)
        except PluginConfigError:
            raise QueueRejected(
                "config_error", business_status="config_error"
            ) from None
        task = next((task for task in catalog.tasks if task.task_id == task_id), None)
        if task is None:
            raise QueueRejected("task_changed")
        return task

    def _identity(self, item):
        identity = Identity(item["platform_id"], item["qq_id"])
        if item["origin"] != f"{identity.platform_id}:FriendMessage:{identity.qq_id}":
            raise QueueRejected("invalid_origin")
        return identity

    def guard(self, item: dict, snapshot: dict) -> None:
        guard_cancellation(item, snapshot)
        if task_is_disabling(snapshot, item["task_id"]):
            raise QueueRejected("task_disabled")
        plugin = self.plugin
        if plugin._terminating:
            raise QueueRejected("service_disabled")
        identity = self._identity(item)
        try:
            permission = parse_config(plugin.config)
            framework = plugin.context.get_config(umo=item["origin"])
            admins = framework.get("admins_id", [])
            if not isinstance(admins, list) or any(
                type(value) is not str for value in admins
            ):
                raise PluginConfigError(
                    "framework administrator configuration is invalid"
                )
            platforms = plugin.context.platform_manager.get_insts()
            ordinary_qq_platform = any(
                platform.meta().id == identity.platform_id
                and platform.meta().name == "aiocqhttp"
                for platform in platforms
            )
        except (PluginConfigError, AttributeError, TypeError, ValueError):
            raise QueueRejected(
                "config_error", business_status="config_error"
            ) from None
        if (
            not ordinary_qq_platform
            or authorize(
                identity, astrbot_admin=identity.qq_id in admins, config=permission
            )
            is None
        ):
            raise QueueRejected("permission_revoked")
        if not permission.enabled:
            raise QueueRejected("service_disabled")
        task = self._task(item["task_id"])
        if task.version != item["task_version"]:
            raise QueueRejected("task_changed")
        if not task.enabled:
            raise QueueRejected("task_disabled")
        owner_key = user_key(identity)
        target_key = binding_key(identity, task.task_id)
        if item["user_key"] != owner_key or item["binding_key"] != target_key:
            raise QueueRejected("binding_missing")
        owner = snapshot["users"].get(owner_key)
        binding = snapshot["bindings"].get(target_key)
        if (
            not isinstance(owner, dict)
            or owner.get("platform_id") != identity.platform_id
            or owner.get("qq_id") != identity.qq_id
            or not isinstance(binding, dict)
            or binding.get("user_key") != owner_key
            or binding.get("task_id") != task.task_id
        ):
            raise QueueRejected("binding_missing")
        if "enrollment" in owner:
            raise QueueRejected("enrollment_active")
        if binding.get("paused", False) is not False:
            raise QueueRejected("binding_paused")
        try:
            connection = resolve_connection(plugin.config, plugin.context.get_config())
        except (PluginConfigError, AttributeError, TypeError, ValueError):
            raise QueueRejected(
                "executor_unavailable", business_status="config_error"
            ) from None
        if (
            plugin.executor is None
            or connection is None
            or connection != plugin.executor.client.connection
        ):
            raise QueueRejected("executor_unavailable", business_status="config_error")
        credential = snapshot["credentials"].get(target_key)
        if (
            not isinstance(credential, dict)
            or type(credential.get("ciphertext")) is not str
            or credential.get("task_version") != binding.get("task_version")
        ):
            raise QueueRejected("credentials_missing", business_status="config_error")
        artifact = snapshot["prepared_artifacts"].get(task.task_id)
        if (
            not isinstance(artifact, dict)
            or artifact.get("task_version") != task.version
            or artifact.get("sha256") != item["artifact_sha256"]
            or artifact.get("inputs_sha256") != item["inputs_sha256"]
        ):
            raise QueueRejected("artifact_changed", business_status="config_error")

    def _values(self, item, snapshot):
        identity = self._identity(item)
        task = self._task(item["task_id"])
        credential = snapshot["credentials"][item["binding_key"]]
        try:
            values = CredentialCipher.from_environment().decrypt(
                credential["ciphertext"],
                identity=identity,
                task_id=task.task_id,
                task_version=credential["task_version"],
            )
            validate_values(task, values)
        except (CredentialError, KeyError):
            raise QueueRejected(
                "credentials_missing", business_status="config_error"
            ) from None
        return values

    async def admission(self, identity, task, origin):
        plugin = self.plugin
        store = plugin.state_store
        if store is None or not store.ready:
            raise StateUnavailableError("execution state is unavailable")
        if plugin.executor is None:
            raise QueueRejected("executor_unavailable", business_status="config_error")
        snapshot = store.snapshot()
        owned_inflight = snapshot["cleanup_jobs"] and all(
            snapshot["batches"].get(run_id, {}).get("status") == "running"
            and snapshot["batches"][run_id].get("kind") == "checkin"
            for run_id in snapshot["cleanup_jobs"]
        )
        if plugin.executor.blocked and not owned_inflight:
            raise QueueRejected("executor_unavailable", business_status="config_error")
        preparation = plugin._preparation_service()
        try:
            artifact = await preparation.load(task)
        except PreparationError:
            raise QueueRejected(
                "artifact_changed", business_status="config_error"
            ) from None
        item = {
            "platform_id": identity.platform_id,
            "qq_id": identity.qq_id,
            "user_key": user_key(identity),
            "binding_key": binding_key(identity, task.task_id),
            "task_id": task.task_id,
            "task_version": task.version,
            "origin": origin,
            "artifact_sha256": artifact.sha256,
            "inputs_sha256": artifact.inputs_sha256,
        }
        snapshot = store.snapshot()
        self.guard(item, snapshot)
        # Validate the binding before accepting work; the worker decrypts again at dispatch.
        self._values(item, snapshot)
        return artifact

    async def execute(self, item):
        plugin = self.plugin
        store = plugin.state_store
        if store is None or not store.ready:
            raise StateUnavailableError("execution state is unavailable")
        executor = plugin.executor
        if executor is None:
            raise QueueRejected("executor_unavailable", business_status="config_error")
        self.guard(item, store.snapshot())
        task = self._task(item["task_id"])
        try:
            artifact = await plugin._preparation_service().load(task)
        except PreparationError:
            raise QueueRejected(
                "artifact_changed", business_status="config_error"
            ) from None
        if (
            artifact.sha256 != item["artifact_sha256"]
            or artifact.inputs_sha256 != item["inputs_sha256"]
        ):
            raise QueueRejected("artifact_changed", business_status="config_error")
        snapshot = store.snapshot()
        self.guard(item, snapshot)
        values = self._values(item, snapshot)
        credential = copy.deepcopy(snapshot["credentials"][item["binding_key"]])
        environment = {
            "TY_ACCOUNTS": json.dumps(
                [{"username": values["username"], "password": values["password"]}],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        }

        def execution_guard(current):
            self.guard(item, current)
            if current["credentials"].get(item["binding_key"]) != credential:
                raise QueueRejected("credentials_changed")

        request = PreparedExecution(
            identity=self._identity(item),
            task_id=task.task_id,
            task_version=task.version,
            source_commit=artifact.source_commit,
            sha256=artifact.sha256,
            runtime="python",
            script=artifact.script,
            environment=environment,
            result_adapter="json_protocol",
            timeout_seconds=task.timeout_seconds,
        )
        return await executor.execute(
            request, guard=execution_guard, run_id=item["run_id"]
        )


def describe_execution(snapshot, identity, task_id=""):
    owner = user_key(identity)
    items = [
        item
        for item in snapshot["batches"].values()
        if isinstance(item, dict)
        and item.get("kind") == "checkin"
        and item.get("user_key") == owner
        and item.get("platform_id") == identity.platform_id
        and item.get("qq_id") == identity.qq_id
        and (not task_id or item.get("task_id") == task_id)
    ]
    if not items:
        return ""
    latest = max(items, key=lambda item: item["sequence"])
    state = {"queued": "排队", "running": "运行", "finished": "完成"}.get(
        latest["status"], "未知"
    )
    result = latest.get("result", {})
    status = result.get("business_status", "unknown")
    allowed = {
        "success",
        "already_signed",
        "failed",
        "needs_auth",
        "unknown",
        "cancelled",
        "timeout",
        "config_error",
    }
    if status not in allowed:
        status = "unknown"
    notice = snapshot["outbox"].get(latest["run_id"], {})
    notice_state = {
        "reserved": "等待终态",
        "pending": "待发送",
        "sending": "发送中",
        "sent": "已发送",
        "failed": "发送失败",
        "unknown": "送达未确认",
        "suppressed": "按偏好静默",
    }.get(notice.get("phase"), "未知")
    return f"最近执行：{latest['run_id']}｜{state}\n业务结果：{status}\n通知：{notice_state}"
