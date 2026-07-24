# -*- coding: utf-8 -*-
"""Hermite basis functions.

Provides Hermite spline basis functions used for curve and surface
construction.
"""

from __future__ import annotations

from FreeCAD import Vector


def _hermite3(p0, t0, p1, t1, t):
    """Cubic Hermite basis.

    Args:
        p0, p1: Start and end points (Vectors).
        t0, t1: Start and end tangent vectors (Vectors).
        t: Parameter in [0, 1].

    Returns:
        Interpolated point.
    """
    h00 = 2 * t**3 - 3 * t**2 + 1
    h10 = t**3 - 2 * t**2 + t
    h01 = -2 * t**3 + 3 * t**2
    h11 = t**3 - t**2
    return p0 * h00 + t0 * h10 + p1 * h01 + t1 * h11


def _hermite5(p0, v0, a0, p1, v1, a1, t):
    """Quintic Hermite basis.

    Args:
        p0, p1: Start and end points (Vectors).
        v0, v1: Start and end tangent vectors (Vectors).
        a0, a1: Start and end acceleration vectors (Vectors).
        t: Parameter in [0, 1].

    Returns:
        Interpolated point.
    """
    t2 = t * t
    t3 = t2 * t
    t4 = t3 * t
    t5 = t4 * t
    h00 = 1 - 10 * t3 + 15 * t4 - 6 * t5
    h10 = t - 6 * t3 + 8 * t4 - 3 * t5
    h20 = 0.5 * (t2 - 3 * t3 + 3 * t4 - t5)
    h01 = 10 * t3 - 15 * t4 + 6 * t5
    h11 = -4 * t3 + 7 * t4 - 3 * t5
    h21 = 0.5 * (t3 - 2 * t4 + t5)
    return p0 * h00 + v0 * h10 + a0 * h20 + p1 * h01 + v1 * h11 + a1 * h21


def _hermite7(p0, v0, a0, j0, p1, v1, a1, j1, t):
    """Septic (degree-7) Hermite basis.

    Args:
        p0, p1: Start and end points (Vectors).
        v0, v1: Start and end tangent vectors (Vectors).
        a0, a1: Start and end acceleration vectors (Vectors).
        j0, j1: Start and end jerk vectors (Vectors).
        t: Parameter in [0, 1].

    Returns:
        Interpolated point.
    """
    t2 = t * t
    t3 = t2 * t
    t4 = t3 * t
    t5 = t4 * t
    t6 = t5 * t
    t7 = t6 * t
    h00 = 1 - 35 * t4 + 84 * t5 - 70 * t6 + 20 * t7
    h10 = t - 20 * t4 + 45 * t5 - 36 * t6 + 10 * t7
    h20 = 0.5 * (t2 - 5 * t4 + 10 * t5 - 7.5 * t6 + 2 * t7)
    h30 = (1.0 / 6.0) * t3 - (2.0 / 3.0) * t4 + t5 - (2.0 / 3.0) * t6 + (1.0 / 6.0) * t7
    h01 = 35 * t4 - 84 * t5 + 70 * t6 - 20 * t7
    h11 = -15 * t4 + 39 * t5 - 34 * t6 + 10 * t7
    h21 = 2.5 * t4 - 7 * t5 + 6.5 * t6 - 2 * t7
    h31 = -(1.0 / 6.0) * t4 + 0.5 * t5 - 0.5 * t6 + (1.0 / 6.0) * t7
    return (
        p0 * h00
        + v0 * h10
        + a0 * h20
        + j0 * h30
        + p1 * h01
        + v1 * h11
        + a1 * h21
        + j1 * h31
    )