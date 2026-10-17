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


# ---- 3D sketch authoring (RFS_Sketch3D) ---------------------------------- #

def plane_normal_for(name, view_dir=None):
    """Map a UI plane choice to a unit normal. "Camera" -> the current
    view direction (falls back to +Z when view_dir is missing or
    degenerate); the principal planes mirror core's XY/YZ/ZX naming."""
    if name == "XY":
        return (0.0, 0.0, 1.0)
    if name == "YZ":
        return (1.0, 0.0, 0.0)
    if name == "ZX":
        return (0.0, 1.0, 0.0)
    # "Camera" and anything else
    if view_dir:
        nx, ny, nz = view_dir
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length > 1e-12:
            return (nx / length, ny / length, nz / length)
    return (0.0, 0.0, 1.0)


def plane_basis(normal):
    """(u, v) unit axes spanning the plane with the given normal, chosen
    deterministically (stable across calls, never degenerate): pick the
    principal axis least aligned with the normal as a reference, u is
    that axis crossed with the normal, v = normal x u."""
    nx, ny, nz = normal
    axis = min(((1.0, 0.0, 0.0), abs(nx)),
               ((0.0, 1.0, 0.0), abs(ny)),
               ((0.0, 0.0, 1.0), abs(nz)), key=lambda t: t[1])[0]
    # u = axis x n (already perpendicular to n)
    ux = axis[1] * nz - axis[2] * ny
    uy = axis[2] * nx - axis[0] * nz
    uz = axis[0] * ny - axis[1] * nx
    length = (ux * ux + uy * uy + uz * uz) ** 0.5 or 1.0
    ux, uy, uz = ux / length, uy / length, uz / length
    # v = n x u
    vx = ny * uz - nz * uy
    vy = nz * ux - nx * uz
    vz = nx * uy - ny * ux
    return (ux, uy, uz), (vx, vy, vz)


def construction_plane(base, view_dir, offset=0.0):
    """The working plane a 3D-sketch click lands on: parallel to the
    camera (view direction), passing through `base + offset*normal`.
    Returns (origin, normal) with normal unit-length. Pure math -- the
    view-direction readback stays in the panel."""
    nx, ny, nz = view_dir
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        nx, ny, nz, length = 0.0, 0.0, 1.0, 1.0   # degenerate: fall back to top view
    nx, ny, nz = nx / length, ny / length, nz / length
    return ((base[0] + nx * offset,
             base[1] + ny * offset,
             base[2] + nz * offset),
            (nx, ny, nz))


def ray_plane_intersect(ray_origin, ray_dir, plane_origin, plane_normal):
    """Parametric intersection of the camera ray (from snaputil.pick_ray)
    with the construction plane. Returns the hit point as an (x, y, z)
    tuple, or None when the ray is (nearly) parallel to the plane.
    Pure math; no FreeCAD objects."""
    d = (ray_dir[0] * plane_normal[0] +
         ray_dir[1] * plane_normal[1] +
         ray_dir[2] * plane_normal[2])
    if abs(d) < 1e-12:
        return None
    t = ((plane_origin[0] - ray_origin[0]) * plane_normal[0] +
         (plane_origin[1] - ray_origin[1]) * plane_normal[1] +
         (plane_origin[2] - ray_origin[2]) * plane_normal[2]) / d
    return (ray_origin[0] + ray_dir[0] * t,
            ray_origin[1] + ray_dir[1] * t,
            ray_origin[2] + ray_dir[2] * t)


def sketch3d_readiness(kind, n_points):
    """Create-button readiness for the 3D sketch panel. Every supported
    curve kind builds from a plain point list, so the only geometric
    requirement is >= 2 points (a curve needs two ends)."""
    if n_points < 2:
        return False, ("Place or type at least 2 points in 3D "
                       "(have %d) -- %s builds through them."
                       % (n_points, kind))
    return True, "Ready: %s through %d point(s)." % (kind, n_points)


def solver_status_text(status):
    """One short verdict line from core.solve_point_constraints' `status`
    dict ({"state": "well"|"under"|"over"|"unknown", "residual", "dof",
    "rank"}) -- issues.md #8 (per-entity diagnostics), the low-risk
    slice: surface the solver verdict that already exists on the
    status line instead of leaving it in a dict nobody reads. The
    per-constraint half of #8 is constraint_badges() below.

    Pure dict -> str, no FreeCAD/numpy needed: headless-testable in
    test_panel_logic. Unknown/empty state yields "" so callers can
    append unconditionally."""
    status = status or {}
    state = status.get("state")
    if state == "over":
        res = status.get("residual")
        suffix = " (residual %.1e)" % res if res is not None else ""
        return ("over-defined: constraints conflict%s -- remove or "
                "relax one" % suffix)
    if state == "under":
        dof, rank = status.get("dof"), status.get("rank")
        if dof is not None and rank is not None and rank <= dof:
            return ("under-defined: %d of %d coordinate DOF still "
                    "free" % (dof - rank, dof))
        return "under-defined: some coordinate DOF still free"
    if state == "well":
        dof = status.get("dof")
        if dof is not None:
            return "well-defined: fully constrained (%d DOF)" % dof
        return "well-defined: fully constrained"
    if state == "solved":
        return "constraints satisfied"
    if state == "unknown":
        return "solver verdict unavailable (numpy missing)"
    return ""


# issues.md #8 badge threshold: satisfied sets land ~1e-9 after a solve,
# so 1e-4 (mm for positions, rad for angles) has huge headroom while
# any human-visible misplacement clears it by orders of magnitude.
_BADGE_TOL = 1e-4


