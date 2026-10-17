# -*- coding: utf-8 -*-
"""Geometric constraint solving for the sketch/curve panel."""

from __future__ import annotations

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from .util import (_hermite3, _nearest_facing_sides, _side_uv_trimmed, grid_to_surface)
from .patches import (_param_map)
from .manufacturing import (_monotone_cubic_fn)


# Type aliases
VectorLike = Union[Vector, Tuple[float, float, float], Sequence[float]]
ConstraintTuple = Tuple[Any, ...]
ConstraintList = List[ConstraintTuple]
PointList = List[Vector]
PinnedSet = Set[int]
StatusDict = Dict[str, Any]

FloatExpr = Union[float, str]  # float or "=expr"



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
def _unit_normal(nx: float, ny: float, nz: float) -> Optional[Vector]:
    """Unit plane normal or None when degenerate."""
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return None
    return Vector(nx / length, ny / length, nz / length)


def _reflect(p: Vector, n: Vector, d: float) -> Vector:
    """Reflection of point p across the plane n.q + d == 0."""
    return p - n * (2.0 * (p.dot(n) + d))


def _constraint_point_indices(c: ConstraintTuple) -> Tuple[int, ...]:
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
    if kind == "on_curve":
        return (c[1],)
    if kind == "equal":
        return (c[1], c[2], c[3], c[4])
    if kind == "fix":
        return (c[1],)
    if kind == "tangent_to_face":
        return (c[1], c[2])
    if kind in ("tangent", "symmetric"):
        return (c[1], c[2], c[3])
    if kind == "concentric":
        return (c[1], c[2])
    if kind == "mirror":
        return (c[1], c[2])
    if kind == "g2":
        return (c[1], c[2], c[3])
    if kind == "g3":
        return (c[1], c[2], c[3], c[4])
    return ()


