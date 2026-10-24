# -*- coding: utf-8 -*-
"""Shared scaffolding for GUI task panels: base classes, selection helpers,
and the reusable _CurveGroup link-list widget.

Split out so panel_curve/panel_surface/panel_qa/panel_import share ONE
implementation of:
  * _EditPanelBase   -- commit/abort-transaction + resetEdit/closeDialog
                        teardown for panels that edit a parametric doc
                        object (CurvePanel, SurfacePanel, LoftPanel, ...).
  * _ReportPanelBase -- Close-only scaffolding for run-and-report QA
                        panels with no doc object to keep re-editable.
  * _CurveGroup      -- a labelled link-list group box (Add selected /
                        Remove / optional Up-Down reorder) used by every
                        SurfacePanel kind and LoftPanel.
  * selection helpers used across panels.

GUI-only.
"""

import FreeCAD
# Delay GUI imports until actually needed to improve headless compatibility
FreeCADGui = None

def _get_qt_widgets():
    """Get QtWidgets module with compatibility handling."""
    global QtWidgets
    if 'QtWidgets' not in globals():
        from rfstudio.compat import get_qt_widgets
        QtWidgets = get_qt_widgets()
    return QtWidgets

def _ensure_freecadgui():
    """Ensure FreeCADGui is imported and available."""
    global FreeCADGui
    if FreeCADGui is None:
        import FreeCADGui
    return FreeCADGui


# ---------------------------------------------------------------------- #
# Selection helpers
# ---------------------------------------------------------------------- #
def _std_buttons():
    """Standard Ok/Cancel button set for FreeCAD task dialogs.

    Returns a plain int bitmask. On PySide6/Qt6 the ``Ok | Cancel`` result
    is a ``QDialogButtonBox.StandardButton`` flag enum that ``int()`` no
    longer accepts directly (it raises TypeError), so read its ``.value``
    when present; on PySide2/Qt5 there is no ``.value`` and ``int()`` on
    the flag works. This keeps the helper working on both bindings."""
    QtWidgets = _get_qt_widgets()
    btns = QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    return int(getattr(btns, "value", btns))


def _selected_objects():
    """Currently selected document objects (whole-object selection)."""
    try:
        _ensure_freecadgui()
        return list(FreeCADGui.Selection.getSelection())
    except Exception:
        return []


def _selected_faces():
    """[(face, owning_object), ...] for every face in the current
    selection -- sub-face picks first, else every face of a whole-object
    pick."""
    try:
        _ensure_freecadgui()
        out = []
        for so in FreeCADGui.Selection.getSelectionEx():
            obj = so.Object
            subs = so.SubObjects or []
            got_face = False
            for sub in subs:
                if getattr(sub, "ShapeType", "") == "Face":
                    out.append((sub, obj))
                    got_face = True
            if not got_face:
                shp = getattr(obj, "Shape", None)
                if shp is not None:
                    for f in shp.Faces:
                        out.append((f, obj))
        return out
    except Exception:
        return []


def _selected_points():
    """Vector points from the current selection: sub-vertex picks first,
    else every vertex of a whole-object pick."""
    try:
        _ensure_freecadgui()
        pts = []
        for so in FreeCADGui.Selection.getSelectionEx():
            obj = so.Object
            subs = so.SubObjects or []
            got = False
            for sub in subs:
                if getattr(sub, "ShapeType", "") == "Vertex":
                    pts.append(sub.Point)
                    got = True
            if not got:
                shp = getattr(obj, "Shape", None)
                if shp is not None:
                    pts.extend(v.Point for v in shp.Vertexes)
        return pts
    except Exception:
        return []


def _notify_overlay_unavailable(panel, feature_name, exc):
    """Best-effort user-visible notice that an optional Coin3D overlay
    (highlight ring, drag gizmo, ...) could not be built on this system --
    the panel keeps working, just without that visual aid. Never raises."""
    try:
        _ensure_freecadgui()
        FreeCAD.Console.PrintWarning(
            "Railform: %s unavailable (%s) -- continuing without it.\n"
            % (feature_name, exc))
    except Exception:
        pass


