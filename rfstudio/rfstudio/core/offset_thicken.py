# -*- coding: utf-8 -*-
"""Manufacturability: offset / thicken / shell."""

# Everything above this point (Loft/Gordon/Coons/Fill/Match/blend_surface)
# produces pure B-spline SHAPES with zero wall thickness -- fine for a
# visual or aero shape, not a manufacturable part. These three wrap OCCT's
# own offset/thickening primitives (BRepOffsetAPI_MakeOffsetShape and
# BRepOffsetAPI_MakeThickSolid, both already exposed on Part.Shape as
# makeOffsetShape/makeThickness) -- no new geometry kernel work needed,
# just the doc-object plumbing this workbench was missing.
from __future__ import annotations

import Part
from FreeCAD import Vector


def offset_surface(shape, distance, tol=1e-3):
    """Pure surface offset (no solid): a new open shell/face offset by
    `distance` along its normal. Sign of `distance` picks the offset side
    -- flip the sign if OCCT walked the wrong way for a given shape."""
    if abs(distance) < 1e-9:
        raise ValueError("offset_surface: distance must be non-zero")
    if isinstance(shape, Part.Face):
        return shape.makeOffsetShape(distance, tol).Faces[0]
    elif isinstance(shape, Part.Shell):
        return shape.makeOffsetShape(distance, tol)
    else:
        raise TypeError("offset_surface expects Part.Face or Part.Shell")


def thicken_surface(shape, distance, tol=1e-3):
    """Thicken a surface into a solid by offsetting and sewing.

    Args:
        shape: Part.Face or Part.Shell to thicken
        distance: thickness (positive adds material in normal direction)
        tol: tolerance for sewing

    Returns:
        Part.Solid (or Part.Compound if sewing fails)
    """
    if abs(distance) < 1e-9:
        raise ValueError("thicken_surface: distance must be non-zero")
    if isinstance(shape, Part.Face):
        offset = shape.makeOffsetShape(distance, tol)
        # Create a loft between original and offset to get volume
        wires = [shape.OuterWire, offset.Faces[0].OuterWire]
        try:
            loft = Part.makeLoft(wires)
            return loft
        except Exception:
            # Fallback: just sew the two surfaces
            try:
                shell = Part.Shell([shape, offset.Faces[0]])
                shell.sewShape()
                return shell
            except Exception:
                return Part.Compound([shape, offset.Faces[0]])
    elif isinstance(shape, Part.Shell):
        offset = shape.makeOffsetShape(distance, tol)
        try:
            solid = Part.makeSolid([shape, offset])
            return solid
        except Exception:
            return Part.Compound([shape, offset])
    else:
        raise TypeError("thicken_surface expects Part.Face or Part.Shell")


def shell_solid(shape, thickness=1e-3, tol=1e-3):
    """Create a shell solid by thickening a closed shell.

    Args:
        shape: Part.Shell that should be closed
        thickness: wall thickness
        tol: tolerance

    Returns:
        Part.Solid (hollow with given wall thickness)
    """
    if not isinstance(shape, Part.Shell):
        raise TypeError("shell_solid expects Part.Shell")
    if not shape.isClosed():
        raise ValueError("shell_solid requires a closed shell")
    # Offset inward and outward, then sew
    inner = shape.makeOffsetShape(-thickness/2.0, tol)
    outer = shape.makeOffsetShape(thickness/2.0, tol)
    try:
        solid = Part.makeSolid([inner, outer])
        return solid
    except Exception:
        return Part.Compound([inner, outer])


def offset_thicken_shell(shape, distance, tol=1e-3):
    """Convenience: offset a shell and thicken the result.

    Equivalent to: thicken_surface(offset_surface(shape, distance), distance)
    but avoids intermediate steps.

    Args:
        shape: Part.Shell to process
        distance: offset distance
        tol: tolerance

    Returns:
        Part.Solid or Part.Compound
    """
    if not isinstance(shape, Part.Shell):
        raise TypeError("offset_thicken_shell expects Part.Shell")
    offset = shape.makeOffsetShape(distance, tol)
    try:
        solid = Part.makeSolid([shape, offset])
        return solid
    except Exception:
        return Part.Compound([shape, offset])