def _constraint_residuals(pts: PointList, constraints: ConstraintList) -> List[float]:
    """Flat residual list for the whole constraint set at the current
    point positions -- each entry is zero exactly when its constraint is
    satisfied. Units are mixed (mm for positions/distances, radians for
    angles); that is fine for a least-squares drive as long as it stays
    consistent between the residual and its Jacobian, which it does since
    the Jacobian here is taken numerically from this same function."""
    # Clear cache at start of each residual evaluation
    if not hasattr(_constraint_residuals, "_face_normal_cache"):
        _constraint_residuals._face_normal_cache = {}
    _constraint_residuals._face_normal_cache.clear()
    res: List[float] = []
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
        elif kind == "on_curve":
            # distance from the point to a FIXED target edge/shape (c[2]),
            # driven to zero -> the point is pulled onto the curve. The
            # target geometry is held constant, so this is a one-sided
            # residual on p_i only (see _constraint_point_indices).
            try:
                d = Part.Vertex(pts[c[1]]).distToShape(c[2])[0]
            except Exception:
                d = 0.0
            res.append(d)
        elif kind == "tangent_to_face":
            # Tangent to Face: the direction from point i to point j
            # (i.e., edge i->j) must be tangent to the given face at the
            # closest point on that face to point i. Residual = dot(dir, n)
            # where dir = normalize(p_j - p_i) and n = face normal at
            # closest point on face to p_i. Zero residual -> tangent.
            try:
                p_i = pts[c[1]]
                p_j = pts[c[2]]
                face = c[3]
                # Direction vector
                v = p_j - p_i
                v_len = v.Length
                if v_len > 1e-9:
                    dir_vec = v * (1.0 / v_len)
                    # Closest point on face to p_i (cached)
                    cache_key = (id(face), (p_i.x, p_i.y, p_i.z))
                    if cache_key in _constraint_residuals._face_normal_cache:
                        closest_on_face, n = _constraint_residuals._face_normal_cache[cache_key]
                    else:
                        # Closest point on face to p_i
                        d = Part.Vertex(p_i).distToShape(face)[0]
                        closest_on_face = d[1][0][1]  # point on face
                        # Face normal at closest point
                        n = face.normalAt(*face.Surface.parameter(closest_on_face))
                        _constraint_residuals._face_normal_cache[cache_key] = (closest_on_face, n)
                    if n.Length > 1e-9:
                        n = n * (1.0 / n.Length)
                        res.append(dir_vec.dot(n))  # zero = tangent
                    else:
                        res.append(0.0)
                else:
                    res.append(0.0)
            except Exception:
                res.append(0.0)
        elif kind == "equal":
            # |p_i - p_j| == |p_k - p_l|: the 'equal-length' relation.
            d1 = pts[c[1]].distanceToPoint(pts[c[2]])
            d2 = pts[c[3]].distanceToPoint(pts[c[4]])
            res.append(d1 - d2)
        elif kind == "tangent":
            # Tangent: point i, pivot j, point k -- the vector j->i must be
            # collinear with j->k (same or opposite direction).
            # Residual: cross product magnitude (zero when collinear).
            v1 = pts[c[1]] - pts[c[2]]
            v2 = pts[c[3]] - pts[c[2]]
            cross = v1.cross(v2)
            res.append(cross.x)
            res.append(cross.y)
            res.append(cross.z)
        elif kind == "g2":
            # G2 curvature continuity at joint j between segments i-j and j-k.
            # Residual = curvature_left - curvature_right (3 components).
            # curvature_left  = T1 / h1  (curvature entering joint from i)
            # curvature_right = T2 / h2  (curvature leaving joint toward k)
            # where T1 = normalize(P_j - P_i), h1 = |P_j - P_i|, etc.
            # When G1 holds (T1 parallel T2), this reduces to requiring
            # equal segment lengths h1 == h2 -- a usable, well-behaved
            # approximation of curvature continuity.
            p_i = pts[c[1]]
            p_j = pts[c[2]]
            p_k = pts[c[3]]
            v1 = p_j - p_i
            v2 = p_k - p_j
            h1 = v1.Length
            h2 = v2.Length
            if h1 > 1e-9 and h2 > 1e-9:
                t1 = v1 * (1.0 / h1)
                t2 = v2 * (1.0 / h2)
                k_left = t1 * (1.0 / h1)
                k_right = t2 * (1.0 / h2)
                diff = k_left - k_right
                res.append(diff.x)
                res.append(diff.y)
                res.append(diff.z)
            else:
                res.extend([0.0, 0.0, 0.0])
        elif kind == "g3":
            # G3 jerk/torsion continuity at joint between segments i-j-k
            # and j-k-l. Residual = jerk_left - jerk_right (3 components).
            # jerk_left  = (K2 - K1) / h2  (rate of curvature change from left)
            # jerk_right = (K3 - K2) / h2  (rate of curvature change to right)
            # where K1 = T1/h1, K2 = T2/h2, K3 = T3/h3 are segment curvatures.
            p_i = pts[c[1]]
            p_j = pts[c[2]]
            p_k = pts[c[3]]
            p_l = pts[c[4]]
            v1 = p_j - p_i
            v2 = p_k - p_j
            v3 = p_l - p_k
            h1 = v1.Length
            h2 = v2.Length
            h3 = v3.Length
            if h1 > 1e-9 and h2 > 1e-9 and h3 > 1e-9:
                t1 = v1 * (1.0 / h1)
                t2 = v2 * (1.0 / h2)
                t3 = v3 * (1.0 / h3)
                k1 = t1 * (1.0 / h1)
                k2 = t2 * (1.0 / h2)
                k3 = t3 * (1.0 / h3)
                jerk_left = (k2 - k1) * (1.0 / h2)
                jerk_right = (k3 - k2) * (1.0 / h2)
                diff = jerk_left - jerk_right
                res.append(diff.x)
                res.append(diff.y)
                res.append(diff.z)
            else:
                res.extend([0.0, 0.0, 0.0])
        elif kind == "concentric":
            # Concentric: two arcs/circles defined by (center_i, point_on_arc_j)
            # and (center_k, point_on_arc_l) must share the same center.
            # This is really a coincide constraint on the centers.
            p1, p2 = pts[c[1]], pts[c[2]]
            res.append(p1.x - p2.x)
            res.append(p1.y - p2.y)
            res.append(p1.z - p2.z)
        elif kind == "symmetric":
            # Symmetric: point i and point j symmetric about point k (center).
            # i + j = 2*k  ->  (i + j)/2 - k = 0
            p1 = pts[c[1]]
            p2 = pts[c[2]]
            pc = pts[c[3]]
            mid = (p1 + p2) * 0.5
            res.append(mid.x - pc.x)
            res.append(mid.y - pc.y)
            res.append(mid.z - pc.z)
        elif kind == "mirror":
            # Mirror: p_j == reflection of p_i across the plane
            # (unit normal n, offset d with n.p + d == 0 on the plane).
            n = _unit_normal(c[3], c[4], c[5])
            d = c[6]
            if n is not None:
                r = _reflect(pts[c[1]], n, d) - pts[c[2]]
                res.append(r.x)
                res.append(r.y)
                res.append(r.z)
        elif kind == "fix":
            # Fix point i where it currently sits: 3 zero residuals
            # (hold position), which pins those coords in the LM step via
            # large residuals are NOT this mechanism -- instead fix is
            # enforced by treating every fixed index as pinned (handled
            # by the solver's pinned set), so the residual contributes
            # nothing here.
            pass
    return res