def constraint_badges(violations, tol=_BADGE_TOL):
    """issues.md #8 (per-constraint badges): True = constraint visibly
    violated at the CURRENT point positions (panel paints it red),
    False = satisfied within tolerance. Input is the per-constraint
    list from core.constraint_violations, aligned 1:1 with the
    constraints.

    Pure list[float] -> list[bool] so the badge decision is testable
    headlessly; on the live panel this means dragging a point off its
    dim turns the badge red BEFORE accept (recompute re-solves and
    clears it)."""
    return [abs(v) > tol for v in (violations or [])]


def points_are_planar(points, tol=1e-6):
    """False when the sketch's points do not lie on one plane (i.e. it is
    genuinely 3D). Pure informational: a non-planar point set is exactly
    what a 3D sketch is FOR, but the panel tells the user so they know
    the curve cannot be made planar without moving points."""
    if len(points) < 4:
        return True
    p0, p1, p2 = points[0], points[1], points[2]
    ux, uy, uz = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
    vx, vy, vz = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < tol:
        # First three points collinear -- fall back to axis-aligned test
        return True
    for p in points[3:]:
        dist = abs((p[0] - p0[0]) * nx + (p[1] - p0[1]) * ny +
                   (p[2] - p0[2]) * nz) / length
        if dist > max(tol, 1e-9):
            return False
    return True


def points_form_closed_loop(points, tol=1e-4):
    """True when the sketch's first and last points coincide (within
    tol) -- the 'closed contour' precondition for offering an n-sided
    fill of the loop. Pure math."""
    if len(points) < 3:
        return False
    a, b = points[0], points[-1]
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 +
            (a[2] - b[2]) ** 2) ** 0.5 < tol


def find_closed_loops(points, tol=1e-4):
    """Find all closed loops in a sequential point list.
    
    Returns a list of (start_idx, end_idx) tuples where points[start_idx]
    and points[end_idx] are the same point (within tol), forming a closed
    loop. The points between start_idx and end_idx (inclusive) form the loop.
    
    This finds loops like P0-P1-P2-P3-P0 (indices 0,3) or P2-P3-P4-P2 (indices 2,5).
    """
    loops = []
    if len(points) < 4:
        return loops
    # Check all pairs (i, j) where i < j and points[i] == points[j] within tol
    # A valid loop needs at least 3 unique points between them
    for i in range(len(points)):
        for j in range(i + 3, len(points)):
            a, b = points[i], points[j]
            dist = ((a[0] - b[0])**2 + (a[1] - b[1])**2 + (a[2] - b[2])**2)**0.5
            if dist < tol:
                # Check that the points between i and j form a valid loop
                # (at least 3 unique points, not all collinear)
                loop_pts = points[i:j+1]
                if len(loop_pts) >= 4:
                    # Check if loop points are not all collinear
                    unique = []
                    for p in loop_pts:
                        if not any(((p[0]-q[0])**2+(p[1]-q[1])**2+(p[2]-q[2])**2)**0.5 < tol for q in unique):
                            unique.append(p)
                    if len(unique) >= 3:
                        loops.append((i, j))
    return loops


def auto_plane_normal(chord, prev_normal):
    """Auto plane-flip: given the direction of the segment just placed
    (`chord`) and the previous plane normal, pick the principal-axis
    normal most PERPENDICULAR to that chord (so the new plane contains
    the segment direction), breaking ties by closeness to the previous
    normal. This is the deterministic 'press-Tab' analog -- the plane
    flips once the drawing direction leaves it."""
    cx, cy, cz = chord
    length = (cx * cx + cy * cy + cz * cz) ** 0.5
    if length < 1e-12:
        return prev_normal
    cx, cy, cz = cx / length, cy / length, cz / length
    cands = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]

    def _score(n):
        perp = 1.0 - abs(n[0] * cx + n[1] * cy + n[2] * cz)  # ~1 if chord lies IN the plane
        align = -(n[0] * prev_normal[0] + n[1] * prev_normal[1] +
                  n[2] * prev_normal[2])  # prefer similar to before
        return (perp, align)

    return max(cands, key=_score)


def apply_axis_lock(new_pt, anchor, axis):
    """Constrain `new_pt` to the world axis ('X'/'Y'/'Z' or None) through
    `anchor`: the locked point keeps the anchor's other two coords and
    takes only the moving coord from `new_pt`. None/unknown axis -> the
    point unchanged. Pure math, so Tab/X/Y/Z behavior is headless-tested."""
    if axis == "X":
        return (new_pt[0], anchor[1], anchor[2])
    if axis == "Y":
        return (anchor[0], new_pt[1], anchor[2])
    if axis == "Z":
        return (anchor[0], anchor[1], new_pt[2])
    return new_pt


def cycle_plane(current):
    """Next plane family for the Tab key. Order matches the panel combo."""
    order = ("Camera", "XY", "YZ", "ZX", "Auto")
    try:
        return order[(order.index(current) + 1) % len(order)]
    except ValueError:
        return "Camera"


def _safe_pt(pts, i):
    try:
        return pts[i]
    except (IndexError, TypeError):
        return None


def constraint_anchor(constr, pts):
    """A representative 3D point for a constraint: badge site in the
    viewport overlay and the hover-pick target. Returns None when the
    referenced indices are out of range."""
    kind = constr[0]
    if kind == "distance":
        a, b = _safe_pt(pts, constr[1]), _safe_pt(pts, constr[2])
        if a is None or b is None:
            return None
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)
    if kind in ("coincide", "concentric", "horizontal", "vertical", "fix"):
        return _safe_pt(pts, constr[1])
    if kind in ("angle", "tangent"):
        return _safe_pt(pts, constr[2])   # vertex / pivot
    if kind in ("g2", "g3"):
        return _safe_pt(pts, constr[2])   # joint point
    if kind == "equal":
        a, b = _safe_pt(pts, constr[1]), _safe_pt(pts, constr[2])
        if a is None or b is None:
            return None
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)
    if kind == "symmetric":
        return _safe_pt(pts, constr[3])   # center
    if kind == "mirror":
        a, b = _safe_pt(pts, constr[1]), _safe_pt(pts, constr[2])
        if a is None or b is None:
            return None
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)
    return None


