"""Tests for services."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from custom_components.homeconnect_ws.const import DOMAIN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeconnect_websocket import CodeResponsError
from homeconnect_websocket.entities import Access, Command, EntityDescription, Option, Status
from homeconnect_websocket.message import Action, Message

from . import setup_config_entry
from .const import CONFIG_ENTRIES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeconnect_websocket.testutils import MockAppliance

FINISH_IN_UID = 410
PAUSE_UID = 310
RESUME_UID = 311
NO_ACCESS = CodeResponsError(519, "/ro/values")
OK = Message(action=Action.RESPONSE)


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


def add_entity(appliance: MockAppliance, entity: Command | Status) -> None:
    """Add an entity to the appliance."""
    appliance.entities[entity.name] = entity
    appliance.entities_uid[entity.uid] = entity
    if isinstance(entity, Command):
        appliance.commands[entity.name] = entity


def finish_in_message(resource: str, program: int | None = None) -> Message:
    """Build the expected message writing finish_in = 3600 s."""
    if program is None:
        data = {"uid": FINISH_IN_UID, "value": 3600}
    else:
        data = {"program": program, "options": [{"uid": FINISH_IN_UID, "value": 3600}]}
    return Message(resource=resource, action=Action.POST, data=data)


async def test_set_finish_in_active_program(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in falls back to /ro/activeProgram when /ro/values is refused."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ, available=True)
    await mock_appliance.entities["Test.ActiveProgram"].update({"value": 500})
    mock_appliance.session.send_sync.side_effect = [NO_ACCESS, OK]

    await hass.services.async_call(
        DOMAIN,
        "set_finish_in",
        {"device_id": get_device_id(hass), "finish_in": {"hours": 1}},
        blocking=True,
    )

    assert [call.args[0] for call in mock_appliance.session.send_sync.await_args_list] == [
        finish_in_message("/ro/values"),
        finish_in_message("/ro/activeProgram", 500),
    ]


async def test_set_finish_in_all_refused(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in tries all variants and reports the last error."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ, available=True)
    await mock_appliance.entities["Test.Option1"].update({"value": 1})
    await mock_appliance.entities["Test.ActiveProgram"].update({"value": 500})
    await mock_appliance.entities["Test.SelectedProgram"].update({"value": 500})
    mock_appliance.session.send_sync.side_effect = [
        NO_ACCESS,
        NO_ACCESS,
        NO_ACCESS,
        CodeResponsError(541, "/ro/selectedProgram"),
    ]

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            DOMAIN,
            "set_finish_in",
            {"device_id": get_device_id(hass), "finish_in": {"hours": 1}},
            blocking=True,
        )
    assert exc_info.value.translation_placeholders["code"] == "541"
    assert [call.args[0] for call in mock_appliance.session.send_sync.await_args_list] == [
        finish_in_message("/ro/values"),
        finish_in_message("/ro/activeProgram", 500),
        Message(
            resource="/ro/activeProgram",
            action=Action.POST,
            data={
                "program": 500,
                "options": [{"uid": 401, "value": 1}, {"uid": FINISH_IN_UID, "value": 3600}],
            },
        ),
        finish_in_message("/ro/selectedProgram", 500),
    ]


async def test_set_finish_in_pause(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in pauses, writes and resumes if allowed."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ, available=True)
    for uid, name in ((PAUSE_UID, "PauseProgram"), (RESUME_UID, "ResumeProgram")):
        add_entity(
            mock_appliance,
            Command(
                EntityDescription(
                    uid=uid,
                    name=f"BSH.Common.Command.{name}",
                    access=Access.WRITE_ONLY,
                    available=True,
                ),
                mock_appliance,
            ),
        )
    add_entity(
        mock_appliance,
        Status(
            EntityDescription(
                uid=320, name="BSH.Common.Status.OperationState", initValue="DelayedStart"
            ),
            mock_appliance,
        ),
    )
    mock_appliance.session.send_sync.side_effect = [NO_ACCESS, OK, OK, OK]

    await hass.services.async_call(
        DOMAIN,
        "set_finish_in",
        {"device_id": get_device_id(hass), "finish_in": {"hours": 1}, "allow_pause": True},
        blocking=True,
    )

    assert [call.args[0] for call in mock_appliance.session.send_sync.await_args_list] == [
        finish_in_message("/ro/values"),
        Message(resource="/ro/values", action=Action.POST, data={"uid": PAUSE_UID, "value": True}),
        finish_in_message("/ro/values"),
        Message(resource="/ro/values", action=Action.POST, data={"uid": RESUME_UID, "value": True}),
    ]


async def test_set_finish_in_no_pause_by_default(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in doesn't pause without allow_pause."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ, available=True)
    mock_appliance.session.send_sync.side_effect = NO_ACCESS

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "set_finish_in",
            {"device_id": get_device_id(hass), "finish_in": {"hours": 1}},
            blocking=True,
        )
    mock_appliance.session.send_sync.assert_awaited_once()


async def test_send_raw(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test send_raw returns the response or the error code."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    data = [{"program": 500, "options": [{"uid": FINISH_IN_UID, "value": 3600}]}]
    mock_appliance.session.send_sync.return_value = Message(
        resource="/ro/activeProgram", action=Action.RESPONSE, data=[]
    )

    response = await hass.services.async_call(
        DOMAIN,
        "send_raw",
        {"device_id": get_device_id(hass), "resource": "/ro/activeProgram", "data": data},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "code": None,
        "message": None,
        "resource": "/ro/activeProgram",
        "data": [],
    }
    mock_appliance.session.send_sync.assert_awaited_once_with(
        Message(resource="/ro/activeProgram", action=Action.POST, data=data)
    )

    mock_appliance.session.send_sync.side_effect = CodeResponsError(519, "/ro/activeProgram")
    response = await hass.services.async_call(
        DOMAIN,
        "send_raw",
        {"device_id": get_device_id(hass), "resource": "/ro/activeProgram", "action": "GET"},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "code": 519,
        "message": "WriteRequest NoAccess",
        "resource": "/ro/activeProgram",
        "data": None,
    }


async def test_describe_option(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test describe_option by key and UID."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    add_finish_in(mock_appliance, Access.READ_WRITE, available=True)
    await mock_appliance.entities_uid[FINISH_IN_UID].update(
        {"access": "Read", "available": False, "value": 7200}
    )

    for key in ("BSH.Common.Option.FinishInRelative", str(FINISH_IN_UID)):
        response = await hass.services.async_call(
            DOMAIN,
            "describe_option",
            {"device_id": get_device_id(hass), "key": key},
            blocking=True,
            return_response=True,
        )
        assert response["uid"] == FINISH_IN_UID
        assert response["access"] == "read"
        assert response["available"] is False
        assert response["value"] == 7200
        assert response["step"] == 60
        assert response["type"] == "Option"
