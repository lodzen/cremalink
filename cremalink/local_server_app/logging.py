"""
This module provides custom logging setup for the local server application,
including an in-memory ring buffer for recent log events and a redaction
function for sensitive data.
"""
import json
import logging
import threading
from collections import deque
from typing import Deque, Dict, List, Optional


class RingBufferHandler(logging.Handler):
    """
    A custom logging handler that stores the most recent log records in a
    fixed-size in-memory deque (a ring buffer).

    This is useful for exposing recent server activity via an API endpoint
    without needing to read from a log file.
    """

    def __init__(self, max_entries: int = 200, forward_logger: logging.Logger | None = None):
        """
        Initializes the handler.

        Args:
            max_entries: The maximum number of log entries to store.
        """
        super().__init__()
        self.max_entries = max_entries
        self.forward_logger = forward_logger
        self._events: Deque[Dict] = deque(maxlen=max_entries)
        self._lock = threading.Lock()  # Lock for thread-safe access to the deque.

    def emit(self, record: logging.LogRecord) -> None:
        """
        Formats and adds a log record to the ring buffer.

        Args:
            record: The log record to be processed.
        """
        # Construct a dictionary from the log record for easy JSON serialization.
        event = {
            "event": record.getMessage(),
            "level": record.levelname,
            "ts": record.created,
            "details": getattr(record, "details", {}),
        }
        with self._lock:
            self._events.append(event)
        if self.forward_logger is not None:
            details = event["details"]
            message = record.getMessage()
            if details:
                message = f"{message} details={json.dumps(details, sort_keys=True, default=str)}"
            self.forward_logger.log(record.levelno, message)

    def get_events(self) -> List[Dict]:
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
    # If the logger already has handlers, assume it's already configured.
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = RingBufferHandler(max_entries=ring_size, forward_logger=forward_logger)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    # Stop log messages from propagating to the root logger.
    logger.propagate = False
    return logger


def redact(
    details: Optional[dict], sensitive_values: tuple[str | None, ...] = ()
) -> dict:
    """
    Filters a dictionary, replacing values of sensitive keys with '***'.

    This is a security measure to prevent secret keys, tokens, and other
    sensitive data from being exposed in logs.

    Args:
        details: A dictionary that may contain sensitive data.

    Returns:
        A new dictionary with sensitive values redacted.
    """
    if not details:
        return {}

    redacted_keys = {
        "access_token",
        "app_crypto_key",
        "app_iv_seed",
        "cipher",
        "command",
        "decoded_prefix",
        "device_ip",
        "dev_crypto_key",
        "dev_iv_seed",
        "dsn",
        "email",
        "enc",
        "lan_key",
        "password",
        "random_1",
        "random_2",
        "refresh_token",
        "sign",
        "time_1",
        "time_2",
    }
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
