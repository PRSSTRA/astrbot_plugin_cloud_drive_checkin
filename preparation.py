"""Offline, deterministic Tianyi zipapp preparation."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SOURCE_PATH = Path("third_party/tianyi/index.py")
_VENDOR_LICENSE_PATH = Path("third_party/tianyi/LICENSE")
_LOCK_PATH = Path("third_party/tianyi/requirements.lock")
_SCHEMA_PATH = Path("_conf_schema.json")
_ENTRY_PATH = Path("runners/tianyi_entry.py")
_PROJECT_LICENSE_PATH = Path("LICENSE")

_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_WHEEL_BYTES = 2 * 1024 * 1024
_MAX_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
_MAX_MEMBERS = 4096
_MAX_REPOSITORY_FILE_BYTES = 512 * 1024
_REQUIRED_DISTRIBUTIONS = frozenset(
    {
        "certifi",
        "charset-normalizer",
        "idna",
        "pyasn1",
        "requests",
        "rsa",
        "urllib3",
    }
)
_REQUIREMENT = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*) "
    r"--hash=sha256:([0-9a-f]{64})\Z",
    re.ASCII,
)
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")
_RESERVED_TOP_LEVEL = frozenset({"__main__.py", "tianyi_source.py", "licenses"})


class PreparationError(ValueError):
    """The pinned local inputs cannot produce a safe Tianyi artifact."""


@dataclass(frozen=True)
class PreparedArtifact:
    script: bytes = field(repr=False)
    sha256: str
    source_commit: str
    source_sha256: str
    inputs_sha256: str


@dataclass(frozen=True)
class TianyiRequirement:
    name: str
    version: str
    sha256: str


@dataclass(frozen=True)
class TianyiUserField:
    field_id: str
    label: str
    env_name: str
    value_type: str
    required: bool
    secret: bool
    hint: str


@dataclass(frozen=True)
class TianyiTaskDeclaration:
    task_id: str
    runtime: str
    script_url: str
    source_commit: str
    source_sha256: str
    user_fields: tuple[TianyiUserField, ...]
    fixed_env: tuple[tuple[str, str], ...]
    input_adapter: str
    result_adapter: str
    dependency_bundle: str


@dataclass(frozen=True)
class TianyiPreparationInputs:
    task: TianyiTaskDeclaration
    requirements: tuple[TianyiRequirement, ...]
    inputs_sha256: str


@dataclass(frozen=True)
class _WheelCandidate:
    path: Path
    distribution_token: str
    requirement: TianyiRequirement


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _read_regular_file(path: Path, maximum: int, label: str) -> bytes:
    descriptor: int | None = None
    # A FIFO must reach fstat rather than waiting for a writer in os.open.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise PreparationError(f"{label} must be a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise PreparationError(f"{label} exceeds the size limit")
        return b"".join(chunks)
    except PreparationError:
        raise
    except OSError:
        raise PreparationError(f"cannot read {label} as a regular file") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise PreparationError(f"cannot close {label}") from None


def _json_value(raw: bytes, label: str) -> object:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError

    try:
        decoded = raw.decode("utf-8")
        return json.loads(
            decoded,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError):
        raise PreparationError(f"{label} is not strict JSON") from None


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    value = _json_value(raw, label)
    if not isinstance(value, dict):
        raise PreparationError(f"{label} must contain a JSON object")
    return value


def _source_pin() -> tuple[str, str]:
    raw = _read_regular_file(
        _ROOT / _SCHEMA_PATH, _MAX_REPOSITORY_FILE_BYTES, "configuration schema"
    )
    schema = _json_object(raw, "configuration schema")
    tasks = schema.get("tasks")
    if not isinstance(tasks, dict):
        raise PreparationError("configuration schema has no task defaults")
    defaults = tasks.get("default")
    if not isinstance(defaults, list):
        raise PreparationError("configuration schema has no task defaults")
    matches = [
        item
        for item in defaults
        if isinstance(item, dict) and item.get("task_id") == "tianyi_cloud"
    ]
    if len(matches) != 1:
        raise PreparationError("configuration schema must define one tianyi_cloud task")
    source_commit = matches[0].get("source_commit")
    source_sha256 = matches[0].get("sha256")
    if (
        not isinstance(source_commit, str)
        or _HEX_40.fullmatch(source_commit) is None
        or not isinstance(source_sha256, str)
        or _HEX_64.fullmatch(source_sha256) is None
    ):
        raise PreparationError("tianyi_cloud source identity is invalid")
    return source_commit, source_sha256


def _requirements(raw: bytes | None = None) -> dict[str, TianyiRequirement]:
    if raw is None:
        raw = _read_regular_file(
            _ROOT / _LOCK_PATH,
            _MAX_REPOSITORY_FILE_BYTES,
            "Tianyi dependency lock",
        )
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise PreparationError("Tianyi dependency lock must be ASCII") from None

    requirements: dict[str, TianyiRequirement] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = _REQUIREMENT.fullmatch(line)
        if match is None:
            raise PreparationError("Tianyi dependency lock has an invalid pin")
        raw_name, version, digest = match.groups()
        name = _canonical_distribution(raw_name)
        if name in requirements:
            raise PreparationError(
                "Tianyi dependency lock has a duplicate distribution"
            )
        requirements[name] = TianyiRequirement(name, version, digest)

    if len(requirements) != 7 or frozenset(requirements) != _REQUIRED_DISTRIBUTIONS:
        raise PreparationError(
            "Tianyi dependency lock must contain the seven required pins"
        )
    return requirements


def _packaging_inputs_sha256(
    *,
    source: bytes,
    entry: bytes,
    lock: bytes,
    vendor_license: bytes,
    project_license: bytes,
) -> str:
    digest = hashlib.sha256(b"astrbot-tianyi-packaging-inputs-v1\0")
    for label, payload in (
        ("source", source),
        ("entry", entry),
        ("lock", lock),
        ("vendor-license", vendor_license),
        ("project-license", project_license),
    ):
        encoded_label = label.encode("ascii")
        digest.update(len(encoded_label).to_bytes(2, "big"))
        digest.update(encoded_label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _tianyi_task_mapping(schema: dict[str, object]) -> dict[str, object]:
    tasks = schema.get("tasks")
    defaults = tasks.get("default") if isinstance(tasks, dict) else None
    if not isinstance(defaults, list):
        raise PreparationError("configuration schema has no task defaults")
    matches = [
        item
        for item in defaults
        if isinstance(item, dict) and item.get("task_id") == "tianyi_cloud"
    ]
    if len(matches) != 1:
        raise PreparationError("configuration schema must define one tianyi_cloud task")
    return matches[0]


def _declaration_string(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise PreparationError("tianyi_cloud task declaration is invalid")
    return value


def _task_declaration(schema: dict[str, object]) -> TianyiTaskDeclaration:
    task = _tianyi_task_mapping(schema)
    source_commit = _declaration_string(task, "source_commit")
    source_sha256 = _declaration_string(task, "sha256")
    if (
        _HEX_40.fullmatch(source_commit) is None
        or _HEX_64.fullmatch(source_sha256) is None
    ):
        raise PreparationError("tianyi_cloud source identity is invalid")

    raw_user_fields = task.get("user_fields")
    if isinstance(raw_user_fields, str):
        try:
            encoded_user_fields = raw_user_fields.encode("utf-8")
        except UnicodeEncodeError:
            raise PreparationError("tianyi_cloud user fields are invalid") from None
        decoded_user_fields = _json_value(
            encoded_user_fields, "tianyi_cloud user fields"
        )
    else:
        decoded_user_fields = raw_user_fields
    if not isinstance(decoded_user_fields, list):
        raise PreparationError("tianyi_cloud user fields are invalid")

    field_keys = {
        "field_id",
        "label",
        "env_name",
        "value_type",
        "required",
        "secret",
        "hint",
    }
    user_fields: list[TianyiUserField] = []
    for raw_field in decoded_user_fields:
        if not isinstance(raw_field, dict) or set(raw_field) != field_keys:
            raise PreparationError("tianyi_cloud user fields are invalid")
        strings = (
            raw_field.get("field_id"),
            raw_field.get("label"),
            raw_field.get("env_name"),
            raw_field.get("value_type"),
            raw_field.get("hint"),
        )
        required = raw_field.get("required")
        secret = raw_field.get("secret")
        if (
            not all(isinstance(value, str) for value in strings)
            or type(required) is not bool
            or type(secret) is not bool
        ):
            raise PreparationError("tianyi_cloud user fields are invalid")
        field_id, label, env_name, value_type, hint = strings
        user_fields.append(
            TianyiUserField(
                field_id=field_id,
                label=label,
                env_name=env_name,
                value_type=value_type,
                required=required,
                secret=secret,
                hint=hint,
            )
        )

    raw_fixed_env = task.get("fixed_env")
    if not isinstance(raw_fixed_env, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in raw_fixed_env.items()
    ):
        raise PreparationError("tianyi_cloud fixed environment is invalid")

    return TianyiTaskDeclaration(
        task_id="tianyi_cloud",
        runtime=_declaration_string(task, "runtime"),
        script_url=_declaration_string(task, "script_url"),
        source_commit=source_commit,
        source_sha256=source_sha256,
        user_fields=tuple(user_fields),
        fixed_env=tuple(sorted(raw_fixed_env.items())),
        input_adapter=_declaration_string(task, "input_adapter"),
        result_adapter=_declaration_string(task, "result_adapter"),
        dependency_bundle=_declaration_string(task, "dependency_bundle"),
    )


def load_tianyi_inputs() -> TianyiPreparationInputs:
    """Read and identify every repository input used by Tianyi preparation."""

    schema = _json_object(
        _read_regular_file(
            _ROOT / _SCHEMA_PATH,
            _MAX_REPOSITORY_FILE_BYTES,
            "configuration schema",
        ),
        "configuration schema",
    )
    task = _task_declaration(schema)
    source = _read_regular_file(
        _ROOT / _SOURCE_PATH, _MAX_REPOSITORY_FILE_BYTES, "Tianyi source"
    )
    if hashlib.sha256(source).hexdigest() != task.source_sha256:
        raise PreparationError("Tianyi source hash does not match configuration")
    entry = _read_regular_file(
        _ROOT / _ENTRY_PATH, _MAX_REPOSITORY_FILE_BYTES, "Tianyi zipapp entry"
    )
    lock = _read_regular_file(
        _ROOT / _LOCK_PATH, _MAX_REPOSITORY_FILE_BYTES, "Tianyi dependency lock"
    )
    vendor_license = _read_regular_file(
        _ROOT / _VENDOR_LICENSE_PATH,
        _MAX_REPOSITORY_FILE_BYTES,
        "vendored Tianyi license",
    )
    project_license = _read_regular_file(
        _ROOT / _PROJECT_LICENSE_PATH, _MAX_REPOSITORY_FILE_BYTES, "project license"
    )
    requirements = _requirements(lock)
    return TianyiPreparationInputs(
        task=task,
        requirements=tuple(requirements[name] for name in sorted(requirements)),
        inputs_sha256=_packaging_inputs_sha256(
            source=source,
            entry=entry,
            lock=lock,
            vendor_license=vendor_license,
            project_license=project_license,
        ),
    )


def _wheel_filename(name: str) -> tuple[str, str, str] | None:
    if not name.endswith(".whl"):
        return None
    components = name[:-4].split("-")
    if len(components) != 5:
        return None
    distribution, version, python_tag, abi_tag, platform_tag = components
    if not distribution or not version:
        return None
    return (
        _canonical_distribution(distribution),
        version,
        f"{python_tag}-{abi_tag}-{platform_tag}",
    )


def _select_wheels(
    wheelhouse: Path, requirements: dict[str, TianyiRequirement]
) -> list[_WheelCandidate]:
    if not isinstance(wheelhouse, Path):
        raise TypeError("wheelhouse must be pathlib.Path")
    if wheelhouse.is_symlink():
        raise PreparationError("wheelhouse must not be a symlink")
    try:
        children = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
    except OSError:
        raise PreparationError("wheelhouse must be a readable directory") from None
    if not wheelhouse.is_dir():
        raise PreparationError("wheelhouse must be a readable directory")

    selected: dict[str, _WheelCandidate] = {}
    for child in children:
        identity = _wheel_filename(child.name)
        if identity is None:
            continue
        name, version, tag = identity
        requirement = requirements.get(name)
        if (
            requirement is None
            or version != requirement.version
            or tag != "py3-none-any"
        ):
            continue
        if name in selected:
            raise PreparationError(f"multiple matching wheels found for {name}")
        if child.is_symlink():
            raise PreparationError(f"wheel for {name} must not be a symlink")
        distribution_token = child.name[:-4].split("-", 1)[0]
        selected[name] = _WheelCandidate(child, distribution_token, requirement)

    missing = sorted(set(requirements).difference(selected))
    if missing:
        raise PreparationError(
            "missing locked py3-none-any wheel(s): " + ", ".join(missing)
        )
    return [selected[name] for name in sorted(selected)]


def _member_path(name: str) -> tuple[str, bool]:
    if (
        not isinstance(name, str)
        or not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or any(ord(character) < 32 for character in name)
    ):
        raise PreparationError("wheel contains an invalid member path")
    is_directory = name.endswith("/")
    path = name[:-1] if is_directory else name
    parts = path.split("/")
    if not path or any(
        not part or part in {".", ".."} or ":" in part for part in parts
    ):
        raise PreparationError("wheel contains an invalid member path")
    return path, is_directory


def _metadata_header(data: bytes, field: str, label: str) -> list[str]:
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(data, headersonly=True)
    except (TypeError, ValueError):
        raise PreparationError(f"{label} metadata is invalid") from None
    if message.defects:
        raise PreparationError(f"{label} metadata is invalid")
    values = message.get_all(field, [])
    return [str(value) for value in values]


def _read_wheel(candidate: _WheelCandidate) -> dict[str, bytes]:
    wheel_name = candidate.requirement.name
    raw = _read_regular_file(
        candidate.path, _MAX_WHEEL_BYTES, f"wheel for {wheel_name}"
    )
    if hashlib.sha256(raw).hexdigest() != candidate.requirement.sha256:
        raise PreparationError(f"wheel hash mismatch for {wheel_name}")

    files: dict[str, bytes] = {}
    seen_members: set[str] = set()
    seen_casefolded: set[str] = set()
    uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as wheel:
            members = wheel.infolist()
            if len(members) > _MAX_MEMBERS:
                raise PreparationError(f"wheel for {wheel_name} has too many members")
            for member in members:
                path, is_directory = _member_path(member.filename)
                folded = path.casefold()
                if path in seen_members or folded in seen_casefolded:
                    raise PreparationError(
                        f"wheel for {wheel_name} has duplicate members"
                    )
                seen_members.add(path)
                seen_casefolded.add(folded)

                mode = (member.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if kind == stat.S_IFLNK:
                    raise PreparationError(f"wheel for {wheel_name} contains a symlink")
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise PreparationError(
                        f"wheel for {wheel_name} contains a special file"
                    )
                if (kind == stat.S_IFDIR) != is_directory and kind != 0:
                    raise PreparationError(
                        f"wheel for {wheel_name} has inconsistent member types"
                    )
                if member.flag_bits & 0x1:
                    raise PreparationError(
                        f"wheel for {wheel_name} contains an encrypted member"
                    )
                if member.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise PreparationError(
                        f"wheel for {wheel_name} uses unsupported compression"
                    )
                if is_directory:
                    if member.file_size != 0:
                        raise PreparationError(
                            f"wheel for {wheel_name} has an invalid directory entry"
                        )
                    continue

                first = path.split("/", 1)[0]
                if first.casefold() in _RESERVED_TOP_LEVEL:
                    raise PreparationError(
                        f"wheel for {wheel_name} overwrites a reserved entry"
                    )
                if first.casefold().endswith(".data"):
                    raise PreparationError(
                        f"wheel for {wheel_name} uses an unsupported .data layout"
                    )
                if path.casefold().endswith(_NATIVE_SUFFIXES):
                    raise PreparationError(
                        f"wheel for {wheel_name} contains a native library"
                    )
                if member.file_size < 0 or member.file_size > _MAX_MEMBER_BYTES:
                    raise PreparationError(
                        f"wheel member for {wheel_name} is too large"
                    )
                uncompressed += member.file_size
                if uncompressed > _MAX_UNCOMPRESSED_BYTES:
                    raise PreparationError(f"wheel for {wheel_name} expands too large")
                data = wheel.read(member)
                if len(data) != member.file_size:
                    raise PreparationError(f"wheel for {wheel_name} is truncated")
                files[path] = data
    except PreparationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise PreparationError(
            f"wheel for {wheel_name} is not a valid archive"
        ) from None

    expected_dist_info = (
        f"{candidate.distribution_token}-{candidate.requirement.version}.dist-info"
    )
    dist_infos = {
        path.split("/", 1)[0]
        for path in files
        if path.split("/", 1)[0].casefold().endswith(".dist-info")
    }
    if dist_infos != {expected_dist_info}:
        raise PreparationError(f"wheel identity mismatch for {wheel_name}")
    metadata_path = f"{expected_dist_info}/METADATA"
    wheel_path = f"{expected_dist_info}/WHEEL"
    if metadata_path not in files or wheel_path not in files:
        raise PreparationError(f"wheel metadata is incomplete for {wheel_name}")

    names = _metadata_header(files[metadata_path], "Name", wheel_name)
    versions = _metadata_header(files[metadata_path], "Version", wheel_name)
    if (
        len(names) != 1
        or _canonical_distribution(names[0]) != wheel_name
        or versions != [candidate.requirement.version]
    ):
        raise PreparationError(f"wheel metadata identity mismatch for {wheel_name}")
    purelib = _metadata_header(files[wheel_path], "Root-Is-Purelib", wheel_name)
    tags = _metadata_header(files[wheel_path], "Tag", wheel_name)
    if purelib != ["true"] or not tags or any(tag != "py3-none-any" for tag in tags):
        raise PreparationError(f"wheel for {wheel_name} is not pure py3-none-any")
    return files


class _Entries:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self._casefolded: set[str] = set()
        self._parents: set[str] = set()

    def add(self, path: str, data: bytes, label: str) -> None:
        folded = path.casefold()
        parts = path.split("/")
        parent_keys = {
            "/".join(parts[:index]).casefold() for index in range(1, len(parts))
        }
        if (
            folded in self._casefolded
            or folded in self._parents
            or any(parent in self._casefolded for parent in parent_keys)
        ):
            raise PreparationError(f"archive path collision from {label}")
        self.files[path] = data
        self._casefolded.add(folded)
        self._parents.update(parent_keys)


def _archive(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path in sorted(entries):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.create_version = 20
            info.extract_version = 20
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, entries[path], compresslevel=9)
    artifact = output.getvalue()
    if not artifact or len(artifact) > _MAX_ARTIFACT_BYTES:
        raise PreparationError("prepared Tianyi artifact exceeds the 1 MiB limit")
    return artifact


def build_tianyi(wheelhouse: Path) -> PreparedArtifact:
    """Build the pinned Tianyi executable artifact entirely from local inputs."""

    source_commit, source_sha256 = _source_pin()
    source = _read_regular_file(
        _ROOT / _SOURCE_PATH, _MAX_REPOSITORY_FILE_BYTES, "Tianyi source"
    )
    if hashlib.sha256(source).hexdigest() != source_sha256:
        raise PreparationError("Tianyi source hash does not match configuration")

    lock = _read_regular_file(
        _ROOT / _LOCK_PATH, _MAX_REPOSITORY_FILE_BYTES, "Tianyi dependency lock"
    )
    requirements = _requirements(lock)
    candidates = _select_wheels(wheelhouse, requirements)
    entry = _read_regular_file(
        _ROOT / _ENTRY_PATH, _MAX_REPOSITORY_FILE_BYTES, "Tianyi zipapp entry"
    )
    vendor_license = _read_regular_file(
        _ROOT / _VENDOR_LICENSE_PATH,
        _MAX_REPOSITORY_FILE_BYTES,
        "vendored Tianyi license",
    )
    project_license = _read_regular_file(
        _ROOT / _PROJECT_LICENSE_PATH, _MAX_REPOSITORY_FILE_BYTES, "project license"
    )

    entries = _Entries()
    entries.add("__main__.py", entry, "zipapp entry")
    entries.add("tianyi_source.py", source, "Tianyi source")
    entries.add("licenses/tianyi/LICENSE", vendor_license, "vendored Tianyi license")
    entries.add("licenses/project/LICENSE", project_license, "project license")
    total_uncompressed = sum(len(value) for value in entries.files.values())
    for candidate in candidates:
        wheel_files = _read_wheel(candidate)
        total_uncompressed += sum(len(value) for value in wheel_files.values())
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise PreparationError("prepared Tianyi inputs expand too large")
        for path, data in wheel_files.items():
            entries.add(path, data, f"wheel {candidate.requirement.name}")

    script = _archive(entries.files)
    inputs_sha256 = _packaging_inputs_sha256(
        source=source,
        entry=entry,
        lock=lock,
        vendor_license=vendor_license,
        project_license=project_license,
    )
    return PreparedArtifact(
        script=script,
        sha256=hashlib.sha256(script).hexdigest(),
        source_commit=source_commit,
        source_sha256=source_sha256,
        inputs_sha256=inputs_sha256,
    )
