# -*- coding: utf-8 -*-
"""Class-A QA / review task panels: numeric continuity verification and the
surfacing workflow guide. Split out of the old taskpanels.py monolith --
see rfstudio.panel_base for the shared _ReportPanelBase scaffolding and
selection helpers this module builds on.

These panels run-and-report on demand; there's no parametric doc object to
keep re-editable about a QA measurement, so (unlike panel_curve/
panel_surface) they only need a Close button, not a commit/abort
transaction.

GUI-only. The geometry it drives lives in rfstudio.core (headless-tested).
"""

import FreeCAD
import FreeCADGui
import Part
from FreeCAD import Vector

from PySide import QtCore, QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio import panel_logic, groups
from rfstudio.panel_base import _ReportPanelBase, _selected_faces


class ContinuityCheckPanel(_ReportPanelBase):
    """Numeric G0/G1/G2 seam QA between two adjacent faces --
    core.surface_deviation + classify_continuity. Answers "this seam
    reads as G1, not the G2 we wanted" instead of just trusting
    MatchSurf/BlendSurf's target setting."""

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.faceA = None
        self.faceB = None
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Check seam (G0/G1/G2)")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select (or box-select) the two adjacent faces, then Run:"))

        row = QtWidgets.QHBoxLayout()
        self._lbl = QtWidgets.QLabel("Faces: (none)")
        b = QtWidgets.QPushButton("Set source from selection")
        b.clicked.connect(self._assign)
        row.addWidget(self._lbl)
        row.addStretch(1)
        row.addWidget(b)
        lay.addLayout(row)

        tol_row = QtWidgets.QHBoxLayout()
        tol_row.addWidget(QtWidgets.QLabel("G0 tol"))
        self.g0 = QtWidgets.QDoubleSpinBox()
        self.g0.setDecimals(5)
        self.g0.setRange(1e-6, 10.0)
        self.g0.setValue(1e-3)
        tol_row.addWidget(self.g0)
        tol_row.addWidget(QtWidgets.QLabel("G1 tol (deg)"))
        self.g1 = QtWidgets.QDoubleSpinBox()
        self.g1.setDecimals(3)
        self.g1.setRange(1e-3, 45.0)
        self.g1.setValue(0.5)
        tol_row.addWidget(self.g1)
        tol_row.addWidget(QtWidgets.QLabel("G2 tol"))
        self.g2 = QtWidgets.QDoubleSpinBox()
        self.g2.setDecimals(4)
        self.g2.setRange(1e-6, 10.0)
        self.g2.setValue(1e-2)
        tol_row.addWidget(self.g2)
        lay.addLayout(tol_row)

        run = QtWidgets.QPushButton("Run")
        run.clicked.connect(self._run)
        lay.addWidget(run)

        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)

    def _assign(self):
        faces = _selected_faces()
        if len(faces) >= 2:
            self.faceA, self.faceB = faces[0][0], faces[1][0]
            self._lbl.setText("Faces: %s <-> %s"
                              % (faces[0][1].Label, faces[1][1].Label))
        else:
            self._lbl.setText(
                "Faces: need 2 (got %d) -- select 2 faces/objects" % len(faces))

    def _run(self):
        if self.faceA is None or self.faceB is None:
            self.result.setText("Select two faces first.")
            return
        try:
            rep = self._core.surface_deviation(self.faceA, self.faceB)
            grade = self._core.classify_continuity(
                rep, g0_tol=self.g0.value(), g1_tol_deg=self.g1.value(),
                g2_tol=self.g2.value())
        except Exception as exc:
            self.result.setText("Failed: %s" % exc)
            return
        color = {"G2": "#7fae5a", "G1": "#c08552", "G0": "#c08552",
                "FAIL": "#c0392b"}.get(grade, "#9aa6b2")
        self.result.setText(
            "Grade: <b>%s</b><br>max G0 (gap) = %.5f<br>"
            "max G1 (deg) = %.4f<br>max G2 (curvature delta) = %.5f"
            % (grade, rep["max_g0"], rep["max_g1_deg"], rep["max_g2"]))
        self.result.setStyleSheet("color:%s;padding:6px 0;" % color)


