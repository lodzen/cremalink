"""
This module defines and manages the background jobs for the local server.
These jobs run periodically in asyncio tasks to handle keep-alive, status
monitoring, and re-keying operations.
"""

import asyncio
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
    """
    interval = settings.nudger_poll_interval
    while not stop_event.is_set():
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
                should_nudge = (live and pending) or (
                    not st.registered and retry_due
                )
            if should_nudge:
                await adapter.register_with_device(st)
        except Exception as exc:
            st.log("local_reg_nudge_failed", {"error": str(exc)})
            # The session is unreachable — drop registration so the next
            # register goes out as a session-creating POST.
            await st.set_registered(False)

        try:
            # Wait for the specified interval or until the stop event is set.
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def monitor_job(
    st: LocalServerState, settings: ServerSettings, stop_event: asyncio.Event
):
    """
    Periodically queues a request to fetch the device's monitoring status.
    """
    interval = settings.monitor_poll_interval
    while not stop_event.is_set():
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
                    state.log("session_register_failed", {"error": str(exc)})
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
                    since = state.last_commands_poll_at
                    await adapter.register_with_device(state, method="PUT")
                except (ConnectionError, TimeoutError, ValueError) as exc:
                    state.log("session_keepalive_failed", {"error": str(exc)})
                    await state.set_registered(False)
                else:
                    if not await state.wait_for_commands_poll(
                        settings.session_probe_timeout_seconds, since=since
                    ):
                        state.log("session_keepalive_no_poll")
                        await state.set_registered(False)
        except Exception as exc:
            state.log("session_job_failed", {"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
