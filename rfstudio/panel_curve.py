# -*- coding: utf-8 -*-
"""Curve-drawing task panels: point-defined curves (CurvePanel) and the
curve-to-curve blend (BlendPanel). Split out of the old taskpanels.py
monolith -- see rfstudio.panel_base for the shared _EditPanelBase
scaffolding and selection helpers this module builds on.

GUI-only. The geometry it drives lives in rfstudio.core (headless-tested).
"""

import FreeCAD
import FreeCADGui
import math

from PySide import QtCore, QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio import parametric
from rfstudio import snaputil
from rfstudio.panel_base import (
    _EditPanelBase, _selected_points, _notify_overlay_unavailable,
)


class _CoordSpin(QtWidgets.QDoubleSpinBox):
    """Numeric up/down for one coordinate; reports focus so clicking it
    highlights the point in 3D."""

    def __init__(self, on_focus):
        super(_CoordSpin, self).__init__()
        self._on_focus = on_focus
        self.setRange(-1.0e6, 1.0e6)
        self.setDecimals(3)
        self.setSingleStep(1.0)
        self.setKeyboardTracking(False)   # emit on commit, not each keystroke

    def focusInEvent(self, event):
        super(_CoordSpin, self).focusInEvent(event)
        self._on_focus()


class _WeightSpin(QtWidgets.QDoubleSpinBox):
    """Numeric up/down for weight (non-negative)."""

    def __init__(self, on_focus):
        super(_WeightSpin, self).__init__()
        self._on_focus = on_focus
        self.setRange(0.001, 10.0)
        self.setDecimals(3)
        self.setSingleStep(0.1)
        self.setKeyboardTracking(False)

    def focusInEvent(self, event):
        super(_WeightSpin, self).focusInEvent(event)
        self._on_focus()


