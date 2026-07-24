# -*- coding: utf-8 -*-
"""Pure decision-logic extracted from the GUI task panels: readiness
diagnostics, status messages, and small geometric helpers that touch
neither FreeCADGui nor PySide.

Why this file exists: Silk/Curves import FreeCAD.Gui at module load, so
nothing that imports them (which includes every rfstudio.panel_* module,
transitively via FreeCADGui itself) can run under `freecadcmd` -- the
whole GUI layer has been manual-smoke-test-only. But the actual DECISION
logic inside these panels (is this input set valid? what should the
status line say? which direction should the draft pull default to?) is
plain data-in/data-out logic with no widget dependency. Splitting it out
here means it's covered by test_panel_logic.py under freecadcmd, the
same headless safety net core.py already has -- not full GUI automation
(impossible given the Gui-at-import constraint above), but real coverage
of the part most likely to silently regress: what a panel decides to
tell the user.

Callers (rfstudio.panel_surface, rfstudio.panel_qa) hold the widgets and
FreeCAD document objects; this module only ever sees plain values.
"""


def readiness(kind, counts, limits):
    """Generic LIMITS-driven readiness check, shared by every SurfacePanel
    kind (Loft/Gordon/Coons/Fill/Match/Sweep) and LoftPanel.

    counts : {prop_name: n} -- how many items are currently in each input
             group.
    limits : {prop_name: (lo, hi)} -- inclusive bounds, either side may
             be None for "no bound".

    Returns (ok, message).
    """
    problems = []
    parts = []
    for prop, n in counts.items():
        lo, hi = limits.get(prop, (None, None))
        parts.append("%s: %d" % (prop, n))
        if lo is not None and n < lo:
            problems.append("%s needs >= %d (has %d)" % (prop, lo, n))
        elif hi is not None and n > hi:
            problems.append("%s allows <= %d (has %d)" % (prop, hi, n))
    if not problems:
        return True, "%s ready: %s." % (kind, "  ·  ".join(parts))
    if all(n == 0 for n in counts.values()):
        return False, ("No inputs yet — select curves/surfaces in the "
                       "3D view, then use an 'Add selected' button below.")
    return False, "%s needs:  %s" % (kind, "   ·   ".join(problems))


def loft_readiness(n_profiles, n_rails, min_profiles):
    """LoftPanel's bespoke (but simpler) status message -- min_profiles is
    parametric.LIMITS["Loft"]["Profiles"][0]."""
    if min_profiles is not None and n_profiles < min_profiles:
        return False, ("Loft needs >= %d profiles (has %d) -- add curves "
                       "above." % (min_profiles, n_profiles))
    return True, ("Loft ready: %d profile(s), %d guide rail(s)."
                  % (n_profiles, n_rails))


def offset_thicken_readiness(mode, has_source, shape_ok):
    if has_source and shape_ok:
        return True, "%s ready." % mode
    return False, "Select a source surface/shell above."


def shell_readiness(has_source, has_faces):
    problems = []
    if not has_source:
        problems.append("select the solid")
    if not has_faces:
        problems.append("select >= 1 face to remove")
    if problems:
        return False, "Shell needs: " + "; ".join(problems)
    return True, "Shell ready."


def blend_readiness(ok, kind="Blend"):
    if ok:
        return True, "%s ready." % kind
    return False, "Select two surfaces above."


def infer_pull_direction(normals):
    """Average a set of (x, y, z) face-normal tuples (need not be unit
    length; zero vectors are ignored) into one normalized pull direction.
    Returns None if there's nothing usable (no normals, or they cancel
    out to ~zero). The geometry-extraction step (face.normalAt) stays in
    the panel -- this is the pure-math averaging part."""
    tx = ty = tz = 0.0
    count = 0
    for nx, ny, nz in normals:
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length > 1e-9:
            tx += nx
            ty += ny
            tz += nz
            count += 1
    if count == 0:
        return None
    length = (tx * tx + ty * ty + tz * tz) ** 0.5
    if length < 1e-9:
        return None
    return (tx / length, ty / length, tz / length)
