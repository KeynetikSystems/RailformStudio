# -*- coding: utf-8 -*-
"""Railform Studio -- GUI entry point.

Kept deliberately trivial: FreeCAD exec()s this file WITHOUT a `__file__` in
scope, so no path logic may live here. The real workbench class (which needs
`__file__` for its icon) is in rfstudio/workbench.py, a normal importable module.
"""

import FreeCADGui
from rfstudio.workbench import RailformStudioWorkbench

FreeCADGui.addWorkbench(RailformStudioWorkbench())
