# -*- coding: utf-8 -*-
"""Blender-Railform rail import panel. Split out of the old taskpanels.py
monolith -- see rfstudio.panel_base for shared helpers.

GUI-only. The geometry it drives lives in rfstudio.core (headless-tested).
"""

import FreeCAD
import FreeCADGui

from PySide import QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio.panel_base import _std_buttons
from rfstudio import groups


class ImportRailsPanel(object):
    """Import Blender-Railform rails (JSON) as exact NURBS curves."""

    def __init__(self):
        from rfstudio import core
        self._core = core
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform – Import rails")
        lay = QtWidgets.QVBoxLayout(self.form)
        lay.addWidget(QtWidgets.QLabel(
            "Railform rail JSON (degree / poles / knots per rail):"))
        self.path = QtWidgets.QLineEdit()
        lay.addWidget(self.path)
        b = QtWidgets.QPushButton("Browse…")
        b.clicked.connect(self._browse)
        lay.addWidget(b)

    def _browse(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.form, "Railform rail JSON", "", "JSON (*.json)")
        if fn:
            self.path.setText(fn)

    def getStandardButtons(self):
        return _std_buttons()

    def accept(self):
        import json
        path = self.path.text().strip()
        if path:
            with open(path, "r") as fh:
                data = json.load(fh)
            doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
            rail_dicts = data.get("rails", [])
            edges = self._core.rails_to_edges(data)
            # rails_to_edges() walks doc_dict["rails"] in order and returns
            # one (edge, continuity) tuple per entry, so zipping the two
            # lists pairs each rebuilt edge back up with its own source
            # dict -- that's what lets us name the object from the
            # interchange data itself. Previously every rail was named only
            # "Rail_<continuity>" (e.g. "Rail_G1", "Rail_G1001", ...): with
            # tens of rails from a real export, that gave zero traceability
            # back to which rail was which once FreeCAD's auto-numbering
            # kicked in. Prefer an explicit "name" field in the interchange
            # JSON when the exporter provides one; otherwise fall back to
            # an index + continuity tag, which is at least stable and
            # ordered rather than an anonymous counter.
            for i, (rail, (edge, cont)) in enumerate(zip(rail_dicts, edges)):
                label = rail.get("name") or ("Rail_%02d_%s" % (i, cont))
                obj = doc.addObject("Part::Feature", label.replace(" ", "_"))
                obj.Label = label
                obj.Shape = edge
                groups.add_to_group(obj, "Imported")
            doc.recompute()
        FreeCADGui.Control.closeDialog()

    def reject(self):
        FreeCADGui.Control.closeDialog()
