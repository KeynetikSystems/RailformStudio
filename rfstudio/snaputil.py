# -*- coding: utf-8 -*-
"""Shared snap/pick utilities.

Before this module existed, near-identical "collect candidate points from
every other object in the document" and "screen-space nearest point" logic
was hand-copied three times: editmode.PointEditor._snap_candidates,
editmode.KnotEditor's picking helpers, and taskpanels.CurvePanel
._collect_snap_cands. Three copies meant three places to fix the same bug.

Split deliberately into two tiers:
  * Gui-free pure functions (seg_dist, collect_snap_points) -- these only
    touch Part/FreeCAD geometry, never the 3D view, so they are exercised
    directly by the headless test suite (see test_core.py). This is the
    same Gui-free-core discipline core.py uses, applied to the editor layer.
  * Thin Gui wrappers (screen_project, nearest_snap, nearest_index) that
    need a live View3DInventor and so can only run inside FreeCAD's Gui.

Callers (editmode.py, taskpanels.py) should import this module rather than
re-implementing any of the above.
"""


def _screen(view):
    """The Qt QScreen the 3D view lives on, or None if it can't be resolved.
    Central so viewport_scale and _snap_tolerance_mm agree on the source."""
    try:
        widget = view.getWidget()
        if widget is None:
            return None
        handle = widget.windowHandle()
        if handle is None:
            return None
        return handle.screen()
    except Exception:
        return None


def viewport_scale(view):
    """The view's device-pixel ratio (physical px per logical px), or 1.0 if
    it can't be determined.

    WHY IT MATTERS FOR SNAP ACCURACY: the Coin mouse-event position and
    view.getPointOnScreen both work in DEVICE (physical) pixels, and every
    screen-distance comparison in this module is between those two, so the
    comparisons are already mutually consistent. The pixel TOLERANCE they are
    compared against must therefore ALSO be in device pixels -- a tolerance
    computed in logical pixels is off by exactly this ratio on a HiDPI
    display, shrinking (or growing) the effective snap zone and letting the
    wrong target win near the boundary. viewport_scale is the factor that
    keeps the tolerance in the same space as the distances.
    """
    screen = _screen(view)
    if screen is None:
        return 1.0
    try:
        r = float(screen.devicePixelRatio())
        return r if r > 0 else 1.0
    except Exception:
        return 1.0


def _snap_tolerance_mm(view, mm: float) -> float:
    """
    Convert a desired tolerance in millimetres to pixel units for the
    supplied view.  Uses the screen's logical DPI (Qt's devicePixelRatio)
    so that the same mm value yields the same physical size on any monitor.

    Returns the tolerance in pixels (float).  If the view or screen cannot be
    queried, falls back to a hard‑coded 18 px (the historic default).
    """
    screen = _screen(view)
    if screen is None:
        return 18.0
    try:
        # logical DPI * devicePixelRatio == physical DPI, i.e. device pixels,
        # matching the device-pixel space of the snap distance comparisons.
        dpi = screen.logicalDotsPerInch()
        px_per_mm = dpi / 25.4
        return mm * px_per_mm * viewport_scale(view)
    except Exception:
        # Fallback – preserves the old behaviour if anything goes wrong.
        return 18.0   # you can change this fallback to whatever you like


