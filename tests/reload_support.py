import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class StarMetadata:
    name: str
    module_path: str
    star_cls: Any
    activated: bool = True

    @property
    def star_cls_type(self):
        return type(self.star_cls)


class ControlledReloadManager:
    """Pinned-manager-shaped lifecycle double backed by real plugin instances."""

    plugin_name = "astrbot_plugin_cloud_drive_checkin"
    loader_detail = "SECRET_LOADER_DIAGNOSTIC"

    def __init__(self, context, plugin):
        self.context = context
        self._pm_lock = asyncio.Lock()
        self.plugin_type = type(plugin)
        self.config = plugin.config
        self.plugin_id = plugin.plugin_id
        self.current_plugin = plugin
        self.current_metadata = None
        self.replacements = []
        self.load_mode = "success"
        self.operations = []
        self.terminate_calls = []
        self.unbind_calls = []
        self.load_calls = []
        self.terminate_started = asyncio.Event()
        self.terminate_finished = asyncio.Event()
        self.load_started = asyncio.Event()
        self.loaded = asyncio.Event()

        self.other_plugin = object()
        self.other_metadata = StarMetadata(
            name="unrelated_plugin",
            module_path="tests.unrelated_plugin.main",
            star_cls=self.other_plugin,
        )
        self.context.stars[:] = [self.other_metadata]
        self.register(plugin)
        self.context._star_manager = self

    def register(self, plugin) -> StarMetadata:
        metadata = StarMetadata(
            name=self.plugin_name,
            module_path=type(plugin).__module__,
            star_cls=plugin,
        )
        self.context.stars.append(metadata)
        self.current_plugin = plugin
        self.current_metadata = metadata
        return metadata

    def remove_target_metadata(self) -> None:
        self.context.stars[:] = [self.other_metadata]
        self.current_metadata = None

    def restore_target_metadata(self, plugin) -> StarMetadata:
        self.remove_target_metadata()
        return self.register(plugin)

    async def _terminate_plugin(self, metadata) -> None:
        self.operations.append(("terminate", metadata))
        self.terminate_calls.append(metadata)
        self.terminate_started.set()
        await metadata.star_cls.terminate()
        self.terminate_finished.set()

    async def _unbind_plugin(self, name, module_path) -> None:
        self.operations.append(("unbind", name, module_path))
        self.unbind_calls.append((name, module_path))
        matches = [
            metadata
            for metadata in self.context.get_all_stars()
            if metadata.name == name and metadata.module_path == module_path
        ]
        if len(matches) != 1:
            raise RuntimeError("controlled registry target is ambiguous")
        self.context.stars.remove(matches[0])
        if self.current_metadata is matches[0]:
            self.current_metadata = None

    async def load(self, specified_module_path=None):
        self.operations.append(("load", specified_module_path))
        self.load_calls.append(specified_module_path)
        self.load_started.set()
        if self.load_mode == "raise":
            raise RuntimeError(self.loader_detail)
        if self.load_mode == "false":
            return False, self.loader_detail
        if self.load_mode == "malformed":
            return {"detail": self.loader_detail}

        replacement = self.plugin_type(self.context, self.config)
        replacement.plugin_id = self.plugin_id
        self.replacements.append(replacement)
        if self.load_mode == "unready":
            self.register(replacement)
            return True, None
        if self.load_mode != "success":
            raise AssertionError(f"unknown controlled load mode: {self.load_mode}")

        await replacement.initialize()
        self.register(replacement)
        self.loaded.set()
        return True, None

    async def recover(self):
        self.remove_target_metadata()
        replacement = self.plugin_type(self.context, self.config)
        replacement.plugin_id = self.plugin_id
        await replacement.initialize()
        self.replacements.append(replacement)
        self.register(replacement)
        return replacement

    async def close(self) -> None:
        seen = set()
        for plugin in reversed([self.current_plugin, *self.replacements]):
            if id(plugin) in seen:
                continue
            seen.add(id(plugin))
            store = getattr(plugin, "state_store", None)
            if store is not None and not store.closed:
                await plugin.terminate()
