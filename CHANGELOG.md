# Changelog

## 1.0.6+as3b.5

Replaces the approach of 1.0.6+as3b.4: the service `send_raw` and the alternative ways of writing the option
(`/ro/activeProgram`, `/ro/selectedProgram`) are removed again, as is the field `allow_pause`.

### Changed

- `set_finish_in` and `set_start_in` can change the time while the program waits for its delayed start.
  Washers refuse writing the option then (519 "WriteRequest NoAccess"), like the app and the control panel.
  The services now do what works there:
  1. Pause the program (`BSH.Common.Command.PauseProgram`), fails with a clear message if the appliance
     doesn't allow pausing at the moment. Wait for the operation state `Pause`.
  2. Write the option to `/ro/values` and wait until the appliance confirms the new value.
  3. Always resume (`BSH.Common.Command.ResumeProgram`) once the appliance is paused, also if a step failed.
     Wait for `DelayedStart`; if the appliance stays paused, resume is sent once more.

  The call only succeeds if the appliance waits for its delayed start again with the new value. Otherwise it
  fails with the step, the reason and the current operation state and value. Each wait times out after 30 s.
  The program is never aborted or restarted.
- New field `pause_resume` (default on). Turned off, the value is written directly also during a delayed start.
- Outside a delayed start the value is written directly as before.
- Calls on the same appliance run one after another.
- Both services can return `{"finish_in" or "start_in": <s>, "operation_state": "...", "paused": true|false}`.

## 1.0.6+as3b.4

### Changed

- `set_finish_in` and `set_start_in` try several ways to write the value, each only if the appliance refused
  the previous one (e.g. washers answer 519 "WriteRequest NoAccess" to `/ro/values` during a delayed start):
  1. `/ro/values` with the option UID
  2. `/ro/activeProgram` with the active program and only this option
  3. `/ro/activeProgram` with the active program, its available options and the new value
  4. `/ro/selectedProgram` with the selected program and only this option
  5. Only with the new field `allow_pause`, and only if the appliance allows pausing at that moment:
     pause, write to `/ro/values`, resume

  Program and option UIDs come from the appliance description. The variant that worked is logged at info
  level. If the appliance rejects the value itself (e.g. 531 ValueOutOfRange) or all variants fail, its
  last error is raised unchanged.
- Service handlers moved to `services.py`.

### Added

- Service `send_raw` (admins only, diagnostics): sends a message with `resource`, `action` (GET, POST,
  NOTIFY; default POST) and `data` unchanged to the appliance and returns its response, error codes included.
- Service `describe_option` (diagnostics): returns the current state of an option or other entity by key or
  UID (access, available, min, max, step, value) including all description changes received so far, plus
  the active and selected program.
- Debug logging of value and description changes (`/ro/values`, `/ro/descriptionChange`) with entity names,
  enabled with the log level debug for `custom_components.homeconnect_ws`.

## 1.0.6+as3b.3

### Fixed

- The services `set_start_in` and `set_finish_in` did nothing: the coroutine writing the value was never
  awaited, so the call returned without sending anything to the appliance. The value is now written to
  `/ro/values` (option UID), which changes the option of the selected or active program, also while the
  program waits for its delayed start. The local access check of the library is skipped for this, so the
  appliance decides; if it refuses, the service fails with its error code
  (e.g. "Error 532 (InvalidUIDValue) setting BSH.Common.Option.FinishInRelative").
- `start_program` and the start button no longer send options the appliance reports as not available
  (e.g. `Load.Half`) or options without a known value. Some appliances answered those with an error
  (e.g. 501 for `/ro/activeProgram`) although the program started.

### Changed

- `start_in` and `finish_in` of the services are rounded to the step size of the option (e.g. 60 s).
- Error messages of `start_program` include the text of the error code and the program name.

## 1.0.6+as3b.2

### Fixed

- A lost connection (e.g. "No PONG received" after the appliance was switched off) no longer logs
  "Task exception was never retrieved" with a traceback. Without its own reconnect, the receive loop of
  homeconnect-websocket raises the connection error in a background task nobody awaits. The coordinator
  now handles it and logs it at debug level; the lost connection is reported by the existing warning.

## 1.0.6+as3b.1

Fork of [chris-mc1/homeconnect_local_hass](https://github.com/chris-mc1/homeconnect_local_hass) 1.0.6
(homeconnect-websocket 1.5.4).

### Fixed

- Connecting to an unreachable Appliance no longer blocks Home Assistant
  ([#475](https://github.com/chris-mc1/homeconnect_local_hass/issues/475)).
  The connect loop retried without any delay and logged a traceback for every attempt that failed with
  an unexpected error (e.g. `WSServerHandshakeError: 503` from a switched off hob), which starved the event loop.
  - Retry with exponential backoff and jitter: 5 s, 10 s, 20 s, ... up to 300 s, reset after a successful
    connection (based on [#490](https://github.com/chris-mc1/homeconnect_local_hass/pull/490)).
  - Connection errors (`ConnectionFailedError`, `HCHandshakeError`, `aiohttp.ClientError`, `OSError`,
    `TimeoutError`) are handled alike; unexpected errors are retried with backoff too.
  - Logging: one warning with host and reason per outage, further attempts at debug level, info on reconnect.
    Tracebacks of unexpected errors are logged at most once per outage.
  - Each connect attempt is limited to 60 s.
- Reconnecting after a lost connection is handled by the integration with the same backoff. The
  reconnect loop of homeconnect-websocket 1.5.4 retried without delay and gave up permanently after a
  handshake error ([homeconnect_websocket#98](https://github.com/chris-mc1/homeconnect_websocket/issues/98)).
  Entities stay available for up to 5 minutes while reconnecting.
- Unloading/reloading a config entry while waiting for a retry ends the connect task immediately.
  Closing is limited to 15 s, so `TaskManager.shutdown()` of homeconnect-websocket can no longer block
  the event loop when a task doesn't finish. The aiohttp session is closed on unload and shutdown.
- Zeroconf discovery only replaces the host of a configured Appliance with a usable address that accepts
  a TCP connection; host changes are logged. See README "Host updates via discovery".
