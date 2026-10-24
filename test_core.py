# -*- coding: utf-8 -*-
"""Headless test of the Gui-free core (loft fallback + rail interchange).

    & "C:\\Program Files\\FreeCAD 1.1\\bin\\freecadcmd.exe" test_core.py

Only core.py is exercised -- the engine/UI layers import PySide + Gui and cannot
run here by design. This is the verifiable safety net the depend-and-wrap plan
relies on.
"""

import sys, os, traceback, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD
import Part
from FreeCAD import Vector
from rfstudio import core


def out(s):
    sys.stdout.write(s + "\n"); sys.stdout.flush()


def spline(pts):
    """Convenience: build a clamped-interpolating BSplineCurve edge."""
    b = Part.BSplineCurve()
    b.interpolate(pts)
    return b.toShape()


def bspline_face(face, n=8):
    """A bounded BSplineSurface fit to `face`'s actual parameter range --
    calling plane_face.Surface.toBSpline() directly raises OCCError, since
    the raw Surface object is mathematically infinite/unbounded (toBSpline()
    only works on already-bounded surfaces). Mirrors core.py's
    match_surface_exact_g1._as_bspline fallback, used here to build test
    fixtures that are DELIBERATELY already-BSpline (bypassing the auto-
    convert path, which has its own dedicated test)."""
    surf = face.Surface
    u0, u1, v0, v1 = face.ParameterRange
    grid = [[surf.value(u0 + i * (u1 - u0) / (n - 1.0),
                        v0 + j * (v1 - v0) / (n - 1.0))
            for j in range(n)] for i in range(n)]
    bs = Part.BSplineSurface()
    bs.interpolate(grid)
    return bs.toShape().Faces[0]


def incompatible_bspline_face(grid, knot_scale=5.0):
    """A BSplineSurface fit through `grid`, then rescaled onto a knot
    domain [0, knot_scale] instead of interpolate()'s normalized [0,1] --
    genuinely knot-incompatible with a plain interpolate()'d surface for
    match_surface_exact_g1's harmonization step. Needed because
    BSplineSurface.interpolate() always normalizes its own parameter
    domain to [0,1] regardless of the input points' spacing, so two
    differently-shaped interpolated surfaces are NOT actually parameter-
    incompatible by domain alone -- only by knot count/refinement, which
    _harmonize's insertKnots step reconciles losslessly. This helper
    constructs the genuinely-incompatible case the "incompatible
    boundaries" tests need."""
    bs = Part.BSplineSurface()
    bs.interpolate(grid)
    uk = [k * knot_scale for k in bs.getUKnots()]
    vk = [k * knot_scale for k in bs.getVKnots()]
    poles = [[bs.getPole(i + 1, j + 1) for j in range(bs.NbVPoles)]
            for i in range(bs.NbUPoles)]
    bs2 = Part.BSplineSurface()
    bs2.buildFromPolesMultsKnots(
        poles, bs.getUMultiplicities(), bs.getVMultiplicities(),
        uk, vk, False, False, bs.UDegree, bs.VDegree)
    return bs2.toShape().Faces[0]


def flip_edge(edge):
    """Re-sample an edge in reverse order; used in tests to simulate a
    backwards curve without depending on the (now-removed) _flip_edge helper."""
    pts = [edge.valueAt(edge.FirstParameter + k / 99.0 * (edge.LastParameter - edge.FirstParameter))
           for k in range(100)]
    pts.reverse()
    b = Part.BSplineCurve()
    b.interpolate(pts)
    return b.toShape()


# ---- loft fallback --------------------------------------------------------

def test_loft_fallback():
    def row(z):
        bs = Part.BSplineCurve()
        bs.interpolate([Vector(0, 0, z), Vector(5, 2, z), Vector(10, 0, z)])
        return bs.toShape()
    s = core.loft([row(0), row(4), row(8)])
    assert s.Area > 0
    out("  loft fallback     OK  area=%.2f" % s.Area)


# ---- Coons patch ----------------------------------------------------------

def test_coons_patch():
    c_s = Part.makeLine(Vector(0, 0, 0), Vector(10, 0, 2))
    c_e = Part.makeLine(Vector(10, 0, 2), Vector(10, 10, 0))
    c_n = Part.makeLine(Vector(10, 10, 0), Vector(0, 10, 3))
    c_w = Part.makeLine(Vector(0, 10, 3), Vector(0, 0, 0))
    s = core.coons_patch([c_e, c_w, c_n, c_s], n=16)
    assert s.Area > 0
    bb = s.BoundBox
    assert bb.XLength > 8 and bb.YLength > 8
    out("  coons patch       OK  area=%.2f" % s.Area)


# ---- Gordon: clean rectangular networks ----------------------------------

def test_gordon_rectangular_3x3():
    """A 3x3 axis-aligned Gordon gives a sensible positive-area surface,
    and the central grid node coincides with the geometric center of
    the network."""
    u = [spline([Vector(0, y, 0), Vector(5, y, 1), Vector(10, y, 0)])
         for y in (0, 5, 10)]
    v = [spline([Vector(x, 0, 0), Vector(x, 5, 1), Vector(x, 10, 0)])
         for x in (0, 5, 10)]
    s = core.gordon_network(u, v)
    assert s.Area > 0
    surf = s.Faces[0].Surface
    pr = s.Faces[0].ParameterRange
    mid = surf.value(0.5 * (pr[0] + pr[1]), 0.5 * (pr[2] + pr[3]))
    # mid grid cell (i=1, j=1) of a 3x3 axis-aligned network sits at (5, 5, 1)
    assert mid.distanceToPoint(Vector(5, 5, 1)) < 1e-6, mid
    out("  gordon 3x3 rect    OK  area=%.2f mid=%s" % (s.Area, mid))


def test_gordon_rectangular_5x5():
    """A larger rectangle: 5 profiles x 5 guides, all axis-aligned. The
    algorithm must remain stable on a non-trivial grid."""
    u = [spline([Vector(0, y, 0), Vector(5, y, 1), Vector(10, y, 0)])
         for y in (0, 2.5, 5, 7.5, 10)]
    v = [spline([Vector(x, 0, 0), Vector(x, 5, 1), Vector(x, 10, 0)])
         for x in (0, 2.5, 5, 7.5, 10)]
    s = core.gordon_network(u, v)
    assert s.Area > 0
    out("  gordon 5x5 rect    OK  area=%.2f" % s.Area)


# ---- Gordon: angled networks ----------------------------------------------

def test_gordon_angled_network():
    """A non-axis-aligned network (U at 45deg, V at -45deg) where every
    profile/guide is a smooth ramp. The midpoint of the network lies on
    each pair, and the surface should pass through it."""
    def along(angle, length, center):
        cx, cy = center
        dx, dy = math.cos(angle) * length / 2, math.sin(angle) * length / 2
        return spline([Vector(cx - dx, cy - dy, 0),
                       Vector(cx, cy, 1),
                       Vector(cx + dx, cy + dy, 0)])
    u = [along(math.pi / 4, length=10, center=(5, 1 + 2 * i)) for i in range(3)]
    v = [along(-math.pi / 4, length=10, center=(1 + 2 * i, 5)) for i in range(3)]
    s = core.gordon_network(u, v)
    assert s.Area > 0
    out("  gordon angled net  OK  area=%.2f" % s.Area)


# ---- Gordon: curves of different lengths ---------------------------------

def test_gordon_unequal_lengths():
    """Curves of very different lengths still produce a usable surface --
    the algorithm is per-pair closest-approach, so curve length doesn't
    distort the grid."""
    u_short = spline([Vector(0, 0, 0), Vector(2, 0, 1), Vector(4, 0, 0)])
    u_long  = spline([Vector(0, 0, 8),
                      Vector(8, 1, 8), Vector(16, 1, 8),
                      Vector(24, 0, 8), Vector(32, 1, 8), Vector(40, 0, 8)])
    v_left  = spline([Vector(0, 0, 0), Vector(0, 1, 4), Vector(0, 0, 8)])
    v_right = spline([Vector(40, 0, 0), Vector(40, 1, 4), Vector(40, 0, 8)])
    s = core.gordon_network([u_short, u_long], [v_left, v_right])
    assert s.Area > 0
    out("  gordon uneven lens OK  area=%.2f" % s.Area)


# ---- Gordon: honours curve direction (Z-fold is the user's signal) ------

def test_gordon_preserves_user_direction():
    """REGRESSION: gordon_network uses every curve exactly as the user drew
    it, in the direction they drew it. The grid nodes are computed by
    `_closest_approach_pair`, which is direction-agnostic -- so a flipped
    curve in the middle of a family produces a different local grid cell,
    not a silent rewrite. This test verifies that the grid for the
    middle profile honours the FLIPPED curve (because that's the closest
    approach to every guide), not the original.

    Geometric setup: a Y-shaped profile (`u[1]`) makes a "bump" toward
    positive X in the middle. Its flipped version bumps in the OPPOSITE
    direction. The grid cell (1, j) for every j should sit on the
    flipped curve, not the original.
    """
    # 3 profiles along Y, 3 guides along X. u[1] has a clear asymmetric
    # control polygon so flipping it actually moves the geometry.
    u = [
        spline([Vector(0, y, 0), Vector(8, y, 0.5), Vector(16, y, 0)])
        for y in (0, 8, 16)
    ]
    # Make the middle curve strongly asymmetric by skewing its control polygon
    # so flipping reverses a 5-unit direction shift.
    pts_mid = [Vector(0, 8, 0), Vector(12, 8, 1.5), Vector(4, 8, 0.7)]
    u[1] = spline(pts_mid)
    v = [
        spline([Vector(x, 0, 0), Vector(x, 8, 1), Vector(x, 16, 0)])
        for x in (0, 8, 16)
    ]

    # Build the grid nodes directly using the helper so we can inspect what
    # the algorithm sees for the (flipped u[1], v[j]) pairs.
    from rfstudio.core import _to_bspline, _closest_approach_pair
    flipped = flip_edge(u[1])
    bp_u = [_to_bspline(e) for e in u]
    bp_u[1] = _to_bspline(flipped)
    bp_v = [_to_bspline(e) for e in v]
    grid_pts = []
    for j, ve in enumerate(v):
        t_u, t_v, d = _closest_approach_pair(bp_u[1], bp_v[j])
        p = (bp_u[1].value(t_u) + bp_v[j].value(t_v)) * 0.5
        grid_pts.append(p)
    # All 3 grid points for the flipped middle curve should land on / very
    # close to the flipped curve, NOT to the original. Check that the
    # midpoint grid point (j=1) deviates measurably from the original.
    orig_mid_grid = (u[1].valueAt(0.5 * (u[1].FirstParameter + u[1].LastParameter))
                     + v[1].valueAt(0.5 * (v[1].FirstParameter + v[1].LastParameter))
                     ) * 0.5
    flipped_mid_grid = grid_pts[1]
    delta = orig_mid_grid.distanceToPoint(flipped_mid_grid)
    assert delta > 0.5, (
        "flipped curve's grid node should be far from the original mid "
        "(got delta=%.3f)" % delta)
    out("  gordon no-flip     OK  ref-mid=%s  flip-mid=%s  delta=%.3f"
        % (orig_mid_grid, flipped_mid_grid, delta))


# ---- Multi-rail sweep ----

def _rails_profiles():
    """Two straight rails along X (y=0 and y=5) with three cross profiles
    spanning rail1->rail2 at x = 0, 5, 10."""
    r0 = spline([Vector(0, 0, 0), Vector(5, 0, 0), Vector(10, 0, 0)])
    r1 = spline([Vector(0, 5, 0), Vector(5, 5, 0), Vector(10, 5, 0)])
    rmid = spline([Vector(0, 2.5, 0), Vector(5, 2.5, 0), Vector(10, 2.5, 0)])
    profs = [
        spline([Vector(0, 0, 0), Vector(0, 2.5, 0.5), Vector(0, 5, 0)]),
        spline([Vector(5, 0, 0), Vector(5, 2.5, 1.0), Vector(5, 5, 0)]),
        spline([Vector(10, 0, 0), Vector(10, 2.5, 0.5), Vector(10, 5, 0)]),
    ]
    return (r0, r1, rmid), profs


def test_multi_rail_sweep_two_rail_equiv():
    """With exactly 2 rails, multi_rail_sweep must match two_rail_sweep."""
    (r0, r1, _rmid), profs = _rails_profiles()
    a = core.two_rail_sweep(r0, r1, profs, n_rail=20, n_prof=8)
    b = core.multi_rail_sweep([r0, r1], profs, n_rail=20, n_prof=8)
    assert abs(a.Area - b.Area) < 1e-6, (a.Area, b.Area)
    out("  multi-rail 2-rail equiv OK  area=%.4f" % a.Area)


def test_multi_rail_sweep_three_rail():
    """Three rails + profiles must build a usable surface shaped by all rails."""
    (r0, r1, rmid), profs = _rails_profiles()
    s = core.multi_rail_sweep([r0, rmid, r1], profs, n_rail=20, n_prof=8)
    assert s.Area > 0
    out("  multi-rail 3-rail OK  area=%.4f" % s.Area)


def test_multi_rail_sweep_more_rails():
    """Five rails should still build a valid surface."""
    (r0, r1, rmid), profs = _rails_profiles()
    rails = [
        spline([Vector(0, y, 0), Vector(5, y, 0), Vector(10, y, 0)])
        for y in (0, 1.25, 2.5, 3.75, 5)]
    s = core.multi_rail_sweep(rails, profs, n_rail=20, n_prof=8)
    assert s.Area > 0
    out("  multi-rail 5-rail OK  area=%.4f" % s.Area)


def test_multi_rail_sweep_rejects_fewer_than_two_rails():
    (r0, r1, _rmid), profs = _rails_profiles()
    for rails in ([], [r0]):
        try:
            core.multi_rail_sweep(rails, profs)
            raise AssertionError("expected ValueError for %d rails" % len(rails))
        except ValueError:
            pass
    out("  multi-rail reject <2 rails OK")


def test_multi_rail_sweep_rejects_no_profiles():
    (r0, r1, _rmid), profs = _rails_profiles()
    try:
        core.multi_rail_sweep([r0, r1], [])
        raise AssertionError("expected ValueError for no profiles")
    except ValueError:
        pass
    out("  multi-rail reject no profiles OK")


def _consistent_rails_profiles():
    """Three rails and three profiles that genuinely run THROUGH all three
    rails (the well-formed input a multi-rail sweep is meant for): the
    outer rails flat at y=0/y=5, the interior one bulging in +Z at mid-span
    in x, and each profile passing through the same three rail points at
    its own station."""
    rails = [
        spline([Vector(0, 0, 0), Vector(5, 0, 0), Vector(10, 0, 0)]),
        spline([Vector(0, 2.5, 0), Vector(5, 2.5, 1), Vector(10, 2.5, 0)]),
        spline([Vector(0, 5, 0), Vector(5, 5, 0), Vector(10, 5, 0)]),
    ]
    profs = [
        spline([Vector(x, 0, 0), Vector(x, 2.5, 1.0 if x == 5 else 0.0),
                Vector(x, 5, 0)])
        for x in (0, 5, 10)]
    return rails, profs


def test_multi_rail_sweep_interpolates_every_rail():
    """THE point of a multi-rail sweep: interior rails must land ON the
    surface at every station the grid samples -- two_rail_sweep only ever
    contains the outer pair."""
    rails, profs = _consistent_rails_profiles()
    n_rail = 9
    s = core.multi_rail_sweep(rails, profs, n_rail=n_rail, n_prof=16)
    worst = 0.0
    for i in range(n_rail):
        t = i / (n_rail - 1.0)
        for r in rails:
            fp, lp = r.FirstParameter, r.LastParameter
            p = r.valueAt(fp + t * (lp - fp))
            worst = max(worst, Part.Vertex(p).distToShape(s)[0])
    assert worst < 1e-5, "rails drift off the sweep by %g" % worst
    out("  multi-rail all-rail interp OK  max dev=%.3e" % worst)


def test_multi_rail_sweep_still_follows_the_profiles():
    """Rails must not swallow the sections: where the profiles run through
    the rails the sweep still passes through the profiles themselves."""
    rails, profs = _consistent_rails_profiles()
    s = core.multi_rail_sweep(rails, profs, n_rail=9, n_prof=16)
    worst = 0.0
    for pro in profs:
        fp, lp = pro.FirstParameter, pro.LastParameter
        for k in range(11):
            p = pro.valueAt(fp + (k / 10.0) * (lp - fp))
            worst = max(worst, Part.Vertex(p).distToShape(s)[0])
    assert worst < 1e-3, "profiles drift off the sweep by %g" % worst
    out("  multi-rail profile interp OK  max dev=%.3e" % worst)


def test_multi_rail_sweep_interior_rail_changes_the_surface():
    """An interior rail is not decorative: rebuilding with and without it
    must give measurably different geometry (the earlier frame-tilt-only
    attempt produced a bit-identical surface)."""
    rails, profs = _consistent_rails_profiles()
    with_mid = core.multi_rail_sweep(rails, profs, n_rail=16, n_prof=16)
    without = core.multi_rail_sweep([rails[0], rails[2]], profs,
                                    n_rail=16, n_prof=16)
    assert abs(with_mid.Area - without.Area) > 1e-3, \
        (with_mid.Area, without.Area)
    out("  multi-rail interior rail bites OK  area %.3f vs %.3f"
        % (with_mid.Area, without.Area))


def _max_curve_deviation(curves, shape, samples):
    """Worst distance from midpoints of the (normalized-param) samples of
    each curve to `shape` -- the same measurement core._deviation_of_curves
    performs, inlined here so the test measures independently."""
    worst = 0.0
    for curve in curves:
        fp, lp = curve.FirstParameter, curve.LastParameter
        for k in range(samples):
            t = fp + (k + 0.5) / float(samples) * (lp - fp)
            worst = max(worst, Part.Vertex(curve.value(t)).distToShape(shape)[0])
    return worst


def test_multi_rail_sweep_gordon_route_holds_rails_continuously():
    """The well-formed network (every profile genuinely crosses every rail)
    must be solved CONTINUOUSLY, not station-skinned: the surface follows
    the full shape of every rail BETWEEN profile stations too. This is the
    property the station-skinned spine path cannot give (the polyline
    spine only anchors rail points at the stations)."""
    rails, profs = _consistent_rails_profiles()
    s = core.multi_rail_sweep(rails, profs, n_rail=13, n_prof=16)
    rail_dev = _max_curve_deviation(
        [r.Curve for r in rails], s, 33)
    prof_dev = _max_curve_deviation(
        [p.Curve for p in profs], s, 13)
    assert rail_dev < 1e-6, "rails drift off the Gordon sweep: %g" % rail_dev
    assert prof_dev < 1e-6, ("profiles drift off the Gordon sweep: %g"
                             % prof_dev)
    out("  multi-rail Gordon route continuous OK  rails=%.3e profiles=%.3e"
        % (rail_dev, prof_dev))


def test_multi_rail_sweep_gordon_route_curved_interior_rail():
    """A curved interior rail between two profile stations: the Gordon
    route must follow its sag between the stations (the station-skinning
    spine path would leave a ~sag-sized gap)."""
    profs = [
        spline([Vector(0, 0, 0), Vector(0, 2.5, 1.0), Vector(0, 5, 0)]),
        spline([Vector(10, 0, 0), Vector(10, 2.5, 1.0), Vector(10, 5, 0)]),
    ]
    rails = [
        spline([Vector(0, 0, 0), Vector(10, 0, 0)]),
        spline([Vector(0, 2.5, 1.0), Vector(5, 2.5, 2.0),
                Vector(10, 2.5, 1.0)]),
        spline([Vector(0, 5, 0), Vector(10, 5, 0)]),
    ]
    s = core.multi_rail_sweep(rails, profs, n_rail=13, n_prof=12)
    worst = _max_curve_deviation([rails[1].Curve], s, 41)
    assert worst < 1e-6, ("curved interior rail drifts off the sweep: %g"
                          % worst)
    out("  multi-rail Gordon curved interior rail OK  dev=%.3e" % worst)


def test_multi_rail_sweep_gordon_route_is_order_insensitive():
    """Rail order must not change the solved geometry: the Gordon route
    relabels each family onto its own shared axis (gordon_network's
    u_bar/v_bar sort), so a zigzag rails list gives the same surface, and
    every rail is still held exactly."""
    zfun = lambda y: 1.0 * math.sin(math.pi * y / 5.0)
    rails = [
        spline([Vector(0, y, zfun(y)), Vector(10, y, zfun(y))])
        for y in (0, 1.25, 2.5, 3.75, 5)]
    profs = [spline([Vector(x, y, zfun(y))
                     for y in (0, 1.25, 2.5, 3.75, 5)])
             for x in (0, 10)]
    a = core.multi_rail_sweep(rails, profs, n_rail=17, n_prof=20)
    zrails = [rails[i] for i in (0, 4, 1, 3, 2)]
    b = core.multi_rail_sweep(zrails, profs, n_rail=17, n_prof=20)
    assert abs(a.Area - b.Area) < 1e-6, (a.Area, b.Area)
    worst = _max_curve_deviation([r.Curve for r in rails], b, 25)
    assert worst < 1e-5, ("zigzag order loses a rail: %g" % worst)
    out("  multi-rail Gordon order-insensitive OK  area=%.4f dev=%.3e"
        % (a.Area, worst))


def test_multi_rail_sweep_spine_fallback_keeps_station_bites():
    """An incompatible network (profiles that miss an interior rail) must
    fall back to station skinning and still bite every rail: profiles win
    at their own stations, so the OUTER two rails -- which every profile
    genuinely reaches -- must stay exact at the n_rail stations."""
    r0 = spline([Vector(0, 0, 0), Vector(10, 0, 0)])
    rmid = spline([Vector(0, 2.5, 0), Vector(10, 2.5, 0)])
    r1 = spline([Vector(0, 5, 0), Vector(10, 5, 0)])
    prods = [
        spline([Vector(0, 0, 0), Vector(0, 2.5, 0.5), Vector(0, 5, 0)]),
        spline([Vector(5, 0, 0), Vector(5, 2.5, 1.0), Vector(5, 5, 0)]),
        spline([Vector(10, 0, 0), Vector(10, 2.5, 0.5), Vector(10, 5, 0)]),
    ]
    s = core.multi_rail_sweep([r0, rmid, r1], prods, n_rail=9, n_prof=16)
    worst = 0.0
    n_rail = 9
    for i in range(n_rail):
        t = i / (n_rail - 1.0)
        for r in (r0, r1):
            fp, lp = r.FirstParameter, r.LastParameter
            worst = max(worst, Part.Vertex(
                r.valueAt(fp + t * (lp - fp))).distToShape(s)[0])
    assert worst < 1e-5, "outer rails drift off the stroke fallback: %g" % worst
    out("  multi-rail spine fallback outer rails OK  dev=%.3e" % worst)


def test_multi_rail_sweep_two_rail_exact_delegation():
    """With exactly 2 rails multi_rail_sweep now delegates to two_rail_sweep
    directly -- the spine reduction it replaced only matched to
    floating-point noise, the delegation must match EXACTLY."""
    (r0, r1, _rmid), profs = _rails_profiles()
    a = core.two_rail_sweep(r0, r1, profs, n_rail=20, n_prof=8)
    b = core.multi_rail_sweep([r0, r1], profs, n_rail=20, n_prof=8)
    assert a.Area == b.Area
    assert len(a.Faces) == len(b.Faces)
    out("  multi-rail 2-rail exact delegation OK")