class CurvePanel(_EditPanelBase):
    """Draw/model a point-defined curve: interpolate / control-poly / approx /
    polyline. Points via coordinate table, selection, or 3D viewport picking.
    Clicking a coordinate highlights that point in the 3D view.

    Also hosts "Symmetric editing" (model only half): when enabled, the
    live 3D editor (editmode.PointEditor) mirrors every drag across a
    chosen plane onto that point's partner in the SAME object -- no
    second object needed. Requires the current point set to already be
    symmetric about the chosen plane (e.g. after mirroring once via
    RFS_Mirror with WeldSeam off and manually merging points, or by
    placing points symmetrically from the start).
    """

    def __init__(self, kind=None, edit_obj=None, editor=None):
        self.kind = kind or (edit_obj.Proxy.Kind if edit_obj else "InterpCurve")
        self._start_edit(edit_obj,
                  ("Draw " if edit_obj is None else "Edit ") + self.kind,
                  lambda: parametric.make_curve(self.kind, self.doc))
        self._editor = editor       # PointEditor when opened alongside 3D edit mode
        if self._editor is None and self.kind != "BlendCurve":
            # Construct one ourselves so we can swap it for KnotEditor later
            # (ControlCurve only). Skip for BlendCurve (no points to drag).
            from rfstudio import editmode
            vobj = self.obj.ViewObject
            if vobj is not None:
                self._editor = editmode.PointEditor(vobj)
                if self._editor.ok:
                    self._editor.on_commit = self._fill_table
                    self._editor.creating = self.creating
                else:
                    self._editor = None
        self._pick_count = 0        # per-pick committed transactions
        self._picking = False
        self._cbid = None
        self._cbid_mov = None       # move-event callback for draw-mode snap
        self._snap_pt = None        # snapped 3D point, used by next click
        self._snap_cands = []       # cached candidates for current pick session
        self._snap_shapes = []      # cached edges for exact projection snap
        self._snap_coin_root = None # Coin3D snap indicator root
        self._active_row = None
        self._rows = []
        self._weights = []          # per-point weights
        # Load existing weights if present
        if hasattr(self.obj, "Weights"):
            self._weights = list(self.obj.Weights)
        else:
            self._weights = []
        self._sync = False
        self._dragger = None
        self._drag_row = None
        self._giz_base = None
        self._giz_s = 1.0
        self._pending = None        # latest gizmo drag value, applied on a timer
        self._pending_scheduled = False
        self._drag_value = None     # final point, committed to obj on release
        self._drawing = False       # draw mode active flag
        self._draw_cb_id = None     # Coin3D callback ID for draw mode
        self._draw_cb_id_mov = None # Coin3D callback ID for mouse move in draw mode
        # Set the points and weights via _set_points to ensure the object's Weights is updated
        self._set_points(self._points())

        # Curvature visualization
        self._curvature_overlay = None
        self._curvature_coords = None
        self._curvature_lines = None
        self._curvature_switch = None
        self._curvature_scale = 1.0

        # Constraints
        self.constraints = []

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – %s" % self.kind)
        self._lay = QtWidgets.QVBoxLayout(self.form)

        # Add curve type converter controls
        self._add_converter_controls()

        self._build()
        self._setup_highlight()
        if self.creating:
            pre = _selected_points()
            if pre:
                self._set_points(pre)
        self._fill_table()
        self.recompute()

    # ---- UI ---- #
    def _add_converter_controls(self):
        """Add controls for converting between curve types."""
        convert_layout = QtWidgets.QHBoxLayout()
        convert_layout.addWidget(QtWidgets.QLabel("Convert To:"))
        self.convert_type_combo = QtWidgets.QComboBox()
        self.convert_type_combo.addItems(["InterpCurve", "ControlCurve", "ApproxCurve", "Polyline", "BezierCurve"])
        self.convert_type_combo.setCurrentText(self.kind)
        convert_layout.addWidget(self.convert_type_combo)
        self.convert_btn = QtWidgets.QPushButton("Convert")
        self.convert_btn.clicked.connect(self._on_convert_clicked)
        convert_layout.addWidget(self.convert_btn)
        convert_layout.addStretch()
        self._lay.addLayout(convert_layout)

    def _build(self):
        header_labels = ["X", "Y", "Z", "W" if self.kind == "ControlCurve" else "", ""]
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(header_labels)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection)   # one row only
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QtWidgets.QHeaderView.Fixed)
        hh.resizeSection(4, 26)
        hh.setMinimumSectionSize(36)
        vh = self.table.verticalHeader()
        vh.setDefaultSectionSize(24)
        vh.setFixedWidth(24)
        vh.sectionClicked.connect(lambda r: self._focus_row(r))
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.table.setMaximumHeight(220)
        self.table.setMaximumWidth(300)
        self.table.setAlternatingRowColors(True)
        # colour the X/Y/Z headers to match the gizmo axes (red/green/blue)
        axis_rgb = ((232, 96, 96), (110, 200, 110), (96, 150, 235))
        bold = QtGui.QFont()
        bold.setBold(True)
        for c, rgb in enumerate(axis_rgb):
            it = self.table.horizontalHeaderItem(c)
            if it is not None:
                it.setForeground(QtGui.QBrush(QtGui.QColor(*rgb)))
                it.setFont(bold)
        self.table.setStyleSheet(
            "QTableWidget{gridline-color:#5a6675;"
            "alternate-background-color:#323b47;background:#2b333d;}"
            "QHeaderView::section{background:#39434f;padding:2px;}"
            "QDoubleSpinBox{background:#222a33;color:#eef3f8;border:1px solid "
            "#4a5563;border-radius:2px;}")
        self._lay.addWidget(self.table)

        row = QtWidgets.QHBoxLayout()
        for label, slot in (("Add selected", self._add_sel),
                            ("Remove", self._remove), ("↑", self._up),
                            ("↓", self._down)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        self._lay.addLayout(row)

        self.pick_btn = QtWidgets.QPushButton("Pick in 3D: off")
        self.pick_btn.setCheckable(True)
        self.pick_btn.toggled.connect(self._toggle_pick)
        self._lay.addWidget(self.pick_btn)

        # Draw mode toggle button
        self.draw_btn = QtWidgets.QPushButton("Draw Curve: off")
        self.draw_btn.setCheckable(True)
        self.draw_btn.toggled.connect(self._toggle_draw_mode)
        self._lay.addWidget(self.draw_btn)

        self._scalar_controls()

        # ControlCurve B-splines: extra toggle that swaps the in-viewport editor
        # from control-point dragging to knot dragging. Only meaningful when
        # there is a 3D editor attached AND the curve kind supports knots.
        if self.kind == "ControlCurve" and getattr(self, "_editor", None) is not None:
            self._knot_toggle = QtWidgets.QCheckBox("Edit knots (drag in viewport)")
            self._knot_toggle.setChecked(False)
            self._knot_toggle.toggled.connect(self._toggle_knot_editor)
            self._lay.addWidget(self._knot_toggle)
            self.table.itemSelectionChanged.connect(self._sync_knot_highlight)

        # Symmetric editing: model only half, drag mirrors the partner
        # point in this SAME object. Only meaningful for point-defined
        # curve kinds with a live 3D editor attached.
        if self.kind in ("InterpCurve", "ControlCurve", "ApproxCurve", "Polyline", "BezierCurve") \
                and getattr(self, "_editor", None) is not None:
            # Insert-on-click toggle. Off by default: a plain viewport click
            # selects/drags points; a segment click inserts a point only when
            # this is on OR Shift is held (mirrors PointEditor.insert_on_click).
            self._insert_toggle = QtWidgets.QCheckBox(
                "Insert point on segment click (else Shift+click)")
            self._insert_toggle.setChecked(
                bool(getattr(self._editor, "insert_on_click", False)))
            self._insert_toggle.toggled.connect(self._toggle_insert_on_click)
            self._lay.addWidget(self._insert_toggle)

            sym_row = QtWidgets.QHBoxLayout()
            self.sym_toggle = QtWidgets.QCheckBox("Symmetric editing (model half)")
            self.sym_toggle.setToolTip(
                "Drag one side, the mirror-image point (across the chosen "
                "plane) follows live in this same curve. Requires the "
                "current points to already be symmetric about that plane.")
            self.sym_plane_combo = QtWidgets.QComboBox()
            self.sym_plane_combo.addItems(["XY", "YZ", "ZX"])
            self.sym_toggle.toggled.connect(self._toggle_symmetry)
            self.sym_plane_combo.currentTextChanged.connect(
                self._on_sym_plane_changed)
            sym_row.addWidget(self.sym_toggle)
            sym_row.addWidget(self.sym_plane_combo)

            # Add promote to MFP button
            self.promote_btn = QtWidgets.QPushButton("Promote to MFP")
            self.promote_btn.setToolTip(
                "Convert this symmetric curve into an explicit MirrorFP pair")
            self.promote_btn.clicked.connect(self._promote_to_mfp)
            sym_row.addWidget(self.promote_btn)

            self._lay.addLayout(sym_row)

            # Bezier-specific controls
            if self.kind == "BezierCurve":
                bezier_row = QtWidgets.QHBoxLayout()
                bezier_row.addWidget(QtWidgets.QLabel("Handle Type:"))
                self.handle_type_combo = QtWidgets.QComboBox()
                self.handle_type_combo.addItems(["Auto", "Aligned", "Vector", "Free"])
                self.handle_type_combo.currentTextChanged.connect(self._on_handle_type_changed)
                bezier_row.addWidget(self.handle_type_combo)
                bezier_row.addStretch()
                self._lay.addLayout(bezier_row)

                # Add button to toggle handle visibility
                self.toggle_handles_btn = QtWidgets.QPushButton("Show Handles: On")
                self.toggle_handles_btn.setCheckable(True)
                self.toggle_handles_btn.setChecked(True)
                self.toggle_handles_btn.toggled.connect(self._toggle_handle_visibility)
                self._lay.addWidget(self.toggle_handles_btn)

        else:
            self.sym_toggle = None
            self.sym_plane_combo = None

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        self._lay.addWidget(self.live)

        # Curvature visualization controls
        curv_layout = QtWidgets.QHBoxLayout()
        self.show_curvature_checkbox = QtWidgets.QCheckBox("Show curvature")
        self.show_curvature_checkbox.setChecked(False)
        self.show_curvature_checkbox.toggled.connect(self._toggle_show_curvature)
        curv_layout.addWidget(self.show_curvature_checkbox)
        curv_layout.addWidget(QtWidgets.QLabel("Scale:"))
        self.curvature_scale_spinbox = QtWidgets.QDoubleSpinBox()
        self.curvature_scale_spinbox.setRange(0.1, 10.0)
        self.curvature_scale_spinbox.setValue(1.0)
        self.curvature_scale_spinbox.setSingleStep(0.1)
        self.curvature_scale_spinbox.valueChanged.connect(self._on_curvature_scale_changed)
        curv_layout.addWidget(self.curvature_scale_spinbox)
        curv_layout.addStretch()
        self._lay.addLayout(curv_layout)

        # Constraints section
        constraints_layout = QtWidgets.QVBoxLayout()
        constraints_layout.addWidget(QtWidgets.QLabel("Constraints:"))

        # Constraint buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self.distance_btn = QtWidgets.QPushButton("Distance")
        self.distance_btn.clicked.connect(self._add_distance_constraint)
        btn_layout.addWidget(self.distance_btn)

        self.horizontal_btn = QtWidgets.QPushButton("Horizontal")
        self.horizontal_btn.clicked.connect(self._add_horizontal_constraint)
        btn_layout.addWidget(self.horizontal_btn)

        self.vertical_btn = QtWidgets.QPushButton("Vertical")
        self.vertical_btn.clicked.connect(self._add_vertical_constraint)
        btn_layout.addWidget(self.vertical_btn)

        self.angle_btn = QtWidgets.QPushButton("Angle")
        self.angle_btn.clicked.connect(self._add_angle_constraint)
        btn_layout.addWidget(self.angle_btn)

        self.coincide_btn = QtWidgets.QPushButton("Coincide")
        self.coincide_btn.clicked.connect(self._add_coincidence_constraint)
        btn_layout.addWidget(self.coincide_btn)

        self.on_curve_btn = QtWidgets.QPushButton("On Curve")
        self.on_curve_btn.setToolTip(
            "Pin the selected point(s) onto a curve: select the point row(s) "
            "here, then select a target edge in the 3D view, then click.")
        self.on_curve_btn.clicked.connect(self._add_on_curve_constraint)
        btn_layout.addWidget(self.on_curve_btn)

        constraints_layout.addLayout(btn_layout)

        # Constraint list
        self.constraint_list = QtWidgets.QListWidget()
        self.constraint_list.setMaximumHeight(100)
        constraints_layout.addWidget(self.constraint_list)

        # Remove constraint button
        self.remove_constraint_btn = QtWidgets.QPushButton("Remove Selected")
        self.remove_constraint_btn.clicked.connect(self._remove_constraint)
        constraints_layout.addWidget(self.remove_constraint_btn)

        # Constraint status feedback
        self.constraint_status = QtWidgets.QLabel("")
        self.constraint_status.setWordWrap(True)
        self.constraint_status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:2px 0;")
        constraints_layout.addWidget(self.constraint_status)

        self._lay.addLayout(constraints_layout)

        # Viewport grid (XY, YZ, XZ planes)
        self._setup_viewport_grid()

    def _on_handle_type_changed(self, htype):
        """Handle handle type selection change."""
        if self._editor and hasattr(self._editor, "set_bezier_handle_type"):
            rows = self._selected_rows()
            if rows:
                self._editor.set_bezier_handle_type(rows[0], htype)
                self._fill_table()  # Refresh to show any visual changes

    def _toggle_handle_visibility(self, visible):
        """Toggle Bezier handle visualization."""
        if self._editor and hasattr(self._editor, "set_handle_visibility"):
            self._editor.set_handle_visibility(visible)

    # ---- curvature visualization ---
    def _toggle_show_curvature(self, checked):
        """Show or hide curvature comb visualization."""
        if checked:
            self._show_curvature()
        else:
            self._hide_curvature()
        # Trigger recompute to update visualization
        self.recompute()

    def _on_curvature_scale_changed(self, value):
        """Update curvature comb scale."""
        self._curvature_scale = value
        self._show_curvature()  # Update existing visualization

    def _show_curvature(self):
        """Display curvature comb as 3D overlay."""
        self._hide_curvature()  # Clean up any existing visualization

        # Get the curve shape
        shape = self.obj.Shape
        if not shape or not shape.Edges:
            return

        edge = shape.Edges[0]
        if not edge:
            return

        # Compute curvature comb using core function
        from rfstudio import core
        try:
            comb = core.curvature_comb(edge, scale=self._curvature_scale, samples=20)
            if not comb:
                return

            # Create Coin3D visualization similar to highlight/gizmo
            from pivy import coin
            sep = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (1.0, 0.5, 0.0)  # Orange color for curvature
            sep.addChild(col)

            # Extract lines from the compound
            if hasattr(comb, 'Edges'):
                coords = coin.SoCoordinate3()
                lineset = coin.SoLineSet()

                points = []
                num_vertices = []
                point_idx = 0

                for i in range(comb.Edges.getLength()):
                    edge = comb.Edges[i]
                    pts = [edge.getPoint(j) for j in range(2)]  # Line has 2 points
                    points.extend([(p.x, p.y, p.z) for p in pts])
                    num_vertices.append(2)

                coords.point.setValues(0, len(points), points)
                lineset.numVertices.setValues(0, len(num_vertices), num_vertices)

                sep.addChild(coords)
                sep.addChild(lineset)

            # Add to scene graph
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(sep)
                self._curvature_overlay = sep

        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"Railform: Curvature visualization failed: {exc}\n")

    def _hide_curvature(self):
        """Hide curvature comb visualization."""
        if self._curvature_overlay:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    view.getSceneGraph().removeChild(self._curvature_overlay)
            except Exception:
                pass
            self._curvature_overlay = None

    # ---- per-kind scalar property controls (Closed / Degree / Tolerance) --
    def _scalar_controls(self):
        """Auto-generate simple controls for this curve object's own
        scalar properties -- e.g. Closed (Interp/Polyline), Degree
        (Control/Approx), Tolerance (Approx). Mirrors
        panel_surface.SurfacePanel._scalar_controls, scoped to this
        curve's own property group (self.kind) instead of iterating a
        surface's link-list groups. Knots (ControlCurve) is deliberately
        skipped -- it's edited via the in-viewport knot-drag editor
        (editmode.KnotEditor), not a spinbox here.
        """
        obj = self.obj
        for name in obj.PropertiesList:
            if obj.getGroupOfProperty(name) != self.kind or name == "Knots":
                continue
            tid = obj.getTypeIdOfProperty(name)
            if tid == "App::PropertyBool":
                self._add_bool_ctrl(name)
            elif tid in ("App::PropertyInteger", "App::PropertyIntegerConstraint"):
                self._add_int_ctrl(name)
            elif tid == "App::PropertyFloat":
                self._add_float_ctrl(name)

    def _row_ctrl(self, label, widget):
        r = QtWidgets.QHBoxLayout()
        r.addWidget(QtWidgets.QLabel(label))
        r.addStretch(1)
        r.addWidget(widget)
        self._lay.addLayout(r)

    def _add_bool_ctrl(self, name):
        cb = QtWidgets.QCheckBox()
        cb.setChecked(bool(getattr(self.obj, name)))
        cb.toggled.connect(lambda v, n=name: self._set(n, v))
        self._row_ctrl(name, cb)

    def _add_int_ctrl(self, name):
        sb = QtWidgets.QSpinBox()
        sb.setRange(1, 8)
        sb.setValue(int(getattr(self.obj, name)))
        sb.valueChanged.connect(lambda v, n=name: self._set(n, v))
        self._row_ctrl(name, sb)

    def _add_float_ctrl(self, name):
        sb = QtWidgets.QDoubleSpinBox()
        sb.setDecimals(4)
        sb.setRange(-1e6, 1e6)
        sb.setSingleStep(0.1)
        sb.setValue(float(getattr(self.obj, name)))
        sb.valueChanged.connect(lambda v, n=name: self._set(n, v))
        self._row_ctrl(name, sb)

    # ---- symmetric editing (model only half) ------------------------ #
    def _toggle_insert_on_click(self, on):
        """Route the 'insert point on segment click' toggle to the live editor
        (see editmode.PointEditor.insert_on_click)."""
        ed = getattr(self, "_editor", None)
        if ed is not None:
            ed.insert_on_click = bool(on)

    def _toggle_symmetry(self, on):
        """Enable/disable live mirrored dragging on the 3D editor. See
        editmode.PointEditor.set_symmetry -- raises ValueError if the
        current points aren't actually symmetric about the chosen plane,
        which we catch here and report instead of silently proceeding."""
        if self._editor is None:
            return
        if not on:
            try:
                self._editor.set_symmetry(None, None)
            except Exception:
                pass
            return
        from rfstudio import mirror as rmirror
        base, normal = rmirror.plane_base_normal(self.sym_plane_combo.currentText())
        try:
            self._editor.set_symmetry(base, normal)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: can't enable symmetric editing (%s) -- the "
                "current points aren't symmetric about this plane yet. "
                "Place points symmetrically first, or build from a "
                "mirrored pair.\n" % exc)
            self.sym_toggle.setChecked(False)

    def _on_sym_plane_changed(self, _plane_name):
        if self.sym_toggle is not None and self.sym_toggle.isChecked():
            self._toggle_symmetry(True)

    def _mirror_edit_on_type(self, r):
        """After a typed X/Y/Z coordinate edit (not a drag), re-mirror
        row r's partner if symmetric editing is on. Drags are already
        handled live inside editmode.PointEditor; this covers the
        spinbox-typing path, which doesn't go through the 3D editor."""
        if self.sym_toggle is None or not self.sym_toggle.isChecked():
            return
        from rfstudio import mirror as rmirror
        base, normal = rmirror.plane_base_normal(self.sym_plane_combo.currentText())
        pts = self._points()
        try:
            pairs = rmirror.find_symmetric_pairs(pts, base, normal)
        except Exception:
            return
        partner = pairs.get(r, r)
        if partner != r:
            pts[partner] = rmirror.mirror_point(pts[r], base, normal)
            self._set_points(pts)
            self._fill_table()

    # ---- points <-> table ---- #
    def _points(self):
        return list(self.obj.Points)

    def _set_points(self, pts):
        self.obj.Points = pts
        if hasattr(self.obj, "Weights"):
            self.obj.Weights = self._weights
        self.recompute()

    def _fill_table(self):
        pts = self._points()
        # Ensure weights list matches points length
        if len(self._weights) < len(pts):
            self._weights.extend([1.0] * (len(pts) - len(self._weights)))
        elif len(self._weights) > len(pts):
            self._weights = self._weights[:len(pts)]
        self.table.setRowCount(len(pts))
        self._rows = []
        for i, p in enumerate(pts):
            spins = []
            # X, Y, Z
            for j, val in enumerate((p.x, p.y, p.z)):
                sp = _CoordSpin(lambda r=i: self._focus_row(r))
                sp.setValue(val)
                sp.setMaximumWidth(92)
                sp.setMinimumWidth(40)
                sp.setAlignment(QtCore.Qt.AlignRight)
                sp.valueChanged.connect(lambda v, r=i, c=j: self._on_spin_changed(r, c))
                self.table.setCellWidget(i, j, sp)
                spins.append(sp)
            # Weight
            if self.kind == "ControlCurve":
                w_spin = _WeightSpin(lambda r=i: self._focus_row(r))
                w_spin.setValue(self._weights[i] if i < len(self._weights) else 1.0)
                w_spin.setMaximumWidth(92)
                w_spin.setMinimumWidth(40)
                w_spin.setAlignment(QtCore.Qt.AlignRight)
                w_spin.valueChanged.connect(lambda v, r=i, c=3: self._on_spin_changed(r, c))
                self.table.setCellWidget(i, 3, w_spin)
                spins.append(w_spin)
            else:
                w_label = QtWidgets.QLabel(str(self._weights[i] if i < len(self._weights) else 1.0))
                w_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                w_label.setStyleSheet("color: #aaa;")
                self.table.setCellWidget(i, 3, w_label)
                spins.append(w_label)
            # Delete button
            del_btn = QtWidgets.QPushButton("×")
            del_btn.setFixedWidth(22)
            del_btn.setStyleSheet(
                "QPushButton{background:#7a1c1c;color:#fff;border:none;"
                "border-radius:2px;font-weight:bold;padding:0;}"
                "QPushButton:hover{background:#c0392b;}")
            del_btn.clicked.connect(lambda checked=False, r=i: self._delete_row(r))
            self.table.setCellWidget(i, 4, del_btn)
            self._rows.append(tuple(spins))
        self._hide_gizmo()
        # Point count/order may have changed -- re-derive the symmetric
        # pairing if the toggle is already on, so a subsequent drag maps
        # to the right partner.
        if getattr(self, "sym_toggle", None) is not None and self.sym_toggle.isChecked():
            self._toggle_symmetry(True)

    def _hide_gizmo(self):
        self._drag_row = None
        if getattr(self, "_gizmo", None):
            self._gizmo.hide()
        if getattr(self, "_hl", None):
            self._highlight_rows([])

    # ---- one central update path (avoids spin<->point<->gizmo loops) ---- #
    def _update_point(self, r, vec, source):
        pts = self._points()
        if not (0 <= r < len(pts)):
            return
        pts[r] = vec
        self.obj.Points = pts
        # never force updateGui() inside a Coin drag callback (re-entrant crash)
        self.recompute(gui=(source != "drag"))
        self._sync = True
        try:
            if source != "drag" and r < len(self._rows):
                sx, sy, sz, _ = self._rows[r]
                sx.setValue(vec.x)
                sy.setValue(vec.y)
                sz.setValue(vec.z)
            if (source != "drag" and self._gizmo and self._gizmo.ok
                    and self._drag_row == r):
                self._gizmo.show(vec, self._gizmo_scale())
        finally:
            self._sync = False
        self._highlight_rows([r])

    def _on_spin_changed(self, r, c):
        if self._sync or r >= len(self._rows):
            return
        from FreeCAD import Vector
        spins = self._rows[r]
        if c < 3:  # X, Y, Z
            sx, sy, sz = spins[0], spins[1], spins[2]
            self._update_point(r, Vector(sx.value(), sy.value(), sz.value()), "spin")
            self._mirror_edit_on_type(r)
        else:  # W (weight)
            if self.kind == "ControlCurve":
                if r < len(self._weights):
                    self._weights[r] = spins[3].value()
                # Update the parametric object's Weights property if it exists
                if hasattr(self.obj, "Weights"):
                    self.obj.Weights = self._weights
                self.recompute(gui=False)  # Weights change requires recompute but no GUI update from spin
        if self._editor:
            try:
                self._editor._refresh_markers()
                self._editor._refresh_arrows()
            except Exception:
                pass

    def _focus_row(self, r):
        self._active_row = r
        # Highlight the row WITHOUT stealing keyboard focus from the cell's
        # spinbox. table.selectRow(r) also moves the current cell to column 0,
        # which yanked focus out of the Y/Z editors the instant they were
        # clicked -- so only X could be typed into. Selecting through the
        # selection model (no setCurrentIndex) keeps the visual row highlight
        # but leaves the focused editor alone.
        try:
            sm = self.table.selectionModel()
            model = self.table.model()
            sel = QtCore.QItemSelection(
                model.index(r, 0),
                model.index(r, self.table.columnCount() - 1))
            sm.select(
                sel,
                QtCore.QItemSelectionModel.ClearAndSelect
                | QtCore.QItemSelectionModel.Rows)
        except Exception:
            self.table.selectRow(r)          # fallback: old behaviour
        self._show_gizmo(r)
        self._highlight_rows([r])
        if self._editor:
            try:
                self._editor.sel = r
                self._editor._refresh_arrows()
            except Exception:
                pass

    def _selected_rows(self):
        rows = sorted({i.row()
                       for i in self.table.selectionModel().selectedRows()})
        if not rows and self._active_row is not None:
            rows = [self._active_row]
        return rows

    # ---- 3D point highlight + RGB axis gizmo (Coin overlay) ---- #
    def _setup_highlight(self):
        self._hl = None
        self._gizmo = None       # in-panel gizmo removed -> point editing via
                                 # ViewProvider edit mode (rfstudio.editmode)
        try:
            from pivy import coin
            sep = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (1.0, 0.85, 0.15)
            self._hl_coord = coin.SoCoordinate3()
            self._hl_markers = coin.SoMarkerSet()
            try:
                self._hl_markers.markerIndex = coin.SoMarkerSet.CIRCLE_FILLED_9_9
            except Exception:
                pass
            self._hl_markers.numPoints = 0
            sep.addChild(col)
            sep.addChild(self._hl_coord)
            sep.addChild(self._hl_markers)

            # live drag preview line (our own node -> safe to update mid-drag)
            self._prev_switch = coin.SoSwitch()
            self._prev_switch.whichChild = -1
            pv = coin.SoSeparator()
            pcol = coin.SoBaseColor()
            pcol.rgb = (0.2, 0.85, 1.0)
            style = coin.SoDrawStyle()
            style.lineWidth = 2
            self._prev_coord = coin.SoCoordinate3()
            self._prev_line = coin.SoLineSet()
            pv.addChild(pcol)
            pv.addChild(style)
            pv.addChild(self._prev_coord)
            pv.addChild(self._prev_line)
            self._prev_switch.addChild(pv)
            sep.addChild(self._prev_switch)

            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(sep)
                self._hl = sep
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: 3D highlight/gizmo unavailable (%s)\n" % exc)
            _notify_overlay_unavailable(self, "3D point highlight/gizmo", exc)

    def _gizmo_scale(self):
        """Handle size ~6% of the point-cloud diagonal, so it scales sensibly."""
        import math
        pts = self._points()
        if len(pts) < 2:
            return 3.0
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        zs = [p.z for p in pts]
        diag = math.sqrt((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
                         + (max(zs) - min(zs)) ** 2)
        return max(diag * 0.06, 1.0)

    def _show_gizmo(self, r):
        if not (self._gizmo and self._gizmo.ok):
            return
        pts = self._points()
        if not (0 <= r < len(pts)):
            self._gizmo.hide()
            return
        self._drag_row = r
        self._gizmo.show(pts[r], self._gizmo_scale())

    def _on_gizmo(self, vec):
        # Called from inside the Coin drag callback -- DEFER the actual update
        # (recompute rebuilds scene nodes; doing it here = access violation).
        if self._drag_row is None:
            return
        self._pending = vec
        if not self._pending_scheduled:
            self._pending_scheduled = True
            QtCore.QTimer.singleShot(0, self._apply_pending)

    def _apply_pending(self):
        # DURING the drag: live-update the spinboxes ONLY. Touching the document
        # object (recompute -> scene rebuild) while the dragger grab is active
        # is an access violation, even when deferred. The curve is committed on
        # release (_commit_drag).
        self._pending_scheduled = False
        vec, self._pending = self._pending, None
        if vec is None or self._drag_row is None:
            return

        # Apply constraints during drag
        constrained_vec = self._apply_constraints_during_drag(self._drag_row, vec)

        self._drag_value = constrained_vec
        r = self._drag_row
        if r >= len(self._rows):
            return
        self._sync = True
        try:
            sx, sy, sz = self._rows[r]
            sx.setValue(constrained_vec.x)
            sy.setValue(constrained_vec.y)
            sz.setValue(constrained_vec.z)
        finally:
            self._sync = False
        self._preview(r, constrained_vec)

    def _preview(self, r, vec):
        """Live curve preview computed from the dragged points -- pure geometry
        into our own Coin line, so it's safe to update during the drag (no
        document recompute / scene rebuild)."""
        if not getattr(self, "_prev_switch", None):
            return
        try:
            pts = list(self._points())
            if not (0 <= r < len(pts)):
                return
            pts[r] = vec
            shape = self.obj.Proxy.compute(self.obj, pts)
            line = shape.discretize(60) if shape is not None else None
        except Exception:
            return
        if not line:
            self._prev_switch.whichChild = -1
            return
        self._prev_coord.point.setValues(
            0, len(line), [(p.x, p.y, p.z) for p in line])
        self._prev_line.numVertices.setValue(len(line))
        self._prev_switch.whichChild = 0

    def _on_gizmo_finish(self):
        # drag released -> safe to rebuild the curve now (deferred off the cb)
        QtCore.QTimer.singleShot(0, self._commit_drag)

    def _commit_drag(self):
        if getattr(self, "_prev_switch", None):
            self._prev_switch.whichChild = -1     # hide preview, show real curve
        r = self._drag_row
        vec, self._drag_value = self._drag_value, None
        if r is not None and vec is not None:
            # Apply constraints to get final positions
            points = self._points()
            points[r] = vec  # Set the dragged point to the final position
            final_points = self._apply_constraints(points)
            self._update_point(r, final_points[r], "commit")   # recompute + recentre gizmo
        else:
            self._recenter_gizmo()

    def _recenter_gizmo(self):
        if self._drag_row is None:
            return
        pts = self._points()
        if 0 <= self._drag_row < len(pts) and self._gizmo and self._gizmo.ok:
            self._gizmo.show(pts[self._drag_row], self._gizmo_scale())

    def _highlight_rows(self, rows):
        if not getattr(self, "_hl", None):
            return
        pts = self._points()
        sel = [pts[i] for i in rows if 0 <= i < len(pts)]
        self._hl_coord.point.setValues(
            0, len(sel), [(p.x, p.y, p.z) for p in sel])
        self._hl_markers.numPoints = len(sel)

    def _teardown_highlight(self):
        if getattr(self, "_gizmo", None):
            self._gizmo.detach()
            self._gizmo = None
        if getattr(self, "_hl", None):
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    view.getSceneGraph().removeChild(self._hl)
            except Exception:
                pass
            self._hl = None

    def _extra_teardown(self):
        # Teardown the in-viewport editor BEFORE the highlight overlay (the
        # panel owns it now; CurveVP.setEdit no longer pre-constructs one).
        ed = getattr(self, "_editor", None)
        if ed is not None:
            try:
                ed.finish()
            except Exception:
                pass
            self._editor = None
        self._teardown_highlight()
        self._remove_viewport_grid()
        self._hide_curvature()

    def _setup_viewport_grid(self):
        """Set up a grid in the XY, YZ, and XZ planes for viewport reference."""
        try:
            from pivy import coin
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            # Create a root separator for the grid
            self._viewport_grid_root = coin.SoSeparator()
            # A single, sparse XY ground grid. The old version drew THREE full
            # planes (XY/YZ/XZ) at 0.25 spacing over 500 units -- ~2000 lines
            # per axis on each plane. Zoomed out those lines merged into solid
            # gray sheets that obscured the model from every angle. Now: one
            # ground plane, two tiers (minor + major), far fewer lines.
            size = 200.0        # total extent (units)
            minor = 5.0         # fine grid spacing
            major = 25.0        # heavier reference lines
            half = size / 2.0

            def _grid_plane(step, rgb):
                """One set of XY grid lines at `step` spacing, colour `rgb`."""
                sep = coin.SoSeparator()
                col = coin.SoBaseColor()
                col.rgb = rgb
                sep.addChild(col)
                coords = coin.SoCoordinate3()
                lines = coin.SoLineSet()
                verts, nverts = [], []
                n = int(round(size / step))
                for i in range(n + 1):
                    c = -half + i * step
                    verts.extend([(-half, c, 0.0), (half, c, 0.0)])   # along X
                    nverts.append(2)
                    verts.extend([(c, -half, 0.0), (c, half, 0.0)])   # along Y
                    nverts.append(2)
                coords.point.setValues(0, len(verts), verts)
                lines.numVertices.setValues(0, len(nverts), nverts)
                sep.addChild(coords)
                sep.addChild(lines)
                return sep

            # minor faint, major slightly brighter so scale still reads
            self._viewport_grid_root.addChild(_grid_plane(minor, (0.28, 0.28, 0.28)))
            self._viewport_grid_root.addChild(_grid_plane(major, (0.45, 0.45, 0.45)))
            sg.addChild(self._viewport_grid_root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: Viewport grid unavailable (%s)\n" % exc)
            self._viewport_grid_root = None

    def _promote_to_mfp(self):
        """Promote the current symmetric curve into an explicit MFP pair."""
        if self.sym_toggle is None or not self.sym_toggle.isChecked():
            return

        try:
            from rfstudio import mirror as rmirror
            parametric = __import__('rfstudio.parametric', fromlist=['make_surface'])

            # Get current points
            points = self._points()
            if len(points) < 2:
                return

            # Get the symmetry plane
            base, normal = rmirror.plane_base_normal(self.sym_plane_combo.currentText())

            # Verify the points are actually symmetric
            try:
                pairs = rmirror.find_symmetric_pairs(points, base, normal)
            except Exception as exc:
                QtGui.QMessageBox.warning(
                    self.form, "Promote to MFP",
                    "Cannot promote to MFP: the current points are not symmetric "
                    "about the selected plane. %s" % str(exc))
                return

            # Create a new MFP object
            doc = self.doc
            mfp_obj = parametric.make_surface("Mirror", doc)
            mfp_obj.Label = self.obj.Label + "_MFP"

            # Set the source to the current object
            mfp_obj.Source = self.obj

            # Set the plane based on the symmetry plane
            plane_map = {"XY": 0, "YZ": 1, "ZX": 2}
            plane_name = self.sym_plane_combo.currentText()
            mfp_obj.Plane = plane_name

            # For now, we'll keep the default base and normal
            # In a more sophisticated version, we could calculate these from the points

            # Enable weld seam to match the behavior of symmetric editing
            mfp_obj.WeldSeam = True

            # Recompute the MFP
            mfp_obj.recompute()

            # Hide the original curve
            self.obj.ViewObject.hide()

            # Select the new MFP object
            Gui = FreeCADGui
            Gui.Selection.clearSelection()
            Gui.Selection.addSelection(mfp_obj)

            # Show a message
            QtGui.QMessageBox.information(
                self.form, "Promote to MFP",
                "Successfully promoted to MFP. The original curve is now the source "
                "of a MirrorFP object with weld enabled.")

        except Exception as exc:
            QtGui.QMessageBox.warning(
                self.form, "Promote to MFP",
                "Failed to promote to MFP: %s" % str(exc))
            import traceback
            traceback.print_exc()

    def _remove_viewport_grid(self):
        """Remove the viewport grid from the scene."""
        if hasattr(self, '_viewport_grid_root') and self._viewport_grid_root is not None:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view is not None:
                    sg = view.getSceneGraph()
                    sg.removeChild(self._viewport_grid_root)
            except Exception:
                pass
            self._viewport_grid_root = None

    def _add_sel(self):
        pts = self._points()
        new_pts = _selected_points()
        pts.extend(new_pts)
        self._weights.extend([1.0] * len(new_pts))
        self._set_points(pts)
        if self._editor:
            self._editor.update_symmetry()
        self._fill_table()

    def _append_point(self, v):
        pts = self._points()
        pts.append(v)
        self._weights.append(1.0)
        self._set_points(pts)
        if self._editor:
            self._editor.update_symmetry()
        self._fill_table()

    def _remove(self):
        pts = self._points()
        rows = sorted(self._selected_rows(), reverse=True)
        # Remove points and weights at the selected indices (in reverse order)
        for i in rows:
            if 0 <= i < len(pts):
                del pts[i]
                if 0 <= i < len(self._weights):
                    del self._weights[i]
        self._active_row = None
        if hasattr(self.obj, "Weights"):
            self.obj.Weights = self._weights
        self._set_points(pts)
        self._fill_table()

    def _delete_row(self, r):
        """Per-row X delete button (see _fill_table) -- deletes just row
        `r`, independent of table selection."""
        pts = self._points()
        if 0 <= r < len(pts):
            del pts[r]
            if 0 <= r < len(self._weights):
                del self._weights[r]
        if self._active_row == r:
            self._active_row = None
        elif self._active_row is not None and self._active_row > r:
            self._active_row -= 1
        if hasattr(self.obj, "Weights"):
            self.obj.Weights = self._weights
        self._set_points(pts)
        self._fill_table()

    def _move(self, delta):
        rows = self._selected_rows()
        pts = self._points()
        if not rows:
            return
        order = rows if delta < 0 else list(reversed(rows))
        moved = []
        for i in order:
            j = i + delta
            if 0 <= j < len(pts):
                # Swap points
                pts[i], pts[j] = pts[j], pts[i]
                # Swap weights
                self._weights[i], self._weights[j] = self._weights[j], self._weights[i]
                moved.append(j)             # point i now lives at j
            else:
                moved.append(i)             # blocked at the boundary -> stays
        if hasattr(self.obj, "Weights"):
            self.obj.Weights = self._weights
        self._set_points(pts)
        self._fill_table()
        # Keep the moved point(s) selected at their NEW row so repeated up/down
        # presses carry the same point. Previously _fill_table() rebuilt the
        # table and dropped the selection, so a point just bounced between two
        # rows (re-selecting the row grabbed whatever swapped into it).
        self._reselect_rows(moved)

    def _reselect_rows(self, rows):
        """Select `rows` (new indices) after a table rebuild, without stealing
        keyboard focus (see _focus_row), and re-sync the 3D highlight/gizmo."""
        rows = [r for r in rows if 0 <= r < self.table.rowCount()]
        if not rows:
            return
        try:
            sm = self.table.selectionModel()
            model = self.table.model()
            last = self.table.columnCount() - 1
            sm.clearSelection()
            for r in rows:
                sel = QtCore.QItemSelection(model.index(r, 0),
                                            model.index(r, last))
                sm.select(sel, QtCore.QItemSelectionModel.Select
                          | QtCore.QItemSelectionModel.Rows)
        except Exception:
            for r in rows:
                self.table.selectRow(r)
        self._active_row = rows[-1]
        self._show_gizmo(rows[-1])
        self._highlight_rows(rows)
        if self._editor:
            try:
                self._editor.sel = rows[-1]
                self._editor._refresh_arrows()
            except Exception:
                pass

    def _up(self):
        self._move(-1)

    def _down(self):
        self._move(1)

    # ---- 3D picking (best-effort) ---- #
    def _toggle_pick(self, on):
        self.pick_btn.setText("Pick in 3D: on" if on else "Pick in 3D: off")
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                raise Exception("No active document")
            view = doc.ActiveView
            if view is None:
                raise Exception("No active view")
            if on:
                self._cbid = view.addEventCallback(
                    "SoMouseButtonEvent", self._on_click)
                self._cbid_mov = view.addEventCallback(
                    "SoLocation2Event", self._on_move_pick)
                self._snap_cands = self._collect_snap_cands()
                self._snap_shapes = self._collect_snap_shapes()
                self._build_snap_overlay(view)
            else:
                if self._cbid is not None:
                    view.removeEventCallback("SoMouseButtonEvent", self._cbid)
                    self._cbid = None
                if self._cbid_mov is not None:
                    view.removeEventCallback("SoLocation2Event", self._cbid_mov)
                    self._cbid_mov = None
                self._remove_snap_overlay(view)
                self._snap_pt = None
                self._snap_cands = []
                self._snap_shapes = []
        except Exception as exc:
            FreeCAD.Console.PrintWarning("Railform: 3D pick unavailable (%s)\n"
                                         % exc)

    def _toggle_draw_mode(self, on):
        """Enable/disable draw mode for continuous point placement."""
        self.draw_btn.setText("Draw Curve: on" if on else "Draw Curve: off")
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                raise Exception("No active document")
            view = doc.ActiveView
            if view is None:
                raise Exception("No active view")
            if on:
                # If pick mode is on, turn it off since these modes are exclusive
                if getattr(self, '_cbid', None) is not None:
                    self._toggle_pick(False)

                # Set up draw mode callbacks
                self._draw_cb_id = view.addEventCallback(
                    "SoMouseButtonEvent", self._on_draw_click)
                self._draw_cb_id_mov = view.addEventCallback(
                    "SoLocation2Event", self._on_draw_move)
                self._snap_cands = self._collect_snap_cands()
                self._snap_shapes = self._collect_snap_shapes()
                self._build_snap_overlay(view)

                # Initialize drawing state
                self._drawing = True
                self._draw_points = []  # Temporary points for drawing
            else:
                # Clean up draw mode callbacks
                if getattr(self, '_draw_cb_id', None) is not None:
                    view.removeEventCallback("SoMouseButtonEvent", self._draw_cb_id)
                    self._draw_cb_id = None
                if getattr(self, '_draw_cb_id_mov', None) is not None:
                    view.removeEventCallback("SoLocation2Event", self._draw_cb_id_mov)
                    self._draw_cb_id_mov = None
                self._remove_snap_overlay(view)
                self._snap_cands = []
                self._snap_shapes = []

                # Finalize drawing
                self._drawing = False
        except Exception as exc:
            FreeCAD.Console.PrintWarning("Railform: draw mode unavailable (%s)\n"
                                         % exc)

    def _collect_snap_cands(self):
        """EXACT high-priority discrete snap targets from all other objects
        -- vertices, control points, knots, and true curve-curve
        intersections (snaputil.collect_strong_snap_points), samples off.
        These win over an on-edge projection in the ranked snap. Shared with
        editmode.PointEditor."""
        return snaputil.collect_strong_snap_points(self.obj.Document,
                                                   exclude=self.obj)

    def _collect_snap_shapes(self):
        """Edges from all other objects, for EXACT perpendicular-
        projection snapping (see snaputil.nearest_point_on_shapes) -- so
        a picked point can land on any arbitrary point along another
        curve's real geometry, not just its vertices/samples/knots."""
        return [e for e, _o in
                snaputil.collect_snap_shapes(self.obj.Document,
                                             exclude=self.obj)]

    def _on_draw_click(self, ev):
        """Handle mouse clicks in draw mode - add points continuously."""
        if ev.get("Type") != "SoMouseButtonEvent" or ev.get("State") != "DOWN":
            return
        if ev.get("Button") != "BUTTON1":
            return
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            pos = ev["Position"]
            # use snapped point if one is active, else project onto view plane
            pt = self._snap_pt if self._snap_pt is not None \
                else view.getPoint(pos[0], pos[1])
            if pt is not None:
                # Initialize drawing point list if not already done
                if not hasattr(self, '_draw_points'):
                    self._draw_points = []
                    # Start a transaction for the entire drawing operation
                    self.doc.commitTransaction()
                    self._pick_count += 1
                    self.doc.openTransaction("Draw %s" % self.kind)
                # Add the point to our drawing list
                self._draw_points.append(pt)
                # Update the snap candidates to include our new points
                self._snap_cands = self._collect_snap_cands()
                self._snap_shapes = self._collect_snap_shapes()
                # Update snap overlay
                if self._snap_coin_root is not None:
                    self._snap_coord_draw.point.set1Value(
                        0, pt.x, pt.y, pt.z)
                    self._snap_sw.whichChild = 0
        except Exception:
            pass

    def _on_draw_move(self, ev):
        """Track cursor during draw mode; find and display nearest snap point."""
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            pos = ev["Position"]
            snap_px = snaputil._snap_tolerance_mm(view, 4.0)
            best = snaputil.nearest_snap_ranked(
                view, self._snap_cands, getattr(self, "_snap_shapes", []),
                pos, snap_px)
            self._snap_pt = best
            if self._snap_coin_root is not None:
                if best is not None:
                    self._snap_coord_draw.point.set1Value(
                        0, best.x, best.y, best.z)
                    self._snap_sw.whichChild = 0
                else:
                    self._snap_sw.whichChild = -1
        except Exception:
            pass

    def _build_snap_overlay(self, view):
        """Add a cyan Coin3D snap-indicator marker to the scene."""
        self._remove_snap_overlay(view)
        try:
            from pivy import coin
            root = coin.SoSeparator()
            self._snap_sw = coin.SoSwitch()
            self._snap_sw.whichChild = -1
            sep = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (0.1, 0.95, 0.95)
            self._snap_coord_draw = coin.SoCoordinate3()
            mk = coin.SoMarkerSet()
            try:
                mk.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
            except Exception:
                pass
            mk.numPoints = 1
            sep.addChild(col)
            sep.addChild(self._snap_coord_draw)
            sep.addChild(mk)
            self._snap_sw.addChild(sep)
            root.addChild(self._snap_sw)
            view.getSceneGraph().addChild(root)
            self._snap_coin_root = root
        except Exception:
            self._snap_coin_root = None

    def _remove_snap_overlay(self, view=None):
        if self._snap_coin_root is None:
            return
        try:
            if view is None:
                doc = FreeCADGui.ActiveDocument
                if doc is None:
                    return
                view = doc.ActiveView
                if view is None:
                    return
            view.getSceneGraph().removeChild(self._snap_coin_root)
        except Exception:
            pass
        self._snap_coin_root = None

    def _on_move_pick(self, ev):
        """Track cursor during pick-mode; find and display nearest snap point."""
        if not self._picking and not getattr(self, 'pick_btn', None):
            return
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            pos = ev["Position"]
            snap_px = snaputil._snap_tolerance_mm(view, 4.0)
            best = snaputil.nearest_snap_ranked(
                view, self._snap_cands, getattr(self, "_snap_shapes", []),
                pos, snap_px)
            self._snap_pt = best
            if self._snap_coin_root is not None:
                if best is not None:
                    self._snap_coord_draw.point.set1Value(
                        0, best.x, best.y, best.z)
                    self._snap_sw.whichChild = 0
                else:
                    self._snap_sw.whichChild = -1
        except Exception:
            pass

    def _on_click(self, ev):
        if ev.get("Type") != "SoMouseButtonEvent" or ev.get("State") != "DOWN":
            return
        if ev.get("Button") != "BUTTON1":
            return
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            pos = ev["Position"]
            # use snapped point if one is active, else project onto view plane
            pt = self._snap_pt if self._snap_pt is not None \
                else view.getPoint(pos[0], pos[1])
            # commit current transaction and open a fresh one so each
            # clicked point is individually Ctrl+Z-able
            self.doc.commitTransaction()
            self._pick_count += 1
            self.doc.openTransaction("Draw %s" % self.kind)
            self._append_point(pt)
            # refresh snap candidates to include the curve we just extended
            self._snap_cands = self._collect_snap_cands()
            self._snap_shapes = self._collect_snap_shapes()
        except Exception:
            pass

    # ---- settings + recompute ---- #
    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self, gui=True):
        if getattr(self, "live", None) is None or self.live.isChecked():
            self.obj.recompute()
            if gui:
                FreeCADGui.updateGui()

    # ---- commit/abort hooks (see _EditPanelBase) ---- #
    def _pre_accept(self):
        # If we're in draw mode, handle drawing completion instead of pick mode
        if getattr(self, '_drawing', False):
            # Finalize the drawing by creating the curve from collected points
            if hasattr(self, '_draw_points') and len(self._draw_points) >= 2:
                # Apply the collected points to the curve object
                # For draw mode, we use default weights (all 1.0) since weights
                # can't be specified during drawing - they can be edited afterward
                pts = self._draw_points
                self._set_points(pts)

                # For ControlCurve, reset weights to defaults (1.0) since we
                # don't capture weights during drawing
                if self.kind == "ControlCurve":
                    self._weights = [1.0] * len(pts)
                    self._set_weights(self._weights)

                # Update the editor and table to reflect the new points
                if self._editor is not None:
                    self._editor.setup()
                self._fill_table()

                # Clean up drawing state
                delattr(self, '_draw_points')

            # Exit draw mode
            self._toggle_draw_mode(False)
        else:
            # Normal pick mode behavior
            self._toggle_pick(False)

    def _pre_reject(self):
        # If we're in draw mode, handle drawing cancellation instead of pick mode
        if getattr(self, '_drawing', False):
            # Clear the drawing points (cancel the drawing)
            if hasattr(self, '_draw_points'):
                delattr(self, '_draw_points')

            # Exit draw mode
            self._toggle_draw_mode(False)
        else:
            # Normal pick mode behavior
            self._toggle_pick(False)

    def _post_abort(self):
        if self.creating:
            # undo the per-pick committed transactions so Cancel removes everything
            for _ in range(self._pick_count):
                try:
                    self.doc.undo()
                except Exception:
                    break

    def _toggle_knot_editor(self, on):
        from rfstudio import editmode
        vobj = self.obj.ViewObject
        if on:
            if isinstance(self._editor, editmode.KnotEditor):
                return
            if self._editor is not None:
                try:
                    self._editor.finish()
                except Exception:
                    pass
            # KnotEditor needs at least one recompute so obj.Shape + obj.Knots
            # are populated; the proxy seeds Knots on first execute().
            if not getattr(self.obj, "Knots", None):
                try:
                    self.obj.recompute()
                except Exception:
                    pass
            self._editor = editmode.KnotEditor(vobj)
            self._editor.on_commit = self._on_knot_commit
        else:
            if isinstance(self._editor, editmode.PointEditor):
                return
            if self._editor is not None:
                try:
                    self._editor.finish()
                except Exception:
                    pass
            self._editor = editmode.PointEditor(vobj)
            if self._editor.ok:
                self._editor.on_commit = self._fill_table

    def _on_knot_commit(self):
        # knots updated -> refresh the points table to reflect the new shape
        # (the points themselves don't move, but Degree-change via panel isn't
        # possible here; this hook exists for symmetry with PointEditor.)
        pass

    def _sync_knot_highlight(self):
        from rfstudio import editmode
        ed = getattr(self, "_editor", None)
        if not isinstance(ed, editmode.KnotEditor):
            return
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        ed.sel = rows[0].row()
        ed._update_highlight()

    # ---- curve type conversion ---- #
    def _sample_curve(self, num_points=50):
        """Sample the current curve to generate points for conversion."""
        shape = self.obj.Shape
        if not shape or not shape.Edges:
            return self._points()  # fallback to current points if no shape
        edge = shape.Edges[0]
        if not edge:
            return self._points()
        # Sample points along the curve
        params = [i / (num_points - 1.0) for i in range(num_points)]
        pts = []
        for p in params:
            try:
                pt = edge.valueAt(edge.FirstParameter + p * (edge.LastParameter - edge.FirstParameter))
                pts.append(pt)
            except Exception as exc:
                # If sampling fails at some point, use what we have -- log
                # instead of silently swallowing, so a partial result is
                # traceable rather than looking identical to full success.
                FreeCAD.Console.PrintWarning(
                    "Railform: curve sampling stopped early (%s)\n" % exc)
                break
        return pts if pts else self._points()

    def _on_convert_clicked(self):
        """Handle convert button click: create new object of selected type."""
        target_type = self.convert_type_combo.currentText()
        if target_type == self.kind:
            return  # no change needed

        # Sample current curve to get points
        pts = self._sample_curve()
        if len(pts) < 2:
            FreeCAD.Console.PrintError("Railform: Need at least 2 points to convert curve\n")
            return

        # Create new object of target type
        doc = self.obj.Document
        new_obj = parametric.make_curve(target_type, doc)
        new_obj.Label = self.obj.Label + " converted"
        new_obj.Points = pts

        # Set type-specific properties with sensible defaults
        if target_type == "ControlCurve":
            # Ensure degree is valid for number of points
            deg = min(3, len(pts) - 1)  # prefer cubic but not exceeding points-1
            deg = max(1, deg)           # at least linear
            new_obj.Degree = deg
            # Knots will be auto-generated in compute()
        elif target_type == "ApproxCurve":
            new_obj.Degree = 3
            new_obj.Tolerance = 0.1
        elif target_type == "Polyline":
            new_obj.Closed = False  # could be enhanced to detect closure
        # InterpCurve needs no extra properties beyond Points

        # Recompute new object
        new_obj.recompute()

        # Select new object and clear old selection
        Gui = FreeCADGui
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(new_obj)

        # Optional: show info message
        FreeCAD.Console.PrintMessage(
            "Railform: Converted {} to {}\n".format(self.obj.Label, new_obj.Label))

    # ---- constraint system ---
    def _add_distance_constraint(self):
        """Add distance constraint between two selected points."""
        rows = self._selected_rows()
        if len(rows) != 2:
            # If not exactly 2 rows selected, use first two points
            points = self._points()
            if len(points) < 2:
                return
            rows = [0, 1]

        # Store constraint: ('distance', index1, index2, distance)
        p1 = self._points()[rows[0]]
        p2 = self._points()[rows[1]]
        distance = p1.distanceToPoint(p2)
        self.constraints.append(('distance', rows[0], rows[1], distance))
        self._update_constraint_list()

    def _add_horizontal_constraint(self):
        """Add horizontal constraint (same Y coordinate) to selected point(s)."""
        rows = self._selected_rows()
        if not rows:
            return

        # For each selected point, add constraint to maintain current Y
        for row in rows:
            point = self._points()[row]
            self.constraints.append(('horizontal', row, point.y))
        self._update_constraint_list()

    def _add_vertical_constraint(self):
        """Add vertical constraint (same X coordinate) to selected point(s)."""
        rows = self._selected_rows()
        if not rows:
            return

        # For each selected point, add constraint to maintain current X
        for row in rows:
            point = self._points()[row]
            self.constraints.append(('vertical', row, point.x))
        self._update_constraint_list()

    def _add_angle_constraint(self):
        """Add angle constraint between three points."""
        rows = self._selected_rows()
        if len(rows) < 3:
            # If not exactly 3 rows selected, use first three points
            points = self._points()
            if len(points) < 3:
                return
            rows = [0, 1, 2]

        # Store constraint: ('angle', point1_index, vertex_index, point3_index, angle_in_degrees)
        # The angle is defined at the middle point (vertex) between the other two points
        p1 = self._points()[rows[0]]
        vertex = self._points()[rows[1]]
        p3 = self._points()[rows[2]]

        # Calculate the angle between vectors (vertex->p1) and (vertex->p3)
        v1 = p1 - vertex
        v2 = p3 - vertex
        angle_rad = v1.getAngle(v2)
        angle_deg = math.degrees(angle_rad)

        self.constraints.append(('angle', rows[0], rows[1], rows[2], angle_deg))
        self._update_constraint_list()

    def _add_coincidence_constraint(self):
        """Add coincidence constraint between two points."""
        rows = self._selected_rows()
        if len(rows) < 2:
            # If not exactly 2 rows selected, use first two points
            points = self._points()
            if len(points) < 2:
                return
            rows = [0, 1]

        # Store constraint: ('coincide', index1, index2)
        self.constraints.append(('coincide', rows[0], rows[1]))
        self._update_constraint_list()

    def _target_edge_from_selection(self):
        """The first Part edge currently selected in the 3D view that is
        NOT this curve, or None. Prefers an explicitly sub-selected edge
        (Object.Edge3); accepts a whole object only if its shape has
        exactly one edge (unambiguous)."""
        import Part
        try:
            sels = FreeCADGui.Selection.getSelectionEx(self.obj.Document.Name)
        except Exception:
            sels = []
        for s in sels:
            if getattr(s, "Object", None) is self.obj:
                continue
            for sub in (getattr(s, "SubObjects", None) or ()):
                if isinstance(sub, Part.Edge):
                    return sub
        for s in sels:
            if getattr(s, "Object", None) is self.obj:
                continue
            shp = getattr(getattr(s, "Object", None), "Shape", None)
            edges = getattr(shp, "Edges", None) if shp else None
            if edges and len(edges) == 1:
                return edges[0]
        return None

    def _add_on_curve_constraint(self):
        """Pin the selected point row(s) onto a target edge selected in the
        3D view (Feature: point-on-curve constraint). Stores
        ('on_curve', point_index, edge) per selected row and re-solves."""
        rows = self._selected_rows()
        if not rows:
            FreeCAD.Console.PrintWarning(
                "Railform: select the point row(s) to pin, then a target "
                "edge in the 3D view, then click On Curve.\n")
            return
        edge = self._target_edge_from_selection()
        if edge is None:
            FreeCAD.Console.PrintWarning(
                "Railform: select a target edge in the 3D view (a single "
                "curve/edge, not this curve) before clicking On Curve.\n")
            return
        for row in rows:
            self.constraints.append(('on_curve', row, edge))
        self._update_constraint_list()
        # apply immediately so the point visibly lands on the curve
        self._set_points(self._apply_constraints(self._points()))

    def _remove_constraint(self):
        """Remove selected constraint from the list."""
        current_row = self.constraint_list.currentRow()
        if current_row >= 0:
            self.constraints.pop(current_row)
            self._update_constraint_list()

    def _update_constraint_list(self):
        """Update the constraint list widget."""
        self.constraint_list.clear()
        for constraint in self.constraints:
            if constraint[0] == 'distance':
                text = f"Distance: P{constraint[1]} <-> P{constraint[2]} = {constraint[3]:.3f}"
            elif constraint[0] == 'horizontal':
                text = f"Horizontal: P{constraint[1]} Y = {constraint[2]:.3f}"
            elif constraint[0] == 'vertical':
                text = f"Vertical: P{constraint[1]} X = {constraint[2]:.3f}"
            elif constraint[0] == 'angle':
                text = f"Angle: P{constraint[1]}-P{constraint[2]}-P{constraint[3]} = {constraint[4]:.1f}°"
            elif constraint[0] == 'coincide':
                text = f"Coincide: P{constraint[1]} <-> P{constraint[2]}"
            elif constraint[0] == 'on_curve':
                text = f"On curve: P{constraint[1]} ∈ edge"
            else:
                text = str(constraint)
            self.constraint_list.addItem(text)

    def _apply_constraints(self, points):
        """Apply constraint solving to adjust points to satisfy constraints.
        Returns adjusted points.

        Delegates to core.solve_point_constraints -- a simultaneous damped
        Gauss-Newton (Levenberg-Marquardt) least-squares solve over all
        constraint residuals at once -- instead of the former per-
        constraint Jacobi relaxation, which treated each constraint in
        isolation and could fight itself / oscillate on coupled
        constraints. The resulting well-/under-/over-constrained state is
        reported back through the constraint-status label when present."""
        if not self.constraints:
            return points
        from rfstudio import core
        status = {}
        adjusted = core.solve_point_constraints(
            points, self.constraints, status=status)
        self._report_constraint_state(status)
        return adjusted

    def _report_constraint_state(self, status):
        """Surface the solver's well-/under-/over-constrained verdict on
        the constraint-status label, if the panel has one."""
        label = getattr(self, "constraint_status", None)
        if label is None or not status:
            return
        state = status.get("state")
        msg = {
            "well": "Fully constrained",
            "under": "Under-constrained (free DOF remain)",
            "over": "Over-constrained (conflicting — best fit shown)",
            "unknown": "Solved (relaxation fallback)",
        }.get(state, "Solved")
        res = status.get("residual")
        if res is not None:
            msg += "  |  residual %.4g" % res
        try:
            label.setText(msg)
        except Exception:
            pass

    def _apply_constraints_during_drag(self, dragged_point_index, new_position):
        """Apply constraints during drag, allowing the dragged point to move freely
        while adjusting other points to satisfy constraints."""
        if not self.constraints:
            return new_position

        # Create a copy of current points
        points = self._points()
        adjusted_points = [p for p in points]

        # Set the dragged point to the new position
        adjusted_points[dragged_point_index] = new_position

        # Apply constraint solving to adjust all points
        # But we want to keep the dragged point where the user placed it
        # So we'll solve for the other points while keeping the dragged point fixed

        # Simple approach: iterate constraint solving multiple times
        max_iterations = 5
        for _ in range(max_iterations):
            changed = False

            for constraint in self.constraints:
                if constraint[0] == 'distance':
                    idx1, idx2, target_dist = constraint[1], constraint[2], constraint[3]
                    p1 = adjusted_points[idx1]
                    p2 = adjusted_points[idx2]
                    current_dist = p1.distanceToPoint(p2)

                    if abs(current_dist - target_dist) > 0.001:
                        # Calculate adjustment vector
                        if current_dist > 0:
                            diff = p2 - p1
                            direction = diff * (1.0 / current_dist)
                            error = current_dist - target_dist
                            adjustment = direction * (error * 0.5)  # Split adjustment

                            # Apply adjustment, but don't move the dragged point
                            if idx1 != dragged_point_index:
                                adjusted_points[idx1] = p1 + adjustment
                            if idx2 != dragged_point_index:
                                adjusted_points[idx2] = p2 - adjustment
                            changed = True

                elif constraint[0] == 'horizontal':
                    idx, target_y = constraint[1], constraint[2]
                    point = adjusted_points[idx]
                    if abs(point.y - target_y) > 0.001:
                        if idx != dragged_point_index:
                            adjusted_points[idx] = point.x, target_y, point.z
                        changed = True

                elif constraint[0] == 'vertical':
                    idx, target_x = constraint[1], constraint[2]
                    point = adjusted_points[idx]
                    if abs(point.x - target_x) > 0.001:
                        if idx != dragged_point_index:
                            adjusted_points[idx] = target_x, point.y, point.z
                        changed = True

                elif constraint[0] == 'angle':
                    idx1, idx_vertex, idx3, target_angle_deg = constraint[1], constraint[2], constraint[3], constraint[4]
                    # Only adjust if neither of the points is the dragged point
                    # (or handle it appropriately if one is)
                    if idx1 != dragged_point_index and idx_vertex != dragged_point_index and idx3 != dragged_point_index:
                        p1 = adjusted_points[idx1]
                        vertex = adjusted_points[idx_vertex]
                        p3 = adjusted_points[idx3]

                        # Calculate current angle
                        v1 = p1 - vertex
                        v2 = p3 - vertex
                        current_angle_rad = v1.getAngle(v2)
                        current_angle_deg = math.degrees(current_angle_rad)

                        if abs(current_angle_deg - target_angle_deg) > 0.1:  # Only adjust if not already satisfied
                            # Calculate angle difference
                            angle_diff_rad = math.radians(target_angle_deg - current_angle_deg)

                            # Adjust by moving point1 and point3 around the vertex
                            # Simple approach: rotate points around vertex by half the error each
                            angle_to_rotate = math.radians((target_angle_deg - current_angle_deg) * 0.5)

                            # Rotate p1 around vertex
                            v1_normalized = v1.normalize()
                            # Create a rotation axis perpendicular to both vectors (cross product)
                            axis = v1.cross(v2)
                            if axis.getLength() > 0:
                                axis = axis.normalize()
                                # Rotate v1 by angle_to_rotate around axis
                                v1_rotated = v1.rotate(axis, angle_to_rotate)
                                adjusted_points[idx1] = vertex + v1_rotated

                                # Rotate v2 by -angle_to_rotate around axis (opposite direction)
                                v2_rotated = v2.rotate(axis, -angle_to_rotate)
                                adjusted_points[idx3] = vertex + v2_rotated

                                changed = True

                elif constraint[0] == 'coincide':
                    idx1, idx2 = constraint[1], constraint[2]
                    p1 = adjusted_points[idx1]
                    p2 = adjusted_points[idx2]

                    # Only adjust if neither point is the dragged point
                    # If one is the dragged point, we move the other to match it
                    if idx1 != dragged_point_index and idx2 != dragged_point_index:
                        # Neither point is dragged, move both to midpoint
                        mid_point = (p1 + p2) * 0.5

                        # Move both points to the midpoint
                        if (p1.distanceToPoint(mid_point) > 0.001 or
                            p2.distanceToPoint(mid_point) > 0.001):
                            adjusted_points[idx1] = mid_point
                            adjusted_points[idx2] = mid_point
                            changed = True
                    elif idx1 == dragged_point_index and idx2 != dragged_point_index:
                        # First point is dragged, move second to match it
                        if p2.distanceToPoint(p1) > 0.001:
                            adjusted_points[idx2] = p1
                            changed = True
                    elif idx1 != dragged_point_index and idx2 == dragged_point_index:
                        # Second point is dragged, move first to match it
                        if p1.distanceToPoint(p2) > 0.001:
                            adjusted_points[idx1] = p2
                            changed = True

                elif constraint[0] == 'on_curve':
                    # Keep an on-curve point stuck to its target edge while it
                    # is the one being dragged (live preview of the pin).
                    idx, edge = constraint[1], constraint[2]
                    if idx == dragged_point_index:
                        try:
                            import Part
                            p = adjusted_points[idx]
                            v = p if isinstance(p, FreeCAD.Vector) \
                                else FreeCAD.Vector(p[0], p[1], p[2])
                            d, near, _ = Part.Vertex(v).distToShape(edge)
                            if d > 0.001 and near:
                                adjusted_points[idx] = near[0][1]
                                changed = True
                        except Exception:
                            pass

            # If no changes in this iteration, we've converged
            if not changed:
                break

        return adjusted_points[dragged_point_index]

    # ---- end constraint system ---


class BlendPanel(_EditPanelBase):
    """Blend curve between two existing curves (auto-detects facing ends)."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj, "Blend curve",
                          lambda: parametric.make_curve("BlendCurve", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Blend curve")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel("Select two curves, then assign:"))
        for slot_name in ("Curve1", "Curve2"):
            r = QtWidgets.QHBoxLayout()
            lbl = QtWidgets.QLabel(slot_name + ": " + self._name(slot_name))
            b = QtWidgets.QPushButton("Set source from selection")
            b.clicked.connect(lambda c=False, s=slot_name, l=lbl: self._assign(s, l))
            r.addWidget(lbl)
            r.addStretch(1)
            r.addWidget(b)
            lay.addLayout(r)

        self.cont = QtWidgets.QComboBox()
        self.cont.addItems(["G1", "G2", "G3"])
        self.cont.setCurrentText(str(self.obj.Continuity))
        self.cont.currentTextChanged.connect(
            lambda v: self._set("Continuity", v))
        lay.addWidget(self.cont)

        self.scale = QtWidgets.QDoubleSpinBox()
        self.scale.setRange(0.1, 5.0)
        self.scale.setSingleStep(0.1)
        self.scale.setValue(float(self.obj.Scale))
        self.scale.valueChanged.connect(lambda v: self._set("Scale", v))
        lay.addWidget(self.scale)

        if self.creating:
            self._prefill()
        self.recompute()

        # Add curve type converter controls for blend curves
        self.kind = "BlendCurve"  # for converter UI consistency
        self._add_converter_controls()

    def _name(self, slot):
        o = getattr(self.obj, slot)
        return o.Label if o else "(none)"

    def _assign(self, slot, lbl):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None)]
        if sel:
            setattr(self.obj, slot, sel[0])
            lbl.setText(slot + ": " + self._name(slot))
            self.recompute()

    def _prefill(self):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object is not self.obj]
        if len(sel) >= 1:
            self.obj.Curve1 = sel[0]
        if len(sel) >= 2:
            self.obj.Curve2 = sel[1]

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
        self.obj.recompute()
        FreeCADGui.updateGui()

    # ---- UI ---- #
    def _add_converter_controls(self):
        """Add controls for converting between curve types."""
        convert_layout = QtWidgets.QHBoxLayout()
        convert_layout.addWidget(QtWidgets.QLabel("Convert To:"))
        self.convert_type_combo = QtWidgets.QComboBox()
        self.convert_type_combo.addItems(["InterpCurve", "ControlCurve", "ApproxCurve", "Polyline"])
        # Since we are always converting from BlendCurve, we don't set current text to self.kind
        # as it's not in the list. We'll default to the first item.
        self.convert_type_combo.setCurrentIndex(0)
        convert_layout.addWidget(self.convert_type_combo)
        self.convert_btn = QtWidgets.QPushButton("Convert")
        self.convert_btn.clicked.connect(self._on_convert_clicked)
        convert_layout.addWidget(self.convert_btn)
        convert_layout.addStretch()
        self._lay.addLayout(convert_layout)

    # ---- curve type conversion for blend curves ---- #
    def _sample_blend_curve(self, num_points=50):
        """Sample the blend curve to generate points for conversion."""
        if not self.obj.Curve1 or not self.obj.Curve2:
            return []   # cannot sample without input curves
        # Get the first edge of each input curve
        shape1 = self.obj.Curve1.Shape
        shape2 = self.obj.Curve2.Shape
        if not shape1 or not shape1.Edges or not shape2 or not shape2.Edges:
            return []
        e1 = shape1.Edges[0]
        e2 = shape2.Edges[0]
        if not e1 or not e2:
            return []
        # Use core.blend_curve to generate a temporary shape for sampling
        from rfstudio import core
        try:
            # Determine which ends face each other (as in BlendCurveFP.execute)
            a1, a2 = core.nearest_ends(e1, e2)
            # FIX: was `obj.Continuity`/`obj.Scale` (bare name, NameError,
            # silently swallowed by the except below) -- must be self.obj.
            blend_shape = core.blend_curve(e1, a1, e2, a2,
                                          continuity=str(self.obj.Continuity),
                                          scale=float(self.obj.Scale))
            if not blend_shape or not blend_shape.Edges:
                return []
            edge = blend_shape.Edges[0]
            # Sample points along the edge
            params = [i / (num_points - 1.0) for i in range(num_points)]
            pts = []
            for p in params:
                try:
                    pt = edge.valueAt(edge.FirstParameter + p * (edge.LastParameter - edge.FirstParameter))
                    pts.append(pt)
                except Exception as exc:
                    FreeCAD.Console.PrintWarning(
                        "Railform: blend-curve sampling stopped early (%s)\n" % exc)
                    break
            return pts if pts else []
        except Exception as ex:
            FreeCAD.Console.PrintError("Railform: Failed to sample blend curve: {}\n".format(ex))
            return []

    def _on_convert_clicked(self):
        """Handle convert button click: create new object of selected type."""
        target_type = self.convert_type_combo.currentText()
        # Sample the blend curve
        pts = self._sample_blend_curve()
        if len(pts) < 2:
            FreeCAD.Console.PrintError("Railform: Need at least 2 points to convert blend curve\n")
            return

        # Create new object of target type
        doc = self.obj.Document
        new_obj = parametric.make_curve(target_type, doc)
        new_obj.Label = self.obj.Label + " converted"
        new_obj.Points = pts

        # Set type-specific properties with sensible defaults
        if target_type == "ControlCurve":
            deg = min(3, len(pts) - 1)
            deg = max(1, deg)
            new_obj.Degree = deg
        elif target_type == "ApproxCurve":
            new_obj.Degree = 3
            new_obj.Tolerance = 0.1
        elif target_type == "Polyline":
            new_obj.Closed = False
        # InterpCurve needs no extra properties

        # Recompute new object
        new_obj.recompute()

        # Select new object and clear old selection
        Gui = FreeCADGui
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(new_obj)

        FreeCAD.Console.PrintMessage(
            "Railform: Converted {} to {}\n".format(self.obj.Label, new_obj.Label))