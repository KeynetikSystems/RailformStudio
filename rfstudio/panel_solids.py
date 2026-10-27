# -*- coding: utf-8 -*-
"""Solid-modeling and interoperability task panels: booleans
(Union/Subtract/Intersect), mesh/scan-to-NURBS reverse engineering, and
hidden-line drafting export. None of these had ANY prior native or
delegated path in rfstudio -- see rfstudio.engines' NATIVE_OVERRIDES
additions and rfstudio.core's boolean_union/boolean_subtract/
boolean_intersect, fit_surface_to_scan, hidden_line_projection, and
export_svg_drawing for the underlying geometry (all headless-tested in
test_core.py).

Split out as its own module (rather than folded into panel_surface.py)
because these three commands don't share panel_surface's "link-list
groups driven by parametric.GROUPS" shape: Boolean's Base is a single
Link (not a LinkList), MeshToSurface has no link-list input at all, and
HiddenLineExport needs a one-shot file-save action (Export SVG) that
none of the existing _EditPanelBase panels need.

GUI-only. The geometry these panels drive lives in rfstudio.core
(headless-tested).
"""

import FreeCAD
import FreeCADGui
import Part

from PySide import QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio import parametric
from rfstudio.panel_base import _EditPanelBase, _selected_objects, \
    drop_callbacks
from rfstudio import gizmo


def _is_solid(obj):
    shp = getattr(obj, "Shape", None)
    return shp is not None and bool(getattr(shp, "Solids", None))


