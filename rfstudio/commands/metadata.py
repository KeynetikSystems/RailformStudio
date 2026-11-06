# -*- coding: utf-8 -*-
"""Command metadata: panel commands, delegate metadata, selection hints, etc.

Centralized data definitions for all curated commands. This replaces the
large inline dictionaries that were in commands.py.
"""

import os
from typing import Any

import FreeCAD
import FreeCADGui

from rfstudio import engines
from rfstudio import taskpanels
from rfstudio import panel_sketch3d


_ICON_DIR = os.path.join(os.path.dirname(__file__), "..", "resources")


def _icon(name: str) -> str:
    p = os.path.join(_ICON_DIR, name)
    return p if os.path.exists(p) else os.path.join(_ICON_DIR, "rfstudio.svg")
# --- selection helpers ------------------------------------------------------ #

def _selection() -> list[Any]:
    """Currently selected objects with sub-elements (SelectionEx)."""
    try:
        return list(FreeCADGui.Selection.getSelectionEx())
    except Exception:
        return []


def _selection_count() -> int:
    return len(_selection())


def _selection_has_face() -> bool:
    """True when at least one selected sub-element is a Face."""
    for so in _selection():
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Face":
                return True
    return False


def _selection_has_edge() -> bool:
    """True when at least one selected sub-element is an Edge."""
    for so in _selection():
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Edge":
                return True
    return False


def _selection_has_solid() -> bool:
    """True when at least one selected object is a Solid."""
    for so in _selection():
        shp = getattr(so.Object, "Shape", None)
        if shp is not None and getattr(shp, "ShapeType", "") == "Solid":
            return True
    return False


def _selection_has_external_sources() -> bool:
    """True when any selected object carries ExternalSources (linked projections,
    i.e. associative references -- editing it directly would break those references)."""
    for so in _selection():
        obj = so.Object
        if obj is not None and hasattr(obj, "ExternalSources") and obj.ExternalSources:
            return True
    return False


# Human-readable selection requirements, surfaced two ways: appended to a
# command's tooltip ("Needs: ...") so a user knows what to select BEFORE
# clicking, and shown as the non-modal status-bar message when a command is
# clicked without a valid selection. One source of truth for both the gate
# (_selection_ok_for) and the hint (selection_requirement) -- see UX note on
# why we no longer silently grey these buttons out.
SELECTION_HINTS = {
    "RFS_Trim": "one face and one boundary curve",
    "RFS_SketchOnSurf": "one face and one curve",
    "RFS_BlendSurf": "two surfaces",
    "RFS_BlendSurfClosed": "three or more faces",
    "RFS_TagProfile": "one or more curves",
    "RFS_TagRail": "one or more curves",
}

_GENERIC_SELECTION_HINT = "at least one object"


def _selection_ok_for(cmd_id: str) -> tuple[bool, str]:
    """Selection-validity pre-check for provider commands that need specific
    geometry. Returns (ok, reason). The reason text comes from
    SELECTION_HINTS so the greyed-out explanation, the tooltip hint, and the
    click-time message all read the same."""
    sel = _selection()
    if not sel:
        return False, "select geometry first"

    if cmd_id == "RFS_Trim":
        return (_selection_has_face() and _selection_has_edge()), \
            "select " + SELECTION_HINTS[cmd_id]
    if cmd_id == "RFS_SketchOnSurf":
        return (_selection_has_face() and _selection_has_edge()), \
            "select " + SELECTION_HINTS[cmd_id]
    if cmd_id == "RFS_BlendSurf":
        return _selection_has_face() and _selection_count() >= 2, \
            "select " + SELECTION_HINTS[cmd_id]
    if cmd_id == "RFS_BlendSurfClosed":
        return _selection_count() >= 3, "select " + SELECTION_HINTS[cmd_id]
    return True, ""
# --- command sets for behaviour gating -------------------------------------- #

