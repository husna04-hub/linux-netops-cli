"""Typed errors with actionable hints and stable exit codes."""

from __future__ import annotations


class NetopsError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class ConfigError(NetopsError):
    """The configuration is missing, malformed, or internally inconsistent."""

    exit_code = 2


class OperationalError(NetopsError):
    """A required operational action could not be completed."""

    exit_code = 3
