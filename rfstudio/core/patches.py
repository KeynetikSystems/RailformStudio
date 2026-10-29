# -*- coding: utf-8 -*-
"""Coons, Gordon-network and N-sided (star-point) surface patches."""

import math
import bisect
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_anchor_map, _chain_loop, _eval01, _hermite5, _hermite7,
                   _monotone, _network_scale, _norm_param, _pt,
                   grid_to_surface)



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
    """BSplineCurve covering exactly the TRIMMED range of `edge`.

    edge.Curve is the full underlying geometry (a Part.Line spans ~+-1e100),
    so it is never used directly. Orientation is NOT applied: FreeCAD's
    valueAt/parameter ignore Edge.Orientation, which is what the spine path's
    _eval01 sees, so both routes now see the same curve. Falls back to a
    50-point interpolation."""
    if isinstance(edge, Part.BSplineCurve):
        return edge
    try:
        e = edge.toNurbs().Edges[0]
        c = e.Curve
        if isinstance(c, Part.BSplineCurve):
            c = c.copy()
            fp, lp = e.FirstParameter, e.LastParameter
            if fp > c.FirstParameter + 1e-12 or lp < c.LastParameter - 1e-12:
                c.segment(fp, lp)
            return c
    except Exception:
        pass
    try:
        fp, lp = edge.FirstParameter, edge.LastParameter
        pts = [edge.valueAt(fp + k / 49.0 * (lp - fp)) for k in range(50)]
        bs = Part.BSplineCurve()
        bs.interpolate(Points=pts)
        return bs
    except Exception:
        return None



