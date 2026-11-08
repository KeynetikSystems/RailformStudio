# -*- coding: utf-8 -*-
"""Engine health tracking for native providers.

Tracks per-command native-provider failures so a button whose native
implementation keeps crashing is auto-disabled instead of failing at
every click. Engine commands (Curves/Silk delegations) are not tracked
here -- their availability is governed by deps.installed() instead.
"""

from typing import Any

import FreeCAD

FAILURE_THRESHOLD = 3
_ENGINE_HEALTH: dict[str, dict[str, Any]] = {}  # cmd_id -> {"failures": int, "disabled": bool}


def engine_health(cmd_id: str) -> dict[str, Any]:
    """Return the health dict for a command id (creating it if needed)."""
    return _ENGINE_HEALTH.setdefault(cmd_id, {"failures": 0, "disabled": False})


def engine_health_record_failure(cmd_id: str, exc: Exception) -> bool:
    """Increment the failure count for a command; disable it once the
    threshold is exceeded. Returns True when the command was just disabled."""
    h = engine_health(cmd_id)
    h["failures"] += 1
    if h["failures"] >= FAILURE_THRESHOLD and not h["disabled"]:
        h["disabled"] = True
        FreeCAD.Console.PrintWarning(
            "Railform Studio: %s auto-disabled after %d native-provider "
            "failures (last: %s).  Use 'Reset Engine Health' to re-enable.\n"
            % (cmd_id, h["failures"], exc))
        return True
    return False


def engine_health_record_success(cmd_id: str) -> None:
    """Reset the failure count for a command (called on successful run)."""
    h = engine_health(cmd_id)
    h["failures"] = 0


def engine_health_disabled() -> list[str]:
    """List of command ids currently disabled by the health tracker."""
    return sorted(c for c, h in _ENGINE_HEALTH.items() if h["disabled"])


def engine_health_reset() -> None:
    """Reset all engine-health failure counts and re-enable commands."""
    _ENGINE_HEALTH.clear()
    FreeCAD.Console.PrintMessage(
        "Railform Studio: engine health reset -- all providers re-enabled.\n")