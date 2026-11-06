# -*- coding: utf-8 -*-
"""Curated toolbar/menu registry.

TOOLBARS carries the full curated command set (menu text + ids).
TOOLBAR_PRIMARY carries the subset shown in actual toolbars (icon-only).

The menus carry the FULL set with text labels, so nothing is more than
one click away. Toolbars show a curated high-frequency subset.
"""

# Curated toolbars/menus -- workflow-ordered, deliberately small.
TOOLBARS = [
    ("Railform Draw",    ["RFS_DrawInterp", "RFS_DrawControl", "RFS_DrawApprox",
                          "RFS_DrawPolyline", "RFS_DrawBezier", "RFS_DrawBlend",
                          "RFS_FilletPolyline", "RFS_JoinCurve", "RFS_DrawFreehand",
                          "RFS_Sketch3D", "RFS_TagProfile", "RFS_TagRail"]),
    ("Railform Surface", ["RFS_Loft", "RFS_Gordon", "RFS_Coons",
                          "RFS_MultiLoft", "RFS_Sweep2Rail", "RFS_SweepN", "RFS_Fill",
                          "RFS_MatchSurf", "RFS_BlendSurf",
                          "RFS_BlendSurfClosed", "RFS_Trim",
                          "RFS_TrimByIntersection", "RFS_SketchOnSurf",
                          "RFS_Offset", "RFS_Thicken", "RFS_Shell",
                          "RFS_StitchShell", "RFS_ExtractSections", "RFS_SectionCurves"]),
    ("Railform Solids",  ["RFS_BooleanUnion", "RFS_BooleanSubtract",
                          "RFS_BooleanIntersect", "RFS_MeshToSurface",
                          "RFS_Mirror", "RFS_MirrorGroup"]),
    # NOTE: demo build -- Clean and Inspect (surface-analysis) toolbars are
    # intentionally omitted. The commands stay registered and functional
    # (runnable ::C or by restoring these entries); they are just not shown.
    ("Railform Inspect", ["RFS_SurfaceReview", "RFS_WorkflowGuide",
                          "RFS_ResetHealth"]),
    ("Railform Exchange",["RFS_ImportRails", "RFS_HiddenLineExport"]),
]

# Toolbars show a curated, high-frequency SUBSET of each group. Icon-only
# toolbar buttons are hard to learn 40 of, so the full set lives one click
# away in the menus and the docked tool shelf -- both of which show text
# labels next to every icon. This is pure data: move an id between here and
# TOOLBARS freely to promote/demote a tool from the toolbar. Every id here
# must also appear in TOOLBARS (which is what registers the command).
TOOLBAR_PRIMARY = [
    ("Railform Draw",    ["RFS_DrawInterp", "RFS_DrawControl", "RFS_DrawBezier",
                          "RFS_DrawBlend", "RFS_DrawFreehand", "RFS_TagProfile", "RFS_TagRail"]),
    ("Railform Surface", ["RFS_Loft", "RFS_Gordon", "RFS_Coons",
                          "RFS_Sweep2Rail", "RFS_Trim", "RFS_BlendSurf"]),
    ("Railform Solids",  ["RFS_BooleanUnion", "RFS_BooleanSubtract",
                          "RFS_Mirror"]),
    ("Railform Inspect", ["RFS_SurfaceReview", "RFS_WorkflowGuide"]),
    ("Railform Exchange", ["RFS_ImportRails", "RFS_HiddenLineExport"]),
]