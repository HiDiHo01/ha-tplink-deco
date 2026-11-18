"""
Custom integration to integrate TP-Link Deco with Home Assistant.

For more details about this integration, please refer to
https://github.com/amosyuen/ha-tplink-deco
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any, cast

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.device_tracker.const import (
    CONF_CONSIDER_HOME,
    CONF_SCAN_INTERVAL,
)
from homeassistant.components.device_tracker.const import (
    DOMAIN as DEVICE_TRACKER_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry, entity_registry, restore_state
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# use voluptuous via alias 'vol'
from .api import TplinkDecoApi
from .const import (
    ATTR_DEVICE_TYPE,
    CONF_CLIENT_POSTFIX,
    CONF_CLIENT_PREFIX,
    CONF_DECO_POSTFIX,
    CONF_DECO_PREFIX,
    CONF_TIMEOUT_ERROR_RETRIES,
    CONF_TIMEOUT_SECONDS,
    CONF_VERIFY_SSL,
    COORDINATOR_CLIENTS_KEY,
    COORDINATOR_DECOS_KEY,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_DECO_POSTFIX,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT_ERROR_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    DEVICE_TYPE_DECO,
    DOMAIN,
    PLATFORMS,
    SERVICE_PAUSE_POLLING,
    SERVICE_REBOOT_DECO,
    SERVICE_RESUME_POLLING,
)
from .coordinator import (
    TpLinkDeco,
    TpLinkDecoClient,
    TplinkDecoClientUpdateCoordinator,
    TpLinkDecoData,
    TplinkDecoUpdateCoordinator,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_create_and_refresh_coordinators(
    hass: HomeAssistant,
    config_data: dict[str, Any],
    config_entry: ConfigEntry,
    consider_home_seconds: int = 1,
    update_interval: timedelta = None,
    deco_data: TpLinkDecoData = None,
    client_data: dict[str, TpLinkDecoClient] = None,
):
    host = str(config_data.get(CONF_HOST))
    username = str(config_data.get(CONF_USERNAME))
    password = str(config_data.get(CONF_PASSWORD))
    timeout_error_retries = int(config_data.get(CONF_TIMEOUT_ERROR_RETRIES, DEFAULT_TIMEOUT_ERROR_RETRIES))
    timeout_seconds = int(config_data.get(CONF_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS))
    verify_ssl = bool(config_data.get(CONF_VERIFY_SSL))
    session = async_get_clientsession(hass)

    api = TplinkDecoApi(
        session,
        host,
        username,
        password,
        verify_ssl,
        timeout_error_retries,
        timeout_seconds,
    )
    deco_coordinator = TplinkDecoUpdateCoordinator(
        hass, api, config_entry, update_interval, deco_data
    )
    if config_entry is None:
        await deco_coordinator._async_update_data()
    else:
        await deco_coordinator.async_config_entry_first_refresh()
    clients_coordinator = TplinkDecoClientUpdateCoordinator(
        hass,
        api,
        config_entry,
        deco_coordinator,
        consider_home_seconds,
        update_interval,
        client_data,
    )
    if config_entry is None:
        await deco_coordinator._async_update_data()
    else:
        await clients_coordinator.async_config_entry_first_refresh()

    return {
        COORDINATOR_DECOS_KEY: deco_coordinator,
        COORDINATOR_CLIENTS_KEY: clients_coordinator,
    }


async def async_create_config_data(hass: HomeAssistant, config_entry: ConfigEntry):
    consider_home_seconds = config_entry.data.get(
        CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME
    )
    scan_interval_seconds = config_entry.data.get(
        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    )
    update_interval = timedelta(seconds=scan_interval_seconds)

    # Load tracked entities from registry
    existing_entries = entity_registry.async_entries_for_config_entry(
        entity_registry.async_get(hass),
        config_entry.entry_id,
    )
    deco_data = TpLinkDecoData()
    client_data = {}

    # Populate client list with existing entries so that we keep track of disconnected clients
    # since deco list_clients only returns connected clients.
    last_states = restore_state.async_get(hass).last_states
    for entry in existing_entries:
        if entry.domain != DEVICE_TRACKER_DOMAIN:
            continue
        state = last_states.get(entry.entity_id)
        if state is None:
            continue
        device_type = state.state.attributes.get(ATTR_DEVICE_TYPE)
        if device_type is None:
            continue
        if device_type == DEVICE_TYPE_DECO:
            deco = TpLinkDeco(entry.unique_id)
            deco.name = entry.original_name
            deco_data.decos[entry.unique_id] = deco
        else:
            client = TpLinkDecoClient(entry.unique_id)
            client.name = entry.original_name
            client_data[entry.unique_id] = client

    return await async_create_and_refresh_coordinators(
        hass,
        dict(config_entry.data),
        config_entry,
        consider_home_seconds,
        update_interval,
        deco_data,
        client_data,
    )


async def async_pause_polling(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Pause Deco polling."""
    coordinator_decos = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]

    if coordinator_decos.paused:
        _LOGGER.info("TP-Link Deco polling is already paused")
        return

    coordinator_decos.paused = True
    _LOGGER.info("TP-Link Deco polling paused")


