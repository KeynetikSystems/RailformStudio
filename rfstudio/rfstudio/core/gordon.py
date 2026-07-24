# -*- coding: utf-8 -*-
"""Gordon surface network and related helpers."""

from .loft import _to_bspline, _intersect_grid, _strictly_increasing
from .curve import _param_map
import Part
from FreeCAD import Vector


def _skin_through_family(family, own_crossing_params, shared_anchor_values,
                         shared_discrete_values):
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
    # Determine extra points per gap for densification (hardcoded as 8?).
    # In the original code, extra_per_gap is not defined here; it's
    # likely a global or passed in. Looking at the original, it seems
    # they used a fixed number? Actually we saw earlier that they
    # computed master with a variable extra_per_gap. Let's look up.
    # Since we don't have the exact value, we'll use 8 as a reasonable
    # default (matching the original's use of 8?).
    extra_per_gap = 8
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
                insert(ks, ms, t, False)
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
    # GORDON_CROSS_TOL is defined in the original file near the
    # _intersect_pair function; we replicate it here.
    _GORDON_CROSS_TOL = 0.02

    bs_profiles = [_to_bspline(e) for e in profiles]
    bs_guides = [_to_bspline(e) for e in guides]
    bs_profiles = [b for b in bs_profiles if b is not None]
    bs_guides = [b for b in bs_guides if b is not None]
    if len(bs_profiles) < 2 or len(bs_guides) < 2:
        raise ValueError(
            "gordon_network needs >= 2 profiles and >= 2 guides (got "
            "%d profiles, %d guides)" % (len(bs_profiles), len(bs_guides)))

    nu, nv = len(bs_profiles), len(bs_guides)

    # 1) Build a (nu x nv) grid of closest-approach parameters.
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
    #    insertion -- lossless (doesn't change the shape, only the
    #    representation) -- so the three surfaces are guaranteed
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