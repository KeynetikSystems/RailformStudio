# -*- coding: utf-8 -*-
"""The workbench class lives HERE, not in InitGui.py.

FreeCAD exec()s InitGui.py in a namespace without `__file__`, so any
`os.path.dirname(__file__)` there raises "name '__file__' is not defined" and the
whole workbench silently fails to register. Inside a real imported module
(this one) `__file__` is defined, so icon-path resolution is safe. InitGui.py is
reduced to a two-line import (the pattern Curves itself uses).
"""

import os
import FreeCAD
import FreeCADGui

ICON = os.path.join(os.path.dirname(__file__), "resources", "rfstudio.svg")


class RailformStudioWorkbench(FreeCADGui.Workbench):

    MenuText = "Railform Studio"
    ToolTip = ("Curated curve->surface modeling over an optional Curves "
               "fallback engine, plus Blender rail import")
    Icon = ICON

    def Initialize(self):
        from rfstudio import commands, deps, engines

        commands.register()
        for toolbar_name, cmd_ids in commands.TOOLBARS:
            self.appendToolbar(toolbar_name, cmd_ids)
            self.appendMenu(toolbar_name.replace("Railform ", ""), cmd_ids)

        st = deps.status()
        missing = [k for k, v in st.items() if not v]
        if missing:
            FreeCAD.Console.PrintWarning(
                "Railform Studio: optional engine(s) not found: %s. Commands "
                "needing them are disabled. Install via Addon Manager.\n"
                % ", ".join(missing))
        else:
            FreeCAD.Console.PrintMessage(
                "Railform Studio: optional engine detected: %s.\n"
                % ", ".join(st))

        # One consolidated check of every curated -> engine command mapping,
        # rather than letting each one fail individually the first time a
        # user happens to click it (see deps.verify_delegations docstring).
        # Wrap in try-except to handle cases where GUI is not fully initialized yet.
        try:
            deps.verify_delegations(engines.DELEGATIONS)
        except Exception as e:
            FreeCAD.Console.PrintMessage("Railform Studio: Skipping delegation verification due to GUI initialization: %s" % e)

        # Visibility for the "two hard external dependencies" risk: which
        # curated actions have no native fallback at all, so a future
        # Curves/Silk breakage silently strands them (see
        # deps.report_single_points_of_failure docstring).
        deps.report_single_points_of_failure(engines.PROVIDERS)

        # Static wiring check of every provider chain (catches typos in
        # NATIVE_OVERRIDES / DELEGATIONS mismatches up front, at
        # activation time, instead of as a dead button at click time).
        problems = engines.validate_providers()
        if problems:
            FreeCAD.Console.PrintWarning(
                "Railform Studio: %d provider-chain problem(s): %s\n"
                % (len(problems), "; ".join(problems)))

    def Activated(self):
        try:
            from rfstudio import toolshelf
            toolshelf.show()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform Studio: tool shelf failed (%s)\n" % exc)

    def Deactivated(self):
        try:
            from rfstudio import toolshelf
            toolshelf.hide()
        except Exception:
            pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"
