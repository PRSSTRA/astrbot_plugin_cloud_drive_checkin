"""提供响应校验和资源元数据操作的 Neo 客户端。

Neo ownership client selectively reused from dev a662b766; repository license.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx

_MAX_ENDPOINT_LENGTH = 2048
_MAX_API_KEY_LENGTH = 4096
_MAX_PROFILE_LENGTH = 128
_MAX_ENV_NAME_LENGTH = 128
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_PATH_LENGTH = 1024
_MAX_COMMAND_LENGTH = 16 * 1024
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_ENV_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

_ERROR_MESSAGES = {
    "access_rejected": "Neo access check failed",
    "invalid_request": "Neo request is invalid",
    "protocol_error": "Neo response is invalid",
    "response_too_large": "Neo response is too large",
    "session_not_ready": "Neo sandbox session is not ready",
    "transport_error": "Neo request failed",
    "unexpected_status": "Neo returned an unexpected status",
}


class NeoConfigError(ValueError):
    """Neo 连接配置无效。"""


class NeoError(RuntimeError):
    """仅携带本地分类和安全错误信息的客户端异常。"""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError("unknown Neo error code")
        if status is not None and (isinstance(status, bool) or
                                   not isinstance(status, int)):
            raise ValueError("Neo status must be an integer or None")
        self.code = code
        self.status = status
        super().__init__(_ERROR_MESSAGES[code])


def _utf8_length(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _decode_json(content: bytes) -> object:
    return json.loads(
        content,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def _canonical_endpoint(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise NeoConfigError("Neo endpoint is invalid")
    encoded_length = _utf8_length(value)
    if encoded_length is None or encoded_length > _MAX_ENDPOINT_LENGTH:
        raise NeoConfigError("Neo endpoint is invalid")
    if value != value.strip() or any(
            ord(character) < 32 for character in value):
        raise NeoConfigError("Neo endpoint is invalid")
    if "?" in value or "#" in value:
        raise NeoConfigError(
            "Neo endpoint must not contain a query or fragment")

    try:
        parsed = urlsplit(value)
        port = parsed.port
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise NeoConfigError("Neo endpoint is invalid") from None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise NeoConfigError("Neo endpoint must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise NeoConfigError("Neo endpoint must not contain credentials")
    if parsed.hostname is None:
        raise NeoConfigError("Neo endpoint is invalid")

    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme.lower() == "http" and
                    port == 80) or (parsed.scheme.lower() == "https" and
                                    port == 443)
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _validate_api_key(value: object) -> str:
    if (not isinstance(value, str) or not value or
            len(value) > _MAX_API_KEY_LENGTH or
            any(not 33 <= ord(character) <= 126 for character in value)):
        raise NeoConfigError("Neo access key is invalid")
    return value


def _validate_profile(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise NeoConfigError("Neo profile is invalid")
    encoded_length = _utf8_length(value)
    if (encoded_length is None or encoded_length > _MAX_PROFILE_LENGTH or
            value != value.strip() or
            any(ord(character) < 32 for character in value)):
        raise NeoConfigError("Neo profile is invalid")
    return value


def _validate_ttl(value: object) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or
            not 900 <= value <= 3600):
        raise NeoConfigError(
            "Neo TTL must be an integer from 900 to 3600 seconds")
    return value


@dataclass(frozen=True)
class NeoConnection:
    """保存已校验的固定连接，访问密钥不参与对象展示。"""

    endpoint: str
    api_key: str = field(repr=False)
    profile: str = "python-default"
    ttl: int = 900

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _canonical_endpoint(self.endpoint))
        object.__setattr__(self, "api_key", _validate_api_key(self.api_key))
        object.__setattr__(self, "profile", _validate_profile(self.profile))
        object.__setattr__(self, "ttl", _validate_ttl(self.ttl))

    @property
    def fingerprint(self) -> str:
        """返回连接身份摘要；TTL 不影响资源归属判定。"""
        encoded = json.dumps(
            [self.endpoint, self.api_key, self.profile],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _mapping_child(parent: Mapping, key: str) -> Mapping | None:
    child = parent.get(key)
    return child if isinstance(child, Mapping) else None


def _complete_framework_connection(
    framework_config: Mapping,) -> tuple[str, str, str] | None:
    provider_settings = _mapping_child(framework_config, "provider_settings")
    if provider_settings is None:
        return None
    sandbox = _mapping_child(provider_settings, "sandbox")
    if sandbox is None or sandbox.get("booter") != "shipyard_neo":
        return None

    endpoint = sandbox.get("shipyard_neo_endpoint")
    access_token = sandbox.get("shipyard_neo_access_token")
    if (not isinstance(endpoint, str) or not endpoint.strip() or
            endpoint.strip() == "__auto__" or
            not isinstance(access_token, str) or not access_token.strip()):
        return None

    profile = sandbox.get("shipyard_neo_profile", "python-default")
    if profile in (None, ""):
        profile = "python-default"
    if not isinstance(profile, str):
        raise NeoConfigError("Neo framework profile is invalid")
    return endpoint, access_token, profile


def resolve_connection(
    plugin_config: Mapping,
    framework_config: Mapping,
) -> NeoConnection | None:
    """优先使用完整的框架连接，否则使用插件连接，不混用来源。

    Returns:
        已校验的连接；插件未启用时为 None。

    Raises:
        NeoConfigError: 配置无效、缺少密钥或未确认 Docker 清理。
    """

    if not isinstance(plugin_config, Mapping):
        raise NeoConfigError("plugin configuration must be a mapping")

    raw_connection = plugin_config.get("neo_connection", {})
    if not isinstance(raw_connection, Mapping):
        raise NeoConfigError("Neo connection configuration must be a mapping")

    enabled = raw_connection.get("enabled", False)
    if not isinstance(enabled, bool):
        raise NeoConfigError("Neo enabled flag must be a boolean")
    if not enabled:
        return None

    if raw_connection.get("docker_cleanup_confirmed", False) is not True:
        raise NeoConfigError("Neo Docker cleanup must be explicitly confirmed")
    if not isinstance(framework_config, Mapping):
        raise NeoConfigError("framework configuration must be a mapping")

    ttl = _validate_ttl(raw_connection.get("ttl", 900))
    framework_connection = _complete_framework_connection(framework_config)
    if framework_connection is not None:
        endpoint, api_key, profile = framework_connection
        return NeoConnection(endpoint=endpoint,
                             api_key=api_key,
                             profile=profile,
                             ttl=ttl)

    endpoint = raw_connection.get("endpoint", "")
    profile = raw_connection.get("profile", "python-default")
    env_name = raw_connection.get("access_key_env",
                                  "CLOUD_DRIVE_CHECKIN_NEO_KEY")
    if (not isinstance(endpoint, str) or not endpoint.strip() or
            endpoint.strip() == "__auto__"):
        raise NeoConfigError("an explicit plugin Neo endpoint is required")
    if (not isinstance(env_name, str) or len(env_name) > _MAX_ENV_NAME_LENGTH or
            _ENV_PATTERN.fullmatch(env_name) is None):
        raise NeoConfigError("Neo access key environment name is invalid")

    api_key = os.environ.get(env_name)
    if api_key is None or not api_key.strip():
        raise NeoConfigError("Neo access key environment variable is not set")
    return NeoConnection(endpoint=endpoint,
                         api_key=api_key,
                         profile=profile,
                         ttl=ttl)


@dataclass(frozen=True)
class _Response:
    status: int
    content: bytes


class NeoClient:
    """封装 Neo 请求、响应校验和资源操作，不执行归属决策。

    请求或协议失败统一抛出不含远端响应正文的 NeoError。
    """

    def __init__(
        self,
        connection: NeoConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(connection, NeoConnection):
            raise TypeError("connection must be a NeoConnection")
        self.connection = connection
        self._client = httpx.AsyncClient(
            base_url=f"{connection.endpoint}/",
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0,
                                  read=30.0,
                                  write=30.0,
                                  pool=10.0),
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._client.aclose()

    def _authorization_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.connection.api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        read_timeout: float = 30.0,
    ) -> _Response:
        request_headers = self._authorization_headers() if authenticated else {}
        if headers is not None:
            request_headers.update(headers)
        timeout = httpx.Timeout(
            connect=10.0,
            read=read_timeout,
            write=30.0,
            pool=10.0,
        )

        try:
            async with (
                    asyncio.timeout(min(300.0, read_timeout + 10.0)),
                    self._client.stream(
                        method,
                        path.lstrip("/"),
                        headers=request_headers,
                        json=json_body,
                        data=data,
                        files=files,
                        timeout=timeout,
                    ) as response,
            ):
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = -1
                    if declared_length > _MAX_RESPONSE_BYTES:
                        raise NeoError("response_too_large",
                                       status=response.status_code)

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise NeoError("response_too_large",
                                       status=response.status_code)
                    content.extend(chunk)
                return _Response(status=response.status_code,
                                 content=bytes(content))
        except NeoError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise NeoError("transport_error") from None

    @staticmethod
    def _json_object(response: _Response) -> dict[str, Any]:
        try:
            value = _decode_json(response.content)
        except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
                RecursionError,
        ):
            raise NeoError("protocol_error", status=response.status) from None
        if not isinstance(value, dict):
            raise NeoError("protocol_error", status=response.status)
        return value

    @classmethod
    def _is_typed_not_found(cls, response: _Response) -> bool:
        if response.status != 404:
            return False
        try:
            value = _decode_json(response.content)
        except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
                RecursionError,
        ):
            return False
        return (isinstance(value, dict) and
                isinstance(value.get("error"), dict) and
                value["error"].get("code") == "not_found")

    @staticmethod
    def _raise_unexpected(response: _Response) -> None:
        raise NeoError("unexpected_status", status=response.status)

    @staticmethod
    def _resource_id(value: object, *, status: int | None = None) -> str:
        if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
            raise NeoError("protocol_error", status=status)
        return value

    @staticmethod
    def _request_id(value: object) -> str:
        if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
            raise NeoError("invalid_request")
        return value

    @staticmethod
    def _capabilities(value: object, *, status: int | None = None) -> list[str]:
        if not isinstance(value, list) or len(value) > 64:
            raise NeoError("protocol_error", status=status)
        for item in value:
            if not isinstance(item, str) or not item:
                raise NeoError("protocol_error", status=status)
            encoded_length = _utf8_length(item)
            if encoded_length is None or encoded_length > 64:
                raise NeoError("protocol_error", status=status)
        return list(value)

    @staticmethod
    def _response_profile(value: object, *, status: int | None = None) -> str:
        if not isinstance(value, str) or not value:
            raise NeoError("protocol_error", status=status)
        encoded_length = _utf8_length(value)
        if encoded_length is None or encoded_length > _MAX_PROFILE_LENGTH:
            raise NeoError("protocol_error", status=status)
        return value

    async def check_access(self) -> None:
        """确认鉴权生效，且配置的 profile 支持 shell 和文件操作。"""
        # profile 发现接口公开，使用不存在的沙箱验证鉴权，避免读取聊天资源。
        probe = "/v1/sandboxes/cloud-drive-checkin-auth-probe"
        anonymous = await self._request("GET", probe, authenticated=False)
        if anonymous.status != 401:
            raise NeoError("access_rejected", status=anonymous.status)
        authenticated_probe = await self._request("GET", probe)
        if not self._is_typed_not_found(authenticated_probe):
            raise NeoError("access_rejected", status=authenticated_probe.status)

        authenticated = await self._request("GET", "/v1/profiles")
        if authenticated.status != 200:
            raise NeoError("access_rejected", status=authenticated.status)
        payload = self._json_object(authenticated)
        items = payload.get("items")
        if not isinstance(items, list):
            raise NeoError("protocol_error", status=authenticated.status)

        for item in items:
            if not isinstance(
                    item, dict) or item.get("id") != self.connection.profile:
                continue
            capabilities = self._capabilities(item.get("capabilities"),
                                              status=authenticated.status)
            if {"shell", "filesystem"}.issubset(capabilities):
                return
            raise NeoError("access_rejected", status=authenticated.status)
        raise NeoError("access_rejected", status=authenticated.status)

    async def create_sandbox(self, run_id: str) -> dict[str, Any]:
        """以 run_id 为幂等键创建沙箱，返回待独立核验的资源标识。"""
        safe_run_id = self._request_id(run_id)
        response = await self._request(
            "POST",
            "/v1/sandboxes",
            headers={"Idempotency-Key": safe_run_id},
            json_body={
                "profile": self.connection.profile,
                "cargo_id": None,
                "ttl": self.connection.ttl,
            },
        )
        if response.status != 201:
            self._raise_unexpected(response)
        payload = self._json_object(response)
        result: dict[str, Any] = {
            "id":
                self._resource_id(payload.get("id"), status=response.status),
            "cargo_id":
                self._resource_id(payload.get("cargo_id"),
                                  status=response.status),
        }
        if "profile" in payload:
            result["profile"] = self._response_profile(payload["profile"],
                                                       status=response.status)
        if "capabilities" in payload:
            result["capabilities"] = self._capabilities(payload["capabilities"],
                                                        status=response.status)
        return result

    async def get_cargo(self, cargo_id: str) -> dict[str, Any] | None:
        """读取 cargo 归属字段；仅明确的 not_found 响应返回 None。"""
        safe_id = self._request_id(cargo_id)
        response = await self._request("GET", f"/v1/cargos/{safe_id}")
        if self._is_typed_not_found(response):
            return None
        if response.status != 200:
            self._raise_unexpected(response)
        payload = self._json_object(response)
        response_id = self._resource_id(payload.get("id"),
                                        status=response.status)
        managed = payload.get("managed")
        managed_by = payload.get("managed_by_sandbox_id")
        if response_id != safe_id or not isinstance(managed, bool):
            raise NeoError("protocol_error", status=response.status)
        if managed_by is not None:
            managed_by = self._resource_id(managed_by, status=response.status)
        return {
            "id": response_id,
            "managed": managed,
            "managed_by_sandbox_id": managed_by,
        }

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        """读取沙箱元数据；仅明确的 not_found 响应返回 None。"""
        safe_id = self._request_id(sandbox_id)
        response = await self._request("GET", f"/v1/sandboxes/{safe_id}")
        if self._is_typed_not_found(response):
            return None
        if response.status != 200:
            self._raise_unexpected(response)
        payload = self._json_object(response)
        response_id = self._resource_id(payload.get("id"),
                                        status=response.status)
        if response_id != safe_id:
            raise NeoError("protocol_error", status=response.status)

        result: dict[str, Any] = {"id": response_id}
        if "cargo_id" in payload:
            result["cargo_id"] = self._resource_id(payload["cargo_id"],
                                                   status=response.status)
        if "profile" in payload:
            result["profile"] = self._response_profile(payload["profile"],
                                                       status=response.status)
        if "capabilities" in payload:
            result["capabilities"] = self._capabilities(payload["capabilities"],
                                                        status=response.status)
        if "status" in payload:
            sandbox_status = payload["status"]
            if not isinstance(sandbox_status, str) or not sandbox_status:
                raise NeoError("protocol_error", status=response.status)
            encoded_length = _utf8_length(sandbox_status)
            if encoded_length is None or encoded_length > 32:
                raise NeoError("protocol_error", status=response.status)
            result["status"] = sandbox_status
        return result

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """删除沙箱，资源明确不存在时视为成功。"""
        safe_id = self._request_id(sandbox_id)
        response = await self._request("DELETE", f"/v1/sandboxes/{safe_id}")
        if response.status == 204 or self._is_typed_not_found(response):
            return
        self._raise_unexpected(response)

    async def delete_cargo(self, cargo_id: str) -> None:
        """删除 cargo，资源明确不存在时视为成功。"""
        safe_id = self._request_id(cargo_id)
        response = await self._request("DELETE", f"/v1/cargos/{safe_id}")
        if response.status == 204 or self._is_typed_not_found(response):
            return
        self._raise_unexpected(response)

    @staticmethod
    def _upload_path(path: object) -> str:
        if not isinstance(path, str) or not path:
            raise NeoError("invalid_request")
        encoded_length = _utf8_length(path)
        if (encoded_length is None or encoded_length > _MAX_PATH_LENGTH or
                path.startswith("/") or "\\" in path or "\x00" in path):
            raise NeoError("invalid_request")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise NeoError("invalid_request")
        return path

    async def upload(self, sandbox_id: str, path: str, data: bytes) -> None:
        """上传到沙箱内的相对路径，并核对回执路径和字节数。"""
        safe_id = self._request_id(sandbox_id)
        safe_path = self._upload_path(path)
        if not isinstance(data, bytes):
            raise NeoError("invalid_request")
        filename = safe_path.rsplit("/", 1)[-1]
        response = await self._request(
            "POST",
            f"/v1/sandboxes/{safe_id}/filesystem/upload",
            data={"path": safe_path},
            files={"file": (filename, data, "application/octet-stream")},
        )
        if response.status != 200:
            self._raise_unexpected(response)
        payload = self._json_object(response)
        size = payload.get("size")
        if (payload.get("status") != "ok" or payload.get("path") != safe_path or
                isinstance(size, bool) or not isinstance(size, int) or
                size != len(data)):
            raise NeoError("protocol_error", status=response.status)

    async def exec(self, sandbox_id: str, command: str,
                   timeout: int) -> dict[str, Any]:
        """执行命令并校验结果；timeout 为 1 至 300 秒。

        Raises:
            NeoError: 请求无效、会话未就绪或请求及响应校验失败。
        """
        safe_id = self._request_id(sandbox_id)
        if not isinstance(command, str) or not command:
            raise NeoError("invalid_request")
        command_length = _utf8_length(command)
        if (command_length is None or command_length > _MAX_COMMAND_LENGTH or
                "\x00" in command or self.connection.api_key in command):
            raise NeoError("invalid_request")
        if (isinstance(timeout, bool) or not isinstance(timeout, int) or
                not 1 <= timeout <= 300):
            raise NeoError("invalid_request")

        response = await self._request(
            "POST",
            f"/v1/sandboxes/{safe_id}/shell/exec",
            json_body={
                "command": command,
                "cwd": ".",
                "timeout": timeout,
                "include_code": False,
            },
            read_timeout=float(timeout + 10),
        )
        if response.status == 503:
            try:
                payload = _decode_json(response.content)
            except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                    RecursionError,
            ):
                payload = None
            if (isinstance(payload, dict) and
                    isinstance(payload.get("error"), dict) and
                    payload["error"].get("code") == "session_not_ready"):
                raise NeoError("session_not_ready", status=503)
        if response.status != 200:
            self._raise_unexpected(response)

        payload = self._json_object(response)
        success = payload.get("success")
        exit_code = payload.get("exit_code")
        output = payload.get("output")
        if not isinstance(output, str):
            raise NeoError("protocol_error", status=response.status)
        output_length = _utf8_length(output)
        if (not isinstance(success, bool) or
            (exit_code is not None and
             (isinstance(exit_code, bool) or not isinstance(exit_code, int))) or
                output_length is None or output_length > _MAX_OUTPUT_BYTES):
            raise NeoError("protocol_error", status=response.status)
        return {"success": success, "exit_code": exit_code, "output": output}
