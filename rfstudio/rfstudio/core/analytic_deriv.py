# -*- coding: utf-8 -*-
"""Analytic (non-finite-difference) surface derivatives."""

from __future__ import annotations

from typing import Dict, Optional

import Part
from FreeCAD import Vector

from .curve import _pt, _eval01
from .gordon import _face_control_grid


def _bspline_surface_cached(face, cache):
    """BSplineSurface copy of `face`'s underlying surface, or None if no
    conversion is possible -- memoized in `cache` (a plain dict the caller
    creates once per blend/match invocation, same convention as the
    edge_cache already threaded through _side_uv_trimmed) so each face's
    conversion happens once, not once per sample point."""
    key = id(face)
    if key in cache:
        return cache[key]
    try:
        surf, _grid = _face_control_grid(face)
    except Exception:
        surf = None
    cache[key] = surf
    return surf


def _surf_dn(face, u, v, du, dv, cache):
    """Exact analytic partial derivative d^(du+dv)S/du^du dv^dv at (u, v),
    read directly off `face`'s control net via OCCT's
    BSplineSurface.getDN -- NOT a finite-difference estimate. Returns None
    (not an approximate value) when `face` can't be expressed as a
    BSplineSurface at all, or this FreeCAD/OCCT build's Part binding
    doesn't expose getDN, so the caller can fall back to its own
    finite-difference path unchanged."""
    surf = _bspline_surface_cached(face, cache)
    if surf is None:
        return None
    # Check if we can compute the requested derivative
    # For a mixed derivative d^(du+dv)/du^du dv^dv, we need u-degree >= du and v-degree >= dv
    if hasattr(surf, 'UDegree') and hasattr(surf, 'VDegree'):
        if surf.UDegree < du or surf.VDegree < dv:
            return None  # Degree too low for requested derivative
    try:
        return surf.getDN(u, v, int(du), int(dv))
    except Exception:
        return None