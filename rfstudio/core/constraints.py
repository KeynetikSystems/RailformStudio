# -*- coding: utf-8 -*-
"""Geometric constraint solving for the sketch/curve panel."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_hermite3, _nearest_facing_sides, _side_uv_trimmed, grid_to_surface)
from .patches import (_param_map)
from .manufacturing import (_monotone_cubic_fn)



# --- geometric constraint solving (sketch/curve panel) ------------------- #
# The curve panel used to satisfy its distance/angle/coincidence/H-V
# constraints with a hand-rolled Jacobi relaxation: walk the constraint
# list, nudge each pair HALFWAY toward satisfying its own constraint,
# repeat a fixed number of passes. That treats every constraint in
# ISOLATION each pass, so coupled constraints (two distances sharing a
# point, a distance plus an angle on the same vertex) fight each other and
# the pass either crawls toward a solution or oscillates without ever
# solving the system. solve_point_constraints replaces that with a real
# SIMULTANEOUS least-squares solve: all constraint residuals are stacked
# into one vector r(x) over the free point coordinates and driven to zero
# together with damped Gauss-Newton (Levenberg-Marquardt). This is the
# same class of solver a sketcher's numeric kernel uses; it is NOT a
# symbolic DCM (no equation-graph decomposition, no driving-dimension
# ordering, no geometric-theorem reasoning), but it solves the coupled
# numeric system in one shot instead of relaxing each constraint alone,
# and it reports whether the system came out well-/under-/over-constrained
# from the Jacobian rank and the final residual.
def _constraint_point_indices(c):
    """The point indices a single constraint tuple references."""
    kind = c[0]
    if kind == "distance":
        return (c[1], c[2])
    if kind in ("horizontal", "vertical"):
        return (c[1],)
    if kind == "angle":
        return (c[1], c[2], c[3])
    if kind == "coincide":
        return (c[1], c[2])
    return ()



def _constraint_residuals(pts, constraints):
    """Flat residual list for the whole constraint set at the current
    point positions -- each entry is zero exactly when its constraint is
    satisfied. Units are mixed (mm for positions/distances, radians for
    angles); that is fine for a least-squares drive as long as it stays
    consistent between the residual and its Jacobian, which it does since
    the Jacobian here is taken numerically from this same function."""
    res = []
    for c in constraints:
        kind = c[0]
        if kind == "distance":
            p1, p2 = pts[c[1]], pts[c[2]]
            res.append(p1.distanceToPoint(p2) - c[3])
        elif kind == "horizontal":
            res.append(pts[c[1]].y - c[2])
        elif kind == "vertical":
            res.append(pts[c[1]].x - c[2])
        elif kind == "angle":
            v1 = pts[c[1]] - pts[c[2]]
            v2 = pts[c[3]] - pts[c[2]]
            l1, l2 = v1.Length, v2.Length
            if l1 > 1e-9 and l2 > 1e-9:
                cosang = max(-1.0, min(1.0, v1.dot(v2) / (l1 * l2)))
                res.append(math.acos(cosang) - math.radians(c[4]))
            else:
                res.append(0.0)
        elif kind == "coincide":
            p1, p2 = pts[c[1]], pts[c[2]]
            res.append(p1.x - p2.x)
            res.append(p1.y - p2.y)
            res.append(p1.z - p2.z)
    return res



def _relax_point_constraints(pts, constraints, pinned, max_iter):
    """Faithful port of the panel's original Jacobi relaxation, kept as a
    dependency-free fallback for when numpy is unavailable. Behaviour
    matches the pre-solver code (each constraint nudged halfway per pass)
    except the H/V branches now write a real Vector instead of the stray
    3-tuple the old code assigned."""
    for _ in range(max_iter):
        changed = False
        for c in constraints:
            kind = c[0]
            if kind == "distance":
                i, j, target = c[1], c[2], c[3]
                p1, p2 = pts[i], pts[j]
                d = p1.distanceToPoint(p2)
                if d > 1e-9 and abs(d - target) > 1e-3:
                    direction = (p2 - p1) * (1.0 / d)
                    adj = direction * ((d - target) * 0.5)
                    if i not in pinned:
                        pts[i] = p1 + adj
                    if j not in pinned:
                        pts[j] = p2 - adj
                    changed = True
            elif kind == "horizontal":
                i, ty = c[1], c[2]
                if i not in pinned and abs(pts[i].y - ty) > 1e-3:
                    pts[i] = Vector(pts[i].x, ty, pts[i].z)
                    changed = True
            elif kind == "vertical":
                i, tx = c[1], c[2]
                if i not in pinned and abs(pts[i].x - tx) > 1e-3:
                    pts[i] = Vector(tx, pts[i].y, pts[i].z)
                    changed = True
            elif kind == "coincide":
                i, j = c[1], c[2]
                p1, p2 = pts[i], pts[j]
                if p1.distanceToPoint(p2) > 1e-3:
                    mid = (p1 + p2) * 0.5
                    if i not in pinned:
                        pts[i] = mid
                    if j not in pinned:
                        pts[j] = mid
                    changed = True
        if not changed:
            break
    return pts



def solve_point_constraints(points, constraints, pinned=None, tol=1e-16,
                            max_iter=80, status=None):
    """Simultaneously satisfy a set of 2D/3D geometric constraints over a
    point list, returning the adjusted points (input list is not mutated).

    Supported constraint tuples (the exact format the curve panel builds):
      ("distance", i, j, d)         -- |p_i - p_j| == d
      ("horizontal", i, y)          -- p_i.y == y
      ("vertical",   i, x)          -- p_i.x == x
      ("angle", i, vertex, k, deg)  -- angle(p_i, vertex, p_k) == deg
      ("coincide", i, j)            -- p_i == p_j

    `pinned`: iterable of point indices to hold fixed (e.g. the point the
                 user is dragging). Points not referenced by ANY constraint
                 are also held fixed -- only coordinates the constraint set
                 actually touches become unknowns, so an unconstrained
                 point never drifts and the solve stays small.
    `status`: optional dict; when given it is filled with
                 {"state": "well"|"under"|"over"|"solved",
                  "residual": final RMS residual, "dof": free-coord count,
                  "rank": Jacobian rank} so a caller can surface
                  well-/under-/over-constrained feedback.

    Primary solver is damped Gauss-Newton (Levenberg-Marquardt) over the
    stacked residual vector; if numpy is unavailable it falls back to the
    original per-constraint relaxation (see _relax_point_constraints).
    """
    pts = [Vector(p.x, p.y, p.z) for p in points]
    if not constraints:
        return pts
    pinned = set(pinned or ())
    involved = set()
    for c in constraints:
        involved.update(_constraint_point_indices(c))
    free_pts = sorted(i for i in involved
                      if i not in pinned and 0 <= i < len(pts))
    if not free_pts:
        return pts

    try:
        import numpy as np
    except Exception:
        if status is not None:
            status.update(state="unknown", residual=None,
                          dof=3 * len(free_pts), rank=None)
        return _relax_point_constraints(pts, constraints, pinned, max_iter)

    idx_of = {pi: k for k, pi in enumerate(free_pts)}
    U = 3 * len(free_pts)

    def set_x(x):
        for pi, k in idx_of.items():
            pts[pi] = Vector(float(x[3 * k]), float(x[3 * k + 1]),
                             float(x[3 * k + 2]))

    def get_x():
        x = np.empty(U)
        for pi, k in idx_of.items():
            p = pts[pi]
            x[3 * k], x[3 * k + 1], x[3 * k + 2] = p.x, p.y, p.z
        return x

    def residual(x):
        set_x(x)
        return np.asarray(_constraint_residuals(pts, constraints), float)

    x = get_x()
    r = residual(x)
    if r.size == 0:
        return pts
    cost = float(r @ r)
    lam = 1e-3
    J = np.zeros((r.size, U))
    for _ in range(max_iter):
        if cost < tol:
            break
        for k in range(U):
            xk = x[k]
            step = 1e-6 * (1.0 + abs(xk))
            x[k] = xk + step
            J[:, k] = (residual(x) - r) / step
            x[k] = xk
        JtJ = J.T @ J
        Jtr = J.T @ r
        eye = np.eye(U)
        improved = False
        for _try in range(10):
            try:
                delta = np.linalg.solve(JtJ + lam * eye, -Jtr)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            x_new = x + delta
            r_new = residual(x_new)
            cost_new = float(r_new @ r_new)
            if cost_new < cost:
                x, r, cost = x_new, r_new, cost_new
                lam = max(lam * 0.5, 1e-12)
                improved = True
                break
            lam = min(lam * 4.0, 1e12)
        if not improved:
            break
    set_x(x)

    if status is not None:
        rms = float(np.sqrt(cost / r.size)) if r.size else 0.0
        try:
            rank = int(np.linalg.matrix_rank(J))
        except Exception:
            rank = None
        if rms > 1e-4:
            state = "over"          # residual won't vanish -> conflicting
        elif rank is not None and rank < U:
            state = "under"         # free DOF remain -> under-constrained
        else:
            state = "well"
        status.update(state=state, residual=rms, dof=U, rank=rank)
    return pts



def rolling_ball_fillet(face_a, face_b, radius, n=24):
    """Approximate constant-radius ("rolling ball") fillet SURFACE strip
    between the two nearest-facing boundary edges of face_a and face_b,
    in real WORLD-UNIT radius rather than blend_surface's dimensionless
    `scale` factor.

    Native implementation of RFS_RollingBallFillet. At each of `n`
    samples along the matched boundary pair (trim-aware, via the same
    _side_uv_trimmed/_nearest_facing_sides machinery blend_surface
    already uses), this builds a cubic-Hermite cross-section whose
    tangent-handle length is derived from `radius` via the standard
    cubic-Bezier circular-arc approximation (kappa = radius * (4/3) *
    tan(theta/4), where theta is the angle between the two cross-
    boundary tangent directions) -- the same construction used to draw
    a "circle" with Bezier curves in 2D graphics, applied per
    cross-section here.

    HONESTY ABOUT WHAT THIS DOES AND DOESN'T GUARANTEE: this is an
    APPROXIMATION, not an exact rolling-ball construction (which would
    require solving where a literal sphere of `radius` sits tangent to
    both surfaces at every point along the seam -- a genuine offset-
    surface intersection problem, more involved than fits here). Each
    cross-section independently targets the given radius via the
    Bezier-kappa formula; the true osculating-circle radius of the
    resulting cubic is only approximately `radius` (better for small
    theta, i.e. two surfaces meeting at a shallow angle; increasingly
    approximate as the corner gets sharper). Use surface_deviation() or
    a direct radius measurement on the built strip to confirm the
    achieved radius on a specific model, exactly as match_surface's own
    docstring recommends for its continuity claims.

    Raises ValueError for a non-positive radius or a degenerate
    (zero-length) cross-boundary tangent anywhere along the sampled
    boundary.
    """
    if radius is None or radius <= 1e-9:
        raise ValueError(
            "rolling_ball_fillet: radius must be positive (got %r)" % radius)
    n = max(2, int(n))
    sa, sb, a_pr, b_pr = _nearest_facing_sides(face_a, face_b)
    edge_cache = {}

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return {0: du.negative(), 1: du, 2: dv.negative(), 3: dv}[side]

    grid = []
    for k in range(n):
        t = k / (n - 1.0)
        ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, edge_cache)
        ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, edge_cache)
        P0 = face_a.valueAt(ua, va)
        P1 = face_b.valueAt(ub, vb)
        T0 = _cross_tan(face_a, sa, ua, va)
        T1 = _cross_tan(face_b, sb, ub, vb)
        if T0.Length < 1e-9 or T1.Length < 1e-9:
            raise ValueError(
                "rolling_ball_fillet: degenerate cross-boundary tangent "
                "at t=%.3f -- the underlying surface may be singular "
                "there" % t)
        T0n = T0 * (1.0 / T0.Length)
        T1n = T1 * (1.0 / T1.Length)
        chord = max((P1 - P0).Length, 1e-9)
        cosang = max(-1.0, min(1.0, T0n.dot(T1n * -1.0)))
        theta = math.acos(cosang)
        kappa = radius * (4.0 / 3.0) * math.tan(max(theta, 1e-6) / 4.0)
        h = min(kappa, chord * 0.9)     # clamp: never overshoot the chord
        row = [_hermite3(P0, T0n * h, P1, (T1n * h) * -1.0, j / (n - 1.0))
              for j in range(n)]
        grid.append(row)
    return grid_to_surface(grid)



def multi_rail_fillet(face_a, face_b, radius_stations, n=24, smooth=True):
    """Rolling-ball fillet strip (see rolling_ball_fillet) whose RADIUS
    varies along the strip according to `radius_stations` -- the
    "multi-section fillet" case (e.g. R3 at one end tapering to R8 at
    the other, or several named stations along a long body-side seam).

    Unlike variable_fillet_solid (which is capped at a LINEAR two-radius
    taper by this FreeCAD build's makeFillet binding), this function builds
    the fillet itself, cross-section by cross-section, so it can honour a
    genuinely SMOOTH multi-station radius law: each cross-section reads the
    radius from the law at its own t, so the law is sampled at full strip
    resolution, not just at the stations.

    `radius_stations`: list of (t, radius) pairs, t in [0, 1] along the
                 matched boundary (the same normalized t
                 rolling_ball_fillet/_side_uv_trimmed sample by). Must
                 contain >= 2 stations; a single station is just a constant
                 radius and should use rolling_ball_fillet directly.
    `smooth`: when True (default) and >= 3 stations are given, the radius
                 law between stations is a MONOTONE, shape-preserving C1
                 cubic (Fritsch-Carlson / PCHIP, see _monotone_cubic_fn) --
                 so the radius tapers with a continuous slope through each
                 interior station instead of kinking (C0) at it, and,
                 because the interpolant is monotone/non-overshooting, the
                 radius never dips below the smaller of two neighbouring
                 stations (it can't go negative or bulge). With exactly 2
                 stations there is nothing to fair (two points are a line);
                 `smooth=False` forces the previous piecewise-LINEAR law
                 (C0 kink at each station) for callers who want it.

    Raises ValueError for fewer than 2 stations, any non-positive
    radius, or (propagated from the per-sample construction) a
    degenerate cross-boundary tangent anywhere along the boundary.
    """
    stations = sorted(radius_stations or [], key=lambda p: p[0])
    if len(stations) < 2:
        raise ValueError(
            "multi_rail_fillet: need >= 2 (t, radius) stations -- got %d "
            "(use rolling_ball_fillet for a constant radius)"
            % len(stations))
    for t, r in stations:
        if r is None or r <= 1e-9:
            raise ValueError(
                "multi_rail_fillet: radius must be positive (got %r at "
                "t=%r)" % (r, t))
    ts = [t for t, _ in stations]
    rs = [r for _, r in stations]
    if smooth and len(stations) >= 3:
        radius_fn = _monotone_cubic_fn(ts, rs)
    else:
        radius_fn = _param_map(ts, rs)

    n = max(2, int(n))
    sa, sb, a_pr, b_pr = _nearest_facing_sides(face_a, face_b)
    edge_cache = {}

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return {0: du.negative(), 1: du, 2: dv.negative(), 3: dv}[side]

    grid = []
    for k in range(n):
        t = k / (n - 1.0)
        r = radius_fn(t)
        ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, edge_cache)
        ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, edge_cache)
        P0 = face_a.valueAt(ua, va)
        P1 = face_b.valueAt(ub, vb)
        T0 = _cross_tan(face_a, sa, ua, va)
        T1 = _cross_tan(face_b, sb, ub, vb)
        if T0.Length < 1e-9 or T1.Length < 1e-9:
            raise ValueError(
                "multi_rail_fillet: degenerate cross-boundary tangent at "
                "t=%.3f" % t)
        T0n = T0 * (1.0 / T0.Length)
        T1n = T1 * (1.0 / T1.Length)
        chord = max((P1 - P0).Length, 1e-9)
        cosang = max(-1.0, min(1.0, T0n.dot(T1n * -1.0)))
        theta = math.acos(cosang)
        kappa = r * (4.0 / 3.0) * math.tan(max(theta, 1e-6) / 4.0)
        h = min(kappa, chord * 0.9)
        row = [_hermite3(P0, T0n * h, P1, (T1n * h) * -1.0, j / (n - 1.0))
              for j in range(n)]
        grid.append(row)
    return grid_to_surface(grid)

__all__ = [
    '_constraint_point_indices',
    '_constraint_residuals',
    '_relax_point_constraints',
    'solve_point_constraints',
    'rolling_ball_fillet',
    'multi_rail_fillet',
]