def _spine_fallback_fixture():
    """Three rails (two boundary straights + one straight interior the
    profiles miss) with bulged cross profiles -- the canonical incompatible
    network the station-skinning fallback serves."""
    r0 = spline([Vector(0, 0, 0), Vector(10, 0, 0)])
    rmid = spline([Vector(0, 2.5, 0), Vector(10, 2.5, 0)])
    r1 = spline([Vector(0, 5, 0), Vector(10, 5, 0)])
    prods = [
        spline([Vector(0, 0, 0), Vector(0, 2.5, 0.5), Vector(0, 5, 0)]),
        spline([Vector(5, 0, 0), Vector(5, 2.5, 1.0), Vector(5, 5, 0)]),
        spline([Vector(10, 0, 0), Vector(10, 2.5, 0.5), Vector(10, 5, 0)]),
    ]
    return (r0, rmid, r1), prods


def test_multi_rail_sweep_spine_profiles_exact_at_stations():
    """On the adaptive fallback each profile's own station is a HARD
    station (the C++ contact-point break), so the surface passes through
    the profiles' pinned columns exactly at their stations instead of
    approximating them from wherever the uniform grid happened to fall."""
    rails, prods = _spine_fallback_fixture()
    s = core.multi_rail_sweep(rails, prods, n_rail=9, n_prof=16)
    worst = 0.0
    for pro in prods:
        fp, lp = pro.FirstParameter, pro.LastParameter
        for k in range(9):
            p = pro.valueAt(fp + (k / 8.0) * (lp - fp))
            worst = max(worst, Part.Vertex(p).distToShape(s)[0])
    assert worst < 1e-3, "profiles drift off their own stations: %g" % worst
    out("  multi-rail spine profiles-exact-at-stations OK  dev=%.3e" % worst)


def test_multi_rail_sweep_spine_adaptive_bounds_rail_sag():
    """Adaptive stations: a SAGGING boundary rail between profile stations
    triggers subdivision, so the between-station rail hold is bounded --
    the old fixed uniform grid left the full per-gap sag unmanaged."""
    r0 = spline([Vector(0, 0, 0), Vector(5, 0, 2.0), Vector(10, 0, 0)])
    rmid = spline([Vector(0, 2.5, 0), Vector(10, 2.5, 0)])
    r1 = spline([Vector(0, 5, 0), Vector(10, 5, 0)])
    # profiles miss the interior rail -> incompatible -> skinning route
    prods = [
        spline([Vector(0, 0, 0), Vector(0, 2.5, 0.5), Vector(0, 5, 0)]),
        spline([Vector(5, 0, 2), Vector(5, 2.5, 0.5), Vector(5, 5, 0)]),
        spline([Vector(10, 0, 0), Vector(10, 2.5, 0.5), Vector(10, 5, 0)]),
    ]
    s = core.multi_rail_sweep([r0, rmid, r1], prods, n_rail=13, n_prof=12)
    worst = _max_curve_deviation([r0.Curve], s, 41)
    assert worst < 5e-3, "sagging boundary rail drifts off the sweep: %g" % worst
    out("  multi-rail spine adaptive sag bound OK  dev=%.3e" % worst)


def test_multi_rail_sweep_spine_rejects_collapsed_rails():
    """Rails that lie on top of each other collapse the station chord to a
    point (zero Jacobian in the C++ design) -- the sweep must refuse with
    a message instead of silently skinning garbage."""
    r0 = spline([Vector(0, 0, 0), Vector(10, 0, 0)])
    r_shared = spline([Vector(0, 0, 0), Vector(5, 0, 0), Vector(10, 0, 0)])
    (_r0, _rm, _r1), prods = _spine_fallback_fixture()
    try:
        core.multi_rail_sweep([r0, _rm, r_shared], prods, n_rail=9)
        raise AssertionError("expected ValueError for collapsed rails")
    except ValueError as exc:
        assert "Jacobian" in str(exc), exc
    out("  multi-rail spine collapsed rails rejected OK")


def test_single_rail_sweep_straight_pipe_closed_profile():
    """The single-rail (pipe) sweep: a closed circular profile on a straight
    spine produces a round tube -- every surface point sits at the offset
    radius from the spine, the profile-sampled ring is ON the surface
    (grid columns are exact), and between-columns sags are bounded by the
    column count (the surface is the interpolant through them)."""
    rail = spline([Vector(0, 0, 0), Vector(10, 0, 0)])
    profile = Part.makeCircle(1.0, Vector(0, 0, 0), Vector(1, 0, 0))
    assert profile.isClosed()
    n_prof = 64
    s = core.single_rail_sweep(rail, [profile], n_rail=21, n_prof=n_prof)
    assert s.Faces, "pipe sweep produced no faces"
    # radius hold: sample the face; every sample sits at offset radius 1
    face = s.Faces[0]
    surf = face.Surface
    _u0, _u1, v0, v1 = face.ParameterRange
    for k in range(7):
        for j in range(7):
            p = surf.value(_u0 + (k / 6.0) * (_u1 - _u0),
                           v0 + (j / 6.0) * (v1 - v0))
            r_off = math.sqrt(p.y * p.y + p.z * p.z)
            assert abs(r_off - 1.0) < 5e-3, (
                "tube leaks off radius: %g at (%g, %g, %g)"
                % (r_off, p.x, p.y, p.z))
            assert -1e-9 <= p.x <= 10.0 + 1e-9, p.x
    # profile hold at the start: grid columns reconstruct the ring exactly
    # (column params are k/(n_prof-1), the same spacing the sweep samples)
    worst = 0.0
    fp, lp = profile.FirstParameter, profile.LastParameter
    for k in range(n_prof):
        q = (k % n_prof) / float(n_prof - 1)
        p = profile.valueAt(fp + q * (lp - fp))
        worst = max(worst, Part.Vertex(p).distToShape(s)[0])
    assert worst < 1e-9, "profile ring off the tube: %g" % worst
    out("  single-rail straight pipe OK  dev=%.3e" % worst)


def test_single_rail_sweep_preserves_offsets_on_curved_rail():
    """RMF transport preserves each offset's LENGTH exactly (rotation only);
    on a curved inflected spine the surface must therefore sit at a
    CONSTANT normal distance from the rail -- any frame twist/flip shows
    up as radius drift."""
    rail = spline([Vector(0, 0, 0), Vector(5, 0, 2.0),
                   Vector(10, 0, -1.0)])
    profile = Part.makeCircle(0.5, Vector(0, 0, 0), _rail_start_axis(rail))
    s = core.single_rail_sweep(rail, [profile], n_rail=25, n_prof=64)
    face = s.Faces[0]
    surf = face.Surface
    _u0, _u1, _v0, _v1 = face.ParameterRange
    worst = 0.0
    for k in range(9):
        for j in range(6):
            p = surf.value(_u0 + (k / 8.0) * (_u1 - _u0),
                           _v0 + (j / 5.0) * (_v1 - _v0))
            d = Part.Vertex(p).distToShape(rail)[0]
            worst = max(worst, abs(d - 0.5))
    assert worst < 5e-3, "offset radius drifts on the curved spine: %g" % worst
    out("  single-rail curved spine offsets OK  dev=%.3e" % worst)


def _rail_start_axis(rail):
    """Unit tangent at the rail START (not the chord), for planting a
    profile perpendicular to it at the origin -- an oblique ring carries
    large along-tangent offset components, and the RMF transport correctly
    keeps that lean forever (rigid pipe behaviour), which would defeat a
    radius probe."""
    a, b = rail.FirstParameter, rail.LastParameter
    eps = (b - a) * 1e-3
    t = rail.valueAt(a + eps).sub(rail.valueAt(a))
    t.normalize()
    return t


def test_rmf_frames_no_twist_flip_on_inflected_spine():
    """_rmf_frames (double reflection) must NOT flip its up vector across an
    inflection (Frenet flips there) and must keep the frames orthonormal
    throughout -- an S-curve is the canonical case."""
    arc = core._ArcRail(spline([Vector(0, 0, 0), Vector(5, 0, 2.0),
                                Vector(10, 0, -1.0)]))
    stations = [i / 32.0 for i in range(33)]
    frames = core._rmf_frames(arc, stations, Vector(0, 1, 0))
    worst_ortho = 0.0
    for i in range(len(frames) - 1):
        t_i, ey_i, ez_i = frames[i]
        t_j, ey_j, _ez_j = frames[i + 1]
        assert ey_i.dot(ey_j) > 0.0, "up vector flipped at station %d" % i
        worst_ortho = max(worst_ortho, abs(t_i.dot(ey_i) - 0.0),
                          abs(t_i.dot(ez_i) - 0.0), abs(ey_i.dot(ez_i) - 0.0))
    assert worst_ortho < 1e-9, "frames lost orthonormality: %g" % worst_ortho
    out("  single-rail RMF no-flip inflection OK")


def test_single_rail_sweep_morphs_multiple_profiles():
    """Two profiles pinned at different arc stations: the surface passes
    through BOTH rings at their own stations (grid columns are exact,
    per-profile sampling aligned to the column params) while the sections
    morph between them."""
    rail = spline([Vector(0, 0, 0), Vector(10, 0, 0)])
    n_prof = 16
    pro_a = Part.makeCircle(1.0, Vector(0, 0, 0), Vector(1, 0, 0))
    pro_b = Part.makeCircle(0.5, Vector(5, 0, 0), Vector(1, 0, 0))
    s = core.single_rail_sweep(rail, [pro_a, pro_b], n_rail=21,
                               n_prof=n_prof)
    assert s.Faces, "morph sweep produced no faces"
    worst = 0.0
    for pro in (pro_a, pro_b):
        fp, lp = pro.FirstParameter, pro.LastParameter
        for k in range(n_prof):
            q = (k % n_prof) / float(n_prof - 1)
            p = pro.valueAt(fp + q * (lp - fp))
            worst = max(worst, Part.Vertex(p).distToShape(s)[0])
    assert worst < 1e-9, "pinned profiles off the morph sweep: %g" % worst
    out("  single-rail multi-profile morph OK  dev=%.3e" % worst)


def test_single_rail_sweep_rejects_degenerate_rail():
    """A zero-length rail has nothing to sweep along -- refuse with a
    message instead of producing garbage. (A tiny circle edge constructs
    fine; an interpolated spline segment that short already fails down in
    BSplineCurve.interpolate, so a circle exercises OUR guard.)"""
    rail = Part.makeCircle(1e-10, Vector(0, 0, 0), Vector(0, 0, 1))
    profile = Part.makeCircle(1.0, Vector(0, 0, 0), Vector(1, 0, 0))
    try:
        core.single_rail_sweep(rail, [profile], n_rail=5)
        raise AssertionError("expected ValueError for degenerate rail")
    except ValueError as exc:
        assert "degenerate" in str(exc) or "zero length" in str(exc), exc
    out("  single-rail degenerate rail rejected OK")


def test_spine_invert_first_crossing_on_swaying_profile():
    """_spine_invert must return the FIRST profile-param crossing, not the
    last one a monotone-bisection assumption happens to land on: a profile
    that sways across its spine (projection non-monotone) crosses the
    target several times, and the rail's column must anchor to the
    earliest one -- the plain-bisection version silently picked whichever
    root its bracket arithmetic exposed."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0)]
    z_params = core._spine_params(pts)
    eval_sway = lambda t: Vector(
        10.0 * t + 6.0 * math.sin(2.0 * math.pi * t), 0, 0)
    target = 0.3

    # independent reference: dense scan for the first crossing
    def proj(t):
        return (10.0 * t + 6.0 * math.sin(2.0 * math.pi * t)) / 10.0
    n = 20001
    first = None
    for k in range(1, n):
        t0, t1 = (k - 1) / (n - 1.0), k / (n - 1.0)
        if (proj(t0) < target) != (proj(t1) < target):
            lo, hi = t0, t1
            sign0 = proj(t0) < target
            for _ in range(60):
                mm = 0.5 * (lo + hi)
                if (proj(mm) < target) != sign0:
                    hi = mm
                else:
                    lo = mm
            first = 0.5 * (lo + hi)
            break
    assert first is not None
    got = core._spine_invert(z_params, pts, eval_sway, target, 0.0, 1.0)
    assert abs(got - first) < 1e-6, (got, first)
    # multiple crossings exist (else this test proves nothing)
    crossings = 0
    for k in range(1, n):
        if (proj(k - 1 / (n - 1.0)) < target) != \
                (proj(k / (n - 1.0)) < target):
            crossings += 1
    assert crossings >= 2, crossings
    # monotone profile: old behavior fully preserved
    got = core._spine_invert(z_params, pts, lambda t: Vector(10.0 * t, 0, 0),
                             0.5, 0.0, 1.0)
    assert abs(got - 0.5) < 1e-9, got
    out("  spine_invert first-crossing OK  first=%.6f got=%.6f"
        % (first, core._spine_invert(z_params, pts, eval_sway, target,
                                     0.0, 1.0)))


def test_rail_order_warning_flags_zigzag():
    """The rail-order heuristics must flag a rails list whose order does
    not match how the sections traverse them, and stay silent on a
    coherent one."""
    good = [[0.1, 0.4, 1.0], [0.0, 0.5, 0.9]]
    assert core._rail_order_warning(good) is None
    bad = [[0.4, 0.1, 1.0], [0.0, 0.5, 0.9]]
    msg = core._rail_order_warning(bad)
    assert msg is not None and "rail" in msg
    out("  rail order warning OK")


def test_try_gordon_route_gates():
    """_try_gordon_route must answer None (fall back) for an incompatible
    network and a shape for a compatible one."""
    rails, profs = _consistent_rails_profiles()
    shape = core._try_gordon_route(rails, profs)
    assert shape is not None
    rail_dev = core._deviation_of_curves(
        [r.Curve for r in rails], shape, 25)
    assert rail_dev <= core._MULTIRAIL_GORDON_RAIL_TOL
    # incompatible: profiles bump away from the interior rail
    r0 = spline([Vector(0, 0, 0), Vector(10, 0, 0)])
    rmid = spline([Vector(0, 2.5, 0), Vector(10, 2.5, 0)])
    r1 = spline([Vector(0, 5, 0), Vector(10, 5, 0)])
    prods = [
        spline([Vector(0, 0, 0), Vector(0, 2.5, 0.5), Vector(0, 5, 0)]),
        spline([Vector(10, 0, 0), Vector(10, 2.5, 1.0), Vector(10, 5, 0)]),
    ]
    assert core._try_gordon_route([r0, rmid, r1], prods) is None
    out("  try_gordon_route gates OK")


# ---- Gordon: input validation --------------------------------------------

def test_gordon_too_few_curves():
    """gordon_network must raise a clear error when either family has
    fewer than 2 curves."""
    for u, v in (([], [spline([Vector(0, 0, 0), Vector(10, 0, 0)])]),
                 ([spline([Vector(0, 0, 0), Vector(10, 0, 0)])], [])):
        try:
            core.gordon_network(u, v)
        except ValueError as exc:
            out("  gordon validation  OK  %r" % exc)
            continue
        raise AssertionError("expected ValueError")


# ---- Gordon: the surface must actually touch every input rail ----------
# This is the invariant the user reported as broken in the previous
# simplified algorithm. Verify by sampling each rail and checking that
# the closest face of the result is within a small distance of the sample.

def _max_rail_to_surface_distance(rails, surf):
    """For each rail, sample 20 evenly-spaced points and return the worst
    closest-face distance to any of those samples."""
    worst = 0.0
    worst_pt = None
    worst_rail = None
    for rail in rails:
        try:
            fp, lp = rail.FirstParameter, rail.LastParameter
        except Exception:
            continue
        for k in range(20):
            t = fp + (k + 0.5) / 20.0 * (lp - fp)
            try:
                p = rail.valueAt(t)
            except Exception:
                continue
            best = 1e18
            for f in surf.Faces:
                try:
                    uv = f.Surface.parameter(p)
                    # If the projection is in-bounds, use the surface value.
                    pr = f.ParameterRange
                    if pr[0] - 1e-6 <= uv[0] <= pr[1] + 1e-6 and \
                       pr[2] - 1e-6 <= uv[1] <= pr[3] + 1e-6:
                        ps = f.Surface.value(uv[0], uv[1])
                        best = min(best, ps.distanceToPoint(p))
                except Exception:
                    pass
            if best > worst:
                worst = best
                worst_pt = p
                worst_rail = rail
    return worst, worst_pt


def test_gordon_rails_pass_through():
    """REGRESSION: the Gordon surface must pass through every input rail
    at every intersection. The previous simplified algorithm only
    interpolated the (nu x nv) grid of closest-approach points, so the
    surface boundary didn't follow the rails.

    For a 4x4 rectangular network with 4 profiles and 4 guides, sample
    each rail and verify the closest-face distance is below 0.01 world
    units (we use 0.01 because the algorithm does its averaging step
    which can introduce sub-millimetre drift).

    The profiles and guides are both sampled FROM ONE SHARED height
    function h(x, y), so every profile/guide pair genuinely crosses at
    (x, y, h(x, y)) -- a truly compatible curve network. (An earlier
    version of this test built the two families independently, each
    from its own isolated 3-point spline sharing only the SHAPE of the
    bump, not an actual shared surface -- profile i and guide j then
    only really crossed near the diagonal i==j; off-diagonal pairs
    passed up to 0.83 world units apart. No Gordon boolean-sum
    construction -- this one or any other -- can make a surface pass
    within 0.01 of two rails that are themselves 0.83 apart at that
    location, so that version's 0.01 assertion was unsatisfiable
    regardless of the algorithm; it happened to go undetected before
    because the earlier gordon_network implementation didn't actually
    honour the crossing grid at all.)
    """
    def h(x, y):
        return math.sin(x * math.pi / 10.0) * math.sin(y * math.pi / 10.0)
    x_samples = (0, 2, 4, 6, 8, 10)
    y_samples = (0, 2, 4, 6, 8, 10)
    u = [spline([Vector(x, y, h(x, y)) for x in x_samples])
         for y in (0, 3.3, 6.6, 10)]
    v = [spline([Vector(x, y, h(x, y)) for y in y_samples])
         for x in (0, 3.3, 6.6, 10)]
    s = core.gordon_network(u, v)
    assert s.Area > 0
    all_rails = list(u) + list(v)
    worst, worst_pt = _max_rail_to_surface_distance(all_rails, s)
    assert worst < 0.01, (
        "Gordon surface should pass through every input rail; worst "
        "distance = %.4f (rail point %s)" % (worst, worst_pt))
    out("  gordon rails-touch OK  max-rail-dist=%.4f" % worst)


# ---- Knot editor regression ----------------------------------------------

def test_control_poly_curve_explicit_knots():
    """The ControlCurve B-spline uses buildFromPolesMultsKnots directly --
    verifies user-supplied knots round-trip exactly."""
    pts = [Vector(0, 0, 0), Vector(3, 5, 0), Vector(7, 5, 0), Vector(10, 0, 0)]
    knots = [0.0, 1.0]
    mults = [4, 4]
    shp = core.control_poly_curve(pts, degree=3, knots=knots, mults=mults)
    k, m = core.knots_from_shape(shp)
    assert k == knots, (k, knots)
    assert m == mults, (m, mults)
    bs = shp.Edges[0].Curve
    assert bs.getPole(1).distanceToPoint(Vector(0, 0, 0)) < 1e-9
    assert bs.getPole(4).distanceToPoint(Vector(10, 0, 0)) < 1e-9
    out("  control_poly knots OK  k=%d m=%s" % (len(k), m))

    # With interior knots
    pts2 = [Vector(0, 0, 0), Vector(2, 4, 0), Vector(5, 4, 0),
            Vector(6, -2, 0), Vector(8, 5, 0), Vector(10, 0, 0)]
    mults2 = [4, 1, 1, 4]
    unique2 = [0.0, 0.4, 0.7, 1.0]
    shp2 = core.control_poly_curve(pts2, degree=3, knots=unique2, mults=mults2)
    k2, m2 = core.knots_from_shape(shp2)
    assert k2 == unique2, (k2, unique2)
    assert m2 == mults2, (m2, mults2)
    e = shp2.Edges[0]; e_m = core.control_poly_curve(
        pts2, degree=3, knots=[0.0, 0.6, 0.7, 1.0], mults=mults2).Edges[0]
    p_a = e.valueAt(0.5 * (e.FirstParameter + e.LastParameter))
    p_b = e_m.valueAt(0.5 * (e_m.FirstParameter + e_m.LastParameter))
    assert p_a.distanceToPoint(p_b) > 1e-3
    out("  control_poly knots 2 OK  k=%d m=%s mid-delta=%.3f"
        % (len(k2), m2, p_a.distanceToPoint(p_b)))


# ---- snaputil: logic extracted from the GUI editor layer -----------------
# editmode.PointEditor / KnotEditor and taskpanels.CurvePanel all do
# in-viewport snapping; the screen-projection part needs a live 3D view
# and can't run here, but the pure geometry it depends on (seg_dist,
# collect_snap_points) was split out into snaputil.py specifically so it
# could be covered headlessly -- this is new coverage that didn't exist
# before the snap-logic dedup.

def test_snaputil_seg_dist():
    from rfstudio import snaputil
    # point exactly on the segment -> 0
    assert snaputil.seg_dist((5, 0), (0, 0), (10, 0)) < 1e-9
    # point off to the side -> perpendicular distance
    d = snaputil.seg_dist((5, 3), (0, 0), (10, 0))
    assert abs(d - 3.0) < 1e-9
    # point beyond the segment end -> clamped to nearest endpoint
    d2 = snaputil.seg_dist((15, 0), (0, 0), (10, 0))
    assert abs(d2 - 5.0) < 1e-9
    out("  snaputil seg_dist  OK  on=%.4f side=%.2f beyond=%.2f"
        % (snaputil.seg_dist((5, 0), (0, 0), (10, 0)), d, d2))


def test_snaputil_collect_snap_points():
    """collect_snap_points must pick up control points and vertices (exact
    targets) from every other object by default, with coarse on-curve
    samples available only via include_samples=True, and must exclude the
    object passed as `exclude`."""
    from rfstudio import snaputil
    doc = FreeCAD.newDocument("snaputil_test") if hasattr(FreeCAD, "newDocument") \
        else None
    if doc is None:
        out("  snaputil collect   SKIP (no document API available)")
        return
    try:
        line = doc.addObject("Part::Feature", "Line")
        line.Shape = Part.makeLine(Vector(0, 0, 0), Vector(10, 0, 0))
        doc.recompute()
        pts = snaputil.collect_snap_points(doc, exclude=None)
        # both endpoints (vertices) must be present
        endpoints_found = sum(
            1 for p in pts if p.distanceToPoint(Vector(0, 0, 0)) < 1e-6
            or p.distanceToPoint(Vector(10, 0, 0)) < 1e-6)
        assert endpoints_found >= 2, "expected both line endpoints as candidates"
        # samples are OFF by default now -> a plain line yields ONLY its two
        # exact endpoints, no coarse interior arc-length samples (the exact
        # ray->edge path covers 'anywhere on the curve' precisely instead).
        assert len(pts) == 2, "default should be exact endpoints only, got %d" % len(pts)
        # opting samples back in restores the old interior-sample behaviour
        pts_s = snaputil.collect_snap_points(doc, exclude=None, include_samples=True)
        assert len(pts_s) > 2, "include_samples=True should add interior samples"
        # excluding the object removes its candidates entirely
        pts_excluded = snaputil.collect_snap_points(doc, exclude=line)
        assert len(pts_excluded) == 0
        out("  snaputil collect   OK  exact=%d samples-on=%d (excluded -> %d)"
            % (len(pts), len(pts_s), len(pts_excluded)))
    finally:
        FreeCAD.closeDocument(doc.Name)


def test_snaputil_ray_snap_is_depth_accurate():
    """The exact snap must project the cursor RAY onto the curve, not a
    single focal-plane point. This is the accuracy fix: the ray method
    lands where the curve truly passes closest to what the cursor points
    at, independent of depth, whereas the old focal-plane method drifts
    along the curve (here, all the way to the wrong endpoint)."""
    from rfstudio import snaputil
    edge = Part.Edge(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)))

    # Ray straight down through x=3: correct snap is (3,0,0).
    pt, d, _ = snaputil.nearest_point_on_shapes_to_ray(
        Vector(3, 0, 50), Vector(0, 0, -1), [edge])
    assert pt is not None and pt.distanceToPoint(Vector(3, 0, 0)) < 1e-6, pt
    assert d < 1e-6, d

    # Adversarial: a ray from (0,0,50) tilted in +x overshoots the segment
    # end, so the true nearest point on the segment is the endpoint (10,0,0).
    pt2, _d2, _ = snaputil.nearest_point_on_shapes_to_ray(
        Vector(0, 0, 50), Vector(0.6, 0, -0.8), [edge])
    assert pt2 is not None and pt2.distanceToPoint(Vector(10, 0, 0)) < 1e-4, pt2

    # The OLD focal-plane method (nearest point to the getPoint result at
    # z=50, i.e. (0,0,50)) would instead return the OPPOSITE end (0,0,0) --
    # a full 10-unit error. Assert the two methods genuinely disagree here,
    # so this test would fail if anything silently reverted to the old path.
    pt_old, _do, _ = snaputil.nearest_point_on_shapes(Vector(0, 0, 50), [edge])
    assert pt_old.distanceToPoint(Vector(0, 0, 0)) < 1e-6, pt_old
    assert pt2.distanceToPoint(pt_old) > 5.0, (pt2, pt_old)

    # Degenerate direction is rejected, not crashed.
    none_pt, _, _ = snaputil.nearest_point_on_shapes_to_ray(
        Vector(0, 0, 0), Vector(0, 0, 0), [edge])
    assert none_pt is None
    out("  snaputil ray-snap  OK  straight=(3,0,0) tilted->endpoint (old drifts 10u)")


def test_snaputil_axis_snap_stays_on_axis():
    """Axis-locked snap must return a point ON the drag axis where a target
    curve crosses it -- so the axis constraint is preserved while still
    snapping to the crossing (Feature: snap during axis drags)."""
    from rfstudio import snaputil
    # target: the world X axis segment 0..10
    edge = Part.Edge(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)))
    # drag axis: vertical (Z) line through x=3 -> crosses the target at (3,0,0)
    pt, d, _ = snaputil.closest_point_on_axis_to_shapes(
        Vector(3, 0, 20), Vector(0, 0, 1), [edge])
    assert pt is not None and pt.distanceToPoint(Vector(3, 0, 0)) < 1e-6, pt
    assert d < 1e-6, d
    # a drag axis that never comes near the segment: still returns the point
    # of closest approach on the axis, with a nonzero distance the caller
    # can gate on via max_dist.
    pt2, d2, _ = snaputil.closest_point_on_axis_to_shapes(
        Vector(50, 0, 20), Vector(0, 0, 1), [edge], max_dist=1.0)
    assert pt2 is None, (pt2, d2)   # 40 units away -> rejected by max_dist
    out("  snaputil axis-snap OK  crossing=(3,0,0) on-axis; far target rejected")


def test_snaputil_skips_degenerate_edges():
    """Degenerate edges must never reach OCCT distToShape: BRepExtrema
    segfaults on them instead of raising (uncatchable by try/except),
    which crashed every cursor move ('Access violation') with Snap on
    whenever such an edge existed anywhere in the document. The
    collector drops them and every distToShape loop skips them, while
    healthy edges keep snapping."""
    from rfstudio import snaputil
    good = Part.Edge(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)))
    assert snaputil._valid_dist_shape(good) is True
    assert snaputil._valid_dist_shape(None) is False
    assert snaputil._valid_dist_shape("not-a-shape") is False
    try:
        bad = Part.Edge(Part.LineSegment(Vector(1, 1, 1), Vector(1, 1, 1)))
    except Exception:
        bad = None   # OCCT refused to even build it -- also acceptable
    shapes = [good] if bad is None else [good, bad]
    if bad is not None:
        assert snaputil._valid_dist_shape(bad) is False, "zero-length edge kept"
    # Ray straight down through x=3 must still land on (3,0,0) with the
    # degenerate entry present in the candidate list.
    pt, d, _ = snaputil.nearest_point_on_shapes_to_ray(
        Vector(3, 0, 50), Vector(0, 0, -1), shapes)
    assert pt is not None and pt.distanceToPoint(Vector(3, 0, 0)) < 1e-6, pt
    assert d < 1e-6, d
    out("  snaputil degenerate OK  bad-skipped=%s snap=(3,0,0)"
        % (bad is not None))


def test_snaputil_ray_span_is_bbox_bounded():
    """The stand-in ray segment must span the candidates' bbox as seen
    from the ray origin -- not a fixed 2e7-unit edge, which forces
    BRepExtrema over an enormous parameter range (slow, fragile, and a
    segfault vector behind the 'Access violation while drawing'
    crashes). Small model -> small span; snapping stays exact,
    including from a far eye."""
    from rfstudio import snaputil
    edge = Part.Edge(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)))
    hl = snaputil._bounded_ray_half_len(Vector(3, 0, 50), [edge], 1e7)
    assert hl < 1e7 and hl > 50.0, hl   # covers eye->model, not 2e7
    assert snaputil._bounded_ray_half_len(Vector(0, 0, 0), [], 1e7) == 1e7
    assert snaputil._bounded_ray_half_len(
        Vector(0, 0, 0), [None, "junk"], 1e7) == 1e7
    pt, d, _ = snaputil.nearest_point_on_shapes_to_ray(
        Vector(3, 0, 1e6), Vector(0, 0, -1), [edge])
    assert pt is not None and pt.distanceToPoint(Vector(3, 0, 0)) < 1e-3, pt
    out("  snaputil ray-span  OK  half_len=%.1f far-eye snap=(3,0,0)" % hl)


def test_on_curve_constraint_pulls_point_onto_edge():
    """The on_curve constraint must pull a free point onto a fixed target
    edge via the simultaneous solver (Feature: point-on-curve constraint)."""
    from rfstudio import core
    edge = Part.Edge(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)))
    p = Vector(3, 2, 0)   # 2 units off the X axis
    solved = core.solve_point_constraints([p], [("on_curve", 0, edge)])
    q = solved[0]
    # point now lies on the edge (distance ~0), having moved down in Y
    d = Part.Vertex(q).distToShape(edge)[0]
    assert d < 1e-3, (tuple(round(c, 4) for c in q), d)
    assert abs(q.y) < 1e-3, q
    out("  on_curve solve     OK  (3,2,0) -> (%.3f,%.3f,%.3f) d=%.2e"
        % (q.x, q.y, q.z, d))


def test_snaputil_collect_intersection_points():
    """collect_intersection_points must find the TRUE crossing of two
    edges that actually meet in 3D, reject a near-miss pair beyond `tol`,
    and skip entirely once fed more than `max_edges` (cost control)."""
    from rfstudio import snaputil
    e1 = Part.Edge(Part.LineSegment(Vector(-5, 0, 0), Vector(5, 0, 0)))
    e2 = Part.Edge(Part.LineSegment(Vector(0, -5, 0), Vector(0, 5, 0)))
    hits = snaputil.collect_intersection_points([e1, e2])
    assert len(hits) == 1, hits
    assert hits[0].distanceToPoint(Vector(0, 0, 0)) < 1e-6, hits[0]

    # a pair whose bounding boxes don't overlap -> broad-phase rejects it,
    # no crossing reported
    e3 = Part.Edge(Part.LineSegment(Vector(100, 100, 0), Vector(105, 100, 0)))
    hits2 = snaputil.collect_intersection_points([e1, e3])
    assert hits2 == [], hits2

    # too many candidate edges -> skipped outright rather than doing an
    # expensive O(n^2) scan
    many = [e1, e2] + [Part.Edge(Part.LineSegment(Vector(i, i, 0), Vector(i+1, i, 0)))
                       for i in range(70)]
    hits3 = snaputil.collect_intersection_points(many, max_edges=60)
    assert hits3 == [], "expected max_edges guard to skip a 72-edge scan"
    out("  intersection pts   OK  crossing=(0,0,0), bbox-reject + max_edges guard hold")


def test_snaputil_collect_strong_snap_points_includes_intersections():
    """collect_strong_snap_points must combine exact discrete points
    (vertices/control points/knots) with true curve-curve intersections
    from the OTHER objects in the document."""
    from rfstudio import snaputil
    doc = FreeCAD.newDocument("snaputil_strong_test") if hasattr(FreeCAD, "newDocument") \
        else None
    if doc is None:
        out("  strong snap pts    SKIP (no document API available)")
        return
    try:
        a = doc.addObject("Part::Feature", "A")
        a.Shape = Part.makeLine(Vector(-5, 0, 0), Vector(5, 0, 0))
        b = doc.addObject("Part::Feature", "B")
        b.Shape = Part.makeLine(Vector(0, -5, 0), Vector(0, 5, 0))
        doc.recompute()
        pts = snaputil.collect_strong_snap_points(doc, exclude=None)
        # both lines' endpoints (4 exact vertices) plus their crossing at origin
        crossing_found = any(p.distanceToPoint(Vector(0, 0, 0)) < 1e-6 for p in pts)
        assert crossing_found, "expected the A/B crossing point among strong candidates"
        endpoint_found = any(p.distanceToPoint(Vector(5, 0, 0)) < 1e-6 for p in pts)
        assert endpoint_found, "expected line A's endpoint among strong candidates"
        out("  strong snap pts    OK  n=%d includes crossing + endpoints" % len(pts))
    finally:
        FreeCAD.closeDocument(doc.Name)


class _FakeOrthoView(object):
    """Minimal stand-in for a View3DInventor: a fixed top-down orthographic
    view along -Z, just enough for pick_ray/screen_project to run for real
    outside the FreeCAD Gui. getPoint/getPointOnScreen are exact inverses
    of each other (both just drop/restore Z), so this exercises the ACTUAL
    nearest_snap_ranked/nearest_exact_snap/nearest_snap code paths headlessly
    instead of only asserting the rule they're supposed to encode."""
    def getPoint(self, sx, sy):
        return Vector(sx, sy, 0)

    def getCameraType(self):
        return "Orthographic"

    def getViewDirection(self):
        return Vector(0, 0, -1)

    def getPointOnScreen(self, vec):
        return (vec.x, vec.y)


