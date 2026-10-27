# -*- coding: utf-8 -*-
"""Surface-building/manufacturability task panels: SurfacePanel (Loft/Gordon/
Coons/Fill/Match/Sweep), the purpose-built LoftPanel, the
Offset/Thicken/Shell/BlendSurf manufacturability panels, and
StitchShellPanel (sew a finished patch network into one shell/solid).
Split out of the old taskpanels.py monolith -- see rfstudio.panel_base for
the shared _EditPanelBase scaffolding, selection helpers, and _CurveGroup
widget this module builds on.

GUI-only. The geometry it drives lives in rfstudio.core (headless-tested).
"""

import FreeCAD
import FreeCADGui

from PySide import QtCore, QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio import parametric
from rfstudio import panel_logic
from rfstudio.panel_base import (
    _EditPanelBase, _CurveGroup, _selected_objects, _notify_overlay_unavailable,
    _CollapsibleGroup, _ShortcutHintBar, drop_callbacks, _status_label,
)
from rfstudio.panel_lawcurve import LawCurveWidget


class SurfacePanel(_EditPanelBase):

    def __init__(self, kind=None, edit_obj=None):
        self.kind = kind or edit_obj.Proxy.Kind
        self._start_edit(edit_obj,
                          ("Create " if edit_obj is None else "Edit ") + self.kind,
                          lambda: parametric.make_surface(self.kind, self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – %s" % self.kind)
        self._lay = QtWidgets.QVBoxLayout(self.form)
        self.groups = []
        self._build()

        # Add surface type converter controls
        self._add_converter_controls()

        if self.creating:
            self._prefill_from_selection()
        self.recompute()

    # ---- UI ------------------------------------------------------------- #
    def _build(self):
        lim_map = parametric.LIMITS.get(self.kind, {})
        for prop, title, hint in parametric.GROUPS[self.kind]:
            max_items = lim_map.get(prop, (None, None))[1]
            # SweepN's Rails list is order-sensitive (rail 0 and rail N-1
            # bound the sweep; the spine fallback walks the rails in list
            # order), so expose the reorder buttons for it specifically.
            # Other link lists don't care about order, and the extra
            # buttons would just invite meaningless reordering.
            draggable = (self.kind == "SweepN" and prop == "Rails")
            g = _CurveGroup(self, prop, title, hint, max_items=max_items,
                            draggable=draggable)
            self.groups.append(g)
            self._lay.addWidget(g.box)

        # Gordon only: cross-transfer strip between U and V lists, like in
        # the Curves workbench. Nothing else -- the surface algorithm does
        # all the orientation/wrapping work itself.
        if self.kind == "Gordon" and len(self.groups) == 2:
            xrow = QtWidgets.QHBoxLayout()
            for label, src_i, dst_i in (("↓ to V", 0, 1), ("↑ to U", 1, 0)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(
                    lambda _=False, s=src_i, d=dst_i: self._xfer(s, d))
                xrow.addWidget(b)
            self._lay.insertLayout(1, xrow)

        self._scalar_controls()

        # Live status line for EVERY kind, not just Gordon. The Gordon
        # panel originally had a bespoke status label because Gordon
        # silently does nothing when one curve family has < 2 rails -- but
        # Coons/Fill/Match/Sweep have the exact same failure mode (LIMITS
        # below isn't satisfied -> execute() returns early -> no shape, no
        # feedback). _update_status() below is the generalized version,
        # driven by parametric.LIMITS, so the "nothing happened" problem
        # gets the same visible diagnostic everywhere.
        self._status = QtWidgets.QLabel("")
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        self._status.setWordWrap(True)
        self._lay.addWidget(self._status)
        # back-compat alias: some call sites still refer to _gordon_status
        self._gordon_status = self._status if self.kind == "Gordon" else None

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        self._lay.addWidget(self.live)

        # Consumed input curves (Profiles/Boundaries/U+V curves/...) are
        # auto-hidden once a surface successfully builds (see
        # groups.apply_input_visibility) so tens of raw rails don't stay
        # cluttering the tree/viewport next to the finished patches. This
        # is the single toggle to bring them back when the user actually
        # needs to select/edit one.
        if hasattr(self.obj, "ShowInputs"):
            self.show_inputs = QtWidgets.QCheckBox("Show input curves")
            self.show_inputs.setChecked(bool(self.obj.ShowInputs))
            self.show_inputs.toggled.connect(
                lambda v: self._set("ShowInputs", v))
            self._lay.addWidget(self.show_inputs)
        else:
            self.show_inputs = None

        self._update_status()

        # Match only: surface WHICH match algorithm actually fired
        # (exact_g1 vs sampled) and, for exact_g1, that the target's own
        # control net near the shared edge was directly overwritten --
        # match_surface_exact_g1's docstring is honest about this
        # mutation, but until now nothing in the UI told the user it had
        # happened. Cheap enough to recompute here for display (same
        # match_surface_to_references() call MatchSurfaceFP.execute()
        # already made to build obj.Shape).
        if self.kind == "Match":
            self._match_diag = QtWidgets.QLabel("")
            self._match_diag.setWordWrap(True)
            self._match_diag.setStyleSheet(
                "font-size:11px;padding:2px 0;")
            self._lay.addWidget(self._match_diag)
            self._update_match_diagnostics()

        # MirrorGroup only: per-source weld outcome. _LastWeldOK is a
        # plain runtime list attribute set by MirrorGroupFP.execute()
        # (not an addProperty -- session-only), index-aligned with
        # Sources as of the last recompute.
        if self.kind == "MirrorGroup":
            self._mirror_weld_diag = QtWidgets.QLabel("")
            self._mirror_weld_diag.setWordWrap(True)
            self._mirror_weld_diag.setStyleSheet(
                "font-size:11px;padding:2px 0;")
            self._lay.addWidget(self._mirror_weld_diag)
            self._update_mirror_weld_diagnostics()

        # Gordon only: click a row in either list to flash the matching
        # curve in the viewport. Selection-driven, no preview grid, no
        # auto-orient -- just a highlight overlay on the document object
        # corresponding to the row you clicked.
        if self.kind == "Gordon" and len(self.groups) == 2:
            self._setup_gordon_curve_highlight()
            self._color_gordon_family_rows()
            # Pick-in-3D toggle: opt-in mode that turns viewport clicks into
            # row selections. Off by default so normal FreeCAD selection
            # keeps working while the panel is open.
            self._gordon_pick_btn = QtWidgets.QPushButton(
                "Pick curves in 3D: off")
            self._gordon_pick_btn.setCheckable(True)
            self._gordon_pick_btn.toggled.connect(self._toggle_gordon_pick)
            self._lay.addWidget(self._gordon_pick_btn)

    def _scalar_controls(self):
        obj = self.obj
        link_props = {p for p, _, _ in parametric.GROUPS[self.kind]}
        
        # Match: wrap Samples/Scale in collapsible Advanced group
        if self.kind == "Match":
            self.adv_match = _CollapsibleGroup("Advanced: samples / scale", collapsed=True)
            for name in ("Samples", "Scale"):
                if name in obj.PropertiesList and obj.getGroupOfProperty(name) == self.kind:
                    tid = obj.getTypeIdOfProperty(name)
                    if tid == "App::PropertyInteger":
                        self._add_int(name)
                    elif tid == "App::PropertyFloat":
                        self._add_float(name)
                    # Move the created widgets into the collapsible group
                    # (they were added via _row -> _lay, so we need to move them)
            # This is complex - let's use a simpler approach
            # Instead, add the controls directly in the group
            self.adv_match.content_layout.addWidget(QtWidgets.QLabel("Samples"))
            self.samples = QtWidgets.QSpinBox()
            self.samples.setRange(4, 200)
            self.samples.setValue(int(getattr(obj, "Samples", 24)))
            self.samples.valueChanged.connect(lambda v: self._set("Samples", v))
            self.adv_match.content_layout.addWidget(self.samples)

            self.adv_match.content_layout.addWidget(QtWidgets.QLabel("Scale"))
            self.scale = QtWidgets.QDoubleSpinBox()
            self.scale.setRange(0.1, 5.0)
            self.scale.setSingleStep(0.1)
            self.scale.setValue(float(getattr(obj, "Scale", 1.0)))
            self.adv_match.content_layout.addWidget(self.scale)
            self.scale.valueChanged.connect(lambda v: self._set("Scale", v))
            self._lay.addWidget(self.adv_match)
            
            # Skip Samples/Scale in the generic loop
            skip_match = {"Samples", "Scale"}
        else:
            skip_match = set()

        for name in obj.PropertiesList:
            if name in skip_match:
                continue
            if obj.getGroupOfProperty(name) != self.kind or name in link_props:
                continue
            tid = obj.getTypeIdOfProperty(name)
            if tid == "App::PropertyEnumeration":
                self._add_enum(name)
            elif tid == "App::PropertyBool":
                self._add_bool(name)
            elif tid in ("App::PropertyInteger", "App::PropertyIntegerConstraint"):
                self._add_int(name)
            elif tid == "App::PropertyFloat":
                self._add_float(name)
            elif tid == "App::PropertyVector":
                self._add_vector(name)

    def _row(self, label, widget):
        r = QtWidgets.QHBoxLayout()
        r.addWidget(QtWidgets.QLabel(label))
        r.addStretch(1)
        r.addWidget(widget)
        self._lay.addLayout(r)

    def _add_enum(self, name):
        cb = QtWidgets.QComboBox()
        cb.addItems(self.obj.getEnumerationsOfProperty(name))
        cb.setCurrentText(str(getattr(self.obj, name)))
        cb.currentTextChanged.connect(
            lambda v, n=name: self._set(n, v))
        self._row(name, cb)

    def _add_bool(self, name):
        cb = QtWidgets.QCheckBox()
        cb.setChecked(bool(getattr(self.obj, name)))
        cb.toggled.connect(lambda v, n=name: self._set(n, v))
        self._row(name, cb)

    def _add_int(self, name):
        sb = QtWidgets.QSpinBox()
        sb.setRange(1, 8 if name == "MaxDegree" else 1000)
        sb.setValue(int(getattr(self.obj, name)))
        sb.valueChanged.connect(lambda v, n=name: self._set(n, v))
        self._row(name, sb)

    def _add_float(self, name):
        sb = QtWidgets.QDoubleSpinBox()
        sb.setDecimals(3)
        sb.setRange(-1e6, 1e6)
        sb.setSingleStep(0.1)
        sb.setValue(float(getattr(self.obj, name)))
        sb.valueChanged.connect(lambda v, n=name: self._set(n, v))
        self._row(name, sb)

    def _add_vector(self, name):
        vec = getattr(self.obj, name)
        spins = []
        for val in (vec.x, vec.y, vec.z):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(3)
            sb.setRange(-1e6, 1e6)
            sb.setSingleStep(0.1)
            sb.setValue(val)
            spins.append(sb)

        def _commit(*_a, n=name, sp=spins):
            self._set(n, FreeCAD.Vector(sp[0].value(), sp[1].value(), sp[2].value()))
        for sb in spins:
            sb.valueChanged.connect(_commit)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(name))
        row.addStretch(1)
        for sb in spins:
            row.addWidget(sb)
        self._lay.addLayout(row)

    def _xfer(self, src_i, dst_i):
        """Move selected items from one group list to the other."""
        src, dst = self.groups[src_i], self.groups[dst_i]
        rows = sorted(
            (src.list.row(it) for it in src.list.selectedItems()), reverse=True)
        if not rows:
            return
        moved = [src.objs.pop(i) for i in rows if 0 <= i < len(src.objs)]
        for o in reversed(moved):
            if o not in dst.objs:
                dst.objs.append(o)
        src._refresh()
        dst._refresh()
        # update both properties then recompute once
        setattr(self.obj, src.prop, src.objs)
        setattr(self.obj, dst.prop, dst.objs)
        self.recompute()
        if getattr(self, "_gordon_status", None) is not None:
            self._update_gordon_status()


    # ---- behaviour ------------------------------------------------------ #
    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def _prefill_from_selection(self):
        sel = [o for o in _selected_objects() if o is not self.obj]
        if not sel or not self.groups:
            return
        g = self.groups[0]
        g.objs = list(sel)
        g._refresh()
        g._commit()

    def recompute(self, gui=True):
        if getattr(self, "live", None) is None or self.live.isChecked():
            self._recompute_obj()
            if gui:
                self._defer_gui_update()
        self._update_status()
        if self.kind == "Match" and getattr(self, "_match_diag", None) is not None:
            self._update_match_diagnostics()
        if self.kind == "MirrorGroup" and getattr(self, "_mirror_weld_diag", None) is not None:
            self._update_mirror_weld_diagnostics()

    def _update_mirror_weld_diagnostics(self):
        """MirrorGroup kind only: report how many of the current Sources
        actually got welded to their own mirror this recompute (only
        meaningful while WeldSeam is on)."""
        lbl = getattr(self, "_mirror_weld_diag", None)
        if lbl is None:
            return
        if not bool(getattr(self.obj, "WeldSeam", False)):
            lbl.setText("")
            return
        welds = getattr(self.obj, "_LastWeldOK", None)
        if not welds:
            lbl.setText("")
            return
        ok = sum(1 for w in welds if w)
        lbl.setText("%d/%d source(s) welded." % (ok, len(welds)))
        lbl.setStyleSheet(
            ("color:#7fae5a;" if ok == len(welds) else "color:#c08552;")
            + "font-size:11px;padding:2px 0;")

    def _update_match_diagnostics(self):
        """Match kind only: show which algorithm match_surface_to_references
        actually used (exact_g2 / exact_g1 vs sampled) and the achieved grade, and --
        when an exact path fired -- an explicit note that the target's control
        net was directly overwritten near the shared edge (see
        core.match_surface_exact_g1/_g2 docstrings), not just resampled."""
        lbl = getattr(self, "_match_diag", None)
        if lbl is None:
            return
        refs = [o for o in getattr(self.obj, "Reference", []) or [] if o]
        tgts = [o for o in getattr(self.obj, "Target", []) or [] if o]
        if not refs or not tgts:
            lbl.setText("")
            return
        tgt_shp = getattr(tgts[0], "Shape", None)
        if tgt_shp is None or not tgt_shp.Faces:
            lbl.setText("")
            return
        try:
            from rfstudio import core
            _rebuilt, report, chosen_ref = core.match_surface_to_references(
                tgt_shp.Faces[0], refs,
                continuity=str(getattr(self.obj, "Continuity", "G2")),
                n=int(getattr(self.obj, "Samples", 24)),
                scale=float(getattr(self.obj, "Scale", 1.0)))
            grade = core.classify_continuity(report)
            colors = {"G2": "#7fae5a", "G1": "#c08552", "G0": "#c08552",
                     "FAIL": "#c0392b"}
            method = report.get("method", "sampled")
            if method == "exact_g1":
                note = (" -- target's control net near the shared edge was "
                       "directly overwritten from the reference (2 rows: "
                       "exact G0/G1 copy, not a resampled fit).")
            elif method == "exact_g2":
                note = (" -- target's control net near the shared edge was "
                       "directly overwritten from the reference (3 rows: "
                       "exact G0/G1/G2 copy, not a resampled fit).")
            else:
                note = (" -- rebuilt by sampled Hermite fit (no control-"
                        "net rows copied).")
            lbl.setText(
                "Match method: <b>%s</b>, achieved: <b>%s</b> "
                "(max G0=%.4f) against '%s'.%s"
                % (method, grade, report["max_g0"],
                   getattr(chosen_ref, "Label", "?"), note))
            lbl.setStyleSheet(
                "color:%s;font-size:11px;padding:2px 0;"
                % colors.get(grade, "#9aa6b2"))
        except Exception as exc:
            lbl.setText("Match diagnostics unavailable: %s" % exc)
            lbl.setStyleSheet("color:#9aa6b2;font-size:11px;padding:2px 0;")

    def _update_status(self):
        """Live readiness diagnostic, generalized from the Gordon-only fix.

        Every SurfacePanel kind shares the same silent-failure shape:
        execute() bails out early (no shape, no error) when a LIMITS bound
        isn't met -- too few profiles, wrong boundary count, missing
        reference/target, etc. Previously only Gordon explained *why*
        nothing was drawn; this makes that explanation generic so Coons,
        Fill, Match and Sweep get the same feedback instead of a quietly
        empty viewport. The actual pass/fail logic lives in
        panel_logic.readiness (headlessly testable); this method only
        owns the widget update.
        """
        status = getattr(self, "_status", None)
        if status is None or not self.groups:
            return
        lim_map = parametric.LIMITS.get(self.kind, {})
        counts = {g.prop: len(g.objs) for g in self.groups}
        ok, msg = panel_logic.readiness(self.kind, counts, lim_map)
        status.setText(msg)
        status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")

    # Back-compat name -- the Gordon-specific code paths below still call
    # this; it now just delegates to the generic version.
    def _update_gordon_status(self):
        self._update_status()

    # ---- Gordon: row click → viewport highlight ------------------------- #
    def _setup_gordon_curve_highlight(self):
        """Thick orange line + endpoint dots on the curve whose row is
        currently selected in either U or V list. Single overlay, shared
        between the two list widgets. Torn down in `_end_edit()`."""
        try:
            from pivy import coin
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: Gordon highlight overlay unavailable (%s)\n" % exc)
            _notify_overlay_unavailable(self, "Row-click viewport highlight", exc)
            self._gordon_hl_root = None
            return
        root = coin.SoSeparator()
        # Curve polyline
        col_line = coin.SoBaseColor(); col_line.rgb = (1.0, 0.55, 0.10)
        style = coin.SoDrawStyle(); style.lineWidth = 5
        self._gordon_hl_line_coord = coin.SoCoordinate3()
        self._gordon_hl_line = coin.SoLineSet()
        self._gordon_hl_line.numVertices.setValue(0)
        self._gordon_hl_line_switch = coin.SoSwitch()
        self._gordon_hl_line_switch.whichChild = -1
        sep_line = coin.SoSeparator()
        sep_line.addChild(col_line)
        sep_line.addChild(style)
        sep_line.addChild(self._gordon_hl_line_coord)
        sep_line.addChild(self._gordon_hl_line)
        self._gordon_hl_line_switch.addChild(sep_line)
        root.addChild(self._gordon_hl_line_switch)
        # Endpoint markers
        col_mk = coin.SoBaseColor(); col_mk.rgb = (1.0, 0.55, 0.10)
        self._gordon_hl_mk_coord = coin.SoCoordinate3()
        self._gordon_hl_mk = coin.SoMarkerSet()
        try:
            self._gordon_hl_mk.markerIndex = coin.SoMarkerSet.CIRCLE_FILLED_9_9
        except Exception:
            pass
        self._gordon_hl_mk.numPoints = 0
        self._gordon_hl_mk_switch = coin.SoSwitch()
        self._gordon_hl_mk_switch.whichChild = -1
        sep_mk = coin.SoSeparator()
        sep_mk.addChild(col_mk)
        sep_mk.addChild(self._gordon_hl_mk_coord)
        sep_mk.addChild(self._gordon_hl_mk)
        self._gordon_hl_mk_switch.addChild(sep_mk)
        root.addChild(self._gordon_hl_mk_switch)
        try:
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: Gordon highlight overlay unavailable (%s)\n" % exc)
            _notify_overlay_unavailable(self, "Row-click viewport highlight", exc)
            self._gordon_hl_root = None
            return
        self._gordon_hl_root = root
        # Hook both list widgets
        for g in self.groups:
            g.list.itemSelectionChanged.connect(
                self._on_gordon_row_selected)

    def _on_gordon_row_selected(self):
        """Highlight the curve corresponding to the first selected row
        across both U and V lists. If nothing is selected, clear."""
        if getattr(self, "_gordon_hl_root", None) is None:
            return
        obj = None
        for g in self.groups:
            for it in g.list.selectedItems():
                row = g.list.row(it)
                if 0 <= row < len(g.objs):
                    obj = g.objs[row]
                    break
            if obj is not None:
                break
        self._set_gordon_hl_curve(obj)

    def _set_gordon_hl_curve(self, obj):
        """Show one polyline + two endpoint dots on `obj`. None clears."""
        if getattr(self, "_gordon_hl_root", None) is None:
            return
        if obj is None:
            self._gordon_hl_line_switch.whichChild = -1
            self._gordon_hl_mk_switch.whichChild = -1
            return
        shp = getattr(obj, "Shape", None)
        if not shp or not shp.Edges:
            self._gordon_hl_line_switch.whichChild = -1
            self._gordon_hl_mk_switch.whichChild = -1
            return
        try:
            e = shp.Edges[0]
            fp, lp = e.FirstParameter, e.LastParameter
            pts = [e.valueAt(fp + k / 79.0 * (lp - fp)) for k in range(80)]
        except Exception:
            self._gordon_hl_line_switch.whichChild = -1
            self._gordon_hl_mk_switch.whichChild = -1
            return
        self._gordon_hl_line_coord.point.setValues(
            0, len(pts), [(p.x, p.y, p.z) for p in pts])
        self._gordon_hl_line.numVertices.setValue(len(pts))
        self._gordon_hl_line_switch.whichChild = 0
        self._gordon_hl_mk_coord.point.set1Value(0, pts[0].x, pts[0].y, pts[0].z)
        self._gordon_hl_mk_coord.point.set1Value(
            1, pts[-1].x, pts[-1].y, pts[-1].z)
        self._gordon_hl_mk.numPoints = 2
        self._gordon_hl_mk_switch.whichChild = 0

    # ---- Gordon: family-coloured row markers ---------------------------- #
    # Two stable colours so the user can scan the panel and tell at a
    # glance which family a row belongs to. Coloured text + leading
    # block glyph = visible swatch without needing a custom QStyledItemDelegate.
    _FAMILY_COLOR = {0: ("30, 200, 220",  "U"),  # cyan
                     1: ("220, 90, 200", "V")}  # magenta

    def _color_gordon_family_rows(self):
        """Apply family colours to both Gordon lists immediately, and
        install a hook that re-applies them whenever a list is refreshed."""
        if not getattr(self, "groups", None) or len(self.groups) != 2:
            return
        for idx, g in enumerate(self.groups):
            self._apply_family_color(g, idx)
            # wrap _refresh so the colours stick on add / remove / xfer
            if not getattr(g, "_family_color_wrapped", False):
                orig = g._refresh
                def make_wrapped(_g, _idx, _orig):
                    def _wrapped():
                        _orig()
                        self._apply_family_color(_g, _idx)
                    return _wrapped
                g._refresh = make_wrapped(g, idx, orig)
                g._family_color_wrapped = True

    def _apply_family_color(self, group, family_idx):
        rgb, glyph = self._FAMILY_COLOR.get(family_idx, ("180, 180, 180", "·"))
        for i in range(group.list.count()):
            it = group.list.item(i)
            base = getattr(group.objs[i], "Label",
                           getattr(group.objs[i], "Name", "?")) if i < len(group.objs) \
                else it.text().split(" ", 1)[-1]
            text = u"● %s" % base  # "● <name>"
            it.setText(text)
            it.setForeground(QtGui.QBrush(QtGui.QColor(rgb)))

    # ---- Gordon: click-in-3D-view -> select-row ------------------------ #
    def _toggle_gordon_pick(self, on):
        self._gordon_pick_btn.setText(
            "Pick curves in 3D: on" if on else "Pick curves in 3D: off")
        if on:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    self._gordon_pick_cbid = view.addEventCallback(
                        "SoMouseButtonEvent", self._on_gordon_pick_click)
                else:
                    self._gordon_pick_cbid = None
            except Exception:
                self._gordon_pick_cbid = None
        else:
            if getattr(self, "_gordon_pick_cbid", None) is not None:
                try:
                    doc = FreeCADGui.ActiveDocument
                    view = doc.ActiveView if doc is not None else None
                    if view is not None:
                        drop_callbacks(view, (
                            ("SoMouseButtonEvent", self,
                             "_gordon_pick_cbid"),
                        ))
                except Exception:
                    pass
            self._gordon_pick_cbid = None

    def _on_gordon_pick_click(self, ev):
        """Click in the 3D view -> find the nearest Gordon-family curve
        and select its row. Active only while the pick toggle is on."""
        try:
            if ev.get("Type") != "SoMouseButtonEvent":
                return
            if ev.get("State") != "DOWN" or ev.get("Button") != "BUTTON1":
                return
            pos = ev["Position"]
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view is None:
                return
            # Build candidates: every curve in both Gordon families, sampled.
            candidates = []
            for fam_idx, g in enumerate(self.groups):
                for obj in g.objs:
                    shp = getattr(obj, "Shape", None)
                    if not shp or not shp.Edges:
                        continue
                    e = shp.Edges[0]
                    try:
                        fp, lp = e.FirstParameter, e.LastParameter
                        for k in range(0, 17):
                            p = e.valueAt(fp + k / 16.0 * (lp - fp))
                            sp = view.getPointOnScreen(p)
                            if sp is not None:
                                candidates.append((fam_idx, g, obj, p, sp))
                    except Exception:
                        pass
            if not candidates:
                return
            bx, by = pos[0], pos[1]
            best_fam = best_g = best_obj = None
            best_d = 14.0  # _PICK_PX-equivalent
            for fam_idx, g, obj, _p, sp in candidates:
                d = ((sp[0] - bx) ** 2 + (sp[1] - by) ** 2) ** 0.5
                if d < best_d:
                    best_d, best_fam, best_g, best_obj = d, fam_idx, g, obj
            if best_obj is None:
                return
            # Select only the matched row, in only the matched list, so the
            # visual highlight overlay matches the pick.
            for idx, g in enumerate(self.groups):
                g.list.blockSignals(True)
                g.list.clearSelection()
                if g is best_g:
                    for i, o in enumerate(g.objs):
                        if o is best_obj:
                            g.list.setCurrentRow(i)
                            break
                g.list.blockSignals(False)
            self._on_gordon_row_selected()
        except Exception:
            pass

    def _teardown_gordon_curve_highlight(self):
        root = getattr(self, "_gordon_hl_root", None)
        if root is None:
            return
        try:
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().removeChild(root)
        except Exception:
            pass
        self._gordon_hl_root = None
        if getattr(self, "_gordon_pick_cbid", None) is not None:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view is not None:
                    drop_callbacks(view, (
                        ("SoMouseButtonEvent", self, "_gordon_pick_cbid"),
                    ))
            except Exception:
                pass
            self._gordon_pick_cbid = None

    # ---- commit/abort hook (see _EditPanelBase) ------------------------- #
    def _extra_teardown(self):
        self._teardown_gordon_curve_highlight()

    # ---- surface type converter controls ---- #
    def _add_converter_controls(self):
        """Add controls for converting between surface types."""
        convert_layout = QtWidgets.QHBoxLayout()
        convert_layout.addWidget(QtWidgets.QLabel("Convert To:"))
        self.convert_type_combo = QtWidgets.QComboBox()
        # Add all surface types except the current one to avoid redundancy
        surface_types = ["Loft", "Gordon", "Coons", "Sweep", "SweepN", "Fill", "Match", "BlendSurf", "ExtendSurf"]
        # Remove current type from the list to avoid redundancy in conversion
        if self.kind in surface_types:
            surface_types.remove(self.kind)
        self.convert_type_combo.addItems(surface_types)
        # Default to first item in list if available
        if surface_types:
            self.convert_type_combo.setCurrentIndex(0)
        else:
            # Fallback if no other types available (shouldn't happen with current surface types)
            self.convert_type_combo.addItems(["No conversion available"])
            self.convert_type_combo.setEnabled(False)
        convert_layout.addWidget(self.convert_type_combo)
        self.convert_btn = QtWidgets.QPushButton("Convert")
        self.convert_btn.clicked.connect(self._on_convert_clicked)
        convert_layout.addWidget(self.convert_btn)
        convert_layout.addStretch()
        self._lay.addLayout(convert_layout)

    # ---- surface type conversion ---- #
    def _sample_surface(self, num_points_u=10, num_points_v=10):
        """Sample the current surface to generate points for conversion."""
        shape = self.obj.Shape
        if not shape or not shape.Faces:
            return []  # cannot sample if no faces
        face = shape.Faces[0]  # take first face
        if not face:
            return []

        # Sample points in a grid pattern across the surface
        pts = []
        for i in range(num_points_u):
            u = i / max(num_points_u - 1, 1)  # avoid division by zero
            for j in range(num_points_v):
                v = j / max(num_points_v - 1, 1)
                try:
                    pt = face.valueAt(u, v)
                    pts.append(pt)
                except Exception as exc:
                    FreeCAD.Console.PrintWarning(
                        "Railform: surface sampling skipped a point (%s)\n" % exc)
                    continue
        return pts

    def _on_convert_clicked(self):
        """Handle convert button click: create new object of selected type."""
        target_type = self.convert_type_combo.currentText()
        if target_type == self.kind:
            return  # no change needed

        # Sample current surface to get points
        pts = self._sample_surface()
        if len(pts) < 3:  # need at least 3 points for a surface
            FreeCAD.Console.PrintError("Railform: Need at least 3 points to convert surface\n")
            return

        # Create new object of target type
        doc = self.obj.Document
        new_obj = parametric.make_surface(target_type, doc)
        new_obj.Label = self.obj.Label + " converted"

        # Try to map inputs based on surface type compatibility
        self._map_surface_inputs(new_obj, target_type)

        # Recompute new object
        new_obj.recompute()

        # Select new object and clear old selection
        FreeCADGui.Selection.clearSelection()
        FreeCADGui.Selection.addSelection(new_obj)

        FreeCAD.Console.PrintMessage(
            "Railform: Converted {} to {}\n".format(self.obj.Label, new_obj.Label))

    def _map_surface_inputs(self, new_obj, target_type):
        """Map input objects from current surface to new surface based on type compatibility."""
        # Get current object's input links
        current_inputs = {}
        for prop, _, _ in parametric.GROUPS.get(self.kind, []):
            if hasattr(self.obj, prop):
                current_inputs[prop] = getattr(self.obj, prop)

        # Map inputs based on target type
        if target_type == "Loft":
            # Loft needs Profiles and optionally GuideRails
            if hasattr(new_obj, "Profiles"):
                if "UCurves" in current_inputs:
                    # Use U curves as profiles
                    new_obj.Profiles = current_inputs["UCurves"]
                elif "VCurves" in current_inputs:
                    # Use V curves as profiles
                    new_obj.Profiles = current_inputs["VCurves"]
                elif "Profiles" in current_inputs:
                    # Direct profile transfer
                    new_obj.Profiles = current_inputs["Profiles"]
                elif "Boundaries" in current_inputs and len(current_inputs["Boundaries"]) >= 2:
                    # Use boundaries as profiles (if at least 2)
                    new_obj.Profiles = current_inputs["Boundaries"]
                elif "Reference" in current_inputs and len(current_inputs["Reference"]) >= 1:
                    # Use reference surfaces as profiles (approximation)
                    new_obj.Profiles = current_inputs["Reference"]

            # Try to set guide rails from other available inputs
            if hasattr(new_obj, "GuideRails"):
                if "Profiles" not in current_inputs or current_inputs.get("Profiles") != new_obj.Profiles:
                    # If we didn't use profiles directly, try to set guide rails
                    if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 2:
                        # Use profiles as guide rails if they're not already used as profiles
                        if not hasattr(new_obj, 'Profiles') or not new_obj.Profiles:
                            new_obj.GuideRails = current_inputs["Profiles"]
                    elif "UCurves" in current_inputs and "VCurves" in current_inputs:
                        # If we have both U and V, use one as profiles, other as guides
                        new_obj.GuideRails = current_inputs["VCurves"]  # arbitrary choice

        elif target_type == "Gordon":
            # Gordon needs UCurves and VCurves
            if hasattr(new_obj, "UCurves") and hasattr(new_obj, "VCurves"):
                if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 2:
                    # Split profiles between U and V (simple approach: alternate)
                    profiles = current_inputs["Profiles"]
                    mid = len(profiles) // 2
                    new_obj.UCurves = profiles[:mid]
                    new_obj.VCurves = profiles[mid:]
                    # Handle edge case of odd number
                    if len(profiles) % 2 == 1 and len(profiles) > 0:
                        # Add extra to U curves
                        new_obj.UCurves.append(profiles[-1])
                elif "UCurves" in current_inputs and "VCurves" in current_inputs:
                    # Direct transfer
                    new_obj.UCurves = current_inputs["UCurves"]
                    new_obj.VCurves = current_inputs["VCurves"]
                elif "Profiles" in current_inputs:
                    # Only have profiles, put them in U curves (need at least 2 in each direction)
                    if len(current_inputs["Profiles"]) >= 4:
                        # Split evenly
                        mid = len(current_inputs["Profiles"]) // 2
                        new_obj.UCurves = current_inputs["Profiles"][:mid]
                        new_obj.VCurves = current_inputs["Profiles"][mid:]
                    elif len(current_inputs["Profiles"]) >= 2:
                        # Put all in U, duplicate in V for minimal surface
                        new_obj.UCurves = current_inputs["Profiles"]
                        new_obj.VCurves = current_inputs["Profiles"][:2]  # at least 2
                elif "Boundaries" in current_inputs:
                    # Try to arrange boundaries as U and V curves
                    boundaries = current_inputs["Boundaries"]
                    if len(boundaries) >= 4:
                        # Assume rectangular layout: first two as U, last two as V
                        new_obj.UCurves = boundaries[:2]
                        new_obj.VCurves = boundaries[2:4]
                    elif len(boundaries) >= 2:
                        # Not enough for proper grid, use what we have
                        new_obj.UCurves = boundaries[:2]
                        if len(boundaries) > 2:
                            new_obj.VCurves = boundaries[2:]
                        else:
                            # Duplicate for V direction
                            new_obj.VCurves = boundaries[:2]

        elif target_type == "Coons":
            # Coons needs exactly 4 boundaries
            if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 4:
                # Use first 4 profiles as boundaries
                if hasattr(new_obj, "Boundaries"):
                    new_obj.Boundaries = current_inputs["Profiles"][:4]
            elif "Boundaries" in current_inputs:
                # Direct transfer if we have exactly 4, otherwise take first 4 or pad
                boundaries = current_inputs["Boundaries"]
                if len(boundaries) >= 4:
                    if hasattr(new_obj, "Boundaries"):
                        new_obj.Boundaries = boundaries[:4]
                else:
                    # Not enough boundaries - pad with first ones or use samples
                    needed = 4 - len(boundaries)
                    if len(boundaries) > 0:
                        # Repeat existing boundaries to fill
                        extended = list(boundaries)
                        for i in range(needed):
                            extended.append(boundaries[i % len(boundaries)])
                        if hasattr(new_obj, "Boundaries"):
                            new_obj.Boundaries = extended[:4]
                    # else: No boundaries - can't create valid Coons, leave empty
            elif "UCurves" in current_inputs and "VCurves" in current_inputs:
                # Try to construct boundary from 4 curves: 2 U, 2 V
                u_curves = current_inputs["UCurves"]
                v_curves = current_inputs["VCurves"]
                if len(u_curves) >= 2 and len(v_curves) >= 2:
                    # Take first 2 of each to form a rough rectangle
                    if hasattr(new_obj, "Boundaries"):
                        new_obj.Boundaries = u_curves[:2] + v_curves[:2]

        elif target_type == "Sweep":
            # Sweep needs Rail1, Rail2, and Profiles
            if "Profiles" in current_inputs:
                if hasattr(new_obj, "Profiles"):
                    new_obj.Profiles = current_inputs["Profiles"]
            elif "UCurves" in current_inputs and len(current_inputs["UCurves"]) >= 1:
                # Use U curves as profiles
                if hasattr(new_obj, "Profiles"):
                    new_obj.Profiles = current_inputs["UCurves"]
            elif "VCurves" in current_inputs and len(current_inputs["VCurves"]) >= 1:
                # Use V curves as profiles
                if hasattr(new_obj, "Profiles"):
                    new_obj.Profiles = current_inputs["VCurves"]
            elif "Boundaries" in current_inputs and len(current_inputs["Boundaries"]) >= 1:
                # Use boundaries as profiles
                if hasattr(new_obj, "Profiles"):
                    new_obj.Profiles = current_inputs["Boundaries"]

            # Set rails from available inputs
            if hasattr(new_obj, "Rail1") and hasattr(new_obj, "Rail2"):
                if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 2:
                    # Use additional profiles as rails
                    profiles = current_inputs["Profiles"]
                    if len(profiles) >= 4:
                        # Split for rails
                        new_obj.Rail1 = profiles[:2]
                        new_obj.Rail2 = profiles[2:4]
                    elif len(profiles) >= 2:
                        # Not enough for separate rails, duplicate
                        new_obj.Rail1 = profiles
                        new_obj.Rail2 = profiles

        elif target_type == "SweepN":
            # Multi-rail sweep: one ordered Rails list + Profiles
            if hasattr(new_obj, "Profiles"):
                if "Profiles" in current_inputs:
                    new_obj.Profiles = current_inputs["Profiles"]
                elif "UCurves" in current_inputs and len(current_inputs["UCurves"]) >= 1:
                    new_obj.Profiles = current_inputs["UCurves"]
                elif "VCurves" in current_inputs and len(current_inputs["VCurves"]) >= 1:
                    new_obj.Profiles = current_inputs["VCurves"]
                elif "Boundaries" in current_inputs and len(current_inputs["Boundaries"]) >= 1:
                    new_obj.Profiles = current_inputs["Boundaries"]
            if hasattr(new_obj, "Rails"):
                if "Rails" in current_inputs:
                    new_obj.Rails = current_inputs["Rails"]
                elif "GuideRails" in current_inputs:
                    new_obj.Rails = current_inputs["GuideRails"]
                elif ("Rail1" in current_inputs and "Rail2" in current_inputs
                      and current_inputs["Rail1"] and current_inputs["Rail2"]):
                    # two_rail_sweep's split rails -> one ordered list
                    new_obj.Rails = list(current_inputs["Rail1"]) + \
                                    list(current_inputs["Rail2"])

        elif target_type == "Fill":
            # Fill needs boundaries (>= 3)
            if hasattr(new_obj, "Boundaries"):
                if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 3:
                    new_obj.Boundaries = current_inputs["Profiles"]
                elif "Boundaries" in current_inputs:
                    new_obj.Boundaries = current_inputs["Boundaries"]
                elif "UCurves" in current_inputs and len(current_inputs["UCurves"]) >= 3:
                    new_obj.Boundaries = current_inputs["UCurves"]
                elif "VCurves" in current_inputs and len(current_inputs["VCurves"]) >= 3:
                    new_obj.Boundaries = current_inputs["VCurves"]
                elif "Reference" in current_inputs and len(current_inputs["Reference"]) >= 3:
                    # Use reference surfaces as boundary approximation
                    new_obj.Boundaries = current_inputs["Reference"]

        elif target_type == "Match":
            # Match needs Reference (one or more) and Target (exactly one)
            if hasattr(new_obj, "Target") and hasattr(new_obj, "Reference"):
                if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 1:
                    # Use first profile as target, rest as references
                    new_obj.Target = [current_inputs["Profiles"][0]]
                    if len(current_inputs["Profiles"]) > 1:
                        new_obj.Reference = current_inputs["Profiles"][1:]
                    else:
                        # Need at least one reference - duplicate target or use something else
                        if "UCurves" in current_inputs and len(current_inputs["UCurves"]) >= 1:
                            new_obj.Reference = [current_inputs["UCurves"][0]]
                        elif "VCurves" in current_inputs and len(current_inputs["VCurves"]) >= 1:
                            new_obj.Reference = [current_inputs["VCurves"][0]]
                elif "Boundaries" in current_inputs and len(current_inputs["Boundaries"]) >= 1:
                    # Use first boundary as target, rest as references
                    new_obj.Target = [current_inputs["Boundaries"][0]]
                    if len(current_inputs["Boundaries"]) > 1:
                        new_obj.Reference = current_inputs["Boundaries"][1:]
                    else:
                        # Need at least one reference
                        if "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 1:
                            new_obj.Reference = [current_inputs["Profiles"][0]]
                elif "UCurves" in current_inputs:
                    # Use first U curve as target, rest as references
                    new_obj.Target = [current_inputs["UCurves"][0]]
                    if len(current_inputs["UCurves"]) > 1:
                        new_obj.Reference = current_inputs["UCurves"][1:]
                    elif "VCurves" in current_inputs and len(current_inputs["VCurves"]) >= 1:
                        # Need at least one reference
                        new_obj.Reference = [current_inputs["VCurves"][0]]
                elif "VCurves" in current_inputs:
                    # Use first V curve as target, rest as references
                    new_obj.Target = [current_inputs["VCurves"][0]]
                    if len(current_inputs["VCurves"]) > 1:
                        new_obj.Reference = current_inputs["VCurves"][1:]
                    elif "UCurves" in current_inputs and len(current_inputs["UCurves"]) >= 1:
                        # Need at least one reference
                        new_obj.Reference = [current_inputs["UCurves"][0]]

        elif target_type == "BlendSurf":
            # BlendSurf needs FaceA and FaceB (surfaces)
            # This is trickier since we're converting from curves to surfaces
            # We'll try to use reference surfaces if available, otherwise skip
            if hasattr(new_obj, "FaceA") and hasattr(new_obj, "FaceB"):
                if "Reference" in current_inputs and len(current_inputs["Reference"]) >= 2:
                    new_obj.FaceA = current_inputs["Reference"][0]
                    new_obj.FaceB = current_inputs["Reference"][1]
                elif "Profiles" in current_inputs and len(current_inputs["Profiles"]) >= 2:
                    # This is a stretch - trying to use curves as surfaces won't work well
                    # but we'll try to convert them to surfaces first if possible
                    pass  # Leave empty - user will need to assign surfaces manually

        # Set default values for type-specific parameters
        self._set_surface_defaults(new_obj, target_type)

    def _set_surface_defaults(self, obj, surface_type):
        """Set reasonable default values for surface type-specific parameters."""
        if surface_type == "Loft":
            obj.Closed = getattr(obj, "Closed", False)
            obj.Solid = getattr(obj, "Solid", False)
            if not hasattr(obj, "Continuity") or not obj.Continuity:
                obj.Continuity = "G1/G2 (smooth)"
            if not hasattr(obj, "MaxDegree") or not obj.MaxDegree:
                obj.MaxDegree = 5
        elif surface_type == "Gordon":
            # Gordon doesn't have many scalar parameters beyond what's handled in _build
            pass
        elif surface_type == "Coons":
            # Coons doesn't have many scalar parameters
            pass
        elif surface_type == "Sweep":
            if not hasattr(obj, "Closed") or not obj.Closed:
                obj.Closed = False
            if not hasattr(obj, "Solid") or not obj.Solid:
                obj.Solid = False
            if not hasattr(obj, "Continuity") or not obj.Continuity:
                obj.Continuity = "G1/G2 (smooth)"
            if not hasattr(obj, "MaxDegree") or not obj.MaxDegree:
                obj.MaxDegree = 5
        elif surface_type == "SweepN":
            # No Closed/Solid/Continuity/MaxDegree on SweepN -- only
            # ProfileSamples/RailSamples, which already carry their own
            # defaults. Nothing to force here.
            pass
        elif surface_type == "Fill":
            if not hasattr(obj, "Samples") or not obj.Samples:
                obj.Samples = (30, 8, 100, 1)
        elif surface_type == "Match":
            if not hasattr(obj, "Continuity") or not obj.Continuity:
                obj.Continuity = "G2"
            if not hasattr(obj, "Scale") or not obj.Scale:
                obj.Scale = 1.0
            if not hasattr(obj, "Samples") or not obj.Samples:
                obj.Samples = (24, 4, 100, 1)
        elif surface_type == "BlendSurf":
            if not hasattr(obj, "Continuity") or not obj.Continuity:
                obj.Continuity = "G2"
            if not hasattr(obj, "Scale") or not obj.Scale:
                obj.Scale = 1.0
            if not hasattr(obj, "Samples") or not obj.Samples:
                obj.Samples = (24, 6, 200, 1)


class LoftPanel(_EditPanelBase):
    """Purpose-built panel for Loft Surface -- Profiles / Guide Rails / Options.

    List management (add/remove/reorder, drag-drop, combo-add) used to be a
    bespoke ~150-line parallel reimplementation of _CurveGroup. It now reuses
    _CurveGroup directly (with draggable=True for parity with the old
    drag-to-reorder UX) so there is exactly one add/remove/reorder
    implementation in the file, not two that can drift apart.
    """

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create Loft" if edit_obj is None else "Edit Loft",
                          lambda: parametric.make_surface("Loft", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Loft Surface")
        self._lay = QtWidgets.QVBoxLayout(self.form)
        self._lay.setSpacing(6)
        self._lay.setContentsMargins(8, 8, 8, 8)

        self._live = None
        self._build()

        if self.creating:
            sel = [o for o in _selected_objects() if o is not self.obj]
            if sel:
                self.profiles.objs.extend(sel)
                self.profiles._refresh()
                self.profiles._commit()
        self.recompute()

    # ---- build ---------------------------------------------------------- #
    def _build(self):
        # title
        title = QtWidgets.QLabel("Loft Surface")
        title.setStyleSheet("font-size:13px;font-weight:bold;")
        self._lay.addWidget(title)

        self.profiles = _CurveGroup(
            self, "Profiles", "Profiles  (in order, ≥ 2)",
            "Cross-sections to loft through -- drag rows to reorder",
            draggable=True)
        self._lay.addWidget(self.profiles.box)

        self.rails = _CurveGroup(
            self, "GuideRails", "Guide Rails  (optional)",
            "Optional path constraints -- drag rows to reorder",
            draggable=True)
        self._lay.addWidget(self.rails.box)

        # OPTIONS
        opt = QtWidgets.QGroupBox("Options")
        ol = QtWidgets.QVBoxLayout(opt)
        ol.setContentsMargins(8, 6, 8, 8)
        ol.setSpacing(6)

        # -- Continuity segmented control
        cont_row = QtWidgets.QHBoxLayout()
        cont_row.addWidget(QtWidgets.QLabel("Continuity"))
        cont_row.addStretch(1)
        self._cont_bg = QtWidgets.QButtonGroup()
        self._cont_bg.setExclusive(True)
        cur_cont = str(getattr(self.obj, "Continuity", "G1/G2 (smooth)"))
        for i, (lbl, val) in enumerate((("G0", "G0 (ruled)"),
                                        ("G1", "G1/G2 (smooth)"),
                                        ("G2", "G1/G2 (smooth)"))):
            b = QtWidgets.QPushButton(lbl)
            b.setCheckable(True)
            b.setFixedWidth(38)
            b.setChecked(cur_cont.startswith(val[:2]))
            self._cont_bg.addButton(b, i)
            cont_row.addWidget(b)
        self._cont_bg.buttonClicked.connect(self._on_continuity)
        ol.addLayout(cont_row)

        # -- Degree slider
        deg_row = QtWidgets.QHBoxLayout()
        deg_row.addWidget(QtWidgets.QLabel("Degree"))
        self._deg_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._deg_slider.setRange(1, 8)
        self._deg_slider.setValue(int(getattr(self.obj, "MaxDegree", 5)))
        self._deg_label = QtWidgets.QLabel(str(self._deg_slider.value()))
        self._deg_label.setFixedWidth(18)
        self._deg_slider.valueChanged.connect(self._on_degree)
        deg_row.addWidget(self._deg_slider, 1)
        deg_row.addWidget(self._deg_label)
        ol.addLayout(deg_row)

        # -- Closed / Solid checkboxes
        check_row = QtWidgets.QHBoxLayout()
        self._chk_closed = QtWidgets.QCheckBox("Closed")
        self._chk_closed.setChecked(bool(getattr(self.obj, "Closed", False)))
        self._chk_closed.toggled.connect(lambda v: self._set("Closed", v))
        self._chk_solid = QtWidgets.QCheckBox("Solid (capped)")
        self._chk_solid.setChecked(bool(getattr(self.obj, "Solid", False)))
        self._chk_solid.toggled.connect(lambda v: self._set("Solid", v))
        check_row.addWidget(self._chk_closed)
        check_row.addStretch(1)
        check_row.addWidget(self._chk_solid)
        ol.addLayout(check_row)

        self._lay.addWidget(opt)

        # Status line -- same generalized diagnostic SurfacePanel uses
        # (parametric.LIMITS["Loft"]["Profiles"] = (2, None)), so a Loft
        # with < 2 profiles tells the user why nothing is drawn instead of
        # leaving an empty viewport.
        self._status = QtWidgets.QLabel("")
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        self._status.setWordWrap(True)
        self._lay.addWidget(self._status)

        # Live preview
        self._live = QtWidgets.QCheckBox("Live preview")
        self._live.setChecked(True)
        self._lay.addWidget(self._live)

        # See SurfacePanel's matching checkbox: Profiles/GuideRails get
        # auto-hidden once the loft builds successfully, so a job with
        # tens of profile curves doesn't leave them all cluttering the
        # tree next to the finished surface. This brings them back.
        if hasattr(self.obj, "ShowInputs"):
            self.show_inputs = QtWidgets.QCheckBox("Show input curves")
            self.show_inputs.setChecked(bool(self.obj.ShowInputs))
            self.show_inputs.toggled.connect(
                lambda v: self._set("ShowInputs", v))
            self._lay.addWidget(self.show_inputs)
        else:
            self.show_inputs = None

        self._lay.addStretch(1)
        self._update_status()

    # ---- callbacks ------------------------------------------------------ #
    def _on_continuity(self, btn):
        idx = self._cont_bg.id(btn)
        val = ("G0 (ruled)" if idx == 0 else "G1/G2 (smooth)")
        self._set("Continuity", val)

    def _on_degree(self, v):
        self._deg_label.setText(str(v))
        self._set("MaxDegree", v)

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
        if self._live is None or self._live.isChecked():
            self._recompute_obj()
            self._defer_gui_update()
        self._update_status()

    def _update_status(self):
        status = getattr(self, "_status", None)
        if status is None:
            return
        lo, _hi = parametric.LIMITS.get("Loft", {}).get("Profiles", (None, None))
        ok, msg = panel_logic.loft_readiness(
            len(self.profiles.objs), len(self.rails.objs), lo)
        status.setText(msg)
        status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


# --------------------------------------------------------------------------- #
# Manufacturability: Offset / Thicken / Shell.
#
# Everything above (Loft/Gordon/Coons/Fill/Match/BlendSurf) produces pure
# B-spline SHAPES with zero wall thickness -- fine for a visual/aero shape,
# not a manufacturable part. These three panels are the doc-object plumbing
# on top of core.offset_surface / core.thicken_surface / core.shell_solid,
# which already wrap OCCT's own offset/thickening primitives.
class OffsetThickenPanel(_EditPanelBase):
    """Offset (open surface, zero thickness) or Thicken (open surface ->
    solid with wall thickness). One panel, `mode` picks which parametric
    kind (OffsetFP/ThickenFP) it drives -- the two share every field
    except Thicken's extra Symmetric toggle."""

    def __init__(self, mode="Offset", edit_obj=None):
        assert mode in ("Offset", "Thicken")
        self.mode = mode
        self._start_edit(edit_obj,
                          ("Create " if edit_obj is None else "Edit ") + mode,
                          lambda: parametric.make_surface(mode, self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – %s" % mode)
        lay = QtWidgets.QVBoxLayout(self.form)

        row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Source: " + self._name())
        b = QtWidgets.QPushButton("Set source from selection")
        b.clicked.connect(self._assign_source)
        row.addWidget(self._src_lbl)
        row.addStretch(1)
        row.addWidget(b)
        lay.addLayout(row)

        val_row = QtWidgets.QHBoxLayout()
        prop = "Distance" if mode == "Offset" else "Thickness"
        val_row.addWidget(QtWidgets.QLabel(prop))
        self.val = QtWidgets.QDoubleSpinBox()
        self.val.setDecimals(3)
        self.val.setRange(-1e5, 1e5)
        self.val.setSingleStep(0.5)
        self.val.setValue(float(getattr(self.obj, prop)))
        val_row.addWidget(self.val)
        lay.addLayout(val_row)
        self.val.valueChanged.connect(lambda v, n=prop: self._set(n, v))

        self.symmetric = None
        if mode == "Thicken":
            self.symmetric = QtWidgets.QCheckBox("Symmetric (split +-half)")
            self.symmetric.setChecked(bool(self.obj.Symmetric))
            lay.addWidget(self.symmetric)
            self.symmetric.toggled.connect(lambda v: self._set("Symmetric", v))

        tol_row = QtWidgets.QHBoxLayout()
        tol_row.addWidget(QtWidgets.QLabel("Tolerance"))
        self.tol = QtWidgets.QDoubleSpinBox()
        self.tol.setDecimals(5)
        self.tol.setRange(1e-6, 1.0)
        self.tol.setSingleStep(1e-3)
        self.tol.setValue(float(self.obj.Tolerance))
        tol_row.addWidget(self.tol)
        lay.addLayout(tol_row)
        self.tol.valueChanged.connect(lambda v: self._set("Tolerance", v))

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)

        if self.creating:
            sel = [o for o in _selected_objects() if o is not self.obj]
            if sel:
                self.obj.Source = sel[0]
                self._src_lbl.setText("Source: " + self._name())
        self.recompute()

    def _name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _assign_source(self):
        sel = [o for o in _selected_objects() if o is not self.obj]
        if sel:
            self.obj.Source = sel[0]
            self._src_lbl.setText("Source: " + self._name())
            self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
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
        has_src = bool(getattr(self.obj, "Source", None))
        shp = getattr(self.obj, "Shape", None)
        shape_ok = shp is not None and not shp.isNull()
        ok, msg = panel_logic.offset_thicken_readiness(self.mode, has_src, shape_ok)
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class ShellPanel(_EditPanelBase):
    """Hollow an existing SOLID to a wall thickness, opening it at one or
    more selected faces -- core.shell_solid / the classic CAD 'Shell'
    feature. Complements OffsetThickenPanel's Thicken mode, which instead
    BUILDS a solid from an open surface rather than hollowing one that
    already exists."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create Shell" if edit_obj is None else "Edit Shell",
                          lambda: parametric.make_surface("Shell", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Shell")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select the solid, then select the face(s) to open:"))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Solid: " + self._src_name())
        b1 = QtWidgets.QPushButton("Set solid from selection")
        b1.clicked.connect(self._assign_solid)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b1)
        lay.addLayout(src_row)

        face_row = QtWidgets.QHBoxLayout()
        self._face_lbl = QtWidgets.QLabel(
            "Faces to remove: " + self._face_name())
        b2 = QtWidgets.QPushButton("Set faces from selection")
        b2.clicked.connect(self._assign_faces)
        face_row.addWidget(self._face_lbl)
        face_row.addStretch(1)
        face_row.addWidget(b2)
        lay.addLayout(face_row)

        t_row = QtWidgets.QHBoxLayout()
        t_row.addWidget(QtWidgets.QLabel("Thickness"))
        self.thk = QtWidgets.QDoubleSpinBox()
        self.thk.setDecimals(3)
        self.thk.setRange(0.001, 1e5)
        self.thk.setSingleStep(0.5)
        self.thk.setValue(float(self.obj.Thickness))
        t_row.addWidget(self.thk)
        lay.addLayout(t_row)
        self.thk.valueChanged.connect(lambda v: self._set("Thickness", v))

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)

        self._update_status()

    def _src_name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _face_name(self):
        ref = getattr(self.obj, "FacesToRemove", None)
        if not ref or not ref[0]:
            return "(none)"
        return "%s (%d)" % (ref[0].Label, len(ref[1]))

    def _assign_solid(self):
        sel = [o for o in _selected_objects() if o is not self.obj]
        if sel:
            self.obj.Source = sel[0]
            self._src_lbl.setText("Solid: " + self._src_name())
            self.recompute()

    def _assign_faces(self):
        names = []
        src = None
        for so in FreeCADGui.Selection.getSelectionEx():
            for sub in (so.SubElementNames or []):
                if "Face" in sub:
                    names.append(sub)
                    src = so.Object
        if src is not None and names:
            self.obj.FacesToRemove = (src, names)
            self._face_lbl.setText("Faces to remove: " + self._face_name())
            self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
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
        self._update_status()

    def _update_status(self):
        has_source = bool(getattr(self.obj, "Source", None))
        ref = getattr(self.obj, "FacesToRemove", None)
        has_faces = bool(ref and ref[0] and ref[1])
        ok, msg = panel_logic.shell_readiness(has_source, has_faces)
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class StitchShellPanel(object):
    """Sew a finished patch network (any mix of Loft/Gordon/Coons/Fill/
    Match/BlendSurf/... objects) into one continuous shell -- or a solid,
    if the result closes. Fixes the gap where n_sided_patch's internal
    Shell/sewShape call was never exposed as a general, user-facing
    'stitch these into one' command.

    Deliberately NOT an _EditPanelBase subclass: unlike every other panel
    in this module, there's no single re-editable parametric object to
    create up front (see _EditPanelBase's docstring) -- this is a
    run-once action over a list of EXISTING objects that produces one new
    Part::Feature, closer in spirit to ImportRailsPanel than to
    SurfacePanel. Close-only buttons; nothing to commit/abort as a live
    transaction beyond the one doc.addObject() call in _run().
    """

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.objs = []
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Stitch shell")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select the finished surface objects to sew into one shell "
            "(or solid, if it closes), then Add:"))

        row = QtWidgets.QHBoxLayout()
        self._lbl = QtWidgets.QLabel("Surfaces: (none)")
        b = QtWidgets.QPushButton("Add selected")
        b.clicked.connect(self._add)
        row.addWidget(self._lbl)
        row.addStretch(1)
        row.addWidget(b)
        lay.addLayout(row)

        clr = QtWidgets.QPushButton("Clear list")
        clr.clicked.connect(self._clear)
        lay.addWidget(clr)

        tol_row = QtWidgets.QHBoxLayout()
        tol_row.addWidget(QtWidgets.QLabel("Sew tolerance"))
        self.tol = QtWidgets.QDoubleSpinBox()
        self.tol.setDecimals(5)
        self.tol.setRange(1e-6, 10.0)
        self.tol.setValue(1e-3)
        tol_row.addWidget(self.tol)
        lay.addLayout(tol_row)

        run = QtWidgets.QPushButton("Stitch")
        run.clicked.connect(self._run)
        lay.addWidget(run)

        note = QtWidgets.QLabel(
            "Note: this only welds coincident boundaries together -- it "
            "does not improve a poor seam. Run 'Audit patch network' "
            "first if you need to know whether the seams being stitched "
            "are actually G1/G2, not just touching.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa6b2;font-size:10px;padding:4px 0;")
        lay.addWidget(note)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("padding:6px 0;")
        lay.addWidget(self.status)

    def _add(self):
        for o in FreeCADGui.Selection.getSelection():
            if getattr(o, "Shape", None) is not None and o not in self.objs:
                self.objs.append(o)
        self._lbl.setText(
            "Surfaces: %d selected (%s)"
            % (len(self.objs),
               ", ".join(getattr(o, "Label", o.Name) for o in self.objs))
            if self.objs else "Surfaces: (none)")

    def _clear(self):
        self.objs = []
        self._lbl.setText("Surfaces: (none)")

    def _run(self):
        faces = []
        for o in self.objs:
            shp = getattr(o, "Shape", None)
            if shp is not None:
                faces.extend(shp.Faces)
        if len(faces) < 2:
            self.status.setText(
                "Need faces from >= 2 surface objects (have %d face(s))."
                % len(faces))
            self.status.setStyleSheet("color:#c08552;padding:6px 0;")
            return
        try:
            result = self._core.stitch_shell(faces, tol=float(self.tol.value()))
        except Exception as exc:
            self.status.setText("Failed: %s" % exc)
            self.status.setStyleSheet("color:#c0392b;padding:6px 0;")
            return
        doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
        obj = doc.addObject("Part::Feature", "StitchedShell")
        obj.Shape = result
        from rfstudio import groups
        groups.add_to_group(obj, "Surfaces")
        doc.recompute()
        kind = "solid" if result.Solids else "shell"
        free = self._core.free_edge_count(result)
        color = "#7fae5a" if free == 0 else "#c08552"
        self.status.setText(
            "Stitched %d face(s) into 1 %s -- '%s' created. "
            "%s" % (len(faces), kind, obj.Label,
                   "No free edges remain (fully closed)." if free == 0
                   else "%d free edge(s) remain -- the network still has "
                        "a gap somewhere." % free))
        self.status.setStyleSheet("color:%s;padding:6px 0;" % color)

    def getStandardButtons(self):
        return int(QtWidgets.QDialogButtonBox.Close)

    def accept(self):
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
        return True

    def reject(self):
        return self.accept()


class BlendSurfPanel(_EditPanelBase):
    """Native G1/G2 blend SURFACE strip between two faces -- core.blend_surface,
    the surface analog of BlendPanel (curves). This is what RFS_BlendSurf now
    tries FIRST, before falling back to Curves_BlendSurf2 (see
    engines.NATIVE_OVERRIDES)."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Blend surface" if edit_obj is None else "Edit BlendSurf",
                          lambda: parametric.make_surface("BlendSurf", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Blend surface")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel("Select two surfaces, then assign:"))
        for slot_name in ("FaceA", "FaceB"):
            r = QtWidgets.QHBoxLayout()
            lbl = QtWidgets.QLabel(slot_name + ": " + self._name(slot_name))
            b = QtWidgets.QPushButton("Set source from selection")
            b.clicked.connect(
                lambda c=False, s=slot_name, l=lbl: self._assign(s, l))
            r.addWidget(lbl)
            r.addStretch(1)
            r.addWidget(b)
            lay.addLayout(r)

        self.cont = QtWidgets.QComboBox()
        self.cont.addItems(["G0", "G1", "G2", "G3"])
        self.cont.setCurrentText(str(self.obj.Continuity))
        lay.addWidget(self.cont)
        self.cont.currentTextChanged.connect(
            lambda v: self._set("Continuity", v))

        # Advanced controls for BlendSurf (collapsible)
        self.adv_blend = _CollapsibleGroup("Advanced: samples / scale", collapsed=True)
        self.adv_blend.content_layout.addWidget(QtWidgets.QLabel("Samples"))
        self.samples = QtWidgets.QSpinBox()
        self.samples.setRange(4, 200)
        self.samples.setValue(int(self.obj.Samples))
        self.samples.valueChanged.connect(lambda v: self._set("Samples", v))
        self.adv_blend.content_layout.addWidget(self.samples)

        self.adv_blend.content_layout.addWidget(QtWidgets.QLabel("Scale"))
        self.scale = QtWidgets.QDoubleSpinBox()
        self.scale.setRange(0.1, 5.0)
        self.scale.setSingleStep(0.1)
        self.scale.setValue(float(self.obj.Scale))
        self.adv_blend.content_layout.addWidget(self.scale)
        self.scale.valueChanged.connect(lambda v: self._set("Scale", v))
        lay.addWidget(self.adv_blend)

        g3_note = QtWidgets.QLabel(
            "G3 uses a finite-difference estimate of the 3rd derivative "
            "across the surface boundary (FreeCAD's surface API has no "
            "analytic 3rd derivative) -- treat it as a best-effort target "
            "and check the achieved result below, not a proven match.")
        g3_note.setWordWrap(True)
        g3_note.setStyleSheet("color:#9aa6b2;font-size:10px;padding:2px 0;")
        lay.addWidget(g3_note)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        # Live G0/G1/G2 readout -- fixes the gap where checking what
        # continuity a blend actually achieved required closing this
        # panel and opening ContinuityCheckPanel separately.
        # surface_deviation/classify_continuity are cheap (a few dozen
        # distToShape calls), so this recomputes on every change alongside
        # the shape itself rather than needing its own button.
        self._continuity_lbl = QtWidgets.QLabel("")
        self._continuity_lbl.setWordWrap(True)
        self._continuity_lbl.setStyleSheet(
            "font-size:11px;padding:2px 0;")
        lay.addWidget(self._continuity_lbl)

        if self.creating:
            self._prefill()
        self.recompute()

    def _name(self, slot):
        o = getattr(self.obj, slot)
        return o.Label if o else "(none)"

    def _assign(self, slot, lbl):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces]
        if sel:
            setattr(self.obj, slot, sel[0])
            lbl.setText(slot + ": " + self._name(slot))
            self.recompute()

    def _prefill(self):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces
               and s.Object is not self.obj]
        if len(sel) >= 1:
            self.obj.FaceA = sel[0]
        if len(sel) >= 2:
            self.obj.FaceB = sel[1]

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def _update_continuity(self):
        """Measure the ACHIEVED continuity (not just the requested
        Continuity setting) against both source faces, via
        core.surface_deviation + classify_continuity -- the same
        measurement ContinuityCheckPanel exposes standalone, surfaced
        here inline so it doesn't require a second panel to check."""
        lbl = self._continuity_lbl
        shp = getattr(self.obj, "Shape", None)
        fa, fb = self.obj.FaceA, self.obj.FaceB
        if shp is None or shp.isNull() or not shp.Faces or fa is None or fb is None:
            lbl.setText("")
            return
        try:
            from rfstudio import core
            blend_face = shp.Faces[0]
            shp_a = getattr(fa, "Shape", None)
            shp_b = getattr(fb, "Shape", None)
            if not shp_a or not shp_a.Faces or not shp_b or not shp_b.Faces:
                lbl.setText("")
                return
            rep_a = core.surface_deviation(blend_face, shp_a.Faces[0], samples=10)
            rep_b = core.surface_deviation(blend_face, shp_b.Faces[0], samples=10)
            grade_a = core.classify_continuity(rep_a)
            grade_b = core.classify_continuity(rep_b)
            order = {"FAIL": 0, "G0": 1, "G1": 2, "G2": 3}
            worst = min(grade_a, grade_b, key=lambda g: order[g])
            colors = {"G2": "#7fae5a", "G1": "#c08552", "G0": "#c08552",
                     "FAIL": "#c0392b"}
            lbl.setText(
                "Achieved continuity -- FaceA side: <b>%s</b> "
                "(max G0=%.4f), FaceB side: <b>%s</b> (max G0=%.4f)"
                % (grade_a, rep_a["max_g0"], grade_b, rep_b["max_g0"]))
            lbl.setStyleSheet(
                "color:%s;font-size:11px;padding:2px 0;"
                % colors.get(worst, "#9aa6b2"))
        except Exception as exc:
            lbl.setText("Continuity check failed: %s" % exc)
            lbl.setStyleSheet("color:#9aa6b2;font-size:11px;padding:2px 0;")

    def recompute(self):
        try:
            self._recompute_obj()
        except Exception as exc:
            self._status.setText("Failed: %s" % exc)
            self._status.setStyleSheet(
                "color:#c0392b;font-size:11px;padding:4px 0;")
            self._defer_gui_update()
            return
        self._defer_gui_update()
        ok, msg = panel_logic.blend_readiness(
            bool(self.obj.FaceA) and bool(self.obj.FaceB))
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")
        self._update_continuity()


class FilletPanel(_EditPanelBase):
    """Radius-driven fillet on a solid's picked edges (RFS_Fillet) --
    the long-promised CAD 'fillet by radius' feature for solids,
    closing the gap issues.md calls out as "no radius-driven fillet"
    (the existing blend_surface is dimensionless-scale only, and
    is the open-surface path; this is the solid path). Wraps
    parametric.FilletFP -> core.fillet_solid."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create Fillet" if edit_obj is None else "Edit Fillet",
                          lambda: parametric.make_surface("Fillet", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Fillet")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select a SOLID, then select the EDGE(s) to round, then "
            "set the radius:"))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Solid: " + self._src_name())
        b1 = QtWidgets.QPushButton("Set solid from selection")
        b1.clicked.connect(self._assign_solid)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b1)
        lay.addLayout(src_row)

        edge_row = QtWidgets.QHBoxLayout()
        self._edge_lbl = QtWidgets.QLabel(
            "Edges to round: " + self._edge_name())
        b2 = QtWidgets.QPushButton("Set edges from selection")
        b2.clicked.connect(self._assign_edges)
        edge_row.addWidget(self._edge_lbl)
        edge_row.addStretch(1)
        edge_row.addWidget(b2)
        lay.addLayout(edge_row)

        r_row = QtWidgets.QHBoxLayout()
        r_row.addWidget(QtWidgets.QLabel("Radius"))
        self.rad = QtWidgets.QDoubleSpinBox()
        self.rad.setDecimals(3)
        self.rad.setRange(0.001, 1e5)
        self.rad.setSingleStep(0.5)
        self.rad.setValue(float(self.obj.Radius))
        r_row.addWidget(self.rad)
        lay.addLayout(r_row)
        self.rad.valueChanged.connect(lambda v: self._set("Radius", v))

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)
        self._update_status()

    def _src_name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _edge_name(self):
        ref = getattr(self.obj, "Edges", None)
        if not ref or not ref[0]:
            return "(none)"
        return "%s (%d)" % (ref[0].Label, len(ref[1]))

    def _assign_solid(self):
        sel = [o for o in _selected_objects() if o is not self.obj and
               getattr(o, "Shape", None) and o.Shape.Solids]
        if sel:
            self.obj.Source = sel[0]
            self._src_lbl.setText("Solid: " + self._src_name())
            self.recompute()

    def _assign_edges(self):
        src = getattr(self.obj, "Source", None)
        names = []
        for so in FreeCADGui.Selection.getSelectionEx():
            for sub in (so.SubElementNames or []):
                if "Edge" in sub and (src is None or so.Object is src):
                    names.append(sub)
                    if src is None:
                        src = so.Object
        if src is not None and names:
            self.obj.Source = src
            self.obj.Edges = (src, names)
            self._src_lbl.setText("Solid: " + self._src_name())
            self._edge_lbl.setText("Edges to round: " + self._edge_name())
            self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
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
        self._update_status()

    def _update_status(self):
        has_source = bool(getattr(self.obj, "Source", None))
        ref = getattr(self.obj, "Edges", None)
        has_edges = bool(ref and ref[0] and ref[1])
        radius_ok = abs(float(self.obj.Radius)) > 1e-9
        if not has_source:
            msg = "Pick a solid source."
            ok = False
        elif not has_edges:
            msg = "Pick at least one edge to round."
            ok = False
        elif not radius_ok:
            msg = "Radius must be non-zero."
            ok = False
        else:
            msg = "Ready. Radius=%g." % float(self.obj.Radius)
            ok = True
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class PolylineFilletPanel(_EditPanelBase):
    """Round the corners of a polyline (RFS_FilletPolyline) with tangent
    arcs of a single radius.

    IN PLACE (preferred): when the selection contains a Railform
    Polyline, the fillet is recorded ON that polyline (its `Fillets`
    record, SolidWorks-sketch style) -- no second, linked curve object
    is created; `Points` keep the sharp corners and execute() rebuilds
    the arcs from the record on every recompute.

    DERIVED (legacy): with no polyline selected -- or when editing an
    existing PolylineFillet object -- it creates/edits a
    parametric.PolylineFilletFP that links the source (kept so old
    documents and workflows still work).

    Both modes wrap core.polyline_corner_fillet."""

    def __init__(self, edit_obj=None):
        target = edit_obj if edit_obj is not None \
            else self._selection_target()
        self._inplace = target is not None and hasattr(target, "Fillets")
        if target is None:
            title = "Create Polyline Fillet"
        elif self._inplace:
            title = "Fillet polyline"
        else:
            title = "Edit Polyline Fillet"
        self._start_edit(target, title,
                         lambda: parametric.make_polyline_fillet(None, self.doc))

        # In place: seed an EMPTY record with the default radius so
        # opening the panel on a sharp polyline previews a uniform
        # fillet immediately (same default the legacy feature used);
        # Cancel/undo restores the sharp polyline since this all runs
        # inside the panel's undo transaction.
        if self._inplace and not list(self.obj.Fillets or []):
            n = len(self.obj.Points or [])
            if n >= 3:
                self.obj.Fillets = [5.0] * n

        # On legacy creation, auto-adopt a selected polyline/curve as the source.
        if self.creating and getattr(self.obj, "Source", None) is None:
            src = self._first_source_in_selection()
            if src is not None:
                self.obj.Source = src

        # Illustrator-style live per-corner fillet handles (drag a ring
        # on the arc to change just that corner's radius) -- see
        # editmode.CornerFilletEditor. Best-effort: absence of pivy/Coin
        # (editor.ok == False) just means no in-viewport handles, same
        # fallback convention as PointEditor/KnotEditor elsewhere.
        # In place the handle editor writes the polyline's own `Fillets`
        # record; in legacy mode it writes the derived object's `Radii`.
        self._corner_editor = None
        self._rebuild_corner_editor()

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Polyline Fillet")
        lay = QtWidgets.QVBoxLayout(self.form)
        if self._inplace:
            lay.addWidget(QtWidgets.QLabel(
                "Rounds every corner of the selected polyline with a\n"
                "tangent arc. The fillet is RECORDED ON the polyline\n"
                "(its points stay sharp; the arcs are rebuilt from the\n"
                "record on every recompute):"))
        else:
            # W4: the dual object model is now explicit in the UI --
            # legacy mode says what it will create and how to get the
            # in-place behaviour instead of silently linking an object.
            lay.addWidget(QtWidgets.QLabel(
                "Rounds every corner of a polyline with a tangent arc.\n"
                "Pick the polyline, then set the radius. (Legacy mode:\n"
                "this creates a LINKED PolylineFillet object -- select\n"
                "a polyline BEFORE running this tool to fillet it in\n"
                "place instead, with no extra object.)"))

        if self._inplace:
            lay.addWidget(QtWidgets.QLabel(
                "Polyline: %s  (edited in place — no new object)"
                % self.obj.Label))
        else:
            src_row = QtWidgets.QHBoxLayout()
            self._src_lbl = QtWidgets.QLabel("Polyline: " + self._src_name())
            b1 = QtWidgets.QPushButton("Set source from selection")
            b1.clicked.connect(self._assign_source)
            src_row.addWidget(self._src_lbl)
            src_row.addStretch(1)
            src_row.addWidget(b1)
            lay.addLayout(src_row)

        r_row = QtWidgets.QHBoxLayout()
        r_row.addWidget(QtWidgets.QLabel("Radius"))
        self.rad = QtWidgets.QDoubleSpinBox()
        self.rad.setDecimals(3)
        self.rad.setRange(0.001, 1e5)
        self.rad.setSingleStep(0.5)
        if self._inplace:
            self.rad.setValue(next(
                (r for r in (self.obj.Fillets or []) if r > 0), 5.0))
            self.rad.valueChanged.connect(self._radius_changed)
        else:
            self.rad.setValue(float(self.obj.Radius))
            self.rad.valueChanged.connect(lambda v: self._set("Radius", v))
        r_row.addWidget(self.rad)
        if self._inplace:
            bclr = QtWidgets.QPushButton("Clear all")
            bclr.setToolTip(
                "Remove every fillet: all corners go sharp (the record "
                "stays, as zeros, so the polyline does not re-seed).")
            bclr.clicked.connect(self._clear_fillets)
            r_row.addWidget(bclr)
        lay.addLayout(r_row)

        self.closed = QtWidgets.QCheckBox(
            "Closed (also round the last→first corner)")
        if self._inplace:
            self.closed.setChecked(bool(self.obj.FilletClosed))
            self.closed.stateChanged.connect(
                lambda s: self._set("FilletClosed", bool(s)))
        else:
            self.closed.setChecked(bool(self.obj.Closed))
            self.closed.stateChanged.connect(
                lambda s: self._set("Closed", bool(s)))
        lay.addWidget(self.closed)

        self._status = _status_label()
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        self.live.stateChanged.connect(self.recompute)
        lay.addWidget(self.live)

        self.recompute()

    def _first_source_in_selection(self):
        for o in _selected_objects():
            if o is self.obj:
                continue
            if getattr(o, "Points", None) or getattr(
                    getattr(o, "Shape", None), "Wires", None):
                return o
        return None

    def _src_name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _assign_source(self):
        src = self._first_source_in_selection()
        if src is not None:
            self.obj.Source = src
            self._src_lbl.setText("Polyline: " + self._src_name())
            self._rebuild_corner_editor()
            self.recompute()

    @staticmethod
    def _selection_target():
        """In-place fillet target from the current selection: a Railform
        Polyline (fillet recorded ON it) preferred, else an existing
        PolylineFillet (edit it instead of stacking a new one), else
        None -> legacy create-and-link flow."""
        def kind_of(o):
            return getattr(getattr(o, "Proxy", None), "Kind", None)
        sel = list(_selected_objects())
        for o in sel:
            if kind_of(o) == "Polyline":
                return o
        for o in sel:
            if kind_of(o) == "PolylineFillet":
                return o
        return None

    def _radius_changed(self, v):
        """In place, the uniform Radius box IS a record write: it
        re-rounds EVERY corner at v (overriding per-corner handle
        refinements -- the box means "all corners", the handles mean
        "just this one"). Legacy mode just sets the Radius property."""
        if not self._inplace:
            self._set("Radius", v)
            return
        n = len(self.obj.Points or [])
        if n:
            self.obj.Fillets = [float(v)] * n
        self.recompute()

    def _clear_fillets(self):
        """In place: all corners sharp -- every radius in the record
        goes to 0 (not empty, so the panel does not re-seed 5.0 on the
        next open)."""
        n = len(self.obj.Points or [])
        self.obj.Fillets = [0.0] * n
        self.recompute()

    def _rebuild_corner_editor(self):
        """(Re)build CornerFilletEditor against the current target --
        called on init and whenever Source is reassigned, since the
        editor's markers are keyed off a specific ViewObject. In place,
        the target is the polyline itself (its `Fillets` record gets
        written); in legacy mode it is the derived PolylineFillet."""
        old = getattr(self, "_corner_editor", None)
        if old is not None:
            try:
                old.finish()
            except Exception:
                pass
            self._corner_editor = None
        src_obj = self.obj if self._inplace \
            else getattr(self.obj, "Source", None)
        if src_obj is None or getattr(src_obj, "ViewObject", None) is None:
            return
        from rfstudio import editmode
        editor = editmode.CornerFilletEditor(self.obj, src_obj.ViewObject)
        if editor.ok:
            editor.on_commit = self._update_status
            self._corner_editor = editor

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self, *_a):
        if self.live is None or self.live.isChecked():
            try:
                self._recompute_obj()
            except Exception as exc:
                self._status.setText("Failed: %s" % exc)
                self._defer_gui_update()
                return
            self._defer_gui_update()
        if getattr(self, "_corner_editor", None) is not None:
            try:
                self._corner_editor._refresh_markers()
            except Exception:
                pass
        self._update_status()

    def _update_status(self):
        if self._inplace:
            sh = getattr(self.obj, "Shape", None)
            if sh is None or sh.isNull():
                self._status.setText(
                    "No result — a polyline needs >= 2 points and real "
                    "corners to fillet.")
                return
            radii = list(self.obj.Fillets or [])
            active = sum(1 for r in radii if r > 0)
            self._status.setText(
                "Ready. %d edges, %d rounded corners (of %d points). "
                "Drag an orange ring in the 3D view for ONE corner; the "
                "Radius box re-rounds all of them."
                % (len(sh.Edges), active, len(radii)))
            return
        if not getattr(self.obj, "Source", None):
            # W6: say exactly what to do next (and how to avoid this
            # legacy flow altogether by pre-selecting the polyline).
            self._status.setText(
                "No polyline yet -- select one in the 3D view, then "
                "press 'Set source from selection'. Tip: select the "
                "polyline BEFORE running this tool to fillet it in "
                "place (no linked object is created that way).")
            return
        sh = getattr(self.obj, "Shape", None)
        if sh is None or sh.isNull():
            self._status.setText(
                "No result — is the source a polyline with corners?")
            return
        self._status.setText(
            "Ready. %d edges. Drag a corner's orange ring in the 3D "
            "view to set its own radius, or use the uniform Radius "
            "above." % len(sh.Edges))

    def _extra_teardown(self):
        if getattr(self, "_corner_editor", None) is not None:
            try:
                self._corner_editor.finish()
            except Exception:
                pass
            self._corner_editor = None


class JoinCurvePanel(_EditPanelBase):
    """Join several curves end-to-end into a single Wire (RFS_JoinCurve).
    Corners are preserved -- no refit (WYSIWYG). Wraps parametric.JoinCurveFP
    -> core.join_curves."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                         "Create Join Curve" if edit_obj is None
                         else "Edit Join Curve",
                         lambda: parametric.make_join_curve(None, self.doc))

        if self.creating and not getattr(self.obj, "Sources", None):
            srcs = self._curves_in_selection()
            if srcs:
                self.obj.Sources = srcs

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Join Curves")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Welds curves end-to-end into a single wire (corners kept).\n"
            "Select the curves, then:"))

        row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel(self._src_text())
        self._src_lbl.setWordWrap(True)
        b1 = QtWidgets.QPushButton("Set curves from selection")
        b1.clicked.connect(self._assign_sources)
        row.addWidget(self._src_lbl, 1)
        row.addWidget(b1)
        lay.addLayout(row)

        self.show_inputs = QtWidgets.QCheckBox("Keep source curves visible")
        self.show_inputs.setChecked(bool(getattr(self.obj, "ShowInputs", False)))
        self.show_inputs.stateChanged.connect(
            lambda s: self._set("ShowInputs", bool(s)))
        lay.addWidget(self.show_inputs)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        self.live.stateChanged.connect(self.recompute)
        lay.addWidget(self.live)

        self.recompute()

    def _curves_in_selection(self):
        out = []
        for o in _selected_objects():
            if o is self.obj:
                continue
            shp = getattr(o, "Shape", None)
            if shp is not None and shp.Edges:
                out.append(o)
        return out

    def _src_text(self):
        srcs = getattr(self.obj, "Sources", None) or []
        return "Curves: " + (", ".join(s.Label for s in srcs)
                             if srcs else "(none)")

    def _assign_sources(self):
        srcs = self._curves_in_selection()
        if srcs:
            self.obj.Sources = srcs
            self._src_lbl.setText(self._src_text())
            self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self, *_a):
        if self.live is None or self.live.isChecked():
            try:
                self._recompute_obj()
            except Exception as exc:
                self._status.setText("Failed: %s" % exc)
                self._defer_gui_update()
                return
            self._defer_gui_update()
        self._update_status()

    def _update_status(self):
        srcs = getattr(self.obj, "Sources", None) or []
        if len(srcs) < 2:
            self._status.setText("Select at least two curves to join.")
            return
        sh = getattr(self.obj, "Shape", None)
        if sh is None or sh.isNull():
            self._status.setText("Curves don't connect end-to-end -- snap "
                                 "their endpoints together first.")
            return
        self._status.setText("Joined %d curves into a wire of %d edges."
                             % (len(srcs), len(sh.Edges)))


