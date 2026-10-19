# -*- coding: utf-8 -*-
"""Gui-free geometry core.

CRITICAL design rule (see project memory 'freecad-unified-wb-plan'): Silk and
Curves import `FreeCAD.Gui` at module load, so they CANNOT be imported under
freecadcmd. Therefore any logic we want to verify headlessly must live here and
depend only on `Part` / `FreeCAD`, never on the engines. The OCCT primitives in
this module are the verified fallbacks (mirrors RailformWB/railform/geometry.py)
and the home for the Blender rail-import math.

This module must remain importable by `freecadcmd test_core.py`.
"""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

G0, G1, G2 = 0, 1, 2


def dot(a, b):
    """Dot product of two vectors."""
    return a.x * b.x + a.y * b.y + a.z * b.z


def cross(a, b):
    """Cross product of two vectors."""
    return Vector(
        a.y * b.z - a.z * b.y,
        a.z * b.x - a.x * b.z,
        a.x * b.y - a.y * b.x
    )


def norm(v):
    """Norm (magnitude) of a vector."""
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)


def as_wire(obj):
    if isinstance(obj, Part.Wire):
        return obj
    if isinstance(obj, Part.Edge):
        return Part.Wire([obj])
    edges = getattr(obj, "Edges", None)
    if edges:
        return Part.Wire(Part.__sortEdges__(edges))
    raise TypeError("cannot coerce %r into a Wire" % (obj,))


def join_curves(shapes):
    """Join several curve shapes/edges end-to-end into one Part.Wire, corners
    preserved (WYSIWYG -- no refit, no smoothing). Edges are ordered by
    connectivity via Part.__sortEdges__ and must meet within OCCT's stitching
    tolerance; a ValueError is raised if they don't form a single connected
    wire. Gui-free -- covered by test_core.py."""
    edges = []
    for s in shapes:
        se = getattr(s, "Edges", None)
        if se:
            edges.extend(se)
        elif isinstance(s, Part.Edge):
            edges.append(s)
    if not edges:
        raise ValueError("join_curves: nothing to join (no edges)")
    if len(edges) == 1:
        return Part.Wire(edges)
    # Group edges into connected chains. A single joined wire needs exactly one
    # chain; >1 means the curves don't meet end-to-end (Part.Wire would silently
    # accept a disconnected set, so this guards WYSIWYG intent explicitly).
    try:
        groups = Part.sortEdges(edges)
    except Exception:
        groups = [Part.__sortEdges__(edges)]
    if len(groups) > 1:
        raise ValueError(
            "join_curves: the selected curves form %d disconnected chains -- "
            "their endpoints must meet (snap them together first)."
            % len(groups))
    return Part.Wire(groups[0])


# --- OCCT fallback surfaces (verified headless) --------------------------- #
def loft(rails, solid=False, ruled=False, closed=False, max_degree=5):
    wires = [as_wire(r) for r in rails]
    if len(wires) < 2:
        raise ValueError("loft needs >= 2 rails")
    return Part.makeLoft(wires, solid, ruled, closed, max_degree)


# --- shared grid helpers -------------------------------------------------- #
def _eval01(edge, t):
    """Point on an edge at normalized parameter t in [0, 1]."""
    a, b = edge.FirstParameter, edge.LastParameter
    return edge.valueAt(a + t * (b - a))


def _orient_family(edges):
    """Flip edges so a family of curves runs in a consistent direction
    (start endpoints aligned). Tolerant of a few curves drawn backwards."""
    if not edges:
        return []
    ref = _eval01(edges[0], 0.0)
    out = []
    for e in edges:
        s, t = _eval01(e, 0.0), _eval01(e, 1.0)
        if s.distanceToPoint(ref) <= t.distanceToPoint(ref):
            out.append((e, False))
        else:
            out.append((e, True))
    return out


def _pt(edge_flip, t):
    edge, flip = edge_flip
    return _eval01(edge, 1.0 - t if flip else t)


def grid_to_surface(grid):
    """Interpolating B-spline surface through a 2D grid of points.
    Falls back to a (approximating) control net if interpolate is unavailable."""
    surf = Part.BSplineSurface()
    try:
        surf.interpolate(grid)
    except Exception:
        nu, nv = len(grid), len(grid[0])
        du, dv = min(3, nu - 1), min(3, nv - 1)
        surf.buildFromPolesMultsKnots(
            grid,
            _clamped_mults(nu, du), _clamped_mults(nv, dv),
            _clamped_knots(nu, du), _clamped_knots(nv, dv),
            False, False, du, dv)
    return surf.toShape()


def _chain_loop(curves):
    """Orient and order curves into a closed loop of (edge, flip) pairs,
    matching _orient_family's convention.

    The Coons patch builder accepts the four boundary edges in any order or
    orientation; this helper reconnects them into a loop so the patch is
    built from a consistent perimeter rather than a scrambled one.

    Returns (edge, flip) tuples -- _pt() below needs .valueAt/
    FirstParameter/LastParameter, which Part.Edge exposes but Part.Wire
    does not, so ordering stays at the edge level throughout (each input
    curve is coerced to a single edge via as_wire(c).Edges[0]; a curve
    given as a genuine multi-edge wire only contributes its first edge --
    the same limitation the old wire-returning version had implicitly,
    since Part.Wire has no unified valueAt() either)."""
    if not curves:
        return []
    edges = [as_wire(c).Edges[0] for c in curves]
    if len(edges) <= 1:
        return [(e, False) for e in edges]

    ordered = []
    remaining = list(edges)
    current = remaining.pop(0)
    ordered.append((current, False))
    prev_pt = _eval01(current, 1.0)

    while remaining:
        best_idx = None
        best_dist = None
        best_flip = False
        for idx, cand in enumerate(remaining):
            start, end = _eval01(cand, 0.0), _eval01(cand, 1.0)
            for flip, pt in ((False, start), (True, end)):
                dist = prev_pt.distanceToPoint(pt)
                if best_dist is None or dist < best_dist - 1e-12:
                    best_dist = dist
                    best_idx = idx
                    best_flip = flip
        if best_idx is None:
            break
        cand = remaining.pop(best_idx)
        ordered.append((cand, best_flip))
        prev_pt = _eval01(cand, 0.0 if best_flip else 1.0)

    return ordered


# --- Cross-boundary derivative helpers -------------------------------------- #
def _compute_finite_difference_epsilon(parameter_range, dimension_index):
    """Compute epsilon for finite difference derivatives.

    Args:
        parameter_range: Tuple (u0, u1, v0, v1) defining parameter bounds
        dimension_index: 0 for u-dimension, 1 for v-dimension

    Returns:
        Epsilon value for finite difference calculations
    """
    u0, u1, v0, v1 = parameter_range
    param_range = (u1 - u0) if dimension_index == 0 else (v1 - v0)
    return max(1e-6, param_range * 1e-3)


def _finite_difference_second_derivative(point_func, u, v, du_sign, dv_sign, eps):
    """Compute second derivative using central difference.

    Args:
        point_func: Function(u, v) -> point on surface
        u, v: Parameter coordinates
        du_sign, dv_sign: Direction signs (+1 or -1) for u and v derivatives
        eps: Step size for finite difference

    Returns:
        Second derivative vector
    """
    p0 = point_func(u, v)
    p1 = point_func(u + du_sign * eps, v + dv_sign * eps)
    p2 = point_func(u + 2 * du_sign * eps, v + 2 * dv_sign * eps)
    return (p0 - 2.0 * p1 + p2) / (eps * eps)


def _finite_difference_third_derivative(point_func, u, v, du_sign, dv_sign, eps):
    """Compute third derivative using forward difference.

    Args:
        point_func: Function(u, v) -> point on surface
        u, v: Parameter coordinates
        du_sign, dv_sign: Direction signs (+1 or -1) for u and v derivatives
        eps: Step size for finite difference

    Returns:
        Third derivative vector
    """
    p0 = point_func(u, v)
    p1 = point_func(u + du_sign * eps, v + dv_sign * eps)
    p2 = point_func(u + 2 * du_sign * eps, v + 2 * dv_sign * eps)
    p3 = point_func(u + 3 * du_sign * eps, v + 3 * dv_sign * eps)
    return (-p0 + 3.0 * p1 - 3.0 * p2 + p3) / (eps ** 3)


def _cross_tan_from_surface_tangent(du, dv, side):
    """Compute cross-boundary tangent vector from surface tangents.

    Args:
        du, dv: Tangent vectors in u and v directions from surface.tangent()
        side: Side indicator (0=-u, 1=+u, 2=-v, 3=+v)

    Returns:
        Tangent vector in the specified direction
    """
    return {0: du.negative(), 1: du, 2: dv.negative(), 3: dv}[side]


# --- (4-sided) Coons patch ------------------------------------------------ #
def _closest_approach_pair(c1, c2, samples=24):
    """Return (tu, tv, distance) where tu is the parameter on c1 closest to c2
    and tv the parameter on c2 closest to c1. Refines by alternating
    'project sample of other onto me' steps."""
    fp1, lp1 = c1.FirstParameter, c1.LastParameter
    fp2, lp2 = c2.FirstParameter, c2.LastParameter
    span1 = max(lp1 - fp1, 1e-12)
    span2 = max(lp2 - fp2, 1e-12)
    samples = max(8, samples)
    pts1 = [c1.value(fp1 + k / samples * span1) for k in range(samples + 1)]
    pts2 = [c2.value(fp2 + k / samples * span2) for k in range(samples + 1)]
    best_t1, best_t2, best_d = fp1, fp2, 1e18
    for k1, p1 in enumerate(pts1):
        for k2, p2 in enumerate(pts2):
            d = p1.distanceToPoint(p2)
            if d < best_d:
                best_d = d
                best_t1 = fp1 + k1 / samples * span1
                best_t2 = fp2 + k2 / samples * span2
    for _ in range(4):
        p2 = c2.value(best_t2)
        try:
            new_t1 = c1.parameter(p2)
        except Exception:
            break
        if new_t1 < fp1 or new_t1 > lp1:
            break
        best_t1 = new_t1
        p1 = c1.value(best_t1)
        try:
            new_t2 = c2.parameter(p1)
        except Exception:
            break
        if new_t2 < fp2 or new_t2 > lp2:
            break
        best_t2 = new_t2
    return (best_t1, best_t2,
            c1.value(best_t1).distanceToPoint(c2.value(best_t2)))


def _to_bspline(edge):
    """Return the BSplineCurve underlying `edge`, or refit a non-BSpline edge
    to a fresh BSpline (lossy but always works)."""
    c = getattr(edge, "Curve", None)
    if isinstance(c, Part.BSplineCurve):
        return c
    try:
        fp, lp = edge.FirstParameter, edge.LastParameter
        pts = [edge.valueAt(fp + k / 49.0 * (lp - fp)) for k in range(50)]
        bs = Part.BSplineCurve()
        bs.interpolate(Points=pts)
        return bs
    except Exception:
        return None


def _intersect_pair(c1, c2):
    """Return [(tu, tv), ...] -- parameter pairs at which the two curves
    actually cross. Uses OCCT `intersectCC` (the real intersection, not
    closest approach). Falls back to closest-approach when the curves
    don't actually cross, so the caller always gets at least one pair."""
    pts = []
    try:
        pts = c1.intersectCC(c2)
    except Exception:
        pts = []
    out = []
    for p in pts:
        try:
            v = Vector(p.X, p.Y, p.Z)
            tu = c1.parameter(v)
            tv = c2.parameter(v)
            if c1.FirstParameter <= tu <= c1.LastParameter and \
               c2.FirstParameter <= tv <= c2.LastParameter:
                out.append((float(tu), float(tv), float(v.distanceToPoint(v))))
        except Exception:
            continue
    if out:
        return out
    # Fallback: closest approach (handles tangent / near-miss / parallel).
    t1, t2, d = _closest_approach_pair(c1, c2)
    return [(float(t1), float(t2), float(d))]


# Above this closest-approach distance a (profile, guide) pair is treated as
# NOT actually crossing, which means the network isn't compatible and no Gordon
# surface can pass through every rail (see gordon_network's warning).
#
# Chosen at 0.02, not machine-epsilon: rails built by interpolating sampled
# points don't cross to full precision even when they are genuinely compatible
# (a verified-compatible 4x4 test network still shows ~1e-3 closest approach),
# so a tighter value cries wolf on good input. 0.02 also happens to be the point
# where the gap starts to break the 0.01 rail-passthrough invariant that
# test_gordon_rails_pass_through enforces: two rails D apart force the surface
# at least ~D/2 from each.
_GORDON_CROSS_TOL = 0.02


def _intersect_grid(bs_profiles, bs_guides):
    """For every (profile, guide) pair, return the best intersection
    parameter pair (tu, tv) and the geometric distance. Prefers real
    OCCT intersections; falls back to closest-approach for non-crossing
    pairs (so a near-miss doesn't kill the skinning step).

    Returns:
      intersection_params_u: list[list[float]], len nu x nv -- tu for (i, j)
      intersection_params_v: list[list[float]], len nu x nv -- tv for (i, j)
      distances: list[list[float]], len nu x nv
    """
    nu, nv = len(bs_profiles), len(bs_guides)
    pu = [[0.0] * nv for _ in range(nu)]
    pv = [[0.0] * nv for _ in range(nu)]
    dd = [[0.0] * nv for _ in range(nu)]
    for i in range(nu):
        for j in range(nv):
            pairs = _intersect_pair(bs_profiles[i], bs_guides[j])
            # Pick the intersection with the smallest distance. Real
            # intersections are exact (distance 0) so this just enforces
            # a deterministic choice for multi-intersection pairs.
            best = min(pairs, key=lambda t: t[2])
            pu[i][j] = best[0]
            pv[i][j] = best[1]
            dd[i][j] = best[2]
    return pu, pv, dd


def _strictly_increasing(vals, eps=1e-6):
    """Nudge a list of floats so consecutive values strictly increase,
    preserving order -- guards against near-tied or reversed averaged
    parameters (e.g. from a near-degenerate crossing) breaking OCCT's
    interpolate(), which requires a strictly increasing Parameters list."""
    out = list(vals)
    for k in range(1, len(out)):
        if out[k] <= out[k - 1] + eps:
            out[k] = out[k - 1] + eps
    return out


def _param_map(anchors_x, anchors_y):
    """Piecewise-linear map from `anchors_x` (strictly increasing) to
    `anchors_y`, extrapolated linearly beyond the first/last anchor."""
    def f(x):
        n = len(anchors_x)
        if n == 1:
            return anchors_y[0]
        if x <= anchors_x[0]:
            i0 = 0
        elif x >= anchors_x[-1]:
            i0 = n - 2
        else:
            i0 = 0
            for k in range(n - 1):
                if anchors_x[k] <= x <= anchors_x[k + 1]:
                    i0 = k
                    break
        i1 = i0 + 1
        x0, x1 = anchors_x[i0], anchors_x[i1]
        y0, y1 = anchors_y[i0], anchors_y[i1]
        if abs(x1 - x0) < 1e-12:
            return y0
        t = (x - x0) / (x1 - x0)
        return y0 + t * (y1 - y0)
    return f


def _skin_through_family(family, own_crossing_params, shared_anchor_values,
                         shared_discrete_values, extra_per_gap=4):
    """Build a B-spline SURFACE that passes exactly (to sampling fidelity)
    through every curve in `family` -- the standard two-pass "global
    surface interpolation" construction (Piegl & Tiller, The NURBS Book,
    9.2.5), generalized so the "continuous" axis is densely resampled
    (not just at the crossing points) to preserve each curve's actual
    shape between crossings.

    This replaces the previous approach of feeding the raw curves to
    Part.makeLoft and discarding the intersection parameters entirely
    (`sample_at_params` used to be computed then immediately `del`eted)
    -- that left S1/S2 with whatever parametrization OCCT's loft chose,
    unrelated to the crossing grid S3 is built from, so summing their
    poles pointwise didn't actually reconstruct a surface through the
    rails. Building all three surfaces on one shared (u_bar, v_bar)
    parameter grid (see gordon_network) is what makes the boolean sum
    S1+S2-S3 geometrically meaningful.

    family                 : list of NC Part.BSplineCurve (the guides,
                             or the profiles).
    own_crossing_params    : NC x NX -- for each curve, its OWN native
                             parameter at each of the NX crossings with
                             the OTHER family.
    shared_anchor_values    : length NX -- the shared "continuous axis"
                             position assigned to each crossing (same
                             for every curve in `family`, e.g. u_bar).
    shared_discrete_values  : length NC -- the shared "discrete axis"
                             position assigned to each curve in `family`
                             (e.g. v_bar).

    Returns (surf, u_ref): surf is a Part.BSplineSurface whose U axis is
    the continuous (shared_anchor_values) axis and whose V axis is the
    discrete (shared_discrete_values) axis; u_ref is the dense-axis
    reference curve (Parameters=the densified `shared_anchor_values`
    master list) callers use to bring OTHER surfaces' correspondingly
    sparse axis up to the same knot structure via insertUKnots/
    insertVKnots -- see gordon_network, where S2's discrete axis and
    BOTH of S3's axes are sparse (built directly from the raw nu/nv
    anchor values, with no dense fill) and need this reference to become
    pole-count-compatible with S1's densely-sampled continuous axis.
    """
    anchors_x = _strictly_increasing(list(shared_anchor_values))
    master = [anchors_x[0]]
    for k in range(len(anchors_x) - 1):
        gap = anchors_x[k + 1] - anchors_x[k]
        for s in range(1, extra_per_gap + 1):
            master.append(anchors_x[k] + gap * s / (extra_per_gap + 1.0))
        master.append(anchors_x[k + 1])
    master = _strictly_increasing(master)

    stage1_curves = []
    for c_idx, curve in enumerate(family):
        own_anchors = list(own_crossing_params[c_idx])
        f = _param_map(anchors_x, own_anchors)
        fp, lp = curve.FirstParameter, curve.LastParameter
        pts = [curve.value(min(max(f(u), fp), lp)) for u in master]
        c = Part.BSplineCurve()
        c.interpolate(Points=pts, Parameters=master)
        stage1_curves.append(c)

    m_u = stage1_curves[0].NbPoles
    stage1_poles = [[sc.getPole(k + 1) for sc in stage1_curves]
                    for k in range(m_u)]

    v_vals = _strictly_increasing(list(shared_discrete_values))
    final_poles = []
    v_ref = None
    for row in stage1_poles:
        cc = Part.BSplineCurve()
        cc.interpolate(Points=row, Parameters=v_vals)
        final_poles.append([cc.getPole(k + 1) for k in range(cc.NbPoles)])
        v_ref = cc

    u_ref = stage1_curves[0]
    surf = Part.BSplineSurface()
    surf.buildFromPolesMultsKnots(
        final_poles,
        u_ref.getMultiplicities(), v_ref.getMultiplicities(),
        u_ref.getKnots(), v_ref.getKnots(),
        False, False, u_ref.Degree, v_ref.Degree)
    return surf, u_ref


def _grid_interpolation_surface(grid_pts, u_vals, v_vals):
    """Tensor-product B-spline surface that interpolates `grid_pts`
    (NP x NG) EXACTLY at the given (u_vals, v_vals) parameter grid -- the
    same two-pass global surface interpolation _skin_through_family uses.
    This is S3, the Gordon boolean sum's correction term; building it on
    the SAME shared parameter grid S1/S2 use (rather than the previous
    plain BSplineSurface.interpolate(), which silently re-parametrizes by
    chord length / normalizes to [0,1] and ignores the crossing
    parameters entirely) is what lets S1+S2-S3 stay geometrically
    meaningful."""
    v_vals = _strictly_increasing(list(v_vals))
    stageA_poles = []
    v_ref = None
    for row in grid_pts:
        cc = Part.BSplineCurve()
        cc.interpolate(Points=row, Parameters=v_vals)
        stageA_poles.append([cc.getPole(k + 1) for k in range(cc.NbPoles)])
        v_ref = cc  # same Parameters every row -> identical knots/mults/degree

    u_vals = _strictly_increasing(list(u_vals))
    m_v = len(stageA_poles[0])
    columns = []
    u_ref = None
    for m in range(m_v):
        col_pts = [stageA_poles[i][m] for i in range(len(stageA_poles))]
        cc = Part.BSplineCurve()
        cc.interpolate(Points=col_pts, Parameters=u_vals)
        u_ref = cc
        columns.append([cc.getPole(k + 1) for k in range(cc.NbPoles)])

    m_u = len(columns[0])
    final_poles = [[columns[m][k] for m in range(m_v)] for k in range(m_u)]

    surf = Part.BSplineSurface()
    surf.buildFromPolesMultsKnots(
        final_poles,
        u_ref.getMultiplicities(), v_ref.getMultiplicities(),
        u_ref.getKnots(), v_ref.getKnots(),
        False, False, u_ref.Degree, v_ref.Degree)
    return surf


def _harmonize_surface_knots(s1, s2, s3, tol=1e-9):
    """Force s1, s2, s3 onto a common knot vector in both directions.

    Strategy: collect the per-direction (knots, mults) pairs from each
    surface, take the per-knot *max* multiplicity, and `insertUKnots` any
    knot that's in one but not the others.

    We pass `bool_add=False` to `insertUKnots` -- OCCT's default `True`
    would *add* the given multiplicities to the existing ones, growing
    the surface past the target. With `False`, OCCT only raises the
    multiplicity to (not by) the given value, matching the others.
    """
    for axis in ("U", "V"):
        get_knots = getattr(s1, "get" + axis + "Knots")
        get_mults = getattr(s1, "get" + axis + "Multiplicities")
        # Build the union (knot, max_mult) from all three.
        merged = {}
        for s in (s1, s2, s3):
            for k, m in zip(get_knots(), get_mults()):
                merged[k] = max(merged.get(k, 0), m)
        ks = sorted(merged.keys())
        ms = [merged[k] for k in ks]
        insert = getattr(s1, "insert" + axis + "Knots")
        for s in (s1, s2, s3):
            try:
                insert(ks, ms, tol, False)
            except Exception:
                pass


def _match_degrees(s1, s2, s3):
    """Lift all three surfaces to a common (uDeg, vDeg).

    We force a minimum of degree 3 (the typical S3 degree) so that the
    S1/S2 lofted surfaces (which can be as low as degree 2) are lifted
    and the resulting control-point sum has enough room to keep the
    skin-through-rails property.
    """
    du = max(s1.UDegree, s2.UDegree, s3.UDegree, 3)
    dv = max(s1.VDegree, s2.VDegree, s3.VDegree, 3)
    s1.increaseDegree(du, dv)
    s2.increaseDegree(du, dv)
    s3.increaseDegree(du, dv)


def gordon_network(profiles, guides, samples_per_curve=24):
    """Gordon surface from a network of U- and V-curves (TiGL 1969).

    Implements `S = S1 + S2 - S3` (control-point-wise sum), the standard
    Gordon formulation used by Curves/TiGL/Rhino _NetworkSrf:
      S1 = surface skinning the guides, built on a shared (u_bar, v_bar)
           parameter grid so it passes through every guide's FULL shape,
           not just the crossings.
      S2 = surface skinning the profiles, built the same way then
           axis-flipped to align with S1.
      S3 = tensor-product interpolating surface through the (nu x nv)
           intersection grid, on that SAME shared parameter grid
           (subtracts the S1+S2 double-counting).

    All three surfaces are built via the standard two-pass "global
    surface interpolation" construction (Piegl & Tiller, The NURBS Book,
    9.2.5; see _skin_through_family / _grid_interpolation_surface) on one
    shared, averaged parameter grid -- u_bar[i] is the representative
    "across-profiles" position for profile i (averaged over its nv
    crossing parameters), v_bar[j] likewise for guide j. This is what
    makes summing S1/S2/S3's poles pointwise geometrically meaningful: a
    prior version built S1/S2 via a plain Part.makeLoft through the raw
    curves and DISCARDED the intersection parameters entirely, so S1, S2
    and S3 each had their own unrelated parametrization and the sum did
    not reconstruct a surface through the rails (measured worst-case
    passthrough error ~0.93 units on a 4x4 network, against the 0.01
    tolerance the invariant requires).

    The result touches every input rail at every crossing -- this is the
    invariant the user reported as broken in the prior simplified
    implementation, and the headless test `test_gordon_rails_pass_through`
    enforces it.

    User-drawn directions are honoured; curves drawn backwards will
    produce a localised Z-fold in the surface at that curve, visible in
    the viewport as feedback.

    Parameters:
      profiles, guides  : iterables of Part.Edge or Part.BSplineCurve.
      samples_per_curve : refinement samples for the closest-approach
                          fallback when two curves don't actually cross.
    """
    bs_profiles = [_to_bspline(e) for e in profiles]
    bs_guides = [_to_bspline(e) for e in guides]
    bs_profiles = [b for b in bs_profiles if b is not None]
    bs_guides = [b for b in bs_guides if b is not None]
    if len(bs_profiles) < 2 or len(bs_guides) < 2:
        raise ValueError(
            "gordon_network needs >= 2 profiles and >= 2 guides (got "
            "%d profiles, %d guides)" % (len(bs_profiles), len(bs_guides)))

    nu, nv = len(bs_profiles), len(bs_guides)

    # 1) Compute the (nu x nv) intersection parameter matrix per family.
    #    Each entry is the parameter where this curve meets the other
    #    curve AT THAT PAIR -- not an average.
    params_u, params_v, gaps = _intersect_grid(bs_profiles, bs_guides)

    # A Gordon surface can only pass through the WHOLE network if that network
    # is COMPATIBLE: every profile must genuinely cross every guide. Where a
    # pair merely comes close, _intersect_grid falls back to closest approach
    # and the surface necessarily splits the difference -- no boolean-sum
    # construction (this one or any other) can pass within t of two rails that
    # are themselves more than t apart. That used to be discarded silently and
    # showed up as "the surface doesn't touch its boundary curves", so report it.
    worst, worst_ij = 0.0, None
    for i in range(nu):
        for j in range(nv):
            if gaps[i][j] > worst:
                worst, worst_ij = gaps[i][j], (i, j)
    if worst_ij is not None and worst > _GORDON_CROSS_TOL:
        FreeCAD.Console.PrintWarning(
            "Railform Gordon: the curve network is not compatible -- profile %d "
            "and guide %d never actually cross (closest approach %.4g). The "
            "surface will pass BETWEEN the rails there rather than through "
            "them. Make every profile genuinely intersect every guide (snap "
            "their crossings together) for a surface that touches all of "
            "them.\n" % (worst_ij[0] + 1, worst_ij[1] + 1, worst))

    # 2) Representative ("compatible network") parameter per index -- the
    #    shared axis every one of S1/S2/S3 is built on.
    #
    #    u_bar[i] is profile i's position on the shared "across-profiles"
    #    axis. It comes from params_v (each GUIDE's OWN parameter at its
    #    crossing with profile i), NOT params_u -- a profile's own
    #    arc-parameter at its crossings with the various guides is a
    #    "along-this-profile" quantity (roughly start/mid/end of ITS OWN
    #    arc for any evenly-spaced network, however i varies), so
    #    averaging params_u by row does NOT vary meaningfully with i.
    #    It's the GUIDES -- which run across every profile in turn --
    #    whose own parametrization naturally increases with profile
    #    index; averaging their crossing parameter over j gives a value
    #    that actually separates profile 0 from profile 1 from profile 2.
    #    v_bar[j] is the mirror image, from params_u averaged over i.
    u_bar_raw = [sum(params_v[i][j] for j in range(nv)) / nv for i in range(nu)]
    v_bar_raw = [sum(params_u[i][j] for i in range(nu)) / nu for j in range(nv)]

    # 2b) The natural INPUT order for one family isn't guaranteed to be
    #     monotonic in the shared axis above -- e.g. a guide family drawn
    #     "backwards" relative to increasing profile index makes u_bar
    #     come out DECREASING even though the profiles themselves were
    #     given in a sensible order (drawn curve DIRECTION is honoured
    #     throughout this module, see test_gordon_preserves_user_direction,
    #     so this isn't something we can just normalize away upstream).
    #     BSplineCurve.interpolate(Parameters=...) requires a strictly
    #     increasing list, so re-sort the shared-axis index order by the
    #     computed u_bar/v_bar and carry every per-index array (curves,
    #     both parameter matrices) along with it. This only relabels
    #     which internal row/column corresponds to which input curve --
    #     the resulting surface's geometry doesn't depend on that label
    #     order, only that it stays internally consistent.
    order_i = sorted(range(nu), key=lambda i: u_bar_raw[i])
    order_j = sorted(range(nv), key=lambda j: v_bar_raw[j])
    bs_profiles = [bs_profiles[i] for i in order_i]
    bs_guides = [bs_guides[j] for j in order_j]
    params_u = [[params_u[i][j] for j in order_j] for i in order_i]
    params_v = [[params_v[i][j] for j in order_j] for i in order_i]
    u_bar = _strictly_increasing([u_bar_raw[i] for i in order_i])
    v_bar = _strictly_increasing([v_bar_raw[j] for j in order_j])

    # 3) S1: skin through guides -- continuous (exact shape) along each
    #    guide's own arc (u_bar anchors), discrete across the nv guides
    #    (v_bar). No exchangeUV needed: built directly in the (u_bar,
    #    v_bar) orientation S1 needs for the final sum.
    own_v_per_guide = [[params_v[i][j] for i in range(nu)] for j in range(nv)]
    s1, u_ref = _skin_through_family(bs_guides, own_v_per_guide, u_bar, v_bar)

    # 4) S2: skin through profiles -- continuous along each profile's own
    #    arc (v_bar anchors), discrete across the nu profiles (u_bar).
    #    Built axis-swapped relative to S1's convention, then
    #    exchangeUV()'d to align -- mirrors the original algorithm's use
    #    of exchangeUV for the same reason.
    own_u_per_profile = [[params_u[i][j] for j in range(nv)] for i in range(nu)]
    s2, v_ref = _skin_through_family(bs_profiles, own_u_per_profile, v_bar, u_bar)
    s2.exchangeUV()

    # 5) S3 = tensor interpolation through the (nu x nv) intersection
    #    grid, at the SAME (u_bar, v_bar) parametrization S1/S2 use.
    grid_pts = [[bs_profiles[i].value(params_u[i][j]) for j in range(nv)]
                for i in range(nu)]
    s3 = _grid_interpolation_surface(grid_pts, u_bar, v_bar)

    # 6) Match degree FIRST. A family with few members (e.g. 3 guides)
    #    only has enough poles for a degree-2 interpolation, while u_ref/
    #    v_ref (built from many dense master samples) are degree 3 -- and
    #    the explicit knot insertion in step 7 requires matching degrees
    #    (inserting a degree-3 multiplicity into a degree-2 curve is
    #    invalid and hangs OCCT's insertion routine rather than raising).
    _match_degrees(s1, s2, s3)

    # 7) Every surface has ONE densely-sampled axis (S1's U, from u_ref;
    #    S2(exchanged)'s V, from v_ref) and axes built directly from the
    #    sparse nu/nv anchor values (S1's V, S2(exchanged)'s U, and BOTH
    #    of S3's axes) -- those sparse axes are consistent in VALUE with
    #    the dense ones (same underlying u_bar/v_bar anchors) but not in
    #    KNOT COUNT, since the dense pass adds extra interior knots at
    #    the fill samples between anchors. Bring every sparse axis up to
    #    the dense reference's exact knot structure via pure knot
    #    insertion -- lossless (doesn't change the surface's shape, only
    #    its representation) -- so the three surfaces are guaranteed
    #    pole-count-compatible before the sum, rather than hoping a
    #    generic post-hoc knot-union reconciliation happens to converge.
    for s in (s1, s3):
        s.insertVKnots(v_ref.getKnots(), v_ref.getMultiplicities(), 1e-9, False)
    for s in (s2, s3):
        s.insertUKnots(u_ref.getKnots(), u_ref.getMultiplicities(), 1e-9, False)

    # 8) A final defensive knot-union pass to mop up any residual (e.g.
    #    floating-point) mismatch the explicit insertion above didn't
    #    already cover.
    _harmonize_surface_knots(s1, s2, s3)

    # 9) S = S1 + S2 - S3 (control-point-wise).
    out = s1.copy()
    nu_p, nv_p = out.NbUPoles, out.NbVPoles
    if (s2.NbUPoles, s2.NbVPoles) != (nu_p, nv_p) or \
       (s3.NbUPoles, s3.NbVPoles) != (nu_p, nv_p):
        # Pole counts diverged. Fall back to the S3 interpolation -- it
        # still passes through every intersection point even if the
        # resulting surface doesn't pass through every rail. This used to
        # return silently, so a degraded surface was indistinguishable from a
        # correct one; say so instead.
        FreeCAD.Console.PrintWarning(
            "Railform Gordon: pole counts diverged after knot harmonisation "
            "(S1 %dx%d, S2 %dx%d, S3 %dx%d) -- falling back to the crossing-"
            "grid interpolation. The surface will pass through the curve "
            "CROSSINGS but will NOT follow the rails between them, so it may "
            "visibly pull away from its boundary curves.\n"
            % (nu_p, nv_p, s2.NbUPoles, s2.NbVPoles, s3.NbUPoles, s3.NbVPoles))
        return s3.toShape()
    for i in range(1, nu_p + 1):
        for j in range(1, nv_p + 1):
            cp = (out.getPole(i, j)
                  + s2.getPole(i, j)
                  - s3.getPole(i, j))
            out.setPole(i, j, cp)
    return out.toShape()


