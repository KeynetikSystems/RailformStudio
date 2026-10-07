# -*- coding: utf-8 -*-
"""Curve drawing / modeling primitives and the Blender rail interchange."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector, Matrix, Placement, Rotation

from .util import (_clamped_knots, _clamped_mults, _eval01, _hermite7)



# --- curve drawing / modeling (our own, Gui-free) ------------------------- #
def sketch3d_wire(points, seg_types=None, bulges=None):
    """A single Wire built from a point list where each segment
    (i -> i+1) can be its own drafting entity:

      "Line"      -- straight edge (G0 break at every point)
      "Arc"       -- circular arc through the two endpoints, pushed off the
                     chord by `bulge` * (chord_len/2) perpendicular to it
                     (AutoCAD polyline bulge semantics: bulge > 0 bulges one
                     side, < 0 the other, 0 degenerates to a line)
      "Spline"    -- contiguous runs are one clamped-interpolating B-spline
                     through the run's points
      "Ellipse"   -- elliptical arc through the two endpoints. The chord
                     defines the major axis; `bulge` is the minor/major
                     axis ratio (0 = circular arc, >0 = elliptical, <0
                     flips the ellipse side). Uses Part.Ellipse + trimmed
                     arc between the endpoints.
      "Rectangle" -- right-angled corner: two perpendicular edges from
                     a to b via an intermediate corner point. `bulge`
                     chooses the corner direction (+1 = right turn in
                     the construction plane, -1 = left turn, 0 = straight).
                     Creates two Line edges.
      "Bezier"    -- contiguous runs become cubic Bezier spans through
                     the run's points (same construction as
                     bezier_curve_shape's Auto handles). `bulge` scales
                     the handle length as a fraction of the chord
                     (<= 0 means the default 1/3), i.e. per-segment
                     tension control without a handle editor.

    Mixed-entity 3D sketch: this is what makes one RFS_Sketch3D hold a
    polyline + arc + spline combo instead of exactly one curve per use.

    Returns a Part.Wire. Raises ValueError on a bad/ambiguous
    (types/bulges) input -- the caller maps that to a QMessageBox."""
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    n = len(pts)
    if n < 2:
        raise ValueError("sketch3d_wire: need >= 2 points")
    seg_types = list(seg_types) if seg_types is not None else ["Spline"] * (n - 1)
    bulges = list(bulges) if bulges is not None else [0.0] * (n - 1)
    if len(seg_types) != n - 1 or len(bulges) != n - 1:
        raise ValueError(
            "sketch3d_wire: seg_types/bulges must have one entry per "
            "segment (got %d/%d for %d points)"
            % (len(seg_types), len(bulges), n))
    edges = []
    i = 0
    while i < n - 1:
        if seg_types[i] == "Spline":
            # Absorb the maximal run of consecutive Spline segments into
            # ONE interpolating B-spline edge through the run's points.
            j = i
            while j < n - 1 and seg_types[j] == "Spline":
                j += 1
            run = pts[i:j + 1]
            edges.append(interpolate_curve(run))
            i = j
            continue
        if seg_types[i] == "Bezier":
            # Absorb the run; each span is a cubic Bezier whose handles
            # follow the polyline direction at the shared points
            # (Catmull-Rom-style tangents), scaled by `bulge` (default
            # 1/3) of the span chord -- tension control per span.
            j = i
            while j < n - 1 and seg_types[j] == "Bezier":
                j += 1
            run = pts[i:j + 1]
            m = len(run)

            def _tan(k):
                if k <= 0:
                    d = run[1] - run[0]
                elif k >= m - 1:
                    d = run[m - 1] - run[m - 2]
                else:
                    d = run[k + 1] - run[k - 1]
                return d * (1.0 / d.Length) if d.Length > 1e-9 else None

            tans = [_tan(k) for k in range(m)]
            for k in range(i, j):
                f = bulges[k] if bulges[k] > 1e-9 else (1.0 / 3.0)
                p0, p3 = pts[k], pts[k + 1]
                L = (p3 - p0).Length
                if L < 1e-9:
                    continue
                t0 = tans[k - i] or (p3 - p0) * (1.0 / L)
                t1 = tans[k - i + 1] or (p3 - p0) * (1.0 / L)
                bz = Part.BezierCurve()
                bz.setPoles([p0, p0 + t0 * (f * L),
                             p3 - t1 * (f * L), p3])
                edges.append(bz.toShape())
            i = j
            continue
        a, b = pts[i], pts[i + 1]
        if seg_types[i] == "Line" or (seg_types[i] == "Arc" and
                                      abs(bulges[i]) < 1e-9):
            if (b - a).Length > 1e-9:
                edges.append(Part.LineSegment(a, b).toShape())
        elif seg_types[i] == "Arc":
            chord = b - a
            length = chord.Length
            if length < 1e-9:
                i += 1
                continue
            mid = (a + b) * 0.5
            axis = min(((1.0, 0.0, 0.0), abs(chord.x)),
                       ((0.0, 1.0, 0.0), abs(chord.y)),
                       ((0.0, 0.0, 1.0), abs(chord.z)),
                       key=lambda t: t[1])[0]
            n_perp = chord.cross(Vector(*axis))
            if n_perp.Length < 1e-12:
                n_perp = Vector(0, 0, 1)
            n_perp.normalize()
            mid += n_perp * (bulges[i] * length * 0.5)
            try:
                edges.append(Part.Arc(a, mid, b).toShape())
            except Exception:
                edges.append(Part.LineSegment(a, b).toShape())
        elif seg_types[i] == "Ellipse":
            # Elliptical arc: chord is major axis, bulge = minor/major ratio
            chord = b - a
            length = chord.Length
            if length < 1e-9:
                i += 1
                continue
            center = (a + b) * 0.5
            # Major axis along chord direction
            major_dir = chord * (1.0 / length)
            # Normal: same logic as Arc (perpendicular to chord)
            axis = min(((1.0, 0.0, 0.0), abs(chord.x)),
                       ((0.0, 1.0, 0.0), abs(chord.y)),
                       ((0.0, 0.0, 1.0), abs(chord.z)),
                       key=lambda t: t[1])[0]
            n_perp = chord.cross(Vector(*axis))
            if n_perp.Length < 1e-12:
                n_perp = Vector(0, 0, 1)
            n_perp.normalize()
            # Minor axis in the plane perpendicular to chord
            minor_dir = n_perp.cross(major_dir)
            minor_dir.normalize()
            major_radius = length * 0.5
            ratio = abs(bulges[i])  # minor/major ratio
            if ratio < 1e-9:
                ratio = 1.0
            minor_radius = major_radius * ratio
            # Choose side based on bulge sign
            side = 1.0 if bulges[i] >= 0 else -1.0
            # Build rotation to align XY-plane ellipse to the target plane:
            # source basis (X=major, Y=minor, Z=normal) -> target basis
            normal = major_dir.cross(minor_dir)
            if normal.Length < 1e-12:
                normal = Vector(0, 0, 1)
            normal.normalize()
            # Rotation matrix columns = target basis vectors in source coordinates
            # Source basis: X=major, Y=minor, Z=normal
            # Target basis: major_dir, minor_dir, normal
            m = Matrix()
            m.A11 = major_dir.x; m.A12 = minor_dir.x; m.A13 = normal.x
            m.A21 = major_dir.y; m.A22 = minor_dir.y; m.A23 = normal.y
            m.A31 = major_dir.z; m.A32 = minor_dir.z; m.A33 = normal.z
            rot = Rotation(m)
            try:
                # Create ellipse in XY plane at origin with given radii
                ellipse = Part.Ellipse(Vector(0, 0, 0), major_radius, minor_radius)
                edge = ellipse.toShape()
                # Apply rotation and translation via Placement
                place = Placement(center, rot)
                edge.Placement = place
                # Trim to half-ellipse connecting a to b (parameter pi to 2pi = a to b via lower half)
                e_curve = edge.Curve
                u1 = math.pi
                u2 = 2.0 * math.pi
                trimmed = e_curve.trim(u1, u2)
                edges.append(trimmed.toShape())
            except Exception:
                edges.append(Part.LineSegment(a, b).toShape())
        elif seg_types[i] == "Rectangle":
            # Right-angled corner via intermediate point
            chord = b - a
            length = chord.Length
            if length < 1e-9:
                i += 1
                continue
            # Corner point: offset from a along chord normal by bulge * length
            axis = min(((1.0, 0.0, 0.0), abs(chord.x)),
                       ((0.0, 1.0, 0.0), abs(chord.y)),
                       ((0.0, 0.0, 1.0), abs(chord.z)),
                       key=lambda t: t[1])[0]
            n_perp = chord.cross(Vector(*axis))
            if n_perp.Length < 1e-12:
                n_perp = Vector(0, 0, 1)
            n_perp.normalize()
            # bulge: +1 = right turn, -1 = left turn, 0 = straight (no corner)
            turn = bulges[i]
            if abs(turn) < 1e-9:
                if (b - a).Length > 1e-9:
                    edges.append(Part.LineSegment(a, b).toShape())
                i += 1
                continue
            corner = a + chord * 0.5 + n_perp * (turn * length * 0.5)
            # Two edges: a->corner, corner->b
            edges.append(Part.LineSegment(a, corner).toShape())
            edges.append(Part.LineSegment(corner, b).toShape())
        else:
            raise ValueError(
                "sketch3d_wire: unknown segment type %r "
                "(want Line/Arc/Spline/Ellipse/Rectangle/Bezier)"
                % (seg_types[i],))
        i += 1
    if not edges:
        raise ValueError("sketch3d_wire: no segments produced")
    return Part.Wire(edges)


def extrude_closed_wire(edges, height):
    """SW-style 'closed region drives a boss' for a closed 3D loop:
    planar loop -> Part.Face(wire) extruded along its normal by `height`
    to a real solid. Non-planar loop -> cannot make a planar Face, so
    raise ValueError asking the caller to fall back to n_sided_patch +
    thicken_surface (a fill sheet + wall is the honest answer there)."""
    wire = edges if hasattr(edges, "Edges") else Part.Wire(edges)
    try:
        face = Part.Face(wire)
    except Exception:
        raise ValueError(
            "extrude_closed_wire: loop is not planar -- use "
            "core.n_sided_patch + core.thicken_surface instead")
    if height <= 0:
        raise ValueError("extrude_closed_wire: height must be positive")
    n = face.Surface.Axis           # plane normal
    return face.extrude(n * height)


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



def _run_clean_patch(n, label):
    import FreeCAD
    from rfstudio import core
    pts = _grid_points_from_selection(n)          # raises on bad selection
    grid = [pts[i * n:(i + 1) * n] for i in range(n)]
    flat_w = _grid_weights_from_selection(n)
    wgrid = ([flat_w[i * n:(i + 1) * n] for i in range(n)]
            if flat_w is not None else None)
    fn = core.clean_patch_44 if n == 4 else core.clean_patch_66
    doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
    obj = doc.addObject("Part::Feature", label)
    obj.Shape = fn(grid, weights=wgrid)
    doc.recompute()


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

__all__ = [
    'interpolate_curve',
    'approximate_curve',
    'control_poly_curve',
    'bezier_curve_shape',
    'clean_patch_surface',
    '_grid_weights_from_selection',
    'clean_patch_44',
    'clean_patch_66',
    '_run_clean_patch',
    'knots_from_shape',
    'polyline',
    'sketch3d_wire',
    'extrude_closed_wire',
    '_end_pos_dir',
    '_end_pos_dir_d3',
    'blend_curve',
    'nearest_ends',
    '_quintic_hermite',
]
