from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import wireless_is_enabled
from .const import COORDINATOR_DECOS_KEY, DOMAIN, SIGNAL_DECO_ADDED, WIFI_NETWORKS
from .coordinator import TpLinkDeco, TplinkDecoUpdateCoordinator
from .device import create_device_info

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class TplinkDecoBinarySensorDescription(BinarySensorEntityDescription):
    """Description of a TP-Link Deco binary sensor."""

    value_fn: Callable[[TpLinkDeco], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[TplinkDecoBinarySensorDescription, ...] = (
    TplinkDecoBinarySensorDescription(
        key="internet_online",
        name="Internet online",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda deco: deco.internet_online,
    ),
    TplinkDecoBinarySensorDescription(
        key="online",
        name="Deco online",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda deco: deco.online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor platform."""
    coordinator: TplinkDecoUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_DECOS_KEY]

    tracked_macs: set[str] = set()

    @callback
    def _add_untracked_deco_binary_sensors() -> None:
        """Add binary sensor entities for newly discovered Deco devices."""

        new_entities: list[BinarySensorEntity] = []

        # for mac, deco in coordinator.data.decos.items():
        _LOGGER.debug(
            "Adding untracked Deco binary sensors: decos=%s tracked_macs=%s",
            list(coordinator.data.decos),
            sorted(tracked_macs),
        )
        for mac in coordinator.data.decos:
            if mac in tracked_macs:
                _LOGGER.debug(
                    "Skipping already tracked Deco binary sensors: mac=%s",
                    mac,
                )
                continue
            _LOGGER.debug(
                "Creating binary sensors for Deco: mac=%s descriptions=%d",
                mac,
                len(BINARY_SENSOR_DESCRIPTIONS),
            )
            new_entities.extend(
                TplinkDecoBinarySensor(coordinator, mac, description)
                for description in BINARY_SENSOR_DESCRIPTIONS
            )
            tracked_macs.add(mac)

        if new_entities:
            async_add_entities(new_entities)

    _add_untracked_deco_binary_sensors()

    coordinator.on_close(
        async_dispatcher_connect(hass, SIGNAL_DECO_ADDED, _add_untracked_deco_binary_sensors)
    )

    _LOGGER.debug(
        "WiFi binary sensors: master=%s decos=%s",
        coordinator.data.master_deco.mac
        if coordinator.data.master_deco is not None
        else None,
        list(coordinator.data.decos),
    )
    await _async_add_wifi_binary_sensors(coordinator, async_add_entities)


async def _async_add_wifi_binary_sensors(coordinator, async_add_entities) -> None:
    """Add read-only on/off status sensors for the Deco's WiFi networks.

    Enabling/disabling WiFi is not supported by the Deco local API (the write
    handler errors server-side), but reading the state works reliably.
    """

    _LOGGER.debug(
        "Adding WiFi binary sensors: master=%s",
        coordinator.data.master_deco.mac
        if coordinator.data.master_deco is not None
        else None,
    )

    # WiFi sensors attach to the master Deco; skip on satellite-only entries.
    master_deco = coordinator.data.master_deco
    if master_deco is None:
        _LOGGER.debug("No master Deco for this entry, skipping WiFi status sensors")
        return

    try:
        config = await coordinator.api.async_get_wireless("wlan")
    except Exception as err:  # noqa: BLE001 - skip sensors, don't fail setup
        _LOGGER.warning("Could not read wireless config for WiFi sensors: %s", err)
        return

    entities = []
    for network in WIFI_NETWORKS:
        if wireless_is_enabled(config, network["paths"]) is None:
            continue
        entities.append(
            DecoWifiBinarySensor(
                coordinator,
                master_deco,
                network,
            )
        )
    async_add_entities(entities)


class TplinkDecoBinarySensor(CoordinatorEntity[TplinkDecoUpdateCoordinator], BinarySensorEntity):
    """TP-Link Deco binary sensor."""

    _attr_has_entity_name = True
    entity_description: TplinkDecoBinarySensorDescription

    def __init__(
        self,
        coordinator: TplinkDecoUpdateCoordinator,
        deco_mac: str,
        description: TplinkDecoBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._deco_mac = deco_mac
        self.entity_description = description  # type: ignore[assignment]
        self._attr_unique_id = f"{deco_mac}_{description.key}"

    @property
    def _deco(self) -> TpLinkDeco:
        return self.coordinator.data.decos[self._deco_mac]

    @property
    def is_on(self) -> bool | None:  # type: ignore[override]
        return self.entity_description.value_fn(self._deco)

    @property
    def available(self) -> bool:  # type: ignore[override]
        # return self._deco is not None
        return True

    @property
    def device_info(self) -> DeviceInfo | None:  # type: ignore[override]
        master_deco = self.coordinator.data.master_deco
        return create_device_info(self._deco, master_deco)


class DecoWifiBinarySensor(BinarySensorEntity):
    """Read-only on/off status of one of the Deco's WiFi networks."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TplinkDecoUpdateCoordinator,
        master_deco: TpLinkDeco,
        network: dict[str, object],
    ) -> None:
        """Initialize a Deco WiFi binary sensor."""
        self.coordinator = coordinator
        self._master_deco = master_deco
        self._form = network["form"]
        self._paths = network["paths"]
        self._attr_name = network["name"]
        self._attr_icon = network["icon"]
        self._attr_unique_id = (
            f"{master_deco.mac}_wifi_{network['key']}"
        )
        self._attr_is_on = None

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the sensor to the master Deco."""
        return create_device_info(
            self._master_deco,
            self._master_deco,
        )

    async def async_update(self) -> None:
        """Refresh the on/off state from the Deco."""
        # Respect the polling switch so we don't hit the router while paused.
        if self.coordinator.paused:
            return
        try:
            config = await self.coordinator.api.async_get_wireless(self._form)
            self._attr_is_on = wireless_is_enabled(config, self._paths)
            self._attr_available = self._attr_is_on is not None
        except Exception as err:  # noqa: BLE001 - keep last state, mark unavailable
            _LOGGER.debug("Error updating %s WiFi status: %s", self._form, err)
            self._attr_available = False
