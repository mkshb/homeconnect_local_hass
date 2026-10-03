"""Tests for services."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from custom_components.homeconnect_ws import program_options
from custom_components.homeconnect_ws.const import DOMAIN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
OPERATION_STATE_UID = 320
DURATION_UID = 330
# Eco 40-60, 3:40 h
DURATION = 13200


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


def add_entity(appliance: MockAppliance, entity: Command | Status | Option) -> None:
    """Add an entity to the appliance."""
    appliance.entities[entity.name] = entity
    appliance.entities_uid[entity.uid] = entity
    if isinstance(entity, Command):
        appliance.commands[entity.name] = entity


class FakeWasher:
    """Answer messages like a washer waiting for its delayed start."""

    def __init__(
        self,
        appliance: MockAppliance,
        *,
        finish_in: int = 6 * 3600,
        ignore_resume: int = 0,
        report_duration: bool = True,
        clamp_to: int | None = None,
        keep_clamped: bool = False,
    ) -> None:
        """Add the entities needed for pause and resume."""
        self.appliance = appliance
        self.ignore_resume = ignore_resume
        self.clamp_to = clamp_to
        self.keep_clamped = keep_clamped
        self.clamped = False
        self.finish_in = add_finish_in(appliance, Access.READ, available=True)
        self.finish_in._min = 0
        self.finish_in._value = finish_in
        for uid, name in ((PAUSE_UID, "PauseProgram"), (RESUME_UID, "ResumeProgram")):
            add_entity(
                appliance,
                Command(
                    EntityDescription(
                        uid=uid,
                        name=f"BSH.Common.Command.{name}",
                        access=Access.WRITE_ONLY,
                        available=True,
                    ),
                    appliance,
                ),
            )
        if report_duration:
            add_entity(
                appliance,
                Option(
                    EntityDescription(
                        uid=DURATION_UID,
                        name="BSH.Common.Option.EstimatedTotalProgramTime",
                        access=Access.READ,
                        available=True,
                        initValue=DURATION,
                    ),
                    appliance,
                ),
            )
        self.state = Status(
            EntityDescription(
                uid=OPERATION_STATE_UID,
                name="BSH.Common.Status.OperationState",
                enumeration={"0": "Ready", "1": "DelayedStart", "2": "Pause", "3": "Run"},
                initValue=1,
            ),
            appliance,
        )
        add_entity(appliance, self.state)
        self.messages: list[tuple[int, Any]] = []
        self._tasks: set[asyncio.Task] = set()
        appliance.session.send_sync.side_effect = self.send_sync

    def _correct_later(self, value: int) -> None:
        """Correct the written value shortly afterwards, like the washer does."""

        def correct() -> None:
            task = asyncio.create_task(self.finish_in.update({"value": value}))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        asyncio.get_running_loop().call_later(0.03, correct)

    async def send_sync(self, message: Message) -> Message:
        """Handle a message."""
        uid = message.data["uid"]
        value = message.data["value"]
        self.messages.append((uid, value))
        if uid == PAUSE_UID:
            await self.state.update({"value": 2})
        elif uid == RESUME_UID:
            if self.ignore_resume:
                self.ignore_resume -= 1
            elif self.finish_in.value <= DURATION:
                await self.state.update({"value": 3})
            else:
                await self.state.update({"value": 1})
        elif uid == FINISH_IN_UID:
            if self.state.value == "DelayedStart":
                raise CodeResponsError(519, "/ro/values")
            if self.clamped and self.keep_clamped:
                return Message(resource="/ro/values", action=Action.RESPONSE)
            await self.finish_in.update({"value": value})
            if self.clamp_to is not None or value < DURATION:
                self.clamped = True
                self._correct_later(self.clamp_to or DURATION)
        return Message(resource="/ro/values", action=Action.RESPONSE)


async def set_finish_in(hass: HomeAssistant, seconds: int, **kwargs: Any) -> dict:
    """Call set_finish_in."""
    return await hass.services.async_call(
        DOMAIN,
        "set_finish_in",
        {"device_id": get_device_id(hass), "finish_in": {"seconds": seconds}, **kwargs},
        blocking=True,
        return_response=True,
    )


@pytest.fixture(autouse=True)
def short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the timeouts of pause and resume."""
    monkeypatch.setattr(program_options, "PAUSE_TIMEOUT", 0.3)
    monkeypatch.setattr(program_options, "VALUE_TIMEOUT", 0.3)
    monkeypatch.setattr(program_options, "SETTLE_TIME", 0.1)
    monkeypatch.setattr(program_options, "SETTLE_MAX_TIME", 0.5)
    monkeypatch.setattr(program_options, "RESUME_AVAILABLE_TIMEOUT", 0.1)
    monkeypatch.setattr(program_options, "RESUME_TIMEOUT", 0.3)


def test_max_duration() -> None:
    """Test a change during a delayed start takes at most 45 s."""
    assert program_options.MAX_DURATION <= 45


@pytest.mark.parametrize("finish_in", [5 * 3600, 8 * 3600, DURATION + 60])
async def test_set_finish_in_delayed_start(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
    finish_in: int,
) -> None:
    """Test set_finish_in pauses, writes and resumes during a delayed start."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance)

    response = await set_finish_in(hass, finish_in)

    assert response == {"finish_in": finish_in, "operation_state": "delayedstart", "paused": True}
    assert washer.messages == [(PAUSE_UID, True), (FINISH_IN_UID, finish_in), (RESUME_UID, True)]
    assert washer.finish_in.value == finish_in


@pytest.mark.parametrize(
    ("finish_in", "translation_key"),
    [
        (2 * 3600, "option_starts_program"),
        (DURATION, "option_starts_program"),
        (86400 + 60, "option_out_of_range"),
    ],
)
async def test_set_finish_in_delayed_start_invalid(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
    finish_in: int,
    translation_key: str,
) -> None:
    """Test values that would start the program are refused without pausing."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance)

    with pytest.raises(ServiceValidationError) as exc_info:
        await set_finish_in(hass, finish_in)

    assert exc_info.value.translation_key == translation_key
    assert washer.messages == []
    assert washer.state.value == "DelayedStart"