def constraint_glyph(kind):
    """One-glyph badge drawn at the constraint anchor in the viewport."""
    return {
        "distance": "D", "coincide": "C", "horizontal": "H",
        "vertical": "V", "angle": "A", "equal": "E", "tangent": "T",
        "concentric": "O", "symmetric": "S", "fix": "X", "mirror": "M",
        "g2": "2", "g3": "3",
    }.get(kind, "?")


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 +
            (a[2] - b[2]) ** 2) ** 0.5


def constraint_tooltip(constr, pts):
    """Human-readable constraint state with the live measured value,
    for list-item tooltips and the viewport hover readout. Never
    raises: out-of-range indices report '?' instead."""
    kind = constr[0]
    try:
        if kind == "distance":
            a, b = pts[constr[1]], pts[constr[2]]
            target = constr[3]
            now = "now %.2f" % _dist(a, b)
            if isinstance(target, str):
                return ("distance |P%d-P%d| target %s (%s)"
                        % (constr[1], constr[2], target, now))
            return ("distance |P%d-P%d| target %.2f (%s)"
                    % (constr[1], constr[2], target, now))
        if kind == "coincide":
            a, b = pts[constr[1]], pts[constr[2]]
            return ("coincident P%d == P%d (gap %.3f)"
                    % (constr[1], constr[2], _dist(a, b)))
        if kind in ("horizontal", "vertical"):
            p = pts[constr[1]]
            axis, val = ("y", p[1]) if kind == "horizontal" else ("x", p[0])
            target = constr[2]
            if isinstance(target, str):
                return ("%s P%d target %s (now %s=%.2f)"
                        % (kind, constr[1], target, axis, val))
            return ("%s P%d target %.2f (now %s=%.2f)"
                    % (kind, constr[1], target, axis, val))
        if kind == "angle":
            target = constr[4]
            if isinstance(target, str):
                return ("angle(P%d,P%d,P%d) target %s"
                        % (constr[1], constr[2], constr[3], target))
            return ("angle(P%d,P%d,P%d) target %.1f deg"
                    % (constr[1], constr[2], constr[3], target))
        if kind == "equal":
            a, b = pts[constr[1]], pts[constr[2]]
            c, d = pts[constr[3]], pts[constr[4]]
            return ("equal |P%d-P%d| (%.2f) == |P%d-P%d| (%.2f)"
                    % (constr[1], constr[2], _dist(a, b),
                       constr[3], constr[4], _dist(c, d)))
        if kind == "fix":
            p = pts[constr[1]]
            return ("fixed P%d at (%.2f, %.2f, %.2f)"
                    % (constr[1], p[0], p[1], p[2]))
        if kind == "tangent":
            return ("tangent P%d-P%d-P%d collinear"
                    % (constr[1], constr[2], constr[3]))
        if kind == "concentric":
            a, b = pts[constr[1]], pts[constr[2]]
            return ("concentric P%d == P%d (gap %.3f)"
                    % (constr[1], constr[2], _dist(a, b)))
        if kind == "symmetric":
            return ("symmetric P%d,P%d about P%d"
                    % (constr[1], constr[2], constr[3]))
        if kind == "mirror":
            return ("mirror P%d <-> P%d across plane n=(%.2f,%.2f,%.2f) d=%.2f"
                    % (constr[1], constr[2], constr[3], constr[4],
                       constr[5], constr[6]))
        if kind == "g2":
            return ("G2 curvature P%d-P%d-P%d continuous"
                    % (constr[1], constr[2], constr[3]))
        if kind == "g3":
            return ("G3 jerk P%d-P%d-P%d-P%d continuous"
                    % (constr[1], constr[2], constr[3], constr[4]))
    except (IndexError, TypeError):
        pass
    return "%s ? (bad indices)" % kind


import ast as _ast
import math as _math


_EXPR_FUNCS = {
    "sqrt": _math.sqrt, "sin": _math.sin, "cos": _math.cos,
    "tan": _math.tan, "asin": _math.asin, "acos": _math.acos,
    "atan": _math.atan, "atan2": _math.atan2, "exp": _math.exp,
    "log": _math.log, "abs": abs, "min": min, "max": max,
    "radians": _math.radians, "degrees": _math.degrees,
}
_EXPR_CONSTS = {"pi": _math.pi, "e": _math.e}


# --- Equation manager data model (issues.md #10 / #15) ---------------- #
# Variable metadata schema:
#   name -> {"value": float, "unit": str, "comment": str,
#            "suppressed": bool, "linked": bool}
# Configuration table schema:
#   configs = [{"name": str, "variables": {name: float},
#               "suppressed_constraints": [idx, ...],
#               "suppressed_points": [idx, ...]}]


def variable_meta_default(name, value):
    """Create default variable metadata entry."""
    return {"value": float(value), "unit": "mm", "comment": "",
            "suppressed": False, "linked": False}


def variable_meta_to_flat(meta):
    """Convert metadata dict to flat dict for expression evaluation."""
    return {name: info["value"] for name, info in meta.items()}


