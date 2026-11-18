# Changelog

## Unreleased

Changes since 2.0.0.dev6:

- **Standby wake writes deliver in <1 s** (`local_server_app`,
  protocol.md §15 — live-verified 2026-11-18: `HassTurnOn` → first
  `command_served` in 0.78 s): standby quiet mode previously suppressed
  every nudge, so a queued wake/brew frame waited for the next ~20 s
  keepalive PUT before the machine even saw `notify:1`. Now an enqueue
  edge still nudges during standby — newly queued work is user intent —
  while periodic re-nudges stay silent. New `work_enqueued` event on
  `LocalServerState` is signalled by every `queue_*` append and wakes
  the nudger sleep instantly instead of after the 30 s standby tick.
- **Striker (ECAM452/Eletta) app-id registration hardened** — the old
  `_ensure_app_id` trusted the cloud `app_id` value blindly: when the
  machine dropped a registration (~300 s TTL) and the reset never
  reached the cloud, every `send_command` was silently ignored for
  hours. Now only a self-confirmed registration is refreshed
  (`_app_id_refreshed_at` window); otherwise the device re-registers
  and confirms via `app_id` echo + datapoint republish — LAN confirms
  by value change, cloud by `data_updated_at`. Registration is
  serialized under `_app_id_lock`, polls every 3 s, and refreshes no
  more than every 60 s (each command already renews the registration).
- **Cloud transport fixes:** request timeouts on every cloud call (a
  hung request previously blocked an executor thread indefinitely),
  automatic Gigya access-token renewal on HTTP 401, thread-safe token
  rotation, and `get_monitor` now reads the real `data_updated_at`
  field — the old `updated_at` key does not exist in Ayla responses, so
  monitor freshness was always assumed.
- **Eletta datapoint map cleaned up** (`ECAM452.json`): five statistics
  datapoints the model never publishes were dropped, two monitor bits
  with model-specific meaning removed from the active set, and
  `monitor_bits` policy now travels with the device map.
- **Cloud/LAN capability split** (`Device`): cloud devices no longer
  run into LAN-only paths — mailbox polling, `sett_profile` republish
  reads and `data_response` waits are gated by real capability checks
  (the previous `getattr(...)` probe never fired because the method
  exists on every transport and merely raises). Passive profile polling
  keeps standby quiet on cloud devices too, and the cloud write-confirm
  timeout rose to 45 s for the observed ~21 s republish latency.
- **ECAM452 cloud settings/profiles:** `read_catalog` and the profile
  reads now resolve striker datapoints (`sett_profile`, `_profile_name`
  alongside `_profiles_`) so an Eletta works cloud-only; the
  `aoff`→`off` settings hint covers both naming schemes.
- **ECAM452 espresso preset:** the espresso command brews the machine's
  stored volume instead of a hardcoded amount.

Changes since 2.0.0.dev5:

- **Recipe-parameter brew support** (`004-recipe-brew-params`): brewing can
  now carry per-call parameter overrides that are merged into the
  effective `0x83` recipe frame client-side — exactly like the Coffee Link
  app (`ParameterModel.defValue`), never persisted on the machine.
  - New `cremalink.ecam.params` module: `ParamMeta`/`PARAMS` registry for
    user-adjustable recipe parameters (display units + raw scaling),
    `to_display`/`to_raw` conversions.
  - `ecam.builder.merge_recipe_params` merges overrides into raw TLV
    values; `ecam.builder.build_brew_params` builds a brew frame from a
    value map (applies the APK's `E0` inclusion filter and wide-tag
    encoding).
  - `Device.recipe_params(beverage_id)` returns `RecipeParams` — the
    machine-defined `0xB0` bounds plus the active profile's current
    `0xA6` values (or descriptor defaults on read failure, flagged via
    `RecipeParams.source`).
  - `Device.brew(beverage_id, overrides=..., params=...)` sends one
    gated brew frame with the effective values.
  - `Device.beverage_catalog()` and `Device.brew_entry()` expose the
    machine-catalogued beverages and their command-map entries.
  - **Lazy, sweep-free catalogue** (research.md R11): the recipe list is
    the app's static model table; a recipe's `0xB0` bounds cost one GET on
    first use and are cached, the profile menu order one GET per profile.
    `Device.beverage_catalog(fetch_priority=...)` and
    `Device.recipe_priority_loaded()` expose this. `read_catalog()` stays
    as an explicit full-sweep diagnostic and is never called implicitly.
  - **Fix:** `authenticate_cloud` failed with an `IndexError` since the
    Gigya consent page started emitting `consentObj2Sig` in double quotes;
    the parser accepts both quote styles and raises a clear error if the
    page layout changes again.
  - **One-request cloud seed:** `Client.fetch_properties(dsn)` fetches
    every datapoint in a single cloud call (the app's store sync) and
    `Device.seed_catalog_from_properties(props)` fills the descriptor and
    menu-order caches from it; live recipe values are always read from the
    machine (cloud copies can be stale).
  - **Offline breaker** (`Device._upstream_call`): a 502 or missing
    response on any upstream POST (property request/write/command/
    refresh) marks the machine unreachable with exponential backoff
    (10 s → 60 s cap). While the breaker is open, upstream POSTs and the
    read wait loops behind them are skipped instead of each blocking a
    worker thread in a ~10 s timeout — observed 2026-11-12 with the
    machine in standby, where queued reads stalled ~6 HA SyncWorkers for
    the whole bootstrap. Local property-store harvests still run, any
    successful POST closes the breaker again, and it re-probes on its own
    after backoff.
  - **Session-register hardening** (`local_server_app`):
    `register_with_device` now catches `TimeoutError` alongside aiohttp
    errors (it escaped as an unhandled exception with an empty message),
    logs the exception type in every failure event, serializes
    concurrent register attempts under a lock, resolves PUT-vs-POST
    inside the lock, and defers a second session-opening POST while one
    is in flight or awaiting its key exchange — the observed 3-POSTs-in-
    <1 s race that matches the earlier datapoint-engine wedge.
  - **Cloud-side session wake** (research.md R17): when `local_reg`
    keeps failing consecutively (`cloud_wake_threshold`, default 3), the
    embedded server POSTs `device_connected` through the Ayla cloud
    channel — the same write the app uses to wake the machine's
    datapoint engine, which is independent of the dead LAN session
    module. New `Client.create_datapoint(dsn, name, value)` (verified
    `datapoints.json` endpoint + `x-ayla-source` header). Throttled to
    `cloud_wake_interval_seconds` (default 60), run off the event loop,
    skipped when `cloud_token_path` is unset, failure counter resets on
    the next successful registration and an announce failure never masks
    the local `ConnectionError`.
  - **Bean Adapt support:** `Device.selected_bean_slot()` decodes the
    currently selected bean system from the `d250`–`d256` bean blobs;
    bean beverages resolve their values from the profile- and
    bean-slot-specific `bs_recipe` datapoints and brew with the app's
    `0x80` multi bit set.
  - **Bean-system selection (US4, protocol-derived — `0xB9` is on
    protocol.md §13's untested-write list):** `Device.bean_systems()`
    decodes configured bean slots and their user-assigned UTF-16BE names
    from the `d25x_beansystem_N` `ba f0` blobs (DECODED `G0` parser
    layout); `Device.select_bean_system(slot)` sends the session-gated
    `0D 06 B9 F0 <slot>` select frame via the same gated path as the
    verified `0xA9` profile select (announce → frame → optional
    `B9 F0 <ok>` simple ack; a silent channel counts as applied, matching
    non-striker `0xA9` semantics). New `ecam.builder.build_bean_select` +
    `REQ_BEAN_SELECT`. Bean-name/configuration writes (`0xBB`) are not
    implemented.
  - **Control set matches the app's brew sheet** (research.md R12 —
    verified `MainBeveragesFragment.E1()`; supersedes the `RecipeDefaults`
    rules which are dead code in APK 4.9.6): `PARAMS` now covers only
    coffee amount (1), taste (2), milk (9), tea temperature `THE_TEMP`
    (13 — only beverages whose descriptor carries tag 13, i.e. Tea on
    the Soul) and water (15). `blend`, `inversion` and generic
    `temp` are gone — inversion is a brew-command flag, not a slider,
    blend needs `nGrinders > 1` (Soul: 0), and generic temperature is a
    global machine setting on `globalTemperature` models. Enable rules:
    `min < max`, quantity current value > 0 (profile-stored value wins
    over the descriptor default), BeanAdapt beverages get coffee alone.
  - **Debug diagnostics:** `authenticate_cloud` logs each auth step at
    debug level (no secrets — the previous error path could dump the
    Gigya response incl. session fields), `Client.fetch_properties`
    logs request timing/response shape/datapoint counts per family, and
    `Device` logs catalogue seeds and `recipe_params` sources.
- **Standby quiet mode for the embedded server** (`local_server_app`,
  research.md R16): `LocalServerState.machine_asleep` (set via
  `EmbeddedLocalServer.set_machine_asleep`, logged as
  `machine_sleep_state`) silences the nudger entirely — queued reads
  wait out the sleep and dead-session re-registration is suppressed, so
  no POST/key-exchange ever hits a sleeping machine. The monitor job
  slows to `monitor_poll_interval_standby` (30 s), while the 20 s
  session keep-alive stays active for datapoint pushes and instant
  wake writes. Standalone servers keep the normal cadence.
- **Nudge cadence matches the app** (`nudger_job`, protocol.md §14.9):
  `local_reg` PUTs now go out only when the machine's `commands.json`
  poll loop looks stopped — an enqueue edge, a poll-stall edge
  (`last_commands_poll_at` older than `commands_poll_active_seconds`),
  or an asleep→awake transition — plus a still-pending re-nudge every
  `nudger_renudge_seconds` (10 s, the app's keepalive cadence) while the
  loop stays dead. A healthy ~2 s poll loop produces zero nudges (live
  drop: ~1 PUT/s → ~0 under load).
- **Register-per-request removed** (`api.py`): the queueing endpoints
  (`/command`, `/property`, `/properties/request`, `/refresh_monitor`,
  `/get_properties`) register only when `session_live()` is false —
  previously every client read cost one `local_reg` PUT (~25/min in HA).
  `/configure` and `/register` keep their unconditional register, and
  zombie sessions still self-heal through keepalive PUT failures.
- **Republish waits are event-driven** (`state.py`, `api.py`,
  `transports/local`, `device.py`): new `GET /wait_datapoint?since=N&timeout=S`
  blocks until a datapoint lands (generation counter, no missed
  arrivals). `Device._wait_datapoint` uses it in every harvest loop —
  recipe fetches, named-property collects, param reads, write-confirm
  and the `data_response` mailbox — replacing the 250 ms `get_property`
  poll that produced ~20 HTTP 404s per recipe read (protocol.md §13).
  Transports without the endpoint fall back to `response_poll` slices.

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
