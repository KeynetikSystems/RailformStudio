# -*- coding: utf-8 -*-
"""Curve and wire utilities."""

import Part
from FreeCAD import Vector

__all__ = ['as_wire', 'join_curves']


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


# Shared helpers used by other modules in this package (not in __all__)
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