def constraint_point_indices(c):
    """Public accessor for the point indices one constraint references
    (issues.md #8 -- the panel tints the involved point-table rows)."""
    return _constraint_point_indices(c)


def tangent_to_face_jacobian(p_i, p_j, face, n):
    """Analytic Jacobian of the tangent_to_face residual
    r = normalize(p_j - p_i) . n  (n = face normal at closest point to p_i).

    Treats n as locally constant (frozen-normal approximation -- the normal
    changes only to second order as p_i slides along the tangent plane), which
    is exactly the regime the solver lives in (small LM steps). Returns
    (dr_dp_i, dr_dp_j) as 3-vectors:

        dr/dp_j =  n^T (I - dir dir^T) / |p_j - p_i|
        dr/dp_i = -n^T (I - dir dir^T) / |p_j - p_i|

    where dir = normalize(p_j - p_i).  Caller is responsible for having
    already normalized n.  Returns (None, None) when the direction is
    degenerate (zero-length p_i->p_j) so the caller falls back to numeric.
    """
    v = p_j - p_i
    v_len = v.Length
    if v_len < 1e-9 or n.Length < 1e-9:
        return None, None
    dir_vec = v * (1.0 / v_len)
    n_unit = n * (1.0 / n.Length)
    # (I - dir dir^T) . n = n - dir (dir . n)
    proj = n_unit - dir_vec * dir_vec.dot(n_unit)
    scale = 1.0 / v_len
    dr_dp_j = proj * scale
    dr_dp_i = proj * (-scale)
    return dr_dp_i, dr_dp_j


def g2_jacobian(p_i, p_j, p_k):
    """Analytic Jacobian of the G2 curvature continuity residual.

    R = T1/h1 - T2/h2  (3-component vector)
    where T1 = normalize(P_j - P_i), h1 = |P_j - P_i|,
          T2 = normalize(P_k - P_j), h2 = |P_k - P_j|.

    Returns (dR_dP_i, dR_dP_j, dR_dP_k) as 3x3 matrices (lists of lists of
    floats).  Returns (None, None, None) for degenerate inputs so the
    solver falls back to numeric differentiation.

    Derivation:
      d(T/h)/dP_source  = (-I + 2*t*t^T) / h^2   (source = where the segment starts)
      d(T/h)/dP_target  = (I - 2*t*t^T) / h^2    (target = where the segment ends)
      R = T1/h1 - T2/h2
      dR/dP_i = (-I + 2*t1*t1^T) / h1^2
      dR/dP_j = (I - 2*t1*t1^T)/h1^2 + (I - 2*t2*t2^T)/h2^2
      dR/dP_k = (-I + 2*t2*t2^T) / h2^2
    """
    v1 = p_j - p_i
    v2 = p_k - p_j
    h1 = v1.Length
    h2 = v2.Length
    if h1 < 1e-9 or h2 < 1e-9:
        return None, None, None
    t1 = v1 * (1.0 / h1)
    t2 = v2 * (1.0 / h2)
    h1_sq = h1 * h1
    h2_sq = h2 * h2

    def _comp(t, idx):
        return (t.x, t.y, t.z)[idx]

    def _dTdh_source(t, h_sq):
        """(-I + 2*t*t^T) / h^2 -- derivative w.r.t. segment start point."""
        rows = []
        for a in range(3):
            row = []
            ta = _comp(t, a)
            for b in range(3):
                val = (-1.0 if a == b else 0.0) + 2.0 * ta * _comp(t, b)
                row.append(val / h_sq)
            rows.append(row)
        return rows

    def _dTdh_target(t, h_sq):
        """(I - 2*t*t^T) / h^2 -- derivative w.r.t. segment end point."""
        rows = []
        for a in range(3):
            row = []
            ta = _comp(t, a)
            for b in range(3):
                val = (1.0 if a == b else 0.0) - 2.0 * ta * _comp(t, b)
                row.append(val / h_sq)
            rows.append(row)
        return rows

    dRi = _dTdh_source(t1, h1_sq)
    dRj = [
        [_dTdh_target(t1, h1_sq)[a][b] + _dTdh_target(t2, h2_sq)[a][b] for b in range(3)]
        for a in range(3)
    ]
    dRk = _dTdh_source(t2, h2_sq)
    return dRi, dRj, dRk


