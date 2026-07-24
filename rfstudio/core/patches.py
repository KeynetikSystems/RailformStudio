# -*- coding: utf-8 -*-
"""Coons, Gordon-network and N-sided (star-point) surface patches."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_chain_loop, _eval01, _hermite5, _pt, grid_to_surface)



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

__all__ = [
    '_closest_approach_pair',
    '_to_bspline',
    '_intersect_pair',
    '_GORDON_CROSS_TOL',
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
    '_average_point',
    '_order_open_loop',
    '_corner_boundary_accel',
    '_star_spoke',
    '_triangular_coons_grid',
    '_nearest_t',
    '_polygon_normal',
    '_perp',
    'two_rail_sweep',
    'plate_fill',
]
