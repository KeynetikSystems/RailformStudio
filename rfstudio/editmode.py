# -*- coding: utf-8 -*-
"""In-viewport point editing for curves -- ViewProvider edit mode, NO SoDragger.

Coin draggers (SoTranslate1Dragger/SoDragPointDragger) reliably access-violate
when driven from our code in this FreeCAD build, so editing is done the way Draft
does it: static geometry (markers + RGB axis arrows) plus manual mouse handling
via addEventCallback. Cursor motion is projected onto the view plane (free drag)
or an axis (constrained). The document is committed only on mouse-up (deferred),
never mid-event -- so it can't crash.

This workaround is scoped to a SPECIFIC observed Coin/FreeCAD build
combination, not a documented upstream limitation -- the crash has not been
reproduced against every FreeCAD/Coin version this workbench might run on.
Rather than hardcode that assumption permanently, it's exposed as a
preference (Railform Studio -> "UseNativeDraggers", default off) so it can
be re-tested and flipped on a build where SoDragger turns out to be fine,
without a code change. See `native_draggers_enabled()` below.

Entered by double-clicking a curve (CurveVP.setEdit).

Exception-handling policy in this file: callbacks invoked FROM a live Coin
drag/mouse event (_on_move, _changed-style handlers, anything running
mid-drag) catch-and-swallow silently and MUST keep doing so -- even a
PrintWarning() call from inside one of those callbacks has reproduced the
access violation this file works around. Everywhere else (finish(),
teardown, document commits scheduled via QTimer) is a normal Qt/document
context where logging on failure is safe and is preferred over bare `pass`,
since silent failures there look identical to "nothing went wrong" from the
user's side.
"""

import math

import FreeCAD
import FreeCADGui
import Part
from FreeCAD import Vector

from rfstudio import snaputil
from rfstudio import core
from rfstudio.panel_base import drop_callbacks

try:
    from PySide import QtCore, QtGui
    try:
        from PySide import QtWidgets
    except Exception:                 # older shim: widgets live in QtGui
        QtWidgets = QtGui
except Exception:
    QtCore = None
    QtGui = None
    QtWidgets = None


# --- analytic curvature/jerk for cubic Bezier segments --- #
def _bezier_segment_derivatives(p0, p1, p2, p3, t):
    """Return (p, d1, d2, d3) for cubic Bezier at parameter t.

    p0, p1, p2, p3 are FreeCAD Vectors (4 poles).
    Returns (position, d1, d2, d3) as Vectors.
    """
    mt = 1.0 - t
    # Position
    p = (p0 * mt**3 +
         p1 * (3 * mt**2 * t) +
         p2 * (3 * mt * t**2) +
         p3 * t**3)
    # First derivative
    d1 = (p1 - p0) * (3 * mt**2) + \
         (p2 - p1) * (6 * mt * t) + \
         (p3 - p2) * (3 * t**2)
    # Second derivative
    d2 = (p2 - p1 * 2 + p0) * (6 * mt) + \
         (p3 - p2 * 2 + p1) * (6 * t)
    # Third derivative (constant for cubic)
    d3 = (p3 - p2 * 3 + p1 * 3 - p0) * 6.0
    return p, d1, d2, d3


def _bezier_curvature_and_jerk(p0, p1, p2, p3, t):
    """Return (curvature, jerk_magnitude) for cubic Bezier at t.

    Returns (curvature, jerk_magnitude) as floats, or (None, None) if degenerate.
    """
    p, d1, d2, d3 = _bezier_segment_derivatives(p0, p1, p2, p3, t)
    speed2 = d1.dot(d1)
    if speed2 < 1e-12:
        return None, None
    # Curvature: |d1 x d2| / |d1|^3
    cross = d1.cross(d2)
    curvature = cross.Length / (speed2 ** 1.5)
    # Jerk: magnitude of d3 projected perpendicular to d1 (similar to curvature but for 3rd derivative)
    # Component of d3 perpendicular to velocity
    speed = speed2 ** 0.5
    if speed < 1e-12:
        return curvature, None
    d1_norm = d1 * (1.0 / speed)
    d3_perp = d3 - d1_norm * d3.dot(d1_norm)
    jerk = d3_perp.Length / (speed2 * speed)  # |d3_perp| / |v|^3
    return curvature, jerk


def _bezier_g2_g3_at_handles(p0, p1, p2, p3):
    """Return (g2_at_start, g3_at_start, g2_at_end, g3_at_end) for a Bezier segment.

    Returns curvature/jerk at t=0 (toward left handle) and t=1 (toward right handle).
    """
    g2_start, g3_start = _bezier_curvature_and_jerk(p0, p1, p2, p3, 0.0)
    g2_end, g3_end = _bezier_curvature_and_jerk(p0, p1, p2, p3, 1.0)
    return g2_start, g3_start, g2_end, g3_end


def _bezier_g2_g3_along_segment(p0, p1, p2, p3, samples=20):
    """Sample G2/G3 along the segment for comb visualization.

    Returns list of (t, g2, g3) for each sample.
    """
    out = []
    for i in range(samples):
        t = i / (samples - 1.0) if samples > 1 else 0.5
        g2, g3 = _bezier_curvature_and_jerk(p0, p1, p2, p3, t)
        if g2 is not None:
            out.append((t, g2, g3))
    return out


try:
    from PySide import QtCore, QtGui
    try:
        from PySide import QtWidgets
    except Exception:                 # older shim: widgets live in QtGui
        QtWidgets = QtGui
except Exception:
    QtCore = None
    QtGui = None
    QtWidgets = None

from rfstudio import snaputil
from rfstudio import core


def native_draggers_enabled():
    """Whether to trust SoTranslate1Dragger/SoDragPointDragger on this
    build, instead of the manual addEventCallback editors in this file.

    Defaults to False (the manually-driven editors below), matching the
    crash this module was written to work around. Override via FreeCAD's
    parameter editor: BaseApp/Preferences/Mod/RailformStudio ->
    UseNativeDraggers (bool). Not wired into PointEditor/KnotEditor's
    construction yet -- they always use the manual path today -- but a
    single, named, inspectable switch is provided here so re-enabling
    native draggers on a build where they're confirmed safe doesn't
    require touching this file again.
    """
    try:
        grp = FreeCAD.ParamGet(
            "User parameter:BaseApp/Preferences/Mod/RailformStudio")
        return bool(grp.GetBool("UseNativeDraggers", False))
    except Exception:
        return False

_HALF_PI = math.pi / 2.0

_AXES = (Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1))
_AXIS_RGB = ((0.95, 0.25, 0.25), (0.35, 0.85, 0.35), (0.30, 0.55, 1.0))
# Pick and snap tolerances are now instance-specific, computed in __init__ based on screen DPI.

# Thin local alias so the rest of this file reads the same as before the
# dedup -- the implementation now lives in snaputil.py (shared with
# taskpanels.CurvePanel's 3D pick mode; see that module's snap handling).
_seg_dist = snaputil.seg_dist


def _closest_on_line_to_ray(p0, u, ro, rd):
    """Point on the axis line (p0 + s*u) closest to the cursor ray (ro + t*rd).

    This is the exact way to drag along an axis: it stays on the axis line and
    tracks the cursor ray, so it doesn't suffer the depth-scale / foreshortening
    error of projecting a focal-plane delta onto the axis. Exact for an
    orthographic camera (rd = view direction); a good approximation for
    perspective. Falls back to p0 when the axis points nearly straight at the
    camera (the drag is then ill-defined)."""
    rl = rd.Length
    if rl < 1e-12:
        return p0
    rd = rd * (1.0 / rl)
    w0 = p0 - ro
    a = u.dot(u)
    b = u.dot(rd)
    d = u.dot(w0)
    e = rd.dot(w0)
    denom = a - b * b                      # a*c - b*b with c = rd.rd = 1
    if abs(denom) < 1e-9:
        return p0
    s = (b * e - d) / denom                # (b*e - c*d)/denom, c = 1
    return p0 + u * s


