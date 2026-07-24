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
                        include_samples=False):
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
        for e in shp.Edges:
            out.append((e, o))
    return out


def _bbox_overlap(b1, b2, pad):
    """True if two Part BoundBoxes overlap once each is grown by `pad` --
    a cheap broad-phase reject so the O(n^2) intersection scan only runs
    distToShape on edge pairs that could actually meet."""
    return (b1.XMin - pad <= b2.XMax + pad and b2.XMin - pad <= b1.XMax + pad and
            b1.YMin - pad <= b2.YMax + pad and b2.YMin - pad <= b1.YMax + pad and
            b1.ZMin - pad <= b2.ZMax + pad and b2.ZMin - pad <= b1.ZMax + pad)


def collect_intersection_points(shapes, tol=1e-6, max_edges=60):
    """True crossing points of the candidate edges -- where two curves
    actually meet in 3D (distToShape <= `tol`), NOT where they merely
    appear to cross on screen. Each such crossing is a semantically exact
    snap target (the CAD counterpart of Blender's vertex snap), so these
    join the exact discrete candidates that win over an on-edge projection.

    Cost control: an O(n^2) pair scan gated by a bounding-box broad-phase
    (_bbox_overlap), and skipped entirely above `max_edges` so a busy
    document never stalls a drag. Gui-free -- headless-testable.
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
    out = []
    for i in range(len(edges)):
        bi = boxes[i]
        if bi is None:
            continue
        for j in range(i + 1, len(edges)):
            bj = boxes[j]
            if bj is None or not _bbox_overlap(bi, bj, tol):
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
    for the (infinite) ray. `half_len` is how far the stand-in segment
    extends either side of `ray_origin`; the default 1e7 dwarfs any real
    model so the segment is effectively the full ray.

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
    try:
        ray = Part.Edge(Part.LineSegment(o - d * half_len, o + d * half_len))
    except Exception:
        return None, None, None
    best_pt, best_d, best_shape = None, 1e18, None
    for shp in shapes:
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
    than the point on the curve itself. Returns (point_on_axis, distance,
    shape), or (None, None, None) if `shapes` is empty / the direction is
    degenerate / every distToShape failed. `max_dist` rejects a target the
    axis never passes within that many world units of.

    Gui-free -- headless-testable, same as the rest of this tier.
    """
    import Part
    from FreeCAD import Vector
    d = Vector(axis_dir)
    if d.Length < 1e-12:
        return None, None, None
    d.normalize()
    o = Vector(axis_origin)
    try:
        axis = Part.Edge(Part.LineSegment(o - d * half_len, o + d * half_len))
    except Exception:
        return None, None, None
    best_pt, best_d, best_shape = None, 1e18, None
    for shp in shapes:
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
    if not shapes:
        return None
    ray = pick_ray(view, pos)
    if ray is None:
        return None
    origin, direction = ray
    pt, _d, _shape = nearest_point_on_shapes_to_ray(
        origin, direction, shapes, max_dist=max_world_dist)
    if pt is None:
        return None
    sp = screen_project(view, pt)
    if sp is None:
        return None
    dpx = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
    return pt if dpx <= max_px else None


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
                        max_world_dist=None):
    """Tiered cursor snap: an EXACT discrete target (vertex / control point
    / knot / curve-curve intersection, from collect_strong_snap_points)
    within `max_px` ALWAYS beats a generic on-edge projection, even if the
    on-edge point is a fraction of a pixel closer. Only when no strong
    candidate is in range do we fall to the exact on-edge point
    (nearest_exact_snap).

    This is Blender's vertex-over-edge priority: aiming near a curve end or
    a shared corner lands you on that mathematically exact point, not on a
    near-point that merely happens to sit slightly nearer the cursor along
    some other edge. Returns the snapped Vector, or None.
    """
    strong = nearest_snap(view, strong_points, pos, max_px)
    if strong is not None:
        return strong
    return nearest_exact_snap(view, edge_shapes, pos, max_px,
                              max_world_dist=max_world_dist)


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