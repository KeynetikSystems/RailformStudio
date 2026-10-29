# -*- coding: utf-8 -*-
"""Class-A QA / review task panels: the surface review summary and the
surfacing workflow guide. Split out of the old taskpanels.py monolith --
see rfstudio.panel_base for the shared _ReportPanelBase scaffolding and
selection helpers this module builds on.

These panels run-and-report on demand; there's no parametric doc object to
keep re-editable about a QA measurement, so (unlike panel_curve/
panel_surface) they only need a Close button, not a commit/abort
transaction.

GUI-only. The geometry it drives lives in rfstudio.core (headless-tested).
"""

from FreeCAD import Vector

from PySide import QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio import panel_logic
from rfstudio.panel_base import _ReportPanelBase, _selected_faces


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

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("padding:6px 0; font-size:11px;")
        lay.addWidget(self.status)

        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet("padding:6px 0;")
        lay.addWidget(self.result)

        self._assign()

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


