# -*- coding: utf-8 -*-
"""Command layer + curated toolbar/menu registry.

Two kinds of curated command:
  * ProviderCommand  -- backs a curated action with an ORDERED CHAIN of
                        providers (engines.PROVIDERS): an engine command,
                        a native implementation, or both with one as
                        fallback. Replaces the old fixed 1:1 DelegateCommand
                        so a curated action can survive its engine being
                        missing or renamed -- see engines.py for the chain
                        per action and which ones still have no native
                        fallback yet.
  * Panel commands   -- open one of our guided task panels (deeper integration).

The TOOLBARS dict below is the curated UX: a small, workflow-ordered set, not
the ~130 raw commands the two engines expose.
"""

import os
import FreeCAD
import FreeCADGui

from PySide import QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio import deps, engines
from rfstudio import panel_nurbspull
from rfstudio import panel_sketch3d
from rfstudio import taskpanels

_ICON_DIR = os.path.join(os.path.dirname(__file__), "resources")


def _icon(name):
    p = os.path.join(_ICON_DIR, name)
    return p if os.path.exists(p) else os.path.join(_ICON_DIR, "rfstudio.svg")


# --- engine health tracking ------------------------------------------------ #
# Tracks per-command native-provider failures so a button whose native
# implementation keeps crashing is auto-disabled instead of failing at
# every click.  Engine commands (Curves/Silk delegations) are not tracked
# here -- their availability is governed by deps.installed() instead.
FAILURE_THRESHOLD = 3
_ENGINE_HEALTH = {}   # cmd_id -> {"failures": int, "disabled": bool}


def engine_health(cmd_id):
    """Return the health dict for a command id (creating it if needed)."""
    return _ENGINE_HEALTH.setdefault(
        cmd_id, {"failures": 0, "disabled": False})


def engine_health_record_failure(cmd_id, exc):
    """Increment the failure count for a command; disable it once the
    threshold is exceeded.  Returns True when the command was just
    disabled."""
    h = engine_health(cmd_id)
    h["failures"] += 1
    if h["failures"] >= FAILURE_THRESHOLD and not h["disabled"]:
        h["disabled"] = True
        FreeCAD.Console.PrintWarning(
            "Railform Studio: %s auto-disabled after %d native-provider "
            "failures (last: %s).  Use 'Reset Engine Health' to re-enable.\n"
            % (cmd_id, h["failures"], exc))
        return True
    return False


def engine_health_record_success(cmd_id):
    """Reset the failure count for a command (called on successful run)."""
    h = engine_health(cmd_id)
    h["failures"] = 0


def engine_health_disabled():
    """List of command ids currently disabled by the health tracker."""
    return sorted(
        c for c, h in _ENGINE_HEALTH.items() if h["disabled"])


def engine_health_reset():
    """Reset all engine-health failure counts and re-enable commands."""
    _ENGINE_HEALTH.clear()
    FreeCAD.Console.PrintMessage(
        "Railform Studio: engine health reset -- all providers re-enabled.\n")


# --- selection helpers ------------------------------------------------------- #
def _selection():
    """Current Gui selection as a list of (object, sub-element-names) pairs."""
    try:
        return FreeCADGui.Selection.getSelectionEx() or []
    except Exception:
        return []


def _selection_count():
    return len(_selection())


def _selection_has_face():
    """True when at least one selected sub-element is a Face."""
    for so in _selection():
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Face":
                return True
    return False


def _selection_has_edge():
    """True when at least one selected sub-element is an Edge."""
    for so in _selection():
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Edge":
                return True
    return False


def _selection_has_solid():
    """True when at least one selected object is a Solid."""
    for so in _selection():
        shp = getattr(so.Object, "Shape", None)
        if shp is not None and getattr(shp, "ShapeType", "") == "Solid":
            return True
    return False


