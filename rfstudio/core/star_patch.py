# -*- coding: utf-8 -*-
"""Native N-sided ("star point") patch."""

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
#


from __future__ import annotations

import Part
from FreeCAD import Vector

from .curve import _pt, _eval01
from .grid_surface import grid_to_surface
from .hermite_profile import _hermite5


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
    so treat the corner-end curvature match as a fairness improvement over
    a bare straight spoke, not a continuity guarantee. None (no analytic
    curvature available) falls back to a zero corner-end acceleration too
    -- i.e. the previous cubic behaviour at that end.
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