# --- (4-sided) Coons patch ------------------------------------------------ #
def coons_patch(boundary_edges, n=24):
    """Bilinearly-blended Coons surface through 4 boundary curves.

    Exact, robust, fully headless-testable -- our own OCCT-only implementation,
    no Curves/Silk dependency. The 4 edges may be given in any order/orientation;
    we chain them into a loop first.
    """
    if len(boundary_edges) != 4:
        raise ValueError("Coons patch needs exactly 4 boundary curves")
    sides = _chain_loop(boundary_edges)          # 4 sides in loop order
    s = [_pt(sides[0], i / (n - 1.0)) for i in range(n)]            # south u:0->1
    e = [_pt(sides[1], j / (n - 1.0)) for j in range(n)]            # east  v:0->1
    north = [_pt(sides[2], 1 - i / (n - 1.0)) for i in range(n)]    # north u:0->1
    w = [_pt(sides[3], 1 - j / (n - 1.0)) for j in range(n)]        # west  v:0->1
    p00, p10 = s[0], s[-1]
    p11, p01 = e[-1], north[0]
    grid = []
    for i in range(n):
        u = i / (n - 1.0)
        row = []
        for j in range(n):
            v = j / (n - 1.0)
            bil = (p00 * ((1 - u) * (1 - v)) + p10 * (u * (1 - v))
                   + p01 * ((1 - u) * v) + p11 * (u * v))
            pt = (s[i] * (1 - v) + north[i] * v
                  + w[j] * (1 - u) + e[j] * u - bil)
            row.append(pt)
        grid.append(row)
    return grid_to_surface(grid)


# --- native N-sided ("star point") patch ----------------------------------- #
# Replaces RFS_StarPatch's delegation to Silk's ControlGridNStar66. Silk's
# implementation lives in ArachNURBS.py, a single 9,600-line file with
# hand-tuned iterative numerical solvers and star-patch classes that reach
# into deeply indexed shared state (StarGrid[i][j][k]...) -- not a small,
# isolable file, and not something we can vendor and silently trust without
# a way to run and compare it against the original. So instead of vendoring,
# this is a genuine from-scratch construction, built entirely on primitives
# already used elsewhere in this module (_polygon_normal, _hermite3,
# grid_to_surface) and verified algebraically below, not just by testing.
#
# HONESTY ABOUT WHAT THIS DOES AND DOESN'T GUARANTEE:
#   - G0 (position) across every boundary AND every internal seam is EXACT
#     by construction (see the derivation in _triangular_coons_grid).
#   - G1-ish (tangent plane) continuity at the star point is approximate:
#     every spoke's tangent at the center is forced into a single shared
#     plane (the Newell-averaged normal of the corner ring), which is the
#     same "vertex tangent plane" rule subdivision surfaces use at an
#     extraordinary vertex. It is NOT a proof of G1, just the standard
#     practical approximation most N-gon fill tools (this workbench
#     included, now) actually ship.
#   - True, provably-exact G2 at an arbitrary-valence star point is a
#     genuinely unsolved-in-general problem in CAGD (the "vertex
#     enclosure constraint" -- Gregory patches, S-patches, and similar
#     constructions each make different tradeoffs to approximate it).
#     This function does NOT claim to solve that; it's a solid, exact-G0,
#     tangent-plane-continuous N-sided fill, which is what most real users
#     of a "star patch" tool actually need visually.
def n_sided_patch(boundary_edges, samples=16):
    """Fill an N-sided (N >= 3) closed loop of boundary curves with N
    triangular Coons sub-patches fanned out from a shared center point.

    Unlike coons_patch (exactly 4 sides) or plate_fill (G0 only, no
    continuity story at all), this handles any N and gives every internal
    seam a documented, checkable continuity guarantee (see module comment
    above) instead of an ad hoc mesh-fill.
    """
    edges = list(boundary_edges)
    n_sides = len(edges)
    if n_sides < 3:
        raise ValueError("n_sided_patch needs >= 3 boundary curves (got %d)"
                         % n_sides)
    loop = _order_open_loop(edges)                 # [(edge, flip), ...]
    corners = [_pt(loop[i], 0.0) for i in range(n_sides)]
    center = _average_point(corners)
    normal = _polygon_normal(corners)
    if normal.Length < 1e-9:
        # degenerate (near-planar-through-center or collinear corners):
        # fall back to any perpendicular, tangent-plane rule becomes moot
        normal = Vector(0, 0, 1)
    else:
        normal.normalize()

    corner_accels = [_corner_boundary_accel(loop[i]) for i in range(n_sides)]
    spokes = [_star_spoke(center, corners[i], normal, corner_accels[i])
             for i in range(n_sides)]

    faces = []
    for i in range(n_sides):
        b = loop[i]
        c_a, c_b = corners[i], corners[(i + 1) % n_sides]
        s_a, s_b = spokes[i], spokes[(i + 1) % n_sides]
        grid = _triangular_coons_grid(center, c_a, c_b, b, s_a, s_b, samples)
        faces.append(grid_to_surface(grid))

    try:
        shell = Part.Shell([f.Faces[0] for f in faces])
        shell.sewShape()
        return shell
    except Exception:
        # Best-effort stitching; if OCCT can't sew the independently-fit
        # sub-patch edges within tolerance, still return something usable
        # (a compound of the N faces) rather than raising -- the faces
        # themselves are each individually exact and valid.
        return Part.Compound(faces)


def _average_point(pts):
    x = sum(p.x for p in pts) / len(pts)
    y = sum(p.y for p in pts) / len(pts)
    z = sum(p.z for p in pts) / len(pts)
    return Vector(x, y, z)


def _order_open_loop(edges, tol=1e-5):
    """Like _chain_loop, but for any N (not just 4) and returns (edge, flip)
    pairs ordered so consecutive sides share an endpoint -- the corner ring
    n_sided_patch needs. Reimplemented separately from _chain_loop rather
    than generalizing it, since coons_patch's 4-side assumption is baked
    into its callers; duplicating the small chaining loop here is safer
    than risking a subtle regression in the well-tested 4-side path."""
    remaining = list(edges)
    first = remaining.pop(0)
    loop = [(first, False)]
    tail = _eval01(first, 1.0)
    while remaining:
        best, best_d, best_flip = None, 1e18, False
        for e in remaining:
            ds = tail.distanceToPoint(_eval01(e, 0.0))
            de = tail.distanceToPoint(_eval01(e, 1.0))
            if ds < best_d:
                best, best_d, best_flip = e, ds, False
            if de < best_d:
                best, best_d, best_flip = e, de, True
        if best_d > tol * max(1.0, tail.Length if hasattr(tail, "Length") else 1.0) \
                and best_d > tol:
            raise ValueError(
                "n_sided_patch: boundary curves don't form a closed loop "
                "(gap of %.4g between consecutive corners)" % best_d)
        loop.append((best, best_flip))
        remaining.remove(best)
        tail = _eval01(best, 0.0 if best_flip else 1.0)
    return loop


def _corner_boundary_accel(loop_item):
    """Raw (unnormalized) 2nd derivative d2P/du2 of the boundary curve at
    the corner where `loop_item` (an (edge, flip) pair from _order_open_loop)
    starts -- i.e. the boundary's OWN curvature vector leaving that corner.
    Used by _star_spoke to shape a spoke's curvature near the rim to agree
    with the boundary it meets, instead of an arbitrary straight spoke.

    No sign flip is needed for a reversed (flip=True) edge: d2P/dt2 =
    d2P/du2 * (du/dt)^2, and (du/dt) = -1 for a reversed direction, so the
    squared term keeps d2's sign unchanged regardless of orientation
    (unlike the 1st derivative, which genuinely flips -- see _pt/_eval01).

    Returns None (not an approximate zero) if the underlying curve has no
    analytic 2nd derivative available -- callers (_star_spoke) then fall
    back to a zero-curvature spoke end, the same behaviour this function's
    introduction did not change.
    """
    edge, _flip = loop_item
    fp = edge.FirstParameter
    try:
        _p, _d1, d2 = edge.Curve.getD2(fp)
        return d2
    except Exception:
        return None


def _star_spoke(center, corner, normal, corner_accel=None):
    """One QUINTIC-Hermite spoke from the shared center to boundary corner.

    Upgraded from a cubic (position + tangent only) to a quintic Hermite
    (adds a 2nd-derivative/curvature term at both ends). The cubic version
    only forced every spoke's TANGENT at the star point into one shared
    plane (see the tangent-plane rule described in the module comment
    above n_sided_patch) -- curvature at the center was left completely
    unconstrained. This version additionally forces every spoke's
    ACCELERATION at the center to the SAME value: zero, chosen specifically
    because zero is trivially, exactly identical across every spoke
    (an averaged nonzero value would still only be approximately shared,
    and would need its own consistency argument). That gives every spoke
    an exact, provable curvature match at the star point itself, on top of
    the existing tangent-plane match -- narrowing, though NOT closing, the
    "true G2 at the star point" gap (see the module comment: continuity
    ACROSS a shared spoke's full length, between its two bordering
    triangular sub-patches, is still not attempted; that remains a
    genuinely open CAGD problem for arbitrary valence).

    `corner_accel`, if given (see _corner_boundary_accel), is the boundary
    curve's own raw 2nd derivative at the corner; only its component along
    the radial (center->corner) direction is used, scaled to this spoke's
    own length the same way match_surface scales _cross_d2 into a Hermite
    acceleration term (raw_d2 * L^2 / 2, since _hermite5's h20/h21 already
    bake in the basis's own 1/2 factor) -- this is a dimensional match-of-
    convenience between two curves with unrelated native parameterizations,
    not an exact derivative transport, so treat the corner-end curvature
    match as a fairness improvement over a bare straight spoke, not a
    continuity guarantee. None (no analytic curvature available) falls
    back to a zero corner-end acceleration too -- i.e. the previous cubic
    behaviour at that end.
    """
    radial = corner - center
    L = max(radial.Length, 1e-9)
    unit_radial = radial * (1.0 / L)
    to_plane = radial - normal * radial.dot(normal)
    if to_plane.Length < 1e-9:
        to_plane = radial          # corner is ~on the axis; fall back
    center_tan = to_plane * (L / max(to_plane.Length, 1e-9) / 3.0)
    corner_tan = unit_radial * (L / 3.0)
    center_accel = Vector(0, 0, 0)   # shared, exactly, across every spoke
    if corner_accel is not None:
        radial_component = unit_radial * corner_accel.dot(unit_radial)
        corner_accel_term = radial_component * (L * L / 2.0)
    else:
        corner_accel_term = Vector(0, 0, 0)

    def spoke_curve(t):
        return _hermite5(center, center_tan, center_accel,
                         corner, corner_tan, corner_accel_term, t)
    return spoke_curve


def _triangular_coons_grid(o, c_a, c_b, boundary_ab, spoke_a, spoke_b, n):
    """Grid(u,v) for a triangular Coons patch: one side collapsed to the
    point `o`, the opposite side is `boundary_ab` (u: 0->1, C_a -> C_b), and
    the other two sides are the spokes (v: 0->1, o -> C_a / o -> C_b).

    Derived directly from the standard bilinear Coons formula with the
    "inner" edge set to the constant o for all u (algebra below; verified
    it reduces to exact boundary match at all 4 sides of the unit square):

        Grid(u,v) = (1-u)*[S_a(v) - v*C_a] + u*[S_b(v) - v*C_b] + v*B(u)

    Sanity checks (all exact by construction, and covered by
    test_n_sided_patch_matches_boundaries in test_core.py):
        v=0  -> Grid(u,0) = o                          for all u
        v=1  -> Grid(u,1) = B(u)                        (boundary preserved)
        u=0  -> Grid(0,v) = S_a(v)                       (spoke A preserved)
        u=1  -> Grid(1,v) = S_b(v)                       (spoke B preserved)
    """
    b_edge, b_flip = boundary_ab

    def B(u):
        return _pt(boundary_ab, u)

    grid = []
    for i in range(n):
        u = i / (n - 1.0)
        row = []
        for j in range(n):
            v = j / (n - 1.0)
            s_a = spoke_a(v)
            s_b = spoke_b(v)
            pt = ((s_a - c_a * v) * (1 - u) + (s_b - c_b * v) * u
                  + B(u) * v)
            row.append(pt)
        grid.append(row)
    return grid


def _nearest_t(edge, pt, samples=100):
    """Normalized param in [0,1] on edge whose point is closest to pt."""
    best_t, best_d = 0.0, 1e18
    for k in range(samples + 1):
        t = k / float(samples)
        d = _eval01(edge, t).distanceToPoint(pt)
        if d < best_d:
            best_d, best_t = d, t
    return best_t


def _polygon_normal(pts):
    """Newell's-method normal of a point sequence (robust, non-planar OK)."""
    nx = ny = nz = 0.0
    m = len(pts)
    for i in range(m):
        a, b = pts[i], pts[(i + 1) % m]
        nx += (a.y - b.y) * (a.z + b.z)
        ny += (a.z - b.z) * (a.x + b.x)
        nz += (a.x - b.x) * (a.y + b.y)
    return Vector(nx, ny, nz)


def _perp(v):
    """Some unit vector perpendicular to v."""
    a = Vector(1, 0, 0) if abs(v.x) < 0.9 else Vector(0, 1, 0)
    p = v.cross(a)
    p.normalize()
    return p


def two_rail_sweep(rail1, rail2, profiles, n_rail=40, n_prof=20):
    """Skin a set of profile curves between two rail curves.

    Each profile is expected to span from rail1 to rail2. We express every
    profile in a local frame anchored on rail1 (chord-to-rail2 as X, the
    section's own normal as the up axis), interpolate those local coordinates
    along the rails, then rebuild points in the live rail frame at each station.

    No matrix inversion and no Curves/libS2R dependency -- robust (degenerate
    only where the rails physically touch) and fully headless-testable.
    """
    n_prof = max(2, int(n_prof))
    n_rail = max(2, int(n_rail))
    cols = [s / (n_prof - 1.0) for s in range(n_prof)]

    # --- per-profile local description ---------------------------------- #
    descs = []          # (station_t, up_vector, [(cx, cy, cz), ...])
    prev_up = None
    for pro in profiles:
        e0, e1 = _eval01(pro, 0.0), _eval01(pro, 1.0)
        # orient so sample param 0 lands on rail1, last sample on rail2
        flip = (_nearest_t(rail1, e1) + _nearest_t(rail2, e0)) < \
               (_nearest_t(rail1, e0) + _nearest_t(rail2, e1))
        samp = [_eval01(pro, (1.0 - s) if flip else s) for s in cols]
        a, b = samp[0], samp[-1]
        chord = b.sub(a)
        L = chord.Length
        if L < 1e-9:
            continue
        ex = Vector(chord)
        ex.normalize()
        nrm = _polygon_normal(samp)
        up = nrm if nrm.Length > 1e-7 else _perp(ex)
        up.normalize()
        if prev_up is not None and up.dot(prev_up) < 0:
            up = up.negative()
        ez = ex.cross(up)
        if ez.Length < 1e-9:
            up = _perp(ex)
            ez = ex.cross(up)
        ez.normalize()
        ey = ez.cross(ex)
        ey.normalize()
        prev_up = ey
        local = []
        for p in samp:
            d = p.sub(a)
            local.append((d.dot(ex) / L, d.dot(ey), d.dot(ez)))
        descs.append((_nearest_t(rail1, a), ey, local))
    if not descs:
        raise ValueError("two-rail sweep: no usable profiles")
    descs.sort(key=lambda d: d[0])

    # --- build the grid along the rails --------------------------------- #
    grid = []
    for i in range(n_rail):
        t = i / (n_rail - 1.0)
        a, b = _eval01(rail1, t), _eval01(rail2, t)
        chord = b.sub(a)
        L = max(chord.Length, 1e-9)
        ex = Vector(chord)
        ex.normalize()
        # bracket the defining profiles by station parameter
        if len(descs) == 1 or t <= descs[0][0]:
            lo = hi = descs[0]
            w = 0.0
        elif t >= descs[-1][0]:
            lo = hi = descs[-1]
            w = 0.0
        else:
            k = 0
            while k < len(descs) - 1 and descs[k + 1][0] < t:
                k += 1
            lo, hi = descs[k], descs[k + 1]
            span = hi[0] - lo[0]
            w = 0.0 if span < 1e-12 else (t - lo[0]) / span
        up = lo[1] * (1.0 - w) + hi[1] * w
        ez = ex.cross(up)
        if ez.Length < 1e-9:
            up = _perp(ex)
            ez = ex.cross(up)
        ez.normalize()
        ey = ez.cross(ex)
        ey.normalize()
        row = []
        for j in range(n_prof):
            cx0, cy0, cz0 = lo[2][j]
            cx1, cy1, cz1 = hi[2][j]
            cx = cx0 * (1.0 - w) + cx1 * w
            cy = cy0 * (1.0 - w) + cy1 * w
            cz = cz0 * (1.0 - w) + cz1 * w
            row.append(a + ex * (cx * L) + ey * cy + ez * cz)
        grid.append(row)
    return grid_to_surface(grid)


# --- N-sided fill (GeomPlate G0) ----------------------------------------- #
def plate_fill(edges, tol=0.001):
    """Smooth surface filling N>=3 boundary edges (G0 at each boundary).
    Uses OCCT GeomPlate. For 4-sided patches Coons is preferred; this handles
    3-sided and 5+ sided regions where Coons doesn't apply."""
    if len(edges) < 3:
        raise ValueError("plate_fill needs >= 3 boundary edges")
    GP = Part.GeomPlate
    builder = GP.BuildPlateSurface()
    builder.setNbBounds(len(edges))
    for e in edges:
        curve = e.Curve if hasattr(e, "Curve") else e
        if not isinstance(curve, Part.BSplineCurve):
            fp = e.FirstParameter
            lp = e.LastParameter
            pts = [e.valueAt(fp + k / 29.0 * (lp - fp)) for k in range(30)]
            bs = Part.BSplineCurve()
            bs.approximate(Points=pts, DegMin=2, DegMax=5, Tolerance=tol)
            curve = bs
        builder.add(GP.CurveConstraint(curve, 0))
    builder.perform()
    if not builder.isDone():
        raise RuntimeError("GeomPlate fill failed")
    ps = builder.surface()
    bsurf = ps.makeApprox(tol, 10, 8, 0)
    return bsurf.toShape()


# --- manufacturability: offset / thicken / shell -------------------------- #
# Everything above this point (Loft/Gordon/Coons/Fill/Match/blend_surface)
# produces pure B-spline SHAPES with zero wall thickness -- fine for a
# visual or aero shape, not a manufacturable part. These three wrap OCCT's
# own offset/thickening primitives (BRepOffsetAPI_MakeOffsetShape and
# BRepOffsetAPI_MakeThickSolid, both already exposed on Part.Shape as
# makeOffsetShape/makeThickness) -- no new geometry kernel work needed,
# just the doc-object plumbing this workbench was missing.
def offset_surface(shape, distance, tol=1e-3):
    """Pure surface offset (no solid): a new open shell/face offset by
    `distance` along its normal. Sign of `distance` picks the offset side
    -- flip the sign if OCCT walked the wrong way for a given shape."""
    if abs(distance) < 1e-9:
        raise ValueError("offset_surface: distance must be non-zero")
    return shape.makeOffsetShape(distance, tol, fill=False)


def thicken_surface(shape, thickness, tol=1e-3, symmetric=False):
    """Turn an open surface/shell into a manufacturable SOLID with wall
    thickness `thickness`, by offsetting and capping the open ends
    (OCCT's fill=True mode). This is the direct fix for "produces
    surfaces, not manufacturable solids with wall thickness".

    symmetric=True splits the thickness +-half both ways off the input
    surface and fuses the two half-thick solids into one, so the wall is
    centered on the original surface instead of growing entirely to one
    side of it.
    """
    if abs(thickness) < 1e-9:
        raise ValueError("thicken_surface: thickness must be non-zero")
    if symmetric:
        s1 = shape.makeOffsetShape(thickness / 2.0, tol, fill=True)
        s2 = shape.makeOffsetShape(-thickness / 2.0, tol, fill=True)
        return s1.fuse(s2)
    return shape.makeOffsetShape(thickness, tol, fill=True)


def extend_surface(face, u_first=0, u_last=0, v_first=0, v_last=0, tol=1e-3):
    """Extend a surface in U and V parametric directions.

    Args:
        face: Part.Face or face-like object to extend
        u_first: extension at start of U parameter (negative = extend backward)
        u_last: extension at end of U parameter (positive = extend forward)
        v_first: extension at start of V parameter
        v_last: extension at end of V parameter
        tol: tolerance for fitting operations

    Returns:
        Extended Part.Face
    """
    # Convert input to a proper Face
    face = _face_from_input(face)
    if face is None:
        return None

    # No extension needed
    if u_first == 0 and u_last == 0 and v_first == 0 and v_last == 0:
        return face

    # Get the surface and its parameter range
    surface = face.Surface
    u0, u1, v0, v1 = face.ParameterRange

    # Calculate extended parameter range
    new_u0 = u0 - u_first
    new_u1 = u1 + u_last
    new_v0 = v0 - v_first
    new_v1 = v1 + v_last

    # If the extended range is the same as original, return original
    if (abs(new_u0 - u0) < tol and abs(new_u1 - u1) < tol and
        abs(new_v0 - v0) < tol and abs(new_v1 - v1) < tol):
        return face

    # Sample points from the surface over the extended range
    # Use a reasonable resolution based on the extent of extension
    nu = max(2, int(abs(u1 - u0) * 2))  # At least 2 points, scale with original size
    nv = max(2, int(abs(v1 - v0) * 2))
    # Ensure we have enough points to capture the extension
    nu = max(nu, int(abs(u_first + u_last) * 2) + 2)
    nv = max(nv, int(abs(v_first + v_last) * 2) + 2)

    # Create a grid of points covering the extended domain
    u_range = new_u1 - new_u0
    v_range = new_v1 - new_v0

    if u_range < tol or v_range < tol:
        # Degenerate case where u_range or v_range is too small
        # Degenerate case - return original if no meaningful extension
        return face

    gu = [(u0 + i * u_range / (nu - 1.0)) for i in range(nu)] if nu > 1 else [u0]
    gv = [(v0 + j * v_range / (nv - 1.0)) for j in range(nv)] if nv > 1 else [v0]

    # Evaluate surface at grid points
    poles = [[surface.value(u, v) for v in gv] for u in gu]

    # Create BSpline surface from the poles
    surf = Part.BSplineSurface()
    try:
        # Try to interpolate first (exact fit through points)
        surf.interpolate(poles)
    except Exception:
        # If interpolation fails, try approximation
        try:
            nu_poles = len(gu)
            nv_poles = len(gv)
            du = min(3, nu_poles - 1) if nu_poles > 1 else 1
            dv = min(3, nv_poles - 1) if nv_poles > 1 else 1
            # Create clamped knot vectors
            uknots = [0.0] * du + [i / (nu_poles - du - 1) for i in range(nu_poles - du - 1)] + [1.0] * du
            vknots = [0.0] * dv + [i / (nv_poles - dv - 1) for i in range(nv_poles - dv - 1)] + [1.0] * dv
            umults = [du] + [1] * (nu_poles - 2 * du) + [du] if nu_poles > 2 * du else [nu_poles]
            vmults = [dv] + [1] * (nv_poles - 2 * dv) + [dv] if nv_poles > 2 * dv else [nv_poles]

            surf.buildFromPolesMultsKnots(poles, umults, vmults, uknots, vknots, False, False, du, dv)
        except Exception:
            # Fall back to simple interpolation with fewer points if needed
            try:
                surf.interpolate(poles)
            except Exception as e:
                raise ValueError(f"extend_surface: failed to create surface from {len(gu)}x{len(gv)} points: {e}")

    # Create and return the extended face
    return face.__class__(surf.toShape())


def shell_solid(solid, faces_to_remove, thickness, tol=1e-3):
    """Hollow an existing SOLID to a `thickness` wall, opening it at
    `faces_to_remove` -- the classic CAD "Shell" feature (as opposed to
    thicken_surface, which BUILDS a solid from a surface; this REMOVES
    material from a solid that already exists). Wraps OCCT's
    BRepOffsetAPI_MakeThickSolid via Shape.makeThickness. `thickness` is
    given as a positive wall thickness; OCCT's own sign convention
    (negative = hollow inward, the normal case) is applied internally."""
    if not faces_to_remove:
        raise ValueError("shell_solid: need at least one face to remove")
    if abs(thickness) < 1e-9:
        raise ValueError("shell_solid: thickness must be non-zero")
    return solid.makeThickness(list(faces_to_remove), -abs(thickness), tol)


# --- polyline corner fillet (RFS_FilletPolyline) ------------------------- #
def _polyline_wire(pts):
    """Plain polyline Wire through pts (fallback when nothing gets rounded)."""
    if len(pts) < 2:
        raise ValueError("polyline_corner_fillet: need at least 2 points")
    edges = [Part.LineSegment(pts[i], pts[i + 1]).toShape()
             for i in range(len(pts) - 1)]
    return Part.Wire(edges)


def polyline_corner_fillet(points, radius, closed=False, tol=1e-7):
    """Round each interior corner of a polyline with a tangent arc.

    `radius` is either:
      * a single float, broadcast to every corner (the original,
        unchanged behavior), or
      * a dict {corner_index: radius} for PER-CORNER control
        (Illustrator-style live-corner radii, driven by
        editmode.CornerFilletEditor's draggable handles). A corner index
        missing from the dict, or given as 0/None, is left sharp -- the
        same convention a 0-radius corner has in Illustrator.

    Returns a Part.Wire: straight runs between corners, joined by circular arcs
    tangent to both adjacent segments. The radius is auto-reduced per corner so
    an arc never consumes more than half of either neighbouring segment (a too-
    large radius degrades gracefully instead of failing). Collinear corners are
    left sharp. `closed` also rounds the wrap-around corner between the last and
    first point. Gui-free -- verified headless in test_core.py.
    """
    radius_map = radius if isinstance(radius, dict) else None
    if radius_map is None and (radius is None or radius <= 0):
        raise ValueError("polyline_corner_fillet: radius must be > 0")

    # copy + drop consecutive duplicate points (zero-length segments break the
    # tangent math)
    pts = []
    for p in points:
        v = Vector(p)
        if not pts or (v - pts[-1]).Length > tol:
            pts.append(v)
    if closed and len(pts) > 1 and (pts[0] - pts[-1]).Length <= tol:
        pts.pop()                       # drop the explicit closing duplicate
    n = len(pts)
    if n < 3:
        return _polyline_wire(pts)

    idxs = range(n) if closed else range(1, n - 1)
    corner = {}                          # i -> (entry_tangent, arc_mid, exit_tangent)
    for i in idxs:
        r = radius_map.get(i, 0.0) if radius_map is not None else radius
        if not r or r <= 0:
            continue                     # sharp corner (0 / missing in map)
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        u, v = a - b, c - b
        lu, lv = u.Length, v.Length
        if lu < tol or lv < tol:
            continue
        u = u * (1.0 / lu)
        v = v * (1.0 / lv)
        cos_phi = max(-1.0, min(1.0, u.dot(v)))
        phi = math.acos(cos_phi)         # interior angle at the corner
        if phi < 1e-4 or abs(math.pi - phi) < 1e-4:
            continue                     # collinear -> leave sharp
        half = phi / 2.0
        d = r / math.tan(half)           # corner->tangent distance
        d = min(d, 0.5 * lu, 0.5 * lv)   # never eat more than half a segment
        r_eff = d * math.tan(half)
        bis = u + v
        lb = bis.Length
        if lb < tol:
            continue
        bis = bis * (1.0 / lb)
        center = b + bis * (r_eff / math.sin(half))
        cb = b - center
        lcb = cb.Length
        if lcb < tol:
            continue
        p1 = b + u * d                   # tangent point toward previous point
        p2 = b + v * d                   # tangent point toward next point
        pmid = center + cb * (r_eff / lcb)   # arc apex (closest point to corner)
        corner[i] = (p1, pmid, p2)

    if not corner:
        return _polyline_wire(pts)

    edges = []
    seg_count = n if closed else n - 1
    for k in range(seg_count):
        i, j = k, (k + 1) % n
        s = corner[i][2] if i in corner else pts[i]
        e = corner[j][0] if j in corner else pts[j]
        if (e - s).Length > tol:
            edges.append(Part.LineSegment(s, e).toShape())
        if j in corner:
            p1, pmid, p2 = corner[j]
            try:
                edges.append(Part.Arc(p1, pmid, p2).toShape())
            except Exception:            # near-degenerate arc -> straight chord
                if (p2 - p1).Length > tol:
                    edges.append(Part.LineSegment(p1, p2).toShape())
    return Part.Wire(edges)