# ---------------------------------------------------------------------- #
# _EditPanelBase -- parametric-object task panels (commit/abort transaction)
# ---------------------------------------------------------------------- #
class _EditPanelBase(object):
    """Shared open/commit/abort-transaction + resetEdit/closeDialog
    scaffolding for every parametric-object task panel (CurvePanel,
    BlendPanel, SurfacePanel, LoftPanel, OffsetThickenPanel, ShellPanel,
    BlendSurfPanel, ...).

    Subclasses call self._start_edit(edit_obj, title, factory) at the top
    of their __init__. It sets:
        self.doc      -- the active document
        self.creating -- True if this panel is creating a NEW object
        self.obj      -- the object being created/edited
    and opens an undo transaction for the whole edit session.

    Optional hooks a subclass may override (all default to no-ops):
        _pre_accept()     -- called just before the transaction commits
        _pre_reject()      -- called just before the transaction aborts
        _post_abort()      -- called after abort (e.g. undo extra per-pick
                             transactions -- see CurvePanel._pick_count)
        _extra_teardown()     -- extra cleanup after closing dialog
    """

    def __init__(self):
        # These will be set by subclasses
        self.doc = None
        self.creating = False
        self.obj = None

    def _start_edit(self, edit_obj, title, factory):
        """Initialize the edit session: open the undo transaction, then
        create/attach the object being edited."""
        _ensure_freecadgui()
        self.doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
        self.creating = edit_obj is None
        self.doc.openTransaction(title)
        self.obj = edit_obj if edit_obj is not None else factory()

    def accept(self):
        """Accept changes and close dialog."""
        try:
            self._pre_accept()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: pre-accept hook failed (%s)\n" % exc)
        try:
            self.obj.recompute()
        except Exception:
            pass
        try:
            self.doc.commitTransaction()
        except Exception as exc:
            FreeCAD.Console.PrintWarning("Railform: commit failed (%s)\n" % exc)
        self._extra_teardown()
        self._close()
        return True

    def reject(self):
        """Reject changes and close dialog."""
        try:
            self._pre_reject()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: pre-reject hook failed (%s)\n" % exc)
        try:
            self.doc.abortTransaction()
        except Exception as exc:
            FreeCAD.Console.PrintWarning("Railform: abort failed (%s)\n" % exc)
        # _post_abort is abort-ONLY (e.g. CurvePanel undoes its extra
        # per-pick transactions here) -- it must never run on the accept
        # path, so it's called directly here rather than from _close(),
        # which both accept() and reject() call.
        try:
            self._post_abort()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: post-abort hook failed (%s)\n" % exc)
        self._extra_teardown()
        self._close()
        return True

    def _pre_accept(self):
        """Hook called before accepting changes."""
        pass

    def _pre_reject(self):
        """Hook called before rejecting changes."""
        pass

    def _post_abort(self):
        """Hook called after abort ONLY -- e.g. undo extra per-pick
        transactions (see CurvePanel._pick_count). Never called on accept."""
        pass

    def _extra_teardown(self):
        """Extra teardown after closing dialog. Runs on both accept and
        reject."""
        pass

    def _close(self):
        """Close the dialog."""
        try:
            _ensure_freecadgui()
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# Shared UI helpers (used by multiple panels)
# ---------------------------------------------------------------------- #
# NOTE: this module loads Qt LAZILY (_get_qt_widgets) so plain
# `import panel_base` works headless and at workbench startup. The
# helpers below are therefore factory FUNCTIONS, not classes: a
# `class X(QtWidgets.QGroupBox)` body would evaluate QtWidgets at
# import time and break workbench loading with
# "name 'QtWidgets' is not defined". Call sites are unaffected --
# `_CollapsibleGroup("Advanced")` etc. work exactly as before.

