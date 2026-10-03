"""Change the start or finish time of a program, also during a delayed start."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
START_IN = "BSH.Common.Option.StartInRelative"
FINISH_IN = "BSH.Common.Option.FinishInRelative"
# Program duration, in this order
DURATION_ENTITIES = (
    "BSH.Common.Option.EstimatedTotalProgramTime",
    "BSH.Common.Option.RemainingProgramTime",
)
WATCHED_ENTITIES = (OPERATION_STATE, RESUME_COMMAND, START_IN, FINISH_IN)

STEP_PAUSE = "pause"
STEP_WRITE = "write"
STEP_RESUME = "resume"

STATE_DELAYED_START = "delayedstart"
STATE_PAUSE = "pause"
STATE_RUN = "run"

# Timeouts in seconds. A washer pauses within 0.2 s, takes a new value within 0.1 s,
# corrects a value it doesn't accept within 0.7 s and waits for its delayed start again
# 2 to 4 s after resuming, so a change normally takes about 5 s.
PAUSE_TIMEOUT = 5
# Until the appliance reports the written value
VALUE_TIMEOUT = 5
# The value is final once it didn't change for SETTLE_TIME, waiting at most SETTLE_MAX_TIME
SETTLE_TIME = 1.5
SETTLE_MAX_TIME = 4
# Until the resume command is reported as available, it is sent anyway afterwards
RESUME_AVAILABLE_TIMEOUT = 2
# Until the program leaves the pause after resuming, resume is sent at most twice
RESUME_TIMEOUT = 8
RESUME_ATTEMPTS = 2
# Upper bound of a change during a delayed start, restoring the old value included
MAX_DURATION = (
    PAUSE_TIMEOUT
    + 2 * (VALUE_TIMEOUT + SETTLE_MAX_TIME)
    + RESUME_ATTEMPTS * (RESUME_AVAILABLE_TIMEOUT + RESUME_TIMEOUT)
)


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
    the option written and the program resumed, it is never aborted or restarted. Values that
    would start the program right away are refused before pausing.
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

    async def _wait_for(self, entity: Entity, predicate: Callable[[], bool], wait: float) -> bool:
        """Wait until the predicate is true, False on timeout."""
        condition = self._condition(entity)
        try:
            async with asyncio.timeout(wait), condition:
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

    @property
    def program_duration(self) -> int | None:
        """Duration of the selected program in seconds, None if unknown."""
        for key in DURATION_ENTITIES:
            entity = self._appliance.entities.get(key)
            if entity is not None and entity.value:
                return int(entity.value)
        return None

    def _start_limit(self, option: Option) -> int | None:
        """Get the value at or below which the program would start right away."""
        if option.name == FINISH_IN:
            return self.program_duration
        if option.name == START_IN:
            return 0
        return None

    def _validate(self, option: Option, value: int, *, delayed: bool) -> None:
        """Refuse values the appliance would not accept or that would start the program."""
        placeholders = {"name": option.name, "value": str(value)}
        minimum = int(option.min) if option.min is not None else None
        maximum = int(option.max) if option.max is not None else None
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="option_out_of_range",
                translation_placeholders={**placeholders, "min": str(minimum), "max": str(maximum)},
            )
        limit = self._start_limit(option)
        if limit is None:
            return
        if delayed and value <= limit:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="option_starts_program",
                translation_placeholders={**placeholders, "limit": str(limit)},
            )
        if option.name == FINISH_IN and value < limit:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="option_below_duration",
                translation_placeholders={**placeholders, "limit": str(limit)},
            )

    def _is_close(self, option: Option, value: int, other: Any) -> bool:
        """Values differ at most by the step size, e.g. a minute counted down."""
        if other is None:
            return False
        return abs(int(other) - value) <= int(option.step or 0)

    async def set_option(
        self, option: Option, value: int, *, pause_resume: bool = True
    ) -> dict[str, Any]:
        """Write the option, returns whether the program was paused and the operation state."""
        async with self._lock:
            paused = pause_resume and self.operation_state == STATE_DELAYED_START
            self._validate(option, value, delayed=self.operation_state == STATE_DELAYED_START)
            if paused and self._is_close(option, value, option.value):
                _LOGGER.info("%s of %s is already %s", option.name, self._name, option.value)
                paused = False
            elif paused:
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

        old_value = int(option.value or 0)
        error: StepError | None = None
        try:
            await self._pause(pause)
            await self._write(option, value, old_value)
        except StepError as exc:
            error = exc
        finally:
            # Resume in any case once the appliance is paused, also if cancelled
            resume_error = await self._resume(resume, option)
        if resume_error is not None:
            # The state after resuming matters most, keep the reason of the earlier step
            reason = f"{error.reason}; {resume_error.reason}" if error else resume_error.reason
            error = StepError(resume_error.step, reason)
        if error is None:
            return
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
            translation_key=(
                "program_started"
                if self.operation_state == STATE_RUN
                else "delayed_start_change_error"
            ),
            translation_placeholders={
                "name": option.name,
                "step": error.step,
                "reason": error.reason,
                "state": str(self.operation_state),
                "value": str(option.value),
            },
        )

    async def _pause(self, pause: Command) -> None:
        state = self._appliance.entities[OPERATION_STATE]
        try:
            await Entity.set_value_raw(pause, True)  # noqa: FBT003
        except CodeResponsError as exc:
            raise StepError(STEP_PAUSE, str(exc)) from None
        _LOGGER.info("Sent pause to %s", self._name)
        if not await self._wait_for(
            state, lambda: self.operation_state == STATE_PAUSE, PAUSE_TIMEOUT
        ):
            raise StepError(STEP_PAUSE, f"state {self.operation_state} instead of pause")

    async def _write_and_settle(self, option: Option, value: int, old_value: int) -> None:
        """Write the value and wait until the appliance stops correcting it."""
        await Entity.set_value_raw(option, value)
        await self._wait_for(option, lambda: option.value != old_value, VALUE_TIMEOUT)
        try:
            async with asyncio.timeout(SETTLE_MAX_TIME):
                while True:
                    current = option.value
                    if not await self._wait_for(
                        option, lambda current=current: option.value != current, SETTLE_TIME
                    ):
                        break
        except TimeoutError:
            _LOGGER.debug("%s of %s keeps changing", option.name, self._name)

    async def _write(self, option: Option, value: int, old_value: int) -> None:
        try:
            await self._write_and_settle(option, value, old_value)
        except CodeResponsError as exc:
            raise StepError(STEP_WRITE, str(exc)) from None
        if self._is_close(option, value, option.value):
            _LOGGER.info("Set %s on %s to %s", option.name, self._name, option.value)
            return

        # The appliance changed the value, e.g. to the program duration which would start the
        # program right away
        changed_to = option.value
        if self._is_close(option, old_value, changed_to):
            raise StepError(STEP_WRITE, f"the appliance kept {changed_to} instead of {value}")
        _LOGGER.warning(
            "%s changed %s to %s instead of %s, restoring %s",
            self._name,
            option.name,
            changed_to,
            value,
            old_value,
        )
        try:
            await self._write_and_settle(option, old_value, int(changed_to))
        except CodeResponsError as exc:
            _LOGGER.warning("Restoring %s on %s failed: %s", option.name, self._name, exc)
        raise StepError(
            STEP_WRITE, f"the appliance changed {value} to {changed_to}, restored {option.value}"
        )

    def _resume_is_safe(self, option: Option) -> bool:
        """Resuming must not start the program right away."""
        limit = self._start_limit(option)
        return limit is None or (option.value is not None and int(option.value) > limit)

    async def _resume(self, resume: Command, option: Option) -> StepError | None:
        """Resume if paused and wait until the program leaves the pause."""
        if self.operation_state != STATE_PAUSE:
            return None
        state = self._appliance.entities[OPERATION_STATE]
        for attempt in range(RESUME_ATTEMPTS):
            if not self._resume_is_safe(option):
                return StepError(
                    STEP_RESUME,
                    f"not resumed, {option.name} {option.value} would start the program,"
                    " resume it manually or set a later time",
                )
            if attempt:
                _LOGGER.warning("%s is still paused, sending resume again", self._name)
            # The appliance decides, waiting only gives the description change some time
            await self._wait_for(resume, lambda: _command_allowed(resume), RESUME_AVAILABLE_TIMEOUT)
            try:
                await Entity.set_value_raw(resume, True)  # noqa: FBT003
            except HomeConnectError as exc:
                _LOGGER.warning("Resuming %s failed: %s", self._name, exc)
                continue
            _LOGGER.info("Sent resume to %s", self._name)
            if await self._wait_for(
                state, lambda: self.operation_state != STATE_PAUSE, RESUME_TIMEOUT
            ):
                break
        if self.operation_state == STATE_DELAYED_START:
            _LOGGER.info("%s is waiting for its delayed start again", self._name)
            return None
        if self.operation_state == STATE_RUN:
            return StepError(STEP_RESUME, "the program has started")
        return StepError(STEP_RESUME, f"state {self.operation_state} instead of delayedstart")
