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
from rfstudio import core
from rfstudio.panel_base import (
    _EditPanelBase, _selected_points, _notify_overlay_unavailable,
    _ShortcutHintBar, _CollapsibleGroup, drop_callbacks,
    _style_point_table, _delete_cell_button, _muted_color,
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
                    # Back-reference so viewport box selection can sync table rows
                    self._editor._panel = self
                    # Start the editor on the same construction plane as the panel
                    try:
                        self._editor.set_plane_family(self._plane_family)
                    except Exception:
                        pass
                else:
                    self._editor = None
        self._pick_count = 0        # per-pick committed transactions
        self._picking = False
        self._cbid = None
        self._cbid_mov = None       # move-event callback for draw-mode snap
        self._cb_key = None         # keyboard-event callback for Tab/XYZ/undo/finish
        self._plane_family = "XY"   # active construction plane family
        self._planar_mode = True    # ON = draw on construction plane; OFF = free-form 3D
        self._axis_lock = None      # 'X', 'Y', 'Z' or None
        self._rubber_root = None    # Coin3D rubberband root
        self._rubber_sw = None      # Coin3D switch for rubberband line
        self._rubber_coords = None  # Coin3D coordinates for rubberband line
        self._last_click_time = 0.0
        self._last_click_pos = (0, 0)
        self._last_cursor_pt = None
        self._num_input = None
        self._ctrl = False
        self._snap_pt = None        # snapped 3D point, used by next click
        self._snap_cands = []       # cached STRONG candidates for pick session
        self._snap_weak = []        # cached off-curve control pts (weak tier)
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
        self.form.setWindowTitle("Railform -- %s" % self.kind)
        self._lay = QtWidgets.QVBoxLayout(self.form)

        # --- viewport shortcut hint bar ----------------------------------- #
        self.shortcut_bar = _ShortcutHintBar("X/Y/Z: lock  |  Tab: plane  |  Esc: drop")
        self._lay.addWidget(self.shortcut_bar)

        # ---- Planar-first authoring controls ---------------------------- #
        planar_row = QtWidgets.QHBoxLayout()
        self.planar_cb = QtWidgets.QCheckBox("Planar Mode")
        self.planar_cb.setChecked(self._planar_mode)
        self.planar_cb.setToolTip(
            "ON = click/drag projects onto the active construction plane "
            "(Class-A orthographic workflow). OFF = free-form 3D.")
        self.planar_cb.toggled.connect(self._toggle_planar_mode)
        planar_row.addWidget(self.planar_cb)

        plane_lbl = QtWidgets.QLabel("Plane:")
        plane_lbl.setStyleSheet("font-weight:bold;")
        planar_row.addWidget(plane_lbl)
        self.plane_combo = QtWidgets.QComboBox()
        self.plane_combo.addItems(["XY", "YZ", "ZX"])
        self.plane_combo.setCurrentText(self._plane_family)
        self.plane_combo.setMaximumWidth(80)
        self.plane_combo.currentTextChanged.connect(self._set_plane_family)
        planar_row.addWidget(self.plane_combo)

        planar_row.addStretch(1)

        # View buttons: reorient camera + switch construction plane to match
        v_top = QtWidgets.QPushButton("Top")
        v_top.setToolTip("Top view — draw on XY plane")
        v_top.setMaximumWidth(45)
        v_top.clicked.connect(self._set_view_top)
        planar_row.addWidget(v_top)

        v_front = QtWidgets.QPushButton("Front")
        v_front.setToolTip("Front view — draw on XZ (ZX) plane")
        v_front.setMaximumWidth(50)
        v_front.clicked.connect(self._set_view_front)
        planar_row.addWidget(v_front)

        v_side = QtWidgets.QPushButton("Side")
        v_side.setToolTip("Side view — draw on YZ plane")
        v_side.setMaximumWidth(50)
        v_side.clicked.connect(self._set_view_side)
        planar_row.addWidget(v_side)

        v_axo = QtWidgets.QPushButton("3D")
        v_axo.setToolTip("Axonometric view")
        v_axo.setMaximumWidth(35)
        v_axo.clicked.connect(self._set_view_axometric)
        planar_row.addWidget(v_axo)

        self._lay.addLayout(planar_row)

        # Add curve type converter controls
        self._add_converter_controls()

        self._build()
        self._setup_highlight()
        # Deliberately NOT auto-seeding a new curve's points from whatever
        # happens to be selected right now -- every draw-tool entry point
        # (RFS_Draw*) is click-to-place, and FreeCAD conventionally leaves
        # the previous curve selected after accept(), so this used to
        # silently start the next curve with the last curve's points. Use
        # the "Add selected" button (_add_sel) to seed from selection
        # on purpose.
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
        # 1. Direct Spline Operations row (always accessible at the top):
        ops_row = QtWidgets.QHBoxLayout()
        if self.kind in ("ControlCurve", "BezierCurve") or hasattr(self.obj, "Degree"):
            self.elevate_btn = QtWidgets.QPushButton("Elevate Degree (p → p+1)")
            self.elevate_btn.setToolTip("Add control poles for finer sculpting without altering the curve shape (exact degree elevation).")
            self.elevate_btn.clicked.connect(self._on_elevate_degree)
            ops_row.addWidget(self.elevate_btn)

        self.fair_btn = QtWidgets.QPushButton("Fair Curve")
        self.fair_btn.setToolTip("Smooth out wobbles by minimizing strain energy (Laplacian relaxation) while preserving endpoints.")
        self.fair_btn.clicked.connect(self._on_fair_curve)
        ops_row.addWidget(self.fair_btn)
        self._lay.addLayout(ops_row)

        # 2. Unified Canvas Drawing master toggle:
        self.canvas_btn = QtWidgets.QPushButton("Canvas drawing: Active (click in 3D)")
        self.canvas_btn.setCheckable(True)
        self.canvas_btn.setChecked(self.creating)
        self.canvas_btn.toggled.connect(self._toggle_canvas_drawing)
        self.pick_btn = self.canvas_btn
        self.draw_btn = self.canvas_btn
        self._lay.addWidget(self.canvas_btn)

        # 3. Parameters & Alignment (Expanded by default)
        self.params_group = _CollapsibleGroup("Parameters & Alignment", collapsed=False)
        self._scalar_controls(self.params_group.content_layout)

        role_row = QtWidgets.QHBoxLayout()
        role_row.addWidget(QtWidgets.QLabel("Surface Role:"))
        self.role_combo = QtWidgets.QComboBox()
        self.role_combo.addItem("None (Standard)")
        self.role_combo.addItem("Profile (Blue - U Cross-Section)")
        self.role_combo.addItem("Guide Rail (Amber - V Guide)")
        cur_role = parametric.get_curve_role(self.obj)
        self.role_combo.setCurrentIndex(1 if cur_role == "Profile" else (2 if cur_role == "GuideRail" else 0))
        self.role_combo.currentIndexChanged.connect(self._on_role_combo_changed)
        role_row.addWidget(self.role_combo)
        role_row.addStretch(1)
        self.params_group.content_layout.addLayout(role_row)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        self.params_group.content_layout.addWidget(self.live)
        self._lay.addWidget(self.params_group)

        # 4. Coordinate Table (Collapsed by default during active drafting or if > 4 points)
        self.coord_group = _CollapsibleGroup(
            "Coordinate Table (%d points)" % len(self._points()),
            collapsed=self.creating or len(self._points()) > 4
        )
        c_lay = self.coord_group.content_layout

        header_labels = ["X", "Y", "Z", "W" if self.kind == "ControlCurve" else "", ""]
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(header_labels)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.selected_rows = set()
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
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
        self.table.setMaximumHeight(200)
        self.table.setMaximumWidth(300)
        _style_point_table(self.table)
        c_lay.addWidget(self.table)

        row = QtWidgets.QHBoxLayout()
        for label, slot in (("Add selected", self._add_sel),
                            ("Remove", self._remove), ("↑", self._up),
                            ("↓", self._down)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        c_lay.addLayout(row)

        # ControlCurve B-splines: knot editor toggle
        if self.kind == "ControlCurve" and getattr(self, "_editor", None) is not None:
            self._knot_toggle = QtWidgets.QCheckBox("Edit knots (drag in viewport)")
            self._knot_toggle.setChecked(False)
            self._knot_toggle.toggled.connect(self._toggle_knot_editor)
            c_lay.addWidget(self._knot_toggle)
            self.table.itemSelectionChanged.connect(self._sync_knot_highlight)

        # Symmetric editing
        if self.kind in ("InterpCurve", "ControlCurve", "ApproxCurve", "Polyline", "BezierCurve") \
                and getattr(self, "_editor", None) is not None:
            self._insert_toggle = QtWidgets.QCheckBox(
                "Insert point on segment click (else Shift+click)")
            self._insert_toggle.setChecked(
                bool(getattr(self._editor, "insert_on_click", False)))
            self._insert_toggle.toggled.connect(self._toggle_insert_on_click)
            c_lay.addWidget(self._insert_toggle)

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

            self.promote_btn = QtWidgets.QPushButton("Promote to MFP")
            self.promote_btn.setToolTip(
                "Convert this symmetric curve into an explicit MirrorFP pair")
            self.promote_btn.clicked.connect(self._promote_to_mfp)
            sym_row.addWidget(self.promote_btn)
            c_lay.addLayout(sym_row)

            if self.kind == "BezierCurve":
                bezier_row = QtWidgets.QHBoxLayout()
                bezier_row.addWidget(QtWidgets.QLabel("Handle Type:"))
                self.handle_type_combo = QtWidgets.QComboBox()
                self.handle_type_combo.addItems(["Auto", "Aligned", "Mirrored", "Vector", "Free"])
                self.handle_type_combo.currentTextChanged.connect(self._on_handle_type_changed)
                bezier_row.addWidget(self.handle_type_combo)
                bezier_row.addStretch()
                c_lay.addLayout(bezier_row)

                self.toggle_handles_btn = QtWidgets.QPushButton("Show Handles: On")
                self.toggle_handles_btn.setCheckable(True)
                self.toggle_handles_btn.setChecked(True)
                self.toggle_handles_btn.toggled.connect(self._toggle_handle_visibility)
                c_lay.addWidget(self.toggle_handles_btn)
        else:
            self.sym_toggle = None
            self.sym_plane_combo = None

        self._lay.addWidget(self.coord_group)

        # 5. Curvature visualization controls (Collapsed by default)
        self.curv_group = _CollapsibleGroup("Curvature Combs & Quality", collapsed=True)
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
        self.curv_group.content_layout.addLayout(curv_layout)
        self._lay.addWidget(self.curv_group)

        # 5b. Multi-CV transform (Phase 1: collective translate/scale/rotate
        # of selected control points; works with table multi-select and
        # viewport Ctrl+drag box selection).
        self.multicv_group = _CollapsibleGroup("Multi-CV Transform", collapsed=True)
        mc_layout = QtWidgets.QVBoxLayout()

        mc_info = QtWidgets.QLabel("0 points selected (Ctrl+click rows, or Ctrl+drag in viewport)")
        mc_info.setStyleSheet("color:palette(mid);font-size:11px;padding:2px 0;")
        self.multicv_info = mc_info
        mc_layout.addWidget(mc_info)

        # Delta XYZ translate
        trans_row = QtWidgets.QHBoxLayout()
        trans_row.addWidget(QtWidgets.QLabel("Move Δ (mm):"))
        self.mc_dx = QtWidgets.QDoubleSpinBox(); self.mc_dx.setRange(-1e6, 1e6); self.mc_dx.setDecimals(3); self.mc_dx.setValue(0.0)
        self.mc_dy = QtWidgets.QDoubleSpinBox(); self.mc_dy.setRange(-1e6, 1e6); self.mc_dy.setDecimals(3); self.mc_dy.setValue(0.0)
        self.mc_dz = QtWidgets.QDoubleSpinBox(); self.mc_dz.setRange(-1e6, 1e6); self.mc_dz.setDecimals(3); self.mc_dz.setValue(0.0)
        for sb in (self.mc_dx, self.mc_dy, self.mc_dz):
            sb.setSingleStep(1.0); sb.setKeyboardTracking(False); trans_row.addWidget(sb)
        mc_btn = QtWidgets.QPushButton("Move Selected")
        mc_btn.setToolTip("Apply the same ΔXYZ to every selected control point")
        mc_btn.clicked.connect(self._on_mc_move_selected)
        trans_row.addWidget(mc_btn)
        mc_layout.addLayout(trans_row)

        # Scale around centroid
        scale_row = QtWidgets.QHBoxLayout()
        scale_row.addWidget(QtWidgets.QLabel("Scale about centroid:"))
        self.mc_scale = QtWidgets.QDoubleSpinBox(); self.mc_scale.setRange(0.001, 1000.0); self.mc_scale.setDecimals(4); self.mc_scale.setValue(1.0); self.mc_scale.setSingleStep(0.1)
        scale_row.addWidget(self.mc_scale)
        sc_btn = QtWidgets.QPushButton("Scale Selected")
        sc_btn.setToolTip("Scale selected control points about their centroid")
        sc_btn.clicked.connect(self._on_mc_scale_selected)
        scale_row.addWidget(sc_btn)
        mc_layout.addLayout(scale_row)

        # Rotate around centroid
        rot_row = QtWidgets.QHBoxLayout()
        rot_row.addWidget(QtWidgets.QLabel("Rotate about centroid (deg):"))
        self.mc_rot = QtWidgets.QDoubleSpinBox(); self.mc_rot.setRange(-360.0, 360.0); self.mc_rot.setDecimals(2); self.mc_rot.setValue(0.0); self.mc_rot.setSingleStep(5.0)
        rot_row.addWidget(self.mc_rot)
        rt_btn = QtWidgets.QPushButton("Rotate Selected")
        rt_btn.setToolTip("Rotate selected control points about their centroid")
        rt_btn.clicked.connect(self._on_mc_rotate_selected)
        rot_row.addWidget(rt_btn)
        mc_layout.addLayout(rot_row)

        self.multicv_group.content_layout.addLayout(mc_layout)
        self._lay.addWidget(self.multicv_group)

        # 6. Constraints section (Collapsed by default)
        self.cons_group = _CollapsibleGroup("Geometric Constraints (0 active)", collapsed=True)
        constraints_layout = QtWidgets.QVBoxLayout()

        btn_layout = QtWidgets.QHBoxLayout()
        for name, slot, tip in [
            ("Distance", self._add_distance_constraint, "Constrain distance between two points"),
            ("Horizontal", self._add_horizontal_constraint, "Constrain segment horizontal"),
            ("Vertical", self._add_vertical_constraint, "Constrain segment vertical"),
            ("Angle", self._add_angle_constraint, "Constrain angle of segment"),
            ("Coincide", self._add_coincidence_constraint, "Constrain two points coincident"),
            ("On Curve", self._add_on_curve_constraint, "Pin selected point onto another curve"),
        ]:
            b = QtWidgets.QPushButton(name)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btn_layout.addWidget(b)
        constraints_layout.addLayout(btn_layout)

        self.constraint_list = QtWidgets.QListWidget()
        self.constraint_list.setMaximumHeight(100)
        constraints_layout.addWidget(self.constraint_list)

        self.remove_constraint_btn = QtWidgets.QPushButton("Remove Selected")
        self.remove_constraint_btn.clicked.connect(self._remove_constraint)
        constraints_layout.addWidget(self.remove_constraint_btn)

        self.constraint_status = QtWidgets.QLabel("")
        self.constraint_status.setWordWrap(True)
        self.constraint_status.setStyleSheet(
            "color:palette(mid);font-size:11px;padding:2px 0;")
        constraints_layout.addWidget(self.constraint_status)

        self.cons_group.content_layout.addLayout(constraints_layout)
        self._lay.addWidget(self.cons_group)

        # Viewport grid (XY, YZ, XZ planes)
        self._setup_viewport_grid()

        # Authoring default: 3D viewport picking engages immediately for a
        # NEW curve (creating=True) so points can be placed by clicking in
        # the 3D view without hunting for the toggle first. Editing an
        # existing curve keeps pick OFF -- a stray viewport click into an
        # edit session must not silently insert or move points.
        if self.creating:
            self.canvas_btn.setChecked(True)

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

    def _on_role_combo_changed(self, idx):
        """Update curve role (Profile / Guide Rail / None) and apply viewport styling."""
        role = "None"
        if idx == 1:
            role = "Profile"
        elif idx == 2:
            role = "GuideRail"
        parametric.set_curve_role(self.obj, role)
        from rfstudio import statusbar
        if role == "Profile":
            statusbar.flash("Curve role: Profile (Blue, auto-routes to surface U-Profiles)")
        elif role == "GuideRail":
            statusbar.flash("Curve role: Guide Rail (Amber, auto-routes to surface V-Rails)")
        else:
            statusbar.flash("Curve role: None")

    # ---- per-kind scalar property controls (Closed / Degree / Tolerance) --
    def _scalar_controls(self, target_layout=None):
        """Auto-generate simple controls for this curve object's own
        scalar properties -- e.g. Closed (Interp/Polyline), Degree
        (Control/Approx), Tolerance (Approx). Mirrors
        panel_surface.SurfacePanel._scalar_controls, scoped to this
        curve's own property group (self.kind) instead of iterating a
        surface's link-list groups. Knots (ControlCurve) is deliberately
        skipped -- it's edited via the in-viewport knot-drag editor
        (editmode.KnotEditor), not a spinbox here.
        """
        self._target_scalar_layout = target_layout or self._lay
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
            elif tid == "App::PropertyEnumeration":
                self._add_enum_ctrl(name)

    def _row_ctrl(self, label, widget):
        r = QtWidgets.QHBoxLayout()
        r.addWidget(QtWidgets.QLabel(label))
        r.addStretch(1)
        r.addWidget(widget)
        target = getattr(self, "_target_scalar_layout", None) or self._lay
        target.addLayout(r)

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

    def _add_enum_ctrl(self, name):
        cb = QtWidgets.QComboBox()
        enums = []
        try:
            enums = self.obj.getEnumerationsOfProperty(name)
        except Exception:
            enums = []
        if not enums:
            val = getattr(self.obj, name, None)
            if isinstance(val, (list, tuple)):
                enums = list(val)
            elif isinstance(val, str):
                enums = [val]
        for item in enums:
            cb.addItem(str(item))
        cur = str(getattr(self.obj, name, ""))
        idx = cb.findText(cur)
        if idx >= 0:
            cb.setCurrentIndex(idx)
        cb.currentTextChanged.connect(lambda v, n=name: self._set(n, v))
        self._row_ctrl(name, cb)

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
        if hasattr(self, "coord_group"):
            self.coord_group.setTitle("Coordinate Table (%d points)" % len(pts))
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
                w_label.setStyleSheet("color:%s;" % _muted_color())
                self.table.setCellWidget(i, 3, w_label)
                spins.append(w_label)
            # Delete button
            del_btn = _delete_cell_button()
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

    def _on_table_selection_changed(self):
        """Sync multi-CV selection between the coordinate table and viewport.

        When the user Ctrl/Cmd-clicks or Shift-clicks rows in the table,
        highlight the corresponding points in the 3D editor (yellow squares)
        and track them in self.selected_rows for collective transforms.
        """
        rows = self._selected_rows()
        self.selected_rows = set(rows)
        if self._editor is not None and hasattr(self._editor, "highlight_points"):
            self._editor.highlight_points(rows)
        # Update the multi-CV info label
        if hasattr(self, "multicv_info"):
            n = len(rows)
            self.multicv_info.setText(
                "%d point%s selected (Ctrl+click rows, or Ctrl+drag in viewport)"
                % (n, "" if n == 1 else "s"))

    def _on_mc_move_selected(self):
        """Apply the same ΔXYZ to every selected control point."""
        if not self.selected_rows:
            return
        dx, dy, dz = self.mc_dx.value(), self.mc_dy.value(), self.mc_dz.value()
        if dx == 0.0 and dy == 0.0 and dz == 0.0:
            return
        pts = list(self._points())
        from FreeCAD import Vector
        delta = Vector(dx, dy, dz)
        for i in sorted(self.selected_rows):
            if 0 <= i < len(pts):
                pts[i] = pts[i] + delta
        self._set_points(pts)
        self.recompute()
        self._fill_table()
        if self._editor is not None:
            self._editor.highlight_points(self.selected_rows)
            self._editor.commit()

    def _on_mc_scale_selected(self):
        """Scale selected control points about their centroid."""
        if not self.selected_rows:
            return
        s = self.mc_scale.value()
        if abs(s - 1.0) < 1e-9:
            return
        pts = list(self._points())
        from FreeCAD import Vector
        sel_pts = [pts[i] for i in sorted(self.selected_rows) if 0 <= i < len(pts)]
        if not sel_pts:
            return
        c = sum((Vector(p.x, p.y, p.z) for p in sel_pts), Vector(0, 0, 0)) * (1.0 / len(sel_pts))
        for i in sorted(self.selected_rows):
            if 0 <= i < len(pts):
                p = Vector(pts[i].x, pts[i].y, pts[i].z)
                pts[i] = c + (p - c) * s
        self._set_points(pts)
        self.recompute()
        self._fill_table()
        if self._editor is not None:
            self._editor.highlight_points(self.selected_rows)
            self._editor.commit()

    def _on_mc_rotate_selected(self):
        """Rotate selected control points about their centroid (degrees)."""
        if not self.selected_rows:
            return
        deg = self.mc_rot.value()
        if abs(deg) < 1e-9:
            return
        import math
        rad = math.radians(deg)
        ca, sa = math.cos(rad), math.sin(rad)
        pts = list(self._points())
        from FreeCAD import Vector
        sel_pts = [pts[i] for i in sorted(self.selected_rows) if 0 <= i < len(pts)]
        if not sel_pts:
            return
        c = sum((Vector(p.x, p.y, p.z) for p in sel_pts), Vector(0, 0, 0)) * (1.0 / len(sel_pts))
        # Rotation axis: view direction (approximate; use Z for now)
        for i in sorted(self.selected_rows):
            if 0 <= i < len(pts):
                p = Vector(pts[i].x, pts[i].y, pts[i].z)
                d = p - c
                # Rotate in XY plane about Z axis
                x2 = c.x + ca * d.x - sa * d.y
                y2 = c.y + sa * d.x + ca * d.y
                pts[i] = Vector(x2, y2, p.z)
        self._set_points(pts)
        self.recompute()
        self._fill_table()
        if self._editor is not None:
            self._editor.highlight_points(self.selected_rows)
            self._editor.commit()

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
        if getattr(self, "_cbid", None) is not None:
            self._toggle_pick(False)
        self._remove_rubberband()
        self._hide_numeric_input()
        self._teardown_highlight()
        self._remove_viewport_grid()
        self._hide_curvature()
        from rfstudio import statusbar
        statusbar.clear()

    def _setup_viewport_grid(self, plane=None):
        """Set up a grid in the active construction plane for viewport reference."""
        try:
            from pivy import coin
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            self._viewport_grid_root = coin.SoSeparator()
            size = 200.0        # total extent (units)
            minor = 5.0         # fine grid spacing
            major = 25.0        # heavier reference lines
            half = size / 2.0
            pname = plane or getattr(self, "_plane_family", "XY")

            def _grid_plane(step, rgb):
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
                    if pname == "YZ":
                        verts.extend([(0.0, -half, c), (0.0, half, c)])
                        nverts.append(2)
                        verts.extend([(0.0, c, -half), (0.0, c, half)])
                        nverts.append(2)
                    elif pname == "ZX":
                        verts.extend([(-half, 0.0, c), (half, 0.0, c)])
                        nverts.append(2)
                        verts.extend([(c, 0.0, -half), (c, 0.0, half)])
                        nverts.append(2)
                    else:  # XY or Camera default
                        verts.extend([(-half, c, 0.0), (half, c, 0.0)])
                        nverts.append(2)
                        verts.extend([(c, -half, 0.0), (c, half, 0.0)])
                        nverts.append(2)
                coords.point.setValues(0, len(verts), verts)
                lines.numVertices.setValues(0, len(nverts), nverts)
                sep.addChild(coords)
                sep.addChild(lines)
                return sep

            # minor faint, major slightly brighter so scale still reads
            self._viewport_grid_root.addChild(_grid_plane(minor, (0.15, 0.15, 0.15)))
            self._viewport_grid_root.addChild(_grid_plane(major, (0.25, 0.25, 0.25)))
            sg.addChild(self._viewport_grid_root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: Viewport grid unavailable (%s)\n" % exc)
            self._viewport_grid_root = None

    def _update_plane_grid(self):
        self._remove_viewport_grid()
        self._setup_viewport_grid(self._plane_family)

    def _set_plane_family(self, name):
        """Switch the active construction plane and propagate to the live editor.

        Called by Tab cycling, view buttons (Top/Front/Side), and the plane
        combo. Updates the viewport grid and the PointEditor so drag stays
        constrained to the same plane.
        """
        self._plane_family = name
        self._update_plane_grid()
        # Propagate to the live viewport editor so drags stay on-plane.
        ed = getattr(self, "_editor", None)
        if ed is not None and hasattr(ed, "set_plane_family"):
            try:
                ed.set_plane_family(name)
            except Exception:
                pass
        # Refresh the cursor HUD plane badge if visible
        try:
            from rfstudio import statusbar
            statusbar.flash("Plane: %s" % name)
        except Exception:
            pass

    def _toggle_planar_mode(self, on):
        """Enable/disable planar-first drawing.

        ON  = click/drag projects onto the active construction plane (Class-A
              orthographic workflow).
        OFF = free-form 3D; the cursor lands at raw view depth.
        """
        self._planar_mode = bool(on)
        try:
            from rfstudio import statusbar
            statusbar.flash("Planar Mode: %s" % ("ON" if self._planar_mode else "OFF (free 3D)"))
        except Exception:
            pass

    def _set_view_top(self):
        """Top view: camera down -Z, construction plane = XY."""
        view = self._active_view()
        if view is None:
            return
        try:
            view.viewTop()
        except Exception:
            pass
        self._set_plane_family("XY")

    def _set_view_front(self):
        """Front view: camera down -Y, construction plane = XZ (named ZX in our convention)."""
        view = self._active_view()
        if view is None:
            return
        try:
            view.viewFront()
        except Exception:
            pass
        self._set_plane_family("ZX")

    def _set_view_side(self):
        """Side view: camera down -X, construction plane = YZ."""
        view = self._active_view()
        if view is None:
            return
        try:
            view.viewSide()
        except Exception:
            pass
        self._set_plane_family("YZ")

    def _set_view_axometric(self):
        """Axonometric 3D view; keep current plane but allow free-form 3D hint."""
        view = self._active_view()
        if view is None:
            return
        try:
            view.viewAxometric()
        except Exception:
            pass

    def _active_view(self):
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return None
            return doc.ActiveView
        except Exception:
            return None

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

    def _on_elevate_degree(self):
        """Elevate B-Spline curve degree by 1 (p -> p+1), preserving exact shape."""
        import Part
        from rfstudio import core, statusbar
        try:
            edge = getattr(self.obj, "Shape", None)
            source = None
            if edge is not None and getattr(edge, "Edges", None) and len(edge.Edges) > 0:
                first_edge = edge.Edges[0]
                if hasattr(first_edge, "Curve") and isinstance(first_edge.Curve, Part.BSplineCurve):
                    source = first_edge
            if source is None:
                source = self._points()

            deg = getattr(self.obj, "Degree", None)
            target = (deg + 1) if deg is not None else None
            res = core.elevate_curve_degree(source, target_degree=target)

            new_poles = res["poles"]
            new_deg = res["degree"]

            if hasattr(self.obj, "Degree"):
                self.obj.Degree = new_deg
            if hasattr(self.obj, "Weights") and "weights" in res:
                self._weights = list(res["weights"])
                self.obj.Weights = self._weights
            if hasattr(self.obj, "Knots") and "knots" in res:
                self.obj.Knots = list(res["knots"])
            if hasattr(self.obj, "Mults") and "mults" in res:
                self.obj.Mults = list(res["mults"])

            self._set_points(new_poles)
            self._fill_table()
            if self._editor is not None:
                try:
                    self._editor._refresh_markers()
                    self._editor._refresh_arrows()
                except Exception:
                    pass
            statusbar.flash("Elevated curve degree to %d (%d poles, exact shape preserved)"
                            % (new_deg, len(new_poles)))
        except Exception as exc:
            statusbar.flash("Degree elevation failed: %s" % exc, error=True)

    def _on_fair_curve(self):
        """Smooth curve points while fixing endpoints."""
        from rfstudio import core, statusbar
        pts = self._points()
        if len(pts) < 4:
            statusbar.flash("Fair curve requires at least 4 points", error=True)
            return
        try:
            smoothed = core.fair_curve(pts, iterations=5, strength=0.4, fix_endpoints=True)
            self._set_points(smoothed)
            self._fill_table()
            if self._editor is not None:
                try:
                    self._editor._refresh_markers()
                    self._editor._refresh_arrows()
                except Exception:
                    pass
            statusbar.flash("Curve faired: smoothed wobbles while preserving endpoints")
        except Exception as exc:
            statusbar.flash("Curve fairing failed: %s" % exc, error=True)

    def _toggle_canvas_drawing(self, on):
        """Unified canvas drawing mode for interactive point authoring."""
        if hasattr(self, "canvas_btn"):
            self.canvas_btn.blockSignals(True)
            self.canvas_btn.setChecked(bool(on))
            self.canvas_btn.setText(
                "Canvas Drawing: Active (click in 3D | Enter to finish)" if on
                else "Canvas Drawing: Paused (click to resume)"
            )
            self.canvas_btn.blockSignals(False)
        self._toggle_pick(on)

    def _toggle_draw_mode(self, on):
        """Legacy alias routing to unified canvas drawing mode."""
        self._toggle_canvas_drawing(on)

    # ---- 3D picking & canvas drafting ---- #
    def _toggle_pick(self, on):
        if hasattr(self, "canvas_btn") and self.canvas_btn.isChecked() != bool(on):
            self.canvas_btn.blockSignals(True)
            self.canvas_btn.setChecked(bool(on))
            self.canvas_btn.setText(
                "Canvas Drawing: Active (click in 3D | Enter to finish)" if on
                else "Canvas Drawing: Paused (click to resume)"
            )
            self.canvas_btn.blockSignals(False)
        self._picking = bool(on)
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                raise Exception("No active document")
            view = doc.ActiveView
            if view is None:
                raise Exception("No active view")
            if on:
                if getattr(self, '_cbid', None) is not None:
                    return
                self._cbid = view.addEventCallback(
                    "SoMouseButtonEvent", self._on_click)
                self._cbid_mov = view.addEventCallback(
                    "SoLocation2Event", self._on_move_pick)
                self._cb_key = view.addEventCallback(
                    "SoKeyboardEvent", self._on_key)
                self._snap_cands = self._collect_snap_cands()
                self._snap_shapes = self._collect_snap_shapes()
                self._build_snap_overlay(view)
                self._ensure_rubberband(view)
                from rfstudio import statusbar
                statusbar.show("Canvas active: click to place | Plane: %s (Tab) | Lock: %s (X/Y/Z) | Enter to finish | Backspace to undo"
                               % (self._plane_family, self._axis_lock or "Free"))
            else:
                drop_callbacks(view, (
                    ("SoMouseButtonEvent", self, "_cbid"),
                    ("SoLocation2Event", self, "_cbid_mov"),
                    ("SoKeyboardEvent", self, "_cb_key"),
                ))
                self._remove_snap_overlay(view)
                self._remove_rubberband(view)
                self._remove_pen_overlay(view)
                self._hide_numeric_input()
                self._hide_cursor_hud()
                self._snap_pt = None
                self._snap_cands = []
                self._snap_weak = []
                self._snap_shapes = []
                from rfstudio import statusbar
                statusbar.clear()
        except Exception as exc:
            FreeCAD.Console.PrintWarning("Railform: canvas drawing unavailable (%s)\n"
                                         % exc)

    def _ensure_cursor_hud(self):
        if getattr(self, "_cursor_hud", None) is None:
            hud = QtWidgets.QLabel()
            hud.setWindowFlags(QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowDoesNotAcceptFocus)
            hud.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
            hud.setStyleSheet(
                "QLabel { background: rgba(18, 24, 34, 220); color: #f0f4f8; "
                "border: 1px solid rgba(80, 150, 230, 200); border-radius: 4px; "
                "padding: 2px 7px; font-size: 11px; font-weight: 500; font-family: sans-serif; }"
            )
            self._cursor_hud = hud
        return self._cursor_hud

    def _update_cursor_hud(self, dist, angle, dx, dy, dz, snap_tag):
        try:
            hud = self._ensure_cursor_hud()
            lock_s = "" if self._axis_lock is None else " [%s-LOCK]" % self._axis_lock
            snap_badge = ""
            if snap_tag:
                if "Crossing" in snap_tag:
                    snap_badge = " <span style='color:#ffa500;font-weight:bold;'>[Crossing]</span>"
                elif "Contact" in snap_tag:
                    snap_badge = " <span style='color:#00ffff;font-weight:bold;'>[Rail Contact]</span>"
                elif "Snap" in snap_tag:
                    snap_badge = " <span style='color:#33ff66;'>[Snap]</span>"

            text = f"<b>L:</b> {dist:.2f} mm &nbsp;|&nbsp; <b>∠</b> {angle:.1f}° &nbsp;|&nbsp; <b>{self._plane_family}</b>{lock_s}{snap_badge}"
            hud.setText(text)
            hud.adjustSize()
            cpos = QtGui.QCursor.pos()
            hud.move(cpos.x() + 18, cpos.y() - 30)
            if not hud.isVisible():
                hud.show()
        except Exception:
            pass

    def _update_cursor_hud_point1(self, snap_tag):
        try:
            hud = self._ensure_cursor_hud()
            lock_s = "" if self._axis_lock is None else " [%s-LOCK]" % self._axis_lock
            snap_badge = ""
            if snap_tag:
                if "Crossing" in snap_tag:
                    snap_badge = " <span style='color:#ffa500;font-weight:bold;'>[Crossing]</span>"
                elif "Contact" in snap_tag:
                    snap_badge = " <span style='color:#00ffff;font-weight:bold;'>[Rail Contact]</span>"
                elif "Snap" in snap_tag:
                    snap_badge = " <span style='color:#33ff66;'>[Snap]</span>"

            text = f"Click to place point 1 &nbsp;|&nbsp; <b>{self._plane_family}</b> (Tab){lock_s}{snap_badge}"
            hud.setText(text)
            hud.adjustSize()
            cpos = QtGui.QCursor.pos()
            hud.move(cpos.x() + 18, cpos.y() - 30)
            if not hud.isVisible():
                hud.show()
        except Exception:
            pass

    def _hide_cursor_hud(self):
        if getattr(self, "_cursor_hud", None) is not None:
            try:
                self._cursor_hud.hide()
            except Exception:
                pass

    def _collect_snap_cands(self):
        """The magnet-eligible EXACT on-geometry snap targets -- vertices,
        knots and true curve-curve intersections -- from all other objects.
        Off-curve control-polygon handles are collected separately into
        self._snap_weak (the distance-only tier), so they no longer hijack a
        draw that is aiming at a curve body. Returns the strong list; sets
        _snap_weak as a side effect. Shared policy with editmode.PointEditor
        (see snaputil.collect_ranked_points / nearest_snap_ranked)."""
        strong, weak = snaputil.collect_ranked_points(self.obj.Document,
                                                      exclude=self.obj)
        self._snap_weak = weak
        self._snap_kinded = snaputil.collect_strong_snap_points_kinded(
            self.obj.Document, exclude=self.obj)
        return strong

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

    def _apply_snap_visual(self, best, best_kind):
        """Update snap marker position, symbol, and color according to snap kind."""
        self._snap_pt = best
        self._snap_kind = best_kind
        if self._snap_coin_root is not None:
            if best is not None:
                self._snap_coord_draw.point.set1Value(0, best.x, best.y, best.z)
                if hasattr(self, "_snap_col") and hasattr(self, "_snap_marker"):
                    try:
                        from pivy import coin
                        if best_kind == snaputil.SNAP_INTERSECTION:
                            self._snap_col.rgb = (1.0, 0.70, 0.10)  # Amber crossing indicator
                            self._snap_marker.markerIndex = coin.SoMarkerSet.CROSS_9_9
                        elif best_kind == snaputil.SNAP_EDGE:
                            self._snap_col.rgb = (0.10, 0.95, 0.95)  # Cyan on-curve projection
                            self._snap_marker.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
                        else:
                            self._snap_col.rgb = (0.20, 0.95, 0.40)  # Lime vertex/midpoint/etc.
                            self._snap_marker.markerIndex = coin.SoMarkerSet.CIRCLE_FILLED_9_9
                    except Exception:
                        pass
                self._snap_sw.whichChild = 0
            else:
                self._snap_sw.whichChild = -1

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
            if getattr(self, "_snap_kinded", None) is None:
                self._snap_kinded = snaputil.collect_strong_snap_points_kinded(doc, exclude=self.obj)
            best, best_kind = snaputil.nearest_snap_ranked_kind(
                view, self._snap_kinded, getattr(self, "_snap_shapes", []),
                pos, snap_px)
            self._apply_snap_visual(best, best_kind)
        except Exception:
            pass

    def _build_snap_overlay(self, view):
        """Add a dynamic Coin3D snap-indicator marker to the scene."""
        self._remove_snap_overlay(view)
        try:
            from pivy import coin
            root = coin.SoSeparator()
            self._snap_sw = coin.SoSwitch()
            self._snap_sw.whichChild = -1
            sep = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (0.1, 0.95, 0.95)
            self._snap_col = col
            self._snap_coord_draw = coin.SoCoordinate3()
            mk = coin.SoMarkerSet()
            try:
                mk.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
            except Exception:
                pass
            mk.numPoints = 1
            self._snap_marker = mk
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

    def _ensure_rubberband(self, view):
        self._remove_rubberband(view)
        try:
            from pivy import coin
            root = coin.SoSeparator()
            self._rubber_sw = coin.SoSwitch()
            self._rubber_sw.whichChild = -1
            sep = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (1.0, 0.85, 0.2)  # Pale yellow/gold for active rubberband
            self._rubber_coords = coin.SoCoordinate3()
            lines = coin.SoLineSet()
            self._rubber_lines = lines
            sep.addChild(col)
            sep.addChild(self._rubber_coords)
            sep.addChild(lines)
            self._rubber_sw.addChild(sep)
            root.addChild(self._rubber_sw)
            view.getSceneGraph().addChild(root)
            self._rubber_root = root
        except Exception:
            self._rubber_root = None
            self._rubber_sw = None
            self._rubber_coords = None

    def _update_rubberband(self, view, curr_pt):
        try:
            if self._rubber_sw is None or self._rubber_coords is None:
                return
            pts = self._points()
            if not pts or curr_pt is None:
                self._rubber_sw.whichChild = -1
                return
            last_p = pts[-1]
            if self.kind == "BezierCurve" and len(pts) >= 1:
                # Sample a smooth prospective cubic Bezier from last_p to curr_pt
                rh = getattr(self.obj, "RightHandles", [])
                p0 = last_p
                p3 = curr_pt
                if rh and len(rh) == len(pts) and (rh[-1] - p0).Length > 1e-4:
                    p1 = rh[-1]
                else:
                    p1 = p0 + (p3 - p0) * (1.0 / 3.0)
                p2 = p3 - (p3 - p0) * (1.0 / 3.0)

                samples = 16
                curve_pts = []
                for s in range(samples + 1):
                    t = float(s) / samples
                    omt = 1.0 - t
                    pt_t = (p0 * (omt * omt * omt) +
                            p1 * (3.0 * omt * omt * t) +
                            p2 * (3.0 * omt * t * t) +
                            p3 * (t * t * t))
                    curve_pts.append((pt_t.x, pt_t.y, pt_t.z))

                self._rubber_coords.point.setValues(0, len(curve_pts), curve_pts)
                if getattr(self, "_rubber_lines", None) is not None:
                    self._rubber_lines.numVertices.setValue(len(curve_pts))
            else:
                self._rubber_coords.point.setValues(
                    0, 2, [(last_p.x, last_p.y, last_p.z), (curr_pt.x, curr_pt.y, curr_pt.z)])
                if getattr(self, "_rubber_lines", None) is not None:
                    self._rubber_lines.numVertices.setValue(2)
            self._rubber_sw.whichChild = 0
        except Exception:
            pass

    def _remove_rubberband(self, view=None):
        if getattr(self, '_rubber_root', None) is None:
            return
        try:
            if view is None:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
            if view is not None:
                view.getSceneGraph().removeChild(self._rubber_root)
        except Exception:
            pass
        self._rubber_root = None
        self._rubber_sw = None
        self._rubber_coords = None

    def _ensure_pen_overlay(self, view):
        self._remove_pen_overlay(view)
        try:
            from pivy import coin
            root = coin.SoSeparator()
            self._pen_sw = coin.SoSwitch()
            self._pen_sw.whichChild = -1

            sep = coin.SoSeparator()

            # Dashed tangent whiskers
            line_sep = coin.SoSeparator()
            line_col = coin.SoBaseColor()
            line_col.rgb = (0.7, 0.75, 0.85)
            line_sep.addChild(line_col)
            line_style = coin.SoDrawStyle()
            line_style.lineWidth = 1
            line_style.linePattern = 0x00FF
            line_sep.addChild(line_style)
            self._pen_line_coords = coin.SoCoordinate3()
            self._pen_lines = coin.SoLineSet()
            line_sep.addChild(self._pen_line_coords)
            line_sep.addChild(self._pen_lines)
            sep.addChild(line_sep)

            # Left handle diamond marker (Cyan)
            left_sep = coin.SoSeparator()
            left_col = coin.SoBaseColor()
            left_col.rgb = (0.1, 0.85, 1.0)
            left_sep.addChild(left_col)
            self._pen_left_coord = coin.SoCoordinate3()
            self._pen_left_mk = coin.SoMarkerSet()
            try:
                self._pen_left_mk.markerIndex = coin.SoMarkerSet.DIAMOND_FILLED_7_7
            except Exception:
                self._pen_left_mk.markerIndex = coin.SoMarkerSet.SQUARE
            left_sep.addChild(self._pen_left_coord)
            left_sep.addChild(self._pen_left_mk)
            sep.addChild(left_sep)

            # Right handle diamond marker (Emerald)
            right_sep = coin.SoSeparator()
            right_col = coin.SoBaseColor()
            right_col.rgb = (0.2, 1.0, 0.5)
            right_sep.addChild(right_col)
            self._pen_right_coord = coin.SoCoordinate3()
            self._pen_right_mk = coin.SoMarkerSet()
            try:
                self._pen_right_mk.markerIndex = coin.SoMarkerSet.DIAMOND_FILLED_7_7
            except Exception:
                self._pen_right_mk.markerIndex = coin.SoMarkerSet.SQUARE
            right_sep.addChild(self._pen_right_coord)
            right_sep.addChild(self._pen_right_mk)
            sep.addChild(right_sep)

            # Central anchor marker (White diamond)
            anchor_sep = coin.SoSeparator()
            anchor_col = coin.SoBaseColor()
            anchor_col.rgb = (1.0, 1.0, 1.0)
            anchor_sep.addChild(anchor_col)
            self._pen_anchor_coord = coin.SoCoordinate3()
            self._pen_anchor_mk = coin.SoMarkerSet()
            try:
                self._pen_anchor_mk.markerIndex = coin.SoMarkerSet.DIAMOND_FILLED_7_7
            except Exception:
                self._pen_anchor_mk.markerIndex = coin.SoMarkerSet.SQUARE
            anchor_sep.addChild(self._pen_anchor_coord)
            anchor_sep.addChild(self._pen_anchor_mk)
            sep.addChild(anchor_sep)

            self._pen_sw.addChild(sep)
            root.addChild(self._pen_sw)
            view.getSceneGraph().addChild(root)
            self._pen_overlay_root = root
        except Exception:
            self._pen_overlay_root = None
            self._pen_sw = None

    def _update_pen_overlay(self, view, anchor, left_h, right_h):
        try:
            if getattr(self, "_pen_sw", None) is None:
                if view:
                    self._ensure_pen_overlay(view)
            if getattr(self, "_pen_sw", None) is None:
                return

            pts = [
                (left_h.x, left_h.y, left_h.z),
                (anchor.x, anchor.y, anchor.z),
                (anchor.x, anchor.y, anchor.z),
                (right_h.x, right_h.y, right_h.z),
            ]
            self._pen_line_coords.point.setValues(0, 4, pts)
            self._pen_lines.numVertices.setValues(0, 2, [2, 2])

            self._pen_left_coord.point.set1Value(0, left_h.x, left_h.y, left_h.z)
            self._pen_left_mk.numPoints = 1

            self._pen_right_coord.point.set1Value(0, right_h.x, right_h.y, right_h.z)
            self._pen_right_mk.numPoints = 1

            self._pen_anchor_coord.point.set1Value(0, anchor.x, anchor.y, anchor.z)
            self._pen_anchor_mk.numPoints = 1

            self._pen_sw.whichChild = 0
        except Exception:
            pass

    def _hide_pen_overlay(self):
        if getattr(self, "_pen_sw", None) is not None:
            try:
                self._pen_sw.whichChild = -1
            except Exception:
                pass

    def _remove_pen_overlay(self, view=None):
        if getattr(self, "_pen_overlay_root", None) is None:
            return
        try:
            if view is None:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
            if view is not None:
                view.getSceneGraph().removeChild(self._pen_overlay_root)
        except Exception:
            pass
        self._pen_overlay_root = None
        self._pen_sw = None
        self._pen_line_coords = None
        self._pen_lines = None
        self._pen_left_coord = None
        self._pen_left_mk = None
        self._pen_right_coord = None
        self._pen_right_mk = None
        self._pen_anchor_coord = None
        self._pen_anchor_mk = None

    def _project_cursor(self, view, pos):
        """Compute the 3D position from screen pos:
        1. If magnet snapped to existing geometry, use snap point.
        2. Else intersect camera ray with the active construction plane.
        3. If axis lock ('X'/'Y'/'Z') is active and points exist, apply axis lock.
        """
        from FreeCAD import Vector
        from rfstudio import panel_logic
        # Planar Mode OFF -> free-form 3D: use raw view depth, no plane projection.
        if not getattr(self, "_planar_mode", True):
            return view.getPoint(pos[0], pos[1])
        pt = None
        if getattr(self, "_snap_pt", None) is not None:
            pt = self._snap_pt
        else:
            ray = snaputil.pick_ray(view, pos)
            if ray is not None:
                ro, rd = ray
                vd = getattr(view, "getViewDirection", lambda: (0.0, 0.0, -1.0))()
                view_dir = (vd[0], vd[1], vd[2]) if hasattr(vd, "__getitem__") else (0.0, 0.0, -1.0)
                norm = panel_logic.plane_normal_for(self._plane_family, view_dir)
                origin, normal = panel_logic.construction_plane((0.0, 0.0, 0.0), norm, offset=0.0)
                hit = panel_logic.ray_plane_intersect(
                    (ro.x, ro.y, ro.z), (rd.x, rd.y, rd.z), origin, normal)
                if hit is not None:
                    pt = Vector(*hit)
            if pt is None:
                pt = view.getPoint(pos[0], pos[1])

        if pt is not None and getattr(self, "_axis_lock", None) is not None:
            pts = self._points()
            if pts:
                last_p = pts[-1]
                locked = panel_logic.apply_axis_lock(
                    (pt.x, pt.y, pt.z), (last_p.x, last_p.y, last_p.z), self._axis_lock)
                pt = Vector(*locked)
        return pt

    def _on_move_pick(self, ev):
        """Track cursor during pick-mode; snap, rubberband preview, and HUD status."""
        if not self._picking and not (getattr(self, 'pick_btn', None) and self.pick_btn.isChecked()):
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
            if getattr(self, "_snap_kinded", None) is None:
                self._snap_kinded = snaputil.collect_strong_snap_points_kinded(doc, exclude=self.obj)
            best, best_kind = snaputil.nearest_snap_ranked_kind(
                view, self._snap_kinded, getattr(self, "_snap_shapes", []),
                pos, snap_px)
            self._apply_snap_visual(best, best_kind)

            # Illustrator Pen Tool Drag Gesture
            if getattr(self, "_pen_drag", False) and self.kind == "BezierCurve":
                dx = pos[0] - self._pen_start_pos[0]
                dy = pos[1] - self._pen_start_pos[1]
                if (dx * dx + dy * dy) > 16:
                    self._pen_dragged = True

                curr_pt = self._project_cursor(view, pos)
                if curr_pt is not None:
                    p_anchor = self._pen_anchor_pt
                    idx = self._pen_anchor_idx
                    v_handle = curr_pt - p_anchor
                    tension = v_handle.Length

                    if self._plane_family == "XY":
                        angle = math.degrees(math.atan2(v_handle.y, v_handle.x))
                    elif self._plane_family == "YZ":
                        angle = math.degrees(math.atan2(v_handle.z, v_handle.y))
                    elif self._plane_family == "ZX":
                        angle = math.degrees(math.atan2(v_handle.x, v_handle.z))
                    else:
                        angle = 0.0

                    # Endpoint continuity snapping to adjacent edges
                    c_mode = getattr(self, "_endpoint_continuity_mode", "G1")
                    endpoint_snapped_tag = ""
                    is_endpoint = (idx == 0 or idx == len(self._points()) - 1)
                    if is_endpoint and c_mode != "Free" and hasattr(self, "_snap_shapes") and self._snap_shapes:
                        best_pt, best_d, best_shape = snaputil.nearest_point_on_shapes(p_anchor, self._snap_shapes, max_dist=2.0)
                        if best_shape is not None:
                            info = core.evaluate_edge_at_point(best_shape, p_anchor)
                            tan_e = info['tangent']
                            if v_handle.Length > 1e-6:
                                dot_val = v_handle.normalize().dot(tan_e)
                                angle_to_tan = math.degrees(math.acos(min(1.0, max(-1.0, abs(dot_val)))))
                                if angle_to_tan < 25.0:
                                    snap_res = core.snap_endpoint_continuity(p_anchor, curr_pt, best_shape, continuity=c_mode)
                                    curr_pt = snap_res['p_handle']
                                    v_handle = curr_pt - p_anchor
                                    tension = v_handle.Length
                                    endpoint_snapped_tag = f" <span style='color:#e066ff;font-weight:bold;'>[{c_mode} Edge Snap]</span>"

                    alt_held = self._alt_held()
                    h_right = curr_pt
                    h_left = p_anchor if alt_held else (p_anchor - v_handle)
                    htype = "Free" if alt_held else "Aligned"

                    lh = list(self.obj.LeftHandles)
                    rh = list(self.obj.RightHandles)
                    htypes = list(self.obj.HandleTypes)

                    if 0 <= idx < len(lh) and 0 <= idx < len(rh):
                        rh[idx] = h_right
                        if not alt_held or (lh[idx] == p_anchor):
                            lh[idx] = h_left
                        if 0 <= idx < len(htypes):
                            htypes[idx] = htype

                        self.obj.LeftHandles = lh
                        self.obj.RightHandles = rh
                        self.obj.HandleTypes = htypes

                    self._update_pen_overlay(view, p_anchor, lh[idx], rh[idx])
                    self.recompute()

                    # On-cursor HUD and status readout
                    hud = self._ensure_cursor_hud()
                    alt_badge = " <span style='color:#ffa500;font-weight:bold;'>[Alt: Cusp]</span>" if alt_held else " <span style='color:#88a0b8;'>(Alt: Cusp)</span>"
                    text = f"<b>Tension:</b> {tension:.2f} mm &nbsp;|&nbsp; <b>∠</b> {angle:.1f}° &nbsp;|&nbsp; <b>{htype}</b>{alt_badge}{endpoint_snapped_tag}"
                    hud.setText(text)
                    hud.adjustSize()
                    cpos = QtGui.QCursor.pos()
                    hud.move(cpos.x() + 18, cpos.y() - 30)
                    if not hud.isVisible():
                        hud.show()

                    from rfstudio import statusbar
                    statusbar.show("Pen Handle Drag: Tension %.2f mm | ∠%.1f° | %s%s | Release to commit"
                                   % (tension, angle, htype, " [CUSP]" if alt_held else " (Hold Alt for cusp)"))
                    return

            pt = self._project_cursor(view, pos)
            self._last_cursor_pt = pt
            self._update_rubberband(view, pt)

            # Live HUD readout
            pts = self._points()
            from rfstudio import statusbar
            snap_tag = ""
            if best is not None:
                if best_kind == snaputil.SNAP_INTERSECTION:
                    snap_tag = " | Crossing: Rail Intersection [Amber]"
                elif best_kind == snaputil.SNAP_EDGE:
                    snap_tag = " | Rail Contact: On-curve [Cyan]"
                elif best_kind:
                    snap_tag = " | Snap: %s [Green]" % best_kind.capitalize()

            if pts and pt is not None:
                p_last = pts[-1]
                dx = pt.x - p_last.x
                dy = pt.y - p_last.y
                dz = pt.z - p_last.z
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if self._plane_family == "XY":
                    angle = math.degrees(math.atan2(dy, dx))
                elif self._plane_family == "YZ":
                    angle = math.degrees(math.atan2(dz, dy))
                elif self._plane_family == "ZX":
                    angle = math.degrees(math.atan2(dx, dz))
                else:
                    angle = 0.0
                lock_s = "[Free]" if self._axis_lock is None else "[%s-LOCKED]" % self._axis_lock
                msg = ("L: %.2f mm | ∠%.1f° | Δ(%.2f, %.2f, %.2f) | Plane: %s (Tab) | %s%s | Enter/DblClick to finish, Backspace to undo"
                       % (dist, angle, dx, dy, dz, self._plane_family, lock_s, snap_tag))
                statusbar.show(msg)
                self._update_cursor_hud(dist, angle, dx, dy, dz, snap_tag)
            elif pt is not None:
                lock_s = "[Free]" if self._axis_lock is None else "[%s-LOCKED]" % self._axis_lock
                msg = ("Click to place point 1 | Plane: %s (Tab to cycle) | %s%s | Esc to cancel"
                       % (self._plane_family, lock_s, snap_tag))
                statusbar.show(msg)
                self._update_cursor_hud_point1(snap_tag)
        except Exception:
            pass

    def _on_click(self, ev):
        if ev.get("Type") != "SoMouseButtonEvent":
            return
        if ev.get("Button") != "BUTTON1":
            return
        state = ev.get("State")
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            if view is None:
                return
            pos = ev["Position"]

            if state == "DOWN":
                # Double-click detection: finishes the curve if >= 2 points
                import time
                now = time.time()
                dt = now - getattr(self, "_last_click_time", 0.0)
                last_pos = getattr(self, "_last_click_pos", (0, 0))
                self._last_click_time = now
                self._last_click_pos = pos
                if dt < 0.35 and abs(pos[0] - last_pos[0]) < 8 and abs(pos[1] - last_pos[1]) < 8:
                    if len(self._points()) >= 2:
                        self.accept()
                        return

                pt = self._project_cursor(view, pos)
                if pt is None:
                    return

                self.doc.commitTransaction()
                self._pick_count += 1
                self.doc.openTransaction("Draw %s" % self.kind)
                self._append_point(pt)
                pts = self._points()

                if self.kind == "BezierCurve":
                    idx = len(pts) - 1
                    lh = list(getattr(self.obj, "LeftHandles", []))
                    rh = list(getattr(self.obj, "RightHandles", []))
                    htypes = list(getattr(self.obj, "HandleTypes", []))

                    while len(lh) < len(pts):
                        lh.append(pts[len(lh)])
                    while len(rh) < len(pts):
                        rh.append(pts[len(rh)])
                    while len(htypes) < len(pts):
                        htypes.append("Aligned")

                    lh[idx] = pt
                    rh[idx] = pt
                    htypes[idx] = "Aligned"

                    self.obj.LeftHandles = lh
                    self.obj.RightHandles = rh
                    self.obj.HandleTypes = htypes

                    self._pen_drag = True
                    self._pen_anchor_pt = pt
                    self._pen_anchor_idx = idx
                    self._pen_start_pos = pos
                    self._pen_dragged = False

                    self._snap_cands = self._collect_snap_cands()
                    self._snap_shapes = self._collect_snap_shapes()
                    self._update_pen_overlay(view, pt, pt, pt)
                else:
                    self._snap_cands = self._collect_snap_cands()
                    self._snap_shapes = self._collect_snap_shapes()
                    self._update_rubberband(view, pt)

            elif state == "UP":
                if getattr(self, "_pen_drag", False):
                    self._pen_drag = False
                    if not getattr(self, "_pen_dragged", False):
                        # Simple click without drag = sharp corner (cusp)
                        idx = getattr(self, "_pen_anchor_idx", None)
                        if idx is not None and self.kind == "BezierCurve":
                            htypes = list(getattr(self.obj, "HandleTypes", []))
                            if 0 <= idx < len(htypes):
                                htypes[idx] = "Free"
                                self.obj.HandleTypes = htypes
                    self._hide_pen_overlay()
                    self.recompute()
                    self._fill_table()
                    self._update_rubberband(view, getattr(self, "_last_cursor_pt", None))
        except Exception:
            pass

    def _on_key(self, ev):
        """Keyboard shortcuts during curve authoring:
        - Tab: cycle construction plane family
        - X/Y/Z: toggle axis lock through previous point
        - Backspace / Delete / Ctrl+Z: undo last placed point
        - Enter / Space: commit curve
        - Esc: cancel/reject
        - V: cycle Bezier handle type
        - 0-9, ., D: direct numeric distance/coordinate entry
        """
        try:
            if ev.get("Type") != "SoKeyboardEvent":
                return
            key = ev.get("Key")
            if ev.get("State") == "DOWN":
                if key == 17:  # Ctrl key
                    self._ctrl = True
                    return
                if getattr(self, "_ctrl", False) and key in (90, 122):
                    self._undo_last_point()
                    return

                from rfstudio import panel_logic, statusbar
                if key == 9:  # Tab: cycle plane
                    self._set_plane_family(panel_logic.cycle_plane(self._plane_family))
                    statusbar.flash("Plane: %s (Tab to cycle)" % self._plane_family)
                elif key in (88, 120):  # X / x
                    self._axis_lock = None if self._axis_lock == "X" else "X"
                    statusbar.flash("Axis lock: %s" % (self._axis_lock or "Free"))
                elif key in (89, 121):  # Y / y
                    self._axis_lock = None if self._axis_lock == "Y" else "Y"
                    statusbar.flash("Axis lock: %s" % (self._axis_lock or "Free"))
                elif key in (90, 122):  # Z / z
                    self._axis_lock = None if self._axis_lock == "Z" else "Z"
                    statusbar.flash("Axis lock: %s" % (self._axis_lock or "Free"))
                elif key in (8, 127):  # Backspace / Delete
                    self._undo_last_point()
                elif key in (13, 32):  # Enter / Space
                    if len(self._points()) >= 2:
                        self.accept()
                elif key == 27:  # Esc
                    self.reject()
                elif key in (86, 118) and self.kind == "BezierCurve":  # V / v
                    self._cycle_bezier_handle_type()
                elif key in (71, 103) and self.kind == "BezierCurve":  # G / g
                    self._cycle_endpoint_continuity_mode()
                elif (48 <= key <= 57) or key in (46, 68, 100):  # 0-9, ., D/d
                    self._show_numeric_input(key)
            elif ev.get("State") == "UP":
                if key == 17:
                    self._ctrl = False
        except Exception:
            pass

    def _alt_held(self):
        try:
            from PySide import QtCore, QtGui
            return bool(QtGui.QGuiApplication.keyboardModifiers() & QtCore.Qt.AltModifier)
        except Exception:
            return False

    def _shift_held(self):
        try:
            from PySide import QtCore, QtGui
            return bool(QtGui.QGuiApplication.keyboardModifiers() & QtCore.Qt.ShiftModifier)
        except Exception:
            return False

    def _undo_last_point(self):
        pts = self._points()
        if pts:
            pts.pop()
            if hasattr(self, "_weights") and len(self._weights) > len(pts):
                self._weights.pop()
            self._set_points(pts)
            self._fill_table()
            from rfstudio import statusbar
            statusbar.flash("Removed point (%d remaining)" % len(pts))
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                self._update_rubberband(view, getattr(self, "_last_cursor_pt", None))

    def _cycle_endpoint_continuity_mode(self):
        modes = ["G1", "G2", "G3", "Free"]
        cur = getattr(self, "_endpoint_continuity_mode", "G1")
        nxt = modes[(modes.index(cur) + 1) % len(modes)]
        self._endpoint_continuity_mode = nxt
        from rfstudio import statusbar
        statusbar.flash("Endpoint Continuity Snapping: %s (G to cycle)" % nxt)

    def _cycle_bezier_handle_type(self):
        pts = self._points()
        if not pts:
            return
        idx = len(pts) - 1
        cycle = ["Auto", "Aligned", "Mirrored", "Vector", "Free"]
        htypes = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(pts)))
        cur = htypes[idx] if idx < len(htypes) else "Auto"
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "Aligned"
        if getattr(self, "_editor", None) and hasattr(self._editor, "set_bezier_handle_type"):
            self._editor.set_bezier_handle_type(idx, nxt)
        else:
            if idx < len(htypes):
                htypes[idx] = nxt
                self.obj.HandleTypes = htypes
        if getattr(self, "handle_type_combo", None):
            self.handle_type_combo.blockSignals(True)
            self.handle_type_combo.setCurrentText(nxt)
            self.handle_type_combo.blockSignals(False)
        from rfstudio import statusbar
        statusbar.flash("Point %d: Handle Type '%s' (V to cycle)" % (idx + 1, nxt))

    def _show_numeric_input(self, initial_key=None):
        """Floating input lineedit near the cursor for direct length or coordinate typing."""
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            if getattr(self, "_num_input", None) is None:
                self._num_input = QtWidgets.QLineEdit()
                self._num_input.setPlaceholderText("Length (e.g. 50) or X, Y, Z")
                self._num_input.setFixedWidth(160)
                self._num_input.setStyleSheet(
                    "QLineEdit { background: rgba(30, 35, 45, 230); color: #fff; "
                    "border: 1px solid #3085d6; border-radius: 4px; padding: 3px; font-weight: bold; }")
                self._num_input.returnPressed.connect(self._on_numeric_return)

            init_char = chr(initial_key) if initial_key and 32 <= initial_key <= 126 and chr(initial_key).lower() != 'd' else ""
            self._num_input.setText(init_char)
            view_w = view.getWidget()
            if view_w:
                cursor_pos = QtGui.QCursor.pos()
                self._num_input.move(cursor_pos.x() + 15, cursor_pos.y() + 15)
                self._num_input.show()
                self._num_input.setFocus()
        except Exception:
            pass

    def _hide_numeric_input(self):
        if getattr(self, "_num_input", None) is not None:
            try:
                self._num_input.hide()
            except Exception:
                pass

    def _on_numeric_return(self):
        """Parse typed numeric input: either distance 'L' or 'X, Y, Z'."""
        try:
            if self._num_input is None:
                return
            text = self._num_input.text().strip()
            self._hide_numeric_input()
            if not text:
                return
            from FreeCAD import Vector
            from rfstudio import statusbar
            pts = self._points()
            if "," in text:
                parts = [float(p.strip()) for p in text.split(",")]
                if len(parts) == 3:
                    pt = Vector(parts[0], parts[1], parts[2])
                    self.doc.commitTransaction()
                    self._pick_count += 1
                    self.doc.openTransaction("Draw %s" % self.kind)
                    self._append_point(pt)
                    statusbar.flash("Placed point at (%g, %g, %g)" % (pt.x, pt.y, pt.z))
            else:
                dist = float(text)
                if pts and getattr(self, "_last_cursor_pt", None) is not None:
                    p_last = pts[-1]
                    cur = self._last_cursor_pt
                    d_vec = cur - p_last
                    d_len = d_vec.Length
                    d_norm = d_vec * (1.0 / d_len) if d_len > 1e-6 else Vector(1, 0, 0)
                    pt = p_last + d_norm * dist
                    self.doc.commitTransaction()
                    self._pick_count += 1
                    self.doc.openTransaction("Draw %s" % self.kind)
                    self._append_point(pt)
                    statusbar.flash("Placed point at L = %.2f mm" % dist)
        except Exception as exc:
            from rfstudio import statusbar
            statusbar.flash("Invalid input: %s" % exc, error=True)

    # ---- settings + recompute ---- #
    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self, gui=True):
        if getattr(self, "live", None) is None or self.live.isChecked():
            self._recompute_obj()
            if gui:
                self._defer_gui_update()

    # ---- commit/abort hooks (see _EditPanelBase) ---- #
    def _pre_accept(self):
        self._toggle_canvas_drawing(False)
        self._hide_cursor_hud()
        self._hide_numeric_input()
        self._flush_pending_transaction(commit=True)

    def _pre_reject(self):
        self._toggle_canvas_drawing(False)
        self._hide_cursor_hud()
        self._hide_numeric_input()
        self._flush_pending_transaction(commit=False)

    def _flush_pending_transaction(self, commit):
        """_on_click/_on_draw_click always leave exactly one transaction open
        after the last pick — commit() closes previous + opens new each time,
        so nothing ever closes the FINAL one. Left open, it isn't in the undo
        stack and isn't durably committed until some *later* command's own
        commitTransaction() call happens to flush it (the 'works the second
        time' symptom). Close it here, one way or the other, before the panel
        exits."""
        if self._pick_count > 0:
            try:
                if commit:
                    self.doc.commitTransaction()
                else:
                    self.doc.abortTransaction()
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    "Railform: could not close pick transaction (%s)\n" % exc)

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
                    self._recompute_obj()
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
        self._recompute_obj()
        self._defer_gui_update()

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