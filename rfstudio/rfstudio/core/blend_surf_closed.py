# -*- coding: utf-8 -*-
"""Closed-loop blend strip (RFS_BlendSurfClosed)."""

from __future__ import annotations

import Part
from FreeCAD import Vector

from .curve import _pt, _eval01
from .derivatives import (
    _compute_finite_difference_epsilon,
    _finite_difference_second_derivative,
    _cross_tan_from_surface_tangent,
)
from .gordon import _face_from_input, _side_uv_trimmed, _nearest_facing_sides, _surf_dn
from .hermite_profile import _hermite3, _hermite5
from .grid_surface import grid_to_surface


def blend_surface_closed(face_list, continuity="G2", n=24, scale=1.0):
    """G1/G2 blend surface strip around a closed loop of N faces.

    Native implementation of the "closed-loop blend" (RFS_BlendSurfClosed):
    a single continuous surface-strip wrapping all the way around a ring
    of N >= 3 faces (the "fill a hole with a fillet ring" use case --
    hood cutout, fender wheel arch, antenna hole). Generalizes over
    arbitrary N, not just 4-sided holes, since real automotive-scale
    rings are rarely exactly quadrilateral and a 4-only special-case
    would immediately force a rewrite (see issues.md: "no closed-loop
    blend" is the highest-priority blend-related gap).

    `face_list`: ordered list of >= 3 Part.Face (or face-bearing FreeCAD
                 objects, coerced via _face_from_input). face_list[i]
                 must face face_list[(i+1) % N] across their nearest
                 boundaries; the caller is responsible for the ordering
                 (the typical "click faces in order around the hole"
                 FreeCAD selection gives this naturally).
    `continuity`: "G0", "G1", or "G2" -- the cross-section basis (same
                 values blend_surface already accepts; the same
                 Hermite-sampling limitations apply: G1/G2 are exact at
                 the sampled points and approximate between them).
    `n`: cross-section sample count along the FULL loop (so each ring
                 member gets ~n/N samples; pick n >= N for a smooth
                 result, otherwise per-member sampling is coarse).
    `scale`: tangent-strength factor (same dimensionless knob
                 blend_surface already exposes; kept for API parity --
                 the close seam's quality is bounded by the same
                 closest-approach search blend_surface's quality is
                 bounded by).

    Returns: a Part.Shape (Shell) of N-1 sub-strip faces (one per
             consecutive pair in the ring), welded into one Shell via
             sewShape. The returned shape's free_edge_count is the QA
             signal: 0 means the wrap closed perfectly; > 0 means the
             closure seam has a residual gap the user should be told
             about (see issues.md: "wrap-around seam is the weak point").

    Raises: ValueError for < 3 faces, or when any pair of consecutive
             faces has no detectable facing boundary (the caller gave
             the faces in the wrong order, or two of them don't actually
             face each other).
    """
    faces = [_face_from_input(f) for f in (face_list or [])]
    faces = [f for f in faces if f is not None]
    if len(faces) < 3:
        raise ValueError(
            "blend_surface_closed: need >= 3 faces forming a closed loop "
            "(got %d)" % len(faces))

    order = {"G0": 0, "G1": 1, "G2": 2}.get(str(continuity), 2)

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return _cross_tan_from_surface_tangent(du, dv, side)

    def _cross_d2(face, pr, side, u, v):
        # Analytic first (see _surf_dn) -- finite difference only as fallback.
        duo, dvo = (2, 0) if side < 2 else (0, 2)
        analytic = _surf_dn(face, u, v, duo, dvo, {})
        if analytic is not None:
            return analytic
        # Fall back to finite differences
        eps = _compute_finite_difference_epsilon(pr, 0 if side < 2 else 1)
        if side < 2:
            # Derivative in u direction
            return _finite_difference_second_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                -1.0 if side == 0 else 1.0, 0.0, eps)
        else:
            # Derivative in v direction
            return _finite_difference_second_derivative(
                lambda u, v: face.valueAt(u, v), u, v,
                0.0, -1.0 if side == 2 else 1.0, eps)

    N = len(faces)
    # Locate the facing-side pairs ONCE, up front -- each consecutive pair
    # in the ring shares one boundary; that's the boundary the cross-
    # section will be anchored to.
    pair_info = []
    for i in range(N):
        a, b = faces[i], faces[(i + 1) % N]
        sa, sb, a_pr, b_pr = _nearest_facing_sides(a, b)
        pair_info.append((a, b, sa, sb, a_pr, b_pr))

    # Build N-1 sub-strips, one per ring member (last one is the
    # wrap-around that closes the ring: face N-1 -> face 0). The Hermite
    # cross-section basis is identical to blend_surface's; the t parameter
    # here runs 0..1 ACROSS the i-th ring member (NOT across the whole
    # loop -- that would require per-pair span estimation which adds
    # nothing; n is set by the caller as a global resolution budget).
    sub_faces = []
    edge_caches = [{} for _ in range(N)]
    for i in range(N):
        face_a, face_b, sa, sb, a_pr, b_pr = pair_info[i]
        ec = edge_caches[i]
        grid = []
        for k in range(n):
            t = k / (n - 1.0)
            ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, ec)
            ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, ec)
            P0 = face_a.valueAt(ua, va)
            P1 = face_b.valueAt(ub, vb)
            chord = max((P1 - P0).Length, 1e-9)
            h = chord * scale / 3.0
            if order == 0:
                row = [P0 + (P1 - P0) * j / (n - 1.0) for j in range(n)]
            elif order == 1:
                T0 = _cross_tan(face_a, sa, ua, va)
                T0 = T0 * (h / max(T0.Length, 1e-9))
                T1 = _cross_tan(face_b, sb, ub, vb)
                T1 = T1 * (h / max(T1.Length, 1e-9))
                row = [_hermite3(P0, T0, P1, T1 * -1.0, j / (n - 1.0))
                       for j in range(n)]
            else:
                T0 = _cross_tan(face_a, sa, ua, va)
                T0 = T0 * (h / max(T0.Length, 1e-9))
                K0 = _cross_d2(face_a, a_pr, sa, ua, va) * (chord * chord / 2.0)
                T1 = _cross_tan(face_b, sb, ub, vb)
                T1 = T1 * (h / max(T1.Length, 1e-9))
                K1 = _cross_d2(face_b, b_pr, sb, ub, vb) * (chord * chord / 2.0)
                row = [_hermite5(P0, T0, K0, P1, T1 * -1.0, K1 * -1.0,
                                j / (n - 1.0)) for j in range(n)]
            grid.append(row)
        sub_faces.append(grid_to_surface(grid).Faces[0])

    # Sew the N-1 sub-strips into one Shell. As in n_sided_patch, if
    # OCCT can't sew within tolerance, return a Compound of the strips
    # rather than raising -- each strip is individually valid.
    try:
        shell = Part.Shell(sub_faces)
        shell.sewShape()
        if shell.isNull() or not shell.Faces:
            return Part.Compound(sub_faces)
        return shell
    except Exception:
        return Part.Compound(sub_faces)