# -*- coding: utf-8 -*-
"""Left-docked tool shelf (Blender 'T panel' style).

A persistent QDockWidget on the left of FreeCAD's main window, holding the
curated commands as labelled tool buttons grouped into sections (Draw first).
Shown when the Railform Studio workbench activates, hidden when it deactivates.

Three usability features on top of the plain button grid:
  * a filter box at the top that live-filters buttons by name/tooltip and
    hides sections with no match;
  * collapsible sections whose expanded/collapsed state is remembered per
    user via FreeCAD's parameter store (so the shelf opens the way you left
    it);
  * buttons for a command whose only way to run is an engine that isn't
    installed are greyed out, with a tooltip saying which engine and where
    to get it -- instead of looking clickable and then erroring into the
    Report view (see engines.missing_engine_for).

GUI-only.
"""

import FreeCAD
import FreeCADGui

from PySide import QtCore, QtGui
try:
    from PySide import QtWidgets
except Exception:
    QtWidgets = QtGui

from rfstudio import commands
from rfstudio import engines
from rfstudio import snapsettings
from rfstudio import statusbar

_DOCK = None

# Per-user persistence of each section's expanded/collapsed state.
_PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/RailformStudio/ToolShelf"


def _params():
    """The RailformStudio/ToolShelf parameter group (section collapse
    memory). Guarded: a missing/renamed ParamGet just yields None and the
    shelf falls back to all-sections-expanded, never an exception at
    activation time."""
    try:
        return FreeCAD.ParamGet(_PARAM_PATH)
    except Exception:
        return None


def _get_expanded(key, default=True):
    p = _params()
    if p is None:
        return default
    try:
        return p.GetBool(key, default)
    except Exception:
        return default


def _set_expanded(key, value):
    p = _params()
    if p is None:
        return
    try:
        p.SetBool(key, bool(value))
    except Exception:
        pass


class _Section(QtWidgets.QWidget):
    """One collapsible group of tool buttons.

    A bold header QToolButton (with a down/right disclosure arrow) toggles
    a body widget holding the buttons. The expanded state is persisted
    under `self.key` so it survives workbench deactivation and restart.
    `apply_filter` narrows the visible buttons to those matching a search
    string; `refresh_availability` re-evaluates engine greying.
    """

    def __init__(self, runner, title, cmd_ids):
        super(_Section, self).__init__()
        self._run = runner
        self.title = title
        self.key = "expanded_" + "".join(c for c in title if c.isalnum())
        # (button, search_haystack, cmd_id, base_tooltip) per command.
        self._buttons = []
        # Guard so a programmatic header toggle (restore / filter) does not
        # get written back to the parameter store as a user choice.
        self._sync = False

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.header = QtWidgets.QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                  QtWidgets.QSizePolicy.Fixed)
        self.header.setAutoRaise(True)
        self.header.setStyleSheet("QToolButton { font-weight: bold; }")
        self.header.toggled.connect(self._on_toggled)
        v.addWidget(self.header)

        self.body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(self.body)
        bl.setContentsMargins(8, 2, 4, 6)
        bl.setSpacing(2)
        for cid in cmd_ids:
            btn, haystack, tip = self._build_button(cid)
            bl.addWidget(btn)
            self._buttons.append((btn, haystack, cid, tip))
        v.addWidget(self.body)

        expanded = _get_expanded(self.key, True)
        self._sync = True
        self.header.setChecked(expanded)
        self._sync = False
        self.body.setVisible(expanded)
        self._set_arrow(expanded)

    def _build_button(self, cmd_id):
        menu, icon, tip = commands.command_info(cmd_id)
        btn = QtWidgets.QToolButton()
        btn.setText("  " + menu)
        if icon:
            btn.setIcon(QtGui.QIcon(icon))
            btn.setIconSize(QtCore.QSize(22, 22))
        btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        btn.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                          QtWidgets.QSizePolicy.Fixed)
        btn.setAutoRaise(True)
        btn.clicked.connect(lambda chk=False, c=cmd_id: self._run(c))
        self._apply_availability(btn, cmd_id, tip)
        return btn, (menu + " " + (tip or "")).lower(), tip

    @staticmethod
    def _apply_availability(btn, cmd_id, tip):
        """Grey the button (and explain why in its tooltip) when its only
        provider is an engine that isn't installed; otherwise enable it
        with its plain tooltip. Called at build time and again from
        refresh_availability so installing the engine mid-session re-enables
        the button on the next show()."""
        missing = None
        try:
            missing = engines.missing_engine_for(cmd_id)
        except Exception:
            missing = None
        if missing:
            btn.setEnabled(False)
            note = ("Needs the %s engine, which isn't installed. "
                    "Install it via the Addon Manager." % missing)
            btn.setToolTip((tip + "\n\n" + note) if tip else note)
        else:
            btn.setEnabled(True)
            btn.setToolTip(tip or "")

    def refresh_availability(self):
        for btn, _haystack, cid, tip in self._buttons:
            self._apply_availability(btn, cid, tip)

    def _set_arrow(self, expanded):
        self.header.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)

    def _on_toggled(self, checked):
        self.body.setVisible(checked)
        self._set_arrow(checked)
        if not self._sync:
            _set_expanded(self.key, checked)

    def apply_filter(self, text):
        """Show only buttons whose name/tooltip contains `text`. An empty
        `text` restores every button and the section's remembered collapse
        state. A non-empty match force-expands the section (without
        overwriting the remembered state). Returns True if any button is
        still visible."""
        text = text.strip().lower()
        if not text:
            for btn, _haystack, _cid, _tip in self._buttons:
                btn.setVisible(True)
            self.setVisible(True)
            expanded = _get_expanded(self.key, True)
            self._sync = True
            self.header.setChecked(expanded)
            self._sync = False
            self.body.setVisible(expanded)
            self._set_arrow(expanded)
            return True

        any_match = False
        for btn, haystack, _cid, _tip in self._buttons:
            match = text in haystack
            btn.setVisible(match)
            any_match = any_match or match
        self.setVisible(any_match)
        if any_match:
            self._sync = True
            self.header.setChecked(True)
            self._sync = False
            self.body.setVisible(True)
            self._set_arrow(True)
        return any_match


