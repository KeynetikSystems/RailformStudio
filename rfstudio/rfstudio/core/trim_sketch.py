# -*- coding: utf-8 -*-
"""Surface trim + sketch-on-surface (native RFS_Trim / RFS_SketchOnSurf)."""

from __future__ import annotations

import Part
from FreeCAD import Vector


def interpolate_curve(points, closed=False):
    """Smooth B-spline that PASSES THROUGH the given points."""
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    if len(pts) < 2:
        raise ValueError("need >= 2 points")
    bs = Part.BSplineCurve()
    bs.interpolate(Points=pts, PeriodicFlag=bool(closed))
    return bs.toShape()


def trim_face(face, wire, tol=1e-4):
    """Trim `face` to `wire`'s boundary. Native replacement for Curves'
    'Trim' command (RFS_Trim).

    Fast path: if `wire` already lies on face's surface (the common case --
    e.g. a curve produced by project_wire_onto_face below, or a curve drawn
    directly on the surface via SketchOnSurf), OCCT can build the trimmed
    face directly from (surface, wire) with Part.Face -- this is the exact
    primitive OCCT provides for "a surface bounded by a wire on it", no
    boolean operation needed, and it's exact rather than tessellated.

    Fallback: if the wire is off-surface (drawn in a different plane, not
    yet projected), we don't have a general BRepProj_Projection binding
    available, so instead project each wire edge onto the face by nearest-
    point search (same technique as project_wire_onto_face) and retry the
    fast path once the wire is on-surface.
    """
    surf = face.Surface
    try:
        return Part.Face(surf, wire)
    except Exception:
        pass
    projected = project_wire_onto_face(face, wire, closed=_is_closed_wire(wire))
    return Part.Face(surf, Part.Wire(projected.Edges))


def _is_closed_wire(wire):
    try:
        vs = wire.Vertexes
        return bool(vs) and vs[0].Point.distanceToPoint(vs[-1].Point) < 1e-7
    except Exception:
        return False


def project_wire_onto_face(face, wire, n=60, closed=None):
    """Project `wire` onto `face`'s surface and rebuild it there as an
    interpolating B-spline. Native replacement for Curves' 'SoS' /
    Sketch-on-Surface (RFS_SketchOnSurf).

    FreeCAD's Part.Shape has no direct Python binding for OCCT's general
    surface projection (BRepProj_Projection); what IS exposed is
    `Vertex.distToShape`, which returns the closest point ON a shape to a
    given point -- exact for the "project this point onto that face"
    question when the point is reasonably close to the face (true for the
    normal "sketch drawn near/on the intended surface" use case). So we
    sample the wire, project every sample with distToShape, and refit --
    the same sample-and-refit idiom the rest of this module already uses
    (see _nearest_t, _closest_approach_pair) rather than reaching for a
    binding that isn't available.
    """
    if closed is None:
        closed = _is_closed_wire(wire)
    edges = wire.Edges if hasattr(wire, "Edges") else [wire]
    if not edges:
        raise ValueError("project_wire_onto_face: wire has no edges")
    pts = []
    per_edge = max(2, n // max(1, len(edges)))
    for e in edges:
        fp, lp = e.FirstParameter, e.LastParameter
        for k in range(per_edge):
            t = k / float(per_edge - 1) if per_edge > 1 else 0.0
            p = e.valueAt(fp + t * (lp - fp))
            dist, pts_on, _info = Part.Vertex(p).distToShape(face)
            pts.append(pts_on[0][1] if pts_on else p)
    # de-duplicate consecutive coincident points (shared edge endpoints)
    dedup = [pts[0]]
    for p in pts[1:]:
        if p.distanceToPoint(dedup[-1]) > 1e-7:
            dedup.append(p)
    if len(dedup) < 2:
        raise ValueError("project_wire_onto_face: projection collapsed to "
                         "a single point")
    return interpolate_curve(dedup, closed=closed)
def interpolate_curve(points, closed=False):
    """Smooth B-spline that PASSES THROUGH the given points."""
    pts = [Vector(*p) if not isinstance(p, Vector) else p for p in points]
    if len(pts) < 2:
        raise ValueError("need >= 2 points")
    bs = Part.BSplineCurve()
    bs.interpolate(Points=pts, PeriodicFlag=bool(closed))
    return bs.toShape()