def evaluate_expression(expr, namespace):
    """Evaluate a driving-dimension expression safely (no imports,
    attributes, lambdas, or statements -- arithmetic + whitelisted
    math calls + names only). `namespace` maps names to numbers.
    Returns a float. Raises ValueError on anything unsafe/unknown."""
    if isinstance(expr, (int, float)):
        return float(expr)
    text = str(expr).strip()
    if text.startswith("="):
        text = text[1:]
    try:
        tree = _ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError("bad expression %r (%s)" % (expr, exc))

    def _eval(node):
        if isinstance(node, _ast.Expression):
            return _eval(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, bool) or \
                    not isinstance(node.value, (int, float)):
                raise ValueError("only numbers allowed in %r" % expr)
            return float(node.value)
        if isinstance(node, _ast.Name):
            if node.id in namespace:
                v = namespace[node.id]
                if not isinstance(v, (int, float)):
                    raise ValueError("name %r is not a number" % node.id)
                return float(v)
            if node.id in _EXPR_CONSTS:
                return _EXPR_CONSTS[node.id]
            raise ValueError("unknown name %r in %r" % (node.id, expr))
        if isinstance(node, _ast.BinOp) and isinstance(
                node.op, (_ast.Add, _ast.Sub, _ast.Mult, _ast.Div,
                          _ast.Pow, _ast.Mod)):
            l, r = _eval(node.left), _eval(node.right)
            if isinstance(node.op, _ast.Add):
                return l + r
            if isinstance(node.op, _ast.Sub):
                return l - r
            if isinstance(node.op, _ast.Mult):
                return l * r
            if isinstance(node.op, _ast.Div):
                return l / r
            if isinstance(node.op, _ast.Pow):
                return l ** r
            return l % r
        if isinstance(node, _ast.UnaryOp) and isinstance(
                node.op, (_ast.UAdd, _ast.USub)):
            v = _eval(node.operand)
            return v if isinstance(node.op, _ast.UAdd) else -v
        if isinstance(node, _ast.Call):
            if not isinstance(node.func, _ast.Name) or \
                    node.func.id not in _EXPR_FUNCS or node.keywords:
                raise ValueError("only math(...) calls allowed in %r" % expr)
            return float(_EXPR_FUNCS[node.func.id](
                *(_eval(a) for a in node.args)))
        raise ValueError("unsupported syntax in %r" % expr)

    try:
        result = _eval(tree)
    except ZeroDivisionError:
        raise ValueError("division by zero in %r" % expr)
    if not isinstance(result, (int, float)):
        raise ValueError("expression %r did not produce a number" % expr)
    return float(result)


def expression_namespace(variables, points):
    """Name -> number mapping for driving dimensions: user variables
    plus P{i}x/P{i}y/P{i}z for every sketch point."""
    ns = dict(variables or {})
    for i, p in enumerate(points or []):
        try:
            ns["P%dx" % i], ns["P%dy" % i], ns["P%dz" % i] = \
                float(p[0]), float(p[1]), float(p[2])
        except (TypeError, IndexError):
            continue
    return ns


def resolve_constraint_expressions(constraints, variables, points):
    """Replace '=expr' dimension targets with their evaluated floats
    (using the CURRENT points, like a sketcher re-solving against the
    live sketch). Plain-number targets pass through untouched. Raises
    ValueError naming the offending constraint on failure."""
    ns = expression_namespace(variables, points)
    out = []
    for c in constraints:
        kind = c[0]
        try:
            if kind == "distance" and isinstance(c[3], str):
                out.append((kind, c[1], c[2],
                            evaluate_expression(c[3], ns)))
            elif kind in ("horizontal", "vertical") and isinstance(c[2], str):
                out.append((kind, c[1], evaluate_expression(c[2], ns)))
            elif kind == "angle" and isinstance(c[4], str):
                out.append((kind, c[1], c[2], c[3],
                            evaluate_expression(c[4], ns)))
            else:
                out.append(c)
        except ValueError as exc:
            raise ValueError("%s: %s" % (kind, exc))
    return out


def mirror_plane_params(name):
    """(nx, ny, nz, d) for a principal mirror plane through the origin,
    or None for an unknown name."""
    return {
        "XY": (0.0, 0.0, 1.0, 0.0),
        "YZ": (1.0, 0.0, 0.0, 0.0),
        "ZX": (0.0, 1.0, 0.0, 0.0),
    }.get(name)


def reflect_point(p, normal, offset):
    """Reflection of point p across the plane n.q + offset == 0, or
    None for a degenerate normal. Pure math (panel mirrors live drags
    through this; the solver enforces the persisted pairs)."""
    nx, ny, nz = normal
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return None
    nx, ny, nz = nx / length, ny / length, nz / length
    s = p[0] * nx + p[1] * ny + p[2] * nz + offset
    return (p[0] - 2.0 * s * nx,
            p[1] - 2.0 * s * ny,
            p[2] - 2.0 * s * nz)


def project_point_to_plane(pt, origin, normal):
    """Orthogonal projection of pt onto the construction plane, or None
    for a degenerate normal. Pure math (panel 'project selected edges'
    samples model edges through this)."""
    nx, ny, nz = normal
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return None
    nx, ny, nz = nx / length, ny / length, nz / length
    s = ((pt[0] - origin[0]) * nx + (pt[1] - origin[1]) * ny +
         (pt[2] - origin[2]) * nz)
    return (pt[0] - s * nx, pt[1] - s * ny, pt[2] - s * nz)


