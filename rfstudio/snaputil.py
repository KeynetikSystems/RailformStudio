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


def collect_snap_points(doc, exclude=None, sample_step=3.0,
                        sample_bounds=(8, 60), include_knots=True):
    """Candidate snap points from every other object in `doc`: their
    control points (Points property), shape vertices, ~1 on-curve
    sample per `sample_step` world-units (capped to `sample_bounds`),
    and -- when `include_knots` is True (the default) -- every unique
    knot parameter of any BSpline edge, so a drag/pick can snap onto
    another curve's ACTUAL knot position, not just wherever the
    arc-length resample happened to fall nearest it.

    `exclude` is normally the object currently being edited. Gui-free --
    only reads Shape/Points -- so this is covered by test_core.py rather
    than relying on manual GUI testing.
    """
    pts = []
    lo, hi = sample_bounds
    for o in doc.Objects:
        if o is exclude or _is_container(o):
            continue
        op = getattr(o, "Points", None)
        if op:
            pts.extend(list(op))
        shp = getattr(o, "Shape", None)
        if not shp:
            continue
        for v in shp.Vertexes:
            pts.append(v.Point)
        for e in shp.Edges:
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
    `doc` -- the SHAPE-level candidate list nearest_point_on_shapes
    projects onto exactly (via OCCT's own nearest-point-on-shape query),
    as opposed to collect_snap_points' discrete list of vertex/control-
    point/sample/knot Vectors. Two different snap strategies, kept as
    two separate collectors:
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
    for o in doc.Objects:
        if o is exclude or _is_container(o):
            continue
        shp = getattr(o, "Shape", None)
        if not shp:
            continue
        for e in shp.Edges:
            out.append((e, o))
    return out


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


# ---- GUI-only thin wrappers (need a live View3DInventor) ------------------ #
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
    """Cursor-driven EXACT perpendicular-projection snap onto real curve
    geometry -- the shape-based counterpart to nearest_snap (which only
    picks among a fixed list of candidate points).

    Projects the cursor's current 3D position (view.getPoint(pos)) onto
    every shape in `shapes` (normally the edges from collect_snap_shapes)
    via nearest_point_on_shapes, then accepts the match only if ITS OWN
    screen position lands within `max_px` of the cursor -- the same
    screen-tolerance convention nearest_snap already applies to its
    discrete candidates, so the two behave consistently (a snap can't
    grab something that looks far away on screen just because it's
    geometrically close in world units, e.g. two curves overlapping in
    depth from the current view).

    Returns the exact snapped Vector, or None if nothing is within
    tolerance, `shapes` is empty, or the view can't resolve a 3D point
    at `pos`.
    """
    try:
        world_pt = view.getPoint(pos[0], pos[1])
    except Exception:
        return None
    if world_pt is None or not shapes:
        return None
    pt, _d, _shape = nearest_point_on_shapes(world_pt, shapes,
                                             max_dist=max_world_dist)
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