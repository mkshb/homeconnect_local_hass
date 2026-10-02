# Changelog

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