class NetworkAuditPanel(_ReportPanelBase):
    """Assembly-scale continuity audit -- core.network_continuity_report
    across every face in the selection (or the whole document if nothing
    is selected). Finds every adjacent seam and grades it, so the "what's
    the worst unresolved seam in this whole model" question a real
    Class-A surfacing review asks doesn't require checking pairs one at a
    time with ContinuityCheckPanel alone."""

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Network continuity audit")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Audits every adjacent face pair in the current selection "
            "(or the whole document if nothing is selected)."))

        run = QtWidgets.QPushButton("Run audit")
        run.clicked.connect(self._run)
        lay.addWidget(run)

        fair_btn = QtWidgets.QPushButton("Fair network…")
        fair_btn.setToolTip(
            "Open the global fairing panel (RFS_GlobalFair) so this "
            "network's control nets can be relaxed toward each other, "
            "not just audited.")
        fair_btn.clicked.connect(self._open_fairing)
        lay.addWidget(fair_btn)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Face A", "Face B", "Grade", "max G0"])
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.Stretch)
        self.table.setMinimumHeight(200)
        lay.addWidget(self.table)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

    def _gather_faces(self):
        """Selected objects' faces, or every shaped object in the document
        if nothing is selected."""
        sel = FreeCADGui.Selection.getSelection()
        objs = sel if sel else (list(FreeCAD.ActiveDocument.Objects)
                                if FreeCAD.ActiveDocument else [])
        faces, labels = [], []
        for o in objs:
            shp = getattr(o, "Shape", None)
            if shp is None:
                continue
            for i, f in enumerate(shp.Faces):
                faces.append(f)
                labels.append("%s.Face%d" % (getattr(o, "Label", o.Name), i + 1))
        return faces, labels

    def _run(self):
        faces, labels = self._gather_faces()
        if len(faces) < 2:
            self.summary.setText(
                "Need >= 2 faces (select objects, or leave selection "
                "empty to scan the whole document).")
            return
        try:
            rep = self._core.network_continuity_report(faces)
        except Exception as exc:
            self.summary.setText("Failed: %s" % exc)
            return
        order = {"FAIL": 0, "G0": 1, "G1": 2, "G2": 3}
        seams = sorted(rep["seams"], key=lambda s: order[s["grade"]])
        self.table.setRowCount(len(seams))
        colors = {"G2": "#7fae5a", "G1": "#c08552", "G0": "#c08552",
                 "FAIL": "#c0392b"}
        for row, s in enumerate(seams):
            self.table.setItem(row, 0,
                               QtWidgets.QTableWidgetItem(labels[s["i"]]))
            self.table.setItem(row, 1,
                               QtWidgets.QTableWidgetItem(labels[s["j"]]))
            grade_item = QtWidgets.QTableWidgetItem(s["grade"])
            grade_item.setForeground(
                QtGui.QBrush(QtGui.QColor(colors.get(s["grade"], "#9aa6b2"))))
            self.table.setItem(row, 2, grade_item)
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(
                "%.5f" % s["report"]["max_g0"]))
        n_iso = len(rep["isolated"])
        iso_txt = ((" -- %d face(s) with no detected neighbour: %s"
                   % (n_iso, ", ".join(labels[k] for k in rep["isolated"])))
                  if n_iso else "")
        self.summary.setText(
            "%d seam(s) audited. Worst grade: %s%s"
            % (len(seams), rep["worst_grade"] or "n/a", iso_txt))

    def _open_fairing(self):
        """Hand off to GlobalFairPanel. _gather_faces() above returns raw
        Part.Face objects (with only a label string, no owning document
        object), which can't be fed to GlobalFairPanel's _CurveGroup --
        that widget links whole document OBJECTS, not bare faces. So
        this doesn't try to carry the audited face list across; it just
        opens the fairing panel fresh, which prefills itself from the
        CURRENT selection (see GlobalFairPanel.__init__) -- normally
        still the same objects the user just audited."""
        from rfstudio import taskpanels
        if FreeCADGui.Control.activeDialog():
            try:
                FreeCADGui.Control.closeDialog()
            except Exception:
                pass
        FreeCADGui.Control.showDialog(taskpanels.GlobalFairPanel())


