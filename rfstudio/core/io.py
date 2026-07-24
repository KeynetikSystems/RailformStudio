# -*- coding: utf-8 -*-
"""Mesh/scan -> NURBS fitting and hidden-line drawing / SVG export."""

import math
import Part
import FreeCAD                      # Console only -- not Gui, works headless
from FreeCAD import Vector

from .util import (grid_to_surface)
from .patches import (_perp)



# --- mesh/scan -> single-patch NURBS fit (RFS_MeshToSurface) -------------- #
# Reverse-engineering gap: import a scanned/meshed panel and get an
# editable NURBS surface back (mirrors Plasticity Studio's PolySplines).
# HONESTY ABOUT SCOPE, up front: this is a SINGLE-PATCH, HEIGHT-FIELD
# fitter, not a general mesh retopology / quad-layout engine. It assumes
# the point cloud is (once projected along `projection_normal`, or the
# auto-detected flattest bounding-box axis) a disk-topology height field --
# i.e. for every (u, v) there is at most one meaningful surface height, the
# way a scanned body panel, a hood, or a terrain patch is. It CANNOT
# reconstruct closed/tubular/branching mesh topology (a full wraparound
# fender, a handle with a hole through it, ...) -- that is a genuinely
# different (patch-layout / multi-patch quad-remeshing) problem, well
# outside what a single BSplineSurface's parameter domain can represent,
# and is not attempted here.
def fit_surface_to_scan(points, nu=24, nv=24, degree=3, projection_normal=None):
    """Fit a single approximating B-spline PATCH to a raw, unstructured
    point cloud (mesh vertices, scan data -- no triangle/face
    connectivity used or required).

    Method: project every point onto the (u, v) plane perpendicular to
    the fit direction, bin into an nu x nv grid, AVERAGE the height of
    every point landing in each bin -- this IS the smoothing step; it is
    binning + averaging, not a least-squares surface fit (FreeCAD's
    BSplineSurface has no direct Python binding for scattered-point
    least-squares approximation the way BSplineCurve.approximate() does
    for curves -- see approximate_curve above). Coarser nu/nv means more
    smoothing (and a fairer, lower-pole-count result); finer nu/nv means
    closer to the raw scan and more susceptible to noise. Empty bins
    (sparse regions of the cloud) are filled by nearest-filled-neighbor,
    a simple flood fill, not a real hole-filling algorithm. The resulting
    grid is fed through the same exact-interpolation path
    grid_to_surface/_grid_interpolation_surface already use elsewhere in
    this module -- just fed averaged/rasterized data instead of exact
    control points or crossing points.

    `points`: iterable of Vector (or objects with .x/.y/.z).
    `nu`, `nv`: fitting grid resolution; must be >= 2.
    `projection_normal`: Vector, direction perpendicular to the intended
                 UV plane (e.g. Vector(0,0,1) for a mesh scanned from
                 above/a hood-like panel lying roughly in the XY plane).
                 If None, auto-picks whichever of the point cloud's
                 bounding-box axes has the smallest extent (the "this is
                 basically flat along this axis" heuristic) -- a
                 reasonable default for a single scanned panel, not a
                 substitute for telling the fitter the real scan
                 direction when you know it.

    Raises ValueError for < 4 points, nu/nv < 2, a zero-length
    `projection_normal`, a point cloud that is degenerate (zero extent)
    in the fitted UV plane, or a grid where every single bin came up
    empty (bin count too fine for how sparse the cloud is -- lower nu/nv).
    """
    pts = []
    for p in points or []:
        if isinstance(p, Vector):
            pts.append(p)
        else:
            pts.append(Vector(p.x, p.y, p.z))
    if len(pts) < 4:
        raise ValueError("fit_surface_to_scan: need >= 4 points (got %d)" % len(pts))
    nu, nv = int(nu), int(nv)
    if nu < 2 or nv < 2:
        raise ValueError("fit_surface_to_scan: nu and nv must be >= 2")

    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    extents = {"x": max(xs) - min(xs), "y": max(ys) - min(ys),
              "z": max(zs) - min(zs)}

    if projection_normal is not None:
        normal = Vector(projection_normal)
        if normal.Length < 1e-9:
            raise ValueError(
                "fit_surface_to_scan: projection_normal must be non-zero")
        normal = normal * (1.0 / normal.Length)
    else:
        flattest = min(extents, key=lambda k: extents[k])
        normal = {"x": Vector(1, 0, 0), "y": Vector(0, 1, 0),
                 "z": Vector(0, 0, 1)}[flattest]

    ex = _perp(normal)
    ey = normal.cross(ex)
    if ey.Length < 1e-9:
        raise ValueError("fit_surface_to_scan: degenerate projection basis")
    ey = ey * (1.0 / ey.Length)

    uvh = [(p.dot(ex), p.dot(ey), p.dot(normal)) for p in pts]
    us = [u for u, _v, _h in uvh]
    vs = [v for _u, v, _h in uvh]
    u0, u1 = min(us), max(us)
    v0, v1 = min(vs), max(vs)
    if (u1 - u0) < 1e-9 or (v1 - v0) < 1e-9:
        raise ValueError(
            "fit_surface_to_scan: point cloud is degenerate in the "
            "fitted UV plane -- pick a different projection_normal")

    sums = [[0.0] * nv for _ in range(nu)]
    counts = [[0] * nv for _ in range(nu)]
    for u, v, h in uvh:
        i = min(nu - 1, int((u - u0) / (u1 - u0) * nu))
        j = min(nv - 1, int((v - v0) / (v1 - v0) * nv))
        sums[i][j] += h
        counts[i][j] += 1

    heights = [[(sums[i][j] / counts[i][j]) if counts[i][j] else None
               for j in range(nv)] for i in range(nu)]

    filled = [(i, j) for i in range(nu) for j in range(nv)
             if heights[i][j] is not None]
    if not filled:
        raise ValueError(
            "fit_surface_to_scan: no bin received any points -- lower "
            "nu/nv (coarser grid) or check the point cloud")
    missing = [(i, j) for i in range(nu) for j in range(nv)
              if heights[i][j] is None]
    for i, j in missing:
        best, best_d = filled[0], 1e18
        for fi, fj in filled:
            d = (fi - i) ** 2 + (fj - j) ** 2
            if d < best_d:
                best_d, best = d, (fi, fj)
        heights[i][j] = heights[best[0]][best[1]]

    grid = []
    for i in range(nu):
        u = u0 + (u1 - u0) * i / (nu - 1.0)
        row = []
        for j in range(nv):
            v = v0 + (v1 - v0) * j / (nv - 1.0)
            row.append(ex * u + ey * v + normal * heights[i][j])
        grid.append(row)
    return grid_to_surface(grid)



