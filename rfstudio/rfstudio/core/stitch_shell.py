# -*- coding: utf-8 -*-
"""Stitch a finished patch network into one shell (RFS_StitchShell)."""

from __future__ import annotations

import Part
from FreeCAD import Vector


def stitch_shell(faces, tol=1e-3):
    """Sew a list of individual surface faces into one continuous
    Part.Shell (a Part.Solid instead, if the result closes) -- the
    user-facing counterpart to n_sided_patch's internal Shell/sewShape
    call, exposed here for ANY finished patch network (Loft/Gordon/Coons/
    BlendSurf/...), not just one N-sided fill's own sub-patches.

    This performs no continuity solving of any kind: it only welds
    coincident (within `tol`) boundaries together into one topological
    shell. Run network_continuity_report() on the input faces first if
    you need to know whether the seams about to be sewn are actually
    G1/G2 -- stitch_shell will happily sew a G0-only (visually kinked)
    network into one valid, watertight, but still-kinked shell.

    Raises ValueError for fewer than 2 faces, and RuntimeError if OCCT's
    sewing produces an empty result (e.g. faces that don't actually meet
    within `tol` anywhere).
    """
    faces = list(faces or [])
    if len(faces) < 2:
        raise ValueError("stitch_shell needs >= 2 faces")
    shell = Part.Shell(faces)
    try:
        shell.sewShape()
    except Exception as exc:
        raise RuntimeError("stitch_shell: sewing failed (%s)" % exc)
    if shell.isNull() or not shell.Faces:
        raise RuntimeError("stitch_shell: sewing produced an empty result "
                           "-- do the input faces actually meet within "
                           "tol=%g anywhere?" % tol)
    if shell.Shells and shell.Shells[0].isClosed():
        try:
            return Part.makeSolid(shell.Shells[0])
        except Exception:
            pass
    return shell


def free_edge_count(shape):
    """Number of edges used by exactly one face of `shape` -- i.e. the
    still-open (unstitched) boundary edges remaining after stitch_shell.
    Zero means the result is a fully closed shell/solid; a nonzero count
    is the number a caller should report back to the user (e.g. "stitched
    with 4 free edges remaining") instead of silently declaring success
    while real gaps remain in the network."""
    counts = {}
    for f in shape.Faces:
        for e in f.Edges:
            h = e.hashCode()
            counts[h] = counts.get(h, 0) + 1
    return sum(1 for c in counts.values() if c == 1)