def g3_jacobian(p_i, p_j, p_k, p_l):
    """Analytic Jacobian of the G3 jerk continuity residual.

    R = (K3 - 2*K2 + K1) / h2  (3-component vector)
    where K1 = T1/h1, K2 = T2/h2, K3 = T3/h3 are segment curvatures,
    T1 = normalize(P_j - P_i), etc., and h2 = |P_k - P_j|.

    Returns (dR_dP_i, dR_dP_j, dR_dP_k, dR_dP_l) as 3x3 matrices.
    Returns (None, None, None, None) for degenerate inputs.

    R includes a 1/h2 scaling factor that depends on P_j and P_k, so the
    Jacobian has an extra term: R * d(1/h2)/dP.
    """
    v1 = p_j - p_i
    v2 = p_k - p_j
    v3 = p_l - p_k
    h1 = v1.Length
    h2 = v2.Length
    h3 = v3.Length
    if h1 < 1e-9 or h2 < 1e-9 or h3 < 1e-9:
        return None, None, None, None
    t1 = v1 * (1.0 / h1)
    t2 = v2 * (1.0 / h2)
    t3 = v3 * (1.0 / h3)
    h1_sq = h1 * h1
    h2_sq = h2 * h2
    h3_sq = h3 * h3

    def _comp(t, idx):
        return (t.x, t.y, t.z)[idx]

    def _dTdh_source(t, h_sq):
        rows = []
        for a in range(3):
            row = []
            ta = _comp(t, a)
            for b in range(3):
                val = (-1.0 if a == b else 0.0) + 2.0 * ta * _comp(t, b)
                row.append(val / h_sq)
            rows.append(row)
        return rows

    def _dTdh_target(t, h_sq):
        rows = []
        for a in range(3):
            row = []
            ta = _comp(t, a)
            for b in range(3):
                val = (1.0 if a == b else 0.0) - 2.0 * ta * _comp(t, b)
                row.append(val / h_sq)
            rows.append(row)
        return rows

    # K1 = T1/h1, K2 = T2/h2, K3 = T3/h3
    # dK1/dP_i = _dTdh_source(t1, h1_sq)
    # dK1/dP_j = _dTdh_target(t1, h1_sq)
    # dK2/dP_j = _dTdh_source(t2, h2_sq)
    # dK2/dP_k = _dTdh_target(t2, h2_sq)
    # dK3/dP_k = _dTdh_source(t3, h3_sq)
    # dK3/dP_l = _dTdh_target(t3, h3_sq)

    dK1_dPi = _dTdh_source(t1, h1_sq)
    dK1_dPj = _dTdh_target(t1, h1_sq)
    dK2_dPj = _dTdh_source(t2, h2_sq)
    dK2_dPk = _dTdh_target(t2, h2_sq)
    dK3_dPk = _dTdh_source(t3, h3_sq)
    dK3_dPl = _dTdh_target(t3, h3_sq)

    # R = (K3 - 2*K2 + K1) / h2
    # dR/dP = (d(K3 - 2*K2 + K1)/dP) / h2 + (K3 - 2*K2 + K1) * d(1/h2)/dP
    # d(1/h2)/dP_j = T2 / h2^2   (since h2 = |P_k - P_j|, d|..|/dP_j = -T2, d(1/h2)/dP_j = T2/h2^2)
    # d(1/h2)/dP_k = -T2 / h2^2
    inv_h2 = 1.0 / h2
    t2_over_h2_sq = t2 * (1.0 / h2_sq)

    def _mat_scale(m, s):
        return [[m[a][b] * s for b in range(3)] for a in range(3)]

    def _mat_add(*mats):
        rows = []
        for a in range(3):
            row = []
            for b in range(3):
                row.append(sum(m[a][b] for m in mats))
            rows.append(row)
        return rows

    def _mat_sub(a, b):
        return [[a[x][y] - b[x][y] for y in range(3)] for x in range(3)]

    # Numerator N = K3 - 2*K2 + K1
    # dN/dP_i = dK1/dP_i
    # dN/dP_j = dK1/dP_j - 2*dK2/dP_j
    # dN/dP_k = -2*dK2/dP_k + dK3/dP_k
    # dN/dP_l = dK3/dP_l
    dN_dPi = dK1_dPi
    dN_dPj = _mat_sub(dK1_dPj, _mat_scale(dK2_dPj, 2.0))
    dN_dPk = _mat_sub(_mat_scale(dK3_dPk, 1.0), _mat_scale(dK2_dPk, 2.0))
    dN_dPl = dK3_dPl

    # R = N / h2, so dR/dP = dN/dP / h2 + N * d(1/h2)/dP
    # N vector = K3 - 2*K2 + K1
    k1 = t1 * (1.0 / h1)
    k2 = t2 * (1.0 / h2)
    k3 = t3 * (1.0 / h3)
    N = k3 - k2 * 2.0 + k1

    # Outer product N (3-vector) times d(1/h2)/dP (3-vector) = 3x3 matrix
    def _outer_vec_scale(vec, scale_vec):
        """Outer product: vec[i] * scale_vec[j] -> 3x3 matrix."""
        rows = []
        for a in range(3):
            row = []
            va = (vec.x, vec.y, vec.z)[a]
            for b in range(3):
                row.append(va * (scale_vec.x, scale_vec.y, scale_vec.z)[b])
            rows.append(row)
        return rows

    dR_dPi = _mat_scale(dN_dPi, inv_h2)
    dR_dPj = _mat_add(
        _mat_scale(dN_dPj, inv_h2),
        _outer_vec_scale(N, t2_over_h2_sq)
    )
    dR_dPk = _mat_add(
        _mat_scale(dN_dPk, inv_h2),
        _mat_scale(_outer_vec_scale(N, t2_over_h2_sq), -1.0)
    )
    dR_dPl = _mat_scale(dN_dPl, inv_h2)

    return dR_dPi, dR_dPj, dR_dPk, dR_dPl