# --- hidden-line drawing projection + SVG export (RFS_HiddenLineExport) -- #
# No drafting/technical-drawing output existed at all. This closes that
# gap in two independently-testable pieces:
#   1. hidden_line_projection() -- the actual HLR (hidden line removal)
#      math, via FreeCAD's headless `Drawing` module (a thin Python
#      binding over OCCT's HLRBRep, the same engine the old Drawing
#      workbench and TechDraw's view-generation both build on). This part
#      genuinely needs FreeCAD's `Drawing` module to be importable; it is
#      NOT part of the Gui-only layer (no FreeCADGui import), but it is
#      also not exercised by test_core.py's freecadcmd run if that build
#      lacks the Drawing module -- see the function's own guard.
#   2. export_svg_drawing() -- turns any two edge-collections (visible /
#      hidden) into a plain SVG file: solid strokes for visible, dashed
#      for hidden. This half has NO dependency on Drawing/HLR at all (it
#      only calls Edge.valueAt), so it is fully headless-testable on its
#      own with any two edge compounds, independent of whether HLR
#      projection succeeded.
def hidden_line_projection(shapes, direction):
    """Project `shapes` along `direction` and split the silhouette into
    VISIBLE and HIDDEN edge compounds via OCCT hidden-line removal
    (FreeCAD's `Drawing` module).

    Returns {"visible": Part.Compound, "hidden": Part.Compound,
    "direction": normalized Vector}.

    Raises ValueError for an empty/null input or a zero-length
    direction. Raises RuntimeError if the `Drawing` module can't be
    imported (some FreeCAD builds trim it) or if the underlying HLR
    projection itself fails -- callers should treat either as "drafting
    export isn't available in this FreeCAD build/session", not retry.

    VERSION CAVEAT: `Drawing.projectEx`'s exact return arity has varied
    across FreeCAD releases (some return a 6-tuple of
    visible-sharp/visible-outline/visible-iso/hidden-sharp/hidden-
    outline/hidden-iso edge groups, others fewer). Rather than hardcode
    one arity, this splits whatever tuple comes back in half (first half
    treated as every visible-edge group, second half as every
    hidden-edge group) and compounds each half -- correct for the common
    even-length "visible groups then hidden groups" layouts, but not
    independently verified against every FreeCAD version. If your build's
    `projectEx` doesn't follow that convention, `export_svg_drawing`
    below will still work correctly on whatever `visible`/`hidden`
    compounds you construct yourself.
    """
    combined = Part.Compound([getattr(s, "Shape", s) for s in (shapes or [])])
    if combined.isNull() or not combined.Edges:
        raise ValueError(
            "hidden_line_projection: nothing to project (empty/null shape)")
    d = Vector(direction)
    if d.Length < 1e-9:
        raise ValueError("hidden_line_projection: direction must be non-zero")
    d = d * (1.0 / d.Length)
    try:
        import Drawing
    except Exception as exc:
        raise RuntimeError(
            "hidden_line_projection: FreeCAD's 'Drawing' module isn't "
            "available in this session (%s) -- drafting export needs it" % exc)
    try:
        result = Drawing.projectEx(combined, d)
    except Exception as exc:
        raise RuntimeError(
            "hidden_line_projection: HLR projection failed (%s)" % exc)
    groups_ = list(result) if isinstance(result, (list, tuple)) else [result]
    half = max(1, len(groups_) // 2)
    visible = Part.Compound([g for g in groups_[:half] if g is not None])
    hidden = Part.Compound([g for g in groups_[half:] if g is not None])
    return {"visible": visible, "hidden": hidden, "direction": d}



def export_svg_drawing(visible_edges, hidden_edges, direction, path,
                       stroke_visible="#000000", stroke_hidden="#999999",
                       hidden_dash="4,3", scale=1.0, margin=10.0,
                       samples_per_edge=24):
    """Write a plain SVG technical drawing from two edge collections --
    the orthographic 2D projection of `visible_edges` as solid strokes
    and `hidden_edges` as dashed strokes, onto the plane perpendicular to
    `direction`.

    `visible_edges`/`hidden_edges`: anything exposing an .Edges list
                 (a Part.Compound, Part.Shape, or a plain list of
                 Part.Edge) -- either may be empty.
    `direction`: the same projection direction used to build the edges
                 (e.g. hidden_line_projection's "direction"), or any
                 direction if you're just flattening arbitrary edges to
                 a plane yourself.
    `path`: output .svg file path.

    This function has NO dependency on HLR/the Drawing module -- it only
    calls Edge.valueAt/FirstParameter/LastParameter, so it can be (and
    is, in test_core.py) exercised directly with plain Part.makeLine
    edges, independent of whether hidden_line_projection's HLR call
    succeeds in a given FreeCAD build.

    Raises ValueError for a zero-length direction or if both edge
    collections are empty (nothing to draw).
    """
    d = Vector(direction)
    if d.Length < 1e-9:
        raise ValueError("export_svg_drawing: direction must be non-zero")
    d = d * (1.0 / d.Length)
    ex = _perp(d)
    ey = d.cross(ex)
    ey = ey * (1.0 / ey.Length)
    origin = Vector(0, 0, 0)

    def _proj2d(p):
        v = p - origin
        return (v.dot(ex), v.dot(ey))

    def _polylines(coll):
        edges = list(getattr(coll, "Edges", coll) or [])
        out = []
        for e in edges:
            try:
                fp, lp = e.FirstParameter, e.LastParameter
                n = max(2, int(samples_per_edge))
                pts = [e.valueAt(fp + k / (n - 1.0) * (lp - fp)) for k in range(n)]
            except Exception:
                continue
            out.append([_proj2d(p) for p in pts])
        return out

    vis_lines = _polylines(visible_edges)
    hid_lines = _polylines(hidden_edges)
    all_pts = [p for line in (vis_lines + hid_lines) for p in line]
    if not all_pts:
        raise ValueError("export_svg_drawing: no edges to draw")

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = (maxx - minx) * scale + 2 * margin
    h = (maxy - miny) * scale + 2 * margin
    if w < 1e-6:
        w = 2 * margin
    if h < 1e-6:
        h = 2 * margin

    def _to_svg(p):
        # SVG's y axis points down; flip so the drawing reads right-side-up.
        return (margin + (p[0] - minx) * scale,
                margin + (maxy - p[1]) * scale)

    lines_svg = []
    for line in vis_lines:
        pts_s = " ".join("%.3f,%.3f" % _to_svg(p) for p in line)
        lines_svg.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1"/>'
            % (pts_s, stroke_visible))
    for line in hid_lines:
        pts_s = " ".join("%.3f,%.3f" % _to_svg(p) for p in line)
        lines_svg.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1" '
            'stroke-dasharray="%s"/>' % (pts_s, stroke_hidden, hidden_dash))

    svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<svg xmlns="http://www.w3.org/2000/svg" width="%.3f" height="%.3f" '
          'viewBox="0 0 %.3f %.3f">\n%s\n</svg>\n'
          % (w, h, w, h, "\n".join(lines_svg)))
    with open(path, "w") as fh:
        fh.write(svg)
    return path

