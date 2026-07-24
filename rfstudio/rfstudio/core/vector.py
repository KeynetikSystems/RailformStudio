# -*- coding: utf-8 -*-
"""Vector mathematics."""

import math
from FreeCAD import Vector

__all__ = ['dot', 'cross', 'norm']


def dot(a, b):
    """Dot product of two vectors."""
    return a.x * b.x + a.y * b.y + a.z * b.z


def cross(a, b):
    """Cross product of two vectors."""
    return Vector(
        a.y * b.z - a.z * b.y,
        a.z * b.x - a.x * b.z,
        a.x * b.y - a.y * b.x
    )


def norm(v):
    """Norm (magnitude) of a vector."""
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)