# -*- coding: utf-8 -*-
"""3D sketch authoring panel (RFS_Sketch3D).

The existing draw tools place points on the *view plane*, so every curve
they make is planar in whatever plane the camera happened to be in when
you clicked. This panel adds true 3D authoring: points can be placed
freely in three dimensions via
  * a construction plane parallel to the view, steppable out of plane
    with an offset spinbox (the ship-design "frame lines" workflow), and
  * snap-to onto arbitrary existing 3D geometry, and
  * direct X/Y/Z table entry per point, or live drag-to-edit in the
    viewport ("Drag points" toggle).

Viewport shortcuts (viewport must have keyboard focus): X/Y/Z toggles
the axis lock, Tab cycles the plane family, Esc drops the active drag.

GUI-only wiring -> the pure decision logic lives in
rfstudio.panel_logic (construction_plane / ray_plane_intersect /
sketch3d_readiness / points_are_planar), covered headlessly by
test_panel_logic.py.
"""

import FreeCAD
import FreeCADGui
import math

from PySide import QtGui

from rfstudio.panel_base import (
    _CollapsibleGroup,
    _ShortcutHintBar,
    _panel_warning,
    _panel_error,
    drop_callbacks,
)
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui

from rfstudio.panel_base import _std_buttons
from rfstudio import panel_logic
from rfstudio import snaputil

KINDS = ["InterpCurve", "ControlCurve", "ApproxCurve", "Polyline",
         "BezierCurve"]


class Sketch3DFP(object):
    """FeaturePython backing a 3D sketch document object.

    Two capabilities in one parametric object (why: constraints and
    mixed entities are exactly the two things the per-curve objects
    from parametric.make_curve can't carry):

      * Per-segment entity tags (Spline/Line/Arc/Ellipse/Rectangle/
        Bezier) + arc bulges ->
        build either a single parametric curve (all-Spline -> one of the
        classic curve kinds against core) or a single Part.Wire mixing
        entities via core.sketch3d_wire (Kind == "Mixed").
      * A persisted constraint list ("distance,i,j,d", 'coincide,i,j',
        'horizontal,i,y', 'vertical,i,x', 'angle,i,vtx,k,deg',
        'equal,i,j,k,l', 'fix,i') that is re-solved over obj.Points on
        every recompute, so dims drive the geometry like a SW sketch.

    Properties come from the panel: Points / Kind / SegmentTypes /
    Bulges / Constraints (segment types + bulges + constraints are
    stored as strings so the object stays valid across document
    reload; floats are repr'd). Empty constraints = plain free points."""

    def __init__(self, obj):
        obj.Proxy = self
        if not hasattr(obj, "Points"):
            obj.addProperty("App::PropertyVectorList", "Points",
                            "Sketch3D", "Sketch points")
        if not hasattr(obj, "Kind"):
            obj.addProperty("App::PropertyString", "Kind", "Sketch3D",
                            "Curve kind, or 'Mixed' for a wire of Line/Arc/Spline")
        if not hasattr(obj, "SegmentTypes"):
            obj.addProperty("App::PropertyStringList", "SegmentTypes",
                            "Sketch3D", "Per-segment entity: Line/Arc/Spline")
        if not hasattr(obj, "Bulges"):
            obj.addProperty("App::PropertyStringList", "Bulges",
                            "Sketch3D", "Per-arc sagitta/chord factor (repr float)")
        if not hasattr(obj, "Constraints"):
            obj.addProperty("App::PropertyStringList", "Constraints",
                            "Sketch3D",
                            "Persisted constraint set, e.g. 'distance,0,1,25.0'")
        if not hasattr(obj, "Variables"):
            obj.addProperty("App::PropertyStringList", "Variables",
                            "Sketch3D",
                            "Named variables for =expressions, 'name,value'")
        # Issue #14: associative projection. ProjectedRefs is aligned
        # 1:1 with Points ('' = free point); ExternalSources holds the
        # link dependencies so the sketch recomputes when a source
        # moves; PlaneOrigin/PlaneNormal persist the projection plane
        # (the panel's construction plane at project time) because
        # execute() has no access to the panel's live camera state.
        if not hasattr(obj, "ProjectedRefs"):
            obj.addProperty("App::PropertyStringList", "ProjectedRefs",
                            "Sketch3D",
                            "Per-point projection ref "
                            "'kind|obj|locator|t|mode|dir' ('' = free)")
        if not hasattr(obj, "ExternalSources"):
            obj.addProperty("App::PropertyLinkList", "ExternalSources",
                            "Sketch3D",
                            "Source objects projected points depend on")
        if not hasattr(obj, "PlaneOrigin"):
            obj.addProperty("App::PropertyVector", "PlaneOrigin",
                            "Sketch3D", "Projection plane origin")
        if not hasattr(obj, "PlaneNormal"):
            obj.addProperty("App::PropertyVector", "PlaneNormal",
                            "Sketch3D", "Projection plane normal")

    def execute(self, obj):
        from rfstudio import core
        from rfstudio import panel_logic
        import json
        try:
            # Load EquationManager state if present (issues.md #10 / #15)
            eq_data = {}
            if hasattr(obj, "EqManagerState") and obj.EqManagerState:
                try:
                    eq_data = json.loads(obj.EqManagerState)
                except Exception:
                    eq_data = {}
            # Build variable namespace: global variables overridden by active config
            variables = _parse_variables(list(getattr(obj, "Variables", []) or []))
            if eq_data.get("configs"):
                active_idx = eq_data.get("active_config", 0)
                if 0 <= active_idx < len(eq_data["configs"]):
                    cfg = eq_data["configs"][active_idx]
                    variables.update(cfg.get("variables", {}))
            constraints = _parse_constraints(list(obj.Constraints))
            raw_pts = list(obj.Points)
            # Issue #14: re-project points that reference external
            # geometry BEFORE resolving/solving, so expressions and the
            # solver both see source-driven coordinates.
            projected = _reproject_points(obj, raw_pts, panel_logic)
            # Apply suppression from active config (issues.md #10 / #15)
            suppressed_constraints = set()
            suppressed_points = set()
            if eq_data.get("configs"):
                active_idx = eq_data.get("active_config", 0)
                if 0 <= active_idx < len(eq_data["configs"]):
                    cfg = eq_data["configs"][active_idx]
                    suppressed_constraints.update(cfg.get("suppressed_constraints", []))
                    suppressed_points.update(cfg.get("suppressed_points", []))
            # Filter out suppressed constraints
            constraints = [c for i, c in enumerate(constraints) if i not in suppressed_constraints]
            constraints = panel_logic.resolve_constraint_expressions(
                constraints, variables,
                [(p.x, p.y, p.z) for p in raw_pts])
            pinned = {c[1] for c in constraints if c[0] == "fix"} | projected
            # Filter out suppressed points from pinning (they stay free)
            pinned = {i for i in pinned if i not in suppressed_points}
            pts = core.solve_point_constraints(
                raw_pts, constraints, pinned=pinned)
            bulges = [float(b) for b in obj.Bulges] if obj.Bulges else None
            types = list(obj.SegmentTypes) if obj.SegmentTypes else None
            if obj.Kind in (None, "", "Mixed"):
                obj.Shape = core.sketch3d_wire(pts, types, bulges)
            else:
                obj.Shape = _build_curve_shape(obj.Kind, pts)
        except Exception as exc:
            _panel_error(self, "Railform Sketch3D: %s" % exc)


def _plane_slice_edges(shape, origin, normal):
    """Intersection of `shape` with the plane (origin, normal) as a flat
    list of edges (Part.Shape.slice returns wires; flatten them). Order
    is OCCT-deterministic for a given shape+plane, which is what makes
    'slice' refs by index survive recompute. Returns [] when the plane
    misses the shape or slicing is unsupported."""
    try:
        nx, ny, nz = normal
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length < 1e-12:
            return []
        from FreeCAD import Vector
        d = nx * origin[0] + ny * origin[1] + nz * origin[2]
        res = shape.slice(Vector(nx, ny, nz), d / length)
    except Exception:
        return []
    edges = []
    for item in res or []:
        if getattr(item, "ShapeType", "") == "Edge":
            edges.append(item)
        else:
            edges.extend(getattr(item, "Edges", None) or [])
    return edges


def _pierce_point(edge, origin, normal, samples=48):
    """issues.md #7 (pierce): the point where the edge first crosses the
    plane (origin, normal) -- sampled sign-change + bisection, so any
    curve type works (line, arc, spline). None when the edge never
    crosses (parallel / entirely on one side), which callers treat as
    'point stays free' instead of an error."""
    ox, oy, oz = origin
    nx, ny, nz = normal

    def s(u):
        p = edge.valueAt(u)
        return ((p.x - ox) * nx + (p.y - oy) * ny + (p.z - oz) * nz)

    u0, u1 = edge.FirstParameter, edge.LastParameter
    prev_u, prev_s = u0, s(u0)
    if abs(prev_s) < 1e-12:
        return edge.valueAt(u0)
    for k in range(1, samples + 1):
        u = u0 + (u1 - u0) * k / float(samples)
        cur_s = s(u)
        if abs(cur_s) < 1e-12:
            return edge.valueAt(u)
        if prev_s * cur_s < 0.0:
            lo, hi, slo = prev_u, u, prev_s
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                sm = s(mid)
                if sm == 0.0 or (hi - lo) < 1e-14:
                    return edge.valueAt(mid)
                if slo * sm < 0.0:
                    hi = mid
                else:
                    lo, slo = mid, sm
            return edge.valueAt(0.5 * (lo + hi))
        prev_u, prev_s = u, cur_s
    return None


def _nearest_on_face(pt, face):
    """issues.md #7 (On Surface): the closest point on `face` to pt,
    or None when the query fails. Tracked against the CURRENT raw point
    each recompute, so the point follows the face when the source body
    moves."""
    try:
        import Part
        from FreeCAD import Vector
        v = Part.Vertex(Vector(pt[0], pt[1], pt[2]))
        q = v.distToShape(face)[1][0][1]   # (vertex point, face point)
        return (q.x, q.y, q.z)
    except Exception:
        return None


def _nearest_on_edges(pt, edges):
    """issues.md #7 (pierce to a FACE): closest point to pt among the
    plane n face intersection edges (nearest branch of the intersection
    curve), or None when there is no crossing."""
    if not edges:
        return None
    try:
        import Part
        from FreeCAD import Vector
        v = Part.Vertex(Vector(pt[0], pt[1], pt[2]))
        best, bd = None, None
        for e in edges:
            res = v.distToShape(e)
            d, q = res[0], res[1][0][1]
            if bd is None or d < bd:
                bd, best = d, (q.x, q.y, q.z)
        return best
    except Exception:
        return None


def _onsurf_point(pt, shape, locator):
    """issues.md #7 (On Surface) resolution. Prefers the face's UV
    anchor stored in the locator ('Face6@u,v'): parameter-space anchoring
    makes the point travel WITH the face (translation AND rotation --
    SolidWorks' on-surface semantics), instead of being re-snapped from
    a stale world position (which clamps at the face boundary once the
    face moves away). A bare 'FaceN' (first derive) or an out-of-range
    UV (source rebuilt) falls back to the nearest point and returns the
    refined locator so the anchor can be persisted into ProjectedRefs.

    Returns (point or None, refined locator or None -- None = no
    rewrite needed)."""
    try:
        base = locator.split("@", 1)[0]
        face = shape.getElement(base)
    except Exception:
        return None, None
    if "@" in locator:
        try:
            u_s, v_s = locator.split("@", 1)[1].split(",")
            u, v = float(u_s), float(v_s)
            u0, u1, v0, v1 = face.ParameterRange
            eu = max(u1 - u0, 1e-9)
            ev = max(v1 - v0, 1e-9)
            if (u0 - 1e-6 * eu <= u <= u1 + 1e-6 * eu and
                    v0 - 1e-6 * ev <= v <= v1 + 1e-6 * ev):
                q = face.valueAt(min(max(u, u0), u1),
                                 min(max(v, v0), v1))
                return (q.x, q.y, q.z), None   # anchor valid: done
        except Exception:
            pass
        # stale/rebuilt UV: fall through to a fresh nearest-point derive
    target = _nearest_on_face(pt, face)
    if target is None:
        return None, None
    refined = None
    try:
        u, v = face.Surface.parameter(FreeCAD.Vector(*target))
        refined = "%s@%r,%r" % (base, u, v)
    except Exception:
        pass   # unparametrizable surface: keep nearest-point behavior
    return target, refined


def _update_ref_locator(obj, i, locator):
    """Persist a refined locator (the face's learned @u,v anchor) back
    into ProjectedRefs entry i -- compare first so an unchanged value
    never re-touches the object on every recompute."""
    try:
        refs = list(obj.ProjectedRefs)
        if i >= len(refs):
            return
        parts = refs[i].split("|")
        if len(parts) != 6 or parts[2] == locator:
            return
        parts[2] = locator
        refs[i] = "|".join(parts)
        obj.ProjectedRefs = refs
    except Exception:
        pass


def _silhouette_edges(shape, direction):
    """issues.md #14 (silhouette projection): the edges separating faces
    that face `direction` from faces that face away (adjacent facing
    signs differ, 0 = grazing) -- the edge-level silhouette of the
    shape. Exact for polyhedral solids; on smooth surfaces the true
    silhouette crosses face interiors, which BREP edges can't represent
    (use 'Slice by sketch plane' there instead).

    Order follows shape.Edges filtered, so silhouette refs by index are
    deterministic for a given shape+direction. Returns [] for shapes
    without faces."""
    dx, dy, dz = direction
    ln = (dx * dx + dy * dy + dz * dz) ** 0.5
    if ln < 1e-12:
        return []
    dx, dy, dz = dx / ln, dy / ln, dz / ln

    def _sign(x, eps=1e-9):
        return 1 if x > eps else (-1 if x < -eps else 0)

    def _face_sign(f):
        try:
            u0, u1, v0, v1 = f.ParameterRange
            n = f.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
            return _sign(n.x * dx + n.y * dy + n.z * dz)
        except Exception:
            return 0   # unmeasurable face counts as grazing
    try:
        faces = list(shape.Faces)
    except Exception:
        return []
    if not faces:
        return []
    edge_faces = []   # (face edge, facing sign) flattened across faces
    for f in faces:
        s = _face_sign(f)
        for fe in f.Edges:
            edge_faces.append((fe, s))
    out = []
    try:
        for e in shape.Edges:
            signs = [s for (fe, s) in edge_faces if e.isSame(fe)]
            if len(signs) >= 2 and any(s != signs[0] for s in signs[1:]):
                out.append(e)
    except Exception:
        return []
    return out


def _reproject_points(obj, raw_pts, panel_logic):
    """Issues #14/#7 core: rewrite every point carrying a ProjectedRefs
    entry from its CURRENT source geometry:

      edge  -- source edge at the stored parameter fraction, projected
               onto the persisted plane (associative projection #14);
      slice -- re-cut section edge of plane n shape (#14);
      sil   -- silhouette edge along the stored direction (#14);
      pierce-- plane n edge/face intersection point, tracked to the
               nearest branch of the current raw point (#7);
      onsurf-- nearest point on the face to the current raw point (#7).

    Returns the set of point indices actually re-derived -- those get
    solver-pinned (the source drives them, exactly like an on-curve
    constraint). A deleted/missing source or unresolvable ref leaves the
    point untouched and FREE, so losing a reference degrades gracefully
    instead of failing the recompute."""
    refs = list(getattr(obj, "ProjectedRefs", []) or [])
    if not refs:
        return set()
    try:
        origin = (obj.PlaneOrigin.x, obj.PlaneOrigin.y, obj.PlaneOrigin.z)
        normal = (obj.PlaneNormal.x, obj.PlaneNormal.y, obj.PlaneNormal.z)
    except Exception:
        return set()
    from FreeCAD import Vector
    doc = obj.Document
    ok = set()
    for i, ref in enumerate(refs):
        if i >= len(raw_pts):
            break
        info = panel_logic.parse_proj_ref(ref)
        if info is None:
            continue
        try:
            src = doc.getObject(info["obj"])
            shape = getattr(src, "Shape", None)
            if shape is None or shape.isNull():
                continue
            kind = info["kind"]
            if kind in ("pierce", "onsurf"):
                # Issue #7: computed against the CURRENT raw point
                # (nearest-branch tracking), no plane projection step.
                raw = (raw_pts[i].x, raw_pts[i].y, raw_pts[i].z)
                refined = None
                if kind == "pierce":
                    loc = info["locator"]
                    if loc.startswith("Edge"):
                        target = _pierce_point(
                            shape.getElement(loc), origin, normal)
                    elif loc.startswith("Face"):
                        target = _nearest_on_edges(
                            raw, _plane_slice_edges(
                                shape.getElement(loc), origin, normal))
                    else:
                        target = None
                else:   # onsurf: UV-anchored (travels with the face)
                    target, refined = _onsurf_point(
                        raw, shape, info["locator"])
                    if refined is not None:
                        _update_ref_locator(obj, i, refined)
                if target is None:
                    continue   # no crossing / query failed -> stays free
                raw_pts[i] = Vector(target[0], target[1], target[2])
                ok.add(i)
                continue
            if kind == "edge":
                edge = shape.getElement(info["locator"])
                proj_dir = info["direction"]
            elif kind == "slice":
                edges = _plane_slice_edges(shape, origin, normal)
                k = int(info["locator"])
                if not (0 <= k < len(edges)):
                    continue
                edge = edges[k]
                proj_dir = None
            else:   # sil
                d = info["direction"]
                if d is None:
                    d = normal
                sil = _silhouette_edges(shape, d)
                k = int(info["locator"])
                if not (0 <= k < len(sil)):
                    continue
                edge = sil[k]
                proj_dir = d
            u0, u1 = edge.FirstParameter, edge.LastParameter
            p = edge.valueAt(u0 + (u1 - u0) * info["t"])
            q = panel_logic.project_point_along(
                (p.x, p.y, p.z), origin, normal, proj_dir)
            if q is None:
                continue
            raw_pts[i] = Vector(q[0], q[1], q[2])
            ok.add(i)
        except Exception:
            continue
    return ok


