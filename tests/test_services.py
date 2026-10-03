"""Tests for services."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from custom_components.homeconnect_ws.const import DOMAIN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeconnect_websocket import CodeResponsError
from homeconnect_websocket.entities import Access, EntityDescription, Option
from homeconnect_websocket.message import Action, Message

from . import setup_config_entry
from .const import CONFIG_ENTRIES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeconnect_websocket.testutils import MockAppliance

FINISH_IN_UID = 410


def add_finish_in(appliance: MockAppliance, access: Access, *, available: bool) -> Option:
    """Add a FinishInRelative option to the appliance."""
    option = Option(
        EntityDescription(
            uid=FINISH_IN_UID,
            name="BSH.Common.Option.FinishInRelative",
            access=access,
            available=available,
            stepSize=60,
            max=86400,
        ),
        appliance,
    )
    appliance.entities[option.name] = option
    appliance.entities_uid[option.uid] = option
    return option


def get_device_id(hass: HomeAssistant) -> str:
    """Get the device id of the config entry."""
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), CONFIG_ENTRIES[0].entry_id)
    return devices[0].id


async def test_set_finish_in(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in writes the option, even if it is reported as read only."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    # e.g. while the program waits for its delayed start
    add_finish_in(mock_appliance, Access.READ, available=False)

    await hass.services.async_call(
        DOMAIN,
        "set_finish_in",
        {"device_id": get_device_id(hass), "finish_in": {"hours": 2, "seconds": 40}},
        blocking=True,
    )

    mock_appliance.session.send_sync.assert_awaited_once_with(
        Message(
            resource="/ro/values",
            action=Action.POST,
            data={"uid": FINISH_IN_UID, "value": 7260},
        )
    )


async def test_set_finish_in_error(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in reports the error code of the appliance."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ_WRITE, available=True)
    mock_appliance.session.send_sync.side_effect = CodeResponsError(532, "/ro/values")

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            DOMAIN,
            "set_finish_in",
            {"device_id": get_device_id(hass), "finish_in": {"minutes": 30}},
            blocking=True,
        )
    assert exc_info.value.translation_key == "set_option_error"
    assert exc_info.value.translation_placeholders["code"] == "532"


async def test_set_start_in_not_available(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_start_in on an appliance without StartInRelative."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            DOMAIN,
            "set_start_in",
            {"device_id": get_device_id(hass), "start_in": {"hours": 1}},
            blocking=True,
        )
    assert exc_info.value.translation_key == "start_in_not_available"
    mock_appliance.session.send_sync.assert_not_awaited()


async def test_start_program_finish_in(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test start_program sends finish_in and only available options."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ_WRITE, available=True)
    await mock_appliance.entities["Test.SelectedProgram"].update({"value": 500})
    await mock_appliance.entities["Test.Option1"].update({"value": 1})
    await mock_appliance.entities["Test.Option2"].update({"value": 2, "available": False})

    await hass.services.async_call(
        DOMAIN,
        "start_program",
        {"device_id": get_device_id(hass), "finish_in": {"hours": 3}},
        blocking=True,
    )

    mock_appliance.session.send_sync.assert_awaited_once_with(
        Message(
            resource="/ro/activeProgram",
            action=Action.POST,
            data={
                "program": 500,
                "options": [{"uid": 401, "value": 1}, {"uid": FINISH_IN_UID, "value": 10800}],
            },
        )
    )
