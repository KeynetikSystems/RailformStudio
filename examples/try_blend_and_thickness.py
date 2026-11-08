# -*- coding: utf-8 -*-
"""Try Railform Studio's blend, continuity readout and virtual thickness.

Run it either way (no UI interaction needed):

  * FreeCAD GUI:   Macro > Macros... (or the Python console):
        exec(open(r"<path to>/examples/try_blend_and_thickness.py").read())
  * Headless:      FreeCADCmd.exe examples/try_blend_and_thickness.py

It builds two curved Coons patches with a gap between them, blends them (G2),
prints the numeric continuity grade at both seams, then gives the blend a
wall thickness and prints the resulting solid. In the GUI the objects appear
in the tree and 3D view.
"""
import os
import sys

import FreeCAD
import Part
from FreeCAD import Vector as V

# Make `rfstudio` importable when the workbench isn't loaded (headless run).
_here = os.path.dirname(os.path.abspath(globals().get("__file__", "examples/x.py")))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from rfstudio import core, parametric  # noqa: E402

doc = FreeCAD.newDocument("RailformDemo")


def _edge(p0, p1, bulge=2.0):
    """A shallow arc between p0 and p1 (bulges in +Z)."""
    mid = (p0 + p1) * 0.5 + V(0, 0, bulge)
    obj = doc.addObject("Part::Feature", "Edge")
    obj.Shape = Part.Arc(p0, mid, p1).toShape()
    return obj


def _patch(x0, x1, name):
    a, b, c, d = V(x0, 0, 0), V(x1, 0, 0), V(x1, 20, 0), V(x0, 20, 0)
    f = parametric.make_surface("Coons", doc)
    f.Label = name
    f.Boundaries = [_edge(a, b), _edge(b, c), _edge(c, d), _edge(d, a)]
    return f


left = _patch(-20, 0, "LeftPatch")
right = _patch(5, 25, "RightPatch")

blend = parametric.make_surface("BlendSurf", doc)
blend.FaceA, blend.FaceB = left, right
blend.Continuity = "G2"
doc.recompute()

# --- numeric continuity readout (what the Surface review panel reports) ---
for name, patch in (("left seam", left), ("right seam", right)):
    rep = core.surface_deviation(patch.Shape.Faces[0], blend.Shape.Faces[0])
    print("%-10s  G0 %.2e   G1 %.4f deg   G2 %.5f (1/mm)   -> %s" % (
        name, rep["max_g0"], rep["max_g1_deg"], rep["max_g2"],
        core.classify_continuity(rep)))

# --- virtual thickness: just set a property; the surface stays editable ---
blend.Thickness = 1.5
blend.ThicknessMode = "Symmetric"
doc.recompute()
print("blend thickness:", blend.ThicknessStatus,
      "| solids:", len(blend.Shape.Solids),
      "| volume: %.1f mm^3" % blend.Shape.Volume)

blend.Thickness = 0.0           # back to the plain surface at any time
doc.recompute()
print("thickness off  :", blend.ThicknessStatus,
      "| solids:", len(blend.Shape.Solids))
blend.Thickness = 1.5           # leave the thick version in the document
doc.recompute()

if FreeCAD.GuiUp:
    import FreeCADGui
    FreeCADGui.SendMsgToActiveView("ViewFit")