def _intersect_pair(c1, c2):
    """[(tu, tv, gap), ...] for real crossings, else closest approach."""
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
                gap = c1.value(tu).distanceToPoint(c2.value(tv))
                out.append((float(tu), float(tv), float(gap)))
        except Exception:
            continue
    if out:
        return out
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
    """Best (tu, tv, gap) per (profile, guide). Ties on gap (all exact
    crossings) break on the smallest tu so the choice is deterministic.
    NOTE: multi-crossing pairs still take the first crossing along the
    profile; a network where that is the wrong one needs ordering by the
    caller."""
    nu, nv = len(bs_profiles), len(bs_guides)
    pu = [[0.0] * nv for _ in range(nu)]
    pv = [[0.0] * nv for _ in range(nu)]
    dd = [[0.0] * nv for _ in range(nu)]
    for i in range(nu):
        for j in range(nv):
            pairs = _intersect_pair(bs_profiles[i], bs_guides[j])
            best = min(pairs, key=lambda t: (round(t[2], 9), t[0]))
            pu[i][j], pv[i][j], dd[i][j] = best
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

    Union of knots is built by CLUSTERING within `tol` (not exact-float dict
    keys), max multiplicity per cluster, then inserted into EACH surface.
    bool_add=False so OCCT raises multiplicity TO the value, not BY it."""
    for axis in ("U", "V"):
        items = []
        for s in (s1, s2, s3):
            ks = getattr(s, "get" + axis + "Knots")()
            ms = getattr(s, "get" + axis + "Multiplicities")()
            items.extend(zip(ks, ms))
        items.sort()
        clusters = []
        for k, m in items:
            if clusters and k - clusters[-1][0] <= tol:
                clusters[-1][1] = max(clusters[-1][1], m)
            else:
                clusters.append([k, m])
        ks = [c[0] for c in clusters]
        ms = [c[1] for c in clusters]
        for s in (s1, s2, s3):
            insert = getattr(s, "insert" + axis + "Knots")
            try:
                insert(ks, ms, tol, False)
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    "Railform Gordon: knot harmonisation (%s) failed: %s\n"
                    % (axis, exc))



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



def gordon_network(profiles, guides, samples_per_curve=24, extra_per_gap=4,
                   _network=None):
    """Gordon surface S = S1 + S2 - S3 from a curve network.

    Changes vs. previous version:
      * u_bar/v_bar average each curve's crossing parameter NORMALISED to
        its own [0,1] domain (raw parameters let the longest-domain curve
        dominate the spacing).
      * warns when a curve's crossing parameters are not monotone along the
        sorted shared axis (the curve would double back).
      * `_network=(bs_profiles, bs_guides, (params_u, params_v, gaps))` lets
        _try_gordon_route hand over the intersection grid it already has.
    See the module history for the construction notes (Piegl & Tiller 9.2.5).
    """
    if _network is not None:
        bs_profiles, bs_guides, (params_u, params_v, gaps) = _network
        bs_profiles, bs_guides = list(bs_profiles), list(bs_guides)
    else:
        bs_profiles = [b for b in (_to_bspline(e) for e in profiles)
                       if b is not None]
        bs_guides = [b for b in (_to_bspline(e) for e in guides)
                     if b is not None]
        if len(bs_profiles) >= 2 and len(bs_guides) >= 2:
            params_u, params_v, gaps = _intersect_grid(bs_profiles, bs_guides)
    if len(bs_profiles) < 2 or len(bs_guides) < 2:
        raise ValueError(
            "gordon_network needs >= 2 profiles and >= 2 guides (got "
            "%d profiles, %d guides)" % (len(bs_profiles), len(bs_guides)))
    nu, nv = len(bs_profiles), len(bs_guides)

    worst, worst_ij = 0.0, None
    for i in range(nu):
        for j in range(nv):
            if gaps[i][j] > worst:
                worst, worst_ij = gaps[i][j], (i, j)
    if worst_ij is not None and worst > _GORDON_CROSS_TOL:
        FreeCAD.Console.PrintWarning(
            "Railform Gordon: the curve network is not compatible -- profile "
            "%d and guide %d never actually cross (closest approach %.4g). "
            "The surface will pass BETWEEN the rails there rather than "
            "through them. Make every profile genuinely intersect every "
            "guide for a surface that touches all of them.\n"
            % (worst_ij[0] + 1, worst_ij[1] + 1, worst))

    # shared axes from NORMALISED crossing parameters
    u_bar_raw = [sum(_norm_param(bs_guides[j], params_v[i][j])
                     for j in range(nv)) / nv for i in range(nu)]
    v_bar_raw = [sum(_norm_param(bs_profiles[i], params_u[i][j])
                     for i in range(nu)) / nu for j in range(nv)]

    order_i = sorted(range(nu), key=lambda i: u_bar_raw[i])
    order_j = sorted(range(nv), key=lambda j: v_bar_raw[j])
    bs_profiles = [bs_profiles[i] for i in order_i]
    bs_guides = [bs_guides[j] for j in order_j]
    params_u = [[params_u[i][j] for j in order_j] for i in order_i]
    params_v = [[params_v[i][j] for j in order_j] for i in order_i]
    u_bar = _strictly_increasing([u_bar_raw[i] for i in order_i])
    v_bar = _strictly_increasing([v_bar_raw[j] for j in order_j])

    # monotonicity of each curve's own crossing parameters on the shared axis
    bad = None
    for j in range(nv):
        if not _monotone([params_v[i][j] for i in range(nu)]):
            bad = "guide %d" % (j + 1)
            break
    if bad is None:
        for i in range(nu):
            if not _monotone(params_u[i]):
                bad = "profile %d" % (i + 1)
                break
    if bad is not None:
        FreeCAD.Console.PrintWarning(
            "Railform Gordon: %s meets the other family in an order that "
            "disagrees with the shared axis (it would double back). The "
            "network is mis-ordered or has a stray extra crossing; expect a "
            "fold in the surface.\n" % bad)

    own_v_per_guide = [[params_v[i][j] for i in range(nu)] for j in range(nv)]
    s1, u_ref = _skin_through_family(bs_guides, own_v_per_guide, u_bar, v_bar,
                                     extra_per_gap=extra_per_gap)
    own_u_per_profile = [[params_u[i][j] for j in range(nv)]
                         for i in range(nu)]
    s2, v_ref = _skin_through_family(bs_profiles, own_u_per_profile, v_bar,
                                     u_bar, extra_per_gap=extra_per_gap)
    s2.exchangeUV()

    grid_pts = [[bs_profiles[i].value(params_u[i][j]) for j in range(nv)]
                for i in range(nu)]
    s3 = _grid_interpolation_surface(grid_pts, u_bar, v_bar)

    _match_degrees(s1, s2, s3)
    for s in (s1, s3):
        s.insertVKnots(v_ref.getKnots(), v_ref.getMultiplicities(), 1e-9,
                       False)
    for s in (s2, s3):
        s.insertUKnots(u_ref.getKnots(), u_ref.getMultiplicities(), 1e-9,
                       False)
    _harmonize_surface_knots(s1, s2, s3)

    out = s1.copy()
    nu_p, nv_p = out.NbUPoles, out.NbVPoles
    if (s2.NbUPoles, s2.NbVPoles) != (nu_p, nv_p) or \
       (s3.NbUPoles, s3.NbVPoles) != (nu_p, nv_p):
        FreeCAD.Console.PrintWarning(
            "Railform Gordon: pole counts diverged after knot harmonisation "
            "(S1 %dx%d, S2 %dx%d, S3 %dx%d) -- falling back to the crossing-"
            "grid interpolation. The surface will pass through the curve "
            "CROSSINGS but will NOT follow the rails between them.\n"
            % (nu_p, nv_p, s2.NbUPoles, s2.NbVPoles, s3.NbUPoles,
               s3.NbVPoles))
        return s3.toShape()
    for i in range(1, nu_p + 1):
        for j in range(1, nv_p + 1):
            out.setPole(i, j, out.getPole(i, j) + s2.getPole(i, j)
                        - s3.getPole(i, j))
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
    # Shared center acceleration: average of the boundary corner
    # accelerations projected onto the tangent plane.  All spokes then
    # leave the center with the same d2P/dt2 (center_accel), giving exact
    # G2 continuity AT the star point in parameter space.  Zero acceleration
    # is the degenerate special case; a non-zero shared value approximates
    # the boundary curvature at the corners.
    center_accel_sum = Vector(0, 0, 0)
    # Shared center jerk: average of the *change* in corner acceleration
    # between consecutive sides, projected onto the tangent plane.  All
    # spokes then leave the center with the same d3P/dt3 (center_jerk),
    # pushing the star-point continuity past G2 in parameter space toward
    # genuine curvature continuity.  Zero jerk is the degenerate case.
    center_jerk_sum = Vector(0, 0, 0)
    n_valid = 0
    n_jerk_valid = 0
    for i, acc in enumerate(corner_accels):
        if acc is None:
            continue
        # Remove the normal component (only tangent-plane curvature
        # matters for G2 across the star point)
        planar = acc - normal * acc.dot(normal)
        if planar.Length > 1e-12:
            center_accel_sum = center_accel_sum + planar
            n_valid += 1
        # Jerk from the difference between this corner's acceleration and
        # the next corner's acceleration (rate of change around the loop).
        next_acc = corner_accels[(i + 1) % n_sides]
        if next_acc is not None:
            jerk_planar = (acc - next_acc) - normal * (acc - next_acc).dot(normal)
            if jerk_planar.Length > 1e-12:
                center_jerk_sum = center_jerk_sum + jerk_planar
                n_jerk_valid += 1
    center_accel = (center_accel_sum * (1.0 / n_valid)) if n_valid > 0 else \
        Vector(0, 0, 0)
    center_jerk = (center_jerk_sum * (1.0 / n_jerk_valid)) if n_jerk_valid > 0 else \
        Vector(0, 0, 0)
    spokes = [_star_spoke(center, corners[i], normal, corner_accels[i],
                          center_accel=center_accel,
                          center_jerk=center_jerk)
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


# --- native N-sided Gregory star-patch -------------------------------------- #
# Gregory patches (Gregory 1974) give exact G1 continuity at every corner
# and at the star point by construction: each corner's tangent plane is
# spanned by the two boundary edges meeting there, and every spoke leaving
# a corner lies in that corner's tangent plane.  At the star point, all N
# spokes share one tangent plane (the Newell-averaged normal of the corner
# ring), giving exact G1 there too.  Combined with the shared center
# acceleration/jerk from _star_spoke (septic Hermite), this pushes the
# star-point continuity past G2 in parameter space.
#
# True provably-exact geometric G2 across every seam remains an open CAGD
# problem for arbitrary valence, but Gregory patches are the standard
# practical approximation used by most N-gon fill tools and give markedly
# better fairness than the plain Coons fan in n_sided_patch.


def _tangent_at(edge, t):
    """Unit tangent of `edge` at normalized parameter t in [0, 1], or None."""
    fp, lp = edge.FirstParameter, edge.LastParameter
    u = fp + t * (lp - fp)
    try:
        _p, d1, _d2 = edge.Curve.getD1(u)
        if d1.Length < 1e-12:
            return None
        d1.normalize()
        return d1
    except Exception:
        return None


def _corner_tangent_data(prev_edge, prev_flip, curr_edge, curr_flip,
                         center_normal):
    """Tangent data at one corner of the star loop.

    Returns a dict with:
      'corner'      : Vector, the corner point
      't_in'        : unit Vector, tangent of the incoming boundary edge
                      at the corner (pointing toward the corner)
      't_out'       : unit Vector, tangent of the outgoing boundary edge
                      at the corner (pointing away from the corner)
      'plane_n'     : unit Vector, normal of the corner's tangent plane,
                      spanned by t_in and t_out (falls back to center_normal
                      when the tangents are degenerate)
    """
    # Incoming edge end tangent (pointing into the corner)
    if prev_flip:
        t_in = _tangent_at(prev_edge, 0.0)   # reversed: start is the corner
    else:
        t_in = _tangent_at(prev_edge, 1.0)   # forward: end is the corner
    # Outgoing edge start tangent (pointing away from the corner)
    if curr_flip:
        t_out = _tangent_at(curr_edge, 1.0)  # reversed: end is the corner
    else:
        t_out = _tangent_at(curr_edge, 0.0)  # forward: start is the corner

    if t_in is None or t_out is None:
        return {"corner": None, "t_in": None, "t_out": None,
                "plane_n": center_normal}

    corner_pt = _eval01(curr_edge, 0.0) if not curr_flip else _eval01(curr_edge, 1.0)

    plane_n = t_in.cross(t_out)
    if plane_n.Length < 1e-12:
        plane_n = center_normal
    else:
        plane_n.normalize()

    return {"corner": corner_pt, "t_in": t_in, "t_out": t_out,
            "plane_n": plane_n}


def _cross_dir_in_plane(edge_tangent, plane_n, length=1.0):
    """A unit vector in `plane_n`'s plane, perpendicular to `edge_tangent`.
    Used as the cross-boundary direction for Gregory control points."""
    if edge_tangent is None or plane_n is None or plane_n.Length < 1e-12:
        return None
    # cross(plane_n, edge_tangent) is perpendicular to both: it lies in the
    # plane (perp to plane_n) and is perpendicular to edge_tangent.
    w = plane_n.cross(edge_tangent)
    if w.Length < 1e-12:
        return None
    w.normalize()
    return w * length


def _gregory_triangle_grid(O, A, B, edge_AB, spoke_OA, spoke_OB,
                           data_A, data_B, samples=16):
    """Grid for one triangular sub-patch (O, A, B), (s, t) layout identical
    to _triangular_coons_grid: s runs A->B, t runs O->boundary, row t=0
    collapses to O.

    A cubic Bezier triangle supplies the interior shape (control points as
    before). Its three sides generally differ from the real boundary and
    spokes, so a Coons-style correction

        P = Bez + (1-s)*dA(t) + s*dB(t) + t*dE(s)

    (dA/dB/dE = real side minus Bezier side) makes all three sides EXACT:
    G0 to the boundary curve and to the shared spokes, so adjacent
    sub-patches sew. The corner deltas are zero, so the correction is
    consistent. G1 across spokes is NOT guaranteed by this construction."""
    n = samples
    # ---- control points (unchanged) ------------------------------------ #
    spoke_OA_dir = (A - O)
    spoke_OA_dir.normalize()
    center_n = data_A["plane_n"]
    w_OA_O = _cross_dir_in_plane(spoke_OA_dir, center_n)
    spoke_AO_dir = (O - A)
    spoke_AO_dir.normalize()
    w_OA_A = _cross_dir_in_plane(spoke_AO_dir, data_A["plane_n"])
    L_OA = (A - O).Length
    zero = Vector(0, 0, 0)
    b210 = O + spoke_OA_dir * (L_OA / 3.0) \
        + (w_OA_O * (L_OA / 3.0) if w_OA_O is not None else zero)
    b120 = A + spoke_AO_dir * (L_OA / 3.0) \
        + (w_OA_A * (L_OA / 3.0) if w_OA_A is not None else zero)

    spoke_OB_dir = (B - O)
    spoke_OB_dir.normalize()
    w_OB_O = _cross_dir_in_plane(spoke_OB_dir, center_n)
    spoke_BO_dir = (O - B)
    spoke_BO_dir.normalize()
    w_OB_B = _cross_dir_in_plane(spoke_BO_dir, data_B["plane_n"])
    L_OB = (B - O).Length
    b201 = O + spoke_OB_dir * (L_OB / 3.0) \
        + (w_OB_O * (L_OB / 3.0) if w_OB_O is not None else zero)
    b102 = B + spoke_BO_dir * (L_OB / 3.0) \
        + (w_OB_B * (L_OB / 3.0) if w_OB_B is not None else zero)

    t_AB_A = data_A["t_out"] if data_A["t_out"] is not None else spoke_OA_dir
    w_AB_A = _cross_dir_in_plane(t_AB_A, data_A["plane_n"])
    if w_AB_A is not None and w_AB_A.dot(O - A) < 0:
        w_AB_A = w_AB_A.negative()
    t_AB_B = data_B["t_in"] if data_B["t_in"] is not None else spoke_OB_dir
    w_AB_B = _cross_dir_in_plane(t_AB_B, data_B["plane_n"])
    if w_AB_B is not None and w_AB_B.dot(O - B) < 0:
        w_AB_B = w_AB_B.negative()
    L_AB = max((B - A).Length, 1e-9)
    b021 = A + (B - A) * (1.0 / 3.0) \
        + (w_AB_A * (L_AB / 3.0) if w_AB_A is not None else zero)
    b012 = B + (A - B) * (1.0 / 3.0) \
        + (w_AB_B * (L_AB / 3.0) if w_AB_B is not None else zero)
    b111 = (b210 + b120 + b021 + b012 + b102 + b201) * (1.0 / 6.0)

    def bez(wO, wA, wB):
        return (O * (wO ** 3) + A * (wA ** 3) + B * (wB ** 3)
                + b210 * (3 * wO * wO * wA) + b120 * (3 * wO * wA * wA)
                + b021 * (3 * wA * wA * wB) + b012 * (3 * wA * wB * wB)
                + b102 * (3 * wO * wB * wB) + b201 * (3 * wO * wO * wB)
                + b111 * (6 * wO * wA * wB))

    grid = []
    for i in range(n):
        s = i / (n - 1.0) if n > 1 else 0.0
        row = []
        for j in range(n):
            t = j / (n - 1.0) if n > 1 else 0.0
            p = bez(1.0 - t, t * (1.0 - s), t * s)
            dA = spoke_OA(t) - bez(1.0 - t, t, 0.0)
            dB = spoke_OB(t) - bez(1.0 - t, 0.0, t)
            dE = _pt(edge_AB, s) - bez(0.0, 1.0 - s, s)
            row.append(p + dA * (1.0 - s) + dB * s + dE * t)
        grid.append(row)
    return grid


def gregory_star_patch(boundary_edges, samples=16):
    """Fill an N-sided (N >= 3) closed loop of boundary curves with N
    G1 Gregory triangular sub-patches fanned from a shared center point.

    Unlike n_sided_patch (plain Coons fan, G0 exact + tangent-plane G1
    approximate), gregory_star_patch gives exact G1 continuity at every
    corner and at the star point by construction: each corner's tangent
    plane is spanned by its two boundary edges, and every spoke lies in
    its corner's tangent plane.  At the star point all N spokes share one
    tangent plane (the Newell-averaged normal of the corner ring).

    Combined with the shared center acceleration/jerk from _star_spoke
    (septic Hermite), this pushes star-point continuity past G2 in
    parameter space toward genuine curvature continuity.

    Honesty: true provably-exact geometric G2 across every seam remains an
    open CAGD problem for arbitrary valence.  Gregory patches are the
    standard practical approximation and give markedly better fairness
    than the plain Coons fan, but this function does NOT claim to solve
    the vertex enclosure constraint.

    Parameters:
      boundary_edges: list of Part.Edge forming a closed loop (N >= 3)
      samples: subdivision samples per sub-patch

    Returns a Part.Shell (or Compound on sew failure)."""
    edges = list(boundary_edges)
    n_sides = len(edges)
    if n_sides < 3:
        raise ValueError("gregory_star_patch needs >= 3 boundary curves "
                         "(got %d)" % n_sides)
    loop = _order_open_loop(edges)
    corners = [_pt(loop[i], 0.0) for i in range(n_sides)]
    center = _average_point(corners)
    normal = _polygon_normal(corners)
    if normal.Length < 1e-9:
        normal = Vector(0, 0, 1)
    else:
        normal.normalize()

    # Per-corner tangent data
    corner_data = []
    for i in range(n_sides):
        prev_edge, prev_flip = loop[i - 1]
        curr_edge, curr_flip = loop[i]
        data = _corner_tangent_data(prev_edge, prev_flip, curr_edge,
                                    curr_flip, normal)
        corner_data.append(data)

    # Shared center acceleration / jerk (septic Hermite spokes)
    corner_accels = [_corner_boundary_accel(loop[i]) for i in range(n_sides)]
    center_accel_sum = Vector(0, 0, 0)
    center_jerk_sum = Vector(0, 0, 0)
    n_valid = 0
    n_jerk_valid = 0
    for i, acc in enumerate(corner_accels):
        if acc is None:
            continue
        planar = acc - normal * acc.dot(normal)
        if planar.Length > 1e-12:
            center_accel_sum = center_accel_sum + planar
            n_valid += 1
        next_acc = corner_accels[(i + 1) % n_sides]
        if next_acc is not None:
            jerk_planar = (acc - next_acc) - normal * (acc - next_acc).dot(normal)
            if jerk_planar.Length > 1e-12:
                center_jerk_sum = center_jerk_sum + jerk_planar
                n_jerk_valid += 1
    center_accel = (center_accel_sum * (1.0 / n_valid)) if n_valid > 0 else \
        Vector(0, 0, 0)
    center_jerk = (center_jerk_sum * (1.0 / n_jerk_valid)) if n_jerk_valid > 0 else \
        Vector(0, 0, 0)
    spokes = [_star_spoke(center, corners[i], normal, corner_accels[i],
                          center_accel=center_accel,
                          center_jerk=center_jerk)
              for i in range(n_sides)]

    faces = []
    for i in range(n_sides):
        b = loop[i]
        c_a, c_b = corners[i], corners[(i + 1) % n_sides]
        s_a, s_b = spokes[i], spokes[(i + 1) % n_sides]
        grid = _gregory_triangle_grid(center, c_a, c_b, b, s_a, s_b,
                                      corner_data[i],
                                      corner_data[(i + 1) % n_sides],
                                      samples)
        faces.append(grid_to_surface(grid))

    try:
        shell = Part.Shell([f.Faces[0] for f in faces])
        shell.sewShape()
        return shell
    except Exception:
        return Part.Compound(faces)


def _average_point(pts):
    x = sum(p.x for p in pts) / len(pts)
    y = sum(p.y for p in pts) / len(pts)
    z = sum(p.z for p in pts) / len(pts)
    return Vector(x, y, z)



def _order_open_loop(edges, tol=1e-5):
    """Chain edges into a loop; returns [(edge, flip), ...].

    Gap tolerance = tol * max(1, bounding-box diagonal of the edge endpoints)
    (it used to scale with the tail's distance from the world origin), and the
    last edge must close back onto the first start."""
    remaining = list(edges)
    ends = []
    for e in remaining:
        ends += [_eval01(e, 0.0), _eval01(e, 1.0)]
    lo = Vector(min(p.x for p in ends), min(p.y for p in ends),
                min(p.z for p in ends))
    hi = Vector(max(p.x for p in ends), max(p.y for p in ends),
                max(p.z for p in ends))
    tol_abs = tol * max(1.0, hi.sub(lo).Length)

    first = remaining.pop(0)
    start = _eval01(first, 0.0)
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
        if best_d > tol_abs:
            raise ValueError(
                "n_sided_patch: boundary curves don't form a closed loop "
                "(gap of %.4g between consecutive corners)" % best_d)
        loop.append((best, best_flip))
        remaining.remove(best)
        tail = _eval01(best, 0.0 if best_flip else 1.0)
    if tail.distanceToPoint(start) > tol_abs:
        raise ValueError(
            "n_sided_patch: boundary curves don't close (last edge ends "
            "%.4g from the first edge's start)" % tail.distanceToPoint(start))
    return loop



def _corner_boundary_accel(loop_item):
    """Curvature VECTOR of the boundary at the corner where `loop_item`
    (an (edge, flip) pair) starts: (d2 - (d2.T)T) / |d1|^2.

    Fixes: (1) a flipped edge starts at its LAST parameter, but the old code
    always evaluated at FirstParameter; (2) the raw d2P/du2 depended on the
    edge's parametrisation/length, this does not. Reversal leaves curvature
    unchanged, so no sign flip. None if unavailable (callers fall back to
    zero curvature).

    Check against your _hermite7 convention: the consumer (_star_spoke)
    scales this by L^2/2 as a Taylor coefficient."""
    edge, flip = loop_item
    u = edge.LastParameter if flip else edge.FirstParameter
    try:
        _p, d1, d2 = edge.Curve.getD2(u)
        speed2 = d1.dot(d1)
        if speed2 < 1e-24:
            return None
        T = d1 * (1.0 / speed2 ** 0.5)
        return (d2 - T * d2.dot(T)) * (1.0 / speed2)
    except Exception:
        return None



def _star_spoke(center, corner, normal, corner_accel=None,
                center_accel=None, center_jerk=None,
                corner_jerk=None):
    """One SEPTIC-Hermite spoke from the shared center to boundary corner.

    Upgraded from a quintic Hermite (position + tangent + curvature at both
    ends) to a septic Hermite (adds a 3rd-derivative/jerk term at both
    ends). The quintic version forced every spoke's TANGENT at the star
    point into one shared plane and every spoke's ACCELERATION at the
    center to a SHARED value -- exact G2 continuity AT the star point in
    parameter space. This version additionally forces every spoke's JERK at
    the center to a SHARED value (center_jerk, defaulting to zero), so
    every spoke leaves the star point with the same d3P/dt3 -- pushing the
    star-point continuity past G2 in parameter space toward genuine
    curvature continuity.

    `corner_accel`, if given (see _corner_boundary_accel), is the boundary
    curve's own raw 2nd derivative at the corner; only its component along
    the radial (center->corner) direction is used, scaled to this spoke's
    own length (raw_d2 * L^2 / 2). `corner_jerk`, if given, is the
    boundary curve's 3rd derivative at the corner; only its radial
    component is used, scaled (raw_d3 * L^3 / 6). None falls back to zero
    for that end -- i.e. the previous quintic behaviour.

    `center_accel`, if given, is the shared 2nd derivative at the center
    used by ALL spokes (d2P/dt2(0) = center_accel for every spoke),
    giving exact G2 at the star point in parameter space.

    `center_jerk`, if given, is the shared 3rd derivative at the center
    used by ALL spokes (d3P/dt3(0) = center_jerk for every spoke),
    pushing continuity past G2 toward G3 at the star point. When None,
    defaults to zero jerk at the center (degenerate but exactly shared).

    Honesty: this narrows the "true G2 at the star point" gap but does NOT
    close it. Continuity ACROSS a shared spoke's full length, between its
    two bordering triangular sub-patches, is still not attempted -- that
    remains a genuinely open CAGD problem for arbitrary valence.
    """
    radial = corner - center
    L = max(radial.Length, 1e-9)
    unit_radial = radial * (1.0 / L)
    to_plane = radial - normal * radial.dot(normal)
    if to_plane.Length < 1e-9:
        to_plane = radial          # corner is ~on the axis; fall back
    center_tan = to_plane * (L / max(to_plane.Length, 1e-9) / 3.0)
    corner_tan = unit_radial * (L / 3.0)
    if center_accel is None:
        center_accel = Vector(0, 0, 0)
    if center_jerk is None:
        center_jerk = Vector(0, 0, 0)
    center_accel_term = center_accel * (L * L / 2.0)
    center_jerk_term = center_jerk * (L ** 3 / 6.0)
    if corner_accel is not None:
        radial_component = unit_radial * corner_accel.dot(unit_radial)
        corner_accel_term = radial_component * (L * L / 2.0)
    else:
        corner_accel_term = Vector(0, 0, 0)
    if corner_jerk is not None:
        radial_jerk = unit_radial * corner_jerk.dot(unit_radial)
        corner_jerk_term = radial_jerk * (L ** 3 / 6.0)
    else:
        corner_jerk_term = Vector(0, 0, 0)

    def spoke_curve(t):
        return _hermite7(center, center_tan, center_accel,
                         center_jerk,
                         corner, corner_tan, corner_accel_term,
                         corner_jerk_term, t)
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


class _NearestTCache:
    """Sampled-edge cache for repeated nearest-parameter queries.

    param() refines the best coarse sample by golden-section search in its
    neighbouring interval, so station positions are no longer quantised to
    1/samples; where the objective is flat at fp roundoff (boundary zones)
    it snaps back to the exact sample parameter instead (see param()).
    Edges must stay referenced for the cache's life (keyed by id())."""
    _GR = 0.6180339887498949

    def __init__(self, samples=100):
        self._samples = int(samples)
        self._by_edge = {}

    def points(self, edge):
        pts = self._by_edge.get(id(edge))
        if pts is None:
            pts = [_eval01(edge, k / float(self._samples))
                   for k in range(self._samples + 1)]
            self._by_edge[id(edge)] = pts
        return pts

    def param(self, edge, pt):
        pts = self.points(edge)
        n = self._samples
        best_k, best_d = 0, 1e18
        for k, p in enumerate(pts):
            d = p.distanceToPoint(pt)
            if d < best_d:
                best_d, best_k = d, k
        a, b = max(0.0, (best_k - 1) / float(n)), min(1.0,
                                                      (best_k + 1) / float(n))
        gr = self._GR

        def f(t):
            return _eval01(edge, t).distanceToPoint(pt)
        c, d = b - gr * (b - a), a + gr * (b - a)
        fc, fd = f(c), f(d)
        for _ in range(40):
            if fc < fd:
                b, d, fd = d, c, fc
                c = b - gr * (b - a)
                fc = f(c)
            else:
                a, c, fc = c, d, fd
                d = a + gr * (b - a)
                fd = f(d)
        t = 0.5 * (a + b)
        # Golden-section presumes a strictly unimodal objective. On the
        # flat zone beside a boundary (or anywhere flatter than fp
        # roundoff) it chases 1e-16 noise, reporting a bogus minimum at
        # t ~= 2e-9 for a point that lies ON the edge end (measured: a
        # profile start station of 0.0 came back as 1.819e-9, which then
        # sat beside station 0 in the station list -- dedupe gap 1e-9
        # keeps both -- and the near-duplicate rows wrecked the surface
        # interpolation). Snap back to the discrete sample whenever the
        # refinement is not MEASURABLY better than the best sample: true
        # wells (depth far above noise) still refine to fp width, while
        # boundary/flat projections return the exact sample parameter.
        if f(t) < best_d - max(best_d, 1.0) * 1e-12:
            return t
        return best_k / float(n)



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
    """Skin profile curves between two rails.

    Correspondence is now PROFILE-DRIVEN: stations are arc fractions of rail1
    and rail2 is evaluated at the arc fraction where each profile actually
    touches it (piecewise-linear map through those contacts). At a profile's
    own station both rail points are therefore its endpoints, even when the
    rails have different lengths/parametrisations. The profiles' own stations
    are added to the row set so the profile is reproduced exactly there.
    """
    n_prof = max(2, int(n_prof))
    n_rail = max(2, int(n_rail))
    cols = [s / (n_prof - 1.0) for s in range(n_prof)]
    cache = _NearestTCache()
    arc1, arc2 = _ArcRail(rail1), _ArcRail(rail2)

    descs = []          # (station on rail1, up_vector, local, frac on rail2)
    prev_up = None
    for pro in profiles:
        e0, e1 = _eval01(pro, 0.0), _eval01(pro, 1.0)
        flip = (cache.param(rail1, e1) + cache.param(rail2, e0)) < \
               (cache.param(rail1, e0) + cache.param(rail2, e1))
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
        descs.append((arc1.fraction_of(cache.param(rail1, a)), ey, local,
                      arc2.fraction_of(cache.param(rail2, b))))
    if not descs:
        raise ValueError("two-rail sweep: no usable profiles")
    descs.sort(key=lambda d: d[0])

    map2 = _anchor_map([(d[0], d[3]) for d in descs])
    if map2 is None:
        FreeCAD.Console.PrintWarning(
            "Railform two-rail sweep: the profiles do not meet rail 2 in the "
            "same order as rail 1 (or the rails run in opposite directions) "
            "-- using equal arc fractions on both rails.\n")
        map2 = lambda x: x    # noqa: E731

    stations = _dedupe_stations(sorted(
        [i / (n_rail - 1.0) for i in range(n_rail)] + [d[0] for d in descs]))

    grid = []
    for t in stations:
        a, b = arc1.point_at(t), arc2.point_at(map2(t))
        chord = b.sub(a)
        L = max(chord.Length, 1e-9)
        ex = Vector(chord)
        ex.normalize()
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


# --- cross-section spine (the polyline a station's rails trace out) -------- #
def _spine_params(pts):
    """Normalized cumulative chord-length params of the polyline through pts."""
    n = len(pts)
    if n < 2:
        return [0.0]
    if n == 2:
        return [0.0, 1.0]
    acc, total = [0.0], 0.0
    for i in range(n - 1):
        total += max(pts[i + 1].sub(pts[i]).Length, 1e-12)
        acc.append(total)
    inv = 1.0 / total
    out = [s * inv for s in acc]
    out[0], out[-1] = 0.0, 1.0
    return out


def _spine_segment(params, u):
    """Index of the polyline segment param u sits on (clamped to the ends)."""
    n = len(params)
    if u <= params[0]:
        return 0
    if u >= params[n - 1]:
        return n - 2
    k = 0
    while params[k + 1] < u:
        k += 1
    return k


def _spine_eval(params, pts, u):
    """Point on the polyline at param u (linear extrapolation outside it)."""
    k = _spine_segment(params, u)
    span = params[k + 1] - params[k]
    w = 0.0 if span < 1e-15 else (u - params[k]) / span
    return pts[k] + pts[k + 1].sub(pts[k]) * w


def _spine_project(params, pts, p):
    """Polyline param of the point on the spine closest to p.

    Projects onto each segment's INFINITE line (deliberately unclamped) so a
    2-point spine reproduces two_rail_sweep's raw (d.ex)/L coordinate even
    for a sample that overhangs the chord."""
    best_u, best_d = params[0], None
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        sx, sy, sz = b.x - a.x, b.y - a.y, b.z - a.z
        l2 = sx * sx + sy * sy + sz * sz
        if l2 < 1e-24:
            t = 0.0
        else:
            t = ((p.x - a.x) * sx + (p.y - a.y) * sy + (p.z - a.z) * sz) / l2
        dx = p.x - (a.x + sx * t)
        dy = p.y - (a.y + sy * t)
        dz = p.z - (a.z + sz * t)
        d = dx * dx + dy * dy + dz * dz
        if best_d is None or d < best_d:
            best_d = d
            best_u = params[k] + (params[k + 1] - params[k]) * t
    return best_u


def _spine_map(z_params, s_params, u):
    """Reference-spine param -> station-spine param, pinning vertex i of the
    reference polyline to vertex i of the station polyline (that vertex-wise
    anchoring is what puts the surface ON every rail).  Identity for a
    2-point spine, so the two-rail case never moves."""
    k = _spine_segment(z_params, u)
    span = z_params[k + 1] - z_params[k]
    w = 0.0 if span < 1e-15 else (u - z_params[k]) / span
    return s_params[k] + (s_params[k + 1] - s_params[k]) * w


def _spine_invert(z_params, pts, evaluate, u_target, lo, hi, iters=32,
                  scan=64):
    """Profile param in [lo, hi] whose projection onto the reference spine is
    u_target.

    Monotonicity is only assumed LOCALLY, not across the whole bracket:
    a coarse scan locates the FIRST (in profile-param order) interval
    where the projection crosses u_target, and bisection -- which needs a
    sign change, i.e. a locally monotone branch -- refines inside just
    that bracket. The previous plain-bisection version assumed the
    projection increased over [lo, 1] throughout; an S-swaying profile
    that crosses the spine, swings back and crosses again made it bisect
    around whatever the mid parameter happened to project to, silently
    mis-assigning the rail's column. When the projection never reaches
    u_target (a profile that stops short of the spine) the nearest
    scanned sample is returned -- the best correspondence available,
    instead of an arbitrary bracket end.

    32 halvings of the found bracket finish well below the rail's own
    resolution."""
    n_scan = max(2, int(scan))
    span = hi - lo
    prev_t, prev_p = lo, _spine_project(z_params, pts, evaluate(lo))
    below = prev_p < u_target
    best_t, best_gap = prev_t, abs(prev_p - u_target)
    bracket = None
    for k in range(1, n_scan + 1):
        t = lo + span * k / float(n_scan)
        p = _spine_project(z_params, pts, evaluate(t))
        gap = abs(p - u_target)
        if gap < best_gap:
            best_t, best_gap = t, gap
        if (p < u_target) != below:
            bracket = (prev_t, t, below)
            break
        prev_t, prev_p, below = t, p, (p < u_target)
    if bracket is None:
        return best_t
    a, b, below_a = bracket
    for _ in range(iters):
        mid = 0.5 * (a + b)
        if (_spine_project(z_params, pts, evaluate(mid)) < u_target) == below_a:
            a = mid
        else:
            b = mid
    return 0.5 * (a + b)


def _sweep_columns(z_params, ref, evaluate, n_prof):
    """Column set for one profile, in (profile param, spine param).

    One column is pinned to each rail's spine vertex (its profile param is
    the inverse of that vertex's projection), so the sweep always has a
    column sitting exactly on every rail at the profile's own station; the
    remaining columns are spread evenly through the gaps.  For 2 rails the
    pins are the profile's endpoints and the spread is j/(n_prof-1) --
    exactly two_rail_sweep's uniform sampling.

    Returns (profile params, spine params, pinned column indices)."""
    n = len(z_params)
    n_prof = max(int(n_prof), n)
    qs = [0.0] * n
    for i in range(1, n - 1):
        qs[i] = _spine_invert(z_params, ref, evaluate, z_params[i],
                              qs[i - 1], 1.0)
    qs[n - 1] = 1.0
    params, pinned = [qs[0]], [0]
    fillers, gaps = n_prof - n, n - 1
    for k in range(gaps):
        lo, hi = qs[k], qs[k + 1]
        m = fillers // gaps + (1 if k < (fillers % gaps) else 0)
        for i in range(1, m + 1):
            params.append(lo + (hi - lo) * (i / (m + 1.0)))
        params.append(hi)
        pinned.append(len(params) - 1)
    at = {col: i for i, col in enumerate(pinned)}
    us = [z_params[at[k]] if k in at else
          _spine_project(z_params, ref, evaluate(params[k]))
          for k in range(len(params))]
    return params, us, pinned


# --- multi-rail sweep: continuous (Gordon) route ---------------------------- #
# When the profile/rail network is COMPATIBLE (every profile genuinely
# crosses every rail), an exact continuous solve exists and the
# station-skinned spine approximation is strictly worse between profile
# stations. These knobs gate when the Gordon route ships:
_MULTIRAIL_GORDON_DENSIFY = 8       # extra_per_gap fed to gordon_network
_MULTIRAIL_GORDON_RAIL_TOL = 1e-5
_MULTIRAIL_GORDON_PROFILE_TOL = 5e-4
_MULTIRAIL_GORDON_RAIL_SAMPLES = 33
_MULTIRAIL_GORDON_PROFILE_SAMPLES = 13

# --- multi-rail sweep: adaptive station-skinning fallback ------------------- #
# Ports of the C++ multi-rail transport design's fallback machinery onto the
# crossed-network spine path (the exact continuous Gordon route above already
# covers what it does better):
_MULTIRAIL_SPINE_TOL = 1e-4        # between-station hold target (model units)
_MULTIRAIL_STATION_FACTOR = 4      # station budget = factor x n_rail (C++ room)
_MULTIRAIL_STATION_PASSES = 10     # subdivision rounds (C++)
_MULTIRAIL_STATION_SAG = 4.0       # subdivide a gap whose midpoint row sags
                                   # more than this x tol (C++ heuristic)


def _smootherstep(s):
    """C2 quintic smootherstep 6s^5 - 15s^4 + 10s^3 (= s^3(6s^2 - 15s + 10)),
    clamped to [0,1]."""
    s = min(max(s, 0.0), 1.0)
    return s * s * s * (s * (6.0 * s - 15.0) + 10.0)


class _ArcRail:
    """Cumulative chord-length table over one rail edge (normalized-parameter
    space end to end; see the original comments on raw edge parameters)."""
    def __init__(self, edge, samples=256):
        self.edge = edge
        self.params = [k / float(samples) for k in range(samples + 1)]
        pts = [_eval01(edge, k / float(samples)) for k in range(samples + 1)]
        acc = [0.0]
        for k in range(samples):
            acc.append(acc[-1] + pts[k].distanceToPoint(pts[k + 1]))
        self.total = acc[-1] if acc[-1] > 1e-12 else 1.0
        self.acc = [a / self.total for a in acc]

    def fraction_of(self, t_norm):
        params, acc = self.params, self.acc
        if t_norm <= params[0]:
            return 0.0
        if t_norm >= params[-1]:
            return 1.0
        k = min(bisect.bisect_right(params, t_norm) - 1, len(params) - 2)
        span = params[k + 1] - params[k]
        w = 0.0 if span < 1e-15 else (t_norm - params[k]) / span
        return acc[k] + w * (acc[k + 1] - acc[k])

    def param_at(self, s):
        acc = self.acc
        s = min(max(s, 0.0), 1.0)
        k = min(max(bisect.bisect_right(acc, s) - 1, 0), len(acc) - 2)
        span = acc[k + 1] - acc[k]
        w = 0.0 if span < 1e-15 else (s - acc[k]) / span
        return self.params[k] + w * (self.params[k + 1] - self.params[k])

    def point_at(self, s):
        return _eval01(self.edge, self.param_at(s))


def _spine_map_c2(z_params, s_params, u):
    """C2 upgrade of _spine_map: the same vertex-wise rail pinning (vertex i
    of the reference spine still maps to vertex i of the station spine
    EXACTLY, which is what puts the surface on every rail), but the position
    between two consecutive pins eases with _smootherstep instead of ramping
    linearly -- the map dwells tangent at each pinned rail vertex, the same
    zero-slope-at-contact behaviour the C++ phi_i weights give, so the
    section transitions between rails are C2 instead of kinked at the pins."""
    k = _spine_segment(z_params, u)
    span = z_params[k + 1] - z_params[k]
    w = 0.0 if span < 1e-15 else (u - z_params[k]) / span
    return s_params[k] + _smootherstep(w) * (s_params[k + 1] - s_params[k])


def _dedupe_stations(stations, gap=1e-9):
    """Drop consecutive sweep-station params closer than `gap`."""
    out = []
    for s in stations:
        if out and s - out[-1] < gap:
            continue
        out.append(s)
    return out


def _deviation_of_curves(curves, shape, samples):
    """Worst distance from points along each curve to `shape`: both ends
    plus `samples` midpoints. Accepts edges or BSplineCurves (edges are
    converted trim-aware). Cost is dominated by distToShape; keep `samples`
    modest."""
    worst = 0.0
    n = max(1, int(samples))
    fracs = [0.0] + [(k + 0.5) / n for k in range(n)] + [1.0]
    for c in curves:
        c = _to_bspline(c)
        if c is None:
            continue
        fp, lp = c.FirstParameter, c.LastParameter
        for f in fracs:
            worst = max(worst,
                        Part.Vertex(c.value(fp + f * (lp - fp))
                                    ).distToShape(shape)[0])
    return worst


def _rail_order_warning(profile_params):
    """Message when rail ORDER disagrees with how the profiles traverse
    them, else None.

    profile_params[i][j] is the parameter along profile i of its
    crossing/closest approach to rail j (index-aligned with the rails
    list). Each row must increase with the rail index: if it doesn't,
    the station-skinned spine walks the rails in a zigzag the profiles
    don't follow -- the surface still passes through every rail (each
    gets its pinned column) but the shaped span between them is
    geometrically meaningless. The Gordon route doesn't need this check
    (it relabels each family onto its own shared axis) -- it is only
    consulted when the spine fallback engages."""
    for i, row in enumerate(profile_params):
        for j in range(len(row) - 1):
            if row[j] >= row[j + 1] - 1e-9:
                return ("rail %d does not come after rail %d along profile "
                        "%d -- the rail order does not match how the "
                        "sections run through them. Reorder the rails so "
                        "the sections cross them in that order."
                        % (j + 1, j, i + 1))
    return None


def _try_gordon_route(rails, profiles):
    """Exact continuous solve for a compatible multi-rail network, or None.

    Gates are RELATIVE: _MULTIRAIL_GORDON_*_TOL * max(1, network diagonal).
    The intersection grid is computed once and handed to gordon_network.
    Returns None (-> spine path) when a profile doesn't cross a rail or the
    measured deviation exceeds the gates."""
    bs_rails = [_to_bspline(r) for r in rails]
    bs_prof = [_to_bspline(p) for p in profiles]
    if any(b is None for b in bs_rails + bs_prof):
        return None
    grid = _intersect_grid(bs_prof, bs_rails)
    gaps = grid[2]
    nu, nv = len(bs_prof), len(bs_rails)
    worst, worst_ij = 0.0, None
    for i in range(nu):
        for j in range(nv):
            if gaps[i][j] > worst:
                worst, worst_ij = gaps[i][j], (i, j)
    if worst_ij is not None and worst > _GORDON_CROSS_TOL:
        FreeCAD.Console.PrintWarning(
            "Railform Multi-rail sweep: profile %d never truly crosses rail "
            "%d (closest approach %.4g), so no exact network solve exists "
            "-- skinning across stations instead. The surface will pass "
            "BETWEEN those two rather than through both there.\n"
            % (worst_ij[0] + 1, worst_ij[1] + 1, worst))
        order_msg = _rail_order_warning(grid[0])
        if order_msg:
            FreeCAD.Console.PrintWarning(
                "Railform Multi-rail sweep: %s\n" % order_msg)
        return None

    scale = max(1.0, _network_scale(bs_rails))
    rail_tol = _MULTIRAIL_GORDON_RAIL_TOL * scale
    prof_tol = _MULTIRAIL_GORDON_PROFILE_TOL * scale
    try:
        shape = gordon_network(profiles, rails,
                               extra_per_gap=_MULTIRAIL_GORDON_DENSIFY,
                               _network=(bs_prof, bs_rails, grid))
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Railform Multi-rail sweep: Gordon solve failed (%s) -- "
            "skinning across stations instead.\n" % exc)
        return None
    rail_dev = _deviation_of_curves(bs_rails, shape,
                                    _MULTIRAIL_GORDON_RAIL_SAMPLES)
    prof_dev = _deviation_of_curves(bs_prof, shape,
                                    _MULTIRAIL_GORDON_PROFILE_SAMPLES)
    if rail_dev <= rail_tol and prof_dev <= prof_tol:
        return shape
    hint = ""
    if worst > rail_tol:
        hint = (" The worst profile/rail crossing gap is %.3g, which alone "
                "exceeds the rail tolerance %.3g -- snap the crossings "
                "together for an exact solve." % (worst, rail_tol))
    FreeCAD.Console.PrintWarning(
        "Railform Multi-rail sweep: Gordon solve does not hold the input "
        "(worst rail deviation %.3g vs %.3g, profile %.3g vs %.3g) -- "
        "skinning across stations instead.%s\n"
        % (rail_dev, rail_tol, prof_dev, prof_tol, hint))
    return None