async def async_resume_polling(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Resume Deco polling."""
    coordinator_decos = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]

    if not coordinator_decos.paused:
        _LOGGER.info("TP-Link Deco polling is already running")
        return

    coordinator_decos.paused = False
    _LOGGER.info("TP-Link Deco polling resumed")
    await coordinator_decos.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Set up this integration using UI."""
    _LOGGER.debug("async_setup_entry: Config entry %s", config_entry.entry_id)

    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})

    data = await async_create_config_data(hass, config_entry)
    hass.data[DOMAIN][config_entry.entry_id] = data
    deco_coordinator = data[COORDINATOR_DECOS_KEY]

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    )

    async def old_async_reboot_deco(service: ServiceCall) -> None:
        coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]
        dr = device_registry.async_get(hass)
        # service.data may contain a list of device IDs; ensure correct type for linters
        device_ids: list[str] = cast(list, service.data.get(ATTR_DEVICE_ID))
        macs = []
        for device_id in device_ids:
            device = dr.async_get(device_id)
            if device is None:
                raise Exception(f"Device ID {device_id} not found")
            id = next(
                (i for i in device.identifiers if i[0] == DOMAIN),
                None,
            )
            if id is None:
                raise Exception(f"Device ID {device_id} has no {DOMAIN} identifier")
            macs.append(id[1])
        await coordinator.api.async_reboot_decos(macs)

    async def test_async_reboot_deco(service: ServiceCall) -> None:
        coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]
        dr = device_registry.async_get(hass)
        device_ids: list[str] = cast(list, service.data.get(ATTR_DEVICE_ID))
        macs = []
        for device_id in device_ids:
            device = dr.async_get(device_id)
            if device is None:
                raise Exception(f"Device ID {device_id} not found")
            id = next(
                (i for i in device.identifiers if i[0] == DOMAIN),
                None,
            )
            if id is None:
                raise Exception(f"Device ID {device_id} has no {DOMAIN} identifier")
            mac = id[1]

            # Skip standalone master — cannot be rebooted via API
            master = coordinator.data.master_deco
            if (
                master is not None
                and mac == master.mac
                and len(coordinator.data.decos) == 1
            ):
                _LOGGER.warning("Skipping reboot of standalone master deco %s — not supported by API", mac)
                continue

            macs.append(mac)

        if macs:
            await coordinator.api.async_reboot_decos(macs)

    async def old_async_reboot_deco(service: ServiceCall) -> None:
        coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]
        dr = device_registry.async_get(hass)
        device_ids: list[str] = cast(list, service.data.get(ATTR_DEVICE_ID))
        macs = []
        for device_id in device_ids:
            device = dr.async_get(device_id)
            if device is None:
                raise Exception(f"Device ID {device_id} not found")
            id = next(
                (i for i in device.identifiers if i[0] == DOMAIN),
                None,
            )
            if id is None:
                raise Exception(f"Device ID {device_id} has no {DOMAIN} identifier")
            mac = id[1]
            _LOGGER.debug("Reboot service: resolved device_id=%s to mac=%s", device_id, mac)
            macs.append(mac)

        _LOGGER.debug("Reboot service: calling async_reboot_decos with macs=%s", macs)
        await coordinator.api.async_reboot_decos(macs)

    async def async_reboot_deco(service: ServiceCall) -> None:
        dr = device_registry.async_get(hass)
        device_ids: list[str] = cast(list, service.data.get(ATTR_DEVICE_ID))

        # Group MACs per coordinator (config entry)
        coordinator_macs: dict[TplinkDecoUpdateCoordinator, list[str]] = {}

        for device_id in device_ids:
            device = dr.async_get(device_id)
            if device is None:
                raise Exception(f"Device ID {device_id} not found")
            identifier = next(
                (i for i in device.identifiers if i[0] == DOMAIN),
                None,
            )
            if identifier is None:
                raise Exception(f"Device ID {device_id} has no {DOMAIN} identifier")
            mac = identifier[1]
            _LOGGER.debug("Reboot service: resolved device_id=%s to mac=%s", device_id, mac)

            # Find the coordinator that owns this MAC
            coordinator = None
            for entry_id, entry_data in hass.data[DOMAIN].items():
                coord = entry_data.get(COORDINATOR_DECOS_KEY)
                if coord is not None and mac in coord.data.decos:
                    coordinator = coord
                    break

            if coordinator is None:
                raise Exception(f"No coordinator found for MAC {mac}")

            coordinator_macs.setdefault(coordinator, []).append(mac)

        for coordinator, macs in coordinator_macs.items():
            _LOGGER.debug("Reboot service: calling async_reboot_decos with macs=%s", macs)
            await coordinator.api.async_reboot_decos(macs)

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBOOT_DECO,
        async_reboot_deco,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list(str), [str]),
            },
            extra=vol.ALLOW_EXTRA,
        ),
    )

    async def async_reboot_all_decos(service: ServiceCall) -> None:
        """Reboot all Deco nodes across all config entries."""
        for entry_data in hass.data[DOMAIN].values():
            coordinator = entry_data.get(COORDINATOR_DECOS_KEY)
            if coordinator is None:
                continue
            macs = list(coordinator.data.decos.keys())
            _LOGGER.debug("Reboot all: rebooting macs=%s", macs)
            await coordinator.api.async_reboot_decos(macs)

    hass.services.async_register(
        DOMAIN,
        "reboot_all_decos",
        async_reboot_all_decos,
    )

    async def handle_pause_polling(service: ServiceCall) -> None:
        """Handle pause polling service."""
        await async_pause_polling(hass, config_entry)

    async def handle_resume_polling(service: ServiceCall) -> None:
        """Handle resume polling service."""
        await async_resume_polling(hass, config_entry)

    hass.services.async_register(
        DOMAIN,
        SERVICE_PAUSE_POLLING,
        handle_pause_polling,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_POLLING,
        handle_resume_polling,
    )

    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    _LOGGER.debug("async_unload_entry: Config entry %s", config_entry.entry_id)

    domain_data = hass.data.get(DOMAIN, {})
    data = domain_data.get(config_entry.entry_id)

    if data is None:
        _LOGGER.debug(
            "async_unload_entry: No stored data for config entry %s",
            config_entry.entry_id,
        )
        return True

    deco_coordinator = data.get(COORDINATOR_DECOS_KEY)
    clients_coordinator = data.get(COORDINATOR_CLIENTS_KEY)

    if deco_coordinator is not None:
        await deco_coordinator.api.async_logout()
        await deco_coordinator.async_close()
    if clients_coordinator is not None:
        await clients_coordinator.async_close()

    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(config_entry, platform)
                for platform in PLATFORMS
            ]
        )
    )

    if unloaded:
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
        hass.services.async_remove(DOMAIN, SERVICE_PAUSE_POLLING)
        hass.services.async_remove(DOMAIN, SERVICE_RESUME_POLLING)

    return unloaded


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.debug("update_listener: Reloading %s", config_entry.entry_id)
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    new = {**config_entry.data}

    if config_entry.version == 1:
        config_entry.version = 2
        new[CONF_VERIFY_SSL] = True

    if config_entry.version == 2:
        config_entry.version = 3
        new[CONF_TIMEOUT_ERROR_RETRIES] = DEFAULT_TIMEOUT_ERROR_RETRIES

    if config_entry.version == 3:
        config_entry.version = 4
        new[CONF_TIMEOUT_SECONDS] = DEFAULT_TIMEOUT_SECONDS

    if config_entry.version == 4:
        config_entry.version = 5
        new[CONF_CLIENT_PREFIX] = ""
        new[CONF_CLIENT_POSTFIX] = ""
        new[CONF_DECO_PREFIX] = ""
        new[CONF_DECO_POSTFIX] = DEFAULT_DECO_POSTFIX

    if config_entry.version == 5:
        config_entry.version = 6
        new[CONF_HOST] = f"http://{new[CONF_HOST]}"

    hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
