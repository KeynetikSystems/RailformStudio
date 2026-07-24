# -*- coding: utf-8 -*-
"""Cross-boundary derivative helpers."""

from __future__ import annotations

__all__ = [
    '_compute_finite_difference_epsilon',
    '_finite_difference_second_derivative',
    '_finite_difference_third_derivative',
    '_cross_tan_from_surface_tangent',
]


def _compute_finite_difference_epsilon(parameter_range, dimension_index):
    """Compute epsilon for finite difference derivatives.

    Args:
        parameter_range: Tuple (u0, u1, v0, v1) defining parameter bounds
        dimension_index: 0 for u-dimension, 1 for v-dimension

    Returns:
        Epsilon value for finite difference calculations
    """
    u0, u1, v0, v1 = parameter_range
    param_range = (u1 - u0) if dimension_index == 0 else (v1 - v0)
    return max(1e-6, param_range * 1e-3)


def _finite_difference_second_derivative(point_func, u, v, du_sign, dv_sign, eps):
    """Compute second derivative using central difference.

    Args:
        point_func: Function(u, v) -> point on surface
        u, v: Parameter coordinates
        du_sign, dv_sign: Direction signs (+1 or -1) for u and v derivatives
        eps: Step size for finite difference

    Returns:
        Second derivative vector
    """
    p0 = point_func(u, v)
    p1 = point_func(u + du_sign * eps, v + dv_sign * eps)
    p2 = point_func(u + 2 * du_sign * eps, v + 2 * dv_sign * eps)
    return (p0 - 2.0 * p1 + p2) / (eps * eps)


def _finite_difference_third_derivative(point_func, u, v, du_sign, dv_sign, eps):
    """Compute third derivative using forward difference.

    Args:
        point_func: Function(u, v) -> point on surface
        u, v: Parameter coordinates
        du_sign, dv_sign: Direction signs (+1 or -1) for u and v derivatives
        eps: Step size for finite difference

    Returns:
        Third derivative vector
    """
    p0 = point_func(u, v)
    p1 = point_func(u + du_sign * eps, v + dv_sign * eps)
    p2 = point_func(u + 2 * du_sign * eps, v + 2 * dv_sign * eps)
    p3 = point_func(u + 3 * du_sign * eps, v + 3 * dv_sign * eps)
    return (-p0 + 3.0 * p1 - 3.0 * p2 + p3) / (eps ** 3)


def _cross_tan_from_surface_tangent(du, dv, side):
    """Compute cross-boundary tangent vector from surface tangents.

    Args:
        du, dv: Tangent vectors in u and v directions from surface.tangent()
        side: Side indicator (0=-u, 1=+u, 2=-v, 3=+v)

    Returns:
        Tangent vector in the specified direction
    """
    return {0: du.negative(), 1: du, 2: dv.negative(), 3: dv}[side]
