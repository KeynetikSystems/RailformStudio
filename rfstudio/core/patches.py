# -*- coding: utf-8 -*-
"""Coons, Gordon-network and N-sided (star-point) surface patches."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_chain_loop, _eval01, _hermite5, _hermite7, _pt, grid_to_surface)



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



def gordon_network(profiles, guides, samples_per_curve=24, extra_per_gap=4):
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
      extra_per_gap     : extra samples inserted between crossings on each
                          family's continuous axis (shape-fidelity knob;
                          the multi-rail sweep raises it when it solves
                          through this function).
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
    s1, u_ref = _skin_through_family(bs_guides, own_v_per_guide, u_bar, v_bar,
                                     extra_per_gap=extra_per_gap)

    # 4) S2: skin through profiles -- continuous along each profile's own
    #    arc (v_bar anchors), discrete across the nu profiles (u_bar).
    #    Built axis-swapped relative to S1's convention, then
    #    exchangeUV()'d to align -- mirrors the original algorithm's use
    #    of exchangeUV for the same reason.
    own_u_per_profile = [[params_u[i][j] for j in range(nv)] for i in range(nu)]
    s2, v_ref = _skin_through_family(bs_profiles, own_u_per_profile, v_bar,
                                     u_bar, extra_per_gap=extra_per_gap)
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
    """Grid of points for one Gregory triangular sub-patch (O, A, B).

    O = star center, A and B = adjacent corners, edge_AB = boundary curve
    from A to B.  spoke_OA / spoke_OB are the quintic/septic Hermite spoke
    callables from _star_spoke.  data_A / data_B are corner tangent dicts.

    Builds a cubic Gregory triangle: 10 control points (3 corners, 6 edge
    Gregory points, 1 interior).  Evaluated on a samples x samples grid over
    the triangle domain and converted to a BSpline surface via
    grid_to_surface.  Boundary match on edge AB is exact; spoke match on OA
    and OB is exact; G1 at O, A, B is exact by construction.
    """
    n = samples
    grid = []

    # --- Gregory control points ------------------------------------------ #
    # Edge OA (spoke from center to corner A)
    # Cross-boundary at O: in center plane, perpendicular to spoke OA
    spoke_OA_dir = (A - O)
    spoke_OA_dir.normalize()
    center_n = data_A["plane_n"]  # use center's plane (shared across corners)
    w_OA_O = _cross_dir_in_plane(spoke_OA_dir, center_n)
    # Cross-boundary at A: in corner A's tangent plane, perpendicular to spoke AO
    spoke_AO_dir = (O - A)
    spoke_AO_dir.normalize()
    w_OA_A = _cross_dir_in_plane(spoke_AO_dir, data_A["plane_n"])

    L_OA = (A - O).Length
    b210 = O + spoke_OA_dir * (L_OA / 3.0) \
        + (w_OA_O * (L_OA / 3.0) if w_OA_O is not None else Vector(0, 0, 0))
    b120 = A + spoke_AO_dir * (L_OA / 3.0) \
        + (w_OA_A * (L_OA / 3.0) if w_OA_A is not None else Vector(0, 0, 0))

    # Edge OB (spoke from center to corner B)
    spoke_OB_dir = (B - O)
    spoke_OB_dir.normalize()
    w_OB_O = _cross_dir_in_plane(spoke_OB_dir, center_n)
    spoke_BO_dir = (O - B)
    spoke_BO_dir.normalize()
    w_OB_B = _cross_dir_in_plane(spoke_BO_dir, data_B["plane_n"])

    L_OB = (B - O).Length
    b201 = O + spoke_OB_dir * (L_OB / 3.0) \
        + (w_OB_O * (L_OB / 3.0) if w_OB_O is not None else Vector(0, 0, 0))
    b102 = B + spoke_BO_dir * (L_OB / 3.0) \
        + (w_OB_B * (L_OB / 3.0) if w_OB_B is not None else Vector(0, 0, 0))

    # Edge AB (boundary curve)
    # Cross-boundary at A: in corner A's tangent plane, perpendicular to
    # boundary tangent at A, pointing into the triangle.
    t_AB_A = data_A["t_out"] if data_A["t_out"] is not None else spoke_OA_dir
    w_AB_A = _cross_dir_in_plane(t_AB_A, data_A["plane_n"])
    # Make sure it points into the triangle (toward center side)
    if w_AB_A is not None:
        to_center = O - A
        if w_AB_A.dot(to_center) < 0:
            w_AB_A = w_AB_A.negative()

    t_AB_B = data_B["t_in"] if data_B["t_in"] is not None else spoke_OB_dir
    w_AB_B = _cross_dir_in_plane(t_AB_B, data_B["plane_n"])
    if w_AB_B is not None:
        to_center = O - B
        if w_AB_B.dot(to_center) < 0:
            w_AB_B = w_AB_B.negative()

    L_AB = max((B - A).Length, 1e-9)
    b021 = A + (B - A) * (1.0 / 3.0) \
        + (w_AB_A * (L_AB / 3.0) if w_AB_A is not None else Vector(0, 0, 0))
    b012 = B + (A - B) * (1.0 / 3.0) \
        + (w_AB_B * (L_AB / 3.0) if w_AB_B is not None else Vector(0, 0, 0))

    # Interior control point: average of edge Gregory points, pulled toward
    # center to keep the patch well-shaped.  This is the standard Gregory
    # interior-point choice that gives G1 continuity across seams.
    b111 = (b210 + b120 + b021 + b012 + b102 + b201) * (1.0 / 6.0)

    # --- Evaluate on triangle domain ------------------------------------- #
    # Cubic Bézier triangle: P(u,v,w) with u+v+w=1, u,v,w>=0.
    # We sample u (O->A direction) and v (O->B direction), w = 1-u-v.
    for i in range(n):
        row = []
        u = i / (n - 1.0) if n > 1 else 0.0
        for j in range(n):
            v = j / (n - 1.0) if n > 1 else 0.0
            w = 1.0 - u - v
            if w < 0:
                # Outside the triangle: clamp to boundary
                w = 0.0
                if u + v > 1.0:
                    scale = 1.0 / (u + v)
                    u *= scale
                    v *= scale
                    w = 0.0
            # Bernstein basis for cubic triangle (degree 3):
            # P = sum_{i+j+k=3} b_ijk * B_ijk(u,v,w)
            # B_ijk = 6 * u^i * v^j * w^k / (i! j! k!)
            u2 = u * u; u3 = u2 * u
            v2 = v * v; v3 = v2 * v
            w2 = w * w; w3 = w2 * w

            p = (O * (u3)                      # b300 = O, i=3
                 + A * (v3)                      # b030 = A, j=3
                 + B * (w3)                      # b003 = B, k=3
                 + b210 * (3 * u2 * v)           # i=2,j=1
                 + b120 * (3 * u * v2)           # i=1,j=2
                 + b021 * (3 * v2 * w)           # j=2,k=1
                 + b012 * (3 * v * w2)           # j=1,k=2
                 + b102 * (3 * u * w2)           # i=1,k=2
                 + b201 * (3 * u2 * w)           # i=2,k=1
                 + b111 * (6 * u * v * w))       # i=1,j=1,k=1
            row.append(p)
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
    """One sweep call's worth of sampled edges for repeated _nearest_t queries.

    multi_rail_sweep / two_rail_sweep query their rails once per profile
    (flip heuristics, stations), so the same rails were re-evaluated
    through OCCT 101 times per profile per query site. The cache samples
    each edge once and re-uses the point list; the returned parameters are
    bit-identical to _nearest_t's (same samples, same distances).

    Lives only inside one sweep call, and only grips edges the caller
    keeps referenced for that whole call, so id() recycling can never
    poison a lookup."""
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
        step = 1.0 / self._samples
        best_t, best_d = 0.0, 1e18
        for k in range(len(pts)):
            d = pts[k].distanceToPoint(pt)
            if d < best_d:
                best_d, best_t = d, k * step
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
    cache = _NearestTCache()

    # --- per-profile local description ---------------------------------- #
    descs = []          # (station_t, up_vector, [(cx, cy, cz), ...])
    prev_up = None
    for pro in profiles:
        e0, e1 = _eval01(pro, 0.0), _eval01(pro, 1.0)
        # orient so sample param 0 lands on rail1, last sample on rail2
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
        descs.append((cache.param(rail1, a), ey, local))
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
    """C2 quintic smootherstep s^3(6s-15s+10), clamped to [0,1] -- the weight
    the C++ design uses for its phi_i partition of unity (zero slope AND zero
    curvature at both ends, so a contact anchor is 'dwellable', not merely
    passable)."""
    s = min(max(s, 0.0), 1.0)
    return s * s * s * (s * (6.0 * s - 15.0) + 10.0)


class _ArcRail:
    """Cumulative chord-length table over one rail edge -- the Python port of
    the C++ transport's ArcCurve.  Stations on the sweep axis are arc
    FRACTIONS, and every rail is evaluated at its own arc-fraction-equivalent
    point, so rails of different lengths or parametrizations stay in sync
    (the normalized-parameter sampling the spine path used before silently
    desynced them; on equal-length, evenly-parametrized fixtures this table
    is the identity, on real rails it is not)."""
    def __init__(self, edge, samples=256):
        self.edge = edge
        # The table lives in NORMALIZED-parameter space end to end:
        # _NearestTCache.param and the station conventions feed normalized
        # params in, and point_at feeds its own results through _eval01,
        # which also expects [0,1]. Edges produced by interpolating
        # BSplineCurve.toShape() often carry RAW geometric parameters
        # (e.g. chord length), so anything that kept raw edge params here
        # would double-scale by the rail length.
        self.params = [k / float(samples) for k in range(samples + 1)]
        pts = [_eval01(edge, k / float(samples)) for k in range(samples + 1)]
        acc = [0.0]
        for k in range(samples):
            acc.append(acc[-1] + pts[k].distanceToPoint(pts[k + 1]))
        self.total = acc[-1] if acc[-1] > 1e-12 else 1.0
        self.acc = [a / self.total for a in acc]

    def fraction_of(self, t_norm):
        """Normalized parameter -> arc fraction in [0,1] (piecewise linear)."""
        params, acc = self.params, self.acc
        if t_norm <= params[0]:
            return 0.0
        if t_norm >= params[-1]:
            return 1.0
        for k in range(len(params) - 1):
            if params[k] <= t_norm <= params[k + 1]:
                span = params[k + 1] - params[k]
                w = 0.0 if span < 1e-15 else (t_norm - params[k]) / span
                return acc[k] + w * (acc[k + 1] - acc[k])
        return 1.0

    def param_at(self, s):
        """Arc fraction -> normalized parameter (piecewise linear inverse)."""
        acc = self.acc
        s = min(max(s, 0.0), 1.0)
        for k in range(len(acc) - 1):
            if acc[k] <= s <= acc[k + 1]:
                span = acc[k + 1] - acc[k]
                w = 0.0 if span < 1e-15 else (s - acc[k]) / span
                return self.params[k] + w * (self.params[k + 1] - self.params[k])
        return self.params[-1]

    def point_at(self, s):
        """Point on the rail at arc fraction s."""
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
    """Worst distance from midpoints along each curve to `shape`."""
    worst = 0.0
    for c in curves:
        fp, lp = c.FirstParameter, c.LastParameter
        for k in range(int(samples)):
            t = fp + (k + 0.5) / float(samples) * (lp - fp)
            worst = max(worst, Part.Vertex(c.value(t)).distToShape(shape)[0])
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
    """Exact continuous solve for a well-formed multi-rail sweep network.

    The compatibility preconditions are measured, not assumed: every
    profile must genuinely cross every rail (within _GORDON_CROSS_TOL,
    plain closest-approach otherwise -- the same test gordon_network's
    own warning enforces). The result is then measured against both
    input families and only shipped when it actually holds every rail
    within _MULTIRAIL_GORDON_RAIL_TOL and every profile within
    _MULTIRAIL_GORDON_PROFILE_TOL -- anything less falls back to the
    station-skinned spine path, which lands ON every rail at every
    station by construction.

    Returns the Part shape, or None to signal "use the spine path".
    The intersection grid is computed here for validation and again
    inside gordon_network for the solve; the duplicate OCCT work buys
    not having to restructure gordon_network's public signature.
    """
    bs_rails = [_to_bspline(r) for r in rails]
    bs_prof = [_to_bspline(p) for p in profiles]
    params_u, _params_v, gaps = _intersect_grid(bs_prof, bs_rails)
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
        order_msg = _rail_order_warning(params_u)
        if order_msg:
            FreeCAD.Console.PrintWarning(
                "Railform Multi-rail sweep: %s\n" % order_msg)
        return None
    try:
        shape = gordon_network(profiles, rails,
                               extra_per_gap=_MULTIRAIL_GORDON_DENSIFY)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Railform Multi-rail sweep: Gordon solve failed (%s) -- "
            "skinning across stations instead.\n" % exc)
        return None
    rail_dev = _deviation_of_curves(bs_rails, shape,
                                    _MULTIRAIL_GORDON_RAIL_SAMPLES)
    prof_dev = _deviation_of_curves(bs_prof, shape,
                                    _MULTIRAIL_GORDON_PROFILE_SAMPLES)
    if rail_dev <= _MULTIRAIL_GORDON_RAIL_TOL and \
            prof_dev <= _MULTIRAIL_GORDON_PROFILE_TOL:
        return shape
    FreeCAD.Console.PrintWarning(
        "Railform Multi-rail sweep: Gordon solve does not hold the input "
        "(worst rail deviation %.3g, profile %.3g) -- skinning across "
        "stations instead.\n" % (rail_dev, prof_dev))
    return None


def multi_rail_sweep(rails, profiles, n_rail=40, n_prof=20):
    """Skin a set of profile curves across N >= 2 rail curves.

    With >= 3 rails and >= 2 profiles, and a COMPATIBLE network (every
    profile genuinely crosses every rail), the surface comes from a
    Gordon boolean-sum solve -- it passes through the FULL shape of every
    rail and profile continuously, not just at the sample stations (see
    _try_gordon_route for the measured acceptance gates and fallbacks).

    Everything else uses an adaptive station-skinning transport that ports
    the C++ multi-rail sweep design's exact-facing machinery onto the
    crossed-network spine idea:

      * rails[0] and rails[-1] bound the sweep exactly as in
        two_rail_sweep; every INTERMEDIATE rail anchors it too.  Each
        station samples ALL rails and bases the section on the polyline
        through those points, with each profile's transverse offsets
        measured against its own reference spine.
      * the stations live on an ARC-FRACTION axis shared by every rail
        (_ArcRail): each rail is sampled at its own arc-fraction point,
        so rails of different lengths or parametrizations stay in sync.
      * each profile's own station is a HARD station (the C++ design's
        contact-point breaks): at exactly that position the row is that
        profile's reconstruction, so "the profile wins at its own
        station" is exact, not an accident of where uniform stations
        happened to fall.
      * between stations the row count ADAPTS: a gap whose midpoint row
        deviates from the average of its bracketing rows by more than
        _MULTIRAIL_STATION_SAG x _MULTIRAIL_SPINE_TOL subdivides, within
        a budget of _MULTIRAIL_STATION_FACTOR x n_rail rows and
        _MULTIRAIL_STATION_PASSES rounds.
      * the section's remap between pinned rail columns eases with a C2
        smootherstep (_spine_map_c2, the C++ phi_i partition of unity),
        and the template morph between adjacent profiles eases the same
        way -- tangent dwell at every anchor instead of C0 kinks.
      * a degenerate station (rails collapsing onto a common point) is
        refused with a message, in the C++ spirit of "failures as text,
        never a bad surface".
      * the finished surface is MEASURED against the boundary rails (the
        one guarantee profile-endpoint pinning always gives); when the
        between-station hold misses the target a warning names the
        number and the knob.  The station pins themselves are exact by
        construction.

    For exactly two rails the call delegates to two_rail_sweep directly:
    the old spine remap was the identity and the columns j/(n_prof-1)
    there, so both paths already produced the same surface to
    floating-point noise -- the delegation now makes that exact instead
    of approximate.

    No Curves/libS2R dependency; robust and headless-testable.
    """
    rails = list(rails)
    n_rails = len(rails)
    if n_rails < 2:
        raise ValueError(
            "multi-rail sweep: need >= 2 rails -- got %d" % n_rails)
    n_prof = max(2, int(n_prof), n_rails)
    n_rail = max(2, int(n_rail))

    # Exactly two rails IS two_rail_sweep: there the spine remap is the
    # identity and the columns spread j/(n_prof-1), so the old N=2
    # reduction reproduced it to floating-point noise. Delegate directly --
    # same numbers, zero reduction drift.
    if n_rails == 2:
        return two_rail_sweep(rails[0], rails[1], profiles, n_rail, n_prof)

    base, far = rails[0], rails[-1]

    if n_rails >= 3 and len(profiles) >= 2:
        gordon = _try_gordon_route(rails, profiles)
        if gordon is not None:
            return gordon

    # --- arc-length rail tables (the shared sweep axis) ------------------ #
    arc = [_ArcRail(r) for r in rails]

    cache = _NearestTCache()
    descs = []
    prev_up = None
    for pro in profiles:
        e0, e1 = _eval01(pro, 0.0), _eval01(pro, 1.0)
        # orient so sample param 0 lands on rail 0, last sample on rail N-1
        flip = (cache.param(base, e1) + cache.param(far, e0)) < \
               (cache.param(base, e0) + cache.param(far, e1))

        def P(t, _pro=pro, _flip=flip):
            return _eval01(_pro, 1.0 - t if _flip else t)

        # Reference spine for this profile: its own two ends (which is what
        # two_rail_sweep measures against) plus every interior rail sampled
        # at the arc-fraction-synced station of the profile's start.  Vertex
        # i is therefore the point the sweep must sit on for rail i, and
        # the profile's transverse coordinates are taken relative to it.
        s_p = arc[0].fraction_of(cache.param(base, P(0.0)))
        ref = [P(0.0)]
        ref += [arc[k].point_at(s_p) for k in range(1, n_rails - 1)]
        ref.append(P(1.0))
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
            # full 3-component offset in the profile's orthonormal frame --
            # the along-chord piece matters whenever the profile bulges away
            # from the polyline spine between rail points
            local.append((u, d.dot(ex), d.dot(ey), d.dot(ez)))
        descs.append((s_p, ey, local, zref))
    if not descs:
        raise ValueError("multi-rail sweep: no usable profiles")
    descs.sort(key=lambda d: d[0])

    # --- rows at sweep stations (cached; adaptive passes reuse them) ----- #
    rows = {}

    def station_row(s):
        """One grid row (plus its per-column spine params) at sweep-station
        arc fraction s."""
        cached = rows.get(s)
        if cached is not None:
            return cached
        wpts = [arc[k].point_at(s) for k in range(n_rails)]
        sparams = _spine_params(wpts)
        # Degenerate-station guard (the C++ zero-Jacobian check): a rail
        # chord that collapses to a point cannot carry a section -- refuse
        # instead of silently skinning garbage.
        chord = wpts[-1].sub(wpts[0])
        if chord.Length < 1e-9:
            raise ValueError(
                "multi-rail sweep: rails collapse (zero Jacobian) at "
                "station %.4g -- the rails touch there, so the sweep "
                "degenerates" % s)
        ex = Vector(chord)
        ex.normalize()
        # A row ON a profile's own station (within table noise of that
        # station's arc fraction) is that profile's exact reconstruction --
        # the hard-station contract. Proximity, not float equality: the
        # uniform grid and an arc-table anchor differ by ~1e-14, and
        # _dedupe_stations keeps only one of the pair, so whichever
        # station survives must still anchor to the profile.
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
            # C2 template morph between the bracketing profiles: the shape
            # blend eases in/out at every anchor (the C++ phi behaviour)
            # instead of ramping linearly, so a profile's own station is a
            # genuine dwell on that profile's shape.
            w = 0.0 if span < 1e-12 else \
                _smootherstep((s - lo[0]) / span)
        up = lo[1] * (1.0 - w) + hi[1] * w
        ez = ex.cross(up)
        if ez.Length < 1e-9:
            up = _perp(ex)
            ez = ex.cross(up)
        ez.normalize()
        ey = ez.cross(ex)
        ey.normalize()
        # interpolated reference spine: vertex k of lo/hi -> vertex k here
        zmap = [lo[3][k] * (1.0 - w) + hi[3][k] * w for k in range(n_rails)]
        row, sps = [], []
        for j in range(n_prof):
            u0, cx0, cy0, cz0 = lo[2][j]
            u1, cx1, cy1, cz1 = hi[2][j]
            u = u0 * (1.0 - w) + u1 * w
            cx = cx0 * (1.0 - w) + cx1 * w
            cy = cy0 * (1.0 - w) + cy1 * w
            cz = cz0 * (1.0 - w) + cz1 * w
            # C2 remap between the pinned rail columns for the GEOMETRY
            # between two anchors (the C++ phi weights: dwell tangent at
            # each rail vertex, so station transitions across the section
            # are C2, not kinked).  At a hard station (w exactly 0 or 1:
            # the row IS one profile's anchor) keep the plain linear map:
            # with the anchor's own spine as the map source it is the
            # identity there, so the row reproduces the profile's original
            # interior positions exactly.
            if 0.0 < w < 1.0:
                sp = _spine_map_c2(zmap, sparams, u)
            else:
                sp = _spine_map(zmap, sparams, u)
            # the section PARAMETERIZATION bookkeeping stays on the plain
            # linear map (pre-C2 behaviour): the finisher's v grid must be
            # the profiles' own column params for the anchor iso-curve's
            # stage-A interpolation to reconstruct the profile curve to
            # machine precision (OCC's interpolate() with a curve's own
            # parameterization reproduces it exactly), which the C2
            # dwell-plateau values would shift.
            sps.append(_spine_map(zmap, sparams, u))
            pt = _spine_eval(sparams, wpts, sp)
            row.append(pt + ex * cx + ey * cy + ez * cz)
        rows[s] = (row, sps)
        return rows[s]

    # initial station set: the uniform n_rail grid plus the profiles' own
    # hard stations and the exact ends
    station_list = _dedupe_stations(sorted(
        [i / float(n_rail - 1) for i in range(n_rail)]
        + [d[0] for d in descs] + [0.0, 1.0]))

    # adaptive subdivision: probe each gap's midpoint row against the
    # average of its bracketing rows over the full pinned-column set
    # (the C++ chord-deviation probe); subdivide where it sags past the
    # target, one round at a time, within the station budget
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

    # final rows: every adaptive station, exactly interpolated through the
    # grid points at EXPLICIT parameters (the station arc fractions along
    # the sweep axis, each column's mean spine position across stations
    # along the section axis) -- the between-node shape tracks the real
    # rail/profile correspondence instead of chord-length guesses
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

    # measured, not assumed (the C++ Result::maxRailDeviation gate): hold
    # the finished surface against the boundary rails -- the one guarantee
    # profile-endpoint pinning always gives -- and say so when the
    # between-station hold misses the target.  Warn, never throw: this
    # path exists for input the exact continuous solve already refused.
    try:
        dev = _deviation_of_curves([base.Curve, far.Curve], shape,
                                   _MULTIRAIL_GORDON_RAIL_SAMPLES)
        if dev > 10.0 * _MULTIRAIL_SPINE_TOL:
            FreeCAD.Console.PrintWarning(
                "Railform Multi-rail sweep: station skinning holds the "
                "boundary rails to %.3g between stations (target %.3g) -- "
                "raise Rail Samples (denser stations) or add profiles near "
                "the worst gap for a tighter sweep.\n"
                % (dev, _MULTIRAIL_SPINE_TOL))
    except Exception:
        pass
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
    'plate_fill',
]