class BooleanPanel(_EditPanelBase):
    """Union / Subtract / Intersect over solids -- RFS_BooleanUnion/
    Subtract/Intersect. Drives parametric.BooleanFP -> core.boolean_union/
    boolean_subtract/boolean_intersect.

    `operation` pre-selects the combo box (each curated command opens
    this panel already set to its own operation), but the combo stays
    live -- the user can switch operations without reopening the panel,
    same convenience BlendPanel's Continuity combo already offers.
    """

    def __init__(self, edit_obj=None, operation="Union"):
        self._start_edit(edit_obj,
                          ("Create " if edit_obj is None else "Edit ")
                          + "Boolean",
                          lambda: parametric.make_surface("Boolean", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Boolean")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select the BASE solid first, then the TOOL solid(s):"))

        base_row = QtWidgets.QHBoxLayout()
        self._base_lbl = QtWidgets.QLabel("Base: " + self._base_name())
        b1 = QtWidgets.QPushButton("Set base from selection")
        b1.clicked.connect(self._assign_base)
        base_row.addWidget(self._base_lbl)
        base_row.addStretch(1)
        base_row.addWidget(b1)
        lay.addLayout(base_row)

        tools_row = QtWidgets.QHBoxLayout()
        self._tools_lbl = QtWidgets.QLabel("Tools: " + self._tools_name())
        b2 = QtWidgets.QPushButton("Add tools from selection")
        b2.clicked.connect(self._add_tools)
        tools_row.addWidget(self._tools_lbl)
        tools_row.addStretch(1)
        tools_row.addWidget(b2)
        lay.addLayout(tools_row)
        clr = QtWidgets.QPushButton("Clear tools")
        clr.clicked.connect(self._clear_tools)
        lay.addWidget(clr)

        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(QtWidgets.QLabel("Operation"))
        self.op = QtWidgets.QComboBox()
        self.op.addItems(["Union", "Subtract", "Intersect"])
        cur_op = str(getattr(self.obj, "Operation", operation) or operation)
        if self.creating:
            cur_op = operation
        self.op.setCurrentText(cur_op)
        self.op.currentTextChanged.connect(lambda v: self._set("Operation", v))
        op_row.addWidget(self.op)
        lay.addLayout(op_row)
        if self.creating:
            self.obj.Operation = cur_op

        self.refine = QtWidgets.QCheckBox(
            "Refine (remove redundant coincident-face splitters)")
        self.refine.setChecked(bool(self.obj.Refine))
        self.refine.toggled.connect(lambda v: self._set("Refine", v))
        lay.addWidget(self.refine)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)

        if self.creating:
            sel = [o for o in _selected_objects() if _is_solid(o)]
            if sel:
                self.obj.Base = sel[0]
                self._base_lbl.setText("Base: " + self._base_name())
            if len(sel) > 1:
                self.obj.Tools = sel[1:]
                self._tools_lbl.setText("Tools: " + self._tools_name())
        self.recompute()

    def _base_name(self):
        o = getattr(self.obj, "Base", None)
        return o.Label if o else "(none)"

    def _tools_name(self):
        links = list(getattr(self.obj, "Tools", []) or [])
        return ", ".join(o.Label for o in links) if links else "(none)"

    def _assign_base(self):
        sel = [o for o in _selected_objects() if _is_solid(o)]
        if sel:
            self.obj.Base = sel[0]
            self._base_lbl.setText("Base: " + self._base_name())
            self.recompute()

    def _add_tools(self):
        existing = list(getattr(self.obj, "Tools", []) or [])
        for o in _selected_objects():
            if _is_solid(o) and o is not self.obj.Base and o not in existing:
                existing.append(o)
        self.obj.Tools = existing
        self._tools_lbl.setText("Tools: " + self._tools_name())
        self.recompute()

    def _clear_tools(self):
        self.obj.Tools = []
        self._tools_lbl.setText("Tools: (none)")
        self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self, *args):
        if self.live is None or self.live.isChecked():
            try:
                self._recompute_obj()
            except Exception as exc:
                self._status.setText("Failed: %s" % exc)
                self._status.setStyleSheet(
                    "color:#c0392b;font-size:11px;padding:4px 0;")
                self._defer_gui_update()
                return
            self._defer_gui_update()
        has_base = bool(getattr(self.obj, "Base", None))
        has_tools = bool(getattr(self.obj, "Tools", None))
        shp = getattr(self.obj, "Shape", None)
        shape_ok = shp is not None and not shp.isNull() and shp.Solids
        if not has_base:
            msg, ok = "Select a base solid.", False
        elif not has_tools:
            msg, ok = "Select >= 1 tool solid.", False
        elif shape_ok:
            msg, ok = ("%s ready. Result volume=%.2f"
                      % (self.op.currentText(), shp.Volume)), True
        else:
            msg, ok = "Waiting on recompute…", False
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class MeshToSurfacePanel(_EditPanelBase):
    """Reverse-engineering: fit a single approximating NURBS patch to a
    raw point cloud / mesh -- RFS_MeshToSurface. Drives
    parametric.MeshToSurfaceFP -> core.fit_surface_to_scan.

    See core.fit_surface_to_scan's docstring for the explicit scope
    honesty: single-patch, height-field-only -- not a general mesh
    retopology engine.
    """

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          ("Create " if edit_obj is None else "Edit ")
                          + "MeshToSurface",
                          lambda: parametric.make_surface("MeshToSurface",
                                                          self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Mesh/scan to surface")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select a Mesh object (or any point-bearing object), then "
            "set the fitting resolution. This fits ONE height-field "
            "patch -- not a general mesh retopology tool; closed/"
            "tubular mesh shapes will not fit well."))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Source: " + self._src_name())
        b = QtWidgets.QPushButton("Set source from selection")
        b.clicked.connect(self._assign_source)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b)
        lay.addLayout(src_row)

        grid_row = QtWidgets.QHBoxLayout()
        grid_row.addWidget(QtWidgets.QLabel("Grid U"))
        self.gu = QtWidgets.QSpinBox()
        self.gu.setRange(2, 200)
        self.gu.setValue(int(self.obj.GridU))
        grid_row.addWidget(self.gu)
        grid_row.addWidget(QtWidgets.QLabel("Grid V"))
        self.gv = QtWidgets.QSpinBox()
        self.gv.setRange(2, 200)
        self.gv.setValue(int(self.obj.GridV))
        grid_row.addWidget(self.gv)
        lay.addLayout(grid_row)
        self.gu.valueChanged.connect(lambda v: self._set("GridU", v))
        self.gv.valueChanged.connect(lambda v: self._set("GridV", v))

        lay.addWidget(QtWidgets.QLabel(
            "Projection normal (0,0,0 = auto-pick flattest axis)"))
        n_row = QtWidgets.QHBoxLayout()
        self._n_spins = []
        cur = self.obj.ProjectionNormal
        for axis, val in zip(("X", "Y", "Z"), (cur.x, cur.y, cur.z)):
            n_row.addWidget(QtWidgets.QLabel(axis))
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(3)
            sb.setRange(-1.0, 1.0)
            sb.setSingleStep(0.1)
            sb.setValue(val)
            sb.valueChanged.connect(self._on_normal_changed)
            n_row.addWidget(sb)
            self._n_spins.append(sb)
        lay.addLayout(n_row)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)

        if self.creating:
            sel = [o for o in _selected_objects() if o is not self.obj]
            if sel:
                self.obj.Source = sel[0]
                self._src_lbl.setText("Source: " + self._src_name())
        self.recompute()

    def _src_name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _assign_source(self):
        sel = [o for o in _selected_objects() if o is not self.obj]
        if sel:
            self.obj.Source = sel[0]
            self._src_lbl.setText("Source: " + self._src_name())
            self.recompute()

    def _on_normal_changed(self, *_args):
        from FreeCAD import Vector
        x, y, z = (sb.value() for sb in self._n_spins)
        self.obj.ProjectionNormal = Vector(x, y, z)
        self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self, *args):
        if self.live is None or self.live.isChecked():
            try:
                self._recompute_obj()
            except Exception as exc:
                self._status.setText("Failed: %s" % exc)
                self._status.setStyleSheet(
                    "color:#c0392b;font-size:11px;padding:4px 0;")
                self._defer_gui_update()
                return
            self._defer_gui_update()
        has_source = bool(getattr(self.obj, "Source", None))
        shp = getattr(self.obj, "Shape", None)
        shape_ok = shp is not None and not shp.isNull() and shp.Faces
        if not has_source:
            msg, ok = "Select a mesh/point source.", False
        elif shape_ok:
            msg, ok = "Ready. Fitted area=%.2f" % shp.Area, True
        else:
            msg, ok = ("Source has < 4 usable points, or hasn't "
                      "recomputed yet."), False
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class HiddenLineExportPanel(_EditPanelBase):
    """2D hidden-line/visible-line technical drawing -- RFS_HiddenLineExport.
    Drives parametric.HiddenLineDrawingFP -> core.hidden_line_projection,
    plus an "Export SVG…" action button wired to
    core.export_svg_drawing -- the drafting-export path that previously
    didn't exist at all in rfstudio.

    NOTE: core.hidden_line_projection needs FreeCAD's `Drawing` module;
    see that function's docstring for the version caveat on
    Drawing.projectEx's exact return arity. If that module isn't
    available in this FreeCAD build, the panel reports the failure
    plainly rather than silently producing an empty drawing.
    """

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          ("Create " if edit_obj is None else "Edit ")
                          + "HiddenLineDrawing",
                          lambda: parametric.make_surface("HiddenLineDrawing",
                                                          self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Hidden-line drawing")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select the solids/surfaces to project, then set a view "
            "direction. Export SVG writes the visible (solid) + hidden "
            "(dashed) outline to a file."))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Sources: " + self._src_name())
        b = QtWidgets.QPushButton("Add selected")
        b.clicked.connect(self._add_sources)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b)
        lay.addLayout(src_row)
        clr = QtWidgets.QPushButton("Clear sources")
        clr.clicked.connect(self._clear_sources)
        lay.addWidget(clr)

        lay.addWidget(QtWidgets.QLabel("View direction"))
        dir_row = QtWidgets.QHBoxLayout()
        self._dir_spins = []
        cur = self.obj.Direction
        for axis, val in zip(("X", "Y", "Z"), (cur.x, cur.y, cur.z)):
            dir_row.addWidget(QtWidgets.QLabel(axis))
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(3)
            sb.setRange(-1.0, 1.0)
            sb.setSingleStep(0.1)
            sb.setValue(val)
            sb.valueChanged.connect(self._on_direction_changed)
            dir_row.addWidget(sb)
            self._dir_spins.append(sb)
        lay.addLayout(dir_row)
        preset_row = QtWidgets.QHBoxLayout()
        for label, vec in (("Top (+Z)", (0, 0, 1)), ("Front (-Y)", (0, -1, 0)),
                          ("Right (+X)", (1, 0, 0))):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _c=False, v=vec: self._set_direction(*v))
            preset_row.addWidget(b)
        lay.addLayout(preset_row)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        export_btn = QtWidgets.QPushButton("Export SVG…")
        export_btn.clicked.connect(self._export_svg)
        lay.addWidget(export_btn)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)

        if self.creating:
            sel = [o for o in _selected_objects() if o is not self.obj]
            if sel:
                self.obj.Sources = sel
                self._src_lbl.setText("Sources: " + self._src_name())
        self.recompute()

    def _src_name(self):
        links = list(getattr(self.obj, "Sources", []) or [])
        return ", ".join(o.Label for o in links) if links else "(none)"

    def _add_sources(self):
        existing = list(getattr(self.obj, "Sources", []) or [])
        for o in _selected_objects():
            if o is not self.obj and o not in existing:
                existing.append(o)
        self.obj.Sources = existing
        self._src_lbl.setText("Sources: " + self._src_name())
        self.recompute()

    def _clear_sources(self):
        self.obj.Sources = []
        self._src_lbl.setText("Sources: (none)")
        self.recompute()

    def _set_direction(self, x, y, z):
        from FreeCAD import Vector
        self._syncing_dir = True
        try:
            for sb, v in zip(self._dir_spins, (x, y, z)):
                sb.setValue(v)
        finally:
            self._syncing_dir = False
        self.obj.Direction = Vector(x, y, z)
        self.recompute()

    def _on_direction_changed(self, *_args):
        if getattr(self, "_syncing_dir", False):
            return
        from FreeCAD import Vector
        x, y, z = (sb.value() for sb in self._dir_spins)
        self.obj.Direction = Vector(x, y, z)
        self.recompute()

    def recompute(self, *args):
        if self.live is None or self.live.isChecked():
            try:
                self._recompute_obj()
            except Exception as exc:
                self._status.setText("Failed: %s" % exc)
                self._status.setStyleSheet(
                    "color:#c0392b;font-size:11px;padding:4px 0;")
                self._defer_gui_update()
                return
            self._defer_gui_update()
        has_sources = bool(getattr(self.obj, "Sources", None))
        shp = getattr(self.obj, "Shape", None)
        shape_ok = shp is not None and not shp.isNull() and shp.Edges
        if not has_sources:
            msg, ok = "Select >= 1 source object.", False
        elif shape_ok:
            msg, ok = ("Ready. %d visible edge(s) projected."
                      % len(shp.Edges)), True
        else:
            msg, ok = ("Projection failed or produced no visible edges "
                      "-- check the Report view (the 'Drawing' module "
                      "may not be available in this FreeCAD build)."), False
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")

    def _export_svg(self):
        from rfstudio import core
        shp = getattr(self.obj, "Shape", None)
        if shp is None or shp.isNull():
            QtWidgets.QMessageBox.warning(
                self.form, "Railform Studio",
                "Nothing to export yet -- select sources and let the "
                "drawing recompute first.")
            return
        fn, _filt = QtWidgets.QFileDialog.getSaveFileName(
            self.form, "Export hidden-line drawing", "", "SVG (*.svg)")
        if not fn:
            return
        if not fn.lower().endswith(".svg"):
            fn += ".svg"
        hidden_edges = []
        doc = self.obj.Document
        for o in doc.Objects:
            if o.Label == self.obj.Label + "_Hidden":
                hidden_edges = list(getattr(o.Shape, "Edges", []) or [])
                break
        try:
            core.export_svg_drawing(
                list(shp.Edges), hidden_edges, self.obj.Direction, fn)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self.form, "Railform Studio", "SVG export failed: %s" % exc)
            return
        FreeCAD.Console.PrintMessage(
            "Railform: exported hidden-line drawing to %s\n" % fn)
