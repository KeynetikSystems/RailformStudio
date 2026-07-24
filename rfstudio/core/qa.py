# -*- coding: utf-8 -*-
"""Class-A QA: numeric continuity verification, patch-network audit, and
visual QA (curvature comb / zebra / reflection / draft-angle)."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (_nearest_facing_sides, _side_uv_trimmed, cross, dot, norm)



# --- Class-A QA: numeric continuity verification --------------------------- #
# match_surface() TARGETS a continuity order; it never reports what was
# actually achieved. Class-A surfacing lives and dies on exactly that
# number, not a visual guess -- these two functions are the measurement
# layer that was missing.
def surface_deviation(face_a, face_b, samples=24):
    """Numeric G0/G1/G2 deviation between two faces across their nearest
    facing boundary -- the QA counterpart to match_surface().

    Method: locate the matching side pair (_nearest_facing_sides, the same
    search match_surface uses internally), sample `samples` points along
    face_a's side, project each to the closest point on face_b
    (Vertex.distToShape -- exact nearest point on a general OCCT face),
    and compare position (G0), cross-boundary tangent angle (G1, degrees),
    and cross-boundary curvature-vector difference (G2).

    This is a MEASUREMENT, not a proof: it samples a finite number of
    points along one boundary, and for G1/G2 at the projected point on
    face_b it assumes the cross-boundary direction is the one belonging to
    the globally-matched side (the same approximation match_surface's own
    construction already relies on). Good enough to flag a bad seam
    numerically; not a substitute for a certified tolerance analysis.

    Returns {"max_g0":, "max_g1_deg":, "max_g2":, "side_a":, "side_b":,
    "samples": [{"t":, "p":, "g0":, "g1_deg":, "g2":}, ...]}.

    Trim-aware: samples face_a's side via _side_uv_trimmed, the same
    trim-aware helper blend_surface / match_surface already use, instead
    of the untrimmed iso-parameter line -- so a QA check run against a
    trimmed face samples points that are actually on its real material,
    rather than points a naive untrimmed walk would have placed outside
    it. (Previously this function had its own inline, non-trim-aware
    `_side_uv` -- fixed here so "what built the seam" and "what grades
    it" agree on where the boundary actually is.)
    """
    import math as _math

    sa, sb, a_pr, b_pr = _nearest_facing_sides(face_a, face_b)
    _edge_cache = {}

    def _cross_tan(face, side, u, v):
        du, dv = face.Surface.tangent(u, v)
        return {0: du.negative(), 1: du, 2: dv.negative(), 3: dv}[side]

    def _cross_d2(face, pr, side, u, v):
        u0, u1, v0, v1 = pr
        if side < 2:
            eps = max(1e-6, (u1 - u0) * 1e-3)
            sgn = -1.0 if side == 0 else 1.0
            p0 = face.valueAt(u, v)
            p1 = face.valueAt(u + sgn * eps, v)
            p2 = face.valueAt(u + sgn * 2 * eps, v)
        else:
            eps = max(1e-6, (v1 - v0) * 1e-3)
            sgn = -1.0 if side == 2 else 1.0
            p0 = face.valueAt(u, v)
            p1 = face.valueAt(u, v + sgn * eps)
            p2 = face.valueAt(u, v + sgn * 2 * eps)
        return (p0 - p1 * 2.0 + p2) * (1.0 / (eps * eps))

    rows = []
    max_g0 = max_g1 = max_g2 = 0.0
    for k in range(samples):
        t = k / (samples - 1.0)
        ua, va = _side_uv_trimmed(face_a, a_pr, sa, t, _edge_cache)
        pa = face_a.valueAt(ua, va)
        dist, pts_on, _info = Part.Vertex(pa).distToShape(face_b)
        pb = pts_on[0][1] if pts_on else pa
        try:
            ub, vb = face_b.Surface.parameter(pb)
        except Exception:
            ub, vb = _side_uv_trimmed(face_b, b_pr, sb, t, _edge_cache)

        Ta = _cross_tan(face_a, sa, ua, va)
        Tb = _cross_tan(face_b, sb, ub, vb)
        La, Lb = Ta.Length, Tb.Length
        if La > 1e-9 and Lb > 1e-9:
            cosang = max(-1.0, min(1.0, Ta.dot(Tb) / (La * Lb)))
            g1_deg = _math.degrees(_math.acos(cosang))
        else:
            g1_deg = 0.0

        Ka = _cross_d2(face_a, a_pr, sa, ua, va)
        Kb = _cross_d2(face_b, b_pr, sb, ub, vb)
        g2 = (Ka - Kb).Length

        rows.append({"t": t, "p": pa, "g0": dist, "g1_deg": g1_deg, "g2": g2})
        max_g0 = max(max_g0, dist)
        max_g1 = max(max_g1, g1_deg)
        max_g2 = max(max_g2, g2)

    return {"max_g0": max_g0, "max_g1_deg": max_g1, "max_g2": max_g2,
            "side_a": sa, "side_b": sb, "samples": rows}



def classify_continuity(report, g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2):
    """Pass/fail classification of a surface_deviation() report against
    tolerance bands: returns the achieved continuity order within
    tolerance ('G2'/'G1'/'G0'/'FAIL'), matching how a Class-A QA sign-off
    is normally phrased ("this seam reads as G1, not the G2 we wanted")."""
    if report["max_g0"] > g0_tol:
        return "FAIL"
    if report["max_g1_deg"] > g1_tol_deg:
        return "G0"
    if report["max_g2"] > g2_tol:
        return "G1"
    return "G2"



# --- Class-A QA: patch-network-scale audit ---------------------------------- #
# A real body-in-white surface model is organized (and reviewed) as dozens
# of adjacent patches, not one pair at a time. This is a QA AUDITOR across
# a whole network -- it finds every adjacent seam and grades it -- not a
# simultaneous multi-patch continuity SOLVER (that would mean re-fitting
# every patch at once to satisfy every seam constraint together, which is
# a much larger, still-unsettled-in-general numerical problem akin to the
# star-point vertex enclosure issue below). Knowing your worst seam is the
# problem this actually solves; not silently claiming to fix it for you.
def adjacency_graph(faces, gap_tol=1e-3):
    """Every pair of faces in `faces` that share a boundary (within
    gap_tol) -- the first step of any patch-network audit. Returns a list
    of (i, j) index pairs into `faces`, found the same way
    _nearest_facing_sides locates a shared boundary for one pair, applied
    pairwise across the whole list."""
    def _iso_mid(face, side):
        u0, u1, v0, v1 = face.ParameterRange
        return face.valueAt(*{0: (u0, (v0 + v1) / 2), 1: (u1, (v0 + v1) / 2),
                              2: ((u0 + u1) / 2, v0), 3: ((u0 + u1) / 2, v1)}[side])
    n = len(faces)
    mids = [[_iso_mid(f, s) for s in range(4)] for f in faces]
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            best = min(mids[i][a].distanceToPoint(mids[j][b])
                       for a in range(4) for b in range(4))
            if best <= gap_tol:
                pairs.append((i, j))
    return pairs



def network_continuity_report(faces, samples=12, gap_tol=1e-3,
                              g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2):
    """Assembly-scale continuity audit across a whole patch network.

    Finds every adjacent pair (adjacency_graph), scores each seam with
    surface_deviation() + classify_continuity(), and rolls up a
    worst-seam summary -- the same "what's the worst unresolved seam in
    this whole model" question a Class-A surfacing review actually asks,
    instead of checking pairs one at a time with match_surface alone.

    Returns {"seams": [{"i":, "j":, "report":, "grade":}, ...],
             "worst_grade":, "worst_seam": (i, j) or None,
             "isolated": [k, ...]}  -- `isolated` is patches with NO
    detected neighbour: either a real hole in the network, or a false
    isolate if gap_tol is tighter than the model's actual patch gaps.

    This is a QA AUDITOR, not a fixer: once you've reviewed the worst
    seam here and are satisfied with it, stitch_shell() is the command
    that actually welds the network's faces into one topological
    shell/solid -- audit here, stitch there, still two separate steps by
    design (see stitch_shell's own docstring).
    """
    pairs = adjacency_graph(faces, gap_tol=gap_tol)
    order = {"FAIL": 0, "G0": 1, "G1": 2, "G2": 3}
    seams = []
    touched = set()
    for i, j in pairs:
        rep = surface_deviation(faces[i], faces[j], samples=samples)
        grade = classify_continuity(rep, g0_tol=g0_tol,
                                    g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)
        seams.append({"i": i, "j": j, "report": rep, "grade": grade})
        touched.add(i)
        touched.add(j)
    isolated = [k for k in range(len(faces)) if k not in touched]
    if seams:
        worst = min(seams, key=lambda s: order[s["grade"]])
        worst_grade, worst_seam = worst["grade"], (worst["i"], worst["j"])
    else:
        worst_grade, worst_seam = None, None
    return {"seams": seams, "worst_grade": worst_grade,
            "worst_seam": worst_seam, "isolated": isolated}



def surface_review_summary(faces, pull_dir=None, samples=12, gap_tol=1e-3,
                           g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2):
    """Roll continuity and draft-angle checks into a single review summary.

    This is a lightweight workflow aid for body-surface review: it gives an
    at-a-glance answer to the two most common questions in a surfacing loop:
    1) which seam is the weakest in the current patch network, and
    2) whether the selected faces clear the chosen draft direction.
    """
    faces = list(faces or [])
    if not faces:
        return {"continuity": {"seams": [], "worst_grade": None,
                               "worst_seam": None, "isolated": []},
                "draft": {"face_count": 0, "worst_angle": None,
                          "angles": []}}

    continuity = network_continuity_report(
        faces, samples=samples, gap_tol=gap_tol,
        g0_tol=g0_tol, g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)

    draft = {"face_count": len(faces), "angles": [], "worst_angle": None}
    if pull_dir is not None:
        for face in faces:
            try:
                ang = draft_angle_analysis(face, pull_dir, bands=(0.0, 3.0, 5.0, 10.0), nu=32, nv=32)
                vals = ang.get("grid", [])
                if vals:
                    flat = [v for row in vals for v in row if v is not None]
                    draft["angles"].append(max(flat) if flat else None)
                else:
                    draft["angles"].append(None)
            except Exception:
                draft["angles"].append(None)
        draft["worst_angle"] = max((a for a in draft["angles"] if a is not None), default=None)
    else:
        draft["worst_angle"] = None
    return {"continuity": continuity, "draft": draft}



def review_recommendation(summary):
    """Turn a review summary into a simple next-step recommendation.

    The goal is to make the QA step feel more like a guided assistant and
    less like a blank report. It classifies the result into one of three
    broad states: good, watch, or fix, and suggests the next action.
    """
    cont = (summary or {}).get("continuity", {})
    draft = (summary or {}).get("draft", {})
    worst_grade = cont.get("worst_grade")
    worst_angle = draft.get("worst_angle")

    if worst_grade == "G2" and (worst_angle is None or worst_angle <= 3.0):
        return {"status": "good", "step": "Proceed to manufacturing review."}
    if worst_grade in ("G1", None) or (worst_angle is not None and worst_angle > 5.0):
        return {"status": "watch", "step": "Refine the seam or draft before moving on."}
    return {"status": "fix", "step": "Improve continuity and draft before manufacture."}



# --- native visual QA: curvature comb / zebra / reflection / draft-angle -- #
# The everyday visual-inspection tools a surfacing modeler actually lives
# in day to day -- previously pure delegations to Curves (ParametricComb /
# ZebraTool / ReflectLines / Curves_DraftAnalysis), with no native
# fallback at all. All four are standard OCCT surface/curve differential
# geometry (Curve.getD2, Face.normalAt) -- no CAGD research gap here, so
# unlike the star-point G2 problem above, this is a genuine, closeable
# implementation gap rather than a documented limitation.
def curvature_comb(edge, scale=1.0, samples=40):
    """Curvature comb 'teeth' along an edge: at each sample, a line
    segment from the curve point outward, length proportional to
    curvature, pointing along the curvature vector. Native replacement
    for Curves' ParametricComb (RFS_Combs). Returns a Part.Compound of
    line edges (each tooth its own straight Part.Edge).
    """
    fp, lp = edge.FirstParameter, edge.LastParameter
    samp = []
    max_k = 1e-12
    for i in range(samples):
        t = fp + i / (samples - 1.0) * (lp - fp)
        try:
            p, d1, d2 = edge.Curve.getD2(t)
        except Exception:
            continue
        speed2 = d1.dot(d1)
        if speed2 < 1e-12:
            continue
        # curvature vector (planar-curve form): component of d2
        # perpendicular to the tangent, scaled by 1/|d1|^2.
        proj = d2 - d1 * (d2.dot(d1) / speed2)
        k = proj.Length / speed2
        max_k = max(max_k, k)
        samp.append((p, proj, k))
    teeth = []
    for p, proj, k in samp:
        if proj.Length < 1e-9 or max_k <= 0:
            continue
        direction = proj * (1.0 / proj.Length)
        length = scale * (k / max_k)
        teeth.append(Part.makeLine(p, p + direction * length))
    return Part.Compound(teeth)



def gaussian_curvature(face, u, v):
    """Compute Gaussian curvature at point (u,v) on a face.

    Returns the Gaussian curvature K = k1 * k2 where k1 and k2 are
    principal curvatures.
    """
    try:
        surf = face.Surface
        # Try to use built-in curvature evaluation if available
        if hasattr(surf, 'GaussianCurvature'):
            return surf.GaussianCurvature(u, v)
        else:
            # Fallback: compute from first and second fundamental forms
            # First fundamental form coefficients
            ru = surf.uDerivative(u, v)  # ∂S/∂u
            rv = surf.vDerivative(u, v)  # ∂S/∂v
            E = dot(ru, ru)
            F = dot(ru, rv)
            G = dot(rv, rv)

            # Unit normal
            normal = cross(ru, rv)
            norm_normal = norm(normal)
            if norm_normal < 1e-12:
                return 0.0
            normal = normal * (1.0 / norm_normal)

            # Second fundamental form coefficients
            ruu = surf.u2Derivative(u, v)  # ∂²S/∂u²
            ruv = surf.uvDerivative(u, v)  # ∂²S/∂u∂v
            rvv = surf.v2Derivative(u, v)  # ∂²S/∂v²
            L = dot(ruu, normal)
            M = dot(ruv, normal)
            N = dot(rvv, normal)

            # Gaussian curvature K = (LN - M²) / (EG - F²)
            denom = E * G - F * F
            if abs(denom) < 1e-12:
                return 0.0
            return (L * N - M * M) / denom
    except Exception:
        return 0.0



def mean_curvature(face, u, v):
    """Compute mean curvature at point (u,v) on a face.

    Returns the mean curvature H = (k1 + k2) / 2 where k1 and k2 are
    principal curvatures.
    """
    try:
        surf = face.Surface
        # Try to use built-in curvature evaluation if available
        if hasattr(surf, 'MeanCurvature'):
            return surf.MeanCurvature(u, v)
        else:
            # Fallback: compute from first and second fundamental forms
            # First fundamental form coefficients
            ru = surf.uDerivative(u, v)  # ∂S/∂u
            rv = surf.vDerivative(u, v)  # ∂S/∂v
            E = dot(ru, ru)
            F = dot(ru, rv)
            G = dot(rv, rv)

            # Unit normal
            normal = cross(ru, rv)
            norm_normal = norm(normal)
            if norm_normal < 1e-12:
                return 0.0
            normal = normal * (1.0 / norm_normal)

            # Second fundamental form coefficients
            ruu = surf.u2Derivative(u, v)  # ∂²S/∂u²
            ruv = surf.uvDerivative(u, v)  # ∂²S/∂u∂v
            rvv = surf.v2Derivative(u, v)  # ∂²S/∂v²
            L = dot(ruu, normal)
            M = dot(ruv, normal)
            N = dot(rvv, normal)

            # Mean curvature H = (EN - 2FM + GL) / (2(EG - F²))
            denom = E * G - F * F
            if abs(denom) < 1e-12:
                return 0.0
            return (E * N - 2 * F * M + G * L) / (2.0 * denom)
    except Exception:
        return 0.0



def _ensure_vector(v):
    """Ensure argument is a Vector, converting from tuple if needed."""
    if isinstance(v, tuple):
        return Vector(*v)
    return v



def _uv_grid(face, nu, nv):
    u0, u1, v0, v1 = face.ParameterRange
    return [[(u0 + i * (u1 - u0) / (nu - 1.0), v0 + j * (v1 - v0) / (nv - 1.0))
            for j in range(nv)] for i in range(nu)]



def _sample_grid(face, scalar_fn, nu, nv):
    """Evaluate scalar_fn(u, v) once at every node of an nu x nv grid over
    face's parameter range. Returns (grid_uv, vals).

    Splitting sampling out from contour extraction (_contour_at_level
    below) lets callers with MULTIPLE iso-levels of the SAME scalar field
    -- zebra_stripes' stripe_count bands, draft_angle_analysis' threshold
    bands -- sample once and reuse, instead of re-evaluating scalar_fn
    once per level for a field that doesn't depend on the level at all.
    Previously this was a real O(nu*nv*levels) cost for no reason.
    """
    grid_uv = _uv_grid(face, nu, nv)
    vals = [[scalar_fn(u, v) for (u, v) in row] for row in grid_uv]
    return grid_uv, vals



def _contour_at_level(face, grid_uv, vals, level, nu, nv):
    """Iso-contour of a pre-sampled scalar grid at `level`, as a list of
    straight Part.Edge segments (one or two per grid cell the contour
    passes through).

    Resolves the classic marching-squares "saddle" ambiguity: a cell
    whose diagonal corners agree (both above or both below `level`) and
    adjacent corners disagree has FOUR edge crossings, which can be
    paired into two contour segments two different ways. This is decided
    with the usual asymptotic test (compare the bilinear-interpolated
    cell-center value's class to corner 0's class) rather than left to
    "whichever two crossings a loop happens to find first" -- the
    previous version of this function had no saddle handling at all and
    silently DROPPED the cell (contour gaps) whenever one occurred.
    """
    segs = []
    for i in range(nu - 1):
        for j in range(nv - 1):
            cp = (grid_uv[i][j], grid_uv[i + 1][j],
                  grid_uv[i + 1][j + 1], grid_uv[i][j + 1])
            cv = (vals[i][j], vals[i + 1][j],
                  vals[i + 1][j + 1], vals[i][j + 1])
            inside = tuple(v > level for v in cv)
            crossing = [k for k in range(4) if inside[k] != inside[(k + 1) % 4]]
            if not crossing:
                continue

            def _cross(k, cp=cp, cv=cv):
                a, b = k, (k + 1) % 4
                t = (level - cv[a]) / (cv[b] - cv[a])
                ua, va = cp[a]
                ub, vb = cp[b]
                return face.valueAt(ua + t * (ub - ua), va + t * (vb - va))

            def _add_seg(a, b):
                # A crossing can land exactly on a shared grid corner
                # (e.g. the iso-level exactly equals that corner's value),
                # which collapses two "different" crossing edges onto the
                # SAME point -- Part.makeLine rejects a zero-length line
                # ("Line through identical points"). That's a genuine
                # point-touch, not a segment, so skip it.
                p1, p2 = _cross(a), _cross(b)
                if p1.distanceToPoint(p2) < 1e-9:
                    return
                segs.append(Part.makeLine(p1, p2))

            if len(crossing) == 2:
                a, b = crossing
                _add_seg(a, b)
            elif len(crossing) == 4:
                center = sum(cv) / 4.0
                if inside[0] == (center > level):
                    pairs = ((0, 1), (2, 3))     # center joins corner0/2
                else:
                    pairs = ((3, 0), (1, 2))     # center joins corner1/3
                for a, b in pairs:
                    _add_seg(a, b)
    return segs



def _marching_squares(face, scalar_fn, level, nu=60, nv=60):
    """Sample + contour a single iso-level in one call -- convenience
    wrapper for callers that only need one level per scalar field (e.g.
    reflection_lines, where each light direction has its own field).
    Callers extracting several levels of the SAME field should call
    _sample_grid once and _contour_at_level per level instead (see
    zebra_stripes / draft_angle_analysis below) to avoid re-sampling."""
    grid_uv, vals = _sample_grid(face, scalar_fn, nu, nv)
    return _contour_at_level(face, grid_uv, vals, level, nu, nv)



def zebra_stripes(face, view_dir=None, stripe_count=24, nu=60, nv=60):
    """Zebra-stripe analysis lines. Native replacement for Curves'
    ZebraTool (RFS_Zebra).

    Rendered zebra shading is a striped environment map; here the
    equivalent is computed directly in surface space: at each (u, v), the
    angle between the surface normal and a fixed `view_dir` is evaluated,
    and contours are extracted at `stripe_count` evenly spaced angle
    bands. Kinks, flats, and G1/G2 breaks in the surface show up as
    bent/merged/split stripes exactly as in the rendered version, but as
    exact curves ON the surface rather than a texture, so they can be
    measured (e.g. with surface_deviation on the underlying patches)
    instead of only eyeballed. The angle field is sampled ONCE
    (_sample_grid) and every stripe level is contoured from that same
    grid (_contour_at_level) -- the field doesn't depend on the stripe
    level, so there's no reason to re-evaluate normalAt() per stripe.
    """
    if len(face.Faces if hasattr(face, "Faces") else []) and \
            not isinstance(face, Part.Face):
        face = face.Faces[0]
    view_dir = Vector(view_dir) if view_dir is not None else Vector(0, 0, 1)
    if view_dir.Length < 1e-9:
        view_dir = Vector(0, 0, 1)
    view_dir.normalize()

    def angle_at(u, v):
        n = face.normalAt(u, v)
        n.normalize()
        return math.degrees(math.acos(max(-1.0, min(1.0, n.dot(view_dir)))))

    grid_uv, vals = _sample_grid(face, angle_at, nu, nv)
    lines = []
    for k in range(1, stripe_count):
        level = 180.0 * k / stripe_count
        lines.extend(_contour_at_level(face, grid_uv, vals, level, nu, nv))
    return Part.Compound(lines)



def standard_light_rig():
    """Three canonical parallel-light-bar directions for reflection-line
    analysis: horizontal, vertical, and 45-degree diagonal.

    This replaces an earlier arbitrary 5-direction rotation fan with a
    small, DOCUMENTED default: horizontal and vertical bars are the two
    orientations a Class-A review actually checks first (horizontal bars
    catch kinks running across a hood/door's main sweep direction,
    vertical bars catch them along pillars/fenders), with one diagonal
    bar as a general-purpose third check. It is still just a reasonable
    default, not a certified studio rig -- pass a project-specific
    `light_dirs` list to reflection_lines() to match a real light-bar
    layout (e.g. an actual studio's overhead fixture positions).
    """
    return [Vector(1, 0, 0), Vector(0, 1, 0), Vector(1, 1, 0)]



def reflection_lines(face, light_dirs, view_dir=None, tol_deg=2.0, nu=60, nv=60):
    """Reflection-line analysis. Native replacement for Curves'
    ReflectLines (RFS_Reflect).

    For each direction in `light_dirs` (parallel 'light bars' -- see
    standard_light_rig() for a documented default fan), extracts the
    curve on `face` where the mirror reflection of `view_dir` off the
    surface normal is, within `tol_deg`, parallel to that light
    direction -- the classic Class-A check of whether a straight
    fluorescent tube reflects as a straight (not kinked/wavy) line.
    """
    if hasattr(face, "Faces") and face.Faces and not isinstance(face, Part.Face):
        face = face.Faces[0]
    view_dir = Vector(view_dir) if view_dir is not None else Vector(0, 0, 1)
    if view_dir.Length < 1e-9:
        view_dir = Vector(0, 0, 1)
    view_dir.normalize()
    out = []
    for light in light_dirs:
        L = Vector(light)
        if L.Length < 1e-9:
            continue
        L.normalize()

        def dev(u, v, L=L):
            n = face.normalAt(u, v)
            n.normalize()
            r = view_dir - n * (2.0 * view_dir.dot(n))     # reflect(view, n)
            if r.Length < 1e-9:
                return 90.0
            r.normalize()
            return math.degrees(math.acos(max(-1.0, min(1.0, abs(r.dot(L))))))

        out.extend(_marching_squares(face, dev, tol_deg, nu, nv))
    return Part.Compound(out)



def draft_angle_analysis(face, pull_dir, bands=(0.0, 3.0, 5.0, 10.0),
                         nu=60, nv=60):
    """Draft-angle analysis. Native replacement for Curves'
    Curves_DraftAnalysis (RFS_DraftAnalysis).

    Draft angle at (u, v) is 90 minus the angle between the surface
    normal and `pull_dir` (the mold-opening / tool-withdrawal axis) --
    positive means the surface clears the tool, negative is an undercut.
    Returns a dict:
      {"bands": [Part.Compound of contour edges at each threshold in
                 `bands`, same order],
       "grid":  nu x nv list-of-lists of the draft angle in degrees
                (for a GUI layer to color as a heat-map overlay),
       "thresholds": list(bands)}
    The default bands (0/3/5/10 degrees) are the usual automotive
    undercut / minimum-draft callout set. The angle field is sampled
    ONCE (_sample_grid) and reused both for "grid" and for every
    threshold in "bands" (_contour_at_level) -- previously "grid" and
    each band's contour independently re-evaluated normalAt() over the
    whole surface, once each, for a field that's identical across all of
    them.
    """
    if hasattr(face, "Faces") and face.Faces and not isinstance(face, Part.Face):
        face = face.Faces[0]
    pull = Vector(pull_dir)
    if pull.Length < 1e-9:
        raise ValueError("draft_angle_analysis: pull_dir must be non-zero")
    pull.normalize()

    def angle_at(u, v):
        n = face.normalAt(u, v)
        n.normalize()
        return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, n.dot(pull)))))

    grid_uv, vals = _sample_grid(face, angle_at, nu, nv)
    band_compounds = [Part.Compound(_contour_at_level(face, grid_uv, vals, b, nu, nv))
                      for b in bands]
    return {"bands": band_compounds, "grid": vals, "thresholds": list(bands)}

__all__ = [
    'surface_deviation',
    'classify_continuity',
    'adjacency_graph',
    'network_continuity_report',
    'surface_review_summary',
    'review_recommendation',
    'curvature_comb',
    'gaussian_curvature',
    'mean_curvature',
    '_ensure_vector',
    '_uv_grid',
    '_sample_grid',
    '_contour_at_level',
    '_marching_squares',
    'zebra_stripes',
    'standard_light_rig',
    'reflection_lines',
    'draft_angle_analysis',
]
