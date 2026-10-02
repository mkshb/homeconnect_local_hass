"""Test for config flow zeroconf discovery."""

from __future__ import annotations

import asyncio
from ipaddress import ip_address
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, Mock
from uuid import uuid4

import pytest
from custom_components.homeconnect_ws import config_flow
from custom_components.homeconnect_ws.config_flow import _async_host_reachable
from custom_components.homeconnect_ws.const import (
    CONF_AES_IV,
    CONF_APPLIANCE_INFO,
    CONF_DESCRIPTION_FILENAME,
    CONF_FEATURE_FILENAME,
    CONF_FILE,
    CONF_MANUAL_HOST,
    CONF_PSK,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import CONF_DESCRIPTION, CONF_DEVICE_ID, CONF_HOST, CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import MockAppliance
from .const import MOCK_CONFIG_DATA_1 as MOCK_CONFIG_DATA
from .const import MOCK_TLS_DEVICE_ID, MOCK_TLS_DEVICE_INFO

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from homeassistant.core import HomeAssistant

MOCK_ZEROCONF_DATA = ZeroconfServiceInfo(
    ip_address=ip_address("192.0.2.2"),
    ip_addresses=[ip_address("192.0.2.2")],
    hostname=f"test_brand-test_tls-{MOCK_TLS_DEVICE_ID}.local.",
    name="Test_TLS Test_Brand Test_vib._homeconnect._tcp.local.",
    port=443,
    properties={
        "txtvers": "2",
        "vers": "5.4-3.11.4.1",
        "id": MOCK_TLS_DEVICE_ID,
        "mac": "000000000001",
        "brand": "Test_Brand",
        "type": "Test_TLS",
        "vib": "Test_vib",
        "info": None,
        "tls": "true",
    },
    type="_homeconnect._tcp.local.",
)

UPLOADED_FILE = str(uuid4())


@pytest.fixture(autouse=True)
def mock_host_reachable(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Don't probe discovered hosts."""
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(config_flow, "_async_host_reachable", probe)
    return probe


async def test_zeroconf_init(
    hass: HomeAssistant,
    mock_process_profile_file: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mock_setup_entry: AsyncMock,
    mock_write_file: MagicMock,
    mock_parse_device_description: Mock,
) -> None:
    """Test setup from zeroconf discovery."""
    appliance = MockAppliance(MOCK_TLS_DEVICE_INFO)
    monkeypatch.setattr(config_flow, "HomeAppliance", appliance)

    randbytes = Mock()
    randbytes.return_value = bytes.fromhex("01020304")
    monkeypatch.setattr(config_flow.random, "randbytes", randbytes)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=MOCK_ZEROCONF_DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "upload"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_FILE: UPLOADED_FILE,
        },
    )
    assert appliance.description == mock_parse_device_description.return_value
    assert appliance.host == "192.0.2.2"
    assert appliance.app_name == "Homeassistant"
    assert appliance.app_id == "01020304"
    assert appliance.psk64 == MOCK_TLS_DEVICE_INFO["key"]
    assert appliance.iv64 is None
    assert appliance.connection_callback == ANY

    appliance._connect.assert_awaited_once()
    appliance._close.assert_awaited_once()
    mock_parse_device_description.assert_called_once_with(
        b"TLS_DeviceDescription",
        b"TLS_FeatureMapping",
    )
    mock_process_profile_file.assert_called_once_with(ANY, UPLOADED_FILE)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test_Brand Test_TLS"
    assert result["data"][CONF_DESCRIPTION_FILENAME] == "01020304/DeviceDescription.xml"
    assert result["data"][CONF_FEATURE_FILENAME] == "01020304/FeatureMapping.xml"
    assert result["data"][CONF_HOST] == "192.0.2.2"
    assert result["data"][CONF_PSK] == MOCK_TLS_DEVICE_INFO["key"]
    assert CONF_AES_IV not in result["data"]
    assert result["data"][CONF_NAME] == "Test_Brand Test_TLS"
    assert result["data"][CONF_DEVICE_ID] == "01020304"
    assert result["data"][CONF_APPLIANCE_INFO] == MOCK_TLS_DEVICE_INFO

    mock_write_file.assert_any_call(
        ANY,
        "01020304/DeviceDescription.xml",
        b"TLS_DeviceDescription",
    )

    mock_write_file.assert_any_call(
        ANY,
        "01020304/FeatureMapping.xml",
        b"TLS_FeatureMapping",
    )
    mock_setup_entry.assert_awaited_once()


