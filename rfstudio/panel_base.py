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
import FreeCADGui

from PySide import QtCore, QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui


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
    btns = QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    return int(getattr(btns, "value", btns))


def _selected_objects():
    """Currently selected document objects (whole-object selection)."""
    return list(FreeCADGui.Selection.getSelection())


def _selected_faces():
    """[(face, owning_object), ...] for every face in the current
    selection -- sub-face picks first, else every face of a whole-object
    pick."""
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


def _selected_points():
    """Vector points from the current selection: sub-vertex picks first,
    else every vertex of a whole-object pick."""
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


def _notify_overlay_unavailable(panel, feature_name, exc):
    """Best-effort user-visible notice that an optional Coin3D overlay
    (highlight ring, drag gizmo, ...) could not be built on this system --
    the panel keeps working, just without that visual aid. Never raises."""
    try:
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
        _extra_teardown()  -- called on BOTH accept and reject, before the
                             dialog is closed (Coin overlay/gizmo teardown)
    """

    def _start_edit(self, edit_obj, title, factory):
        self.doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
        self.creating = edit_obj is None
        self.doc.openTransaction(title)
        self.obj = edit_obj if edit_obj is not None else factory()
        self._edit_title = title

    def _pre_accept(self):
        pass

    def _pre_reject(self):
        pass

    def _post_abort(self):
        pass

    def _extra_teardown(self):
        pass

    def getStandardButtons(self):
        return _std_buttons()

    def accept(self):
        try:
            self._pre_accept()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: pre-accept hook failed (%s)\n" % exc)
        try:
            self.obj.recompute()
        except Exception:
            pass
        self._extra_teardown()
        try:
            self.doc.commitTransaction()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: commit failed (%s)\n" % exc)
        self._close()
        return True

    def reject(self):
        try:
            self._pre_reject()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: pre-reject hook failed (%s)\n" % exc)
        self._extra_teardown()
        try:
            self.doc.abortTransaction()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: abort failed (%s)\n" % exc)
        try:
            self._post_abort()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: post-abort hook failed (%s)\n" % exc)
        self._close()
        return True

    def _close(self):
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass


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
        return int(QtWidgets.QDialogButtonBox.Close)

    def accept(self):
        self._close()
        return True

    def reject(self):
        self._close()
        return True

    def _close(self):
        try:
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