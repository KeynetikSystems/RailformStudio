# -*- coding: utf-8 -*-
"""Fillet and chamfer operations."""

from __future__ import annotations

import math
from typing import Dict, Optional

import Part
from FreeCAD import Vector

from .curve import _pt


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