def _CollapsibleGroup(title, collapsed=True):
    """A checkable group box whose content area collapses on toggle.

    Usage: g = _CollapsibleGroup("Advanced"); g.content_layout.addWidget(...)
    Returns the QGroupBox with a `.content_layout` attribute.
    """
    QtWidgets = _get_qt_widgets()
    box = QtWidgets.QGroupBox(title)
    box.setCheckable(True)
    box.setChecked(not collapsed)
    box.setStyleSheet("QGroupBox::indicator { width: 14px; height: 14px; }")
    inner = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(inner)
    layout.setContentsMargins(8, 4, 8, 8)
    layout.setSpacing(4)
    main = QtWidgets.QVBoxLayout(box)
    main.setContentsMargins(0, 0, 0, 0)
    main.addWidget(inner)
    box.toggled.connect(inner.setVisible)
    inner.setVisible(not collapsed)
    box.content_layout = layout
    return box


def _ShortcutHintBar(shortcuts_text="X/Y/Z: lock  |  Tab: plane  |  Esc: drop  |  hover+Enter: edit constraint"):
    """A one-line bar showing viewport shortcuts. Fixed height, light style.

    Returns the QWidget with `.lbl` and a `.set_shortcuts(text)` helper.
    """
    QtWidgets = _get_qt_widgets()
    bar = QtWidgets.QWidget()
    bar.setFixedHeight(28)
    lay = QtWidgets.QHBoxLayout(bar)
    lay.setContentsMargins(8, 4, 8, 4)
    lay.setSpacing(8)
    lbl = QtWidgets.QLabel(shortcuts_text)
    lbl.setStyleSheet("color:#9aa6b2;font-size:10px;")
    lay.addWidget(lbl)
    lay.addStretch(1)
    bar.lbl = lbl

    def set_shortcuts(text):
        lbl.setText(text)
    bar.set_shortcuts = set_shortcuts
    return bar


# ---------------------------------------------------------------------- #
# Coordinate-table styling (points/weights grids).
# ---------------------------------------------------------------------- #
# These DELIBERATELY set no background / gridline / alternate-row / spinbox
# colours: FreeCAD's theme stylesheet (FreeCAD.qss) already styles
# QAbstractItemView / QTableView / QHeaderView / QSpinBox from the user's
# own General->Colors preferences (e.g. gridline-color:@GeneralGridLinesColor,
# alternate-background-color:@GeneralAlternateBackgroundColor), applied
# globally. Setting our own table stylesheet would OVERRIDE that theme per
# widget -- the opposite of matching it. So we only add what the theme
# can't express: axis-coloured X/Y/Z headers, a flat delete button, and a
# muted weight label. (An earlier version detected dark/light via QPalette
# and swapped two hard-coded palettes; that both fought the theme and was
# unreliable -- a global Qt stylesheet doesn't repopulate QPalette, so the
# luminance read didn't reflect what was actually painted.)
# Factory FUNCTIONS (not Qt subclasses) so this module keeps its lazy-Qt
# property (guarded by test_gui_imports #5).

# X/Y/Z header text colours matching the gizmo axes (red/green/blue). One
# mid-tone set that reads on both light and dark table backgrounds, since
# we can't reliably detect the active theme (see note above). These are a
# deliberate RFS semantic, not a theme colour, and are set as item
# foreground (not via stylesheet) so they survive whatever theme is active.
_AXIS_HEADER_RGB = ((214, 69, 69), (52, 164, 84), (58, 123, 213))


def _muted_color():
    """A neutral mid-grey for secondary/placeholder text (e.g. the
    read-only weight label) that stays legible on both light and dark
    FreeCAD themes. A single value, not theme-derived, because a global Qt
    stylesheet theme doesn't repopulate QPalette for us to read."""
    return "#808a93"


def _style_point_table(table):
    """Enable the active FreeCAD theme's own alternating-row and grid
    styling (FreeCAD.qss paints those from the user's colour prefs) and
    colour the X/Y/Z header text to match the gizmo axes. Sets NO
    background / gridline / spinbox colours of its own, so the table
    inherits the active theme rather than overriding it. The first three
    header columns are assumed to be X/Y/Z."""
    from PySide import QtGui
    table.setAlternatingRowColors(True)   # use the theme's alternate colour
    table.setShowGrid(True)               # use the theme's gridline colour
    bold = QtGui.QFont()
    bold.setBold(True)
    for c, rgb in enumerate(_AXIS_HEADER_RGB):
        it = table.horizontalHeaderItem(c)
        if it is not None:
            it.setForeground(QtGui.QBrush(QtGui.QColor(*rgb)))
            it.setFont(bold)