class _SnapBar(QtWidgets.QGroupBox):
    """Vertex / Edge / Intersection snap on-off toggles (Draft-style),
    backed by rfstudio.snapsettings so the editor snap collectors honour
    them and the choice persists. Changing a box takes effect on the next
    drag (the collectors read snapsettings when a drag begins)."""

    def __init__(self):
        super(_SnapBar, self).__init__("Snapping")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 4)
        lay.setSpacing(8)
        short = {"vertex": "Vertex", "edge": "Edge",
                 "intersection": "Intersect"}
        for kind in snapsettings.KINDS:
            cb = QtWidgets.QCheckBox(short.get(kind, kind))
            cb.setChecked(snapsettings.enabled(kind))
            cb.setToolTip(snapsettings.LABELS.get(kind, kind))
            cb.toggled.connect(
                lambda checked, k=kind: snapsettings.set_enabled(k, checked))
            lay.addWidget(cb)
        lay.addStretch(1)


class _ToolShelf(QtWidgets.QDockWidget):

    def __init__(self, mw):
        super(_ToolShelf, self).__init__("Railform Tools", mw)
        self.setObjectName("RailformToolShelf")
        self.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea
                             | QtCore.Qt.RightDockWidgetArea)

        container = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Filter tools…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_filter)
        outer.addWidget(self.search)

        outer.addWidget(_SnapBar())

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._sections = []
        for group_name, cmd_ids in commands.TOOLBARS:
            sec = _Section(self._run, group_name.replace("Railform ", ""),
                           cmd_ids)
            self._sections.append(sec)
            lay.addWidget(sec)
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # Shown only when a live filter matches nothing, so a blank shelf
        # reads as "no match" rather than "broken".
        self._empty = QtWidgets.QLabel("No tools match that filter.")
        self._empty.setStyleSheet("color:#9aa6b2;font-size:11px;padding:6px;")
        self._empty.setWordWrap(True)
        self._empty.setVisible(False)
        outer.addWidget(self._empty)

        self.setWidget(container)
        self.setMinimumWidth(160)

    def _on_filter(self, text):
        for sec in self._sections:
            sec.apply_filter(text)
        if text.strip():
            any_visible = any(sec.isVisible() for sec in self._sections)
            self._empty.setVisible(not any_visible)
        else:
            self._empty.setVisible(False)

    def refresh_availability(self):
        """Re-evaluate engine greying for every button (an engine may have
        been installed since the shelf was built)."""
        for sec in self._sections:
            sec.refresh_availability()

    @staticmethod
    def _run(cmd_id):
        try:
            FreeCADGui.runCommand(cmd_id, 0)
        except Exception as exc:
            # A console-only PrintError leaves the button looking like it
            # did nothing (most users keep the Report view closed) -- flash
            # the failure on the status bar too, named by the tool's menu
            # label rather than its raw command id.
            try:
                label = commands.command_info(cmd_id)[0]
            except Exception:
                label = cmd_id
            FreeCAD.Console.PrintError("Railform: %s failed (%s)\n"
                                       % (cmd_id, exc))
            statusbar.flash("%s failed: %s" % (label, exc), error=True)


def show():
    global _DOCK
    mw = FreeCADGui.getMainWindow()
    if mw is None:
        return
    if _DOCK is None:
        _DOCK = _ToolShelf(mw)
        mw.addDockWidget(QtCore.Qt.LeftDockWidgetArea, _DOCK)
    else:
        _DOCK.refresh_availability()
    _DOCK.setVisible(True)
    _DOCK.raise_()


def hide():
    if _DOCK is not None:
        _DOCK.setVisible(False)
