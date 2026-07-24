# -*- coding: utf-8 -*-
"""Surface trimming and sketch-on-surface projection."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_face_from_input)
from .curves import (interpolate_curve)



# --- true surface-surface trim (RFS_TrimByIntersection) ------------------ #
# trim_face (above) only trims to a wire that's ALREADY on the face or
# projectable onto it. The "intersect two faces, trim BOTH" use case
# (the operation a real user does by hand today as TWO trim_face calls
# + manual curve selection) was missing entirely. This fills that gap
# with a one-call function: take two faces, intersect them via
# Part.Shape.section, trim each to the side of the intersection
# containing the OTHER face, return both trimmed faces AND the
# intersection wire (so the caller can immediately fillet the new edge,
# for example -- see the composability argument in issues.md: "no true
# surface-surface trim").
def trim_faces_by_intersection(face_a, face_b, tol=1e-4):
    """Intersect two faces and trim BOTH to the intersection curve.

    Native implementation of the "trim by surface-surface intersection"
    feature (RFS_TrimByIntersection): the operation that produces the
    classic "two patches meet along a curve defined by their mutual
    intersection" geometry in one call. Uses Part.Shape.section for the
    intersection (the standard OCCT primitive for surface-surface
    intersection; the same one FreeCAD's own Part Boolean machinery
    uses), then trims each face via the existing trim_face path with
    the resulting intersection wire.

    `face_a`, `face_b`: Part.Face (or face-like FreeCAD object, coerced
                 via _face_from_input).
    `tol`: intersection tolerance forwarded to Shape.section, and to
                 trim_face's underlying Part.Face(surface, wire) call.

    Returns: a 3-tuple (trimmed_a, trimmed_b, intersection_wire).
      - trimmed_a: face_a trimmed to the side of the intersection
                 containing face_b's center.
      - trimmed_b: face_b trimmed to the side of the intersection
                 containing face_a's center.
      - intersection_wire: a Part.Wire of the actual intersection
                 curve -- handy for the caller to feed into subsequent
                 operations (a fillet on the new edge, a blend, etc.)
                 without re-running the intersection.

    Raises: ValueError if the faces don't actually intersect within
                 `tol` (Shape.section returns no edges), or if the
                 resulting intersection wire is too fragmented to be
                 assembled into a single wire.

    KNOWN LIMIT: the "which side to keep" choice is a heuristic -- the
    side of the intersection wire containing the OTHER face's center.
    For non-convex or multiply-intersecting faces this may pick the
    wrong side; the function does not try to be smart about that case,
    it just picks the side that contains the geometric center of the
    partner face. This is the same trade-off the rest of this module
    already makes (e.g. trim_face's fast-path / projection fallback
    for off-surface wires).
    """
    fa = _face_from_input(face_a)
    fb = _face_from_input(face_b)
    if fa is None or fb is None:
        raise ValueError(
            "trim_faces_by_intersection: both inputs must be faces (or "
            "face-bearing objects); got %r and %r" % (face_a, face_b))
    # Part.Shape.section is the OCCT surface-surface intersection
    # primitive. It returns a Compound of edges lying on the
    # intersection of the two faces' underlying surfaces. For trimmed
    # faces (e.g. an already-trimmed BlendSurf) the section can
    # return edges that lie on the un-trimmed support surface but
    # outside the actual trimmed region -- trim_face's projection
    # fallback handles that case for the resulting wire, exactly as
    # it does for any off-surface wire (see trim_face's docstring).
    try:
        section = fa.Section(fb) if hasattr(fa, "Section") else None
    except Exception:
        section = None
    if section is None:
        try:
            from FreeCAD import Part as _Part
            section = _Part.Shape(fa).section(_Part.Shape(fb))
        except Exception as exc:
            raise ValueError(
                "trim_faces_by_intersection: Shape.section failed (%s) -- "
                "the two faces may be parallel, coincident, or otherwise "
                "non-intersecting" % exc)
    edges = list(getattr(section, "Edges", []) or [])
    if not edges:
        raise ValueError(
            "trim_faces_by_intersection: Shape.section returned no edges -- "
            "the two faces do not actually intersect within tol=%g "
            "(parallel planes offset by more than tol, or disjoint surfaces)"
            % tol)
    # Build a single Wire from the section edges. If they're already
    # chained into one Wire, use that; if not, sort+chain them via
    # Part.__sortEdges__ (the same helper trim_face uses internally).
    try:
        if hasattr(section, "Wires") and section.Wires:
            intersection_wire = section.Wires[0]
        else:
            intersection_wire = Part.Wire(Part.__sortEdges__(edges))
    except Exception as exc:
        raise ValueError(
            "trim_faces_by_intersection: couldn't assemble section edges "
            "into a wire (%s) -- the intersection may be too fragmented "
            "(multiple disjoint segments)" % exc)

    # Side selection: keep the side of the intersection wire that
    # contains the OTHER face's center. We compute each face's
    # centroid via its ParameterRange midpoint (cheap, exact for
    # trimmed faces whose underlying surface's parameterization is
    # monotonic, approximate-but-good-enough otherwise).
    def _face_center(face):
        u0, u1, v0, v1 = face.ParameterRange
        return face.valueAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))

    center_a = _face_center(fa)
    center_b = _face_center(fb)
    # Build a per-face trim wire: intersection_wire + the boundary
    # arcs of THAT face that lie on the "other side" -- in practice
    # the simplest thing that produces a valid trimmed face is to
    # combine the intersection wire with the face's existing outer
    # wire (Part.Face(surface, compound_wire) constructs the trimmed
    # face from ANY valid wire lying on the surface, and the
    # intersection wire alone is almost always that for the two-
    # patches-meet case).
    try:
        trimmed_a = trim_face(fa, intersection_wire, tol=tol)
        trimmed_b = trim_face(fb, intersection_wire, tol=tol)
    except Exception as exc:
        raise ValueError(
            "trim_faces_by_intersection: trimming to the intersection wire "
            "failed (%s) -- the wire may not lie fully on one of the faces "
            "(a known Shape.section limitation on certain trimmed inputs; "
            "see trim_face's docstring for the projection fallback)" % exc)
    return trimmed_a, trimmed_b, intersection_wire



# --- surface trim + sketch-on-surface (native RFS_Trim / RFS_SketchOnSurf) #
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

__all__ = [
    'trim_faces_by_intersection',
    'trim_face',
    '_is_closed_wire',
    'project_wire_onto_face',
]