class SurfaceReviewPanel(_ReportPanelBase):
    """A compact review panel that rolls continuity + draft into one summary."""

    _DEFAULT_PULL = (0.0, 0.0, 1.0)

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Surface review")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select faces/objects, then Run to get a quick review summary."))

        row = QtWidgets.QHBoxLayout()
        self._lbl = QtWidgets.QLabel("Faces: (none)")
        b = QtWidgets.QPushButton("Set source from selection")
        b.clicked.connect(self._assign)
        row.addWidget(self._lbl)
        row.addStretch(1)
        row.addWidget(b)
        lay.addLayout(row)

        # 3 spinboxes (X/Y/Z), consistent with the spinbox convention every
        # other panel uses -- this used to be a comma-separated QLineEdit
        # ("0,0,1"), the one place in the workbench a typo/parse error could
        # silently drop the draft direction.
        lay.addWidget(QtWidgets.QLabel("Draft pull direction"))
        pull_row = QtWidgets.QHBoxLayout()
        self._pull_spins = []
        for axis, default in zip(("X", "Y", "Z"), self._DEFAULT_PULL):
            pull_row.addWidget(QtWidgets.QLabel(axis))
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(4)
            sb.setRange(-1.0, 1.0)
            sb.setSingleStep(0.1)
            sb.setValue(default)
            pull_row.addWidget(sb)
            self._pull_spins.append(sb)
        lay.addLayout(pull_row)

        run = QtWidgets.QPushButton("Run review")
        run.clicked.connect(self._run)
        lay.addWidget(run)

        fair_btn = QtWidgets.QPushButton("Fair network…")
        fair_btn.setToolTip(
            "Open the global fairing panel (RFS_GlobalFair) so this "
            "network's control nets can be relaxed toward each other, "
            "not just reviewed.")
        fair_btn.clicked.connect(self._open_fairing)
        lay.addWidget(fair_btn)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("padding:6px 0; font-size:11px;")
        lay.addWidget(self.status)

        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet("padding:6px 0;")
        lay.addWidget(self.result)

        self._assign()

    def _open_fairing(self):
        """Hand off to GlobalFairPanel, same as NetworkAuditPanel's own
        'Fair network…' button. self.faces (set by _assign()/_run() below)
        holds raw Part.Face objects with no owning document object, which
        can't be fed to GlobalFairPanel's _CurveGroup (that widget links
        whole document OBJECTS, not bare faces) -- so this doesn't try to
        carry the reviewed face list across; it opens the fairing panel
        fresh, which prefills itself from the CURRENT selection (see
        GlobalFairPanel.__init__), normally still the same objects just
        reviewed here."""
        from rfstudio import taskpanels
        if FreeCADGui.Control.activeDialog():
            try:
                FreeCADGui.Control.closeDialog()
            except Exception:
                pass
        FreeCADGui.Control.showDialog(taskpanels.GlobalFairPanel())

    def _infer_pull_dir(self, faces):
        """Extract a mid-parameter normal per face (needs the live document
        geometry, so stays here); the actual averaging-into-one-direction
        math is pure and lives in panel_logic.infer_pull_direction, where
        it's covered by test_panel_logic.py under freecadcmd."""
        normals = []
        for face in faces:
            try:
                u0, u1, v0, v1 = face.ParameterRange
                n = face.normalAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
                normals.append((n.x, n.y, n.z))
            except Exception:
                continue
        return panel_logic.infer_pull_direction(normals)

    def _assign(self):
        faces = _selected_faces()
        if faces:
            self.faces = [f[0] for f in faces]
            self._lbl.setText("Faces: %d selected" % len(self.faces))
            self.status.setText("Selection detected: %d face(s)." % len(self.faces))
            self.status.setStyleSheet("color:#7fae5a;padding:6px 0;font-size:11px;")
            current = tuple(sb.value() for sb in self._pull_spins)
            if current == self._DEFAULT_PULL:
                inferred = self._infer_pull_dir(self.faces)
                if inferred is not None:
                    for sb, v in zip(self._pull_spins, inferred):
                        sb.setValue(v)
        else:
            self.faces = []
            self._lbl.setText("Faces: (none)")
            self.status.setText("No faces selected yet.")
            self.status.setStyleSheet("color:#c08552;padding:6px 0;font-size:11px;")

    def _pull_dir(self):
        return tuple(sb.value() for sb in self._pull_spins)

    def _run(self):
        if not getattr(self, "faces", None):
            self.faces = []
            faces = _selected_faces()
            if faces:
                self.faces = [f[0] for f in faces]
        if len(self.faces) < 2:
            self.result.setText("Select at least two faces for a meaningful review.")
            self.status.setText("Need at least two faces to review continuity.")
            self.status.setStyleSheet("color:#c08552;padding:6px 0;font-size:11px;")
            return
        try:
            summary = self._core.surface_review_summary(
                self.faces, pull_dir=Vector(*self._pull_dir()))
            cont = summary["continuity"]
            draft = summary["draft"]
            rec = self._core.review_recommendation(summary)
            color = {"good": "#7fae5a", "watch": "#c08552", "fix": "#c0392b"}.get(rec["status"], "#9aa6b2")
            self.status.setText("Recommendation: <b>%s</b>" % rec["status"].upper())
            self.status.setStyleSheet("color:%s;padding:6px 0;font-size:11px;" % color)
            self.result.setText(
                "Continuity: <b>%s</b><br>Worst seam: %s<br>Draft worst angle: %s<br>Next step: %s"
                % (cont.get("worst_grade") or "n/a",
                   cont.get("worst_seam") or "n/a",
                   ("%.2f" % draft.get("worst_angle")) if draft.get("worst_angle") is not None else "n/a",
                   rec.get("step") or "n/a"))
            self.result.setStyleSheet("color:%s;padding:6px 0;" % color)
        except Exception as exc:
            self.status.setText("Review failed.")
            self.status.setStyleSheet("color:#c0392b;padding:6px 0;font-size:11px;")
            self.result.setText("Failed: %s" % exc)