class ChamferPanel(FilletPanel):
    """Radius-driven chamfer on a solid's picked edges (RFS_Chamfer) --
    flat-faced cousin of FilletPanel. Inherits FilletPanel's layout
    (it differs only in the Distance spinbox instead of Radius) and
    drives parametric.ChamferFP -> core.chamfer_solid."""

    def __init__(self, edit_obj=None):
        # Bypass FilletPanel.__init__ -- different doc object kind, different
        # property name. Inline the same shape here so the two panels stay
        # parallel and both readable on their own.
        self._start_edit(edit_obj,
                          "Create Chamfer" if edit_obj is None else "Edit Chamfer",
                          lambda: parametric.make_surface("Chamfer", self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Chamfer")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select a SOLID, then select the EDGE(s) to chamfer, then "
            "set the distance:"))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Solid: " + self._src_name())
        b1 = QtWidgets.QPushButton("Set solid from selection")
        b1.clicked.connect(self._assign_solid)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b1)
        lay.addLayout(src_row)

        edge_row = QtWidgets.QHBoxLayout()
        self._edge_lbl = QtWidgets.QLabel(
            "Edges to chamfer: " + self._edge_name())
        b2 = QtWidgets.QPushButton("Set edges from selection")
        b2.clicked.connect(self._assign_edges)
        edge_row.addWidget(self._edge_lbl)
        edge_row.addStretch(1)
        edge_row.addWidget(b2)
        lay.addLayout(edge_row)

        d_row = QtWidgets.QHBoxLayout()
        d_row.addWidget(QtWidgets.QLabel("Distance (symmetric)"))
        self.rad = QtWidgets.QDoubleSpinBox()       # aliased -- reuse method
        self.rad.setDecimals(3)
        self.rad.setRange(0.001, 1e5)
        self.rad.setSingleStep(0.5)
        self.rad.setValue(float(self.obj.Distance))
        d_row.addWidget(self.rad)
        lay.addLayout(d_row)
        self.rad.valueChanged.connect(lambda v: self._set("Distance", v))

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)
        self._update_status()

    # Methods _src_name, _edge_name, _assign_solid, _assign_edges, _set,
    # recompute, _update_status are inherited from FilletPanel; only the
    # status read changes property name -- patched in _update_status:
    def _update_status(self):
        has_source = bool(getattr(self.obj, "Source", None))
        ref = getattr(self.obj, "Edges", None)
        has_edges = bool(ref and ref[0] and ref[1])
        distance_ok = abs(float(self.obj.Distance)) > 1e-9
        if not has_source:
            msg = "Pick a solid source."
            ok = False
        elif not has_edges:
            msg = "Pick at least one edge to chamfer."
            ok = False
        elif not distance_ok:
            msg = "Distance must be non-zero."
            ok = False
        else:
            msg = "Ready. Distance=%g." % float(self.obj.Distance)
            ok = True
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class BlendSurfClosedPanel(_EditPanelBase):
    """Closed-loop G1/G2 blend around an N-sided face ring
    (RFS_BlendSurfClosed) -- the closed analog of BlendSurfPanel. Drives
    parametric.BlendSurfClosedFP -> core.blend_surface_closed. Mirrors
    BlendSurfPanel's UI shape so the two stay learnable as a pair."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create BlendSurfClosed" if edit_obj is None else
                          "Edit BlendSurfClosed",
                          lambda: parametric.make_surface("BlendSurfClosed",
                                                          self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Closed-loop blend")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select >= 3 face-bearing objects forming a CLOSED LOOP "
            "(in order around the hole), then assign:"))

        # The Faces link-list slot -- same pattern BlendSurfPanel's
        # FaceA/FaceB buttons use, generalized to one ordered list.
        self._faces_lbl = QtWidgets.QLabel("Faces: " + self._faces_name())
        b = QtWidgets.QPushButton("Add selected faces to ring")
        b.clicked.connect(self._add_faces)
        r = QtWidgets.QHBoxLayout()
        r.addWidget(self._faces_lbl)
        r.addStretch(1)
        r.addWidget(b)
        lay.addLayout(r)
        clr = QtWidgets.QPushButton("Clear ring")
        clr.clicked.connect(self._clear_faces)
        lay.addWidget(clr)

        self.cont = QtWidgets.QComboBox()
        self.cont.addItems(["G0", "G1", "G2"])
        self.cont.setCurrentText(str(self.obj.Continuity))
        lay.addWidget(self.cont)
        self.cont.currentTextChanged.connect(
            lambda v: self._set("Continuity", v))

        s_row = QtWidgets.QHBoxLayout()
        s_row.addWidget(QtWidgets.QLabel("Scale"))
        self.scale = QtWidgets.QDoubleSpinBox()
        self.scale.setRange(0.1, 5.0)
        self.scale.setSingleStep(0.1)
        self.scale.setValue(float(self.obj.Scale))
        s_row.addWidget(self.scale)
        lay.addLayout(s_row)
        self.scale.valueChanged.connect(lambda v: self._set("Scale", v))

        n_row = QtWidgets.QHBoxLayout()
        n_row.addWidget(QtWidgets.QLabel("Cross-section samples"))
        self.samples = QtWidgets.QSpinBox()
        self.samples.setRange(6, 200)
        self.samples.setValue(int(self.obj.Samples))
        n_row.addWidget(self.samples)
        lay.addLayout(n_row)
        self.samples.valueChanged.connect(lambda v: self._set("Samples", v))

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
            self._prefill()
        self.recompute()

    def _faces_name(self):
        links = list(getattr(self.obj, "Faces", []) or [])
        if not links:
            return "(none)"
        return ", ".join(o.Label for o in links)

    def _add_faces(self):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces
               and s.Object is not self.obj]
        existing = list(getattr(self.obj, "Faces", []) or [])
        for o in sel:
            if o not in existing:
                existing.append(o)
        self.obj.Faces = existing
        self._faces_lbl.setText("Faces: " + self._faces_name())
        self.recompute()

    def _clear_faces(self):
        self.obj.Faces = []
        self._faces_lbl.setText("Faces: (none)")
        self.recompute()

    def _prefill(self):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces
               and s.Object is not self.obj]
        if sel:
            self.obj.Faces = list(sel)

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
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
        n = len(list(getattr(self.obj, "Faces", []) or []))
        if n < 3:
            self._status.setText(
                "Need >= 3 faces in the ring (have %d)." % n)
            self._status.setStyleSheet("color:#c08552;font-size:11px;padding:4px 0;")
        else:
            shp = getattr(self.obj, "Shape", None)
            if shp and not shp.isNull() and getattr(shp, "Faces", None):
                # surface_deviation needs two faces; we measure the
                # closure seam (last->first) as a sanity readout.
                from rfstudio import core
                try:
                    fseq = []
                    for link in self.obj.Faces:
                        s = getattr(link, "Shape", None)
                        if s and s.Faces:
                            fseq.append(s.Faces[0])
                    if len(fseq) >= 2:
                        rep = core.surface_deviation(fseq[-1], fseq[0], samples=10)
                        grade = core.classify_continuity(rep)
                        colors = {"G2": "#7fae5a", "G1": "#c08552",
                                 "G0": "#c08552", "FAIL": "#c0392b"}
                        self._status.setText(
                            "Ring: %d face(s); closure seam: <b>%s</b> "
                            "(max G0=%.4f)" % (n, grade, rep["max_g0"]))
                        self._status.setStyleSheet(
                            "color:%s;font-size:11px;padding:4px 0;"
                            % colors.get(grade, "#9aa6b2"))
                    else:
                        self._status.setText("Ring: %d face(s)." % n)
                        self._status.setStyleSheet(
                            "color:#7fae5a;font-size:11px;padding:4px 0;")
                except Exception:
                    self._status.setText("Ring: %d face(s)." % n)
                    self._status.setStyleSheet(
                        "color:#7fae5a;font-size:11px;padding:4px 0;")
            else:
                self._status.setText("Ring: %d face(s)." % n)
                self._status.setStyleSheet(
                    "color:#7fae5a;font-size:11px;padding:4px 0;")


class VariableFilletPanel(_EditPanelBase):
    """Variable-radius (multi-station) fillet on a single solid edge --
    RFS_VariableFillet. Drives parametric.VariableFilletFP ->
    core.variable_fillet_solid.

    UI: a Plasticity/Fusion-style draggable LAW CURVE graph (param 0..1 on
    X, radius on Y -- see rfstudio.panel_lawcurve.LawCurveWidget) is the
    primary editor, backed by the same compact numeric table this panel
    used to expose alone. Both views stay synced onto the same
    obj.Params/obj.Radii pair: drag a point on the graph, or type an exact
    value in the table -- either edits the other. The graph draws the
    same PIECEWISE-LINEAR interpolation core.variable_fillet_solid
    actually builds (see that function's docstring) -- no smoothing is
    implied that OCCT's makeFillet binding doesn't provide.
    """

    _Y_PAD = 1.3   # graph Y-range padding factor around the current radii

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create Variable Fillet" if edit_obj is None else
                          "Edit Variable Fillet",
                          lambda: parametric.make_surface("VariableFillet",
                                                          self.doc))
        self._syncing = False

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Variable-radius fillet")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select a SOLID, then select ONE edge to round, then shape "
            "the radius law below (drag points on the graph, or edit "
            "the table for exact values):"))

        src_row = QtWidgets.QHBoxLayout()
        self._src_lbl = QtWidgets.QLabel("Solid: " + self._src_name())
        b1 = QtWidgets.QPushButton("Set solid from selection")
        b1.clicked.connect(self._assign_solid)
        src_row.addWidget(self._src_lbl)
        src_row.addStretch(1)
        src_row.addWidget(b1)
        lay.addLayout(src_row)

        edge_row = QtWidgets.QHBoxLayout()
        self._edge_lbl = QtWidgets.QLabel("Edge: " + self._edge_name())
        b2 = QtWidgets.QPushButton("Set edge from selection")
        b2.clicked.connect(self._assign_edge)
        edge_row.addWidget(self._edge_lbl)
        edge_row.addStretch(1)
        edge_row.addWidget(b2)
        lay.addLayout(edge_row)

        lay.addWidget(QtWidgets.QLabel("Radius law:"))
        self.graph = LawCurveWidget(x_label="Edge param", y_label="Radius",
                                    y_min=0.0, y_max=1.0)
        self.graph.on_change = self._on_graph_changed
        self.graph.setToolTip(
            "Left-click empty space to add a station, drag a point to "
            "move it, right-click a point to delete it (>= 2 always kept).")
        lay.addWidget(self.graph)

        range_row = QtWidgets.QHBoxLayout()
        range_row.addWidget(QtWidgets.QLabel("Graph Y range"))
        self._y_min = QtWidgets.QDoubleSpinBox()
        self._y_min.setDecimals(3)
        self._y_min.setRange(0.0, 1e6)
        self._y_min.valueChanged.connect(self._on_range_changed)
        self._y_max = QtWidgets.QDoubleSpinBox()
        self._y_max.setDecimals(3)
        self._y_max.setRange(0.001, 1e6)
        self._y_max.valueChanged.connect(self._on_range_changed)
        range_row.addWidget(self._y_min)
        range_row.addWidget(QtWidgets.QLabel("to"))
        range_row.addWidget(self._y_max)
        auto_btn = QtWidgets.QPushButton("Auto")
        auto_btn.setToolTip("Rescale the graph to fit the current stations")
        auto_btn.clicked.connect(lambda: self._autoscale_graph(commit=False))
        range_row.addWidget(auto_btn)
        range_row.addStretch(1)
        lay.addLayout(range_row)

        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Param", "Radius"])
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.Stretch)
        self.table.setMaximumHeight(110)
        lay.addWidget(self.table)

        row_btns = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("Add station")
        add_btn.clicked.connect(self._add_station)
        rm_btn = QtWidgets.QPushButton("Remove selected")
        rm_btn.clicked.connect(self._remove_station)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(rm_btn)
        lay.addLayout(row_btns)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color:#9aa6b2;font-size:11px;padding:4px 0;")
        lay.addWidget(self._status)

        self.live = QtWidgets.QCheckBox("Live preview")
        self.live.setChecked(True)
        lay.addWidget(self.live)
        self.live.stateChanged.connect(self.recompute)

        self._fill_from_obj()
        self._update_status()

    def _src_name(self):
        o = getattr(self.obj, "Source", None)
        return o.Label if o else "(none)"

    def _edge_name(self):
        ref = getattr(self.obj, "Edge", None)
        if not ref or not ref[0]:
            return "(none)"
        return "%s (%s)" % (ref[0].Label, ref[1][0] if ref[1] else "?")

    def _assign_solid(self):
        sel = [o for o in _selected_objects() if o is not self.obj and
               getattr(o, "Shape", None) and o.Shape.Solids]
        if sel:
            self.obj.Source = sel[0]
            self._src_lbl.setText("Solid: " + self._src_name())
            self.recompute()

    def _assign_edge(self):
        src = getattr(self.obj, "Source", None)
        for so in FreeCADGui.Selection.getSelectionEx():
            for sub in (so.SubElementNames or []):
                if "Edge" in sub and (src is None or so.Object is src):
                    self.obj.Source = so.Object
                    self.obj.Edge = (so.Object, [sub])
                    self._src_lbl.setText("Solid: " + self._src_name())
                    self._edge_lbl.setText("Edge: " + self._edge_name())
                    self.recompute()
                    return

    # ---- graph <-> table <-> obj sync ------------------------------- #
    def _fill_from_obj(self):
        params = list(getattr(self.obj, "Params", []) or [0.0, 1.0])
        radii = list(getattr(self.obj, "Radii", []) or [1.0, 1.0])
        self._autoscale_graph(points=list(zip(params, radii)), commit=False)
        self._set_views(list(zip(params, radii)))

    def _autoscale_graph(self, points=None, commit=True):
        """Rescale the graph's Y axis to fit the given (or current)
        stations, with headroom so points near the edges aren't clipped
        against the plot border."""
        pts = points if points is not None else self.graph.points()
        radii = [r for _t, r in pts] or [1.0]
        lo, hi = min(radii), max(radii)
        if hi - lo < 1e-6:
            lo, hi = max(0.0, lo - 1.0), hi + 1.0
        pad = (hi - lo) * (self._Y_PAD - 1.0) / 2.0
        lo, hi = max(0.0, lo - pad), hi + pad
        self._syncing = True
        try:
            self._y_min.setValue(lo)
            self._y_max.setValue(hi)
            self.graph.set_y_range(lo, hi)
        finally:
            self._syncing = False
        if commit:
            self._on_graph_changed(self.graph.points())

    def _on_range_changed(self, *_args):
        if self._syncing:
            return
        self.graph.set_y_range(self._y_min.value(), self._y_max.value())

    def _set_views(self, points):
        """Push `points` (list of (t, radius)) into both the graph and
        the table without re-triggering either's change handler."""
        self._syncing = True
        try:
            self.graph.set_points(points)
            self.table.setRowCount(0)
            for t, r in sorted(points, key=lambda p: p[0]):
                self._append_row(t, r)
        finally:
            self._syncing = False

    def _on_graph_changed(self, points):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.table.setRowCount(0)
            for t, r in sorted(points, key=lambda p: p[0]):
                self._append_row(t, r)
        finally:
            self._syncing = False
        self._commit(points)

    def _append_row(self, p=0.5, r=1.0):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, val in enumerate((p, r)):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(3)
            sb.setRange(0.0, 1.0 if col == 0 else 1e6)
            sb.setSingleStep(0.05 if col == 0 else 0.5)
            sb.setValue(val)
            sb.valueChanged.connect(self._commit_table)
            self.table.setCellWidget(row, col, sb)

    def _add_station(self):
        self._append_row()
        self._commit_table()

    def _remove_station(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if len(rows) >= self.table.rowCount():
            rows = rows[:-1]      # never drop below 1 remaining row here;
                                  # _commit_table below still enforces >= 2
                                  # before it reaches core.py
        for r in rows:
            self.table.removeRow(r)
        self._commit_table()

    def _commit_table(self, *_args):
        if self._syncing:
            return
        points = []
        for row in range(self.table.rowCount()):
            p_sb = self.table.cellWidget(row, 0)
            r_sb = self.table.cellWidget(row, 1)
            if p_sb is None or r_sb is None:
                continue
            points.append((p_sb.value(), r_sb.value()))
        self._syncing = True
        try:
            self.graph.set_points(points if points else [(0.0, 1.0), (1.0, 1.0)])
        finally:
            self._syncing = False
        self._commit(points)

    def _commit(self, points):
        params = [t for t, _r in points]
        radii = [r for _t, r in points]
        self.obj.Params = params
        self.obj.Radii = radii
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
        self._update_status()

    def _update_status(self):
        has_source = bool(getattr(self.obj, "Source", None))
        ref = getattr(self.obj, "Edge", None)
        has_edge = bool(ref and ref[0] and ref[1])
        n = self.table.rowCount()
        if not has_source:
            msg, ok = "Pick a solid source.", False
        elif not has_edge:
            msg, ok = "Pick one edge to round.", False
        elif n < 2:
            msg, ok = "Need >= 2 radius stations.", False
        else:
            msg, ok = "Ready. %d station(s)." % n, True
        self._status.setText(msg)
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class RollingBallFilletPanel(_EditPanelBase):
    """Constant (or multi-station variable) radius fillet SURFACE strip
    between two open faces -- RFS_RollingBallFillet. The real-world-unit
    radius counterpart to BlendSurfPanel's dimensionless Scale. Drives
    parametric.RollingBallFilletFP -> core.rolling_ball_fillet /
    core.multi_rail_fillet (the latter fires automatically once >= 2
    station rows are present).

    UI: same draggable law-curve graph as VariableFilletPanel (see
    rfstudio.panel_lawcurve.LawCurveWidget), gated behind a "Use tapered
    radius" toggle -- unlike VariableFilletPanel's stations (always
    required), RollingBallFilletFP.execute() treats an EMPTY station list
    as "use the single constant Radius spinbox instead" (see that
    function), so the toggle here just makes that either/or choice
    explicit instead of "add a 2nd table row to silently switch modes."
    """

    _Y_PAD = 1.3

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Rolling-ball fillet" if edit_obj is None else
                          "Edit RollingBallFillet",
                          lambda: parametric.make_surface("RollingBallFillet",
                                                          self.doc))
        self._syncing = False

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Rolling-ball fillet")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel("Select two surfaces, then assign:"))
        for slot_name in ("FaceA", "FaceB"):
            r = QtWidgets.QHBoxLayout()
            lbl = QtWidgets.QLabel(slot_name + ": " + self._name(slot_name))
            b = QtWidgets.QPushButton("Set source from selection")
            b.clicked.connect(
                lambda c=False, s=slot_name, l=lbl: self._assign(s, l))
            r.addWidget(lbl)
            r.addStretch(1)
            r.addWidget(b)
            lay.addLayout(r)

        rad_row = QtWidgets.QHBoxLayout()
        rad_row.addWidget(QtWidgets.QLabel("Radius (constant)"))
        self.radius = QtWidgets.QDoubleSpinBox()
        self.radius.setDecimals(3)
        self.radius.setRange(0.001, 1e5)
        self.radius.setSingleStep(0.5)
        self.radius.setValue(float(self.obj.Radius))
        rad_row.addWidget(self.radius)
        lay.addLayout(rad_row)
        self.radius.valueChanged.connect(lambda v: self._set("Radius", v))

        n_row = QtWidgets.QHBoxLayout()
        n_row.addWidget(QtWidgets.QLabel("Cross-section samples"))
        self.samples = QtWidgets.QSpinBox()
        self.samples.setRange(6, 200)
        self.samples.setValue(int(self.obj.Samples))
        n_row.addWidget(self.samples)
        lay.addLayout(n_row)
        self.samples.valueChanged.connect(lambda v: self._set("Samples", v))

        existing_params = list(getattr(self.obj, "StationParams", []) or [])
        self.taper_toggle = QtWidgets.QCheckBox(
            "Use tapered radius (law curve) instead of the constant "
            "Radius above")
        self.taper_toggle.setChecked(len(existing_params) >= 2)
        self.taper_toggle.toggled.connect(self._on_taper_toggled)
        lay.addWidget(self.taper_toggle)

        self.graph = LawCurveWidget(x_label="Seam param", y_label="Radius",
                                    y_min=0.0, y_max=1.0)
        self.graph.on_change = self._on_graph_changed
        self.graph.setToolTip(
            "Left-click empty space to add a station, drag a point to "
            "move it, right-click a point to delete it (>= 2 always kept).")
        lay.addWidget(self.graph)

        range_row = QtWidgets.QHBoxLayout()
        range_row.addWidget(QtWidgets.QLabel("Graph Y range"))
        self._y_min = QtWidgets.QDoubleSpinBox()
        self._y_min.setDecimals(3)
        self._y_min.setRange(0.0, 1e6)
        self._y_min.valueChanged.connect(self._on_range_changed)
        self._y_max = QtWidgets.QDoubleSpinBox()
        self._y_max.setDecimals(3)
        self._y_max.setRange(0.001, 1e6)
        self._y_max.valueChanged.connect(self._on_range_changed)
        range_row.addWidget(self._y_min)
        range_row.addWidget(QtWidgets.QLabel("to"))
        range_row.addWidget(self._y_max)
        auto_btn = QtWidgets.QPushButton("Auto")
        auto_btn.setToolTip("Rescale the graph to fit the current stations")
        auto_btn.clicked.connect(lambda: self._autoscale_graph(commit=False))
        range_row.addWidget(auto_btn)
        range_row.addStretch(1)
        lay.addLayout(range_row)

        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Param", "Radius"])
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.Stretch)
        self.table.setMaximumHeight(110)
        lay.addWidget(self.table)
        row_btns = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("Add station")
        add_btn.clicked.connect(self._add_station)
        rm_btn = QtWidgets.QPushButton("Remove selected")
        rm_btn.clicked.connect(self._remove_station)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(rm_btn)
        lay.addLayout(row_btns)

        self._fill_from_obj()
        self._apply_taper_enabled(self.taper_toggle.isChecked())

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
            self._prefill()
        self.recompute()

    def _name(self, slot):
        o = getattr(self.obj, slot)
        return o.Label if o else "(none)"

    def _assign(self, slot, lbl):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces]
        if sel:
            setattr(self.obj, slot, sel[0])
            lbl.setText(slot + ": " + self._name(slot))
            self.recompute()

    def _prefill(self):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces
               and s.Object is not self.obj]
        if len(sel) >= 1:
            self.obj.FaceA = sel[0]
        if len(sel) >= 2:
            self.obj.FaceB = sel[1]

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    # ---- taper toggle -------------------------------------------------- #
    def _apply_taper_enabled(self, on):
        self.graph.setEnabled(on)
        self.table.setEnabled(on)
        self.radius.setEnabled(not on)

    def _on_taper_toggled(self, on):
        self._apply_taper_enabled(on)
        if on:
            self._commit_stations(self.graph.points())
        else:
            self.obj.StationParams = []
            self.obj.StationRadii = []
            self.recompute()

    # ---- graph <-> table <-> obj sync ------------------------------- #
    def _fill_from_obj(self):
        params = list(getattr(self.obj, "StationParams", []) or [])
        radii = list(getattr(self.obj, "StationRadii", []) or [])
        if len(params) < 2:
            r0 = float(getattr(self.obj, "Radius", 5.0))
            params, radii = [0.0, 1.0], [r0, r0]
        self._autoscale_graph(points=list(zip(params, radii)), commit=False)
        self._set_views(list(zip(params, radii)))

    def _autoscale_graph(self, points=None, commit=True):
        pts = points if points is not None else self.graph.points()
        radii = [r for _t, r in pts] or [1.0]
        lo, hi = min(radii), max(radii)
        if hi - lo < 1e-6:
            lo, hi = max(0.0, lo - 1.0), hi + 1.0
        pad = (hi - lo) * (self._Y_PAD - 1.0) / 2.0
        lo, hi = max(0.0, lo - pad), hi + pad
        self._syncing = True
        try:
            self._y_min.setValue(lo)
            self._y_max.setValue(hi)
            self.graph.set_y_range(lo, hi)
        finally:
            self._syncing = False
        if commit:
            self._on_graph_changed(self.graph.points())

    def _on_range_changed(self, *_args):
        if self._syncing:
            return
        self.graph.set_y_range(self._y_min.value(), self._y_max.value())

    def _set_views(self, points):
        self._syncing = True
        try:
            self.graph.set_points(points)
            self.table.setRowCount(0)
            for t, r in sorted(points, key=lambda p: p[0]):
                self._append_row(t, r)
        finally:
            self._syncing = False

    def _on_graph_changed(self, points):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.table.setRowCount(0)
            for t, r in sorted(points, key=lambda p: p[0]):
                self._append_row(t, r)
        finally:
            self._syncing = False
        if self.taper_toggle.isChecked():
            self._commit_stations(points)

    def _append_row(self, p=0.5, r=5.0):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, val in enumerate((p, r)):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(3)
            sb.setRange(0.0, 1.0 if col == 0 else 1e6)
            sb.setSingleStep(0.05 if col == 0 else 0.5)
            sb.setValue(val)
            sb.valueChanged.connect(self._commit_table)
            self.table.setCellWidget(row, col, sb)

    def _add_station(self):
        self._append_row()
        self._commit_table()

    def _remove_station(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._commit_table()

    def _commit_table(self, *_args):
        if self._syncing:
            return
        points = []
        for row in range(self.table.rowCount()):
            p_sb = self.table.cellWidget(row, 0)
            r_sb = self.table.cellWidget(row, 1)
            if p_sb is None or r_sb is None:
                continue
            points.append((p_sb.value(), r_sb.value()))
        self._syncing = True
        try:
            self.graph.set_points(points if points else [(0.0, 5.0), (1.0, 5.0)])
        finally:
            self._syncing = False
        if self.taper_toggle.isChecked():
            self._commit_stations(points)

    def _commit_stations(self, points):
        self.obj.StationParams = [t for t, _r in points]
        self.obj.StationRadii = [r for _t, r in points]
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
        ok = bool(self.obj.FaceA) and bool(self.obj.FaceB)
        mode = ("tapered (%d stations)" % self.table.rowCount()
               if self.taper_toggle.isChecked() else
               "constant R=%g" % float(self.obj.Radius))
        self._status.setText(
            ("Ready -- %s." % mode) if ok else "Pick two surfaces.")
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")


class GlobalFairPanel(_EditPanelBase):
    """Global (network-wide) fairing pass -- RFS_GlobalFair. Drives
    parametric.GlobalFairFP -> core.global_fair_network: a discrete
    Laplacian relaxation of every selected face's control net, run for
    a fixed iteration count. See core.global_fair_network's docstring
    for the explicit "this is local relaxation smoothing, NOT a
    variational least-squares network solver" honesty -- the same
    scope discipline BalanceNetworkPanel already applies to continuity.
    """

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create GlobalFair" if edit_obj is None else
                          "Edit GlobalFair",
                          lambda: parametric.make_surface("GlobalFair",
                                                          self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Global fairing")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select the patch network's faces, then Run. This smooths "
            "each face's INTERIOR control net toward its neighbors; by "
            "default every seam/outer boundary is kept fixed."))

        self.group = _CurveGroup(self, "Faces", "Faces  (>= 1)",
                                 "Faces whose control nets will be relaxed")
        lay.addWidget(self.group.box)

        i_row = QtWidgets.QHBoxLayout()
        i_row.addWidget(QtWidgets.QLabel("Iterations"))
        self.iters = QtWidgets.QSpinBox()
        self.iters.setRange(1, 200)
        self.iters.setValue(int(self.obj.Iterations))
        i_row.addWidget(self.iters)
        lay.addLayout(i_row)
        self.iters.valueChanged.connect(lambda v: self._set("Iterations", v))

        l_row = QtWidgets.QHBoxLayout()
        l_row.addWidget(QtWidgets.QLabel("Lambda (relaxation strength)"))
        self.lam = QtWidgets.QDoubleSpinBox()
        self.lam.setRange(0.0, 1.0)
        self.lam.setSingleStep(0.05)
        self.lam.setDecimals(3)
        self.lam.setValue(float(self.obj.Lambda))
        l_row.addWidget(self.lam)
        lay.addLayout(l_row)
        self.lam.valueChanged.connect(lambda v: self._set("Lambda", v))

        self.fix_boundary = QtWidgets.QCheckBox(
            "Keep every seam/outer boundary fixed (recommended)")
        self.fix_boundary.setChecked(bool(self.obj.FixBoundary))
        self.fix_boundary.toggled.connect(
            lambda v: self._set("FixBoundary", v))
        lay.addWidget(self.fix_boundary)

        note = QtWidgets.QLabel(
            "This is a fixed-iteration-count Laplacian relaxation, not a "
            "convergence-checked solver -- rerun with more iterations if "
            "the network still looks rippled, and always re-check "
            "continuity afterward (Audit patch network) if 'keep "
            "boundary fixed' is unchecked.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa6b2;font-size:10px;padding:4px 0;")
        lay.addWidget(note)

        run = QtWidgets.QPushButton("Run")
        run.clicked.connect(self.recompute)
        lay.addWidget(run)

        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet("padding:6px 0;")
        lay.addWidget(self.result)

        if self.creating:
            sel = [o for o in _selected_objects() if o is not self.obj]
            if sel:
                self.group.objs = list(sel)
                self.group._refresh()
                self.group._commit()
        self.recompute()

    def _set(self, name, value):
        setattr(self.obj, name, value)

    def recompute(self, *args):
        try:
            self._recompute_obj()
        except Exception as exc:
            self.result.setText("Failed: %s" % exc)
            self.result.setStyleSheet("color:#c0392b;padding:6px 0;")
            self._defer_gui_update()
            return
        self._defer_gui_update()
        n = len(list(getattr(self.obj, "Faces", []) or []))
        if n == 0:
            self.result.setText("Select >= 1 face first.")
            self.result.setStyleSheet("color:#c08552;padding:6px 0;")
            return
        self.result.setText(
            "Ran on %d face(s). Object label carries the energy-before/"
            "after readout: %s" % (n, self.obj.Label))
        self.result.setStyleSheet("color:#7fae5a;padding:6px 0;")


