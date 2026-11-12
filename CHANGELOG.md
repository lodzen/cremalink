# Changelog

## 2.0.0.dev5 — 2026-11-06

Changes since 2.0.0.dev4:

- **LAN session lifecycle follows the app's model:** poll silence no longer
  kills the session. A standby/idle machine can go minutes without polling
  `commands.json` while still answering `local_reg` — the previous 4 s probe
  declared it dead and re-POSTed every ~23 s, producing ~7.4k
  registration + key-exchange storms per day that wedged the module's
  datapoint engine machine-side (it kept accepting registrations but stopped
  polling entirely, until a machine power-cycle). Silence below
  `session_silence_reset_seconds` (default 900 s) is now logged at DEBUG and
  the session stays live on PUT keepalives; only transport errors — or
  silence beyond the backstop — trigger a re-POST.
- **`notify` reflects real queue depth** (`1` when commands are pending, `0`
  when idle) instead of hardcoded `1`, matching the app's
  `_pendingLanCommands.isEmpty()` rule.
- **`commands.json` and datapoint replies return `206 PARTIAL_CONTENT`**
  while commands remain queued and `200 OK` when drained — the machine's
  "keep polling" signal (app parity: `getResponseCode`).
- **`commands.json` accepts any HTTP method** (aioecam parity);
  **`POST /local_lan/property/datapoint/ack.json`** is routed to the
  datapoint handler instead of a silent 404.
- **Key-exchange fields `proto`, `ver`, `key_id`, `sec` are parsed and
  logged**; a non-empty `sec` (secure-setup exchange) is flagged via
  `key_exchange_secure_unsupported` so future firmware changes are
  diagnosable.
- **`local_lan_unhandled` catch-all** logs any machine request on an
  unrouted `local_lan` path — previously invisible silent 404/405s.
- **New setting:** `LocalServerSettings.session_silence_reset_seconds`
  (default `900.0`). `session_probe_timeout_seconds` is now informational
  only.
- **Docs/CI:** Sphinx author updated to Lodzen; docs deploy now also
  triggers on `development` branch pushes.

**Full Changelog**:
<https://github.com/lodzen/cremalink/compare/2.0.0.dev4...2.0.0.dev5>
