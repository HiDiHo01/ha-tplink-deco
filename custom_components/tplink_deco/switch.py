from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COORDINATOR_DECOS_KEY
from .const import COORDINATOR_WIRELESS_KEY
from .const import DOMAIN
from .device import create_device_info
from .coordinator import TpLinkDeco
from .coordinator import TplinkDecoUpdateCoordinator
from .wireless import TplinkDecoWirelessUpdateCoordinator

LEGACY_POLLING_UNIQUE_ID = "tplink_deco_polling"


def polling_unique_id(config_entry_id: str) -> str:
    """Return the polling switch unique ID for a single config entry."""
    return f"{LEGACY_POLLING_UNIQUE_ID}_{config_entry_id}"


def _migrate_polling_switch_unique_id(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Migrate only this entry's legacy switch, preserving its entity ID."""
    registry = er.async_get(hass)
    legacy_entity_id = registry.async_get_entity_id(
        SWITCH_DOMAIN, DOMAIN, LEGACY_POLLING_UNIQUE_ID
    )
    if legacy_entity_id is None:
        return

    legacy_entry = registry.async_get(legacy_entity_id)
    if legacy_entry is None or legacy_entry.config_entry_id != config_entry.entry_id:
        return

    new_unique_id = polling_unique_id(config_entry.entry_id)
    # A scoped switch already in the registry must never be overwritten.
    if registry.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, new_unique_id) is not None:
        return

    registry.async_update_entity(legacy_entity_id, new_unique_id=new_unique_id)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up switch."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]

    _migrate_polling_switch_unique_id(hass, config_entry)
    async_add_entities([DecoPollingSwitch(coordinator)])

    wireless = hass.data[DOMAIN][config_entry.entry_id].get(COORDINATOR_WIRELESS_KEY)
    if wireless is None:
        return

    discovered_masters: set[str] = set()

    @callback
    def discover_guest_wifi_switch() -> None:
        """Register a guest switch once for each master with known WLAN state."""
        master = coordinator.data.master_deco
        if (
            master is None
            or master.mac in discovered_masters
            or not isinstance(wireless.data, dict)
            or type(wireless.data.get("guest")) is not bool
        ):
            return
        async_add_entities([DecoGuestWifiSwitch(wireless, coordinator, master)])
        discovered_masters.add(master.mac)

    config_entry.async_on_unload(
        wireless.async_add_listener(discover_guest_wifi_switch)
    )
    config_entry.async_on_unload(
        coordinator.async_add_listener(discover_guest_wifi_switch)
    )
    discover_guest_wifi_switch()


class DecoPollingSwitch(SwitchEntity):
    """Switch to control Deco polling."""

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_name = "Polling"
        self._attr_unique_id = polling_unique_id(coordinator.config_entry.entry_id)
        self._attr_icon = "mdi:lan-connect"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach switch to the master Deco device."""
        master_deco = self.coordinator.data.master_deco
        return create_device_info(master_deco, master_deco, self.coordinator)

    @property
    def is_on(self) -> bool:
        """Return True if polling is active."""
        return not self.coordinator.paused

    async def async_turn_off(self, **kwargs):
        """Pause polling."""
        self.coordinator.paused = True
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Resume polling."""
        self.coordinator.paused = False
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()


class DecoGuestWifiSwitch(
    CoordinatorEntity[TplinkDecoWirelessUpdateCoordinator], SwitchEntity
):
    """Enable or disable a firmware-supported guest network on its master."""

    _attr_has_entity_name = True
    _attr_translation_key = "guest_wifi"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:wifi-lock-open"

    def __init__(
        self,
        coordinator: TplinkDecoWirelessUpdateCoordinator,
        deco_coordinator: TplinkDecoUpdateCoordinator,
        master_deco: TpLinkDeco,
    ) -> None:
        """Keep the switch on its original master and entry-owned coordinator."""
        super().__init__(coordinator)
        self._deco_coordinator = deco_coordinator
        self._master_deco = master_deco
        self._attr_unique_id = f"{master_deco.mac}_wifi_guest_switch"

    async def async_added_to_hass(self) -> None:
        """Re-evaluate eligibility immediately after a master change."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._deco_coordinator.async_add_listener(self._handle_deco_update)
        )

    @callback
    def _handle_deco_update(self) -> None:
        """Refresh availability independently of the WLAN polling interval."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo | None:
        """Associate this control with the correct master Deco device."""
        return create_device_info(
            self._master_deco, self._master_deco, self._deco_coordinator
        )

    @property
    def available(self) -> bool:
        """Require a fresh, supported and unambiguous master-owned status."""
        return (
            super().available
            and not self.coordinator._guest_write_lock.locked()
            and self.coordinator.guest_write_generation(self._master_deco.mac)
            is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Report the observed guest status, not a requested action."""
        data = self.coordinator.data
        return data.get("guest") if isinstance(data, dict) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the supported guest bands and verify their new state."""
        await self.coordinator.async_set_guest_network(self._master_deco.mac, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the supported guest bands and verify their new state."""
        await self.coordinator.async_set_guest_network(self._master_deco.mac, False)