# Panel commands that need at least one object selected before they can run.
_PANEL_NEEDS_SELECTION = {
    "RFS_Shell", "RFS_Offset", "RFS_Thicken",
    "RFS_MirrorGroup", "RFS_SurfaceReview",
    "RFS_BlendSurf", "RFS_BlendSurfClosed", "RFS_TrimByIntersection",
    "RFS_MeshToSurface", "RFS_HiddenLineExport", "RFS_Mirror",
    "RFS_Sweep2Rail", "RFS_SweepN", "RFS_Fill",
}

# Commands that modify/destroy geometry -- require explicit confirmation.
_DESTRUCTIVE = {
    "RFS_BooleanSubtract", "RFS_BooleanUnion", "RFS_BooleanIntersect",
    "RFS_Trim", "RFS_TrimByIntersection", "RFS_Shell",
}

# Provider commands whose native path opens a guided slot-picker panel
# (engines.native_trim / native_sketch_on_surf -> TrimSoSPanel;
# native_blend_surface -> BlendSurfPanel). For these we must NOT gate on
# (or reject) the viewport selection before running: the whole point of the
# panel is to let the user assign the face/curve/surface roles explicitly,
# prefilled from the selection when possible. The panel is the real commit
# point, so the destructive-confirm prompt moves there too (and the native
# trim/SoS are additive anyway -- they add a new Part::Feature, leaving the
# source untouched).
_OPENS_PANEL = {"RFS_Trim", "RFS_SketchOnSurf", "RFS_BlendSurf"}


def _tip_with_hint(cmd_id: str, tip: str) -> str:
    """Append the selection requirement (if any) to a command's tooltip, so
    the toolbar/menu/shelf button all explain what to pick before it will
    do anything."""
    req = selection_requirement(cmd_id)
    if not req:
        return tip
    note = "Needs: " + req + "."
    return (tip + "\n\n" + note) if tip else note


def selection_requirement(cmd_id: str) -> str:
    """Short 'what to select' phrase for a command, or '' when it needs no
    particular selection. Drives the tooltip hint and the click-time
    message. Panel commands that just need *something* selected fall back to
    the generic phrase; the specific provider requirements live in
    SELECTION_HINTS."""
    if cmd_id in SELECTION_HINTS:
        return SELECTION_HINTS[cmd_id]
    if cmd_id in _PANEL_NEEDS_SELECTION:
        return _GENERIC_SELECTION_HINT
    return ""


# --- panel command factories ------------------------------------------------ #

def _surface_panel(kind: str) -> Any:
    return lambda: taskpanels.SurfacePanel(kind=kind)


def _curve_panel(kind: str) -> Any:
    return lambda: taskpanels.CurvePanel(kind=kind)


def _draw_tool_panel() -> Any:
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
# Curated panel commands -- ids owned by us, our own OCCT geometry.
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
                   "Model one half, mirror the rest -- optionally weld "
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
                        "Rebuild Railform rails (JSON) as exact NURB"
                        "curves/surfaces.",
                        "rfs_import.svg"),
    "RFS_Sweep2Rail": (_surface_panel("Sweep2Rail"),
                       "2-rail sweep",
                       "Parametric surface between two rails with profile(s) (re-editable).",
                       "rfs_sweep2.svg"),
    "RFS_SweepN": (_surface_panel("SweepN"),
                   "N-rail sweep",
                   "Parametric sweep through N rails + profiles (re-editable).",
                   "rfs_sweepn.svg"),
    "RFS_Fill": (_surface_panel("Fill"),
                 "Fill (N-sided patch)",
                 "Parametric Gregory-star patch for N-sided holes (re-editable).",
                 "rfs_fill.svg"),
