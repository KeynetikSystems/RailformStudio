# -*- coding: utf-8 -*-
"""Coons patch and related helpers."""

from .curve import as_wire, _eval01, _pt
from .grid_surface import grid_to_surface
import Part
from FreeCAD import Vector


def _closest_approach_pair(c1, c2, samples=24):
    """Return (tu, tv, distance) where tu is the parameter on c1 closest to c2
    and tv the parameter on c2 closest to c1. Refines by alternating
    'project sample of other onto me' steps."""
    fp1, lp1 = c1.FirstParameter, c1.LastParameter
    fp2, lp2 = c2.FirstParameter, c2.LastParameter
    span1 = max(lp1 - fp1, 1e-12)
    span2 = max(lp2 - fp2, 1e-12)
    samples = max(8, samples)
    pts1 = [c1.value(fp1 + k / samples * span1) for k in range(samples + 1)]
    pts2 = [c2.value(fp2 + k / samples * span2) for k in range(samples + 1)]
    best_t1, best_t2, best_d = fp1, fp2, 1e18
    for k1, p1 in enumerate(pts1):
        for k2, p2 in enumerate(pts2):
            d = p1.distanceToPoint(p2)
            if d < best_d:
                best_d = d
                best_t1 = fp1 + k1 / samples * span1
                best_t2 = fp2 + k2 / samples * span2
    for _ in range(4):
        p2 = c2.value(best_t2)
        try:
            new_t1 = c1.parameter(p2)
        except Exception:
            break
        if new_t1 < fp1 or new_t1 > lp1:
            break
        best_t1 = new_t1
        p1 = c1.value(best_t1)
        try:
            new_t2 = c2.parameter(p1)
        except Exception:
            break
        if new_t2 < fp2 or new_t2 > lp2:
            break
        best_t2 = new_t2
    return (best_t1, best_t2,
            c1.value(best_t1).distanceToPoint(c2.value(best_t2)))


def coons_patch(boundary_edges, n=24):
    """Bilinearly-blended Coons surface through 4 boundary curves.

    Exact, robust, fully headless-testable -- our own OCCT-only implementation,
    no Curves/Silk dependency. The 4 edges may be given in any order/orientation;
    we chain them into a loop first.
    """
    if len(boundary_edges) != 4:
        raise ValueError("Coons patch needs exactly 4 boundary curves")
    sides = _chain_loop(boundary_edges)          # 4 sides in loop order
    s = [_pt(sides[0], i / (n - 1.0)) for i in range(n)]            # south u:0->1
    e = [_pt(sides[1], j / (n - 1.0)) for j in range(n)]            # east  v:0->1
    north = [_pt(sides[2], 1 - i / (n - 1.0)) for i in range(n)]    # north u:0->1
    w = [_pt(sides[3], 1 - j / (n - 1.0)) for j in range(n)]        # west  v:0->1
    p00, p10 = s[0], s[-1]
    p11, p01 = e[-1], north[0]
    grid = []
    for i in range(n):
        u = i / (n - 1.0)
        row = []
        for j in range(n):
            v = j / (n - 1.0)
            bil = (p00 * ((1 - u) * (1 - v)) + p10 * (u * (1 - v))
                   + p01 * ((1 - u) * v) + p11 * (u * v))
            pt = (s[i] * (1 - v) + north[i] * v
                  + w[j] * (1 - u) + e[j] * u - bil)
            row.append(pt)
        grid.append(row)
    return grid_to_surface(grid)