# -*- coding: utf-8 -*-
"""Parametric surface objects (Part::FeaturePython) driven by our own OCCT core.

Each object stores LINKS to its input curves, so moving a rail recomputes the
surface automatically -- the re-editable, parametric model that Curves' fire-and-
forget commands lack. All geometry comes from rfstudio.core (Gui-free, verified
headless); this module is the FreeCAD object/viewprovider wiring on top.

GUI-only (ViewProvider needs the Gui); the geometry it calls is not.
"""

import os
import FreeCAD
import FreeCADGui
import Part

from rfstudio import core
from rfstudio import groups
from rfstudio.mirror import mirror_shape, plane_base_normal


def _icon(name):
    return os.path.join(os.path.dirname(__file__), "resources", name)


# Tree/view-provider icon per object Kind. Without a getIcon() the objects fall
# back to the generic Part::FeaturePython icon (the "Part" box in the tree);
# CurveVP/SurfaceVP.getIcon() map the object's Kind to its toolbar glyph here.
_KIND_ICON = {
    # curves
    "InterpCurve": "rfs_interp.svg", "ControlCurve": "rfs_controlcurve.svg",
    "ApproxCurve": "rfs_approx.svg", "Polyline": "rfs_polyline.svg",
    "BezierCurve": "rfs_bezier.svg", "PolylineFillet": "rfs_polyfillet.svg",
    "JoinCurve": "rfs_join.svg",
    # surfaces
    "Loft": "rfs_loft.svg", "Gordon": "rfs_gordon.svg", "Coons": "rfs_coons.svg",
    "Sweep": "rfs_sweep.svg", "Fill": "rfs_fill.svg", "Match": "rfs_match.svg",
    "Offset": "rfs_offset.svg", "Thicken": "rfs_thicken.svg",
    "Shell": "rfs_shell.svg", "ExtendSurf": "rfs_extend.svg",
    "BlendSurf": "rfs_blend.svg",
    "N_sidedPatch": "rfs_patch44.svg",
    "Mirror": "rfs_mirror.svg", "MirrorGroup": "rfs_mirror.svg",
}


def _kind_icon(kind, fallback):
    """Absolute path to the icon for `kind`, or `fallback` if unknown/missing."""
    path = _icon(_KIND_ICON.get(kind, fallback))
    return path if os.path.exists(path) else _icon(fallback)


def _first_edge(obj):
    shp = getattr(obj, "Shape", None)
    if shp is None or not shp.Edges:
        return None
    return shp.Edges[0]


def _two_rail_sweep(rail1, rail2, profiles, blending="Blend",
                    profile_samples=20, rail_samples=40,
                    parametrization=0.0, extend=False):
    """Native two-rail sweep (our own OCCT skin -- no Curves/libS2R).

    Skins the profile curves between the two rails. blending / parametrization /
    extend are accepted for property/API compatibility but the native skin is
    deterministic and does not use them. This replaces the old libS2R path,
    which inverted a per-station frame matrix that goes singular whenever the
    two rails lack a compatible shared parametrization ('Cannot invert singular
    matrix')."""
    return core.two_rail_sweep(rail1, rail2, list(profiles),
                               n_rail=int(rail_samples),
                               n_prof=int(profile_samples))


# --------------------------------------------------------------------------- #
class _BaseFP(object):
    Kind = "Surface"

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def onChanged(self, obj, prop):
        pass

    def compute(self, obj, points):
        """Pure preview hook: build a Part.Shape from a CANDIDATE point
        list (e.g. mid-drag, before the point is committed to obj.Points)
        without touching obj itself. Used by panel_curve.CurvePanel and
        editmode.PointEditor for the live drag-preview line.

        Default: None (no preview) for any kind that doesn't override
        this -- e.g. PolylineFillet, which has no per-point drag preview
        concept. Point-defined curve kinds (InterpCurve/ControlCurve/
        ApproxCurve/Polyline/BezierCurve) override this below.
        """
        return None


class LoftFP(_BaseFP):
    Kind = "Loft"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Profiles", "Loft",
                        "Cross-section profiles to loft through (>= 2), in order")
        obj.addProperty("App::PropertyLinkList", "GuideRails", "Loft",
                        "Optional guide rails that constrain the loft path")
        obj.addProperty("App::PropertyEnumeration", "Continuity", "Loft",
                        "Cross-section smoothness")
        obj.Continuity = ["G0 (ruled)", "G1/G2 (smooth)"]
        obj.Continuity = "G1/G2 (smooth)"
        obj.addProperty("App::PropertyBool", "Closed", "Loft",
                        "Close the loft back to the first profile")
        obj.addProperty("App::PropertyBool", "Solid", "Loft",
                        "Build a solid instead of a shell (caps the ends)")
        obj.addProperty("App::PropertyIntegerConstraint", "MaxDegree", "Loft",
                        "Maximum surface degree along the loft direction")
        obj.MaxDegree = (5, 1, 8, 1)
        obj.addProperty("App::PropertyBool", "ShowInputs", "Loft",
                        "Keep the source Profiles/GuideRails visible "
                        "instead of auto-hiding them once the loft builds")
        obj.ShowInputs = False

    def execute(self, obj):
        # GuideRails: if two are present use the two-rail sweep as loft
        rails = [e for e in (_first_edge(o) for o in (obj.GuideRails or []))
                 if e]
        profiles = [e for e in (_first_edge(o) for o in (obj.Profiles or []))
                    if e]
        if len(profiles) < 1:
            return
        if rails and profiles:
            try:
                if len(rails) == 1:
                    obj.Shape = core.single_rail_sweep(
                        rails[0], profiles, n_rail=40, n_prof=20)
                elif len(rails) == 2:
                    obj.Shape = core.two_rail_sweep(
                        rails[0], rails[1], profiles,
                        n_rail=40, n_prof=20)
                else:
                    obj.Shape = core.multi_rail_sweep(
                        rails, profiles, n_rail=40, n_prof=20)
                groups.apply_input_visibility(obj, ["Profiles", "GuideRails"])
                return
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    "Railform Loft: guide-rail sweep failed (%s), "
                    "falling back to plain loft.\n" % exc)
        shapes = [o.Shape for o in (obj.Profiles or [])
                  if getattr(o, "Shape", None)]
        if len(shapes) < 2:
            return
        obj.Shape = core.loft(
            shapes,
            solid=bool(obj.Solid),
            ruled=str(obj.Continuity).startswith("G0"),
            closed=bool(obj.Closed),
            max_degree=int(obj.MaxDegree))
        groups.apply_input_visibility(obj, ["Profiles", "GuideRails"])


