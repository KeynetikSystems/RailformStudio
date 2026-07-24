# -*- coding: utf-8 -*-
"""Grid-to-surface utilities and helpers."""

from .curve import as_wire, _eval01
import Part
from FreeCAD import Vector


def grid_to_surface(grid):
    """Interpolating B-spline surface through a 2D grid of points.
    Falls back to a (approximating) control net if interpolate is unavailable."""
    surf = Part.BSplineSurface()
    try:
        surf.interpolate(grid)
    except Exception:
        nu, nv = len(grid), len(grid[0])
        du, dv = min(3, nu - 1), min(3, nv - 1)
        surf.buildFromPolesMultsKnots(
            grid,
            _clamped_mults(nu, du), _clamped_mults(nv, dv),
            _clamped_knots(nu, du), _clamped_knots(nv, dv),
            False, False, du, dv)
    return surf.toShape()


def _chain_loop(curves):
    """Orient and order curves into a closed loop of (edge, flip) pairs,
    matching _orient_family's convention.

    The Coons patch builder accepts the four boundary edges in any order or
    orientation; this helper reconnects them into a loop so the patch is
    built from a consistent perimeter rather than a scrambled one.

    Returns (edge, flip) tuples -- _pt() below needs .valueAt/
    FirstParameter/LastParameter, which Part.Edge exposes but Part.Wire
    does not, so ordering stays at the edge level throughout (each input
    curve is coerced to a single edge via as_wire(c).Edges[0]; a curve
    given as a genuine multi-edge wire only contributes its first edge --
    the same limitation the old wire-returning version had implicitly,
    since Part.Wire has no unified valueAt() either)."""
    if not curves:
        return []
    edges = [as_wire(c).Edges[0] for c in curves]
    if len(edges) <= 1:
        return [(e, False) for e in edges]

    ordered = []
    remaining = list(edges)
    current = remaining.pop(0)
    ordered.append((current, False))
    prev_pt = _eval01(current, 1.0)

    while remaining:
        best_idx = None
        best_dist = None
        best_flip = False
        for idx, cand in enumerate(remaining):
            start, end = _eval01(cand, 0.0), _eval01(cand, 1.0)
            for flip, pt in ((False, start), (True, end)):
                dist = prev_pt.distanceToPoint(pt)
                if best_dist is None or dist < best_dist - 1e-12:
                    best_dist = dist
                    best_idx = idx
                    best_flip = flip
        if best_idx is None:
            break
        cand = remaining.pop(best_idx)
        ordered.append((cand, best_flip))
        prev_pt = _eval01(cand, 0.0 if best_flip else 1.0)
    return ordered


def _clamped_mults(n_poles, degree):
    n_interior = n_poles - degree - 1
    return [degree + 1] + [1] * n_interior + [degree + 1]


def _clamped_knots(n_poles, degree):
    n_interior = n_poles - degree - 1
    if n_interior < 0:
        raise ValueError("need n_poles >= degree+1")
    interior = [float(i + 1) / (n_interior + 1) for i in range(n_interior)]
    return [0.0] + interior + [1.0]