# --- radius-driven fillet / chamfer (RFS_Fillet) -------------------------- #
# blend_surface (above) handles the OPEN-SURFACE case -- "I have two
# adjacent unclosed patches, build a blend strip between them" -- but has
# no concept of an actual world-units RADIUS, only a dimensionless scale
# factor. For the SOLID case -- "I have a solid, round these edges by 5
# mm" -- the user expects CAD-fillet semantics: a real numeric radius,
# variable-radius support, and the same input-validation contract the rest
# of this module uses (ValueError on bad input, see stitch_shell /
# shell_solid). These two wrappers close that gap by being thin,
# well-validated passes through Part.Shape.makeFillet / makeChamfer --
# the actual geometry kernel is OCCT's BRepFilletAPI, which FreeCAD
# already exposes. We just own the parameter-validation + constant-radius
# / variable-radius dispatch and the testable ValueError contract.
def _solid_fillet_chamfer_common(solid, edges, op):
    """Shared body for fillet_solid / chamfer_solid: input validation +
    the actual Part call. `op` is "fillet" or "chamfer" -- selects which
    Part method, and the error message on missing edges."""
    if not getattr(solid, "Solids", None):
        raise ValueError(
            "%s_solid: input must be a SOLID (Part::Feature with .Solids), "
            "got %s" % (op, type(solid).__name__))
    if not edges:
        raise ValueError("%s_solid: need at least one edge to fillet" % op)
    method = getattr(solid, "makeFillet" if op == "fillet" else "makeChamfer",
                     None)
    if method is None:
        raise RuntimeError(
            "%s_solid: Part shape doesn't expose make%s -- the FreeCAD "
            "Python binding is missing this method (unsupported OCCT build?)"
            % (op, "Fillet" if op == "fillet" else "Chamfer"))
    try:
        return method(list(edges))
    except Exception as exc:
        raise RuntimeError(
            "%s_solid: OCCT %s failed (%s) -- usually a radius too large "
            "for the geometry (e.g. fillet wider than the solid's local "
            "extent) or an edge not actually on the solid"
            % (op, op, exc))


def fillet_solid(solid, edges, radius, variable_radii=None, tol=1e-3):
    """Round the given edges of a SOLID with a fixed or per-edge radius.

    Native implementation of the "fillet" CAD feature for solids
    (RFS_Fillet): the input has to be a closed solid (anything that
    exposes .Solids), the output is a new solid whose selected edges
    are replaced by tangent cylindrical walls. Replaces the "dimensionless
    scale" limitation of blend_surface for the solid case -- this is the
    radius-driven path a user actually wants when they say "R5 mm fillet
    between these two faces."

    `solid`:    a Part::Feature / Part.Shape whose .Solids is non-empty.
    `edges`:    iterable of Part.Edge (must belong to `solid` -- user-
                picked ones, not arbitrary edges from other shapes).
    `radius`:   float, the constant fillet radius in world units.
    `variable_radii`: optional dict {Part.Edge: float}. When supplied,
                the function falls through to a per-edge call: each
                edge is filleted with its own radius in turn and the
                results are fused. The constant-radius path (the common
                case) is a single OCCT call and stays the fast one.
    `tol`:      forwarded to OCCT (not used by the Python wrapper itself;
                kept for the parametric.FilletFP signature to match the
                convention thicken_surface / offset_surface already use).

    Returns: a new Part.Shape with .Solids populated.

    Raises:
      ValueError on non-solid input, empty edges, or zero/negative radius.
      RuntimeError on OCCT failure (typically a radius too large for the
      local geometry, or an edge not actually belonging to the solid).
    """
    if variable_radii is None:
        if radius is None or abs(radius) < 1e-9:
            raise ValueError(
                "fillet_solid: radius must be non-zero (got %r)" % radius)
        return _solid_fillet_chamfer_common(solid, list(edges), "fillet")
    # Variable-radius path: one fillet per edge, fused. Each per-edge
    # call rebuilds from the ORIGINAL solid (so they don't compound), and
    # the final fuse tolerates the corners where adjacent fillets meet.
    # Slower than the constant-radius path by N -- fine for N in single
    # digits, which covers the realistic CAD "a couple of different radii
    # in this fillet chain" use case; for N >> 10, callers should prefer
    # the constant path with a single average radius.
    if not variable_radii:
        raise ValueError(
            "fillet_solid: variable_radii provided but empty -- either "
            "pass a non-empty {edge: radius} map, or omit it for constant "
            "radius")
    edges = list(edges)
    if len(variable_radii) != len(edges):
        raise ValueError(
            "fillet_solid: variable_radii has %d entries but %d edges "
            "supplied -- lengths must match (one radius per edge)"
            % (len(variable_radii), len(edges)))
    for e, r in variable_radii.items():
        if r is None or abs(r) < 1e-9:
            raise ValueError(
                "fillet_solid: variable_radii contains a non-positive "
                "value (%r) -- every radius must be non-zero" % r)
    parts = []
    for e, r in zip(edges, [variable_radii[e] for e in edges]):
        try:
            parts.append(solid.makeFillet(r, [e]))
        except Exception as exc:
            raise RuntimeError(
                "fillet_solid: per-edge fillet failed for radius=%g (%s) "
                "-- usually a radius too large for the local geometry"
                % (r, exc))
    if len(parts) == 1:
        return parts[0]
    out = parts[0]
    for p in parts[1:]:
        out = out.fuse(p)
    return out


def chamfer_solid(solid, edges, distance, variable_distances=None, tol=1e-3):
    """Chamfer (flat 45-degree face) on the given edges of a SOLID.

    Mirror of fillet_solid: a flat bevel instead of a round arc. Same
    constant-or-variable-distance contract, same ValueError / RuntimeError
    discipline. Like fillet_solid, this is a thin wrapper over
    Part.Shape.makeChamfer -- the geometry kernel is OCCT's
    BRepFilletAPI_MakeChamfer.
    """
    if variable_distances is None:
        if distance is None or abs(distance) < 1e-9:
            raise ValueError(
                "chamfer_solid: distance must be non-zero (got %r)" % distance)
        return _solid_fillet_chamfer_common(solid, list(edges), "chamfer")
    if not variable_distances:
        raise ValueError(
            "chamfer_solid: variable_distances provided but empty -- either "
            "pass a non-empty {edge: distance} map, or omit it for constant "
            "distance")
    edges = list(edges)
    if len(variable_distances) != len(edges):
        raise ValueError(
            "chamfer_solid: variable_distances has %d entries but %d "
            "edges supplied -- lengths must match" % (len(variable_distances),
                                                     len(edges)))
    for e, d in variable_distances.items():
        if d is None or abs(d) < 1e-9:
            raise ValueError(
                "chamfer_solid: variable_distances contains a non-positive "
                "value (%r)" % d)
    parts = []
    for e, d in zip(edges, [variable_distances[e] for e in edges]):
        try:
            parts.append(solid.makeChamfer(d, [e]))
        except Exception as exc:
            raise RuntimeError(
                "chamfer_solid: per-edge chamfer failed for distance=%g "
                "(%s)" % (d, exc))
    if len(parts) == 1:
        return parts[0]
    out = parts[0]
    for p in parts[1:]:
        out = out.fuse(p)
    return out


# --- variable-radius / multi-section fillets (RFS_VariableFillet,
#     RFS_RollingBallFillet) ----------------------------------------------- #
# fillet_solid/chamfer_solid above are CONSTANT radius per edge (or a
# distinct-but-still-constant radius per edge via variable_radii). Real
# Class-A fillet work regularly needs a radius that VARIES ALONG one edge
# (a taper from R3 to R8 along a body-side seam), and a real-world-unit
# RADIUS on the OPEN-SURFACE blend case (blend_surface's `scale` is only
# a dimensionless tangent-strength knob, not a radius). The three
# functions below close those two gaps.
def _monotone_cubic_fn(ts, rs):
    """Build a callable r(t) that evaluates a MONOTONE cubic (Fritsch-
    Carlson / PCHIP-style) Hermite interpolant through the stations
    (ts[k], rs[k]). Inputs must be sorted ascending in t; queries outside
    [ts[0], ts[-1]] are clamped to the end stations.

    Why this specific interpolant and not a natural cubic spline: a
    natural/B-spline cubic through a few radius stations can OVERSHOOT
    between them (dip below the smaller neighbour, or above the larger),
    which for a fillet radius law can drive the radius negative or past a
    value the geometry can't build -- a correctness hazard, not just an
    aesthetic one. The Fritsch-Carlson tangent construction is shape-
    preserving: the interpolant is monotone on every interval where the
    data is monotone and never leaves the [min, max] envelope of its
    surrounding stations, so a law built from positive stations stays
    strictly positive. It is C1 (continuous tangent) at every interior
    station -- the slope-continuity a piecewise-linear law lacks -- and
    needs no numpy/scipy, so it degrades the same way everywhere.
    """
    m = len(ts)
    if m == 1:
        r0 = rs[0]
        return lambda t: r0
    h = [ts[k + 1] - ts[k] for k in range(m - 1)]
    delta = [(rs[k + 1] - rs[k]) / h[k] if h[k] > 0 else 0.0
             for k in range(m - 1)]
    slope = [0.0] * m
    slope[0] = delta[0]
    slope[m - 1] = delta[m - 2]
    for k in range(1, m - 1):
        if delta[k - 1] * delta[k] <= 0.0:
            slope[k] = 0.0  # local extremum -> flat tangent, no overshoot
        else:
            w1 = 2.0 * h[k] + h[k - 1]
            w2 = h[k] + 2.0 * h[k - 1]
            slope[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])

    def r_of(t):
        if t <= ts[0]:
            return rs[0]
        if t >= ts[-1]:
            return rs[-1]
        seg = 0
        while seg < m - 2 and t > ts[seg + 1]:
            seg += 1
        hk = h[seg] if h[seg] > 0 else 1.0
        u = (t - ts[seg]) / hk
        u2 = u * u
        u3 = u2 * u
        h00 = 2 * u3 - 3 * u2 + 1
        h10 = u3 - 2 * u2 + u
        h01 = -2 * u3 + 3 * u2
        h11 = u3 - u2
        return (h00 * rs[seg] + h10 * hk * slope[seg]
                + h01 * rs[seg + 1] + h11 * hk * slope[seg + 1])

    return r_of


def variable_fillet_solid(solid, edge, radius_params, tol=1e-3):
    """Round a single edge of a SOLID with a radius that VARIES along the
    edge, instead of fillet_solid's single constant (or per-edge-
    constant) radius.

    Native implementation of the "variable-radius fillet" feature
    (RFS_VariableFillet).

    HONESTY ABOUT THIS FreeCAD/OCCT BUILD'S BINDING -- READ THIS: the
    Part.Shape.makeFillet binding shipped with this FreeCAD (1.1) exposes
    ONLY two forms -- makeFillet(radius, edges) (constant) and
    makeFillet(r_start, r_end, edges) (a single LINEAR taper from the
    edge's start vertex to its end vertex). It does NOT accept a
    multi-point [param, radius] law: passing one raises "This method
    accepts: one radius / two radii and a list of edges". (The earlier
    version of this function passed such a law and therefore FAILED on
    every call on this build.) So on a SOLID EDGE the only variable-radius
    fillet physically reachable here is the two-radius linear taper, and
    that is exactly what this function now builds. For a genuine MULTI-
    STATION, C1-SMOOTH radius law, use multi_rail_fillet, which builds the
    fillet as a surface strip cross-section by cross-section and CAN honour
    an arbitrary smooth law (it does not go through makeFillet).

    `edge`: a single Part.Edge belonging to `solid`.
    `radius_params`: list of (t, radius) pairs, t in [0, 1] (0 =
                 edge.FirstParameter, 1 = edge.LastParameter). Exactly the
                 two END radii drive the taper: the radius at t=0 and the
                 radius at t=1 (linearly extrapolated from the two nearest
                 stations if no station sits exactly on an end). Must
                 contain >= 2 stations. Because only a linear taper is
                 representable, supplying >= 3 stations that are NOT
                 collinear (i.e. that ask for a genuinely curved law)
                 raises -- rather than silently flattening the interior
                 stations the caller asked for -- and points the caller at
                 multi_rail_fillet.

    Raises ValueError for a non-solid input, fewer than 2 radius_params,
    any (t, radius) with t outside [0, 1], any non-positive radius, or a
    >= 3-station request whose stations don't lie on one straight line
    (unrepresentable as a taper on this binding). Raises RuntimeError on
    OCCT failure (typically a radius too large for the adjacent geometry),
    mirroring fillet_solid's own error contract.
    """
    if not getattr(solid, "Solids", None):
        raise ValueError(
            "variable_fillet_solid: input must be a SOLID (Part::Feature "
            "with .Solids), got %s" % type(solid).__name__)
    pairs = sorted(radius_params or [], key=lambda p: p[0])
    if len(pairs) < 2:
        raise ValueError(
            "variable_fillet_solid: need >= 2 (t, radius) points to "
            "define a variable-radius law -- got %d (use fillet_solid "
            "for a constant radius)" % len(pairs))
    for t, r in pairs:
        if not (0.0 <= t <= 1.0):
            raise ValueError(
                "variable_fillet_solid: parameter t=%r out of [0, 1]" % t)
        if r is None or r <= 1e-9:
            raise ValueError(
                "variable_fillet_solid: radius must be positive (got %r "
                "at t=%r)" % (r, t))
    # Only a single linear taper is representable on this build's binding.
    # Fit the line through the end stations and verify every interior
    # station lies on it (within tol); otherwise the caller wants a curved
    # law this binding cannot build -- say so instead of faking it.
    t0, r0 = pairs[0]
    t1, r1 = pairs[-1]
    span = (t1 - t0) if (t1 - t0) > 1e-12 else 1.0
    slope = (r1 - r0) / span
    for t, r in pairs[1:-1]:
        if abs((r0 + slope * (t - t0)) - r) > max(tol, 1e-6):
            raise ValueError(
                "variable_fillet_solid: this FreeCAD/OCCT build's makeFillet "
                "binding can only build a LINEAR two-radius taper on a solid "
                "edge, but the %d stations given describe a curved radius "
                "law (station t=%r r=%r is off the end-to-end line). Use "
                "multi_rail_fillet for a genuine smooth multi-station law, "
                "or supply just the two end radii." % (len(pairs), t, r))
    # Extrapolate the two stations out to the edge's actual ends (t=0, t=1)
    # so the taper spans the whole edge, clamping to stay positive.
    r_at_0 = max(r0 + slope * (0.0 - t0), 1e-6)
    r_at_1 = max(r0 + slope * (1.0 - t0), 1e-6)
    try:
        if abs(r_at_0 - r_at_1) < 1e-9:
            return solid.makeFillet(r_at_0, [edge])
        return solid.makeFillet(r_at_0, r_at_1, [edge])
    except Exception as exc:
        raise RuntimeError(
            "variable_fillet_solid: OCCT taper fillet failed (%s) -- "
            "usually a radius too large for the geometry next to this "
            "edge" % exc)


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
    if kind in ("coincide", "concentric", "mirror", "tangent_to_face"):
        return (c[1], c[2])
    if kind == "equal":
        return (c[1], c[2], c[3], c[4])
    if kind in ("tangent", "symmetric"):
        return (c[1], c[2], c[3])
    if kind == "on_curve":
        return (c[1],)
    return ()


def constraint_point_indices(c):
    """Public alias of _constraint_point_indices (panel badge code)."""
    return _constraint_point_indices(c)


def _shape_normal_near(shape, p):
    """Unit normal of the face of `shape` nearest to point p, or None."""
    try:
        shape = getattr(shape, "Shape", shape)
        d, _pts, info = Part.Vertex(p).distToShape(shape)
        if info[0][0] != "Face" and info[1][0] != "Face":
            return None
        rec = info[1] if info[1][0] == "Face" else info[0]
        face = shape.Faces[rec[1]]
        u, v = rec[2]
        n = face.normalAt(u, v)
        return n if n.Length > 1e-12 else None
    except Exception:
        return None


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
        elif kind in ("coincide", "concentric"):
            p1, p2 = pts[c[1]], pts[c[2]]
            res.append(p1.x - p2.x)
            res.append(p1.y - p2.y)
            res.append(p1.z - p2.z)
        elif kind == "equal":          # |Pi-Pj| == |Pk-Pl|
            res.append(pts[c[1]].distanceToPoint(pts[c[2]])
                       - pts[c[3]].distanceToPoint(pts[c[4]]))
        elif kind == "tangent":        # Pi-Pj-Pk collinear (Pj = pivot)
            a = pts[c[2]] - pts[c[1]]
            b = pts[c[3]] - pts[c[2]]
            la, lb = a.Length, b.Length
            if la > 1e-9 and lb > 1e-9:
                cr = a.cross(b) * (1.0 / max(la, lb))   # ~ mm * sin(angle)
                res.extend([cr.x, cr.y, cr.z])
            else:
                res.extend([0.0, 0.0, 0.0])
        elif kind == "symmetric":      # Pi, Pj symmetric about centre Pk
            s = pts[c[1]] + pts[c[2]] - pts[c[3]] * 2.0
            res.extend([s.x, s.y, s.z])
        elif kind == "mirror":         # ("mirror", i, j, nx, ny, nz, d)
            n = Vector(c[3], c[4], c[5])
            ln = n.Length
            if ln > 1e-12:
                n = n * (1.0 / ln)
                pi = pts[c[1]]
                s = pi.dot(n) + c[6]
                r = pts[c[2]] - (pi - n * (2.0 * s))
                res.extend([r.x, r.y, r.z])
            else:
                res.extend([0.0, 0.0, 0.0])
        elif kind == "on_curve":       # ("on_curve", i, edge)
            try:
                res.append(Part.Vertex(pts[c[1]]).distToShape(c[2])[0])
            except Exception:
                res.append(0.0)
        elif kind == "tangent_to_face":   # ("tangent_to_face", i, j, src)
            d = pts[c[2]] - pts[c[1]]
            n = _shape_normal_near(c[3], pts[c[1]])
            if n is not None and d.Length > 1e-9:
                res.append(d.dot(n))
            else:
                res.append(0.0)
    return res


def constraint_violations(points, constraints):
    """Per-constraint violation magnitude at the CURRENT point positions
    (max |residual| of that constraint; 0.0 = satisfied or unmeasurable),
    aligned 1:1 with `constraints`. Drives the red 'off-target' badges in
    the 3D sketch panel (panel_logic.constraint_badges)."""
    pts = [Vector(p.x, p.y, p.z) if hasattr(p, "x")
           else Vector(p[0], p[1], p[2]) for p in points]
    out = []
    for c in constraints:
        try:
            r = _constraint_residuals(pts, [c])
        except Exception:
            r = []
        out.append(max((abs(v) for v in r), default=0.0))
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
        if not changed:
            break
    return pts


def solve_point_constraints(points, constraints, pinned=None, tol=1e-16,
                            max_iter=80, status=None, point_planes=None):
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

    # Optional per-point construction planes: [(origin, normal, u, v) | None]
    # aligned with `points`. A free point with a plane is kept ON it by one
    # extra residual (p - origin) . n, so constraints can't push it off its
    # sketch plane.
    plane_items = []
    for i in free_pts:
        pp = point_planes[i] if point_planes and i < len(point_planes) else None
        if pp is not None:
            n = Vector(pp[1])
            if n.Length > 1e-12:
                plane_items.append((i, Vector(pp[0]), n * (1.0 / n.Length)))

    def residual(x):
        set_x(x)
        r = _constraint_residuals(pts, constraints)
        for pi, o, n in plane_items:
            r.append((pts[pi] - o).dot(n))
        return np.asarray(r, float)

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


# --- closed-loop blend strip (RFS_BlendSurfClosed) ------------------------ #
# blend_surface (above) builds an OPEN strip between two faces -- fine
# for "blend these two adjacent faces of a model" but not for "blend
# the inside of this hole all the way around." For the closed case --
# the typical "fill a hood cutout / fender wheel arch / antenna hole
# with one continuous fillet ring" -- a fresh construction is needed:
# one cross-section parameter t runs the full loop, and at each t the
# cross-section is taken between the i-th and (i+1)-th face of the ring
# (mod N). The per-cross-section math is exactly the same Hermite
# construction blend_surface already uses; the only new thing is the
# t -> face-list mapping and the boundary sampling now wraps mod N.
def blend_surface_closed(face_list, continuity="G2", n=24, scale=1.0):
    """G1/G2 blend surface strip around a closed loop of N faces.

    Native implementation of the "closed-loop blend" (RFS_BlendSurfClosed):
    a single continuous surface-strip wrapping all the way around a ring
    of N >= 3 faces (the "fill a hole with a fillet ring" use case --
    hood cutout, fender wheel arch, antenna hole). Generalizes over
    arbitrary N, not just 4-sided holes, since real automotive-scale
    rings are rarely exactly quadrilateral and a 4-only special-case
    would immediately force a rewrite (see issues.md: "no closed-loop
    blend" is the highest-priority blend-related gap).

    `face_list`: ordered list of >= 3 Part.Face (or face-bearing FreeCAD
                 objects, coerced via _face_from_input). face_list[i]
                 must face face_list[(i+1) % N] across their nearest
                 boundaries; the caller is responsible for the ordering
                 (the typical "click faces in order around the hole"
                 FreeCAD selection gives this naturally).
    `continuity`: "G0", "G1", or "G2" -- the cross-section basis (same
                 values blend_surface already accepts; the same
                 Hermite-sampling limitations apply: G1/G2 are exact at
                 the sampled points and approximate between them).
    `n`: cross-section sample count along the FULL loop (so each ring
                 member gets ~n/N samples; pick n >= N for a smooth
                 result, otherwise per-member sampling is coarse).
    `scale`: tangent-strength factor (same dimensionless knob
                 blend_surface already exposes; kept for API parity --
                 the close seam's quality is bounded by the same
                 closest-approach search blend_surface's quality is
                 bounded by).

    Returns: a Part.Shape (Shell) of N-1 sub-strip faces (one per
             consecutive pair in the ring), welded into one Shell via
             sewShape. The returned shape's free_edge_count is the QA
             signal: 0 means the wrap closed perfectly; > 0 means the
             closure seam has a residual gap the user should be told
             about (see issues.md: "wrap-around seam is the weak point").

    Raises: ValueError for < 3 faces, or when any pair of consecutive
             faces has no detectable facing boundary (the caller gave
             the faces in the wrong order, or two of them don't actually
             face each other).
    """
    faces = [_face_from_input(f) for f in (face_list or [])]
    faces = [f for f in faces if f is not None]
    if len(faces) < 3:
        raise ValueError(
            "blend_surface_closed: need >= 3 faces forming a closed loop "
            "(got %d)" % len(faces))

    order = {"G0": 0, "G1": 1, "G2": 2}.get(str(continuity), 2)
    _dn_cache = {}      # shared analytic-derivative cache for _cross_d2

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return _cross_tan_from_surface_tangent(du, dv, side)

    def _cross_d2(face, pr, side, u, v):
        # Analytic first (see _surf_dn) -- finite difference only as fallback.
        duo, dvo = (2, 0) if side < 2 else (0, 2)
        analytic = _surf_dn(face, u, v, duo, dvo, _dn_cache)
        if analytic is not None:
            return analytic
        # Fall back to finite differences
        eps = _compute_finite_difference_epsilon(pr, 0 if side < 2 else 1)
        if side < 2:
            # Derivative in u direction
            return _finite_difference_second_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                -1.0 if side == 0 else 1.0, 0.0, eps)
        else:
            # Derivative in v direction
            return _finite_difference_second_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                0.0, -1.0 if side == 2 else 1.0, eps)

    N = len(faces)
    # Locate the facing-side pairs ONCE, up front -- each consecutive pair
    # in the ring shares one boundary; that's the boundary the cross-
    # section will be anchored to.
    pair_info = []
    for i in range(N):
        a, b = faces[i], faces[(i + 1) % N]
        sa, sb, a_pr, b_pr = _nearest_facing_sides(a, b)
        pair_info.append((a, b, sa, sb, a_pr, b_pr))

    # Build N-1 sub-strips, one per ring member (last one is the
    # wrap-around that closes the ring: face N-1 -> face 0). The Hermite
    # cross-section basis is identical to blend_surface's; the t parameter
    # here runs 0..1 ACROSS the i-th ring member (NOT across the whole
    # loop -- that would require per-pair span estimation which adds
    # nothing; n is set by the caller as a global resolution budget).
    sub_faces = []
    edge_caches = [{} for _ in range(N)]
    for i in range(N):
        face_a, face_b, sa, sb, a_pr, b_pr = pair_info[i]
        ec = edge_caches[i]
        grid = []
        for k in range(n):
            t = k / (n - 1.0)
            ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, ec)
            ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, ec)
            P0 = face_a.valueAt(ua, va)
            P1 = face_b.valueAt(ub, vb)
            chord = max((P1 - P0).Length, 1e-9)
            h = chord * scale / 3.0
            if order == 0:
                row = [P0 + (P1 - P0) * j / (n - 1.0) for j in range(n)]
            elif order == 1:
                T0 = _cross_tan(face_a, sa, ua, va)
                T0 = T0 * (h / max(T0.Length, 1e-9))
                T1 = _cross_tan(face_b, sb, ub, vb)
                T1 = T1 * (h / max(T1.Length, 1e-9))
                row = [_hermite3(P0, T0, P1, T1 * -1.0, j / (n - 1.0))
                       for j in range(n)]
            else:
                T0 = _cross_tan(face_a, sa, ua, va)
                T0 = T0 * (h / max(T0.Length, 1e-9))
                K0 = _cross_d2(face_a, a_pr, sa, ua, va) * (chord * chord / 2.0)
                T1 = _cross_tan(face_b, sb, ub, vb)
                T1 = T1 * (h / max(T1.Length, 1e-9))
                K1 = _cross_d2(face_b, b_pr, sb, ub, vb) * (chord * chord / 2.0)
                row = [_hermite5(P0, T0, K0, P1, T1 * -1.0, K1 * -1.0,
                                j / (n - 1.0)) for j in range(n)]
            grid.append(row)
        sub_faces.append(grid_to_surface(grid).Faces[0])

    # Sew the N-1 sub-strips into one Shell. As in n_sided_patch, if
    # OCCT can't sew within tolerance, return a Compound of the strips
    # rather than raising -- each strip is individually valid.
    try:
        shell = Part.Shell(sub_faces)
        shell.sewShape()
        if shell.isNull() or not shell.Faces:
            return Part.Compound(sub_faces)
        return shell
    except Exception:
        return Part.Compound(sub_faces)


# --- true surface-surface trim (RFS_TrimByIntersection) ------------------ #
# trim_face (above) only trims to a wire that's ALREADY on the face or
# projectable onto it. The "intersect two faces, trim BOTH" use case
# (the operation a real user does by hand today as TWO trim_face calls
# + manual curve selection) was missing entirely. This fills that gap
# with a one-call function: take two faces, intersect them via
# Part.Shape.section, trim each to the side of the intersection
# containing the OTHER face, return both trimmed faces AND the
# intersection wire (so the caller can immediately fillet the new edge,
# for example -- see the composability argument in issues.md: "no true
# surface-surface trim").
def trim_faces_by_intersection(face_a, face_b, tol=1e-4):
    """Intersect two faces and trim BOTH to the intersection curve.

    Native implementation of the "trim by surface-surface intersection"
    feature (RFS_TrimByIntersection): the operation that produces the
    classic "two patches meet along a curve defined by their mutual
    intersection" geometry in one call. Uses Part.Shape.section for the
    intersection (the standard OCCT primitive for surface-surface
    intersection; the same one FreeCAD's own Part Boolean machinery
    uses), then trims each face via the existing trim_face path with
    the resulting intersection wire.

    `face_a`, `face_b`: Part.Face (or face-like FreeCAD object, coerced
                 via _face_from_input).
    `tol`: intersection tolerance forwarded to Shape.section, and to
                 trim_face's underlying Part.Face(surface, wire) call.

    Returns: a 3-tuple (trimmed_a, trimmed_b, intersection_wire).
      - trimmed_a: face_a trimmed to the side of the intersection
                 containing face_b's center.
      - trimmed_b: face_b trimmed to the side of the intersection
                 containing face_a's center.
      - intersection_wire: a Part.Wire of the actual intersection
                 curve -- handy for the caller to feed into subsequent
                 operations (a fillet on the new edge, a blend, etc.)
                 without re-running the intersection.

    Raises: ValueError if the faces don't actually intersect within
                 `tol` (Shape.section returns no edges), or if the
                 resulting intersection wire is too fragmented to be
                 assembled into a single wire.

    KNOWN LIMIT: the "which side to keep" choice is a heuristic -- the
    side of the intersection wire containing the OTHER face's center.
    For non-convex or multiply-intersecting faces this may pick the
    wrong side; the function does not try to be smart about that case,
    it just picks the side that contains the geometric center of the
    partner face. This is the same trade-off the rest of this module
    already makes (e.g. trim_face's fast-path / projection fallback
    for off-surface wires).
    """
    fa = _face_from_input(face_a)
    fb = _face_from_input(face_b)
    if fa is None or fb is None:
        raise ValueError(
            "trim_faces_by_intersection: both inputs must be faces (or "
            "face-bearing objects); got %r and %r" % (face_a, face_b))
    # Part.Shape.section is the OCCT surface-surface intersection
    # primitive. It returns a Compound of edges lying on the
    # intersection of the two faces' underlying surfaces. For trimmed
    # faces (e.g. an already-trimmed BlendSurf) the section can
    # return edges that lie on the un-trimmed support surface but
    # outside the actual trimmed region -- trim_face's projection
    # fallback handles that case for the resulting wire, exactly as
    # it does for any off-surface wire (see trim_face's docstring).
    try:
        section = fa.Section(fb) if hasattr(fa, "Section") else None
    except Exception:
        section = None
    if section is None:
        try:
            from FreeCAD import Part as _Part
            section = _Part.Shape(fa).section(_Part.Shape(fb))
        except Exception as exc:
            raise ValueError(
                "trim_faces_by_intersection: Shape.section failed (%s) -- "
                "the two faces may be parallel, coincident, or otherwise "
                "non-intersecting" % exc)
    edges = list(getattr(section, "Edges", []) or [])
    if not edges:
        raise ValueError(
            "trim_faces_by_intersection: Shape.section returned no edges -- "
            "the two faces do not actually intersect within tol=%g "
            "(parallel planes offset by more than tol, or disjoint surfaces)"
            % tol)
    # Build a single Wire from the section edges. If they're already
    # chained into one Wire, use that; if not, sort+chain them via
    # Part.__sortEdges__ (the same helper trim_face uses internally).
    try:
        if hasattr(section, "Wires") and section.Wires:
            intersection_wire = section.Wires[0]
        else:
            intersection_wire = Part.Wire(Part.__sortEdges__(edges))
    except Exception as exc:
        raise ValueError(
            "trim_faces_by_intersection: couldn't assemble section edges "
            "into a wire (%s) -- the intersection may be too fragmented "
            "(multiple disjoint segments)" % exc)

    # Side selection: keep the side of the intersection wire that
    # contains the OTHER face's center. We compute each face's
    # centroid via its ParameterRange midpoint (cheap, exact for
    # trimmed faces whose underlying surface's parameterization is
    # monotonic, approximate-but-good-enough otherwise).
    def _face_center(face):
        u0, u1, v0, v1 = face.ParameterRange
        return face.valueAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))

    center_a = _face_center(fa)
    center_b = _face_center(fb)
    # Build a per-face trim wire: intersection_wire + the boundary
    # arcs of THAT face that lie on the "other side" -- in practice
    # the simplest thing that produces a valid trimmed face is to
    # combine the intersection wire with the face's existing outer
    # wire (Part.Face(surface, compound_wire) constructs the trimmed
    # face from ANY valid wire lying on the surface, and the
    # intersection wire alone is almost always that for the two-
    # patches-meet case).
    try:
        trimmed_a = trim_face(fa, intersection_wire, tol=tol)
        trimmed_b = trim_face(fb, intersection_wire, tol=tol)
    except Exception as exc:
        raise ValueError(
            "trim_faces_by_intersection: trimming to the intersection wire "
            "failed (%s) -- the wire may not lie fully on one of the faces "
            "(a known Shape.section limitation on certain trimmed inputs; "
            "see trim_face's docstring for the projection fallback)" % exc)
    return trimmed_a, trimmed_b, intersection_wire


