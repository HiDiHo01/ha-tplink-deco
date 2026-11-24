"""Regression coverage for explicit guest WiFi control and its safety checks."""

import asyncio
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.tplink_deco.api import TplinkDecoApi
from custom_components.tplink_deco.switch import DecoGuestWifiSwitch
from custom_components.tplink_deco.wireless import TplinkDecoWirelessUpdateCoordinator

MASTER = "02:00:00:00:61:01"


@pytest.mark.asyncio
async def test_guest_write_uses_only_encrypted_guest_enable_fields() -> None:
    """Do not transmit SSIDs, credentials or unrelated wireless settings."""
    api = TplinkDecoApi(MagicMock(), "https://192.0.2.1", "user", "password", True)
    api.async_login_if_needed = AsyncMock()
    api._stok = "synthetic"
    api._encode_payload = MagicMock(return_value="cipher")
    api._async_post = AsyncMock(return_value={"data": "response"})
    api._decrypt_data = MagicMock(return_value={"error_code": 0, "result": {}})

    await api.async_set_guest_wireless(("band2_4", "band5_1"), True)

    api._encode_payload.assert_called_once_with(
        {
            "operation": "write",
            "params": {
                "band2_4": {"guest": {"enable": True}},
                "band5_1": {"guest": {"enable": True}},
            },
        }
    )
    api._async_post.assert_awaited_once_with(
        "Set wireless guest network",
        "https://192.0.2.1/cgi-bin/luci/;stok=synthetic/admin/wireless",
        params={"form": "wlan"},
        data="cipher",
    )


@pytest.mark.asyncio
async def test_guest_write_rejects_invalid_input_without_post() -> None:
    """Reject unsupported bands and non-boolean states before router I/O."""
    api = TplinkDecoApi(MagicMock(), "https://192.0.2.1", "user", "password", True)
    api._async_post = AsyncMock()
    for bands, enabled in [
        (("unknown",), True),
        (("band2_4", "band2_4"), True),
        ((), True),
        (("band2_4",), 1),
    ]:
        with pytest.raises(ValueError):
            await api.async_set_guest_wireless(bands, enabled)
    api._async_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_write_is_not_retried_after_uncertain_failure() -> None:
    """Ambiguous firmware/transport failures must never replay a command."""
    api = TplinkDecoApi(MagicMock(), "https://192.0.2.1", "user", "password", True)
    api.async_login_if_needed = AsyncMock()
    api._stok = "synthetic"
    api._encode_payload = MagicMock(return_value="cipher")
    api._async_post = AsyncMock(side_effect=TimeoutError("secret"))

    with pytest.raises(Exception, match="not confirmed"):
        await api.async_set_guest_wireless(("band2_4",), False)
    api._async_post.assert_awaited_once()


def _coordinator() -> TplinkDecoWirelessUpdateCoordinator:
    """Construct a synthetic WLAN coordinator for write-guard regression."""
    wireless = object.__new__(TplinkDecoWirelessUpdateCoordinator)
    wireless._guest_write_lock = asyncio.Lock()
    wireless.api = MagicMock()
    wireless.api.async_get_wireless = AsyncMock(
        return_value={
            "band2_4": {"guest": {"enable": True}},
            "band5_1": {"guest": {"enable": False}},
        }
    )
    wireless.api.async_set_guest_wireless = AsyncMock()
    wireless._deco_coordinator = MagicMock(paused=False)
    wireless._deco_coordinator.data.master_deco.mac = MASTER
    wireless._observed_master_mac = MASTER
    wireless._master_generation = 0
    wireless.data = {"guest": True}
    wireless.last_update_success = True
    wireless.async_refresh = AsyncMock()
    return wireless


@pytest.mark.asyncio
async def test_guest_write_refreshes_observed_router_state() -> None:
    """Always re-read guest state; do not optimistically update it."""
    wireless = _coordinator()
    await wireless.async_set_guest_enabled(True, MASTER)
    wireless.api.async_set_guest_wireless.assert_awaited_once_with(
        ("band2_4", "band5_1"), True
    )
    wireless.async_refresh.assert_awaited_once()
    assert wireless.data == {"guest": True}


@pytest.mark.asyncio
async def test_guest_write_rejects_stale_master_without_io() -> None:
    """Never write through a coordinator belonging to a former master."""
    wireless = _coordinator()
    with pytest.raises(UpdateFailed, match="current master"):
        await wireless.async_set_guest_enabled(False, "different-master")
    wireless.api.async_get_wireless.assert_not_awaited()
    wireless.api.async_set_guest_wireless.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_write_requires_verified_band_flags() -> None:
    """Unrecognized states are not proof of writable firmware support."""
    wireless = _coordinator()
    wireless.api.async_get_wireless.return_value = {
        "band2_4": {"guest": {"enable": "unsupported"}}
    }
    with pytest.raises(UpdateFailed, match="not supported"):
        await wireless.async_set_guest_enabled(False, MASTER)
    wireless.api.async_set_guest_wireless.assert_not_awaited()


def test_guest_switch_preserves_distinct_entity_id() -> None:
    """Keep the existing wireless binary-sensor unique ID untouched."""
    wireless = _coordinator()
    master = MagicMock(mac=MASTER)
    entity = DecoGuestWifiSwitch(wireless, wireless._deco_coordinator, master)
    assert entity.unique_id == f"{MASTER}_guest_wifi_switch"
    assert entity.is_on is True
    assert entity.available is True


@pytest.mark.asyncio
async def test_guest_write_rejects_failed_readback() -> None:
    """Never claim success when router readback contradicts the command."""
    wireless = _coordinator()
    wireless.data = {"guest": False}
    with pytest.raises(UpdateFailed, match="could not be verified"):
        await wireless.async_set_guest_enabled(True, MASTER)
    wireless.api.async_set_guest_wireless.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_write_rejects_master_handover_during_preflight() -> None:
    """Discard stale wireless settings after an A -> B -> A handover."""
    wireless = _coordinator()

    async def changed_master() -> dict[str, object]:
        """Simulate a master generation invalidation during an awaited read."""
        wireless._master_generation += 1
        return {"band2_4": {"guest": {"enable": True}}}

    wireless.api.async_get_wireless.side_effect = changed_master
    with pytest.raises(UpdateFailed, match="Master Deco changed"):
        await wireless.async_set_guest_enabled(False, MASTER)
    wireless.api.async_set_guest_wireless.assert_not_awaited()