def project_point_along(pt, origin, normal, direction=None):
    """Projection of pt onto the plane (origin, normal) along a direction
    (issue #14 -- 'project along plane normal vs along view direction').

    direction None  -> drop straight down the plane normal, i.e. exactly
                       project_point_to_plane (the pre-#14 behavior).
    direction (dx,dy,dz) -> travel p + s*d until the plane; returns None
                       when d is parallel to the plane (never lands).

    Pure math; normalized like project_point_to_plane so a non-unit
    direction (e.g. a raw camera vector) behaves identically to its
    unit twin."""
    if direction is None:
        return project_point_to_plane(pt, origin, normal)
    nx, ny, nz = normal
    nlen = (nx * nx + ny * ny + nz * nz) ** 0.5
    dx, dy, dz = direction
    dlen = (dx * dx + dy * dy + dz * dz) ** 0.5
    if nlen < 1e-12 or dlen < 1e-12:
        return None
    nx, ny, nz = nx / nlen, ny / nlen, nz / nlen
    dx, dy, dz = dx / dlen, dy / dlen, dz / dlen
    denom = dx * nx + dy * ny + dz * nz
    if abs(denom) < 1e-12:
        return None   # parallel to the plane: no intersection
    s = ((origin[0] - pt[0]) * nx + (origin[1] - pt[1]) * ny +
         (origin[2] - pt[2]) * nz) / denom
    return (pt[0] + s * dx, pt[1] + s * dy, pt[2] + s * dz)


# --- associative projection refs (issues #14 / #7) --------------------- #
# One string per point on Sketch3DFP.ProjectedRefs, '' = free point:
#   'edge|<ObjectName>|EdgeN|<frac>|<n|d>|<dir>'   source edge, frac is
#       the normalized parameter (0..1 across First..Last) so a rebuilt
#       source edge of different length still maps to the same relative
#       spot.
#   'slice|<ObjectName>|<edgeIndex>|<frac>|<n|d>|<dir>'  cross-section
#       edge from plane-of-sketch intersecting the source shape.
#   'sil|<ObjectName>|<edgeIndex>|<frac>|<n|d>|<dir>'  silhouette edge
#       (edges whose adjacent faces disagree in facing along <dir>).
#   'pierce|<ObjectName>|EdgeN/FaceN|0|n|'  plane-edge crossing /
#       plane-face intersection curve point (issue #7).
#   'onsurf|<ObjectName>|FaceN|0|n|'  nearest point on the face
#       (issue #7); t/mode/dir unused (0 / 'n' / '').
# mode 'n' = project along the persisted plane normal, 'd' = along the
# stored direction vector (captured view direction at project time) --
# both deterministic across recompute.

_PROJ_REF_FIELDS = 6


def encode_proj_ref(kind, obj_name, locator, t, direction=None):
    """Format one associative-projection ref string. See parse_proj_ref
    for the grammar. direction None encodes mode 'n' (plane normal)."""
    if direction is None:
        mode, dir_s = "n", ""
    else:
        mode = "d"
        dir_s = ",".join(repr(float(v)) for v in direction)
    return "|".join((kind, obj_name, locator, repr(float(t)), mode, dir_s))


def parse_proj_ref(ref):
    """Ref string -> {'kind','obj','locator','t','direction'} dict, or
    None for '' / malformed / out-of-range values. Callers must treat
    None as 'point is free' (never as an error)."""
    if not ref:
        return None
    parts = str(ref).split("|")
    if len(parts) != _PROJ_REF_FIELDS:
        return None
    kind, obj_name, locator, t_s, mode, dir_s = parts
    if kind not in ("edge", "slice", "sil", "pierce", "onsurf") or \
            not obj_name or not locator:
        return None
    try:
        t = float(t_s)
    except ValueError:
        return None
    if not 0.0 <= t <= 1.0:
        return None
    if mode == "n":
        direction = None
    elif mode == "d":
        try:
            direction = tuple(float(v) for v in dir_s.split(","))
        except ValueError:
            return None
        if len(direction) != 3:
            return None
    else:
        return None
    return {"kind": kind, "obj": obj_name, "locator": locator,
            "t": t, "direction": direction}


# --- Equation Manager & Configuration Table (issues.md #10 / #15) ----- #
# The EquationManager provides a global equation manager dialog, design
# table (configurations), suppression state, and SolidWorks equations.txt
# import/export. It is headless-testable (no Qt dependency in the core).


