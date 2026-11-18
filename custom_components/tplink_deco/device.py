"""TP-Link Deco."""

from homeassistant.const import ATTR_VIA_DEVICE
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo

from .const import DOMAIN
from .coordinator import TpLinkDeco


def create_device_info(
    deco: TpLinkDeco | None,
    master_deco: TpLinkDeco | None
) -> DeviceInfo | None:
    """Return device information for a Deco device."""
    if deco is None:
        return None

    device_info = DeviceInfo(
        identifiers={(DOMAIN, deco.mac)},
        connections={(CONNECTION_NETWORK_MAC, deco.mac)},
        name=f"{deco.name} Deco",
        manufacturer="TP-Link",
        model=deco.device_model,
        sw_version=deco.sw_version,
        hw_version=deco.hw_version,
        configuration_url=f"https://{deco.ip_address}" if deco.ip_address else None,
    )
    # if master_deco is not None and deco != master_deco:
    if master_deco is not None and deco.mac != master_deco.mac:
        device_info[ATTR_VIA_DEVICE] = (DOMAIN, master_deco.mac)

    return device_info


def create_clients_device_info(
    deco: TpLinkDeco | None
) -> DeviceInfo | None:
    """Return device info for the clients sub-device."""
    if deco is None:
        return None
    return DeviceInfo(
        identifiers={(DOMAIN, f"{deco.mac}_clients")},
        name=f"{deco.name} Clients",
        manufacturer="TP-Link",
        model=deco.device_model,
        via_device=(DOMAIN, deco.mac),  # koppel aan de deco node
    )