def enforce_bezier_handle_constraints(point_idx, which, left_handles, right_handles, handle_types, points):
    """Apply handle type constraints after editing a handle.

    `which` ('left' or 'right') identifies which handle was JUST
    dragged, so the other one can be adjusted to match.

    Args:
        point_idx: Index of the point whose handle was dragged
        which: 'left' or 'right' indicating which handle was dragged
        left_handles: List of left handle positions (modified in place)
        right_handles: List of right handle positions (modified in place)
        handle_types: List of handle type strings for each point
        points: List of point positions
    """
    if point_idx >= len(handle_types):
        return

    htype = handle_types[point_idx]
    point = points[point_idx] if point_idx < len(points) else Vector(0, 0, 0)
    left = left_handles[point_idx]
    right = right_handles[point_idx]

    if htype == "Auto":
        # Auto: Handles adjust to maintain smooth curve (simplified)
        # In a full implementation, this would calculate based on neighbors
        pass
    elif htype == "Aligned":
        # Aligned: Handles opposite (180 deg) but lengths can vary
        if which == 'left' and (left - point).Length > 1e-9:
            left_dir = (left - point).normalize()
            right_handles[point_idx] = point - left_dir * (right - point).Length
        elif which == 'right' and (right - point).Length > 1e-9:
            right_dir = (right - point).normalize()
            left_handles[point_idx] = point - right_dir * (left - point).Length
    elif htype == "Vector":
        # Vector: Handles locked together (same angle, can scale independently)
        if which == 'left' and (left - point).Length > 1e-9:
            left_dir = (left - point).normalize()
            right_handles[point_idx] = point + left_dir * (right - point).Length
        elif which == 'right' and (right - point).Length > 1e-9:
            right_dir = (right - point).normalize()
            left_handles[point_idx] = point + right_dir * (left - point).Length

__all__ = [
    'fit_surface_to_scan',
    'hidden_line_projection',
    'export_svg_drawing',
    'enforce_bezier_handle_constraints',
]