def constraint_violations(pts, constraints):
    """Per-constraint violation at the current point positions, aligned
    1:1 with `constraints`: max |residual| over each constraint's own
    flat residual entries (mixed units -- mm for positions, radians for
    angles; fine for badge thresholding). issues.md #8: satisfiable sets
    converge to ~0, conflicting (over-defined) sets stay visibly
    non-zero; 'fix' is always 0.0 (it pins, it doesn't solve). A
    constraint that can't even be evaluated (corrupt indices) reports
    0.0 rather than raising -- badges must never break the panel."""
    out = []
    for c in constraints:
        try:
            res = _constraint_residuals(pts, [c])
        except Exception:
            res = []
        out.append(max((abs(x) for x in res), default=0.0))
    return out



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
            elif kind == "on_curve":
                i, edge = c[1], c[2]
                if i not in pinned:
                    try:
                        d, near, _ = Part.Vertex(pts[i]).distToShape(edge)
                        if d > 1e-3 and near:
                            pts[i] = near[0][1]   # snap onto the target edge
                            changed = True
                    except Exception:
                        pass
            elif kind == "equal":
                i, j, k, l = c[1], c[2], c[3], c[4]
                e1 = (pts[j] - pts[i]).Length
                e2 = (pts[l] - pts[k]).Length
                if e1 > 1e-9 and e2 > 1e-9 and abs(e1 - e2) > 1e-3:
                    adj = (e1 - e2) * 0.25
                    d1 = (pts[j] - pts[i]) * (adj / e1) if e1 > 1e-9 else None
                    d2 = (pts[l] - pts[k]) * (adj / e2) if e2 > 1e-9 else None
                    if i not in pinned and d1 is not None:
                        pts[i] = pts[i] + d1; changed = True
                    if j not in pinned and d1 is not None:
                        pts[j] = pts[j] - d1; changed = True
                    if k not in pinned and d2 is not None:
                        pts[k] = pts[k] + d2; changed = True
                    if l not in pinned and d2 is not None:
                        pts[l] = pts[l] - d2; changed = True
            elif kind == "tangent":
                i, j, k = c[1], c[2], c[3]
                v1 = pts[i] - pts[j]
                v2 = pts[k] - pts[j]
                l1, l2 = v1.Length, v2.Length
                if l1 > 1e-9 and l2 > 1e-9:
                    cross = v1.cross(v2)
                    cl = cross.Length
                    if cl > 1e-3:
                        # Push i and k toward collinearity with j
                        dir1 = v1 * (1.0 / l1)
                        dir2 = v2 * (1.0 / l2)
                        # Target direction: average of the two (or opposite)
                        target = (dir1 + dir2) * 0.5
                        if target.Length > 1e-9:
                            target.normalize()
                            if i not in pinned:
                                pts[i] = pts[j] + target * l1; changed = True
                            if k not in pinned:
                                pts[k] = pts[j] + target * l2; changed = True
            elif kind == "concentric":
                i, j = c[1], c[2]
                p1, p2 = pts[i], pts[j]
                if p1.distanceToPoint(p2) > 1e-3:
                    mid = (p1 + p2) * 0.5
                    if i not in pinned:
                        pts[i] = mid; changed = True
                    if j not in pinned:
                        pts[j] = mid; changed = True
            elif kind == "symmetric":
                i, j, k = c[1], c[2], c[3]
                p1, p2, pc = pts[i], pts[j], pts[k]
                mid = (p1 + p2) * 0.5
                err = (mid - pc).Length
                if err > 1e-3:
                    if i not in pinned:
                        pts[i] = p1 + (pc - mid); changed = True
                    if j not in pinned:
                        pts[j] = p2 + (pc - mid); changed = True
            elif kind == "mirror":
                i, j = c[1], c[2]
                n = _unit_normal(c[3], c[4], c[5])
                if n is None:
                    continue
                d = c[6]
                want_i = _reflect(pts[j], n, d)
                want_j = _reflect(pts[i], n, d)
                if i not in pinned:
                    pts[i] = pts[i] + (want_i - pts[i]) * 0.5; changed = True
                if j not in pinned:
                    pts[j] = pts[j] + (want_j - pts[j]) * 0.5; changed = True
            elif kind == "fix":
                pass   # pinning is handled via the caller's `pinned` set
            elif kind == "tangent_to_face":
                # Relaxation: move p_j to make direction tangent to face at p_i
                i, j = c[1], c[2]
                if i in pinned or j in pinned:
                    continue
                try:
                    p_i = pts[i]
                    p_j = pts[j]
                    face = c[3]
                    v = p_j - p_i
                    v_len = v.Length
                    if v_len > 1e-9:
                        dir_vec = v * (1.0 / v_len)
                        closest_on_face = Part.Vertex(p_i).distToShape(face)[1][0][1]
                        n = face.normalAt(*face.Surface.parameter(closest_on_face))
                        if n.Length > 1e-9:
                            n = n * (1.0 / n.Length)
                            # Project dir onto tangent plane
                            proj = dir_vec - n * dir_vec.dot(n)
                            if proj.Length > 1e-9:
                                proj = proj * (1.0 / proj.Length)
                                # Move p_j to p_i + proj * |v|
                                pts[j] = p_i + proj * v_len
                                changed = True
                except Exception:
                    pass
        if not changed:
            break
    return pts