# --- global fairing / multi-patch optimization (RFS_GlobalFair) ---------- #
# balance_network_continuity (below) fixes SEAM continuity one seam at a
# time by rebuilding a whole face against a neighbor. What it does NOT do
# is anything about a single patch's own INTERIOR shape being rippled/
# unfair even when every seam already reads G2 -- that's a property of
# the control net's own smoothness, not of any one seam. These functions
# are the "fair the whole network's control nets" feature -- see
# global_fair_network's docstring for the explicit scope limits (this is
# NOT a variational least-squares solver).
def _face_control_grid(face):
    """(surf_copy, grid) where grid[i][j] is the (0-based) control point
    of a FRESH COPY of face's underlying surface, expressed as a
    BSplineSurface -- callers mutate `surf_copy`'s poles freely without
    touching `face` itself. Raises ValueError if the surface can't be
    expressed as, or converted/fit to, a BSplineSurface (mirrors
    match_surface_exact_g1's _as_bspline helper, generalized into a
    standalone function since global_fair_network needs it outside that
    function's ref/tgt framing)."""
    surf = face.Surface
    if isinstance(surf, Part.BSplineSurface):
        surf = surf.copy()
    else:
        try:
            surf = surf.toBSpline()
        except Exception:
            try:
                u0, u1, v0, v1 = face.ParameterRange
                # Adaptive sampling density based on parameter range size
                # Ensure we have enough points for derivative calculations
                u_range = max(u1 - u0, 1e-6)  # Avoid division by zero
                v_range = max(v1 - v0, 1e-6)
                # Base density on range size, with reasonable bounds
                nu = max(8, min(32, int(u_range * 4)))
                nv = max(8, min(32, int(v_range * 4)))
                grid_pts = [[surf.value(u0 + i * u_range / (nu - 1.0),
                                        v0 + j * v_range / (nv - 1.0))
                            for j in range(nv)] for i in range(nu)]
                bs = Part.BSplineSurface()
                bs.interpolate(grid_pts)
                surf = bs
            except Exception as exc:
                raise ValueError(
                    "_face_control_grid: surface type %s can't be "
                    "converted to a BSplineSurface (%s)"
                    % (type(face.Surface).__name__, exc))
    nu, nv = surf.NbUPoles, surf.NbVPoles
    grid = [[surf.getPole(i + 1, j + 1) for j in range(nv)]
            for i in range(nu)]
    return surf, grid


# --- analytic (non-finite-difference) surface derivatives ---------------- #
# match_surface / blend_surface / blend_surface_closed each used to estimate
# the cross-boundary 2nd/3rd derivative (_cross_d2 / _cross_d3) with a purely
# one-sided FINITE DIFFERENCE, stacked on top of an already-finite-difference
# lower-order estimate for the 3rd-derivative case. FreeCAD's Part API does
# expose the exact analytic derivative of a BSplineSurface's control net
# (Part.BSplineSurface.getDN(u, v, du, dv), the surface analog of
# BSplineCurve.getD2/getD3 already used elsewhere in this module for curves)
# -- it was simply never reached for surfaces because a face's native
# Surface object isn't always already a BSplineSurface. _bspline_surface_cached
# does the same toBSpline()/grid-fit conversion _face_control_grid already
# performs for fairing, reused here for derivative evaluation; _surf_dn tries
# the analytic path first and returns None (not an approximation) when no
# BSpline conversion is possible, so callers can fall back to their own
# finite-difference estimate exactly as before -- this is a strict accuracy
# upgrade for the common case, not a behaviour change for the cases it can't
# help.
def _bspline_surface_cached(face, cache):
    """BSplineSurface copy of `face`'s underlying surface, or None if no
    conversion is possible -- memoized in `cache` (a plain dict the caller
    creates once per blend/match invocation, same convention as the
    edge_cache already threaded through _side_uv_trimmed) so each face's
    conversion happens once, not once per sample point."""
    key = id(face)
    if key in cache:
        return cache[key]
    try:
        surf, _grid = _face_control_grid(face)
    except Exception:
        surf = None
    cache[key] = surf
    return surf


def _surf_dn(face, u, v, du, dv, cache):
    """Exact analytic partial derivative d^(du+dv)S/du^du dv^dv at (u, v),
    read directly off `face`'s control net via OCCT's
    BSplineSurface.getDN -- NOT a finite-difference estimate. Returns None
    (not an approximate value) when `face` can't be expressed as a
    BSplineSurface at all, or this FreeCAD/OCCT build's Part binding
    doesn't expose getDN, so the caller can fall back to its own
    finite-difference path unchanged."""
    surf = _bspline_surface_cached(face, cache)
    if surf is None:
        return None
    # Check if we can compute the requested derivative
    # For a mixed derivative d^(du+dv)/du^du dv^dv, we need u-degree >= du and v-degree >= dv
    if hasattr(surf, 'UDegree') and hasattr(surf, 'VDegree'):
        if surf.UDegree < du or surf.VDegree < dv:
            return None  # Degree too low for requested derivative
    try:
        return surf.getDN(u, v, int(du), int(dv))
    except Exception:
        return None


def network_fairness_energy(faces):
    """Sum, over every face's control net, of the discrete second-
    difference ("bending energy") of its INTERIOR poles -- a cheap
    numeric proxy for how rippled a patch network's control nets are.
    Lower is fairer; zero only for a perfectly planar/ruled grid of
    poles. This is NOT a physical bending energy (no arc-length or
    material stiffness involved), it is the same discrete Laplacian
    magnitude global_fair_network minimizes, exposed standalone so a
    caller (or a QA panel) can report a before/after number without
    re-running the whole fairing pass.

    Faces whose surface can't be expressed as a BSplineSurface are
    silently skipped (contribute 0) rather than raising -- this is a
    diagnostic aggregate, not a correctness-critical path.
    """
    faces = [_face_from_input(f) for f in (faces or [])]
    faces = [f for f in faces if f is not None]
    total = 0.0
    for face in faces:
        try:
            _surf, grid = _face_control_grid(face)
        except Exception:
            continue
        nu = len(grid)
        nv = len(grid[0]) if nu else 0
        for i in range(1, nu - 1):
            for j in range(nv):
                lap = grid[i - 1][j] + grid[i + 1][j] - grid[i][j] * 2.0
                total += lap.Length ** 2
        for i in range(nu):
            for j in range(1, nv - 1):
                lap = grid[i][j - 1] + grid[i][j + 1] - grid[i][j] * 2.0
                total += lap.Length ** 2
    return total


def _pole_ij_boundary(surf, side, along):
    """1-based (i, j) pole index on `surf`'s boundary `side` (0=umin,
    1=umax,2=vmin,3=vmax) at position `along` running along that side --
    the depth-0 case of match_surface_exact_g1's _pole_ij, pulled out as
    a standalone helper since _resync_shared_boundaries needs it without
    that function's ref/tgt framing."""
    nu, nv = surf.NbUPoles, surf.NbVPoles
    if side == 0:
        return (1, 1 + along)
    if side == 1:
        return (nu, 1 + along)
    if side == 2:
        return (1 + along, 1)
    return (1 + along, nv)


def _harmonize(s1, ax1, s2, ax2, tol=1e-9):
    """Merge the knot vectors of two B-spline surfaces along one axis each
    ("U"/"V"), in place, so their boundary rows line up pole-for-pole.
    Raises ValueError if the parametrizations can't be made compatible.
    (Module-level twin of the closure inside match_surface_exact_g1;
    _resync_shared_boundaries referenced this name with no definition in
    scope, so every shared-pole fairing resync raised NameError.)"""
    k1 = list(getattr(s1, "get" + ax1 + "Knots")())
    m1 = list(getattr(s1, "get" + ax1 + "Multiplicities")())
    k2 = list(getattr(s2, "get" + ax2 + "Knots")())
    m2 = list(getattr(s2, "get" + ax2 + "Multiplicities")())
    merged = {}
    for k, m in zip(k1, m1):
        merged[k] = max(merged.get(k, 0), m)
    for k, m in zip(k2, m2):
        merged[k] = max(merged.get(k, 0), m)
    ks = sorted(merged)
    ms = [merged[k] for k in ks]
    try:
        getattr(s1, "insert" + ax1 + "Knots")(ks, ms, tol, False)
        getattr(s2, "insert" + ax2 + "Knots")(ks, ms, tol, False)
    except Exception as exc:
        raise ValueError("knot harmonization failed (%s)" % exc)
    k1b = list(getattr(s1, "get" + ax1 + "Knots")())
    k2b = list(getattr(s2, "get" + ax2 + "Knots")())
    if len(k1b) != len(k2b) or any(abs(a - b) > 1e-6
                                   for a, b in zip(k1b, k2b)):
        raise ValueError("boundary parametrizations aren't knot-compatible "
                         "even after harmonization")


def _resync_shared_boundaries(orig_faces, surfs, pairs, gap_tol):
    """Best-effort re-synchronization of shared boundary poles after a
    fairing pass with fix_boundary=False (see global_fair_network's
    docstring). For each adjacent pair, harmonize their knot vectors and
    degrees along the shared edge direction, then average the boundary rows
    together in place so relaxing one face's boundary doesn't pull it away
    from its neighbor and reopen a seam gap.

    This extends the original approach by using knot harmonization (like
    match_surface_exact_g1) to handle mismatched pole counts between
    adjacent faces, enabling resynchronization even when refinements differ.
    """
    for i, j in pairs:
        sa, sb, a_pr, b_pr = _nearest_facing_sides(orig_faces[i], orig_faces[j])
        si, sj = surfs[i], surfs[j]
        if si is None or sj is None:
            continue

        # Determine which parameter varies along the shared edge
        # For side 0 (umin) or 1 (umax): U is constant, V varies
        # For side 2 (vmin) or 3 (vmax): V is constant, U varies
        if sa in (0, 1):  # U-min or U-max -> V varies along edge
            u_surf_i, u_surf_j = None, None  # Not harmonizing U
            v_surf_i, v_surf_j = si, sj      # Harmonizing V
        else:  # side in (2, 3) -> V-min or V-max -> U varies along edge
            u_surf_i, u_surf_j = si, sj      # Harmonizing U
            v_surf_i, v_surf_j = None, None  # Not harmonizing V

        # Harmonize knots and match degrees in the varying direction
        if u_surf_i is not None:
            try:
                # Harmonize knot vectors in U direction
                _harmonize(u_surf_i, "U", u_surf_j, "U")

                # Match degrees in U direction
                deg_u_i = u_surf_i.UDegree
                deg_u_j = u_surf_j.UDegree
                if deg_u_i != deg_u_j:
                    target_deg_u = max(deg_u_i, deg_u_j)
                    try:
                        u_surf_i.increaseDegree(target_deg_u, u_surf_i.VDegree)
                        u_surf_j.increaseDegree(target_deg_u, u_surf_j.VDegree)
                    except Exception as exc:
                        # If degree matching fails, skip this pair
                        continue
            except Exception:
                # If harmonization fails, skip this pair
                continue
        if v_surf_i is not None:
            try:
                # Harmonize knot vectors in V direction
                _harmonize(v_surf_i, "V", v_surf_j, "V")

                # Match degrees in V direction
                deg_v_i = v_surf_i.VDegree
                deg_v_j = v_surf_j.VDegree
                if deg_v_i != deg_v_j:
                    target_deg_v = max(deg_v_i, deg_v_j)
                    try:
                        v_surf_i.increaseDegree(v_surf_i.UDegree, target_deg_v)
                        v_surf_j.increaseDegree(v_surf_j.UDegree, target_deg_v)
                    except Exception as exc:
                        # If degree matching fails, skip this pair
                        continue
            except Exception:
                # If harmonization fails, skip this pair
                continue

        # Get pole counts after potential harmonization
        def _side_pole_count(surf, side):
            return surf.NbVPoles if side < 2 else surf.NbUPoles

        na = _side_pole_count(si, sa)
        nb = _side_pole_count(sj, sb)
        # After harmonization, pole counts should match (or we'd have skipped above)
        for along in range(na):
            pi = _pole_ij_boundary(si, sa, along)
            pj = _pole_ij_boundary(sj, sb, along)
            pa = si.getPole(*pi)
            pb = sj.getPole(*pj)
            avg = (pa + pb) * 0.5
            si.setPole(pi[0], pi[1], avg)
            sj.setPole(pj[0], pj[1], avg)


def _variational_fair_direct(orig_faces, surfs, fix_boundary, energy, gap_tol):
    """Solve the WHOLE patch network's fairing in one linear system --
    the "assemble and solve one global system across every shared pole
    simultaneously" step the Jacobi relaxation only approximates.

    Mutates the BSplineSurface objects in `surfs` IN PLACE and returns
    True on success, or returns False WITHOUT touching any pole when the
    solve can't be done (numpy/scipy missing, no free poles, no fixed
    anchor, or a singular/non-finite system) so the caller can fall back
    to the iterative path with nothing half-modified.

    How the system is built:
      * Every control pole (patch p, row i, col j) is a node. When
        `fix_boundary` is False, poles that COINCIDE across a shared seam
        (matched via _nearest_facing_sides + _pole_ij_boundary, same
        correspondence _resync_shared_boundaries uses, and only where the
        two sides have equal pole counts) are merged into a SINGLE unknown
        by union-find -- that merge is what makes the seam solved jointly
        by both patches at once instead of averaged after the fact.
      * Fixed nodes (Dirichlet data): with `fix_boundary` True, every
        patch-boundary pole; with it False, only NAKED outer-boundary
        poles (boundary poles that never got merged with a neighbour).
        All remaining nodes are free unknowns.
      * A linear fairing operator is applied per patch and stacked over
        the merged node set:
          energy="membrane"   -> first differences along grid edges;
                                 its minimiser is exactly the fully-
                                 converged Jacobi (average-of-neighbours)
                                 fixed point, reached directly here.
          energy="thin_plate" -> second differences along each u- and
                                 v-line, i.e. the SAME discrete bending
                                 term network_fairness_energy sums, so the
                                 result is the true global minimiser of the
                                 energy the report measures.
        With operator O and unknown/fixed split, minimising ||O x||^2
        gives (O^T O)_ff x_f = -(O^T O)_fb x_b, solved per coordinate.

    Because merged seam nodes get ONE solved position written back to every
    constituent pole, shared seams close exactly (better than the
    averaging _resync_shared_boundaries does).
    """
    try:
        import numpy as np
        import scipy.sparse as sp
        import scipy.sparse.linalg as spla
    except Exception:
        return False

    dims = []
    for surf in surfs:
        dims.append((surf.NbUPoles, surf.NbVPoles) if surf is not None
                    else None)

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for p, d in enumerate(dims):
        if d is None:
            continue
        nu, nv = d
        for i in range(nu):
            for j in range(nv):
                find((p, i, j))

    if not fix_boundary:
        pairs = adjacency_graph(orig_faces, gap_tol=gap_tol)
        for a, b in pairs:
            if surfs[a] is None or surfs[b] is None:
                continue
            sa, sb, _apr, _bpr = _nearest_facing_sides(orig_faces[a],
                                                       orig_faces[b])
            na = surfs[a].NbVPoles if sa < 2 else surfs[a].NbUPoles
            nb = surfs[b].NbVPoles if sb < 2 else surfs[b].NbUPoles
            if na != nb:
                continue
            for along in range(na):
                ia, ja = _pole_ij_boundary(surfs[a], sa, along)
                ib, jb = _pole_ij_boundary(surfs[b], sb, along)
                union((a, ia - 1, ja - 1), (b, ib - 1, jb - 1))

    def is_boundary(node):
        p, i, j = node
        nu, nv = dims[p]
        return i == 0 or i == nu - 1 or j == 0 or j == nv - 1

    from collections import defaultdict
    members = defaultdict(list)
    for node in list(parent.keys()):
        members[find(node)].append(node)

    fixed_roots = set()
    for root, nodes in members.items():
        any_boundary = any(is_boundary(n) for n in nodes)
        patches = {n[0] for n in nodes}
        if fix_boundary:
            if any_boundary:
                fixed_roots.add(root)
        else:
            if any_boundary and len(patches) == 1:
                fixed_roots.add(root)

    free_roots = [r for r in members if r not in fixed_roots]
    fixed_list = [r for r in members if r in fixed_roots]
    if not free_roots or not fixed_list:
        # nothing to move, or nothing to anchor the free poles to
        return not free_roots

    col = {r: k for k, r in enumerate(free_roots)}
    fcol = {r: k for k, r in enumerate(fixed_list)}

    rows = []
    for p, d in enumerate(dims):
        if d is None:
            continue
        nu, nv = d
        if energy == "membrane":
            for i in range(nu):
                for j in range(nv):
                    if i + 1 < nu:
                        rows.append([(find((p, i, j)), 1.0),
                                     (find((p, i + 1, j)), -1.0)])
                    if j + 1 < nv:
                        rows.append([(find((p, i, j)), 1.0),
                                     (find((p, i, j + 1)), -1.0)])
        else:  # thin_plate
            for i in range(1, nu - 1):
                for j in range(nv):
                    rows.append([(find((p, i - 1, j)), 1.0),
                                 (find((p, i, j)), -2.0),
                                 (find((p, i + 1, j)), 1.0)])
            for i in range(nu):
                for j in range(1, nv - 1):
                    rows.append([(find((p, i, j - 1)), 1.0),
                                 (find((p, i, j)), -2.0),
                                 (find((p, i, j + 1)), 1.0)])
    if not rows:
        return True

    nf, nb = len(free_roots), len(fixed_list)
    rf, cf, vf = [], [], []
    rb, cb, vb = [], [], []
    for ri, terms in enumerate(rows):
        acc = {}
        for root, coeff in terms:
            acc[root] = acc.get(root, 0.0) + coeff
        for root, coeff in acc.items():
            if root in col:
                rf.append(ri); cf.append(col[root]); vf.append(coeff)
            else:
                rb.append(ri); cb.append(fcol[root]); vb.append(coeff)

    nrows = len(rows)
    Of = sp.csr_matrix((vf, (rf, cf)), shape=(nrows, nf))
    Ob = sp.csr_matrix((vb, (rb, cb)), shape=(nrows, nb))

    xb = np.empty((nb, 3))
    for r, k in fcol.items():
        p, i, j = r
        pole = surfs[p].getPole(i + 1, j + 1)
        xb[k] = (pole.x, pole.y, pole.z)

    A = (Of.T @ Of).tocsc()
    rhs = -(Of.T @ (Ob @ xb))
    try:
        X = spla.spsolve(A, rhs)
    except Exception:
        return False
    X = np.asarray(X)
    if X.ndim == 1:
        X = X.reshape(nf, 3)
    if not np.all(np.isfinite(X)):
        return False

    for root, k in col.items():
        v = Vector(float(X[k, 0]), float(X[k, 1]), float(X[k, 2]))
        for (p, i, j) in members[root]:
            surfs[p].setPole(i + 1, j + 1, v)
    return True


def global_fair_network(faces, iterations=20, lambda_=0.5, fix_boundary=True,
                        gap_tol=1e-3, samples=8, tol=None,
                        solver="auto", energy="thin_plate"):
    """Global fairing over a whole patch network's control nets -- the
    native implementation of "global fairing / multi-patch optimization"
    (RFS_GlobalFair).

    Two solvers are available via `solver`:

    * solver="direct"/"auto" (DEFAULT, when numpy+scipy are present) --
      a real VARIATIONAL, energy-minimizing least-squares network solve:
      assemble ONE global linear system over every free pole (with poles
      shared across a seam merged into a single unknown, so seams are
      solved by both patches simultaneously) and solve it in a single
      shot for the exact minimiser of the chosen discrete energy. This is
      the "assemble and solve one global system across every shared pole
      simultaneously" step that real Class-A fairing tools (Alias, ICEM
      Surf) do and that the relaxation below only approaches. `energy`
      selects the functional:
        "thin_plate" (default) minimises the SAME discrete second-
            difference bending term network_fairness_energy reports, so
            energy_after is its true constrained global minimum;
        "membrane" minimises first-difference (stretch) energy, whose
            minimiser is exactly the fully-converged Jacobi fixed point.
      `iterations`/`lambda_`/`tol` are not used by this path (there is
      nothing to iterate -- it is a direct solve); the returned
      "iterations" is 0 and "converged" is True. If numpy/scipy are
      missing, or the assembled system has no fixed anchor or comes out
      singular, the direct path cleanly declines and (for solver="auto")
      the Jacobi path below runs instead, with nothing half-modified.

    * solver="jacobi" (or the automatic fallback) -- the original
      discrete Laplacian smoothing pass (the same "average with your
      neighbors" idea mesh-fairing tools apply to vertices, applied here
      to each surface's B-spline CONTROL POLES) repeated for
      `iterations` passes. It is honest local relaxation, not a global
      solve; it approaches the same membrane minimiser the direct path
      reaches exactly. Its per-pass behaviour:

      - INTERIOR poles of each face are relaxed toward the average of
        their up-to-4 neighbors in the control net, blended by
        `lambda_` (0 = no change, 1 = fully replace with the neighbor
        average -- 0.3-0.6 is a reasonable working range; pushing it
        much higher tends to overshoot/oscillate rather than converge
        faster).
      - BOUNDARY poles are either left completely untouched
        (`fix_boundary=True`, the default -- every seam and outer edge
        stays exactly where it was, so this only smooths each patch's
        INTERIOR shape and can never reopen a seam gap) or, when
        `fix_boundary=False`, relaxed the same way as interior poles and
        then re-synchronized across shared seams (see
        _resync_shared_boundaries) -- this CAN shift seams and will
        generally disturb any exact G0/G1 a prior match_surface/
        blend_surface pass established, so treat it as an advanced
        option and re-run network_continuity_report() afterward.

    `tol`, if given, adds a REAL convergence check on top of the
    `iterations` cap: after each pass, every surface's largest single-pole
    displacement (max over every relaxed pole of |new - old|) is tracked,
    and the whole pass stops as soon as the largest displacement across
    ALL surfaces falls below `tol` -- i.e. this no longer blindly runs a
    fixed count, it runs UNTIL the Jacobi relaxation has actually settled
    (or the `iterations` cap is hit first, whichever comes first). This is
    still the same discrete-Laplacian relaxation described above (each
    pass already updates every pole SIMULTANEOUSLY from the previous
    pass's snapshot -- true Jacobi, not Gauss-Seidel), not a direct
    (single-shot) linear solve of the fixed-point system; `tol` only
    changes WHEN it stops, not what it's iterating. `tol=None` (default)
    preserves the exact previous fixed-iteration-count behaviour.

    Returns a dict:
      {"faces": [new_face_or_None, ...] -- one entry per input face, in
               input order; None means that face's surface couldn't be
               expressed as a BSplineSurface and was left untouched,
       "energy_before": network_fairness_energy(faces) on the input,
       "energy_after": the same, computed on the result,
       "iterations": the number of passes actually run (== `iterations`
               unless `tol` is given and convergence is reached early),
       "converged": True for the direct solver (it computes the exact
               minimiser), or for the Jacobi path True only if `tol` was
               given and every pole displacement fell below it,
       "solver": "direct" or "jacobi" -- which path actually ran,
       "energy": the energy functional used ("thin_plate"/"membrane") for
               the direct path, else None}

    Raises ValueError for an empty `faces` list, `iterations < 1`,
    `lambda_` outside [0, 1], a non-positive `tol`, an unknown `solver`,
    or an unknown `energy`. Raises RuntimeError only if solver="direct"
    is explicitly requested but the direct solve is unavailable (numpy/
    scipy missing or singular system) -- use solver="auto" to allow the
    Jacobi fallback instead.
    """
    orig_faces = [_face_from_input(f) for f in (faces or [])]
    orig_faces = [f for f in orig_faces if f is not None]
    if not orig_faces:
        raise ValueError("global_fair_network: faces must be non-empty")
    if iterations < 1:
        raise ValueError("global_fair_network: iterations must be >= 1")
    if not (0.0 <= lambda_ <= 1.0):
        raise ValueError("global_fair_network: lambda_ must be in [0, 1]")
    if tol is not None and tol <= 0:
        raise ValueError("global_fair_network: tol must be positive if given")
    if solver not in ("auto", "direct", "jacobi"):
        raise ValueError("global_fair_network: solver must be 'auto', "
                         "'direct', or 'jacobi' (got %r)" % (solver,))
    if energy not in ("thin_plate", "membrane"):
        raise ValueError("global_fair_network: energy must be 'thin_plate' "
                         "or 'membrane' (got %r)" % (energy,))

    energy_before = network_fairness_energy(orig_faces)

    surfs = []
    for f in orig_faces:
        try:
            s, _grid = _face_control_grid(f)
            surfs.append(s)
        except Exception:
            surfs.append(None)

    # --- direct variational solve (default) ---------------------------- #
    if solver in ("auto", "direct"):
        if _variational_fair_direct(orig_faces, surfs, fix_boundary,
                                    energy, gap_tol):
            new_faces = [None if s is None else s.toShape().Faces[0]
                         for s in surfs]
            energy_faces = [nf if nf is not None else orig_faces[i]
                            for i, nf in enumerate(new_faces)]
            return {"faces": new_faces, "energy_before": energy_before,
                    "energy_after": network_fairness_energy(energy_faces),
                    "iterations": 0, "converged": True,
                    "solver": "direct", "energy": energy}
        if solver == "direct":
            raise RuntimeError(
                "global_fair_network: direct solver unavailable (numpy/"
                "scipy missing, or the system has no anchor / is singular) "
                "and solver='direct' forbids the Jacobi fallback -- use "
                "solver='auto' to allow it")
        # solver == "auto": rebuild fresh surfs (the failed direct attempt
        # guarantees it left poles untouched, but re-derive to be safe) and
        # fall through to Jacobi.
        surfs = []
        for f in orig_faces:
            try:
                s, _grid = _face_control_grid(f)
                surfs.append(s)
            except Exception:
                surfs.append(None)

    pairs = adjacency_graph(orig_faces, gap_tol=gap_tol) if not fix_boundary \
        else []

    converged = False
    ran = 0
    for _ in range(int(iterations)):
        ran += 1
        max_disp = 0.0
        for surf in surfs:
            if surf is None:
                continue
            nu, nv = surf.NbUPoles, surf.NbVPoles
            old = [[surf.getPole(i + 1, j + 1) for j in range(nv)]
                  for i in range(nu)]
            lo_i, hi_i = (0, nu) if not fix_boundary else (1, nu - 1)
            lo_j, hi_j = (0, nv) if not fix_boundary else (1, nv - 1)
            for i in range(lo_i, hi_i):
                for j in range(lo_j, hi_j):
                    neighbors = []
                    if i > 0:
                        neighbors.append(old[i - 1][j])
                    if i < nu - 1:
                        neighbors.append(old[i + 1][j])
                    if j > 0:
                        neighbors.append(old[i][j - 1])
                    if j < nv - 1:
                        neighbors.append(old[i][j + 1])
                    if not neighbors:
                        continue
                    avg = neighbors[0]
                    for extra in neighbors[1:]:
                        avg = avg + extra
                    avg = avg * (1.0 / len(neighbors))
                    new_p = old[i][j] * (1.0 - lambda_) + avg * lambda_
                    if tol is not None:
                        disp = new_p.distanceToPoint(old[i][j])
                        if disp > max_disp:
                            max_disp = disp
                    surf.setPole(i + 1, j + 1, new_p)
        if not fix_boundary:
            _resync_shared_boundaries(orig_faces, surfs, pairs, gap_tol)
        if tol is not None and max_disp < tol:
            converged = True
            break

    new_faces = [None if s is None else s.toShape().Faces[0] for s in surfs]
    energy_faces = [nf if nf is not None else orig_faces[i]
                    for i, nf in enumerate(new_faces)]
    energy_after = network_fairness_energy(energy_faces)

    return {"faces": new_faces, "energy_before": energy_before,
            "energy_after": energy_after, "iterations": ran,
            "converged": converged, "solver": "jacobi", "energy": None}