class EquationManager:
    """Global equation manager for a document.

    Manages named variables with metadata (units, comments, suppression,
    linking), configuration tables (design tables), and suppression state
    per constraint/point/handle. Provides SolidWorks-style equations.txt
    import/export.

    Data model (all plain Python types for headless testability):
      variables = {name: {"value": float, "unit": str, "comment": str,
                         "suppressed": bool, "linked": bool}}
      configs = [{"name": str, "variables": {name: float},
                  "suppressed_constraints": [idx, ...],
                  "suppressed_points": [idx, ...],
                  "suppressed_handles": [idx, ...]}]
      linked_objects = [obj_name, ...]  # names of objects sharing variables

    The active configuration index controls which variable values are
    used for expression evaluation.
    """

    def __init__(self, doc):
        self.doc = doc
        self.variables = {}       # name -> meta dict
        self.configs = []         # list of config dicts
        self.active_config = 0    # index into configs
        self.linked_objects = []  # names of objects sharing this manager

    # --- Variable management ---

    def add_variable(self, name, value=0.0, unit="mm", comment=""):
        """Add or update a variable. Returns the metadata dict."""
        if name in self.variables:
            self.variables[name]["value"] = float(value)
            if unit: self.variables[name]["unit"] = unit
            if comment: self.variables[name]["comment"] = comment
        else:
            self.variables[name] = variable_meta_default(name, value)
            self.variables[name]["unit"] = unit
            self.variables[name]["comment"] = comment
        self._propagate_to_linked(name)
        return self.variables[name]

    def remove_variable(self, name):
        if name in self.variables:
            del self.variables[name]
            # Also remove from all configs
            for cfg in self.configs:
                cfg["variables"].pop(name, None)
            self._propagate_to_linked(name, removed=True)

    def set_variable_value(self, name, value):
        if name in self.variables:
            self.variables[name]["value"] = float(value)
            self._propagate_to_linked(name)
            return True
        return False

    def get_variable_value(self, name):
        if name in self.variables:
            return self.variables[name]["value"]
        return None

    def get_variable_meta(self, name):
        return self.variables.get(name)

    def get_all_variables(self):
        """Return flat dict for expression evaluation (current config)."""
        flat = variable_meta_to_flat(self.variables)
        # Override with active config values if any
        if self.configs and 0 <= self.active_config < len(self.configs):
            cfg_vars = self.configs[self.active_config].get("variables", {})
            flat.update(cfg_vars)
        return flat

    def _propagate_to_linked(self, name, removed=False):
        """Propagate variable changes to linked sketch objects."""
        # This is a hook; actual propagation happens via the panel
        pass

    # --- Configuration / Design Table management ---

    def add_config(self, name, variables=None, suppressed_constraints=None,
                   suppressed_points=None, suppressed_handles=None):
        """Add a new configuration. Returns the config index."""
        cfg = {"name": name,
               "variables": dict(variables) if variables else {},
               "suppressed_constraints": list(suppressed_constraints) if suppressed_constraints else [],
               "suppressed_points": list(suppressed_points) if suppressed_points else [],
               "suppressed_handles": list(suppressed_handles) if suppressed_handles else []}
        self.configs.append(cfg)
        return len(self.configs) - 1

    def remove_config(self, idx):
        if 0 <= idx < len(self.configs):
            self.configs.pop(idx)
            if self.active_config >= len(self.configs):
                self.active_config = max(0, len(self.configs) - 1)
            return True
        return False

    def set_active_config(self, idx):
        if 0 <= idx < len(self.configs):
            self.active_config = idx
            return True
        return False

    def get_active_config(self):
        if self.configs and 0 <= self.active_config < len(self.configs):
            return self.configs[self.active_config]
        return None

    def update_config_variables(self, idx, variables):
        if 0 <= idx < len(self.configs):
            self.configs[idx]["variables"].update(variables)
            return True
        return False

    def update_config_suppression(self, idx, constraints=None, points=None, handles=None):
        if 0 <= idx < len(self.configs):
            if constraints is not None:
                self.configs[idx]["suppressed_constraints"] = list(constraints)
            if points is not None:
                self.configs[idx]["suppressed_points"] = list(points)
            if handles is not None:
                self.configs[idx]["suppressed_handles"] = list(handles)
            return True
        return False

    def is_suppressed(self, kind, idx):
        """Check if a constraint/point/handle is suppressed in active config."""
        cfg = self.get_active_config()
        if not cfg:
            return False
        key = f"suppressed_{kind}s"
        return idx in cfg.get(key, [])

    # --- Linked objects ---

    def link_object(self, obj_name):
        if obj_name not in self.linked_objects:
            self.linked_objects.append(obj_name)

    def unlink_object(self, obj_name):
        if obj_name in self.linked_objects:
            self.linked_objects.remove(obj_name)

    # --- SolidWorks equations.txt import/export ---

    def export_equations_txt(self):
        """Export variables and configs to SolidWorks-style equations.txt format.

        Format:
          "VarName" = Value "Unit"  ' Comment
          $CONFIG "ConfigName"
          "VarName" = Value
          $ENDCONFIG
        """
        lines = []
        # Global variables
        for name, meta in sorted(self.variables.items()):
            if meta.get("suppressed"):
                continue
            val = meta["value"]
            unit = meta.get("unit", "mm")
            comment = meta.get("comment", "")
            line = f'"{name}" = {val} "{unit}"'
            if comment:
                line += f"  '{comment}"
            lines.append(line)
        # Configurations
        for cfg in self.configs:
            lines.append(f'$CONFIG "{cfg["name"]}"')
            for name, val in sorted(cfg.get("variables", {}).items()):
                lines.append(f'"{name}" = {val}')
            if cfg.get("suppressed_constraints"):
                lines.append(f'  $SUPPRESS_CONSTRAINTS {",".join(map(str, cfg["suppressed_constraints"]))}')
            if cfg.get("suppressed_points"):
                lines.append(f'  $SUPPRESS_POINTS {",".join(map(str, cfg["suppressed_points"]))}')
            if cfg.get("suppressed_handles"):
                lines.append(f'  $SUPPRESS_HANDLES {",".join(map(str, cfg["suppressed_handles"]))}')
            lines.append("$ENDCONFIG")
        return "\n".join(lines)

    def import_equations_txt(self, text):
        """Import from SolidWorks-style equations.txt format.

        Returns (success: bool, errors: list[str]).
        """
        errors = []
        self.variables.clear()
        self.configs.clear()
        self.active_config = 0
        current_cfg = None
        in_cfg = False

        for line_num, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Config start
            if line.startswith('$CONFIG'):
                import re
                m = re.match(r'\$CONFIG\s+"([^"]+)"', line)
                if m:
                    current_cfg = {"name": m.group(1), "variables": {},
                                   "suppressed_constraints": [],
                                   "suppressed_points": [],
                                   "suppressed_handles": []}
                    in_cfg = True
                else:
                    errors.append(f"Line {line_num}: bad $CONFIG syntax")
                continue
            # Config end
            if line == '$ENDCONFIG':
                if in_cfg and current_cfg:
                    self.configs.append(current_cfg)
                    current_cfg = None
                    in_cfg = False
                continue
            # Suppression in config
            if in_cfg and current_cfg is not None:
                if line.startswith('$SUPPRESS_CONSTRAINTS'):
                    parts = line.split()
                    if len(parts) > 1:
                        current_cfg["suppressed_constraints"] = [int(x) for x in parts[1].split(",") if x.strip()]
                    continue
                if line.startswith('$SUPPRESS_POINTS'):
                    parts = line.split()
                    if len(parts) > 1:
                        current_cfg["suppressed_points"] = [int(x) for x in parts[1].split(",") if x.strip()]
                    continue
                if line.startswith('$SUPPRESS_HANDLES'):
                    parts = line.split()
                    if len(parts) > 1:
                        current_cfg["suppressed_handles"] = [int(x) for x in parts[1].split(",") if x.strip()]
                    continue
                # Variable in config
                import re
                m = re.match(r'"([^"]+)"\s*=\s*([-\d.]+)', line)
                if m:
                    name, val = m.group(1), float(m.group(2))
                    current_cfg["variables"][name] = val
                continue
            # Global variable
            import re
            m = re.match(r'"([^"]+)"\s*=\s*([-\d.]+)\s*"([^"]+)"(?:\s*\'(.*))?', line)
            if m:
                name, val, unit, comment = m.group(1), float(m.group(2)), m.group(3), m.group(4) or ""
                self.add_variable(name, val, unit, comment)
                continue
            # Fallback without unit
            m = re.match(r'"([^"]+)"\s*=\s*([-\d.]+)(?:\s*\'(.*))?', line)
            if m:
                name, val, comment = m.group(1), float(m.group(2)), m.group(3) or ""
                self.add_variable(name, val, "mm", comment)
                continue
            errors.append(f"Line {line_num}: unrecognized format: {line[:50]}")
        return len(errors) == 0, errors


