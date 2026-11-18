"""TP-Link Deco Coordinator"""

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import TplinkDecoApi
from .const import DOMAIN, SIGNAL_CLIENT_ADDED, SIGNAL_DECO_ADDED
from .exceptions import LoginForbiddenException, LoginInvalidException

_LOGGER: logging.Logger = logging.getLogger(__name__)


def bits_to_kilobytes(bits_count):
    return bits_count / 8 if bits_count is not None else bits_count


def filter_invalid_ip(ip_address):
    try:
        # Check that it parsed to an IP address
        ipaddress.ip_address(ip_address)
        return ip_address
    except ValueError:
        return None


def snake_case_to_title_space(str):
    return " ".join([w.title() for w in str.split("_")])


async def async_call_and_propagate_config_error(func, *args):
    try:
        return await func(*args)
    except (LoginForbiddenException, LoginInvalidException) as err:
        raise ConfigEntryAuthFailed from err


class TpLinkDeco:
    """Class to manage TP-Link Deco device."""

    def __init__(self, mac: str) -> None:
        self.mac = mac

        self.hw_version = None
        self.sw_version = None
        self.device_model = None
        self.device_type = None

        self.name = None
        self.nickname = None
        self.ip_address = None
        self.client_mesh = None
        self.online = None
        self.inet = None
        self.group_status = None
        self.internet_online = None
        self.role = None
        self.master = None
        self.slave = None
        self.standalone = None
        self.topology_auto = None
        self.topology_device_id = None
        self.connection_type = None
        self.interface = None
        self.previous = None
        self.bssid_band2_4 = None
        self.bssid_band5 = None
        self.bssid_sta_2g = None
        self.bssid_sta_5g = None
        self.set_gateway_support = None
        self.speed_get_support = None
        self.signal_band2_4 = None
        self.signal_band5 = None
        self.backhaul_speed = None
        self.backhaul_max_speed = None
        self.cpu_usage = None
        self.cpu_usage_raw = None
        self.mem_usage = None
        self.mem_usage_raw = None
        self.inet_error_msg = None

    def update(
        self,
        data: dict[str, Any],
    ) -> None:
        self.hw_version = data.get("hardware_ver")
        self.sw_version = data.get("software_ver")
        self.device_model = data.get("device_model")
        self.device_type = data.get("device_type")
        self.name = data.get("custom_nickname")  # Only set if custom value
        self.nickname = data.get("nickname")  # Always set, even if same as name
        if self.name is None:
            self.name = snake_case_to_title_space(self.nickname)
        self.ip_address = filter_invalid_ip(data.get("device_ip"))
        self.client_mesh = data.get("client_mesh")
        self.previous = data.get("previous")  # MAC van upstream node
        self.online = data.get("group_status") == "connected"
        self.group_status = data.get("group_status")
        self.inet_status = data.get("inet_status")
        self.internet_online = None
        if self.inet_status is None:
            self.internet_online = None
        elif isinstance(self.inet_status, str):
            self.internet_online = self.inet_status.lower() in ("online", "connected", "up")
        else:
            self.internet_online = bool(self.inet_status)
        self.role = data.get("role")
        self.master = data.get("role") == "master"
        self.slave = data.get("role") == "slave"
        topology = data.get("topology", {})
        self.topology_auto = topology.get("auto")
        self.topology_device_id = topology.get("device_id")
        self.connection_type = data.get("connection_type")
        self.interface = data.get("interface")
        self.bssid_band2_4 = data.get("bssid_2g")
        self.bssid_band5 = data.get("bssid_5g")
        self.bssid_sta_2g = data.get("bssid_sta_2g")
        self.bssid_sta_5g = data.get("bssid_sta_5g")
        self.set_gateway_support = data.get("set_gateway_support")
        self.speed_get_support = data.get("speed_get_support")
        signal_level = data.get("signal_level")
        self.signal_band2_4 = int(signal_level.get("band2_4") or 0) if signal_level else 0
        self.signal_band5 = int(signal_level.get("band5") or 0) if signal_level else 0
        self.backhaul_speed = data.get("backhaul_speed") or data.get("backhual_speed")
        self.backhaul_max_speed = data.get("backhaul_max_speed") or data.get("backhual_max_speed")
        self.inet_error_msg = data.get("inet_error_msg")


