import asyncio

from astrbot.api.star import Context, Star

from .storage import StateStore


_STATE_REGISTRY_ATTRIBUTE = "_cloud_drive_checkin_state_registry"


class CloudDriveCheckin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.state_store: StateStore | None = None

    def _state_slot(self) -> dict:
        registry = getattr(self.context, _STATE_REGISTRY_ATTRIBUTE, None)
        if registry is None:
            registry = {}
            setattr(self.context, _STATE_REGISTRY_ATTRIBUTE, registry)

        slot = registry.get(self.plugin_id)
        if slot is None:
            slot = {"lock": asyncio.Lock(), "owner": None}
            registry[self.plugin_id] = slot
        return slot

    async def initialize(self) -> None:
        slot = self._state_slot()
        async with slot["lock"]:
            owner = slot["owner"]
            if owner is not None and not owner.closed:
                if not owner.closing:
                    raise RuntimeError("the plugin state already has an active owner")
                await owner.close()
                if not owner.closed:
                    raise RuntimeError("the previous plugin state owner did not close")

            store = StateStore(self)
            self.state_store = store
            slot["owner"] = store
            try:
                await store.initialize()
            except asyncio.CancelledError:
                store.stop_accepting()
                raise
            except Exception:
                store.stop_accepting()
                raise

    async def terminate(self) -> None:
        store = self.state_store
        if store is None:
            return

        slot = self._state_slot()
        async with slot["lock"]:
            await store.close()