def _delete_cell_button():
    """A flat, borderless 'x' delete button for a table cell. Inherits the
    theme's own text colour at rest (no colour override, so it matches the
    active FreeCAD theme) and turns red only on hover -- replacing the
    filled-maroon button whose rows stacked into a heavy, alarming red
    column."""
    qw = _get_qt_widgets()
    from PySide import QtCore
    btn = qw.QPushButton("×")
    btn.setFixedWidth(22)
    btn.setFlat(True)
    btn.setToolTip("Delete this point")
    try:
        btn.setCursor(QtCore.Qt.PointingHandCursor)
    except Exception:
        pass
    btn.setStyleSheet(
        "QPushButton{background:transparent;border:none;"
        "font-weight:bold;font-size:15px;padding:0;}"
        "QPushButton:hover{color:#e74c3c;}")
    return btn


# Shared status-line styling: one source for the grey/red status text
# that used to be inlined ~20 times across panels (see _status_label).
_STATUS_STYLE = "color:#9aa6b2;font-size:11px;padding:4px 0;"
_ERROR_STYLE = "color:#c0392b;font-size:11px;padding:4px 0;"


def _status_label(text=""):
    """Shared wrapping status line (grey, 11px) for task panels.

    This exact QLabel + word-wrap + stylesheet block was inlined in
    ~20 panel builds with drifting padding values; the factory is the
    single source (W3 -- shared widget builders). A FACTORY, not a Qt
    subclass, so this module keeps its lazy-Qt property: no Qt name is
    evaluated at import time (guarded by test_gui_imports check #5)."""
    qw = _get_qt_widgets()
    lbl = qw.QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(_STATUS_STYLE)
    return lbl


def _panel_status(panel, text, is_error=False):
    """Route a message to a panel's status label if it has one,
    otherwise fall back to Console (never silent)."""
    if hasattr(panel, "status") and panel.status is not None:
        if is_error:
            panel.status.setStyleSheet(_ERROR_STYLE)
        else:
            panel.status.setStyleSheet(_STATUS_STYLE)
        panel.status.setText(text)
    elif is_error:
        FreeCAD.Console.PrintError("Railform: " + text + "\n")
    else:
        FreeCAD.Console.PrintWarning("Railform: " + text + "\n")


def _panel_warning(panel, text):
    _panel_status(panel, text, is_error=False)


def _panel_error(panel, text):
    _panel_status(panel, text, is_error=True)


def drop_callbacks(view, specs):
    """Defensively remove Coin event callbacks and clear their IDs.

    specs: sequence of (event_type, owner, attr_name) -- e.g.
    ``("SoMouseButtonEvent", self, "_cb_btn")``.

    Three invariants the raw ``view.removeEventCallback`` call sites kept
    violating, each a known crash/AV ingredient:

    * **Per-callback isolation** -- the old pattern wrapped all removals
      in ONE try/except, so the first failure leaked every callback
      after it (and the missing-attr AttributeError of a half-built
      editor aborted the whole teardown).
    * **Null after remove** -- IDs are always cleared, even when removal
      raises, so a second teardown (panel close + document close) can
      never hand a STALE id back: FreeCAD callback ids are numeric and
      reused, so a stale one can unregister someone else's handler.
    * **Idempotent** -- calling it twice is a no-op.

    Duck-typed on `view`; safe under freecadcmd mocks (no Qt needed)."""
    for etype, owner, attr in specs:
        cb = getattr(owner, attr, None)
        if cb is None:
            continue
        try:
            view.removeEventCallback(etype, cb)
        except Exception as exc:
            try:
                FreeCAD.Console.PrintWarning(
                    "Railform: removing %s callback failed (%s)\n"
                    % (etype, exc))
            except Exception:
                pass
        finally:
            setattr(owner, attr, None)