class MirrorPanel(_EditPanelBase):
    """Model one half, mirror the rest -- RFS_Mirror. Drives
    parametric.MirrorFP -> Part.Shape.mirror (+ rfstudio.mirror /
    core.stitch_shell when WeldSeam is on).

    This is a ONE-WAY parametric mirror: edit Source and recompute, the
    mirrored copy follows automatically via FreeCAD's own dependency
    graph -- two objects (Source + Mirror), one always derived from the
    other. It is NOT live bidirectional point-dragging on a single
    object; for that ("drag either half of the SAME curve, no second
    object at all") see CurvePanel's "Symmetric editing" checkbox
    (panel_curve.py / editmode.PointEditor.set_symmetry).
    """

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create Mirror" if edit_obj is None else "Edit Mirror",
                          lambda: parametric.make_surface("Mirror", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Mirror")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Model one half, mirror the rest. Pick the source, a plane, "
            "and optionally weld the seam into one continuous shell/solid."))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Source: " + self._src_name())
        b = QtWidgets.QPushButton("Set source from selection")
        b.clicked.connect(self._assign_source)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b)
        lay.addLayout(src_row)

        plane_row = QtWidgets.QHBoxLayout()
        plane_row.addWidget(QtWidgets.QLabel("Plane"))
        self.plane = QtWidgets.QComboBox()
        self.plane.addItems(["XY", "YZ", "ZX", "Custom", "Pick from model"])
        self.plane.setCurrentText(str(self.obj.Plane))
        self.plane.currentTextChanged.connect(lambda v: self._set("Plane", v))
        plane_row.addWidget(self.plane)
        lay.addLayout(plane_row)

        self._custom_row = QtWidgets.QWidget()
        crow = QtWidgets.QHBoxLayout(self._custom_row)
        crow.setContentsMargins(0, 0, 0, 0)
        self._base_spins, self._normal_spins = [], []
        for label, vec, store in (("Base", self.obj.Base, self._base_spins),
                                  ("Normal", self.obj.Normal, self._normal_spins)):
            crow.addWidget(QtWidgets.QLabel(label))
            for axis, val in zip(("X", "Y", "Z"), (vec.x, vec.y, vec.z)):
                sb = QtWidgets.QDoubleSpinBox()
                sb.setDecimals(3)
                sb.setRange(-1e6, 1e6)
                sb.setValue(val)
                sb.valueChanged.connect(self._on_custom_changed)
                crow.addWidget(sb)
                store.append(sb)
        lay.addWidget(self._custom_row)
        self._custom_row.setVisible(str(self.obj.Plane) == "Custom")
        self.plane.currentTextChanged.connect(
            lambda v: self._custom_row.setVisible(v == "Custom"))

        # Pick from model functionality
        self._pick_mode = False
        self._pick_cb = None
        self._selected_face = None

        self.show_inputs = QtWidgets.QCheckBox(
            "Show source alongside the mirrored copy")
        self.show_inputs.setChecked(bool(self.obj.ShowInputs))
        self.show_inputs.toggled.connect(lambda v: self._set("ShowInputs", v))
        lay.addWidget(self.show_inputs)

        self.weld = QtWidgets.QCheckBox(
            "Weld seam (stitch into one continuous shell/solid)")
        self.weld.setChecked(bool(self.obj.WeldSeam))
        self.weld.setToolTip(
            "Requires Source's boundary to actually touch the mirror "
            "plane. Falls back to an unwelded compound with a console "
            "warning if stitching fails.")
        self.weld.toggled.connect(lambda v: self._set("WeldSeam", v))
        lay.addWidget(self.weld)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)

        if self.creating:
            sel = [o for o in _selected_objects() if o is not self.obj]
            if sel:
                self.obj.Source = sel[0]
                self._src_lbl.setText("Source: " + self._src_name())
        self.recompute()

    def _extra_teardown(self):
        # If the dialog is closed (accept OR reject) while "Pick from
        # model" is still waiting for a click, _stop_pick_mode() removes
        # the SoMouseButtonEvent callback -- otherwise it keeps firing
        # against this closed panel/its obj on every later 3D-view click.
        if getattr(self, '_pick_mode', False):
            self._stop_pick_mode()

    def _src_name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _assign_source(self):
        sel = [o for o in _selected_objects() if o is not self.obj]
        if sel:
            self.obj.Source = sel[0]
            self._src_lbl.setText("Source: " + self._src_name())
            self.recompute()

    def _on_custom_changed(self, *_args):
        from FreeCAD import Vector
        bx, by, bz = (sb.value() for sb in self._base_spins)
        nx, ny, nz = (sb.value() for sb in self._normal_spins)
        self.obj.Base = Vector(bx, by, bz)
        self.obj.Normal = Vector(nx, ny, nz)
        self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        if name == "Plane" and value == "Pick from model":
            self._start_pick_mode()
        elif name == "Plane" and self._pick_mode:
            self._stop_pick_mode()
        self.recompute()

    def _start_pick_mode(self):
        """Start face picking mode to select a plane from the model."""
        try:
            self._pick_mode = True
            self._pick_btn = QtWidgets.QPushButton("Pick face (click in 3D view)")
            self._pick_btn.setStyleSheet("QPushButton { background-color: #4a90e2; color: white; }")
            # Insert the button before the status label
            # self._layout() never existed -- the panel's layout is the one
            # QVBoxLayout(self.form) installed on the form widget.
            _lay = self.form.layout()
            _lay.insertWidget(_lay.count() - 1, self._pick_btn)
            self._pick_btn.clicked.connect(self._cancel_pick_mode)

            # Set up the pick callback
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                self._pick_cb = view.addEventCallback("SoMouseButtonEvent", self._on_pick)
                self._pick_btn.setText("Click on a face to select its plane")
        except Exception as exc:
            self._stop_pick_mode()
            QtGui.QMessageBox.warning(
                self.form, "Pick Mode Error",
                "Failed to start pick mode: %s" % str(exc))

    def _stop_pick_mode(self):
        """Stop face picking mode."""
        self._pick_mode = False
        if getattr(self, "_pick_cb", None) is not None:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view is not None:
                    drop_callbacks(view, (
                        ("SoMouseButtonEvent", self, "_pick_cb"),
                    ))
            except Exception:
                pass
            self._pick_cb = None
        if self._pick_btn:
            self._pick_btn.hide()
            self._pick_btn.deleteLater()
            self._pick_btn = None
        self._pick_btn = None
        # Reset to a default plane if we were in pick mode
        if self.obj.Plane == "Pick from model":
            self.obj.Plane = "XY"
            self.plane.setCurrentText("XY")

    def _cancel_pick_mode(self):
        """Cancel pick mode and return to previous state."""
        self._stop_pick_mode()
        self.plane.setCurrentText(getattr(self.obj, "Plane", "XY"))

    def _on_pick(self, event):
        """Handle mouse click events during pick mode."""
        if not self._pick_mode or not self._pick_cb:
            return

        try:
            if event.get("State") != "DOWN":
                return
            if event.get("Button") != "BUTTON1":
                return

            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if not view:
                return

            # Get the clicked point
            pos = event["Position"]
            point = view.getPoint(pos[0], pos[1])

            # Find the closest face under the cursor
            from pivy import coin
            # Create a ray from the camera through the point
            ray = view.getRay(pos[0], pos[1])
            if not ray:
                return

            # Get the objects under the ray
            result = view.getObjects(ray, 5)  # Get up to 5 objects
            if not result or not result[0]:
                return

            # Get the first object and face
            obj_info = result[0]
            obj = obj_info.get("Object")
            if not obj:
                return

            # Check if it's a face
            if hasattr(obj, "Face") and isinstance(obj.Face, Part.Face):
                face = obj.Face
                # Get the face normal at center
                try:
                    u0, u1, v0, v1 = face.ParameterRange
                    center_u = 0.5 * (u0 + u1)
                    center_v = 0.5 * (v0 + v1)
                    normal = face.normalAt(center_u, center_v)
                    # Create a plane from this normal and a point on the face
                    point_on_face = face.valueAt(center_u, center_v)

                    # Set the plane to custom with this normal and point
                    self.obj.Plane = "Custom"
                    self.obj.Base = point_on_face
                    self.obj.Normal = normal
                    self.plane.setCurrentText("Custom")
                    self._update_custom_controls()

                    # Stop pick mode
                    self._stop_pick_mode()

                    # Show feedback
                    QtGui.QMessageBox.information(
                        self.form, "Face Selected",
                        "Selected face normal: (%0.3f, %0.3f, %0.3f)" %
                        (normal.x, normal.y, normal.z))
                except Exception as exc:
                    QtGui.QMessageBox.warning(
                        self.form, "Error",
                        "Failed to process selected face: %s" % str(exc))
            else:
                QtGui.QMessageBox.information(
                    self.form, "Not a Face",
                    "Please click on a face to select its plane.")

        except Exception as exc:
            QtGui.QMessageBox.warning(
                self.form, "Pick Error",
                "Error during pick processing: %s" % str(exc))

    def _update_custom_controls(self):
        """Update the custom base/normal spin controls when values change."""
        if not hasattr(self, '_base_spins') or not self._base_spins:
            return
        try:
            base = self.obj.Base
            normal = self.obj.Normal
            for i, spin in enumerate(self._base_spins):
                spin.setValue([base.x, base.y, base.z][i])
            for i, spin in enumerate(self._normal_spins):
                spin.setValue([normal.x, normal.y, normal.z][i])
        except Exception:
            pass

    def recompute(self, *args):
        if self.live is None or self.live.isChecked():
            try:
                self._recompute_obj()
            except Exception as exc:
                self._status.setText("Failed: %s" % exc)
                self._status.setStyleSheet(
                    "color:#c0392b;font-size:11px;padding:4px 0;")
                self._defer_gui_update()
                return
            self._defer_gui_update()
        has_source = bool(getattr(self.obj, "Source", None))
        shp = getattr(self.obj, "Shape", None)
        shape_ok = shp is not None and not shp.isNull()
        if not has_source:
            msg, ok = "Select a source object.", False
        elif shape_ok:
            welded = bool(getattr(self.obj, "_LastWeldOK", False))
            if self.obj.WeldSeam:
                note = (" (welded solid)" if welded and shp.Solids else
                        " (welded shell)" if welded else
                        " (weld requested but failed -- showing "
                        "unwelded compound; check the Report view)")
            else:
                note = ""
            msg, ok = "Ready.%s" % note, True
        else:
            msg, ok = "Waiting on recompute…", False
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")