def _selection_has_external_sources():
    """True when any selected object carries ExternalSources (linked projections,
    i.e. associative references from issues #14/#7 -- editing it directly would
    break those references)."""
    for so in _selection():
        obj = so.Object
        if obj is not None and hasattr(obj, "ExternalSources") and \
                obj.ExternalSources:
            return True
    return False


def _selection_ok_for(cmd_id):
    """Selection-validity pre-check for provider commands that need specific
    geometry.  Returns (ok, reason)."""
    sel = _selection()
    if not sel:
        return False, "select geometry first"

    if cmd_id == "RFS_Trim":
        # needs a face + an edge/curve
        return (_selection_has_face() and _selection_has_edge()), \
            "select one face and one boundary curve"
    if cmd_id == "RFS_SketchOnSurf":
        return (_selection_has_face() and _selection_has_edge()), \
            "select one face and one curve"
    if cmd_id == "RFS_BlendSurf":
        return _selection_has_face() and _selection_count() >= 2, \
            "select two surfaces"
    if cmd_id == "RFS_BlendSurfClosed":
        return _selection_count() >= 3, "select >= 3 faces"
    if cmd_id in ("RFS_Combs", "RFS_Zebra", "RFS_Reflect",
                  "RFS_DraftAnalysis", "RFS_Analysis"):
        return _selection_has_face(), "select one face/surface"
    if cmd_id == "RFS_ContinuityCheck":
        return _selection_count() >= 2, "select two adjacent faces"
    if cmd_id == "RFS_NetworkAudit":
        return _selection_has_face(), "select a patch network"
    if cmd_id == "RFS_GaussianCurvature":
        return _selection_has_face(), "select faces"
    return True, ""


# --- command sets for behaviour gating -------------------------------------- #
# Panel commands that need at least one object selected before they can run.
_PANEL_NEEDS_SELECTION = {
    "RFS_Fillet", "RFS_Chamfer", "RFS_Shell", "RFS_Offset", "RFS_Thicken",
    "RFS_MirrorGroup", "RFS_Combs", "RFS_Zebra", "RFS_Reflect",
    "RFS_Analysis", "RFS_DraftAnalysis", "RFS_ContinuityCheck",
    "RFS_NetworkAudit", "RFS_SurfaceReview", "RFS_GaussianCurvature",
    "RFS_BlendSurf", "RFS_BlendSurfClosed", "RFS_TrimByIntersection",
    "RFS_MeshToSurface", "RFS_HiddenLineExport", "RFS_Mirror",
    "RFS_Sweep2Rail", "RFS_Fill",
}

# Commands that modify/destroy geometry -- require explicit confirmation.
_DESTRUCTIVE = {
    "RFS_BooleanSubtract", "RFS_BooleanUnion", "RFS_BooleanIntersect",
    "RFS_Trim", "RFS_TrimByIntersection", "RFS_Shell", "RFS_Fillet",
    "RFS_Chamfer",
}

# Commands that must not run while another object is being edited.
_EDITING_OBJ = None  # name of the object currently in edit mode