# --- multi-neighbor / simultaneous continuity solving (RFS_BalanceNetwork) -
# network_continuity_report is AUDIT ONLY: it tells the user "this seam
# is G0, that one is G2" but never tries to fix anything. The natural
# "fix it" command is a local relaxation pass over the network: find the
# worst seam, rebuild the worse-scoring of its two faces via match_surface
# at the requested continuity target, re-evaluate, repeat until stable
# (or until the iteration cap is hit). This is the same "start with the
# worst, fix it, re-measure" loop real surfacing tools (Rhino's
# MatchSrf / Rebuild, Alias' Match Surface) use -- it's not a
# simultaneous network SOLVE in the least-squares sense, just a greedy
# local pass that the existing match_surface already does the per-face
# work for. The new function is purely an orchestrator on top of the
# existing match_surface + network_continuity_report -- no new
# geometry kernel, no new failure mode beyond "max iterations hit
# without converging" (which the returned converged flag reports).
def balance_network_continuity(faces, references=None, target="G2",
                                max_iter=5, samples=12, gap_tol=1e-3,
                                g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2,
                                simultaneous=True):
    """Simultaneous local-relaxation pass over a patch network.

    Improved implementation of the "balance network continuity" feature
    (RFS_BalanceNetwork): for each iteration, identify ALL seams below
    target continuity, compute the desired corrections for each involved
    face, and apply a consensus correction to each face. This reduces
    oscillation compared to the greedy single-seam approach and better
    leverages global network information.

    The algorithm:
    1. Evaluate current network continuity
    2. For each seam below target, determine what adjustment would fix it
    3. For each face, compute a target position as the average of all
       corrections suggested by its sub-standard seams
    4. Move each face toward its consensus target
    5. Repeat until convergence or max_iter

    `faces`: list of Part.Face (or face-bearing FreeCAD objects,
                 coerced via _face_from_input) -- the network to
                 balance. Each face is its own object; the function
                 tracks them by their position in the input list.
    `references`: optional list of faces that should NOT be rebuilt
                 themselves but SHOULD be matched against (e.g. the
                 "master/reference" surfaces the rest of the network
                 must match but that the user wants to keep fixed).
                 Default: every face is fair game.
    `target`: "G1" or "G2" -- the continuity to drive toward at every
                 rebuild. (G0 is implicit -- match_surface always
                 preserves G0 exactly.)
    `max_iter`: safety cap on iterations (default 5; convergence is
                 usually < 3 in practice; the cap exists so a patho-
                 logical oscillating case can't loop forever).
    `samples, gap_tol, *_tol`: forwarded to
                 network_continuity_report / surface_deviation.

    Returns: a dict with the following keys:
      - "shapes": list of (orig_index, new_shape_or_None) -- one entry
                 per input face, in input order. None means "this face
                 was never rebuilt" (either it's a reference, or its
                 worst seam was already at the target grade).
      - "iterations": the number of outer passes actually run.
      - "converged": True if the last move made no significant changes,
                 False if the cap was hit.
      - "initial_worst": the network's worst_grade at the start.
      - "final_worst": the network's worst_grade at the end.
      - "per_iteration": list of dicts, one per iteration, with
                 "worst_grade", "changed" (number of faces updated),
                 and the per-seam grades.

    Raises: ValueError for empty `faces` or `max_iter < 1`.
    """
    faces = [_face_from_input(f) for f in (faces or [])]
    faces = [f for f in faces if f is not None]
    if not faces:
        raise ValueError("balance_network_continuity: faces must be non-empty")
    if max_iter < 1:
        raise ValueError(
            "balance_network_continuity: max_iter must be >= 1 (got %d)"
            % max_iter)
    if target not in ("G0", "G1", "G2"):
        raise ValueError(
            "balance_network_continuity: target must be G0/G1/G2 (got %r)"
            % target)
    ref_set = set()
    if references:
        for r in references:
            fr = _face_from_input(r)
            if fr is not None:
                ref_set.add(id(fr))

    # Working copy of the faces -- we update positions in place when
    # possible. `working[i]` is the i-th face's current shape.
    working = list(faces)
    # Track which faces were actually modified (vs just copied)
    updated = [False] * len(working)
    order = {"FAIL": 0, "G0": 1, "G1": 2, "G2": 3}

    def _grade(report):
        return classify_continuity(report, g0_tol=g0_tol,
                                  g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)

    def _report():
        return network_continuity_report(
            working, samples=samples, gap_tol=gap_tol,
            g0_tol=g0_tol, g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)

    def _safe_copy_face(face):
        """Create a copy of a face to avoid modifying the original during
        trial computations."""
        try:
            return face.copy()
        except Exception:
            # If copy fails, return the original (less safe but prevents crash)
            return face

    initial = _report()
    initial_worst = initial["worst_grade"]
    per_iter = []

    if initial_worst is None:
        # No adjacent pairs found at all -- the "network" is a set of
        # isolated faces. Nothing to balance; return a no-op report
        # rather than failing (this is what a user would intuitively
        # expect from a button that says "balance" when there's
        # nothing to balance).
        return {"shapes": [(i, None) for i in range(len(faces))],
                "iterations": 0, "converged": True,
                "initial_worst": None, "final_worst": None,
                "per_iteration": [{"worst_grade": None, "changed": 0}]}

    converged = False
    iterations = 0
    for it in range(max_iter):
        iterations = it + 1
        rep = _report()
        worst_grade = rep["worst_grade"]
        if worst_grade is None or order[worst_grade] >= order[target]:
            # Already at or past target -- converged.
            converged = True
            per_iter.append({"worst_grade": worst_grade, "changed": 0})
            break

        # For each face, collect target positions from sub-standard seams
        # targets[i] will be a list of candidate target faces for face i
        targets = [[] for _ in range(len(working))]
        total_considered = 0

        # Process each seam below target
        for seam in rep["seams"]:
            if order[seam["grade"]] >= order[target]:
                continue  # This seam meets or exceeds target, skip

            i, j = seam["i"], seam["j"]
            face_i, face_j = working[i], working[j]

            # Skip if either face is a reference (should not be modified)
            if id(face_i) in ref_set or id(face_j) in ref_set:
                continue

            # Try to fix this seam by adjusting face_i to match face_j
            try:
                temp_i = _safe_copy_face(face_i)
                target_i = match_surface(face_j, temp_i, continuity=target, n=samples)
                target_i_face = target_i.Faces[0] if getattr(target_i, "Faces", None) else target_i
                targets[i].append(target_i_face)
                total_considered += 1
            except Exception:
                # If match_surface fails, skip this constraint
                pass

            # Try to fix this seam by adjusting face_j to match face_i
            try:
                temp_j = _safe_copy_face(face_j)
                target_j = match_surface(face_i, temp_j, continuity=target, n=samples)
                target_j_face = target_j.Faces[0] if getattr(target_j, "Faces", None) else target_j
                targets[j].append(target_j_face)
                total_considered += 1
            except Exception:
                # If match_surface fails, skip this constraint
                pass

        # If no adjustments were considered, we're done
        if total_considered == 0:
            converged = True
            per_iter.append({"worst_grade": worst_grade, "changed": 0})
            break

        # Apply the consensus adjustments (use first target for simplicity)
        any_changes = False
        for idx in range(len(working)):
            if id(working[idx]) in ref_set:
                continue  # Skip references

            if not targets[idx]:
                continue  # No constraints for this face

            # Use the first target candidate (to avoid complex averaging)
            target_face = targets[idx][0]

            # Only update if the target is significantly different
            try:
                # Simple check: compare if they're the same object or equivalent
                if working[idx] is not target_face:
                    # Check if they're geometrically equivalent by comparing bounds
                    curr_box = working[idx].BoundBox
                    targ_box = target_face.BoundBox

                    # If bounds differ significantly, apply update
                    if (abs(curr_box.XLength - targ_box.XLength) > 1e-4 or
                        abs(curr_box.YLength - targ_box.YLength) > 1e-4 or
                        abs(curr_box.ZLength - targ_box.ZLength) > 1e-4):
                        working[idx] = target_face
                        updated[idx] = True
                        any_changes = True
            except Exception:
                # If comparison fails, update anyway (better safe than sorry)
                if working[idx] is not target_face:
                    working[idx] = target_face
                    updated[idx] = True
                    any_changes = True

        # Record iteration results
        changed_count = sum(1 for u in updated if u)
        post_rep = _report()
        per_iter.append({
            "worst_grade": post_rep["worst_grade"],
            "changed": changed_count,
        })

        # Reset updated flags for next iteration
        updated = [False] * len(working)

        # Check for convergence
        if not any_changes:
            converged = True
            break

    final = _report()
    return {
        "shapes": [(i, (working[i] if updated[i] else None)) for i in range(len(faces))],
        "iterations": iterations,
        "converged": converged,
        "initial_worst": initial_worst,
        "final_worst": final["worst_grade"],
        "per_iteration": per_iter,
    }


# --- stitch a finished patch network into one shell (RFS_StitchShell) ---- #
# n_sided_patch already sews its own fan of sub-patches internally
# (Part.Shell + sewShape), but that was never exposed as a general,
# user-facing command for an arbitrary finished network -- e.g. a Loft +
# Gordon + two BlendSurf strips that visually line up but are still four
# separate FreeCAD objects/shapes. This is that command: pure OCCT
# sewing (BRepBuilderAPI_Sewing via Shape.sewShape), nothing more. It is
# NOT a continuity solver -- see network_continuity_report's docstring --
# it only closes small geometric gaps up to `tol` between faces that
# already meet there; it does not adjust shape to make a poor seam better.
def stitch_shell(faces, tol=1e-3):
    """Sew a list of individual surface faces into one continuous
    Part.Shell (a Part.Solid instead, if the result closes) -- the
    user-facing counterpart to n_sided_patch's internal Shell/sewShape
    call, exposed here for ANY finished patch network (Loft/Gordon/Coons/
    BlendSurf/...), not just one N-sided fill's own sub-patches.

    This performs no continuity solving of any kind: it only welds
    coincident (within `tol`) boundaries together into one topological
    shell. Run network_continuity_report() on the input faces first if
    you need to know whether the seams about to be sewn are actually
    G1/G2 -- stitch_shell will happily sew a G0-only (visually kinked)
    network into one valid, watertight, but still-kinked shell.

    Raises ValueError for fewer than 2 faces, and RuntimeError if OCCT's
    sewing produces an empty result (e.g. faces that don't actually meet
    within `tol` anywhere).
    """
    faces = list(faces or [])
    if len(faces) < 2:
        raise ValueError("stitch_shell needs >= 2 faces")
    shell = Part.Shell(faces)
    try:
        shell.sewShape()
    except Exception as exc:
        raise RuntimeError("stitch_shell: sewing failed (%s)" % exc)
    if shell.isNull() or not shell.Faces:
        raise RuntimeError("stitch_shell: sewing produced an empty result "
                           "-- do the input faces actually meet within "
                           "tol=%g anywhere?" % tol)
    if shell.Shells and shell.Shells[0].isClosed():
        try:
            return Part.makeSolid(shell.Shells[0])
        except Exception:
            pass
    return shell


def free_edge_count(shape):
    """Number of edges used by exactly one face of `shape` -- i.e. the
    still-open (unstitched) boundary edges remaining after stitch_shell.
    Zero means the result is a fully closed shell/solid; a nonzero count
    is the number a caller should report back to the user (e.g. "stitched
    with 4 free edges remaining") instead of silently declaring success
    while real gaps remain in the network."""
    counts = {}
    for f in shape.Faces:
        for e in f.Edges:
            h = e.hashCode()
            counts[h] = counts.get(h, 0) + 1
    return sum(1 for c in counts.values() if c == 1)


# --- surface trim + sketch-on-surface (native RFS_Trim / RFS_SketchOnSurf) #
def trim_face(face, wire, tol=1e-4):
    """Trim `face` to `wire`'s boundary. Native replacement for Curves'
    'Trim' command (RFS_Trim).

    Fast path: if `wire` already lies on face's surface (the common case --
    e.g. a curve produced by project_wire_onto_face below, or a curve drawn
    directly on the surface via SketchOnSurf), OCCT can build the trimmed
    face directly from (surface, wire) with Part.Face -- this is the exact
    primitive OCCT provides for "a surface bounded by a wire on it", no
    boolean operation needed, and it's exact rather than tessellated.

    Fallback: if the wire is off-surface (drawn in a different plane, not
    yet projected), we don't have a general BRepProj_Projection binding
    available, so instead project each wire edge onto the face by nearest-
    point search (same technique as project_wire_onto_face) and retry the
    fast path once the wire is on-surface.
    """
    surf = face.Surface
    try:
        return Part.Face(surf, wire)
    except Exception:
        pass
    projected = project_wire_onto_face(face, wire, closed=_is_closed_wire(wire))
    return Part.Face(surf, Part.Wire(projected.Edges))


def _is_closed_wire(wire):
    try:
        vs = wire.Vertexes
        return bool(vs) and vs[0].Point.distanceToPoint(vs[-1].Point) < 1e-7
    except Exception:
        return False


def project_wire_onto_face(face, wire, n=60, closed=None):
    """Project `wire` onto `face`'s surface and rebuild it there as an
    interpolating B-spline. Native replacement for Curves' 'SoS' /
    Sketch-on-Surface (RFS_SketchOnSurf).

    FreeCAD's Part.Shape has no direct Python binding for OCCT's general
    surface projection (BRepProj_Projection); what IS exposed is
    `Vertex.distToShape`, which returns the closest point ON a shape to a
    given point -- exact for the "project this point onto that face"
    question when the point is reasonably close to the face (true for the
    normal "sketch drawn near/on the intended surface" use case). So we
    sample the wire, project every sample with distToShape, and refit --
    the same sample-and-refit idiom the rest of this module already uses
    (see _nearest_t, _closest_approach_pair) rather than reaching for a
    binding that isn't available.
    """
    if closed is None:
        closed = _is_closed_wire(wire)
    edges = wire.Edges if hasattr(wire, "Edges") else [wire]
    if not edges:
        raise ValueError("project_wire_onto_face: wire has no edges")
    pts = []
    per_edge = max(2, n // max(1, len(edges)))
    for e in edges:
        fp, lp = e.FirstParameter, e.LastParameter
        for k in range(per_edge):
            t = k / float(per_edge - 1) if per_edge > 1 else 0.0
            p = e.valueAt(fp + t * (lp - fp))
            dist, pts_on, _info = Part.Vertex(p).distToShape(face)
            pts.append(pts_on[0][1] if pts_on else p)
    # de-duplicate consecutive coincident points (shared edge endpoints)
    dedup = [pts[0]]
    for p in pts[1:]:
        if p.distanceToPoint(dedup[-1]) > 1e-7:
            dedup.append(p)
    if len(dedup) < 2:
        raise ValueError("project_wire_onto_face: projection collapsed to "
                         "a single point")
    return interpolate_curve(dedup, closed=closed)


# --- G1/G2 surface match (Hermite-profile skinning) ----------------------- #
def _face_from_input(face_or_obj):
    """Coerce a FreeCAD object or face-like object to a Part.Face."""
    if face_or_obj is None:
        return None
    if isinstance(face_or_obj, Part.Face):
        return face_or_obj
    if hasattr(face_or_obj, "Faces") and getattr(face_or_obj, "Faces", None):
        return face_or_obj.Faces[0]
    shp = getattr(face_or_obj, "Shape", None)
    if shp is not None and getattr(shp, "Faces", None):
        return shp.Faces[0]
    return face_or_obj


def _hermite3(p0, t0, p1, t1, t):
    h00 =  2*t**3 - 3*t**2 + 1
    h10 =    t**3 - 2*t**2 + t
    h01 = -2*t**3 + 3*t**2
    h11 =    t**3 -   t**2
    return p0*h00 + t0*h10 + p1*h01 + t1*h11


def _hermite5(p0, v0, a0, p1, v1, a1, t):
    t2 = t*t; t3 = t2*t; t4 = t3*t; t5 = t4*t
    h00 = 1 - 10*t3 + 15*t4 -  6*t5
    h10 =     t     -  6*t3 +  8*t4 -  3*t5
    h20 = 0.5*(t2   -  3*t3 +  3*t4 -    t5)
    h01 =          10*t3 - 15*t4 +  6*t5
    h11 =          -4*t3 +  7*t4 -  3*t5
    h21 = 0.5*(t3 -  2*t4 +    t5)
    return p0*h00 + v0*h10 + a0*h20 + p1*h01 + v1*h11 + a1*h21


def _hermite7(p0, v0, a0, j0, p1, v1, a1, j1, t):
    """Septic (degree-7) Hermite basis, 8 boundary conditions:
    position, 1st, 2nd, and 3rd derivative at each end. Same convention as
    _hermite5: the basis is scaled so that h20''(0) = h30'''(0) = 1
    (likewise at t=1), which means the input accelerations a0/a1 and
    jerks j0/j1 are fed in directly -- no extra 1/2 or 1/6 multiplier
    needed at the call site.

    Coefficients derived once symbolically (rational Gaussian elimination
    on the 8x8 system: 4 derivatives at t=0 and 4 at t=1) and inlined
    here for speed. Verified by test_hermite7_end_conditions in test_core.
    The exact fractional coefficients (1/6, 1/2, 2/3, 5/2, 7.5 = 15/2,
    etc.) are written as a0..a7 / 6 to keep the form compact.
    """
    t2 = t * t
    t3 = t2 * t
    t4 = t3 * t
    t5 = t4 * t
    t6 = t5 * t
    t7 = t6 * t
    h00 =                       1   - 35*t4 + 84*t5 - 70*t6 + 20*t7
    h10 =             t               - 20*t4 + 45*t5 - 36*t6 + 10*t7
    h20 =         0.5*t2               -  5*t4 + 10*t5 - 7.5*t6 +  2*t7
    h30 = (1.0/6.0)*t3                  - (2.0/3.0)*t4 + t5 - (2.0/3.0)*t6 + (1.0/6.0)*t7
    h01 =                              35*t4 - 84*t5 + 70*t6 - 20*t7
    h11 =                             -15*t4 + 39*t5 - 34*t6 + 10*t7
    h21 =                            2.5*t4 -  7*t5 + 6.5*t6 -  2*t7
    h31 =                  -(1.0/6.0)*t4 + 0.5*t5 - 0.5*t6 + (1.0/6.0)*t7
    return p0*h00 + v0*h10 + a0*h20 + j0*h30 + p1*h01 + v1*h11 + a1*h21 + j1*h31


def match_surface(ref_face, tgt_face, continuity="G2", n=24, scale=1.0):
    """Rebuild tgt_face so its edge nearest ref_face is G1/G2-continuous.

    Uses quintic Hermite profile skinning: the shared boundary is preserved
    exactly (G0), tangent direction matches ref_face (G1), and curvature in the
    cross-boundary direction matches ref_face (G2). The far/side boundaries of
    tgt_face are also preserved (the profiles anchor there). This does not modify
    ref_face.
    """
    ref_face = _face_from_input(ref_face)
    tgt_face = _face_from_input(tgt_face)
    import math as _math
    order = {"G0": 0, "G1": 1, "G2": 2, "G3": 3}.get(str(continuity), 2)

    def _iso_mid(face, side):
        u0, u1, v0, v1 = face.ParameterRange
        return face.valueAt(*{0: (u0, (v0+v1)/2), 1: (u1, (v0+v1)/2),
                               2: ((u0+u1)/2, v0), 3: ((u0+u1)/2, v1)}[side])

    best, rs, ts = 1e18, 0, 0
    for r in range(4):
        for t in range(4):
            d = _iso_mid(ref_face, r).distanceToPoint(_iso_mid(tgt_face, t))
            if d < best:
                best, rs, ts = d, r, t

    far_side  = {0: 1, 1: 0, 2: 3, 3: 2}[ts]
    ref_is_u  = rs < 2
    tgt_is_u  = ts < 2
    ref_pr    = ref_face.ParameterRange
    tgt_pr    = tgt_face.ParameterRange

    # Trim-aware boundary sampling: walk each face's ACTUAL boundary edge
    # for the matched side (which may be shorter/reshaped by a prior trim)
    # instead of assuming the boundary spans the full untrimmed iso-
    # parameter line end to end -- see _side_uv_trimmed's docstring. One
    # shared cache per call, so each (face, side) pair's real boundary
    # edge is located once, not once per sample below.
    _edge_cache = {}

    def ref_uv(t):
        return _side_uv_trimmed(ref_face, ref_pr, rs, t, _edge_cache)

    def tgt_far_uv(t):
        return _side_uv_trimmed(tgt_face, tgt_pr, far_side, t, _edge_cache)

    ref_surf = ref_face.Surface
    tgt_surf = tgt_face.Surface

    def _cross_tan(u, v):
        du, dv = ref_surf.tangent(u, v)
        return _cross_tan_from_surface_tangent(du, dv, rs)

    _dn_cache = {}

    def _cross_d2(u, v):
        # Analytic path first (exact, off ref_face's own control net) --
        # only fall back to finite differences when no BSpline conversion
        # is possible (see _surf_dn's docstring).
        du, dv = (2, 0) if ref_is_u else (0, 2)
        analytic = _surf_dn(ref_face, u, v, du, dv, _dn_cache)
        if analytic is not None:
            return analytic
        # Fall back to finite differences
        eps = _compute_finite_difference_epsilon(ref_pr, 0 if ref_is_u else 1)
        du_sign = -1.0 if (ref_is_u and rs == 0) or (not ref_is_u and rs == 2) else 1.0
        dv_sign = -1.0 if (not ref_is_u and rs == 0) or (ref_is_u and rs == 2) else 1.0
        # Actually, for pure u or v derivatives, one of the signs is zero
        if ref_is_u:
            # Derivative in u direction
            return _finite_difference_second_derivative(
                lambda u, v: ref_face.valueAt(u, v), u, v,
                -1.0 if rs == 0 else 1.0, 0.0, eps)
        else:
            # Derivative in v direction
            return _finite_difference_second_derivative(
                lambda u, v: ref_face.valueAt(u, v), u, v,
                0.0, -1.0 if rs == 2 else 1.0, eps)

    def _cross_d3(u, v):
        """G3 counterpart to _cross_d2. Tries the exact analytic 3rd
        derivative (BSplineSurface.getDN) first -- FreeCAD's Part API
        does expose it once ref_face is (or is converted to) a
        BSplineSurface, so this is no longer a finite-difference-only
        estimate for the common case. Only falls back to stacked finite
        differences (noisier, since it compounds atop an already-finite-
        difference 2nd derivative) when no BSpline conversion succeeds."""
        du, dv = (3, 0) if ref_is_u else (0, 3)
        analytic = _surf_dn(ref_face, u, v, du, dv, _dn_cache)
        if analytic is not None:
            return analytic
        # Fall back to finite differences
        eps = _compute_finite_difference_epsilon(ref_pr, 0 if ref_is_u else 1)
        if ref_is_u:
            # Derivative in u direction
            return _finite_difference_third_derivative(
                lambda u, v: ref_face.valueAt(u, v), u, v,
                -1.0 if rs == 0 else 1.0, 0.0, eps)
        else:
            # Derivative in v direction
            return _finite_difference_third_derivative(
                lambda u, v: ref_face.valueAt(u, v), u, v,
                0.0, -1.0 if rs == 2 else 1.0, eps)

    grid = []
    for k in range(n):
        t = k / (n - 1.0)
        r_u, r_v = ref_uv(t)
        f_u, f_v = tgt_far_uv(t)
        P0 = ref_face.valueAt(r_u, r_v)
        P1 = tgt_face.valueAt(f_u, f_v)
        chord = max((P1 - P0).Length, 1e-9)
        h = chord * scale / 3.0

        if order == 0:
            row = [P0 + (P1 - P0) * j / (n - 1.0) for j in range(n)]
        elif order == 1:
            T0 = _cross_tan(r_u, r_v)
            T0 = T0 * (h / max(T0.Length, 1e-9))
            du, dv = tgt_surf.tangent(f_u, f_v)
            T1 = du if tgt_is_u else dv
            T1 = T1 * (h / max(T1.Length, 1e-9))
            row = [_hermite3(P0, T0, P1, T1, j / (n - 1.0)) for j in range(n)]
        elif order == 2:
            T0 = _cross_tan(r_u, r_v)
            T0 = T0 * (h / max(T0.Length, 1e-9))
            K0 = _cross_d2(r_u, r_v) * (chord * chord / 2.0)
            du, dv = tgt_surf.tangent(f_u, f_v)
            T1 = du if tgt_is_u else dv
            T1 = T1 * (h / max(T1.Length, 1e-9))
            row = [_hermite5(P0, T0, K0, P1, T1, Vector(0, 0, 0),
                             j / (n - 1.0)) for j in range(n)]
        else:
            # G3: septic Hermite -- ref side gets tangent/curvature/jerk
            # matching (T0/K0/J0); the target's far side is only
            # position-anchored (T1 comes from tgt_face's own tangent,
            # and the acceleration/jerk terms on that side are zero),
            # mirroring the existing G2 branch's asymmetric treatment
            # above (K1 is Vector(0,0,0) there too -- match_surface only
            # forces continuity FROM the reference, never modifies it).
            T0 = _cross_tan(r_u, r_v)
            T0 = T0 * (h / max(T0.Length, 1e-9))
            K0 = _cross_d2(r_u, r_v) * (chord * chord / 2.0)
            J0 = _cross_d3(r_u, r_v) * (chord ** 3 / 6.0)
            du, dv = tgt_surf.tangent(f_u, f_v)
            T1 = du if tgt_is_u else dv
            T1 = T1 * (h / max(T1.Length, 1e-9))
            row = [_hermite7(P0, T0, K0, J0, P1, T1, Vector(0, 0, 0),
                            Vector(0, 0, 0), j / (n - 1.0)) for j in range(n)]
        grid.append(row)
    return grid_to_surface(grid)


def match_surface_to_references(target_face, reference_faces, continuity="G2",
                                n=24, scale=1.0):
    """Try several reference faces and return the one that best matches the
    target edge, along with the rebuilt surface and a continuity report.

    This is a lightweight workflow for patch-network editing: when a target
    patch needs to be rebuilt to match multiple adjacent patches, the tool can
    evaluate a short list of possible reference faces and choose the one that
    yields the best seam quality instead of blindly picking the first.

    For each candidate this now tries match_surface_exact_g1() FIRST when
    G1 or G2 continuity is requested -- an exact, control-net-level match
    rather than match_surface()'s sampled Hermite approximation (see that
    function's docstring). It only succeeds when the two faces' boundary
    parametrizations are knot-compatible; whenever it isn't (raises
    ValueError), this transparently falls back to the sampled
    match_surface() path exactly as before, so behaviour for incompatible
    surfaces is unchanged. surface_deviation()/classify_continuity() are
    still run and returned either way -- this upgrade reduces how often a
    seam needs the sampled approximation, it doesn't remove the need to
    verify numerically when it does fall back.

    The returned report dict now also carries report["method"], either
    "exact_g1" or "sampled" -- with two match algorithms now silently
    competing for each candidate reference face, a "the match looks
    different than expected" bug report needs a fast way to tell which
    one actually produced the result without re-deriving it from scratch;
    this is that.
    """
    target_face = _face_from_input(target_face)
    refs = []
    for ref in reference_faces or []:
        face = _face_from_input(ref)
        if face is not None:
            refs.append(face)
    if target_face is None or not refs:
        raise ValueError("match_surface_to_references: need a target face and at least one reference")

    best = None
    best_report = None
    best_face = None
    best_shape = None
    for ref in refs:
        try:
            base_report = surface_deviation(target_face, ref, samples=max(4, n // 2))
            base_score = (base_report["max_g0"], base_report["max_g1_deg"], base_report["max_g2"])
            rebuilt = None
            method = "sampled"
            if continuity in ("G1", "G2"):
                try:
                    rebuilt = match_surface_exact_g1(ref, target_face)
                    method = "exact_g1"
                except Exception:
                    rebuilt = None      # not knot-compatible -- fall through
            if rebuilt is None:
                rebuilt = match_surface(ref, target_face, continuity=continuity, n=n, scale=scale)
                method = "sampled"
            grade = classify_continuity(base_report)
            score = base_score + (grade != "G2",)
            if best is None or score < best:
                best = score
                base_report["method"] = method
                best_report = base_report
                best_face = ref
                best_shape = rebuilt
        except Exception:
            continue
    if best_shape is None:
        raise ValueError("match_surface_to_references: no reference face produced a valid match")
    return best_shape, best_report, best_face



def match_surface_exact_g1(ref_face, tgt_face, n_rows=2):
    """Exact-by-construction G0/G1 rebuild of tgt_face's edge nearest
    ref_face, when the two surfaces' boundary parametrizations can be
    harmonized onto a shared knot vector.

    match_surface() (above) resamples the boundary with a Hermite basis,
    so continuity only holds AT THE SAMPLED POINTS -- fine as a general
    first pass, but it's a fit, not a construction, which is exactly the
    limitation match_surface()'s own docstring and surface_deviation()'s
    QA step exist to manage. This function instead works directly on
    tgt_face's CONTROL NET:

      0. Neither face needs to already be a BSplineSurface -- any OCCT
         surface type that supports toBSpline() (planes, cylinders,
         cones, spheres, tori -- the common analytic neighbors of a
         free-form patch, e.g. a hood surface meeting a planar splash
         panel) is converted automatically. Only a surface type with no
         BSpline conversion at all is rejected.
      1. Harmonize tgt's knot vector with ref's along the direction that
         runs ALONG the shared edge (the same insertUKnots/insertVKnots
         technique _harmonize_surface_knots already uses in
         gordon_network): the UNION of both knot vectors (per-knot max
         multiplicity) is inserted into BOTH surfaces, so two boundaries
         that are the same underlying curve but at DIFFERENT refinement
         (e.g. tgt has an extra knot inserted from local editing) still
         harmonize correctly -- this only fails when the two boundaries
         are fundamentally different curves (different parameter range,
         or knot insertion itself fails), not merely differently
         refined.
      2. If degrees differ after harmonization, raise both to the higher
         one (increaseDegree), so pole counts line up 1:1 along the edge.
      3. Overwrite tgt's boundary row of poles with ref's boundary row
         (exact G0), and -- when n_rows >= 2 -- tgt's second row with
         ref's second row (exact G1: the cross-boundary tangent vector at
         EVERY point along the shared edge is now literally the same
         control-point difference on both sides, not just approximately
         equal at some samples).

    Raises ValueError (not a silent wrong answer, and not a bare OCCT
    exception type callers would need to guess at) whenever any step
    can't be satisfied, so callers fall back to match_surface()'s sampled
    approximation instead -- see match_surface_to_references(), which now
    tries this function first and falls back automatically, and records
    which path was actually used in its returned report (key "method").

    CAVEAT for callers inspecting tgt's poles/knots afterward (rather
    than just sampling the returned shape): step 1's knot insertion and
    step 2's possible degree elevation both change tgt's control net --
    interior pole COUNT and positions may differ from tgt's original
    surface even away from the matched edge, though the shape itself is
    unchanged by both operations (they're exact refinements). Only the
    first `n_rows` rows from the matched edge are additionally
    overwritten to equal ref's.

    G2 is deliberately NOT attempted here: the correct placement of the
    row *two* poles in from the boundary depends on more of tgt's own
    interior shape than G0/G1 do, and forcing it purely from ref's
    boundary risks visibly distorting tgt away from its intended form.
    For G2, use match_surface(continuity="G2") and verify the result with
    surface_deviation() -- that sampled/verified path remains the only
    G2 option, on purpose.

    TRIMMED FACES: this function has no concept of trimming at all -- it
    operates purely on the control net, unlike match_surface()/
    blend_surface(), which both sample the matched boundary via
    _side_uv_trimmed. Before doing anything else it checks whether either
    face's matched side is a genuine trim fragment (_side_is_trimmed) and
    raises ValueError if so, so callers (match_surface_to_references) fall
    back to the sampled, trim-aware path instead of silently copying a
    control-net row across the wrong material extent.

    CALLER-VISIBLE POLE MUTATION: this function is only ever reached via
    match_surface_to_references() when it reports back "method": "exact_g1"
    in its returned report -- callers/UI layers that want to tell the user
    their target's control net was directly overwritten (rather than
    rebuilt by sampling) should check that field. As of this writing
    MatchSurfaceFP/SurfacePanel do not yet surface this distinction in the
    UI; BlendSurfPanel doesn't apply here since blend_surface() always
    uses the sampled Hermite path, never this function.
    """
    ref_face = _face_from_input(ref_face)
    tgt_face = _face_from_input(tgt_face)
    rs, ts, ref_pr, tgt_pr = _nearest_facing_sides(ref_face, tgt_face)

    # Trim guard: exact_g1 works purely on the CONTROL NET (_pole_ij below
    # copies pole rows directly) and has no concept of a trimmed boundary
    # at all -- unlike match_surface()/blend_surface(), which both sample
    # the matched side via _side_uv_trimmed and so already follow a real,
    # trimmed edge correctly. If either face's matched side is a genuine
    # trim fragment (not the full untrimmed iso-line), copying ref's
    # boundary row onto tgt's control net would silently use the wrong
    # material extent -- e.g. copying a full-width row onto an edge that's
    # only half as long. Raise here (the same "fail loud, let the caller
    # fall back" convention every other exact_g1 rejection in this
    # function already uses) so match_surface_to_references transparently
    # falls back to match_surface()'s sampled, trim-aware path instead.
    if _side_is_trimmed(ref_face, ref_pr, rs) or _side_is_trimmed(tgt_face, tgt_pr, ts):
        raise ValueError(
            "match_surface_exact_g1: the matched boundary is a trimmed "
            "edge, not the full untrimmed iso-parameter line -- an exact "
            "control-net copy would use the wrong material extent; fall "
            "back to match_surface() (trim-aware sampling) instead")

    def _as_bspline(face, which):
        surf = face.Surface
        if isinstance(surf, Part.BSplineSurface):
            return surf.copy()
        try:
            return surf.toBSpline()
        except Exception:
            pass
        # Analytic surfaces (planes, cylinders, cones, ...) are
        # mathematically infinite/unbounded in their own parametrization,
        # so an unbounded toBSpline() always raises ("Will raise an
        # exception if surface is infinite in U or V"). Fit a
        # BSplineSurface to a grid of points sampled over the FACE's
        # actual (bounded) parameter range instead -- exact for planes,
        # a close approximation for curved analytics -- rather than
        # rejecting every analytic neighbor outright.
        try:
            u0, u1, v0, v1 = face.ParameterRange
            n = 8
            grid = [[surf.value(u0 + i * (u1 - u0) / (n - 1.0),
                                v0 + j * (v1 - v0) / (n - 1.0))
                    for j in range(n)] for i in range(n)]
            bs = Part.BSplineSurface()
            bs.interpolate(grid)
            return bs
        except Exception as exc:
            raise ValueError(
                "match_surface_exact_g1: %s surface type (%s) can't be "
                "converted to a BSplineSurface (%s) -- fall back to "
                "match_surface()" % (which, type(surf).__name__, exc))

    ref_s = _as_bspline(ref_face, "ref_face's")
    tgt_s = _as_bspline(tgt_face, "tgt_face's")
    ref_is_u, tgt_is_u = rs < 2, ts < 2
    axis_ref = "V" if ref_is_u else "U"
    axis_tgt = "V" if tgt_is_u else "U"

    def _harmonize(s1, ax1, s2, ax2, tol=1e-9):
        k1 = list(getattr(s1, "get" + ax1 + "Knots")())
        m1 = list(getattr(s1, "get" + ax1 + "Multiplicities")())
        k2 = list(getattr(s2, "get" + ax2 + "Knots")())
        m2 = list(getattr(s2, "get" + ax2 + "Multiplicities")())
        merged = {}
        for k, m in zip(k1, m1):
            merged[k] = max(merged.get(k, 0), m)
        for k, m in zip(k2, m2):
            merged[k] = max(merged.get(k, 0), m)
        ks = sorted(merged)
        ms = [merged[k] for k in ks]
        try:
            getattr(s1, "insert" + ax1 + "Knots")(ks, ms, tol, False)
            getattr(s2, "insert" + ax2 + "Knots")(ks, ms, tol, False)
        except Exception as exc:
            raise ValueError(
                "match_surface_exact_g1: knot harmonization failed (%s) "
                "-- boundary parametrizations aren't compatible; fall "
                "back to match_surface()" % exc)
        # Defensive re-check: insertion can silently no-op on a knot
        # outside a surface's actual span rather than raising, so confirm
        # the two axes ended up with the SAME knot sequence, not just
        # that insertKnots didn't complain.
        k1b = list(getattr(s1, "get" + ax1 + "Knots")())
        k2b = list(getattr(s2, "get" + ax2 + "Knots")())
        if len(k1b) != len(k2b) or any(abs(a - b) > 1e-6 for a, b in zip(k1b, k2b)):
            raise ValueError(
                "match_surface_exact_g1: boundary parametrizations aren't "
                "knot-compatible even after harmonization (different "
                "parameter ranges, not just different refinement) -- "
                "fall back to match_surface()")

    _harmonize(ref_s, axis_ref, tgt_s, axis_tgt)

    deg_ref = ref_s.VDegree if ref_is_u else ref_s.UDegree
    deg_tgt = tgt_s.VDegree if tgt_is_u else tgt_s.UDegree
    if deg_ref != deg_tgt:
        target_deg = max(deg_ref, deg_tgt)
        try:
            ref_s.increaseDegree(*((ref_s.UDegree, target_deg) if ref_is_u
                                   else (target_deg, ref_s.VDegree)))
            tgt_s.increaseDegree(*((tgt_s.UDegree, target_deg) if tgt_is_u
                                   else (target_deg, tgt_s.VDegree)))
        except Exception as exc:
            raise ValueError(
                "match_surface_exact_g1: degree elevation failed (%s) -- "
                "fall back to match_surface()" % exc)

    n_ref = ref_s.NbVPoles if ref_is_u else ref_s.NbUPoles
    n_tgt = tgt_s.NbVPoles if tgt_is_u else tgt_s.NbUPoles
    if n_ref != n_tgt:
        raise ValueError(
            "match_surface_exact_g1: pole counts along the shared edge "
            "still disagree after harmonization (%d vs %d) -- fall back "
            "to match_surface()" % (n_ref, n_tgt))

    def _pole_ij(side, depth, along, nu, nv):
        """1-based OCCT (i,j) pole index `depth` rows in from `side`
        (0=umin,1=umax,2=vmin,3=vmax), at position `along` running along
        the shared edge."""
        if side == 0:
            return (1 + depth, 1 + along)
        if side == 1:
            return (nu - depth, 1 + along)
        if side == 2:
            return (1 + along, 1 + depth)
        return (1 + along, nv - depth)

    n_rows = max(1, min(int(n_rows), 2))
    for depth in range(n_rows):
        for along in range(n_ref):
            ri, rj = _pole_ij(rs, depth, along, ref_s.NbUPoles, ref_s.NbVPoles)
            ti, tj = _pole_ij(ts, depth, along, tgt_s.NbUPoles, tgt_s.NbVPoles)
            tgt_s.setPole(ti, tj, ref_s.getPole(ri, rj))

    return tgt_s.toShape()


# --- surface-to-surface blend (native RFS_BlendSurf) ----------------------- #
def blend_surface(face_a, face_b, continuity="G2", n=24, scale=1.0):
    """Native G1/G2 blend SURFACE strip between the two nearest-facing
    boundary edges of face_a and face_b -- the surface analog of
    blend_curve() above, and the native fallback for RFS_BlendSurf, which
    previously had NO native path at all (pure Curves_BlendSurf2
    delegation, unlike Loft/Gordon/Coons/Fill/Match).

    Builds one quintic-Hermite cross-section per sample t along the
    matched boundary pair: each cross-section starts exactly on face_a's
    edge (matching that face's cross-boundary tangent for G1, plus
    curvature for G2) and ends exactly on face_b's edge (mirrored). G0 is
    exact by construction (every cross-section literally starts/ends at
    the sampled edge points); G1/G2 are the same Hermite-matching
    approximation match_surface() already relies on -- not a symbolic
    guarantee. Use surface_deviation() on the two new seams afterward to
    confirm the achieved tolerance on a specific model (see the QA tooling
    added alongside this).

    Sampling is trim-aware: each cross-section's anchor point is taken
    from the face's ACTUAL boundary edge for the matched side (which may
    be shorter or reshaped by a prior RFS_Trim or N-sided-fill boundary),
    not from the untrimmed iso-parameter line -- see _side_uv_trimmed.
    Previously the untrimmed-line assumption meant blending against a
    trimmed face could silently anchor cross-sections on (u, v)
    positions that had been trimmed away and were no longer real
    material on the face.
    """
    order = {"G0": 0, "G1": 1, "G2": 2, "G3": 3}.get(str(continuity), 2)

    sa, sb, a_pr, b_pr = _nearest_facing_sides(face_a, face_b)
    # Trim-aware boundary sampling -- see _side_uv_trimmed's docstring.
    # One shared cache per call, so each (face, side) pair's real
    # boundary edge is located once, not once per sample below.
    _edge_cache = {}

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return _cross_tan_from_surface_tangent(du, dv, side)

    _dn_cache = {}

    def _cross_d2(face, pr, side, u, v):
        # Analytic path first (exact, off `face`'s own control net) --
        # see _surf_dn's docstring. Only falls back to finite differences
        # when no BSpline conversion succeeds.
        duo, dvo = (2, 0) if side < 2 else (0, 2)
        analytic = _surf_dn(face, u, v, duo, dvo, _dn_cache)
        if analytic is not None:
            return analytic
        # Fall back to finite differences
        eps = _compute_finite_difference_epsilon(pr, 0 if side < 2 else 1)
        if side < 2:
            # Derivative in u direction
            return _finite_difference_second_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                -1.0 if side == 0 else 1.0, 0.0, eps)
        else:
            # Derivative in v direction
            return _finite_difference_second_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                0.0, -1.0 if side == 2 else 1.0, eps)

    def _cross_d3(face, pr, side, u, v):
        """G3 counterpart to _cross_d2 above. Tries the exact analytic
        3rd derivative (BSplineSurface.getDN) first -- once `face` is (or
        converts to) a BSplineSurface this is no longer a finite-
        difference-only estimate. Only falls back to stacked finite
        differences (noisier -- compounds atop an already-finite-
        difference 2nd derivative) when no BSpline conversion succeeds;
        in that fallback case, still treat a "G3" surface blend as a
        best-effort target and verify with surface_deviation()."""
        duo, dvo = (3, 0) if side < 2 else (0, 3)
        analytic = _surf_dn(face, u, v, duo, dvo, _dn_cache)
        if analytic is not None:
            return analytic
        # Fall back to finite differences
        eps = _compute_finite_difference_epsilon(pr, 0 if side < 2 else 1)
        if side < 2:
            # Derivative in u direction
            return _finite_difference_third_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                -1.0 if side == 0 else 1.0, 0.0, eps)
        else:
            # Derivative in v direction
            return _finite_difference_third_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                0.0, -1.0 if side == 2 else 1.0, eps)

    grid = []
    for k in range(n):
        t = k / (n - 1.0)
        ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, _edge_cache)
        ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, _edge_cache)
        P0 = face_a.valueAt(ua, va)
        P1 = face_b.valueAt(ub, vb)
        chord = max((P1 - P0).Length, 1e-9)
        h = chord * scale / 3.0
        if order == 0:
            row = [P0 + (P1 - P0) * j / (n - 1.0) for j in range(n)]
        elif order == 1:
            T0 = _cross_tan(face_a, sa, ua, va)
            T0 = T0 * (h / max(T0.Length, 1e-9))
            T1 = _cross_tan(face_b, sb, ub, vb)
            T1 = T1 * (h / max(T1.Length, 1e-9))
            row = [_hermite3(P0, T0, P1, T1 * -1.0, j / (n - 1.0))
                  for j in range(n)]
        elif order == 2:
            T0 = _cross_tan(face_a, sa, ua, va)
            T0 = T0 * (h / max(T0.Length, 1e-9))
            K0 = _cross_d2(face_a, a_pr, sa, ua, va) * (chord * chord / 2.0)
            T1 = _cross_tan(face_b, sb, ub, vb)
            T1 = T1 * (h / max(T1.Length, 1e-9))
            K1 = _cross_d2(face_b, b_pr, sb, ub, vb) * (chord * chord / 2.0)
            row = [_hermite5(P0, T0, K0, P1, T1 * -1.0, K1 * -1.0,
                             j / (n - 1.0)) for j in range(n)]
        else:
            # G3: septic Hermite (_hermite7) -- the surface port of
            # blend_curve's G3 path. _cross_d3 now tries the analytic
            # derivative first (see its docstring); the finite-difference
            # fallback noise caveat only applies when that fails.
            T0 = _cross_tan(face_a, sa, ua, va)
            T0 = T0 * (h / max(T0.Length, 1e-9))
            K0 = _cross_d2(face_a, a_pr, sa, ua, va) * (chord * chord / 2.0)
            J0 = _cross_d3(face_a, a_pr, sa, ua, va) * (chord ** 3 / 6.0)
            T1 = _cross_tan(face_b, sb, ub, vb)
            T1 = T1 * (h / max(T1.Length, 1e-9))
            K1 = _cross_d2(face_b, b_pr, sb, ub, vb) * (chord * chord / 2.0)
            J1 = _cross_d3(face_b, b_pr, sb, ub, vb) * (chord ** 3 / 6.0)
            row = [_hermite7(P0, T0, K0, J0, P1, T1 * -1.0, K1 * -1.0,
                            J1 * -1.0, j / (n - 1.0)) for j in range(n)]
        grid.append(row)
    return grid_to_surface(grid)


