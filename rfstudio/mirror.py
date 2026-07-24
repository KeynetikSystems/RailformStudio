# -*- coding: utf-8 -*-
"""Mirror / symmetric-modeling geometry helpers.

Gui-free, like core.py -- these only touch Vector math, never FreeCADGui,
so they're headlessly testable. Split into their own module rather than
folded into core.py so this specific feature area (live symmetric editing
+ parametric MirrorFP) has one obvious home.

Two independent capabilities live on top of these primitives:
  * Live bidirectional symmetric editing of a SINGLE curve object
    (editmode.PointEditor.set_symmetry) -- "model only half" by dragging
    either side of one object; the other side follows in real time.
  * MirrorFP (parametric.py) -- a separate, one-way parametric mirror
    object (Source -> mirrored copy), optionally welded into one
    continuous shell/solid via core.stitch_shell.
"""

from FreeCAD import Vector


def plane_base_normal(plane_name, base=None, normal=None):
    """(base, normal) Vector pair for the standard principal planes
    ("XY", "YZ", "ZX"), or a user-supplied Custom base/normal.

    Raises ValueError for a zero-length custom normal.
    """
    if plane_name == "XY":
        return Vector(0, 0, 0), Vector(0, 0, 1)
    if plane_name == "YZ":
        return Vector(0, 0, 0), Vector(1, 0, 0)
    if plane_name == "ZX":
        return Vector(0, 0, 0), Vector(0, 1, 0)
    b = base if base is not None else Vector(0, 0, 0)
    n = normal if normal is not None else Vector(0, 0, 1)
    if n.Length < 1e-12:
        raise ValueError("plane_base_normal: custom normal must be non-zero")
    return b, n * (1.0 / n.Length)


def mirror_point(p, plane_base, plane_normal):
    """Reflect point p across the plane (plane_base, plane_normal).

    Raises ValueError for a zero-length plane_normal.
    """
    n = plane_normal
    if n.Length < 1e-12:
        raise ValueError("mirror_point: plane_normal must be non-zero")
    n = n * (1.0 / n.Length)
    d = (p - plane_base).dot(n)
    return p - n * (2.0 * d)


def find_symmetric_pairs(points, plane_base, plane_normal, tol=1e-4):
    """Pair up indices in `points` that are mirror images of each other
    across (plane_base, plane_normal), within `tol`.

    A point ON the plane (within tol) pairs with itself -- these are the
    seam points that must stay on-plane during symmetric editing.

    Returns {i: j, ...} covering every index (self-paired seam points
    included, so callers can look up any index unconditionally).

    Raises ValueError if any point has no partner (the set isn't
    actually symmetric about this plane) -- callers use this to refuse
    enabling symmetric editing on a non-symmetric curve rather than
    silently mis-pairing points.
    """
    n = plane_normal
    if n.Length < 1e-12:
        raise ValueError("find_symmetric_pairs: plane_normal must be non-zero")
    n = n * (1.0 / n.Length)
    dists = [(p - plane_base).dot(n) for p in points]
    pairs = {}
    used = set()
    for i, p in enumerate(points):
        if i in used:
            continue
        if abs(dists[i]) <= tol:
            pairs[i] = i
            used.add(i)
            continue
        target = mirror_point(p, plane_base, n)
        best_j, best_d = None, tol
        for j, q in enumerate(points):
            if j == i or j in used:
                continue
            d = target.distanceToPoint(q)
            if d <= best_d:
                best_j, best_d = j, d
        if best_j is None:
            raise ValueError(
                "find_symmetric_pairs: point %d has no mirror partner "
                "within tol=%g -- this point set isn't symmetric about "
                "the chosen plane" % (i, tol))
        pairs[i] = best_j
        pairs[best_j] = i
        used.add(i)
        used.add(best_j)
    return pairs


def mirror_shape(shape, plane_base, plane_normal, weld=False, stitch_fn=None):
    """Mirror `shape` (a Part.Shape) across (plane_base, plane_normal).

    If `weld` is True, attempts to stitch the original and mirrored
    faces into one continuous shell/solid via `stitch_fn` (normally
    core.stitch_shell, injected so this module stays free of any
    core.py import cycle). Falls back to an unwelded Part.Compound of
    [shape, mirrored] if stitching fails or `stitch_fn` isn't provided
    and `weld` was requested -- callers should surface that fallback to
    the user (see parametric.MirrorFP.execute), not treat it as success.

    Raises ValueError for a zero-length plane_normal (via mirror_point's
    underlying validation, triggered on the FreeCAD mirror() call path).
    """
    n = plane_normal
    if n.Length < 1e-12:
        raise ValueError("mirror_shape: plane_normal must be non-zero")
    n = n * (1.0 / n.Length)
    mirrored = shape.mirror(plane_base, n)
    if not weld:
        return mirrored, False
    if stitch_fn is None or not getattr(shape, "Faces", None) or not mirrored.Faces:
        return mirrored, False
    try:
        welded = stitch_fn(list(shape.Faces) + list(mirrored.Faces))
        return welded, True
    except Exception:
        return mirrored, False