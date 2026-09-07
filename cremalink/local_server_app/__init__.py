"""
This package contains the core implementation of the cremalink local proxy server,
which is an aiohttp application.

It exposes the main application factory `create_app` and the `ServerSettings`
class for configuration.
"""

from cremalink.local_server_app.api import create_app
from cremalink.local_server_app.config import ServerSettings
from cremalink.local_server_app.embedded import DEFAULT_PORT, EmbeddedLocalServer


def __getattr__(name):
    if name == "create_app":
        return create_app
    if name == "ServerSettings":
        return ServerSettings
    if name == "EmbeddedLocalServer":
        return EmbeddedLocalServer
    if name == "DEFAULT_PORT":
        return DEFAULT_PORT
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = ["DEFAULT_PORT", "EmbeddedLocalServer", "ServerSettings", "create_app"]
