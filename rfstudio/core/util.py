# -*- coding: utf-8 -*-
"""Shared low-level geometry helpers (vectors, grids, hermite bases,
finite-difference derivatives, trim-aware boundary sampling, knot utilities).
Base layer: depends on nothing else in the package."""

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

__all__ = [
    'G0',
    'G1',
    'G2',
    'dot',
    'cross',
    'norm',
    'as_wire',
    'join_curves',
    'loft',
    '_eval01',
    '_orient_family',
    '_pt',
    'grid_to_surface',
    '_chain_loop',
    '_compute_finite_difference_epsilon',
    '_finite_difference_second_derivative',
    '_finite_difference_third_derivative',
    '_cross_tan_from_surface_tangent',
    '_face_from_input',
    '_hermite3',
    '_hermite5',
    '_hermite7',
    '_nearest_facing_sides',
    '_side_uv',
    '_trimmed_boundary_candidates',
    '_trimmed_boundary_edge',
    '_side_uv_trimmed',
    '_side_is_trimmed',
    'rail_to_bspline',
    'rails_to_edges',
    '_clamped_knots',
    '_clamped_mults',
]