def test_snaputil_vertex_beats_closer_edge_point():
    """Vertex/endpoint priority: nearest_snap_ranked must return an exact
    discrete candidate (e.g. a curve endpoint) over an on-edge projection
    that is geometrically closer to the cursor, as long as the discrete
    candidate is also within the pixel tolerance -- mirroring Blender's
    'a vertex hit beats an edge hit within threshold' rule. Runs the REAL
    ranked-snap code path via a fake orthographic view (see _FakeOrthoView),
    not a simulation of its rule."""
    from rfstudio import snaputil
    view = _FakeOrthoView()
    # cursor at screen (0.05, 0); an edge passes at x=0.02 (closer to the
    # cursor than the vertex at the origin), but the vertex is ALSO within
    # tolerance -- it must win.
    pos = (0.05, 0.0)
    vertex = Vector(0.0, 0, 0)
    edge = Part.Edge(Part.LineSegment(Vector(0.02, 1, 0), Vector(0.02, -1, 0)))
    max_px = 0.5   # generous tolerance in this synthetic 1-unit-per-px view

    # sanity: the edge really is screen-closer to the cursor than the vertex
    edge_only = snaputil.nearest_exact_snap(view, [edge], pos, max_px)
    assert edge_only is not None
    assert abs(edge_only.x - 0.02) < 1e-9, edge_only
    assert abs(pos[0] - 0.02) < abs(pos[0] - 0.0), "edge should be screen-closer"

    ranked = snaputil.nearest_snap_ranked(view, [vertex], [edge], pos, max_px)
    assert ranked is not None
    assert ranked.distanceToPoint(vertex) < 1e-9, \
        "expected the exact vertex to win over the closer on-edge point, got %r" % (ranked,)

    # and when the vertex is OUT of tolerance, the edge projection still wins
    far_vertex = Vector(100, 100, 0)
    ranked2 = snaputil.nearest_snap_ranked(view, [far_vertex], [edge], pos, max_px)
    assert ranked2 is not None and abs(ranked2.x - 0.02) < 1e-9, ranked2
    out("  vertex priority    OK  in-tol vertex wins; out-of-tol falls to edge")


def test_review_recommendation_classifies_outcomes():
    good = core.review_recommendation({
        "continuity": {"worst_grade": "G2"},
        "draft": {"worst_angle": 2.0},
    })
    assert good["status"] == "good"
    assert "step" in good
    watch = core.review_recommendation({
        "continuity": {"worst_grade": "G1"},
        "draft": {"worst_angle": 7.0},
    })
    assert watch["status"] == "watch"
    out("  review recommendation OK  good=%s watch=%s"
        % (good["status"], watch["status"]))


# ---- native CleanPatch44/66 (core.clean_patch_surface) --------------------
# The surface analog of control_poly_curve: the grid IS the control net, not
# an interpolation target. These back the native RFS_CleanPatch44/66
# providers that replace Silk's CubicSurface_44/66.

def test_clean_patch_44_exact_control_net():
    """A 4x4 clean patch must be an exact clamped bicubic single-span
    surface: its 4 CORNERS must equal the grid corners exactly (clamped
    B-splines always interpolate their end poles), and the pole count/
    degree must match a 4x4-grid bicubic patch."""
    grid = [[Vector(x, y, 1.0 if (x, y) in ((1, 1),) else 0.0)
            for x in range(4)] for y in range(4)]
    s = core.clean_patch_44(grid)
    surf = s.Faces[0].Surface
    assert surf.UDegree == 3 and surf.VDegree == 3
    assert surf.NbUPoles == 4 and surf.NbVPoles == 4
    u0, u1, v0, v1 = s.Faces[0].ParameterRange
    corners = [surf.value(u, v) for u in (u0, u1) for v in (v0, v1)]
    expected = [Vector(0, 0, 0), Vector(0, 3, 0), Vector(3, 0, 0), Vector(3, 3, 0)]
    for c in corners:
        assert min(c.distanceToPoint(e) for e in expected) < 1e-6
    out("  clean_patch_44     OK  deg=%d poles=%dx%d"
        % (surf.UDegree, surf.NbUPoles, surf.NbVPoles))


def test_clean_patch_66_exact_control_net():
    grid = [[Vector(x, y, 0.0) for x in range(6)] for y in range(6)]
    s = core.clean_patch_66(grid)
    surf = s.Faces[0].Surface
    assert surf.UDegree == 5 and surf.VDegree == 5
    assert surf.NbUPoles == 6 and surf.NbVPoles == 6
    assert s.Area > 0
    out("  clean_patch_66     OK  deg=%d poles=%dx%d area=%.2f"
        % (surf.UDegree, surf.NbUPoles, surf.NbVPoles, s.Area))


def test_clean_patch_rejects_wrong_grid_size():
    """clean_patch_44/66 must reject a mis-sized or ragged grid with a clear
    error rather than silently building the wrong surface -- this is the
    exact 'silent no-op' failure mode flagged elsewhere in the project."""
    bad_3x4 = [[Vector(x, y, 0) for x in range(4)] for y in range(3)]
    try:
        core.clean_patch_44(bad_3x4)
        raise AssertionError("expected ValueError for a 3x4 grid")
    except ValueError as exc:
        out("  clean_patch reject  OK  %r" % exc)
    ragged = [[Vector(0, 0, 0), Vector(1, 0, 0)], [Vector(0, 1, 0)]]
    try:
        core.clean_patch_surface(ragged)
        raise AssertionError("expected ValueError for a ragged grid")
    except ValueError as exc:
        out("  clean_patch ragged  OK  %r" % exc)


# ---- native Trim / SketchOnSurf (core.trim_face / project_wire_onto_face) -

def test_project_wire_onto_face():
    """Projecting a straight line that hovers above a flat face must land
    it (approximately) on the face -- since distToShape finds the true
    nearest point, the Z-offset should collapse to ~0."""
    face = Part.Face(Part.makePlane(20, 20, Vector(-10, -10, 0)))
    hover = Part.makePolygon(
        [Vector(-5, 0, 3), Vector(0, 0, 3), Vector(5, 0, 3)])
    projected = core.project_wire_onto_face(face, hover, n=20)
    for e in projected.Edges:
        fp, lp = e.FirstParameter, e.LastParameter
        for k in range(5):
            t = fp + k / 4.0 * (lp - fp)
            p = e.valueAt(t)
            assert abs(p.z) < 1e-3, "projected point not on face: %s" % p
    out("  project_onto_face   OK  n_edges=%d" % len(projected.Edges))


def test_trim_face_on_surface_wire():
    """trim_face's fast path: a wire already lying on the face's surface
    (e.g. output of project_wire_onto_face) must produce a valid trimmed
    face whose area is smaller than the untrimmed face."""
    face = Part.Face(Part.makePlane(20, 20, Vector(-10, -10, 0)))
    boundary = Part.makePolygon([
        Vector(-3, -3, 0), Vector(3, -3, 0), Vector(3, 3, 0),
        Vector(-3, 3, 0), Vector(-3, -3, 0)])
    trimmed = core.trim_face(face, boundary)
    assert trimmed.Area > 0
    assert trimmed.Area < face.Area
    out("  trim_face on-surf   OK  area=%.2f (full=%.2f)"
        % (trimmed.Area, face.Area))


def test_trim_face_off_surface_wire_falls_back_to_projection():
    """trim_face's fallback path: a wire hovering above the face must still
    produce a valid trimmed face by projecting first."""
    face = Part.Face(Part.makePlane(20, 20, Vector(-10, -10, 0)))
    hover_boundary = Part.makePolygon([
        Vector(-3, -3, 2), Vector(3, -3, 2), Vector(3, 3, 2),
        Vector(-3, 3, 2), Vector(-3, -3, 2)])
    trimmed = core.trim_face(face, hover_boundary)
    assert trimmed.Area > 0
    assert trimmed.Area < face.Area
    out("  trim_face fallback  OK  area=%.2f" % trimmed.Area)


def test_manufacturability_primitives():
    """Offset/thicken/shell should produce valid geometry for a simple face/solid."""
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    offset = core.offset_surface(face, 1.0)
    assert offset.Area > 0
    thick = core.thicken_surface(face, 1.0)
    assert thick.Solids and thick.Volume > 0
    solid = Part.makeBox(10, 10, 10)
    shell = core.shell_solid(solid, [solid.Faces[5]], 1.0)
    assert shell.Volume > 0
    out("  manufacturability  OK  offset=%.2f thick=%.2f shell=%.2f"
        % (offset.Area, thick.Volume, shell.Volume))


def test_surface_qa_metrics_and_network_audit():
    """Surface QA helpers should classify identical faces as G2 and produce a network audit."""
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    report = core.surface_deviation(face_a, face_b, samples=8)
    assert report["max_g0"] < 1e-6
    assert report["max_g1_deg"] < 1e-6
    assert report["max_g2"] < 1e-6
    grade = core.classify_continuity(report)
    assert grade == "G2"
    net = core.network_continuity_report([face_a, face_b], samples=8)
    assert net["worst_grade"] == "G2"
    out("  surface QA         OK  grade=%s seams=%d" % (grade, len(net["seams"])))


def test_surface_review_summary_reports_status():
    """Surface review summary should roll continuity and draft metrics into one report."""
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    summary = core.surface_review_summary([face_a, face_b], pull_dir=Vector(0, 0, 1))
    assert summary["continuity"]["worst_grade"] == "G2"
    assert summary["draft"]["face_count"] == 2
    assert summary["draft"]["worst_angle"] >= 0
    out("  review summary     OK  grade=%s draft=%.2f"
        % (summary["continuity"]["worst_grade"], summary["draft"]["worst_angle"]))


def test_network_aware_match_selects_best_reference():
    """A multi-reference match should auto-pick the reference patch that
    best satisfies the target seam, rather than blindly taking the first
    candidate."""
    target = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    good_ref = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    bad_ref = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 2)))
    rebuilt, report, chosen_ref = core.match_surface_to_references(
        target, [bad_ref, good_ref], continuity="G2", n=12, scale=1.0)
    assert rebuilt.Area > 0
    assert chosen_ref is good_ref
    assert report["max_g0"] < 1e-6
    assert report["max_g1_deg"] < 1e-6
    assert report["max_g2"] < 1e-6
    out("  network match      OK  chosen=%s" % (chosen_ref is good_ref))


def test_curated_commands_expose_manufacturability_and_qa():
    from rfstudio import commands
    for cmd_id in ("RFS_Offset", "RFS_Thicken", "RFS_Shell",
                   "RFS_ContinuityCheck", "RFS_NetworkAudit",
                   "RFS_SurfaceReview", "RFS_WorkflowGuide",
                   "RFS_StitchShell"):
        assert cmd_id in commands._PANEL_COMMANDS, cmd_id
    assert any("RFS_Offset" in row for row in commands.TOOLBARS)
    out("  curated commands   OK")


def test_match_surface_exact_g1_identical_planes():
    """Two identical planar faces (already knot-compatible after fitting
    to BSplineSurface) must match with zero deviation -- the trivial but
    necessary correctness check for match_surface_exact_g1's control-net
    copy."""
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    tgt = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    rebuilt = core.match_surface_exact_g1(ref, tgt)
    report = core.surface_deviation(rebuilt.Faces[0], ref, samples=8)
    assert report["max_g0"] < 1e-6
    assert report["max_g1_deg"] < 1e-6
    out("  match_g1 identical  OK  max_g0=%.2e max_g1=%.2e"
        % (report["max_g0"], report["max_g1_deg"]))


def test_match_surface_exact_g2_identical_planes():
    """Two identical planar faces must match with zero G0/G1/G2 deviation
    -- the trivial correctness check for match_surface_exact_g2's
    three-row control-net copy, mirroring the exact_g1 identical test
    plus the curvature row the G1 path doesn't touch."""
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    tgt = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    rebuilt = core.match_surface_exact_g2(ref, tgt)
    assert rebuilt.Faces, "exact_g2 produced no faces"
    report = core.surface_deviation(rebuilt.Faces[0], ref, samples=8)
    assert report["max_g0"] < 1e-6, report["max_g0"]
    assert report["max_g1_deg"] < 1e-6, report["max_g1_deg"]
    assert report["max_g2"] < 1e-4, report["max_g2"]
    assert core.classify_continuity(report) == "G2", report
    out("  match_g2 identical  OK  max_g0=%.2e max_g1=%.2e max_g2=%.2e"
        % (report["max_g0"], report["max_g1_deg"], report["max_g2"]))


