"""Classified internal Stage 5 failures; never public ErrorCode additions."""

from __future__ import annotations

import re

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


class SyncFailure(ValueError):
    """A deterministic, content-free Stage 5 failure with a safe code."""

    def __init__(self, code: str) -> None:
        if _CODE.fullmatch(code) is None:
            raise ValueError("sync failure code must be a safe identifier")
        self.code = code
        super().__init__(f"stage 5 sync failure: {code}")


class ScanError(SyncFailure):
    """A complete directory snapshot could not be produced."""


class SyncPlanError(SyncFailure):
    """Canonical durable state cannot be planned safely."""


class RecoveryBlockedError(SyncFailure):
    """A recoverable boundary cannot be continued safely."""


class CurrentRepairBlockedError(SyncFailure):
    """The current derived index cannot be recreated exactly."""
