"""Frozen process-local runtime modes."""

from enum import StrEnum


class RuntimeMode(StrEnum):
    STARTING = "STARTING"
    SERVING = "SERVING"
    DRAINING = "DRAINING"
    MAINTENANCE = "MAINTENANCE"
    BLOCKED = "BLOCKED"
    SHUTTING_DOWN = "SHUTTING_DOWN"