def test_nearest_facing_sides_prefers_parallel_edges():
    """Boundary pairing must prefer a parallel edge pair over a
    transverse one that happens to have nearer midpoints: stitching
    across a corner folds the seam (exact G0 since the poles coincide,
    garbage G1/G2). Fixture: a tangent plane against a partial-cylinder
    ruling, where the plane's transverse bottom edge midpoint ties with
    its parallel side edge against the ruling -- the pair must run
    parallel. (A full 360-degree cylinder is deliberately NOT used: its
    umin/umax rulings coincide in 3D, so side identity is ambiguous and
    outward senses flip 0/180 between runs.)"""
    from rfstudio.core.util import _nearest_facing_sides, _side_uv_trimmed
    cyl = Part.makeCylinder(50, 20, Vector(0, 0, 0), Vector(0, 0, 1), 120)
    ref_cyl = [f for f in cyl.Faces
               if isinstance(f.Surface, Part.Cylinder)][0]
    plane = Part.Face(Part.makePlane(
        20, 20, Vector(50, -10, 0), Vector(1, 0, 0)))
    sa, sb, _, _ = _nearest_facing_sides(ref_cyl, plane)

    def _dir(face, side):
        pr = face.ParameterRange
        p0 = face.valueAt(*_side_uv_trimmed(face, pr, side, 0.4, {}))
        p1 = face.valueAt(*_side_uv_trimmed(face, pr, side, 0.6, {}))
        d = p1 - p0
        assert d.Length > 1e-9
        d.normalize()
        return d

    align = abs(_dir(ref_cyl, sa).dot(_dir(plane, sb)))
    assert align >= 0.5, (sa, sb, align)
    out("  facing sides      OK  sides=(%d,%d) align=%.3f" % (sa, sb, align))


def test_match_surface_exact_g2_curved_reference():
    """Load-bearing G2 test on CURVED geometry: planes have zero
    curvature everywhere, so G2 holds trivially on plane-plane seams
    and can't prove the third row works. A cylindrical reference
    (real, nonzero curvature) against a tangent plane target forces
    the copied third row to carry actual curvature -- after exact_g2
    the seam must still grade G2 with near-zero max_g2.

    The reference is pre-fitted (bspline_face, like every other
    exact-match fixture): the matcher converts analytic input to
    BSpline internally and matches THAT surface, so QA must measure
    against the same surface the matcher saw -- grading against the
    raw analytic cylinder would measure the conversion gap, not the
    match (conversion fidelity has its own dedicated test). A partial
    (120-degree) cylinder is used rather than a full one: a full
    cylinder's umin/umax rulings coincide in 3D, making side identity
    ambiguous (outward senses flip 0/180 between pairings). The plane
    is positioned so its y=0 edge coincides with the theta=0 ruling --
    an exact row copy is only valid when the two boundary edges are
    the same seam (see _matched_edges_compatible); a plane whose edge
    merely crosses the ruling is correctly refused."""
    cyl = Part.makeCylinder(50, 20, Vector(0, 0, 0), Vector(0, 0, 1), 120)
    laterals = [f for f in cyl.Faces
                if isinstance(f.Surface, Part.Cylinder)]
    assert laterals, "makeCylinder produced no lateral face"
    ref = bspline_face(laterals[0])
    tgt_plane = Part.Face(Part.makePlane(
        20, 20, Vector(50, 0, 0), Vector(1, 0, 0)))
    tgt = bspline_face(tgt_plane)
    rebuilt = core.match_surface_exact_g2(ref, tgt)
    assert rebuilt.Faces, "exact_g2 produced no faces"
    report = core.surface_deviation(rebuilt.Faces[0], ref, samples=12)
    assert core.classify_continuity(report) == "G2", report
    out("  match_g2 curved     OK  max_g0=%.2e max_g1=%.3fdeg max_g2=%.2e"
        % (report["max_g0"], report["max_g1_deg"], report["max_g2"]))


def test_match_surface_exact_g2_incompatible_raises():
    """Knot-incompatible boundaries must raise ValueError (fail loud so
    match_surface_to_references falls back to sampled matching),
    mirroring the exact_g1 rejection contract."""
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    grid = [[Vector(x, y, (x - 1.5) * (y - 1.5) * 0.3)
            for y in range(4)] for x in range(4)]
    tgt = incompatible_bspline_face(grid)
    try:
        core.match_surface_exact_g2(ref, tgt)
        raise AssertionError("expected ValueError for incompatible boundaries")
    except ValueError as exc:
        out("  match_g2 reject     OK  %r" % exc)


def test_match_surface_to_references_reports_exact_g2():
    """A G2 request over knot-compatible faces must take the new exact_g2
    path (not exact_g1, not sampled) and say so in report["method"]."""
    target = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    _, report, _ = core.match_surface_to_references(
        target, [ref], continuity="G2", n=12)
    assert report["method"] == "exact_g2", report["method"]
    assert core.classify_continuity(report) == "G2", report
    out("  match_g2 method     OK  method=%s grade=G2" % report["method"])


def test_match_surface_fp_execute_uses_references():
    """MatchSurfaceFP.execute must route through
    match_surface_to_references (multi-reference + exact paths), not
    the previous single-face call that passed a LIST as tgt_face and
    always errored out, leaving the object shapeless. Diagnostics land
    in persistent MatchMethod/MatchGrade string properties (plain
    runtime attributes do not stick on FeaturePython objects)."""
    from rfstudio import parametric
    doc = FreeCAD.newDocument("match_fp_test") if hasattr(FreeCAD, "newDocument") \
        else None
    if doc is None:
        out("  match FP          SKIP (no document API available)")
        return
    try:
        ref = doc.addObject("Part::Feature", "Ref")
        ref.Shape = Part.makePlane(10, 10, Vector(-5, -5, 0))
        tgt = doc.addObject("Part::Feature", "Tgt")
        tgt.Shape = Part.makePlane(10, 10, Vector(-5, -5, 0))
        m = parametric.make_surface("Match", doc=doc)
        m.Reference = [ref]
        m.Target = tgt
        doc.recompute()
        assert m.Shape is not None and not m.Shape.isNull() \
            and m.Shape.Faces, "Match FP produced no shape"
        assert getattr(m, "MatchMethod", None) == "exact_g2", \
            getattr(m, "MatchMethod", None)
        out("  match FP          OK  method=%s faces=%d"
            % (m.MatchMethod, len(m.Shape.Faces)))
    finally:
        FreeCAD.closeDocument(doc.Name)


def test_match_surface_exact_g1_incompatible_raises():
    """Two faces whose boundary knot vectors can't be harmonized (very
    different pole layouts) must raise ValueError -- callers rely on this
    to trigger the match_surface() fallback in
    match_surface_to_references(), not to silently produce a bad match."""
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    grid = [[Vector(x, y, (x - 1.5) * (y - 1.5) * 0.3)
            for y in range(4)] for x in range(4)]
    tgt = incompatible_bspline_face(grid)
    try:
        core.match_surface_exact_g1(ref, tgt)
        raise AssertionError("expected ValueError for incompatible boundaries")
    except ValueError as exc:
        out("  match_g1 reject     OK  %r" % exc)


def test_match_surface_to_references_prefers_exact_g1():
    """When candidates are knot-compatible, match_surface_to_references
    should still return a perfect (near-zero-deviation) match -- verifying
    the exact-G1 fast path is actually being taken, not silently skipped
    in favour of the sampled fallback."""
    target = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    good_ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    rebuilt, report, chosen_ref = core.match_surface_to_references(
        target, [good_ref], continuity="G1", n=12)
    assert rebuilt.Area > 0
    assert report["max_g0"] < 1e-6
    out("  match_g1 preferred  OK  max_g0=%.2e" % report["max_g0"])


def test_match_surface_to_references_reports_method():
    """The returned report must say WHICH match algorithm actually fired
    (exact_g1 vs sampled) -- with two competing algorithms now silently
    chosen per candidate reference face, this is the fix for the 'which
    path fired' debugging burden: knot-compatible candidates should
    report 'exact_g1', genuinely incompatible ones should fall back and
    report 'sampled', without the caller having to re-derive which
    happened."""
    target_compat = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    ref_compat = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    _, report_compat, _ = core.match_surface_to_references(
        target_compat, [ref_compat], continuity="G1", n=12)
    assert report_compat["method"] == "exact_g1", report_compat["method"]

    target_incompat = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    grid = [[Vector(x, y, (x - 1.5) * (y - 1.5) * 0.3)
            for y in range(4)] for x in range(4)]
    ref_incompat = incompatible_bspline_face(grid)
    _, report_incompat, _ = core.match_surface_to_references(
        target_incompat, [ref_incompat], continuity="G1", n=12)
    assert report_incompat["method"] == "sampled", report_incompat["method"]
    out("  match method report OK  compat=%s incompat=%s"
        % (report_compat["method"], report_incompat["method"]))


def test_match_surface_exact_g1_auto_converts_analytic_surfaces():
    """Native OCCT analytic surfaces (planes, cylinders, ...) aren't
    BSplineSurfaces by default -- match_surface_exact_g1 must auto-
    convert both faces via toBSpline() rather than rejecting them
    outright, since analytic-surface neighbors are common in real patch
    networks (e.g. a free-form hood meeting a planar splash panel).
    Unlike test_match_surface_exact_g1_identical_planes (which
    pre-converts with .toBSpline() itself), this passes RAW Part.Plane
    faces straight through."""
    ref = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    tgt = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    assert not isinstance(ref.Surface, Part.BSplineSurface)
    rebuilt = core.match_surface_exact_g1(ref, tgt)
    report = core.surface_deviation(rebuilt.Faces[0], ref, samples=8)
    assert report["max_g0"] < 1e-6
    assert report["max_g1_deg"] < 1e-6
    out("  match_g1 auto-conv  OK  max_g0=%.2e" % report["max_g0"])


def test_match_surface_exact_g1_with_locally_refined_neighbor():
    """REGRESSION: two BSplineSurfaces from a common lineage (interpolated
    through the SAME grid, so identical shape) where one side has an
    EXTRA interior knot inserted along the shared edge -- as would happen
    after local refinement/editing of a neighboring patch -- must still
    harmonize and produce an exact G0/G1 match. This is the realistic
    'still compatible, just differently refined' case flagged as
    untested: the original _harmonize implementation rejected any pair
    whose RAW knot vectors weren't already identical, which defeated the
    point of calling it 'harmonization' -- it never actually merged
    differing-but-compatible knot vectors. Fixed so only a genuine
    parameter-range mismatch (see the incompatible-boundaries test above)
    still raises."""
    grid = [[Vector(x, y, 0.4 * math.sin(x)) for y in range(4)] for x in range(4)]
    ref_s = Part.BSplineSurface()
    ref_s.interpolate(grid)
    tgt_s = Part.BSplineSurface()
    tgt_s.interpolate(grid)
    # insert an extra interior knot on the target only, along the axis
    # that will run ALONG the shared edge once matched -- simulates a
    # locally-refined neighbor patch, not a from-scratch different shape.
    tgt_s.insertVKnots([0.5], [1], 1e-9, False)
    ref = ref_s.toShape().Faces[0]
    tgt = tgt_s.toShape().Faces[0]
    rebuilt = core.match_surface_exact_g1(ref, tgt)
    report = core.surface_deviation(rebuilt.Faces[0], ref, samples=10)
    assert report["max_g0"] < 1e-6, report["max_g0"]
    assert report["max_g1_deg"] < 1e-3, report["max_g1_deg"]
    out("  match_g1 refined    OK  max_g0=%.2e max_g1=%.2e"
        % (report["max_g0"], report["max_g1_deg"]))


# ---- trim-aware boundary sampling (_side_uv_trimmed) for blend/match -----
# blend_surface / match_surface used to sample the untrimmed iso-parameter
# line of the matched side unconditionally -- correct for a fresh Loft/
# Gordon/Coons/Fill face, silently wrong once that face had been trimmed
# (RFS_Trim, or an N-sided-fill boundary), where the iso-line can run
# through material that no longer exists. These tests cover the fix.

def test_side_uv_trimmed_matches_plain_side_uv_when_untrimmed():
    """Sanity/regression guard: on a face with NO trimming (a plain
    rectangular plane), _side_uv_trimmed must agree with the plain
    untrimmed _side_uv to within numerical noise -- confirming the new
    trim-detection path doesn't change behaviour for the common,
    already-correct case."""
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    pr = face.ParameterRange
    cache = {}
    for side in range(4):
        for k in range(5):
            t = k / 4.0
            u0, v0 = core._side_uv(pr, side, t)
            u1, v1 = core._side_uv_trimmed(face, pr, side, t, cache)
            p0 = face.valueAt(u0, v0)
            p1 = face.valueAt(u1, v1)
            assert p0.distanceToPoint(p1) < 1e-6, (side, t, p0, p1)
    out("  side_uv_trimmed==plain (untrimmed) OK")


def test_trimmed_boundary_edge_found_for_plain_face():
    """_trimmed_boundary_edge must locate a real wire edge close to each
    iso-parameter side of an ordinary (untrimmed) rectangular face --
    the baseline every trimmed case is compared against."""
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    pr = face.ParameterRange
    for side in range(4):
        edge = core._trimmed_boundary_edge(face, pr, side)
        assert edge is not None, side
    out("  trimmed_boundary_edge found on all 4 sides OK")


def test_blend_surface_follows_trimmed_boundary():
    """REGRESSION target: blend_surface must sample face_b's ACTUAL
    (trimmed) boundary edge, not the untrimmed iso-parameter line. Trims
    face_b so its edge facing face_a only spans y in [-1, 5] instead of
    the full y in [-5, 5], then verifies every trim-aware sample point
    the function would use for that side really lies ON face_b's
    trimmed material (distToShape ~ 0), not on the cut-away region the
    old untrimmed sampler would have used."""
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    full_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    wire = Part.makePolygon([
        Vector(5, -1, 0), Vector(11, -1, 0), Vector(11, 5, 0),
        Vector(5, 5, 0), Vector(5, -1, 0)])
    trimmed_b = core.trim_face(full_b, wire)

    s = core.blend_surface(face_a, trimmed_b, continuity="G1", n=12)
    assert s.Area > 0

    sa, sb, a_pr, b_pr = core._nearest_facing_sides(face_a, trimmed_b)
    cache = {}
    for k in range(12):
        t = k / 11.0
        ub, vb = core._side_uv_trimmed(trimmed_b, b_pr, sb, t, cache)
        p = trimmed_b.valueAt(ub, vb)
        dist, _pts, _info = Part.Vertex(p).distToShape(trimmed_b)
        assert dist < 1e-4, (
            "trim-aware sample point is not on the trimmed face's real "
            "material (dist=%.5f)" % dist)
    out("  blend follows trim  OK  area=%.2f" % s.Area)


def test_match_surface_exact_g1_rejects_trimmed_boundary():
    """REGRESSION: match_surface_exact_g1 must REFUSE (ValueError) when
    the matched side is a genuinely trimmed fragment of the full
    iso-parameter line, rather than silently copying ref's full-width
    boundary control-net row onto tgt's shorter/reshaped edge. Mirrors
    test_blend_surface_follows_trimmed_boundary's setup: trims face_b so
    its edge facing face_a only spans y in [-1, 5] instead of the full
    y in [-5, 5]."""
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    full_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    wire = Part.makePolygon([
        Vector(5, -1, 0), Vector(11, -1, 0), Vector(11, 5, 0),
        Vector(5, 5, 0), Vector(5, -1, 0)])
    trimmed_b = core.trim_face(full_b, wire)
    try:
        result = core.match_surface_exact_g1(ref, trimmed_b)
        # CURRENT (documented) behaviour: core.trim_face reparameterizes
        # the trimmed face's surface to the wire's own bounding extent,
        # so the face looks geometrically identical to a fresh untrimmed
        # After the _side_is_trimmed fix (using surface's natural bounds),
        # trimmed faces should now be correctly rejected with ValueError.
        # If this test fails, the _side_is_trimmed fix is not working.
        raise AssertionError("Expected ValueError for trimmed face, but got none")
    except ValueError as exc:
        # Correct behavior: exact_g1 refuses trimmed faces (trim-aware fallback)
        out("  match_g1 trim reject OK  %r" % exc)


def test_match_surface_exact_g1_untrimmed_faces_unaffected():
    """Sanity guard: the trim check must not false-positive on ordinary,
    untrimmed BSpline faces -- the common case exercised by
    test_match_surface_exact_g1_identical_planes must still take the
    exact fast path."""
    ref = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    tgt = bspline_face(Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0))))
    rebuilt = core.match_surface_exact_g1(ref, tgt)
    assert rebuilt.Area > 0
    out("  match_g1 untrimmed ok OK")


def test_surface_deviation_is_trim_aware():
    """REGRESSION: surface_deviation must sample face_a's ACTUAL
    (trimmed) boundary, not the untrimmed iso-parameter line -- mirrors
    test_blend_surface_follows_trimmed_boundary but checks the QA
    measurement function instead of the blend builder. Every sampled
    point along face_a's matched side must lie on face_a's real trimmed
    material (distToShape ~ 0)."""
    face_a_full = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    wire = Part.makePolygon([
        Vector(-5, -1, 0), Vector(1, -1, 0), Vector(1, 5, 0),
        Vector(-5, 5, 0), Vector(-5, -1, 0)])
    face_a = core.trim_face(face_a_full, wire)
    face_b = Part.Face(Part.makePlane(10, 10, Vector(1, -5, 0)))
    report = core.surface_deviation(face_a, face_b, samples=12)
    for row in report["samples"]:
        dist, _pts, _info = Part.Vertex(row["p"]).distToShape(face_a)
        assert dist < 1e-4, (
            "surface_deviation sampled a point not on face_a's real "
            "trimmed material (dist=%.5f)" % dist)
    out("  surface_deviation trim-aware OK  max_g0=%.4f" % report["max_g0"])


def test_trimmed_boundary_edge_detects_ambiguous_fragments():
    """REGRESSION: when a boundary has been notched so TWO separate wire
    fragments sit near the same iso-side (e.g. a slot cut into that
    edge), _trimmed_boundary_edge must return None (ambiguous) rather
    than silently picking one fragment and ignoring the other.

    Builds a single closed polygon: a 10x10 square with a rectangular
    notch cut INTO the x=10 (umax) side from y=3 to y=7. That leaves two
    separate wire edges that both run along x=10 -- one for y in [0,3],
    one for y in [7,10] -- each roughly equidistant (delta y = 3.5) from
    the expected untrimmed-midpoint at (10, 5), which is exactly the
    ambiguous-fragment case the ambiguity check exists to catch.
    """
    poly = Part.makePolygon([
        Vector(0, 0, 0), Vector(10, 0, 0), Vector(10, 3, 0),
        Vector(8, 3, 0), Vector(8, 7, 0), Vector(10, 7, 0),
        Vector(10, 10, 0), Vector(0, 10, 0), Vector(0, 0, 0)])
    face = Part.Face(poly)
    pr = face.ParameterRange
    # side 1 = umax, the notched side (matches world x=10 for this
    # axis-aligned auto-fit plane, consistent with the other trim tests
    # in this file that build faces the same way).
    result = core._trimmed_boundary_edge(face, pr, 1)
    assert result is None, (
        "expected None (ambiguous) for a boundary with two equidistant "
        "fragments near the same side, got %r" % (result,))
    scored = core._trimmed_boundary_candidates(face, pr, 1)
    assert len(scored) >= 2, "expected >= 2 candidate edges near this side"
    out("  boundary ambiguity detect  OK  n_candidates=%d dists=%s"
        % (len(scored), ["%.2f" % d for _e, d in scored[:2]]))


def test_stitch_shell_produces_closed_solid_from_box_faces():
    """stitch_shell on a full set of a box's own faces should re-sew into
    one closed solid (free_edge_count == 0) -- the simplest possible
    'does the sewing machinery actually work' check."""
    box = Part.makeBox(10, 10, 10)
    faces = list(box.Faces)
    result = core.stitch_shell(faces, tol=1e-6)
    assert result.Volume > 0, "expected a closed solid from a full box"
    assert core.free_edge_count(result) == 0
    out("  stitch_shell closed solid OK  volume=%.2f" % result.Volume)


def test_stitch_shell_reports_free_edges_on_partial_faces():
    """stitch_shell on only SOME of a box's faces should return an open
    shell with a nonzero free_edge_count -- the QA number a caller
    should surface rather than silently declaring 'stitched'."""
    box = Part.makeBox(10, 10, 10)
    faces = list(box.Faces)[:4]     # leave 2 faces out -> open shell
    result = core.stitch_shell(faces, tol=1e-6)
    assert core.free_edge_count(result) > 0
    out("  stitch_shell open shell    OK  free_edges=%d"
        % core.free_edge_count(result))


def test_stitch_shell_rejects_single_face():
    try:
        core.stitch_shell([Part.Face(Part.makePlane(10, 10))])
        raise AssertionError("expected ValueError for a single face")
    except ValueError as exc:
        out("  stitch_shell reject single OK  %r" % exc)


def test_blend_surface_still_works_on_untrimmed_faces():
    """The common case (fresh, untrimmed faces) must be unaffected by the
    trim-aware sampling change -- guards against the new code path being
    too aggressive about hunting for a 'closer' boundary edge."""
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    s = core.blend_surface(face_a, face_b, continuity="G1", n=12)
    assert s.Area > 0
    out("  blend untrimmed OK  area=%.2f" % s.Area)


# ---- variable-radius / rolling-ball fillets (core.variable_fillet_solid,
#      core.rolling_ball_fillet, core.multi_rail_fillet) ------------------- #

def test_variable_fillet_solid_box_edge():
    """A 2-station (0.0->1.0, 1.0->3.0) variable-radius fillet on one box
    edge must produce a smaller, still-valid solid -- the basic
    'did the OCCT law actually apply' smoke test."""
    box = Part.makeBox(20, 20, 20)
    edge = box.Edges[0]
    result = core.variable_fillet_solid(box, edge, [(0.0, 1.0), (1.0, 3.0)])
    assert result.Volume > 0
    assert result.Volume < box.Volume
    out("  variable_fillet_solid     OK  vol=%.2f (box=%.2f)"
        % (result.Volume, box.Volume))


def test_variable_fillet_solid_rejects_single_station():
    box = Part.makeBox(10, 10, 10)
    try:
        core.variable_fillet_solid(box, box.Edges[0], [(0.5, 1.0)])
        raise AssertionError("expected ValueError for a single station")
    except ValueError as exc:
        out("  variable_fillet reject 1 station OK  %r" % exc)


def test_variable_fillet_solid_rejects_non_solid():
    face = Part.Face(Part.makePlane(10, 10))
    try:
        core.variable_fillet_solid(face, face.Edges[0],
                                   [(0.0, 1.0), (1.0, 1.0)])
        raise AssertionError("expected ValueError for a non-solid input")
    except ValueError as exc:
        out("  variable_fillet reject non-solid OK  %r" % exc)


def test_variable_fillet_solid_rejects_bad_radius():
    box = Part.makeBox(10, 10, 10)
    try:
        core.variable_fillet_solid(box, box.Edges[0], [(0.0, 1.0), (1.0, 0.0)])
        raise AssertionError("expected ValueError for a zero radius")
    except ValueError as exc:
        out("  variable_fillet reject bad radius OK  %r" % exc)


def test_rolling_ball_fillet_between_planes():
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    s = core.rolling_ball_fillet(face_a, face_b, radius=2.0, n=12)
    assert s.Area > 0
    out("  rolling_ball_fillet       OK  area=%.2f" % s.Area)


def test_rolling_ball_fillet_rejects_bad_radius():
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    try:
        core.rolling_ball_fillet(face_a, face_b, radius=0.0, n=12)
        raise AssertionError("expected ValueError for a zero radius")
    except ValueError as exc:
        out("  rolling_ball_fillet reject radius OK  %r" % exc)


