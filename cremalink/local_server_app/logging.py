"""
This module provides custom logging setup for the local server application,
including an in-memory ring buffer for recent log events and a redaction
function for sensitive data.
"""

import copy
import json
import logging
import threading
from collections import deque

REDACTED_KEYS = {
    "access_token",
    "advertised_ip",
    "api_key",
    "app_crypto_key",
    "app_iv_seed",
    "authorization",
    "cipher",
    "command",
    "cookie",
    "credential",
    "decoded_prefix",
    "device_key",
    "device_ip",
    "dev_crypto_key",
    "dev_iv_seed",
    "dsn",
    "email",
    "encryption_key",
    "enc",
    "ip_address",
    "lan_key",
    "password",
    "private_key",
    "random_1",
    "random_2",
    "refresh_token",
    "secret",
    "session_key",
    "session_token",
    "server_ip",
    "sign",
    "token",
    "time_1",
    "time_2",
}
OPERATIONAL_VISIBLE_KEYS = {
    "advertised_ip",
    "command",
    "device_ip",
    "dsn",
    "ip_address",
    "server_ip",
}


class OperationalConsoleHandler(logging.StreamHandler):
    """Render operational details to the standalone server's console."""

    def emit(self, record: logging.LogRecord) -> None:
        details = getattr(record, "operational_details", None)
        if details:
            record = copy.copy(record)
            record.msg = (
                f"{record.getMessage()} details="
                f"{json.dumps(details, sort_keys=True, default=str)}"
            )
            record.args = ()
        super().emit(record)


class RingBufferHandler(logging.Handler):
    """
    A custom logging handler that stores the most recent log records in a
    fixed-size in-memory deque (a ring buffer).

    This is useful for exposing recent server activity via an API endpoint
    without needing to read from a log file.
    """

    def __init__(
        self,
        max_entries: int = 200,
        forward_logger: logging.Logger | None = None,
        diagnostic_sensitive_values: tuple[str | None, ...] = (),
        operational_sensitive_values: tuple[str | None, ...] = (),
    ):
        """
        Initializes the handler.

        Args:
            max_entries: The maximum number of log entries to store.
        """
        super().__init__()
        self.max_entries = max_entries
        self.forward_logger = forward_logger
        self.diagnostic_sensitive_values = diagnostic_sensitive_values
        self.operational_sensitive_values = operational_sensitive_values
        self._events: deque[dict] = deque(maxlen=max_entries)
        self._lock = threading.Lock()  # Lock for thread-safe access to the deque.

    def emit(self, record: logging.LogRecord) -> None:
        """
        Formats and adds a log record to the ring buffer.

        Args:
            record: The log record to be processed.
        """
        diagnostic_sensitive_values = getattr(
            record,
            "diagnostic_sensitive_values",
            self.diagnostic_sensitive_values,
        )
        details = redact(
            getattr(record, "details", {}),
            diagnostic_sensitive_values,
        )
        event = {
            "event": _redact_text(record.getMessage(), diagnostic_sensitive_values),
            "level": record.levelname,
            "ts": record.created,
            "details": details,
        }
        if not getattr(record, "exclude_from_diagnostics", False):
            with self._lock:
                self._events.append(event)
        if self.forward_logger is not None:
            operational_details = getattr(
                record, "operational_details", event["details"]
            )
            operational_details = redact_operational(
                operational_details,
                getattr(
                    record,
                    "operational_sensitive_values",
                    self.operational_sensitive_values,
                ),
            )
            message = _redact_text(
                record.getMessage(),
                getattr(
                    record,
                    "operational_sensitive_values",
                    self.operational_sensitive_values,
                ),
            )
            if operational_details:
                message = (
                    f"{message} details="
                    f"{json.dumps(operational_details, sort_keys=True, default=str)}"
                )
            self.forward_logger.log(record.levelno, message)

    def get_events(self) -> list[dict]:
        """
        Retrieves a thread-safe copy of all events currently in the buffer.

        Returns:
            A list of log event dictionaries.
        """
        with self._lock:
            return list(self._events)