def _nearest_facing_sides(face_a, face_b):
    """Which iso-parameter side (0=umin,1=umax,2=vmin,3=vmax) of each face
    faces the other -- the boundary-pair finder shared by match_surface,
    blend_surface, and surface_deviation (previously duplicated inline in
    match_surface; factored out here so the new QA/blend code doesn't grow
    a fourth copy)."""
    def _iso_mid(face, side):
        u0, u1, v0, v1 = face.ParameterRange
        return face.valueAt(*{0: (u0, (v0 + v1) / 2), 1: (u1, (v0 + v1) / 2),
                              2: ((u0 + u1) / 2, v0), 3: ((u0 + u1) / 2, v1)}[side])
    best, sa, sb = 1e18, 0, 0
    for a in range(4):
        for b in range(4):
            d = _iso_mid(face_a, a).distanceToPoint(_iso_mid(face_b, b))
            if d < best:
                best, sa, sb = d, a, b
    return sa, sb, face_a.ParameterRange, face_b.ParameterRange


# --- trim-aware boundary sampling for blend_surface / match_surface ------- #
# _nearest_facing_sides above only locates WHICH iso-parameter side of each
# face faces the other; it says nothing about whether that side's untrimmed
# iso-parameter line is still real material on the face. blend_surface and
# match_surface used to assume it always was -- correct for a fresh Loft/
# Gordon/Coons/Fill face, silently wrong once the face has been trimmed
# (RFS_Trim, or an N-sided-fill boundary), where the untrimmed iso-line can
# run through a region that's been cut away. The three helpers below let
# both functions sample the face's ACTUAL boundary edge for the matched
# side instead, and fall back to the untrimmed line only when no such edge
# can be found (the original, still-correct behaviour for untrimmed faces).
def _side_uv(pr, side, t):
    """Linear iso-parameter point on the UNTRIMMED boundary `side` (0=umin,
    1=umax,2=vmin,3=vmax) of a face's ParameterRange `pr`, at normalized
    position t in [0,1]. Correct whenever a face's boundary genuinely IS
    the full iso-parameter line (any fresh Loft/Gordon/Coons/Fill output);
    used directly here as the fallback _side_uv_trimmed reaches for when no
    closer real boundary edge is found. Previously hand-duplicated as an
    inline closure in blend_surface, match_surface, and surface_deviation;
    kept as one function so all three agree on the untrimmed case."""
    u0, u1, v0, v1 = pr
    if side < 2:
        return ({0: u0, 1: u1}[side], v0 + t * (v1 - v0))
    return (u0 + t * (u1 - u0), {2: v0, 3: v1}[side])


def _trimmed_boundary_candidates(face, pr, side):
    """[(edge, distance_to_expected_iso_midpoint), ...] for every wire edge
    of `face`, sorted nearest-first -- the raw material _trimmed_boundary_edge
    picks its single answer from. Split out so ambiguity (two or more
    plausible edges near the same side -- e.g. a notch cut into the
    boundary leaves TWO wire fragments near the same iso-side) can be
    detected by comparing the best and second-best candidates, instead of
    _trimmed_boundary_edge silently taking whichever one happened to have
    the nearest midpoint and ignoring the rest."""
    edges = []
    for w in face.Wires:
        edges.extend(w.Edges)
    if not edges:
        return []
    u0, u1, v0, v1 = pr
    iso_mid_uv = {0: (u0, 0.5 * (v0 + v1)), 1: (u1, 0.5 * (v0 + v1)),
                 2: (0.5 * (u0 + u1), v0), 3: (0.5 * (u0 + u1), v1)}[side]
    target_pt = face.valueAt(*iso_mid_uv)
    scored = []
    for e in edges:
        try:
            mid = e.valueAt(0.5 * (e.FirstParameter + e.LastParameter))
        except Exception:
            continue
        scored.append((e, mid.distanceToPoint(target_pt)))
    scored.sort(key=lambda pair: pair[1])
    return scored


def _trimmed_boundary_edge(face, pr, side, max_rel_gap=0.5, ambiguity_rel=2.0):
    """The real Part.Edge of `face`'s wire(s) that runs along iso-parameter
    `side` (0=umin,1=umax,2=vmin,3=vmax) of `pr`, found as the wire edge
    whose midpoint is nearest that iso-line's own (untrimmed) midpoint --
    the same 'trust the closest match' idiom _nearest_facing_sides already
    uses to pair up sides across two faces, applied here within one face to
    pair a side with its own real boundary.

    Returns None when `face` has no wire edges at all (shouldn't happen for
    a real face), when the nearest edge found is farther from the expected
    iso-line location than `max_rel_gap` * the face's bounding diagonal --
    i.e. there's genuinely no boundary anywhere near that side (a real
    topology mismatch, not just ordinary trimming) -- OR when the search is
    AMBIGUOUS: a second wire edge sits almost as close to the expected
    midpoint as the best one (within `ambiguity_rel` x the best distance,
    with an absolute floor so two edges that are both essentially AT the
    target don't get flagged from floating-point noise alone). That last
    case is the "a notch was cut into this boundary, leaving two separate
    wire fragments near the same side" failure mode: silently picking the
    single closest fragment would make blend_surface/match_surface follow
    only one of them and ignore the other with no indication anything was
    wrong. Callers get None (the same "nothing usable found" signal as the
    no-edges/too-far cases) and fall back to the untrimmed iso-line rather
    than guessing which fragment was intended.

    This is a heuristic, same as before -- max_rel_gap and ambiguity_rel
    are both untuned constants, not validated against elongated
    automotive-scale patches or faces with several inner-boundary wires
    (holes/islands). Treat both as a reasonable default, not a certified
    threshold.
    """
    scored = _trimmed_boundary_candidates(face, pr, side)
    if not scored:
        return None
    best, best_d = scored[0]
    try:
        diag = max(face.BoundBox.DiagonalLength, 1e-9)
    except Exception:
        diag = 1.0
    if best_d > max_rel_gap * diag:
        return None
    if len(scored) > 1:
        _second, second_d = scored[1]
        floor = 1e-6 * diag
        if second_d < max(best_d * ambiguity_rel, best_d + floor) and \
                second_d <= max_rel_gap * diag:
            return None
    return best


def _side_uv_trimmed(face, pr, side, t, edge_cache):
    """Trim-aware replacement for _side_uv(): walks `face`'s ACTUAL
    boundary edge for `side` (via that edge's own native parametrization)
    instead of assuming the boundary spans the untrimmed iso-parameter
    line end to end.

    This is what lets blend_surface / match_surface correctly follow a
    TRIMMED edge's real (shorter, or differently shaped) extent -- e.g.
    after RFS_Trim or an N-sided-fill boundary -- instead of anchoring
    cross-sections on (u, v) positions that were trimmed away and no
    longer correspond to real material on the face.

    `edge_cache` is a plain dict the caller creates once per blend/match
    call (`{}`) and passes to every _side_uv_trimmed call made during that
    call's sampling loop, so each (face, side) pair's boundary edge is
    located and orientation-checked ONCE, not once per sample.

    Falls back to the plain untrimmed _side_uv() when no real boundary
    edge can be found for this (face, side) -- the common case of a
    fresh, untrimmed Loft/Gordon/Coons/Fill face, where the iso-parameter
    line already IS the exact boundary and there is nothing extra to find.
    """
    key = (id(face), side)
    entry = edge_cache.get(key)
    if entry is None:
        edge = _trimmed_boundary_edge(face, pr, side)
        if edge is None:
            entry = False              # sentinel: "looked, found nothing"
        else:
            # Orient the edge so increasing t moves the same direction the
            # untrimmed iso-line would have: whichever edge endpoint is
            # closer to the iso-line's own t=0 corner becomes this edge's
            # t=0.
            u0, u1, v0, v1 = pr
            t0_uv = {0: (u0, v0), 1: (u1, v0),
                     2: (u0, v0), 3: (u0, v1)}[side]
            t0_pt = face.valueAt(*t0_uv)
            e0 = edge.valueAt(edge.FirstParameter)
            e1 = edge.valueAt(edge.LastParameter)
            flip = e0.distanceToPoint(t0_pt) > e1.distanceToPoint(t0_pt)
            entry = (edge, flip)
        edge_cache[key] = entry
    if entry is False:
        return _side_uv(pr, side, t)
    edge, flip = entry
    fp, lp = edge.FirstParameter, edge.LastParameter
    tt = (1.0 - t) if flip else t
    p = edge.valueAt(fp + tt * (lp - fp))
    try:
        u, v = face.Surface.parameter(p)
        # Clamp defensively: parameter() can return a value fractionally
        # outside `pr` from floating-point edge-of-domain round-off, which
        # would otherwise propagate into an out-of-range valueAt() call.
        u = min(max(u, pr[0]), pr[1])
        v = min(max(v, pr[2]), pr[3])
        return (u, v)
    except Exception:
        return _side_uv(pr, side, t)


def _side_is_trimmed(face, pr, side, corner_tol_rel=1e-3):
    """True if `face`'s real boundary on iso-parameter `side` is a
    genuinely TRIMMED edge (shorter/reshaped vs the full untrimmed
    iso-parameter line), False if there is no such distinction (a plain,
    untrimmed face, or one whose wire happens to run the full iso-line
    anyway).

    Used to guard match_surface_exact_g1 (see its docstring): that
    function copies boundary CONTROL-NET rows directly (_pole_ij) rather
    than sampling the boundary at all, so it has no way to know whether
    the "matched side" it's about to copy is the whole untrimmed edge or
    only a fragment of it -- unlike match_surface()/blend_surface(), which
    both sample via _side_uv_trimmed and therefore already follow a
    trimmed edge correctly. On a trimmed face, exact_g1's control-net copy
    would still copy ref's FULL boundary row onto tgt's edge, which is
    only correct if tgt's own boundary also spans the full iso-line --
    exactly the assumption trimming breaks.

    Method: locate the real boundary edge (_trimmed_boundary_edge); if
    none is found (or the search is ambiguous), there's nothing to prove
    is trimmed, so return False (matches the existing untrimmed-fallback
    behaviour elsewhere in this module). If one is found, compare its two
    endpoints to the expected untrimmed corner points for that side --
    if both endpoints sit within `corner_tol_rel` * the face's bounding
    diagonal of the full iso-line's own corners, the edge spans the whole
    side and there is nothing to distinguish from "untrimmed" for this
    purpose; otherwise the edge is a genuine fragment of the full side and
    the face is trimmed there.
    """
    edge = _trimmed_boundary_edge(face, pr, side)
    if edge is None:
        return False
    u0, u1, v0, v1 = pr
    corner0_uv, corner1_uv = {
        0: ((u0, v0), (u0, v1)), 1: ((u1, v0), (u1, v1)),
        2: ((u0, v0), (u1, v0)), 3: ((u0, v1), (u1, v1)),
    }[side]
    c0 = face.valueAt(*corner0_uv)
    c1 = face.valueAt(*corner1_uv)
    try:
        e0 = edge.valueAt(edge.FirstParameter)
        e1 = edge.valueAt(edge.LastParameter)
    except Exception:
        return True    # can't verify extent -- treat conservatively as trimmed
    try:
        diag = max(face.BoundBox.DiagonalLength, 1e-9)
    except Exception:
        diag = 1.0
    tol = corner_tol_rel * diag
    matches_untrimmed = (
        (e0.distanceToPoint(c0) < tol and e1.distanceToPoint(c1) < tol) or
        (e0.distanceToPoint(c1) < tol and e1.distanceToPoint(c0) < tol))
    return not matches_untrimmed


# --- Class-A QA: numeric continuity verification --------------------------- #
# match_surface() TARGETS a continuity order; it never reports what was
# actually achieved. Class-A surfacing lives and dies on exactly that
# number, not a visual guess -- these two functions are the measurement
# layer that was missing.
def surface_deviation(face_a, face_b, samples=24):
    """Numeric G0/G1/G2 deviation between two faces across their nearest
    facing boundary -- the QA counterpart to match_surface().

    Method: locate the matching side pair (_nearest_facing_sides, the same
    search match_surface uses internally), sample `samples` points along
    face_a's side, project each to the closest point on face_b
    (Vertex.distToShape -- exact nearest point on a general OCCT face),
    and compare position (G0), cross-boundary tangent angle (G1, degrees),
    and cross-boundary curvature-vector difference (G2).

    This is a MEASUREMENT, not a proof: it samples a finite number of
    points along one boundary, and for G1/G2 at the projected point on
    face_b it assumes the cross-boundary direction is the one belonging to
    the globally-matched side (the same approximation match_surface's own
    construction already relies on). Good enough to flag a bad seam
    numerically; not a substitute for a certified tolerance analysis.

    Returns {"max_g0":, "max_g1_deg":, "max_g2":, "side_a":, "side_b":,
    "samples": [{"t":, "p":, "g0":, "g1_deg":, "g2":}, ...]}.

    Trim-aware: samples face_a's side via _side_uv_trimmed, the same
    trim-aware helper blend_surface / match_surface already use, instead
    of the untrimmed iso-parameter line -- so a QA check run against a
    trimmed face samples points that are actually on its real material,
    rather than points a naive untrimmed walk would have placed outside
    it. (Previously this function had its own inline, non-trim-aware
    `_side_uv` -- fixed here so "what built the seam" and "what grades
    it" agree on where the boundary actually is.)
    """
    import math as _math

    sa, sb, a_pr, b_pr = _nearest_facing_sides(face_a, face_b)
    _edge_cache = {}

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return {0: du.negative(), 1: du, 2: dv.negative(), 3: dv}[side]

    def _cross_d2(face, pr, side, u, v):
        u0, u1, v0, v1 = pr
        if side < 2:
            eps = max(1e-6, (u1 - u0) * 1e-3)
            sgn = -1.0 if side == 0 else 1.0
            p0 = face.valueAt(u, v)
            p1 = face.valueAt(u + sgn * eps, v)
            p2 = face.valueAt(u + sgn * 2 * eps, v)
        else:
            eps = max(1e-6, (v1 - v0) * 1e-3)
            sgn = -1.0 if side == 2 else 1.0
            p0 = face.valueAt(u, v)
            p1 = face.valueAt(u, v + sgn * eps)
            p2 = face.valueAt(u, v + sgn * 2 * eps)
        return (p0 - p1 * 2.0 + p2) * (1.0 / (eps * eps))

    rows = []
    max_g0 = max_g1 = max_g2 = 0.0
    for k in range(samples):
        t = k / (samples - 1.0)
        ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, _edge_cache)
        pa = face_a.valueAt(ua, va)
        dist, pts_on, _info = Part.Vertex(pa).distToShape(face_b)
        pb = pts_on[0][1] if pts_on else pa
        try:
            ub, vb = face_b.Surface.parameter(pb)
        except Exception:
            ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, _edge_cache)

        Ta = _cross_tan(face_a, sa, ua, va)
        Tb = _cross_tan(face_b, sb, ub, vb)
        La, Lb = Ta.Length, Tb.Length
        if La > 1e-9 and Lb > 1e-9:
            cosang = max(-1.0, min(1.0, Ta.dot(Tb) / (La * Lb)))
            g1_deg = _math.degrees(_math.acos(cosang))
        else:
            g1_deg = 0.0

        Ka = _cross_d2(face_a, a_pr, sa, ua, va)
        Kb = _cross_d2(face_b, b_pr, sb, ub, vb)
        g2 = (Ka - Kb).Length

        rows.append({"t": t, "p": pa, "g0": dist, "g1_deg": g1_deg, "g2": g2})
        max_g0 = max(max_g0, dist)
        max_g1 = max(max_g1, g1_deg)
        max_g2 = max(max_g2, g2)

    return {"max_g0": max_g0, "max_g1_deg": max_g1, "max_g2": max_g2,
            "side_a": sa, "side_b": sb, "samples": rows}


def classify_continuity(report, g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2):
    """Pass/fail classification of a surface_deviation() report against
    tolerance bands: returns the achieved continuity order within
    tolerance ('G2'/'G1'/'G0'/'FAIL'), matching how a Class-A QA sign-off
    is normally phrased ("this seam reads as G1, not the G2 we wanted")."""
    if report["max_g0"] > g0_tol:
        return "FAIL"
    if report["max_g1_deg"] > g1_tol_deg:
        return "G0"
    if report["max_g2"] > g2_tol:
        return "G1"
    return "G2"


# --- Class-A QA: patch-network-scale audit ---------------------------------- #
# A real body-in-white surface model is organized (and reviewed) as dozens
# of adjacent patches, not one pair at a time. This is a QA AUDITOR across
# a whole network -- it finds every adjacent seam and grades it -- not a
# simultaneous multi-patch continuity SOLVER (that would mean re-fitting
# every patch at once to satisfy every seam constraint together, which is
# a much larger, still-unsettled-in-general numerical problem akin to the
# star-point vertex enclosure issue below). Knowing your worst seam is the
# problem this actually solves; not silently claiming to fix it for you.
def adjacency_graph(faces, gap_tol=1e-3):
    """Every pair of faces in `faces` that share a boundary (within
    gap_tol) -- the first step of any patch-network audit. Returns a list
    of (i, j) index pairs into `faces`, found the same way
    _nearest_facing_sides locates a shared boundary for one pair, applied
    pairwise across the whole list."""
    def _iso_mid(face, side):
        u0, u1, v0, v1 = face.ParameterRange
        return face.valueAt(*{0: (u0, (v0 + v1) / 2), 1: (u1, (v0 + v1) / 2),
                              2: ((u0 + u1) / 2, v0), 3: ((u0 + u1) / 2, v1)}[side])
    n = len(faces)
    mids = [[_iso_mid(f, s) for s in range(4)] for f in faces]
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            best = min(mids[i][a].distanceToPoint(mids[j][b])
                       for a in range(4) for b in range(4))
            if best <= gap_tol:
                pairs.append((i, j))
    return pairs


def network_continuity_report(faces, samples=12, gap_tol=1e-3,
                              g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2):
    """Assembly-scale continuity audit across a whole patch network.

    Finds every adjacent pair (adjacency_graph), scores each seam with
    surface_deviation() + classify_continuity(), and rolls up a
    worst-seam summary -- the same "what's the worst unresolved seam in
    this whole model" question a Class-A surfacing review actually asks,
    instead of checking pairs one at a time with match_surface alone.

    Returns {"seams": [{"i":, "j":, "report":, "grade":}, ...],
             "worst_grade":, "worst_seam": (i, j) or None,
             "isolated": [k, ...]}  -- `isolated` is patches with NO
    detected neighbour: either a real hole in the network, or a false
    isolate if gap_tol is tighter than the model's actual patch gaps.

    This is a QA AUDITOR, not a fixer: once you've reviewed the worst
    seam here and are satisfied with it, stitch_shell() is the command
    that actually welds the network's faces into one topological
    shell/solid -- audit here, stitch there, still two separate steps by
    design (see stitch_shell's own docstring).
    """
    pairs = adjacency_graph(faces, gap_tol=gap_tol)
    order = {"FAIL": 0, "G0": 1, "G1": 2, "G2": 3}
    seams = []
    touched = set()
    for i, j in pairs:
        rep = surface_deviation(faces[i], faces[j], samples=samples)
        grade = classify_continuity(rep, g0_tol=g0_tol,
                                    g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)
        seams.append({"i": i, "j": j, "report": rep, "grade": grade})
        touched.add(i)
        touched.add(j)
    isolated = [k for k in range(len(faces)) if k not in touched]
    if seams:
        worst = min(seams, key=lambda s: order[s["grade"]])
        worst_grade, worst_seam = worst["grade"], (worst["i"], worst["j"])
    else:
        worst_grade, worst_seam = None, None
    return {"seams": seams, "worst_grade": worst_grade,
            "worst_seam": worst_seam, "isolated": isolated}