"RFS_MatchSurf": (_surface_panel("MatchSurf"),
                      "Match surface",
                      "G1/G2 match a surface to adjacent faces (re-editable).",
                      "rfs_match.svg"),
    "RFS_BlendSurfClosed": (_surface_panel("BlendSurfClosed"),
                            "Closed blend",
                            "Parametric G1/G2 blend strip around a closed face loop (re-editable).",
                            "rfs_blendclosed.svg"),
    "RFS_TrimByIntersection": (_surface_panel("TrimByIntersection"),
                               "Trim by intersection",
                               "Trim two faces to their mutual intersection curve (re-editable).",
                               "rfs_trimintersect.svg"),
    "RFS_Offset": (_surface_panel("Offset"),
                   "Offset surface",
                   "Parametric parallel offset of a surface/shell (re-editable).",
                   "rfs_offset.svg"),
    "RFS_Thicken": (_surface_panel("Thicken"),
                    "Thicken surface",
                    "Parametric thickening to a solid with wall thickness (re-editable).",
                    "rfs_thicken.svg"),
    "RFS_Shell": (_surface_panel("Shell"),
                  "Shell solid",
                  "Parametric hollow-out with face removal (re-editable).",
                  "rfs_shell.svg"),
    "RFS_StitchShell": (_surface_panel("StitchShell"),
                        "Stitch shell",
                        "Sew a selection of faces into a continuous shell (re-editable).",
                        "rfs_stitch.svg"),
    # ---- Solids / Exchange ----------------------------------------------
    "RFS_MeshToSurface": (lambda: taskpanels.MeshToSurfacePanel(),
                          "Mesh to surface",
                          "Fit a single NURBS patch to a point-cloud/mesh scan (height-field).",
                          "rfs_mesh2surf.svg"),
    "RFS_HiddenLineExport": (lambda: taskpanels.HiddenLineExportPanel(),
                             "Hidden-line export",
                             "Project 3D model to 2D hidden-line drawing and export SVG.",
                             "rfs_hl_export.svg"),
}


# Delegate commands (engine-backed with native fallback chains).
# Menu text, tooltip, icon. These map to engines.PROVIDERS chains.
_DELEGATE_META = {
    "RFS_BlendSurf": ("Blend surface",
                      "G1/G2 blend strip between two surfaces (native first, Curves fallback).",
                      "rfs_blend.svg"),
    "RFS_Trim": ("Trim face",
                 "Trim a face to a boundary wire on its surface (native first, Curves fallback).",
                 "rfs_trim.svg"),
    "RFS_SketchOnSurf": ("Sketch on surface",
                         "Project a curve onto a surface (native first, Curves fallback).",
                         "rfs_sos.svg"),
    "RFS_MultiLoft": ("Multi-loft (legacy)",
                      "Parametric loft through many profiles -- use RFS_Loft for new work.",
                      "rfs_loft.svg"),
    "RFS_BooleanUnion": ("Boolean union",
                         "Fuse selected solids (native OCCT).",
                         "rfs_union.svg"),
    "RFS_BooleanSubtract": ("Boolean subtract",
                            "Cut one solid from another (native OCCT).",
                            "rfs_subtract.svg"),
    "RFS_BooleanIntersect": ("Boolean intersect",
                             "Intersect selected solids (native OCCT).",
                             "rfs_intersect.svg"),
    "RFS_MeshToSurface": ("Mesh to surface (legacy)",
                          "Fit surface to scan -- use panel command RFS_MeshToSurface instead.",
                          "rfs_mesh2surf.svg"),
    "RFS_HiddenLineExport": ("Hidden-line export (legacy)",
                             "Export 2D hidden-line SVG -- use panel command RFS_HiddenLineExport instead.",
                             "rfs_hl_export.svg"),
    "RFS_TagProfile": ("Tag as Profile",
                       "Tag selected curve(s) as Profiles (auto-routed to U/Profile inputs).",
                       "rfs_tag_profile.svg"),
    "RFS_TagRail": ("Tag as Guide Rail",
                    "Tag selected curve(s) as Guide Rails (auto-routed to V/Rail inputs).",
                    "rfs_tag_rail.svg"),
    "RFS_ResetHealth": ("Reset Engine Health",
                        "Re-enable any Railform Studio commands auto-disabled "
                        "after repeated native-provider failures.",
                        "rfstudio.svg"),
}