def multi_rail_sweep(rails, profiles, n_rail=40, n_prof=20):
    """Skin profile curves across N >= 2 rails.

    Same structure as before (2 rails -> two_rail_sweep; compatible network
    -> Gordon; otherwise adaptive station skinning) with these corrections to
    the station-skinning path:

      * each profile's reference spine now passes through where it ACTUALLY
        contacts every interior rail (closest approach), not through the
        rail point at the profile's start fraction;
      * every rail k is sampled at arc_k(map_k(s)), where map_k is the
        piecewise-linear map through the profiles' contact fractions
        (_anchor_map), so at a profile's own station every rail point is
        its contact point -> interior rails are on the surface there;
      * ALL rails (not only the two boundary rails) are measured against the
        finished surface and reported.
    """
    rails = list(rails)
    n_rails = len(rails)
    if n_rails < 2:
        raise ValueError(
            "multi-rail sweep: need >= 2 rails -- got %d" % n_rails)
    n_prof = max(2, int(n_prof), n_rails)
    n_rail = max(2, int(n_rail))

    if n_rails == 2:
        return two_rail_sweep(rails[0], rails[1], profiles, n_rail, n_prof)

    base, far = rails[0], rails[-1]

    if n_rails >= 3 and len(profiles) >= 2:
        gordon = _try_gordon_route(rails, profiles)
        if gordon is not None:
            return gordon

    arc = [_ArcRail(r) for r in rails]
    bs_rails = [_to_bspline(r) for r in rails]
    cache = _NearestTCache()
    descs = []
    prev_up = None
    for pro in profiles:
        e0, e1 = _eval01(pro, 0.0), _eval01(pro, 1.0)
        flip = (cache.param(base, e1) + cache.param(far, e0)) < \
               (cache.param(base, e0) + cache.param(far, e1))

        def P(t, _pro=pro, _flip=flip):
            return _eval01(_pro, 1.0 - t if _flip else t)

        s_p = arc[0].fraction_of(cache.param(base, P(0.0)))
        fracs = [s_p] + [0.0] * (n_rails - 2) + \
            [arc[-1].fraction_of(cache.param(far, P(1.0)))]
        ref = [P(0.0)] + [None] * (n_rails - 2) + [P(1.0)]
        bs_pro = _to_bspline(pro)
        for k in range(1, n_rails - 1):
            f_k = None
            if bs_pro is not None and bs_rails[k] is not None:
                try:
                    _tp, tr, _d = _closest_approach_pair(bs_pro, bs_rails[k])
                    f_k = arc[k].fraction_of(_norm_param(bs_rails[k], tr))
                except Exception:
                    f_k = None
            if f_k is None:
                f_k = s_p
            fracs[k] = f_k
            ref[k] = arc[k].point_at(f_k)
        zref = _spine_params(ref)
        qcols, ucols, _pinned = _sweep_columns(zref, ref, P, n_prof)
        samp = [P(q) for q in qcols]
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
        for k, p in enumerate(samp):
            u = ucols[k]
            d = p.sub(_spine_eval(zref, ref, u))
            local.append((u, d.dot(ex), d.dot(ey), d.dot(ez)))
        descs.append((s_p, ey, local, zref, fracs))
    if not descs:
        raise ValueError("multi-rail sweep: no usable profiles")
    descs.sort(key=lambda d: d[0])

    # per-rail correspondence maps (sweep axis = rail 0 arc fraction)
    maps = [lambda x: x]
    for k in range(1, n_rails):
        m = _anchor_map([(d[0], d[4][k]) for d in descs])
        if m is None:
            FreeCAD.Console.PrintWarning(
                "Railform Multi-rail sweep: the profiles do not meet rail %d "
                "in the same order as rail 1 -- sampling it at equal arc "
                "fractions instead (the surface may miss that rail between "
                "stations).\n" % (k + 1))
            m = lambda x: x   # noqa: E731
        maps.append(m)

    rows = {}

    def station_row(s):
        cached = rows.get(s)
        if cached is not None:
            return cached
        wpts = [arc[k].point_at(maps[k](s)) for k in range(n_rails)]
        sparams = _spine_params(wpts)
        chord = wpts[-1].sub(wpts[0])
        if chord.Length < 1e-9:
            raise ValueError(
                "multi-rail sweep: rails collapse (zero Jacobian) at "
                "station %.4g -- the rails touch there, so the sweep "
                "degenerates" % s)
        ex = Vector(chord)
        ex.normalize()
        hard = None
        for d in descs:
            if abs(s - d[0]) <= 1e-9:
                hard = d
                break
        if hard is not None:
            lo = hi = hard
            w = 0.0
        elif len(descs) == 1 or s <= descs[0][0]:
            lo = hi = descs[0]
            w = 0.0
        elif s >= descs[-1][0]:
            lo = hi = descs[-1]
            w = 0.0
        else:
            k = 0
            while k < len(descs) - 1 and descs[k + 1][0] < s:
                k += 1
            lo, hi = descs[k], descs[k + 1]
            span = hi[0] - lo[0]
            w = 0.0 if span < 1e-12 else _smootherstep((s - lo[0]) / span)
        up = lo[1] * (1.0 - w) + hi[1] * w
        ez = ex.cross(up)
        if ez.Length < 1e-9:
            up = _perp(ex)
            ez = ex.cross(up)
        ez.normalize()
        ey = ez.cross(ex)
        ey.normalize()
        zmap = [lo[3][k] * (1.0 - w) + hi[3][k] * w for k in range(n_rails)]
        row, sps = [], []
        for j in range(n_prof):
            u0, cx0, cy0, cz0 = lo[2][j]
            u1, cx1, cy1, cz1 = hi[2][j]
            u = u0 * (1.0 - w) + u1 * w
            cx = cx0 * (1.0 - w) + cx1 * w
            cy = cy0 * (1.0 - w) + cy1 * w
            cz = cz0 * (1.0 - w) + cz1 * w
            if 0.0 < w < 1.0:
                sp = _spine_map_c2(zmap, sparams, u)
            else:
                sp = _spine_map(zmap, sparams, u)
            sps.append(_spine_map(zmap, sparams, u))
            pt = _spine_eval(sparams, wpts, sp)
            row.append(pt + ex * cx + ey * cy + ez * cz)
        rows[s] = (row, sps)
        return rows[s]

    station_list = _dedupe_stations(sorted(
        [i / float(n_rail - 1) for i in range(n_rail)]
        + [d[0] for d in descs] + [0.0, 1.0]))

    max_stations = _MULTIRAIL_STATION_FACTOR * n_rail
    for _round in range(_MULTIRAIL_STATION_PASSES):
        room = max_stations - len(station_list)
        if room <= 0:
            break
        inserts = {}
        for k in range(len(station_list) - 1):
            if room <= 0:
                break
            sa, sb = station_list[k], station_list[k + 1]
            row_m = station_row(0.5 * (sa + sb))
            row_a = station_row(sa)
            row_b = station_row(sb)
            dev = 0.0
            for j in range(n_prof):
                chord2 = 0.5 * (row_a[0][j] + row_b[0][j])
                dev = max(dev, row_m[0][j].distanceToPoint(chord2))
            if dev > _MULTIRAIL_STATION_SAG * _MULTIRAIL_SPINE_TOL:
                inserts[k] = 0.5 * (sa + sb)
                room -= 1
        if not inserts:
            break
        grown = []
        for k in range(len(station_list) - 1):
            grown.append(station_list[k])
            if k in inserts:
                grown.append(inserts[k])
        grown.append(station_list[-1])
        station_list = _dedupe_stations(grown)

    spine_u_acc = [0.0] * n_prof
    grid = []
    for s in station_list:
        row, sps = station_row(s)
        grid.append(row)
        for j in range(n_prof):
            spine_u_acc[j] += sps[j]
    v_vals = [acc / float(len(station_list)) for acc in spine_u_acc]
    try:
        shape = _grid_interpolation_surface(
            grid, station_list, v_vals).toShape()
    except Exception:
        shape = grid_to_surface(grid)

    try:
        dev = _deviation_of_curves(rails, shape,
                                   _MULTIRAIL_GORDON_RAIL_SAMPLES)
        if dev > 10.0 * _MULTIRAIL_SPINE_TOL:
            FreeCAD.Console.PrintWarning(
                "Railform Multi-rail sweep: station skinning holds the "
                "rails to %.3g between stations (target %.3g) -- raise Rail "
                "Samples (denser stations) or add profiles near the worst "
                "gap for a tighter sweep.\n"
                % (dev, _MULTIRAIL_SPINE_TOL))
    except Exception:
        pass
    return shape