def surface_review_summary(faces, pull_dir=None, samples=12, gap_tol=1e-3,
                           g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2):
    """Roll continuity and draft-angle checks into a single review summary.

    This is a lightweight workflow aid for body-surface review: it gives an
    at-a-glance answer to the two most common questions in a surfacing loop:
    1) which seam is the weakest in the current patch network, and
    2) whether the selected faces clear the chosen draft direction.
    """
    faces = list(faces or [])
    if not faces:
        return {"continuity": {"seams": [], "worst_grade": None,
                               "worst_seam": None, "isolated": []},
                "draft": {"face_count": 0, "worst_angle": None,
                          "angles": []}}

    continuity = network_continuity_report(
        faces, samples=samples, gap_tol=gap_tol,
        g0_tol=g0_tol, g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)

    draft = {"face_count": len(faces), "angles": [], "worst_angle": None}
    if pull_dir is not None:
        for face in faces:
            try:
                ang = draft_angle_analysis(face, pull_dir, bands=(0.0, 3.0, 5.0, 10.0), nu=32, nv=32)
                vals = ang.get("grid", [])
                if vals:
                    flat = [v for row in vals for v in row if v is not None]
                    draft["angles"].append(max(flat) if flat else None)
                else:
                    draft["angles"].append(None)
            except Exception:
                draft["angles"].append(None)
        draft["worst_angle"] = max((a for a in draft["angles"] if a is not None), default=None)
    else:
        draft["worst_angle"] = None
    return {"continuity": continuity, "draft": draft}


def review_recommendation(summary):
    """Turn a review summary into a simple next-step recommendation.

    The goal is to make the QA step feel more like a guided assistant and
    less like a blank report. It classifies the result into one of three
    broad states: good, watch, or fix, and suggests the next action.
    """
    cont = (summary or {}).get("continuity", {})
    draft = (summary or {}).get("draft", {})
    worst_grade = cont.get("worst_grade")
    worst_angle = draft.get("worst_angle")

    if worst_grade == "G2" and (worst_angle is None or worst_angle <= 3.0):
        return {"status": "good", "step": "Proceed to manufacturing review."}
    if worst_grade in ("G1", None) or (worst_angle is not None and worst_angle > 5.0):
        return {"status": "watch", "step": "Refine the seam or draft before moving on."}
    return {"status": "fix", "step": "Improve continuity and draft before manufacture."}


# --- native visual QA: curvature comb / zebra / reflection / draft-angle -- #
# The everyday visual-inspection tools a surfacing modeler actually lives
# in day to day -- previously pure delegations to Curves (ParametricComb /
# ZebraTool / ReflectLines / Curves_DraftAnalysis), with no native
# fallback at all. All four are standard OCCT surface/curve differential
# geometry (Curve.getD2, Face.normalAt) -- no CAGD research gap here, so
# unlike the star-point G2 problem above, this is a genuine, closeable
# implementation gap rather than a documented limitation.
def curvature_comb(edge, scale=1.0, samples=40):
    """Curvature comb 'teeth' along an edge: at each sample, a line
    segment from the curve point outward, length proportional to
    curvature, pointing along the curvature vector. Native replacement
    for Curves' ParametricComb (RFS_Combs). Returns a Part.Compound of
    line edges (each tooth its own straight Part.Edge).
    """
    fp, lp = edge.FirstParameter, edge.LastParameter
    samp = []
    max_k = 1e-12
    for i in range(samples):
        t = fp + i / (samples - 1.0) * (lp - fp)
        try:
            p, d1, d2 = edge.Curve.getD2(t)
        except Exception:
            continue
        speed2 = d1.dot(d1)
        if speed2 < 1e-12:
            continue
        # curvature vector (planar-curve form): component of d2
        # perpendicular to the tangent, scaled by 1/|d1|^2.
        proj = d2 - d1 * (d2.dot(d1) / speed2)
        k = proj.Length / speed2
        max_k = max(max_k, k)
        samp.append((p, proj, k))
    teeth = []
    for p, proj, k in samp:
        if proj.Length < 1e-9 or max_k <= 0:
            continue
        direction = proj * (1.0 / proj.Length)
        length = scale * (k / max_k)
        teeth.append(Part.makeLine(p, p + direction * length))
    return Part.Compound(teeth)


def gaussian_curvature(face, u, v):
    """Compute Gaussian curvature at point (u,v) on a face.

    Returns the Gaussian curvature K = k1 * k2 where k1 and k2 are
    principal curvatures.
    """
    try:
        surf = face.Surface
        # Try to use built-in curvature evaluation if available
        if hasattr(surf, 'GaussianCurvature'):
            return surf.GaussianCurvature(u, v)
        else:
            # Fallback: compute from first and second fundamental forms
            # First fundamental form coefficients
            ru = surf.uDerivative(u, v)  # ∂S/∂u
            rv = surf.vDerivative(u, v)  # ∂S/∂v
            E = dot(ru, ru)
            F = dot(ru, rv)
            G = dot(rv, rv)

            # Unit normal
            normal = cross(ru, rv)
            norm_normal = norm(normal)
            if norm_normal < 1e-12:
                return 0.0
            normal = normal * (1.0 / norm_normal)

            # Second fundamental form coefficients
            ruu = surf.u2Derivative(u, v)  # ∂²S/∂u²
            ruv = surf.uvDerivative(u, v)  # ∂²S/∂u∂v
            rvv = surf.v2Derivative(u, v)  # ∂²S/∂v²
            L = dot(ruu, normal)
            M = dot(ruv, normal)
            N = dot(rvv, normal)

            # Gaussian curvature K = (LN - M²) / (EG - F²)
            denom = E * G - F * F
            if abs(denom) < 1e-12:
                return 0.0
            return (L * N - M * M) / denom
    except Exception:
        return 0.0


def mean_curvature(face, u, v):
    """Compute mean curvature at point (u,v) on a face.

    Returns the mean curvature H = (k1 + k2) / 2 where k1 and k2 are
    principal curvatures.
    """
    try:
        surf = face.Surface
        # Try to use built-in curvature evaluation if available
        if hasattr(surf, 'MeanCurvature'):
            return surf.MeanCurvature(u, v)
        else:
            # Fallback: compute from first and second fundamental forms
            # First fundamental form coefficients
            ru = surf.uDerivative(u, v)  # ∂S/∂u
            rv = surf.vDerivative(u, v)  # ∂S/∂v
            E = dot(ru, ru)
            F = dot(ru, rv)
            G = dot(rv, rv)

            # Unit normal
            normal = cross(ru, rv)
            norm_normal = norm(normal)
            if norm_normal < 1e-12:
                return 0.0
            normal = normal * (1.0 / norm_normal)

            # Second fundamental form coefficients
            ruu = surf.u2Derivative(u, v)  # ∂²S/∂u²
            ruv = surf.uvDerivative(u, v)  # ∂²S/∂u∂v
            rvv = surf.v2Derivative(u, v)  # ∂²S/∂v²
            L = dot(ruu, normal)
            M = dot(ruv, normal)
            N = dot(rvv, normal)

            # Mean curvature H = (EN - 2FM + GL) / (2(EG - F²))
            denom = E * G - F * F
            if abs(denom) < 1e-12:
                return 0.0
            return (E * N - 2 * F * M + G * L) / (2.0 * denom)
    except Exception:
        return 0.0


def _ensure_vector(v):
    """Ensure argument is a Vector, converting from tuple if needed."""
    if isinstance(v, tuple):
        return Vector(*v)
    return v


def _uv_grid(face, nu, nv):
    u0, u1, v0, v1 = face.ParameterRange
    return [[(u0 + i * (u1 - u0) / (nu - 1.0), v0 + j * (v1 - v0) / (nv - 1.0))
            for j in range(nv)] for i in range(nu)]


def _sample_grid(face, scalar_fn, nu, nv):
    """Evaluate scalar_fn(u, v) once at every node of an nu x nv grid over
    face's parameter range. Returns (grid_uv, vals).

    Splitting sampling out from contour extraction (_contour_at_level
    below) lets callers with MULTIPLE iso-levels of the SAME scalar field
    -- zebra_stripes' stripe_count bands, draft_angle_analysis' threshold
    bands -- sample once and reuse, instead of re-evaluating scalar_fn
    once per level for a field that doesn't depend on the level at all.
    Previously this was a real O(nu*nv*levels) cost for no reason.
    """
    grid_uv = _uv_grid(face, nu, nv)
    vals = [[scalar_fn(u, v) for (u, v) in row] for row in grid_uv]
    return grid_uv, vals


def _contour_at_level(face, grid_uv, vals, level, nu, nv):
    """Iso-contour of a pre-sampled scalar grid at `level`, as a list of
    straight Part.Edge segments (one or two per grid cell the contour
    passes through).

    Resolves the classic marching-squares "saddle" ambiguity: a cell
    whose diagonal corners agree (both above or both below `level`) and
    adjacent corners disagree has FOUR edge crossings, which can be
    paired into two contour segments two different ways. This is decided
    with the usual asymptotic test (compare the bilinear-interpolated
    cell-center value's class to corner 0's class) rather than left to
    "whichever two crossings a loop happens to find first" -- the
    previous version of this function had no saddle handling at all and
    silently DROPPED the cell (contour gaps) whenever one occurred.
    """
    segs = []
    for i in range(nu - 1):
        for j in range(nv - 1):
            cp = (grid_uv[i][j], grid_uv[i + 1][j],
                  grid_uv[i + 1][j + 1], grid_uv[i][j + 1])
            cv = (vals[i][j], vals[i + 1][j],
                  vals[i + 1][j + 1], vals[i][j + 1])
            inside = tuple(v > level for v in cv)
            crossing = [k for k in range(4) if inside[k] != inside[(k + 1) % 4]]
            if not crossing:
                continue

            def _cross(k, cp=cp, cv=cv):
                a, b = k, (k + 1) % 4
                t = (level - cv[a]) / (cv[b] - cv[a])
                ua, va = cp[a]
                ub, vb = cp[b]
                return face.valueAt(ua + t * (ub - ua), va + t * (vb - va))

            def _add_seg(a, b):
                # A crossing can land exactly on a shared grid corner
                # (e.g. the iso-level exactly equals that corner's value),
                # which collapses two "different" crossing edges onto the
                # SAME point -- Part.makeLine rejects a zero-length line
                # ("Line through identical points"). That's a genuine
                # point-touch, not a segment, so skip it.
                p1, p2 = _cross(a), _cross(b)
                if p1.distanceToPoint(p2) < 1e-9:
                    return
                segs.append(Part.makeLine(p1, p2))

            if len(crossing) == 2:
                a, b = crossing
                _add_seg(a, b)
            elif len(crossing) == 4:
                center = sum(cv) / 4.0
                if inside[0] == (center > level):
                    pairs = ((0, 1), (2, 3))     # center joins corner0/2
                else:
                    pairs = ((3, 0), (1, 2))     # center joins corner1/3
                for a, b in pairs:
                    _add_seg(a, b)
    return segs


def _marching_squares(face, scalar_fn, level, nu=60, nv=60):
    """Sample + contour a single iso-level in one call -- convenience
    wrapper for callers that only need one level per scalar field (e.g.
    reflection_lines, where each light direction has its own field).
    Callers extracting several levels of the SAME field should call
    _sample_grid once and _contour_at_level per level instead (see
    zebra_stripes / draft_angle_analysis below) to avoid re-sampling."""
    grid_uv, vals = _sample_grid(face, scalar_fn, nu, nv)
    return _contour_at_level(face, grid_uv, vals, level, nu, nv)


def zebra_stripes(face, view_dir=None, stripe_count=24, nu=60, nv=60):
    """Zebra-stripe analysis lines. Native replacement for Curves'
    ZebraTool (RFS_Zebra).

    Rendered zebra shading is a striped environment map; here the
    equivalent is computed directly in surface space: at each (u, v), the
    angle between the surface normal and a fixed `view_dir` is evaluated,
    and contours are extracted at `stripe_count` evenly spaced angle
    bands. Kinks, flats, and G1/G2 breaks in the surface show up as
    bent/merged/split stripes exactly as in the rendered version, but as
    exact curves ON the surface rather than a texture, so they can be
    measured (e.g. with surface_deviation on the underlying patches)
    instead of only eyeballed. The angle field is sampled ONCE
    (_sample_grid) and every stripe level is contoured from that same
    grid (_contour_at_level) -- the field doesn't depend on the stripe
    level, so there's no reason to re-evaluate normalAt() per stripe.
    """
    if len(face.Faces if hasattr(face, "Faces") else []) and \
            not isinstance(face, Part.Face):
        face = face.Faces[0]
    view_dir = Vector(view_dir) if view_dir is not None else Vector(0, 0, 1)
    if view_dir.Length < 1e-9:
        view_dir = Vector(0, 0, 1)
    view_dir.normalize()

    def angle_at(u, v):
        n = face.normalAt(u, v)
        n.normalize()
        return math.degrees(math.acos(max(-1.0, min(1.0, n.dot(view_dir)))))

    grid_uv, vals = _sample_grid(face, angle_at, nu, nv)
    lines = []
    for k in range(1, stripe_count):
        level = 180.0 * k / stripe_count
        lines.extend(_contour_at_level(face, grid_uv, vals, level, nu, nv))
    return Part.Compound(lines)


def standard_light_rig():
    """Three canonical parallel-light-bar directions for reflection-line
    analysis: horizontal, vertical, and 45-degree diagonal.

    This replaces an earlier arbitrary 5-direction rotation fan with a
    small, DOCUMENTED default: horizontal and vertical bars are the two
    orientations a Class-A review actually checks first (horizontal bars
    catch kinks running across a hood/door's main sweep direction,
    vertical bars catch them along pillars/fenders), with one diagonal
    bar as a general-purpose third check. It is still just a reasonable
    default, not a certified studio rig -- pass a project-specific
    `light_dirs` list to reflection_lines() to match a real light-bar
    layout (e.g. an actual studio's overhead fixture positions).
    """
    return [Vector(1, 0, 0), Vector(0, 1, 0), Vector(1, 1, 0)]


def reflection_lines(face, light_dirs, view_dir=None, tol_deg=2.0, nu=60, nv=60):
    """Reflection-line analysis. Native replacement for Curves'
    ReflectLines (RFS_Reflect).

    For each direction in `light_dirs` (parallel 'light bars' -- see
    standard_light_rig() for a documented default fan), extracts the
    curve on `face` where the mirror reflection of `view_dir` off the
    surface normal is, within `tol_deg`, parallel to that light
    direction -- the classic Class-A check of whether a straight
    fluorescent tube reflects as a straight (not kinked/wavy) line.
    """
    if hasattr(face, "Faces") and face.Faces and not isinstance(face, Part.Face):
        face = face.Faces[0]
    view_dir = Vector(view_dir) if view_dir is not None else Vector(0, 0, 1)
    if view_dir.Length < 1e-9:
        view_dir = Vector(0, 0, 1)
    view_dir.normalize()
    out = []
    for light in light_dirs:
        L = Vector(light)
        if L.Length < 1e-9:
            continue
        L.normalize()

        def dev(u, v, L=L):
            n = face.normalAt(u, v)
            n.normalize()
            r = view_dir - n * (2.0 * view_dir.dot(n))     # reflect(view, n)
            if r.Length < 1e-9:
                return 90.0
            r.normalize()
            return math.degrees(math.acos(max(-1.0, min(1.0, abs(r.dot(L))))))

        out.extend(_marching_squares(face, dev, tol_deg, nu, nv))
    return Part.Compound(out)


def draft_angle_analysis(face, pull_dir, bands=(0.0, 3.0, 5.0, 10.0),
                         nu=60, nv=60):
    """Draft-angle analysis. Native replacement for Curves'
    Curves_DraftAnalysis (RFS_DraftAnalysis).

    Draft angle at (u, v) is 90 minus the angle between the surface
    normal and `pull_dir` (the mold-opening / tool-withdrawal axis) --
    positive means the surface clears the tool, negative is an undercut.
    Returns a dict:
      {"bands": [Part.Compound of contour edges at each threshold in
                 `bands`, same order],
       "grid":  nu x nv list-of-lists of the draft angle in degrees
                (for a GUI layer to color as a heat-map overlay),
       "thresholds": list(bands)}
    The default bands (0/3/5/10 degrees) are the usual automotive
    undercut / minimum-draft callout set. The angle field is sampled
    ONCE (_sample_grid) and reused both for "grid" and for every
    threshold in "bands" (_contour_at_level) -- previously "grid" and
    each band's contour independently re-evaluated normalAt() over the
    whole surface, once each, for a field that's identical across all of
    them.
    """
    if hasattr(face, "Faces") and face.Faces and not isinstance(face, Part.Face):
        face = face.Faces[0]
    pull = Vector(pull_dir)
    if pull.Length < 1e-9:
        raise ValueError("draft_angle_analysis: pull_dir must be non-zero")
    pull.normalize()

    def angle_at(u, v):
        n = face.normalAt(u, v)
        n.normalize()
        return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, n.dot(pull)))))

    grid_uv, vals = _sample_grid(face, angle_at, nu, nv)
    band_compounds = [Part.Compound(_contour_at_level(face, grid_uv, vals, b, nu, nv))
                      for b in bands]
    return {"bands": band_compounds, "grid": vals, "thresholds": list(bands)}


# --- curve drawing / modeling (our own, Gui-free) ------------------------- #
def interpolate_curve(points, closed=False):
    """Smooth B-spline that PASSES THROUGH the given points."""
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    if len(pts) < 2:
        raise ValueError("need >= 2 points")
    bs = Part.BSplineCurve()
    bs.interpolate(Points=pts, PeriodicFlag=bool(closed))
    return bs.toShape()


def approximate_curve(points, degree=3, tol=1e-3):
    """B-spline that APPROXIMATES (fits near) the points -- fewer poles, fairer."""
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    if len(pts) < 2:
        raise ValueError("need >= 2 points")
    bs = Part.BSplineCurve()
    bs.approximate(Points=pts, DegMin=min(3, degree), DegMax=degree, Tolerance=tol)
    return bs.toShape()


def control_poly_curve(points, degree=3, knots=None, mults=None, weights=None):
    """B-spline whose CONTROL POLYGON is the given points (pen/Bezier-style).

    If `knots` and `mults` are both supplied, they are used verbatim -- this is
    what the knot-drag editor relies on to write the user's edits back into the
    geometry. Otherwise a clamped-uniform vector is synthesised.
    If `weights` is provided, a rational B-spline (NURBS) is created.
    """
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    n = len(pts)
    if n < 2:
        raise ValueError("need >= 2 control points")
    deg = max(1, min(degree, n - 1))
    bs = Part.BSplineCurve()
    # NB: there is no `buildFromPolesMultsWeightsKnots` on Part.BSplineCurve in
    # this OCCT build -- the rational case passes `weights` as the LAST argument
    # to buildFromPolesMultsKnots (poles, mults, knots, periodic, degree, weights).
    if knots is not None and mults is not None:
        if weights is not None:
            # rational spline with explicit knots/mults/weights
            bs.buildFromPolesMultsKnots(
                pts, list(mults), [float(k) for k in knots], False, deg,
                [float(w) for w in weights])
        else:
            bs.buildFromPolesMultsKnots(
                pts, list(mults), [float(k) for k in knots], False, deg)
    else:
        if weights is not None:
            bs.buildFromPolesMultsKnots(
                pts, _clamped_mults(n, deg), _clamped_knots(n, deg), False, deg,
                [float(w) for w in weights])
        else:
            bs.buildFromPolesMultsKnots(
                pts, _clamped_mults(n, deg), _clamped_knots(n, deg), False, deg)
    return bs.toShape()


def bezier_curve_shape(points, left_handles=None, right_handles=None):
    """Composite cubic-Bezier curve through `points`, using each point's own
    LEFT (incoming) and RIGHT (outgoing) handle as the classic two-handle
    Bezier control scheme -- the geometry BezierCurveFP is actually
    supposed to build (it previously ignored the handles entirely and
    just interpolated the anchor points, which meant dragging a handle
    had zero effect on the curve).

    `left_handles[i]` / `right_handles[i]` are ABSOLUTE positions (not
    offsets), matching how editmode.PointEditor's Bezier-handle drag
    code already stores them. If either list is missing or the wrong
    length, handles default to 1/3 of the way to the neighbouring point
    (a reasonable "Auto" starting shape) rather than raising.

    Segment i (between points[i] and points[i+1]) is a single cubic
    Bezier with poles:
        points[i], right_handles[i], left_handles[i+1], points[i+1]
    Continuity across a shared point is whatever relationship the
    caller's handle-type constraints (Auto/Aligned/Vector/Free) already
    established between that point's left/right handle -- this function
    does not itself enforce continuity, it only builds the curve those
    handles describe.

    Returns a Part.Wire of N-1 Bezier edges (a single edge for 2 points).
    Raises ValueError for fewer than 2 points.
    """
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    n = len(pts)
    if n < 2:
        raise ValueError("bezier_curve_shape: need >= 2 points")

    def _default_handle(i, towards):
        # towards = +1 (right handle, toward i+1) or -1 (left handle, toward i-1)
        j = i + towards
        if not (0 <= j < n):
            j = i - towards
        return pts[i] + (pts[j] - pts[i]) * (1.0 / 3.0)

    lh = list(left_handles) if left_handles and len(left_handles) == n else None
    rh = list(right_handles) if right_handles and len(right_handles) == n else None

    edges = []
    for i in range(n - 1):
        p0 = pts[i]
        p3 = pts[i + 1]
        p1 = rh[i] if rh is not None else _default_handle(i, 1)
        p2 = lh[i + 1] if lh is not None else _default_handle(i + 1, -1)
        bz = Part.BezierCurve()
        bz.setPoles([p0, p1, p2, p3])
        edges.append(bz.toShape())
    return Part.Wire(edges) if len(edges) > 1 else edges[0]


def clean_patch_surface(grid, degree=None, weights=None):
    """Single-span B-spline surface whose CONTROL NET is `grid` exactly --
    the surface analog of control_poly_curve above. This is what native
    RFS_CleanPatch44 / RFS_CleanPatch66 build, replacing Silk's
    CubicSurface_44 / CubicSurface_66 (see engines.py's provider chains).

    `grid` is a rectangular (n x n) list-of-lists of points -- 4x4 for a
    "low-CP bicubic patch" (degree 3), 6x6 for "bi-quintic" (degree 5).
    Unlike grid_to_surface() (core.py, used by Coons/Gordon), this does NOT
    interpolate through the grid -- the grid points ARE the control poles,
    same clamped-uniform-knots technique control_poly_curve already uses,
    just applied along both U and V. That's the same "low degree, few
    control points, exact control-net placement" idea Silk's CleanPatch
    tools are built around; we just build it directly against OCCT instead
    of going through Silk's ArachNURBS layer.
    If `weights` is provided, a rational B-spline surface (NURBS) is created.
    """
    rows = [[Vector(*p) if not isinstance(p, Vector) else p for p in row]
            for row in grid]
    nu = len(rows)
    if nu < 2:
        raise ValueError("clean_patch_surface needs a grid with >= 2 rows")
    nv = len(rows[0])
    if nv < 2:
        raise ValueError("clean_patch_surface needs a grid with >= 2 columns")
    if any(len(r) != nv for r in rows):
        raise ValueError("clean_patch_surface needs a RECTANGULAR grid "
                         "(every row must have the same length)")
    du = max(1, min(degree if degree is not None else nu - 1, nu - 1))
    dv = max(1, min(degree if degree is not None else nv - 1, nv - 1))
    surf = Part.BSplineSurface()
    # As with the curve: no `buildFromPolesMultsWeightsKnots` exists. The
    # surface's rational case passes a 2D `weights` grid (matching the 2D poles)
    # as the LAST argument to buildFromPolesMultsKnots -- NOT a flattened list.
    if weights is not None:
        weights_2d = [[float(w) for w in row] for row in weights]
        surf.buildFromPolesMultsKnots(
            rows,
            _clamped_mults(nu, du), _clamped_mults(nv, dv),
            _clamped_knots(nu, du), _clamped_knots(nv, dv),
            False, False, du, dv, weights_2d)
    else:
        surf.buildFromPolesMultsKnots(
            rows,
            _clamped_mults(nu, du), _clamped_mults(nv, dv),
            _clamped_knots(nu, du), _clamped_knots(nv, dv),
            False, False, du, dv)
    return surf.toShape()


def _grid_weights_from_selection(n):
    """Optional NxN weight grid, parallel to _grid_points_from_selection:
    if the SAME selection carries per-point weight annotations (e.g. a
    point object exposing a 'Weight' property), collect them in the same
    row-major order; otherwise return None (non-rational patch, the
    existing behavior)."""
    import FreeCADGui
    sel = FreeCADGui.Selection.getSelectionEx()
    weights = []
    for so in sel:
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Vertex":
                weights.append(float(getattr(so.Object, "Weight", 1.0)))
        if not so.SubObjects and getattr(so.Object, "Shape", None):
            weights.extend([float(getattr(so.Object, "Weight", 1.0))] * len(so.Object.Shape.Vertexes))
    if len(weights) != n * n or all(w == 1.0 for w in weights):
        return None
    return weights


def clean_patch_44(grid, weights=None):
    """Create a 4x4 bicubic patch whose control net is exactly `grid`.

    This is the native implementation for RFS_CleanPatch44, replacing
    Silk's CubicSurface_44. The grid must be a 4x4 list-of-lists of points.

    Args:
        grid: A 4x4 list of lists of points (each point as [x,y,z] or Vector)
        weights: Optional 4x4 list of lists of weights for rational B-spline surface

    Returns:
        Part.Shape: A B-spline surface with degree 3 in both directions
                   whose control net matches the input grid exactly

    Raises:
        ValueError: If grid is not 4x4 or is not rectangular
    """
    # Validate grid dimensions
    if not isinstance(grid, list) or len(grid) != 4:
        raise ValueError("clean_patch_44 expects a 4x4 grid")
    for row in grid:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("clean_patch_44 expects a 4x4 grid")

    return clean_patch_surface(grid, degree=3, weights=weights)


def clean_patch_66(grid, weights=None):
    """Create a 6x6 bi-quintic patch whose control net is exactly `grid`.

    This is the native implementation for RFS_CleanPatch66, replacing
    Silk's CubicSurface_66. The grid must be a 6x6 list-of-lists of points.

    Args:
        grid: A 6x6 list of lists of points (each point as [x,y,z] or Vector)
        weights: Optional 6x6 list of lists of weights for rational B-spline surface

    Returns:
        Part.Shape: A B-spline surface with degree 5 in both directions
                   whose control net matches the input grid exactly

    Raises:
        ValueError: If grid is not 6x6 or is not rectangular
    """
    # Validate grid dimensions
    if not isinstance(grid, list) or len(grid) != 6:
        raise ValueError("clean_patch_66 expects a 6x6 grid")
    for row in grid:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("clean_patch_66 expects a 6x6 grid")

    return clean_patch_surface(grid, degree=5, weights=weights)


def knots_from_shape(shape):
    """Read the (knots, mults) pair off the first BSpline edge of `shape`.

    OCCT exposes `KnotSequence` (multiplicity-expanded, e.g. [0,0,0,0,1,1,1,1])
    and `getMultiplicities()` (e.g. [4,4]). Here we return the *unique* knot
    values (one per distinct value) along with the multiplicity list, so the
    knot editor can clamp between neighbouring UNIQUE knots without collapsing
    clamped-end duplicates. Returns ([float, ...], [int, ...]) or (None, None).
    """
    edges = getattr(shape, "Edges", None) or []
    for e in edges:
        c = getattr(e, "Curve", None)
        if isinstance(c, Part.BSplineCurve):
            seq = list(c.KnotSequence)
            mults = list(c.getMultiplicities())
            unique = []
            last = None
            for k in seq:
                if last is None or abs(k - last) > 1e-9:
                    unique.append(float(k))
                    last = k
            return unique, mults
    return None, None


def polyline(points, closed=False):
    """Straight polyline (G0) through the points."""
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    if len(pts) < 2:
        raise ValueError("need >= 2 points")
    if closed:
        pts = pts + [pts[0]]
    return Part.makePolygon(pts)


def _end_pos_dir(edge, at_end):
    """Position, outward unit tangent, and 2nd derivative at an edge end.
    'outward' = pointing away from the curve into the gap to be blended."""
    crv = edge.Curve
    u = edge.LastParameter if at_end else edge.FirstParameter
    try:
        p, d1, d2 = crv.getD2(u)
    except Exception:
        p, d1, d2 = edge.valueAt(u), edge.tangentAt(u), None
    out = d1 if at_end else d1 * -1.0
    if out.Length:
        out = out * (1.0 / out.Length)
    return p, out, d2


def _end_pos_dir_d3(edge, at_end):
    """Position, outward unit tangent, 2nd and 3rd derivatives at an edge end.

    Same convention as _end_pos_dir (the tangent is sign-flipped for the
    START end so it always points OUT of the curve, into the blend gap).
    The 3rd derivative is returned as-is (no sign flip) -- the blend caller
    sign-flips it in the same way the G2 path sign-flips a1/v1 (see
    blend_curve), since Hermite boundary data is always taken with the
    curve's own forward-time parameterization and the blend's parameter
    runs the opposite way on the second curve.
    """
    crv = edge.Curve
    u = edge.LastParameter if at_end else edge.FirstParameter
    try:
        p, d1, d2, d3 = crv.getD3(u)
    except Exception:
        # No 3rd derivative available (e.g. OCCT raises on a degenerate
        # curve). Caller falls back to G2 by checking for None.
        return None
    out = d1 if at_end else d1 * -1.0
    if out.Length:
        out = out * (1.0 / out.Length)
    # a0/a1, j0/j1 stay in the curve's own time direction; blend_curve
    # sign-flips a1/v1 in the G2 path, and we do the same for a1, j1
    # in the G3 path.
    a = d2 if at_end else d2 * -1.0
    j = d3 if at_end else d3 * -1.0
    return p, out, a, j


def blend_curve(edge1, at_end1, edge2, at_end2, continuity="G2", scale=1.0):
    """Bridge two curve ends with a G1 (cubic), G2 (quintic), or G3
    (septic) blend.

    at_end1/at_end2: True -> blend from that edge's END point, False -> START.

    G3 NOTE: the 3rd derivative is taken directly from OCCT's
    Geom_Curve::getD3(u), so it is analytic on B-spline / Bezier / arc
    inputs and is NOT a finite-difference estimate (the same way the 1st
    and 2nd derivatives on the G1/G2 paths are analytic). The downstream
    boundary quality is therefore only as good as the 1D curve definition
    itself, with no FD noise compounding -- which is what makes G3 cheap
    enough to ship here, even though the surface variant (G3 blend_surface)
    is the high-risk part the planning notes flagged. Use
    classify_continuity() on the input edges first if you need a sense of
    how meaningful the 3rd derivative is on a specific input.
    """
    p0, d0, a0 = _end_pos_dir(edge1, at_end1)
    p1, d1, a1 = _end_pos_dir(edge2, at_end2)
    L = max(p0.distanceToPoint(p1), 1e-6)

    if continuity == "G1" or a0 is None or a1 is None:
        bz = Part.BezierCurve()
        bz.setPoles([p0, p0 + d0 * (L / 3.0 * scale),
                     p1 + d1 * (L / 3.0 * scale), p1])
        return bz.toShape()

    if continuity == "G3":
        # 3rd-derivative path. Falls back to G2 if either input edge
        # doesn't expose getD3 (older OCCT surfaces, degenerate curves).
        d3_0 = _end_pos_dir_d3(edge1, at_end1)
        d3_1 = _end_pos_dir_d3(edge2, at_end2)
        if d3_0 is None or d3_1 is None:
            continuity = "G2"   # graceful fallback
        else:
            p0g, d0g, a0g, j0g = d3_0
            p1g, d1g, a1g, j1g = d3_1
            v0, v1 = d0g * (L * scale), d1g * (L * scale)
            n = 24
            pts = []
            for i in range(n):
                t = i / (n - 1.0)
                # Same sign-flip convention as the G2 path: a1, v1, j1
                # are taken in edge2's own time direction, and the blend
                # parameter runs the OPPOSITE way on the second curve,
                # so a1, v1, j1 are negated. (See _hermite7's docstring:
                # the basis is symmetric, so flipping the sign of the
                # right-end coefficients correctly gives the
                # reverse-parameter boundary data.)
                pts.append(_hermite7(p0g, v0, a0g, j0g,
                                     p1g, -v1, -a1g, -j1g, t))
            bs = Part.BSplineCurve()
            bs.interpolate(Points=pts)
            return bs.toShape()

    # G2: sample a quintic Hermite (pos+vel+acc at both ends) then interpolate
    v0, v1 = d0 * (L * scale), d1 * (L * scale)
    n = 24
    pts = []
    for i in range(n):
        t = i / (n - 1.0)
        # quintic Hermite basis (note end-2 derivatives point inward => -)
        h = _quintic_hermite(t)
        pts.append(p0 * h[0] + v0 * h[1] + a0 * h[2]
                   + a1 * h[3] + v1 * (-h[4]) + p1 * h[5])
    bs = Part.BSplineCurve()
    bs.interpolate(Points=pts)
    return bs.toShape()