class GordonFP(_BaseFP):
    Kind = "Gordon"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "UCurves", "Gordon",
                        "Curves running in the U direction (at least 2)")
        obj.addProperty("App::PropertyLinkList", "VCurves", "Gordon",
                        "Curves running in the V direction (at least 2)")
        obj.addProperty("App::PropertyIntegerConstraint", "Samples", "Gordon",
                        "Sample count for the curve-curve intersection search "
                        "(higher = more accurate on tightly-curved networks)")
        obj.Samples = (24, 8, 96, 1)
        obj.addProperty("App::PropertyBool", "ShowInputs", "Gordon",
                        "Keep the source U/V curves visible instead of "
                        "auto-hiding them once the surface builds")
        obj.ShowInputs = False

    def execute(self, obj):
        u = [e for e in (_first_edge(o) for o in obj.UCurves) if e]
        v = [e for e in (_first_edge(o) for o in obj.VCurves) if e]
        if len(u) < 2 or len(v) < 2:
            return
        try:
            obj.Shape = core.gordon_network(u, v,
                                           samples_per_curve=int(obj.Samples))
            groups.apply_input_visibility(obj, ["UCurves", "VCurves"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Gordon: %s\n" % exc)


class CoonsFP(_BaseFP):
    Kind = "Coons"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Boundaries", "Coons",
                        "Exactly 4 boundary curves (any order)")
        obj.addProperty("App::PropertyIntegerConstraint", "Samples", "Coons",
                        "Surface sampling density")
        obj.Samples = (24, 6, 200, 1)
        obj.addProperty("App::PropertyBool", "ShowInputs", "Coons",
                        "Keep the 4 source boundary curves visible instead "
                        "of auto-hiding them once the patch builds")
        obj.ShowInputs = False

    def execute(self, obj):
        edges = [e for e in (_first_edge(o) for o in obj.Boundaries) if e]
        if len(edges) != 4:
            return
        obj.Shape = core.coons_patch(edges, n=int(obj.Samples))
        groups.apply_input_visibility(obj, ["Boundaries"])


class Sweep2RailFP(_BaseFP):
    Kind = "Sweep"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Rail1", "Sweep",
                        "First (spine) rail — use exactly one curve")
        obj.addProperty("App::PropertyLinkList", "Rail2", "Sweep",
                        "Second (guide) rail — use exactly one curve")
        obj.addProperty("App::PropertyLinkList", "Profiles", "Sweep",
                        "Cross-section curves (at least one)")
        obj.addProperty("App::PropertyEnumeration", "Blending", "Sweep",
                        "Blending method across the surface")
        obj.Blending = ["Average", "Blend", "Rail1", "Rail2"]
        obj.Blending = "Blend"
        obj.addProperty("App::PropertyIntegerConstraint", "ProfileSamples", "Sweep",
                        "Cross-section sample count")
        obj.ProfileSamples = (20, 2, 200, 1)
        obj.addProperty("App::PropertyIntegerConstraint", "RailSamples", "Sweep",
                        "Rail sample count")
        obj.RailSamples = (40, 2, 500, 1)
        obj.addProperty("App::PropertyFloat", "Parametrization", "Sweep",
                        "Parametrization factor (0 = chord length)")
        obj.Parametrization = 0.0
        obj.addProperty("App::PropertyBool", "Extend", "Sweep",
                        "Extend surface to cover full rail length")
        obj.Extend = False

    def execute(self, obj):
        rails = [e for e in (_first_edge(o) for o in obj.Rail1 or []) if e]
        rails += [e for e in (_first_edge(o) for o in obj.Rail2 or []) if e]
        if len(rails) != 2:
            return
        profiles = [e for e in (_first_edge(o) for o in obj.Profiles or []) if e]
        if not profiles:
            return
        try:
            obj.Shape = core.two_rail_sweep(
                rails[0], rails[1], profiles,
                n_rail=int(obj.RailSamples),
                n_prof=int(obj.ProfileSamples))
            groups.apply_input_visibility(obj, ["Rail1", "Rail2", "Profiles"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Sweep: %s\n" % exc)


class SweepNFP(_BaseFP):
    """Multi-rail sweep: N >= 2 rails, interior rails anchor the surface too.

    Reuses the same Profiles / ProfileSamples / RailSamples knobs as the
    two-rail Sweep; the only structural difference is a single ordered
    Rails link list (rail 0 and rail N-1 bound it, the ones between pin
    intermediate sections exactly).
    """
    Kind = "SweepN"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Rails", "SweepN",
                        "Rail curves in order (>= 2); the first and last "
                        "bound the sweep, intermediate rails anchor it")
        obj.addProperty("App::PropertyLinkList", "Profiles", "SweepN",
                        "Cross-section curves (at least one)")
        obj.addProperty("App::PropertyIntegerConstraint", "ProfileSamples",
                        "SweepN", "Cross-section sample count")
        obj.ProfileSamples = (20, 2, 200, 1)
        obj.addProperty("App::PropertyIntegerConstraint", "RailSamples",
                        "SweepN", "Rail sample count")
        obj.RailSamples = (40, 2, 500, 1)
        obj.addProperty("App::PropertyBool", "ShowInputs", "SweepN",
                        "Keep the source Rails/Profiles visible instead "
                        "of auto-hiding them once the sweep builds")
        obj.ShowInputs = False

    def execute(self, obj):
        rails = [e for e in (_first_edge(o) for o in (obj.Rails or [])) if e]
        if len(rails) < 2:
            return
        profiles = [e for e in (_first_edge(o) for o in obj.Profiles or []) if e]
        if not profiles:
            return
        try:
            obj.Shape = core.multi_rail_sweep(
                rails, profiles,
                n_rail=int(obj.RailSamples),
                n_prof=int(obj.ProfileSamples))
            groups.apply_input_visibility(obj, ["Rails", "Profiles"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Multi-rail sweep: %s\n" % exc)


class PlateFillFP(_BaseFP):
    """N>=3 fill using OCCT GeomPlate (G0 only)."""
    Kind = "Fill"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Boundaries", "Fill",
                        "Boundary curves (≥ 3) forming a closed loop")
        obj.addProperty("App::PropertyFloat", "Tolerance", "Fill",
                        "OCCT plate fill tolerance")
        obj.Tolerance = 0.001
        obj.addProperty("App::PropertyBool", "ShowInputs", "Fill",
                        "Show boundary curves")
        obj.ShowInputs = False

    def execute(self, obj):
        edges = [e for e in (_first_edge(o) for o in obj.Boundaries) if e]
        if len(edges) < 3:
            return
        try:
            obj.Shape = core.plate_fill(edges, tol=float(obj.Tolerance))
            groups.apply_input_visibility(obj, ["Boundaries"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Fill: %s\n" % exc)


class OffsetFP(_BaseFP):
    Kind = "Offset"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "Offset",
                        "Surface to offset")
        obj.addProperty("App::PropertyFloat", "Distance", "Offset",
                        "Offset distance (positive or negative)")
        obj.Distance = 1.0
        obj.addProperty("App::PropertyFloat", "Tolerance", "Offset",
                        "OCCT offset tolerance")
        obj.Tolerance = 1e-3

    def execute(self, obj):
        src = obj.Source
        if not src:
            return
        surf = getattr(src, "Shape", None)
        if not surf:
            return
        try:
            obj.Shape = core.offset_surface(surf, float(obj.Distance),
                                            tol=float(obj.Tolerance))
            groups.apply_input_visibility(obj, ["Source"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Offset: %s\n" % exc)


class ThickenFP(_BaseFP):
    Kind = "Thicken"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "Thicken",
                        "Surface to thicken")
        obj.addProperty("App::PropertyFloat", "Thickness", "Thicken",
                        "Thickness (positive)")
        obj.Thickness = 2.0
        obj.addProperty("App::PropertyBool", "Symmetric", "Thicken",
                        "Thicken both sides of the surface")
        obj.Symmetric = False
        obj.addProperty("App::PropertyFloat", "Tolerance", "Thicken",
                        "OCCT thick-solid tolerance")
        obj.Tolerance = 1e-3

    def execute(self, obj):
        src = obj.Source
        if not src:
            return
        surf = getattr(src, "Shape", None)
        if not surf:
            return
        try:
            obj.Shape = core.thicken_surface(
                surf, float(obj.Thickness),
                symmetric=bool(obj.Symmetric),
                tol=float(obj.Tolerance))
            groups.apply_input_visibility(obj, ["Source"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Thicken: %s\n" % exc)


class ShellFP(_BaseFP):
    """Hollows an existing SOLID to a wall thickness, opened at chosen
    faces -- the classic CAD "Shell" feature (removes material from a
    solid that already exists, as opposed to ThickenFP which BUILDS a
    solid from an open surface). Wraps core.shell_solid (OCCT's
    makeThickness)."""
    Kind = "Shell"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "Shell",
                        "Solid to hollow out")
        obj.addProperty("App::PropertyLinkSub", "FacesToRemove", "Shell",
                        "Face(s) to open when hollowing (sub-element refs "
                        "on Source)")
        obj.addProperty("App::PropertyFloat", "Thickness", "Shell",
                        "Wall thickness (positive)")
        obj.Thickness = 2.0
        obj.addProperty("App::PropertyFloat", "Tolerance", "Shell",
                        "OCCT thick-solid tolerance")
        obj.Tolerance = 1e-3

    def execute(self, obj):
        src = obj.Source
        if not src:
            return
        solid = getattr(src, "Shape", None)
        ref = obj.FacesToRemove
        sub_obj, sub_names = ref if ref else (None, [])
        if solid is None or sub_obj is None or not sub_names:
            return
        faces = []
        for name in sub_names:
            try:
                faces.append(solid.getElement(name))
            except Exception:
                pass
        if not faces:
            return
        try:
            obj.Shape = core.shell_solid(solid, faces, float(obj.Thickness),
                                         tol=float(obj.Tolerance))
            groups.apply_input_visibility(obj, ["Source"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Shell: %s\n" % exc)


class ExtendSurfFP(_BaseFP):
    """Extends a surface in U and V parametric directions."""
    Kind = "ExtendSurf"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "BaseSurface", "ExtendSurf",
                        "Surface to extend")
        obj.addProperty("App::PropertyFloat", "UFirst", "ExtendSurf",
                        "Extension at start of U parameter (negative extends backward)")
        obj.UFirst = 0.0
        obj.addProperty("App::PropertyFloat", "ULast", "ExtendSurf",
                        "Extension at end of U parameter (positive extends forward)")
        obj.ULast = 0.0
        obj.addProperty("App::PropertyFloat", "VFirst", "ExtendSurf",
                        "Extension at start of V parameter")
        obj.VFirst = 0.0
        obj.addProperty("App::PropertyFloat", "VLast", "ExtendSurf",
                        "Extension at end of V parameter")
        obj.VLast = 0.0
        obj.addProperty("App::PropertyFloat", "Tolerance", "ExtendSurf",
                        "Surface fitting tolerance")
        obj.Tolerance = 1e-3

    def execute(self, obj):
        src = obj.BaseSurface
        if not src:
            return
        surf = getattr(src, "Shape", None)
        if not surf:
            return
        try:
            obj.Shape = core.extend_surface(
                surf, 
                float(obj.UFirst), 
                float(obj.ULast), 
                float(obj.VFirst), 
                float(obj.VLast),
                tol=float(obj.Tolerance)
            )
            groups.apply_input_visibility(obj, ["BaseSurface"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform ExtendSurf: %s\n" % exc)


class BlendSurfFP(_BaseFP):
    """Native G1/G2 blend SURFACE strip between two faces -- the surface
    analog of BlendCurveFP above, and the fix for RFS_BlendSurf's
    previous no-native-fallback gap. Wraps core.blend_surface (quintic-
    Hermite cross-section skin, same technique match_surface uses)."""
    Kind = "BlendSurf"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "FaceA", "BlendSurf",
                        "First surface")
        obj.addProperty("App::PropertyLink", "FaceB", "BlendSurf",
                        "Second surface")
        obj.addProperty("App::PropertyEnumeration", "Continuity", "BlendSurf",
                        "Blend continuity")
        obj.Continuity = ["G0", "G1", "G2"]
        obj.Continuity = "G2"
        obj.addProperty("App::PropertyFloat", "Scale", "BlendSurf",
                        "Tangent strength")
        obj.Scale = 1.0
        obj.addProperty("App::PropertyIntegerConstraint", "Samples", "BlendSurf",
                        "Cross-section sample count")
        obj.Samples = (24, 6, 200, 1)

    def execute(self, obj):
        if not obj.FaceA or not obj.FaceB:
            return
        sa = getattr(obj.FaceA, "Shape", None)
        sb = getattr(obj.FaceB, "Shape", None)
        if sa is None or sb is None or not sa.Faces or not sb.Faces:
            return
        try:
            obj.Shape = core.blend_surface(
                sa.Faces[0], sb.Faces[0],
                continuity=str(obj.Continuity),
                n=int(obj.Samples), scale=float(obj.Scale))
            groups.apply_input_visibility(obj, ["FaceA", "FaceB"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform BlendSurf: %s\n" % exc)


class N_sidedPatchFP(_BaseFP):
    """N-sided patch with improved continuity (G1/G2) using star-point method."""
    Kind = "N_sidedPatch"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Boundaries", "N_sidedPatch",
                        "Boundary curves (≥ 3) forming a closed loop")
        obj.addProperty("App::PropertyIntegerConstraint", "Samples", "N_sidedPatch",
                        "Sampling density for the patch")
        obj.Samples = (16, 6, 100, 1)
        obj.addProperty("App::PropertyBool", "ShowInputs", "N_sidedPatch",
                        "Show boundary curves")
        obj.ShowInputs = False

    def execute(self, obj):
        edges = [e for e in (_first_edge(o) for o in obj.Boundaries) if e]
        if len(edges) < 3:
            return
        try:
            obj.Shape = core.n_sided_patch(edges, int(obj.Samples))
            groups.apply_input_visibility(obj, ["Boundaries"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform N_sidedPatch: %s\n" % exc)


# --------------------------------------------------------------------------- #
# Curve classes
# --------------------------------------------------------------------------- #

class InterpCurveFP(_BaseFP):
    """Interpolating B-spline curve through points."""
    Kind = "InterpCurve"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyVectorList", "Points", "InterpCurve",
                        "Interpolation points")
        obj.addProperty("App::PropertyBool", "ShowInputs", "InterpCurve",
                        "Show points")
        obj.ShowInputs = False

    def execute(self, obj):
        points = list(obj.Points)
        if len(points) < 2:
            return
        try:
            obj.Shape = core.interpolate_curve(points)
            groups.apply_input_visibility(obj, ["Points"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform InterpCurve: %s\n" % exc)

    def compute(self, obj, points):
        try:
            return core.interpolate_curve(list(points)) if len(points) >= 2 else None
        except Exception:
            return None


class ControlCurveFP(_BaseFP):
    """B-spline curve whose control polygon is the given points
    (pen/Bezier-style), with optional per-point weights (rational NURBS)
    and an editable knot vector.

    Geometry is delegated to core.control_poly_curve -- the same
    buildFromPolesMultsKnots/buildFromPolesMultsWeightsKnots call every
    other B-spline builder in this workbench uses. This previously called
    a nonexistent Part.BSplineCurve.createFromPolesWeightsKnots API (no
    such method exists; the real one is buildFromPolesMults*Knots), which
    always raised and was silently swallowed -- so this curve kind never
    actually built a shape.
    """
    Kind = "ControlCurve"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyVectorList", "Points", "ControlCurve",
                        "Control points")
        obj.addProperty("App::PropertyFloatList", "Weights", "ControlCurve",
                        "Weights for control points (rational NURBS if any != 1.0)")
        obj.addProperty("App::PropertyIntegerConstraint", "Degree", "ControlCurve",
                        "Curve degree (capped by point count)")
        obj.Degree = (3, 1, 8, 1)
        # Knots/Mults: the UNIQUE knot values and their multiplicities --
        # what editmode.KnotEditor actually drags and writes back. Without
        # this property the knot-drag editor had nothing real to read
        # (getattr(obj, "Knots", ...) silently fell back to a default every
        # time), so every knot drag was a no-op.
        obj.addProperty("App::PropertyFloatList", "Knots", "ControlCurve",
                        "Unique knot parameter values (edited via the "
                        "in-viewport knot-drag editor)")
        obj.addProperty("App::PropertyIntegerList", "Mults", "ControlCurve",
                        "Knot multiplicities, parallel to Knots")
        obj.addProperty("App::PropertyBool", "ShowInputs", "ControlCurve",
                        "Show control points")
        obj.ShowInputs = False

    def _valid_stored_knots(self, obj, n, deg):
        knots = list(getattr(obj, "Knots", []) or [])
        mults = list(getattr(obj, "Mults", []) or [])
        if not knots or len(knots) != len(mults):
            return None, None
        if sum(mults) != n + deg + 1:
            return None, None       # stale (from a different point count/degree)
        return knots, mults

    def execute(self, obj):
        points = list(obj.Points)
        n = len(points)
        if n < 2:
            return
        deg = max(1, min(int(obj.Degree), n - 1))
        weights = list(obj.Weights) if obj.Weights and len(obj.Weights) == n else None

        knots, mults = self._valid_stored_knots(obj, n, deg)
        if knots is not None:
            try:
                obj.Shape = core.control_poly_curve(
                    points, degree=deg, knots=knots, mults=mults, weights=weights)
                groups.apply_input_visibility(obj, ["Points", "Weights"])
                return
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    "Railform ControlCurve: stored knots rejected (%s), "
                    "regenerating defaults.\n" % exc)

        try:
            obj.Shape = core.control_poly_curve(points, degree=deg, weights=weights)
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform ControlCurve: %s\n" % exc)
            return
        # Seed Knots/Mults from what was actually built, so the knot-drag
        # editor has real values the first time it's opened.
        uk, um = core.knots_from_shape(obj.Shape)
        if uk is not None:
            obj.Knots = uk
            obj.Mults = um
        groups.apply_input_visibility(obj, ["Points", "Weights"])

    def compute(self, obj, points):
        points = list(points)
        if len(points) < 2:
            return None
        try:
            deg = max(1, min(int(getattr(obj, "Degree", 3) or 3), len(points) - 1))
            weights = list(obj.Weights) if obj.Weights and len(obj.Weights) == len(points) else None
            return core.control_poly_curve(points, degree=deg, weights=weights)
        except Exception:
            return None


class ApproxCurveFP(_BaseFP):
    """Approximating B-spline curve."""
    Kind = "ApproxCurve"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyVectorList", "Points", "ApproxCurve",
                        "Points to approximate")
        obj.addProperty("App::PropertyFloat", "Tolerance", "ApproxCurve",
                        "Approximation tolerance")
        obj.Tolerance = 0.001
        obj.addProperty("App::PropertyBool", "ShowInputs", "ApproxCurve",
                        "Show points")
        obj.ShowInputs = False

    def execute(self, obj):
        points = list(obj.Points)
        if len(points) < 2:
            return
        try:
            obj.Shape = core.approximate_curve(
                points, degree=3, tol=float(obj.Tolerance))
            groups.apply_input_visibility(obj, ["Points"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform ApproxCurve: %s\n" % exc)

    def compute(self, obj, points):
        try:
            return (core.approximate_curve(list(points), degree=3,
                                           tol=float(getattr(obj, "Tolerance", 1e-3)))
                    if len(points) >= 2 else None)
        except Exception:
            return None


class PolylineFP(_BaseFP):
    """Polyline through points.

    Corner FILLETS are a RECORD on this object -- `Fillets`, index-aligned
    with Points (0 = sharp) -- SolidWorks-sketch style rather than a
    second, linked curve object: `Points` keeps the SHARP corners (the
    sketch geometry) and execute() rebuilds the tangent arcs from the
    record on every recompute, so a fillet survives point edits and any
    other recompute. Driven live by editmode.CornerFilletEditor's
    handles. (The older derived-feature form -- a PolylineFillet object
    linking this one -- is parametric.PolylineFilletFP, still supported
    for existing documents.)"""
    Kind = "Polyline"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyVectorList", "Points", "Polyline",
                        "Points")
        obj.addProperty("App::PropertyFloatList", "Fillets", "Polyline",
                        "Per-corner fillet radius, index-aligned with "
                        "Points (0/missing = sharp). The fillet RECORD: "
                        "Points stay sharp and the arcs are rebuilt from "
                        "this list on every recompute. Driven live by "
                        "the in-viewport corner handles; a uniform "
                        "Radius panel writes the same value at every "
                        "index.")
        obj.addProperty("App::PropertyBool", "FilletClosed", "Polyline",
                        "Also round the corner between the last and "
                        "first point")
        obj.FilletClosed = False
        obj.addProperty("App::PropertyBool", "ShowInputs", "Polyline",
                        "Show points")
        obj.ShowInputs = False

    @staticmethod
    def _fillet_map(obj, n):
        """{corner_index: radius} from the `Fillets` record (indices
        beyond the current point count ignored), or None when there is
        nothing to round (no record / all-zero) -- in which case the
        plain single-edge polyline is built, exactly as before fillets
        existed."""
        radii = list(getattr(obj, "Fillets", []) or [])
        fmap = {i: float(r) for i, r in enumerate(radii[:n])
                if r and r > 0}
        return fmap or None

    def _build_fillet_shape(self, obj, pts):
        """Rounded wire from the record, or None if the fillet build
        fails (caller falls back to the plain polyline)."""
        fmap = self._fillet_map(obj, len(pts))
        if fmap is None:
            return None
        return core.polyline_corner_fillet(
            pts, fmap, closed=bool(getattr(obj, "FilletClosed", False)))

    def execute(self, obj):
        points = obj.Points
        if len(points) < 2:
            return
        try:
            pts = list(points)
            shape = self._build_fillet_shape(obj, pts)
            if shape is not None:
                # Fillet record active: rebuild the rounded wire from the
                # SHARP points -- the record is the constraint, Points is
                # the sketch geometry, and neither overwrites the other.
                obj.Shape = shape
            else:
                # A polyline as a SINGLE curve edge: a degree-1 (linear) B-spline
                # through the points. Same straight-segment geometry as a polygon
                # wire, but one edge -- uniform with the other Railform curves and
                # cleaner for downstream loft/sweep. (Part has no `Polyline`; that
                # raised "module 'Part' has no attribute 'Polyline'".)
                n = len(pts)
                bs = Part.BSplineCurve()
                knots = [float(i) for i in range(n)]     # one knot per point
                mults = [2] + [1] * (n - 2) + [2]        # clamped ends (degree+1)
                bs.buildFromPolesMultsKnots(pts, mults, knots, False, 1)
                obj.Shape = bs.toShape()
            groups.apply_input_visibility(obj, ["Points"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Polyline: %s\n" % exc)

    def compute(self, obj, points):
        pts = list(points)
        n = len(pts)
        if n < 2:
            return None
        # Live point-drag preview must show the filleted result too --
        # otherwise dragging a point of a filleted polyline flashes the
        # sharp shape until the next full recompute (panel_curve.py and
        # editmode.PointEditor both render this return value).
        try:
            shape = self._build_fillet_shape(obj, pts)
            if shape is not None:
                return shape
        except Exception:
            return None
        try:
            bs = Part.BSplineCurve()
            knots = [float(i) for i in range(n)]
            mults = [2] + [1] * (n - 2) + [2]
            bs.buildFromPolesMultsKnots(pts, mults, knots, False, 1)
            return bs.toShape()
        except Exception:
            return None


class PolylineFilletFP(_BaseFP):
    """Round the corners of a source polyline/curve with tangent arcs.

    Parametric: links to a Source curve (typically a RailformPolyline) and
    rebuilds a rounded wire whenever the source or Radius changes. Wraps
    core.polyline_corner_fillet (verified headless)."""
    Kind = "PolylineFillet"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "PolylineFillet",
                        "Polyline (or point-defined curve) whose corners to round")
        obj.addProperty("App::PropertyFloat", "Radius", "PolylineFillet",
                        "Uniform fillet radius applied at every corner "
                        "(auto-reduced where a segment is too short). "
                        "Used whenever Radii is empty or mismatched.")
        obj.Radius = 5.0
        obj.addProperty("App::PropertyFloatList", "Radii", "PolylineFillet",
                        "Per-corner radius (index-aligned with the "
                        "source's Points list; 0 = sharp corner). Driven "
                        "live by the in-viewport corner-fillet handles "
                        "(editmode.CornerFilletEditor). Falls back to the "
                        "uniform Radius above when empty or the wrong "
                        "length for the current source point count.")
        obj.Radii = []
        obj.addProperty("App::PropertyBool", "Closed", "PolylineFillet",
                        "Also round the corner between the last and first point")
        obj.Closed = False
        obj.addProperty("App::PropertyBool", "ShowInputs", "PolylineFillet",
                        "Keep the source curve visible")
        obj.ShowInputs = True

    def _source_points(self, obj):
        src = getattr(obj, "Source", None)
        if src is None:
            return []
        pts = getattr(src, "Points", None)
        if pts:                          # RailformPolyline & other point curves
            return list(pts)
        shp = getattr(src, "Shape", None)   # fallback: ordered wire vertices
        if shp is not None and getattr(shp, "Wires", None):
            try:
                return [v.Point for v in shp.Wires[0].OrderedVertexes]
            except Exception:
                pass
        return []

    def execute(self, obj):
        pts = self._source_points(obj)
        if len(pts) < 2:
            return
        # Per-corner Radii (driven live by editmode.CornerFilletEditor's
        # draggable handles) takes precedence when it's actually usable
        # for the current point count; otherwise fall back to the single
        # uniform Radius, exactly as before this property existed.
        radii = list(getattr(obj, "Radii", []) or [])
        if len(radii) == len(pts):
            radius_arg = {i: r for i, r in enumerate(radii) if r and r > 0}
            if not radius_arg:
                # every corner explicitly zeroed -- nothing to round, but
                # still produce the plain (sharp) polyline rather than
                # raising on an empty radius map.
                try:
                    obj.Shape = core.polyline_corner_fillet(
                        pts, {}, closed=bool(obj.Closed))
                except Exception as exc:
                    FreeCAD.Console.PrintError(
                        "Railform PolylineFillet: %s\n" % exc)
                return
        else:
            radius_arg = float(obj.Radius)
        try:
            obj.Shape = core.polyline_corner_fillet(
                pts, radius_arg, closed=bool(obj.Closed))
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform PolylineFillet: %s\n" % exc)
            return
        # single-link source: apply_input_visibility only handles link LISTS,
        # so drive the source's visibility from ShowInputs directly.
        src = getattr(obj, "Source", None)
        if src is not None and getattr(src, "ViewObject", None) is not None:
            src.ViewObject.Visibility = bool(obj.ShowInputs)


class JoinCurveFP(_BaseFP):
    """Join several curves end-to-end into one Wire (corners preserved, no
    refit -- WYSIWYG). Parametric: links to the source curves and rebuilds when
    any of them changes. Wraps core.join_curves (verified headless)."""
    Kind = "JoinCurve"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Sources", "JoinCurve",
                        "Curves to join end-to-end (their endpoints must meet)")
        obj.addProperty("App::PropertyBool", "ShowInputs", "JoinCurve",
                        "Keep the source curves visible")
        obj.ShowInputs = False

    def execute(self, obj):
        srcs = obj.Sources or []
        shapes = [s.Shape for s in srcs if getattr(s, "Shape", None)]
        if not shapes:
            return
        try:
            obj.Shape = core.join_curves(shapes)
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform JoinCurve: %s\n" % exc)
            return
        groups.apply_input_visibility(obj, ["Sources"])


class BezierCurveFP(_BaseFP):
    """Bezier curve with editable handles."""
    Kind = "BezierCurve"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyVectorList", "Points", "BezierCurve",
                        "Control points")
        obj.addProperty("App::PropertyVectorList", "LeftHandles", "BezierCurve",
                        "Left control handles")
        obj.addProperty("App::PropertyVectorList", "RightHandles", "BezierCurve",
                        "Right control handles")
        # Per-point handle type ("Auto"/"Aligned"/"Vector"/"Free"): one string
        # per control point, so this is a StringList -- NOT an Enumeration.
        # An empty Enumeration returns None, and execute()/editmode.py both call
        # len() on it, which raised "object of type 'NoneType' has no len()" and
        # aborted the recompute so the curve never drew. StringList defaults to
        # [] and matches every consumer (editmode.py, the impl plan).
        obj.addProperty("App::PropertyStringList", "HandleTypes", "BezierCurve",
                        "Handle types for each point")
        obj.addProperty("App::PropertyBool", "ShowInputs", "BezierCurve",
                        "Show control points and handles")
        obj.ShowInputs = False

    def execute(self, obj):
        points = obj.Points
        left_handles = obj.LeftHandles
        right_handles = obj.RightHandles
        handle_types = obj.HandleTypes

        if len(points) < 2:
            return

        # Initialize handle arrays if needed
        if len(left_handles) != len(points):
            left_handles = [points[i] for i in range(len(points))]
            obj.LeftHandles = left_handles
        if len(right_handles) != len(points):
            right_handles = [points[i] for i in range(len(points))]
            obj.RightHandles = right_handles
        if len(handle_types) != len(points):
            handle_types = ["Auto"] * len(points)
            obj.HandleTypes = handle_types

        try:
            # Real two-handle Bezier geometry (core.bezier_curve_shape),
            # not a plain interpolation through the anchor points -- the
            # previous version built curve.interpolate(points) here, which
            # meant LeftHandles/RightHandles were maintained and displayed
            # but had NO effect whatsoever on the actual curve shape.
            obj.Shape = core.bezier_curve_shape(points, left_handles, right_handles)
            groups.apply_input_visibility(obj, ["Points", "LeftHandles", "RightHandles"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform BezierCurve: %s\n" % exc)

    def compute(self, obj, points):
        points = list(points)
        if len(points) < 2:
            return None
        try:
            lh = list(getattr(obj, "LeftHandles", []) or [])
            rh = list(getattr(obj, "RightHandles", []) or [])
            return core.bezier_curve_shape(points, lh if len(lh) == len(points) else None,
                                           rh if len(rh) == len(points) else None)
        except Exception:
            return None


class MatchSurfaceFP(_BaseFP):
    Kind = "Match"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Reference", "Match",
                        "Reference surfaces (one or more)")
        obj.addProperty("App::PropertyLink", "Target", "Match",
                        "Target surface to rebuild (exactly 1)")
        obj.addProperty("App::PropertyBool", "ShowInputs", "Match",
                        "Keep the reference and target surfaces visible")
        obj.ShowInputs = True
        # Persistent diagnostics (plain runtime attributes do NOT stick
        # on FeaturePython objects -- AttributeError -- so these are
        # real properties, unlike MirrorFP's _LastWeldOK pattern).
        obj.addProperty("App::PropertyString", "MatchMethod", "Match",
                        "Which algorithm produced the current shape (exact_g2/exact_g1/sampled)")
        obj.addProperty("App::PropertyString", "MatchGrade", "Match",
                        "Achieved continuity grade of the last recompute (G2/G1/G0/FAIL)")

    def execute(self, obj):
        ref_objs = obj.Reference or []
        tgt_obj = obj.Target
        if not tgt_obj:
            return
        ref_surfaces = [o.Shape for o in ref_objs if getattr(o, "Shape", None)]
        tgt_surf = getattr(tgt_obj, "Shape", None)
        if not tgt_surf or not ref_surfaces:
            return
        try:
            rebuilt, report, _chosen = core.match_surface_to_references(
                tgt_surf, ref_surfaces,
                continuity="G2", n=24, scale=1.0)
            obj.Shape = rebuilt
            # Guarded for documents created before these properties
            # existed (no structural change during recompute otherwise).
            try:
                if hasattr(obj, "MatchMethod"):
                    obj.MatchMethod = report.get("method", "sampled")
                if hasattr(obj, "MatchGrade"):
                    obj.MatchGrade = core.classify_continuity(report)
            except Exception:
                pass
            groups.apply_input_visibility(obj, ["Reference", "Target"])
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Match: %s\n" % exc)


class MirrorFP(_BaseFP):
    """One-way parametric mirror: Source -> a derived mirrored copy,
    optionally welded into one continuous shell/solid. Two objects
    (Source + this one), the mirror always re-derived from the source via
    FreeCAD's own dependency graph -- for live bidirectional point-
    dragging on a SINGLE curve instead (no second object), see
    editmode.PointEditor.set_symmetry (CurvePanel's "Symmetric editing"
    checkbox). Wraps rfstudio.mirror.mirror_shape, welding via
    core.stitch_shell. See panel_solids.MirrorPanel (RFS_Mirror) for the
    editing UI."""
    Kind = "Mirror"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "Mirror",
                        "Object to mirror (curve, surface, or solid)")
        obj.addProperty("App::PropertyEnumeration", "Plane", "Mirror",
                        "Mirror plane. 'Pick from model' is a transient "
                        "UI-only value (see MirrorPanel) that always "
                        "resolves to Custom's Base/Normal by the time "
                        "execute() runs.")
        obj.Plane = ["XY", "YZ", "ZX", "Custom", "Pick from model"]
        obj.Plane = "XY"
        obj.addProperty("App::PropertyVector", "Base", "Mirror",
                        "Plane base point (Custom / Pick from model only)")
        obj.Base = FreeCAD.Vector(0, 0, 0)
        obj.addProperty("App::PropertyVector", "Normal", "Mirror",
                        "Plane normal (Custom / Pick from model only)")
        obj.Normal = FreeCAD.Vector(0, 0, 1)
        obj.addProperty("App::PropertyBool", "WeldSeam", "Mirror",
                        "Stitch Source and its mirror into one continuous "
                        "shell/solid instead of two separate shapes")
        obj.WeldSeam = False
        obj.addProperty("App::PropertyBool", "ShowInputs", "Mirror",
                        "Keep Source visible alongside the mirrored copy")
        obj.ShowInputs = True

    def execute(self, obj):
        src = obj.Source
        if src is None:
            return
        shape = getattr(src, "Shape", None)
        if shape is None or shape.isNull():
            return
        try:
            base, normal = plane_base_normal(obj.Plane, obj.Base, obj.Normal)
            result, welded = mirror_shape(
                shape, base, normal,
                weld=bool(obj.WeldSeam), stitch_fn=core.stitch_shell)
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform Mirror: %s\n" % exc)
            return
        obj.Shape = result
        # Plain runtime attribute, not an addProperty -- MirrorPanel.recompute()
        # reads this right after each self.obj.recompute() to report whether a
        # requested weld actually succeeded; it doesn't need to survive a
        # save/reload, only the current session.
        obj._LastWeldOK = welded
        if bool(obj.WeldSeam) and not welded:
            FreeCAD.Console.PrintWarning(
                "Railform Mirror: weld requested but stitching failed "
                "(Source's boundary may not touch the mirror plane) -- "
                "showing an unwelded compound instead.\n")
        # Single-link source: apply_input_visibility only handles link
        # LISTS (see groups.py), so drive Source's visibility from
        # ShowInputs directly -- same pattern as PolylineFilletFP.
        if getattr(src, "ViewObject", None) is not None:
            src.ViewObject.Visibility = bool(obj.ShowInputs)


class MirrorGroupFP(_BaseFP):
    """Mirror every object in Sources across one plane in a single step --
    the per-folder counterpart to MirrorFP. Select everything under a
    Railform Curves/Surfaces folder, invoke RFS_MirrorGroup: each source
    is mirrored independently (optionally welded to ITS OWN mirror), and
    all N results are combined into one compound Shape. To weld the
    WHOLE resulting assembly into one continuous shell instead, run
    StitchShellPanel on the result afterward -- not reinvented here."""
    Kind = "MirrorGroup"

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkList", "Sources", "MirrorGroup",
                        "Objects to mirror (curves, surfaces, or solids)")
        obj.addProperty("App::PropertyEnumeration", "Plane", "MirrorGroup",
                        "Mirror plane")
        obj.Plane = ["XY", "YZ", "ZX", "Custom"]
        obj.Plane = "XY"
        obj.addProperty("App::PropertyVector", "Base", "MirrorGroup",
                        "Plane base point (Custom only)")
        obj.Base = FreeCAD.Vector(0, 0, 0)
        obj.addProperty("App::PropertyVector", "Normal", "MirrorGroup",
                        "Plane normal (Custom only)")
        obj.Normal = FreeCAD.Vector(0, 0, 1)
        obj.addProperty("App::PropertyBool", "WeldSeam", "MirrorGroup",
                        "Weld each source to its own mirror (independently, "
                        "not the whole group into one shell)")
        obj.WeldSeam = False
        obj.addProperty("App::PropertyBool", "ShowInputs", "MirrorGroup",
                        "Keep all sources visible alongside their mirrors")
        obj.ShowInputs = True

    def execute(self, obj):
        sources = [s for s in (obj.Sources or []) if getattr(s, "Shape", None)]
        if not sources:
            return
        try:
            base, normal = plane_base_normal(obj.Plane, obj.Base, obj.Normal)
            results, welds = [], []
            for src in sources:
                result, welded = mirror_shape(
                    src.Shape, base, normal,
                    weld=bool(obj.WeldSeam), stitch_fn=core.stitch_shell)
                results.append(result)
                welds.append(welded)
            obj.Shape = Part.Compound(results)
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform MirrorGroup: %s\n" % exc)
            return
        # Plain runtime attribute (not addProperty), index-aligned with
        # Sources as of this execute() call -- session-only, same
        # convention as MirrorFP's own _LastWeldOK.
        obj._LastWeldOK = welds
        if bool(obj.WeldSeam) and not all(welds):
            FreeCAD.Console.PrintWarning(
                "Railform MirrorGroup: %d/%d source(s) welded -- the rest "
                "fell back to an unwelded compound (boundary may not touch "
                "the mirror plane).\n" % (sum(welds), len(welds)))
        groups.apply_input_visibility(obj, ["Sources"])


# --------------------------------------------------------------------------- #
_FP = {"Loft": LoftFP, "Gordon": GordonFP, "Coons": CoonsFP,
       "Sweep": Sweep2RailFP, "SweepN": SweepNFP, "Fill": PlateFillFP,
       "Match": MatchSurfaceFP,
       "Offset": OffsetFP, "Thicken": ThickenFP, "Shell": ShellFP,
       "ExtendSurf": ExtendSurfFP, "BlendSurf": BlendSurfFP, "N_sidedPatch": N_sidedPatchFP,
       "InterpCurve": InterpCurveFP, "ControlCurve": ControlCurveFP,
       "ApproxCurve": ApproxCurveFP, "Polyline": PolylineFP,
       "BezierCurve": BezierCurveFP, "PolylineFillet": PolylineFilletFP,
       "JoinCurve": JoinCurveFP, "Mirror": MirrorFP,
       "MirrorGroup": MirrorGroupFP}
_LABEL = {"Loft": "RailformLoft", "Gordon": "RailformGordon",
          "Coons": "RailformCoons", "Sweep": "RailformSweep",
          "SweepN": "RailformSweepN",
          "ExtendSurf": "RailformExtendSurf",
          "Fill": "RailformFill", "Match": "RailformMatch",
          "Offset": "RailformOffset", "Thicken": "RailformThicken",
          "Shell": "RailformShell", "BlendSurf": "RailformBlendSurf",
          "N_sidedPatch": "RailformN_sidedPatch",
          "InterpCurve": "RailformInterpCurve", "ControlCurve": "RailformControlCurve",
          "ApproxCurve": "RailformApproxCurve", "Polyline": "RailformPolyline",
          "BezierCurve": "RailformBezierCurve",
          "PolylineFillet": "RailformPolylineFillet",
          "JoinCurve": "RailformJoinCurve", "Mirror": "RailformMirror",
          "MirrorGroup": "RailformMirrorGroup"}

# which link-list properties each kind exposes (panel reads this)
GROUPS = {
    "Loft":   [("Profiles",   "Profiles",    "Cross-sections to loft through (≥ 2)"),
               ("GuideRails", "Guide Rails", "Optional path constraints")],
    "Gordon": [("UCurves", "U curves", "U-direction curves"),
               ("VCurves", "V curves", "V-direction curves")],
    "Coons":  [("Boundaries", "Boundaries", "Exactly 4 edges")],
    "Sweep":  [("Rail1",    "Rail 1",    "Spine rail — one curve"),
               ("Rail2",    "Rail 2",    "Guide rail — one curve"),
               ("Profiles", "Profiles",  "Cross-section curves (≥ 1)")],
    "SweepN": [("Rails",    "Rails",     "Rail curves in order (>= 2; "
                                           "reorder with the arrows -- "
                                           "the first and last bound the "
                                           "sweep)"),
               ("Profiles", "Profiles",  "Cross-section curves (≥ 1)")],
    "ExtendSurf": [("BaseSurface", "Base Surface", "Surface to extend")],
    "Fill":   [("Boundaries", "Boundaries", "Boundary curves (>= 3)")],
    "Match":  [("Reference", "Reference surfaces (one or more)"),
               ("Target",    "Target surface to rebuild (exactly 1)")],
    "N_sidedPatch": [("Boundaries", "Boundaries", "Boundary curves (≥ 3)")],
    "InterpCurve": [("Points", "Points", "Interpolation points")],
    "ControlCurve": [("Points", "Points", "Control points"),
                     ("Weights", "Weights", "Weights for control points")],
    "ApproxCurve": [("Points", "Points", "Points to approximate")],
    "Polyline": [("Points", "Points", "Points")],
    "BezierCurve": [("Points", "Points", "Control points"),
                    ("LeftHandles", "Left Handles", "Left control handles"),
                    ("RightHandles", "Right Handles", "Right control handles")],
    "MirrorGroup": [("Sources", "Sources",
                     "Curves, surfaces, or solids to mirror (>= 1)")],
}
LIMITS = {"Loft":   {"Profiles": (2, None), "GuideRails": (0, None)},
          "Gordon": {},
          "Coons":  {"Boundaries": (4, 4)},
          "Sweep":  {"Rail1": (1, 1), "Rail2": (1, 1), "Profiles": (1, None)},
          "SweepN": {"Rails": (2, None), "Profiles": (1, None)},
          "ExtendSurf": {"BaseSurface": (1, 1)},
          "Fill":   {"Boundaries": (3, None)},
          "Match":  {"Reference": (1, None), "Target": (1, 1)},
          "N_sidedPatch": {"Boundaries": (3, None)},
          "InterpCurve": {"Points": (2, None)},
          "ControlCurve": {"Points": (2, None), "Weights": (0, None)},
          "ApproxCurve": {"Points": (2, None)},
          "Polyline": {"Points": (2, None)},
          "BezierCurve": {"Points": (2, None)},
          "MirrorGroup": {"Sources": (1, None)}}


def _style_curve_vobj(vobj):
    """Thicker blue wire on new curve objects -- the SolidWorks sketch
    wire look, so fresh curves don't render as hairlines."""
    try:
        vobj.LineColor = (0.20, 0.45, 1.00)
        vobj.LineWidth = 3.0
    except Exception:
        pass


def make_surface(kind, doc=None):
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument()
    obj = doc.addObject("Part::FeaturePython", _LABEL[kind])
    _FP[kind](obj)
    if obj.ViewObject is not None:
        SurfaceVP(obj.ViewObject)
    groups.add_to_group(obj, "Surfaces")
    return obj


def make_curve(kind, doc=None):
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument()
    obj = doc.addObject("Part::FeaturePython", _LABEL[kind])
    _FP[kind](obj)
    if obj.ViewObject is not None:
        _style_curve_vobj(obj.ViewObject)
        # Attach the ViewProvider proxy (CurveVP) so double-clicking the
        # curve enters point-edit mode via setEdit(). Do NOT instantiate a
        # PointEditor here: PointEditor is the interactive editor, not a
        # ViewProvider (it never sets vobj.Proxy), and building one at
        # creation-time left a second editor -- with its own duplicate
        # mouse-event callbacks and Coin overlay -- leaked onto the view on
        # top of the one the curve panel builds. The Part::FeaturePython's
        # own view provider still renders obj.Shape either way.
        CurveVP(obj.ViewObject)
    groups.add_to_group(obj, "Curves")
    return obj


def make_polyline_fillet(source, doc=None):
    """Create a PolylineFillet that rounds `source`'s corners (see
    PolylineFilletFP). `source` may be None and assigned later by the panel."""
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument()
    obj = doc.addObject("Part::FeaturePython", _LABEL["PolylineFillet"])
    PolylineFilletFP(obj)
    if source is not None:
        obj.Source = source
    if obj.ViewObject is not None:
        _style_curve_vobj(obj.ViewObject)
        CurveVP(obj.ViewObject)
    groups.add_to_group(obj, "Curves")
    return obj


def make_join_curve(sources, doc=None):
    """Create a JoinCurve that welds `sources` (a list of curves) into one Wire
    (see JoinCurveFP). `sources` may be None/empty and set later by the panel."""
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument()
    obj = doc.addObject("Part::FeaturePython", _LABEL["JoinCurve"])
    JoinCurveFP(obj)
    if sources:
        obj.Sources = list(sources)
    if obj.ViewObject is not None:
        _style_curve_vobj(obj.ViewObject)
        CurveVP(obj.ViewObject)
    groups.add_to_group(obj, "Curves")
    return obj


class CurveVP(object):
    """Shared ViewProvider for curve objects."""

    def __init__(self, vobj):
        vobj.Proxy = self
        try:
            self.kind = vobj.Object.Proxy.Kind
        except Exception:
            self.kind = None

    def getIcon(self):
        return _kind_icon(getattr(self, "kind", None), "rfs_interp.svg")

    def __getstate__(self):
        return {"kind": getattr(self, "kind", None)}

    def __setstate__(self, state):
        self.kind = state.get("kind") if isinstance(state, dict) else None
        return None

    def onChanged(self, vobj, prop):
        pass

    def execute(self, vobj):
        return

    def doubleClicked(self, vobj):
        # Open the full curve panel (coordinate table, Edit-knots toggle,
        # insert-on-click) in edit mode instead of the bare Coin PointEditor
        # that setEdit builds -- this is what restores the parameter panel on
        # double-click. Returning True marks the double-click handled so
        # FreeCAD does not also call setEdit. Lazy import avoids a
        # parametric <-> taskpanels import cycle at module load.
        try:
            from rfstudio import taskpanels
            if FreeCADGui.Control.activeDialog():
                return True
            kind = getattr(self, "kind", None) or getattr(
                getattr(vobj.Object, "Proxy", None), "Kind", None)
            if kind == "PolylineFillet":
                panel = taskpanels.PolylineFilletPanel(edit_obj=vobj.Object)
            elif kind == "JoinCurve":
                panel = taskpanels.JoinCurvePanel(edit_obj=vobj.Object)
            else:
                panel = taskpanels.CurvePanel(edit_obj=vobj.Object)
            FreeCADGui.Control.showDialog(panel)
            return True
        except Exception as exc:
            FreeCAD.Console.PrintError(
                "Railform: open edit panel failed (%s)\n" % exc)
            return False

    def setEdit(self, vobj, mode=0):
        # Editing is only allowed via double-click in the tree browser
        # (CurveVP.doubleClicked).  Returning False here blocks all other
        # entry points (Edit menu, toolbar edit buttons, etc.) so curves
        # are not editable except through the browser double-click.
        return False

    def unsetEdit(self, vobj, mode=0):
        if hasattr(self, 'edit_mode'):
            self.edit_mode.finish()
            del self.edit_mode
        return


# --------------------------------------------------------------------------- #
class SurfaceVP(object):
    """Shared ViewProvider for surface objects."""

    def __init__(self, vobj):
        vobj.Proxy = self
        try:
            self.kind = vobj.Object.Proxy.Kind
        except Exception:
            self.kind = None
        vobj.addProperty("App::PropertyBool", "ShowEdges", "View",
                        "Show edges")
        vobj.ShowEdges = False
        vobj.addProperty("App::PropertyBool", "ShowAxis", "View",
                        "Show axis")
        vobj.ShowAxis = False
        vobj.addProperty("App::PropertyBool", "ShowSamples", "View",
                        "Show surface sample points")
        vobj.ShowSamples = False

    def getIcon(self):
        return _kind_icon(getattr(self, "kind", None), "rfs_loft.svg")

    def __getstate__(self):
        return {"kind": getattr(self, "kind", None)}

    def __setstate__(self, state):
        self.kind = state.get("kind") if isinstance(state, dict) else None
        return None

    def onChanged(self, vobj, prop):
        if prop == "ShowEdges" or prop == "ShowSamples":
            vobj.Shape.update()

    def execute(self, vobj):
        return

    def setEdit(self, vobj, mode=0):
        # Editing is only allowed via double-click in the tree browser
        # (SurfaceVP.doubleClicked).  Returning False here blocks all other
        # entry points so surfaces are not editable except through the
        # browser double-click.
        return False

    def unsetEdit(self, vobj, mode=0):
        if hasattr(self, 'edit_mode'):
            self.edit_mode.finish()
            del self.edit_mode
        return

    def doubleClicked(self, vobj):
        """Open NURBS Push/Pull panel for direct surface control-point
        editing.  This is the only entry point for NURBS Push/Pull -- it
        was removed from the curated toolbar so it cannot be launched
        accidentally from a button."""
        try:
            from rfstudio import panel_nurbspull
            if FreeCADGui.Control.activeDialog():
                return True
            FreeCADGui.Selection.clearSelection()
            FreeCADGui.Selection.addSelection(vobj.Object)
            panel = panel_nurbspull.createPanel()
            FreeCADGui.Control.showDialog(panel)
            return True
        except Exception as exc:
            FreeCAD.Console.PrintError(
                "Railform: NURBS Push/Pull open failed (%s)\n" % exc)
            return False