def _equation_manager_registry():
    """Internal: document -> EquationManager registry (for headless access).
    
    Uses document FileName as stable key (survives rename). For unsaved
    documents, falls back to document Name with a generated UUID suffix.
    """
    if not hasattr(_equation_manager_registry, "_cache"):
        _equation_manager_registry._cache = {}
    return _equation_manager_registry._cache


def _doc_stable_key(doc) -> str:
    """Return a stable key for a document that survives rename.
    Uses FileName for saved docs, falls back to Name + UUID for unsaved."""
    fname = getattr(doc, "FileName", "") or ""
    if fname:
        return f"file:{fname}"
    # Unsaved document: use Name + a generated UUID-like suffix
    # The Name is read-only but we can use it as base
    return f"name:{getattr(doc, 'Name', 'unsaved')}"


def get_equation_manager(doc):
    """Get or create the EquationManager for a document."""
    reg = _equation_manager_registry()
    key = _doc_stable_key(doc)
    if key not in reg:
        reg[key] = EquationManager(doc)
    return reg[key]


def serialize_equation_manager(em):
    """Serialize EquationManager state for persistence."""
    return {
        "variables": em.variables,
        "configs": em.configs,
        "active_config": em.active_config,
        "linked_objects": em.linked_objects,
    }


def deserialize_equation_manager(doc, data):
    """Deserialize EquationManager state from persistence."""
    em = get_equation_manager(doc)
    em.variables = data.get("variables", {})
    em.configs = data.get("configs", [])
    em.active_config = data.get("active_config", 0)
    em.linked_objects = data.get("linked_objects", [])
    return em


# --- per-point under/over-defined diagnostics (issues.md #8) -------------- #
# Constraint-level badges (constraint_badges) tell the user WHICH dim is
# violated.  Per-row DOF analysis goes one level deeper: for each sketch
# point, how many coordinate degrees of freedom does it have, how many are
# actually constrained by the incident constraint set, and is the point
# under-defined (free to move), over-defined (conflicting dims), or
# fully-defined?  Pure math -- headless-testable, no FreeCAD/numpy needed.

def _gauss_rank(matrix, tol=1e-9):
    """Rank of a real matrix via Gaussian elimination with partial pivoting.
    Pure Python (no numpy) so panel_logic stays dependency-free for
    headless test_panel_logic.py runs under a stock interpreter."""
    if not matrix or not matrix[0]:
        return 0
    m = [list(row) for row in matrix]
    n_rows = len(m)
    n_cols = len(m[0])
    rank = 0
    pivot_col = 0
    for col in range(n_cols):
        # find pivot row at or below `rank`
        best = None
        best_val = 0.0
        for r in range(rank, n_rows):
            v = abs(m[r][col])
            if v > best_val:
                best_val = v
                best = r
        if best is None or best_val < tol:
            continue
        m[rank], m[best] = m[best], m[rank]
        piv = m[rank][col]
        for r in range(n_rows):
            if r != rank and abs(m[r][col]) > tol:
                factor = m[r][col] / piv
                for c in range(col, n_cols):
                    m[r][c] -= factor * m[rank][c]
        rank += 1
        if rank >= n_rows:
            break
    return rank