def create_logger(
    name: str,
    ring_size: int,
    *,
    forward_logger: logging.Logger | None = None,
    diagnostic_sensitive_values: tuple[str | None, ...] = (),
    operational_sensitive_values: tuple[str | None, ...] = (),
    console: bool = False,
) -> logging.Logger:
    """
    Creates and configures a logger with the RingBufferHandler.

    This function ensures that handlers are not added multiple times to the
    same logger instance.

    Args:
        name: The name of the logger.
        ring_size: The size of the ring buffer for the handler.

    Returns:
        A configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if not any(isinstance(handler, RingBufferHandler) for handler in logger.handlers):
        handler = RingBufferHandler(
            max_entries=ring_size,
            forward_logger=forward_logger,
            diagnostic_sensitive_values=diagnostic_sensitive_values,
            operational_sensitive_values=operational_sensitive_values,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if console and not any(
        getattr(handler, "_cremalink_console", False) for handler in logger.handlers
    ):
        handler = OperationalConsoleHandler()
        handler.setFormatter(formatter)
        handler._cremalink_console = True
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    details: dict | None = None,
    *,
    diagnostic_sensitive_values: tuple[str | None, ...] = (),
    operational_sensitive_values: tuple[str | None, ...] = (),
    level: int = logging.INFO,
    telemetry: bool = False,
) -> None:
    """Emit one record with separate operational and diagnostic details."""
    extra = {
        "details": redact(details, diagnostic_sensitive_values),
        "diagnostic_sensitive_values": diagnostic_sensitive_values,
    }
    if telemetry:
        extra["operational_details"] = redact_telemetry_operational(
            details, diagnostic_sensitive_values
        )
        extra["exclude_from_diagnostics"] = True
    else:
        extra["operational_details"] = redact_operational(
            details, operational_sensitive_values
        )
    extra["operational_sensitive_values"] = operational_sensitive_values
    log_method = getattr(logger, "log", None)
    if log_method is not None:
        if any(isinstance(handler, RingBufferHandler) for handler in logger.handlers):
            log_method(level, event, extra=extra)
        else:
            message = event
            if extra["operational_details"]:
                message = (
                    f"{event} details="
                    f"{json.dumps(extra['operational_details'], sort_keys=True, default=str)}"
                )
            log_method(level, message)
    else:
        method_name = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warning",
            logging.ERROR: "error",
            logging.CRITICAL: "critical",
        }.get(level, "info")
        getattr(logger, method_name)(event, extra=extra)


def _redact(
    details: dict | None,
    sensitive_values: tuple[str | None, ...],
    redacted_keys: set[str],
) -> dict:
    if not details:
        return {}

    replacements = tuple(value for value in sensitive_values if value)

    def _redact_value(key: str, value):
        if key.lower() in redacted_keys:
            return "***"
        if isinstance(value, dict):
            return {
                nested_key: _redact_value(nested_key, nested_value)
                for nested_key, nested_value in value.items()
            }
        if isinstance(value, list):
            return [_redact_value(key, item) for item in value]
        if isinstance(value, str):
            for sensitive_value in replacements:
                value = value.replace(sensitive_value, "***")
        return value

    return {key: _redact_value(key, value) for key, value in details.items()}


def _redact_text(value: str, sensitive_values: tuple[str | None, ...]) -> str:
    for sensitive_value in sensitive_values:
        if sensitive_value:
            value = value.replace(sensitive_value, "***")
    return value


def redact(details: dict | None, sensitive_values: tuple[str | None, ...] = ()) -> dict:
    """
    Filters a dictionary, replacing values of sensitive keys with '***'.

    This is a security measure to prevent secret keys, tokens, and other
    sensitive data from being exposed in logs.

    Args:
        details: A dictionary that may contain sensitive data.

    Returns:
        A new dictionary with sensitive values redacted.
    """
    return _redact(details, sensitive_values, REDACTED_KEYS)


def redact_operational(
    details: dict | None, sensitive_values: tuple[str | None, ...] = ()
) -> dict:
    """Redact credentials while permitting approved troubleshooting values."""
    return _redact(
        details,
        sensitive_values,
        REDACTED_KEYS - OPERATIONAL_VISIBLE_KEYS,
    )


def redact_telemetry_operational(
    details: dict | None, sensitive_values: tuple[str | None, ...] = ()
) -> dict:
    """Permit top-level device identifiers without unredacting nested telemetry."""
    safe_details = redact(details, sensitive_values)
    if not details:
        return safe_details

    for key in ("advertised_ip", "device_ip", "dsn"):
        if key in details:
            visible_value = details[key]
            remaining_sensitive_values = tuple(
                value for value in sensitive_values if value != visible_value
            )
            safe_details[key] = _redact(
                {key: visible_value},
                remaining_sensitive_values,
                REDACTED_KEYS - {key},
            )[key]
    return safe_details