# ---------------------------------------------------------------------- #
# _ReportPanelBase -- run-and-report QA panels (no doc-object, Close only)
# ---------------------------------------------------------------------- #
class _ReportPanelBase(object):
    """Shared scaffolding for run-and-report QA panels
    (ContinuityCheckPanel, NetworkAuditPanel, SurfaceReviewPanel,
    WorkflowGuidePanel, GaussianCurvaturePanel). These measure and display
    something on demand -- no parametric doc object to keep re-editable,
    so they only need a Close button, no transaction."""

    def getStandardButtons(self):
        QtWidgets = _get_qt_widgets()
        return int(QtWidgets.QDialogButtonBox.Close)

    def accept(self):
        self._close()
        return True

    def reject(self):
        self._close()
        return True

    def _close(self):
        try:
            _ensure_freecadgui()
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# _CurveGroup -- reusable "named link-list" widget
# ---------------------------------------------------------------------- #
class _CurveGroup(object):
    """One labelled group box: a QListWidget of linked objects (a link-list
    property on the panel's document object), with Add-selected/Remove
    buttons and, if `draggable`, Up/Down reorder buttons.

    panel      -- the owning _EditPanelBase (needs .obj and .recompute())
    prop       -- the link-list property name on panel.obj
    title      -- group-box title
    hint       -- one-line hint shown under the title
    max_items  -- optional cap; Add-selected silently stops adding past it
    draggable  -- if True, add Up/Down reorder buttons
    """

    def __init__(self, panel, prop, title, hint, max_items=None,
                draggable=False):
        self.panel = panel
        self.prop = prop
        self.max_items = max_items
        self.objs = list(getattr(panel.obj, prop, []) or [])

        QtWidgets = _get_qt_widgets()
        self.box = QtWidgets.QGroupBox(title)
        lay = QtWidgets.QVBoxLayout(self.box)
        if hint:
            lbl = QtWidgets.QLabel(hint)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#9aa6b2;font-size:10px;")
            lay.addWidget(lbl)

        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.setMaximumHeight(120)
        lay.addWidget(self.list)

        btn_row = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("Add selected")
        add_btn.clicked.connect(self._add_selected)
        rm_btn = QtWidgets.QPushButton("Remove")
        rm_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(rm_btn)
        if draggable:
            up_btn = QtWidgets.QPushButton("↑")
            up_btn.setFixedWidth(28)
            up_btn.clicked.connect(lambda: self._move(-1))
            down_btn = QtWidgets.QPushButton("↓")
            down_btn.setFixedWidth(28)
            down_btn.clicked.connect(lambda: self._move(1))
            btn_row.addWidget(up_btn)
            btn_row.addWidget(down_btn)
        lay.addLayout(btn_row)

        self._refresh()

    def _refresh(self):
        self.list.clear()
        for o in self.objs:
            self.list.addItem(getattr(o, "Label", getattr(o, "Name", "?")))

    def _commit(self):
        try:
            setattr(self.panel.obj, self.prop, self.objs)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: could not set %s (%s)\n" % (self.prop, exc))
        if hasattr(self.panel, "recompute"):
            self.panel.recompute()

    def _add_selected(self):
        for o in _selected_objects():
            if o is self.panel.obj or o in self.objs:
                continue
            if self.max_items is not None and len(self.objs) >= self.max_items:
                break
            self.objs.append(o)
        self._refresh()
        self._commit()

    def _remove_selected(self):
        rows = sorted((self.list.row(it)
                       for it in self.list.selectedItems()), reverse=True)
        for r in rows:
            if 0 <= r < len(self.objs):
                del self.objs[r]
        self._refresh()
        self._commit()

    def _move(self, delta):
        rows = sorted(self.list.row(it) for it in self.list.selectedItems())
        if not rows:
            return
        order = rows if delta < 0 else list(reversed(rows))
        for i in order:
            j = i + delta
            if 0 <= j < len(self.objs):
                self.objs[i], self.objs[j] = self.objs[j], self.objs[i]
        self._refresh()
        for i in rows:
            j = i + delta
            if 0 <= j < len(self.objs):
                self.list.item(j).setSelected(True)
        self._commit()