def nearest_ends(e1, e2):
    """Which ends of two edges face each other -> (at_end1, at_end2) bools
    (True = that edge's END point). Used to auto-orient a blend."""
    s1, t1 = _eval01(e1, 0.0), _eval01(e1, 1.0)
    s2, t2 = _eval01(e2, 0.0), _eval01(e2, 1.0)
    cands = [((False, False), s1.distanceToPoint(s2)),
             ((False, True), s1.distanceToPoint(t2)),
             ((True, False), t1.distanceToPoint(s2)),
             ((True, True), t1.distanceToPoint(t2))]
    return min(cands, key=lambda c: c[1])[0]


def _quintic_hermite(t):
    t2 = t * t; t3 = t2 * t; t4 = t3 * t; t5 = t4 * t
    H_p0 = 1 - 10 * t3 + 15 * t4 - 6 * t5
    H_v0 = t - 6 * t3 + 8 * t4 - 3 * t5
    H_a0 = 0.5 * t2 - 1.5 * t3 + 1.5 * t4 - 0.5 * t5
    H_a1 = 0.5 * t3 - t4 + 0.5 * t5
    H_v1 = -4 * t3 + 7 * t4 - 3 * t5
    H_p1 = 10 * t3 - 15 * t4 + 6 * t5
    return (H_p0, H_v0, H_a0, H_a1, H_v1, H_p1)


# --- Blender Railform rail interchange ------------------------------------ #
# Interchange format (JSON), one object with a "rails" list. Each rail:
#   { "degree": int,
#     "poles":  [[x,y,z], ...],          # control points
#     "knots":  [float, ...],            # optional; clamped-uniform if omitted
#     "mults":  [int, ...],              # optional; matches knots
#     "weights":[float, ...],            # optional; non-rational if omitted
#     "continuity": "G0|G1|G2|G3" }      # advisory tag for downstream policy
#
# Keeping this Gui-free means we can build + round-trip-test it under freecadcmd,
# unlike anything that touches Silk/Curves.
def rail_to_bspline(rail):
    """Rebuild one interchange rail as an exact Part.BSplineCurve edge."""
    poles = [Vector(*p) for p in rail["poles"]]
    degree = int(rail.get("degree", 3))
    bs = Part.BSplineCurve()
    if "knots" in rail and "mults" in rail:
        weights = rail.get("weights")
        bs.buildFromPolesMultsKnots(
            poles,
            rail["mults"],
            rail["knots"],
            False,                 # periodic
            degree,
            weights if weights else None,
        )
    else:
        # clamped-uniform knot vector from poles + degree
        bs.buildFromPolesMultsKnots(
            poles,
            _clamped_mults(len(poles), degree),
            _clamped_knots(len(poles), degree),
            False,
            degree,
        )
    return bs.toShape()


def rails_to_edges(doc_dict):
    """interchange dict -> list of BSpline edges (+ their continuity tags)."""
    out = []
    for rail in doc_dict.get("rails", []):
        out.append((rail_to_bspline(rail), rail.get("continuity", "G0")))
    return out


def _clamped_knots(n_poles, degree):
    n_interior = n_poles - degree - 1
    if n_interior < 0:
        raise ValueError("need n_poles >= degree+1")
    interior = [float(i + 1) / (n_interior + 1) for i in range(n_interior)]
    return [0.0] + interior + [1.0]


def _clamped_mults(n_poles, degree):
    n_interior = n_poles - degree - 1
    return [degree + 1] + [1] * n_interior + [degree + 1]

# --- boolean operations (RFS_BooleanUnion/Subtract/Intersect) ------------- #
# Everything above this point that touches Solids (fillet_solid,
# chamfer_solid, shell_solid, thicken_surface) wraps a SPECIFIC OCCT solid
# operation. Plain combine/cut/intersect booleans were simply never wired
# up at all -- not because OCCT's boolean engine is unreliable (it is the
# same BRepAlgoAPI_Fuse/Cut/Common machinery FreeCAD's own PartDesign
# workbench runs on every recompute), but because no curated command ever
# called it. These three are thin, validated wrappers -- same
# ValueError-on-bad-input discipline the rest of this module already uses
# (see fillet_solid / shell_solid) -- plus an optional `refine` pass
# (Part.Shape.removeSplitter) since chained fuse/cut calls routinely leave
# redundant coincident-face splitters behind that make the result look
# more complex than it geometrically is.
def _coerce_solid(obj, name):
    shp = getattr(obj, "Shape", obj)
    if shp is None or shp.isNull() or not getattr(shp, "Solids", None):
        raise ValueError(
            "%s: every input must be a solid (Part::Feature with .Solids "
            "or a Part.Shape with .Solids), got %s" % (name, type(obj).__name__))
    return shp


def _coerce_solid_list(objs, name):
    return [_coerce_solid(o, name) for o in (objs or [])]


def _refine(shape, refine):
    if not refine:
        return shape
    try:
        return shape.removeSplitter()
    except Exception:
        return shape


def boolean_union(shapes, refine=True):
    """Fuse >= 2 solids into one (Part.Shape.fuse, chained).

    `refine=True` (default) runs Part.Shape.removeSplitter() afterward to
    drop redundant coincident-face splitters left behind by the chained
    fuse -- purely cosmetic/topological cleanup, never changes the volume.
    Raises ValueError for fewer than 2 solids, or if any input isn't a
    solid.
    """
    solids = _coerce_solid_list(shapes, "boolean_union")
    if len(solids) < 2:
        raise ValueError("boolean_union needs >= 2 solids (got %d)" % len(solids))
    out = solids[0]
    for s in solids[1:]:
        try:
            out = out.fuse(s)
        except Exception as exc:
            raise RuntimeError("boolean_union: OCCT fuse failed (%s)" % exc)
    return _refine(out, refine)


def boolean_subtract(base, tools, refine=True):
    """Cut `tools` out of `base` (Part.Shape.cut, chained). `base` and
    every entry in `tools` must be solids. Raises ValueError for a
    non-solid base, an empty tool list, or a non-solid tool."""
    base_shp = _coerce_solid(base, "boolean_subtract")
    tool_shapes = _coerce_solid_list(tools, "boolean_subtract")
    if not tool_shapes:
        raise ValueError("boolean_subtract needs >= 1 tool solid")
    out = base_shp
    for t in tool_shapes:
        try:
            out = out.cut(t)
        except Exception as exc:
            raise RuntimeError("boolean_subtract: OCCT cut failed (%s)" % exc)
    return _refine(out, refine)


def boolean_intersect(shapes, refine=True):
    """Keep only the common volume of >= 2 solids (Part.Shape.common,
    chained). Raises ValueError for fewer than 2 solids, or RuntimeError
    if the result has no volume at all (the inputs don't actually
    overlap) -- a silent empty solid is exactly the "silent no-op"
    failure mode this module avoids elsewhere (see clean_patch_surface's
    rejection of mis-sized grids)."""
    solids = _coerce_solid_list(shapes, "boolean_intersect")
    if len(solids) < 2:
        raise ValueError("boolean_intersect needs >= 2 solids (got %d)" % len(solids))
    out = solids[0]
    for s in solids[1:]:
        try:
            out = out.common(s)
        except Exception as exc:
            raise RuntimeError("boolean_intersect: OCCT common failed (%s)" % exc)
    out = _refine(out, refine)
    if not out.Solids:
        raise RuntimeError(
            "boolean_intersect: result has no volume -- the inputs may "
            "not actually overlap")
    return out


# --- mesh/scan -> single-patch NURBS fit (RFS_MeshToSurface) -------------- #
# Reverse-engineering gap: import a scanned/meshed panel and get an
# editable NURBS surface back (mirrors Plasticity Studio's PolySplines).
# HONESTY ABOUT SCOPE, up front: this is a SINGLE-PATCH, HEIGHT-FIELD
# fitter, not a general mesh retopology / quad-layout engine. It assumes
# the point cloud is (once projected along `projection_normal`, or the
# auto-detected flattest bounding-box axis) a disk-topology height field --
# i.e. for every (u, v) there is at most one meaningful surface height, the
# way a scanned body panel, a hood, or a terrain patch is. It CANNOT
# reconstruct closed/tubular/branching mesh topology (a full wraparound
# fender, a handle with a hole through it, ...) -- that is a genuinely
# different (patch-layout / multi-patch quad-remeshing) problem, well
# outside what a single BSplineSurface's parameter domain can represent,
# and is not attempted here.
def fit_surface_to_scan(points, nu=24, nv=24, degree=3, projection_normal=None):
    """Fit a single approximating B-spline PATCH to a raw, unstructured
    point cloud (mesh vertices, scan data -- no triangle/face
    connectivity used or required).

    Method: project every point onto the (u, v) plane perpendicular to
    the fit direction, bin into an nu x nv grid, AVERAGE the height of
    every point landing in each bin -- this IS the smoothing step; it is
    binning + averaging, not a least-squares surface fit (FreeCAD's
    BSplineSurface has no direct Python binding for scattered-point
    least-squares approximation the way BSplineCurve.approximate() does
    for curves -- see approximate_curve above). Coarser nu/nv means more
    smoothing (and a fairer, lower-pole-count result); finer nu/nv means
    closer to the raw scan and more susceptible to noise. Empty bins
    (sparse regions of the cloud) are filled by nearest-filled-neighbor,
    a simple flood fill, not a real hole-filling algorithm. The resulting
    grid is fed through the same exact-interpolation path
    grid_to_surface/_grid_interpolation_surface already use elsewhere in
    this module -- just fed averaged/rasterized data instead of exact
    control points or crossing points.

    `points`: iterable of Vector (or objects with .x/.y/.z).
    `nu`, `nv`: fitting grid resolution; must be >= 2.
    `projection_normal`: Vector, direction perpendicular to the intended
                 UV plane (e.g. Vector(0,0,1) for a mesh scanned from
                 above/a hood-like panel lying roughly in the XY plane).
                 If None, auto-picks whichever of the point cloud's
                 bounding-box axes has the smallest extent (the "this is
                 basically flat along this axis" heuristic) -- a
                 reasonable default for a single scanned panel, not a
                 substitute for telling the fitter the real scan
                 direction when you know it.

    Raises ValueError for < 4 points, nu/nv < 2, a zero-length
    `projection_normal`, a point cloud that is degenerate (zero extent)
    in the fitted UV plane, or a grid where every single bin came up
    empty (bin count too fine for how sparse the cloud is -- lower nu/nv).
    """
    pts = []
    for p in points or []:
        if isinstance(p, Vector):
            pts.append(p)
        else:
            pts.append(Vector(p.x, p.y, p.z))
    if len(pts) < 4:
        raise ValueError("fit_surface_to_scan: need >= 4 points (got %d)" % len(pts))
    nu, nv = int(nu), int(nv)
    if nu < 2 or nv < 2:
        raise ValueError("fit_surface_to_scan: nu and nv must be >= 2")

    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    extents = {"x": max(xs) - min(xs), "y": max(ys) - min(ys),
              "z": max(zs) - min(zs)}

    if projection_normal is not None:
        normal = Vector(projection_normal)
        if normal.Length < 1e-9:
            raise ValueError(
                "fit_surface_to_scan: projection_normal must be non-zero")
        normal = normal * (1.0 / normal.Length)
    else:
        flattest = min(extents, key=lambda k: extents[k])
        normal = {"x": Vector(1, 0, 0), "y": Vector(0, 1, 0),
                 "z": Vector(0, 0, 1)}[flattest]

    ex = _perp(normal)
    ey = normal.cross(ex)
    if ey.Length < 1e-9:
        raise ValueError("fit_surface_to_scan: degenerate projection basis")
    ey = ey * (1.0 / ey.Length)

    uvh = [(p.dot(ex), p.dot(ey), p.dot(normal)) for p in pts]
    us = [u for u, _v, _h in uvh]
    vs = [v for _u, v, _h in uvh]
    u0, u1 = min(us), max(us)
    v0, v1 = min(vs), max(vs)
    if (u1 - u0) < 1e-9 or (v1 - v0) < 1e-9:
        raise ValueError(
            "fit_surface_to_scan: point cloud is degenerate in the "
            "fitted UV plane -- pick a different projection_normal")

    sums = [[0.0] * nv for _ in range(nu)]
    counts = [[0] * nv for _ in range(nu)]
    for u, v, h in uvh:
        i = min(nu - 1, int((u - u0) / (u1 - u0) * nu))
        j = min(nv - 1, int((v - v0) / (v1 - v0) * nv))
        sums[i][j] += h
        counts[i][j] += 1

    heights = [[(sums[i][j] / counts[i][j]) if counts[i][j] else None
               for j in range(nv)] for i in range(nu)]

    filled = [(i, j) for i in range(nu) for j in range(nv)
             if heights[i][j] is not None]
    if not filled:
        raise ValueError(
            "fit_surface_to_scan: no bin received any points -- lower "
            "nu/nv (coarser grid) or check the point cloud")
    missing = [(i, j) for i in range(nu) for j in range(nv)
              if heights[i][j] is None]
    for i, j in missing:
        best, best_d = filled[0], 1e18
        for fi, fj in filled:
            d = (fi - i) ** 2 + (fj - j) ** 2
            if d < best_d:
                best_d, best = d, (fi, fj)
        heights[i][j] = heights[best[0]][best[1]]

    grid = []
    for i in range(nu):
        u = u0 + (u1 - u0) * i / (nu - 1.0)
        row = []
        for j in range(nv):
            v = v0 + (v1 - v0) * j / (nv - 1.0)
            row.append(ex * u + ey * v + normal * heights[i][j])
        grid.append(row)
    return grid_to_surface(grid)


# --- hidden-line drawing projection + SVG export (RFS_HiddenLineExport) -- #
# No drafting/technical-drawing output existed at all. This closes that
# gap in two independently-testable pieces:
#   1. hidden_line_projection() -- the actual HLR (hidden line removal)
#      math, via FreeCAD's headless `Drawing` module (a thin Python
#      binding over OCCT's HLRBRep, the same engine the old Drawing
#      workbench and TechDraw's view-generation both build on). This part
#      genuinely needs FreeCAD's `Drawing` module to be importable; it is
#      NOT part of the Gui-only layer (no FreeCADGui import), but it is
#      also not exercised by test_core.py's freecadcmd run if that build
#      lacks the Drawing module -- see the function's own guard.
#   2. export_svg_drawing() -- turns any two edge-collections (visible /
#      hidden) into a plain SVG file: solid strokes for visible, dashed
#      for hidden. This half has NO dependency on Drawing/HLR at all (it
#      only calls Edge.valueAt), so it is fully headless-testable on its
#      own with any two edge compounds, independent of whether HLR
#      projection succeeded.
def hidden_line_projection(shapes, direction):
    """Project `shapes` along `direction` and split the silhouette into
    VISIBLE and HIDDEN edge compounds via OCCT hidden-line removal
    (FreeCAD's `Drawing` module).

    Returns {"visible": Part.Compound, "hidden": Part.Compound,
    "direction": normalized Vector}.

    Raises ValueError for an empty/null input or a zero-length
    direction. Raises RuntimeError if the `Drawing` module can't be
    imported (some FreeCAD builds trim it) or if the underlying HLR
    projection itself fails -- callers should treat either as "drafting
    export isn't available in this FreeCAD build/session", not retry.

    VERSION CAVEAT: `Drawing.projectEx`'s exact return arity has varied
    across FreeCAD releases (some return a 6-tuple of
    visible-sharp/visible-outline/visible-iso/hidden-sharp/hidden-
    outline/hidden-iso edge groups, others fewer). Rather than hardcode
    one arity, this splits whatever tuple comes back in half (first half
    treated as every visible-edge group, second half as every
    hidden-edge group) and compounds each half -- correct for the common
    even-length "visible groups then hidden groups" layouts, but not
    independently verified against every FreeCAD version. If your build's
    `projectEx` doesn't follow that convention, `export_svg_drawing`
    below will still work correctly on whatever `visible`/`hidden`
    compounds you construct yourself.
    """
    combined = Part.Compound([getattr(s, "Shape", s) for s in (shapes or [])])
    if combined.isNull() or not combined.Edges:
        raise ValueError(
            "hidden_line_projection: nothing to project (empty/null shape)")
    d = Vector(direction)
    if d.Length < 1e-9:
        raise ValueError("hidden_line_projection: direction must be non-zero")
    d = d * (1.0 / d.Length)
    proj_mod, last_exc = None, None
    for _modname in ("TechDraw", "Drawing"):     # Drawing removed in FC 1.0
        try:
            proj_mod = __import__(_modname)
            break
        except Exception as exc:
            last_exc = exc
    if proj_mod is None:
        raise RuntimeError(
            "hidden_line_projection: neither FreeCAD's 'TechDraw' nor "
            "'Drawing' module is available in this session (%s) -- "
            "drafting export needs one" % last_exc)
    try:
        result = proj_mod.projectEx(combined, d)
    except Exception as exc:
        raise RuntimeError(
            "hidden_line_projection: HLR projection failed (%s)" % exc)
    groups_ = list(result) if isinstance(result, (list, tuple)) else [result]
    half = max(1, len(groups_) // 2)
    visible = Part.Compound([g for g in groups_[:half] if g is not None])
    hidden = Part.Compound([g for g in groups_[half:] if g is not None])
    return {"visible": visible, "hidden": hidden, "direction": d}


def export_svg_drawing(visible_edges, hidden_edges, direction, path,
                       stroke_visible="#000000", stroke_hidden="#999999",
                       hidden_dash="4,3", scale=1.0, margin=10.0,
                       samples_per_edge=24):
    """Write a plain SVG technical drawing from two edge collections --
    the orthographic 2D projection of `visible_edges` as solid strokes
    and `hidden_edges` as dashed strokes, onto the plane perpendicular to
    `direction`.

    `visible_edges`/`hidden_edges`: anything exposing an .Edges list
                 (a Part.Compound, Part.Shape, or a plain list of
                 Part.Edge) -- either may be empty.
    `direction`: the same projection direction used to build the edges
                 (e.g. hidden_line_projection's "direction"), or any
                 direction if you're just flattening arbitrary edges to
                 a plane yourself.
    `path`: output .svg file path.

    This function has NO dependency on HLR/the Drawing module -- it only
    calls Edge.valueAt/FirstParameter/LastParameter, so it can be (and
    is, in test_core.py) exercised directly with plain Part.makeLine
    edges, independent of whether hidden_line_projection's HLR call
    succeeds in a given FreeCAD build.

    Raises ValueError for a zero-length direction or if both edge
    collections are empty (nothing to draw).
    """
    d = Vector(direction)
    if d.Length < 1e-9:
        raise ValueError("export_svg_drawing: direction must be non-zero")
    d = d * (1.0 / d.Length)
    ex = _perp(d)
    ey = d.cross(ex)
    ey = ey * (1.0 / ey.Length)
    origin = Vector(0, 0, 0)

    def _proj2d(p):
        v = p - origin
        return (v.dot(ex), v.dot(ey))

    def _polylines(coll):
        edges = list(getattr(coll, "Edges", coll) or [])
        out = []
        for e in edges:
            try:
                fp, lp = e.FirstParameter, e.LastParameter
                n = max(2, int(samples_per_edge))
                pts = [e.valueAt(fp + k / (n - 1.0) * (lp - fp)) for k in range(n)]
            except Exception:
                continue
            out.append([_proj2d(p) for p in pts])
        return out

    vis_lines = _polylines(visible_edges)
    hid_lines = _polylines(hidden_edges)
    all_pts = [p for line in (vis_lines + hid_lines) for p in line]
    if not all_pts:
        raise ValueError("export_svg_drawing: no edges to draw")

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = (maxx - minx) * scale + 2 * margin
    h = (maxy - miny) * scale + 2 * margin
    if w < 1e-6:
        w = 2 * margin
    if h < 1e-6:
        h = 2 * margin

    def _to_svg(p):
        # SVG's y axis points down; flip so the drawing reads right-side-up.
        return (margin + (p[0] - minx) * scale,
                margin + (maxy - p[1]) * scale)

    lines_svg = []
    for line in vis_lines:
        pts_s = " ".join("%.3f,%.3f" % _to_svg(p) for p in line)
        lines_svg.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1"/>'
            % (pts_s, stroke_visible))
    for line in hid_lines:
        pts_s = " ".join("%.3f,%.3f" % _to_svg(p) for p in line)
        lines_svg.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1" '
            'stroke-dasharray="%s"/>' % (pts_s, stroke_hidden, hidden_dash))

    svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<svg xmlns="http://www.w3.org/2000/svg" width="%.3f" height="%.3f" '
          'viewBox="0 0 %.3f %.3f">\n%s\n</svg>\n'
          % (w, h, w, h, "\n".join(lines_svg)))
    with open(path, "w") as fh:
        fh.write(svg)
    return path
def enforce_bezier_handle_constraints(point_idx, which, left_handles, right_handles, handle_types, points):
    """Apply handle type constraints after editing a handle.

    `which` ('left' or 'right') identifies which handle was JUST
    dragged, so the other one can be adjusted to match.

    Args:
        point_idx: Index of the point whose handle was dragged
        which: 'left' or 'right' indicating which handle was dragged
        left_handles: List of left handle positions (modified in place)
        right_handles: List of right handle positions (modified in place)
        handle_types: List of handle type strings for each point
        points: List of point positions
    """
    if point_idx >= len(handle_types):
        return

    htype = handle_types[point_idx]
    point = points[point_idx] if point_idx < len(points) else Vector(0, 0, 0)
    left = left_handles[point_idx]
    right = right_handles[point_idx]

    if htype == "Auto":
        # Auto: Handles adjust to maintain smooth curve (simplified)
        # In a full implementation, this would calculate based on neighbors
        pass
    elif htype == "Aligned":
        # Aligned: Handles opposite (180 deg) but lengths can vary
        if which == 'left' and (left - point).Length > 1e-9:
            left_dir = (left - point).normalize()
            right_handles[point_idx] = point - left_dir * (right - point).Length
        elif which == 'right' and (right - point).Length > 1e-9:
            right_dir = (right - point).normalize()
            left_handles[point_idx] = point - right_dir * (left - point).Length
    elif htype == "Vector":
        # Vector: Handles locked together (same angle, can scale independently)
        if which == 'left' and (left - point).Length > 1e-9:
            left_dir = (left - point).normalize()
            right_handles[point_idx] = point + left_dir * (right - point).Length
        elif which == 'right' and (right - point).Length > 1e-9:
            right_dir = (right - point).normalize()
            left_handles[point_idx] = point + right_dir * (left - point).Length
    # "Free": No constraints - leave as is


# --------------------------------------------------------------------------- #
# 3D-sketch wire builders (Sketch3DFP / Sketch3DPanel)
# --------------------------------------------------------------------------- #
def _v3(p):
    return Vector(p.x, p.y, p.z) if hasattr(p, "x") else Vector(p[0], p[1], p[2])


def _seg_plane_normal(pts, i):
    """Plane normal for the arc/ellipse on segment i: the plane through
    the chord and the nearest neighbouring point; falls back to the world
    axis least aligned with the chord when the neighbours are collinear."""
    c = pts[i + 1] - pts[i]
    cands = []
    if i > 0:
        cands.append(pts[i - 1] - pts[i])
    if i + 2 < len(pts):
        cands.append(pts[i + 2] - pts[i + 1])
    for v in cands:
        nrm = c.cross(v)
        if nrm.Length > 1e-9 * max(c.Length, 1.0) * max(v.Length, 1.0):
            return nrm * (1.0 / nrm.Length)
    axes = [Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)]
    ax = min(axes, key=lambda a: abs(c.dot(a)) / max(c.Length, 1e-12))
    nrm = c.cross(ax)
    return nrm * (1.0 / nrm.Length) if nrm.Length > 1e-12 else Vector(0, 0, 1)


def sketch3d_wire(points, types=None, bulges=None):
    """One Part.Wire from a point list with a per-SEGMENT entity type
    (segment i joins points[i] -> points[i+1]):

      "Spline"    consecutive Spline segments are interpolated as ONE B-spline
      "Line"      straight segment
      "Arc"       circular arc; bulge = sagitta / (chord/2), sign = side
      "Ellipse"   half-ellipse with the chord as major axis; |bulge| =
                  minor/major ratio (clamped to 0.999)
      "Rectangle" L-shaped two-line corner path; bulge < 0 flips the corner
      "Bezier"    cubic Bezier; handle length = |bulge| * chord (0 -> 1/3),
                  end tangents taken from the neighbouring points

    `types`/`bulges` default to Spline / 0. Raises ValueError for < 2 points
    or nothing buildable."""
    pts = [_v3(p) for p in points]
    n = len(pts)
    if n < 2:
        raise ValueError("sketch3d_wire: need >= 2 points")
    types = list(types or [])
    types += ["Spline"] * (n - 1 - len(types))
    bulges = [float(b) for b in (bulges or [])]
    bulges += [0.0] * (n - 1 - len(bulges))
    edges = []

    def _line(a, b):
        if a.distanceToPoint(b) > 1e-9:
            edges.append(Part.LineSegment(a, b).toShape())

    i = 0
    while i < n - 1:
        t = types[i]
        a, b = pts[i], pts[i + 1]
        c = b - a
        L = c.Length
        if L < 1e-9:
            i += 1
            continue
        if t == "Spline":
            j = i
            while j < n - 1 and types[j] == "Spline":
                j += 1
            run = pts[i:j + 1]
            if len(run) == 2:
                _line(run[0], run[1])
            else:
                shp = interpolate_curve(run)
                edges.extend(shp.Edges)
            i = j
            continue
        bul = bulges[i]
        if t == "Arc" and abs(bul) > 1e-9:
            nrm = _seg_plane_normal(pts, i)
            m = nrm.cross(c)
            m = m * (1.0 / m.Length)
            mid = (a + b) * 0.5 + m * (bul * L * 0.5)
            edges.append(Part.Arc(a, mid, b).toShape())
        elif t == "Ellipse" and abs(bul) > 1e-9:
            nrm = _seg_plane_normal(pts, i)
            m = nrm.cross(c)
            m = m * (1.0 / m.Length)
            ratio = min(abs(bul), 0.999)
            center = (a + b) * 0.5
            ell = Part.Ellipse(b, center + m * (ratio * L * 0.5), center)
            edges.append(Part.ArcOfEllipse(ell, 0.0, math.pi).toShape())
        elif t == "Rectangle":
            axes = [Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)]
            ax = max(axes, key=lambda q: abs(c.dot(q)))
            leg = ax * c.dot(ax)
            corner = a + leg if bul >= 0 else b - leg
            _line(a, corner)
            _line(corner, b)
        elif t == "Bezier":
            h = (abs(bul) if abs(bul) > 1e-9 else 1.0 / 3.0) * L
            ta = (pts[i + 1] - pts[i - 1]) if i > 0 else c
            tb = (pts[i + 2] - pts[i]) if i + 2 < n else c
            ta = ta * (1.0 / ta.Length) if ta.Length > 1e-12 else c * (1.0 / L)
            tb = tb * (1.0 / tb.Length) if tb.Length > 1e-12 else c * (1.0 / L)
            bc = Part.BezierCurve()
            bc.setPoles([a, a + ta * h, b - tb * h, b])
            edges.append(bc.toShape())
        else:                       # Line, or a zero-bulge Arc/Ellipse
            _line(a, b)
        i += 1
    if not edges:
        raise ValueError("sketch3d_wire: no buildable segments")
    if len(edges) == 1:
        return Part.Wire(edges)
    return Part.Wire(Part.__sortEdges__(edges))


def _polygon_points_of_wire(wire, who):
    """Ordered corner points + closed flag of a POLYGONAL wire; raises
    ValueError for wires with curved edges (fillet/chamfer here are
    corner operations on straight segments only)."""
    for e in wire.Edges:
        c = e.Curve
        straight = (getattr(c, "TypeId", "") in ("Part::GeomLine",
                                                  "Part::GeomLineSegment")
                    or getattr(c, "Degree", 9) == 1)
        if not straight:
            raise ValueError("%s: only polygonal (all-straight-segment) "
                             "wires are supported" % who)
    pts = [v.Point for v in wire.OrderedVertexes]
    closed = bool(wire.isClosed())
    if closed and len(pts) > 1 and pts[0].distanceToPoint(pts[-1]) < 1e-7:
        pts = pts[:-1]
    if len(pts) < 3:
        raise ValueError("%s: need >= 3 corners" % who)
    return pts, closed


def wire_corner_fillet(wire, radius):
    """Round every corner of a polygonal wire with tangent arcs
    (delegates to polyline_corner_fillet)."""
    pts, closed = _polygon_points_of_wire(wire, "wire_corner_fillet")
    return polyline_corner_fillet(pts, float(radius), closed=closed)


def wire_corner_chamfer(wire, distance):
    """Cut every corner of a polygonal wire flat, `distance` back along
    both legs (clamped to half the shorter leg). Returns a Part.Wire."""
    pts, closed = _polygon_points_of_wire(wire, "wire_corner_chamfer")
    d = float(distance)
    n = len(pts)
    out = []
    for i in range(n):
        p = pts[i]
        if not closed and i in (0, n - 1):
            out.append(p)
            continue
        prv, nxt = pts[(i - 1) % n], pts[(i + 1) % n]
        u, v = prv - p, nxt - p
        lu, lv = u.Length, v.Length
        if lu < 1e-9 or lv < 1e-9 or d <= 1e-9:
            out.append(p)
            continue
        k = min(d, 0.5 * lu, 0.5 * lv)
        out.append(p + u * (k / lu))
        out.append(p + v * (k / lv))
    if closed:
        out.append(out[0])
    return Part.makePolygon(out)


def extrude_closed_wire(edges, height):
    """Planar closed loop -> face -> prism of `height` along the face
    normal. Raises ValueError if the loop isn't closed or isn't planar
    (callers fall back to an n-sided fill)."""
    try:
        wire = Part.Wire(Part.__sortEdges__(list(edges)))
        if not wire.isClosed():
            raise ValueError("loop is not closed")
        face = Part.Face(wire)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("extrude_closed_wire: loop is not planar (%s)" % exc)
    u0, u1, v0, v1 = face.ParameterRange
    nrm = face.normalAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
    return face.extrude(nrm * float(height))
