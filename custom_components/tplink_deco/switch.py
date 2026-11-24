from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COORDINATOR_DECOS_KEY
from .const import COORDINATOR_WIRELESS_KEY
from .const import DOMAIN
from .device import create_device_info
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

    wireless_coordinator = hass.data[DOMAIN][config_entry.entry_id].get(
        COORDINATOR_WIRELESS_KEY
    )
    if wireless_coordinator is None:
        return

    tracked_masters: set[str] = set()

    @callback
    def add_guest_switch() -> None:
        """Discover the guest control only after a supported WLAN read."""
        master = coordinator.data.master_deco
        if (
            master is None
            or not isinstance(wireless_coordinator.data, dict)
            or "guest" not in wireless_coordinator.data
            or master.mac in tracked_masters
        ):
            return
        async_add_entities(
            [DecoGuestWifiSwitch(wireless_coordinator, coordinator, master)]
        )
        tracked_masters.add(master.mac)

    config_entry.async_on_unload(
        wireless_coordinator.async_add_listener(add_guest_switch)
    )
    config_entry.async_on_unload(coordinator.async_add_listener(add_guest_switch))
    add_guest_switch()


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


class DecoGuestWifiSwitch(CoordinatorEntity, SwitchEntity):
    """Control guest WiFi only on the owning, current master Deco."""

    _attr_has_entity_name = True
    _attr_name = "Guest WiFi"
    _attr_translation_key = "guest_wifi"
    _attr_icon = "mdi:wifi-lock-open"

    def __init__(
        self,
        coordinator: TplinkDecoWirelessUpdateCoordinator,
        deco_coordinator,
        master_deco,
    ) -> None:
        """Associate the switch with its master without replacing status sensors."""
        super().__init__(coordinator)
        self._deco_coordinator = deco_coordinator
        self._master_deco = master_deco
        self._attr_unique_id = f"{master_deco.mac}_guest_wifi_switch"

    async def async_added_to_hass(self) -> None:
        """Observe master handovers before allowing user writes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._deco_coordinator.async_add_listener(self._handle_master_update)
        )

    @callback
    def _handle_master_update(self) -> None:
        """Re-evaluate availability when the selected master changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Reject stale firmware data and formerly owned masters."""
        master = self._deco_coordinator.data.master_deco
        return (
            master is not None
            and master.mac == self._master_deco.mac
            and not self._deco_coordinator.paused
            and super().available
            and isinstance(self.coordinator.data, dict)
            and "guest" in self.coordinator.data
        )

    @property
    def is_on(self) -> bool | None:
        """Report the observed router state, not the commanded state."""
        data = self.coordinator.data
        return data.get("guest") if isinstance(data, dict) else None

    @property
    def device_info(self) -> DeviceInfo:
        """Link the guest switch to the existing master Deco."""
        return create_device_info(
            self._master_deco, self._master_deco, self._deco_coordinator
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the supported guest WiFi bands on the current master."""
        await self.coordinator.async_set_guest_enabled(True, self._master_deco.mac)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the supported guest WiFi bands on the current master."""
        await self.coordinator.async_set_guest_enabled(False, self._master_deco.mac)
