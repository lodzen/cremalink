"""
This module defines and manages the background jobs for the local server.
These jobs run periodically in asyncio tasks to handle keep-alive, status
monitoring, and re-keying operations.
"""

import asyncio
import logging
import time

from cremalink.local_server_app.config import ServerSettings
from cremalink.local_server_app.device_adapter import DeviceAdapter
from cremalink.local_server_app.state import LocalServerState


class JobManager:
    """A simple manager for starting and stopping asyncio background tasks."""

    def __init__(self):
        self.tasks: list[asyncio.Task] = []

    def start(self, coro, name: str):
        """Creates an asyncio task from a coroutine and adds it to the manager."""
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)

    async def stop(self):
        """Cancels and cleans up all managed tasks."""
        for task in self.tasks:
            task.cancel()
        # Wait for all tasks to acknowledge cancellation
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()


async def nudger_job(
    st: LocalServerState,
    adapter: DeviceAdapter,
    settings: ServerSettings,
    stop_event: asyncio.Event,
):
    """Notifies a live session about pending commands (``PUT local_reg``).

    ``notify: 1`` makes the machine poll ``commands.json`` promptly, which is
    what drains the outgoing queue. While the session is down, registration
    retries are throttled to the register-timeout cadence — ``POST`` is what
    actually opens a session (key exchange), ``PUT`` alone cannot re-key a
    live or half-dead one (lodzen/cremalink#4).

    Cadence follows the app (protocol.md §15, ``AylaLanModule``): one
    ``local_reg`` PUT per keepalive tick with ``notify`` reflecting queue
    depth — **not** one PUT per nudger tick. The machine polls
    ``commands.json`` on its own ~1.3 s loop once notified, so faster
    PUTs cannot drain the queue any quicker (live-observed ~1 PUT/s
    while pending vs. the app's ~1 PUT/10 s). A fresh PUT still goes out
    immediately on the empty→non-empty edge so user writes keep ~1-tick
    latency.
    """
    prev_pending = False
    prev_polling = False
    prev_asleep = False
    # Start "now": the re-nudge window counts real pending time, not a
    # zero epoch that would fire on the first tick regardless.
    last_nudged_at = time.monotonic()
    while not stop_event.is_set():
        pending = prev_pending
        try:
            # Nudge (PUT, notify) when the live session has commands waiting.
            # A dead session is re-registered (POST, new key exchange) by the
            # session job — here it is only retried at register-timeout
            # cadence so a dead machine isn't spammed every tick.
            async with st.lock:
                pending = len(st.command_queue) > 0
                live = st.session_live()
                retry_due = (
                    st.last_register_attempt_at is None
                    or time.monotonic() - st.last_register_attempt_at
                    >= settings.session_register_timeout_seconds
                )
            now = time.monotonic()
            # An active commands.json poll loop drains the queue on its
            # own ~1-2 s cadence — a notify PUT is only worth sending
            # when the loop looks stopped: an edge (queue/stall/wake) or
            # the app-cadence re-nudge while the loop stays dead. While
            # the machine polls, pending work drains anyway.
            machine_polling = (
                st.last_commands_poll_at is not None
                and now - st.last_commands_poll_at
                < settings.commands_poll_active_seconds
            )
            # asleep→awake is itself an edge: work queued through the
            # sleep must prompt the poll loop as soon as it's back.
            woke = prev_asleep and not st.machine_asleep
            edge = (
                pending
                and not machine_polling
                and (not prev_pending or prev_polling or woke)
            )
            renudge_due = (
                not machine_polling
                and now - last_nudged_at >= settings.nudger_renudge_seconds
            )
            # In standby the re-nudge stays off — a sleeping machine must
            # not be pushed every 10 s for routine queued reads. A queue
            # *edge* still nudges: newly enqueued work is user intent
            # (a wake frame has no other way to reach the machine — the
            # next keepalive PUT is up to ~20 s out, observed as a very
            # slow power-on), and the ~30 s standby monitor GET needs the
            # same prompt drain to see the transition.
            should_nudge = (
                live and pending and (edge or (renudge_due and not st.machine_asleep))
            ) or (not st.registered and retry_due)
            if should_nudge:
                await adapter.register_with_device(st)
                last_nudged_at = now
            prev_pending = pending
            prev_polling = machine_polling
            prev_asleep = st.machine_asleep
        except Exception as exc:
            # Include the exception type — TimeoutError's str() is empty.
            st.log(
                "local_reg_nudge_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            # The session is unreachable — drop registration so the next
            # register goes out as a session-creating POST.
            await st.set_registered(False)

        interval = (
            settings.nudger_poll_interval_standby
            if st.machine_asleep
            else settings.nudger_poll_interval
        )
        # Sleep for the interval, an enqueue (work_enqueued — queued work
        # must edge-nudge at once, a wake frame cannot wait out a 30 s
        # standby tick), or shutdown, whichever comes first.
        enqueued_wait = asyncio.ensure_future(st.work_enqueued.wait())
        stop_wait = asyncio.ensure_future(stop_event.wait())
        try:
            await asyncio.wait(
                {enqueued_wait, stop_wait},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            enqueued_wait.cancel()
            stop_wait.cancel()
        st.work_enqueued.clear()


async def monitor_job(
    st: LocalServerState, settings: ServerSettings, stop_event: asyncio.Event
):
    """
    Periodically queues a request to fetch the device's monitoring status.
    """
    while not stop_event.is_set():
        # Standby slows the monitor loop — the wake transition is still
        # caught within the standby interval, while routine polls no
        # longer queue against a sleeping machine every few seconds.
        interval = (
            settings.monitor_poll_interval_standby
            if st.machine_asleep
            else settings.monitor_poll_interval
        )
        delay = interval
        try:
            # Only queue a request if the server is configured and another request isn't already pending.
            async with st.lock:
                ready = st.is_configured() and not st._monitor_request_pending
                last_sent_at = st.last_monitor_command_sent_at
            if ready:
                if last_sent_at is None:
                    await st.queue_monitor()
                else:
                    delay = interval - (time.monotonic() - last_sent_at)
                    if delay <= 0:
                        await st.queue_monitor()
                        delay = interval
        except Exception as exc:
            st.log("monitor_poll_failed", {"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(delay, 0.001))
        except asyncio.TimeoutError:
            continue


async def session_job(
    state: LocalServerState,
    adapter: DeviceAdapter,
    settings: ServerSettings,
    stop_event: asyncio.Event,
):
    """Maintains the LAN session (lodzen/cremalink#4).

    - Live session → keep-alive ``PUT local_reg`` (``notify: 1``), then
      verify the machine still polls ``commands.json``; a silent machine
      marks the session dead and the next tick re-registers.
    - Dead session → ``POST local_reg`` starts a new session and always
      triggers a key exchange; keys are only replaced inside
      :meth:`LocalServerState.init_crypto`, never wiped beforehand —
      a preemptive wipe plus a no-rekey ``PUT`` is what produced the
      hollow sessions this job replaces.
    """
    interval = settings.session_keepalive_interval_seconds
    while not stop_event.is_set():
        try:
            if not state.is_configured():
                pass
            elif not state.session_live():
                await state.rekey()
                try:
                    await adapter.register_with_device(state, method="POST")
                except (ConnectionError, TimeoutError, ValueError) as exc:
                    state.log(
                        "session_register_failed",
                        {"error": f"{type(exc).__name__}: {exc}"},
                    )
                else:
                    exchanged = await state.wait_for_key_exchange(
                        settings.session_register_timeout_seconds
                    )
                    state.log(
                        "session_registered",
                        {"method": "POST", "key_exchanged": exchanged},
                    )
            else:
                try:
                    await adapter.register_with_device(state, method="PUT")
                except (ConnectionError, TimeoutError, ValueError) as exc:
                    state.log(
                        "session_keepalive_failed",
                        {"error": f"{type(exc).__name__}: {exc}"},
                    )
                    await state.set_registered(False)
                else:
                    # A quiet machine is not a dead one (§14.9): standby
                    # machines may not poll commands.json for long stretches
                    # while still answering local_reg — the app marks
                    # sessions dead on transport errors only. Silence is
                    # logged for diagnostics; only a very long silence
                    # (session_silence_reset_seconds) allows one re-POST as a
                    # backstop for sessions that died machine-side.
                    anchors = [
                        t
                        for t in (
                            state.last_commands_poll_at,
                            state.last_key_exchange_at,
                        )
                        if t is not None
                    ]
                    silent_for = time.monotonic() - max(anchors) if anchors else 0.0
                    if silent_for >= settings.session_silence_reset_seconds:
                        state.log(
                            "session_silence_reset",
                            {"silent_seconds": round(silent_for)},
                        )
                        await state.set_registered(False)
                    elif silent_for >= settings.session_probe_timeout_seconds:
                        state.log(
                            "session_keepalive_no_poll",
                            {"silent_seconds": round(silent_for)},
                            level=logging.DEBUG,
                        )
        except Exception as exc:
            state.log("session_job_failed", {"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