def _build_curve_shape(kind, pts):
    """Sketch3D Kind -> the matching core curve builder. Mirrors the
    parametric._FP dispatch so the all-Spline case keeps parity with
    what parametric.make_curve would have built."""
    from rfstudio import core
    if kind == "InterpCurve":
        return core.interpolate_curve(pts)
    if kind == "ControlCurve":
        return core.control_poly_curve(pts, degree=3)
    if kind == "ApproxCurve":
        return core.approximate_curve(pts)
    if kind == "Polyline":
        return core.polyline(pts)
    if kind == "BezierCurve":
        return core.bezier_curve_shape(pts)
    raise ValueError("Sketch3D: unknown curve kind %r" % (kind,))


def _parse_constraints(strings):
    """'distance,0,1,25.0' -> ('distance', 0, 1, 25.0), etc. A value
    starting with '=' stays a raw expression string for later
    resolution (panel_logic.resolve_constraint_expressions); unknown
    kind names or wrong arities raise ValueError so a corrupt property
    fails loudly on recompute instead of silently dropping the dim."""
    def _num_or_expr(s):
        s = s.strip()
        return s if s.startswith("=") else float(s)
    out = []
    for s in strings:
        parts = [p.strip() for p in s.split(",")]
        kind = parts[0]
        def _ints(n):
            return [int(float(x)) for x in parts[1:1 + n]]
        if kind == "distance":
            out.append((kind, _ints(2)[0], _ints(2)[1],
                        _num_or_expr(parts[3])))
        elif kind == "coincide":
            out.append((kind, _ints(2)[0], _ints(2)[1]))
        elif kind in ("horizontal", "vertical"):
            out.append((kind, int(float(parts[1])), _num_or_expr(parts[2])))
        elif kind == "angle":
            out.append((kind, _ints(3)[0], _ints(3)[1], _ints(3)[2],
                        _num_or_expr(parts[4])))
        elif kind == "equal":
            out.append((kind, _ints(4)[0], _ints(4)[1], _ints(4)[2],
                        _ints(4)[3]))
        elif kind == "fix":
            out.append((kind, int(float(parts[1]))))
        elif kind == "tangent":
            out.append((kind, _ints(3)[0], _ints(3)[1], _ints(3)[2]))
        elif kind == "concentric":
            out.append((kind, _ints(2)[0], _ints(2)[1]))
        elif kind == "symmetric":
            out.append((kind, _ints(3)[0], _ints(3)[1], _ints(3)[2]))
        elif kind == "mirror":
            out.append((kind, _ints(2)[0], _ints(2)[1], float(parts[3]),
                        float(parts[4]), float(parts[5]), float(parts[6])))
        else:
            raise ValueError("Sketch3D: unknown constraint %r" % s)
    return out


def _parse_variables(strings):
    """'name,value' -> {name: float}. Malformed entries raise ValueError."""
    out = {}
    for s in strings:
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != 2:
            raise ValueError("Sketch3D: bad variable %r" % s)
        out[parts[0]] = float(parts[1])
    return out


def _serialize_variables(variables):
    return ["%s,%r" % (name, value) for name, value in sorted(variables.items())]


def _serialize_constraints(constraints):
    return [",".join(str(v) for v in c) for c in constraints]


def make_sketch3d_obj(kind_tag, doc=None):
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument()
    obj = doc.addObject("Part::FeaturePython", "Sketch3D")
    Sketch3DFP(obj)
    obj.Kind = kind_tag
    if obj.ViewObject is not None:
        # Reuse the same thick-blue SW-style curve style for sketches.
        from rfstudio import parametric
        parametric._style_curve_vobj(obj.ViewObject)
    return obj


def _resolve_sources(doc, refs):
    """Unique source document objects named by projection refs (issue
    #14): returned list goes to ExternalSources so the sketch is
    recomputed when any source moves. Missing/renamed objects are
    skipped (the ref then simply stops re-projecting -- graceful)."""
    out, seen = [], set()
    for ref in refs:
        info = panel_logic.parse_proj_ref(ref)
        if info is None or info["obj"] in seen:
            continue
        seen.add(info["obj"])
        src = doc.getObject(info["obj"])
        if src is not None:
            out.append(src)
    return out


class _PointRow(object):
    """One row of the point table: X/Y/Z spinboxes + delete button."""

    def __init__(self, panel, x=0.0, y=0.0, z=0.0):
        self.panel = panel
        self.widget = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(self.widget)
        lay.setContentsMargins(0, 0, 0, 0)
        self.spins = []
        for v in (x, y, z):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(-1e6, 1e6)
            s.setDecimals(3)
            s.setSingleStep(1.0)
            s.setValue(v)
            s.valueChanged.connect(panel._table_changed)
            lay.addWidget(s)
            self.spins.append(s)
        b = QtWidgets.QPushButton("x")
        b.setFixedWidth(24)
        b.clicked.connect(self._del)
        lay.addWidget(b)

        # Segment entity to the NEXT point (Line/Arc/Spline) + bulge for arcs
        lay.addWidget(QtWidgets.QLabel("→"))
        self.seg = QtWidgets.QComboBox()
        for t in ("Spline", "Line", "Arc", "Ellipse", "Rectangle", "Bezier"):
            self.seg.addItem(t)
        self.seg.currentIndexChanged.connect(panel._table_changed)
        lay.addWidget(self.seg)
        self.bulge = QtWidgets.QDoubleSpinBox()
        self.bulge.setRange(-32, 32)
        self.bulge.setDecimals(2)
        self.bulge.setSingleStep(0.1)
        self.bulge.setValue(0.5)
        self.bulge.setToolTip("Arc bulge (sagitta / chord/2, 0 = line); "
                              "Ellipse minor/major ratio; Rectangle turn; "
                              "Bezier handle length (0 = auto 1/3)")
        self.bulge.valueChanged.connect(panel._table_changed)
        lay.addWidget(self.bulge)

    def seg_type(self):
        return self.seg.currentText()

    def set_seg(self, text):
        try:
            idx = [self.seg.itemText(k) for k in range(self.seg.count())].index(text)
        except ValueError:
            return
        self.seg.blockSignals(True)
        self.seg.setCurrentIndex(idx)
        self.seg.blockSignals(False)

    def set_bulge(self, v):
        self.bulge.blockSignals(True)
        self.bulge.setValue(v)
        self.bulge.blockSignals(False)

    def values(self):
        return tuple(s.value() for s in self.spins)

    def set_values(self, p):
        for s, v in zip(self.spins, p):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def _del(self):
        self.panel._remove_row(self)


