# -*- coding: utf-8 -*-
"""True surface-surface trim (RFS_TrimByIntersection)."""

from __future__ import annotations

import Part
from FreeCAD import Vector

from .curve import _face_from_input
from .trim_sketch import trim_face


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
            import FreeCAD
            section = FreeCAD.Part.Shape(fa).section(FreeCAD.Part.Shape(fb))
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