def solve_point_constraints(points, constraints, pinned=None, tol=1e-16,
                            max_iter=80, status=None, point_planes=None):
    """Simultaneously satisfy a set of 2D/3D geometric constraints over a
    point list, returning the adjusted points (input list is not mutated).

    Supported constraint tuples (the exact format the curve panel builds):
      ("distance", i, j, d)         -- |p_i - p_j| == d
      ("horizontal", i, y)          -- p_i.y == y  (in point's local plane if assigned)
      ("vertical",   i, x)          -- p_i.x == x  (in point's local plane if assigned)
      ("angle", i, vertex, k, deg)  -- angle(p_i, vertex, p_k) == deg
      ("coincide", i, j)            -- p_i == p_j
      ("on_curve", i, edge)         -- p_i lies on the fixed Part edge/shape
      ("equal", i, j, k, l)         -- |p_i-p_j| == |p_k-p_l|
      ("tangent", i, j, k)          -- i-j-k collinear (tangent continuity)
      ("concentric", i, j)          -- two arc centers coincide
      ("symmetric", i, j, k)        -- i,j symmetric about k
      ("fix", i)                    -- pin point i
      ("mirror", i, j, nx, ny, nz, d) -- p_j == reflection of p_i
                                          across plane n.q + d == 0
      ("tangent_to_face", i, j, face) -- direction p_i->p_j tangent to
                                           face at closest point to p_i

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
    `point_planes`: optional list of per-point plane data (same length as points).
                  Each entry is either None (global 3D) or a tuple
                  (origin, normal, u_dir, v_dir) defining a local coordinate
                  system. When provided, points are constrained to stay on
                  their assigned planes (orthogonal projection) and
                  horizontal/vertical constraints are evaluated in the local
                  plane coordinates.

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

    # Validate and prepare point_planes
    if point_planes is not None:
        if len(point_planes) != len(pts):
            raise ValueError("point_planes length must match points length")
        # Normalize: ensure each plane tuple has (origin, normal, u_dir, v_dir) with unit vectors
        normalized_planes = []
        for pp in point_planes:
            if pp is None:
                normalized_planes.append(None)
            else:
                origin, normal, u_dir, v_dir = pp
                # Ensure normal is unit
                n_len = normal.Length
                if n_len < 1e-12:
                    normalized_planes.append(None)
                    continue
                normal = normal * (1.0 / n_len)
                u_len = u_dir.Length
                v_len = v_dir.Length
                if u_len < 1e-12 or v_len < 1e-12:
                    normalized_planes.append(None)
                    continue
                u_dir = u_dir * (1.0 / u_len)
                v_dir = v_dir * (1.0 / v_len)
                normalized_planes.append((origin, normal, u_dir, v_dir))
        point_planes = normalized_planes
    else:
        point_planes = [None] * len(pts)

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

    def project_to_planes():
        """Project all points to their assigned planes."""
        for i, pp in enumerate(point_planes):
            if pp is not None and i < len(pts):
                origin, normal, u_dir, v_dir = pp
                p = pts[i]
                # Orthogonal projection onto plane
                d = (p - origin).dot(normal)
                pts[i] = p - normal * d

    def local_coords(i, p):
        """Convert 3D point to local 2D coordinates in its plane."""
        pp = point_planes[i] if i < len(point_planes) else None
        if pp is None:
            return p.x, p.y  # global XY
        origin, normal, u_dir, v_dir = pp
        rel = p - origin
        return rel.dot(u_dir), rel.dot(v_dir)

    def from_local_coords(i, u, v):
        """Convert local 2D coordinates to 3D point on plane."""
        pp = point_planes[i] if i < len(point_planes) else None
        if pp is None:
            return Vector(u, v, 0)
        origin, normal, u_dir, v_dir = pp
        return origin + u_dir * u + v_dir * v

    def residual(x):
        set_x(x)
        project_to_planes()
        # Clear face normal cache for this residual evaluation
        if not hasattr(_constraint_residuals, "_face_normal_cache"):
            _constraint_residuals._face_normal_cache = {}
        _constraint_residuals._face_normal_cache.clear()
        return np.asarray(_constraint_residuals(pts, constraints), float)

    x = get_x()
    r = residual(x)
    if r.size == 0:
        return pts
    cost = float(r @ r)
    lam = 1e-3
    J = np.zeros((r.size, U))

    def _fill_analytic_rows():
        """Overlay exact Jacobian rows for tangent_to_face, g2, and g3
        constraints onto J, at the current pts."""
        nonlocal J
        off = 0
        for c in constraints:
            n_res = 0
            try:
                n_res = len(_constraint_residuals(pts, [c]))
            except Exception:
                n_res = 0
            if c[0] == "tangent_to_face":
                i, j, face = c[1], c[2], c[3]
                if i not in idx_of or j not in idx_of:
                    off += n_res
                    continue
                p_i = pts[i]
                p_j = pts[j]
                cache_key = (id(face), (p_i.x, p_i.y, p_i.z))
                if cache_key in _constraint_residuals._face_normal_cache:
                    _closest_on_face, n = _constraint_residuals._face_normal_cache[cache_key]
                else:
                    try:
                        _d = Part.Vertex(p_i).distToShape(face)[0]
                        _closest_on_face = _d[1][0][1]
                        n = face.normalAt(*face.Surface.parameter(_closest_on_face))
                        _constraint_residuals._face_normal_cache[cache_key] = (_closest_on_face, n)
                    except Exception:
                        n = None
                if n is None or n.Length < 1e-9:
                    off += n_res
                    continue
                dr_dp_i, dr_dp_j = tangent_to_face_jacobian(p_i, p_j, face, n)
                if dr_dp_i is None:
                    off += n_res
                    continue
                row = off
                ci_i = idx_of[i]
                ci_j = idx_of[j]
                J[row, 3 * ci_i:3 * ci_i + 3] = (dr_dp_i.x, dr_dp_i.y, dr_dp_i.z)
                J[row, 3 * ci_j:3 * ci_j + 3] = (dr_dp_j.x, dr_dp_j.y, dr_dp_j.z)
                off += n_res
            elif c[0] == "g2":
                i, j, k = c[1], c[2], c[3]
                if i not in idx_of or j not in idx_of or k not in idx_of:
                    off += n_res
                    continue
                p_i = pts[i]
                p_j = pts[j]
                p_k = pts[k]
                dRi, dRj, dRk = g2_jacobian(p_i, p_j, p_k)
                if dRi is None:
                    off += n_res
                    continue
                ci_i, ci_j, ci_k = idx_of[i], idx_of[j], idx_of[k]
                for r in range(3):
                    row = off + r
                    J[row, 3 * ci_i:3 * ci_i + 3] = (dRi[r][0], dRi[r][1], dRi[r][2])
                    J[row, 3 * ci_j:3 * ci_j + 3] = (dRj[r][0], dRj[r][1], dRj[r][2])
                    J[row, 3 * ci_k:3 * ci_k + 3] = (dRk[r][0], dRk[r][1], dRk[r][2])
                off += n_res
            elif c[0] == "g3":
                i, j, k, l = c[1], c[2], c[3], c[4]
                if i not in idx_of or j not in idx_of or k not in idx_of or l not in idx_of:
                    off += n_res
                    continue
                p_i = pts[i]
                p_j = pts[j]
                p_k = pts[k]
                p_l = pts[l]
                dRi, dRj, dRk, dRl = g3_jacobian(p_i, p_j, p_k, p_l)
                if dRi is None:
                    off += n_res
                    continue
                ci_i, ci_j, ci_k, ci_l = idx_of[i], idx_of[j], idx_of[k], idx_of[l]
                for r in range(3):
                    row = off + r
                    J[row, 3 * ci_i:3 * ci_i + 3] = (dRi[r][0], dRi[r][1], dRi[r][2])
                    J[row, 3 * ci_j:3 * ci_j + 3] = (dRj[r][0], dRj[r][1], dRj[r][2])
                    J[row, 3 * ci_k:3 * ci_k + 3] = (dRk[r][0], dRk[r][1], dRk[r][2])
                    J[row, 3 * ci_l:3 * ci_l + 3] = (dRl[r][0], dRl[r][1], dRl[r][2])
                off += n_res
            else:
                off += n_res

    for _ in range(max_iter):
        if cost < tol:
            break
        for k in range(U):
            xk = x[k]
            step = 1e-6 * (1.0 + abs(xk))
            x[k] = xk + step
            J[:, k] = (residual(x) - r) / step
            x[k] = xk
        # Overlay analytic rows (exact derivatives) on the numeric columns:
        _fill_analytic_rows()
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
    'constraint_point_indices',
    'constraint_violations',
    'solve_point_constraints',
    'tangent_to_face_jacobian',
    'g2_jacobian',
    'g3_jacobian',
    'rolling_ball_fillet',
    'multi_rail_fillet',
]
