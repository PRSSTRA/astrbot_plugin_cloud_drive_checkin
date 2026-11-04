"""AES-GCM 凭据封装，选择性复用 dev@a662b766（GPL-3.0）。"""
from __future__ import annotations

import base64
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import json_bytes
from .config import MAX_CREDENTIAL_BYTES
from .config import PluginConfigError
from .config import Script
from .config import validate_environment
from .permissions import Identity

_KEY_ENV = "CLOUD_DRIVE_CHECKIN_MASTER_KEY"
_NONCE_BYTES = 12
_MAX_ENVELOPE_BYTES = _NONCE_BYTES + MAX_CREDENTIAL_BYTES + 16
_MAX_ENVELOPE_TEXT = 4 * ((_MAX_ENVELOPE_BYTES + 2) // 3)


class CredentialError(ValueError):
    """凭据或认证上下文无效。"""


def parse_values(text: str, count: int) -> list[str]:
    """拆分井号分隔的字段，仅接受转义井号和反斜杠，保留原始空白。

    Raises:
        CredentialError: 转义不完整、转义字符无效或字段数不匹配。
    """
    if type(text) is not str or type(count) is not int or not 1 <= count <= 64:
        raise CredentialError("credential field count is invalid")
    values = []
    current = []
    escaped = False
    for character in text:
        if escaped:
            if character not in ("#", "\\"):
                raise CredentialError("only \\# and \\\\ escapes are allowed")
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "#":
            values.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        raise CredentialError("credential text ends with an incomplete escape")
    values.append("".join(current))
    if len(values) != count:
        raise CredentialError(
            "credential field count does not match the declared fields")
    return values


def validate_values(script: Script, values: dict) -> dict[str, str]:
    """校验凭据键与声明完全一致，并按声明顺序返回独立副本。"""
    if (not isinstance(script, Script) or type(values) is not dict or
            set(values) != set(script.env_names) or
            len(script.env_names) != len(set(script.env_names))):
        raise CredentialError(
            "credential fields do not match the script declaration")
    try:
        return validate_environment(
            {name: values[name] for name in script.env_names})
    except PluginConfigError as error:
        raise CredentialError(str(error)) from None


def _decode_base64(value: object) -> bytes:
    if type(value) is not str:
        raise ValueError
    encoded = value.encode("ascii")
    decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded) != encoded:
        raise ValueError
    return decoded


def _context(identity: Identity, script_id: str, fields_hash: str) -> bytes:
    if (not isinstance(identity, Identity) or
            type(identity.platform_id) is not str or not identity.platform_id or
            type(identity.qq_id) is not str or
            re.fullmatch(r"[1-9][0-9]*", identity.qq_id, re.ASCII) is None or
            type(script_id) is not str or re.fullmatch(
                r"[a-z][a-z0-9_]{0,63}", script_id, re.ASCII) is None or
            type(fields_hash) is not str or
            re.fullmatch(r"[0-9a-f]{64}", fields_hash, re.ASCII) is None):
        raise CredentialError("credential context is invalid")
    try:
        return json_bytes({
            "format": "v1",
            "identity": [identity.platform_id, identity.qq_id],
            "script_id": script_id,
            "fields_hash": fields_hash
        })
    except UnicodeError:
        raise CredentialError("credential context is invalid") from None


def _plaintext(env: dict) -> bytes:
    try:
        validate_environment(env)
        return json_bytes({"env": env})
    except PluginConfigError as error:
        raise CredentialError(str(error)) from None


def _unique_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


class CredentialCipher:
    """绑定平台、用户、脚本及有序字段摘要的认证加密器。"""

    __slots__ = ("_cipher",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise CredentialError("credential master key is invalid")
        try:
            self._cipher = AESGCM(key)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise CredentialError("credential master key is invalid") from None

    def __repr__(self) -> str:
        return "CredentialCipher()"

    @classmethod
    def from_environment(cls) -> CredentialCipher:
        """从主密钥环境变量读取规范 Base64 编码的 32 字节密钥。"""
        encoded = os.environ.get(_KEY_ENV)
        if encoded is None:
            raise CredentialError("credential master key is not configured")
        try:
            key = _decode_base64(encoded)
        except (ValueError, TypeError, UnicodeError):
            raise CredentialError("credential master key is invalid") from None
        return cls(key)

    def encrypt(self, env: dict, *, identity: Identity, script_id: str,
                fields_hash: str) -> str:
        """使用随机 nonce 加密，返回带版本前缀的可持久化文本。

        Args:
            env: 已收集的完整环境变量映射，所有值均按秘密处理。
            identity: 凭据所属的平台实例与 QQ 身份。
            script_id: 稳定脚本 ID。
            fields_hash: 有序字段名摘要，字段改动后须重新配置。

        Raises:
            CredentialError: 凭据、认证上下文或加密操作无效。
        """
        plaintext = _plaintext(env)
        context = _context(identity, script_id, fields_hash)
        try:
            nonce = os.urandom(_NONCE_BYTES)
            return "v1." + base64.urlsafe_b64encode(
                nonce +
                self._cipher.encrypt(nonce, plaintext, context)).decode("ascii")
        except (ValueError, TypeError, OSError, OverflowError,
                UnsupportedAlgorithm):
            raise CredentialError(
                "credentials could not be encrypted") from None

    def decrypt(self, token: str, *, identity: Identity, script_id: str,
                fields_hash: str) -> dict[str, str]:
        """校验封装与认证上下文后解密，拒绝跨身份或跨字段声明复用。

        Raises:
            CredentialError: 格式、认证或解密后的环境变量校验失败。
        """
        try:
            if type(token) is not str:
                raise ValueError
            prefix, encoded = token.split(".")
            if prefix != "v1" or not encoded or len(
                    encoded) > _MAX_ENVELOPE_TEXT:
                raise ValueError
            envelope = _decode_base64(encoded)
            if not _NONCE_BYTES + 16 <= len(envelope) <= _MAX_ENVELOPE_BYTES:
                raise ValueError
            plaintext = self._cipher.decrypt(
                envelope[:_NONCE_BYTES], envelope[_NONCE_BYTES:],
                _context(identity, script_id, fields_hash))
            if len(plaintext) > MAX_CREDENTIAL_BYTES:
                raise ValueError
            payload = json.loads(plaintext.decode("utf-8"),
                                 object_pairs_hook=_unique_pairs)
            if type(payload) is not dict or set(payload) != {"env"}:
                raise ValueError
            return validate_environment(payload["env"])
        except (InvalidTag, ValueError, TypeError, OverflowError, UnicodeError,
                RecursionError, UnsupportedAlgorithm):
            raise CredentialError(
                "credentials could not be decrypted") from None
