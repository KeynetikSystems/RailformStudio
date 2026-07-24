# -*- coding: utf-8 -*-
"""Solid-oriented ops: offset/thicken/shell, fillets & chamfers, booleans,
shell stitching."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_face_from_input)



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

__all__ = [
    'offset_surface',
    'thicken_surface',
    'extend_surface',
    'shell_solid',
    '_polyline_wire',
    'polyline_corner_fillet',
    '_solid_fillet_chamfer_common',
    'fillet_solid',
    'chamfer_solid',
    '_monotone_cubic_fn',
    'variable_fillet_solid',
    'stitch_shell',
    'free_edge_count',
    '_coerce_solid',
    '_coerce_solid_list',
    '_refine',
    'boolean_union',
    'boolean_subtract',
    'boolean_intersect',
]