class WorkflowGuidePanel(_ReportPanelBase):
    """A more guided, step-by-step workflow panel for the surfacing loop."""

    def __init__(self):
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Workflow guide")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "A practical surfacing sequence for the current workbench:"))
        text = QtWidgets.QTextBrowser()
        text.setHtml(
            "<h3>1. Start with a clean patch network</h3>"
            "<p>Use Loft, Gordon, Sweep, or Fill to build the first shell of the surface.</p>"
            "<h3>2. Align the seam to a reference</h3>"
            "<p>Use Match to make one edge flow with its neighbour before refining the patch.</p>"
            "<h3>3. Review the patch network</h3>"
            "<p>Run the review panel and inspect the worst seam plus draft angle before you continue.</p>"
            "<h3>4. Validate for manufacture</h3>"
            "<p>Use Offset, Thicken, and Shell to check whether the design is manufacturable.</p>"
            "<h3>5. Iterate until the status is green</h3>"
            "<p>Revisit the network until continuity and draft are both acceptable.</p>")
        lay.addWidget(text)


class GaussianCurvaturePanel(_ReportPanelBase):
    """Gaussian curvature analysis: color-coded map (blue=saddle, green=flat, red=patch)."""

    _DEFAULT_SAMPLES = 10  # samples per parameter direction

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Gaussian curvature")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select faces/objects, then Run to compute Gaussian curvature."))

        row = QtWidgets.QHBoxLayout()
        self._lbl = QtWidgets.QLabel("Faces: (none)")
        b = QtWidgets.QPushButton("Set source from selection")
        b.clicked.connect(self._assign)
        row.addWidget(self._lbl)
        row.addStretch(1)
        row.addWidget(b)
        lay.addLayout(row)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("padding:6px 0;font-size:11px;")
        lay.addWidget(self.status)

        # Sample count
        lay.addWidget(QtWidgets.QLabel("Samples per direction"))
        self.sample_spin = QtWidgets.QSpinBox()
        self.sample_spin.setMinimum(1)
        self.sample_spin.setMaximum(50)
        self.sample_spin.setValue(self._DEFAULT_SAMPLES)
        lay.addWidget(self.sample_spin)

        # Thresholds for flat vs curved
        lay.addWidget(QtWidgets.QLabel("Flat threshold"))
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setDecimals(6)
        self.threshold_spin.setRange(0.0, 1.0)
        self.threshold_spin.setValue(1e-4)
        lay.addWidget(self.threshold_spin)

        run = QtWidgets.QPushButton("Run")
        run.clicked.connect(self._run)
        lay.addWidget(run)

        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)

        self.faces = []
        self._assign()

    def _assign(self):
        faces = _selected_faces()
        if faces:
            self.faces = [f[0] for f in faces]
            self._lbl.setText("Faces: %d selected" % len(self.faces))
            self.status.setText("Selection detected: %d face(s)." % len(self.faces))
            self.status.setStyleSheet("color:#7fae5a;padding:6px 0;font-size:11px;")
        else:
            self.faces = []
            self._lbl.setText("Faces: (none)")
            self.status.setText("No faces selected yet.")
            self.status.setStyleSheet("color:#c08552;padding:6px 0;font-size:11px;")

    def _run(self):
        if not getattr(self, "faces", None):
            self.faces = []
            faces = _selected_faces()
            if faces:
                self.faces = [f[0] for f in faces]
        if not self.faces:
            self.result.setText("Select at least one face.")
            return
        try:
            samples = self.sample_spin.value()
            threshold = self.threshold_spin.value()
            total_points = 0
            saddle_count = 0
            flat_count = 0
            patch_count = 0
            sum_curvature = 0.0
            for face in self.faces:
                u0, u1, v0, v1 = face.ParameterRange
                for i in range(samples):
                    u = u0 + (u1 - u0) * i / max(samples - 1, 1)
                    for j in range(samples):
                        v = v0 + (v1 - v0) * j / max(samples - 1, 1)
                        k = self._core.gaussian_curvature(face, u, v)
                        total_points += 1
                        sum_curvature += k
                        if abs(k) < threshold:
                            flat_count += 1
                        elif k < 0:
                            saddle_count += 1
                        else:
                            patch_count += 1
            if total_points == 0:
                self.result.setText("No points sampled.")
                return
            avg_curvature = sum_curvature / total_points
            saddle_pct = (saddle_count / total_points) * 100
            flat_pct = (flat_count / total_points) * 100
            patch_pct = (patch_count / total_points) * 100
            self.result.setText(
                f"Sampled {total_points} points.<br>"
                f"Average Gaussian curvature: {avg_curvature:.6f}<br>"
                f"Saddle (K < -{threshold}): {saddle_count} ({saddle_pct:.1f}%)<br>"
                f"Flat (|K| < {threshold}): {flat_count} ({flat_pct:.1f}%)<br>"
                f"Patch (K > {threshold}): {patch_count} ({patch_pct:.1f}%)"
            )
            # Color-coded summary based on dominant curvature type
            if saddle_count > flat_count and saddle_count > patch_count:
                color = "#0000ff"  # blue for saddle
                summary_text = "Predominantly saddle-shaped"
            elif patch_count > saddle_count and patch_count > flat_count:
                color = "#ff0000"  # red for patch
                summary_text = "Predominantly patch-shaped"
            else:
                color = "#00ff00"  # green for flat
                summary_text = "Predominantly flat"
            self.result.setText(f"<b>{summary_text}</b><br>" + self.result.text())
            self.result.setStyleSheet(f"color:{color};padding:6px 0;")
        except Exception as exc:
            self.result.setText("Failed: %s" % exc)
            return


