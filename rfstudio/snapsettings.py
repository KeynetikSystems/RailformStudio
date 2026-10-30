# -*- coding: utf-8 -*-
"""Which snap KINDS are currently enabled (vertex / edge / intersection).

A tiny persisted on/off flag per snap family, read by the editor snap
collectors (editmode.PointEditor) so the user can mute a snap kind that is
getting in the way -- the Draft-workbench "snap toolbar" idea, scoped to the
three families Railform's ranked snapping actually distinguishes
(snaputil.nearest_snap_ranked_kind):

    vertex        -- exact discrete points: shape vertices, control points,
                     and B-spline knots (snaputil.collect_snap_points)
    intersection  -- true curve-curve crossings (collect_intersection_points)
    edge          -- perpendicular projection onto any point of a curve
                     (nearest_exact_snap)

Gui-free (only touches FreeCAD's parameter store), so it imports and runs
headless -- the collectors that call it are exercised by test_core.py.
State lives under Preferences/Mod/RailformStudio/Snapping; every kind
defaults ON, matching the previous always-snap-everything behaviour.
"""

import FreeCAD

_PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/RailformStudio/Snapping"

# The snap families, in display order. These strings are also the snap-kind
# tags snaputil.nearest_snap_ranked_kind returns, so a kind can be looked up
# against enabled() directly.
KINDS = ("vertex", "edge", "intersection", "midpoint", "center",
         "surface", "perpendicular", "tangent")

LABELS = {
    "vertex": "Vertices / control points",
    "edge": "On-curve projection",
    "intersection": "Curve intersections",
    "midpoint": "Edge midpoints",
    "center": "Arc / circle centres",
    "surface": "On-surface projection",
    "perpendicular": "Perpendicular to a curve",
    "tangent": "Tangent to a curve",
}

# Per-family default when no preference is stored yet. The three original
# families and the two cheap derived ones (midpoint/centre) default ON --
# the historic always-snap behaviour. The three that default OFF are the
# heavier or more specialised ones: surface projection is a per-face
# distToShape scan, and the two constraint families (perpendicular/tangent)
# add anchor-relative targets that only some authoring tasks want, so the
# everyday drag stays uncluttered until the user turns them on.
_DEFAULTS = {
    "surface": False,
    "perpendicular": False,
    "tangent": False,
}


def _params():
    try:
        return FreeCAD.ParamGet(_PARAM_PATH)
    except Exception:
        return None


def enabled(kind, default=None):
    """True if snap `kind` is currently enabled. `default` None means 'use
    this family's built-in default' (_DEFAULTS, mostly True); pass an explicit
    bool to override. Unknown/unavailable store -> the default, so a missing
    parameter store never silently flips a family."""
    if default is None:
        default = _DEFAULTS.get(kind, True)
    p = _params()
    if p is None:
        return default
    try:
        return p.GetBool(kind, default)
    except Exception:
        return default


def set_enabled(kind, value):
    """Persist snap `kind`'s on/off state. No-op if the parameter store
    can't be reached."""
    p = _params()
    if p is None:
        return
    try:
        p.SetBool(kind, bool(value))
    except Exception:
        pass


def enabled_kinds():
    """The set of currently-enabled kinds (subset of KINDS)."""
    return {k for k in KINDS if enabled(k)}
