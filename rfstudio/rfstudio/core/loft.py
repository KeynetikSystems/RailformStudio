# -*- coding: utf-8 -*-
"""Loft and surface intersection utilities."""

from .curve import as_wire
from .coons import _closest_approach_pair
import Part
from FreeCAD import Vector


def loft(rails, solid=False, ruled=False, closed=False, max_degree=5):
    wires = [as_wire(r) for r in rails]
    if len(wires) < 2:
        raise ValueError("loft needs >= 2 rails")
    return Part.makeLoft(wires, solid, ruled, closed, max_degree)


def _to_bspline(edge):
    """Return the BSplineCurve underlying `edge`, or refit a non-BSpline edge
    to a fresh BSpline (lossy but always works)."""
    c = getattr(edge, "Curve", None)
    if isinstance(c, Part.BSplineCurve):
        return c
    try:
        fp, lp = edge.FirstParameter, edge.LastParameter
        pts = [edge.valueAt(fp + k / 49.0 * (lp - fp)) for k in range(50)]
        bs = Part.BSplineCurve()
        bs.interpolate(Points=pts)
        return bs
    except Exception:
        return None


def _intersect_pair(c1, c2):
    """Return [(tu, tv), ...] -- parameter pairs at which the two curves
    actually cross. Uses OCCT `intersectCC` (the real intersection, not
    closest approach). Falls back to closest-approach when the curves
    don't actually cross, so the caller always gets at least one pair."""
    pts = []
    try:
        pts = c1.intersectCC(c2)
    except Exception:
        pts = []
    out = []
    for p in pts:
        try:
            v = Vector(p.X, p.Y, p.Z)
            tu = c1.parameter(v)
            tv = c2.parameter(v)
            if c1.FirstParameter <= tu <= c1.LastParameter and \
               c2.FirstParameter <= tv <= c2.LastParameter:
                out.append((float(tu), float(tv), float(v.distanceToPoint(v))))
        except Exception:
            continue
    if out:
        return out
    # Fallback: closest approach (handles tangent / near-miss / parallel).
    t1, t2, d = _closest_approach_pair(c1, c2)
    return [(float(t1), float(t2), float(d))]


def _intersect_grid(bs_profiles, bs_guides):
    """For every (profile, guide) pair, return the best intersection
    parameter pair (tu, tv) and the geometric distance. Prefers real
    OCCT intersections; falls back to closest-approach for non-crossing
    pairs (so a near-miss doesn't kill the skinning step).

    Returns:
      intersection_params_u: list[list[float]], len nu x nv -- tu for (i, j)
      intersection_params_v: list[list[float]], len nu x nv -- tv for (i, j)
      distances: list[list[float]], len nu x nv
    """
    nu, nv = len(bs_profiles), len(bs_guides)
    pu = [[0.0] * nv for _ in range(nu)]
    pv = [[0.0] * nv for _ in range(nu)]
    dd = [[0.0] * nv for _ in range(nu)]
    for i in range(nu):
        for j in range(nv):
            pairs = _intersect_pair(bs_profiles[i], bs_guides[j])
            # Pick the intersection with the smallest distance. Real
            # intersections are exact (distance 0) so this just enforces
            # a deterministic choice for multi-intersection pairs.
            best = min(pairs, key=lambda t: t[2])
            pu[i][j] = best[0]
            pv[i][j] = best[1]
            dd[i][j] = best[2]
    return pu, pv, dd


def _strictly_increasing(vals, eps=1e-6):
    """Nudge a list of floats so consecutive values strictly increase,
    preserving order -- guards against near-tied or reversed averaged
    parameters (e.g. from a near-degenerate crossing) breaking OCCT's
    interpolate(), which requires a strictly increasing Parameters list."""
    out = list(vals)
    for k in range(1, len(out)):
        if out[k] <= out[k - 1] + eps:
            out[k] = out[k - 1] + eps
    return out