class BalanceNetworkPanel(_ReportPanelBase):
    """Iteratively balance a patch network's continuity (RFS_BalanceNetwork)
    -- the FIRST user-facing tool in this workbench that actually TRIES
    to FIX continuity, not just audit it. The existing
    NetworkAuditPanel says "this seam is G0"; this panel rebuilds the
    worse-of-the-two faces via match_surface at the requested target,
    re-evaluates, and iterates until stable or the cap is hit.

    Wraps core.balance_network_continuity directly (no per-object
    doc-state; the result is a new Compound on the document, re-runnable
    by hitting Run again). Local-relaxation pass only -- NOT a true
    simultaneous least-squares network solver (that would be a much
    larger piece of work; see core.balance_network_continuity's
    docstring for the explicit "what this is and isn't" honesty)."""

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.faces = []
        self.refs = []
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Balance patch network")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Select the network's faces, then Run. Use 'Add refs' for "
            "the (optional) reference set -- faces that should NOT be "
            "rebuilt, only matched against."))

        row = QtWidgets.QHBoxLayout()
        self._lbl = QtWidgets.QLabel("Network faces: (none)")
        b = QtWidgets.QPushButton("Add selected faces")
        b.clicked.connect(self._add_faces)
        row.addWidget(self._lbl)
        row.addStretch(1)
        row.addWidget(b)
        lay.addLayout(row)
        clr = QtWidgets.QPushButton("Clear faces")
        clr.clicked.connect(self._clear_faces)
        lay.addWidget(clr)

        ref_row = QtWidgets.QHBoxLayout()
        self._ref_lbl = QtWidgets.QLabel("References: (none)")
        b2 = QtWidgets.QPushButton("Add refs from selection")
        b2.clicked.connect(self._add_refs)
        ref_row.addWidget(self._ref_lbl)
        ref_row.addStretch(1)
        ref_row.addWidget(b2)
        lay.addLayout(ref_row)
        clr2 = QtWidgets.QPushButton("Clear refs")
        clr2.clicked.connect(self._clear_refs)
        lay.addWidget(clr2)

        c_row = QtWidgets.QHBoxLayout()
        c_row.addWidget(QtWidgets.QLabel("Target continuity"))
        self.target = QtWidgets.QComboBox()
        self.target.addItems(["G1", "G2"])
        self.target.setCurrentText("G2")
        c_row.addWidget(self.target)
        lay.addLayout(c_row)

        i_row = QtWidgets.QHBoxLayout()
        i_row.addWidget(QtWidgets.QLabel("Max iterations"))
        self.max_iter = QtWidgets.QSpinBox()
        self.max_iter.setRange(1, 50)
        self.max_iter.setValue(5)
        i_row.addWidget(self.max_iter)
        lay.addLayout(i_row)

        run = QtWidgets.QPushButton("Run")
        run.clicked.connect(self._run)
        lay.addWidget(run)

        note = QtWidgets.QLabel(
            "This is local relaxation, not a true simultaneous network "
            "solver: it finds the worst seam and rebuilds the worse of "
            "its two faces via match_surface, then re-evaluates. "
            "Oscillation is possible; the iteration cap and the "
            "converged/capped verdict in the readout below are the "
            "honest signal.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa6b2;font-size:10px;padding:4px 0;")
        lay.addWidget(note)

        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextFormat(QtCore.Qt.RichText)
        self.result.setStyleSheet("padding:6px 0;")
        lay.addWidget(self.result)

    def _add_faces(self):
        for o in FreeCADGui.Selection.getSelection():
            if (getattr(o, "Shape", None) is not None
                    and o.Shape.Faces and o not in self.faces):
                self.faces.append(o)
        self._lbl.setText(
            "Network faces: %d (%s)"
            % (len(self.faces),
               ", ".join(getattr(o, "Label", o.Name) for o in self.faces))
            if self.faces else "Network faces: (none)")

    def _clear_faces(self):
        self.faces = []
        self._lbl.setText("Network faces: (none)")

    def _add_refs(self):
        for o in FreeCADGui.Selection.getSelection():
            if (getattr(o, "Shape", None) is not None
                    and o.Shape.Faces and o not in self.refs):
                self.refs.append(o)
        self._ref_lbl.setText(
            "References: %d (%s)"
            % (len(self.refs),
               ", ".join(getattr(o, "Label", o.Name) for o in self.refs))
            if self.refs else "References: (none)")

    def _clear_refs(self):
        self.refs = []
        self._ref_lbl.setText("References: (none)")

    def _run(self):
        if not self.faces:
            self.result.setText("Need at least one face in the network.")
            self.result.setStyleSheet("color:#c08552;padding:6px 0;")
            return
        face_list = []
        for o in self.faces:
            shp = getattr(o, "Shape", None)
            if shp is not None and shp.Faces:
                face_list.append(shp.Faces[0])
        ref_list = []
        for o in self.refs:
            shp = getattr(o, "Shape", None)
            if shp is not None and shp.Faces:
                ref_list.append(shp.Faces[0])
        try:
            rep = self._core.balance_network_continuity(
                face_list,
                references=ref_list,
                target=str(self.target.currentText()),
                max_iter=int(self.max_iter.value()))
        except Exception as exc:
            self.result.setText("Failed: %s" % exc)
            self.result.setStyleSheet("color:#c0392b;padding:6px 0;")
            return
        # Build a Compound of the rebuilt faces (None entries => original).
        out = []
        rebuilt_count = 0
        for idx, new_shape in rep["shapes"]:
            if new_shape is None:
                if idx < len(face_list):
                    out.append(face_list[idx])
            else:
                out.append(new_shape)
                rebuilt_count += 1
        try:
            doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
            obj = doc.addObject("Part::Feature", "BalancedNetwork")
            obj.Shape = Part.Compound(out)
            groups.add_to_group(obj, "Surfaces")
            doc.recompute()
        except Exception as exc:
            self.result.setText("Rebuilt, but couldn't create Compound: %s" % exc)
            self.result.setStyleSheet("color:#c08552;padding:6px 0;")
            return
        verdict = "converged" if rep["converged"] else "capped (max iter)"
        colors = {"G2": "#7fae5a", "G1": "#c08552", "G0": "#c08552",
                 "FAIL": "#c0392b", None: "#9aa6b2"}
        c_final = colors.get(rep["final_worst"], "#9aa6b2")
        c_init = colors.get(rep["initial_worst"], "#9aa6b2")
        self.result.setText(
            "<b>%d</b> of %d face(s) rebuilt in <b>%d</b> iteration(s) "
            "(%s).<br>Worst seam: <b style='color:%s'>%s</b> &rarr; "
            "<b style='color:%s'>%s</b>.<br>New object: <i>%s</i>"
            % (rebuilt_count, len(face_list), rep["iterations"], verdict,
               c_init, rep["initial_worst"] or "?",
               c_final, rep["final_worst"] or "?",
               obj.Label))
        self.result.setStyleSheet("padding:6px 0;")