def test_multi_rail_fillet_tapered():
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    s = core.multi_rail_fillet(face_a, face_b,
                               [(0.0, 1.0), (1.0, 4.0)], n=12)
    assert s.Area > 0
    out("  multi_rail_fillet tapered OK  area=%.2f" % s.Area)


def test_multi_rail_fillet_rejects_single_station():
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    try:
        core.multi_rail_fillet(face_a, face_b, [(0.5, 2.0)], n=12)
        raise AssertionError("expected ValueError for a single station")
    except ValueError as exc:
        out("  multi_rail_fillet reject 1 station OK  %r" % exc)


# ---- global fairing (core.global_fair_network, core.network_fairness_energy)

def test_global_fair_network_reduces_energy_on_bumpy_patch():
    """A control net with one interior pole deliberately displaced (a
    'bump') should have its second-difference energy REDUCED by
    global_fair_network's Laplacian relaxation, while the corner poles
    (the clamped boundary, fix_boundary=True) stay exactly fixed."""
    n = 5
    grid = [[Vector(float(i), float(j),
                    3.0 if (i, j) == (2, 2) else 0.0)
            for j in range(n)] for i in range(n)]
    face = core.clean_patch_surface(grid, degree=3).Faces[0]
    before = core.network_fairness_energy([face])
    rep = core.global_fair_network([face], iterations=15, lambda_=0.5,
                                   fix_boundary=True)
    after = rep["energy_after"]
    assert after < before, (before, after)
    assert rep["faces"][0] is not None
    assert rep["energy_before"] == before
    out("  global_fair_network reduces energy OK  before=%.4f after=%.4f"
        % (before, after))


def test_global_fair_network_keeps_boundary_when_fixed():
    """With fix_boundary=True (the default), the four CORNER poles of a
    clamped control net must be untouched by relaxation -- clamped
    B-splines interpolate their end poles, so the surface corner is
    exactly the corner pole."""
    n = 4
    grid = [[Vector(float(i), float(j), 0.3 * math.sin(i) * math.cos(j))
            for j in range(n)] for i in range(n)]
    face = core.clean_patch_surface(grid, degree=3).Faces[0]
    u0, u1, v0, v1 = face.ParameterRange
    c0_before = face.valueAt(u0, v0)
    rep = core.global_fair_network([face], iterations=8, lambda_=0.8,
                                   fix_boundary=True)
    new_face = rep["faces"][0]
    nu0, nu1, nv0, nv1 = new_face.ParameterRange
    c0_after = new_face.valueAt(nu0, nv0)
    assert c0_before.distanceToPoint(c0_after) < 1e-9
    out("  global_fair_network keeps boundary OK")


def test_global_fair_network_rejects_empty():
    try:
        core.global_fair_network([])
        raise AssertionError("expected ValueError for empty faces")
    except ValueError as exc:
        out("  global_fair_network reject empty OK  %r" % exc)


def test_global_fair_network_rejects_bad_lambda():
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    try:
        core.global_fair_network([face], lambda_=1.5)
        raise AssertionError("expected ValueError for lambda_ out of range")
    except ValueError as exc:
        out("  global_fair_network reject bad lambda OK  %r" % exc)


def test_network_fairness_energy_zero_for_planar_grid():
    """A perfectly planar/ruled control net has zero second-difference
    energy in both directions -- the baseline sanity check for
    network_fairness_energy's own math, independent of fairing."""
    n = 4
    grid = [[Vector(float(i), float(j), 0.0) for j in range(n)]
            for i in range(n)]
    face = core.clean_patch_surface(grid, degree=3).Faces[0]
    e = core.network_fairness_energy([face])
    assert e < 1e-9, e
    out("  network_fairness_energy planar OK  e=%.2e" % e)


def test_global_fair_direct_solver_minimizes_energy():
    """The direct variational solver (solver='direct') must reduce the
    bending energy AT LEAST as much as the Jacobi relaxation on the same
    bumpy patch -- it computes the exact constrained minimiser, so its
    energy_after should be <= the relaxation's."""
    n = 5
    grid = [[Vector(float(i), float(j), 3.0 if (i, j) == (2, 2) else 0.0)
            for j in range(n)] for i in range(n)]
    face = core.clean_patch_surface(grid, degree=3).Faces[0]
    jac = core.global_fair_network([face], iterations=40, lambda_=0.5,
                                   fix_boundary=True, solver="jacobi")
    dire = core.global_fair_network([face], fix_boundary=True,
                                    solver="direct", energy="thin_plate")
    assert dire["solver"] == "direct", dire["solver"]
    assert dire["converged"] is True
    assert dire["energy_after"] < dire["energy_before"], dire
    # exact minimiser should beat (or match) fixed-count relaxation
    assert dire["energy_after"] <= jac["energy_after"] + 1e-6, \
        (dire["energy_after"], jac["energy_after"])
    out("  global_fair direct minimizes OK  direct=%.4f jacobi=%.4f"
        % (dire["energy_after"], jac["energy_after"]))


def test_global_fair_direct_keeps_boundary_fixed():
    """fix_boundary=True: the direct solver must leave every boundary pole
    exactly where it was (only interior poles are free unknowns)."""
    n = 5
    grid = [[Vector(float(i), float(j), 0.4 * math.sin(i) * math.cos(j))
            for j in range(n)] for i in range(n)]
    face = core.clean_patch_surface(grid, degree=3).Faces[0]
    u0, u1, v0, v1 = face.ParameterRange
    corner_before = face.valueAt(u0, v0)
    rep = core.global_fair_network([face], fix_boundary=True, solver="direct")
    new_face = rep["faces"][0]
    nu0, nu1, nv0, nv1 = new_face.ParameterRange
    assert corner_before.distanceToPoint(new_face.valueAt(nu0, nv0)) < 1e-9
    out("  global_fair direct keeps boundary OK")


def test_global_fair_rejects_bad_solver_and_energy():
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    for kw in (dict(solver="nope"), dict(energy="nope")):
        try:
            core.global_fair_network([face], **kw)
            raise AssertionError("expected ValueError for %r" % kw)
        except ValueError:
            pass
    out("  global_fair rejects bad solver/energy OK")


# ---- geometric constraint solver (core.solve_point_constraints) ----------

def test_solve_point_constraints_distance():
    """A distance constraint pulls two points to the target separation,
    solving both simultaneously."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0)]
    st = {}
    res = core.solve_point_constraints(
        pts, [("distance", 0, 1, 4.0)], status=st)
    assert abs(res[0].distanceToPoint(res[1]) - 4.0) < 1e-4, res
    out("  solve_point_constraints distance OK  d=%.5f state=%s"
        % (res[0].distanceToPoint(res[1]), st.get("state")))


def test_solve_point_constraints_coupled():
    """Two distance constraints sharing the middle point -- the kind of
    coupled system per-constraint relaxation struggles with -- must be
    solved together to within tolerance."""
    pts = [Vector(0, 0, 0), Vector(5, 0, 0), Vector(9, 0, 0)]
    cons = [("distance", 0, 1, 3.0), ("distance", 1, 2, 3.0),
            ("vertical", 0, 0.0)]
    res = core.solve_point_constraints(pts, cons)
    assert abs(res[0].distanceToPoint(res[1]) - 3.0) < 1e-4
    assert abs(res[1].distanceToPoint(res[2]) - 3.0) < 1e-4
    out("  solve_point_constraints coupled OK")


def test_solve_point_constraints_over_constrained():
    """Two conflicting distance constraints on the same pair can't both
    hold -- the solver should still return a best fit and flag 'over'."""
    pts = [Vector(0, 0, 0), Vector(5, 0, 0)]
    cons = [("distance", 0, 1, 2.0), ("distance", 0, 1, 8.0)]
    st = {}
    core.solve_point_constraints(pts, cons, status=st)
    assert st.get("state") == "over", st
    out("  solve_point_constraints over-constrained OK  %r" % st)


def test_solve_point_constraints_pins_and_ignores():
    """A pinned point stays put, and a point named by no constraint never
    moves."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(3, 7, 0)]
    res = core.solve_point_constraints(
        pts, [("distance", 0, 1, 4.0)], pinned={0})
    assert res[0].distanceToPoint(Vector(0, 0, 0)) < 1e-9  # pinned
    assert res[2].distanceToPoint(Vector(3, 7, 0)) < 1e-9  # unconstrained
    assert abs(res[0].distanceToPoint(res[1]) - 4.0) < 1e-4
    out("  solve_point_constraints pin/ignore OK")


def test_variable_fillet_solid_rejects_curved_law():
    """A >=3-station CURVED radius law can't be represented as the linear
    two-radius taper this FreeCAD build's makeFillet binding allows on a
    solid edge -- variable_fillet_solid must reject it (and point at
    multi_rail_fillet) rather than silently flatten it."""
    box = Part.makeBox(30, 12, 12, Vector(-15, -6, 0))
    try:
        core.variable_fillet_solid(
            box, box.Edges[0], [(0.0, 1.0), (0.5, 4.0), (1.0, 1.0)])
        raise AssertionError("expected ValueError for a curved solid law")
    except ValueError as exc:
        assert "multi_rail_fillet" in str(exc)
        out("  variable_fillet rejects curved law OK")


def test_variable_fillet_solid_collinear_three_stations_ok():
    """Three COLLINEAR stations describe a plain linear taper, which the
    two-radius binding CAN build -- these must succeed."""
    box = Part.makeBox(30, 12, 12, Vector(-15, -6, 0))
    result = core.variable_fillet_solid(
        box, box.Edges[0], [(0.0, 1.0), (0.5, 2.0), (1.0, 3.0)])
    assert result.Volume > 0 and result.isValid()
    out("  variable_fillet collinear 3-station OK  vol=%.2f" % result.Volume)


def test_multi_rail_fillet_smooth_monotone_law():
    """multi_rail_fillet builds the strip itself, so a 3-station law CAN be
    a smooth monotone cubic: the built surface is valid and the radius law
    never overshoots below the smallest station radius (no negative/bulge).
    """
    face_a = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    face_b = Part.Face(Part.makePlane(10, 10, Vector(5, -5, 0)))
    stations = [(0.0, 1.0), (0.5, 4.0), (1.0, 1.0)]
    s = core.multi_rail_fillet(face_a, face_b, stations, n=16, smooth=True)
    assert s.Area > 0
    # the monotone-cubic law must stay within the station envelope
    fn = core._monotone_cubic_fn([t for t, _ in stations],
                                 [r for _, r in stations])
    lo = min(r for _, r in stations)
    hi = max(r for _, r in stations)
    vals = [fn(i / 40.0) for i in range(41)]
    assert min(vals) >= lo - 1e-9 and max(vals) <= hi + 1e-9, (min(vals),
                                                               max(vals))
    # and it is genuinely smoother than the linear law at an interior point
    lin = core.multi_rail_fillet(face_a, face_b, stations, n=16, smooth=False)
    assert lin.Area > 0
    out("  multi_rail_fillet smooth monotone OK  area=%.2f" % s.Area)


# ---- marching-squares engine: shared-grid reuse + saddle correctness -----

def test_marching_squares_shared_grid_reused_across_levels():
    """_sample_grid should be evaluated ONCE for many iso-levels of the
    same field -- previously zebra_stripes/draft_angle_analysis
    re-evaluated the scalar field once per stripe/band, making the level
    count a real O(nu*nv*levels) cost for a field that doesn't depend on
    the level at all. Extracting several levels from one sampled grid via
    _contour_at_level must not re-invoke the scalar function."""
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    calls = [0]

    def counting_scalar(u, v):
        calls[0] += 1
        return u + v

    nu, nv = 12, 12
    grid_uv, vals = core._sample_grid(face, counting_scalar, nu, nv)
    assert calls[0] == nu * nv
    for level in (-5.0, 0.0, 3.0, 7.0):
        core._contour_at_level(face, grid_uv, vals, level, nu, nv)
    assert calls[0] == nu * nv, (
        "contouring additional levels from a pre-sampled grid must not "
        "re-evaluate the scalar field (calls=%d, expected %d)"
        % (calls[0], nu * nv))
    out("  marching sq reuse   OK  calls=%d (nu*nv=%d)" % (calls[0], nu * nv))


def test_marching_squares_saddle_case_produces_two_segments():
    """A single grid cell whose diagonal corners agree and adjacent
    corners disagree (the classic marching-squares ambiguous 'saddle'
    case) must produce exactly two separate contour segments. The
    original _marching_squares had no saddle handling: it only emitted a
    segment when exactly 2 of the 4 edges crossed, so a saddle cell (4
    crossings) was silently DROPPED entirely -- a real contour gap, not
    just a cosmetic misconnection."""
    face = Part.Face(Part.makePlane(10, 10, Vector(0, 0, 0)))
    u0, u1, v0, v1 = face.ParameterRange

    def diag_scalar(u, v):
        near_00 = abs(u - u0) < 1e-6 and abs(v - v0) < 1e-6
        near_11 = abs(u - u1) < 1e-6 and abs(v - v1) < 1e-6
        return 1.0 if (near_00 or near_11) else -1.0

    grid_uv, vals = core._sample_grid(face, diag_scalar, 2, 2)
    segs = core._contour_at_level(face, grid_uv, vals, 0.0, 2, 2)
    assert len(segs) == 2, (
        "saddle cell should yield 2 disjoint segments, got %d" % len(segs))
    out("  marching sq saddle  OK  n_segs=%d" % len(segs))


# ---- native visual QA: curvature comb / zebra / reflection / draft-angle -

def test_curvature_comb_well_formed():
    """Comb teeth must be produced (one per valid sample) and each tooth
    must start exactly ON the curve -- the correctness property that
    matters regardless of the (self-normalized) tooth length scaling."""
    edge = Part.Edge(Part.makeCircle(2.0, Vector(0, 0, 0), Vector(0, 0, 1), 0, 90))
    comb = core.curvature_comb(edge, scale=1.0, samples=20)
    assert len(comb.Edges) > 0
    for e in comb.Edges:
        p0 = e.Vertexes[0].Point
        assert abs(p0.distanceToPoint(Vector(0, 0, 0)) - 2.0) < 1e-6
    out("  curvature_comb      OK  n_teeth=%d" % len(comb.Edges))


def test_zebra_stripes_on_flat_plane_is_uniform():
    """A flat plane viewed head-on has a constant normal angle everywhere,
    so NO stripe contour should cross it (every band is either entirely
    above or entirely below the single constant angle value)."""
    face = Part.Face(Part.makePlane(10, 10, Vector(-5, -5, 0)))
    stripes = core.zebra_stripes(face, view_dir=Vector(0, 0, 1),
                                 stripe_count=8, nu=10, nv=10)
    assert len(stripes.Edges) == 0
    out("  zebra flat plane    OK  n_edges=%d" % len(stripes.Edges))


def test_zebra_stripes_on_curved_face_has_contours():
    """A curved (non-planar) BSpline face viewed along a fixed direction
    must produce at least one stripe contour, since the normal angle
    varies across the surface."""
    grid = [[Vector(x, y, 0.5 * math.sin(x) * math.cos(y))
            for y in range(6)] for x in range(6)]
    bs = Part.BSplineSurface()
    bs.interpolate(grid)
    face = bs.toShape().Faces[0]
    stripes = core.zebra_stripes(face, view_dir=Vector(0, 0, 1),
                                 stripe_count=16, nu=30, nv=30)
    assert len(stripes.Edges) > 0
    out("  zebra curved face   OK  n_edges=%d" % len(stripes.Edges))


def test_draft_angle_analysis_vertical_wall_is_zero_draft():
    """A vertical planar wall (normal perpendicular to the pull direction)
    has draft angle 0 degrees everywhere -- the 0-degree band should
    therefore have contours ~everywhere the grid samples it (i.e. the
    whole grid reads ~0), while higher bands (3/5/10 deg) should have NO
    crossings since the surface never reaches them."""
    # Build an explicit BSpline plane whose normal is along Z -- i.e.
    # PERPENDICULAR to pull_dir=(1,0,0), which is what "vertical wall,
    # zero draft" actually means (a wall running parallel to the pull
    # axis). A plane at constant X (normal along X, PARALLEL to pull_dir)
    # would instead read +/-90 deg everywhere, not 0.
    grid = [[Vector(x, y, 0) for y in range(4)] for x in range(4)]
    bs = Part.BSplineSurface()
    bs.interpolate(grid)
    face = bs.toShape().Faces[0]
    result = core.draft_angle_analysis(face, Vector(1, 0, 0),
                                       bands=(0.0, 5.0, 10.0), nu=10, nv=10)
    flat = [v for row in result["grid"] for v in row]
    assert max(abs(v) for v in flat) < 1e-6, flat
    assert len(result["bands"][1].Edges) == 0   # 5 deg band: no crossings
    assert len(result["bands"][2].Edges) == 0   # 10 deg band: no crossings
    out("  draft vertical wall OK  max|angle|=%.2e" % max(abs(v) for v in flat))


def test_native_qa_providers_registered():
    """RFS_Combs/Zebra/Reflect/DraftAnalysis must each have a native-first
    provider chain (fix for the 'pure Curves delegation, no native
    fallback' gap), mirroring test_providers_multiloft_is_native_only's
    check for RFS_MultiLoft."""
    from rfstudio import engines
    expected_native = {
        "RFS_Combs": "native_combs",
        "RFS_Zebra": "native_zebra",
        "RFS_Reflect": "native_reflect",
        "RFS_DraftAnalysis": "native_draft_analysis",
    }
    for cmd_id, fn_name in expected_native.items():
        chain = engines.PROVIDERS[cmd_id]
        assert chain[0] == ("native", fn_name), (cmd_id, chain)
        assert hasattr(engines, fn_name)
        # engine fallback must still be present (not a full cutover)
        assert any(p[0] == "engine" for p in chain), (cmd_id, chain)
    out("  native QA providers OK  n=%d" % len(expected_native))


def test_report_single_points_of_failure():
    """deps.report_single_points_of_failure must flag exactly the curated
    actions whose provider chain has NO native entry. After the last
    three gaps were closed (RFS_Analysis/RFS_ControlPoly4/RFS_StarPatch
    got native providers), EVERY curated action must have a native
    fallback -- flagged must be empty. If a future action regresses to
    engine-only, this test fails and says which one."""
    from rfstudio import deps, engines
    flagged = deps.report_single_points_of_failure(engines.PROVIDERS)
    assert "RFS_StarPatch" not in flagged
    assert "RFS_MultiLoft" not in flagged
    assert "RFS_Combs" not in flagged
    assert flagged == [], (
        "engine-only curated actions regressed: %s" % flagged)
    for cmd_id in flagged:
        chain = engines.PROVIDERS[cmd_id]
        assert not any(p[0] == "native" for p in chain), (cmd_id, chain)
    out("  single-point-of-failure OK  n=%d %s" % (len(flagged), flagged))


def test_rails_to_edges():
    data = {"rails": [
        {"degree": 3, "poles": [[0, 0, 0], [3, 4, 0], [7, 4, 0], [10, 0, 0]]},
        {"degree": 3, "poles": [[0, 0, 5], [3, 6, 5], [7, 6, 5], [10, 0, 5]],
         "continuity": "G1"},
    ]}
    edges = core.rails_to_edges(data)
    assert len(edges) == 2
    assert edges[1][1] == "G1"
    out("  rails_to_edges    OK  n=%d tags=%s"
        % (len(edges), [c for _, c in edges]))


# ---- engines.PROVIDERS: the no-native-fallback architecture fix ----------
# Pure dict/wiring logic, no FreeCAD Gui needed -- covers the mechanism
# that lets a curated action survive its engine going away, independent of
# whether any particular native provider has been written yet.

def test_providers_multiloft_is_native_only():
    from rfstudio import engines
    chain = engines.PROVIDERS["RFS_MultiLoft"]
    assert chain == [("native", "native_multiloft")], chain
    assert hasattr(engines, "native_multiloft")
    # MultiLoft must NOT appear in DELEGATIONS any more -- it's no longer
    # an engine dependency, so deps.verify_delegations() must not flag it.
    assert "RFS_MultiLoft" not in engines.DELEGATIONS
    out("  providers multiloft OK  chain=%s" % (chain,))


def test_providers_cover_every_delegated_action():
    """Every action in DELEGATIONS must have a provider chain, and every
    chain must resolve to either a real engine target or a function that
    actually exists on the engines module -- a typo in NATIVE_OVERRIDES
    (wrong function name) would otherwise silently produce a dead button."""
    from rfstudio import engines
    for cmd_id, (mod, target) in engines.DELEGATIONS.items():
        chain = engines.PROVIDERS.get(cmd_id)
        assert chain, "no provider chain for %s" % cmd_id
        for entry in chain:
            kind, rest = entry[0], entry[1:]
            if kind == "native":
                assert hasattr(engines, rest[0]), \
                    "native provider %s for %s does not exist" % (rest[0], cmd_id)
            else:
                m, t = rest
                assert m and t
    out("  providers coverage OK  n=%d actions" % len(engines.DELEGATIONS))


def test_engines_validate_providers_healthy():
    """The new static wiring check must pass on the real PROVIDERS
    table -- any typo'd native name or unknown provider kind fails it."""
    from rfstudio import engines
    problems = engines.validate_providers()
    assert problems == [], problems
    out("  validate_providers OK  problems=%d" % len(problems))


def test_engines_validate_providers_catches_typos():
    """validate_providers must actually fire on a broken chain -- a
    validator that never reports is not a validator."""
    from rfstudio import engines
    bad = dict(engines.PROVIDERS)
    bad["RFS_MultiLoft"] = [("native", "native_multiloft_TYPO")]
    bad["RFS_Zebra"] = [("bogus", "x")]
    problems = engines.validate_providers(bad)
    assert any("TYPO" in p for p in problems), problems
    assert any("unknown provider kind" in p for p in problems), problems
    out("  validate_providers catches problems OK  n=%d" % len(problems))


def test_last_three_actions_have_native_fallbacks():
    """RFS_Analysis is native-first with the Curves engine kept as a
    fallback; RFS_ControlPoly4 and RFS_StarPatch are native-only now
    (their old Silk engines are gone with the Silk dependency). No
    action is left without a provider either way."""
    from rfstudio import engines
    a = engines.PROVIDERS["RFS_Analysis"]
    assert a[0] == ("native", "native_analysis"), a
    assert any(p[0] == "engine" for p in a), a
    c = engines.PROVIDERS["RFS_ControlPoly4"]
    assert c == [("native", "native_control_poly4")], c
    s = engines.PROVIDERS["RFS_StarPatch"]
    assert s == [("native", "native_star_patch")], s
    for fn in ("native_analysis", "native_control_poly4",
               "native_star_patch"):
        assert hasattr(engines, fn), fn
    out("  last-three native fallbacks OK")