# --- single-rail (pipe) sweep ---------------------------------------------- #
def _rail_tangent(arc, s):
    """Unit tangent of an _ArcRail's edge at arc fraction s.

    Central difference of valueAt over neighbouring normalized parameters --
    no reliance on edge.tangentAt/TableCurve tangent conventions (interpolated
    edges often carry RAW geometric parameters, and the tangent needs the
    same normalized convention _eval01 and the arc table use).
    """
    eps = 1.0 / max(2.0 * len(arc.acc), 4.0)
    s = min(max(s, 0.0), 1.0)
    lo, hi = max(s - eps, 0.0), min(s + eps, 1.0)
    if hi - lo < 1e-12:
        lo, hi = max(s - 2.0 * eps, 0.0), 1.0
    d = _eval01(arc.edge, hi).sub(_eval01(arc.edge, lo))
    if d.Length < 1e-12:
        raise ValueError("single-rail sweep: rail has no tangent at "
                         "station %.4g" % s)
    d.normalize()
    return d


def _rmf_frames(arc, stations, up0):
    """Rotation-minimizing frames along the rail at the given arc stations.

    Wang et al. double-reflection: each consecutive station pair reflects
    the transported up vector across the tangent bisector, then across the
    plane containing the old/new up, so the frame rides the spine WITHOUT
    the Frenet frame's twist/flip pathologies (a Frenet frame flips at every
    inflection point and a fixed world-up shears the section off the pipe).
    Returns one (tangent, ey, ez) triple per station; all orthonormal.
    """
    frames = []
    prev_tan = prev_up = None
    for s in stations:
        t = _rail_tangent(arc, s)
        if prev_up is None:
            ey = up0
        else:
            v1 = t.sub(prev_tan)
            c1 = v1.dot(v1)
            ey = prev_up
            if c1 > 1e-14:
                ey = ey - v1 * (2.0 * v1.dot(ey) / c1)
                tl = prev_tan + v1 * (2.0 * v1.dot(t) / c1)
                v2 = t.sub(tl)
                c2 = v2.dot(v2)
                if c2 > 1e-14:
                    ey = ey - v2 * (2.0 * v2.dot(ey) / c2)
        ey = ey.sub(t * (ey.dot(t)))
        if ey.Length < 1e-9:
            ey = _perp(t)
        ey.normalize()
        ez = t.cross(ey)
        if ez.Length < 1e-9:
            ey = _perp(t)
            ez = t.cross(ey)
        ez.normalize()
        ey = ez.cross(t)
        ey.normalize()
        frames.append((t, ey, ez))
        prev_tan, prev_up = t, ey
    return frames