class TpLinkDecoClient:
    """Class to manage TP-Link Deco Client."""

    def __init__(self, mac: str) -> None:
        self.mac = mac
        self.name = None
        self.ip_address = None
        self.online = False
        self.connection_type = None
        self.interface = None
        self.down_kilobytes_per_s = 0
        self.up_kilobytes_per_s = 0
        self.deco_mac = None
        self.last_activity = None

    def update(
        self,
        data: dict[str, Any],
        deco_mac: str,
        utc_point_in_time: datetime,
    ) -> None:
        self.deco_mac = deco_mac
        self.name = data.get("name")
        self.ip_address = filter_invalid_ip(data.get("ip"))
        self.online = data.get("online")
        self.connection_type = data.get("connection_type")
        self.interface = data.get("interface")
        self.down_kilobytes_per_s = bits_to_kilobytes(data.get("down_speed", 0))
        self.up_kilobytes_per_s = bits_to_kilobytes(data.get("up_speed", 0))
        self.last_activity = utc_point_in_time


class TpLinkDecoData:
    """Class for coordinator data."""

    def __init__(
        self,
        master_deco: TpLinkDeco | None = None,
        decos: dict[str, TpLinkDeco] | None = None,
    ) -> None:
        self.master_deco = master_deco
        self.decos = {} if decos is None else decos

    @property
    def standalone(self) -> bool:
        """True if this entry has only one Deco node (no satellites)."""
        return len(self.decos) == 1


class TplinkDecoUpdateCoordinator(DataUpdateCoordinator[TpLinkDecoData]):
    """Class to manage fetching data from the API."""

    data: TpLinkDecoData

    def __init__(
        self,
        hass: HomeAssistant,
        api: TplinkDecoApi,
        config_entry: ConfigEntry,
        update_interval: timedelta | None = None,
        data: TpLinkDecoData | None = None,
    ) -> None:
        """Initialize."""
        self.api = api
        self._on_close: list[Callable] = []

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}-decos",
            update_interval=update_interval,
        )
        # Must happen after super().__init__
        self.data = TpLinkDecoData() if data is None else data
        _LOGGER.debug("Initialized TpLinkDecoUpdateCoordinator with data: %s", self.data)
        self.paused = False

    async def _async_update_data(self) -> TpLinkDecoData:
        """Update data via api."""
        if self.paused:
            _LOGGER.debug("Deco polling is paused")
            return self.data

        new_decos = await async_call_and_propagate_config_error(
            self.api.async_list_devices
        )

        performance_data = await async_call_and_propagate_config_error(
            self.api.async_get_performance
        )

        old_decos = self.data.decos
        master_deco = None
        deco_added = False
        decos = {}
        for new_deco in new_decos:
            mac = new_deco["mac"]
            deco = old_decos.get(mac)
            if deco is None:
                deco_added = True
                deco = TpLinkDeco(mac)
                _LOGGER.debug("_async_update_data: Found new deco mac=%s", deco.mac)
            deco.update(new_deco)
            decos[mac] = deco
            if deco.master:
                master_deco = deco

        for mac, old_deco in old_decos.items():
            if mac not in decos:
                _LOGGER.debug(
                    "_async_update_data: Deco mac=%s not returned by API, marking offline",
                    mac,
                )
                old_deco.online = False
                old_deco.internet_online = False
                decos[mac] = old_deco

        # Zet globale performance data op de master Deco
        result = performance_data.get("result", {})
        if master_deco is not None:
            cpu_raw = result.get("cpu_usage")
            mem_raw = result.get("mem_usage")

            if cpu_raw is not None:
                cpu_percent = cpu_raw * 100
                master_deco.cpu_usage_raw = round(cpu_percent, 1)

                if master_deco.cpu_usage is not None:
                    master_deco.cpu_usage = round(
                        (master_deco.cpu_usage * 0.7) + (cpu_percent * 0.3), 1
                    )
                else:
                    master_deco.cpu_usage = round(cpu_percent, 1)

            if mem_raw is not None:
                mem_percent = mem_raw * 100
                master_deco.mem_usage_raw = round(mem_percent, 1)

                if master_deco.mem_usage is not None:
                    master_deco.mem_usage = round(
                        (master_deco.mem_usage * 0.7) + (mem_percent * 0.3), 1
                    )
                else:
                    master_deco.mem_usage = round(mem_percent, 1)

        if deco_added:
            async_dispatcher_send(self.hass, SIGNAL_DECO_ADDED)

        # Set standalone on each deco
        is_standalone = len(decos) == 1
        for deco in decos.values():
            deco.standalone = is_standalone and deco.master

        return TpLinkDecoData(master_deco, decos)

    @callback
    def on_close(self, func: CALLBACK_TYPE) -> None:
        """Add a function to call when coordinator is closed."""
        self._on_close.append(func)

    async def async_close(self) -> None:
        """Call functions on close."""
        for func in self._on_close:
            try:
                func()
            except Exception as err:
                _LOGGER.error("Error calling on_close function %s: %s", func, err)
        self._on_close.clear()


class TplinkDecoClientUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: TplinkDecoApi,
        config_entry: ConfigEntry,
        deco_update_coordinator: TplinkDecoUpdateCoordinator | None,
        consider_home_seconds: int,
        update_interval: timedelta | None = None,
        data: dict[str, TpLinkDecoClient] | None = None,
    ) -> None:
        """Initialize."""
        self.api = api
        self._deco_update_coordinator = deco_update_coordinator
        self._consider_home_seconds = consider_home_seconds
        self._on_close: list[Callable] = []

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}-clients",
            update_interval=update_interval,
        )
        # Must happen after super().__init__
        self.data = {} if data is None else data

    async def _async_update_data(self) -> dict[str, TpLinkDecoClient] | None:
        """Update data via api."""
        if self._deco_update_coordinator.paused:
            _LOGGER.debug("Deco client polling is paused")
            return self.data

        if len(self._deco_update_coordinator.data.decos) == 0:
            return self.data  # No decos, so no clients

        old_clients = self.data
        clients = {}
        client_added = False
        # List clients for all decos if _deco_update_coordinator is not provided
        deco_macs = self._deco_update_coordinator.data.decos.keys()
        utc_point_in_time = dt_util.utcnow()
        # Send list client requests in parallel for each deco

        deco_client_responses = await asyncio.gather(
            *[
                async_call_and_propagate_config_error(
                    self.api.async_list_clients, deco_mac
                )
                for deco_mac in deco_macs
            ]
        )

        # if len(deco_client_responses) > 0:
        # deco_macs is not subscriptable, must be iterated
        for deco_mac, deco_clients in zip(deco_macs, deco_client_responses):
            for deco_client in deco_clients:
                client_mac = deco_client["mac"]
                client = old_clients.get(client_mac)
                if client is None:
                    client_added = True
                    client = TpLinkDecoClient(client_mac)
                    _LOGGER.debug(
                        "_async_update_data: Found new client mac=%s", client.mac
                    )
                client.update(deco_client, deco_mac, utc_point_in_time)
                clients[client_mac] = client

        # Copy over clients no longer online
        for client in old_clients.values():
            mac = client.mac
            if mac not in clients:
                clients[mac] = client
                if client.last_activity is None:
                    client.online = False
                else:
                    client.online = (
                        utc_point_in_time - client.last_activity
                    ).total_seconds() < self._consider_home_seconds

        if client_added:
            async_dispatcher_send(self.hass, SIGNAL_CLIENT_ADDED)

        return clients

    @callback
    def on_close(self, func: CALLBACK_TYPE) -> None:
        """Add a function to call when coordinator is closed."""
        self._on_close.append(func)

    async def async_close(self) -> None:
        """Call functions on close."""
        for func in self._on_close:
            try:
                func()
            except Exception as err:
                _LOGGER.error("Error calling on_close function %s: %s", func, err)
        self._on_close.clear()
