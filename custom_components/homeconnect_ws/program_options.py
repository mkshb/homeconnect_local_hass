"""Change options of the selected or active program, also during a delayed start."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeconnect_websocket import CodeResponsError
from homeconnect_websocket.entities import Access, Entity

from .helpers import start_options

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeconnect_websocket import HomeAppliance
    from homeconnect_websocket.entities import Command, Option

_LOGGER = logging.getLogger(__name__)

# The appliance rejected the value itself, other ways to write it won't help
VALUE_ERROR_CODES = frozenset(
    {
        531,  # ValueOutOfRange
        532,  # InvalidUIDValue
        533,  # Incomplete
        534,  # Inconsistent
        536,  # InvalidFormat
    }
)

PAUSE_COMMAND = "BSH.Common.Command.PauseProgram"
RESUME_COMMAND = "BSH.Common.Command.ResumeProgram"
OPERATION_STATE = "BSH.Common.Status.OperationState"
PAUSE_STATE_TIMEOUT = 10


def _command_allowed(command: Command | None) -> bool:
    return (
        command is not None
        and command.access in (Access.READ_WRITE, Access.WRITE_ONLY)
        and command.available is True
    )


async def _wait_for(condition: Callable[[], bool]) -> None:
    """Wait until the condition is met, raises TimeoutError."""
    async with asyncio.timeout(PAUSE_STATE_TIMEOUT):
        while not condition():  # noqa: ASYNC110 entity values change via library callbacks
            await asyncio.sleep(0.5)


async def set_program_option(
    appliance: HomeAppliance, option: Option, value: int, *, allow_pause: bool = False
) -> str:
    """
    Write an option of the selected or active program, returns the name of the variant used.

    Writing the option to /ro/values works until the program waits for its delayed start,
    then e.g. washers answer 519 (WriteRequest NoAccess). Other ways are tried one after
    another, each only if the previous one was refused. None of them aborts or starts the
    program. If all fail, the last error of the appliance is raised.
    """
    active_program = appliance.active_program
    selected_program = appliance.selected_program
    variants: list[tuple[str, Callable[[], Awaitable]]] = [
        # Skip the local access check of the library, the appliance decides
        ("/ro/values", lambda: Entity.set_value_raw(option, value)),
    ]
    if active_program is not None:
        variants.extend(
            [
                (
                    "/ro/activeProgram (option only)",
                    lambda: active_program.start({option.uid: value}, override_options=True),
                ),
                (
                    "/ro/activeProgram (all available options)",
                    lambda: active_program.start(
                        start_options(active_program, {option.uid: value}),
                        override_options=True,
                    ),
                ),
            ]
        )
    if selected_program is not None:
        variants.append(
            (
                "/ro/selectedProgram (option only)",
                lambda: selected_program.select({option.uid: value}, override_options=True),
            )
        )
    if (
        allow_pause
        and OPERATION_STATE in appliance.entities
        and _command_allowed(appliance.commands.get(PAUSE_COMMAND))
        and RESUME_COMMAND in appliance.commands
    ):
        variants.append(
            ("pause, /ro/values, resume", lambda: _set_while_paused(appliance, option, value))
        )

    last_error: CodeResponsError | None = None
    for name, write in variants:
        try:
            await write()
        except CodeResponsError as exc:
            _LOGGER.debug("Setting %s to %s via %s failed: %s", option.name, value, name, exc)
            last_error = exc
            if exc.code in VALUE_ERROR_CODES:
                break
        else:
            _LOGGER.info("Set %s to %s via %s", option.name, value, name)
            return name

    raise last_error


async def _set_while_paused(appliance: HomeAppliance, option: Option, value: int) -> None:
    """Pause the program, write the option and resume."""
    pause = appliance.commands[PAUSE_COMMAND]
    resume = appliance.commands[RESUME_COMMAND]
    operation_state = appliance.entities[OPERATION_STATE]
    state_before = operation_state.value

    await pause.execute(True)  # noqa: FBT003
    try:
        await Entity.set_value_raw(option, value)
    finally:
        # Resume in any case, the program must not stay paused
        try:
            await _wait_for(lambda: _command_allowed(resume))
            await resume.execute(True)  # noqa: FBT003
        except (CodeResponsError, TimeoutError):
            _LOGGER.warning("Failed to resume the program after changing %s", option.name)
            raise
    try:
        await _wait_for(lambda: operation_state.value == state_before)
    except TimeoutError:
        _LOGGER.warning(
            "Operation state is %s after resuming, was %s",
            operation_state.value,
            state_before,
        )
