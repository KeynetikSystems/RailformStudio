# -*- coding: utf-8 -*-
"""Command implementations: ProviderCommand, PanelCommand, HealthResetCommand.

These are the command classes that FreeCAD's Gui instantiates and calls
(GetResources, IsActive, Activated). Splitting them here keeps the
registration logic (registry.py) and metadata (metadata.py) separate.
"""

from typing import Any, Callable

import FreeCAD
import FreeCADGui

from rfstudio import deps, engines
from rfstudio import statusbar
from rfstudio.commands.health import (engine_health, engine_health_disabled,
                                       engine_health_record_failure,
                                       engine_health_record_success,
                                       engine_health_reset)
from rfstudio.commands.metadata import (_DESTRUCTIVE, _OPENS_PANEL,
                                         _PANEL_NEEDS_SELECTION,
                                         _selection, _selection_count,
                                         _selection_has_edge,
                                         _selection_has_external_sources,
                                         _selection_has_face,
                                         _selection_ok_for,
                                         _tip_with_hint, selection_requirement,
                                         _GENERIC_SELECTION_HINT)
from rfstudio.commands.selection import _confirm_destructive

try:
    from PySide import QtWidgets
except Exception:
    from PySide import QtGui as QtWidgets  # type: ignore


_EDITING_OBJ: str | None = None  # name of the object currently in edit mode
class ProviderCommand:
    """Re-exposes a curated action under one identity, backed by an ORDERED
    CHAIN of providers (engines.PROVIDERS) instead of one fixed engine
    command."""
    def __init__(self, cmd_id: str, menu: str, tip: str, icon: str) -> None:
        self._id = cmd_id
        self._chain = engines.PROVIDERS[cmd_id]
        self._res = {"MenuText": menu, "ToolTip": _tip_with_hint(cmd_id, tip),
                     "Pixmap": icon}

    def GetResources(self) -> dict[str, str]:
        return self._res

    def _provider_ready(self, provider: tuple) -> bool:
        if provider[0] == "native":
            return hasattr(engines, provider[1])
        _, mod, _target = provider
        return deps.installed(mod)

    def IsActive(self) -> bool:
        if FreeCAD.ActiveDocument is None:
            return False
        if engine_health(self._id)["disabled"]:
            return False
        if not any(self._provider_ready(p) for p in self._chain):
            return False
        return True

    def Activated(self) -> None:
        if self._id not in _OPENS_PANEL:
            ok, reason = _selection_ok_for(self._id)
            if not ok:
                statusbar.flash("%s — %s" % (self._res.get("MenuText", self._id), reason), error=True)
                return
            if self._id in _DESTRUCTIVE and not _confirm_destructive(self._id, self._res.get("MenuText", self._id)):
                return
        for provider in self._chain:
            if provider[0] == "native":
                fn = getattr(engines, provider[1], None)
                if fn is None:
                    continue
                try:
                    fn()
                    engine_health_record_success(self._id)
                    return
                except Exception as exc:
                    engine_health_record_failure(self._id, exc)
                    continue
            else:
                _, mod, target = provider
                if engines.run_engine_command(mod, target):
                    engine_health_record_success(self._id)
                    return
        FreeCAD.Console.PrintError("Railform Studio: no working provider for %s (tried: %s).\n" % (self._id, self._chain))
        statusbar.flash("“%s” isn't available right now — none of its providers could run." % self._res.get("MenuText", self._id), error=True, msec=8000)
class PanelCommand:
    """Opens one of our curated task panels."""

    def __init__(self, cmd_id: str, factory: Callable[[], Any], 
                 menu: str, tip: str, icon: str) -> None:
        self._id = cmd_id
        self._factory = factory
        self._res = {"MenuText": menu, "ToolTip": _tip_with_hint(cmd_id, tip),
                     "Pixmap": icon}

    def GetResources(self) -> dict[str, str]:
        return self._res

    def IsActive(self) -> bool:
        if FreeCAD.ActiveDocument is None:
            return False
        if _selection_has_external_sources():
            return False
        return True

    def Activated(self) -> None:
        global _EDITING_OBJ
        if FreeCAD.ActiveDocument is None:
            FreeCAD.newDocument()
        if self._id in _PANEL_NEEDS_SELECTION and not _selection():
            statusbar.flash(
                "%s — select %s first."
                % (self._res.get("MenuText", self._id),
                   _GENERIC_SELECTION_HINT),
                error=True)
            return
        if _EDITING_OBJ is not None:
            statusbar.flash(
                "Finish editing \u201c%s\u201d before opening another tool."
                % _EDITING_OBJ)
            return
        if self._id in _DESTRUCTIVE and not _confirm_destructive(
                self._id, self._res.get("MenuText", self._id)):
            return
        if FreeCADGui.Control.activeDialog():
            try:
                FreeCADGui.Control.closeDialog()
            except Exception:
                pass
        sel = _selection()
        if sel:
            _EDITING_OBJ = getattr(sel[0].Object, "Name", None)
        try:
            panel = self._factory()
            FreeCADGui.Control.showDialog(panel)
        except Exception:
            _EDITING_OBJ = None
            raise
        finally:
            def _clear() -> None:
                global _EDITING_OBJ
                _EDITING_OBJ = None
            try:
                FreeCADGui.Control.closeDialog
            except Exception:
                pass
            FreeCADGui.Control.activeDialog
            from PySide import QtCore
            QtCore.QTimer.singleShot(500, _clear)


class HealthResetCommand:
    """Resets the engine-health failure counts and re-enables any
    auto-disabled commands."""

    def __init__(self) -> None:
        self._res = {
            "MenuText": "Reset Engine Health",
            "ToolTip": "Re-enable any Railform Studio commands auto-disabled "
                       "after repeated native-provider failures.",
            "Pixmap": "rfstudio.svg",
        }

    def GetResources(self) -> dict[str, str]:
        return self._res

    def IsActive(self) -> bool:
        return FreeCAD.ActiveDocument is not None

    def Activated(self) -> None:
        engine_health_reset()
        try:
            QtWidgets.QMessageBox.information(
                FreeCADGui.getMainWindow(), "Railform Studio",
                "Engine health reset -- all providers re-enabled.")
        except Exception:
            pass