class TrimByIntersectionPanel(_EditPanelBase):
    """Intersect two faces and trim both to the intersection curve
    (RFS_TrimByIntersection) -- the one-call 'surface-surface trim'
    operation. Drives parametric.TrimByIntersectionFP ->
    core.trim_faces_by_intersection. The result is a Compound of
    the two trimmed faces; the intersection wire is part of the
    doc object but is not separately highlighted (callers can
    rebuild to inspect via .Shape)."""

    def __init__(self, edit_obj=None):
        self._start_edit(edit_obj,
                          "Create TrimByIntersection" if edit_obj is None else
                          "Edit TrimByIntersection",
                          lambda: parametric.make_surface("TrimByIntersection",
                                                          self.doc))

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Trim by intersection")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select two faces; the operation intersects them and trims "
            "each to the side of the intersection containing the other "
            "face:"))
        for slot_name in ("FaceA", "FaceB"):
            r = QtWidgets.QHBoxLayout()
            lbl = QtWidgets.QLabel(slot_name + ": " + self._name(slot_name))
            b = QtWidgets.QPushButton("Set source from selection")
            b.clicked.connect(
                lambda c=False, s=slot_name, l=lbl: self._assign(s, l))
            r.addWidget(lbl)
            r.addStretch(1)
            r.addWidget(b)
            lay.addLayout(r)

        t_row = QtWidgets.QHBoxLayout()
        t_row.addWidget(QtWidgets.QLabel("Intersection tolerance"))
        self.tol = QtWidgets.QDoubleSpinBox()
        self.tol.setDecimals(6)
        self.tol.setRange(1e-7, 1.0)
        self.tol.setSingleStep(1e-4)
        self.tol.setValue(float(self.obj.Tolerance))
        t_row.addWidget(self.tol)
        lay.addLayout(t_row)
        self.tol.valueChanged.connect(lambda v: self._set("Tolerance", v))

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
            self._prefill()
        self.recompute()

    def _name(self, slot):
        o = getattr(self.obj, slot)
        return o.Label if o else "(none)"

    def _assign(self, slot, lbl):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces
               and s.Object is not self.obj]
        if sel:
            setattr(self.obj, slot, sel[0])
            lbl.setText(slot + ": " + self._name(slot))
            self.recompute()

    def _prefill(self):
        sel = [s.Object for s in FreeCADGui.Selection.getSelectionEx()
               if getattr(s.Object, "Shape", None) and s.Object.Shape.Faces
               and s.Object is not self.obj]
        if len(sel) >= 1:
            self.obj.FaceA = sel[0]
        if len(sel) >= 2:
            self.obj.FaceB = sel[1]

    def _set(self, name, value):
        setattr(self.obj, name, value)
        self.recompute()

    def recompute(self):
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
        ok = bool(self.obj.FaceA) and bool(self.obj.FaceB)
        self._status.setText(
            "Ready." if ok else "Pick two faces to intersect and trim.")
        self._status.setStyleSheet(
            ("color:#7fae5a;" if ok else "color:#c08552;")
            + "font-size:11px;padding:4px 0;")