async def test_set_finish_in_already_set(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test nothing is paused if the value is already set."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance, finish_in=6 * 3600 - 60)

    response = await set_finish_in(hass, 6 * 3600)

    assert response["paused"] is False
    assert washer.messages == []


async def test_set_finish_in_clamped(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test a value corrected by the appliance is restored before resuming."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    # Without a known duration the value can't be checked in advance
    washer = FakeWasher(mock_appliance, report_duration=False)

    with pytest.raises(HomeAssistantError) as exc_info:
        await set_finish_in(hass, 2 * 3600)

    assert exc_info.value.translation_key == "delayed_start_change_error"
    placeholders = exc_info.value.translation_placeholders
    assert placeholders["step"] == "write"
    assert placeholders["state"] == "delayedstart"
    assert placeholders["value"] == str(6 * 3600)
    assert washer.messages == [
        (PAUSE_UID, True),
        (FINISH_IN_UID, 2 * 3600),
        (FINISH_IN_UID, 6 * 3600),
        (RESUME_UID, True),
    ]


async def test_set_finish_in_no_resume_if_starting(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test resume isn't sent if the program would start right away."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance, clamp_to=DURATION, keep_clamped=True)

    with pytest.raises(HomeAssistantError) as exc_info:
        await set_finish_in(hass, DURATION + 3600)

    placeholders = exc_info.value.translation_placeholders
    assert placeholders["step"] == "resume"
    assert placeholders["state"] == "pause"
    assert (RESUME_UID, True) not in washer.messages
    assert washer.state.value == "Pause"


async def test_set_finish_in_resume_again(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test resume is sent again if the appliance stays paused."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance, ignore_resume=1)

    response = await set_finish_in(hass, 5 * 3600)

    assert response["operation_state"] == "delayedstart"
    assert washer.messages == [
        (PAUSE_UID, True),
        (FINISH_IN_UID, 5 * 3600),
        (RESUME_UID, True),
        (RESUME_UID, True),
    ]


async def test_set_finish_in_stays_paused(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test the error names the state if the appliance doesn't resume."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance, ignore_resume=2)

    with pytest.raises(HomeAssistantError) as exc_info:
        await set_finish_in(hass, 5 * 3600)

    placeholders = exc_info.value.translation_placeholders
    assert placeholders["step"] == "resume"
    assert placeholders["state"] == "pause"
    assert washer.messages[-2:] == [(RESUME_UID, True), (RESUME_UID, True)]


async def test_set_finish_in_program_started(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test a program that started on resume is reported as error."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    # The integration doesn't know the duration, the appliance starts on resume
    washer = FakeWasher(mock_appliance, report_duration=False, clamp_to=DURATION, keep_clamped=True)

    with pytest.raises(HomeAssistantError) as exc_info:
        await set_finish_in(hass, 5 * 3600)

    assert exc_info.value.translation_key == "program_started"
    assert exc_info.value.translation_placeholders["state"] == "run"
    assert washer.messages.count((RESUME_UID, True)) == 1
    assert washer.messages.count((PAUSE_UID, True)) == 1


async def test_set_finish_in_pause_not_available(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test a clear error if pausing is not allowed."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance)
    await mock_appliance.commands["BSH.Common.Command.PauseProgram"].update({"available": False})

    with pytest.raises(HomeAssistantError) as exc_info:
        await set_finish_in(hass, 5 * 3600)

    assert exc_info.value.translation_key == "pause_not_available"
    assert washer.messages == []


async def test_set_finish_in_without_pause_resume(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test pause_resume false writes directly during a delayed start."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance)

    with pytest.raises(HomeAssistantError) as exc_info:
        await set_finish_in(hass, 5 * 3600, pause_resume=False)

    assert exc_info.value.translation_key == "set_option_error"
    assert washer.messages == [(FINISH_IN_UID, 5 * 3600)]


async def test_set_finish_in_not_delayed(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test set_finish_in writes directly if the program doesn't wait for its start."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance)
    await washer.state.update({"value": 0})

    response = await set_finish_in(hass, DURATION)

    assert response == {"finish_in": DURATION, "operation_state": "ready", "paused": False}
    assert washer.messages == [(FINISH_IN_UID, DURATION)]

    with pytest.raises(ServiceValidationError) as exc_info:
        await set_finish_in(hass, DURATION - 60)
    assert exc_info.value.translation_key == "option_below_duration"


async def test_set_finish_in_one_at_a_time(
    hass: HomeAssistant,
    mock_appliance: MockAppliance,
    patch_entity_description: None,
) -> None:
    """Test two calls don't overlap and the second value wins."""
    assert await setup_config_entry(hass, CONFIG_ENTRIES[0])
    washer = FakeWasher(mock_appliance)

    await asyncio.gather(set_finish_in(hass, 5 * 3600), set_finish_in(hass, 8 * 3600))

    assert washer.messages == [
        (PAUSE_UID, True),
        (FINISH_IN_UID, 5 * 3600),
        (RESUME_UID, True),
        (PAUSE_UID, True),
        (FINISH_IN_UID, 8 * 3600),
        (RESUME_UID, True),
    ]
    assert washer.finish_in.value == 8 * 3600


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