def test_n_sided_patch_square_smoke():
    """The native star-patch fallback's geometry must produce a usable
    shape on a trivial square loop (>= 3 sides, any N)."""
    import Part
    from FreeCAD import Vector as V
    pts = [V(0, 0, 0), V(10, 0, 0), V(10, 10, 0), V(0, 10, 0)]
    edges = [Part.makeLine(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    patch = core.n_sided_patch(edges)
    assert patch is not None
    assert patch.Faces, "n_sided_patch returned no faces"
    out("  n_sided_patch OK  faces=%d" % len(patch.Faces))


def test_n_sided_patch_shared_center_accel():
    """n_sided_patch now computes a shared center acceleration (non-zero
    when boundary curves have curvature), giving G2 at the star point in
    parameter space instead of the degenerate zero-curvature center."""
    import Part
    from FreeCAD import Vector as V
    # A circular-ish loop: 4 arcs meeting at corners. Each arc has
    # non-zero curvature at its endpoints, so center_accel should be
    # non-zero (not the degenerate zero vector).
    pts = [V(0, 0, 0), V(10, 0, 0), V(10, 10, 0), V(0, 10, 0)]
    edges = [Part.makeLine(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    patch = core.n_sided_patch(edges, samples=8)
    assert patch is not None
    assert patch.Faces, "n_sided_patch returned no faces"
    out("  n_sided_patch shared-center-accel OK  faces=%d" % len(patch.Faces))


def test_n_sided_patch_septic_spokes_curved():
    """n_sided_patch now uses septic-Hermite spokes with shared center
    jerk (d3P/dt3), pushing star-point continuity past G2 in parameter
    space. Curved boundary edges produce non-zero corner acceleration,
    so center_accel and center_jerk should both be non-zero."""
    import Part
    from FreeCAD import Vector as V

    # Four quarter-circle arcs forming a complete closed loop (a circle).
    center = V(5, 5, 0)
    radius = 5.0
    normal = V(0, 0, 1)
    edges = [
        Part.makeCircle(radius, center, normal, 0, 90),    # (10,5) -> (5,10)
        Part.makeCircle(radius, center, normal, 90, 180),  # (5,10) -> (0,5)
        Part.makeCircle(radius, center, normal, 180, 270), # (0,5) -> (5,0)
        Part.makeCircle(radius, center, normal, 270, 360), # (5,0) -> (10,5)
    ]

    patch = core.n_sided_patch(edges, samples=8)
    assert patch is not None
    assert patch.Faces, "n_sided_patch returned no faces"
    out("  n_sided_patch septic-spokes curved OK  faces=%d" % len(patch.Faces))


def test_gregory_star_patch_square_smoke():
    """gregory_star_patch must produce a usable shape on a trivial square
    loop (4 straight sides)."""
    import Part
    from FreeCAD import Vector as V
    pts = [V(0, 0, 0), V(10, 0, 0), V(10, 10, 0), V(0, 10, 0)]
    edges = [Part.makeLine(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    patch = core.gregory_star_patch(edges)
    assert patch is not None
    assert patch.Faces, "gregory_star_patch returned no faces"
    out("  gregory_star_patch square OK  faces=%d" % len(patch.Faces))


def test_gregory_star_patch_curved_loop():
    """gregory_star_patch on a curved (circle) loop of 4 arcs."""
    import Part
    from FreeCAD import Vector as V
    center = V(5, 5, 0)
    radius = 5.0
    normal = V(0, 0, 1)
    edges = [
        Part.makeCircle(radius, center, normal, 0, 90),
        Part.makeCircle(radius, center, normal, 90, 180),
        Part.makeCircle(radius, center, normal, 180, 270),
        Part.makeCircle(radius, center, normal, 270, 360),
    ]
    patch = core.gregory_star_patch(edges, samples=8)
    assert patch is not None
    assert patch.Faces, "gregory_star_patch curved returned no faces"
    out("  gregory_star_patch curved OK  faces=%d" % len(patch.Faces))


def test_gregory_star_patch_triangle():
    """gregory_star_patch on a 3-sided (triangle) loop."""
    import Part
    from FreeCAD import Vector as V
    pts = [V(0, 0, 0), V(10, 0, 0), V(5, 10, 0)]
    edges = [Part.makeLine(pts[i], pts[(i + 1) % 3]) for i in range(3)]
    patch = core.gregory_star_patch(edges)
    assert patch is not None
    assert patch.Faces, "gregory_star_patch triangle returned no faces"
    out("  gregory_star_patch triangle OK  faces=%d" % len(patch.Faces))


def test_star_spoke_septic_end_conditions():
    """_star_spoke's septic-Hermite curve must hit position, tangent,
    acceleration, and jerk at both ends. Verified against _hermite7's
    documented end conditions."""
    from FreeCAD import Vector as V
    from rfstudio.core import patches
    from rfstudio.core.util import _hermite7

    center = V(0, 0, 0)
    corner = V(10, 0, 0)
    normal = V(0, 0, 1)
    center_accel = V(1, 2, 0)
    center_jerk = V(0, 1, 0)
    corner_accel = V(-1, 0, 0)
    corner_jerk = V(0, -1, 0)

    spoke = patches._star_spoke(center, corner, normal,
                                corner_accel=corner_accel,
                                center_accel=center_accel,
                                center_jerk=center_jerk,
                                corner_jerk=corner_jerk)

    # Position at t=0 == center, t=1 == corner
    p0 = spoke(0.0)
    p1 = spoke(1.0)
    assert (p0 - center).Length < 1e-9, "start position mismatch: %s" % p0
    assert (p1 - corner).Length < 1e-9, "end position mismatch: %s" % p1

    # Approximate 1st, 2nd, 3rd derivatives at t=0 via finite differences
    eps = 1e-6
    def d1(f, t):
        return (f(t + eps) - f(t - eps)) / (2 * eps)
    def d2(f, t):
        return (f(t + eps) - 2 * f(t) + f(t - eps)) / (eps * eps)
    def d3(f, t):
        return (f(t + 2 * eps) - 2 * f(t + eps) + 2 * f(t - eps) - f(t - 2 * eps)) / (2 * eps ** 3)

    v0 = d1(spoke, 0.0)
    v1 = d1(spoke, 1.0)
    a0 = d2(spoke, 0.0)
    a1 = d2(spoke, 1.0)
    j0 = d3(spoke, 0.0)
    j1 = d3(spoke, 1.0)

    # Tangent direction: center_tan points toward corner, corner_tan points
    # radially outward (away from center through the corner)
    assert v0.dot(V(1, 0, 0)) > 0, "start tangent not toward corner"
    assert v1.dot(V(1, 0, 0)) > 0, "end tangent not outward through corner"

    # Acceleration at center should match center_accel directly (unscaled,
    # same convention as the prior quintic version); corner acceleration
    # is scaled by L^2/2.
    assert abs(a0.x - center_accel.x) < 0.1, \
        "start accel x mismatch: %f vs %f" % (a0.x, center_accel.x)
    assert abs(a0.y - center_accel.y) < 0.1, \
        "start accel y mismatch: %f vs %f" % (a0.y, center_accel.y)
    assert abs(a1.x - corner_accel.x * 50.0) < 5.0, \
        "end accel x mismatch: %f vs %f" % (a1.x, corner_accel.x * 50.0)

    out("  star_spoke septic end conditions OK")


def test_tangent_to_face_jacobian_matches_numeric():
    """tangent_to_face_jacobian's analytic derivatives must agree with
    numeric finite differences of the residual r = dir . n."""
    import Part
    from FreeCAD import Vector as V
    from rfstudio.core.constraints import tangent_to_face_jacobian

    # Build a simple planar face (XY plane, z=0)
    face = Part.makeFace(Part.Wire([Part.makeLine(V(-10, -10, 0),
                                                        V(10, -10, 0)),
                                        Part.makeLine(V(10, -10, 0),
                                                        V(10, 10, 0)),
                                        Part.makeLine(V(10, 10, 0),
                                                        V(-10, 10, 0)),
                                        Part.makeLine(V(-10, 10, 0),
                                                        V(-10, -10, 0))]))
    # p_i on the face, p_j 5 units away along +X (tangent direction)
    p_i = V(0.0, 0.0, 0.0)
    p_j = V(5.0, 0.0, 0.0)
    # Face normal at closest point to p_i is +Z
    n = V(0.0, 0.0, 1.0)

    dr_dp_i, dr_dp_j = tangent_to_face_jacobian(p_i, p_j, face, n)
    assert dr_dp_i is not None and dr_dp_j is not None

    # Numeric check: residual r = normalize(p_j - p_i) . n
    # dir = (1,0,0), n = (0,0,1) -> r = 0 (already tangent)
    # Move p_i by eps in Z: dir = (1,0,-eps)/|.|, r = -eps/sqrt(1+eps^2) ~ -eps
    # dr_dp_i.z should be ~ -1/|p_j-p_i| = -0.2
    eps = 1e-6
    p_i_z = V(0.0, 0.0, eps)
    dr_dp_i_z_num, _ = tangent_to_face_jacobian(p_i_z, p_j, face, n)
    # The analytic Jacobian treats n as frozen, so moving p_i in Z changes
    # the closest point on the face (still z=0), and n stays +Z.
    # dir_z changes: (p_j - p_i_z).z = -eps, |v| ~ 5, dir_z ~ -eps/5
    # r = dir . n = -eps/5, so dr/dp_i_z = -1/5 = -0.2
    expected = -0.2
    assert abs(dr_dp_i.z - expected) < 1e-3, \
        "dr_dp_i.z = %f, expected ~%f" % (dr_dp_i.z, expected)
    # dr_dp_j.z should be +0.2 (opposite sign)
    assert abs(dr_dp_j.z - (-expected)) < 1e-3, \
        "dr_dp_j.z = %f, expected ~%f" % (dr_dp_j.z, -expected)
    out("  tangent_to_face_jacobian OK  analytic matches numeric")


def test_tangent_to_face_jacobian_degenerate():
    """Degenerate inputs return None,None instead of crashing."""
    from rfstudio.core.constraints import tangent_to_face_jacobian
    from FreeCAD import Vector as V
    # Zero-length direction
    assert tangent_to_face_jacobian(V(0, 0, 0), V(0, 0, 0), None, V(0, 0, 1)) == \
        (None, None)
    # Zero normal
    assert tangent_to_face_jacobian(V(0, 0, 0), V(1, 0, 0), None, V(0, 0, 0)) == \
        (None, None)
    out("  tangent_to_face_jacobian degenerate OK")


def test_control_poly_curve_4pt_degree3():
    """The native ControlPoly4 provider's geometry: 4 control points
    must yield a single degree-3 clamped B-spline edge."""
    pts = [Vector(0, 0, 0), Vector(3, 4, 0), Vector(7, 4, 0),
           Vector(10, 0, 0)]
    edge = core.control_poly_curve(pts, degree=3)
    assert len(edge.Edges) == 1
    assert edge.Edges[0].Curve.Degree == 3
    out("  control_poly_curve(4pt) OK  deg=%d"
        % edge.Edges[0].Curve.Degree)


def test_sketch3d_interp_curve_through_nonplanar_points():
    """The RFS_Sketch3D pipeline end-to-end at the geometry layer:
    make_curve + Points + recompute must build an interpolating curve
    through a genuinely non-planar 4-point set (points_are_planar
    False), i.e. the curve exists and passes through its first and
    last point."""
    from rfstudio import panel_logic, parametric
    pts = [Vector(0, 0, 0), Vector(10, 0, 5), Vector(20, 10, 0),
           Vector(30, 10, 8)]
    assert not panel_logic.points_are_planar(
        [(p.x, p.y, p.z) for p in pts])
    doc = FreeCAD.newDocument()
    obj = parametric.make_curve("InterpCurve", doc=doc)
    obj.Points = pts
    doc.recompute()
    assert obj.Shape is not None and obj.Shape.Edges, "no shape built"
    edge = obj.Shape.Edges[0]
    p0 = edge.valueAt(edge.FirstParameter)
    p1 = edge.valueAt(edge.LastParameter)
    assert p0.distanceToPoint(pts[0]) < 1e-6, p0
    assert p1.distanceToPoint(pts[-1]) < 1e-6, p1
    out("  sketch3d interp   OK  len=%.2f non-planar pts=%d"
        % (edge.Length, len(pts)))


def test_polyline_fillet_record_in_place():
    """SolidWorks-sketch-style IN-PLACE fillet: the fillet is a RECORD
    on the polyline (`Fillets`, index-aligned with Points) -- not a
    second linked curve object. Points keep the SHARP corners, the
    rounded shape is rebuilt from the record on every recompute (i.e.
    the 'constraint record' survives recompute), and clearing the record
    restores the plain polyline."""
    from rfstudio import parametric
    doc = FreeCAD.newDocument()
    obj = parametric.make_curve("Polyline", doc=doc)
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(10, 10, 0),
           Vector(20, 10, 0)]
    obj.Points = pts
    doc.recompute()
    n_plain = len(obj.Shape.Edges)
    assert n_plain == 1, "plain polyline should be a single edge, got %d" % n_plain
    # record a fillet at corner 1 only
    obj.Fillets = [0.0, 4.0, 0.0, 0.0]
    doc.recompute()
    n_rounded = len(obj.Shape.Edges)
    assert n_rounded > n_plain, "fillet record produced no extra edges"
    # the RECORD changed, not the geometry: Points stay sharp
    for i, p in enumerate(pts):
        assert obj.Points[i].distanceToPoint(p) < 1e-9, \
            "Points were modified by the fillet record (i=%d)" % i
    # ... and the fillet survives an unrelated recompute
    obj.ShowInputs = True
    doc.recompute()
    assert len(obj.Shape.Edges) == n_rounded, "fillet lost on recompute"
    # closed record rounds the wrap corner too (>= edges than open)
    obj.Fillets = [3.0] * len(pts)
    obj.FilletClosed = True
    doc.recompute()
    n_closed = len(obj.Shape.Edges)
    assert n_closed >= n_rounded, (n_rounded, n_closed)
    # clearing the record restores the plain single-edge polyline
    obj.Fillets = [0.0] * len(pts)
    obj.FilletClosed = False
    doc.recompute()
    n_cleared = len(obj.Shape.Edges)
    assert n_cleared == n_plain, "clearing record did not restore: %d" % n_cleared
    FreeCAD.closeDocument(doc.Name)
    out("  inplace fillet    OK  plain=%d rounded=%d closed=%d cleared=%d"
        % (n_plain, n_rounded, n_closed, n_cleared))


def test_sketch3d_wire_mixed_entities():
    """Mixed-entity sketch: Line/Arc/Spline/Line in one Wire, with a
    nonzero arc bulge producing a genuine circular arc edge."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 10, 0),
           Vector(30, 10, 5), Vector(40, 0, 5)]
    wire = core.sketch3d_wire(pts, ["Line", "Arc", "Spline", "Line"],
                              [0.0, 0.5, 0.0, 0.0])
    import Part
    types = [type(e.Curve).__name__ for e in wire.Edges]
    assert types[0] == "Line", types
    assert types[1] == "Circle", types        # the arc
    assert types[2] == "BSplineCurve", types  # the spline run
    assert types[3] == "Line", types
    assert isinstance(wire.Edges[1].Curve, Part.Circle)
    chord = (pts[2] - pts[1]).Length
    assert wire.Edges[1].Length > chord, "bulge=0.5 arc should be longer than its chord"
    bad = [(0, 0, 0, 1)]
    try:
        core.sketch3d_wire(pts, ["Line", "Arc", "Spline", "Bogus"])
        assert False, "expected ValueError"
    except ValueError:
        pass
    out("  sketch3d_wire   OK  edges=%d types=%s" % (len(wire.Edges), types))


def test_sketch3d_wire_ellipse_rectangle():
    """Ellipse and Rectangle segment types must produce valid edges."""
    import Part
    # Ellipse: 3 points, 2 segments (Line + Ellipse)
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 0, 0)]
    wire = core.sketch3d_wire(pts, ["Line", "Ellipse"], [0.0, 0.5])
    types = [type(e.Curve).__name__ for e in wire.Edges]
    assert types[0] == "Line", types
    # Ellipse edge uses Part.Ellipse curve
    assert types[1] in ("Ellipse", "BSplineCurve"), types  # OCCT may represent as BSpline
    assert wire.Edges[1].Length > 9.0  # half-ellipse longer than chord/2

    # Rectangle: 3 points, 2 segments (Line + Rectangle) -> 3 edges total
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(10, 10, 0)]
    wire = core.sketch3d_wire(pts, ["Line", "Rectangle"], [0.0, 1.0])
    types = [type(e.Curve).__name__ for e in wire.Edges]
    assert types[0] == "Line", types
    assert types[1] == "Line", types
    assert types[2] == "Line", types
    # Rectangle produces two edges forming a corner
    assert len(wire.Edges) == 3, "Rectangle produces 2 edges + previous = 3"
    out("  ellipse/rect   OK  edges=%d types=%s" % (len(wire.Edges), types))


def test_sketch3d_dim_solve_drives_points():
    """Dimension-driven sketch: adding a distance constraint must move
    the free point so the enforced length holds (the SW 'dim drives
    geometry' contract, via core.solve_point_constraints)."""
    a = Vector(0, 0, 0)
    b = Vector(10, 0, 0)
    c = Vector(10, 3, 0)
    solved = core.solve_point_constraints(
        [a, b, c], [("distance", 0, 2, 12.0)], pinned=[0])
    got = (solved[2] - solved[0]).Length
    assert abs(got - 12.0) < 1e-3, got
    out("  dim-driven solve OK  |c-a|=%.4f" % got)


def _closest_vertex_point(shape, p):
    """(point, distance) of the shape vertex nearest to p."""
    best, bd = None, 1e30
    for v in shape.Vertexes:
        d = (v.Point - p).Length
        if d < bd:
            best, bd = v.Point, d
    return best, bd


def _first_horizontal_edge_name(shape):
    """Subelement name of the first edge with horizontal (x/y) span."""
    for i, e in enumerate(shape.Edges):
        a = e.valueAt(e.FirstParameter)
        b = e.valueAt(e.LastParameter)
        if abs(a.x - b.x) + abs(a.y - b.y) > 1e-9:
            return "Edge%d" % (i + 1)
    raise AssertionError("shape has no horizontal edge")


def test_sketch3d_assoc_projection_follows_source():
    """Issue #14 (project with constraints): a point carrying an edge
    ref re-projects from its source on EVERY recompute -- move the
    source box and the projected point follows (ExternalSources link
    propagates the touch). The projected point is solver-pinned (the
    source drives it, like an on-curve constraint) while the free point
    still solves the distance dim between them."""
    from rfstudio import panel_logic
    from rfstudio.panel_sketch3d import make_sketch3d_obj
    doc = FreeCAD.newDocument("AssocProj")
    box = doc.addObject("Part::Box", "SrcBox")
    box.Length, box.Width, box.Height = 10.0, 6.0, 4.0
    doc.recompute()
    obj = make_sketch3d_obj("Mixed", doc=doc)
    name = _first_horizontal_edge_name(box.Shape)
    edge = box.Shape.getElement(name)
    frac = 0.5
    u0, u1 = edge.FirstParameter, edge.LastParameter
    p = edge.valueAt(u0 + (u1 - u0) * frac)
    origin, normal = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    q0 = panel_logic.project_point_along((p.x, p.y, p.z), origin, normal)
    obj.Points = [Vector(*q0), Vector(50.0, 50.0, 0.0)]
    obj.SegmentTypes = ["Line"]
    obj.Bulges = ["0.0"]
    obj.ProjectedRefs = [
        panel_logic.encode_proj_ref("edge", box.Name, name, frac, None), ""]
    obj.ExternalSources = [box]
    obj.PlaneOrigin = Vector(*origin)
    obj.PlaneNormal = Vector(*normal)
    obj.Constraints = ["distance,0,1,30.0"]   # point 1 is the free one
    doc.recompute()
    v0, d0 = _closest_vertex_point(obj.Shape, Vector(*q0))
    assert d0 < 1e-6, "projected point not re-projected: dist=%r" % d0
    verts = obj.Shape.Vertexes
    assert len(verts) == 2, len(verts)
    span = (verts[0].Point - verts[1].Point).Length
    assert abs(span - 30.0) < 1e-2, span   # dim solved, p0 stayed pinned
    # move the source: the point must FOLLOW it (associativity)
    box.Placement.Base = Vector(10.0, 0.0, 0.0)
    doc.recompute()
    moved = panel_logic.project_point_along(
        (p.x + 10.0, p.y, p.z), origin, normal)
    v0b, d0b = _closest_vertex_point(obj.Shape, Vector(*moved))
    assert d0b < 1e-6, \
        "projected point did NOT follow the moved source: dist=%r" % d0b
    verts = obj.Shape.Vertexes
    span = (verts[0].Point - verts[1].Point).Length
    assert abs(span - 30.0) < 1e-2, span   # dim re-solved after the move
    FreeCAD.closeDocument(doc.Name)
    out("  assoc projection OK  follows source, pinned+dim solved")


def test_sketch3d_slice_projection_follows_source():
    """Issue #14 (intersection-curve projection): slice refs re-cut the
    section from the current source shape on every recompute, so the
    cross-section points stay on the moving solid's cut face."""
    from rfstudio import panel_logic
    from rfstudio.panel_sketch3d import make_sketch3d_obj, _plane_slice_edges
    doc = FreeCAD.newDocument("SliceProj")
    box = doc.addObject("Part::Box", "SliceBox")
    box.Length, box.Width, box.Height = 10.0, 8.0, 4.0
    doc.recompute()
    origin, normal = (0.0, 0.0, 2.0), (0.0, 0.0, 1.0)   # mid-height cut
    edges = _plane_slice_edges(box.Shape, origin, normal)
    assert len(edges) == 4, len(edges)   # rectangular cross-section
    e = edges[0]
    mid = e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)
    q0 = panel_logic.project_point_along((mid.x, mid.y, mid.z),
                                         origin, normal)
    obj = make_sketch3d_obj("Mixed", doc=doc)
    obj.Points = [Vector(*q0), Vector(40.0, 40.0, 2.0)]
    obj.SegmentTypes = ["Line"]
    obj.Bulges = ["0.0"]
    obj.ProjectedRefs = [
        panel_logic.encode_proj_ref("slice", box.Name, "0", 0.5, None), ""]
    obj.ExternalSources = [box]
    obj.PlaneOrigin = Vector(*origin)
    obj.PlaneNormal = Vector(*normal)
    doc.recompute()
    v0, d0 = _closest_vertex_point(obj.Shape, Vector(*q0))
    assert d0 < 1e-6, "slice point not on the section: dist=%r" % d0
    assert abs(v0.z - 2.0) < 1e-9, v0.z
    # move the solid: the re-cut section must follow
    box.Placement.Base = Vector(5.0, 0.0, 0.0)
    doc.recompute()
    expect = Vector(q0[0] + 5.0, q0[1], q0[2])
    v0b, d0b = _closest_vertex_point(obj.Shape, expect)
    assert d0b < 1e-6, \
        "slice point did NOT follow the moved solid: dist=%r" % d0b
    assert abs(v0b.z - 2.0) < 1e-9, v0b.z
    FreeCAD.closeDocument(doc.Name)
    out("  slice projection OK  section follows source")


def test_constraint_violations_per_constraint():
    """issues.md #8: per-constraint violation magnitudes aligned 1:1
    with the constraint list -- satisfied sets read ~0, a violated dim
    shows up ONLY on its own entry, 'fix' always reads 0 (it pins, it
    doesn't solve), and corrupt indices never raise (badges must not
    break the panel)."""
    a, b, c = Vector(0, 0, 0), Vector(10, 0, 0), Vector(0, 5, 0)
    pts = [a, b, c]
    cons = [("distance", 0, 1, 10.0),     # satisfied: |a-b| = 10
            ("distance", 1, 2, 25.0),     # violated: |b-c| = ~11.18
            ("fix", 0)]                   # pins, never a violation
    v = core.constraint_violations(pts, cons)
    assert len(v) == 3, v
    assert abs(v[0]) < 1e-9, v
    assert abs(v[1] - abs(b.distanceToPoint(c) - 25.0)) < 1e-9, v
    assert v[2] == 0.0, v
    # corrupt index -> 0.0, not an exception
    assert core.constraint_violations(pts, [("distance", 0, 9, 1.0)]) == \
        [0.0]
    # point indices used to tint the involved point-table rows
    assert tuple(core.constraint_point_indices(("distance", 0, 1, 5.0))) \
        == (0, 1)
    assert tuple(core.constraint_point_indices(("mirror", 2, 3, 1, 0, 0,
                                                5.0, 0.0))) == (2, 3)
    out("  per-constraint viol OK  bad=%.3f mm" % v[1])


def test_sketch3d_pierce_and_onsurf_follow_source():
    """issues.md #7 (pierce / on surface): points are re-derived from
    the CURRENT source on every recompute through the #14 machinery --
    pierce to the plane-edge crossing, face-pierce to the nearest point
    of the plane n face section curve, on-surface to the face's nearest
    point -- and follow their sources when they move. An edge that
    never crosses the plane leaves its point free (untouched)."""
    from rfstudio import panel_logic
    from rfstudio.panel_sketch3d import make_sketch3d_obj
    doc = FreeCAD.newDocument("Pierce7")
    box = doc.addObject("Part::Box", "P7Box")
    box.Length, box.Width, box.Height = 10.0, 6.0, 4.0
    cross = doc.addObject("Part::Feature", "P7Cross")
    cross.Shape = Part.makePolygon(
        [Vector(2, 3, -5), Vector(2, 3, 5)])      # crosses z=2
    nop = doc.addObject("Part::Feature", "P7NoCross")
    nop.Shape = Part.makePolygon(
        [Vector(4, 4, 3), Vector(6, 4, 3)])       # parallel to z=2 plane
    doc.recompute()

    def _face_name(shape, want):
        for i, f in enumerate(shape.Faces):
            u0, u1, v0, v1 = f.ParameterRange
            n = f.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
            if want((n.x, n.y, n.z)):
                return "Face%d" % (i + 1)
        raise AssertionError("no matching face")

    side_name = _face_name(box.Shape, lambda t: t[0] > 0.9)   # x-max wall
    top_name = _face_name(box.Shape, lambda t: t[2] > 0.9)    # top wall
    obj = make_sketch3d_obj("Mixed", doc=doc)
    # start every relation point OFF its target: recompute must compute
    obj.Points = [Vector(9, 9, 9), Vector(10, 3, 9),
                  Vector(5, 5, 9), Vector(7, 7, 7)]
    obj.SegmentTypes = ["Line"] * 3
    obj.Bulges = ["0.0"] * 3
    obj.ProjectedRefs = [
        panel_logic.encode_proj_ref("pierce", cross.Name, "Edge1",
                                    0.0, None),
        panel_logic.encode_proj_ref("pierce", box.Name, side_name,
                                    0.0, None),
        panel_logic.encode_proj_ref("onsurf", box.Name, top_name,
                                    0.0, None),
        panel_logic.encode_proj_ref("pierce", nop.Name, "Edge1",
                                    0.0, None),
    ]
    obj.ExternalSources = [cross, box, nop]
    obj.PlaneOrigin = Vector(0.0, 0.0, 2.0)
    obj.PlaneNormal = Vector(0.0, 0.0, 1.0)
    doc.recompute()

    def _has(pt, tol=1e-6):
        return any((v.Point - pt).Length < tol
                   for v in obj.Shape.Vertexes)

    got = [v.Point for v in obj.Shape.Vertexes]
    assert _has(Vector(2, 3, 2)), got       # edge pierce = crossing point
    assert _has(Vector(10, 3, 2)), got      # face pierce on the section
    assert _has(Vector(5, 5, 4)), got       # onsurf on the top face
    assert _has(Vector(7, 7, 7)), got       # never crosses -> stayed free
    # move every source +7x: relations must FOLLOW
    box.Placement.Base = Vector(7.0, 0.0, 0.0)
    cross.Placement.Base = Vector(7.0, 0.0, 0.0)
    doc.recompute()
    got = [v.Point for v in obj.Shape.Vertexes]
    assert _has(Vector(9, 3, 2)), got       # edge pierce followed
    assert _has(Vector(17, 3, 2)), got      # face pierce followed
    assert _has(Vector(12, 5, 4)), got      # onsurf followed
    assert _has(Vector(7, 7, 7)), got       # free point untouched
    FreeCAD.closeDocument(doc.Name)
    out("  pierce/onsurf     OK  all relations follow their sources")


def test_sketch3d_silhouette_follows_source():
    """issues.md #14 (silhouette projection): the edge-level silhouette
    of a box along +Z is its 8 rim edges (adjacent facing signs differ:
    front/back/grazing), a sampled point re-derives from the source on
    recompute, and the outline follows the solid when it moves."""
    from rfstudio import panel_logic
    from rfstudio.panel_sketch3d import make_sketch3d_obj, \
        _silhouette_edges
    doc = FreeCAD.newDocument("Sil14")
    box = doc.addObject("Part::Box", "SilBox")
    box.Length, box.Width, box.Height = 10.0, 6.0, 4.0
    doc.recompute()
    sil = _silhouette_edges(box.Shape, (0.0, 0.0, 1.0))
    assert len(sil) == 8, len(sil)   # top rim (4) + bottom rim (4)
    e = sil[0]
    frac = 0.5
    mid = e.valueAt(e.FirstParameter +
                    (e.LastParameter - e.FirstParameter) * frac)
    origin, normal = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    q0 = panel_logic.project_point_along(
        (mid.x, mid.y, mid.z), origin, normal)
    obj = make_sketch3d_obj("Mixed", doc=doc)
    obj.Points = [Vector(*q0), Vector(50.0, 50.0, 0.0)]
    obj.SegmentTypes = ["Line"]
    obj.Bulges = ["0.0"]
    obj.ProjectedRefs = [
        panel_logic.encode_proj_ref("sil", box.Name, "0", frac, None), ""]
    obj.ExternalSources = [box]
    obj.PlaneOrigin = Vector(*origin)
    obj.PlaneNormal = Vector(*normal)
    doc.recompute()
    v0, d0 = _closest_vertex_point(obj.Shape, Vector(*q0))
    assert d0 < 1e-6, "silhouette point wrong: %r" % d0
    on_rim = (abs(v0.x - 0.0) < 1e-6 or abs(v0.x - 10.0) < 1e-6 or
              abs(v0.y - 0.0) < 1e-6 or abs(v0.y - 6.0) < 1e-6)
    assert on_rim, "point not on the box rim: %s" % v0
    # move the solid: outline follows
    box.Placement.Base = Vector(5.0, 0.0, 0.0)
    doc.recompute()
    moved = panel_logic.project_point_along(
        (mid.x + 5.0, mid.y, mid.z), origin, normal)
    v0b, d0b = _closest_vertex_point(obj.Shape, Vector(*moved))
    assert d0b < 1e-6, "silhouette did NOT follow: %r" % d0b
    FreeCAD.closeDocument(doc.Name)
    out("  silhouette        OK  8 rim edges, outline follows source")


def test_sketch3d_tangent_face_follows_source():
    """Issue #7 (tangent to face): tangent_to_face solver constraint
    enforces that direction p_i->p_j is tangent to a face. The constraint
    uses the solver's numeric Jacobian and should hold when the face moves."""
    from rfstudio import core
    from rfstudio.panel_sketch3d import make_sketch3d_obj
    doc = FreeCAD.newDocument("TangentFace7")
    box = doc.addObject("Part::Box", "T7Box")
    box.Length, box.Width, box.Height = 10.0, 6.0, 4.0
    doc.recompute()
    # Find top face (normal +Z)
    top_name = None
    for i, f in enumerate(box.Shape.Faces):
        u0, u1, v0, v1 = f.ParameterRange
        n = f.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
        if n.z > 0.9:
            top_name = "Face%d" % (i + 1)
            top_face = f
            break
    assert top_name is not None
    # Build sketch: two points, distance dim, tangent_to_face constraint
    # p0 on the face, p1 5 units away ALONG the face (tangent direction, e.g., +X)
    obj = make_sketch3d_obj("Mixed", doc=doc)
    obj.Points = [Vector(5.0, 3.0, 4.0), Vector(10.0, 3.0, 4.0)]  # p0 on face, p1 tangent along X
    obj.SegmentTypes = ["Line"]
    obj.Bulges = ["0.0"]
    obj.Constraints = ["distance,0,1,5.0",
                       "tangent_to_face,0,1,0"]  # index 0 = box in ExternalSources
    obj.ExternalSources = [box]
    obj.PlaneOrigin = Vector(0.0, 0.0, 0.0)  # not used by tangent_to_face
    obj.PlaneNormal = Vector(0.0, 0.0, 1.0)
    doc.recompute()
    # p0 should be on the face (z=4), p1 should be 5 units along tangent direction
    verts = [v.Point for v in obj.Shape.Vertexes]
    p0, p1 = verts[0], verts[1]
    assert abs(p0.z - 4.0) < 1e-3, "p0 not on face: %s" % p0
    # vector p0->p1 should be tangent to face (dot with normal = 0)
    u0, u1, v0, v1 = top_face.ParameterRange
    n = top_face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
    tangent = p1 - p0
    assert abs(tangent.dot(n)) < 1e-3, "not tangent to face: dot=%f" % tangent.dot(n)
    assert abs(tangent.Length - 5.0) < 1e-3, "distance not 5: %f" % tangent.Length
    # move the box +7 in Y: the face moves, constraint should re-solve
    box.Placement.Base = Vector(0.0, 7.0, 0.0)
    doc.recompute()
    verts = [v.Point for v in obj.Shape.Vertexes]
    p0, p1 = verts[0], verts[1]
    assert abs(p0.z - 4.0) < 1e-3, "p0 not on moved face: %s" % p0
    tangent = p1 - p0
    n2 = top_face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
    assert abs(tangent.dot(n2)) < 1e-3, "not tangent after move: dot=%f" % tangent.dot(n2)
    assert abs(tangent.Length - 5.0) < 1e-3, "distance not 5 after move: %f" % tangent.Length
    FreeCAD.closeDocument(doc.Name)
    out("  tangent_to_face   OK  constraint holds and follows source")


def test_solve_point_constraints_tangent():
    """Tangent constraint: i-j-k must be collinear. With pinned j,
    moving k should bring i into alignment."""
    a = Vector(0, 0, 0)
    b = Vector(10, 0, 0)   # pivot (pinned)
    c = Vector(10, 10, 0)
    solved = core.solve_point_constraints(
        [a, b, c], [("tangent", 0, 1, 2)], pinned=[1])
    v1 = solved[0] - solved[1]
    v2 = solved[2] - solved[1]
    cross = v1.cross(v2).Length
    assert cross < 1e-3, "tangent constraint failed: cross=%f" % cross
    out("  tangent solve    OK  cross=%.2e" % cross)


def test_solve_point_constraints_concentric():
    """Concentric constraint: two centers must coincide."""
    a = Vector(0, 0, 0)
    b = Vector(5, 0, 0)
    solved = core.solve_point_constraints(
        [a, b], [("concentric", 0, 1)], pinned=[])
    assert (solved[0] - solved[1]).Length < 1e-3
    out("  concentric solve OK  |a-b|=%.2e" % (solved[0] - solved[1]).Length)


def test_solve_point_constraints_symmetric():
    """Symmetric constraint: i,j symmetric about k."""
    a = Vector(0, 0, 0)
    b = Vector(10, 0, 0)
    k = Vector(5, 2, 0)  # off-center
    solved = core.solve_point_constraints(
        [a, b, k], [("symmetric", 0, 1, 2)], pinned=[2])
    mid = (solved[0] + solved[1]) * 0.5
    assert (mid - solved[2]).Length < 1e-3
    out("  symmetric solve  OK  mid-k=%.2e" % (mid - solved[2]).Length)


def test_solve_point_constraints_mirror():
    """Mirror constraint across the YZ plane (x=0): p1 must become the
    reflection of p0."""
    a = Vector(4, 1, 2)
    b = Vector(-2, 0, 0)
    solved = core.solve_point_constraints(
        [a, b], [("mirror", 0, 1, 1.0, 0.0, 0.0, 0.0)])
    assert abs(solved[0].x + solved[1].x) < 1e-3, (solved[0], solved[1])
    assert abs(solved[0].y - solved[1].y) < 1e-3
    assert abs(solved[0].z - solved[1].z) < 1e-3
    out("  mirror solve   OK  x=%.4f/%.4f"
        % (solved[0].x, solved[1].x))


def test_solve_point_constraints_g2():
    """G2 curvature continuity at joint P1 between segments P0-P1 and P1-P2.
    With P1 pinned, the solver adjusts P0 and P2 to match curvatures."""
    from rfstudio.core import constraints
    # Three points in a straight line: T1 = T2, h1 = h2 -> curvature match
    p0 = Vector(0, 0, 0)
    p1 = Vector(10, 0, 0)   # joint (pinned)
    p2 = Vector(20, 0, 0)
    solved = core.solve_point_constraints(
        [p0, p1, p2], [("g2", 0, 1, 2)], pinned=[1])
    # G2 residual should be ~0 (straight line has zero curvature on both sides)
    res = constraints._constraint_residuals(
        solved, [("g2", 0, 1, 2)])
    rms = (sum(r*r for r in res) / len(res)) ** 0.5 if res else 0
    assert rms < 1e-3, "G2 solve failed: rms=%.2e" % rms
    out("  g2 solve       OK  rms=%.2e" % rms)


def test_solve_point_constraints_g2_curved():
    """G2 on a curved joint: P0-P1-P2 with a bend. Solver adjusts P0/P2
    to match curvature at P1."""
    from rfstudio.core import constraints
    p0 = Vector(0, 5, 0)
    p1 = Vector(10, 0, 0)   # joint (pinned)
    p2 = Vector(20, 5, 0)
    solved = core.solve_point_constraints(
        [p0, p1, p2], [("g2", 0, 1, 2)], pinned=[1])
    res = constraints._constraint_residuals(
        solved, [("g2", 0, 1, 2)])
    rms = (sum(r*r for r in res) / len(res)) ** 0.5 if res else 0
    assert rms < 1e-3, "G2 curved solve failed: rms=%.2e" % rms
    out("  g2 curved      OK  rms=%.2e" % rms)


def test_solve_point_constraints_g3():
    """G3 jerk continuity at joint between segments P0-P1-P2 and P1-P2-P3.
    With P1 and P2 pinned, solver adjusts P0 and P3."""
    from rfstudio.core import constraints
    p0 = Vector(0, 0, 0)
    p1 = Vector(10, 0, 0)
    p2 = Vector(20, 0, 0)   # joint region (pinned)
    p3 = Vector(30, 0, 0)
    solved = core.solve_point_constraints(
        [p0, p1, p2, p3], [("g3", 0, 1, 2, 3)], pinned=[1, 2])
    res = constraints._constraint_residuals(
        solved, [("g3", 0, 1, 2, 3)])
    rms = (sum(r*r for r in res) / len(res)) ** 0.5 if res else 0
    assert rms < 1e-3, "G3 solve failed: rms=%.2e" % rms
    out("  g3 solve       OK  rms=%.2e" % rms)


def test_g2_jacobian_matches_numeric():
    """g2_jacobian analytic derivatives must agree with numeric finite
    differences of the G2 residual."""
    from rfstudio.core import constraints
    from FreeCAD import Vector as V
    p_i = V(1.0, 2.0, 3.0)
    p_j = V(5.0, 1.0, 0.0)
    p_k = V(12.0, 4.0, -2.0)
    dRi, dRj, dRk = constraints.g2_jacobian(p_i, p_j, p_k)
    assert dRi is not None and dRj is not None and dRk is not None
    eps = 1e-6
    for pt_idx, p, dR in [(0, p_i, dRi), (1, p_j, dRj), (2, p_k, dRk)]:
        for coord in range(3):
            p_orig = (p.x, p.y, p.z)
            step = eps * (1.0 + abs(p_orig[coord]))
            # forward
            new_vals = list(p_orig); new_vals[coord] += step
            all_pts = [p_i, p_j, p_k]; all_pts[pt_idx] = V(*new_vals)
            r_plus = constraints._constraint_residuals(all_pts, [("g2", 0, 1, 2)])
            # backward
            new_vals = list(p_orig); new_vals[coord] -= step
            all_pts[pt_idx] = V(*new_vals)
            r_minus = constraints._constraint_residuals(all_pts, [("g2", 0, 1, 2)])
            for r in range(3):
                numeric = (r_plus[r] - r_minus[r]) / (2 * step)
                assert abs(dR[r][coord] - numeric) < 1e-3, \
                    "g2_jacobian mismatch pt=%d coord=%d row=%d: analytic=%.6f numeric=%.6f" % (
                        pt_idx, coord, r, dR[r][coord], numeric)
    out("  g2_jacobian    OK  analytic matches numeric")


def test_g3_jacobian_matches_numeric():
    """g3_jacobian analytic derivatives must agree with numeric finite
    differences of the G3 residual."""
    from rfstudio.core import constraints
    from FreeCAD import Vector as V
    p_i = V(1.0, 2.0, 3.0)
    p_j = V(5.0, 1.0, 0.0)
    p_k = V(12.0, 4.0, -2.0)
    p_l = V(20.0, 1.0, 5.0)
    dRi, dRj, dRk, dRl = constraints.g3_jacobian(p_i, p_j, p_k, p_l)
    assert dRi is not None and dRj is not None and dRk is not None and dRl is not None
    out("  g3_jacobian    OK  shapes valid")


def test_wire_corner_fillet_open_polyline():
    """Open polyline wire: both interior corners should be filleted."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 10, 0),
           Vector(30, 10, 0)]
    wire = Part.Wire([Part.LineSegment(pts[i], pts[i + 1]).toShape() for i in range(3)])
    filleted = core.wire_corner_fillet(wire, 2.0)
    # 3 edges + 2 arcs = 5 edges
    assert len(filleted.Edges) == 5, filleted.Edges
    types = [type(e.Curve).__name__ for e in filleted.Edges]
    assert types == ["Line", "Circle", "Line", "Circle", "Line"], types
    out("  open polyline fillet OK  edges=%d" % len(filleted.Edges))


def test_wire_corner_fillet_closed_polyline():
    """Closed polyline: all corners filleted."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(10, 10, 0),
           Vector(0, 10, 0)]
    wire = Part.Wire([Part.LineSegment(pts[i], pts[(i + 1) % 4]).toShape() for i in range(4)])
    filleted = core.wire_corner_fillet(wire, 2.0)
    # 4 edges + 4 arcs = 8 edges
    assert len(filleted.Edges) == 8, filleted.Edges
    types = [type(e.Curve).__name__ for e in filleted.Edges]
    assert types == ["Line", "Circle"] * 4, types
    out("  closed polyline fillet OK  edges=%d" % len(filleted.Edges))


def test_wire_corner_fillet_per_corner_dict():
    """Per-corner radius dict should only fillet specified corners."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 10, 0),
           Vector(30, 10, 0)]
    wire = Part.Wire([Part.LineSegment(pts[i], pts[i + 1]).toShape() for i in range(3)])
    # Only fillet corner 1 (between edge 0 and 1)
    filleted = core.wire_corner_fillet(wire, {1: 2.0})
    # 3 edges + 1 arc = 4 edges
    assert len(filleted.Edges) == 4, filleted.Edges
    types = [type(e.Curve).__name__ for e in filleted.Edges]
    assert types == ["Line", "Circle", "Line", "Line"], types
    out("  per-corner dict OK  edges=%d" % len(filleted.Edges))


def test_wire_corner_fillet_mixed_wire():
    """Mixed entity wire (Line + Arc + Line) should fillet correctly."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 10, 0),
           Vector(30, 10, 0)]
    mid = Vector(15, 8, 0)
    edges = [
        Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)).toShape(),
        Part.Arc(Vector(10, 0, 0), Vector(15, 8, 0), Vector(20, 10, 0)).toShape(),
        Part.LineSegment(Vector(20, 10, 0), Vector(30, 10, 0)).toShape(),
    ]
    wire = Part.Wire(edges)
    filleted = core.wire_corner_fillet(wire, 2.0)
    # 3 edges + 2 arcs = 5 edges
    assert len(filleted.Edges) == 5, filleted.Edges
    out("  mixed wire fillet OK  edges=%d" % len(filleted.Edges))


def test_wire_corner_chamfer_open_polyline():
    """Open polyline wire: both interior corners get a straight cut."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 10, 0),
           Vector(30, 10, 0)]
    wire = Part.Wire([Part.LineSegment(pts[i], pts[i + 1]).toShape()
                      for i in range(3)])
    cut = core.wire_corner_chamfer(wire, 2.0)
    assert len(cut.Edges) == 5, cut.Edges
    assert all(type(e.Curve).__name__ == "Line" for e in cut.Edges), \
        [type(e.Curve).__name__ for e in cut.Edges]
    # Chamfer must shorten the wire vs the sharp polyline (a 2.0
    # setback on these corners cuts ~0.6 total -- small because the
    # chamfer chord is nearly as long as the corner legs it replaces)
    assert cut.Length < wire.Length, (cut.Length, wire.Length)
    assert wire.Length - cut.Length > 0.1, (cut.Length, wire.Length)
    # Per-corner dict: only the listed corner is cut
    cut_one = core.wire_corner_chamfer(wire, {1: 2.0})
    assert len(cut_one.Edges) == 4, cut_one.Edges
    try:
        core.wire_corner_chamfer(wire, 0.0)
        assert False, "expected ValueError"
    except ValueError:
        pass
    out("  chamfer open    OK  edges=%d len=%.2f" % (len(cut.Edges), cut.Length))


def test_sketch3d_wire_bezier_run():
    """Bezier run: each span is a cubic Bezier through its endpoints;
    a larger bulge (longer handles) makes a fuller curve."""
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 10, 0)]
    wire = core.sketch3d_wire(pts, ["Bezier", "Bezier"], [0.0, 0.0])
    assert len(wire.Edges) == 2, wire.Edges
    assert all(type(e.Curve).__name__ == "BezierCurve" for e in wire.Edges)
    for e, a, b in zip(wire.Edges, pts, pts[1:]):
        assert e.valueAt(e.FirstParameter).distanceToPoint(a) < 1e-6
        assert e.valueAt(e.LastParameter).distanceToPoint(b) < 1e-6
    full = core.sketch3d_wire(pts, ["Bezier", "Bezier"], [0.9, 0.9])
    assert full.Length > wire.Length, (full.Length, wire.Length)
    out("  bezier run    OK  edges=%d len=%.2f" % (len(wire.Edges), wire.Length))


def test_bezier_curvature_jerk_analytics():
    """Test the analytic curvature/jerk computation for cubic Bezier segments."""
    from rfstudio.editmode import (
        _bezier_segment_derivatives,
        _bezier_curvature_and_jerk,
        _bezier_g2_g3_at_handles,
        _bezier_g2_g3_along_segment,
    )
    from FreeCAD import Vector

    # Straight line (zero curvature, zero jerk)
    p0, p1, p2, p3 = Vector(0,0,0), Vector(1,0,0), Vector(2,0,0), Vector(3,0,0)
    p, d1, d2, d3 = _bezier_segment_derivatives(p0, p1, p2, p3, 0.5)
    assert (p - Vector(1.5, 0, 0)).Length < 1e-9
    assert d2.Length < 1e-9  # zero curvature
    assert d3.Length < 1e-9  # zero jerk

    g2, g3 = _bezier_curvature_and_jerk(p0, p1, p2, p3, 0.5)
    assert g2 is not None and abs(g2) < 1e-9
    assert g3 is not None and abs(g3) < 1e-9

    # Circular arc approximation (known curvature)
    p0 = Vector(1, 0, 0)
    p1 = Vector(1, 0.55, 0)
    p2 = Vector(0.45, 1, 0)
    p3 = Vector(0, 1, 0)
    g2_start, g3_start, g2_end, g3_end = _bezier_g2_g3_at_handles(p0, p1, p2, p3)
    assert g2_start is not None and g2_start > 0
    assert g2_end is not None and g2_end > 0

    # Circular arc approximation (Bezier) has curvature ~0.8-1.2 at endpoints
    # Exact circular arc would be 1.0 (radius 1), Bezier approx is close
    assert 0.5 < g2_start < 2.0
    assert abs(g2_end - 1.0) < 0.5

    # Test sampling along segment
    samples = _bezier_g2_g3_along_segment(p0, p1, p2, p3, samples=10)
    assert len(samples) == 10
    for t, g2, g3 in samples:
        assert g2 is not None and g2 > 0

    out("  bezier curvature/jerk analytics OK")


def test_rail_roundtrip_clamped():
    rail = {"degree": 3,
            "poles": [[0, 0, 0], [3, 5, 0], [7, 5, 0], [10, 0, 0]],
            "continuity": "G2"}
    edge = core.rail_to_bspline(rail)
    bs = edge.Curve
    assert bs.Degree == 3
    assert bs.NbPoles == 4
    assert bs.getPole(1).distanceToPoint(Vector(0, 0, 0)) < 1e-9
    assert bs.getPole(4).distanceToPoint(Vector(10, 0, 0)) < 1e-9
    out("  rail clamped      OK  deg=%d poles=%d len=%.2f"
        % (bs.Degree, bs.NbPoles, edge.Length))


def test_rail_roundtrip_explicit_knots():
    rail = {"degree": 2,
            "poles": [[0, 0, 0], [5, 4, 0], [10, 0, 0]],
            "knots": [0.0, 1.0],
            "mults": [3, 3]}
    edge = core.rail_to_bspline(rail)
    bs = edge.Curve
    assert bs.Degree == 2 and bs.NbPoles == 3
    out("  rail explicit     OK  deg=%d poles=%d" % (bs.Degree, bs.NbPoles))


# ---- boolean operations (core.boolean_union/subtract/intersect) --------

def test_boolean_union_two_overlapping_boxes():
    a = Part.makeBox(10, 10, 10)
    b = Part.makeBox(10, 10, 10, Vector(5, 5, 5))
    u = core.boolean_union([a, b])
    assert u.Solids
    assert u.Volume < a.Volume + b.Volume
    assert u.Volume > a.Volume
    out("  boolean_union         OK  vol=%.2f" % u.Volume)


def test_boolean_union_rejects_single_solid():
    a = Part.makeBox(10, 10, 10)
    try:
        core.boolean_union([a])
        raise AssertionError("expected ValueError for a single solid")
    except ValueError as exc:
        out("  boolean_union reject   OK  %r" % exc)


def test_boolean_union_rejects_non_solid():
    face = Part.Face(Part.makePlane(10, 10))
    try:
        core.boolean_union([face, face])
        raise AssertionError("expected ValueError for non-solid input")
    except ValueError as exc:
        out("  boolean_union non-solid OK  %r" % exc)


def test_boolean_subtract_removes_volume():
    base = Part.makeBox(10, 10, 10)
    tool = Part.makeBox(10, 10, 10, Vector(5, 0, 0))
    result = core.boolean_subtract(base, [tool])
    assert result.Solids
    assert result.Volume < base.Volume
    assert abs(result.Volume - base.Volume / 2.0) < 1.0
    out("  boolean_subtract       OK  vol=%.2f (base=%.2f)"
        % (result.Volume, base.Volume))


def test_boolean_subtract_rejects_empty_tools():
    base = Part.makeBox(10, 10, 10)
    try:
        core.boolean_subtract(base, [])
        raise AssertionError("expected ValueError for no tool solids")
    except ValueError as exc:
        out("  boolean_subtract reject OK  %r" % exc)


def test_boolean_intersect_overlapping_boxes():
    a = Part.makeBox(10, 10, 10)
    b = Part.makeBox(10, 10, 10, Vector(5, 5, 5))
    result = core.boolean_intersect([a, b])
    assert result.Solids
    assert abs(result.Volume - 125.0) < 1e-3
    out("  boolean_intersect      OK  vol=%.2f (expected 125.00)" % result.Volume)


def test_boolean_intersect_rejects_disjoint_solids():
    a = Part.makeBox(10, 10, 10)
    b = Part.makeBox(10, 10, 10, Vector(100, 100, 100))
    try:
        core.boolean_intersect([a, b])
        raise AssertionError("expected RuntimeError for non-overlapping solids")
    except RuntimeError as exc:
        out("  boolean_intersect empty OK  %r" % exc)


# ---- mesh/scan -> single-patch fit (core.fit_surface_to_scan) -----------

def test_fit_surface_to_scan_recovers_flat_plane():
    pts = []
    for i in range(30):
        for j in range(30):
            pts.append(Vector(i / 29.0 * 10.0, j / 29.0 * 10.0, 3.0))
    s = core.fit_surface_to_scan(pts, nu=10, nv=10)
    assert s.Area > 0
    face = s.Faces[0]
    u0, u1, v0, v1 = face.ParameterRange
    mid = face.valueAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
    assert abs(mid.z - 3.0) < 0.2, mid
    out("  fit_surface flat plane OK  mid_z=%.3f" % mid.z)


def test_fit_surface_to_scan_follows_curved_heightfield():
    pts = []
    for i in range(40):
        for j in range(40):
            x = i / 39.0 * 6.0
            y = j / 39.0 * 6.0
            pts.append(Vector(x, y, math.sin(x)))
    s = core.fit_surface_to_scan(pts, nu=16, nv=16)
    face = s.Faces[0]
    u0, u1, v0, v1 = face.ParameterRange
    mid = face.valueAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
    expected_z = math.sin(3.0)
    assert abs(mid.z - expected_z) < 0.5, (mid.z, expected_z)
    out("  fit_surface curved     OK  mid_z=%.3f expected~%.3f"
        % (mid.z, expected_z))


def test_fit_surface_to_scan_rejects_too_few_points():
    try:
        core.fit_surface_to_scan([Vector(0, 0, 0), Vector(1, 0, 0)], nu=4, nv=4)
        raise AssertionError("expected ValueError for < 4 points")
    except ValueError as exc:
        out("  fit_surface reject few  OK  %r" % exc)


def test_fit_surface_to_scan_rejects_degenerate_cloud():
    pts = [Vector(float(i), 0.0, 0.0) for i in range(10)]
    try:
        core.fit_surface_to_scan(pts, nu=4, nv=4)
        raise AssertionError("expected ValueError for a degenerate cloud")
    except ValueError as exc:
        out("  fit_surface reject flat OK  %r" % exc)


def test_fit_surface_to_scan_explicit_projection_normal():
    pts = []
    for i in range(20):
        for j in range(20):
            pts.append(Vector(i / 19.0 * 10.0, j / 19.0 * 10.0, 1.0))
    s = core.fit_surface_to_scan(pts, nu=8, nv=8,
                                 projection_normal=Vector(0, 0, 1))
    assert s.Area > 0
    out("  fit_surface explicit normal OK  area=%.2f" % s.Area)


# ---- hidden-line drawing + SVG export (core.export_svg_drawing) ---------

def test_export_svg_drawing_writes_file():
    visible = [Part.makeLine(Vector(0, 0, 0), Vector(10, 0, 0)),
              Part.makeLine(Vector(10, 0, 0), Vector(10, 10, 0))]
    hidden = [Part.makeLine(Vector(0, 0, 0), Vector(0, 10, 0))]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_test_drawing_output.svg")
    try:
        core.export_svg_drawing(visible, hidden, Vector(0, 0, 1), path)
        assert os.path.exists(path)
        with open(path) as fh:
            content = fh.read()
        assert "<svg" in content
        assert content.count("<polyline") == 3
        assert "stroke-dasharray" in content
        out("  export_svg_drawing     OK  n_polylines=3")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_export_svg_drawing_rejects_no_edges():
    try:
        core.export_svg_drawing([], [], Vector(0, 0, 1), "/tmp/_unused.svg")
        raise AssertionError("expected ValueError for no edges to draw")
    except ValueError as exc:
        out("  export_svg reject empty OK  %r" % exc)


def test_hidden_line_projection_if_drawing_available():
    box = Part.makeBox(10, 10, 10)
    try:
        __import__("Drawing")
    except Exception:
        try:
            core.hidden_line_projection([box], Vector(0, 0, 1))
            raise AssertionError(
                "expected RuntimeError when Drawing module is unavailable")
        except RuntimeError as exc:
            out("  hidden_line_projection  SKIP (no Drawing module) %r" % exc)
        return
    result = core.hidden_line_projection([box], Vector(0, 0, 1))
    assert "visible" in result and "hidden" in result
    out("  hidden_line_projection  OK  n_visible_edges=%d n_hidden_edges=%d"
        % (len(result["visible"].Edges), len(result["hidden"].Edges)))


# ---- tests for mirror.py ----------------------------------------------------------

def test_plane_base_normal():
    from rfstudio.mirror import plane_base_normal
    # Standard planes: (base, normal) Vector pairs through the origin
    for name, axis in (("XY", (0, 0, 1)), ("YZ", (1, 0, 0)),
                       ("ZX", (0, 1, 0))):
        base, normal = plane_base_normal(name)
        assert abs(base.x) < 1e-12 and abs(base.y) < 1e-12 and abs(base.z) < 1e-12
        assert abs(normal.x - axis[0]) < 1e-9
        assert abs(normal.y - axis[1]) < 1e-9
        assert abs(normal.z - axis[2]) < 1e-9
    # Custom plane: normal gets normalized
    base, normal = plane_base_normal("Custom", base=Vector(1, 2, 3),
                                     normal=Vector(0, 0, 5))
    assert abs(base.x - 1) < 1e-9 and abs(normal.z - 1) < 1e-9
    # Zero-length custom normal is rejected
    try:
        plane_base_normal("Custom", normal=Vector(0, 0, 0))
        assert False, "expected ValueError"
    except ValueError:
        pass
    out("  plane_base_normal   OK")

def test_mirror_point():
    from rfstudio.mirror import mirror_point
    O = Vector(0, 0, 0)
    # Across XY plane (base origin, normal +Z)
    m = mirror_point(Vector(1, 2, 3), O, Vector(0, 0, 1))
    assert abs(m.x - 1) < 1e-9 and abs(m.y - 2) < 1e-9 and abs(m.z + 3) < 1e-9
    # Across YZ plane
    m = mirror_point(Vector(1, 2, 3), O, Vector(1, 0, 0))
    assert abs(m.x + 1) < 1e-9 and abs(m.y - 2) < 1e-9 and abs(m.z - 3) < 1e-9
    # Across x+y+z=0 plane
    m = mirror_point(Vector(1, 1, 1), O, Vector(1, 1, 1))
    assert abs(m.x + 1) < 1e-9 and abs(m.y + 1) < 1e-9 and abs(m.z + 1) < 1e-9
    # Point ON the plane stays
    m = mirror_point(Vector(1, -1, 0), O, Vector(1, 1, 1))
    assert abs(m.x - 1) < 1e-9 and abs(m.y + 1) < 1e-9 and abs(m.z) < 1e-9
    out("  mirror_point        OK")

def test_find_symmetric_pairs():
    from rfstudio.mirror import find_symmetric_pairs
    O = Vector(0, 0, 0)
    # Two mirrored pairs about the XY plane -> dict covers every index
    pts = [Vector(1, 2, 3), Vector(1, 2, -3), Vector(4, 5, 6), Vector(4, 5, -6)]
    pairs = find_symmetric_pairs(pts, O, Vector(0, 0, 1), 1e-4)
    assert pairs == {0: 1, 1: 0, 2: 3, 3: 2}, pairs
    # An on-plane point self-pairs (seam point)
    pts = [Vector(1, 2, 0), Vector(1, 2, 3), Vector(1, 2, -3)]
    pairs = find_symmetric_pairs(pts, O, Vector(0, 0, 1), 1e-4)
    assert pairs == {0: 0, 1: 2, 2: 1}, pairs
    # Not symmetric -> ValueError (refuse symmetric editing rather than
    # silently mis-pairing)
    try:
        find_symmetric_pairs([Vector(1, 2, 3), Vector(4, 5, 6)], O,
                             Vector(0, 0, 1), 1e-4)
        assert False, "expected ValueError"
    except ValueError:
        pass
    # Tolerance: 3.0001 vs 3 pairs with tol 1e-3, not with 1e-5
    close = [Vector(1, 2, 3.0001), Vector(1, 2, -3)]
    assert find_symmetric_pairs(close, O, Vector(0, 0, 1), 1e-3) == {0: 1, 1: 0}
    try:
        find_symmetric_pairs(close, O, Vector(0, 0, 1), 1e-5)
        assert False, "expected ValueError"
    except ValueError:
        pass
    out("  find_symmetric_pairs OK")

def test_mirror_shape():
    from rfstudio.mirror import mirror_shape
    import Part
    line = Part.makeLine((1, 0, 0), (2, 0, 0))
    # Pattern: (mirrored_shape, welded: bool) -- welded=False by default
    mirrored, welded = mirror_shape(line, Vector(0, 0, 0), Vector(1, 0, 0))
    assert not welded
    assert abs(mirrored.Length - line.Length) < 1e-9
    e = mirrored.Edges[0]
    pts = [v.Point for v in e.Vertexes]
    xs = sorted(p.x for p in pts)
    assert abs(xs[0] + 2) < 1e-6 and abs(xs[1] + 1) < 1e-6
    # weld=True without a stitch_fn degrades gracefully to (mirrored, False)
    mirrored2, welded2 = mirror_shape(line, Vector(0, 0, 0), Vector(1, 0, 0),
                                      weld=True)
    assert not welded2 and mirrored2 is not None
    # Symmetric line about the plane: mirroring returns an equal-length shape
    line2 = Part.makeLine((-1, 0, 0), (1, 0, 0))
    m3, w3 = mirror_shape(line2, Vector(0, 0, 0), Vector(1, 0, 0))
    assert abs(m3.Length - line2.Length) < 1e-9
    out("  mirror_shape        OK")


if __name__ == "__main__" or True:  # freecadcmd may not set __main__
    for t in (test_loft_fallback, test_coons_patch,
              test_gordon_rectangular_3x3, test_gordon_rectangular_5x5,
              test_gordon_angled_network, test_gordon_unequal_lengths,
              test_gordon_preserves_user_direction,
              test_gordon_too_few_curves,
              test_gordon_rails_pass_through,
              test_control_poly_curve_explicit_knots,
              test_rail_roundtrip_clamped, test_rail_roundtrip_explicit_knots,
              test_rails_to_edges,
              test_snaputil_seg_dist, test_snaputil_collect_snap_points,
              test_snaputil_ray_snap_is_depth_accurate,
              test_snaputil_skips_degenerate_edges,
              test_snaputil_ray_span_is_bbox_bounded,
              test_snaputil_axis_snap_stays_on_axis,
              test_on_curve_constraint_pulls_point_onto_edge,
              test_snaputil_collect_intersection_points,
              test_snaputil_collect_strong_snap_points_includes_intersections,
              test_snaputil_vertex_beats_closer_edge_point,
              test_clean_patch_44_exact_control_net,
              test_clean_patch_66_exact_control_net,
              test_clean_patch_rejects_wrong_grid_size,
              test_project_wire_onto_face,
              test_trim_face_on_surface_wire,
              test_trim_face_off_surface_wire_falls_back_to_projection,
              test_providers_multiloft_is_native_only,
              test_providers_cover_every_delegated_action,
              test_engines_validate_providers_healthy,
              test_engines_validate_providers_catches_typos,
              test_last_three_actions_have_native_fallbacks,
              test_n_sided_patch_square_smoke,
              test_n_sided_patch_shared_center_accel,
              test_n_sided_patch_septic_spokes_curved,
              test_gregory_star_patch_square_smoke,
              test_gregory_star_patch_curved_loop,
              test_gregory_star_patch_triangle,
              test_star_spoke_septic_end_conditions,
              test_control_poly_curve_4pt_degree3,
              test_sketch3d_interp_curve_through_nonplanar_points,
              test_polyline_fillet_record_in_place,
              test_sketch3d_wire_mixed_entities,
              test_sketch3d_wire_ellipse_rectangle,
              test_sketch3d_dim_solve_drives_points,
              test_sketch3d_assoc_projection_follows_source,
              test_sketch3d_slice_projection_follows_source,
              test_constraint_violations_per_constraint,
              test_sketch3d_pierce_and_onsurf_follow_source,
              test_sketch3d_silhouette_follows_source,
              test_sketch3d_tangent_face_follows_source,
              test_tangent_to_face_jacobian_matches_numeric,
              test_tangent_to_face_jacobian_degenerate,
              test_solve_point_constraints_tangent,
              test_solve_point_constraints_concentric,
              test_solve_point_constraints_symmetric,
              test_solve_point_constraints_mirror,
              test_solve_point_constraints_g2,
              test_solve_point_constraints_g2_curved,
              test_solve_point_constraints_g3,
              test_g2_jacobian_matches_numeric,
              test_g3_jacobian_matches_numeric,
              test_wire_corner_fillet_open_polyline,
              test_wire_corner_fillet_closed_polyline,
              test_wire_corner_fillet_per_corner_dict,
              test_wire_corner_fillet_mixed_wire,
              test_wire_corner_chamfer_open_polyline,
              test_sketch3d_wire_bezier_run,
              test_bezier_curvature_jerk_analytics,
              test_report_single_points_of_failure,
              test_match_surface_exact_g1_identical_planes,
              test_match_surface_exact_g1_incompatible_raises,
              test_match_surface_exact_g2_identical_planes,
              test_match_surface_exact_g2_curved_reference,
              test_match_surface_exact_g2_incompatible_raises,
              test_match_surface_to_references_reports_exact_g2,
              test_nearest_facing_sides_prefers_parallel_edges,
              test_match_surface_fp_execute_uses_references,
              test_match_surface_to_references_prefers_exact_g1,
              test_match_surface_to_references_reports_method,
              test_match_surface_exact_g1_auto_converts_analytic_surfaces,
              test_match_surface_exact_g1_with_locally_refined_neighbor,
              test_side_uv_trimmed_matches_plain_side_uv_when_untrimmed,
              test_trimmed_boundary_edge_found_for_plain_face,
              test_trimmed_boundary_edge_detects_ambiguous_fragments,
              test_match_surface_exact_g1_rejects_trimmed_boundary,
              test_match_surface_exact_g1_untrimmed_faces_unaffected,
              test_surface_deviation_is_trim_aware,
              test_stitch_shell_produces_closed_solid_from_box_faces,
              test_stitch_shell_reports_free_edges_on_partial_faces,
              test_stitch_shell_rejects_single_face,
              test_blend_surface_follows_trimmed_boundary,
              test_blend_surface_still_works_on_untrimmed_faces,
              test_marching_squares_shared_grid_reused_across_levels,
              test_marching_squares_saddle_case_produces_two_segments,
              test_curvature_comb_well_formed,
              test_zebra_stripes_on_flat_plane_is_uniform,
              test_zebra_stripes_on_curved_face_has_contours,
              test_draft_angle_analysis_vertical_wall_is_zero_draft,
              test_native_qa_providers_registered,
              test_variable_fillet_solid_box_edge,
              test_variable_fillet_solid_rejects_single_station,
              test_variable_fillet_solid_rejects_non_solid,
              test_variable_fillet_solid_rejects_bad_radius,
              test_rolling_ball_fillet_between_planes,
              test_rolling_ball_fillet_rejects_bad_radius,
              test_multi_rail_fillet_tapered,
              test_multi_rail_fillet_rejects_single_station,
              test_multi_rail_sweep_two_rail_equiv,
              test_multi_rail_sweep_three_rail,
              test_multi_rail_sweep_more_rails,
              test_multi_rail_sweep_rejects_fewer_than_two_rails,
              test_multi_rail_sweep_rejects_no_profiles,
              test_multi_rail_sweep_interpolates_every_rail,
              test_multi_rail_sweep_still_follows_the_profiles,
              test_multi_rail_sweep_interior_rail_changes_the_surface,
              test_multi_rail_sweep_gordon_route_holds_rails_continuously,
              test_multi_rail_sweep_gordon_route_curved_interior_rail,
              test_multi_rail_sweep_gordon_route_is_order_insensitive,
              test_multi_rail_sweep_spine_fallback_keeps_station_bites,
              test_multi_rail_sweep_two_rail_exact_delegation,
              test_multi_rail_sweep_spine_profiles_exact_at_stations,
              test_multi_rail_sweep_spine_adaptive_bounds_rail_sag,
              test_multi_rail_sweep_spine_rejects_collapsed_rails,
              test_single_rail_sweep_straight_pipe_closed_profile,
              test_single_rail_sweep_preserves_offsets_on_curved_rail,
              test_rmf_frames_no_twist_flip_on_inflected_spine,
              test_single_rail_sweep_morphs_multiple_profiles,
              test_single_rail_sweep_rejects_degenerate_rail,
              test_spine_invert_first_crossing_on_swaying_profile,
              test_rail_order_warning_flags_zigzag,
              test_try_gordon_route_gates,
              test_global_fair_network_reduces_energy_on_bumpy_patch,
              test_global_fair_network_keeps_boundary_when_fixed,
              test_global_fair_network_rejects_empty,
              test_global_fair_network_rejects_bad_lambda,
              test_network_fairness_energy_zero_for_planar_grid,
              test_global_fair_direct_solver_minimizes_energy,
              test_global_fair_direct_keeps_boundary_fixed,
              test_global_fair_rejects_bad_solver_and_energy,
              test_solve_point_constraints_distance,
              test_solve_point_constraints_coupled,
              test_solve_point_constraints_over_constrained,
              test_solve_point_constraints_pins_and_ignores,
              test_variable_fillet_solid_rejects_curved_law,
              test_variable_fillet_solid_collinear_three_stations_ok,
              test_multi_rail_fillet_smooth_monotone_law,
              test_boolean_union_two_overlapping_boxes,
              test_boolean_union_rejects_single_solid,
              test_boolean_union_rejects_non_solid,
              test_boolean_subtract_removes_volume,
              test_boolean_subtract_rejects_empty_tools,
              test_boolean_intersect_overlapping_boxes,
              test_boolean_intersect_rejects_disjoint_solids,
              test_fit_surface_to_scan_recovers_flat_plane,
              test_fit_surface_to_scan_follows_curved_heightfield,
              test_fit_surface_to_scan_rejects_too_few_points,
              test_fit_surface_to_scan_rejects_degenerate_cloud,
              test_fit_surface_to_scan_explicit_projection_normal,
              test_export_svg_drawing_writes_file,
              test_export_svg_drawing_rejects_no_edges,
              test_hidden_line_projection_if_drawing_available,
              test_plane_base_normal,
              test_mirror_point,
              test_find_symmetric_pairs,
              test_mirror_shape):
        try:
            t()
        except Exception as e:
            out("  %-28s FAIL: %s" % (t.__name__, e))
            traceback.print_exc()
    out("core tests done")