"""Asynchronous preparation and verified caching for the fixed Tianyi artifact."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from .preparation import (
    PreparationError,
    PreparedArtifact,
    TianyiPreparationInputs,
    TianyiRequirement,
    TianyiUserField,
    build_tianyi,
    load_tianyi_inputs,
)
from .storage import CommitUnconfirmedError, StateStore, StateUnavailableError
from .task_config import TaskConfig

_METADATA_HOST = "pypi.org"
_FILES_HOST = "files.pythonhosted.org"
_ALLOWED_HOSTS = frozenset({_METADATA_HOST, _FILES_HOST})
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_WHEEL_BYTES = 2 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_CACHE_FILES = 32
_MAX_CACHE_BYTES = 32 * 1024 * 1024
_DOWNLOAD_TOTAL_SECONDS = 120.0
_CHUNK_BYTES = 64 * 1024
_CACHE_DIRECTORY = "prepared_artifacts"
_ARTIFACT_NAME = re.compile(r"([0-9a-f]{64})\.pyz\Z", re.ASCII)
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RECORD_FIELDS = frozenset(
    {
        "task_version",
        "source_commit",
        "source_sha256",
        "sha256",
        "size",
        "inputs_sha256",
    }
)


class PreparationBusy(PreparationError):
    """Raised when another preparation is already active in this service."""


class _PublicIPNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve the two fixed hosts and connect only to the verified numeric IP."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._backend = httpcore.AnyIOBackend() if backend is None else backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await _resolve_public_addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    host=address,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("download source has no public address")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PreparationService:
    """Download locked wheels and publish verified, content-addressed zipapps."""

    def __init__(
        self,
        store: StateStore,
        data_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(data_dir, Path):
            raise TypeError("data_dir must be pathlib.Path")
        if client is not None and not isinstance(client, httpx.AsyncClient):
            raise TypeError("client must be httpx.AsyncClient")
        self._store = store
        self._data_dir = data_dir
        self._cache_dir = data_dir / _CACHE_DIRECTORY
        self._client = client
        self._owns_client = client is None
        self._operations: set[asyncio.Task[Any]] = set()
        self._preparing: asyncio.Task[PreparedArtifact] | None = None
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def prepare(
        self, task: TaskConfig, *, guard: Callable[[], None]
    ) -> PreparedArtifact:
        """Prepare or repair the fixed Tianyi artifact for one configured task."""

        if not callable(guard):
            raise TypeError("guard must be callable")
        self._ensure_open()
        if self._preparing is not None and not self._preparing.done():
            raise PreparationBusy("another artifact preparation is in progress")

        operation = asyncio.create_task(self._prepare(task, guard))
        self._preparing = operation
        self._operations.add(operation)
        try:
            return await operation
        finally:
            self._operations.discard(operation)
            if self._preparing is operation:
                self._preparing = None

    async def load(self, task: TaskConfig) -> PreparedArtifact:
        """Load an already prepared artifact without downloading or repairing it."""

        self._ensure_open()
        operation = asyncio.create_task(self._load(task))
        self._operations.add(operation)
        try:
            return await operation
        finally:
            self._operations.discard(operation)

    async def close(self) -> None:
        """Cancel owned operations, await their threads, then close the owned client."""

        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await _wait_uninterruptibly(self._close_task)
            raise

    async def _close(self) -> None:
        operations = tuple(self._operations)
        for operation in operations:
            operation.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        try:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            raise PreparationError("preparation client could not be closed") from None
        finally:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise PreparationError("preparation service is closed")

    async def _prepare(
        self, task: TaskConfig, guard: Callable[[], None]
    ) -> PreparedArtifact:
        inputs = await _run_in_thread(load_tianyi_inputs)
        guard()
        _validate_task(task, inputs)

        snapshot = self._ready_snapshot()
        record = _current_record(snapshot, task, inputs)
        if record is not None:
            script = await _run_in_thread(
                _read_artifact_file,
                self._artifact_path(record["sha256"]),
                record["size"],
                record["sha256"],
            )
            guard()
            if script is not None:
                await self._reclaim_ready_cache()
                if _current_record(self._ready_snapshot(), task, inputs) != record:
                    raise PreparationError("prepared artifact cache changed")
                guard()
                return _artifact_from_record(script, record)

        try:
            with tempfile.TemporaryDirectory(prefix="checkin-tianyi-") as temporary:
                wheelhouse = Path(temporary)
                await self._download_wheels(inputs.requirements, wheelhouse, guard)
                guard()
                artifact = await _run_in_thread(build_tianyi, wheelhouse)
                guard()
                current_inputs = await _run_in_thread(load_tianyi_inputs)
                guard()
                if current_inputs != inputs:
                    raise PreparationError(
                        "Tianyi preparation inputs changed during preparation"
                    )
        except asyncio.CancelledError:
            raise
        except PreparationError:
            raise
        except (OSError, RuntimeError):
            raise PreparationError("Tianyi artifact preparation failed") from None

        if (
            artifact.source_commit != inputs.task.source_commit
            or artifact.source_sha256 != inputs.task.source_sha256
            or artifact.inputs_sha256 != inputs.inputs_sha256
            or len(artifact.script) > _MAX_ARTIFACT_BYTES
            or hashlib.sha256(artifact.script).hexdigest() != artifact.sha256
        ):
            raise PreparationError(
                "Tianyi preparation inputs changed during preparation"
            )

        metadata = {
            "task_version": task.version,
            "source_commit": artifact.source_commit,
            "source_sha256": artifact.source_sha256,
            "sha256": artifact.sha256,
            "size": len(artifact.script),
            "inputs_sha256": artifact.inputs_sha256,
        }
        quota_snapshot = self._ready_snapshot()
        _prospective_cache_references(quota_snapshot, task.task_id, metadata)
        await _run_in_thread(
            _reclaim_cache_files,
            self._cache_dir,
            set(_snapshot_cache_references(quota_snapshot)),
        )
        guard()
        _prospective_cache_references(self._ready_snapshot(), task.task_id, metadata)
        await _run_in_thread(
            _write_artifact_file, self._cache_dir, artifact.sha256, artifact.script
        )
        guard()

        def publish(candidate: dict[str, Any]) -> None:
            guard()
            _prospective_cache_references(candidate, task.task_id, metadata)
            records = candidate.get("prepared_artifacts")
            if type(records) is not dict:
                raise StateUnavailableError("prepared artifact state is unavailable")
            records[task.task_id] = metadata.copy()

        await self._store.mutate(publish)
        guard()
        await self._reclaim_ready_cache()
        if _current_record(self._ready_snapshot(), task, inputs) != metadata:
            raise PreparationError("prepared artifact state changed")
        guard()
        return artifact

    async def _load(self, task: TaskConfig) -> PreparedArtifact:
        inputs = await _run_in_thread(load_tianyi_inputs)
        _validate_task(task, inputs)
        record = _current_record(self._ready_snapshot(), task, inputs)
        if record is None:
            raise PreparationError("prepared artifact cache is not current")
        script = await _run_in_thread(
            _read_artifact_file,
            self._artifact_path(record["sha256"]),
            record["size"],
            record["sha256"],
        )
        if script is None:
            raise PreparationError("prepared artifact cache is invalid")
        if _current_record(self._ready_snapshot(), task, inputs) != record:
            raise PreparationError("prepared artifact cache changed")
        return _artifact_from_record(script, record)

    async def _download_wheels(
        self,
        requirements: tuple[TianyiRequirement, ...],
        wheelhouse: Path,
        guard: Callable[[], None],
    ) -> None:
        try:
            async with asyncio.timeout(_DOWNLOAD_TOTAL_SECONDS):
                for requirement in requirements:
                    metadata_url = (
                        f"https://{_METADATA_HOST}/pypi/"
                        f"{requirement.name}/{requirement.version}/json"
                    )
                    metadata = await self._download_metadata(metadata_url)
                    filename, wheel_url = _select_locked_wheel(metadata, requirement)
                    guard()
                    await self._download_wheel(
                        wheel_url, wheelhouse / filename, requirement.sha256
                    )
                    guard()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise PreparationError("locked wheel download timed out") from None

    async def _download_metadata(self, url: str) -> dict[str, object]:
        raw = await self._download_bytes(url, _MAX_METADATA_BYTES, "wheel metadata")
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError):
            raise PreparationError("wheel metadata is invalid") from None
        if not isinstance(value, dict):
            raise PreparationError("wheel metadata is invalid")
        return value

    async def _download_bytes(self, url: str, maximum: int, label: str) -> bytes:
        _validate_download_url(url)
        chunks: list[bytes] = []
        total = 0
        try:
            client = self._http_client()
            async with client.stream(
                "GET",
                url,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                _validate_response(response, maximum)
                async for chunk in response.aiter_raw(_CHUNK_BYTES):
                    total += len(chunk)
                    if total > maximum:
                        raise PreparationError(f"{label} exceeds the size limit")
                    chunks.append(chunk)
        except asyncio.CancelledError:
            raise
        except PreparationError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            raise PreparationError(f"{label} download failed") from None
        return b"".join(chunks)

    async def _download_wheel(
        self, url: str, destination: Path, expected_sha256: str
    ) -> None:
        _validate_download_url(url)
        digest = hashlib.sha256()
        total = 0
        try:
            client = self._http_client()
            with destination.open("xb") as output:
                async with client.stream(
                    "GET",
                    url,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    _validate_response(response, _MAX_WHEEL_BYTES)
                    async for chunk in response.aiter_raw(_CHUNK_BYTES):
                        total += len(chunk)
                        if total > _MAX_WHEEL_BYTES:
                            raise PreparationError(
                                "locked wheel exceeds the size limit"
                            )
                        digest.update(chunk)
                        output.write(chunk)
        except asyncio.CancelledError:
            with suppress(OSError):
                destination.unlink()
            raise
        except PreparationError:
            with suppress(OSError):
                destination.unlink()
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            with suppress(OSError):
                destination.unlink()
            raise PreparationError("locked wheel download failed") from None
        if digest.hexdigest() != expected_sha256:
            with suppress(OSError):
                destination.unlink()
            raise PreparationError("locked wheel hash does not match the lock")

    def _ready_snapshot(self) -> dict[str, Any]:
        if not self._store.ready:
            if self._store.has_pending:
                raise CommitUnconfirmedError("a state commit is pending")
            raise StateUnavailableError("state store is unavailable")
        return self._store.snapshot()

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=_public_ip_transport(),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    def _artifact_path(self, sha256: str) -> Path:
        return self._cache_dir / f"{sha256}.pyz"

    async def _reclaim_ready_cache(self) -> None:
        if not self._store.ready:
            return
        references = _snapshot_cache_references(self._store.snapshot())
        await _run_in_thread(_reclaim_cache_files, self._cache_dir, set(references))


def _validate_task(task: TaskConfig, inputs: TianyiPreparationInputs) -> None:
    if not isinstance(task, TaskConfig):
        raise PreparationError("task is not a parsed task configuration")
    expected = inputs.task
    fields = tuple(
        TianyiUserField(
            field_id=field.field_id,
            label=field.label,
            env_name=field.env_name,
            value_type=field.value_type,
            required=field.required,
            secret=field.secret,
            hint=field.hint,
        )
        for field in task.user_fields
    )
    if (
        task.runtime != expected.runtime
        or task.script_url != expected.script_url
        or task.source_commit != expected.source_commit
        or task.sha256 != expected.source_sha256
        or fields != expected.user_fields
        or task.fixed_env != expected.fixed_env
        or task.input_adapter != expected.input_adapter
        or task.result_adapter != expected.result_adapter
        or task.dependency_bundle != expected.dependency_bundle
    ):
        raise PreparationError("task does not match the fixed Tianyi declaration")


def _cache_reference(record: object) -> tuple[str, int]:
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        raise PreparationError("prepared artifact metadata is invalid")
    sha256 = record.get("sha256")
    size = record.get("size")
    if (
        not isinstance(sha256, str)
        or _HEX_64.fullmatch(sha256) is None
        or type(size) is not int
        or size <= 0
        or size > _MAX_ARTIFACT_BYTES
    ):
        raise PreparationError("prepared artifact metadata is invalid")
    return sha256, size


def _add_cache_reference(references: dict[str, int], record: object) -> None:
    sha256, size = _cache_reference(record)
    previous = references.setdefault(sha256, size)
    if previous != size:
        raise PreparationError("prepared artifact metadata is inconsistent")


def _check_cache_quota(references: Mapping[str, int]) -> None:
    if (
        len(references) > _MAX_CACHE_FILES
        or sum(references.values()) > _MAX_CACHE_BYTES
    ):
        raise PreparationError("prepared artifact cache exceeds its limit")


def _snapshot_cache_references(snapshot: Mapping[str, object]) -> dict[str, int]:
    records = snapshot.get("prepared_artifacts")
    if type(records) is not dict:
        raise StateUnavailableError("prepared artifact state is unavailable")
    references: dict[str, int] = {}
    for record in records.values():
        _add_cache_reference(references, record)
    _check_cache_quota(references)
    return references


def _prospective_cache_references(
    snapshot: Mapping[str, object], task_id: str, replacement: Mapping[str, object]
) -> dict[str, int]:
    records = snapshot.get("prepared_artifacts")
    if type(records) is not dict:
        raise StateUnavailableError("prepared artifact state is unavailable")
    references: dict[str, int] = {}
    for existing_task_id, record in records.items():
        if existing_task_id != task_id:
            _add_cache_reference(references, record)
    _add_cache_reference(references, replacement)
    _check_cache_quota(references)
    return references


def _current_record(
    snapshot: Mapping[str, object],
    task: TaskConfig,
    inputs: TianyiPreparationInputs,
) -> dict[str, Any] | None:
    records = snapshot.get("prepared_artifacts")
    if type(records) is not dict:
        raise StateUnavailableError("prepared artifact state is unavailable")
    value = records.get(task.task_id)
    if type(value) is not dict or set(value) != _RECORD_FIELDS:
        return None
    size = value.get("size")
    sha256 = value.get("sha256")
    if (
        value.get("task_version") != task.version
        or value.get("source_commit") != inputs.task.source_commit
        or value.get("source_sha256") != inputs.task.source_sha256
        or value.get("inputs_sha256") != inputs.inputs_sha256
        or not isinstance(sha256, str)
        or _HEX_64.fullmatch(sha256) is None
        or type(size) is not int
        or size <= 0
        or size > _MAX_ARTIFACT_BYTES
    ):
        return None
    return value


def _artifact_from_record(script: bytes, record: Mapping[str, Any]) -> PreparedArtifact:
    return PreparedArtifact(
        script=script,
        sha256=record["sha256"],
        source_commit=record["source_commit"],
        source_sha256=record["source_sha256"],
        inputs_sha256=record["inputs_sha256"],
    )


def _read_artifact_file(path: Path, size: int, sha256: str) -> bytes | None:
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != size
            or metadata.st_size > _MAX_ARTIFACT_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return None
        script = b"".join(chunks)
        if hashlib.sha256(script).hexdigest() != sha256:
            return None
        return script
    except OSError:
        return None
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _write_artifact_file(cache_dir: Path, sha256: str, script: bytes) -> None:
    if (
        len(script) > _MAX_ARTIFACT_BYTES
        or hashlib.sha256(script).hexdigest() != sha256
    ):
        raise PreparationError("prepared artifact is invalid")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            raise PreparationError("prepared artifact cache is invalid")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact-", dir=cache_dir
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(script)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, cache_dir / f"{sha256}.pyz")
            directory_descriptor = os.open(cache_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink()
    except PreparationError:
        raise
    except OSError:
        raise PreparationError("prepared artifact could not be cached") from None


def _reclaim_cache_files(cache_dir: Path, referenced: set[str]) -> None:
    try:
        if not cache_dir.exists():
            return
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            raise PreparationError("prepared artifact cache is invalid")
        for child in cache_dir.iterdir():
            match = _ARTIFACT_NAME.fullmatch(child.name)
            if match is None or child.is_symlink():
                continue
            try:
                metadata = child.stat()
            except OSError:
                raise PreparationError(
                    "prepared artifact cache cannot be inspected"
                ) from None
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if match.group(1) not in referenced:
                child.unlink()
                continue
    except PreparationError:
        raise
    except OSError:
        raise PreparationError("prepared artifact cache cannot be maintained") from None


def _select_locked_wheel(
    metadata: Mapping[str, object], requirement: TianyiRequirement
) -> tuple[str, str]:
    info = metadata.get("info")
    urls = metadata.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise PreparationError("wheel metadata is invalid")
    if (
        _canonical_distribution(info.get("name")) != requirement.name
        or info.get("version") != requirement.version
    ):
        raise PreparationError("wheel metadata identity does not match the lock")

    candidates: list[tuple[str, str]] = []
    for entry in urls:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        digests = entry.get("digests")
        url = entry.get("url")
        if (
            entry.get("packagetype") != "bdist_wheel"
            or not isinstance(filename, str)
            or not isinstance(digests, dict)
            or digests.get("sha256") != requirement.sha256
            or not isinstance(url, str)
            or _wheel_identity(filename)
            != (requirement.name, requirement.version, "py3-none-any")
        ):
            continue
        parsed = _validate_download_url(url)
        if parsed.hostname != _FILES_HOST or parsed.path.rsplit("/", 1)[-1] != filename:
            raise PreparationError("locked wheel download source is invalid")
        candidates.append((filename, url))
    if len(candidates) != 1:
        raise PreparationError("wheel metadata has no unique locked pure-Python wheel")
    return candidates[0]


def _wheel_identity(filename: str) -> tuple[str, str, str] | None:
    if "/" in filename or "\\" in filename or not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) != 5 or not parts[0] or not parts[1]:
        return None
    return (
        _canonical_distribution(parts[0]),
        parts[1],
        f"{parts[2]}-{parts[3]}-{parts[4]}",
    )


def _canonical_distribution(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return re.sub(r"[-_.]+", "-", value).lower()


async def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    if host not in _ALLOWED_HOSTS or port != 443:
        raise PreparationError("download source is not allowed")
    try:
        answers = await _run_in_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except asyncio.CancelledError:
        raise
    except (OSError, RuntimeError):
        raise PreparationError("download source could not be resolved") from None
    addresses: list[str] = []
    try:
        for answer in answers:
            address = ipaddress.ip_address(answer[4][0])
            if not address.is_global:
                raise PreparationError("download source resolved unsafely")
            normalized = str(address)
            if normalized not in addresses:
                addresses.append(normalized)
    except (IndexError, TypeError, ValueError):
        raise PreparationError("download source resolved unsafely") from None
    if not addresses:
        raise PreparationError("download source could not be resolved")
    return tuple(addresses)


def _public_ip_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    )
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.AsyncConnectionPool):
        raise PreparationError("secure HTTP transport is unavailable")
    # The pool retains the original URL origin. Only connect_tcp is pinned, so
    # httpcore still supplies the original hostname to TLS SNI and the Host header.
    pool._network_backend = _PublicIPNetworkBackend()
    return transport


def _validate_download_url(url: str):
    try:
        # Validate exactly what HTTPX receives before urlsplit strips controls.
        httpx.URL(url)
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError, httpx.InvalidURL):
        raise PreparationError("download source is not allowed") from None
    if (
        parsed.scheme != "https"
        or parsed.netloc not in _ALLOWED_HOSTS
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise PreparationError("download source is not allowed")
    return parsed


def _validate_response(response: httpx.Response, maximum: int) -> None:
    if response.status_code != 200:
        raise PreparationError("download response was rejected")
    if (
        response.headers.get("content-encoding", "identity").strip().lower()
        != "identity"
    ):
        raise PreparationError("encoded download responses are not accepted")
    content_length = response.headers.get("content-length")
    if content_length is None:
        return
    try:
        length = int(content_length, 10)
    except ValueError:
        raise PreparationError("download response length is invalid") from None
    if length < 0 or length > maximum:
        raise PreparationError("download response exceeds the size limit")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


async def _run_in_thread(function: Callable[..., Any], *args: Any) -> Any:
    thread_task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError:
        await _wait_uninterruptibly(thread_task)
        raise


async def _wait_uninterruptibly(task: asyncio.Task[Any]) -> None:
    waiter = asyncio.gather(task, return_exceptions=True)
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            continue