def _confirm_destructive(cmd_id, menu_text):
    """Show a confirmation dialog before running a destructive command.
    Returns True when the user confirms, False to abort."""
    try:
        reply = QtWidgets.QMessageBox.question(
            FreeCADGui.getMainWindow(), "Railform Studio",
            "“%s” will modify the selected geometry.\n"
            "This cannot be undone with Ctrl+Z unless the operation is "
            "wrapped in a transaction.  Continue?" % menu_text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return reply == QtWidgets.QMessageBox.Yes
    except Exception:
        return True


# --------------------------------------------------------------------------- #
class ProviderCommand:
    """Re-exposes a curated action under one identity, backed by an ORDERED
    CHAIN of providers (engines.PROVIDERS) instead of one fixed engine
    command. This is the mechanism that lets a curated action keep working
    when its engine is missing or has renamed its command -- as native
    providers are written (see engines.py's triage notes), they're added to
    engines.NATIVE_OVERRIDES and this class picks them up automatically,
    with no change needed here or in the toolbar registry.

    Replaces the old DelegateCommand, which had no fallback path at all:
    one fixed (mod, target) pair, and a dead button if it broke.
    """

    def __init__(self, cmd_id, menu, tip, icon):
        self._id = cmd_id
        self._chain = engines.PROVIDERS[cmd_id]
        self._res = {"MenuText": menu, "ToolTip": tip, "Pixmap": _icon(icon)}

    def GetResources(self):
        return self._res

    def _provider_ready(self, provider):
        if provider[0] == "native":
            return hasattr(engines, provider[1])
        _, mod, _target = provider
        return deps.installed(mod)

    def IsActive(self):
        if FreeCAD.ActiveDocument is None:
            return False
        if engine_health(self._id)["disabled"]:
            return False
        if not any(self._provider_ready(p) for p in self._chain):
            return False
        ok, _reason = _selection_ok_for(self._id)
        return ok

    def Activated(self):
        ok, reason = _selection_ok_for(self._id)
        if not ok:
            FreeCAD.Console.PrintWarning(
                "Railform Studio: %s -- %s\n" % (self._id, reason))
            try:
                QtWidgets.QMessageBox.warning(
                    FreeCADGui.getMainWindow(), "Railform Studio",
                    "%s" % reason)
            except Exception:
                pass
            return
        if self._id in _DESTRUCTIVE and not _confirm_destructive(
                self._id, self._res.get("MenuText", self._id)):
            return
        for provider in self._chain:
            if provider[0] == "native":
                fn = getattr(engines, provider[1], None)
                if fn is None:
                    continue
                try:
                    fn()
                    engine_health_record_success(self._id)
                    return
                except Exception as exc:
                    engine_health_record_failure(self._id, exc)
                    continue
            else:
                _, mod, target = provider
                if engines.run_engine_command(mod, target):
                    engine_health_record_success(self._id)
                    return
        FreeCAD.Console.PrintError(
            "Railform Studio: no working provider for %s (tried: %s).\n"
            % (self._id, self._chain))
        # A console-only PrintError is easy to miss -- the toolbar button
        # just looks like it did nothing. Every provider in the chain has
        # already failed at this point, so also surface it as a dialog.
        try:
            QtWidgets.QMessageBox.warning(
                FreeCADGui.getMainWindow(), "Railform Studio",
                "“%s” isn’t available right now: none of its "
                "providers could run (tried: %s).\nSee the Report view for "
                "details." % (self._res.get("MenuText", self._id),
                              ", ".join(str(p) for p in self._chain)))
        except Exception:
            pass


class HealthResetCommand:
    """Resets the engine-health failure counts and re-enables any
    auto-disabled commands."""

    def __init__(self):
        self._res = {
            "MenuText": "Reset Engine Health",
            "ToolTip": "Re-enable any Railform Studio commands auto-disabled "
                       "after repeated native-provider failures.",
            "Pixmap": _icon("rfstudio.svg"),
        }

    def GetResources(self):
        return self._res

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        engine_health_reset()
        try:
            QtWidgets.QMessageBox.information(
                FreeCADGui.getMainWindow(), "Railform Studio",
                "Engine health reset -- all providers re-enabled.")
        except Exception:
            pass


class PanelCommand:
    """Opens one of our curated task panels."""

    def __init__(self, factory, menu, tip, icon):
        self._factory = factory     # callable -> task-panel instance
        self._res = {"MenuText": menu, "ToolTip": tip, "Pixmap": _icon(icon)}

    def GetResources(self):
        return self._res

    def IsActive(self):
        if FreeCAD.ActiveDocument is None:
            return False
        if self._id in _PANEL_NEEDS_SELECTION and not _selection():
            return False
        if _selection_has_external_sources():
            # referenced objects are read-only -- editing breaks associations
            return False
        return True

    def Activated(self):
        global _EDITING_OBJ
        if FreeCAD.ActiveDocument is None:
            FreeCAD.newDocument()
        # single-edit-mode: refuse to open a second edit panel while one
        # object is already being edited
        if _EDITING_OBJ is not None:
            try:
                QtWidgets.QMessageBox.information(
                    FreeCADGui.getMainWindow(), "Railform Studio",
                    "Finish editing “%s” before opening another tool."
                    % _EDITING_OBJ)
            except Exception:
                pass
            return
        # destructive confirmation
        if self._id in _DESTRUCTIVE and not _confirm_destructive(
                self._id, self._res.get("MenuText", self._id)):
            return
        # auto-clear any (possibly stale) open dialog instead of blocking --
        # a stranded dialog used to wedge the workbench so no new tool worked
        if FreeCADGui.Control.activeDialog():
            try:
                FreeCADGui.Control.closeDialog()
            except Exception:
                pass
        # mark the object being edited (best-effort: first selected object)
        sel = _selection()
        if sel:
            _EDITING_OBJ = getattr(sel[0].Object, "Name", None)
        try:
            panel = self._factory()
            FreeCADGui.Control.showDialog(panel)
        except Exception:
            _EDITING_OBJ = None
            raise
        finally:
            # clear edit-mode marker when the panel closes
            def _clear():
                global _EDITING_OBJ
                _EDITING_OBJ = None
            try:
                FreeCADGui.Control.closeDialog
            except Exception:
                pass
            FreeCADGui.Control.activeDialog
            # use a timer so the marker clears after the dialog is dismissed
            from PySide import QtCore
            QtCore.QTimer.singleShot(500, _clear)


def _surface_panel(kind):
    return lambda: taskpanels.SurfacePanel(kind=kind)


def _curve_panel(kind):
    return lambda: taskpanels.CurvePanel(kind=kind)


def _draw_tool_panel():
    """Return a factory for a CurvePanel with draw mode activated by default."""
    def factory():
        panel = taskpanels.CurvePanel(kind="InterpCurve")  # Default to InterpCurve
        # Activate draw mode after a short delay to ensure UI is ready
        def activate_draw_mode():
            if hasattr(panel, 'draw_btn') and panel.draw_btn:
                panel.draw_btn.setChecked(True)
                panel._toggle_draw_mode(True)
        # Use a timer to defer activation until after the panel is fully shown
        from PySide import QtCore
        QtCore.QTimer.singleShot(100, activate_draw_mode)
        return panel
    return factory


# --------------------------------------------------------------------------- #
# Custom (panel) commands -- ids owned by us, our own OCCT geometry.
_PANEL_COMMANDS = {
    # ---- Draw: native curve modeling ------------------------------------
    "RFS_DrawInterp": (_curve_panel("InterpCurve"),
                       "Interpolate curve",
                       "Draw a B-spline THROUGH points (table / pick / select).",
                       "rfs_interp.svg"),
    "RFS_DrawControl": (_curve_panel("ControlCurve"),
                        "Control-point curve",
                        "Draw a B-spline from a control polygon (pen-style).",
                        "rfs_controlcurve.svg"),
    "RFS_DrawApprox": (_curve_panel("ApproxCurve"),
                       "Approximate curve",
                       "Fit a fair low-pole B-spline NEAR points.",
                       "rfs_approx.svg"),
    "RFS_DrawPolyline": (_curve_panel("Polyline"),
                         "Polyline",
                         "Draw a straight (G0) polyline.",
                         "rfs_polyline.svg"),
    "RFS_DrawBezier": (_curve_panel("BezierCurve"),
                       "Bezier curve",
                       "Draw a Bezier curve with editable tangent handles "
                       "(Auto/Aligned/Vector/Free).",
                       # control-curve glyph + purple "B" badge (Bezier is a
                       # handle/CV-based curve, sibling of the control curve).
                       "rfs_bezier.svg"),
    # ---- Draw: freehand tool --------------------------------------------
    "RFS_DrawFreehand": (_draw_tool_panel(),
                         "Draw curve",
                         "Draw a curve by clicking to place points continuously "
                         "(like Blender's curve tool).",
                         "pencil.svg"),
    # 3D (non-planar) sketch authoring: points placed on an offset
    # construction plane, snapped to arbitrary 3D geometry, or typed
    # directly into the X/Y/Z table -- then baked into a parametric
    # curve via parametric.make_curve.
    "RFS_Sketch3D": (lambda: panel_sketch3d.Sketch3DPanel(),
                     "3D sketch",
                     "Author points in 3D (offset construction plane, "
                     "snap to 3D, typed XYZ) and bake a curve.",
                     "pencil.svg"),
    "RFS_DrawBlend": (lambda: taskpanels.BlendPanel(),
                      "Blend curve",
                      "G1/G2 blend between two curves (auto-detects ends).",
                      "rfs_blendcurve.svg"),
    "RFS_FilletPolyline": (lambda: taskpanels.PolylineFilletPanel(),
                           "Fillet polyline",
                           "Round the corners of a polyline with tangent arcs "
                           "of a set radius. Select the polyline first: the "
                           "fillet is then recorded ON it (no extra object); "
                           "parametric, editable.",
                           "rfs_polyfillet.svg"),
    "RFS_JoinCurve": (lambda: taskpanels.JoinCurvePanel(),
                      "Join curves",
                      "Weld several curves end-to-end into a single wire "
                      "(corners preserved; parametric).",
                      "rfs_join.svg"),
    "RFS_Mirror": (lambda: taskpanels.MirrorPanel(),
                   "Mirror",
                   "Model one half, mirror the rest — optionally weld "
                   "the seam into one continuous shell/solid.",
                   "rfs_mirror.svg"),
    "RFS_MirrorGroup": (_surface_panel("MirrorGroup"),
                        "Mirror group",
                        "Mirror every selected object across a plane in "
                        "one step (select everything under a Curves/"
                        "Surfaces folder first).",
                        "rfs_mirror.svg"),
    # ---- Surface --------------------------------------------------------
    "RFS_Loft": (lambda: taskpanels.LoftPanel(),
                 "Loft surface",
                 "Parametric loft through profiles with optional guide rails (re-editable).",
                 "rfs_loft.svg"),
    "RFS_Gordon": (_surface_panel("Gordon"),
                   "Gordon / network surface",
                   "Parametric surface through a U/V curve network (re-editable).",
                   "rfs_gordon.svg"),
    "RFS_Coons": (_surface_panel("Coons"),
                  "Coons patch",
                  "Parametric 4-sided Coons patch (re-editable).",
                  "rfs_coons.svg"),
    "RFS_ImportRails": (lambda: taskpanels.ImportRailsPanel(),
                        "Import Blender rails",
                        "Rebuild Railform rails (JSON) as exact NURBS curves.",
                        "rfs_import.svg"),
    # Native two-rail sweep (bypasses Curves' Birail object requirement)
    "RFS_Sweep2Rail":  (_surface_panel("Sweep"),
                        "Two-rail sweep",
                        "Sweep profiles along two rail curves — select Rail 1, Rail 2, Profiles.",
                        "rfs_sweep.svg"),
    # N-sided fill via OCCT GeomPlate
    "RFS_Fill":        (_surface_panel("Fill"),
                        "N-sided fill",
                        "Fill 3 or more boundary curves into a smooth surface (G0 boundary match).",
                        "rfs_fill.svg"),
    # G1/G2 surface match via Hermite skinning
    "RFS_MatchSurf":   (_surface_panel("Match"),
                        "Surface match (G2)",
                        "Rebuild a surface so one edge is G1/G2-continuous with a reference surface.",
                        "rfs_match.svg"),
    # Manufacturability: offset / thicken / shell
    "RFS_Offset":      (lambda: taskpanels.OffsetThickenPanel(mode="Offset"),
                        "Offset surface",
                        "Offset an open surface or shell along its normal.",
                        "rfs_offset.svg"),
    "RFS_Thicken":     (lambda: taskpanels.OffsetThickenPanel(mode="Thicken"),
                        "Thicken surface",
                        "Turn an open surface into a solid with wall thickness.",
                        "rfs_thicken.svg"),
    "RFS_Shell":       (lambda: taskpanels.ShellPanel(),
                        "Shell solid",
                        "Hollow an existing solid by removing selected faces.",
                        "rfs_shell.svg"),
    "RFS_ExtendSurf":  (_surface_panel("ExtendSurf"),
                        "Extend surface",
                        "Extend a surface in U and V parametric directions (re-editable).",
                        "rfs_extend.svg"),
    # Class-A QA: numeric seam and network continuity checks
    "RFS_ContinuityCheck": (lambda: taskpanels.ContinuityCheckPanel(),
                            "Check seam continuity",
                            "Measure G0/G1/G2 continuity numerically across two adjacent faces.",
                            "rfs_continuity.svg"),
    "RFS_NetworkAudit": (lambda: taskpanels.NetworkAuditPanel(),
                         "Audit patch network",
                         "Audit continuity across every adjacent face pair in a patch network.",
                         "rfs_network_audit.svg"),
    "RFS_SurfaceReview": (lambda: taskpanels.SurfaceReviewPanel(),
                          "Review surfaces",
                          "Roll continuity and draft analysis into a single surface-review summary.",
                          "rfs_surface_review.svg"),
    "RFS_WorkflowGuide": (lambda: taskpanels.WorkflowGuidePanel(),
                          "Workflow guide",
                          "Show a guided surfacing checklist for patch creation, review, and manufacture.",
                          "rfs_workflow.svg"),
    "RFS_GaussianCurvature": (lambda: taskpanels.GaussianCurvaturePanel(),
                              "Gaussian curvature",
                              "Color-coded Gaussian curvature classification (saddle / flat / patch) over selected faces.",
                              "rfs_gaussian.svg"),
    "RFS_StitchShell": (lambda: taskpanels.StitchShellPanel(),
                        "Stitch shell",
                        "Sew a finished network of surface objects into one continuous shell/solid.",
                        "rfs_stitch.svg"),
    # ---- New in 2026-07: four gaps from issues.md closed in one batch -----
    # Radius-driven fillet + chamfer on solids (the RFS_Fillet / RFS_Chamfer
    # entries close the "no radius-driven fillet" gap).
    "RFS_Fillet": (lambda: taskpanels.FilletPanel(),
                   "Fillet (radius)",
                   "Constant-radius fillet on a solid's picked edges (radius in world units).",
                   "rfs_fillet.svg"),
    "RFS_Chamfer": (lambda: taskpanels.ChamferPanel(),
                    "Chamfer",
                    "Constant-distance chamfer on a solid's picked edges.",
                    "rfs_chamfer.svg"),
    # Closed-loop G1/G2 blend around an N-sided face ring
    # (closes the "no closed-loop blend" gap).
    "RFS_BlendSurfClosed": (lambda: taskpanels.BlendSurfClosedPanel(),
                            "Closed-loop blend",
                            "G1/G2 blend surface strip around a closed ring of >= 3 faces.",
                            "rfs_blend_closed.svg"),
    # True surface-surface intersection trim
    # (closes the "no true surface-surface trim" gap).
    "RFS_TrimByIntersection": (lambda: taskpanels.TrimByIntersectionPanel(),
                               "Trim by intersection",
                               "Intersect two faces and trim both to the intersection curve.",
                               "rfs_trim_intersect.svg"),
    # Iterative local-relaxation continuity solver
    # (closes the "no simultaneous multi-neighbor continuity solving" gap).
    "RFS_BalanceNetwork": (lambda: taskpanels.BalanceNetworkPanel(),
                           "Balance network continuity",
                           "Iteratively rebuild the worst seam's worse face to drive the network toward a target continuity.",
                           "rfs_balance.svg"),
    # NURBS push/pull direct manipulation
    # Removed from curated toolbar -- only accessible via double-click in the
    # tree browser (SurfaceVP.doubleClicked), so direct manipulation is
    # never started accidentally from a toolbar button.
    # "RFS_NURBSPushPull": (lambda: panel_nurbspull.createPanel(),
    #                        "NURBS Push/Pull",
    #                        "Push/pull NURBS control points with interactive draggers",
    #                        "rfs_nurbs_pushpull.svg"),
}

# Delegating commands -- curated label + tooltip + icon per engine command.
_DELEGATE_META = {
    "RFS_MultiLoft":    ("Multi loft", "Loft through many sections (native -- no longer requires Curves).", "rfs_multiloft.svg"),
    "RFS_BlendSurf":    ("Blend surface", "G1/G2 blend strip between two edges (Curves).", "rfs_blend.svg"),
    "RFS_Trim":         ("Trim surface", "Trim a face with a curve (native; falls back to Curves).", "rfs_trim.svg"),
    "RFS_SketchOnSurf": ("Sketch on surface", "Project a curve onto a surface (native; falls back to Curves).", "rfs_sos.svg"),
    "RFS_CleanPatch44": ("Clean patch 4x4", "Low-CP bicubic patch from a control grid (native; falls back to Silk).", "rfs_patch44.svg"),
    "RFS_CleanPatch66": ("Clean patch 6x6", "Low-CP bi-quintic patch (native; falls back to Silk).", "rfs_patch66.svg"),
    "RFS_ControlPoly4": ("Control poly (cubic)", "Cubic control polygon from 4 points (native; falls back to Silk).", "rfs_poly.svg"),
    "RFS_StarPatch":    ("Star-point patch", "N-sided patch (native; exact "
                       "G0 / tangent-plane G1 continuity).",
                       "rfs_star.svg"),
    "RFS_Combs":        ("Curvature combs", "Parametric curvature comb (Curves).", "rfs_comb.svg"),
    "RFS_Zebra":        ("Zebra", "Zebra stripe analysis (Curves).", "rfs_zebra.svg"),
    "RFS_Reflect":      ("Reflect lines", "Reflection line analysis (Curves).", "rfs_reflect.svg"),
    "RFS_Analysis":     ("Surface analysis", "Continuity/draft review over selected faces (native; falls back to Curves).", "rfs_analysis.svg"),
    "RFS_DraftAnalysis":("Draft analysis", "Draft-angle analysis (Curves).", "rfs_draft.svg"),
    # ---- Native-only additions: variable-radius fillets + global fairing --
    "RFS_VariableFillet": ("Variable-radius fillet",
                          "Multi-station variable-radius fillet on a solid "
                          "edge (native; e.g. R3 tapering to R8).",
                          "rfs_fillet_variable.svg"),
    "RFS_RollingBallFillet": ("Rolling-ball fillet",
                             "Real-world-radius fillet between two surfaces, "
                             "with an optional multi-section taper (native).",
                             "rfs_rollingball.svg"),
    "RFS_GlobalFair": ("Global fairing",
                       "Network-wide Laplacian fairing pass over a patch "
                       "network's control nets (native).",
                       "rfs_global_fair.svg"),
    # ---- Native-only additions: booleans, mesh-to-surface, hidden-line
    # drafting export. None of these had any prior engine delegation --
    # Curves/Silk never offered booleans, mesh import, or drafting output,
    # so this was a pure scope gap rather than anything OCCT couldn't do.
    "RFS_BooleanUnion": ("Boolean union",
                        "Fuse >= 2 solids into one (native).",
                        "rfs_boolean_union.svg"),
    "RFS_BooleanSubtract": ("Boolean subtract",
                           "Cut tool solid(s) out of a base solid (native).",
                           "rfs_boolean_subtract.svg"),
    "RFS_BooleanIntersect": ("Boolean intersect",
                            "Keep only the common volume of >= 2 solids "
                            "(native).",
                            "rfs_boolean_intersect.svg"),
    "RFS_MeshToSurface": ("Mesh/scan to surface",
                         "Fit a single approximating NURBS patch to a raw "
                         "point cloud or mesh (native; single-patch, "
                         "height-field only).",
                         "rfs_mesh_to_surface.svg"),
    "RFS_HiddenLineExport": ("Hidden-line drawing",
                            "Project solids/surfaces to a 2D hidden-line "
                            "drawing and export as SVG (native).",
                            "rfs_hidden_line.svg"),
    # --- engine health ------------------------------------------------------- #
    "RFS_ResetHealth": ("Reset Engine Health",
                       "Re-enable any Railform Studio commands auto-disabled "
                       "after repeated native-provider failures.",
                       "rfstudio.svg"),
}

# Curated toolbars/menus -- workflow-ordered, deliberately small.
TOOLBARS = [
    ("Railform Draw",    ["RFS_DrawInterp", "RFS_DrawControl", "RFS_DrawApprox",
                          "RFS_DrawPolyline", "RFS_DrawBezier", "RFS_DrawBlend",
                          "RFS_FilletPolyline", "RFS_JoinCurve", "RFS_DrawFreehand",
                          "RFS_Sketch3D"]),
    ("Railform Surface", ["RFS_Loft", "RFS_Gordon", "RFS_Coons",
                          "RFS_MultiLoft", "RFS_Sweep2Rail", "RFS_Fill",
                          "RFS_MatchSurf", "RFS_BlendSurf",
                          "RFS_BlendSurfClosed", "RFS_Trim",
                          "RFS_TrimByIntersection", "RFS_SketchOnSurf",
                          "RFS_Offset", "RFS_Thicken", "RFS_Shell",
                          "RFS_StitchShell"]),
    ("Railform Clean",   ["RFS_ControlPoly4", "RFS_CleanPatch44",
                          "RFS_CleanPatch66", "RFS_StarPatch",
                          "RFS_Fillet", "RFS_Chamfer",
                          "RFS_VariableFillet", "RFS_RollingBallFillet"]),
    ("Railform Solids",  ["RFS_BooleanUnion", "RFS_BooleanSubtract",
                          "RFS_BooleanIntersect", "RFS_MeshToSurface",
                          "RFS_Mirror", "RFS_MirrorGroup"]),
    ("Railform Inspect", ["RFS_Combs", "RFS_Zebra", "RFS_Reflect",
                          "RFS_Analysis", "RFS_DraftAnalysis",
                          "RFS_GaussianCurvature",
                          "RFS_ContinuityCheck", "RFS_NetworkAudit",
                          "RFS_BalanceNetwork", "RFS_GlobalFair",
                          "RFS_SurfaceReview", "RFS_WorkflowGuide",
                          "RFS_ResetHealth"]),
    ("Railform Exchange",["RFS_ImportRails", "RFS_HiddenLineExport"]),
]


def command_info(cmd_id):
    """(menu_text, icon_path, tooltip) for a curated command -- used by the
    docked tool shelf to build its buttons."""
    if cmd_id in _PANEL_COMMANDS:
        _, menu, tip, icon = _PANEL_COMMANDS[cmd_id]
        return menu, _icon(icon), tip
    if cmd_id in _DELEGATE_META:
        menu, tip, icon = _DELEGATE_META[cmd_id]
        return menu, _icon(icon), tip
    return cmd_id, "", ""


def register():
    """Instantiate + addCommand every curated command. Called from Initialize."""
    for cmd_id, (panel, menu, tip, icon) in _PANEL_COMMANDS.items():
        FreeCADGui.addCommand(cmd_id, PanelCommand(panel, menu, tip, icon))
    for cmd_id, (menu, tip, icon) in _DELEGATE_META.items():
        if cmd_id == "RFS_ResetHealth":
            continue   # registered below as a plain command, not a provider
        FreeCADGui.addCommand(cmd_id, ProviderCommand(cmd_id, menu, tip, icon))
    FreeCADGui.addCommand("RFS_ResetHealth", HealthResetCommand())