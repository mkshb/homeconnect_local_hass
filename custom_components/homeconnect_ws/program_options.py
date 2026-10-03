"""Change the start or finish time of a program, also during a delayed start."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeconnect_websocket import CodeResponsError
from homeconnect_websocket.entities import Access, Entity
from homeconnect_websocket.errors import HomeConnectError

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeconnect_websocket import HomeAppliance
    from homeconnect_websocket.entities import Command, Option

_LOGGER = logging.getLogger(__name__)

PAUSE_COMMAND = "BSH.Common.Command.PauseProgram"
RESUME_COMMAND = "BSH.Common.Command.ResumeProgram"
OPERATION_STATE = "BSH.Common.Status.OperationState"
WATCHED_ENTITIES = (
    OPERATION_STATE,
    RESUME_COMMAND,
    "BSH.Common.Option.StartInRelative",
    "BSH.Common.Option.FinishInRelative",
)

STEP_PAUSE = "pause"
STEP_WRITE = "write"
STEP_RESUME = "resume"
STEP_CONFIRM = "confirm"

STATE_DELAYED_START = "delayedstart"
STATE_PAUSE = "pause"

# Waiting for the operation state and for the appliance to confirm the new value
STEP_TIMEOUT = 30
# Waiting for the resume command to become available after pausing
RESUME_AVAILABLE_TIMEOUT = 5


class StepError(Exception):
    """A step of changing the option during a delayed start failed."""

    def __init__(self, step: str, reason: str) -> None:
        """Step and reason."""
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


def _command_allowed(command: Command | None) -> bool:
    return (
        command is not None
        and command.access in (Access.READ_WRITE, Access.WRITE_ONLY)
        and command.available is True
    )


class OptionWriter:
    """
    Write the start or finish time of the selected or active program, one call at a time.

    During a delayed start some appliances (e.g. washers) refuse to change the option with
    519 (WriteRequest NoAccess), like the app and the control panel do. The program is paused,
    the option written and the program resumed, it is never aborted or restarted.
    """

    def __init__(self, appliance: HomeAppliance, name: str) -> None:
        """Register the callbacks used to wait for changes."""
        self._appliance = appliance
        self._name = name
        self._lock = asyncio.Lock()
        self._conditions: dict[Entity, asyncio.Condition] = {}
        for key in WATCHED_ENTITIES:
            if entity := appliance.entities.get(key):
                self._condition(entity)

    def _condition(self, entity: Entity) -> asyncio.Condition:
        # Callbacks stay registered: unregistering while the library iterates over the
        # callbacks of the entity would fail
        if (condition := self._conditions.get(entity)) is None:
            condition = asyncio.Condition()

            async def notify(_: Entity) -> None:
                async with condition:
                    condition.notify_all()

            entity.register_callback(notify)
            self._conditions[entity] = condition
        return condition

    async def _wait_for(
        self, entity: Entity, predicate: Callable[[], bool], wait_time: float | None = None
    ) -> bool:
        """Wait until the predicate is true, False on timeout."""
        condition = self._condition(entity)
        try:
            async with asyncio.timeout(wait_time or STEP_TIMEOUT), condition:
                await condition.wait_for(predicate)
        except TimeoutError:
            return False
        return True

    @property
    def operation_state(self) -> str | None:
        """Operation state in lower case, e.g. delayedstart."""
        entity = self._appliance.entities.get(OPERATION_STATE)
        if entity is None or entity.value is None:
            return None
        return str(entity.value).rsplit(".", 1)[-1].lower()

    async def _wait_for_state(self, state: str) -> bool:
        entity = self._appliance.entities.get(OPERATION_STATE)
        if entity is None:
            return False
        return await self._wait_for(entity, lambda: self.operation_state == state)

    async def set_option(
        self, option: Option, value: int, *, pause_resume: bool = True
    ) -> dict[str, Any]:
        """Write the option, returns whether the program was paused and the operation state."""
        async with self._lock:
            paused = pause_resume and self.operation_state == STATE_DELAYED_START
            if paused:
                await self._set_option_paused(option, value)
            else:
                # Skip the local access check of the library, the appliance decides
                await Entity.set_value_raw(option, value)
            return {"paused": paused, "operation_state": self.operation_state}

    async def _set_option_paused(self, option: Option, value: int) -> None:
        pause = self._appliance.commands.get(PAUSE_COMMAND)
        resume = self._appliance.commands.get(RESUME_COMMAND)
        if not _command_allowed(pause) or resume is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="pause_not_available",
                translation_placeholders={"name": option.name},
            )

        error: StepError | None = None
        try:
            await self._pause_and_write(pause, option, value)
        except StepError as exc:
            error = exc
        finally:
            # Resume in any case once the appliance is paused, also if cancelled
            resume_error = await self._resume(resume)
        error = error or resume_error
        if error is None and option.value != value:
            error = StepError(STEP_CONFIRM, f"value is {option.value} after resuming")
        if error is not None:
            _LOGGER.warning(
                "Changing %s on %s failed at step %s: %s, operation state %s",
                option.name,
                self._name,
                error.step,
                error.reason,
                self.operation_state,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="delayed_start_change_error",
                translation_placeholders={
                    "name": option.name,
                    "step": error.step,
                    "reason": error.reason,
                    "state": str(self.operation_state),
                    "value": str(option.value),
                },
            )

    async def _pause_and_write(self, pause: Command, option: Option, value: int) -> None:
        try:
            await Entity.set_value_raw(pause, True)  # noqa: FBT003
        except CodeResponsError as exc:
            raise StepError(STEP_PAUSE, str(exc)) from None
        _LOGGER.info("Paused %s to change %s", self._name, option.name)
        if not await self._wait_for_state(STATE_PAUSE):
            raise StepError(STEP_PAUSE, f"state {self.operation_state} instead of pause")

        try:
            await Entity.set_value_raw(option, value)
        except CodeResponsError as exc:
            raise StepError(STEP_WRITE, str(exc)) from None
        if not await self._wait_for(option, lambda: option.value == value):
            raise StepError(STEP_WRITE, f"value is {option.value} instead of {value}")
        _LOGGER.info("Set %s on %s to %s", option.name, self._name, value)

    async def _resume(self, resume: Command) -> StepError | None:
        """Resume if paused and wait for the delayed start, send resume twice if needed."""
        if self.operation_state != STATE_PAUSE:
            return None
        for attempt in range(2):
            if attempt:
                if self.operation_state != STATE_PAUSE:
                    break
                _LOGGER.warning("%s is still paused, sending resume again", self._name)
            # The appliance decides, waiting only gives the description change some time
            await self._wait_for(resume, lambda: _command_allowed(resume), RESUME_AVAILABLE_TIMEOUT)
            try:
                await Entity.set_value_raw(resume, True)  # noqa: FBT003
            except HomeConnectError as exc:
                _LOGGER.warning("Resuming %s failed: %s", self._name, exc)
                continue
            _LOGGER.info("Sent resume to %s", self._name)
            if await self._wait_for_state(STATE_DELAYED_START):
                _LOGGER.info("%s is waiting for its delayed start again", self._name)
                return None
        return StepError(STEP_RESUME, f"state {self.operation_state} instead of delayedstart")