class Sketch3DPanel(object):
    """Author a genuinely 3D (non-planar-by-design) sketch/curve."""

    def __init__(self):
        # --- panel state: FIRST, before any widget exists ------------------ #
        # Widget population (combo.addItem etc.) emits signals immediately,
        # so every handler below must find valid attributes from the very
        # first signal. Previously this block lived mid-__init__ and every
        # panel open raised AttributeError from _on_plane_changed /
        # _update_status during setup.
        self._ready = False
        self._rows = []
        self._constraints = []   # list of ("distance", i, j, d)
        self._variables = {}
        # Issue #14: associative projection refs aligned 1:1 with rows
        # ('' = free point) + the plane they were projected onto.
        self._proj_refs = []
        self._proj_plane = None
        self._undo_stack = []
        self._undo_guard = False
        self._handoff_target = None
        self._handoff_obj = None
        self._constraint_creating = False
        self._constraint_first_idx = None
        self._constraint_preview_sep = None
        self._constraint_highlight_root = None
        self._constraint_drag_sep = None
        self._hover_constr = None
        self._drawing = False
        self._cb_click = None
        self._cb_move = None
        self._cb_key = None
        self._drag_on = False
        self._dragging = False
        self._drag_idx = None
        self._drag_anchor = None
        self._cb_drag_click = None
        self._cb_drag_move = None
        self._axis_lock = None
        self._ctrl = False
        self._plane_base = (0.0, 0.0, 0.0)
        self._plane_normal = (0.0, 0.0, 1.0)
        self._snap_pt = None
        self._snap_cands = []
        self._snap_shapes = []
        self._snap_feedback_root = None
        self._snap_shown_pt = None
        self._last_snap_px = None
        self._plane_grid_root = None
        self._plane_triad_root = None
        self._triad_axes = {}
        self._cb_triad_click = None
        self._cb_triad_move = None
        self._triad_dragging = False
        self._triad_drag_axis = None
        self._triad_drag_center = False
        self._triad_drag_start = None
        self._dim_preview_root = None
        self._dim_preview_sep = {}   # label -> coin separator for each constraint
        self._section_plane = None

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Railform -- 3D Sketch")
        lay = QtWidgets.QVBoxLayout(self.form)

        # --- viewport shortcut hint bar ----------------------------------- #
        self.shortcut_bar = _ShortcutHintBar()
        lay.addWidget(self.shortcut_bar)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Curve kind:"))
        self.kind = QtWidgets.QComboBox()
        for k in KINDS:
            self.kind.addItem(k)
        self.kind.currentIndexChanged.connect(self._update_status)
        row.addWidget(self.kind)
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Plane:"))
        self.plane_combo = QtWidgets.QComboBox()
        for name in ("Camera", "XY", "YZ", "ZX", "Auto"):
            self.plane_combo.addItem(name)
        self.plane_combo.currentIndexChanged.connect(self._on_plane_changed)
        row.addWidget(self.plane_combo)
        lay.addLayout(row)

        # --- construction-plane controls ------------------------------ #
        row = QtWidgets.QHBoxLayout()
        self.place_btn = QtWidgets.QPushButton("Place points: off")
        self.place_btn.setCheckable(True)
        self.place_btn.toggled.connect(self._toggle_placing)
        row.addWidget(self.place_btn)
        row.addWidget(QtWidgets.QLabel("Plane offset:"))
        self.offset = QtWidgets.QDoubleSpinBox()
        self.offset.setRange(-1e6, 1e6)
        self.offset.setDecimals(2)
        self.offset.setSingleStep(5.0)
        self.offset.setSuffix(" mm")
        self.offset.valueChanged.connect(self._on_plane_changed)
        row.addWidget(self.offset)
        lay.addLayout(row)

        self.snap_btn = QtWidgets.QPushButton("Snap: on")
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        self.snap_btn.toggled.connect(
            lambda on: self.snap_btn.setText("Snap: on" if on else "Snap: off"))
        lay.addWidget(self.snap_btn)

        # --- live drag-to-edit + axis lock -------------------------------- #
        drag_row = QtWidgets.QHBoxLayout()
        self.drag_btn = QtWidgets.QPushButton("Drag points: off")
        self.drag_btn.setCheckable(True)
        self.drag_btn.toggled.connect(self._toggle_drag_edit)
        drag_row.addWidget(self.drag_btn)
        self.axis_label = QtWidgets.QLabel("Axis: free (X/Y/Z to lock, Tab = plane)")
        drag_row.addWidget(self.axis_label)
        lay.addLayout(drag_row)

        self.extrude_chk = QtWidgets.QCheckBox(
            "Extrude closed loop (planar only) by")
        self.extrude_chk.setEnabled(False)
        lay.addWidget(self.extrude_chk)
        self.extrude_h = QtWidgets.QDoubleSpinBox()
        self.extrude_h.setRange(0, 1e6)
        self.extrude_h.setDecimals(2)
        self.extrude_h.setValue(10.0)
        self.extrude_h.setSuffix(" mm")
        lay.addWidget(self.extrude_h)

        # --- corner fillet/chamfer (advanced) ------------------------------- #
        self.adv_fillet = _CollapsibleGroup("Advanced: fillet / chamfer", collapsed=True)
        fillet_row = QtWidgets.QHBoxLayout()
        self.fillet_chk = QtWidgets.QCheckBox("Fillet corners (constant radius)")
        self.fillet_chk.setEnabled(False)  # only for closed loops
        fillet_row.addWidget(self.fillet_chk)
        self.fillet_r = QtWidgets.QDoubleSpinBox()
        self.fillet_r.setRange(0, 1e6)
        self.fillet_r.setDecimals(2)
        self.fillet_r.setValue(2.0)
        self.fillet_r.setSuffix(" mm")
        fillet_row.addWidget(self.fillet_r)
        self.adv_fillet.content_layout.addLayout(fillet_row)

        # --- corner chamfer controls -------------------------------------- #
        chamfer_row = QtWidgets.QHBoxLayout()
        self.chamfer_chk = QtWidgets.QCheckBox("Chamfer corners (setback)")
        self.chamfer_chk.setEnabled(False)  # only for closed loops
        chamfer_row.addWidget(self.chamfer_chk)
        self.chamfer_d = QtWidgets.QDoubleSpinBox()
        self.chamfer_d.setRange(0, 1e6)
        self.chamfer_d.setDecimals(2)
        self.chamfer_d.setValue(2.0)
        self.chamfer_d.setSuffix(" mm")
        chamfer_row.addWidget(self.chamfer_d)
        self.adv_fillet.content_layout.addLayout(chamfer_row)

        lay.addWidget(self.adv_fillet)

        # --- project model edges / surface handoff / templates / section ---- #
        tool_row = QtWidgets.QHBoxLayout()
        self.proj_along = QtWidgets.QComboBox()
        self.proj_along.addItems(["Along plane normal", "Along view direction"])
        self.proj_along.setToolTip("Projection direction for 'Project "
                                   "selected edges': straight down the "
                                   "construction-plane normal, or along the "
                                   "3D view direction captured when you "
                                   "click (stored per point, so rebuilds "
                                   "stay deterministic -- issue #14)")
        tool_row.addWidget(self.proj_along)
        project_btn = QtWidgets.QPushButton("Project selected edges")
        project_btn.setToolTip("Sample the selected model edges and drop "
                               "the projected points onto the construction "
                               "plane as new Line rows. Each point keeps an "
                               "associative reference to its source edge, so "
                               "the sketch re-projects and follows when the "
                               "source moves (issue #14); projected points "
                               "are driven -- dragging them is undone on the "
                               "next recompute")
        project_btn.clicked.connect(self._project_selection)
        tool_row.addWidget(project_btn)
        slice_btn = QtWidgets.QPushButton("Slice by sketch plane")
        slice_btn.setToolTip("Intersect the selected solids with the "
                             "construction plane and add the cross-section "
                             "edges as associative rows (plane section, "
                             "issue #14) -- the section follows the solid "
                             "on recompute")
        slice_btn.clicked.connect(self._slice_selection)
        tool_row.addWidget(slice_btn)
        sil_btn = QtWidgets.QPushButton("Silhouette")
        sil_btn.setToolTip("Add the selected solids' silhouette outline "
                           "(edges where adjacent faces disagree in "
                           "facing along the projection direction) as "
                           "associative rows (issue #14). Exact for "
                           "polyhedral solids; smooth surfaces only "
                           "contribute their edge-level approximation")
        sil_btn.clicked.connect(self._silhouette_selection)
        tool_row.addWidget(sil_btn)
        self.handoff_profile_btn = QtWidgets.QPushButton("Use as Loft profile")
        self.handoff_profile_btn.setToolTip("Accept the sketch, then open "
                                            "the Loft panel with this wire "
                                            "prefilled as a profile")
        self.handoff_profile_btn.clicked.connect(
            lambda: self._accept_with_handoff("profile"))
        tool_row.addWidget(self.handoff_profile_btn)
        self.handoff_rail_btn = QtWidgets.QPushButton("Use as Loft rail")
        self.handoff_rail_btn.setToolTip("Accept the sketch, then open "
                                         "the Loft panel with this wire "
                                         "prefilled as a guide rail")
        self.handoff_rail_btn.clicked.connect(
            lambda: self._accept_with_handoff("rail"))
        tool_row.addWidget(self.handoff_rail_btn)

        self.adv_tools = _CollapsibleGroup("Advanced: project / handoff / templates / section", collapsed=True)
        self.adv_tools.content_layout.addLayout(tool_row)

        # --- in-context relations (issues.md #7: pierce / on surface) ---- #
        rel_row = QtWidgets.QHBoxLayout()
        self.rel_point = QtWidgets.QSpinBox()
        self.rel_point.setPrefix("pt# ")
        self.rel_point.setRange(0, 999)
        self.rel_point.setToolTip("Point-table row that 'Pierce' / "
                                  "'On face' will attach to the selected "
                                  "geometry (issue #7)")
        rel_row.addWidget(self.rel_point)
        pierce_btn = QtWidgets.QPushButton("Pierce to selection")
        pierce_btn.setToolTip("Constrain the chosen point to where the "
                              "selected edge (or face) crosses the sketch "
                              "plane -- re-derived on every rebuild, so it "
                              "follows the source (issue #7)")
        pierce_btn.clicked.connect(lambda: self._apply_relation("pierce"))
        rel_row.addWidget(pierce_btn)
        onsurf_btn = QtWidgets.QPushButton("On face to selection")
        onsurf_btn.setToolTip("Constrain the chosen point to lie on the "
                              "selected face: it tracks the face's closest "
                              "point and follows the face when the source "
                              "body moves (issue #7)")
        onsurf_btn.clicked.connect(lambda: self._apply_relation("onsurf"))
        rel_row.addWidget(onsurf_btn)
        self.adv_tools.content_layout.addLayout(rel_row)

        # --- templates / break-apart/merge / section view ------------------- #
        tool_row2 = QtWidgets.QHBoxLayout()
        save_tpl = QtWidgets.QPushButton("Save template")
        save_tpl.setToolTip("Save current sketch (points, constraints, variables) "
                            "to a JSON template file")
        save_tpl.clicked.connect(self._template_save)
        tool_row2.addWidget(save_tpl)
        load_tpl = QtWidgets.QPushButton("Load template")
        load_tpl.setToolTip("Load sketch from a JSON template file (undoable)")
        load_tpl.clicked.connect(self._template_load)
        tool_row2.addWidget(load_tpl)
        break_btn = QtWidgets.QPushButton("Break apart")
        break_btn.setToolTip("Split sketch into separate wires per closed loop")
        break_btn.clicked.connect(self._break_apart)
        tool_row2.addWidget(break_btn)
        merge_btn = QtWidgets.QPushButton("Merge selected")
        merge_btn.setToolTip("Merge selected wires into this sketch")
        merge_btn.clicked.connect(self._merge_sketches)
        tool_row2.addWidget(merge_btn)
        self.section_btn = QtWidgets.QPushButton("Section view: off")
        self.section_btn.setCheckable(True)
        self.section_btn.setToolTip("Toggle a clipping plane at the construction plane")
        self.section_btn.toggled.connect(self._toggle_section_view)
        tool_row2.addWidget(self.section_btn)

        self.adv_tools.content_layout.addLayout(tool_row2)

        # --- Equations / Configurations (issues.md #10 / #15) --------------- #
        self.adv_eq = _CollapsibleGroup("Equations / Configurations", collapsed=True)
        # Design Table / Configurations
        cfg_row = QtWidgets.QHBoxLayout()
        cfg_row.addWidget(QtWidgets.QLabel("Configs:"))
        self.cfg_combo = QtWidgets.QComboBox()
        self.cfg_combo.setToolTip("Active configuration (design table row)")
        self.cfg_combo.currentIndexChanged.connect(self._on_config_changed)
        cfg_row.addWidget(self.cfg_combo)
        add_cfg = QtWidgets.QPushButton("+ config")
        add_cfg.clicked.connect(self._add_config)
        cfg_row.addWidget(add_cfg)
        del_cfg = QtWidgets.QPushButton("- config")
        del_cfg.clicked.connect(self._del_config)
        cfg_row.addWidget(del_cfg)
        self.adv_eq.content_layout.addLayout(cfg_row)

        # Suppression table
        sup_row = QtWidgets.QHBoxLayout()
        sup_row.addWidget(QtWidgets.QLabel("Suppress:"))
        self.sup_kind = QtWidgets.QComboBox()
        self.sup_kind.addItems(["constraint", "point", "handle"])
        self.sup_kind.setToolTip("Entity kind to suppress in current config")
        sup_row.addWidget(self.sup_kind)
        self.sup_idx = QtWidgets.QSpinBox()
        self.sup_idx.setRange(0, 999)
        self.sup_idx.setPrefix("idx ")
        self.sup_idx.setToolTip("Index of entity to suppress/unsuppress")
        sup_row.addWidget(self.sup_idx)
        sup_toggle = QtWidgets.QPushButton("Toggle suppress")
        sup_toggle.clicked.connect(self._toggle_suppression)
        sup_toggle.setToolTip("Toggle suppression of selected entity in active config")
        sup_row.addWidget(sup_toggle)
        self.adv_eq.content_layout.addLayout(sup_row)

        # Import/Export equations.txt
        io_row = QtWidgets.QHBoxLayout()
        export_btn = QtWidgets.QPushButton("Export equations.txt")
        export_btn.setToolTip("Export variables and configs to SolidWorks equations.txt")
        export_btn.clicked.connect(self._export_equations)
        io_row.addWidget(export_btn)
        import_btn = QtWidgets.QPushButton("Import equations.txt")
        import_btn.setToolTip("Import variables and configs from SolidWorks equations.txt")
        import_btn.clicked.connect(self._import_equations)
        io_row.addWidget(import_btn)
        self.adv_eq.content_layout.addLayout(io_row)

        self.adv_tools.content_layout.addWidget(self.adv_eq)

        lay.addWidget(self.adv_tools)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Points:"))
        add = QtWidgets.QPushButton("+ point")
        add.clicked.connect(lambda: self._add_row())
        row.addWidget(add)
        lay.addLayout(row)

        self.rows_lay = QtWidgets.QVBoxLayout()
        lay.addLayout(self.rows_lay)

        self.status = QtWidgets.QLabel("")
        lay.addWidget(self.status)

        # --- dimension-driven points ---------------------------------- #
        dim_row = QtWidgets.QHBoxLayout()
        dim_row.addWidget(QtWidgets.QLabel("Constraint:"))
        self.dim_type = QtWidgets.QComboBox()
        for t in ("distance", "coincide", "horizontal", "vertical",
                  "angle", "equal", "fix", "tangent", "concentric",
                  "symmetric", "mirror"):
            self.dim_type.addItem(t)
        self.dim_type.currentIndexChanged.connect(self._on_dim_type_changed)
        dim_row.addWidget(self.dim_type)
        dim_row.addWidget(QtWidgets.QLabel("P"))
        self.dim_i = QtWidgets.QSpinBox()
        self.dim_i.setRange(0, 999)
        dim_row.addWidget(self.dim_i)
        dim_row.addWidget(QtWidgets.QLabel("→ P"))
        self.dim_j = QtWidgets.QSpinBox()
        self.dim_j.setRange(0, 999)
        dim_row.addWidget(self.dim_j)
        dim_row.addWidget(QtWidgets.QLabel("→ P"))
        self.dim_k = QtWidgets.QSpinBox()
        self.dim_k.setRange(0, 999)
        dim_row.addWidget(self.dim_k)
        dim_row.addWidget(QtWidgets.QLabel("="))
        self.dim_len = QtWidgets.QDoubleSpinBox()
        self.dim_len.setRange(0, 1e6)
        self.dim_len.setDecimals(2)
        self.dim_len.setValue(10.0)
        dim_row.addWidget(self.dim_len)
        self.dim_fx = QtWidgets.QLineEdit()
        self.dim_fx.setPlaceholderText("=expr (P0x, width, sin..)")
        self.dim_fx.setToolTip("Driving-dimension expression, e.g. =P0x+25 or "
                               "=2*width -- overrides the spinbox above and "
                               "re-evaluates on every solve")
        dim_row.addWidget(self.dim_fx)
        add_dim = QtWidgets.QPushButton("Add dim")
        add_dim.clicked.connect(self._add_dim)
        dim_row.addWidget(add_dim)
        lay.addLayout(dim_row)

        # --- named variables for expressions (issues.md #10) -------------------- #
        var_row = QtWidgets.QHBoxLayout()
        var_row.addWidget(QtWidgets.QLabel("Vars:"))
        self.var_name = QtWidgets.QLineEdit()
        self.var_name.setPlaceholderText("name")
        self.var_name.setFixedWidth(80)
        var_row.addWidget(self.var_name)
        self.var_val = QtWidgets.QDoubleSpinBox()
        self.var_val.setRange(-1e6, 1e6)
        self.var_val.setDecimals(3)
        var_row.addWidget(self.var_val)
        self.var_unit = QtWidgets.QComboBox()
        self.var_unit.addItems(["mm", "cm", "m", "in", "deg", "rad", ""])
        self.var_unit.setCurrentText("mm")
        self.var_unit.setFixedWidth(55)
        self.var_unit.setToolTip("Variable unit (for documentation & equations.txt)")
        var_row.addWidget(self.var_unit)
        self.var_comment = QtWidgets.QLineEdit()
        self.var_comment.setPlaceholderText("comment")
        self.var_comment.setFixedWidth(100)
        self.var_comment.setToolTip("Optional comment (exported to equations.txt)")
        var_row.addWidget(self.var_comment)
        add_var = QtWidgets.QPushButton("+ var")
        add_var.clicked.connect(self._add_var)
        var_row.addWidget(add_var)
        self.var_list = QtWidgets.QListWidget()
        self.var_list.setMaximumHeight(56)
        self.var_list.setToolTip("Named variables usable in =expressions")
        var_row.addWidget(self.var_list)
        clear_vars = QtWidgets.QPushButton("Clear")
        clear_vars.clicked.connect(self._clear_vars)
        var_row.addWidget(clear_vars)
        lay.addLayout(var_row)

        # --- mirror about a principal plane (advanced) -------------------- #
        self.adv_mirror = _CollapsibleGroup("Advanced: mirror", collapsed=True)
        mirror_row = QtWidgets.QHBoxLayout()
        mirror_row.addWidget(QtWidgets.QLabel("Mirror:"))
        self.mirror_plane = QtWidgets.QComboBox()
        for name in ("XY", "YZ", "ZX"):
            self.mirror_plane.addItem(name)
        self.mirror_plane.setCurrentIndex(
            self.mirror_plane.findText("YZ"))
        mirror_row.addWidget(self.mirror_plane)
        self.mirror_live = QtWidgets.QCheckBox("Live")
        self.mirror_live.setToolTip("Auto-append the mirrored point for "
                                    "every placed point")
        mirror_row.addWidget(self.mirror_live)
        mirror_now = QtWidgets.QPushButton("Mirror now")
        mirror_now.setToolTip("Append mirrored copies of all current "
                              "points with mirror constraints")
        mirror_now.clicked.connect(self._mirror_now)
        mirror_row.addWidget(mirror_now)
        self.adv_mirror.content_layout.addLayout(mirror_row)
        lay.addWidget(self.adv_mirror)

        # --- on-canvas constraint creation (advanced) --------------------- #
        self.adv_constraint = _CollapsibleGroup("Advanced: on-canvas constraints", collapsed=True)
        canvas_row = QtWidgets.QHBoxLayout()
        self.constraint_btn = QtWidgets.QPushButton("Add Constraint: off")
        self.constraint_btn.setCheckable(True)
        self.constraint_btn.toggled.connect(self._toggle_constraint_mode)
        canvas_row.addWidget(self.constraint_btn)
        self.constraint_type = QtWidgets.QComboBox()
        for t in ("distance", "coincide", "horizontal", "vertical", "angle"):
            self.constraint_type.addItem(t)
        self.constraint_type.setEnabled(False)
        canvas_row.addWidget(self.constraint_type)
        self.adv_constraint.content_layout.addLayout(canvas_row)
        lay.addWidget(self.adv_constraint)

        self.dim_list = QtWidgets.QListWidget()
        self.dim_list.itemClicked.connect(self._on_dim_list_clicked)
        lay.addWidget(self.dim_list)
        solve_row = QtWidgets.QHBoxLayout()
        solve = QtWidgets.QPushButton("Solve dims")
        solve.clicked.connect(self._solve_dims)
        solve_row.addWidget(solve)
        self.undo_btn = QtWidgets.QPushButton("Undo")
        self.undo_btn.setEnabled(False)
        self.undo_btn.setToolTip("Undo last point / constraint / solve (Ctrl+Z)")
        self.undo_btn.clicked.connect(lambda: self._undo())
        solve_row.addWidget(self.undo_btn)
        clear_dims = QtWidgets.QPushButton("Clear dims")

        def _clear_dims():
            if self._constraints:
                self._push_undo()
            self._constraints = []
            self.dim_list.clear()
            self._update_status()
            if self.dim_preview_btn.isChecked():
                self._build_dim_preview()

        clear_dims.clicked.connect(_clear_dims)
        solve_row.addWidget(clear_dims)
        lay.addLayout(solve_row)

        # --- dimension preview in 3D view --------------------------------- #
        preview_row = QtWidgets.QHBoxLayout()
        self.dim_preview_btn = QtWidgets.QPushButton("Show dims in 3D: off")
        self.dim_preview_btn.setCheckable(True)
        self.dim_preview_btn.toggled.connect(self._toggle_dim_preview)
        preview_row.addWidget(self.dim_preview_btn)
        lay.addLayout(preview_row)

        self._ready = True
        # --- Equation Manager (issues.md #10 / #15) -------------------- #
        from rfstudio import panel_logic
        self._eq_manager = panel_logic.get_equation_manager(
            FreeCAD.ActiveDocument or FreeCAD.newDocument())
        # Sync legacy _variables dict with EquationManager for backward compat
        self._variables = self._eq_manager.get_all_variables()
        self._update_status()

    # --- named variables (EquationManager-backed) --------------------- #
    def _add_var(self):
        name = self.var_name.text().strip()
        if not name or not (name[0].isalpha() or name[0] == "_") or \
                not all(ch.isalnum() or ch == "_" for ch in name):
            self.status.setText("Variable name must be identifier-like")
            return
        self._push_undo()
        unit = self.var_unit.text().strip() if hasattr(self, "var_unit") else "mm"
        comment = self.var_comment.text().strip() if hasattr(self, "var_comment") else ""
        self._eq_manager.add_variable(name, self.var_val.value(), unit, comment)
        self._variables = self._eq_manager.get_all_variables()
        self._refresh_var_list()
        self.var_name.clear()

    def _clear_vars(self):
        if self._eq_manager.variables:
            self._push_undo()
        self._eq_manager.variables.clear()
        self._variables = {}
        self._refresh_var_list()

    def _refresh_var_list(self):
        try:
            self.var_list.clear()
            for name, meta in sorted(self._eq_manager.variables.items()):
                val = meta["value"]
                unit = meta.get("unit", "mm")
                comment = meta.get("comment", "")
                label = f"{name} = {val:.3f} {unit}"
                if comment:
                    label += f"  # {comment}"
                self.var_list.addItem(label)
        except Exception:
            pass

    # --- Equation Manager / Configurations (issues.md #10 / #15) -------- #
    def _refresh_cfg_combo(self):
        """Refresh the configuration combo box from EquationManager."""
        try:
            self.cfg_combo.blockSignals(True)
            self.cfg_combo.clear()
            for i, cfg in enumerate(self._eq_manager.configs):
                self.cfg_combo.addItem(cfg["name"])
            if self._eq_manager.configs:
                self.cfg_combo.setCurrentIndex(self._eq_manager.active_config)
            self.cfg_combo.blockSignals(False)
        except Exception:
            pass

    def _on_config_changed(self, idx):
        """Switch active configuration."""
        if self._eq_manager.set_active_config(idx):
            self._variables = self._eq_manager.get_all_variables()
            self._refresh_var_list()
            self._update_status()
            self.status.setText(f"Switched to config: {self._eq_manager.configs[idx]['name']}")

    def _add_config(self):
        """Add a new configuration."""
        from PySide import QtWidgets
        name, ok = QtWidgets.QInputDialog.getText(
            self.widget, "New Configuration", "Configuration name:")
        if ok and name.strip():
            self._push_undo()
            self._eq_manager.add_config(name.strip())
            self._refresh_cfg_combo()
            self.cfg_combo.setCurrentIndex(len(self._eq_manager.configs) - 1)
            self._on_config_changed(len(self._eq_manager.configs) - 1)

    def _del_config(self):
        """Delete the active configuration."""
        if len(self._eq_manager.configs) <= 1:
            self.status.setText("Cannot delete last configuration")
            return
        idx = self._eq_manager.active_config
        name = self._eq_manager.configs[idx]["name"]
        self._push_undo()
        self._eq_manager.remove_config(idx)
        self._refresh_cfg_combo()
        self.status.setText(f"Deleted config: {name}")

    def _toggle_suppression(self):
        """Toggle suppression of an entity in the active config."""
        kind = self.sup_kind.currentText()
        idx = self.sup_idx.value()
        cfg = self._eq_manager.get_active_config()
        if not cfg:
            return
        key = f"suppressed_{kind}s"
        suppressed = cfg.get(key, [])
        if idx in suppressed:
            suppressed.remove(idx)
            action = "unsuppressed"
        else:
            suppressed.append(idx)
            action = "suppressed"
        cfg[key] = suppressed
        self._push_undo()
        self.status.setText(f"{kind.capitalize()} {idx} {action} in config '{cfg['name']}'")

    def _export_equations(self):
        """Export variables and configs to SolidWorks equations.txt."""
        from PySide import QtWidgets
        txt = self._eq_manager.export_equations_txt()
        fname, _ = QtWidgets.QFileDialog.getSaveFileName(
            self.widget, "Export equations.txt", "equations.txt", "Text files (*.txt)")
        if fname:
            try:
                with open(fname, "w") as f:
                    f.write(txt)
                self.status.setText(f"Exported equations.txt to {fname}")
            except Exception as exc:
                self.status.setText(f"Export failed: {exc}")

    def _import_equations(self):
        """Import variables and configs from SolidWorks equations.txt."""
        from PySide import QtWidgets
        fname, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.widget, "Import equations.txt", "", "Text files (*.txt)")
        if fname:
            try:
                with open(fname, "r") as f:
                    txt = f.read()
                ok, errors = self._eq_manager.import_equations_txt(txt)
                if ok:
                    self._variables = self._eq_manager.get_all_variables()
                    self._refresh_var_list()
                    self._refresh_cfg_combo()
                    self.status.setText(f"Imported equations.txt from {fname}")
                else:
                    self.status.setText(f"Import errors: {'; '.join(errors[:3])}")
            except Exception as exc:
                self.status.setText(f"Import failed: {exc}")

    def _on_dim_list_clicked(self, item):
        """Highlight the constraint's points in the viewport when clicked in list."""
        try:
            row = self.dim_list.row(item)
            if 0 <= row < len(self._constraints):
                constr = self._constraints[row]
                kind = constr[0]
                pts = self._points()
                if kind == "distance":
                    i, j = constr[1], constr[2]
                    self._highlight_constraint_points(pts[i], pts[j])
                elif kind in ("coincide", "concentric"):
                    i, j = constr[1], constr[2]
                    self._highlight_constraint_points(pts[i], pts[j])
                elif kind in ("horizontal", "vertical", "fix"):
                    i = constr[1]
                    self._highlight_constraint_points(pts[i], None)
        except Exception:
            pass

    def _highlight_constraint_points(self, p0, p1):
        """Temporarily highlight the constraint's points in the viewport."""
        try:
            from pivy import coin
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            # Remove previous highlight
            self._remove_constraint_highlight()
            root = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (1.0, 1.0, 0.0)  # yellow highlight
            root.addChild(col)
            coords = coin.SoCoordinate3()
            points = coin.SoPointSet()
            verts = [p0]
            if p1 is not None:
                verts.append(p1)
            coords.point.setValues(0, len(verts), verts)
            root.addChild(coords)
            root.addChild(points)
            sg.addChild(root)
            self._constraint_highlight_root = root
        except Exception:
            pass

    def _remove_constraint_highlight(self):
        if getattr(self, '_constraint_highlight_root', None) is not None:
            try:
                view = FreeCADGui.ActiveDocument.ActiveView
                self._safe_remove_child(view, self._constraint_highlight_root)
            except Exception:
                pass
            self._constraint_highlight_root = None
        self._constraint_drag_sep = None

    # ------------------------------------------------------------------ #
    # point table
    def _add_row(self, p=(0.0, 0.0, 0.0)):
        if not getattr(self, "_undo_guard", False):
            self._push_undo()
        r = _PointRow(self, *p)
        self._rows.append(r)
        if len(self._proj_refs) < len(self._rows):
            self._proj_refs.append("")   # new rows start free (issue #14)
        self.rows_lay.addWidget(r.widget)
        # Live mirror: auto-append the mirrored counterpart (but never
        # during undo restore, which rebuilds rows under _undo_guard).
        if not getattr(self, "_undo_guard", False) and \
                getattr(self, "mirror_live", None) is not None and \
                self.mirror_live.isChecked():
            self._mirror_point(len(self._rows) - 1)
        self._update_status()

    def _mirror_point(self, idx):
        """Append the mirror of row idx (no undo push -- the caller owns
        the single push for the whole gesture). Returns the new index or
        None. Skips indices that already have a mirror pair."""
        if any(c[0] == "mirror" and (c[1] == idx or c[2] == idx)
               for c in self._constraints):
            return None
        params = panel_logic.mirror_plane_params(
            self.mirror_plane.currentText())
        if params is None:
            return None
        try:
            p = self._rows[idx].values()
        except IndexError:
            return None
        q = panel_logic.reflect_point(p, params[:3], params[3])
        if q is None:
            return None
        r = _PointRow(self, *q)
        self._rows.append(r)
        if len(self._proj_refs) < len(self._rows):
            self._proj_refs.append("")   # mirrored point is free (issue #14)
        self.rows_lay.addWidget(r.widget)
        j = len(self._rows) - 1
        c = ("mirror", idx, j) + params
        self._constraints.append(c)
        self.dim_list.addItem("mirror P%d<->P%d (%s)"
                              % (idx, j, self.mirror_plane.currentText()))
        return j

    def _mirror_now(self):
        """Mirror every currently-unpaired point, one undo step."""
        if not self._rows:
            self.status.setText("Mirror: no points yet")
            return
        self._push_undo()
        self._undo_guard = True
        try:
            for i in range(len(self._rows)):
                # rows list grows as we append; only mirror originals
                if i >= len(self._rows):
                    break
                self._mirror_point(i)
        finally:
            self._undo_guard = False
        self._update_status()
        self._refresh_dim_tooltips()
        if self.dim_preview_btn.isChecked():
            self._build_dim_preview()

    def _sync_mirror_partners(self, idx):
        """During a live drag, move idx's mirror partners to the exact
        reflection so the pair tracks the cursor (the persisted mirror
        constraint re-enforces it on solve/accept either way)."""
        try:
            p = self._rows[idx].values()
        except IndexError:
            return
        for c in self._constraints:
            if c[0] != "mirror":
                continue
            other = c[2] if c[1] == idx else c[1] if c[2] == idx else None
            if other is None or not (0 <= other < len(self._rows)):
                continue
            q = panel_logic.reflect_point(p, c[3:6], c[6])
            if q is None:
                continue
            try:
                self._rows[other].set_values(q)
            except IndexError:
                pass

    def _remove_row(self, r):
        if r in self._rows:
            self._push_undo()
            idx = self._rows.index(r)
            self._rows.remove(r)
            if idx < len(self._proj_refs):
                del self._proj_refs[idx]   # keep refs 1:1 (issue #14)
            self.rows_lay.removeWidget(r.widget)
            r.widget.setParent(None)
            r.widget.deleteLater()
            self._update_status()

    def _points(self):
        return [r.values() for r in self._rows]

    # --- undo (snapshot-based, Ctrl+Z / Undo button) ---------------------- #
    def _snapshot(self):
        return {
            "points": [r.values() for r in self._rows],
            "segs": [r.seg_type() for r in self._rows],
            "bulges": [r.bulge.value() for r in self._rows],
            "constraints": [tuple(c) for c in self._constraints],
            "labels": [self.dim_list.item(i).text()
                       for i in range(self.dim_list.count())],
            "variables": dict(getattr(self, "_variables", {})),
            "refs": list(getattr(self, "_proj_refs", [])),
        }

    def _push_undo(self):
        try:
            stack = self._undo_stack
        except AttributeError:
            stack = self._undo_stack = []
        stack.append(self._snapshot())
        if len(stack) > 60:
            del stack[:-60]
        try:
            self.undo_btn.setEnabled(True)
        except Exception:
            pass

    def _apply_snapshot(self, s):
        self._undo_guard = True
        try:
            for r in list(self._rows):
                self.rows_lay.removeWidget(r.widget)
                r.widget.setParent(None)
                r.widget.deleteLater()
            self._rows = []
            for p, seg, b in zip(s["points"], s["segs"], s["bulges"]):
                r = _PointRow(self, *p)
                r.set_seg(seg)
                r.set_bulge(b)
                self._rows.append(r)
                self.rows_lay.addWidget(r.widget)
            self._constraints = [tuple(c) for c in s["constraints"]]
            self.dim_list.clear()
            for label in s["labels"]:
                self.dim_list.addItem(label)
            self._variables = dict(s.get("variables", {}))
            self._refresh_var_list()
            # restore projection refs aligned to the rebuilt rows
            refs = list(s.get("refs", []))
            refs = (refs + [""] * len(self._rows))[:len(self._rows)]
            self._proj_refs = refs
        finally:
            self._undo_guard = False
        self._update_status()
        try:
            if self.dim_preview_btn.isChecked():
                self._build_dim_preview()
            if not self._undo_stack:
                self.undo_btn.setEnabled(False)
        except Exception:
            pass

    def _undo(self):
        try:
            stack = self._undo_stack
        except AttributeError:
            return
        if not stack:
            return
        self._apply_snapshot(stack.pop())
        try:
            self.status.setText("Undone (%d left)" % len(stack))
        except Exception:
            pass

    def _table_changed(self):
        if not getattr(self, "_ready", False):
            return
        self._update_status()
        self._refresh_dim_tooltips()
        if self.dim_preview_btn.isChecked():
            self._build_dim_preview()

    def _refresh_dim_tooltips(self):
        """Keep list-item tooltips live (measured values go stale after
        every point edit and solve)."""
        try:
            pts = self._points()
            for i, c in enumerate(self._constraints):
                item = self.dim_list.item(i)
                if item is not None:
                    item.setToolTip(panel_logic.constraint_tooltip(c, pts))
        except Exception:
            pass

    def _hover_constraint(self, pos):
        """(idx, constr, anchor) of the constraint whose badge sits
        closest to the cursor (within 14 px), else None."""
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
        except Exception:
            return None
        if view is None or not self._constraints:
            return None
        pts = self._points()
        best, best_d2 = None, 14.0 * 14.0
        for idx, c in enumerate(self._constraints):
            a = panel_logic.constraint_anchor(c, pts)
            if a is None:
                continue
            try:
                sp = snaputil.screen_project(view, FreeCAD.Vector(*a))
            except Exception:
                continue
            if not sp:
                continue
            try:
                dx, dy = sp[0] - pos[0], sp[1] - pos[1]
            except Exception:
                continue
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best, best_d2 = (idx, c, a), d2
        return best

    def _edit_hovered_constraint(self):
        """Load the hovered constraint into the editors and remove the
        original: adjust values, press Add dim, re-solve."""
        hov = getattr(self, "_hover_constr", None)
        if not hov:
            return
        idx, c, _a = hov
        if not (0 <= idx < len(self._constraints)):
            return
        try:
            kind = c[0]
            self.dim_type.setCurrentIndex(
                self.dim_type.findText(kind))
            if kind == "distance":
                self.dim_i.setValue(c[1]); self.dim_j.setValue(c[2])
                self.dim_len.setValue(c[3])
            elif kind in ("coincide", "concentric"):
                self.dim_i.setValue(c[1]); self.dim_j.setValue(c[2])
            elif kind in ("horizontal", "vertical"):
                self.dim_i.setValue(c[1]); self.dim_len.setValue(c[2])
            elif kind == "angle":
                self.dim_i.setValue(c[1]); self.dim_j.setValue(c[2])
                self.dim_k.setValue(c[3]); self.dim_len.setValue(c[4])
            elif kind == "equal":
                self.dim_i.setValue(c[1]); self.dim_j.setValue(c[2])
                self.dim_k.setValue(c[4])
            elif kind == "fix":
                self.dim_i.setValue(c[1])
            elif kind in ("tangent", "symmetric"):
                self.dim_i.setValue(c[1]); self.dim_j.setValue(c[2])
                self.dim_k.setValue(c[3])
            elif kind == "mirror":
                self.dim_i.setValue(c[1]); self.dim_j.setValue(c[2])
            self._push_undo()
            del self._constraints[idx]
            self.dim_list.takeItem(idx)
            self._hover_constr = None
            self._update_status()
            self._refresh_dim_tooltips()
            if self.dim_preview_btn.isChecked():
                self._build_dim_preview()
            self.status.setText(
                "%s loaded into editors -- adjust and press Add dim" % kind)
        except Exception:
            pass

    def _solver_diag(self, pts):
        """issues.md #8 (per-entity diagnostics), low-risk slice: run
        the solver on the CURRENT points/constraints and return its
        verdict (well/under/over-defined with DOF counts) for the
        status line. Constraints go through the same expression-
        resolution pass execute() uses, so variable-based dimensions
        read correctly. Any failure degrades to no suffix, never to a
        broken status line; skipped entirely when there are no dims,
        so the common no-constraints path costs nothing."""
        if not self._constraints or len(pts) < 2:
            return ""
        try:
            vecs = []
            for p in pts:
                try:
                    vecs.append(FreeCAD.Vector(p.x, p.y, p.z))
                except AttributeError:
                    vecs.append(FreeCAD.Vector(p[0], p[1], p[2]))
            constraints = panel_logic.resolve_constraint_expressions(
                self._constraints, getattr(self, "_variables", {}) or {},
                [(v.x, v.y, v.z) for v in vecs])
            pinned = {c[1] for c in constraints if c[0] == "fix"}
            from rfstudio import core
            status = {}
            core.solve_point_constraints(
                vecs, constraints, pinned=pinned, status=status)
            text = panel_logic.solver_status_text(status)
            return ("  | " + text) if text else ""
        except Exception:
            return ""

    def _apply_constraint_badges(self, pts):
        """issues.md #8 (per-constraint badges): paint every constraint
        currently violated at the REAL (un-solved) point positions red
        in the dim list, and tint the point-table rows of the points it
        touches -- the 'which constraint is fighting me' answer, live
        while dragging (recompute re-solves and clears the badges).
        Returns the violated count so the status line can say how many.
        Any failure degrades to no coloring, never to a broken panel;
        when everything is satisfied the previous tinting is cleared."""
        badges = []
        violated_pts = set()
        try:
            if self._constraints:
                from rfstudio import core
                vecs = []
                for p in pts:
                    try:
                        vecs.append(FreeCAD.Vector(p.x, p.y, p.z))
                    except AttributeError:
                        vecs.append(FreeCAD.Vector(p[0], p[1], p[2]))
                constraints = panel_logic.resolve_constraint_expressions(
                    self._constraints, getattr(self, "_variables", {}) or {},
                    [(v.x, v.y, v.z) for v in vecs])
                viol = core.constraint_violations(vecs, constraints)
                badges = panel_logic.constraint_badges(viol)
                for i, bad in enumerate(badges):
                    if not bad or i >= len(constraints):
                        continue
                    for j in core.constraint_point_indices(constraints[i]):
                        if 0 <= j < len(self._rows):
                            violated_pts.add(j)
            for i in range(self.dim_list.count()):
                item = self.dim_list.item(i)
                if item is None:
                    continue
                bad = i < len(badges) and badges[i]
                item.setBackground(QtGui.QBrush(
                    QtGui.QColor(250, 195, 195) if bad
                    else QtGui.QColor(0, 0, 0, 0)))
            for j, r in enumerate(self._rows):
                r.widget.setStyleSheet(
                    "background:#fadadd;" if j in violated_pts else "")
        except Exception:
            return 0
        return sum(1 for b in badges if b)

    def _update_status(self):
        if not getattr(self, "_ready", False):
            return
        ok, msg = panel_logic.sketch3d_readiness(self.kind.currentText(),
                                                 len(self._rows))
        pts = self._points()
        if ok and not panel_logic.points_are_planar(pts):
            msg += "  (non-planar -- a genuinely 3D curve)"
        closed = panel_logic.points_form_closed_loop(pts)
        loops = panel_logic.find_closed_loops(pts)
        self.extrude_chk.setEnabled(closed)
        self.fillet_chk.setEnabled(closed)
        self.chamfer_chk.setEnabled(closed)
        if closed:
            msg += "  | closed loop -- extrude/fillet available"
        if len(loops) > 1:
            msg += "  | %d closed loops detected" % len(loops)
        msg += self._solver_diag(pts)
        n_bad = self._apply_constraint_badges(pts)
        if n_bad:
            msg += "  | %d off-target (red)" % n_bad
        self.status.setText(msg)
        try:
            for b in (self.handoff_profile_btn, self.handoff_rail_btn):
                b.setEnabled(ok)
        except Exception:
            pass

    # --- dimension-driven sketch -------------------------------------- #
    def _add_dim(self):
        kind = self.dim_type.currentText()
        i, j, k = self.dim_i.value(), self.dim_j.value(), self.dim_k.value()
        v = self.dim_len.value()
        fx = self.dim_fx.text().strip()
        n = len(self._rows)
        def _oob(idx):
            return idx < 0 or idx >= n
        def _target():
            """Spinbox value, or the validated fx expression string."""
            if not fx:
                return v
            if "," in fx:
                self.status.setText("Bad expression: commas break the "
                                    "stored format -- use nested calls")
                return None
            try:
                panel_logic.evaluate_expression(
                    fx, panel_logic.expression_namespace(
                        self._variables, self._points()))
            except ValueError as exc:
                self.status.setText("Bad expression: %s" % exc)
                return None
            return fx if fx.startswith("=") else "=" + fx
        if kind == "distance":
            if i == j or _oob(i) or _oob(j):
                return self._bad()
            t = _target()
            if t is None:
                return
            c = ("distance", i, j, t)
            label = "P%d -> P%d = %s" % (i, j, t if isinstance(t, str)
                                         else "%.2f" % t)
        elif kind == "coincide":
            if i == j or _oob(i) or _oob(j):
                return self._bad()
            c = ("coincide", i, j); label = "P%d == P%d" % (i, j)
        elif kind in ("horizontal", "vertical"):
            if _oob(i):
                return self._bad()
            t = _target()
            if t is None:
                return
            c = (kind, i, t)
            label = "%s P%d = %s" % (kind, i, t if isinstance(t, str)
                                     else "%.2f" % t)
        elif kind == "equal":
            if i == j or j == k or _oob(i) or _oob(j) or _oob(k):
                return self._bad()
            c = ("equal", i, j, j, k)   # |Pi-Pj| == |Pj-Pk|
            label = "|P%d-P%d| == |P%d-P%d|" % (i, j, j, k)
        elif kind == "fix":
            if _oob(i):
                return self._bad()
            c = ("fix", i); label = "fix P%d" % i
        elif kind == "tangent":
            if len({i, j, k}) < 3 or _oob(i) or _oob(j) or _oob(k):
                return self._bad()
            c = ("tangent", i, j, k); label = "tangent P%d-P%d-P%d" % (i, j, k)
        elif kind == "concentric":
            if i == j or _oob(i) or _oob(j):
                return self._bad()
            c = ("concentric", i, j); label = "concentric P%d==P%d" % (i, j)
        elif kind == "symmetric":
            if len({i, j, k}) < 3 or _oob(i) or _oob(j) or _oob(k):
                return self._bad()
            c = ("symmetric", i, j, k); label = "symmetric P%d,P%d about P%d" % (i, j, k)
        elif kind == "mirror":
            if i == j or _oob(i) or _oob(j):
                return self._bad()
            params = panel_logic.mirror_plane_params(
                self.mirror_plane.currentText())
            if params is None:
                return self._bad()
            c = ("mirror", i, j) + params
            label = "mirror P%d<->P%d (%s)" % (
                i, j, self.mirror_plane.currentText())
        else:  # angle
            if len({i, j, k}) < 3 or _oob(i) or _oob(j) or _oob(k):
                return self._bad()
            t = _target()
            if t is None:
                return
            c = ("angle", i, j, k, t)
            label = "angle(P%d,P%d,P%d) = %s" % (
                i, j, k, t if isinstance(t, str) else "%.1f deg" % t)
        self._push_undo()
        self._constraints.append(c)
        self.dim_list.addItem(label)
        try:
            item = self.dim_list.item(self.dim_list.count() - 1)
            if item is not None:
                item.setToolTip(panel_logic.constraint_tooltip(c, self._points()))
        except Exception:
            pass
        self._update_status()
        if self.dim_preview_btn.isChecked():
            self._build_dim_preview()

    def _on_dim_type_changed(self, *_args):
        """Update UI when constraint type changes in the manual entry row."""
        kind = self.dim_type.currentText()
        # Enable/disable spinboxes based on constraint type
        need_i = kind in ("distance", "coincide", "horizontal", "vertical", "angle", "equal", "fix", "tangent", "concentric", "symmetric", "mirror")
        need_j = kind in ("distance", "coincide", "angle", "equal", "tangent", "concentric", "symmetric", "mirror")
        need_k = kind in ("angle", "equal", "tangent", "concentric", "symmetric")
        need_len = kind in ("distance", "horizontal", "vertical", "angle")
        self.dim_i.setEnabled(need_i)
        self.dim_j.setEnabled(need_j)
        self.dim_k.setEnabled(need_k)
        self.dim_len.setEnabled(need_len)

    def _bad(self):
        _panel_warning(self, "Railform 3D sketch: constraint point indices out of range")

    def _solve_dims(self):
        """Adjust the movable points so the dims hold -- the
        'dimensions drive the sketch' step. Reuses
        core.solve_point_constraints' damped Gauss-Newton solve."""
        from rfstudio import core
        pts = self._points()
        if len(pts) < 2:
            self.status.setText("Need >= 2 points first.")
            return
        try:
            status = {}
            self._push_undo()
            numeric = panel_logic.resolve_constraint_expressions(
                self._constraints, self._variables, pts)
            solved = core.solve_point_constraints(
                [FreeCAD.Vector(*p) for p in pts], numeric,
                status=status)
            for row, p in zip(self._rows, solved):
                row.set_values((p.x, p.y, p.z))
            state = status.get("state", "?")
            self.status.setText(
                "Dims solved: state=%s, rms=%.2e, %d constraint(s)"
                % (state, status.get("residual", -1.0),
                   len(self._constraints)))
            if self.dim_preview_btn.isChecked():
                self._build_dim_preview()
        except Exception as exc:
            self.status.setText("Dim solve failed: %s" % exc)

    # ------------------------------------------------------------------ #
    # placement mode
    def _plane_geometry(self):
        """(origin, normal, u, v) of the current construction plane."""
        origin, normal = panel_logic.construction_plane(
            self._plane_base, self._plane_normal,
            offset=self.offset.value())
        u, v = panel_logic.plane_basis(normal)
        return origin, normal, u, v

    def _setup_plane_grid(self):
        """Draw the construction plane as a faint reference grid in the
        viewport -- the visual answer to 'where will my clicks land?'."""
        self._remove_plane_grid()
        try:
            from pivy import coin
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            origin, normal, u, v = self._plane_geometry()
            size, minor, major = 200.0, 5.0, 25.0
            half = size / 2.0

            def _grid_at(step, rgb):
                sep = coin.SoSeparator()
                collector = sep  # simple separator with color+lines
                col = coin.SoBaseColor()
                col.rgb = rgb
                sep.addChild(col)
                coords = coin.SoCoordinate3()
                lines = coin.SoLineSet()
                verts, counts = [], []
                n = int(round(size / step))
                for i in range(n + 1):
                    c = -half + i * step
                    for a, b in (((-half, c), (half, c)),
                                 ((c, -half), (c, half))):
                        verts.append((origin[0] + u[0] * a[0] + v[0] * a[1],
                                      origin[1] + u[1] * a[0] + v[1] * a[1],
                                      origin[2] + u[2] * a[0] + v[2] * a[1]))
                        verts.append((origin[0] + u[0] * b[0] + v[0] * b[1],
                                      origin[1] + u[1] * b[0] + v[1] * b[1],
                                      origin[2] + u[2] * b[0] + v[2] * b[1]))
                        counts.append(2)
                coords.point.setValues(0, len(verts), verts)
                lines.numVertices.setValues(0, len(counts), counts)
                sep.addChild(coords)
                sep.addChild(lines)
                return sep

            root = coin.SoSeparator()
            root.addChild(_grid_at(minor, (0.28, 0.28, 0.28)))
            root.addChild(_grid_at(major, (0.45, 0.45, 0.45)))
            sg.addChild(root)
            self._plane_grid_root = root
        except Exception as exc:
            _panel_warning(self, "Railform 3D sketch: plane grid unavailable (%s)" % exc)
            self._plane_grid_root = None

    def _remove_plane_grid(self):
        if self._plane_grid_root is None:
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            self._safe_remove_child(view, self._plane_grid_root)
        except Exception:
            pass
        self._plane_grid_root = None

    # --- dimension preview in 3D view ----------------------------------- #
    def _toggle_dim_preview(self, on):
        self.dim_preview_btn.setText(
            "Show dims in 3D: on" if on else "Show dims in 3D: off")
        if on:
            self._build_dim_preview()
        else:
            self._remove_dim_preview()

    def _build_dim_preview(self):
        """Build Coin3D overlay for all active constraints."""
        self._remove_dim_preview()
        try:
            from pivy import coin
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            root = coin.SoSeparator()
            pts = self._points()
            for label, constr in zip(
                    [self.dim_list.item(i).text()
                     for i in range(self.dim_list.count())],
                    self._constraints):
                sep = self._coin_for_constraint(constr, pts)
                if sep is not None:
                    root.addChild(sep)
                    self._dim_preview_sep[label] = sep
            if root.getNumChildren() > 0:
                sg.addChild(root)
                self._dim_preview_root = root
        except Exception as exc:
            _panel_warning(self, "Railform 3D sketch: dim preview unavailable (%s)" % exc)

    def _coin_for_constraint(self, constr, pts):
        """Create Coin3D geometry for one constraint."""
        from pivy import coin
        kind = constr[0]
        # Colors: blue for dims, green for relations, orange for fixes
        if kind in ("distance", "horizontal", "vertical", "angle", "equal"):
            col = (0.20, 0.45, 1.00)   # blue
        elif kind in ("coincide", "tangent", "concentric", "symmetric",
                      "mirror"):
            col = (0.20, 0.85, 0.20)   # green
        else:
            col = (1.00, 0.65, 0.00)   # orange
        sep = coin.SoSeparator()
        col_node = coin.SoBaseColor()
        col_node.rgb = col
        sep.addChild(col_node)
        coords = coin.SoCoordinate3()
        lines = coin.SoLineSet()
        verts, counts = [], []
        try:
            if kind == "distance":
                i, j = constr[1], constr[2]
                a, b = pts[i], pts[j]
                verts.extend([a, b])
                counts.append(2)
                # Arrowheads at both ends (simple cone markers)
                for p in (a, b):
                    self._add_arrow_head(sep, p, (b[0]-a[0], b[1]-a[1], b[2]-a[2]), col)
            elif kind in ("coincide", "concentric"):
                i, j = constr[1], constr[2]
                a, b = pts[i], pts[j]
                verts.extend([a, b])
                counts.append(2)
            elif kind in ("horizontal", "vertical"):
                i, val = constr[1], constr[2]
                a = pts[i]
                if kind == "horizontal":
                    p1 = (a[0] - 10, val, a[2])
                    p2 = (a[0] + 10, val, a[2])
                else:
                    p1 = (val, a[1] - 10, a[2])
                    p2 = (val, a[1] + 10, a[2])
                verts.extend([p1, p2])
                counts.append(2)
            elif kind == "angle":
                i, j, k = constr[1], constr[2], constr[3]
                a, b, c = pts[i], pts[j], pts[k]
                # Draw two lines from vertex
                verts.extend([b, a])
                counts.append(2)
                verts.extend([b, c])
                counts.append(2)
            elif kind == "equal":
                i, j, k, l = constr[1], constr[2], constr[3], constr[4]
                a, b = pts[i], pts[j]
                c, d = pts[k], pts[l]
                verts.extend([a, b])
                counts.append(2)
                verts.extend([c, d])
                counts.append(2)
            elif kind == "tangent":
                i, j, k = constr[1], constr[2], constr[3]
                a, b, c = pts[i], pts[j], pts[k]
                verts.extend([b, a])
                counts.append(2)
                verts.extend([b, c])
                counts.append(2)
            elif kind == "symmetric":
                i, j, k = constr[1], constr[2], constr[3]
                a, b, c = pts[i], pts[j], pts[k]
                verts.extend([a, b])
                counts.append(2)
                verts.extend([a, c])
                counts.append(2)
                verts.extend([b, c])
                counts.append(2)
            elif kind == "mirror":
                i, j = constr[1], constr[2]
                a, b = pts[i], pts[j]
                verts.extend([a, b])
                counts.append(2)
            elif kind == "fix":
                i = constr[1]
                a = pts[i]
                # Draw a small box around the fixed point
                for dx, dy, dz in [(-2,-2,-2), (2,-2,-2), (2,2,-2), (-2,2,-2),
                                   (-2,-2,2), (2,-2,2), (2,2,2), (-2,2,2)]:
                    verts.append((a[0]+dx, a[1]+dy, a[2]+dz))
                counts = [8]
            if not verts:
                return None
            # Badge: small 3D cross at the constraint anchor so each
            # constrained site reads in the viewport, not just as a line.
            anchor = panel_logic.constraint_anchor(constr, pts)
            if anchor is not None:
                s = 2.5
                for ax in range(3):
                    p1 = [anchor[0], anchor[1], anchor[2]]
                    p2 = [anchor[0], anchor[1], anchor[2]]
                    p1[ax] -= s
                    p2[ax] += s
                    verts.extend([tuple(p1), tuple(p2)])
                    counts.append(2)
            coords.point.setValues(0, len(verts), verts)
            lines.numVertices.setValues(0, len(counts), counts)
            sep.addChild(coords)
            sep.addChild(lines)
            return sep
        except Exception:
            return None

    def _add_arrow_head(self, sep, pt, direction, color):
        """Add a small cone arrowhead at pt pointing along direction."""
        try:
            from pivy import coin
            dx, dy, dz = direction
            length = (dx*dx + dy*dy + dz*dz)**0.5
            if length < 1e-6:
                return
            dx, dy, dz = dx/length, dy/length, dz/length
            # Cone at the tip
            cone = coin.SoCone()
            cone.bottomRadius = 1.5
            cone.height = 4.0
            trans = coin.SoTranslation()
            trans.translation = (pt[0] + dx*3, pt[1] + dy*3, pt[2] + dz*3)
            rot = coin.SoRotation()
            # Rotate from +Z to direction
            up = (0.0, 0.0, 1.0)
            axis = (up[1]*dz - up[2]*dy,
                    up[2]*dx - up[0]*dz,
                    up[0]*dy - up[1]*dx)
            ax_len = (axis[0]**2 + axis[1]**2 + axis[2]**2)**0.5
            if ax_len > 1e-6:
                angle = math.acos(max(-1.0, min(1.0, up[0]*dx + up[1]*dy + up[2]*dz)))
                rot.rotation.setValue(axis[0]/ax_len, axis[1]/ax_len, axis[2]/ax_len, angle)
            mat = coin.SoMaterial()
            mat.diffuseColor = color
            mat.ambientColor = color
            sep = coin.SoSeparator()
            sep.addChild(trans)
            sep.addChild(rot)
            sep.addChild(mat)
            sep.addChild(cone)
            sep.getParent().addChild(sep)  # add to the parent separator
        except Exception:
            pass

    def _remove_dim_preview(self):
        if self._dim_preview_root is None:
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            self._safe_remove_child(view, self._dim_preview_root)
        except Exception:
            pass
        self._dim_preview_root = None
        self._dim_preview_sep.clear()

    def _build_constraint_preview_live(self, p0, p1, d):
        """Build live preview for constraint being created (distance only)."""
        self._remove_constraint_preview()
        try:
            from pivy import coin
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            root = coin.SoSeparator()
            col = (0.20, 0.85, 0.20)  # green for live preview
            sep = coin.SoSeparator()
            col_node = coin.SoBaseColor()
            col_node.rgb = col
            sep.addChild(col_node)
            coords = coin.SoCoordinate3()
            lines = coin.SoLineSet()
            verts = [p0, p1]
            coords.point.setValues(0, len(verts), verts)
            lines.numVertices.setValues(0, 1, [2])
            sep.addChild(coords)
            sep.addChild(lines)
            root.addChild(sep)
            sg.addChild(root)
            self._constraint_preview_sep = root
        except Exception:
            pass

    def _setup_plane_triad(self):
        """Draw a triad gizmo at the construction plane origin with X/Y/Z axes.
        Allows dragging axes to rotate the plane, and dragging center to translate."""
        self._remove_plane_triad()
        try:
            from pivy import coin
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            origin, normal, u, v = self._plane_geometry()
            # Create triad root
            root = coin.SoSeparator()
            root.setName("TriadRoot")
            # X axis (red)
            x_axis = self._make_triad_axis(root, origin, u, (1.0, 0.0, 0.0), 'X')
            # Y axis (green)
            y_axis = self._make_triad_axis(root, origin, v, (0.0, 1.0, 0.0), 'Y')
            # Z axis (blue)
            z_axis = self._make_triad_axis(root, origin, normal, (0.0, 0.0, 1.0), 'Z')
            # Center sphere (draggable for translation)
            center_sphere = self._make_triad_center(root, origin, (0.8, 0.8, 0.8))
            sg.addChild(root)
            self._plane_triad_root = root
            self._triad_axes = {'X': (x_axis, u), 'Y': (y_axis, v), 'Z': (z_axis, normal)}
            # Add event callback for triad interaction
            self._cb_triad_click = view.addEventCallback(
                "SoMouseButtonEvent", self._on_triad_click)
            self._cb_triad_move = view.addEventCallback(
                "SoLocation2Event", self._on_triad_move)
        except Exception as exc:
            _panel_warning(self, "Railform 3D sketch: plane triad unavailable (%s)" % exc)
            self._plane_triad_root = None

    def _make_triad_axis(self, parent, origin, direction, color, label):
        """Create one axis of the triad (line + cone + label)."""
        from pivy import coin
        sep = coin.SoSeparator()
        sep.setName("TriadAxis_" + label)
        col = coin.SoBaseColor()
        col.rgb = color
        sep.addChild(col)
        # Axis line
        coords = coin.SoCoordinate3()
        line = coin.SoLineSet()
        axis_len = 50.0
        p1 = (origin[0], origin[1], origin[2])
        p2 = (origin[0] + direction[0] * axis_len,
              origin[1] + direction[1] * axis_len,
              origin[2] + direction[2] * axis_len)
        coords.point.setValues(0, 2, [p1, p2])
        line.numVertices.setValues(0, 1, [2])
        sep.addChild(coords)
        sep.addChild(line)
        # Arrowhead cone at tip
        trans = coin.SoTranslation()
        trans.translation = p2
        cone = coin.SoCone()
        cone.bottomRadius = 1.5
        cone.height = 4.0
        rot = coin.SoRotation()
        # Rotate from +Z to direction
        up = (0.0, 0.0, 1.0)
        axis = (up[1]*direction[2] - up[2]*direction[1],
                up[2]*direction[0] - up[0]*direction[2],
                up[0]*direction[1] - up[1]*direction[0])
        ax_len = (axis[0]**2 + axis[1]**2 + axis[2]**2)**0.5
        if ax_len > 1e-6:
            axis = (axis[0]/ax_len, axis[1]/ax_len, axis[2]/ax_len)
            angle = math.acos(max(-1.0, min(1.0, up[0]*direction[0] + up[1]*direction[1] + up[2]*direction[2])))
            rot.rotation.setValue(axis[0], axis[1], axis[2], angle)
        mat = coin.SoMaterial()
        mat.diffuseColor = color
        mat.ambientColor = color
        cone_sep = coin.SoSeparator()
        cone_sep.addChild(trans)
        cone_sep.addChild(rot)
        cone_sep.addChild(mat)
        cone_sep.addChild(cone)
        sep.addChild(cone_sep)
        parent.addChild(sep)
        return sep

    def _make_triad_center(self, parent, origin, color):
        """Create the center sphere for translation dragging."""
        from pivy import coin
        sep = coin.SoSeparator()
        sep.setName("TriadCenter")
        col = coin.SoBaseColor()
        col.rgb = color
        sep.addChild(col)
        trans = coin.SoTranslation()
        trans.translation = origin
        sphere = coin.SoSphere()
        sphere.radius = 3.0
        trans.translation = origin
        sep.addChild(trans)
        sep.addChild(sphere)
        parent.addChild(sep)
        return sep

    def _remove_plane_triad(self):
        if self._plane_triad_root is None:
            # Still clear stale callback IDs: a previous setup may have
            # registered them and failed before assigning the root.
            self._cb_triad_click = None
            self._cb_triad_move = None
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is not None:
                drop_callbacks(view, (
                    ("SoMouseButtonEvent", self, "_cb_triad_click"),
                    ("SoLocation2Event", self, "_cb_triad_move"),
                ))
            self._safe_remove_child(view, self._plane_triad_root)
        except Exception:
            pass
        self._plane_triad_root = None
        self._triad_axes = {}
        self._cb_triad_click = None
        self._cb_triad_move = None

    def _pick_triad(self, view, pos):
        """'X'/'Y'/'Z'/'center' if the cursor pick ray hits that triad part,
        else None. Single shared implementation for the triad move/click
        handlers and the placement-click gate below (previously three
        near-identical SoRayPickAction copies)."""
        if self._plane_triad_root is None:
            return None
        try:
            from pivy import coin
            ray = snaputil.pick_ray(view, pos)
            if ray is None:
                return None
            ro, rd = ray
            pick_action = coin.SoRayPickAction(view.getViewportRegion())
            pick_action.setRay(coin.SbVec3f(ro.x, ro.y, ro.z),
                               coin.SbVec3f(rd.x, rd.y, rd.z))
            pick_action.apply(self._plane_triad_root)
            picked = pick_action.getPickedPoint()
            if not picked:
                return None
            path = picked.getPath()
            if path is None:
                return None
            for node in path.getNodeList():
                try:
                    name = node.getName()
                except Exception:
                    continue
                if name.startswith("TriadAxis_"):
                    return name.split("_")[1]
                if name == "TriadCenter":
                    return "center"
        except Exception:
            pass
        return None

    def _safe_remove_child(self, view, node):
        """Remove a Coin child only while it is still attached: a blind
        removeChild on an already-detached node can fault inside Coin
        instead of raising, which surfaces as 'Access violation' in the
        Report view rather than a catchable Python exception."""
        if node is None:
            return False
        try:
            sg = view.getSceneGraph() if view is not None else None
            if sg is None:
                return False
            try:
                if sg.findChild(node) < 0:
                    return False
            except Exception:
                pass
            sg.removeChild(node)
            return True
        except Exception:
            return False

    def _on_triad_move(self, ev):
        """Handle mouse move over triad - highlight axis under cursor."""
        if not self._drawing or self._plane_triad_root is None:
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            hit = self._pick_triad(view, ev["Position"])
            if hit in ("X", "Y", "Z"):
                self._highlight_triad_axis(hit)
            elif hit == "center":
                self._highlight_triad_center()
            else:
                self._unhighlight_triad()
        except Exception:
            pass

    def _on_triad_click(self, ev):
        """Handle click on triad - start dragging axis or center."""
        if ev.get("Type") != "SoMouseButtonEvent" or ev.get("State") != "DOWN":
            return
        if ev.get("Button") != "BUTTON1":
            return
        if not self._drawing or self._plane_triad_root is None:
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            hit = self._pick_triad(view, ev["Position"])
            if hit in ("X", "Y", "Z"):
                self._triad_dragging = True
                self._triad_drag_axis = hit
                self._triad_drag_start = ev["Position"]
                self.status.setText("Dragging %s axis to rotate plane" % hit)
            elif hit == "center":
                self._triad_dragging = True
                self._triad_drag_center = True
                self._triad_drag_start = ev["Position"]
                self.status.setText("Dragging center to translate plane")
        except Exception:
            pass

    def _on_triad_release(self, ev):
        """Handle mouse button release - end dragging."""
        if ev.get("Type") != "SoMouseButtonEvent" or ev.get("State") != "UP":
            return
        if not getattr(self, '_triad_dragging', False):
            return
        if ev.get("Button") != "BUTTON1":
            return
        try:
            self._triad_dragging = False
            self._triad_drag_axis = None
            self._triad_drag_center = False
            self._unhighlight_triad()
            self.status.setText("")
            # Update plane from drag
            if self._plane_grid_root is not None:
                self._remove_plane_grid()
                self._setup_plane_grid()
            if self._plane_triad_root is not None:
                self._remove_plane_triad()
                self._setup_plane_triad()
            self._update_status()
        except Exception:
            pass

    def _highlight_triad_axis(self, axis_name):
        # Visual feedback - could change color/thickness
        pass

    def _highlight_triad_center(self):
        pass

    def _unhighlight_triad(self):
        pass

    def _on_plane_changed(self, *_args):
        """Plane-orientation / offset changed: retarget the
        construction plane and refresh its viewport grid and triad."""
        if not getattr(self, "_ready", False):
            return
        try:
            vd = FreeCADGui.ActiveDocument.ActiveView.getViewDirection()
        except Exception:
            vd = (0.0, 0.0, 1.0)
        self._plane_normal = panel_logic.plane_normal_for(
            self.plane_combo.currentText(), (vd[0], vd[1], vd[2]))
        if self._plane_grid_root is not None:
            self._remove_plane_grid()
            self._setup_plane_grid()
        if self._plane_triad_root is not None:
            self._remove_plane_triad()
            self._setup_plane_triad()
        self._update_status()

    def _toggle_placing(self, on):
        self.place_btn.setText("Place points: on" if on else "Place points: off")
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                raise RuntimeError("no active document")
            view = doc.ActiveView
            if view is None:
                raise RuntimeError("no active view")
            if on:
                if self._drawing:
                    return   # avoid leaking a second pair of callbacks
                if self._drag_on:
                    self.drag_btn.setChecked(False)
                # Lock the construction plane's orientation to the camera
                # at mode start (unless a principal plane is chosen);
                # its origin is (0,0,0) + offset along that normal.
                vd = view.getViewDirection()
                if self.plane_combo.currentText() != "Auto":
                    self._plane_normal = panel_logic.plane_normal_for(
                        self.plane_combo.currentText(), (vd[0], vd[1], vd[2]))
                # Auto: keep the previous normal, _on_click flips it per
                # placed segment via auto_plane_normal.
                self._cb_click = view.addEventCallback(
                    "SoMouseButtonEvent", self._on_click)
                self._cb_move = view.addEventCallback(
                    "SoLocation2Event", self._on_move)
                self._ensure_key_cb(view)
                self._snap_cands = snaputil.collect_strong_snap_points(
                    doc.Document, exclude=None)
                self._snap_shapes = [e for e, _o in
                                     snaputil.collect_snap_shapes(
                                         doc.Document, exclude=None)]
                self._drawing = True
                self._setup_plane_grid()
                self._setup_plane_triad()
            else:
                self._stop_placing(view)
                self._remove_plane_grid()
                self._remove_plane_triad()
        except Exception as exc:
            _panel_warning(self, "Railform 3D sketch: placement unavailable (%s)" % exc)
            self.place_btn.setChecked(False)

    def _stop_placing(self, view=None):
        if not self._drawing:
            return
        try:
            view = view or FreeCADGui.ActiveDocument.ActiveView
            if view is not None:
                drop_callbacks(view, (
                    ("SoMouseButtonEvent", self, "_cb_click"),
                    ("SoLocation2Event", self, "_cb_move"),
                ))
        except Exception:
            pass
        self._cb_click = self._cb_move = None
        self._drawing = False
        self._maybe_remove_key_cb(view)

    def _ensure_key_cb(self, view=None):
        """Register the SoKeyboardEvent handler once (shared by place and
        drag modes, so unregister only when both are off)."""
        if self._cb_key is not None:
            return
        try:
            view = view or FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            self._cb_key = view.addEventCallback(
                "SoKeyboardEvent", self._on_key)
        except Exception:
            self._cb_key = None

    def _maybe_remove_key_cb(self, view=None):
        if self._cb_key is None:
            return
        if self._drawing or self._drag_on:
            return
        try:
            view = view or FreeCADGui.ActiveDocument.ActiveView
            if view is not None:
                drop_callbacks(view, (("SoKeyboardEvent", self, "_cb_key"),))
        except Exception:
            pass
        self._cb_key = None

    def _on_key(self, ev):
        """X/Y/Z toggles the axis lock, Tab cycles the plane family, Esc
        ends the active drag gesture, Ctrl+Z undoes. Viewport must have
        keyboard focus."""
        try:
            if ev.get("Type") != "SoKeyboardEvent":
                return
            key = ev.get("Key")
            if ev.get("State") == "DOWN":
                if key == 17:     # Ctrl
                    self._ctrl = True
                    return
                if getattr(self, "_ctrl", False) and key in (90, 122):
                    self._undo()  # Ctrl+Z (takes precedence over Z-lock)
                    return
            elif ev.get("State") == "UP":
                if key == 17:
                    self._ctrl = False
                return
            if key in (88, 120):      # X / x
                self._set_axis_lock(None if self._axis_lock == "X" else "X")
            elif key in (89, 121):    # Y / y
                self._set_axis_lock(None if self._axis_lock == "Y" else "Y")
            elif key in (90, 122):    # Z / z
                self._set_axis_lock(None if self._axis_lock == "Z" else "Z")
            elif key == 9:            # Tab: cycle plane family
                nxt = panel_logic.cycle_plane(self.plane_combo.currentText())
                self.plane_combo.setCurrentIndex(self.plane_combo.findText(nxt))
                # _on_plane_changed rebuilds grid/triad + status line already
                self.status.setText("Plane: %s (Tab)" % nxt)
            elif key == 27:           # Esc: drop the active drag gesture
                if self._dragging:
                    self._dragging = False
                    self._drag_idx = None
                    self._drag_anchor = None
                    self._remove_snap_feedback()
                    self._update_status()
            elif key == 13:           # Enter: edit hovered constraint
                self._edit_hovered_constraint()
        except Exception:
            pass

    def _set_axis_lock(self, axis):
        self._axis_lock = axis
        try:
            self.axis_label.setText(
                "Axis: %s (X/Y/Z to lock, Tab = plane)"
                % ("free" if axis is None else axis + "-locked"))
        except Exception:
            pass
        try:
            self.status.setText(
                "Axis lock: %s" % ("free" if axis is None else axis))
        except Exception:
            pass

    def _on_move(self, ev):
        try:
            doc = FreeCADGui.ActiveDocument
            if doc is None:
                return
            view = doc.ActiveView
            pos = ev["Position"]
            if self.snap_btn.isChecked():
                # Recompute the (OCCT-heavy) snap only once the cursor
                # actually moved: nearest_snap_ranked sweeps every
                # document edge per call, so re-running it on sub-pixel
                # mousemove noise is pure cost and crash exposure.
                last = getattr(self, "_last_snap_px", None)
                try:
                    moved = last is None or (
                        (pos[0] - last[0]) ** 2 +
                        (pos[1] - last[1]) ** 2) ** 0.5 > 2.0
                except Exception:
                    moved = True
                if moved:
                    px = snaputil._snap_tolerance_mm(view, 4.0)
                    self._snap_pt = snaputil.nearest_snap_ranked(
                        view, self._snap_cands, self._snap_shapes, pos, px)
                    try:
                        self._last_snap_px = (pos[0], pos[1])
                    except Exception:
                        pass
            else:
                self._snap_pt = None
                self._last_snap_px = None
            self._hover_constr = self._hover_constraint(pos)
            # Live plane tracking in "Auto" mode: as the cursor moves, if we
            # have at least one placed point, reorient the construction
            # plane to contain the direction from the last point to the
            # cursor hit on the current plane. This mimics SW's continuous
            # plane tracking during drag.
            if self.plane_combo.currentText() == "Auto" and self._rows:
                ray = snaputil.pick_ray(view, pos)
                if ray is not None:
                    ro, rd = ray
                    origin, normal = panel_logic.construction_plane(
                        self._plane_base, self._plane_normal,
                        offset=self.offset.value())
                    hit = panel_logic.ray_plane_intersect(
                        (ro.x, ro.y, ro.z), (rd.x, rd.y, rd.z), origin, normal)
                    if hit is not None:
                        last = self._rows[-1].values()
                        hit = panel_logic.apply_axis_lock(
                            hit, last, self._axis_lock)
                        chord = (hit[0] - last[0], hit[1] - last[1], hit[2] - last[2])
                        new_n = panel_logic.auto_plane_normal(chord, normal)
                        if new_n != normal:
                            self._plane_normal = new_n
                            if self._plane_grid_root is not None:
                                self._setup_plane_grid()
        except Exception:
            self._snap_pt = None
        # Live snap feedback during drag
        self._update_snap_feedback()

    def _update_snap_feedback(self):
        """Show live snap marker and distance while dragging in placement mode."""
        try:
            if not self._drawing:
                self._remove_snap_feedback()
                return
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                self._remove_snap_feedback()
                return
            if self._snap_pt is not None:
                # Rebuild the marker only when the snap point actually
                # moved: a full scene-graph teardown per mousemove is
                # both slow and risky inside Coin event dispatch.
                prev = getattr(self, "_snap_shown_pt", None)
                moved = True
                if prev is not None:
                    try:
                        moved = (((self._snap_pt.x - prev[0]) ** 2 +
                                  (self._snap_pt.y - prev[1]) ** 2 +
                                  (self._snap_pt.z - prev[2]) ** 2) ** 0.5
                                 > 1e-6)
                    except Exception:
                        moved = True
                if moved:
                    # Show snap marker at snapped point
                    self._show_snap_marker(self._snap_pt)
                    try:
                        self._snap_shown_pt = (self._snap_pt.x,
                                               self._snap_pt.y, self._snap_pt.z)
                    except Exception:
                        pass
                # Show distance from last point to snap point
                if self._rows:
                    last = self._rows[-1].values()
                    dx = self._snap_pt.x - last[0]
                    dy = self._snap_pt.y - last[1]
                    dz = self._snap_pt.z - last[2]
                    dist = (dx*dx + dy*dy + dz*dz)**0.5
                    self.status.setText("Snap: %.2f mm from last point" % dist)
            else:
                self._remove_snap_feedback()
                hov = getattr(self, "_hover_constr", None)
                if hov is not None:
                    self.status.setText(
                        panel_logic.constraint_tooltip(hov[1], self._points())
                        + "  (Enter = edit)")
        except Exception:
            pass

    def _show_snap_marker(self, pt):
        """Show a temporary marker at the snap point."""
        try:
            from pivy import coin
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            sg = view.getSceneGraph()
            self._remove_snap_feedback()
            root = coin.SoSeparator()
            col = coin.SoBaseColor()
            col.rgb = (1.0, 1.0, 0.0)  # yellow snap marker
            root.addChild(col)
            coords = coin.SoCoordinate3()
            points = coin.SoPointSet()
            coords.point.setValues(0, 1, [(pt.x, pt.y, pt.z)])
            root.addChild(coords)
            root.addChild(points)
            # Add a crosshair
            lines = coin.SoLineSet()
            lc = coin.SoCoordinate3()
            size = 5.0
            verts = [
                (pt.x - size, pt.y, pt.z), (pt.x + size, pt.y, pt.z),
                (pt.x, pt.y - size, pt.z), (pt.x, pt.y + size, pt.z),
                (pt.x, pt.y, pt.z - size), (pt.x, pt.y, pt.z + size),
            ]
            lc.point.setValues(0, len(verts), verts)
            lines.numVertices.setValues(0, 3, [2, 2, 2])
            root.addChild(lc)
            root.addChild(lines)
            sg.addChild(root)
            self._snap_feedback_root = root
        except Exception:
            pass

    def _remove_snap_feedback(self):
        if getattr(self, '_snap_feedback_root', None) is not None:
            try:
                view = FreeCADGui.ActiveDocument.ActiveView
                self._safe_remove_child(view, self._snap_feedback_root)
            except Exception:
                pass
            self._snap_feedback_root = None
        self._snap_shown_pt = None

    def _on_click(self, ev):
        if ev.get("Type") != "SoMouseButtonEvent" or ev.get("State") != "DOWN":
            return
        if ev.get("Button") != "BUTTON1":
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            pos = ev["Position"]
            # A click on the triad belongs to the plane gizmo, not to
            # point placement (both handlers listen to BUTTON1).
            try:
                if self._drawing and self._pick_triad(view, pos) is not None:
                    return
            except Exception:
                pass
            if self._snap_pt is not None:
                p = (self._snap_pt.x, self._snap_pt.y, self._snap_pt.z)
            else:
                ray = snaputil.pick_ray(view, pos)
                if ray is None:
                    return
                ro, rd = ray
                origin, normal = panel_logic.construction_plane(
                    self._plane_base, self._plane_normal,
                    offset=self.offset.value())
                hit = panel_logic.ray_plane_intersect(
                    (ro.x, ro.y, ro.z), (rd.x, rd.y, rd.z), origin, normal)
                if hit is None:
                    return
                p = hit
            if self._axis_lock is not None and self._rows:
                p = panel_logic.apply_axis_lock(
                    p, self._rows[-1].values(), self._axis_lock)
            self._add_row(p)
            # First placed point anchors the construction plane base for Auto mode.
            if self.plane_combo.currentText() == "Auto" and len(self._rows) == 1:
                self._plane_base = p
                if self._plane_grid_root is not None:
                    self._setup_plane_grid()
            # Auto plane-flip: re-target the construction plane to contain
            # the segment just drawn (with Auto selected), like SW's Tab.
            if self.plane_combo.currentText() == "Auto" and len(self._rows) >= 2:
                a, b = self._rows[-2].values(), self._rows[-1].values()
                chord = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
                self._plane_normal = panel_logic.auto_plane_normal(
                    chord, self._plane_normal)
                if self._plane_grid_root is not None:
                    self._setup_plane_grid()
            # Keep snap candidates fresh so the just-placed point snaps
            self._snap_cands = snaputil.collect_strong_snap_points(
                FreeCADGui.ActiveDocument.Document, exclude=None)
            self._snap_shapes = [e for e, _o in
                                 snaputil.collect_snap_shapes(
                                     FreeCADGui.ActiveDocument.Document,
                                     exclude=None)]
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # live drag-to-edit
    def _toggle_drag_edit(self, on):
        self.drag_btn.setText("Drag points: on" if on else "Drag points: off")
        self._drag_on = bool(on)
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                raise RuntimeError("no active view")
            if on:
                if self.place_btn.isChecked():
                    self.place_btn.setChecked(False)
                self._ensure_key_cb(view)
                doc = FreeCADGui.ActiveDocument.Document
                self._snap_cands = snaputil.collect_strong_snap_points(
                    doc, exclude=None)
                self._snap_shapes = [e for e, _o in
                                     snaputil.collect_snap_shapes(
                                         doc, exclude=None)]
                self._cb_drag_click = view.addEventCallback(
                    "SoMouseButtonEvent", self._on_drag_click)
                self._cb_drag_move = view.addEventCallback(
                    "SoLocation2Event", self._on_drag_move)
            else:
                self._stop_drag_edit(view)
        except Exception as exc:
            _panel_warning(self, "Railform 3D sketch: drag mode unavailable (%s)" % exc)
            self._drag_on = False
            self.drag_btn.setChecked(False)

    def _stop_drag_edit(self, view=None):
        try:
            view = view or FreeCADGui.ActiveDocument.ActiveView
            if view is not None:
                drop_callbacks(view, (
                    ("SoMouseButtonEvent", self, "_cb_drag_click"),
                    ("SoLocation2Event", self, "_cb_drag_move"),
                ))
        except Exception:
            pass
        self._cb_drag_click = self._cb_drag_move = None
        self._dragging = False
        self._drag_idx = None
        self._drag_anchor = None
        try:
            self._remove_snap_feedback()
        except Exception:
            pass
        self._maybe_remove_key_cb(view)

    def _nearest_row_index(self, view, pos, max_px=12):
        """Row whose 3D point projects closest to the cursor, or None."""
        best, best_d2 = None, float(max_px * max_px)
        for i, row in enumerate(self._rows):
            try:
                sp = snaputil.screen_project(
                    view, FreeCAD.Vector(*row.values()))
            except Exception:
                continue
            if not sp:
                continue
            try:
                dx, dy = sp[0] - pos[0], sp[1] - pos[1]
            except Exception:
                continue
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best, best_d2 = i, d2
        return best

    def _drag_hit(self, view, pos):
        """Where the dragged point goes: snap target when snapping wins,
        else the construction-plane hit. Returns (point_tuple, snapped)."""
        if self.snap_btn.isChecked():
            try:
                px = snaputil._snap_tolerance_mm(view, 4.0)
                best = snaputil.nearest_snap_ranked(
                    view, self._snap_cands, getattr(self, "_snap_shapes", []),
                    pos, px)
            except Exception:
                best = None
            if best is not None:
                return (best.x, best.y, best.z), True
        ray = snaputil.pick_ray(view, pos)
        if ray is None:
            return None, False
        ro, rd = ray
        origin, normal = panel_logic.construction_plane(
            self._plane_base, self._plane_normal,
            offset=self.offset.value())
        hit = panel_logic.ray_plane_intersect(
            (ro.x, ro.y, ro.z), (rd.x, rd.y, rd.z), origin, normal)
        if hit is None:
            return None, False
        return hit, False

    def _on_drag_click(self, ev):
        try:
            if ev.get("Type") != "SoMouseButtonEvent":
                return
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            pos = ev["Position"]
            if ev.get("State") == "DOWN" and ev.get("Button") == "BUTTON1":
                if not self._drag_on or self._dragging:
                    return
                idx = self._nearest_row_index(view, pos)
                if idx is None:
                    return
                self._push_undo()
                self._dragging = True
                self._drag_idx = idx
                self._drag_anchor = self._rows[idx].values()
                self.status.setText(
                    "Dragging P%d -- release to drop (Esc cancels%s)" % (
                        idx, ", %s-locked" % self._axis_lock
                        if self._axis_lock else ""))
            elif ev.get("State") == "UP" and ev.get("Button") == "BUTTON1":
                if not self._dragging:
                    return
                self._dragging = False
                self._drag_idx = None
                self._drag_anchor = None
                self._remove_snap_feedback()
                self._update_status()
        except Exception:
            pass

    def _on_drag_move(self, ev):
        if not self._drag_on or not self._dragging:
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view is None:
                return
            pos = ev["Position"]
            hit, snapped = self._drag_hit(view, pos)
            if hit is None:
                return
            hit = panel_logic.apply_axis_lock(
                hit, self._drag_anchor, self._axis_lock)
            row = self._rows[self._drag_idx]
            row.set_values(hit)
            self._sync_mirror_partners(self._drag_idx)
            if snapped:
                try:
                    self._show_snap_marker(FreeCAD.Vector(*hit))
                except Exception:
                    pass
            else:
                self._remove_snap_feedback()
            self._table_changed()  # status + live dim preview
            self.status.setText(
                "Dragging P%d: (%.2f, %.2f, %.2f)%s" % (
                    self._drag_idx, hit[0], hit[1], hit[2],
                    " [%s-locked]" % self._axis_lock
                    if self._axis_lock else ""))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # constraint creation mode (on-canvas)
    def _toggle_constraint_mode(self, on):
        self.constraint_btn.setText("Add Constraint: on" if on else "Add Constraint: off")
        self.constraint_type.setEnabled(on)
        self._constraint_creating = on
        self._constraint_first_idx = None
        self._constraint_preview_sep = None
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if on:
                self._cb_constraint_click = view.addEventCallback(
                    "SoMouseButtonEvent", self._on_constraint_click)
                self._cb_constraint_move = view.addEventCallback(
                    "SoLocation2Event", self._on_constraint_move)
            else:
                drop_callbacks(view, (
                    ("SoMouseButtonEvent", self, "_cb_constraint_click"),
                    ("SoLocation2Event", self, "_cb_constraint_move"),
                ))
                self._remove_constraint_preview()
        except Exception as exc:
            _panel_warning(self, "Railform 3D sketch: constraint mode unavailable (%s)" % exc)
            self.constraint_btn.setChecked(False)

    def _remove_constraint_preview(self):
        if self._constraint_preview_sep is not None:
            try:
                view = FreeCADGui.ActiveDocument.ActiveView
                self._safe_remove_child(view, self._constraint_preview_sep)
            except Exception:
                pass
            self._constraint_preview_sep = None

    def _on_constraint_move(self, ev):
        """Live preview while dragging to set distance value."""
        if not self._constraint_creating or self._constraint_first_idx is None:
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            pos = ev["Position"]
            ray = snaputil.pick_ray(view, pos)
            if ray is None:
                return
            ro, rd = ray
            origin, normal = panel_logic.construction_plane(
                self._plane_base, self._plane_normal,
                offset=self.offset.value())
            hit = panel_logic.ray_plane_intersect(
                (ro.x, ro.y, ro.z), (rd.x, rd.y, rd.z), origin, normal)
            if hit is None:
                return
            # Update preview with current distance
            p0 = self._rows[self._constraint_first_idx].values()
            d = ((p0[0] - hit[0])**2 + (p0[1] - hit[1])**2 + (p0[2] - hit[2])**2)**0.5
            self.dim_len.setValue(d)
            # Update live preview in viewport
            self._build_constraint_preview_live(p0, hit, d)
        except Exception:
            pass

    def _on_constraint_click(self, ev):
        if ev.get("Type") != "SoMouseButtonEvent" or ev.get("State") != "DOWN":
            return
        if ev.get("Button") != "BUTTON1":
            return
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            pos = ev["Position"]
            # Find nearest point index
            ray = snaputil.pick_ray(view, pos)
            if ray is None:
                return
            ro, rd = ray
            origin, normal = panel_logic.construction_plane(
                self._plane_base, self._plane_normal,
                offset=self.offset.value())
            hit = panel_logic.ray_plane_intersect(
                (ro.x, ro.y, ro.z), (rd.x, rd.y, rd.z), origin, normal)
            if hit is None:
                return
            # Find nearest point
            pts = self._points()
            best_idx, best_dist = None, 1e6
            for i, p in enumerate(pts):
                d = ((p[0] - hit[0])**2 + (p[1] - hit[1])**2 + (p[2] - hit[2])**2)**0.5
                if d < best_dist:
                    best_idx, best_dist = i, d
            if best_idx is None or best_dist > 50:  # too far
                return
            if self._constraint_first_idx is None:
                # First click - select first point
                self._constraint_first_idx = best_idx
                self.status.setText("Constraint: select second point")
            else:
                # Second click - create constraint
                i, j = self._constraint_first_idx, best_idx
                if i == j:
                    return
                kind = self.constraint_type.currentText()
                d = self.dim_len.value()
                if kind == "distance":
                    c = ("distance", i, j, d)
                    label = "P%d -> P%d = %.2f" % (i, j, d)
                elif kind == "coincide":
                    c = ("coincide", i, j)
                    label = "P%d == P%d" % (i, j)
                elif kind == "horizontal":
                    c = ("horizontal", i, d)
                    label = "horizontal P%d = %.2f" % (i, d)
                elif kind == "vertical":
                    c = ("vertical", i, d)
                    label = "vertical P%d = %.2f" % (i, d)
                elif kind == "angle":
                    # Need third point - skip for now
                    return
                else:
                    return
                self._push_undo()
                self._constraints.append(c)
                self.dim_list.addItem(label)
                try:
                    item = self.dim_list.item(self.dim_list.count() - 1)
                    if item is not None:
                        item.setToolTip(
                            panel_logic.constraint_tooltip(c, self._points()))
                except Exception:
                    pass
                self._constraint_first_idx = None
                self._update_status()
                if self.dim_preview_btn.isChecked():
                    self._build_dim_preview()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # project model edges onto the construction plane
    def _projection_direction(self):
        """Captured projection direction for the current 'Along ...'
        combo: None = straight down the plane normal (deterministic),
        else the 3D view direction NOW -- stored per ref at project
        time, so rebuilds never follow the camera around."""
        try:
            if "view" in self.proj_along.currentText().lower():
                return tuple(FreeCADGui.ActiveDocument.ActiveView
                             .getViewDirection())
        except Exception:
            pass
        return None

    def _project_selection(self):
        """Sample the selected model edges and drop the projected points
        onto the construction plane as new Line rows, EACH carrying an
        associative ProjectedRefs entry (issue #14): on every recompute
        the object re-projects the source edge at the stored parameter
        fraction onto its persisted plane, so the sketch follows the
        source when it moves. Projection direction comes from the
        'Along ...' combo -- plane normal, or the view direction
        captured now (stored per ref, so rebuilds stay deterministic)."""
        try:
            origin, normal, _u, _v = self._plane_geometry()
        except Exception as exc:
            self.status.setText("Project: no construction plane (%s)" % exc)
            return
        direction = self._projection_direction()
        try:
            sel = FreeCADGui.Selection.getSelectionEx()
        except Exception:
            sel = []
        # (edge, source object name, FreeCAD subelement name) triples so
        # each sampled point can record where it came from.
        edges = []
        for so in sel:
            src_name = getattr(getattr(so, "Object", None), "Name", "") or ""
            subs = getattr(so, "SubObjects", None) or []
            names = getattr(so, "SubElementNames", None) or []
            for i, sub in enumerate(subs):
                if getattr(sub, "ShapeType", "") == "Edge":
                    name = names[i] if i < len(names) else ""
                    edges.append((sub, src_name, name))
            if not subs:
                shp = getattr(getattr(so, "Object", None), "Shape", None)
                for j, e in enumerate(getattr(shp, "Edges", []) or []):
                    edges.append((e, src_name, "Edge%d" % (j + 1)))
        if not edges:
            self.status.setText("Project: select one or more model edges first")
            return
        pts = []
        refs = []
        for e, src_name, sub_name in edges:
            try:
                u0, u1 = e.FirstParameter, e.LastParameter
            except Exception:
                continue
            for k in range(12):
                frac = k / 11.0
                t = u0 + (u1 - u0) * frac
                try:
                    p = e.valueAt(t)
                except Exception:
                    continue
                q = panel_logic.project_point_along(
                    (p.x, p.y, p.z), origin, normal, direction)
                if q is None:
                    continue   # e.g. view dir parallel to plane: skip both
                pts.append(q)
                refs.append(panel_logic.encode_proj_ref(
                    "edge", src_name, sub_name, frac, direction))
            if len(pts) + len(self._rows) > 200:
                break
        if not pts:
            self.status.setText("Project: could not sample the selection")
            return
        self._push_undo()
        self._undo_guard = True
        try:
            for p, ref in zip(pts, refs):
                r = _PointRow(self, *p)
                r.set_seg("Line")
                self._rows.append(r)
                self._proj_refs.append(ref)
                self.rows_lay.addWidget(r.widget)
        finally:
            self._undo_guard = False
        self._proj_plane = (tuple(origin), tuple(normal))
        self._update_status()
        self.status.setText(
            "Projected %d point(s) from %d edge(s) -- associative, "
            "rebuild follows the source (%s)"
            % (len(pts), len(edges),
               "view direction" if direction is not None else "plane normal"))

    def _slice_selection(self):
        """Intersect the selected solids with the construction plane and
        add the cross-section edges as rows carrying 'slice' refs
        (issue #14 bullet 4 -- intersection-curve projection). The slice
        is re-cut from the source on every recompute, so the section
        follows the solid when it moves. Always projects along the
        plane normal -- the plane itself defines the cut."""
        try:
            origin, normal, _u, _v = self._plane_geometry()
        except Exception as exc:
            self.status.setText("Slice: no construction plane (%s)" % exc)
            return
        try:
            sel = FreeCADGui.Selection.getSelectionEx()
        except Exception:
            sel = []
        shapes = []
        for so in sel:
            obj = getattr(so, "Object", None)
            shp = getattr(obj, "Shape", None)
            if obj is not None and shp is not None and \
                    getattr(shp, "ShapeType", "") in ("Solid", "Shell",
                                                      "Compound", "Face"):
                shapes.append((obj.Name, shp))
        if not shapes:
            self.status.setText(
                "Slice: select one or more solids/faces to cut "
                "with the sketch plane")
            return
        pts = []
        refs = []
        for src_name, shp in shapes:
            edges = _plane_slice_edges(shp, origin, normal)
            for k, e in enumerate(edges):
                try:
                    u0, u1 = e.FirstParameter, e.LastParameter
                except Exception:
                    continue
                for m in range(12):
                    frac = m / 11.0
                    t = u0 + (u1 - u0) * frac
                    try:
                        p = e.valueAt(t)
                    except Exception:
                        continue
                    q = panel_logic.project_point_along(
                        (p.x, p.y, p.z), origin, normal, None)
                    if q is None:
                        continue
                    pts.append(q)
                    refs.append(panel_logic.encode_proj_ref(
                        "slice", src_name, str(k), frac, None))
                if len(pts) + len(self._rows) > 200:
                    break
            if len(pts) + len(self._rows) > 200:
                break
        if not pts:
            self.status.setText(
                "Slice: the plane misses the selection (or no edges)")
            return
        self._push_undo()
        self._undo_guard = True
        try:
            for p, ref in zip(pts, refs):
                r = _PointRow(self, *p)
                r.set_seg("Line")
                self._rows.append(r)
                self._proj_refs.append(ref)
                self.rows_lay.addWidget(r.widget)
        finally:
            self._undo_guard = False
        self._proj_plane = (tuple(origin), tuple(normal))
        self._update_status()
        self.status.setText(
            "Sliced %d cross-section point(s) from %d shape(s) -- "
            "associative, rebuild re-cuts the section"
            % (len(pts), len(shapes)))

    def _silhouette_selection(self):
        """issues.md #14 (silhouette projection): sample each selected
        shape's edge-level silhouette along the current projection
        direction and add it as associative 'sil' rows -- the outline
        re-derives from the source on every recompute, like the other
        projection kinds."""
        try:
            origin, normal, _u, _v = self._plane_geometry()
        except Exception as exc:
            self.status.setText(
                "Silhouette: no construction plane (%s)" % exc)
            return
        direction = self._projection_direction()
        eff = direction if direction is not None else normal
        try:
            sel = FreeCADGui.Selection.getSelectionEx()
        except Exception:
            sel = []
        shapes = []
        for so in sel:
            obj = getattr(so, "Object", None)
            shp = getattr(obj, "Shape", None)
            if obj is not None and shp is not None and \
                    not shp.isNull() and getattr(shp, "Faces", None):
                shapes.append((obj.Name, shp))
        if not shapes:
            self.status.setText(
                "Silhouette: select one or more shapes with faces first")
            return
        pts = []
        refs = []
        for src_name, shp in shapes:
            sil = _silhouette_edges(shp, eff)
            for k, e in enumerate(sil):
                try:
                    u0, u1 = e.FirstParameter, e.LastParameter
                except Exception:
                    continue
                for m in range(12):
                    frac = m / 11.0
                    t = u0 + (u1 - u0) * frac
                    try:
                        p = e.valueAt(t)
                    except Exception:
                        continue
                    q = panel_logic.project_point_along(
                        (p.x, p.y, p.z), origin, normal, direction)
                    if q is None:
                        continue
                    pts.append(q)
                    refs.append(panel_logic.encode_proj_ref(
                        "sil", src_name, str(k), frac, direction))
                if len(pts) + len(self._rows) > 200:
                    break
            if len(pts) + len(self._rows) > 200:
                break
        if not pts:
            self.status.setText(
                "Silhouette: no edge-level silhouette found (smooth "
                "surface? use 'Slice by sketch plane' instead)")
            return
        self._push_undo()
        self._undo_guard = True
        try:
            for p, ref in zip(pts, refs):
                r = _PointRow(self, *p)
                r.set_seg("Line")
                self._rows.append(r)
                self._proj_refs.append(ref)
                self.rows_lay.addWidget(r.widget)
        finally:
            self._undo_guard = False
        self._proj_plane = (tuple(origin), tuple(normal))
        self._update_status()
        self.status.setText(
            "Silhouette: %d point(s) from %d shape(s) -- associative, "
            "outline follows the source"
            % (len(pts), len(shapes)))

    def _selected_sub(self):
        """(object name, subelement name, sub object) of the first
        3D-view selection carrying an Edge/Face sub-element, else
        (None, None, None)."""
        try:
            sel = FreeCADGui.Selection.getSelectionEx()
        except Exception:
            sel = []
        for so in sel:
            obj = getattr(so, "Object", None)
            subs = getattr(so, "SubObjects", None) or []
            names = getattr(so, "SubElementNames", None) or []
            for i, sub in enumerate(subs):
                if getattr(sub, "ShapeType", "") not in ("Edge", "Face"):
                    continue
                if obj is None or i >= len(names) or not names[i]:
                    continue
                return obj.Name, names[i], sub
        return None, None, None

    def _apply_relation(self, kind):
        """issues.md #7: attach the chosen point row (pt# spinbox) to
        external geometry selected in the 3D view -- 'pierce' = the
        sketch-plane crossing of the selected edge/face, 'onsurf' =
        nearest point on the selected face. The ref lands in
        ProjectedRefs and is re-derived every recompute through the
        same machinery as #14, so the relation follows the source;
        enforcement is the re-projection pin (no solver Jacobian
        needed, contrary to what issues.md #7 originally assumed)."""
        idx = self.rel_point.value()
        while len(self._proj_refs) < len(self._rows):
            self._proj_refs.append("")   # defensive realignment
        if not (0 <= idx < len(self._rows)):
            self.status.setText(
                "Pierce/On face: pt# %d does not exist yet -- add the "
                "point (or lower the spinbox) first" % idx)
            return
        name, sub, _obj = self._selected_sub()
        if name is None:
            self.status.setText(
                "%s: select a specific edge or face in the 3D view first"
                % ("Pierce" if kind == "pierce" else "On face"))
            return
        if kind == "onsurf" and not sub.startswith("Face"):
            self.status.setText(
                "On face: selection is %r -- pick a FACE" % sub)
            return
        try:
            origin, normal, _u, _v = self._plane_geometry()
            self._proj_plane = (tuple(origin), tuple(normal))
        except Exception:
            if kind == "pierce":
                self.status.setText(
                    "Pierce: no construction plane -- pick a plane "
                    "(Tab cycles them) and retry")
                return
        self._push_undo()
        self._proj_refs[idx] = panel_logic.encode_proj_ref(
            kind, name, sub, 0.0, None)
        self._update_status()
        if kind == "pierce":
            self.status.setText(
                "P%d pierces %s.%s -- follows the source on rebuild "
                "(issue #7)" % (idx, name, sub))
        else:
            self.status.setText(
                "P%d lies on %s.%s -- follows the face on rebuild "
                "(issue #7)" % (idx, name, sub))

    # ------------------------------------------------------------------ #
    # task-panel API
    def getStandardButtons(self):
        return _std_buttons()

    def accept(self):
        pts = self._points()
        kind = self.kind.currentText()
        ok, msg = panel_logic.sketch3d_readiness(kind, len(pts))
        if not ok:
            _panel_error(self, "Railform 3D sketch: " + msg)
            return   # keep the dialog open -- don't lose the points
        try:
            self._stop_placing()
            self._stop_drag_edit()
            self._remove_plane_grid()
            self._remove_dim_preview()
            if getattr(self, "_section_plane", None) is not None:
                try:
                    self._safe_remove_child(
                        FreeCADGui.ActiveDocument.ActiveView,
                        self._section_plane)
                except Exception:
                    pass
                self._section_plane = None
            from rfstudio import parametric
            from FreeCAD import Vector
            doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
            types = [r.seg_type() for r in self._rows[:-1]]
            bulges = [r.bulge.value() for r in self._rows[:-1]]
            mixed = any(t != "Spline" for t in types)
            # Constraints OR mixed entities OR associative projections
            # need the unified parametric object (Sketch3DFP) -- the
            # classic per-curve objects from parametric.make_curve have
            # no constraint/mixed-entity/projected-ref home.
            proj_refs = (list(getattr(self, "_proj_refs", [])) +
                         [""] * len(pts))[:len(pts)]
            if mixed or self._constraints or any(proj_refs):
                obj = make_sketch3d_obj("Mixed" if mixed else kind, doc=doc)
                obj.Points = [Vector(*p) for p in pts]
                if mixed:
                    obj.SegmentTypes = types
                    obj.Bulges = [repr(b) for b in bulges]
                obj.Constraints = _serialize_constraints(self._constraints)
                # Variables: use EquationManager serialization for full state
                obj.Variables = _serialize_variables(self._variables)
                # EquationManager full state (issues.md #10 / #15)
                eq_data = panel_logic.serialize_equation_manager(self._eq_manager)
                if not hasattr(obj, "EqManagerState"):
                    obj.addProperty("App::PropertyString", "EqManagerState",
                                    "Sketch3D", "Equation manager state (JSON)")
                import json
                obj.EqManagerState = json.dumps(eq_data)
                # Issue #14: associative projection payload -- per-point
                # refs (already row-aligned via the panel's upkeep) +
                # link dependencies + the projection plane captured now.
                obj.ProjectedRefs = proj_refs
                obj.ExternalSources = _resolve_sources(doc, proj_refs)
                if getattr(self, "_proj_plane", None) is not None:
                    o, n = self._proj_plane
                    obj.PlaneOrigin = Vector(*o)
                    obj.PlaneNormal = Vector(*n)
            else:
                obj = parametric.make_curve(kind, doc=doc)
                obj.Points = [Vector(*p) for p in pts]
            self._handoff_obj = obj
            doc.recompute()
            # Contour recognition: detect ALL closed loops and offer actions for each
            # (SW's multi-region sketch support)
            from rfstudio import core
            from rfstudio import groups
            loops = panel_logic.find_closed_loops(pts)
            for loop_idx, (start, end) in enumerate(loops):
                loop_pts = pts[start:end+1]
                loop_wire = core.sketch3d_wire(
                    [Vector(*p) for p in loop_pts], types[start:end], bulges[start:end])
                
                # Extrude if checked
                if self.extrude_chk.isChecked():
                    try:
                        extruded = core.extrude_closed_wire(
                            loop_wire.Edges, self.extrude_h.value())
                        fobj = doc.addObject("Part::Feature", "Sketch3D_Extrude_Loop%d" % loop_idx)
                        fobj.Shape = extruded
                        groups.add_to_group(fobj, "Surfaces")
                    except ValueError as exc:
                        _panel_warning(self, "Railform 3D sketch: loop %d -- %s -- falling back to fill" % (loop_idx, exc))
                        fobj = doc.addObject("Part::Feature", "Sketch3D_Fill_Loop%d" % loop_idx)
                        fobj.Shape = core.n_sided_patch(list(loop_wire.Edges))
                        groups.add_to_group(fobj, "Surfaces")
                
                # Fillet if checked
                if self.fillet_chk.isChecked():
                    r = self.fillet_r.value()
                    if r > 0:
                        filleted = core.wire_corner_fillet(loop_wire, r)
                        fobj = doc.addObject("Part::Feature", "Sketch3D_Fillet_Loop%d" % loop_idx)
                        fobj.Shape = filleted
                        groups.add_to_group(fobj, "Surfaces")

                # Chamfer if checked
                if self.chamfer_chk.isChecked():
                    d = self.chamfer_d.value()
                    if d > 0:
                        cut = core.wire_corner_chamfer(loop_wire, d)
                        fobj = doc.addObject("Part::Feature", "Sketch3D_Chamfer_Loop%d" % loop_idx)
                        fobj.Shape = cut
                        groups.add_to_group(fobj, "Surfaces")
                
            # Create a group for this sketch and its features (nested feature tree)
            sketch_label = "Sketch3D"
            if self._constraints:
                sketch_label = "Sketch3D_Constrained"
            elif mixed:
                sketch_label = "Sketch3D_Mixed"
            sketch_group = groups.ensure_group(doc, sketch_label)
            
            # Add the sketch object itself to the sketch group
            if mixed or self._constraints or any(proj_refs):
                groups.add_to_group(obj, sketch_label)
            
            doc.recompute()

            FreeCADGui.Control.closeDialog()
        except Exception as exc:
            _panel_error(self, "Railform 3D sketch: create failed (%s)" % exc)

    
    def reject(self):
        try:
            self._stop_placing()
            self._stop_drag_edit()
            self._remove_plane_grid()
            self._remove_dim_preview()
            if getattr(self, "_section_plane", None) is not None:
                try:
                    self._safe_remove_child(
                        FreeCADGui.ActiveDocument.ActiveView,
                        self._section_plane)
                except Exception:
                    pass
                self._section_plane = None
        except Exception:
            pass
        FreeCADGui.Control.closeDialog()

    # --- sketch templates (JSON save/load) -------------------------------- #
    def _template_save(self):
        """Serialize the current sketch (points + constraints + vars + segs + bulges)
        to a JSON template file chosen via file dialog."""
        from rfstudio import panel_logic
        pts = self._points()
        constraints = [tuple(c) for c in self._constraints]
        seg_types = [r.seg_type() for r in self._rows]
        bulges = [r.bulge.value() for r in self._rows]
        variables = dict(getattr(self, "_variables", {}))
        data = {
            "points": pts,
            "constraints": constraints,
            "segment_types": seg_types,
            "bulges": bulges,
            "variables": variables,
            "proj_refs": list(getattr(self, "_proj_refs", [])),
        }
        import json
        try:
            from PySide import QtWidgets as _QW
        except Exception:
            from PySide import QtGui as _QW
        path, _ = _QW.QFileDialog.getSaveFileName(
            None, "Save sketch template", "sketch_template.json",
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            self.status.setText("Template saved: %s (%d points, %d constraints)" %
                                (path, len(pts), len(constraints)))
        except Exception as exc:
            self.status.setText("Template save failed: %s" % exc)

    def _template_load(self):
        """Load a sketch from a JSON template file chosen via file dialog.
        Replaces the current sketch, respecting undo."""
        import json
        try:
            from PySide import QtWidgets as _QW
        except Exception:
            from PySide import QtGui as _QW
        path, _ = _QW.QFileDialog.getOpenFileName(
            None, "Load sketch template", "",
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as exc:
            self.status.setText("Bad template file: %s" % exc)
            return
        from rfstudio import panel_logic
        self._push_undo()
        try:
            self._undo_guard = True
            for r in list(self._rows):
                self.rows_lay.removeWidget(r.widget)
                r.widget.setParent(None)
                r.widget.deleteLater()
            self._rows = []
            for p in data.get("points", []):
                r = _PointRow(self, *p)
                self._rows.append(r)
                self.rows_lay.addWidget(r.widget)
            segs = data.get("segment_types", [])
            bulges_raw = data.get("bulges", [])
            for i, r in enumerate(self._rows):
                if i < len(segs):
                    r.set_seg(segs[i])
                if i < len(bulges_raw):
                    r.set_bulge(float(bulges_raw[i]))
            constraints = data.get("constraints", [])
            self._constraints = [tuple(c) for c in constraints]
            self.dim_list.clear()
            for c in self._constraints:
                label = panel_logic.constraint_tooltip(c, self._points())
                self.dim_list.addItem(label)
            self._variables = data.get("variables", {})
            self._refresh_var_list()
            # projection refs travel with the template (issue #14);
            # absent/shorter lists pad with '' = free points
            refs = list(data.get("proj_refs", []))
            self._proj_refs = (refs + [""] * len(self._rows))[:len(self._rows)]
            self._update_status()
            if self.dim_preview_btn.isChecked():
                self._build_dim_preview()
            self.status.setText("Template loaded: %d points, %d constraints" %
                                (len(data.get("points", [])), len(constraints)))
        except Exception as exc:
            self.status.setText("Template load failed: %s" % exc)
        finally:
            self._undo_guard = False

    # --- break-apart / merge ----------------------------------------------- #
    def _break_apart(self):
        """Split the sketch into separate wires per detected closed loop.
        Creates one Part::Feature per loop."""
        from rfstudio import core
        from rfstudio import groups
        from rfstudio import panel_logic
        pts = self._points()
        if len(pts) < 2:
            self.status.setText("Break apart: need at least 2 points")
            return
        types = [r.seg_type() for r in self._rows]
        bulges = [r.bulge.value() for r in self._rows]
        loops = panel_logic.find_closed_loops(pts)
        if not loops:
            self.status.setText("Break apart: no closed loops detected")
            return
        doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
        for loop_idx, (start, end) in enumerate(loops):
            loop_pts = pts[start:end+1]
            loop_types = types[start:end]
            loop_bulges = bulges[start:end]
            wire = core.sketch3d_wire(
                [FreeCAD.Vector(*p) for p in loop_pts], loop_types, loop_bulges)
            fobj = doc.addObject("Part::Feature", "Sketch3D_Loop%d" % loop_idx)
            fobj.Shape = wire
            groups.add_to_group(fobj, "Sketches")
        self.status.setText("Break apart: created %d loop wire(s)" % len(loops))
        doc.recompute()

    def _merge_sketches(self):
        """Merge selected wires (Part::Feature) into this sketch.
        Points are appended; constraints/vars are preserved."""
        sel = FreeCADGui.Selection.getSelection()
        if not sel:
            self.status.setText("Merge: select one or more wire objects first")
            return
        wires = []
        for obj in sel:
            if hasattr(obj, "Shape") and obj.Shape is not None:
                wires.append(obj.Shape)
        if not wires:
            self.status.setText("Merge: selection contains no valid wires")
            return
        from rfstudio import core
        all_pts = self._points()
        all_types = [r.seg_type() for r in self._rows]
        all_bulges = [r.bulge.value() for r in self._rows]
        for w in wires:
            if not hasattr(w, "Edges"):
                continue
            for e in w.Edges:
                try:
                    p0 = e.valueAt(e.FirstParameter)
                    p1 = e.valueAt(e.LastParameter)
                    if not all_pts or (p0.x != all_pts[-1][0] and p0.y != all_pts[-1][1] and p0.z != all_pts[-1][2]):
                        all_pts.append((p0.x, p0.y, p0.z))
                    all_pts.append((p1.x, p1.y, p1.z))
                    all_types.append("Line")
                    all_bulges.append(0.0)
                except Exception:
                    continue
        self._push_undo()
        self._undo_guard = True
        try:
            old_n = len(self._rows)
            old_refs = list(getattr(self, "_proj_refs", []))
            for r in list(self._rows):
                self.rows_lay.removeWidget(r.widget)
                r.widget.setParent(None)
                r.widget.deleteLater()
            self._rows = []
            for p, t, b in zip(all_pts, all_types, all_bulges):
                r = _PointRow(self, *p)
                r.set_seg(t)
                r.set_bulge(b)
                self._rows.append(r)
                self.rows_lay.addWidget(r.widget)
            # kept points keep their refs; appended merge points are free
            self._proj_refs = (old_refs[:old_n] +
                               [""] * len(self._rows))[:len(self._rows)]
        finally:
            self._undo_guard = False
        self._update_status()
        if self.dim_preview_btn.isChecked():
            self._build_dim_preview()
        self.status.setText("Merged %d wire(s) into sketch (%d points total)" %
                            (len(wires), len(self._rows)))

    # --- section view clipping --------------------------------------------- #
    def _toggle_section_view(self, on):
        """Enable a clipping plane (aligned to the construction plane) in the
        active 3D view for this panel's session."""
        try:
            btn = self.section_btn
        except AttributeError:
            btn = None
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
        except Exception:
            self.status.setText("Section view: no active 3D view")
            if btn is not None:
                btn.setChecked(False)
            return
        if self._section_plane is not None:
            # Remove any previous clipping plane before (re-)adding.
            try:
                self._safe_remove_child(view, self._section_plane)
            except Exception:
                pass
            self._section_plane = None
        if not on:
            if btn is not None:
                btn.setText("Section view: off")
            self.status.setText("Section view: off")
            return
        try:
            origin, normal, _u, _v = self._plane_geometry()
        except Exception as exc:
            self.status.setText("Section view: no plane (%s)" % exc)
            if btn is not None:
                btn.setChecked(False)
            return
        try:
            from pivy import coin
        except Exception as exc:
            self.status.setText("Section view: Coin3D unavailable (%s)" % exc)
            if btn is not None:
                btn.setChecked(False)
            return
        plane = coin.SoClipPlane()
        plane.plane.setValue(normal[0], normal[1], normal[2],
                             -origin[0]*normal[0] - origin[1]*normal[1] - origin[2]*normal[2])
        sep = coin.SoSeparator()
        sep.addChild(plane)
        view.getSceneGraph().addChild(sep)
        self._section_plane = sep
        if btn is not None:
            btn.setText("Section view: on")
        self.status.setText("Section view: on (plane %s)" % self.plane_combo.currentText())

    # --- surface handoff -------------------------------------------------- #
    def _accept_with_handoff(self, target):
        """Accept the sketch, then open the Loft panel with the new wire
        prefilled as a profile or a guide rail (target one of the two).
        Removes the sketch->surface reselection tax: one click instead
        of close, find object, open Loft, Add selected."""
        self._handoff_target = target
        self._handoff_obj = None
        self.accept()
        obj = getattr(self, "_handoff_obj", None)
        self._handoff_target = None
        self._handoff_obj = None
        if obj is None:
            return
        try:
            FreeCADGui.Selection.clearSelection()
            FreeCADGui.Selection.addSelection(obj)
            from rfstudio.panel_surface import LoftPanel
            panel = LoftPanel()
            if target == "rail":
                try:
                    if obj in panel.profiles.objs:
                        panel.profiles.objs.remove(obj)
                        panel.profiles._refresh()
                        panel.profiles._commit()
                    panel.rails.objs.append(obj)
                    panel.rails._refresh()
                    panel.rails._commit()
                    panel.recompute()
                except Exception as exc:
                    _panel_warning(self, "Railform 3D sketch: rail handoff fell back to profiles (%s)" % exc)
            FreeCADGui.Control.showDialog(panel)
        except Exception as exc:
            _panel_error(self, "Railform 3D sketch: Loft handoff failed (%s)" % exc)
