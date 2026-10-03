"""Services of the Home Connect Websocket integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Never

import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeconnect_websocket import CodeResponsError
from homeconnect_websocket.message import Action, Message

from .const import DOMAIN
from .helpers import error_decorator, get_config_entry_from_call, start_program
from .program_options import set_program_option

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
    from homeconnect_websocket import HomeAppliance
    from homeconnect_websocket.entities import Entity

_LOGGER = logging.getLogger(__name__)

START_IN = "BSH.Common.Option.StartInRelative"
FINISH_IN = "BSH.Common.Option.FinishInRelative"

SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required("resource"): cv.string,
        vol.Optional("action", default=Action.POST.value): vol.In(
            [Action.GET.value, Action.POST.value, Action.NOTIFY.value]
        ),
        vol.Optional("data"): vol.Any(list, dict),
    },
    extra=vol.ALLOW_EXTRA,
)

DESCRIBE_OPTION_SCHEMA = vol.Schema(
    {vol.Required("key"): vol.Any(cv.positive_int, cv.string)},
    extra=vol.ALLOW_EXTRA,
)


def _get_entity_or_raise(appliance: HomeAppliance, key: str, error_key: str) -> Entity:
    entity = appliance.entities.get(key)
    if not entity:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=error_key,
        )
    return entity


def _duration_to_seconds(entity: Entity, data: dict) -> int:
    seconds = (
        int(data.get("hours", 0)) * 3600
        + int(data.get("minutes", 0)) * 60
        + int(data.get("seconds", 0))
    )
    # The appliance only accepts multiples of the step size (e.g. 60 s)
    if step := getattr(entity, "step", None):
        seconds = round(seconds / step) * int(step)
    return seconds


def _raise_code_error(err: CodeResponsError, translation_key: str, name: str) -> Never:
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders={
            "code": str(err.code),
            "message": err.message,
            "resource": err.resource,
            "name": name,
        },
    ) from None


async def _get_appliance(call: ServiceCall) -> HomeAppliance:
    config_entry = await get_config_entry_from_call(call.hass, call)
    return config_entry.runtime_data.appliance


@error_decorator
async def handle_start_program(call: ServiceCall) -> ServiceResponse:
    """Start the selected program."""
    appliance = await _get_appliance(call)
    options = {}
    if "start_in" in call.data:
        entity = _get_entity_or_raise(appliance, START_IN, "start_in_not_available")
        options[entity.uid] = _duration_to_seconds(entity, call.data["start_in"])

    if "finish_in" in call.data:
        entity = _get_entity_or_raise(appliance, FINISH_IN, "finish_in_not_available")
        options[entity.uid] = _duration_to_seconds(entity, call.data["finish_in"])

    if appliance.selected_program:
        try:
            await start_program(appliance.selected_program, options)
        except CodeResponsError as exc:
            _raise_code_error(exc, "start_program_error", appliance.selected_program.name)
    else:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_program_selected",
        )


async def _set_option(call: ServiceCall, key: str, field: str, error_key: str) -> None:
    appliance = await _get_appliance(call)
    entity = _get_entity_or_raise(appliance, key, error_key)
    try:
        await set_program_option(
            appliance,
            entity,
            _duration_to_seconds(entity, call.data[field]),
            allow_pause=call.data.get("allow_pause", False),
        )
    except CodeResponsError as exc:
        _raise_code_error(exc, "set_option_error", entity.name)


@error_decorator
async def handle_set_start_in(call: ServiceCall) -> ServiceResponse:
    """Change the start delay."""
    await _set_option(call, START_IN, "start_in", "start_in_not_available")


@error_decorator
async def handle_set_finish_in(call: ServiceCall) -> ServiceResponse:
    """Change the finish time."""
    await _set_option(call, FINISH_IN, "finish_in", "finish_in_not_available")


@error_decorator
async def handle_send_raw(call: ServiceCall) -> ServiceResponse:
    """Send a message to the appliance and return its response, for diagnostics."""
    appliance = await _get_appliance(call)
    message = Message(
        resource=call.data["resource"],
        action=Action(call.data["action"]),
        data=call.data.get("data"),
    )
    _LOGGER.info("Sending %s %s: %s", message.action, message.resource, message.data)
    try:
        response = await appliance.session.send_sync(message)
    except CodeResponsError as exc:
        return {"code": exc.code, "message": exc.message, "resource": exc.resource, "data": None}
    if response is None:
        return {"code": None, "message": None, "resource": message.resource, "data": None}
    return {
        "code": response.code,
        "message": None,
        "resource": response.resource,
        "data": response.data,
    }


@error_decorator
async def handle_describe_option(call: ServiceCall) -> ServiceResponse:
    """Return the current description of an entity by key or UID, for diagnostics."""
    appliance = await _get_appliance(call)
    key = call.data["key"]
    if isinstance(key, int) or key.isdigit():
        entity = appliance.entities_uid.get(int(key))
    else:
        entity = appliance.entities.get(key)
    if entity is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entity_not_found",
            translation_placeholders={"key": str(key)},
        )
    description = entity.dump()
    if description.get("enum"):
        description["enum"] = {str(k): v for k, v in description["enum"].items()}
    description["type"] = type(entity).__name__
    active_program = appliance.active_program
    selected_program = appliance.selected_program
    description["active_program"] = active_program.name if active_program else None
    description["selected_program"] = selected_program.name if selected_program else None
    return description


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services."""
    hass.services.async_register(DOMAIN, "start_program", handle_start_program)
    hass.services.async_register(DOMAIN, "set_start_in", handle_set_start_in)
    hass.services.async_register(DOMAIN, "set_finish_in", handle_set_finish_in)
    # Sends anything to the appliance, admins only
    async_register_admin_service(
        hass,
        DOMAIN,
        "send_raw",
        handle_send_raw,
        SEND_RAW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "describe_option",
        handle_describe_option,
        DESCRIBE_OPTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