class PointEditor(object):

    def __init__(self, vobj):
        self.vobj = vobj
        self.obj = vobj.Object
        doc = FreeCADGui.ActiveDocument
        self.view = doc.ActiveView if doc is not None else None
        # Compute DPI-aware tolerances
        if self.view is not None:
            self._snap_px = snaputil._snap_tolerance_mm(self.view, 4.0)   # 4 mm
            self._pick_px = snaputil._snap_tolerance_mm(self.view, 2.5) # 2.5 mm
        else:
            self._snap_px = 18.0
            self._pick_px = 14.0
        self.sel = 0
        self.drag = None
        self._watchdog = None
        self._snap_pts = []
        self._snap_shapes = []
        self._hover_pts_cache = None    # lazily built for hover-snap preview
        self._hover_shapes_cache = None
        self._sym_pairs = None      # {idx: partner_idx} or None (off)
        self._sym_plane = None      # (base, normal) or None
        self.on_commit = None       # called after each drag commit (panel refresh)
        self._cb_btn = None
        self._cb_mov = None
        self.ok = False
        self.creating = False       # set by panel when in creation mode
        self.preview_object = None  # for symmetry preview during creation
        # context-aware point insertion/deletion
        self.hover_point = None     # index of point under cursor for deletion
        self.hover_segment = None   # index of segment under cursor for insertion
        # Inserting a point by clicking a segment is OPT-IN: a plain click
        # selects/drags points instead. Hold Shift while clicking a segment
        # (read live via _shift_held) OR turn on the panel's "insert point on
        # click" toggle (self.insert_on_click).
        self.insert_on_click = False
        # dynamic precision input
        self.dragging_with_ctrl = False  # True when Ctrl is held during drag
        self.input_field = None          # Qt line edit for coordinate input
        # Bezier-specific attributes
        self.is_bezier = False
        self.handle_vis_root = None
        self.handles_visible = True
        # Overlay handles read by teardown/update paths for EVERY curve kind
        # (previously only assigned for Bezier -> finish() raised
        # AttributeError on every non-Bezier curve, skipping callback removal).
        self.comb_vis_root = None
        self.jerk_vis_root = None
        self._hl = None             # hover-marker coord node (built in _build)
        self._hl_markers = None
        self.dragging_handle = None  # (point_idx, 'left'|'right') or None
        self.hover_handle = None     # (point_idx, 'left'|'right') or None
        # Tangent-to-face virtual intersection handles (issue #9)
        self._tangent_targets = {}   # point_idx -> (face_obj, face_name, sub_element)
        try:
            from pivy import coin
            self.coin = coin
            self._build()
            self._cb_btn = self.view.addEventCallback(
                "SoMouseButtonEvent", self._on_button)
            self._cb_mov = self.view.addEventCallback(
                "SoLocation2Event", self._on_move)
            self._cb_key = self.view.addEventCallback(
                "SoKeyboardEvent", self._on_key)
            self.ok = True
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: point editor unavailable (%s)\n" % exc)

    # ---- symmetry preview during creation ---- #
    def set_creating(self, creating):
        """Set whether we are in the process of creating a new curve.
        This enables/disables the symmetry preview feature."""
        self.creating = creating
        if not creating and self.preview_object:
            self._destroy_symmetry_preview()

    def _create_symmetry_preview(self):
        """Create a temporary visual preview of the mirrored curve during creation."""
        if not self.creating or not self._sym_plane:
            return
        pts = self._points()
        if len(pts) < 2:  # need at least two points to show a curve
            return
        import Part
        # Create a polyline from the current points (for preview during creation)
        try:
            polyline = Part.makePolygon([Vector(p.x, p.y, p.z) for p in pts])
        except Exception:
            return
        base, normal = self._sym_plane
        from rfstudio import mirror as rmirror
        mirrored_polyline = rmirror.mirror_shape(polyline, base, normal)
        # Create a visual object in the document
        doc = self.doc if hasattr(self, 'doc') else None
        if doc is None:
            doc = self.obj.Document
        self.preview_object = doc.addObject("Part::Feature", "SymmetryPreview")
        self.preview_object.Shape = mirrored_polyline
        self.preview_object.ViewObject.LineColor = (1.0, 0.5, 0.0)  # orange
        self.preview_object.ViewObject.DrawStyle = "Dashed"
        self.preview_object.ViewObject.LineWidth = 2
        doc.recompute()

    def _update_symmetry_preview(self):
        """Update the symmetry preview with the current points."""
        if not self.creating or not self._sym_plane or not self.preview_object:
            return
        pts = self._points()
        if len(pts) < 2:
            self._destroy_symmetry_preview()
            return
        import Part
        try:
            polyline = Part.makePolygon([Vector(p.x, p.y, p.z) for p in pts])
        except Exception:
            return
        base, normal = self._sym_plane
        from rfstudio import mirror as rmirror
        mirrored_polyline = rmirror.mirror_shape(polyline, base, normal)
        self.preview_object.Shape = mirrored_polyline
        self.obj.Document.recompute()

    def _destroy_symmetry_preview(self):
        """Remove the symmetry preview object."""
        if self.preview_object:
            try:
                self.obj.Document.removeObject(self.preview_object.Name)
            except Exception:
                pass
            self.preview_object = None
        self.obj.Document.recompute()

    def update_symmetry(self):
        """Update the symmetry preview based on current state.
        Call this when points are added or symmetry is toggled."""
        if not self.creating:
            self._destroy_symmetry_preview()
            return
        if self._sym_plane is not None and self._sym_pairs is not None:
            # In edit mode with symmetry, we don't need a preview (the mirroring is live in the editor)
            self._destroy_symmetry_preview()
        else:
            # In creation mode, or symmetry off, we may want a preview if symmetry is on
            if self._sym_plane is not None:
                self._create_symmetry_preview()
            else:
                self._destroy_symmetry_preview()

    # ---- geometry ---- #
    def _points(self):
        return list(self.obj.Points)

    def _arrow_len(self):
        pts = self._points()
        if len(pts) < 2:
            return 8.0
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        zs = [p.z for p in pts]
        diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
                + (max(zs) - min(zs)) ** 2) ** 0.5
        return max(diag * 0.045, 1.0)       # 50% shorter (halved again)

    def _build(self):
        coin = self.coin
        self.root = coin.SoSeparator()

        # control-point markers
        mk = coin.SoSeparator()
        c = coin.SoBaseColor()
        c.rgb = (1.0, 0.8, 0.1)
        self.mk_coord = coin.SoCoordinate3()
        self.mk_set = coin.SoMarkerSet()
        try:
            self.mk_set.markerIndex = coin.SoMarkerSet.CIRCLE_FILLED_7_7
        except Exception:
            pass
        mk.addChild(c)
        mk.addChild(self.mk_coord)
        mk.addChild(self.mk_set)
        self.root.addChild(mk)

        # RGB axis arrows (shaft + cone tip) — SoSwitch so they can be hidden
        self.arrow_switch = coin.SoSwitch()
        self.arrow_switch.whichChild = 0
        arrow_grp = coin.SoSeparator()
        self.arrow_coords = []
        self.arrow_tip_trans = []   # SoTranslation: cone position
        self.arrow_cones = []       # SoCone: updated when L changes
        # per-axis rotation to align default +Y cone with world axis
        _cone_rots = [
            (coin.SoRotationXYZ.Z, -_HALF_PI),  # X: +Y -> +X
            (None, 0.0),                          # Y: already along +Y
            (coin.SoRotationXYZ.X, _HALF_PI),   # Z: +Y -> +Z
        ]
        for i, rgb in enumerate(_AXIS_RGB):
            asep = coin.SoSeparator()
            acol = coin.SoBaseColor()
            acol.rgb = rgb
            style = coin.SoDrawStyle()
            style.lineWidth = 3
            coord = coin.SoCoordinate3()
            line = coin.SoLineSet()
            line.numVertices.setValue(2)
            asep.addChild(acol)
            asep.addChild(style)
            asep.addChild(coord)
            asep.addChild(line)
            # arrowhead cone
            csep = coin.SoSeparator()
            ctrans = coin.SoTranslation()
            csep.addChild(ctrans)
            rot_axis, rot_angle = _cone_rots[i]
            if rot_axis is not None:
                crot = coin.SoRotationXYZ()
                crot.axis = rot_axis
                crot.angle = rot_angle
                csep.addChild(crot)
            cone = coin.SoCone()
            cone.height = 1.0
            cone.bottomRadius = 0.3
            csep.addChild(cone)
            asep.addChild(csep)
            arrow_grp.addChild(asep)
            self.arrow_coords.append(coord)
            self.arrow_tip_trans.append(ctrans)
            self.arrow_cones.append(cone)
        self.arrow_switch.addChild(arrow_grp)
        self.root.addChild(self.arrow_switch)

        # live preview line
        self.prev_switch = coin.SoSwitch()
        self.prev_switch.whichChild = -1
        pv = coin.SoSeparator()
        pc = coin.SoBaseColor()
        pc.rgb = (0.2, 0.85, 1.0)
        self.prev_coord = coin.SoCoordinate3()
        self.prev_line = coin.SoLineSet()
        pv.addChild(pc)
        pv.addChild(self.prev_coord)
        pv.addChild(self.prev_line)
        self.prev_switch.addChild(pv)
        self.root.addChild(self.prev_switch)

        # snap indicator: cyan ring that appears on a nearby existing point
        self._snap_switch = coin.SoSwitch()
        self._snap_switch.whichChild = -1
        snap_sep = coin.SoSeparator()
        snap_col = coin.SoBaseColor()
        snap_col.rgb = (0.1, 0.95, 0.95)
        self._snap_coord = coin.SoCoordinate3()
        snap_mk = coin.SoMarkerSet()
        try:
            snap_mk.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
        except Exception:
            pass
        snap_mk.numPoints = 1
        snap_sep.addChild(snap_col)
        snap_sep.addChild(self._snap_coord)
        snap_sep.addChild(snap_mk)
        self._snap_switch.addChild(snap_sep)
        self.root.addChild(self._snap_switch)

        # hover marker: ring on the point under the cursor (deletion cue)
        hl_sep = coin.SoSeparator()
        hl_col = coin.SoBaseColor()
        hl_col.rgb = (1.0, 0.3, 0.3)
        self._hl = coin.SoCoordinate3()
        self._hl_markers = coin.SoMarkerSet()
        try:
            self._hl_markers.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
        except Exception:
            pass
        self._hl_markers.numPoints = 0
        hl_sep.addChild(hl_col)
        hl_sep.addChild(self._hl)
        hl_sep.addChild(self._hl_markers)
        self.root.addChild(hl_sep)

        self.view.getSceneGraph().addChild(self.root)
        self._refresh_markers()
        self._refresh_arrows()

        # Determine if we're editing a BezierCurve
        self.is_bezier = (hasattr(self.obj, "Proxy") and
                         getattr(self.obj.Proxy, "Type", None) == "BezierCurve")

        # Initialize Bezier handle visualization if applicable
        if self.is_bezier:
            self._build_bezier_handles()
            self._update_bezier_handles()

    def _refresh_markers(self):
        pts = self._points()
        self.mk_coord.point.setValues(
            0, len(pts), [(p.x, p.y, p.z) for p in pts])
        self.mk_set.numPoints = len(pts)

    def _update_cones(self, p, L):
        cone_h = max(L * 0.35, 0.3)
        cone_r = max(L * 0.10, 0.09)
        for ctrans, cone, a in zip(self.arrow_tip_trans, self.arrow_cones, _AXES):
            ctrans.translation.setValue(p.x + a.x * L,
                                        p.y + a.y * L,
                                        p.z + a.z * L)
            cone.height = cone_h
            cone.bottomRadius = cone_r

    def _refresh_arrows(self):
        pts = self._points()
        if not (0 <= self.sel < len(pts)):
            self.arrow_switch.whichChild = -1
            return
        self.arrow_switch.whichChild = 0
        p = pts[self.sel]
        L = self._arrow_len()
        for i, coord in enumerate(self.arrow_coords):
            a = _AXES[i]
            coord.point.setValues(0, 2, [(p.x, p.y, p.z),
                                         (p.x + a.x * L, p.y + a.y * L,
                                          p.z + a.z * L)])
        self._update_cones(p, L)

    # ---- snap ---- #
    def _snap_candidates(self):
        """EXACT high-priority discrete targets from all other objects:
        control points, vertices, knot positions, and true curve-curve
        intersections (see snaputil.collect_strong_snap_points). These win
        over a generic on-edge projection in _snap. Coarse arc-length
        samples are intentionally excluded -- the exact ray->edge path
        already covers 'anywhere on a curve' precisely, so a sample could
        only pull a snap OFF the true curve. Shared with CurvePanel's 3D
        pick mode."""
        return snaputil.collect_strong_snap_points(self.obj.Document,
                                                   exclude=self.obj)

    def _snap_shape_candidates(self):
        """Edges from all other objects, for EXACT perpendicular-
        projection snapping (see snaputil.nearest_point_on_shapes) --
        lets a drag snap onto any arbitrary point along another curve,
        not just its vertices/samples/knots."""
        return [e for e, _o in
                snaputil.collect_snap_shapes(self.obj.Document,
                                             exclude=self.obj)]

    def _snap(self, pos):
        """Return the snapped point within self._snap_px screen pixels, or
        None. Vertex/endpoint priority: an EXACT discrete target (vertex,
        control point, knot, or curve-curve intersection) within tolerance
        always wins over a generic on-edge projection -- only if none is in
        range do we fall to the exact point anywhere along a curve's real
        geometry (see snaputil.nearest_snap_ranked)."""
        return snaputil.nearest_snap_ranked(
            self.view, self._snap_pts, getattr(self, "_snap_shapes", []),
            pos, self._snap_px)

    def _hover_snap_candidates(self):
        """(shapes, points) for the hover-snap preview, built lazily and
        cached so a hover doesn't re-scan every object each mouse move. The
        cache is invalidated at the end of each drag (see _release), since
        the edited curve's geometry changes there.

        Separate from the drag-time _snap_pts/_snap_shapes (which are
        rebuilt at drag start) precisely so a preview can run BEFORE any
        drag begins -- the whole point of the hover preview."""
        if self._hover_shapes_cache is None:
            try:
                self._hover_shapes_cache = [
                    e for e, _o in snaputil.collect_snap_shapes(
                        self.obj.Document, exclude=self.obj)]
                self._hover_pts_cache = snaputil.collect_strong_snap_points(
                    self.obj.Document, exclude=self.obj)
            except Exception:
                self._hover_shapes_cache = []
                self._hover_pts_cache = []
        return self._hover_shapes_cache, self._hover_pts_cache

    # ---- symmetric editing (model only half) ------------------------- #
    def set_symmetry(self, plane_base, plane_normal, tol=1e-4):
        """Enable/disable live symmetric dragging. Pass plane_base=None
        to disable. Raises ValueError (propagated from
        rfstudio.mirror.find_symmetric_pairs) if the current point set
        isn't symmetric about the given plane -- callers should catch
        this and refuse to enable the toggle rather than silently
        mis-pairing points."""
        if plane_base is None:
            self._sym_pairs = None
            self._sym_plane = None
            self.update_symmetry()
            return
        from rfstudio import mirror as rmirror
        self._sym_pairs = rmirror.find_symmetric_pairs(
            self._points(), plane_base, plane_normal, tol=tol)
        self._sym_plane = (plane_base, plane_normal)
        self.update_symmetry()


    # ---- picking ---- #
    def _screen(self, vec):
        return snaputil.screen_project(self.view, vec)

    def _pick_axis(self, pos):
        pts = self._points()
        if not (0 <= self.sel < len(pts)):
            return None
        p = pts[self.sel]
        L = self._arrow_len()
        sp = self._screen(p)
        if sp is None:
            return None
        # All three arrows START at the point, so measuring against the full
        # sp->tip segment made a click ON the marker match every axis: every
        # point grab silently became an axis-locked drag, which (since axis
        # drags disable snapping) killed snap-to-curve. Treat the marker itself
        # as a POINT grab, and only count the OUTER part of each arrow as an
        # axis handle.
        if ((pos[0] - sp[0]) ** 2 + (pos[1] - sp[1]) ** 2) ** 0.5 <= self._pick_px:
            return None
        best, bestd = None, self._pick_px
        for i, a in enumerate(_AXES):
            tip = self._screen(Vector(p.x + a.x * L, p.y + a.y * L, p.z + a.z * L))
            if tip is None:
                continue
            base = (sp[0] + 0.3 * (tip[0] - sp[0]),
                    sp[1] + 0.3 * (tip[1] - sp[1]))
            d = _seg_dist(pos, base, tip)
            if d < bestd:
                best, bestd = i, d
        return best

    def _pick_point(self, pos):
        best, bestd = None, self._pick_px
        for i, p in enumerate(self._points()):
            sp = self._screen(p)
            if sp is None:
                continue
            d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
            if d < bestd:
                best, bestd = i, d
        return best

    # ---- events ---- #
    def _on_button(self, ev):
        try:
            if ev.get("Type") != "SoMouseButtonEvent":
                return
            if ev.get("Button") != "BUTTON1":
                return
            if ev.get("State") == "DOWN":
                # Cancel any existing drag
                if self.drag:
                    self._release()

                # Check for Ctrl+click for coordinate input
                if self.dragging_with_ctrl and not self.drag:
                    self._show_input_field(ev["Position"])
                    return

                # Try to pick a handle for dragging (if in Bezier mode and not dragging)
                if not self.drag and self.is_bezier:
                    handle_info = self._pick_handle(ev["Position"])
                    if handle_info is not None:
                        # Start handle dragging
                        self.drag = {"type": "handle", "info": handle_info, "g0": self.view.getPoint(ev["Position"][0], ev["Position"][1])}
                        self._start_watchdog()
                        return

                # Fall back to existing logic (segment, axis, point)
                self._press(ev["Position"])
            elif ev.get("State") == "UP":
                self._release()
        except Exception:
            pass

    def _press(self, pos):
        if self.drag:
            # A previous drag never got its mouse-up (e.g. the LMB was
            # released after the cursor left the 3D view mid-drag -- Coin
            # only delivers SoMouseButtonEvent while over the view widget,
            # so the UP event silently never arrives). Force it closed
            # instead of leaking drag state forever.
            self._release()

        # Check for Ctrl+click for coordinate input
        if self.dragging_with_ctrl and not self.drag:
            # Show coordinate input field
            self._show_input_field(pos)
            return

        # Insert a point on a segment click only when explicitly requested:
        # Shift held, or the panel's insert-on-click toggle is on. Otherwise a
        # plain click over a segment falls through to point selection/dragging
        # (previously ANY click on a segment auto-inserted a point).
        if (not self.drag and not self.dragging_with_ctrl
                and (self.insert_on_click or self._shift_held())):
            segment_idx = self._pick_segment(pos)
            if segment_idx is not None:
                # Get click position for new point
                view_point = self.view.getPoint(pos[0], pos[1])
                if view_point is not None:
                    # Insert point after the segment
                    self.insert_point(segment_idx, view_point)
                    return

        # Handle point selection for dragging/deletion
        axis = self._pick_axis(pos)
        if axis is not None:
            self._begin(self.sel, axis, pos)
            return
        idx = self._pick_point(pos)
        if idx is not None:
            self.sel = idx
            self._refresh_arrows()
            self._begin(idx, None, pos)

    def _shift_held(self):
        """True if Shift is currently held (live Qt modifier state)."""
        try:
            from PySide import QtCore, QtGui
            return bool(QtGui.QGuiApplication.keyboardModifiers()
                        & QtCore.Qt.ShiftModifier)
        except Exception:
            return False

    def _begin(self, idx, axis, pos):
        g0 = self.view.getPoint(pos[0], pos[1])
        self.drag = {"idx": idx, "axis": axis, "g0": g0,
                     "p0": self._points()[idx]}
        self._snap_pts = self._snap_candidates()  # cache for this drag
        self._snap_shapes = self._snap_shape_candidates()  # cache for this drag
        self._start_watchdog()

    def _on_move(self, ev):
        try:
            pos = ev["Position"]
            if self.drag:
                if self.drag.get("type") == "handle":
                    # Handle dragging
                    self._edit_bezier_handle(self.drag["info"], pos)
                else:
                    # Point / axis dragging.
                    p0 = self.drag["p0"]
                    axis = self.drag["axis"]
                    if axis is not None:
                        # Axis-locked: place the point at the closest point on
                        # the axis line to the cursor ray (exact, no depth-scale
                        # error).
                        a = _AXES[axis]
                        try:
                            ro = self.view.getPoint(pos[0], pos[1])
                            rd = self.view.getViewDirection()
                            newp = _closest_on_line_to_ray(p0, a, ro, rd)
                        except Exception:
                            g = self.view.getPoint(pos[0], pos[1])
                            newp = p0 + a * (g - self.drag["g0"]).dot(a)
                        # Snap to where the drag axis crosses another curve.
                        # The snapped point stays ON the axis line, so the
                        # axis constraint is preserved -- this snaps WITHIN
                        # the lock, it doesn't break it. (Previously snapping
                        # was disabled entirely while axis-locked, which is
                        # what made "drag a point onto that line" impossible.)
                        snapped = snaputil.nearest_axis_snap(
                            self.view, p0, a,
                            getattr(self, "_snap_shapes", []), pos, self._snap_px)
                        if snapped is not None:
                            newp = snapped
                            self._snap_coord.point.set1Value(
                                0, snapped.x, snapped.y, snapped.z)
                            self._snap_switch.whichChild = 0
                        else:
                            self._snap_switch.whichChild = -1
                    else:
                        # Free drag in the view plane, with snap-to-other-curves.
                        g = self.view.getPoint(pos[0], pos[1])
                        newp = p0 + (g - self.drag["g0"])
                        snapped = self._snap(pos)
                        if snapped is not None:
                            newp = snapped
                            self._snap_coord.point.set1Value(
                                0, snapped.x, snapped.y, snapped.z)
                            self._snap_switch.whichChild = 0
                        else:
                            self._snap_switch.whichChild = -1
                    self._live(self.drag["idx"], newp)
            else:
                # Handle hover detection when not dragging
                self._on_hover(pos)
        except Exception:
            pass

    def _on_hover(self, pos):
        """Handle mouse movement when not dragging for hover detection."""
        # Reset hover states
        old_hover_point = self.hover_point
        old_hover_segment = self.hover_segment
        old_hover_handle = self.hover_handle
        self.hover_point = None
        self.hover_segment = None
        self.hover_handle = None  # (point_idx, 'left'|'right') or None

        try:
            # Check for hover over points (for deletion)
            if not self.creating:  # Only in edit mode, not creation
                self.hover_point = self._pick_point(pos)

                # If not hovering over a point, check for hover over segment (for insertion)
                if self.hover_point is None:
                    self.hover_segment = self._pick_segment(pos)

                # If not hovering over point or segment, check for hover over handle (for editing)
                if self.hover_point is None and self.hover_segment is None and self.is_bezier:
                    self.hover_handle = self._pick_handle(pos)

            # Update cursor based on hover state
            self._update_cursor()

            # Update hover state if changed
            if (self.hover_point != old_hover_point or
                self.hover_segment != old_hover_segment or
                self.hover_handle != old_hover_handle):
                # Trigger a redraw to show hover highlight
                self._update_hover_highlight()

            # Hover-snap preview: light up the snap marker when the cursor is
            # near another curve, BEFORE any drag starts, so snapping is
            # discoverable (and you can see which target will be grabbed).
            # Suppressed while hovering directly over our own point/handle so
            # it doesn't fight the deletion/edit hover cues.
            if (not self.creating and self.hover_point is None
                    and self.hover_handle is None):
                shapes, dpts = self._hover_snap_candidates()
                snapped = snaputil.nearest_snap_ranked(
                    self.view, dpts, shapes, pos, self._snap_px)
                if snapped is not None:
                    self._snap_coord.point.set1Value(
                        0, snapped.x, snapped.y, snapped.z)
                    self._snap_switch.whichChild = 0
                else:
                    self._snap_switch.whichChild = -1
            elif self._snap_switch.whichChild != -1:
                self._snap_switch.whichChild = -1

        except Exception:
            pass

    def _pick_segment(self, pos):
        """Check if cursor is over a line segment, return segment index or None."""
        pts = self._points()
        if len(pts) < 2:
            return None

        # Convert screen coordinates to 3D point on view plane
        view_point = self.view.getPoint(pos[0], pos[1])
        if view_point is None:
            return None

        # Check each segment
        min_dist = float('inf')
        closest_segment = None

        for i in range(len(pts) - 1):
            p1 = pts[i]
            p2 = pts[i + 1]

            # Calculate distance from point to line segment
            seg_vec = p2 - p1
            point_vec = view_point - p1

            # Handle degenerate segment
            if seg_vec.Length < 1e-6:
                dist = (view_point - p1).Length
            else:
                # Project point onto segment
                t = point_vec.dot(seg_vec) / seg_vec.dot(seg_vec)
                t = max(0, min(1, t))  # Clamp to segment
                projection = p1 + seg_vec * t
                dist = (view_point - projection).Length

            # Convert 3D distance to screen distance for consistent sensitivity
            screen_dist = self._get_screen_distance(view_point, projection) if dist > 0 else 0
            if screen_dist < min_dist and screen_dist < 10:  # 10 pixel threshold
                min_dist = screen_dist
                closest_segment = i

        return closest_segment

    def _update_cursor(self):
        """Update cursor based on hover state."""
        try:
            from PySide import QtGui
            if self.hover_handle is not None:
                # Drag handle cursor (closed hand)
                QtGui.QGuiApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.ClosedHandCursor))
            elif self.hover_point is not None:
                # Pointing hand cursor for deletion
                QtGui.QGuiApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
            elif (self.hover_segment is not None
                    and (self.insert_on_click or self._shift_held())):
                # Crosshair cursor for insertion -- only when a click would
                # actually insert (Shift held or insert-on-click toggle on)
                QtGui.QGuiApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CrossCursor))
            else:
                # Restore default cursor
                QtGui.QGuiApplication.restoreOverrideCursor()
        except Exception:
            pass

    def _update_hover_highlight(self):
        """Update visual highlight for hover state."""
        # Update point highlight for deletion
        if self.hover_point is not None and self._hl is not None:
            pts = self._points()
            if 0 <= self.hover_point < len(pts):
                self._hl.point.set1Value(0, pts[self.hover_point].x,
                                       pts[self.hover_point].y, pts[self.hover_point].z)
                self._hl_markers.numPoints = 1
            else:
                self._hl_markers.numPoints = 0
        elif self._hl is not None:
            self._hl_markers.numPoints = 0

        # TODO: Add segment highlight for insertion (would need additional visualization)
        # Update Bezier handle highlight if applicable
        self._update_bezier_handle_highlight()

    def _update_bezier_handle_highlight(self):
        """Update visual highlight for Bezier handle hover state."""
        if not self.is_bezier or not self.handle_vis_root:
            return

        # Reset all highlights
        self.left_handle_markers.numPoints = 0
        self.right_handle_markers.numPoints = 0

        if self.hover_handle is not None:
            point_idx, which = self.hover_handle
            pts = self._points()
            if 0 <= point_idx < len(pts):
                if which == 'left':
                    # Highlight left handle
                    left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
                    if point_idx < len(left_handles):
                        handle_pos = left_handles[point_idx]
                        self.left_handle_coords.point.set1Value(0, handle_pos.x, handle_pos.y, handle_pos.z)
                        self.left_handle_markers.numPoints = 1
                else:  # right
                    # Highlight right handle
                    right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))
                    if point_idx < len(right_handles):
                        handle_pos = right_handles[point_idx]
                        self.right_handle_coords.point.set1Value(0, handle_pos.x, handle_pos.y, handle_pos.z)
                        self.right_handle_markers.numPoints = 1

    def _get_screen_distance(self, point1, point2):
        """Calculate screen distance between two 3D points."""
        if point1 is None or point2 is None:
            return float('inf')
        try:
            screen1 = self._screen(point1)
            screen2 = self._screen(point2)
            if screen1 is None or screen2 is None:
                return float('inf')
            dx = screen1[0] - screen2[0]
            dy = screen1[1] - screen2[1]
            return (dx*dx + dy*dy) ** 0.5
        except Exception:
            return float('inf')

    def _pick_handle(self, pos):
        """Check if cursor is near a Bezier handle, return (point_idx, 'left'|'right') or None."""
        if not self.is_bezier:
            return None
        pts = self._points()
        if not pts:
            return None
        left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
        right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))

        # Convert screen pos to 3D point on view plane
        view_point = self.view.getPoint(pos[0], pos[1])
        if view_point is None:
            return None

        min_dist = float('inf')
        closest = None

        # Check left handles (blue)
        for i, handle in enumerate(left_handles):
            dist = self._get_screen_distance(view_point, handle)
            if dist < min_dist and dist < 12:  # 12 pixel threshold for handles
                min_dist = dist
                closest = (i, 'left')

        # Check right handles (green)
        for i, handle in enumerate(right_handles):
            dist = self._get_screen_distance(view_point, handle)
            if dist < min_dist and dist < 12:
                min_dist = dist
                closest = (i, 'right')

        return closest

    def _edit_bezier_handle(self, handle_info, pos):
        """Edit a Bezier handle based on handle type constraints."""
        point_idx, which = handle_info  # which is 'left' or 'right'
        view_point = self.view.getPoint(pos[0], pos[1])
        if view_point is None:
            return

        # Get current data
        points = self._points()
        left_handles = list(getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(points)))
        right_handles = list(getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(points)))
        handle_types = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(points)))

        if point_idx >= len(points):
            return

        point = points[point_idx]

        # Update the appropriate handle
        if which == 'left':
            left_handles[point_idx] = view_point
        else:  # right
            right_handles[point_idx] = view_point

        # Enforce handle type constraints (G1/G2 continuity)
        self._enforce_bezier_handle_constraints(point_idx, which, left_handles,
                                                right_handles, handle_types)

        # Apply G1/G2 continuity snapping to adjacent spans
        self._snap_bezier_continuity(point_idx, which, left_handles, right_handles, handle_types)

        # Update the object
        obj = self.obj
        obj.LeftHandles = left_handles
        obj.RightHandles = right_handles
        obj.HandleTypes = handle_types
        obj.recompute()
        obj.Document.recompute()

        # Update visualization
        self._update_bezier_handles()

        # Update status bar with G2/G3 readout
        self._update_bezier_status_readout(point_idx)

    def _enforce_bezier_handle_constraints(self, point_idx, which, left_handles,
                                           right_handles, handle_types):
        """Apply handle type constraints after editing a handle.

        `which` ('left' or 'right') identifies which handle was JUST
        dragged, so the other one can be adjusted to match. Delegates to
        core.enforce_bezier_handle_constraints for the core logic, then
        handles TangentFace locally (needs face geometry).
        """
        from rfstudio import core
        core.enforce_bezier_handle_constraints(point_idx, which, left_handles, right_handles, handle_types, self._points())

        # Handle TangentFace type locally (requires face geometry)
        if point_idx < len(handle_types) and handle_types[point_idx] == "TangentFace":
            self._enforce_tangent_face(point_idx, which, left_handles, right_handles)

    def set_bezier_handle_type(self, point_idx, htype):
        """Set the handle type for a specific point."""
        if not self.is_bezier or point_idx < 0 or point_idx >= len(self._points()):
            return
        handle_types = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(self._points())))
        if point_idx < len(handle_types):
            handle_types[point_idx] = htype
            self.obj.HandleTypes = handle_types
            self.obj.recompute()
            # If setting to TangentFace, prompt for face selection
            if htype == "TangentFace":
                self._prompt_tangent_face_selection(point_idx)

    def _prompt_tangent_face_selection(self, point_idx):
        """Prompt user to select a face for tangency constraint."""
        try:
            from PySide import QtWidgets
            # Check if there's a current selection with a face
            sel = FreeCADGui.Selection.getSelectionEx()
            face_found = False
            for so in sel:
                subs = getattr(so, "SubObjects", None) or []
                names = getattr(so, "SubElementNames", None) or []
                for i, sub in enumerate(subs):
                    if getattr(sub, "ShapeType", "") == "Face" and i < len(names):
                        self.set_tangent_target(point_idx, so.Object, sub, names[i])
                        face_found = True
                        self.status_message(f"Tangent target set: {so.Object.Name}.{names[i]}")
                        break
                if face_found:
                    break
            if not face_found:
                self.status_message("Select a face first, then press 'T' on a handle")
        except Exception:
            pass

    def status_message(self, msg):
        """Update panel status bar if available."""
        try:
            if hasattr(self, "on_commit") and self.on_commit is not None:
                panel = getattr(self.on_commit, "__self__", None)
                if panel and hasattr(panel, "status"):
                    panel.status.setText(msg)
        except Exception:
            pass

    def set_tangent_target(self, point_idx, face_obj, face_shape, face_name):
        """Set the tangency target face for a point (virtual intersection handle)."""
        self._tangent_targets[point_idx] = (face_obj, face_name, face_shape)
        # Mark the handle type as TangentFace
        handle_types = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(self._points())))
        if point_idx < len(handle_types):
            handle_types[point_idx] = "TangentFace"
            self.obj.HandleTypes = handle_types

    def clear_tangent_target(self, point_idx):
        """Clear the tangency target for a point."""
        if point_idx in self._tangent_targets:
            del self._tangent_targets[point_idx]
        handle_types = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(self._points())))
        if point_idx < len(handle_types):
            handle_types[point_idx] = "Auto"
            self.obj.HandleTypes = handle_types

    def _compute_virtual_intersection(self, point_idx, which):
        """Compute the virtual intersection handle position for tangency to face.

        The virtual intersection is where the tangent plane of the target face
        (at the closest point to our curve point) intersects the handle line.
        This gives a handle position that enforces tangency to the face.

        Args:
            point_idx: Index of the control point
            which: 'left' or 'right' handle

        Returns:
            Vector position for the handle, or None if not computable
        """
        if point_idx not in self._tangent_targets:
            return None

        pts = self._points()
        if point_idx >= len(pts):
            return None

        point = pts[point_idx]
        face_obj, face_name, face_shape = self._tangent_targets[point_idx]

        try:
            # Find closest point on face to our curve point
            import Part
            from FreeCAD import Vector
            vertex = Part.Vertex(point)
            dist, pts_on, _ = vertex.distToShape(face_shape)
            if not pts_on:
                return None
            closest_on_face = pts_on[0][1]  # point on face

            # Get face normal at that point
            face = face_shape  # could be a Face or a solid; assume Face
            if hasattr(face, "Surface"):
                uv = face.Surface.parameter(closest_on_face)
                if uv is not None:
                    u, v = uv
                    normal = face.normalAt(u, v)
                else:
                    # Fallback: approximate normal from nearby points
                    normal = Vector(0, 0, 1)
            else:
                normal = Vector(0, 0, 1)

            # The tangent plane at the closest point
            # Virtual intersection: handle should lie in the tangent plane
            # and be aligned with the projection of the other handle onto that plane

            # Get the opposite handle
            left_handles = getattr(self.obj, "LeftHandles", [Vector(0, 0, 0)] * len(pts))
            right_handles = getattr(self.obj, "RightHandles", [Vector(0, 0, 0)] * len(pts))

            if which == 'left':
                other_handle = right_handles[point_idx]
            else:
                other_handle = left_handles[point_idx]

            # Project other handle onto tangent plane
            to_other = other_handle - point
            normal = normal.normalize() if normal.Length > 1e-9 else Vector(0, 0, 1)
            # Remove normal component
            projected = to_other - normal * to_other.dot(normal)

            # Virtual intersection is at the projected position (same distance from point)
            virtual = point + projected
            return virtual

        except Exception:
            return None

    def _enforce_tangent_face(self, point_idx, which, left_handles, right_handles):
        """Enforce tangency to face by adjusting the opposite handle to the virtual intersection."""
        virtual = self._compute_virtual_intersection(point_idx, which)
        if virtual is None:
            return False

        # Adjust the opposite handle to the virtual intersection
        if which == 'left':
            right_handles[point_idx] = virtual
        else:
            left_handles[point_idx] = virtual
        return True

    def _snap_bezier_continuity(self, point_idx, which, left_handles,
                                      right_handles, handle_types):
        """Snap edited handle to achieve G1/G2 continuity with adjacent spans.

        When dragging a handle, this snaps the OPPOSITE handle (or adjusts
        the dragged one) to achieve:
        - G1: tangent continuity (opposite handles collinear with point)
        - G2: curvature continuity (second derivative continuous)
        """
        points = self._points()
        if point_idx >= len(points):
            return

        htype = handle_types[point_idx] if point_idx < len(handle_types) else "Auto"
        point = points[point_idx]

        # Get the dragged handle and its opposite
        if which == 'left':
            dragged = left_handles[point_idx]
            opposite = right_handles[point_idx]
        else:
            dragged = right_handles[point_idx]
            opposite = left_handles[point_idx]

        # Only apply G1/G2 snapping for Aligned/Vector/Auto types
        if htype in ("Aligned", "Vector", "Auto"):
            # G1: snap opposite handle to be collinear with dragged handle and point
            if htype in ("Aligned", "Auto"):
                dir_dragged = dragged - point
                if dir_dragged.Length > 1e-9:
                    dir_normalized = dragged - point
                    dir_normalized = dir_normalized * (1.0 / dir_dragged.Length)
                    # For Aligned: opposite handle is 180 degrees opposite
                    # For G1: opposite handle direction matches dragged direction
                    if htype == "Aligned":
                        opposite = point - dir_normalized * (opposite - point).Length
                    else:  # Auto
                        # G1: opposite tangent matches dragged direction
                        opposite = point + dir_normalized * (opposite - point).Length

            # G2: curvature continuity - snap to match curvature with adjacent spans
            if htype in ("Vector", "Auto"):
                # For G2: second derivative continuity
                # The curvature at the shared point should match from both sides
                # This is approximated by matching the "curvature vector" direction
                # We'll use a simpler heuristic: snap the dragged handle to make
                # the control polygon symmetric around the point
                if len(points) >= 3:
                    # Get adjacent points
                    idx = points.index(point) if point in points else -1
                    if idx > 0 and idx < len(points) - 1:
                        prev_pt = points[idx - 1]
                        next_pt = points[idx + 1]
                        # For G2: the control points should form a smooth curve
                        # The handle positions should be symmetric in a curvature sense
                        # Heuristic: project the dragged handle onto the bisector
                        chord = next_pt - prev_pt
                        if chord.Length > 1e-9:
                            chord_dir = chord * (1.0 / chord.Length)
                            # Project dragged handle onto chord bisector plane
                            to_dragged = dragged - point
                            proj = to_dragged.dot(chord_dir)
                            dragged = point + chord_dir * proj + (to_dragged - chord_dir * proj) * 0.5

        # Update the appropriate handle list
        if which == 'left':
            left_handles[point_idx] = dragged
            right_handles[point_idx] = opposite
        else:
            right_handles[point_idx] = dragged
            left_handles[point_idx] = opposite

    def _update_bezier_status_readout(self, point_idx):
        """Update panel status bar with G2/G3 readout for the edited point."""
        if not hasattr(self, "on_commit") or self.on_commit is None:
            return
        try:
            # Get the panel's status label
            panel = getattr(self.on_commit, "__self__", None)
            if panel and hasattr(panel, "status"):
                pts = self._points()
                if point_idx >= len(pts):
                    return
                left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(self._points()))
                right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(self._points()))
                p0 = pts[point_idx]
                left = left_handles[point_idx]
                right = right_handles[point_idx]
                # Compute G2 at this point
                if point_idx > 0:
                    p_prev = pts[point_idx - 1]
                    left_prev = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(self._points()))[point_idx - 1]
                    right_prev = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(self._points()))[point_idx - 1]
                    # G2 at point_idx from left span
                    p0, p1, p2, p3 = pts[point_idx - 1], right_prev, left, pts[point_idx]
                    g2_left = _bezier_curvature_and_jerk(p0, p1, p2, p3, 1.0)[0]
                else:
                    g2_left = None
                if point_idx < len(pts) - 1:
                    p_next = pts[point_idx + 1]
                    left_next = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(self._points()))[point_idx + 1]
                    right_next = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(self._points()))[point_idx + 1]
                    # G2 at point_idx from right span
                    p0, p1, p2, p3 = pts[point_idx], right, left_next, pts[point_idx + 1]
                    g2_right = _bezier_curvature_and_jerk(pts[point_idx], right, left_next, pts[point_idx + 1], 0.0)[0]
                else:
                    g2_right = None
                # Format status text
                if g2_left is not None and g2_right is not None:
                    g2_match = abs(g2_left - g2_right) if g2_left and g2_right else None
                    panel.status.setText(f"G2: left={g2_left:.4f} right={g2_right:.4f} Δ={g2_match:.4f}" if g2_match is not None else f"G2: left={g2_left:.4f} right={g2_right:.4f}")
                elif g2_left is not None:
                    panel.status.setText(f"G2: left={g2_left:.4f}")
                elif g2_right is not None:
                    panel.status.setText(f"G2: right={g2_right:.4f}")
                else:
                    panel.status.setText("G2: --")
        except Exception:
            pass

    def set_handle_visibility(self, visible):
        """Show or hide Bezier handle visualization."""
        self.handles_visible = visible
        if self.is_bezier:
            if visible and self.handle_vis_root:
                self._update_bezier_handles()
            elif not visible:
                self._destroy_bezier_handles()

    def delete_point(self, idx):
        """Delete a point at the specified index."""
        if not (0 <= idx < len(self._points())):
            return False

        pts = self._points()
        weights = self._weights[:] if hasattr(self, '_weights') else []

        # Remove the point
        del pts[idx]
        if hasattr(self, '_weights') and len(weights) > idx:
            del weights[idx]
            self._weights = weights

        # Update the object
        self._set_points(pts)

        # Reset hover state
        self.hover_point = None
        self._update_cursor()
        self._update_hover_highlight()

        return True

    def insert_point(self, idx, point):
        """Insert a point at the specified index (after the point at idx)."""
        pts = self._points()
        weights = self._weights[:] if hasattr(self, '_weights') else []

        # Insert the point after index idx
        insert_pos = idx + 1
        pts.insert(insert_pos, point)
        if hasattr(self, '_weights'):
            weights.insert(insert_pos, 1.0)  # Default weight
            self._weights = weights

        # Update the object
        self._set_points(pts)

        # Reset hover state
        self.hover_segment = None
        self._update_cursor()
        self._update_hover_highlight()

        return True

    def _set_points(self, pts):
        """Write `pts` back to the edited object as its new control-point
        list, recompute, and rebuild the on-screen markers/arrows.

        Used by delete_point / insert_point / _on_input_return, which change
        the point COUNT (a plain drag only moves one existing marker and
        goes through commit() instead). This mirrors commit()'s write-back
        tail so both the count-changing and the drag paths keep the
        document, the Coin markers, the selection arrows, and the panel
        (via on_commit) in sync. _refresh_markers() resets
        SoMarkerSet.numPoints, so a shorter/longer list is handled."""
        self.obj.Points = pts
        if (hasattr(self.obj, "Weights") and hasattr(self, "_weights")
                and len(self._weights) == len(pts)):
            self.obj.Weights = list(self._weights)
        self.obj.recompute()
        self.obj.Document.recompute()
        self._refresh_markers()
        self._refresh_arrows()
        if self.on_commit is not None:
            try:
                self.on_commit()
            except Exception:
                pass

    def _focus_row(self, idx):
        """Make point `idx` the active selection and move the arrow gizmo
        onto it -- the 3D-editor analogue of focusing that point's row in
        the panel's coordinate table (the source of the name). Called after
        a point is added via the coordinate input field so the freshly
        created point shows as selected. No-op for an out-of-range index."""
        pts = self._points()
        if not (0 <= idx < len(pts)):
            return
        self.sel = idx
        self._refresh_arrows()

    def _on_key(self, ev):
        """Handle key presses for point deletion, Ctrl key tracking, and handle type shortcuts."""
        try:
            if ev.get("Type") != "SoKeyboardEvent":
                return
            if ev.get("State") == "DOWN":
                key = ev.get("Key")
                # Track Ctrl key state
                if key == 17:  # Ctrl key
                    self.dragging_with_ctrl = True
                # Handle type shortcuts (when not creating)
                elif not self.creating and self.is_bezier:
                    if key == 65:  # A - Auto
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Auto")
                    elif key == 76:  # L - Aligned
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Aligned")
                    elif key == 86:  # V - Vector (G1)
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Vector")
                    elif key == 71:  # G - G2 curvature continuity
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "G2")
                    elif key == 74:  # J - G3 jerk continuity
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "G3")
                    elif key == 70:  # F - Free
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Free")
                    elif key == 84:  # T - Tangent-to-face (virtual intersection)
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "TangentFace")
                # Delete or Backspace key
                elif key == 127 or key == 8:
                    if self.hover_point is not None and not self.creating:
                        self.delete_point(self.hover_point)
            elif ev.get("State") == "UP":
                key = ev.get("Key")
                # Track Ctrl key state
                if key == 17:  # Ctrl key
                    self.dragging_with_ctrl = False
                    # Hide input field if visible when Ctrl is released
                    self._hide_input_field()
        except Exception:
            pass

    def _show_input_field(self, pos):
        """Show a text field for entering coordinates."""
        try:
            from PySide import QtGui, QtCore

            # Get the 3D position where we want to place the point
            view_point = self.view.getPoint(pos[0], pos[1])
            if view_point is None:
                return

            # Create the input field if it doesn't exist
            if self.input_field is None:
                self.input_field = QtGui.QLineEdit()
                self.input_field.setPlaceholderText("X, Y, Z")
                self.input_field.setFixedWidth(100)
                self.input_field.returnPressed.connect(self._on_input_return)
                self.input_field.editingFinished.connect(self._hide_input_field)
                # Apply some styling
                self.input_field.setStyleSheet("""
                    QLineEdit {
                        background-color: rgba(255, 255, 255, 200);
                        border: 1px solid #5a6675;
                        border-radius: 3px;
                        padding: 2px;
                    }
                """)

            # Set the current coordinates as initial text
            self.input_field.setText(f"{view_point.x:.3f}, {view_point.y:.3f}, {view_point.z:.3f}")
            self.input_field.selectAll()

            # Position the input field near the cursor
            # We need to convert the 3D point to screen coordinates
            screen_pos = self._screen(view_point)
            if screen_pos:
                # Convert to global screen position
                view_widget = self.view.getWidget()
                if view_widget:
                    local_pos = QtCore.QPoint(int(screen_pos[0]), int(screen_pos[1]))
                    global_pos = view_widget.mapToGlobal(local_pos)
                    # Position below the cursor
                    self.input_field.move(global_pos.x() + 10, global_pos.y() + 20)
                    self.input_field.show()
                    self.input_field.setFocus()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"Railform: Error showing input field: {exc}\n")

    def _hide_input_field(self):
        """Hide the input field."""
        if self.input_field is not None:
            self.input_field.hide()

    def _on_input_return(self):
        """Handle Enter key press in the input field."""
        try:
            if self.input_field is None:
                return

            text = self.input_field.text().strip()
            self._hide_input_field()

            # Parse the coordinates
            parts = [p.strip() for p in text.split(',')]
            if len(parts) != 3:
                return

            x = float(parts[0])
            y = float(parts[1])
            z = float(parts[2])

            # Create a vector from the coordinates
            from FreeCAD import Vector
            point = Vector(x, y, z)

            # If we were in the middle of placing a point via click+ctrl,
            # we need to determine where to put it
            # For now, we'll just add it as a new point at the end
            # A more sophisticated approach would determine the insertion point
            # based on where the user clicked

            pts = self._points()
            pts.append(point)
            if hasattr(self, '_weights'):
                self._weights.append(1.0)  # Default weight

            # Update the object
            self._set_points(pts)

            # Select the newly added point
            self.sel = len(pts) - 1
            self._refresh_arrows()
            self._focus_row(self.sel)

        except ValueError:
            # Invalid number format
            pass
        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"Railform: Error processing input: {exc}\n")
        finally:
            self.dragging_with_ctrl = False

    def _live(self, idx, newp):
        """Move marker + arrows + preview, WITHOUT touching the document."""
        pts = self._points()
        if not (0 <= idx < len(pts)):
            return
        if self._sym_plane is not None and self._sym_pairs is not None:
            from rfstudio import mirror as rmirror
            base, normal = self._sym_plane
            partner = self._sym_pairs.get(idx, idx)
            if partner == idx:
                # on-plane (seam) point -- clamp the drag back onto the
                # plane so a seam point can't be dragged off it and
                # reopen a gap between the two halves.
                d = (newp - base).dot(normal)
                newp = newp - normal * d
            else:
                mirrored = rmirror.mirror_point(newp, base, normal)
                pts[partner] = mirrored
                self.mk_coord.point.set1Value(
                    partner, mirrored.x, mirrored.y, mirrored.z)
        pts[idx] = newp
        self.mk_coord.point.set1Value(idx, newp.x, newp.y, newp.z)
        if idx == self.sel:
            self._set_arrows_at(newp)
        try:
            shape = self.obj.Proxy.compute(self.obj, pts)
            line = shape.discretize(60) if shape is not None else None
        except Exception:
            line = None
        if line:
            self.prev_coord.point.setValues(
                0, len(line), [(q.x, q.y, q.z) for q in line])
            self.prev_line.numVertices.setValue(len(line))
            self.prev_switch.whichChild = 0

        # Update Bezier handles if applicable
        if self.is_bezier:
            self._update_bezier_handles()

    def _set_arrows_at(self, p):
        L = self._arrow_len()
        for i, coord in enumerate(self.arrow_coords):
            a = _AXES[i]
            coord.point.setValues(0, 2, [(p.x, p.y, p.z),
                                         (p.x + a.x * L, p.y + a.y * L,
                                          p.z + a.z * L)])
        self._update_cones(p, L)

    def _release(self):
        if not self.drag:
            return
        self._stop_watchdog()
        drag, self.drag = self.drag, None
        self.prev_switch.whichChild = -1
        self._snap_switch.whichChild = -1
        self._snap_pts = []
        self._snap_shapes = []
        # The edited curve just changed shape -> drop the hover-snap cache so
        # the preview rebuilds against current geometry on the next hover.
        self._hover_pts_cache = None
        self._hover_shapes_cache = None
        # Persist the drag. Bezier-handle drags already wrote themselves back in
        # _edit_bezier_handle (live), so only a point/axis drag needs its final
        # marker position committed to obj.Points. (Previously _release never
        # called commit() at all, so 3D drags were silently discarded.)
        if drag.get("type") != "handle" and "idx" in drag:
            self.commit(drag["idx"])

    def commit(self, idx=None):
        try:
            pts = self._points()
            if self._sym_plane is not None and self._sym_pairs is not None:
                # A symmetric drag touched two rows (idx AND its
                # partner, updated live in _live() above) -- write
                # back every marker position, not just idx.
                vals = self.mk_coord.point.getValues()
                for k in range(min(len(pts), len(vals))):
                    v = vals[k].getValue()
                    pts[k] = Vector(v[0], v[1], v[2])
            elif idx is not None and 0 <= idx < len(pts):
                mp = self.mk_coord.point.getValues()[idx].getValue()
                pts[idx] = Vector(mp[0], mp[1], mp[2])
            self.obj.Points = pts
            self.obj.recompute()
            self.obj.Document.recompute()
            self._refresh_markers()
            self._refresh_arrows()
            if self.on_commit is not None:
                try:
                    self.on_commit()
                except Exception:
                    pass
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: %s\n" % exc)

    # ---- release-event safety net ---- #
    def _start_watchdog(self):
        """Poll the real Qt button state while a drag is active.

        Coin's SoMouseButtonEvent is only delivered to addEventCallback
        while the cursor is over the 3D view widget. If the LMB is
        released after a fast drag has carried the cursor outside that
        widget (very easy to do -- e.g. dragging toward the task panel to
        click OK), the UP event never reaches _on_button and self.drag is
        left set forever: this is the reported "stuck in drag mode, can't
        click OK" bug. This timer is the fix -- if our own drag state ever
        disagrees with the OS/Qt button state, force the release ourselves.
        """
        if QtCore is None or QtWidgets is None or self._watchdog is not None:
            return
        self._watchdog = QtCore.QTimer()
        self._watchdog.setInterval(120)
        self._watchdog.timeout.connect(self._check_button_still_down)
        self._watchdog.start()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            try:
                self._watchdog.stop()
                self._watchdog.deleteLater()
            except Exception:
                pass
            self._watchdog = None

    def _check_button_still_down(self):
        if not self.drag:
            self._stop_watchdog()
            return
        try:
            if not (QtWidgets.QApplication.mouseButtons()
                    & QtCore.Qt.LeftButton):
                self._release()
        except Exception:
            pass

    # ---- teardown ---- #
    def finish(self):
        """Tear down the point editor: stop the drag watchdog, remove the
        symmetry-preview and Bezier-handle overlays, remove every
        viewport event callback (mouse/move/keyboard), hide any open
        coordinate input field, and detach the Coin scene root.

        Consolidated from two definitions that used to silently shadow
        each other (only the second/textually-last `finish()` in the
        class body ever ran) -- the one that ran was missing
        _destroy_symmetry_preview()/_destroy_bezier_handles(), so every
        edit-session close leaked the "SymmetryPreview" document object
        and the Bezier handle Coin overlay; the one that never ran was
        missing the _cb_key keyboard-callback removal and
        _hide_input_field(). This single version does all of it.
        """
        self._stop_watchdog()
        self._destroy_symmetry_preview()
        self._destroy_bezier_handles()
        drop_callbacks(self.view, (
            ("SoMouseButtonEvent", self, "_cb_btn"),
            ("SoLocation2Event", self, "_cb_mov"),
            ("SoKeyboardEvent", self, "_cb_key"),
        ))
        try:
            self._hide_input_field()
        except Exception:
            pass
        try:
            self.view.getSceneGraph().removeChild(self.root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: point editor scene teardown failed (%s)\n" % exc)

    # ---- Bezier handle visualization ----
    def _build_bezier_handles(self):
        """Build Coin visualization for Bezier handles."""
        if not self.is_bezier or not self.coin:
            return

        try:
            from pivy import coin
            self.handle_vis_root = coin.SoSeparator()

            # Left handles (blue squares)
            left_sep = coin.SoSeparator()
            left_col = coin.SoBaseColor()
            left_col.rgb = (0.2, 0.6, 1.0)  # Blue
            left_sep.addChild(left_col)
            left_style = coin.SoDrawStyle()
            left_style.pointWidth = 8
            left_sep.addChild(left_style)
            left_coords = coin.SoCoordinate3()
            left_markers = coin.SoMarkerSet()
            left_markers.markerIndex = coin.SoMarkerSet.SQUARE
            left_sep.addChild(left_coords)
            left_sep.addChild(left_markers)
            self.handle_vis_root.addChild(left_sep)
            self.left_handle_coords = left_coords
            self.left_handle_markers = left_markers

            # Right handles (green squares)
            right_sep = coin.SoSeparator()
            right_col = coin.SoBaseColor()
            right_col.rgb = (0.2, 1.0, 0.6)  # Green
            right_sep.addChild(right_col)
            right_style = coin.SoDrawStyle()
            right_style.pointWidth = 8
            right_sep.addChild(right_style)
            right_coords = coin.SoCoordinate3()
            right_markers = coin.SoMarkerSet()
            right_markers.markerIndex = coin.SoMarkerSet.SQUARE
            right_sep.addChild(right_coords)
            right_sep.addChild(right_markers)
            self.handle_vis_root.addChild(right_sep)
            self.right_handle_coords = right_coords
            self.right_handle_markers = right_markers

            # Connect lines from points to handles (light gray)
            line_sep = coin.SoSeparator()
            line_col = coin.SoBaseColor()
            line_col.rgb = (0.7, 0.7, 0.7)
            line_sep.addChild(line_col)
            line_style = coin.SoDrawStyle()
            line_style.lineWidth = 1
            line_style.linePattern = coin.SoDrawStyle.DOT  # Dashed
            line_sep.addChild(line_style)
            line_coords = coin.SoCoordinate3()
            line_set = coin.SoLineSet()
            line_sep.addChild(line_coords)
            line_sep.addChild(line_set)
            self.handle_vis_root.addChild(line_sep)
            self.line_coords = line_coords
            self.line_set = line_set

            # Add to scene
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(self.handle_vis_root)

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: Bezier handle visualization unavailable ({exc})\n")

        # Build curvature comb visualization
        self._build_curvature_comb()

    def _update_bezier_handles(self):
        """Update Bezier handle visualization."""
        if not self.is_bezier or not self.handle_vis_root or not self.handles_visible:
            return

        try:
            pts = self._points()
            if not pts:
                return

            # Get handle data
            left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
            right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))

            # Update left handle positions
            left_pts = [(h.x, h.y, h.z) for h in left_handles]
            self.left_handle_coords.point.setValues(0, len(left_pts), left_pts)
            self.left_handle_markers.numPoints = len(left_pts)

            # Update right handle positions
            right_pts = [(h.x, h.y, h.z) for h in right_handles]
            self.right_handle_coords.point.setValues(0, len(right_pts), right_pts)
            self.right_handle_markers.numPoints = len(right_pts)

            # Update connecting lines (point to left handle, point to right handle)
            line_pts = []
            for i, (p, lh, rh) in enumerate(zip(pts, left_handles, right_handles)):
                line_pts.append((p.x, p.y, p.z))  # Point to left
                line_pts.append((lh.x, lh.y, lh.z))
                line_pts.append((p.x, p.y, p.z))  # Point to right
                line_pts.append((rh.x, rh.y, rh.z))
            self.line_coords.point.setValues(0, len(line_pts), line_pts)
            # Each handle connection is 2 points, so numVertices = 2 per connection
            num_vertices = [2] * (len(pts) * 2)  # Left and right for each point
            self.line_set.numValues = len(num_vertices)
            self.line_set.numVertices.setValues(0, len(num_vertices), num_vertices)

            # Update curvature comb
            self._update_curvature_comb()

            # Update jerk handles
            self._update_jerk_handles()

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: Error updating Bezier handles: {exc}\n")

    def _build_jerk_handles(self):
        """Build Coin visualization for G3 jerk handles (third derivative control)."""
        if not self.is_bezier or not self.coin:
            return

        try:
            from pivy import coin
            self.jerk_vis_root = coin.SoSeparator()

            # Jerk handles (purple diamonds) - third derivative control
            jerk_sep = coin.SoSeparator()
            jerk_col = coin.SoBaseColor()
            jerk_col.rgb = (0.8, 0.3, 1.0)  # Purple
            jerk_sep.addChild(jerk_col)
            jerk_style = coin.SoDrawStyle()
            jerk_style.pointWidth = 10
            jerk_sep.addChild(jerk_style)
            jerk_coords = coin.SoCoordinate3()
            jerk_markers = coin.SoMarkerSet()
            jerk_markers.markerIndex = coin.SoMarkerSet.DIAMOND
            jerk_sep.addChild(jerk_coords)
            jerk_sep.addChild(jerk_markers)
            self.handle_vis_root.addChild(jerk_sep)
            self.jerk_handle_coords = jerk_coords
            self.jerk_handle_markers = jerk_markers

            # Add to scene
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(self.handle_vis_root)

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: Jerk handle visualization unavailable ({exc})\n")

    def _update_jerk_handles(self):
        """Update G3 jerk handle visualization."""
        if not self.is_bezier or not self.jerk_vis_root or not self.handles_visible:
            return

        try:
            pts = self._points()
            if len(pts) < 2:
                return

            # Compute jerk handles for each interior point
            # Jerk handle position is based on the third derivative at each point
            jerk_pts = []
            for i in range(len(pts)):
                if i == 0 or i == len(pts) - 1:
                    # Endpoints - use nearby jerk
                    if i == 0:
                        # First point - use jerk at t=0 of first segment
                        if len(pts) > 1:
                            p0, p1, p2, p3 = self._points()[0], right_handles[0], left_handles[1], pts[1]
                            jerk = _bezier_curvature_and_jerk(p0, p1, p2, p3, 0.0)[1]
                            if jerk is not None:
                                jerk_vec = _bezier_segment_derivatives(p0, p1, p2, p3, 0.0)[3]
                                jerk_pts.append((pts[0] + jerk_vec * 0.1).x)
                                # Simplified: use a visual offset
                                jerk_pts.append(pts[0])
                    else:
                        # Last point - use jerk at t=1 of last segment
                        pass
                else:
                    # Interior points - compute jerk from adjacent segments
                    pass
            
            # For now, show simplified jerk handles at each interior point
            # Jerk visualization is complex; we'll use a simplified approach
            pts = self._points()
            left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
            right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))
            
            jerk_pts = []
            for i in range(1, len(pts) - 1):
                # Compute third derivative at interior points
                p0 = pts[i-1]
                p3 = pts[i+1]
                p1 = right_handles[i-1]
                p2 = left_handles[i]
                
                # Approximate jerk as difference in curvature vectors
                # This is a simplified visualization
                jerk_vec = (right_handles[i] - left_handles[i]) * 0.5
                jerk_pt = pts[i] + jerk_vec * 0.1
                jerk_pts.append((jerk_pt.x, jerk_pt.y, jerk_pt.z))
            
            if jerk_pts:
                self.jerk_handle_coords.point.setValues(0, len(jerk_pts), jerk_pts)
                self.jerk_handle_markers.numPoints = len(jerk_pts)
            else:
                self.jerk_handle_markers.numPoints = 0

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: Error updating jerk handles: {exc}\n")

    def _destroy_bezier_handles(self):
        """Destroy Bezier handle visualization and jerk handle visualization."""
        if self.handle_vis_root:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    view.getSceneGraph().removeChild(self.handle_vis_root)
            except Exception:
                pass
            self.handle_vis_root = None

        # Destroy jerk handle visualization
        if hasattr(self, 'jerk_vis_root') and self.jerk_vis_root:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    view.getSceneGraph().removeChild(self.jerk_vis_root)
            except Exception:
                pass
            self.jerk_vis_root = None

        # Destroy curvature comb
        self._destroy_curvature_comb()

    # ---- Bezier G2/G3 curvature comb visualization ----
    def _build_curvature_comb(self):
        """Build Coin visualization for G2/G3 curvature comb on Bezier segments."""
        if not self.is_bezier or not self.coin:
            return

        try:
            from pivy import coin
            self.comb_vis_root = coin.SoSeparator()

            # G2 curvature comb (magenta lines)
            comb_sep = coin.SoSeparator()
            comb_col = coin.SoBaseColor()
            comb_col.rgb = (1.0, 0.0, 1.0)  # Magenta
            comb_sep.addChild(comb_col)
            comb_style = coin.SoDrawStyle()
            comb_style.lineWidth = 2
            comb_sep.addChild(comb_style)
            comb_coords = coin.SoCoordinate3()
            comb_set = coin.SoLineSet()
            self.comb_coords = comb_coords
            self.comb_set = comb_set
            comb_sep.addChild(comb_coords)
            comb_sep.addChild(comb_set)
            self.comb_vis_root.addChild(comb_sep)

            # G3 jerk comb (orange lines, shorter)
            jerk_sep = coin.SoSeparator()
            jerk_col = coin.SoBaseColor()
            jerk_col.rgb = (1.0, 0.5, 0.0)  # Orange
            jerk_sep.addChild(jerk_col)
            jerk_style = coin.SoDrawStyle()
            jerk_style.lineWidth = 2
            jerk_style.linePattern = coin.SoDrawStyle.DOT
            jerk_sep.addChild(jerk_style)
            jerk_coords = coin.SoCoordinate3()
            jerk_set = coin.SoLineSet()
            self.jerk_coords = jerk_coords
            self.jerk_set = jerk_set
            jerk_sep.addChild(jerk_coords)
            jerk_sep.addChild(jerk_set)
            self.comb_vis_root.addChild(jerk_sep)

            # Numeric readout overlay (SoText2 is 2D, use 3D text approach)
            # We'll use a text label in the status area instead for now

            # Add to scene
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(self.comb_vis_root)

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: Curvature comb visualization unavailable ({exc})\n")

    def _update_curvature_comb(self):
        """Update G2/G3 curvature comb visualization for all Bezier segments.

        G2 (curvature): teeth perpendicular to tangent, length ~ curvature
        G3 (jerk):     teeth perpendicular to tangent, length ~ jerk (rate of curvature change)
        """
        if not self.is_bezier or not self.comb_vis_root or not self.handles_visible:
            return

        try:
            pts = self._points()
            if len(pts) < 2:
                return

            left_handles = getattr(self.obj, "LeftHandles", [Vector(0, 0, 0)] * len(pts))
            right_handles = getattr(self.obj, "RightHandles", [Vector(0, 0, 0)] * len(pts))

            # Sample each segment
            samples_per_segment = 20
            comb_pts = []   # G2 curvature comb
            jerk_pts = []   # G3 jerk comb

            for i in range(len(pts) - 1):
                p0 = pts[i]
                p3 = pts[i + 1]
                p1 = right_handles[i]
                p2 = left_handles[i + 1]

                for j in range(samples_per_segment):
                    t = j / (samples_per_segment - 1.0)
                    p, d1, d2, d3 = _bezier_segment_derivatives(p0, p1, p2, p3, t)
                    speed2 = d1.dot(d1)
                    if speed2 < 1e-12:
                        continue

                    # G2 Curvature vector: component of d2 perpendicular to tangent
                    # k = |d1 x d2| / |d1|^3  (scalar curvature)
                    # curvature vector = (d2 - d1*(d2·d1/|d1|^2)) / |d1|^2
                    proj = d2 - d1 * (d2.dot(d1) / speed2)
                    curvature_vec = proj * (1.0 / speed2)

                    # G3 Jerk: component of d3 perpendicular to tangent
                    # j = |d3_perp| / |v|^3
                    speed = speed2 ** 0.5
                    if speed > 1e-12:
                        d1_norm = d1 * (1.0 / speed)
                        d3_perp = d3 - d1_norm * d3.dot(d1_norm)
                        jerk = d3_perp.Length / (speed2 * speed)
                        # Jerk direction is along d3_perp
                        jerk_dir = d3_perp * (1.0 / d3_perp.Length) if d3_perp.Length > 1e-12 else Vector(0, 0, 0)
                    else:
                        jerk = 0.0
                        jerk_dir = Vector(0, 0, 0)

                    # Scale for visualization
                    scale = self._arrow_len() * 0.5

                    # Curvature comb: draw from curve point along curvature vector
                    comb_end = p + curvature_vec * scale
                    # Jerk comb: draw from curve point along jerk direction (perpendicular)
                    jerk_end = p + jerk_dir * jerk * scale * 0.3

                    comb_pts.append((p.x, p.y, p.z))
                    comb_pts.append((comb_end.x, comb_end.y, comb_end.z))
                    jerk_pts.append((p.x, p.y, p.z))
                    jerk_pts.append((jerk_end.x, jerk_end.y, jerk_end.z))

            # Update Coin
            if comb_pts:
                self.comb_coords.point.setValues(0, len(comb_pts), comb_pts)
                self.comb_set.numVertices.setValues(0, len(comb_pts) // 2, [2] * (len(comb_pts) // 2))
            else:
                self.comb_set.numVertices.setValues(0, 0, [])

            if jerk_pts:
                self.jerk_coords.point.setValues(0, len(jerk_pts), jerk_pts)
                self.jerk_set.numVertices.setValues(0, len(jerk_pts) // 2, [2] * (len(jerk_pts) // 2))
            else:
                self.jerk_set.numVertices.setValues(0, 0, [])

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: Error updating curvature comb: {exc}\n")

    def _destroy_curvature_comb(self):
        """Destroy curvature comb visualization."""
        if self.comb_vis_root:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    view.getSceneGraph().removeChild(self.comb_vis_root)
            except Exception:
                pass
            self.comb_vis_root = None


class KnotEditor(object):
    """Drag the knots of a ControlCurve B-spline.

    Knots are 1-D values in [0,1] parameter space, but they are rendered as
    3-D markers placed on the actual curve (via edge.valueAt) so the user
    can see and grab them. On drag we re-project the cursor onto the curve
    (nearest sample in normalised parameter), clamp between the neighbouring
    knots so they cannot cross, and write the new value back to obj.Knots
    on mouse-up (deferred via QTimer.singleShot, like PointEditor).

    Must NOT live alongside PointEditor -- the panel swaps them. CurveVP.setEdit
    still owns PointEditor; the panel's "Edit knots" button calls editor.finish()
    on the PointEditor and instantiates KnotEditor in its place.
    """

    _PARAM_SAMPLES = 120            # drag-projection resolution
    _MIN_GAP = 1e-4                 # min spacing between knots

    def __init__(self, vobj):
        self.vobj = vobj
        self.obj = vobj.Object
        doc = FreeCADGui.ActiveDocument
        self.view = doc.ActiveView if doc is not None else None
        self.sel = -1                # currently highlighted knot index
        self.drag = None             # {"idx", "t0"}
        self._watchdog = None
        self.on_commit = None
        self._cb_btn = None
        self._cb_mov = None
        self.ok = False
        self._samples_cache = None   # list of (t, x, y) for the live curve
        self._knot_input_index = None  # index of knot being edited via input field
        self.input_field = None        # (were read but never assigned: AttributeError
        self.dragging_with_ctrl = False  # swallowed by _on_button -> editor inert)
        self._pick_px = (snaputil._snap_tolerance_mm(self.view, 2.5)
                         if self.view is not None else 14.0)
        try:
            from pivy import coin
            self.coin = coin
            self._build()
            self._cb_btn = self.view.addEventCallback(
                "SoMouseButtonEvent", self._on_button)
            self._cb_mov = self.view.addEventCallback(
                "SoLocation2Event", self._on_move)
            self.ok = True
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: knot editor unavailable (%s)\n" % exc)

    # ---- helpers ---- #
    def _knots(self):
        return [float(k) for k in (getattr(self.obj, "Knots", []) or [])]

    def _show_input_field(self, pos):
        """Show a text field for entering a knot parameter value."""
        try:
            from PySide import QtGui, QtCore

            # Check if we have a knot under the cursor (similar to _press logic)
            if not self.obj or not hasattr(self.obj, "Knots"):
                return
            knots = self._knots()
            if not knots:
                return

            # Find the knot nearest to the cursor
            picked_index = self._pick_knot(pos)
            if picked_index is None or picked_index < 0 or picked_index >= len(knots):
                return

            # Store the index for use in _on_input_return
            self._knot_input_index = picked_index

            # Get the current knot value
            current_value = knots[picked_index]

            # Get the 3D position of this knot for positioning the input field
            knot_position = self._value_at_t(current_value)
            if knot_position is None:
                return

            # Create the input field if it doesn't exist
            if self.input_field is None:
                self.input_field = QtGui.QLineEdit()
                self.input_field.setPlaceholderText("Knot value (0.0-1.0)")
                self.input_field.setFixedWidth(80)
                self.input_field.returnPressed.connect(self._on_input_return)
                self.input_field.editingFinished.connect(self._hide_input_field)
                # Apply some styling
                self.input_field.setStyleSheet("""
                    QLineEdit {
                        background-color: rgba(255, 255, 255, 200);
                        border: 1px solid #5a6675;
                        border-radius: 3px;
                        padding: 2px;
                    }
                """)

            # Set the current value as initial text
            self.input_field.setText(f"{current_value:.6f}")
            self.input_field.selectAll()

            # Position the input field near the cursor
            screen_pos = self._screen(knot_position)
            if screen_pos:
                # Convert to global screen position
                view_widget = self.view.getWidget()
                if view_widget:
                    local_pos = QtCore.QPoint(int(screen_pos[0]), int(screen_pos[1]))
                    global_pos = view_widget.mapToGlobal(local_pos)
                    # Position below the cursor
                    self.input_field.move(global_pos.x() + 10, global_pos.y() + 20)
                    self.input_field.show()
                    self.input_field.setFocus()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"Railform: Error showing knot input field: {exc}\n")

    def _hide_input_field(self):
        """Hide the input field."""
        if self.input_field is not None:
            self.input_field.hide()
        self._knot_input_index = None

    def _on_input_return(self):
        """Handle Enter key press in the input field."""
        try:
            if self.input_field is None:
                return

            text = self.input_field.text().strip()
            self._hide_input_field()

            # Parse the knot value
            try:
                new_value = float(text)
                # Clamp to valid range [0, 1]
                new_value = max(0.0, min(1.0, new_value))
            except ValueError:
                # Invalid number format - restore original value and exit
                if hasattr(self, '_knot_input_index') and self._knot_input_index is not None:
                    knots = self._knots()
                    if 0 <= self._knot_input_index < len(knots):
                        self.input_field.setText(f"{knots[self._knot_input_index]:.6f}")
                return

            # Update the knot value
            knots = self._knots()
            if hasattr(self, '_knot_input_index') and self._knot_input_index is not None:
                knot_index = self._knot_input_index
                if 0 <= knot_index < len(knots):
                    knots[knot_index] = new_value
                    self.obj.Knots = knots
                    self.obj.recompute()
                    self.obj.Document.recompute()
                    self._refresh_markers()
                    if self.on_commit is not None:
                        try:
                            self.on_commit()
                        except Exception:
                            pass
            # Clear the stored index
            self._knot_input_index = None
        except ValueError:
            # Invalid number format
            pass
        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"Railform: Error processing knot input: {exc}\n")

    def _first_edge(self):
        shp = getattr(self.obj, "Shape", None)
        if shp is None or not shp.Edges:
            return None
        return shp.Edges[0]

    def _value_at_t(self, t):
        e = self._first_edge()
        if e is None:
            return None
        try:
            fp, lp = e.FirstParameter, e.LastParameter
            return e.valueAt(fp + max(0.0, min(1.0, t)) * (lp - fp))
        except Exception:
            return None

    def _screen(self, vec):
        return snaputil.screen_project(self.view, vec)

    def _refresh_sample_cache(self):
        """Project the curve to screen once per drag; reused for snap + pick."""
        e = self._first_edge()
        if e is None:
            self._samples_cache = []
            return
        out = []
        try:
            fp, lp = e.FirstParameter, e.LastParameter
            for k in range(self._PARAM_SAMPLES + 1):
                t = k / float(self._PARAM_SAMPLES)
                p = e.valueAt(fp + t * (lp - fp))
                sp = self._screen(p)
                if sp is not None:
                    out.append((t, sp[0], sp[1]))
        except Exception:
            pass
        self._samples_cache = out

    def _nearest_t_to_screen(self, pos):
        """Inverse-project cursor screen pos to curve parameter t in [0,1]."""
        if not self._samples_cache:
            return None
        bx, by = pos[0], pos[1]
        best, bestd = 0.0, 1e18
        for t, x, y in self._samples_cache:
            d = (x - bx) * (x - bx) + (y - by) * (y - by)
            if d < bestd:
                best, bestd = t, d
        return best

    # ---- coin scene ---- #
    def _build(self):
        coin = self.coin
        self.root = coin.SoSeparator()

        mk = coin.SoSeparator()
        c = coin.SoBaseColor()
        c.rgb = (0.30, 0.85, 1.0)           # cyan to distinguish from control pts
        self.mk_coord = coin.SoCoordinate3()
        self.mk_set = coin.SoMarkerSet()
        try:
            self.mk_set.markerIndex = coin.SoMarkerSet.DIAMOND_FILLED_9_9
        except Exception:
            pass
        mk.addChild(c)
        mk.addChild(self.mk_coord)
        mk.addChild(self.mk_set)
        self.root.addChild(mk)

        # selected-knot highlight ring (rendered on top)
        self.hl_switch = coin.SoSwitch()
        self.hl_switch.whichChild = -1
        hl = coin.SoSeparator()
        hc = coin.SoBaseColor()
        hc.rgb = (1.0, 0.55, 0.10)
        self.hl_coord = coin.SoCoordinate3()
        hm = coin.SoMarkerSet()
        try:
            hm.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
        except Exception:
            pass
        hm.numPoints = 1
        hl.addChild(hc)
        hl.addChild(self.hl_coord)
        hl.addChild(hm)
        self.hl_switch.addChild(hl)
        self.root.addChild(self.hl_switch)

        # Tangent-direction arrow gizmo on the selected knot. Orange to match
        # the highlight ring; only visible when a knot is selected. Dragging
        # it constrains the knot move to slide along the curve tangent.
        self.arrow_switch = coin.SoSwitch()
        self.arrow_switch.whichChild = -1
        self.arrow_coord = coin.SoCoordinate3()
        self.arrow_line = coin.SoLineSet()
        self.arrow_line.numVertices.setValue(2)
        self.arrow_tip_trans = coin.SoTranslation()
        self.arrow_cone = coin.SoCone()
        self.arrow_cone.height = 1.0
        self.arrow_cone.bottomRadius = 0.3
        # default cone is along +Y; we'll rotate per-frame to match tangent
        self.arrow_rot = coin.SoRotation()
        asep = coin.SoSeparator()
        acol = coin.SoBaseColor()
        acol.rgb = (1.0, 0.55, 0.10)
        astyle = coin.SoDrawStyle()
        astyle.lineWidth = 3
        asep.addChild(acol)
        asep.addChild(astyle)
        asep.addChild(self.arrow_coord)
        asep.addChild(self.arrow_line)
        asep.addChild(self.arrow_tip_trans)
        asep.addChild(self.arrow_rot)
        asep.addChild(self.arrow_cone)
        self.arrow_switch.addChild(asep)
        self.root.addChild(self.arrow_switch)

        self.view.getSceneGraph().addChild(self.root)
        self._refresh_markers()

    def _refresh_markers(self):
        ks = self._knots()
        pts = []
        for t in ks:
            p = self._value_at_t(t)
            if p is not None:
                pts.append((p.x, p.y, p.z))
        self.mk_coord.point.setValues(0, len(pts), pts)
        self.mk_set.numPoints = len(pts)
        self._update_highlight()
        self._update_arrow()

    def _update_highlight(self):
        ks = self._knots()
        if not (0 <= self.sel < len(ks)):
            self.hl_switch.whichChild = -1
            return
        p = self._value_at_t(ks[self.sel])
        if p is None:
            self.hl_switch.whichChild = -1
            return
        self.hl_coord.point.set1Value(0, p.x, p.y, p.z)
        self.hl_switch.whichChild = 0

    def _tangent_at_t(self, t):
        """Unit tangent of the curve at parameter t in [0,1] (or (0,0,0) on fail)."""
        e = self._first_edge()
        if e is None:
            return Vector(0, 0, 0)
        try:
            fp, lp = e.FirstParameter, e.LastParameter
            u = fp + max(0.0, min(1.0, t)) * (lp - fp)
            d = e.tangentAt(u)
            if d.Length > 1e-9:
                d = d * (1.0 / d.Length)
            return d
        except Exception:
            return Vector(0, 0, 0)

    def _arrow_len(self):
        # a fixed screen-friendly arrow length: ~6% of the curve bbox diagonal
        e = self._first_edge()
        if e is None:
            return 1.0
        try:
            bb = e.BoundBox
            diag = ((bb.XLength) ** 2 + (bb.YLength) ** 2
                    + (bb.ZLength) ** 2) ** 0.5
        except Exception:
            diag = 1.0
        return max(diag * 0.06, 0.5)

    def _update_arrow(self):
        """Show a tangent-direction arrow at the selected knot (or hide it)."""
        ks = self._knots()
        if not (0 <= self.sel < len(ks)):
            self.arrow_switch.whichChild = -1
            return
        p = self._value_at_t(ks[self.sel])
        if p is None:
            self.arrow_switch.whichChild = -1
            return
        L = self._arrow_len()
        tan = self._tangent_at_t(ks[self.sel])
        tip = Vector(p.x + tan.x * L, p.y + tan.y * L, p.z + tan.z * L)
        self.arrow_coord.point.setValues(
            0, 2, [(p.x, p.y, p.z), (tip.x, tip.y, tip.z)])
        self.arrow_tip_trans.translation.setValue(tip.x, tip.y, tip.z)
        # rotate the default +Y cone to align with `tan` (rotation from +Y)
        if tan.Length > 1e-9:
            from pivy import coin as _coin
            q = _coin.SbRotation(_coin.SbVec3f(0, 1, 0),
                                 _coin.SbVec3f(tan.x, tan.y, tan.z))
            self.arrow_rot.rotation.setValue(q)
        # size the cone proportional to the arrow length
        self.arrow_cone.height = max(L * 0.30, 0.2)
        self.arrow_cone.bottomRadius = max(L * 0.10, 0.06)
        self.arrow_switch.whichChild = 0

    def _set_arrows_at(self, p, tan, L):
        """Reposition the arrow to a different anchor point / tangent."""
        tip = Vector(p.x + tan.x * L, p.y + tan.y * L, p.z + tan.z * L)
        self.arrow_coord.point.setValues(
            0, 2, [(p.x, p.y, p.z), (tip.x, tip.y, tip.z)])
        self.arrow_tip_trans.translation.setValue(tip.x, tip.y, tip.z)

    # ---- picking ---- #
    def _pick_knot(self, pos):
        """Find the knot whose rendered marker is nearest the cursor screen px."""
        ks = self._knots()
        best, bestd = -1, self._pick_px
        for i, t in enumerate(ks):
            p = self._value_at_t(t)
            if p is None:
                continue
            sp = self._screen(p)
            if sp is None:
                continue
            d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
            if d < bestd:
                best, bestd = i, d
        return best

    def _pick_arrow(self, pos):
        """If the cursor is on the tangent arrow of the selected knot, return
        the anchor's screen position; otherwise None."""
        ks = self._knots()
        if not (0 <= self.sel < len(ks)):
            return None
        p = self._value_at_t(ks[self.sel])
        if p is None:
            return None
        L = self._arrow_len()
        tan = self._tangent_at_t(ks[self.sel])
        tip = Vector(p.x + tan.x * L, p.y + tan.y * L, p.z + tan.z * L)
        sp_anchor = self._screen(p)
        sp_tip = self._screen(tip)
        if sp_anchor is None or sp_tip is None:
            return None
        # The arrow's own segment starts exactly at the knot's screen
        # position, so a plain click on the knot marker itself is also
        # "close to this segment" -- without this guard EVERY knot drag
        # got misclassified as grabbing the tangent arrow (see _press),
        # which locks the drag to sliding along the curve's tangent at
        # that point instead of free 2D repositioning. Require the click
        # to be meaningfully past the marker's own pick radius before
        # treating it as an arrow grab -- clicks within that radius fall
        # through to _pick_knot's normal (unconstrained) drag instead.
        d_anchor = ((pos[0] - sp_anchor[0]) ** 2
                   + (pos[1] - sp_anchor[1]) ** 2) ** 0.5
        if d_anchor < self._pick_px:
            return None
        d = _seg_dist(pos, sp_anchor, sp_tip)
        return (sp_anchor, sp_tip) if d < self._pick_px else None

    # ---- events ---- #
    def _on_button(self, ev):
        try:
            if ev.get("Type") != "SoMouseButtonEvent":
                return
            if ev.get("Button") != "BUTTON1":
                return
            if ev.get("State") == "DOWN":
                # Cancel any existing drag
                if self.drag:
                    self._release()

                # Check for Ctrl+click for coordinate input
                if self.dragging_with_ctrl and not self.drag:
                    self._show_input_field(ev["Position"])
                    return

                # Go straight to knot picking. This used to copy PointEditor's
                # is_bezier / _pick_handle branch, but KnotEditor has neither
                # attribute -- the AttributeError was swallowed by the bare
                # except below, so every click silently did nothing and knot
                # editing never started.
                self._press(ev["Position"])
            elif ev.get("State") == "UP":
                self._release()
        except Exception:
            pass

    def _press(self, pos):
        if self.drag:
            # Previous drag never got its mouse-up (see _start_watchdog's
            # docstring below) -- clear it before starting a new one.
            self._release()
        self._refresh_sample_cache()
        # arrow takes priority over a knot marker underneath it
        on_arrow = self._pick_arrow(pos)
        idx = self._pick_knot(pos)
        if idx is None or idx < 0:
            return
        self.sel = idx
        self._update_highlight()
        self._update_arrow()
        ks = self._knots()
        if not (0 <= idx < len(ks)):
            return
        lo = ks[idx - 1] if idx > 0 else ks[idx]
        hi = ks[idx + 1] if idx < len(ks) - 1 else ks[idx]
        # for clamped curves, the endpoints are pinned at 0 and 1
        if idx == 0:
            lo = 0.0
        if idx == len(ks) - 1:
            hi = 1.0
        drag = {"idx": idx, "t0": ks[idx], "lo": lo, "hi": hi}
        if on_arrow is not None:
            # constrained-along-tangent drag: project cursor onto the tangent
            # line on screen, then back into curve parameter via sample cache.
            drag["axis"] = 0
            drag["anchor_t"] = ks[idx]
        self.drag = drag
        self._start_watchdog()

    def _on_move(self, ev):
        if not self.drag:
            return
        try:
            pos = ev["Position"]
            d = self.drag
            t_new = self._tangent_screen_to_t(pos) if d.get("axis") == 0 \
                else self._nearest_t_to_screen(pos)
            if t_new is None:
                return
            # clamp between neighbours so knots cannot cross
            t_new = max(d["lo"] + self._MIN_GAP, min(d["hi"] - self._MIN_GAP, t_new))
            d["t_final"] = t_new         # remember for commit-on-release
            ks = self._knots()
            if 0 <= d["idx"] < len(ks):
                ks[d["idx"]] = t_new
                # Live preview only: move the marker on the current (cached)
                # curve. Don't recompute here -- the curve would reshape under
                # our sample cache. We commit on _release().
                self.mk_coord.point.set1Value(d["idx"], *self._marker_for(t_new))
                if d["idx"] == self.sel:
                    self.hl_coord.point.set1Value(0, *self._marker_for(t_new))
                    if d.get("axis") == 0:
                        # slide the tangent arrow with the knot
                        tan = self._tangent_at_t(t_new)
                        self._set_arrows_at(
                            self._value_at_t(t_new), tan, self._arrow_len())
        except Exception:
            pass

    def _tangent_screen_to_t(self, pos):
        """Project cursor screen pos onto the tangent line at the active knot
        (anchor), then find the parameter on the curve whose screen point is
        nearest to that projection. Used for axis-constrained drag."""
        ks = self._knots()
        if not (0 <= self.sel < len(ks)) or not self._samples_cache:
            return None
        anchor_t = self.drag.get("anchor_t", ks[self.sel])
        anchor = self._value_at_t(anchor_t)
        if anchor is None:
            return None
        tan = self._tangent_at_t(anchor_t)
        L = self._arrow_len()
        # anchor + tan*L is the arrow tip in world; project both onto screen
        tip = Vector(anchor.x + tan.x * L, anchor.y + tan.y * L,
                      anchor.z + tan.z * L)
        sp_anchor = self._screen(anchor)
        sp_tip = self._screen(tip)
        if sp_anchor is None or sp_tip is None:
            return None
        # foot of perpendicular from cursor onto segment (sp_anchor -> sp_tip)
        ax, ay = sp_anchor
        bx, by = sp_tip
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-9:
            return self._nearest_t_to_screen(pos)
        u = ((pos[0] - ax) * dx + (pos[1] - ay) * dy) / L2
        u = max(0.0, min(1.0, u))
        proj = (ax + u * dx, ay + u * dy)
        # map projection to nearest parameter via sample cache
        bx2, by2 = proj
        best, bestd = None, 1e18
        for t, x, y in self._samples_cache:
            d2 = (x - bx2) * (x - bx2) + (y - by2) * (y - by2)
            if d2 < bestd:
                best, bestd = t, d2
        return best

    def _marker_for(self, t):
        p = self._value_at_t(t)
        return (p.x, p.y, p.z) if p is not None else (0.0, 0.0, 0.0)

    # ---- Advanced curve editing operations ---- #
    def _elevate_degree(self):
        """Elevate the degree of the curve by 1 while preserving shape."""
        try:
            curve = self._first_edge()
            if curve is None or not isinstance(curve.Curve, Part.BSplineCurve):
                return

            # Get current degree and increase by 1
            current_degree = curve.Curve.Degree
            new_degree = min(current_degree + 1, 10)  # Limit to reasonable maximum

            # Create a copy of the curve and elevate its degree
            bs_curve = curve.Curve.copy()
            bs_curve.increaseDegree(new_degree)

            # Update the object with the new curve
            new_shape = bs_curve.toShape()
            self.obj.Shape = new_shape
            self.obj.recompute()
            self.obj.Document.recompute()

            # Refresh display
            self._refresh_markers()
            self._refresh_markers()
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: Degree elevation failed: %s\n" % exc)

    def _reduce_degree(self):
        """Reduce the degree of the curve by 1 while approximating shape."""
        try:
            curve = self._first_edge()
            if curve is None or not isinstance(curve.Curve, Part.BSplineCurve):
                return

            # Get current degree and decrease by 1 (minimum degree 1)
            current_degree = curve.Curve.Degree
            if current_degree <= 1:
                return  # Already at minimum degree

            new_degree = current_degree - 1

            # Sample points from the current curve for approximation
            pts = []
            num_samples = max(20, (current_degree + 1) * 4)  # Sample adequately
            for i in range(num_samples + 1):
                t = i / num_samples
                pt = curve.valueAt(t * curve.Length)
                pts.append(pt)

            # Create approximated curve with lower degree
            new_curve = Part.BSplineCurve()
            new_curve.approximate(Points=pts, DegMin=new_degree, DegMax=new_degree, Tolerance=1e-3)

            # Update the object with the new curve
            new_shape = new_curve.toShape()
            self.obj.Shape = new_shape
            self.obj.recompute()
            self.obj.Document.recompute()

            # Refresh display
            self._refresh_markers()
            self._refresh_markers()
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: Degree reduction failed: %s\n" % exc)

    def _insert_knot(self, param):
        """Insert a knot at the specified parameter value."""
        try:
            curve = self._first_edge()
            if curve is None or not isinstance(curve.Curve, Part.BSplineCurve):
                return False

            # Get the BSpline curve
            bs_curve = curve.Curve.copy()

            # Convert parameter to curve's domain
            if curve.Length > 0:
                t = param  # Already normalized [0,1] from our parameters
                param_in_curve = curve.FirstParameter + t * (curve.LastParameter - curve.FirstParameter)
            else:
                return False

            # Insert the knot (multiplicity 1 for simple insertion)
            # Try to insert, if it fails (knot already exists), try with multiplicity 0
            try:
                bs_curve.insertKnot(param_in_curve, 1, 1e-3, False)
            except:
                # If insertion fails, the knot might already exist
                return False

            # Update the object with the new curve
            new_shape = bs_curve.toShape()
            self.obj.Shape = new_shape
            self.obj.recompute()
            self.obj.Document.recompute()

            # Refresh display
            self._refresh_markers()
            self._refresh_markers()
            return True
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: Knot insertion failed: %s\n" % exc)
            return False

    def _remove_knot(self, index):
        """Remove a knot at the specified index."""
        try:
            curve = self._first_edge()
            if curve is None or not isinstance(curve.Curve, Part.BSplineCurve):
                return False

            knots = self._knots()
            if index < 0 or index >= len(knots):
                return False

            # Get the parameter value of the knot to remove
            param = knots[index]

            # Convert parameter to curve's domain
            if curve.Length > 0:
                param_in_curve = curve.FirstParameter + param * (curve.LastParameter - curve.FirstParameter)
            else:
                return False

            # Get the BSpline curve
            bs_curve = curve.Curve.copy()

            # Remove the knot (assuming multiplicity 1)
            # Note: Exact knot removal with multiplicity is complex in OCC
            # For simplicity, we'll approximate by removing and re-approximating
            pts = []
            num_samples = max(20, (bs_curve.Degree + 1) * 4)
            for i in range(num_samples + 1):
                t = i / num_samples
                pt = bs_curve.valueAt(t * bs_curve.Length)
                pts.append(pt)

            # Create approximated curve without the knot (using degree reduction approximation)
            # Actually, we'll just recreate with current degree but without explicitly removing knot
            # since direct knot removal is non-trivial, we approximate to remove redundancy
            new_curve = Part.BSplineCurve()
            new_curve.approximate(Points=pts, DegMin=bs_curve.Degree, DegMax=bs_curve.Degree, Tolerance=1e-3)

            # Update the object with the new curve
            new_shape = new_curve.toShape()
            self.obj.Shape = new_shape
            self.obj.recompute()
            self.obj.Document.recompute()

            # Refresh display
            self._refresh_markers()
            self._refresh_markers()
            return True
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: Knot removal failed: %s\n" % exc)
            return False

    def _reparameterize(self, method="uniform"):
        """Reparameterize the curve using the specified method."""
        try:
            curve = self._first_edge()
            if curve is None:
                return

            if method == "uniform":
                # For uniform reparameterization, we sample points and rebuild with uniform knots
                pts = []
                num_samples = max(20, (curve.Degree + 1) * 4)
                for i in range(num_samples + 1):
                    t = i / num_samples
                    pt = curve.valueAt(t * curve.Length)
                    pts.append(pt)

                # Rebuild curve with uniform knot vector
                new_curve = Part.BSplineCurve()
                new_curve.interpolate(Points=pts, Parameter=[i/num_samples for i in range(num_samples + 1)])

                # Update the object with the new curve
                new_shape = new_curve.toShape()
                self.obj.Shape = new_shape
                self.obj.recompute()
                self.obj.Document.recompute()

                # Refresh display
                self._refresh_markers()
                self._refresh_markers()
            elif method == "centripetal":
                # Centripetal parameterization based on square root of chord length
                pts = []
                if curve.Length > 0:
                    # Sample points along the curve
                    num_samples = max(20, (curve.Degree + 1) * 4)
                    for i in range(num_samples + 1):
                        t = i / num_samples
                        pt = curve.valueAt(t * curve.Length)
                        pts.append(pt)

                    # Calculate centripetal parameters
                    params = [0.0]
                    for i in range(1, len(pts)):
                        # Distance between consecutive points
                        dist = (pts[i] - pts[i-1]).Length
                        params.append(params[-1] + math.sqrt(dist))

                    # Normalize to [0,1]
                    if params[-1] > 0:
                        params = [p / params[-1] for p in params]

                    # Rebuild curve with centripetal parameterization
                    new_curve = Part.BSplineCurve()
                    new_curve.interpolate(Points=pts, Parameter=params)

                    # Update the object with the new curve
                    new_shape = new_curve.toShape()
                    self.obj.Shape = new_shape
                    self.obj.recompute()
                    self.obj.Document.recompute()

                    # Refresh display
                    self._refresh_markers()
                    self._refresh_markers()
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: Reparameterization failed: %s\n" % exc)

    def _fit_curve(self, tolerance=1e-3):
        """Fit (approximate) the curve to a set of points with specified tolerance."""
        try:
            curve = self._first_edge()
            if curve is None or not isinstance(curve.Curve, Part.BSplineCurve):
                return

            # Sample points from the current curve
            pts = []
            num_samples = max(20, (curve.Degree + 1) * 4)
            for i in range(num_samples + 1):
                t = i / num_samples
                pt = curve.valueAt(t * curve.Length)
                pts.append(pt)

            # Create approximated curve with same degree but specified tolerance
            new_curve = Part.BSplineCurve()
            new_curve.approximate(Points=pts, DegMin=curve.Degree, DegMax=curve.Degree, Tolerance=tolerance)

            # Update the object with the new curve
            new_shape = new_curve.toShape()
            self.obj.Shape = new_shape
            self.obj.recompute()
            self.obj.Document.recompute()

            # Refresh display
            self._refresh_markers()
            self._refresh_markers()
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: Curve fitting failed: %s\n" % exc)

    def _release(self):
        if not self.drag:
            return
        self._stop_watchdog()
        idx = self.drag["idx"]
        t_final = self.drag.get("t_final")
        self.drag = None
        # Commit the final knot value and recompute once (deferred, like
        # PointEditor). Apply the value the user actually dragged to (t_final);
        # previously this re-read the unchanged obj.Knots and wrote it straight
        # back, so knot drags were silently discarded.
        def commit():
            try:
                ks = self._knots()
                if t_final is not None and 0 <= idx < len(ks):
                    ks[idx] = t_final
                    self.obj.Knots = ks
                    self.obj.recompute()
                    self.obj.Document.recompute()
                self._refresh_markers()
                if self.on_commit is not None:
                    try:
                        self.on_commit()
                    except Exception:
                        pass
            except Exception as exc:
                FreeCAD.Console.PrintError("Railform (knots): %s\n" % exc)

        if QtCore is not None:
            QtCore.QTimer.singleShot(0, commit)
        else:
            commit()

    # ---- release-event safety net ---- #
    def _start_watchdog(self):
        """Poll the real Qt button state while a drag is active.

        Coin's SoMouseButtonEvent is only delivered to addEventCallback
        while the cursor is over the 3D view widget. If the LMB is
        released after a fast drag has carried the cursor outside that
        widget (very easy to do -- e.g. dragging toward the task panel to
        click OK), the UP event never reaches _on_button and self.drag is
        left set forever: this is the reported "stuck in drag mode, can't
        click OK" bug. This timer is the fix -- if our own drag state ever
        disagrees with the OS/Qt button state, force the release ourselves.
        """
        if QtCore is None or QtWidgets is None or self._watchdog is not None:
            return
        self._watchdog = QtCore.QTimer()
        self._watchdog.setInterval(120)
        self._watchdog.timeout.connect(self._check_button_still_down)
        self._watchdog.start()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            try:
                self._watchdog.stop()
                self._watchdog.deleteLater()
            except Exception:
                pass
            self._watchdog = None

    def _check_button_still_down(self):
        if not self.drag:
            self._stop_watchdog()
            return
        try:
            if not (QtWidgets.QApplication.mouseButtons()
                    & QtCore.Qt.LeftButton):
                self._release()
        except Exception:
            pass

    # ---- teardown ---- #
    def finish(self):
        self._stop_watchdog()
        drop_callbacks(self.view, (
            ("SoMouseButtonEvent", self, "_cb_btn"),
            ("SoLocation2Event", self, "_cb_mov"),
        ))
        try:
            self.view.getSceneGraph().removeChild(self.root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: knot editor scene teardown failed (%s)\n" % exc)


class CornerFilletEditor(object):
    """Illustrator-style LIVE per-corner fillet handles.

    Two modes, keyed off the edited object itself:
      * IN PLACE (preferred): `fillet_obj` IS the polyline (has a
        `Fillets` record) -- handles read/write that record directly, so
        the fillet lives on the curve, SolidWorks-sketch style, and no
        second object exists.
      * DERIVED (legacy): `fillet_obj` is a parametric.PolylineFillet
        and reads/writes its `Radii` off its linked `Source`.

    One small ring marker per rounded-eligible corner of the SOURCE
    polyline, positioned exactly on that corner's fillet arc apex (the
    same `pmid` point core.polyline_corner_fillet computes for its arc)
    -- so the handle IS the visible fillet, not an abstract proxy for
    it. Dragging a handle along the corner's own bisector changes ONLY
    that corner's radius, live, following the exact
    d = r / tan(half_angle) relationship the underlying fillet
    construction uses -- so the handle position and the arc actually
    built always agree, with no separate approximation of "where the
    handle should be" vs "what the geometry does".

    A sharp (radius 0) corner's handle sits directly ON the corner
    itself (d=0), matching Illustrator's own convention that a
    zero-radius live corner has its handle right at the point.

    Follows the same manual addEventCallback + Qt-poll-watchdog pattern
    as PointEditor/KnotEditor in this module (see the module docstring
    for why SoDragger is avoided here) rather than a native dragger.

    Entered/exited by the owning PolylineFilletPanel, alongside (not
    instead of) the polyline's own PointEditor -- this only manages the
    fillet-radius handles, not point add/remove/drag, which stays with
    PointEditor on the source curve if the user opens it separately.
    """

    def __init__(self, fillet_obj, vobj_of_source):
        self.obj = fillet_obj                # PolylineFillet, or (in
                                             # place) the polyline itself
        self.vobj = vobj_of_source            # ViewObject of the SOURCE curve
        # In place iff the edited object carries the `Fillets` record
        # itself (a Polyline); legacy PolylineFillet objects have
        # Source/Radii instead.
        self._inplace = hasattr(fillet_obj, "Fillets")
        doc = FreeCADGui.ActiveDocument
        self.view = doc.ActiveView if doc is not None else None
        self.sel = -1
        self.drag = None
        self._watchdog = None
        self.on_commit = None                # called after each drag commit
        self._cb_btn = None
        self._cb_mov = None
        self._idx_order = []                 # marker k -> corner index
        self._pick_px = (snaputil._snap_tolerance_mm(self.view, 2.5)
                         if self.view is not None else 14.0)
        self.ok = False
        try:
            from pivy import coin
            self.coin = coin
            self._build()
            self._cb_btn = self.view.addEventCallback(
                "SoMouseButtonEvent", self._on_button)
            self._cb_mov = self.view.addEventCallback(
                "SoLocation2Event", self._on_move)
            self.ok = True
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: corner fillet handles unavailable (%s)\n" % exc)

    # ---- geometry (mirrors core.polyline_corner_fillet's per-corner math) --
    def _source_points(self):
        if self._inplace:
            return list(getattr(self.obj, "Points", []) or [])
        src = getattr(self.obj, "Source", None)
        if src is None:
            return []
        return list(getattr(src, "Points", []) or [])

    def _radii(self):
        """Current per-corner radii, index-aligned with _source_points().

        In place: the `Fillets` record itself, zero-padded so corners
        added after the record was written show as sharp (their index
        simply has no radius yet). Legacy: falls back to a flat list of
        the uniform Radius, same convention as PolylineFilletFP.execute().
        """
        pts = self._source_points()
        if self._inplace:
            radii = list(getattr(self.obj, "Fillets", []) or [])
            if len(radii) < len(pts):
                radii += [0.0] * (len(pts) - len(radii))
            return radii[:len(pts)]
        radii = list(getattr(self.obj, "Radii", []) or [])
        if len(radii) != len(pts):
            radii = [float(getattr(self.obj, "Radius", 5.0))] * len(pts)
        return radii

    def _set_radii(self, radii):
        """Persist a dragged/edited radius list: the `Fillets` record in
        place, `Radii` on the legacy derived PolylineFillet."""
        if self._inplace:
            self.obj.Fillets = [float(r) for r in radii]
        else:
            self.obj.Radii = radii

    def _corner_indices(self):
        pts = self._source_points()
        n = len(pts)
        if n < 3:
            return []
        if self._inplace:
            closed = bool(getattr(self.obj, "FilletClosed", False))
        else:
            closed = bool(getattr(self.obj, "Closed", False))
        return list(range(n)) if closed else list(range(1, n - 1))

    def _corner_frame(self, i, tol=1e-7):
        """(b, u_unit, v_unit, bis_unit, half_angle, max_d) for corner i,
        or None if degenerate/collinear -- the same per-corner checks
        core.polyline_corner_fillet's own loop applies."""
        pts = self._source_points()
        n = len(pts)
        if not (0 <= i < n) or n < 3:
            return None
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        u, v = a - b, c - b
        lu, lv = u.Length, v.Length
        if lu < tol or lv < tol:
            return None
        u = u * (1.0 / lu)
        v = v * (1.0 / lv)
        cos_phi = max(-1.0, min(1.0, u.dot(v)))
        phi = math.acos(cos_phi)
        if phi < 1e-4 or abs(math.pi - phi) < 1e-4:
            return None            # collinear -- nothing to fillet here
        bis = u + v
        if bis.Length < tol:
            return None
        bis = bis * (1.0 / bis.Length)
        return b, u, v, bis, phi / 2.0, min(0.5 * lu, 0.5 * lv)

    def _handle_point(self, i):
        """World position of corner i's handle: the fillet arc's apex
        (the point closest to the corner on the arc) for a rounded
        corner, or the bare corner point itself for a sharp (radius 0)
        one -- matching Illustrator's own zero-radius handle placement."""
        fr = self._corner_frame(i)
        if fr is None:
            return None
        b, u, v, bis, half, max_d = fr
        radii = self._radii()
        r = radii[i] if i < len(radii) and radii[i] else 0.0
        if r <= 0:
            return b
        d = min(r / math.tan(half), max_d)
        r_eff = d * math.tan(half)
        center = b + bis * (r_eff / math.sin(half))
        cb = b - center
        if cb.Length < 1e-9:
            return b
        return center + cb * (r_eff / cb.Length)

    # ---- coin scene ---- #
    def _build(self):
        coin = self.coin
        self.root = coin.SoSeparator()
        col = coin.SoBaseColor()
        col.rgb = (1.0, 0.55, 0.10)          # orange -- matches KnotEditor's ring
        self.mk_coord = coin.SoCoordinate3()
        self.mk_set = coin.SoMarkerSet()
        try:
            self.mk_set.markerIndex = coin.SoMarkerSet.CIRCLE_LINE_9_9
        except Exception:
            pass
        self.root.addChild(col)
        self.root.addChild(self.mk_coord)
        self.root.addChild(self.mk_set)

        # selected-handle highlight (filled, brighter) drawn on top
        self.hl_switch = coin.SoSwitch()
        self.hl_switch.whichChild = -1
        hl = coin.SoSeparator()
        hc = coin.SoBaseColor()
        hc.rgb = (1.0, 0.8, 0.15)
        self.hl_coord = coin.SoCoordinate3()
        hm = coin.SoMarkerSet()
        try:
            hm.markerIndex = coin.SoMarkerSet.CIRCLE_FILLED_7_7
        except Exception:
            pass
        hm.numPoints = 1
        hl.addChild(hc)
        hl.addChild(self.hl_coord)
        hl.addChild(hm)
        self.hl_switch.addChild(hl)
        self.root.addChild(self.hl_switch)

        self.view.getSceneGraph().addChild(self.root)
        self._refresh_markers()

    def _refresh_markers(self):
        idxs = self._corner_indices()
        pts = []
        for i in idxs:
            p = self._handle_point(i)
            if p is not None:
                pts.append((p.x, p.y, p.z))
        self.mk_coord.point.setValues(0, len(pts), pts)
        self.mk_set.numPoints = len(pts)
        self._idx_order = idxs
        self._update_highlight()

    def _update_highlight(self):
        if not (0 <= self.sel < len(self._source_points())):
            self.hl_switch.whichChild = -1
            return
        p = self._handle_point(self.sel)
        if p is None:
            self.hl_switch.whichChild = -1
            return
        self.hl_coord.point.set1Value(0, p.x, p.y, p.z)
        self.hl_switch.whichChild = 0

    def _screen(self, vec):
        return snaputil.screen_project(self.view, vec)

    def _pick(self, pos):
        best, bestd = None, self._pick_px
        for i in self._idx_order:
            p = self._handle_point(i)
            if p is None:
                continue
            sp = self._screen(p)
            if sp is None:
                continue
            d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
            if d < bestd:
                best, bestd = i, d
        return best

    # ---- events ---- #
    def _on_button(self, ev):
        try:
            if ev.get("Type") != "SoMouseButtonEvent":
                return
            if ev.get("Button") != "BUTTON1":
                return
            if ev.get("State") == "DOWN":
                if self.drag:
                    self._release()
                idx = self._pick(ev["Position"])
                if idx is not None:
                    self.sel = idx
                    self.drag = {"idx": idx}
                    self._update_highlight()
                    self._start_watchdog()
            elif ev.get("State") == "UP":
                self._release()
        except Exception:
            pass

    def _on_move(self, ev):
        if not self.drag:
            return
        try:
            i = self.drag["idx"]
            fr = self._corner_frame(i)
            if fr is None:
                return
            b, u, v, bis, half, max_d = fr
            pos = ev["Position"]
            world = self.view.getPoint(pos[0], pos[1])
            if world is None:
                return
            # Project the cursor onto the corner's own bisector ray
            # (from the corner point b, along bis), clamp to a valid
            # tangent-distance range -- this is exactly the `d` core.
            # polyline_corner_fillet solves for from a given radius, run
            # in reverse (handle position -> d -> radius).
            d = max(0.0, min(max_d, (world - b).dot(bis)))
            r = d * math.tan(half) if d > 1e-9 else 0.0
            radii = self._radii()
            if i < len(radii):
                radii[i] = r
            self._set_radii(radii)
            try:
                self.obj.recompute()
            except Exception:
                pass
            self._refresh_markers()
        except Exception:
            pass

    def _release(self):
        if not self.drag:
            return
        self._stop_watchdog()
        self.drag = None
        if self.on_commit is not None:
            try:
                self.on_commit()
            except Exception:
                pass

    # ---- release-event safety net (see PointEditor's own for why) ---- #
    def _start_watchdog(self):
        if QtCore is None or QtWidgets is None or self._watchdog is not None:
            return
        self._watchdog = QtCore.QTimer()
        self._watchdog.setInterval(120)
        self._watchdog.timeout.connect(self._check_button_still_down)
        self._watchdog.start()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            try:
                self._watchdog.stop()
                self._watchdog.deleteLater()
            except Exception:
                pass
            self._watchdog = None

    def _check_button_still_down(self):
        if not self.drag:
            self._stop_watchdog()
            return
        try:
            if not (QtWidgets.QApplication.mouseButtons()
                    & QtCore.Qt.LeftButton):
                self._release()
        except Exception:
            pass

    # ---- teardown ---- #
    def finish(self):
        self._stop_watchdog()
        drop_callbacks(self.view, (
            ("SoMouseButtonEvent", self, "_cb_btn"),
            ("SoLocation2Event", self, "_cb_mov"),
        ))
        try:
            self.view.getSceneGraph().removeChild(self.root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: corner fillet handle scene teardown failed "
                "(%s)\n" % exc)


class SurfaceEditor(object):
    """Direct surface control point (CV) manipulation -- ViewProvider edit mode.

    Allows direct manipulation of surface control points (vertices of the
    control net) for NURBS surfaces. Similar to PointEditor but for 2D
    control point grids instead of 1D curve control points.

    Entered by double-clicking a surface (SurfaceVP.setEdit).
    """

    def __init__(self, vobj):
        self.vobj = vobj
        self.obj = vobj.Object
        doc = FreeCADGui.ActiveDocument
        self.view = doc.ActiveView if doc is not None else None
        self.sel_u = 0  # selected u index
        self.sel_v = 0  # selected v index
        self.drag = None  # {"u": u, "v": v, "start_u": u, "start_v": v}
        self._watchdog = None
        self.on_commit = None  # called after each drag commit (panel refresh)
        self._cb_btn = None
        self._cb_mov = None
        self.ok = False
        self.input_field = None  # Qt line edit for coordinate input
        self.dragging_with_ctrl = False  # True when Ctrl is held during drag
        self._surface_cache = {}  # cache for surface conversions
        self._original_surface = None  # backup of original surface
        self._control_grid = None  # 2D list of control points
        self._pick_px = (snaputil._snap_tolerance_mm(self.view, 2.5)
                         if self.view is not None else 14.0)

        try:
            from pivy import coin
            self.coin = coin
            self._build()
            self._cb_btn = self.view.addEventCallback(
                "SoMouseButtonEvent", self._on_button)
            self._cb_mov = self.view.addEventCallback(
                "SoLocation2Event", self._on_move)
            self.ok = True
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: surface editor unavailable (%s)\n" % exc)

    # ---- helpers ---- #
    def _update_surface_cache(self):
        """Update cached surface and control grid from the object's shape."""
        try:
            shape = self.obj.Shape
            if not shape or not shape.Faces:
                self._surface_cache = {}
                self._control_grid = None
                return

            face = shape.Faces[0]  # Work with first face for simplicity
            surf, grid = core._face_control_grid(face)
            self._surface_cache['surface'] = surf
            self._surface_cache['grid'] = grid
            self._control_grid = grid
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: surface editor cache update failed (%s)\n" % exc)
            self._surface_cache = {}
            self._control_grid = None

    def _get_control_point(self, u, v):
        """Get control point at (u, v) grid coordinates."""
        if (self._control_grid is None or
            u < 0 or u >= len(self._control_grid) or
            v < 0 or v >= len(self._control_grid[0])):
            return None
        return self._control_grid[u][v]

    def _set_control_point(self, u, v, point):
        """Set control point at (u, v) grid coordinates."""
        if (self._control_grid is not None and
            0 <= u < len(self._control_grid) and
            0 <= v < len(self._control_grid[0])):
            self._control_grid[u][v] = point

    def _update_surface_from_grid(self):
        """Update the object's shape from the modified control grid."""
        try:
            if self._control_grid is None:
                return

            # Create surface from modified control grid
            surf = core.grid_to_surface(self._control_grid)
            shape = surf.toShape()

            # Update the object
            self.obj.Shape = shape
            self.obj.recompute()
            self.obj.Document.recompute()

            # Refresh display
            self._refresh_markers()
        except Exception as exc:
            FreeCAD.Console.PrintError(
                "Railform: surface update failed (%s)\n" % exc)

    def _build(self):
        """Build Coin visualization for control points."""
        try:
            from pivy import coin
            self.root = coin.SoSeparator()

            # Control point markers (yellow spheres)
            self.mk_coord = coin.SoCoordinate3()
            self.mk_set = coin.SoMarkerSet()
            self.mk_set.markerIndex = coin.SoMarkerSet.CUBE
            self.mk_set.renderOrdering = coin.SoGuiHighlightRendering.SORTED_LOW_PRIORITY

            # point picking
            self.pt_switch = coin.SoSwitch()
            self.pt_switch.whichChild = -1

            pt_sep = coin.SoSeparator()
            pt_col = coin.SoBaseColor()
            pt_col.rgb = (0.9, 0.9, 0.2)  # Yellow
            pt_sep.addChild(pt_col)
            pt_style = coin.SoDrawStyle()
            pt_style.pointWidth = 8
            pt_sep.addChild(pt_style)
            pt_sep.addChild(self.mk_coord)
            pt_sep.addChild(self.mk_set)
            self.pt_switch.addChild(pt_sep)

            # Selected point highlight (larger red sphere)
            self.hl_coord = coin.SoCoordinate3()
            self.hl_set = coin.SoMarkerSet()
            self.hl_set.markerIndex = coin.SoMarkerSet.CUBE
            self.hl_set.renderOrdering = coin.SoGuiHighlightRendering.SORTED_HIGH_PRIORITY

            hl_sep = coin.SoSeparator()
            hl_col = coin.SoBaseColor()
            hl_col.rgb = (1.0, 0.3, 0.3)  # Red
            hl_sep.addChild(hl_col)
            hl_style = coin.SoDrawStyle()
            hl_style.pointWidth = 12
            hl_sep.addChild(hl_style)
            hl_sep.addChild(self.hl_coord)
            hl_sep.addChild(self.hl_set)
            self.hl_switch = coin.SoSwitch()
            self.hl_switch.whichChild = -1
            hl_sep.addChild(self.hl_switch)

            self.root.addChild(self.pt_switch)
            self.root.addChild(self.hl_switch)

            # Add to scene
            doc = FreeCADGui.ActiveDocument
            view = doc.ActiveView if doc is not None else None
            if view:
                view.getSceneGraph().addChild(self.root)

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: surface editor visualization unavailable ({exc})\n")

    def _refresh_markers(self):
        """Update the control point markers in the 3D view."""
        try:
            if not self.ok or not self.coin:
                return

            self._update_surface_cache()
            if self._control_grid is None:
                return

            # Flatten the 2D grid into a 1D list of points for Coin
            points = []
            for u_row in self._control_grid:
                for pt in u_row:
                    points.append((pt.x, pt.y, pt.z))

            self.mk_coord.point.setValues(0, len(points), points)
            total_points = len(points)
            self.mk_set.numPoints = total_points

            # Update selected point highlight
            if (0 <= self.sel_u < len(self._control_grid) and
                0 <= self.sel_v < len(self._control_grid[0])):
                pt = self._control_grid[self.sel_u][self.sel_v]
                self.hl_coord.point.set1Value(0, pt.x, pt.y, pt.z)
                self.hl_set.numPoints = 1
                self.hl_switch.whichChild = 0
                self.pt_switch.whichChild = 0  # Enable point picking
            else:
                self.hl_set.numPoints = 0
                self.hl_switch.whichChild = -1
                self.pt_switch.whichChild = -1  # Disable point picking

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: surface marker refresh failed ({exc})\n")

    # ---- picking ---- #
    def _pick_control_point(self, pos):
        """Find the control point nearest the cursor screen position."""
        try:
            if not self.ok or not self.coin or self._control_grid is None:
                return None

            best_u, best_v = -1, -1
            best_dist = float('inf')

            # Check each control point
            for u, u_row in enumerate(self._control_grid):
                for v, pt in enumerate(u_row):
                    # Project 3D point to screen
                    sp = self._screen(pt)
                    if sp is None:
                        continue

                    # Calculate distance to cursor
                    d = ((sp[0] - pos[0]) ** 2 + (sp[1] - pos[1]) ** 2) ** 0.5
                    if d < best_dist and d < self._pick_px:
                        best_dist = d
                        best_u, best_v = u, v

            if best_u >= 0 and best_v >= 0:
                return (best_u, best_v)
            return None
        except Exception:
            return None

    def _screen(self, vec):
        """Project 3D vector to screen coordinates."""
        try:
            return snaputil.screen_project(self.view, vec)
        except Exception:
            return None

    # ---- events ---- #
    def _on_button(self, ev):
        try:
            if ev.get("Type") != "SoMouseButtonEvent":
                return
            if ev.get("Button") != "BUTTON1":
                return
            if ev.get("State") == "DOWN":
                # Cancel any existing drag
                if self.drag:
                    self._release()

                # A copied Ctrl+click coordinate-input branch was removed here
                # (see the other _on_button): SurfaceEditor has no
                # _show_input_field and never sets dragging_with_ctrl.
                # Try to pick a control point for dragging
                if not self.drag:
                    pick_result = self._pick_control_point(ev["Position"])
                    if pick_result is not None:
                        u, v = pick_result
                        self.drag = {"u": u, "v": v, "start_u": u, "start_v": v}
                        self.sel_u, self.sel_v = u, v
                        self._refresh_markers()
                        self._start_watchdog()
                        return

        except Exception:
            pass

    def _on_move(self, ev):
        if not self.drag:
            return
        try:
            pos = ev["Position"]
            # For now, we'll just update the point position based on screen movement
            # A more sophisticated implementation would project onto the surface
            # For simplicity, we'll use a basic approach similar to PointEditor
            u = self.drag["u"]
            v = self.drag["v"]

            # Get current point
            current_pt = self._get_control_point(u, v)
            if current_pt is None:
                return

            # Simple approach: move point relative to mouse movement
            # This is a simplified version - a full implementation would
            # properly project mouse movement to surface parameters
            if hasattr(self, '_last_mouse_pos'):
                dx = pos[0] - self._last_mouse_pos[0]
                dy = pos[1] - self._last_mouse_pos[1]

                # Convert screen delta to world delta (approximate)
                # This is simplified - a real implementation would use proper ray tracing
                scale = 0.01  # pixels to world units scaling factor
                offset = Vector(dx * scale, -dy * scale, 0)  # Y is inverted in screen coords

                new_pt = current_pt + offset
                self._set_control_point(u, v, new_pt)
                self._update_surface_from_grid()
                self._refresh_markers()

            self._last_mouse_pos = pos
        except Exception:
            pass

    def _release(self):
        if not self.drag:
            return
        self._stop_watchdog()
        self.drag = None
        # Final commit already happened in _on_move via _update_surface_from_grid
        if self.on_commit is not None:
            try:
                self.on_commit()
            except Exception:
                pass

    # ---- release-event safety net ---- #
    def _start_watchdog(self):
        """Poll the real Qt button state while a drag is active."""
        if QtCore is None or QtWidgets is None or self._watchdog is not None:
            return
        self._watchdog = QtCore.QTimer()
        self._watchdog.setInterval(120)
        self._watchdog.timeout.connect(self._check_button_still_down)
        self._watchdog.start()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            try:
                self._watchdog.stop()
                self._watchdog.deleteLater()
            except Exception:
                pass
            self._watchdog = None

    def _check_button_still_down(self):
        if not self.drag:
            self._stop_watchdog()
            return
        try:
            if not (QtWidgets.QApplication.mouseButtons()
                    & QtCore.Qt.LeftButton):
                self._release()
        except Exception:
            pass

    # ---- teardown ---- #
    def finish(self):
        self._stop_watchdog()
        drop_callbacks(self.view, (
            ("SoMouseButtonEvent", self, "_cb_btn"),
            ("SoLocation2Event", self, "_cb_mov"),
        ))
        try:
            self.view.getSceneGraph().removeChild(self.root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: knot editor scene teardown failed (%s)\n" % exc)