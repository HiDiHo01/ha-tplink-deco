"""TP-Link Deco button platform."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COORDINATOR_DECOS_KEY, DOMAIN, SIGNAL_DECO_ADDED
from .coordinator import TpLinkDeco, TplinkDecoUpdateCoordinator
from .device import create_device_info

_LOGGER = logging.getLogger(__name__)


class TplinkDecoRebootButton(CoordinatorEntity[TplinkDecoUpdateCoordinator], ButtonEntity):
    """Button to reboot a single TP-Link Deco node."""

    _attr_has_entity_name = True
    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: TplinkDecoUpdateCoordinator, deco_mac: str) -> None:
        super().__init__(coordinator)
        self._deco_mac = deco_mac
        self._attr_unique_id = f"{deco_mac}_reboot"

    @property
    def _deco(self) -> TpLinkDeco:
        return self.coordinator.data.decos[self._deco_mac]

    @property
    def _is_standalone(self) -> bool:
        """True if this deco is the only node in the mesh (no satellites)."""
        _LOGGER.debug("Checking if deco mac=%s is standalone: master=%s, total_decos=%d",
                      self._deco_mac,
                      self.coordinator.data.master_deco.mac if self.coordinator.data.master_deco else None,
                      len(self.coordinator.data.decos))
        _LOGGER.debug("Standalone %s", self._deco.master and len(self.coordinator.data.decos) == 1)
        return (self._deco.master and len(self.coordinator.data.decos) == 1)

    @property
    def device_info(self) -> DeviceInfo:
        return create_device_info(self._deco, self.coordinator.data.master_deco)

    @property
    def not_in_use_available(self) -> bool:
        master = self.coordinator.data.master_deco
        is_standalone_master = (
            master is not None
            and self._deco_mac == master.mac
            and len(self.coordinator.data.decos) == 1
        )
        return not is_standalone_master and super().available

    async def async_press(self) -> None:
        """Reboot this Deco node."""
        if self._is_standalone:
            await self.coordinator.api.async_reboot_decos([self._deco.mac])
            self._deco.online = False
            self._deco.internet_online = False
            self.coordinator.paused = True
            self.coordinator.async_update_listeners()
            await asyncio.sleep(20)
            self.coordinator.paused = False
            await self.coordinator.async_refresh()
        else:
            # Refresh auth session before attempting reboot
            await self.coordinator.async_request_refresh()

            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(identifiers={(DOMAIN, self._deco_mac)})
            if device is None:
                _LOGGER.error("Device not found in registry for mac=%s", self._deco_mac)
                return
            await self.hass.services.async_call(
                DOMAIN,
                "reboot_deco",
                {"device_id": [device.id]},
                blocking=True,
            )
            # Mark all decos offline if master is rebooting
            if self._deco.master:
                for deco in self.coordinator.data.decos.values():
                    deco.online = False
                    deco.internet_online = False
            else:
                self._deco.online = False
                self._deco.internet_online = False
            self.coordinator.paused = True
            _LOGGER.debug("Before update_listeners: master online=%s, slaves=%s",
                          self._deco.online,
                          {mac: deco.online for mac, deco in self.coordinator.data.decos.items()}
                          )
            # self.coordinator.async_update_listeners()
            # Force coordinator to notify all listeners with current data
            self.coordinator.async_set_updated_data(self.coordinator.data)
            await asyncio.sleep(20)
            self.coordinator.paused = False
            await self.coordinator.async_refresh()


class TpLinkDecoRebootAllButton(CoordinatorEntity, ButtonEntity):
    """Button to reboot all Deco nodes."""

    _attr_icon = "mdi:restart"
    _attr_has_entity_name = True
    _attr_translation_key = "reboot_all"

    def __init__(self, coordinator: TplinkDecoUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_reboot_all"

    @property
    def name(self) -> str:
        return "Reboot All"

    @property
    def device_info(self) -> DeviceInfo | None:
        return create_device_info(
            self.coordinator.data.master_deco,
            self.coordinator.data.master_deco,
        )

    async def async_press(self) -> None:
        macs = list(self.coordinator.data.decos.keys())
        await self.coordinator.api.async_reboot_decos(macs)


async def old_async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator: TplinkDecoUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_DECOS_KEY]

    tracked_macs: set[str] = set()

    @callback
    def _add_untracked_deco_buttons() -> None:
        new_entities: list[ButtonEntity] = []
        for mac, deco in coordinator.data.decos.items():
            if mac in tracked_macs:
                continue
            _LOGGER.debug("Adding reboot button for deco mac=%s", mac)
            new_entities.append(TplinkDecoRebootButton(coordinator, mac))
            tracked_macs.add(mac)

        if new_entities:
            async_add_entities(new_entities)

    _add_untracked_deco_buttons()

    # entities = [TpLinkDecoRebootAllButton(coordinator)]
    entities = []
    if len(coordinator.data.decos) > 1:
        entities.append(TpLinkDecoRebootAllButton(coordinator))
    entities += [
        TplinkDecoRebootButton(coordinator, deco)
        for deco in coordinator.data.decos.values()
    ]
    async_add_entities(entities)

    coordinator.on_close(
        async_dispatcher_connect(hass, SIGNAL_DECO_ADDED, _add_untracked_deco_buttons)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator: TplinkDecoUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_DECOS_KEY]

    tracked_macs: set[str] = set()

    @callback
    def _add_untracked_deco_buttons() -> None:
        new_entities: list[ButtonEntity] = []

        # Add Reboot All button if mesh has more than 1 node and not yet added
        if len(coordinator.data.decos) > 1 and "reboot_all" not in tracked_macs:
            new_entities.append(TpLinkDecoRebootAllButton(coordinator))
            tracked_macs.add("reboot_all")

        for mac in coordinator.data.decos:
            if mac in tracked_macs:
                continue
            _LOGGER.debug("Adding reboot button for deco mac=%s", mac)
            new_entities.append(TplinkDecoRebootButton(coordinator, mac))
            tracked_macs.add(mac)

        if new_entities:
            async_add_entities(new_entities)

    _add_untracked_deco_buttons()

    coordinator.on_close(
        async_dispatcher_connect(hass, SIGNAL_DECO_ADDED, _add_untracked_deco_buttons)
    )