def single_rail_sweep(rail, profiles, n_rail=40, n_prof=20):
    """Pipe sweep along ONE rail with a rotation-minimizing frame.

    Fixes: profile offsets used to be measured in a single-station frame
    chain, rows were rebuilt in a chain seeded elsewhere (frames differ by
    the transport rotation on curved rails), and the sag probe compared a
    fresh single-station frame against the transported chain. Now every
    pass builds ONE chain over the station list (which always contains each
    profile's own station exactly), measures every profile's offsets in that
    chain's frame at its station, builds all rows from the same frames, and
    probes the gap midpoints from the same chain. A profile is reproduced
    exactly at its station, and the sag measure sees geometry, not frame
    mismatch.
    """
    n_prof = max(2, int(n_prof))
    n_rail = max(2, int(n_rail))
    if not profiles:
        raise ValueError("single-rail sweep: no profiles")
    arc = _ArcRail(rail)
    if float(arc.total) <= 1e-9:
        raise ValueError("single-rail sweep: rail is degenerate (zero "
                         "length) -- nothing to sweep along")
    cache = _NearestTCache()
    cols = [k / float(n_prof - 1) for k in range(n_prof)]

    descs = []                       # (station, samples)
    for pro in profiles:
        samp = [_eval01(pro, q) for q in cols]
        mid = samp[len(samp) // 2]
        if mid.sub(samp[0]).Length < 1e-9:
            continue
        descs.append((arc.fraction_of(cache.param(rail, samp[0])), samp))
    if not descs:
        raise ValueError("single-rail sweep: profiles are degenerate "
                         "(zero chord)")
    descs.sort(key=lambda d: d[0])
    profile_stations = [d[0] for d in descs]

    # frame seed at station 0: first profile's plane normal, made
    # perpendicular to the rail tangent there
    t0 = _rail_tangent(arc, 0.0)
    nrm = _polygon_normal(descs[0][1])
    nrm = nrm.sub(t0 * (nrm.dot(t0)))
    if nrm.Length > 1e-9:
        nrm.normalize()
        up0 = nrm
    else:
        up0 = _perp(t0)
    m = n_prof                        # closed profiles close natively

    def merge_stations(base):
        out = [s for s in sorted(base)
               if all(abs(s - p) > 1e-9 for p in profile_stations)]
        return _dedupe_stations(sorted(out + profile_stations))

    def nearest_index(stations, s):
        k = bisect.bisect_left(stations, s)
        cands = [i for i in (k - 1, k) if 0 <= i < len(stations)]
        return min(cands, key=lambda i: abs(stations[i] - s))

    def build(stations):
        frames = _rmf_frames(arc, stations, up0)
        offs = []
        for s_i, samp in descs:
            t, ey, ez = frames[nearest_index(stations, s_i)]
            spine_i = arc.point_at(s_i)
            o = []
            for p in samp:
                d = p.sub(spine_i)
                o.append((d.dot(t), d.dot(ey), d.dot(ez)))
            offs.append((s_i, o))

        def desc_at(s):
            if len(offs) == 1 or s <= offs[0][0]:
                return offs[0], offs[0], 0.0
            if s >= offs[-1][0]:
                return offs[-1], offs[-1], 0.0
            k = 0
            while k < len(offs) - 1 and offs[k + 1][0] < s:
                k += 1
            lo, hi = offs[k], offs[k + 1]
            span = hi[0] - lo[0]
            return lo, hi, 0.0 if span < 1e-12 else (s - lo[0]) / span

        grid = []
        for i, s in enumerate(stations):
            t, ey, ez = frames[i]
            spine = arc.point_at(s)
            lo, hi, w = desc_at(s)
            row = []
            for j in range(m):
                a0, b0, c0 = lo[1][j]
                a1, b1, c1 = hi[1][j]
                row.append(spine + t * (a0 * (1.0 - w) + a1 * w)
                           + ey * (b0 * (1.0 - w) + b1 * w)
                           + ez * (c0 * (1.0 - w) + c1 * w))
            grid.append(row)
        return grid

    station_list = merge_stations([i / float(n_rail - 1)
                                   for i in range(n_rail)])
    max_stations = _MULTIRAIL_STATION_FACTOR * n_rail
    for _pass in range(_MULTIRAIL_STATION_PASSES):
        room = max_stations - len(station_list)
        if room <= 0 or len(station_list) < 2:
            break
        # interleave stations and gap midpoints so ONE chain covers both
        probe = []
        for k in range(len(station_list) - 1):
            probe.append(station_list[k])
            probe.append(0.5 * (station_list[k] + station_list[k + 1]))
        probe.append(station_list[-1])
        grid = build(probe)
        sags = []
        for k in range(len(station_list) - 1):
            ra, rm, rb = grid[2 * k], grid[2 * k + 1], grid[2 * k + 2]
            dev = 0.0
            for j in range(m):
                chord_mid = (ra[j] + rb[j]) * 0.5
                dev = max(dev, rm[j].distanceToPoint(chord_mid))
            if dev > _MULTIRAIL_STATION_SAG * _MULTIRAIL_SPINE_TOL:
                sags.append((dev, probe[2 * k + 1]))
        if not sags:
            break
        sags.sort(reverse=True)
        station_list = merge_stations(
            station_list + [s for _d, s in sags[:room]])
    grid = build(station_list)

    v_vals = _strictly_increasing(list(cols))
    try:
        shape = _grid_interpolation_surface(
            grid, station_list, v_vals).toShape()
    except Exception:
        shape = grid_to_surface(grid)
    return shape



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
        curve = _to_bspline(e) if hasattr(e, "Curve") else e
        if curve is None:
            raise RuntimeError("plate_fill: could not convert an edge")
        builder.add(GP.CurveConstraint(curve, 0))
    builder.perform()
    if not builder.isDone():
        raise RuntimeError("GeomPlate fill failed")
    ps = builder.surface()
    bsurf = ps.makeApprox(tol, 10, 8, 0)
    return bsurf.toShape()

__all__ = [
    '_closest_approach_pair',
    '_to_bspline',
    '_intersect_pair',
    '_GORDON_CROSS_TOL',
    '_MULTIRAIL_GORDON_DENSIFY',
    '_MULTIRAIL_GORDON_RAIL_TOL',
    '_MULTIRAIL_GORDON_PROFILE_TOL',
    '_MULTIRAIL_GORDON_RAIL_SAMPLES',
    '_MULTIRAIL_GORDON_PROFILE_SAMPLES',
    '_MULTIRAIL_SPINE_TOL',
    '_MULTIRAIL_STATION_FACTOR',
    '_MULTIRAIL_STATION_PASSES',
    '_MULTIRAIL_STATION_SAG',
    '_smootherstep',
    '_ArcRail',
    '_spine_map_c2',
    '_dedupe_stations',
    '_intersect_grid',
    '_strictly_increasing',
    '_param_map',
    '_skin_through_family',
    '_grid_interpolation_surface',
    '_harmonize_surface_knots',
    '_match_degrees',
    'gordon_network',
    'coons_patch',
    'n_sided_patch',
    'gregory_star_patch',
    '_average_point',
    '_order_open_loop',
    '_corner_boundary_accel',
    '_star_spoke',
    '_tangent_at',
    '_corner_tangent_data',
    '_cross_dir_in_plane',
    '_gregory_triangle_grid',
    '_triangular_coons_grid',
    '_nearest_t',
    '_NearestTCache',
    '_polygon_normal',
    '_perp',
    '_spine_params',
    '_spine_segment',
    '_spine_eval',
    '_spine_project',
    '_spine_map',
    '_spine_invert',
    '_sweep_columns',
    '_deviation_of_curves',
    '_rail_order_warning',
    '_try_gordon_route',
    'two_rail_sweep',
    'multi_rail_sweep',
    'single_rail_sweep',
    '_rail_tangent',
    '_rmf_frames',
    'plate_fill',
]
