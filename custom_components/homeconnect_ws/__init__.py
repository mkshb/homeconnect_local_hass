"""The Home Connect Websocket integration."""

from __future__ import annotations

import contextlib
import logging
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DESCRIPTION
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util.hass_dict import HassKey
from homeconnect_websocket import (
    DeviceDescription,
    parse_device_description,
)

from .const import (
    CONF_APPLIANCE_INFO,
    CONF_DESCRIPTION_FILENAME,
    CONF_DEV_OVERRIDE_HOST,
    CONF_DEV_OVERRIDE_PSK,
    CONF_FEATURE_FILENAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import HomeConnectCoordinator
from .entity_descriptions import get_available_entities
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType
    from homeconnect_websocket import HomeAppliance

    from .entity_descriptions import _EntityDescriptionsType

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: {
            vol.Optional(CONF_DEV_OVERRIDE_HOST): str,
            vol.Optional(CONF_DEV_OVERRIDE_PSK): str,
        }
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class HCData:
    """Dataclass for runtime data."""

    appliance: HomeAppliance
    device_info: DeviceInfo
    available_entity_descriptions: _EntityDescriptionsType
    coordinator: HomeConnectCoordinator


@dataclass
class HCConfig:
    """Dataclass for hass.data."""

    override_host: str | None = None
    override_psk: str | None = None


type HCConfigEntry = ConfigEntry[HCData]

HC_KEY: HassKey[HCConfig] = HassKey(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration global config."""
    hass.data.setdefault(DOMAIN, HCConfig())
    if DOMAIN in config:
        hass.data[HC_KEY].override_host = config[DOMAIN].get(CONF_DEV_OVERRIDE_HOST)
        hass.data[HC_KEY].override_psk = config[DOMAIN].get(CONF_DEV_OVERRIDE_PSK)

    async_setup_services(hass)
    return True


def load_description(storage_dir: Path, config_entry: HCConfigEntry) -> DeviceDescription:
    """Load device description from file."""
    with (storage_dir / config_entry.data[CONF_DESCRIPTION_FILENAME]).open() as file:
        device_description_xml = file.read()
    with (storage_dir / config_entry.data[CONF_FEATURE_FILENAME]).open() as file:
        feature_mapping_xml = file.read()
    description = parse_device_description(device_description_xml, feature_mapping_xml)
    description["info"].update(config_entry.data[CONF_APPLIANCE_INFO])
    return description


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HCConfigEntry,
) -> bool:
    """Set up this integration using config entry."""
    if config_entry.version == 1:
        _LOGGER.debug("Setting up %s", config_entry.data[CONF_DESCRIPTION]["info"].get("model"))
        description = deepcopy(config_entry.data[CONF_DESCRIPTION])
    else:
        _LOGGER.debug("Setting up %s", config_entry.data[CONF_APPLIANCE_INFO].get("model"))
        storage_dir = Path(hass.config.path(STORAGE_DIR, DOMAIN))
        description = await hass.async_add_executor_job(load_description, storage_dir, config_entry)

    coordinator = HomeConnectCoordinator(hass, config_entry, description)

    appliance = coordinator.appliance
    device_info = DeviceInfo(
        hw_version=appliance.info.get("hwVersion"),
        identifiers={(DOMAIN, config_entry.unique_id)},
        model=f"{appliance.info.get('type')}",
        model_id=appliance.info.get("vib"),
        sw_version=appliance.info.get("swVersion"),
    )

    if mac := appliance.info.get("mac"):
        device_info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}

    if brand := appliance.info.get("brand"):
        device_info["manufacturer"] = brand.capitalize()

    if (type_ := appliance.info.get("type")) and brand:
        device_info["name"] = f"{brand.capitalize()} {type_}"

    available_entities = get_available_entities(appliance)

    config_entry.runtime_data = HCData(
        appliance=appliance,
        device_info=device_info,
        available_entity_descriptions=available_entities,
        coordinator=coordinator,
    )

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HCConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.version == 1:
        _LOGGER.debug("Unloading %s", entry.data[CONF_DESCRIPTION]["info"].get("vib"))
    else:
        _LOGGER.debug("Unloading %s", entry.data[CONF_APPLIANCE_INFO].get("vib"))

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.coordinator.close()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: HCConfigEntry) -> bool:  # noqa: ARG001
    """Migrate config entry."""
    return True


async def async_remove_entry(hass: HomeAssistant, config_entry: HCConfigEntry) -> None:
    """Remove a config entry."""

    def remove_files(storage_dir: Path, config_entry: HCConfigEntry) -> None:
        """Remove profile files."""
        with contextlib.suppress(FileNotFoundError):
            (storage_dir / Path(config_entry.data[CONF_DESCRIPTION_FILENAME])).unlink()
        with contextlib.suppress(FileNotFoundError):
            (storage_dir / Path(config_entry.data[CONF_FEATURE_FILENAME])).unlink()
        with contextlib.suppress(FileNotFoundError, OSError):
            (storage_dir / Path(config_entry.data[CONF_DESCRIPTION_FILENAME])).parent.rmdir()

    if config_entry.version >= 2:
        storage_dir = Path(hass.config.path(STORAGE_DIR, DOMAIN))
        await hass.async_add_executor_job(remove_files, storage_dir, config_entry)