def _constraint_jacobian_row(c, pts, residual_fn=None):
    """Numeric Jacobian rows for one constraint w.r.t. all point coords.
    Returns a list of row-vectors (one per residual entry), each of length
    3 * len(pts), with zeros everywhere except the columns of the points
    the constraint touches.  Finite-difference step is scaled to the
    coordinate magnitude.  `pts` is a list of objects with .x/.y/.z
    (e.g. FreeCAD.Vector or any duck-typed triple).  `residual_fn` is an
    optional callable (pts, [c]) -> list[float]; defaults to
    core.constraints._constraint_residuals when available, injected by
    tests so panel_logic stays importable without FreeCAD."""
    if residual_fn is None:
        try:
            from rfstudio.core.constraints import _constraint_residuals as _cr
        except Exception:
            return [[0.0] * (3 * len(pts))]
        residual_fn = _cr
    eps = 1e-6
    n_pts = len(pts)
    base = residual_fn(pts, [c])
    n_res = len(base)
    rows = [[0.0] * (3 * n_pts) for _ in range(n_res)]
    for pi in range(n_pts):
        p0 = pts[pi]
        for coord in range(3):
            orig = (p0.x, p0.y, p0.z)[coord]
            step = eps * (1.0 + abs(orig))
            new_vals = [p0.x, p0.y, p0.z]
            new_vals[coord] = orig + step
            pts[pi] = _Point3(*new_vals)
            try:
                pert = residual_fn(pts, [c])
            except Exception:
                pert = base
            pts[pi] = p0
            for k in range(n_res):
                rows[k][3 * pi + coord] = (pert[k] - base[k]) / step
    return rows


class _Point3(object):
    """Duck-typed 3D point with .x/.y/.z for use inside _constraint_jacobian_row."""
    __slots__ = ("x", "y", "z")
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def point_involved_constraints(pts, constraints, indices_fn=None):
    """Map point index -> list of constraint indices that reference it.
    Pure dict[int, list[int]]; the panel uses this to list, for a clicked
    point, exactly which dims are fighting it (drag-to-diagnose).  `indices_fn`
    is an optional callable (c) -> tuple of point indices; defaults to
    core.constraints._constraint_point_indices when available, injected by
    tests so panel_logic stays importable without FreeCAD."""
    if indices_fn is None:
        try:
            from rfstudio.core.constraints import \
                _constraint_point_indices as _cpi
        except Exception:
            return {}
        indices_fn = _cpi
    out = {}
    for ci, c in enumerate(constraints):
        for pi in indices_fn(c):
            if 0 <= pi < len(pts):
                out.setdefault(pi, []).append(ci)
    return out


def point_dof_analysis(pts, constraints, pinned=None, residual_fn=None):
    """Per-point degrees-of-freedom analysis for a 3D sketch.

    Returns a dict mapping point index -> {
        "dof": int,            # coordinate DOF (3 in global 3D)
        "rank": int,           # rank of incident constraint Jacobian
        "state": "under"|"over"|"well"|"fixed",
        "n_constraints": int,  # how many constraints touch this point
        "constraints": [ci, ...],  # constraint indices involving this point
        "free_dof": int,       # dof - rank when under, 0 otherwise
        "redundant": int,      # rank - dof when over, 0 otherwise
    }

    pinned points are reported as "fixed" (they are held by the user or a
    fix constraint and do not participate in the solve as unknowns).
    Points not referenced by any constraint are "under" with dof=3, rank=0
    (completely free) -- unless pinned.

    Pure math (Gaussian-elimination rank, no numpy), so this runs
    headlessly under `python test_panel_logic.py`.  The panel colors each
    row from the returned state: red = over, amber = under, green = well,
    gray = fixed.
    """
    pinned = set(pinned or ())
    n_pts = len(pts)
    involved = point_involved_constraints(pts, constraints)
    out = {}
    for pi in range(n_pts):
        if pi in pinned:
            out[pi] = {
                "dof": 0, "rank": 0, "state": "fixed",
                "n_constraints": len(involved.get(pi, [])),
                "constraints": list(involved.get(pi, [])),
                "free_dof": 0, "redundant": 0,
            }
            continue
        dof = 3
        con_indices = involved.get(pi, [])
        n_con = len(con_indices)
        if n_con == 0:
            out[pi] = {
                "dof": dof, "rank": 0, "state": "under",
                "n_constraints": 0, "constraints": [],
                "free_dof": dof, "redundant": 0,
            }
            continue
        # Build the sub-Jacobian: only rows of constraints touching pi,
        # only columns for pi's own 3 coords (rank w.r.t. this point alone
        # tells us whether this point's motion is constrained).  Using only
        # pi's columns is the right granularity for per-row badges: it
        # answers "if I grab this point, can I move it?"
        jac_rows = []
        for ci in con_indices:
            c = constraints[ci]
            try:
                rows = _constraint_jacobian_row(c, pts, residual_fn=residual_fn)
            except Exception:
                rows = []
            for row in rows:
                # keep only pi's 3 columns
                jac_rows.append([row[3 * pi], row[3 * pi + 1], row[3 * pi + 2]])
        rank = _gauss_rank(jac_rows) if jac_rows else 0
        rank = min(rank, dof)
        if rank < dof:
            state = "under"
        elif rank > dof:
            state = "over"
        else:
            state = "well"
        out[pi] = {
            "dof": dof, "rank": rank, "state": state,
            "n_constraints": n_con,
            "constraints": list(con_indices),
            "free_dof": dof - rank if state == "under" else 0,
            "redundant": rank - dof if state == "over" else 0,
        }
    return out


def point_dof_badge(state):
    """Short label + color hint for a per-point DOF state."""
    return {
        "under": ("under", "amber"),
        "over": ("over", "red"),
        "well": ("ok", "green"),
        "fixed": ("fixed", "gray"),
    }.get(state, ("?", "gray"))


def point_dof_summary(analysis):
    """One-line verdict from point_dof_analysis: counts per state."""
    counts = {}
    for pi, info in analysis.items():
        st = info["state"]
        counts[st] = counts.get(st, 0) + 1
    parts = []
    for st in ("over", "under", "well", "fixed"):
        if counts.get(st):
            parts.append("%d %s" % (counts[st], st))
    if not parts:
        return "no points"
    return "; ".join(parts)
