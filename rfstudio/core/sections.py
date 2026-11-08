# -*- coding: utf-8 -*-
"""Section extraction: pull cross-section (or rail-wise) curves back out of a
swept/lofted surface, so a pipe or sweep can be edited and rebuilt.

Convention (verified on single_rail_sweep / two_rail_sweep / multi_rail_sweep):
a sweep surface's U parameter runs along the rails (one U value per rail
station) and V runs around/across the profile. So

    "Cross-sections"  ->  constant-U iso curves   (one profile per station)
    "Rails"           ->  constant-V iso curves   (one rail-wise curve per
                                                   profile point)

For a single-rail PIPE the "Rails" direction gives the lines that run
parallel to the spine (the frame is rotation-minimizing, so they stay
within a few percent of the rail's length).
"""

import Part

SECTION_DIRECTIONS = ("Cross-sections", "Rails")
SECTION_SPACINGS = ("Parameter", "Arc length")


def _fractions(count, include_ends):
    if count < 1:
        raise ValueError("extract_iso_curves: count must be >= 1")
    if count == 1:
        return [0.5]
    if include_ends:
        return [i / (count - 1.0) for i in range(count)]
    return [(i + 1.0) / (count + 1.0) for i in range(count)]


def _arclength_params(path_curve, lo, hi, fractions, samples=200):
    """Parameters on [lo, hi] at the given FRACTIONS of arc length along
    `path_curve` (sampled; monotone, robust to odd parametrizations)."""
    ts = [lo + (hi - lo) * k / samples for k in range(samples + 1)]
    pts = [path_curve.value(t) for t in ts]
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + a.distanceToPoint(b))
    total = cum[-1]
    if total < 1e-12:
        return [lo + (hi - lo) * f for f in fractions]
    out = []
    for f in fractions:
        target = f * total
        k = 1
        while k < len(cum) - 1 and cum[k] < target:
            k += 1
        seg = cum[k] - cum[k - 1]
        w = 0.0 if seg < 1e-15 else (target - cum[k - 1]) / seg
        out.append(ts[k - 1] + (ts[k] - ts[k - 1]) * w)
    return out


def extract_iso_curves(shape, count=9, direction="Cross-sections",
                       spacing="Parameter", include_ends=True, face_index=0):
    """Iso-parameter curves of one face of `shape` as a list of Part.Edge.

    direction: "Cross-sections" (constant U) or "Rails" (constant V).
    spacing:   "Parameter" (even in the varying parameter) or "Arc length"
               (even along the surface's mid iso curve in that direction).
    include_ends: put curves exactly on the two boundary iso lines.
    """
    if direction not in SECTION_DIRECTIONS:
        raise ValueError("extract_iso_curves: direction must be one of %s"
                         % (SECTION_DIRECTIONS,))
    if spacing not in SECTION_SPACINGS:
        raise ValueError("extract_iso_curves: spacing must be one of %s"
                         % (SECTION_SPACINGS,))
    faces = getattr(shape, "Faces", None)
    if not faces:
        raise ValueError("extract_iso_curves: source has no faces")
    if not 0 <= int(face_index) < len(faces):
        raise ValueError("extract_iso_curves: face index %s out of range "
                         "(0..%d)" % (face_index, len(faces) - 1))
    face = faces[int(face_index)]
    surf = face.Surface
    u0, u1, v0, v1 = face.ParameterRange
    fr = _fractions(int(count), bool(include_ends))
    cross = direction == "Cross-sections"
    lo, hi = (u0, u1) if cross else (v0, v1)       # the VARYING parameter
    if spacing == "Arc length":
        mid = 0.5 * ((v0 + v1) if cross else (u0 + u1))
        path = surf.vIso(mid) if cross else surf.uIso(mid)
        plo, phi = path.FirstParameter, path.LastParameter
        params = _arclength_params(path, plo, phi, fr)
        # path is parametrized like the surface's varying parameter; map
        # onto [lo, hi] in case the iso curve's own range differs.
        params = [lo + (p - plo) / (phi - plo) * (hi - lo) for p in params] \
            if abs(phi - plo) > 1e-15 else [lo + (hi - lo) * f for f in fr]
    else:
        params = [lo + (hi - lo) * f for f in fr]
    edges = []
    for p in params:
        curve = surf.uIso(p) if cross else surf.vIso(p)
        edges.append(curve.toShape())
    return edges


__all__ = ["SECTION_DIRECTIONS", "SECTION_SPACINGS", "extract_iso_curves"]