async def test_zeroconf_duplicate_entry(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test zeroconf discovered duplicate entry."""
    mock_config = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA,
        unique_id=MOCK_TLS_DEVICE_ID,
    )
    mock_config.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=MOCK_ZEROCONF_DATA
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_setup_entry.assert_not_awaited()


async def test_zeroconf_update_host(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test updating host from zeroconf discovery."""
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(config_flow, "_async_host_reachable", probe)
    mock_config = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA,
        unique_id=MOCK_TLS_DEVICE_ID,
    )
    mock_config.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=MOCK_ZEROCONF_DATA
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config.data[CONF_HOST] == "192.0.2.2"
    probe.assert_awaited_once_with("192.0.2.2", 80)
    mock_setup_entry.assert_not_awaited()


async def test_zeroconf_update_manual_host(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test updating host from zeroconf discovery when manual host is set."""
    mock_config = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DESCRIPTION: "description",
            CONF_HOST: "1.2.3.4",
            CONF_PSK: "PSK_KEY",
            CONF_AES_IV: "AES_IV",
            CONF_DEVICE_ID: "Test_Device_ID",
            CONF_NAME: "Fake_Brand HomeAppliance",
            CONF_MANUAL_HOST: True,
        },
        unique_id=MOCK_TLS_DEVICE_ID,
    )
    mock_config.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=MOCK_ZEROCONF_DATA
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config.data[CONF_HOST] == "1.2.3.4"
    mock_setup_entry.assert_not_awaited()


async def test_zeroconf_invalid_discovery_info(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test zeroconf with invalid_discovery_info."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("192.0.2.2"),
            ip_addresses=[ip_address("192.0.2.2")],
            hostname=f"test_brand-test_tls-{MOCK_TLS_DEVICE_ID}.local.",
            name="Test_TLS Test_Brand Test_vib._homeconnect._tcp.local.",
            port=443,
            properties={},
            type="_homeconnect._tcp.local.",
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"
    mock_setup_entry.assert_not_awaited()


@pytest.mark.parametrize(
    ("addresses", "probe_result"),
    [
        # Announced address not reachable (e.g. stale or reflected record)
        (["198.51.100.169"], False),
        # Unusable addresses are ignored without probing
        (["127.0.0.2", "169.254.10.20", "fe80::1", "0.0.0.0"], True),  # noqa: S104
        # Current host still announced
        (["1.2.3.4", "192.0.2.2"], True),
    ],
)
async def test_zeroconf_keep_host(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    addresses: list[str],
    probe_result: bool,  # noqa: FBT001
) -> None:
    """Test discovery doesn't replace the host with an unusable or unreachable address."""
    probe = AsyncMock(return_value=probe_result)
    monkeypatch.setattr(config_flow, "_async_host_reachable", probe)
    mock_config = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA,
        unique_id=MOCK_TLS_DEVICE_ID,
    )
    mock_config.add_to_hass(hass)
    assert mock_config.data[CONF_HOST] == "1.2.3.4"

    ips = [ip_address(address) for address in addresses]
    discovery_info = ZeroconfServiceInfo(
        ip_address=ips[0],
        ip_addresses=ips,
        hostname=MOCK_ZEROCONF_DATA.hostname,
        name=MOCK_ZEROCONF_DATA.name,
        port=443,
        properties=MOCK_ZEROCONF_DATA.properties,
        type=MOCK_ZEROCONF_DATA.type,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery_info
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config.data[CONF_HOST] == "1.2.3.4"
    if probe_result:
        probe.assert_not_awaited()
    else:
        probe.assert_awaited_once_with("198.51.100.169", 80)
        assert "Ignoring discovered address(es) 198.51.100.169" in caplog.text
    mock_setup_entry.assert_not_awaited()


async def test_host_reachable(socket_enabled: None) -> None:
    """Test the TCP reachability probe."""
    probe = _async_host_reachable  # not patched by mock_host_reachable
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert await probe("127.0.0.1", port)
    server.close()
    await server.wait_closed()
    assert not await probe("127.0.0.1", port)