# ---- pure geometry (headless-testable) ------------------------------------ #
def seg_dist(p, a, b):
    """Distance from 2D screen point p=(x,y) to the 2D segment a-b."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _edge_knot_points(edge, tol=1e-9):
    """Points on `edge` at each of its own UNIQUE knot parameters --
    i.e. the actual knot locations of the underlying BSplineCurve, not
    an arc-length resample that only coincidentally lands near one.

    Returns [] for a non-BSpline edge (lines, arcs, ...) or any edge
    whose Curve/KnotSequence can't be read -- callers should treat this
    the same as "no extra candidates from this edge", not an error.
    """
    import Part
    c = getattr(edge, "Curve", None)
    if not isinstance(c, Part.BSplineCurve):
        return []
    try:
        fp, lp = edge.FirstParameter, edge.LastParameter
        seq = list(c.KnotSequence)     # multiplicity-expanded
    except Exception:
        return []
    out = []
    last = None
    for k in seq:
        if last is not None and abs(k - last) <= tol:
            continue                   # dedupe repeated (multiplicity) knots
        last = k
        if fp - tol <= k <= lp + tol:
            try:
                out.append(edge.valueAt(min(max(k, fp), lp)))
            except Exception:
                pass
    return out


def _is_container(o):
    """True for group/container objects (App::DocumentObjectGroup, App::Part).

    Their Shape is a COMPOUND of their children, which are already visited
    individually -- so iterating them duplicates every candidate and, worse,
    re-introduces the geometry of the very object passed as `exclude` (the one
    being edited). That made a drag snap onto the point it was dragging, which
    read as "snapping is broken / the point won't snap to another curve".
    """
    for t in ("App::DocumentObjectGroup", "App::Part"):
        try:
            if o.isDerivedFrom(t):
                return True
        except Exception:
            pass
    return getattr(o, "Group", None) is not None


def _all_objects(doc, exclude=None):
    """Yield all objects in the document and its containers, excluding `exclude`."""
    visited = set()
    stack = list(doc.Objects)
    while stack:
        o = stack.pop()
        if o is None or o.ID in visited:
            continue
        visited.add(o.ID)
        if o is exclude:
            continue
        yield o
        # If o is a container, add its children to the stack
        if _is_container(o):
            # Get the group of the container
            group = getattr(o, "Group", None)
            if group is not None:
                for child in group:
                    if child is not None:
                        stack.append(child)


def collect_snap_points(doc, exclude=None, sample_step=3.0,
                        sample_bounds=(8, 60), include_knots=True,
                        include_samples=False, include_control=True):
    """EXACT discrete snap targets from every other object in `doc`
    (including objects inside groups and parts): their control points
    (Points property), shape vertices, and -- when `include_knots` is True
    (the default) -- every unique knot parameter of any BSpline edge.

    `include_samples` (default False) adds ~1 arc-length sample per
    `sample_step` world-units (capped to `sample_bounds`) along each edge.
    These are OFF by default because they are a lower-accuracy proxy: the
    exact ray->edge projection (nearest_point_on_shapes_to_ray, used by
    nearest_exact_snap) already snaps to ANY point along a curve's real
    geometry precisely, so a coarse arc-length sample can only ever pull a
    snap ~sample_step/2 off the true curve where the exact path would have
    landed on it. Everything this function returns by default is therefore
    a mathematically exact point (a vertex, a control point, or a knot),
    which is exactly the set that should win a snap over a generic on-edge
    projection (see nearest_snap_ranked). Turn samples back on only for a
    caller that deliberately wants the old coarse behaviour.

    `exclude` is normally the object currently being edited. Gui-free --
    only reads Shape/Points -- so this is covered by test_core.py rather
    than relying on manual GUI testing.
    """
    pts = []
    lo, hi = sample_bounds
    for o in _all_objects(doc, exclude):
        if include_control:
            op = getattr(o, "Points", None)
            if op:
                pts.extend(list(op))
        shp = getattr(o, "Shape", None)
        if not shp:
            continue
        for v in shp.Vertexes:
            pts.append(v.Point)
        for e in shp.Edges:
            if include_samples:
                try:
                    n = max(lo, min(hi, int(e.Length / sample_step)))
                    fp, lp = e.FirstParameter, e.LastParameter
                    span = lp - fp
                    for k in range(1, n):
                        pts.append(e.valueAt(fp + span * k / n))
                except Exception:
                    pass
            if include_knots:
                pts.extend(_edge_knot_points(e))
    return pts


def collect_control_points(doc, exclude=None):
    """Just the off-curve CONTROL-polygon points (the `Points` property) of
    every other object -- a ControlCurve's handles, which generally do NOT
    lie on the curve itself. Kept separate from collect_snap_points' on-
    geometry targets because the ranker treats control points as a WEAK tier
    (SNAP_CONTROL): they can still snap, but only when genuinely nearest, so
    they no longer out-prioritise contact with a curve body. Gui-free."""
    pts = []
    for o in _all_objects(doc, exclude):
        op = getattr(o, "Points", None)
        if op:
            pts.extend(list(op))
    return pts


def collect_ranked_points(doc, exclude=None):
    """(strong, weak) snap candidates for the non-kinded nearest_snap_ranked
    callers (the authoring panels). `strong` are the magnet-eligible EXACT
    on-geometry targets -- vertices, knots and true curve-curve intersections;
    `weak` are the off-curve control-polygon points. Splitting them is what
    lets nearest_snap_ranked keep control points as a distance-only tier so a
    drawn curve end can make contact with another curve's body instead of
    being grabbed by a nearby control handle. Gui-free -- headless-testable."""
    strong = collect_snap_points(doc, exclude=exclude, include_control=False)
    shapes = [e for e, _o in collect_snap_shapes(doc, exclude=exclude)]
    strong += collect_intersection_points(shapes)
    weak = collect_control_points(doc, exclude=exclude)
    return strong, weak


def _valid_dist_shape(shape):
    """False for shapes that must never reach OCCT distToShape: null
    shapes, edges with no readable curve, and zero-length edges.
    BRepExtrema segfaults (rather than raising) on such inputs, and the
    per-mousemove snap loop calls distToShape on every collected edge --
    so a single degenerate edge anywhere in the document crashed every
    cursor move with 'Access violation' while Snap was on. The panel's
    try/except cannot catch a segfault; filtering once here can."""
    if shape is None:
        return False
    try:
        if shape.isNull():
            return False
    except Exception:
        return False
    try:
        if shape.ShapeType == "Edge":
            try:
                if shape.Length < 1e-9:
                    return False
            except Exception:
                return False
            try:
                if getattr(shape, "Curve", None) is None:
                    return False
            except Exception:
                return False
    except Exception:
        pass
    return True


def collect_snap_shapes(doc, exclude=None):
    """[(edge, owning_object), ...] for every edge of every other object in
    `doc` (including objects inside groups and parts) -- the SHAPE-level
    candidate list nearest_point_on_shapes projects onto exactly (via
    OCCT's own nearest-point-on-shape query), as opposed to
    collect_snap_points' discrete list of vertex/control-point/sample/knot
    Vectors. Two different snap strategies, kept as two separate
    collectors:
      * collect_snap_points  -> snap onto one of a fixed set of points
                               (control points included, even where they
                               aren't ON the curve itself, e.g. a
                               ControlCurve's control polygon).
      * collect_snap_shapes  -> snap onto ANY point along a curve's
                               actual geometry (perpendicular projection),
                               not limited to samples/knots/vertices.

    `exclude` is normally the object currently being edited. Gui-free --
    only reads Shape -- so this is covered by test_core.py rather than
    relying on manual GUI testing.
    """
    out = []
    for o in _all_objects(doc, exclude):
        shp = getattr(o, "Shape", None)
        if not shp:
            continue
        try:
            edges = shp.Edges
        except Exception:
            continue
        for e in edges:
            # Drop degenerate edges at collection time so the
            # per-mousemove distToShape loop never sees them (see
            # _valid_dist_shape: OCCT segfaults on them instead of
            # raising, which try/except around the call cannot catch).
            if _valid_dist_shape(e):
                out.append((e, o))
    return out


def collect_snap_faces(doc, exclude=None):
    """[face, ...] for every face of every other object in `doc` (groups and
    parts included) -- the SURFACE-projection snap candidates. OCCT's
    distToShape treats a Face exactly like an Edge, so
    nearest_point_on_shapes_to_ray projects the cursor pick ray onto a
    surface the same way it projects onto a curve. This is what lets a
    dragged point land ON a surface (not only on curves), which is the point
    of authoring section/feature curves that actually lie on a reference
    skin. Gated upstream by the 'surface' snap family (off by default -- it
    is a heavier scan than the curve families).

    `exclude` is normally the object being edited. Gui-free -- only reads
    Shape -- so this is covered by test_core.py, not manual GUI testing.
    """
    out = []
    for o in _all_objects(doc, exclude):
        shp = getattr(o, "Shape", None)
        if not shp:
            continue
        try:
            faces = shp.Faces
        except Exception:
            continue
        for f in faces:
            # _valid_dist_shape only special-cases Edge; a non-null Face
            # passes, which is what we want (OCCT distToShape is safe on
            # the trimmed B-spline faces this workbench produces).
            if _valid_dist_shape(f):
                out.append(f)
    return out


def _bbox_overlap(b1, b2, pad):
    """True if two Part BoundBoxes overlap once each is grown by `pad` --
    a cheap broad-phase reject so the O(n^2) intersection scan only runs
    distToShape on edge pairs that could actually meet."""
    return (b1.XMin - pad <= b2.XMax + pad and b2.XMin - pad <= b1.XMax + pad and
            b1.YMin - pad <= b2.YMax + pad and b2.YMin - pad <= b1.YMax + pad and
            b1.ZMin - pad <= b2.ZMax + pad and b2.ZMin - pad <= b1.ZMax + pad)


def _intersection_candidate_pairs(boxes, tol):
    """Index edge bounding boxes into a uniform spatial grid and yield only
    the (i, j) pairs whose padded boxes share a grid cell -- the broad phase
    that lets the crossing scan scale past the old flat O(n^2) pair loop.

    This is what removes the hard `max_edges` cliff in
    collect_intersection_points: on a real patch network the edges are spread
    out, so each cell holds only a handful, and the number of pairs actually
    distance-tested grows ~linearly with the edge count instead of
    quadratically. Returns a set of (i, j) with i < j. Gui-free."""
    valid = [(i, b) for i, b in enumerate(boxes) if b is not None]
    if len(valid) < 2:
        return set()
    # Overall extent + a cell size that targets ~a few edges per cell.
    xs = [b.XMin for _i, b in valid] + [b.XMax for _i, b in valid]
    ys = [b.YMin for _i, b in valid] + [b.YMax for _i, b in valid]
    zs = [b.ZMin for _i, b in valid] + [b.ZMax for _i, b in valid]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-9)
    import math as _math
    cells_per_axis = max(1, int(_math.ceil(len(valid) ** (1.0 / 3.0))))
    cell = max(span / cells_per_axis, 1e-9)
    ox, oy, oz = min(xs), min(ys), min(zs)

    def _cells(b):
        i0 = int((b.XMin - tol - ox) / cell); i1 = int((b.XMax + tol - ox) / cell)
        j0 = int((b.YMin - tol - oy) / cell); j1 = int((b.YMax + tol - oy) / cell)
        k0 = int((b.ZMin - tol - oz) / cell); k1 = int((b.ZMax + tol - oz) / cell)
        for ci in range(i0, i1 + 1):
            for cj in range(j0, j1 + 1):
                for ck in range(k0, k1 + 1):
                    yield (ci, cj, ck)

    grid = {}
    for i, b in valid:
        for c in _cells(b):
            grid.setdefault(c, []).append(i)
    pairs = set()
    for members in grid.values():
        if len(members) < 2:
            continue
        for a in range(len(members)):
            for bb in range(a + 1, len(members)):
                i, j = members[a], members[bb]
                pairs.add((i, j) if i < j else (j, i))
    return pairs


def collect_intersection_points(shapes, tol=1e-6, max_edges=600):
    """True crossing points of the candidate edges -- where two curves
    actually meet in 3D (distToShape <= `tol`), NOT where they merely
    appear to cross on screen. Each such crossing is a semantically exact
    snap target (the CAD counterpart of Blender's vertex snap), so these
    join the exact discrete candidates that win over an on-edge projection.

    Cost control: a uniform spatial-grid broad phase
    (_intersection_candidate_pairs) so only edges that share a cell get
    distance-tested -- this scales ~linearly with edge count on a spread-out
    patch network, where the old flat O(n^2) scan was skipped entirely above
    60 edges (so a real model lost intersection snapping altogether). A small
    edge count still uses the direct padded-bbox scan (no grid overhead);
    `max_edges` is now only a last-resort safety ceiling, much higher.
    Gui-free -- headless-testable.
    """
    edges = list(shapes)
    if len(edges) < 2 or len(edges) > max_edges:
        return []
    boxes = []
    for e in edges:
        try:
            boxes.append(e.BoundBox)
        except Exception:
            boxes.append(None)

    # Small n: the flat padded-bbox scan is cheaper than building a grid.
    if len(edges) <= 40:
        pairs = [(i, j) for i in range(len(edges))
                 for j in range(i + 1, len(edges))]
    else:
        pairs = _intersection_candidate_pairs(boxes, tol)

    out = []
    for i, j in pairs:
        bi, bj = boxes[i], boxes[j]
        if bi is None or bj is None or not _bbox_overlap(bi, bj, tol):
            continue
        if not _valid_dist_shape(edges[i]) or not _valid_dist_shape(edges[j]):
            continue
        try:
            d, pts_on, _info = edges[i].distToShape(edges[j])
        except Exception:
            continue
        if d <= tol and pts_on:
            out.append(pts_on[0][0])
    return out


def collect_strong_snap_points(doc, exclude=None):
    """The full set of EXACT, high-priority discrete snap targets: vertices,
    control points and knots (collect_snap_points, samples off) PLUS true
    curve-curve intersections (collect_intersection_points). This is the
    candidate set nearest_snap_ranked prefers over any on-edge projection,
    so a snap lands on a real vertex / shared corner / crossing rather than
    a near-point a fraction of a pixel away along some edge.

    Gui-free -- headless-testable.
    """
    pts = collect_snap_points(doc, exclude=exclude)
    shapes = [e for e, _o in collect_snap_shapes(doc, exclude=exclude)]
    pts += collect_intersection_points(shapes)
    return pts


def nearest_point_on_shapes(world_point, shapes, max_dist=None):
    """Exact nearest point to `world_point` across `shapes` (an iterable
    of Part.Shape/Edge/Face, e.g. from collect_snap_shapes), via OCCT's
    Vertex.distToShape -- the same exact nearest-point primitive
    core.py's project_wire_onto_face / surface_deviation already rely on
    elsewhere in this workbench. This is a true PERPENDICULAR PROJECTION
    onto the shape's actual geometry, not an approximation bounded by a
    fixed sampling resolution the way collect_snap_points' arc-length/
    knot candidates are.

    Returns (point, distance, shape) for whichever shape has the
    globally closest point, or (None, None, None) if `shapes` is empty
    or every distToShape call failed. `max_dist`, if given, discards a
    match farther than that many world units -- returns (None, None,
    None) rather than a distant "closest anyway" result.

    Gui-free (only touches Part) -- headless-testable, same as the rest
    of this module's pure-geometry tier.
    """
    import Part
    best_pt, best_d, best_shape = None, 1e18, None
    for shp in shapes:
        if not _valid_dist_shape(shp):
            continue
        try:
            d, pts_on, _info = Part.Vertex(world_point).distToShape(shp)
        except Exception:
            continue
        if d < best_d:
            best_d = d
            best_pt = pts_on[0][1] if pts_on else None
            best_shape = shp
    if best_pt is None:
        return None, None, None
    if max_dist is not None and best_d > max_dist:
        return None, None, None
    return best_pt, best_d, best_shape


def _bounded_ray_half_len(origin, shapes, fallback):
    """Half-length for the stand-in ray/axis segment used by the
    distToShape snap queries below, bounded by the candidates'
    combined bounding box (eye-to-box distance + box span) instead of
    the raw `fallback` (default 1e7). A 2e7-unit edge forces
    BRepExtrema_DistShapeShape over an enormous parameter range --
    slow, numerically fragile, and a segfault vector on awkward
    geometry -- while the segment only ever needs to span the model
    as seen from the ray origin. Returns `fallback` unchanged when no
    usable box exists. `origin` is a FreeCAD Vector."""
    try:
        lo = [1e100, 1e100, 1e100]
        hi = [-1e100, -1e100, -1e100]
        found = False
        for s in shapes:
            try:
                b = s.BoundBox
            except Exception:
                continue
            try:
                lo[0] = min(lo[0], b.XMin)
                lo[1] = min(lo[1], b.YMin)
                lo[2] = min(lo[2], b.ZMin)
                hi[0] = max(hi[0], b.XMax)
                hi[1] = max(hi[1], b.YMax)
                hi[2] = max(hi[2], b.ZMax)
                found = True
            except Exception:
                continue
        if not found:
            return fallback
        cx = (lo[0] + hi[0]) / 2.0
        cy = (lo[1] + hi[1]) / 2.0
        cz = (lo[2] + hi[2]) / 2.0
        span = ((hi[0] - lo[0]) ** 2 + (hi[1] - lo[1]) ** 2 +
                (hi[2] - lo[2]) ** 2) ** 0.5
        dist = ((origin.x - cx) ** 2 + (origin.y - cy) ** 2 +
                (origin.z - cz) ** 2) ** 0.5
        return dist + span + 1.0
    except Exception:
        return fallback


def nearest_point_on_shapes_to_ray(ray_origin, ray_dir, shapes,
                                   max_dist=None, half_len=1e7):
    """Exact nearest point on `shapes` to the PICK RAY (the line through
    `ray_origin` in direction `ray_dir`), rather than to a single world
    point.

    This is the accuracy-correct core of cursor snapping. The old approach
    projected the cursor to ONE world point via view.getPoint -- which
    lands on the camera focal plane -- and asked for the nearest point on
    the curve to THAT. Unless the curve lies on the focal plane, the
    nearest point on the curve to a focal-plane point is NOT the nearest
    point on the curve to the actual pick ray, so the snap drifted along
    the curve by an amount that depended on the curve's depth and shifted
    as the user orbited. Measuring distance to the ray-as-a-line removes
    that error: the result is where the curve actually passes closest to
    what the cursor is pointing at, independent of view depth.

    Implemented with the same exact OCCT primitive nearest_point_on_shapes
    uses -- Shape.distToShape -- against a long line segment standing in
    for the (infinite) ray. The segment spans the candidates' bounding
    box as seen from the ray origin (see _bounded_ray_half_len); the
    `half_len` argument is only a fallback when no usable box exists.

    Returns (point_on_shape, distance, shape) for the globally closest
    shape, or (None, None, None) if `shapes` is empty, `ray_dir` is
    degenerate, or every distToShape failed. `max_dist`, if given, rejects
    a match whose perpendicular distance to the ray exceeds it.

    Gui-free (only touches Part / FreeCAD) -- headless-testable, same as
    the rest of this module's pure-geometry tier.
    """
    import Part
    from FreeCAD import Vector
    d = Vector(ray_dir)
    if d.Length < 1e-12:
        return None, None, None
    d.normalize()
    o = Vector(ray_origin)
    shapes = list(shapes)
    if not shapes:
        return None, None, None
    half_len = _bounded_ray_half_len(o, shapes, half_len)
    try:
        ray = Part.Edge(Part.LineSegment(o - d * half_len, o + d * half_len))
    except Exception:
        return None, None, None
    best_pt, best_d, best_shape = None, 1e18, None
    for shp in shapes:
        if not _valid_dist_shape(shp):
            continue
        try:
            dist, pts_on, _info = ray.distToShape(shp)
        except Exception:
            continue
        if dist < best_d:
            best_d = dist
            # distToShape pairs are (point_on_ray, point_on_shape)
            best_pt = pts_on[0][1] if pts_on else None
            best_shape = shp
    if best_pt is None:
        return None, None, None
    if max_dist is not None and best_d > max_dist:
        return None, None, None
    return best_pt, best_d, best_shape


def closest_point_on_axis_to_shapes(axis_origin, axis_dir, shapes,
                                    max_dist=None, half_len=1e7):
    """The point ON the axis line (through `axis_origin`, direction
    `axis_dir`) where the line passes closest to any of `shapes`, plus that
    closest distance and the winning shape.

    This is the counterpart to nearest_point_on_shapes_to_ray for
    axis-locked dragging: there the point must stay on the drag axis, so we
    want the position ALONG THE AXIS at which a target curve is nearest --
    i.e. where the axis crosses (or comes closest to) the curve -- rather
    than the point on the curve itself. The stand-in axis segment spans
    the candidates' bounding box as seen from the axis origin (see
    _bounded_ray_half_len); `half_len` is only a fallback. Returns
    (point_on_axis, distance, shape), or (None, None, None) if `shapes`
    is empty / the direction is degenerate / every distToShape failed.
    `max_dist` rejects a target the axis never passes within that many
    world units of.

    Gui-free -- headless-testable, same as the rest of this tier.
    """
    import Part
    from FreeCAD import Vector
    d = Vector(axis_dir)
    if d.Length < 1e-12:
        return None, None, None
    d.normalize()
    o = Vector(axis_origin)
    shapes = list(shapes)
    if not shapes:
        return None, None, None
    half_len = _bounded_ray_half_len(o, shapes, half_len)
    try:
        axis = Part.Edge(Part.LineSegment(o - d * half_len, o + d * half_len))
    except Exception:
        return None, None, None
    best_pt, best_d, best_shape = None, 1e18, None
    for shp in shapes:
        if not _valid_dist_shape(shp):
            continue
        try:
            dist, pts_on, _info = axis.distToShape(shp)
        except Exception:
            continue
        if dist < best_d:
            best_d = dist
            best_pt = pts_on[0][0] if pts_on else None   # point on the AXIS
            best_shape = shp
    if best_pt is None:
        return None, None, None
    if max_dist is not None and best_d > max_dist:
        return None, None, None
    return best_pt, best_d, best_shape


# ---- GUI-only thin wrappers (need a live View3DInventor) ------------------ #
def pick_ray(view, pos):
    """(origin, direction) of the cursor pick ray at screen `pos`, or None.

    The ray is what the user is really pointing at, and snapping to it
    (see nearest_point_on_shapes_to_ray) is what makes snaps land
    accurately regardless of how far the target sits from the focal plane.

    Handles both camera types:
      * Perspective  -- rays fan out from the eye, so the ray passes
        through the camera position and the focal-plane point under the
        cursor; direction = (focal_point - eye).
      * Orthographic -- rays are parallel to the view direction, so the
        ray is the view direction through the focal-plane point.

    Defensive like the rest of this wrapper tier: any missing/renamed
    view method just yields None (caller falls back to discrete snapping),
    never an exception into the drag handler.
    """
    from FreeCAD import Vector
    try:
        focal = view.getPoint(pos[0], pos[1])
    except Exception:
        return None
    if focal is None:
        return None
    focal = Vector(focal)
    # perspective: use the true eye ray
    try:
        if view.getCameraType() == "Perspective":
            node = view.getCameraNode()
            ep = node.position.getValue().getValue()   # SbVec3f -> (x,y,z)
            eye = Vector(ep[0], ep[1], ep[2])
            d = focal.sub(eye)
            if d.Length > 1e-12:
                return eye, d.normalize()
    except Exception:
        pass
    # orthographic (or perspective fallback): view direction through focal pt
    try:
        vd = Vector(view.getViewDirection())
        if vd.Length > 1e-12:
            return focal, vd.normalize()
    except Exception:
        pass
    return None



def screen_project(view, vec):
    """vec (Vector, or x/y/z attrs) -> (sx, sy) screen position, or None."""
    try:
        return view.getPointOnScreen(vec)
    except Exception:
        try:
            return view.getPointOnScreen(vec.x, vec.y, vec.z)
        except Exception:
            return None


def nearest_exact_snap(view, shapes, pos, max_px, max_world_dist=None):
    """Cursor-driven EXACT projection snap onto real curve geometry -- the
    shape-based counterpart to nearest_snap (which only picks among a
    fixed list of candidate points).

    Snaps to the cursor PICK RAY, not to a single focal-plane point: it
    builds the ray at `pos` (see pick_ray) and finds the point on `shapes`
    (normally the edges from collect_snap_shapes) where the geometry passes
    closest to that ray (see nearest_point_on_shapes_to_ray). This is what
    makes the snap accurate -- the previous implementation projected the
    cursor onto the camera focal plane via view.getPoint and measured
    nearest-point to THAT, so the result drifted along the curve by an
    amount that depended on the curve's depth and changed as the user
    orbited. Measuring against the ray removes that depth error entirely.

    The match is then accepted only if its OWN screen position lands within
    `max_px` of the cursor -- the same screen-tolerance convention
    nearest_snap applies to its discrete candidates, so the two behave
    consistently (a snap can't grab something that looks far away on screen
    just because it is geometrically close in world units, e.g. two curves
    overlapping in depth from the current view).

    Returns the exact snapped Vector, or None if nothing is within
    tolerance, `shapes` is empty, or the view can't resolve a pick ray at
    `pos`.
    """
    pt, _dpx = _exact_snap_detail(view, shapes, pos, max_px,
                                  max_world_dist=max_world_dist)
    return pt


def _exact_snap_detail(view, shapes, pos, max_px, max_world_dist=None):
    """nearest_exact_snap's core, but returns (point, screen_px_distance) so
    a caller can compare two different shape sets (e.g. edges vs faces) and
    keep whichever projects nearer on screen. Returns (None, None) when
    nothing is within `max_px`, `shapes` is empty, or no pick ray resolves."""
    if not shapes:
        return None, None
    ray = pick_ray(view, pos)
    if ray is None:
        return None, None
    origin, direction = ray
    pt, _d, _shape = nearest_point_on_shapes_to_ray(
        origin, direction, shapes, max_dist=max_world_dist)
    if pt is None:
        return None, None
    sp = screen_project(view, pt)
    if sp is None:
        return None, None
    dpx = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
    return (pt, dpx) if dpx <= max_px else (None, None)


def nearest_axis_snap(view, axis_origin, axis_dir, shapes, pos, max_px,
                      max_world_dist=None):
    """Axis-locked snap: while a point is being dragged along the line
    (`axis_origin`, `axis_dir`), return the position ON THAT LINE where a
    target curve crosses/comes closest -- but only once the crossing itself
    has come within `max_px` of the cursor on screen, so the snap engages
    as the user drags up to it rather than teleporting the point to a
    distant crossing the moment the drag starts.

    The returned point lies exactly on the drag axis, so the axis
    constraint is preserved -- this snaps WITHIN the constraint, it does
    not break it. Returns the snapped Vector on the axis, or None.
    """
    if not shapes:
        return None
    pt, _d, _shape = closest_point_on_axis_to_shapes(
        axis_origin, axis_dir, shapes, max_dist=max_world_dist)
    if pt is None:
        return None
    sp = screen_project(view, pt)
    if sp is None:
        return None
    dpx = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
    return pt if dpx <= max_px else None


def nearest_snap(view, points, pos, max_px):
    """Nearest of `points` (world Vectors) to screen `pos`, within
    `max_px` screen pixels, or None. Used for cursor-driven snap-while-
    dragging in both PointEditor and CurvePanel's 3D pick mode.

    NB: this function's `def` line had been deleted, orphaning its body as
    unreachable code inside nearest_exact_snap. The module still imported
    cleanly, but every caller hit AttributeError -- swallowed by _on_move's
    blanket except -- so the discrete-snap fallback (and the whole drag-move
    handler) silently died. Keep the signature."""
    best, bestd = None, max_px
    for p in points:
        sp = screen_project(view, p)
        if sp is None:
            continue
        d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
        if d < bestd:
            best, bestd = p, d
    return best


def nearest_snap_ranked(view, strong_points, edge_shapes, pos, max_px,
                        max_world_dist=None, weak_points=None,
                        magnet_fraction=None):
    """Magnet-tiered cursor snap (Vector result; see nearest_snap_ranked_kind
    for the family-reporting variant the PointEditor uses). `strong_points`
    are the magnet-eligible EXACT on-geometry targets (vertices, knots, true
    intersections -- collect_ranked_points' first element); `weak_points` are
    the off-curve control-polygon handles (its second element), ranked
    distance-only; `edge_shapes` the on-curve projection candidates.

    Two passes:
      1. A strong target within the inner magnet radius (magnet_fraction *
         max_px) wins outright -- endpoints/crossings stay sticky.
      2. Otherwise the nearest of {strong, weak, on-curve projection} within
         `max_px` wins. Aiming at a curve body makes the projection ~0 px
         away, so it beats a nearby control handle -- the fix for a drawn
         curve end that couldn't make contact with another curve.

    Returns the snapped Vector, or None.
    """
    if magnet_fraction is None:
        magnet_fraction = MAGNET_FRACTION
    magnet_px = max_px * magnet_fraction
    # Pass 1 -- inner magnet: a strong on-geometry target is sticky.
    s = nearest_snap(view, strong_points, pos, magnet_px)
    if s is not None:
        return s
    # Pass 2 -- outer annulus: nearest across strong + weak + projection.
    best, bestd = None, max_px
    for p in list(strong_points) + list(weak_points or []):
        sp = screen_project(view, p)
        if sp is None:
            continue
        d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
        if d < bestd:
            best, bestd = p, d
    ept, epx = _exact_snap_detail(view, edge_shapes, pos, max_px,
                                  max_world_dist=max_world_dist)
    if ept is not None and (best is None or epx < bestd):
        best = ept
    return best


# ---- kinded snapping (which snap FAMILY won, for visible feedback) -------- #
# The three families nearest_snap_ranked distinguishes, as plain string tags
# so a caller can colour/glyph the snap indicator by kind and gate collection
# with rfstudio.snapsettings. These MUST match snapsettings.KINDS.
SNAP_VERTEX = "vertex"            # shape vertices + B-spline knots (ON geometry)
SNAP_INTERSECTION = "intersection"   # true curve-curve crossings
SNAP_EDGE = "edge"               # perpendicular projection onto a curve
SNAP_MIDPOINT = "midpoint"        # arc-length midpoint of a curve/edge
SNAP_CENTER = "center"            # centre of an arc / circle / ellipse edge
SNAP_SURFACE = "surface"          # projection onto any point of a FACE
SNAP_CONTROL = "control"          # off-curve control-polygon point (WEAK tier)
# Constraint families -- relative to a drag ANCHOR (the neighbouring point of
# the one being moved), not just the cursor position:
SNAP_PERPENDICULAR = "perpendicular"  # foot of the perpendicular from anchor
SNAP_TANGENT = "tangent"          # point where anchor->P is tangent to a curve

# --- ranking policy -------------------------------------------------------- #
# The families that get the "magnet" privilege: EXACT points that lie on real
# geometry and are meaningful landing targets. A magnet-eligible target within
# the inner magnet radius always wins; everything else (control points, on-
# curve/on-surface projection) competes on true screen distance. Control
# points are deliberately NOT here -- they are off the curve, so letting them
# win the whole tolerance circle is exactly what stopped a drag from making
# contact with a curve body ("can't ask for land on this curve").
MAGNET_KINDS = frozenset((SNAP_VERTEX, SNAP_INTERSECTION, SNAP_MIDPOINT,
                          SNAP_CENTER, SNAP_PERPENDICULAR, SNAP_TANGENT))
# Inner radius, as a fraction of the pixel tolerance, in which a magnet target
# is "sticky". Outside it, the nearest thing wins -- so the outer annulus of
# the tolerance circle is reachable for on-curve / on-surface contact.
MAGNET_FRACTION = 0.5


def _nearest_kinded(view, kinded_points, pos, max_px, kinds=None):
    """(point, kind, screen_dist) nearest to `pos` within `max_px`, optionally
    restricted to `kinds`. screen_dist is max_px when nothing qualifies (so a
    caller can compare a projection against it directly). Gui wrapper."""
    best, bestd, bestk = None, max_px, None
    for p, k in kinded_points:
        if kinds is not None and k not in kinds:
            continue
        sp = screen_project(view, p)
        if sp is None:
            continue
        d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
        if d < bestd:
            best, bestd, bestk = p, d, k
    return best, bestk, bestd


def collect_feature_points_kinded(doc, exclude=None, kinds=None):
    """Derived EXACT discrete targets that aren't vertices/knots but are
    geometrically meaningful: the arc-length MIDPOINT of every edge
    (SNAP_MIDPOINT) and the CENTRE of every arc/circle/ellipse edge
    (SNAP_CENTER). These are the cheap, high-value snaps Draft exposes --
    halving a span or centring on a radius without first drawing a
    construction point.

    `kinds`, if given, limits which of the two families is collected; None
    collects both. Gui-free -- only reads Shape/Curve -- headless-testable.
    """
    import Part
    want_mid = kinds is None or SNAP_MIDPOINT in kinds
    want_ctr = kinds is None or SNAP_CENTER in kinds
    if not (want_mid or want_ctr):
        return []
    out = []
    for o in _all_objects(doc, exclude):
        shp = getattr(o, "Shape", None)
        if not shp:
            continue
        try:
            edges = shp.Edges
        except Exception:
            continue
        for e in edges:
            if not _valid_dist_shape(e):
                continue
            if want_mid:
                try:
                    # Arc-length midpoint (getParameterByLength), so a
                    # non-uniform B-spline's midpoint is the geometric
                    # half-way point, not the parametric one. Fall back to
                    # the parameter midpoint if length mapping is refused.
                    try:
                        m = e.getParameterByLength(e.Length / 2.0)
                    except Exception:
                        m = 0.5 * (e.FirstParameter + e.LastParameter)
                    out.append((e.valueAt(m), SNAP_MIDPOINT))
                except Exception:
                    pass
            if want_ctr:
                c = getattr(e, "Curve", None)
                if isinstance(c, (Part.Circle, Part.Ellipse)):
                    try:
                        out.append((c.Center, SNAP_CENTER))
                    except Exception:
                        pass
    return out


def perpendicular_points_from(anchor, edges, n=96, tol=1e-9):
    """Feet of the perpendicular dropped from `anchor` (a Vector) onto each
    edge: the points P where (P - anchor) is perpendicular to the edge's
    tangent. Dragging the moving end to such a P makes the segment
    anchor->P meet the target curve at a right angle -- the perpendicular
    constraint, without a datum line.

    All feet are found (a wavy curve can have several), not just the nearest:
    g(t) = (P(t) - anchor) . T(t) is sampled along the curve and every sign
    change is bisected to a root. Feet essentially AT an endpoint are dropped
    (those are already SNAP_VERTEX targets and aren't a genuine right angle).
    Returns [Vector, ...]. Gui-free -- headless-testable.
    """
    from FreeCAD import Vector
    a = Vector(anchor)
    out = []
    for e in edges:
        if not _valid_dist_shape(e):
            continue
        try:
            fp, lp = e.FirstParameter, e.LastParameter
        except Exception:
            continue
        span = lp - fp
        if span <= tol:
            continue

        def g(t):
            P = e.valueAt(t)
            T = e.tangentAt(t)
            return (P - a).dot(T)

        try:
            prev_t, prev = fp, g(fp)
        except Exception:
            continue
        for i in range(1, n + 1):
            t = fp + span * i / n
            try:
                cur = g(t)
            except Exception:
                prev_t, prev = t, None
                continue
            if prev is not None and prev != 0.0 and (prev < 0.0) != (cur < 0.0):
                lo, hi, glo = prev_t, t, prev
                for _ in range(40):
                    mid = 0.5 * (lo + hi)
                    try:
                        gm = g(mid)
                    except Exception:
                        break
                    if (glo < 0.0) != (gm < 0.0):
                        hi = mid
                    else:
                        lo, glo = mid, gm
                root = 0.5 * (lo + hi)
                if root - fp > span * 1e-3 and lp - root > span * 1e-3:
                    try:
                        out.append(e.valueAt(root))
                    except Exception:
                        pass
            prev_t, prev = t, cur
    return out


def tangent_points_from(anchor, edges, n=96, tol_deg=1.0):
    """Points P on each edge where the line anchor->P is TANGENT to the curve
    -- i.e. (P - anchor) is parallel to the curve tangent there. Dragging the
    moving end to such a P makes the span leave the target curve tangentially
    (a G1-looking join without a construction tangent).

    The residual r(t) = |（P-anchor) x T| / (|P-anchor| |T|) is sin(angle)
    between the chord and the tangent; its local minima are found on a sampled
    grid and golden-section refined, and a minimum is accepted only when
    r <= sin(tol_deg). Returns [Vector, ...]. Gui-free -- headless-testable.
    """
    import math
    from FreeCAD import Vector
    a = Vector(anchor)
    thresh = math.sin(math.radians(tol_deg))
    out = []
    for e in edges:
        if not _valid_dist_shape(e):
            continue
        try:
            fp, lp = e.FirstParameter, e.LastParameter
        except Exception:
            continue
        span = lp - fp
        if span <= 1e-12:
            continue

        def r(t):
            P = e.valueAt(t)
            T = e.tangentAt(t)
            v = P - a
            vl = v.Length
            tl = T.Length
            if vl < 1e-9 or tl < 1e-9:
                return None
            return v.cross(T).Length / (vl * tl)

        ts = [fp + span * i / n for i in range(n + 1)]
        rs = []
        for t in ts:
            try:
                rs.append(r(t))
            except Exception:
                rs.append(None)
        for i in range(1, n):
            a0, a1, a2 = rs[i - 1], rs[i], rs[i + 1]
            if a0 is None or a1 is None or a2 is None:
                continue
            if a1 <= a0 and a1 <= a2:                 # interior local minimum
                lo, hi = ts[i - 1], ts[i + 1]
                ok = True
                for _ in range(40):
                    m1 = lo + 0.382 * (hi - lo)
                    m2 = lo + 0.618 * (hi - lo)
                    r1, r2 = r(m1), r(m2)
                    if r1 is None or r2 is None:
                        ok = False
                        break
                    if r1 < r2:
                        hi = m2
                    else:
                        lo = m1
                if not ok:
                    continue
                tm = 0.5 * (lo + hi)
                rm = r(tm)
                if rm is not None and rm <= thresh:
                    try:
                        out.append(e.valueAt(tm))
                    except Exception:
                        pass
    return out


def collect_constraint_snap_points_kinded(doc, anchor, exclude=None,
                                           kinds=None, edges=None):
    """Anchor-relative constraint snaps as a kinded list: perpendicular feet
    (SNAP_PERPENDICULAR) and tangent points (SNAP_TANGENT) from `anchor` onto
    every other object's edges. These are what make a curve END join another
    curve *cleanly* (right-angle or tangential) rather than just touching it
    at an arbitrary spot.

    `anchor` is the fixed reference point (normally the neighbour of the point
    being dragged); None yields []. `edges` lets the caller pass an
    already-collected edge list (it usually has one for on-curve projection)
    so the document isn't scanned twice. `kinds` limits the families. Gui-free
    -- headless-testable via its two collectors.
    """
    want_perp = kinds is None or SNAP_PERPENDICULAR in kinds
    want_tan = kinds is None or SNAP_TANGENT in kinds
    if anchor is None or not (want_perp or want_tan):
        return []
    if edges is None:
        edges = [e for e, _o in collect_snap_shapes(doc, exclude=exclude)]
    out = []
    if want_perp:
        for p in perpendicular_points_from(anchor, edges):
            out.append((p, SNAP_PERPENDICULAR))
    if want_tan:
        for p in tangent_points_from(anchor, edges):
            out.append((p, SNAP_TANGENT))
    return out


def collect_strong_snap_points_kinded(doc, exclude=None, kinds=None):
    """The document's discrete snap targets, each tagged with its family so
    the indicator can show WHICH kind engaged and the ranker can treat them
    differently. Returns [(Vector, kind), ...] covering SNAP_VERTEX
    (on-geometry vertices + knots), SNAP_CONTROL (off-curve control-polygon
    points -- the WEAK tier), SNAP_MIDPOINT / SNAP_CENTER
    (collect_feature_points_kinded) and SNAP_INTERSECTION (true curve-curve
    crossings).

    Control points share the SNAP_VERTEX toggle (they're collected whenever
    vertices are) but carry the distinct SNAP_CONTROL tag so
    nearest_snap_ranked_kind can rank them as distance-only rather than
    magnet-sticky -- the fix for off-curve handles hijacking a drag that was
    aiming at a curve body.

    `kinds` (a set of kind tags), if given, limits which families are
    collected -- e.g. {SNAP_VERTEX} skips the midpoint/centre scan and the
    intersection scan entirely. None collects all of them. Gui-free --
    headless-testable, same as the collectors it delegates to.

    (Constraint families -- SNAP_PERPENDICULAR / SNAP_TANGENT -- are NOT
    here: they depend on a drag anchor, so they come from
    collect_constraint_snap_points_kinded instead.)"""
    want_vertex = kinds is None or SNAP_VERTEX in kinds
    want_inter = kinds is None or SNAP_INTERSECTION in kinds
    out = []
    if want_vertex:
        for p in collect_snap_points(doc, exclude=exclude,
                                     include_control=False):
            out.append((p, SNAP_VERTEX))
        for p in collect_control_points(doc, exclude=exclude):
            out.append((p, SNAP_CONTROL))
    out.extend(collect_feature_points_kinded(doc, exclude=exclude, kinds=kinds))
    if want_inter:
        shapes = [e for e, _o in collect_snap_shapes(doc, exclude=exclude)]
        for p in collect_intersection_points(shapes):
            out.append((p, SNAP_INTERSECTION))
    return out


def nearest_snap_kind(view, kinded_points, pos, max_px):
    """nearest_snap over a KINDED candidate list [(pt, kind), ...]. Returns
    (point, kind) for the nearest within `max_px`, or (None, None)."""
    best, bestd, bestk = None, max_px, None
    for p, k in kinded_points:
        sp = screen_project(view, p)
        if sp is None:
            continue
        d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
        if d < bestd:
            best, bestd, bestk = p, d, k
    return best, bestk


def nearest_snap_ranked_kind(view, kinded_strong, edge_shapes, pos, max_px,
                             max_world_dist=None, face_shapes=None,
                             magnet_fraction=None):
    """Magnet-tiered cursor snap that also reports which family won, for a
    kind-coloured indicator. `kinded_strong` is a [(pt, kind), ...] list (from
    collect_strong_snap_points_kinded plus any anchor-relative constraint
    points from collect_constraint_snap_points_kinded); `edge_shapes` /
    `face_shapes` are the on-curve / on-surface projection candidates.

    Two passes, which is the fix for 'the discrete tier unconditionally
    outranks on-curve contact':

      1. A MAGNET-eligible exact target (MAGNET_KINDS: vertex/knot,
         intersection, midpoint, centre, perpendicular, tangent) within the
         inner magnet radius (magnet_fraction * max_px) wins outright -- these
         stay sticky, so aiming AT an endpoint/crossing/foot lands exactly on
         it.
      2. Otherwise EVERYTHING left competes on true screen distance: the weak
         SNAP_CONTROL points, on-curve projection (SNAP_EDGE) and on-surface
         projection (SNAP_SURFACE). Because an on-curve projection's screen
         distance is ~0 when you aim at a curve body, contact with the curve
         now beats an off-curve control handle a few pixels away -- you can
         finally 'ask for land on this curve'.

    Returns (point, kind), or (None, None)."""
    if magnet_fraction is None:
        magnet_fraction = MAGNET_FRACTION
    # Pass 1 -- inner magnet: a landing target close to the cursor is sticky.
    magnet_px = max_px * magnet_fraction
    mpt, mk, _md = _nearest_kinded(view, kinded_strong, pos, magnet_px,
                                   kinds=MAGNET_KINDS)
    if mpt is not None:
        return mpt, mk
    # Pass 2 -- outer annulus: nearest wins across all remaining candidates.
    best_pt, best_k, best_d = _nearest_kinded(view, kinded_strong, pos, max_px)
    ept, epx = _exact_snap_detail(view, edge_shapes, pos, max_px,
                                  max_world_dist=max_world_dist)
    if ept is not None and (best_pt is None or epx < best_d):
        best_pt, best_k, best_d = ept, SNAP_EDGE, epx
    if face_shapes:
        fpt, fpx = _exact_snap_detail(view, face_shapes, pos, max_px,
                                      max_world_dist=max_world_dist)
        if fpt is not None and (best_pt is None or fpx < best_d):
            best_pt, best_k, best_d = fpt, SNAP_SURFACE, fpx
    return (best_pt, best_k) if best_pt is not None else (None, None)


def nearest_index(pos, screen_points, max_px):
    """Index into `screen_points` (list of (sx,sy)-or-None) nearest to
    screen `pos`, within `max_px`, or None."""
    best, bestd = None, max_px
    for i, sp in enumerate(screen_points):
        if sp is None:
            continue
        d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
        if d < bestd:
            best, bestd = i, d
    return best