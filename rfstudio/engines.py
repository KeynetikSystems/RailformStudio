# -*- coding: utf-8 -*-
"""Engine adapter layer (GUI side).

Thin wrappers that drive the Curves and Silk workbench commands. This is where
depend-and-wrap lives: we translate a curated Railform action into an existing
engine command, ensuring that engine's commands are registered first.
"""

import FreeCAD

from rfstudio import deps
from rfstudio import groups


def run_engine_command(mod_name, cmd_id):
    """Pre-load <mod_name>'s commands then run <cmd_id>. Returns True if fired."""
    import FreeCADGui
    if not deps.installed(mod_name):
        FreeCAD.Console.PrintError(
            "Railform Studio: %s is not installed -- this action needs it.\n"
            % mod_name)
        return False
    deps.ensure_engine_commands(mod_name)
    if not deps.command_available(cmd_id):
        FreeCAD.Console.PrintError(
            "Railform Studio: command '%s' not found after loading %s.\n"
            % (cmd_id, mod_name))
        return False
    try:
        FreeCADGui.runCommand(cmd_id, 0)
    except Exception as exc:
        # A renamed/broken engine command must not wedge the curated
        # button's provider chain -- report and let ProviderCommand fall
        # through to the next provider.
        FreeCAD.Console.PrintWarning(
            "Railform Studio: '%s' raised while running -- trying next "
            "provider (%s)\n" % (cmd_id, exc))
        return False
    return True


# Curated action -> (engine mod, engine command id). This stays the source
# of truth for "what engine command would satisfy this action" -- used by
# deps.verify_delegations() for the startup health check -- but it is no
# longer the *only* way to satisfy an action; see PROVIDERS below.
DELEGATIONS = {
    # ---- Surface --------------------------------------------------------
    "RFS_BlendSurf":    ("Curves", "Curves_BlendSurf2"),
    "RFS_Trim":         ("Curves", "Trim"),
    "RFS_SketchOnSurf": ("Curves", "SoS"),
    # ---- Surface (Silk: clean low-CP + star points) ---------------------
    "RFS_CleanPatch44": ("Silk",   "CubicSurface_44"),
    "RFS_CleanPatch66": ("Silk",   "CubicSurface_66"),
    "RFS_ControlPoly4": ("Silk",   "ControlPoly4"),
    "RFS_StarPatch":    ("Silk",   "ControlGridNStar66"),
    # ---- Inspect (diagnostics) -> Curves --------------------------------
    "RFS_Combs":        ("Curves", "ParametricComb"),
    "RFS_Zebra":        ("Curves", "ZebraTool"),
    "RFS_Reflect":      ("Curves", "ReflectLines"),
    "RFS_Analysis":     ("Curves", "Curves_SurfaceAnalysis"),
    "RFS_DraftAnalysis":("Curves", "Curves_DraftAnalysis"),
}
# Note: RFS_MultiLoft is intentionally absent here -- see NATIVE_OVERRIDES.


# --- provider chains: the fix for "pure delegation, no native fallback" -- #
# A provider is one of:
#   ("engine", mod_name, engine_cmd_id)  -- delegate to an installed addon
#   ("native", callable_name)            -- call a function in THIS module
#
# Each curated action gets an ORDERED list, tried in turn by
# commands.ProviderCommand. This is what lets a native fallback be added
# for one action at a time without touching the dispatch mechanism --
# adding a native implementation is just adding an entry to
# NATIVE_OVERRIDES below, nothing else has to change.
#
# Triage (see project SWOT discussion for the reasoning):
#   - RFS_MultiLoft is redundant with the workbench's own parametric Loft
#     (core.loft / LoftFP) and has been switched to native-only below --
#     it never needed Curves in the first place.
#   - RFS_Combs/Zebra/Reflect/DraftAnalysis are now native too (see
#     core.curvature_comb / zebra_stripes / reflection_lines /
#     draft_angle_analysis) -- pure OCCT curvature/normal evaluation, no
#     CAGD novelty required, so unlike the star-point G2 problem this was
#     a genuinely closeable gap. The Curves delegation is kept as a
#     fallback for each in case a selection/geometry edge case the native
#     path doesn't yet handle comes up. RFS_Analysis is likewise now
#     native-first (core.surface_review_summary); the Curves delegation
#     remains as its fallback.
#   - RFS_Analysis (generic surface review) got its native path too:
#     core.surface_review_summary / review_recommendation drive
#     native_analysis below, with the Curves delegation kept as fallback.
#   - RFS_ControlPoly4 builds a degree-3 control-polygon curve natively
#     from the same core.control_poly_curve technique, with Silk kept as
#     fallback.
#   - RFS_StarPatch is different: true G2 N-sided patching stays Silk-only,
#     but it is no longer a single point of failure -- it has a degraded
#     native last resort (native_star_patch over core.n_sided_patch, exact
#     G0/tangent-plane G1 only). If Silk is installed it always wins; the
#     native path only runs when the engine command is unavailable or
#     errors, and says so loudly.
#   - RFS_CleanPatch44/66 and RFS_ControlPoly4 are the surface analog of
#     core.control_poly_curve (already native for curves) -- same
#     buildFromPolesMultsKnots technique on a 2D grid instead of 1D.
#   - RFS_Trim/RFS_SketchOnSurf are moderate OCCT work (BRepFeat_SplitShape
#     / Geom2d projection).
#   - RFS_BlendSurf now has a native path too (core.blend_surface, the
#     same quintic-Hermite technique match_surface already uses) --
#     Curves_BlendSurf2 is kept only as the fallback for selections the
#     native nearest-facing-side search can't resolve.
#   - RFS_StarPatch is now native only (core.n_sided_patch, exact G0 /
#     tangent-plane G1). Silk's ControlGridNStar66 offered true G2 but is
#     no longer a dependency; the curated button degrades gracefully to a
#     working native tool instead of a dead button.
#
# RFS_CleanPatch44/66 and RFS_Trim/RFS_SketchOnSurf are chained NATIVE
# FIRST, then the original engine command as fallback -- unlike MultiLoft
# (a clean redundancy, safe to cut over fully), these are freshly written
# and only geometry-tested (see test_core.py), not field-tested against
# every selection shape a real user will throw at them. Each native_*
# function below raises ValueError on a bad/ambiguous selection rather
# than printing-and-returning, specifically so ProviderCommand's chain
# logic (commands.py) falls through to the still-installed engine command
# instead of silently doing nothing -- see the "silent no-op" discussion
# in the project SWOT for why that distinction matters.
NATIVE_OVERRIDES = {
    "RFS_MultiLoft":    [("native", "native_multiloft")],
    "RFS_CleanPatch44": [("native", "native_clean_patch_44"),
                         ("engine", "Silk", "CubicSurface_44")],
    "RFS_CleanPatch66": [("native", "native_clean_patch_66"),
                         ("engine", "Silk", "CubicSurface_66")],
    "RFS_Trim":         [("native", "native_trim"),
                         ("engine", "Curves", "Trim")],
    "RFS_SketchOnSurf": [("native", "native_sketch_on_surf"),
                         ("engine", "Curves", "SoS")],
    # RFS_BlendSurf now tries the native quintic-Hermite skin
    # (core.blend_surface) FIRST -- this used to be a pure Curves
    # delegation with no native path at all (the one surface-analog gap
    # RFS_Trim/RFS_SketchOnSurf/RFS_CleanPatch44/66 already got fixed
    # for). Curves_BlendSurf2 stays as the fallback for selections the
    # native path can't handle yet (e.g. non-planar-adjacent faces OCCT's
    # nearest-facing-side search can't resolve cleanly).
    "RFS_BlendSurf":    [("native", "native_blend_surface"),
                         ("engine", "Curves", "Curves_BlendSurf2")],
    # Last three actions that used to have NO native fallback at all
    # (flagged by deps.report_single_points_of_failure):
    "RFS_Analysis":     [("native", "native_analysis"),
                          ("engine", "Curves", "Curves_SurfaceAnalysis")],
    "RFS_ControlPoly4": [("native", "native_control_poly4"),
                          ("engine", "Silk", "ControlPoly4")],
    # Star patch: native only (exact G0 / tangent-plane G1). Silk's
    # ControlGridNStar66 offered true G2 but is no longer a dependency;
    # the curated button degrades gracefully to a working native tool
    # instead of a dead button when Silk is absent.
    "RFS_StarPatch":    [("native", "native_star_patch")],
    # Native visual-QA tools (fix for "no native fallback" gap #3) --
    # curvature comb / zebra stripes / reflection lines / draft-angle
    # analysis are all pure OCCT curvature/normal evaluation (see the
    # matching core.* functions), so each gets a native-first provider
    # with the old Curves delegation kept only as a fallback.
    "RFS_Combs":        [("native", "native_combs"),
                         ("engine", "Curves", "ParametricComb")],
    "RFS_Zebra":        [("native", "native_zebra"),
                         ("engine", "Curves", "ZebraTool")],
    "RFS_Reflect":      [("native", "native_reflect"),
                         ("engine", "Curves", "ReflectLines")],
    "RFS_DraftAnalysis":[("native", "native_draft_analysis"),
                         ("engine", "Curves", "Curves_DraftAnalysis")],
}


# Brand-new curated actions with NO engine equivalent at all (unlike
# everything above, which each replaced or still falls back to a Curves/
# Silk delegation): variable-radius fillets, radius-driven rolling-ball
# blends, and network-wide fairing are native-only chains from day one.
NATIVE_OVERRIDES["RFS_VariableFillet"] = [("native", "native_variable_fillet")]
NATIVE_OVERRIDES["RFS_RollingBallFillet"] = [
    ("native", "native_rolling_ball_fillet")]
NATIVE_OVERRIDES["RFS_GlobalFair"] = [("native", "native_global_fair")]

# Booleans, mesh/scan-to-NURBS fitting, and hidden-line drafting export --
# three more native-only additions, none of which had ANY engine
# equivalent (Curves/Silk never offered booleans, mesh import, or
# drafting output). Unlike the fillet/fairing gaps above, these were
# never a kernel limitation -- see native_boolean_*/native_mesh_to_surface/
# native_hidden_line_export's own docstrings below for what each wraps.
NATIVE_OVERRIDES["RFS_BooleanUnion"] = [("native", "native_boolean_union")]
NATIVE_OVERRIDES["RFS_BooleanSubtract"] = [("native", "native_boolean_subtract")]
NATIVE_OVERRIDES["RFS_BooleanIntersect"] = [("native", "native_boolean_intersect")]
NATIVE_OVERRIDES["RFS_MeshToSurface"] = [("native", "native_mesh_to_surface")]
NATIVE_OVERRIDES["RFS_HiddenLineExport"] = [
    ("native", "native_hidden_line_export")]


def _build_providers():
    chains = {}
    for cmd_id, (mod, target) in DELEGATIONS.items():
        chains[cmd_id] = NATIVE_OVERRIDES.get(cmd_id, [("engine", mod, target)])
    for cmd_id, chain in NATIVE_OVERRIDES.items():
        chains.setdefault(cmd_id, chain)
    return chains


PROVIDERS = _build_providers()


def _add_feature_with_undo(doc, label, shape):
    """Add a Part::Feature carrying `shape`, wrapped in an undo transaction
    so Ctrl+Z reverts the creation. Raises on failure (transaction aborted)."""
    doc.openTransaction("Railform Studio: " + label)
    try:
        obj = doc.addObject("Part::Feature", label)
        obj.Shape = shape
        doc.recompute()
        doc.commitTransaction()
    except Exception:
        try:
            doc.abortTransaction()
        except Exception:
            pass
        raise
    return obj


def _with_undo(doc, label, fn):
    """Run `fn` inside an undo transaction so Ctrl+Z reverts its document
    mutations (object creation, Shape changes, group membership, etc.).
    Raises on failure (transaction aborted)."""
    doc.openTransaction("Railform Studio: " + label)
    try:
        result = fn()
        doc.commitTransaction()
        return result
    except Exception:
        try:
            doc.abortTransaction()
        except Exception:
            pass
        raise


# --- native providers ------------------------------------------------------ #
def native_multiloft():
    """Native provider for RFS_MultiLoft.

    This is the first concrete instance of "fix the no-native-fallback
    threat": RFS_MultiLoft used to delegate to Curves' MultiLoft command,
    but the workbench's own parametric Loft (core.loft / parametric.LoftFP)
    already covers "loft through many profiles" and is strictly more
    capable -- re-editable, supports guide rails, recomputes automatically.
    So the fix here isn't a new algorithm, it's recognizing the delegation
    was never necessary and routing the curated action at our own tool.
    """
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.LoftPanel())


def _grid_points_from_selection(n):
    """Collect an n x n row-major grid of points for the clean-patch
    providers. Two accepted selections, tried in order:

      1. ONE object whose Shape has exactly n*n vertices (e.g. a sketch of
         a point grid, or a Part::Feature compound of vertices) -- the
         shape's own vertex order is used, which is deterministic.
      2. n*n individual picks (vertices / point objects), taken in
         selection (click) order via FreeCADGui.Selection.getSelectionEx().
         This is the same "selection order defines meaning" convention
         already used elsewhere in this codebase (e.g. BlendPanel's
         Curve1/Curve2 assignment) -- it's a first-cut UX, not a polished
         picker widget; row 0 must be picked left-to-right, then row 1, etc.

    Raises ValueError (not a silent empty return) if neither shape matches,
    so the caller's exception handling can report exactly what's missing.
    """
    import FreeCADGui
    sel = FreeCADGui.Selection.getSelectionEx()
    if len(sel) == 1:
        shp = getattr(sel[0].Object, "Shape", None)
        if shp is not None and len(shp.Vertexes) == n * n:
            return [v.Point for v in shp.Vertexes]
    pts = []
    for so in sel:
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Vertex":
                pts.append(sub.Point)
        if not so.SubObjects and getattr(so.Object, "Shape", None):
            pts.extend(v.Point for v in so.Object.Shape.Vertexes)
    if len(pts) != n * n:
        raise ValueError(
            "need exactly %d points selected in row-major order (or one "
            "object whose shape has exactly %d vertices) -- got %d"
            % (n * n, n * n, len(pts)))
    return pts


def native_clean_patch_44():
    """Native provider for RFS_CleanPatch44 -- replaces Silk's
    CubicSurface_44. See core.clean_patch_44 for the geometry (an exact,
    non-interpolated 4x4 control-net bicubic patch, the same technique
    core.control_poly_curve already uses for curves)."""
    _run_clean_patch(4, "CleanPatch44")


def native_clean_patch_66():
    """Native provider for RFS_CleanPatch66 -- replaces Silk's
    CubicSurface_66. See core.clean_patch_66."""
    _run_clean_patch(6, "CleanPatch66")


def _run_clean_patch(n, label):
    import FreeCAD
    from rfstudio import core
    pts = _grid_points_from_selection(n)          # raises on bad selection
    grid = [pts[i * n:(i + 1) * n] for i in range(n)]
    # Get weights from selection (same as in core.py)
    flat_w = core._grid_weights_from_selection(n)
    wgrid = ([flat_w[i * n:(i + 1) * n] for i in range(n)]
            if flat_w is not None else None)
    fn = core.clean_patch_44 if n == 4 else core.clean_patch_66
    doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
    _add_feature_with_undo(doc, label, fn(grid, weights=wgrid))


def _face_and_wire_from_selection(need_label):
    """Shared selection parsing for native_trim / native_sketch_on_surf:
    exactly one face and at least one edge (chained into a wire) selected.
    Raises ValueError (not a silent return) on anything else, so the
    provider chain falls through to the engine command instead of
    quietly doing nothing -- see NATIVE_OVERRIDES's comment above."""
    import FreeCADGui
    import Part
    sel = FreeCADGui.Selection.getSelectionEx()
    face = None
    wire_edges = []
    for so in sel:
        shp = getattr(so.Object, "Shape", None)
        if shp is None:
            continue
        if face is None and shp.Faces:
            face = shp.Faces[0]
        elif shp.Edges:
            wire_edges.extend(shp.Edges)
    if face is None or not wire_edges:
        raise ValueError(
            "%s needs one face and one boundary curve/wire selected "
            "(got face=%s, %d edge(s))"
            % (need_label, face is not None, len(wire_edges)))
    wire = (Part.Wire(Part.__sortEdges__(wire_edges))
           if len(wire_edges) > 1 else Part.Wire([wire_edges[0]]))
    return face, wire


def native_trim():
    """Native provider for RFS_Trim -- replaces Curves' Trim command with
    core.trim_face (Part.Face(surface, wire), OCCT's own primitive for
    'a surface bounded by a wire on it')."""
    import FreeCAD
    from rfstudio import core
    face, wire = _face_and_wire_from_selection("Trim")
    trimmed = core.trim_face(face, wire)
    doc = FreeCAD.ActiveDocument
    _add_feature_with_undo(doc, "Trim", trimmed)


def native_blend_surface():
    """Native provider for RFS_BlendSurf -- replaces the previously
    pure-Curves delegation (Curves_BlendSurf2) with core.blend_surface,
    the quintic-Hermite cross-section skin already used by match_surface.
    Opens the guided BlendSurfPanel (Face A / Face B / continuity /
    scale) rather than firing blind off the current selection, matching
    native_multiloft's pattern below."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.BlendSurfPanel())


def native_sketch_on_surf():
    """Native provider for RFS_SketchOnSurf -- replaces Curves' SoS command
    with core.project_wire_onto_face (nearest-point projection + refit,
    since FreeCAD's Part API doesn't expose a general surface-projection
    binding directly)."""
    import FreeCAD
    from rfstudio import core
    face, wire = _face_and_wire_from_selection("Sketch on surface")
    projected = core.project_wire_onto_face(face, wire)
    doc = FreeCAD.ActiveDocument
    _add_feature_with_undo(doc, "SketchOnSurf", projected)


# --- variable-radius / rolling-ball fillets + global fairing -------------- #
def native_variable_fillet():
    """Native provider for RFS_VariableFillet -- opens the guided
    VariableFilletPanel (Solid / Edge / radius-station table) rather
    than firing off the current selection blind, matching
    native_multiloft/native_blend_surface's pattern above."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.VariableFilletPanel())


def native_rolling_ball_fillet():
    """Native provider for RFS_RollingBallFillet -- opens
    RollingBallFilletPanel (Face A / Face B / radius / optional
    multi-section station table)."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.RollingBallFilletPanel())


def native_global_fair():
    """Native provider for RFS_GlobalFair -- opens GlobalFairPanel (face
    list / iterations / lambda / fix-boundary toggle)."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.GlobalFairPanel())


# --- booleans / mesh-to-surface / hidden-line drafting export ------------ #
def native_boolean_union():
    """Native provider for RFS_BooleanUnion -- opens BooleanPanel
    pre-set to Union. Wraps core.boolean_union (Part.Shape.fuse,
    chained)."""
    _open_boolean_panel("Union")


def native_boolean_subtract():
    """Native provider for RFS_BooleanSubtract -- opens BooleanPanel
    pre-set to Subtract. Wraps core.boolean_subtract (Part.Shape.cut,
    chained)."""
    _open_boolean_panel("Subtract")


def native_boolean_intersect():
    """Native provider for RFS_BooleanIntersect -- opens BooleanPanel
    pre-set to Intersect. Wraps core.boolean_intersect
    (Part.Shape.common, chained)."""
    _open_boolean_panel("Intersect")


def _open_boolean_panel(operation):
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.BooleanPanel(operation=operation))


def native_mesh_to_surface():
    """Native provider for RFS_MeshToSurface -- opens MeshToSurfacePanel
    (Source mesh/point object / grid resolution / projection normal).
    Wraps core.fit_surface_to_scan; see that function's docstring for
    the explicit single-patch, height-field-only scope."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.MeshToSurfacePanel())


def native_hidden_line_export():
    """Native provider for RFS_HiddenLineExport -- opens
    HiddenLineExportPanel (Sources / projection direction / Export SVG
    button). Wraps core.hidden_line_projection + core.export_svg_drawing."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import taskpanels
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument()
    if FreeCADGui.Control.activeDialog():
        try:
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
    FreeCADGui.Control.showDialog(taskpanels.HiddenLineExportPanel())


# --- native visual QA: curvature comb / zebra / reflection / draft-angle -- #
def _selected_edge():
    """(edge, source_label) from the current selection: a sub-edge pick
    first, else the first edge of a whole-object pick. Raises ValueError
    (not a silent no-op) on nothing usable -- same convention as
    _grid_points_from_selection / _face_and_wire_from_selection above.
    The source label travels with the geometry so callers (native_combs)
    can derive a STABLE, human-meaningful output object name instead of
    an anonymous auto-numbered one -- see groups.find_or_create."""
    import FreeCADGui
    for so in FreeCADGui.Selection.getSelectionEx():
        label = getattr(so.Object, "Label", so.Object.Name)
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Edge":
                return sub, label
        if not so.SubObjects:
            shp = getattr(so.Object, "Shape", None)
            if shp is not None and shp.Edges:
                return shp.Edges[0], label
    raise ValueError("select one edge/curve first")


def _selected_face():
    """(face, source_label) from the current selection: a sub-face pick
    first, else the first face of a whole-object pick. Raises ValueError
    on nothing usable. Shared by native_zebra / native_reflect /
    native_draft_analysis -- the source label is what lets each of those
    update the SAME output object on a re-run instead of spawning a new
    one every time (see groups.find_or_create)."""
    import FreeCADGui
    for so in FreeCADGui.Selection.getSelectionEx():
        label = getattr(so.Object, "Label", so.Object.Name)
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Face":
                return sub, label
        if not so.SubObjects:
            shp = getattr(so.Object, "Shape", None)
            if shp is not None and shp.Faces:
                return shp.Faces[0], label
    raise ValueError("select one face/surface first")


def _current_view_dir():
    """Best-effort active-viewport camera direction, used as the default
    'eye' for zebra/reflection analysis so the pattern matches what the
    user is actually looking at. Falls back to +Z (top-down) if there's
    no active 3D view -- never raises, since a missing view direction
    just means a less-tailored default, not a broken command."""
    import FreeCAD
    try:
        import FreeCADGui
        doc = FreeCADGui.ActiveDocument
        view = doc.ActiveView if doc is not None else None
        if view:
            return FreeCAD.Vector(view.getViewDirection())
        else:
            return FreeCAD.Vector(0, 0, 1)
    except Exception:
        return FreeCAD.Vector(0, 0, 1)


def native_combs():
    """Native provider for RFS_Combs -- replaces Curves' ParametricComb
    with core.curvature_comb (OCCT curvature evaluation via
    Curve.getD2), no Curves dependency.

    The output object is named from its SOURCE edge's owning object
    ("<source>_Comb") and looked up/reused via groups.find_or_create --
    re-running the check (the normal iterative-review loop: check, tweak
    the curve, check again) updates that same object's Shape in place
    instead of leaving a trail of stale CurvatureComb014-style objects
    behind. Filed under the QA group so it doesn't clutter the main
    curve/surface tree either.
    """
    import FreeCAD
    from rfstudio import core
    edge, label = _selected_edge()          # raises on bad selection
    comb = core.curvature_comb(edge)
    doc = FreeCAD.ActiveDocument
    def _do():
        obj, _created = groups.find_or_create(doc, "%s_Comb" % label)
        obj.Shape = comb
        groups.add_to_group(obj, "QA")
        doc.recompute()
    _with_undo(doc, "Curvature Comb", _do)


def native_zebra():
    """Native provider for RFS_Zebra -- replaces Curves' ZebraTool with
    core.zebra_stripes (exact iso-angle contours in surface space,
    computed against the current 3D view direction).

    See native_combs' docstring: the output is named/reused by source
    face label ("<source>_Zebra") via groups.find_or_create, so
    re-running zebra on the same face updates in place instead of
    piling up new objects each time.
    """
    import FreeCAD
    from rfstudio import core
    face, label = _selected_face()
    stripes = core.zebra_stripes(face, view_dir=_current_view_dir())
    doc = FreeCAD.ActiveDocument
    def _do():
        obj, _created = groups.find_or_create(doc, "%s_Zebra" % label)
        obj.Shape = stripes
        groups.add_to_group(obj, "QA")
        doc.recompute()
    _with_undo(doc, "Zebra", _do)


def native_reflect():
    """Native provider for RFS_Reflect -- replaces Curves' ReflectLines
    with core.reflection_lines. Uses core.standard_light_rig() (a
    documented horizontal/vertical/diagonal light-bar fan -- see that
    function's docstring for why those three, not an arbitrary rotation
    fan) as the default; a future task panel could expose a
    project-specific rig as a user setting without changing
    core.reflection_lines itself.

    Output named/reused by source face label ("<source>_Reflect") --
    see native_combs' docstring for why.
    """
    import FreeCAD
    from rfstudio import core
    face, label = _selected_face()
    lines = core.reflection_lines(face, core.standard_light_rig(),
                                  view_dir=_current_view_dir())
    doc = FreeCAD.ActiveDocument
    def _do():
        obj, _created = groups.find_or_create(doc, "%s_Reflect" % label)
        obj.Shape = lines
        groups.add_to_group(obj, "QA")
        doc.recompute()
    _with_undo(doc, "Reflection Lines", _do)


def native_draft_analysis():
    """Native provider for RFS_DraftAnalysis -- replaces Curves'
    Curves_DraftAnalysis with core.draft_angle_analysis. Pull direction
    defaults to +Z (the usual mold-opening/tool-withdrawal axis).

    Each non-empty threshold band becomes its OWN labelled object (e.g.
    "<source>_Draft_5deg") rather than one undifferentiated compound --
    collapsing every band into a single shape (an earlier version of
    this function) meant the command was strictly less useful than
    core.draft_angle_analysis itself, which already returns the bands
    separately. A full colour-graded heat-map overlay (using the
    returned "grid") is still a task-panel concern, not implemented by
    this single-shot native provider.

    Band names are derived from the source face's label
    ("<source>_Draft_5deg") and reused via groups.find_or_create, so
    re-running draft analysis on the same face during an iterative
    review updates the existing band objects instead of adding a new
    batch every time -- see native_combs' docstring.
    """
    import FreeCAD
    from rfstudio import core
    face, label = _selected_face()
    result = core.draft_angle_analysis(face, FreeCAD.Vector(0, 0, 1))
    doc = FreeCAD.ActiveDocument
    def _do():
        made = 0
        for threshold, band in zip(result["thresholds"], result["bands"]):
            if not band.Edges:
                continue
            obj, _created = groups.find_or_create(
                doc, "%s_Draft_%gdeg" % (label, threshold))
            obj.Shape = band
            groups.add_to_group(obj, "QA")
            made += 1
        if made == 0:
            FreeCAD.Console.PrintWarning(
                "Railform Studio: draft analysis found no threshold "
                "crossings on the selected face at bands %s -- the face may "
                "be entirely above or below every band.\n"
                % (result["thresholds"],))
        doc.recompute()
    _with_undo(doc, "Draft Analysis", _do)


# --- native analysis / control-polygon curve / star-patch fallback -------- #
def _selected_faces():
    """All (face, source_label) from the current selection: sub-face
    picks first, else the first face of each whole-object pick. Raises
    ValueError on nothing usable -- same convention as _selected_face()."""
    import FreeCADGui
    faces = []
    for so in FreeCADGui.Selection.getSelectionEx():
        label = getattr(so.Object, "Label", so.Object.Name)
        subs = [s for s in (so.SubObjects or [])
                if getattr(s, "ShapeType", "") == "Face"]
        if subs:
            faces.extend((s, label) for s in subs)
        else:
            shp = getattr(so.Object, "Shape", None)
            if shp is not None and shp.Faces:
                faces.append((shp.Faces[0], label))
    if not faces:
        raise ValueError("select one or more faces/surfaces first")
    return faces


def native_analysis():
    """Native provider for RFS_Analysis -- the generic curvature/
    continuity review Curves' Curves_SurfaceAnalysis used to own.
    Runs core.surface_review_summary (adjacency graph + per-seam
    deviation/continuity grading) over every selected face and prints
    the worst seam + recommendation to the Report view. Raises
    ValueError on bad selection so ProviderCommand falls through to the
    Curves delegation instead of silently doing nothing."""
    import FreeCAD
    from rfstudio import core
    picked = _selected_faces()
    faces = [f for f, _ in picked]
    summary = core.surface_review_summary(faces, pull_dir=FreeCAD.Vector(0, 0, 1))
    rec = core.review_recommendation(summary)
    worst = summary["continuity"]["worst_grade"]
    n_seams = len(summary["continuity"]["seams"])
    n_iso = len(summary["continuity"]["isolated"])
    FreeCAD.Console.PrintMessage(
        "Railform Studio -- surface analysis over %d face(s):\n"
        "  seams checked: %d, worst grade: %s, isolated patches: %d\n"
        "  worst draft angle (deg): %s\n"
        "  recommendation: %s\n"
        % (len(faces), n_seams, worst, n_iso,
           summary["draft"]["worst_angle"], rec))


def native_control_poly4():
    """Native provider for RFS_ControlPoly4 -- replaces Silk's
    ControlPoly4 with core.control_poly_curve (the same
    buildFromPolesMultsKnots technique already native for curves).
    Takes exactly 4 control points (the 2x2 grid convention used by
    _grid_points_from_selection) and builds a degree-3 clamped-uniform
    B-spline whose control polygon is those 4 points."""
    import FreeCAD
    from rfstudio import core
    pts = _grid_points_from_selection(2)   # raises on bad selection
    edge = core.control_poly_curve(pts, degree=3)
    doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
    _add_feature_with_undo(doc, "ControlPoly4", edge)


def native_star_patch():
    """Native provider for RFS_StarPatch. Builds an N-sided star patch via
    core.gregory_star_patch: exact G1 continuity at every corner and at the
    star point by construction (Gregory tangent-plane rule), with shared
    center acceleration/jerk from _star_spoke (septic Hermite) pushing
    continuity past G2 in parameter space.

    True provably-exact geometric G2 across every seam remains an open CAGD
    problem for arbitrary valence; Gregory patches are the standard
    practical approximation. No Silk or Curves dependency.

    Selection: the >= 3 boundary edges of one closed loop, picked as
    edge sub-objects (or one object whose shape is the loop itself).
    Raises ValueError on a bad selection so ProviderCommand reports the
    failure chain clearly."""
    import FreeCAD
    import FreeCADGui
    from rfstudio import core
    sel = FreeCADGui.Selection.getSelectionEx()
    edges = []
    for so in sel:
        shp = getattr(so.Object, "Shape", None)
        if shp is None:
            continue
        for sub in (so.SubObjects or []):
            if getattr(sub, "ShapeType", "") == "Edge":
                edges.append(sub)
        if not so.SubObjects:
            edges.extend(shp.Edges)
    if len(edges) < 3:
        raise ValueError(
            "star patch needs >= 3 boundary edges selected (got %d)"
            % len(edges))
    patch = core.gregory_star_patch(edges)
    doc = FreeCAD.ActiveDocument or FreeCAD.newDocument()
    _add_feature_with_undo(doc, "StarPatch_Native", patch)


def validate_providers(providers=None, engines_module=None):
    """Static sanity check of the provider-chain wiring, runnable
    headlessly (no FreeCADGui needed) so it can be exercised by
    test_core.py and catches dead buttons at test time instead of at
    click time:

      * every chain entry is a known ("engine"|"native", ...) shape;
      * every ("native", name) resolves to an attribute on the engines
        module (a typo here yields a permanently dead provider);
      * every ("engine", mod, _) uses a mod name in deps.ENGINES;
      * every action in DELEGATIONS has a non-empty chain.

    Returns the list of problems found (empty list = healthy). Also
    called once from the workbench health check path, mirroring
    deps.verify_delegations' "fail loudly, up front" approach."""
    import sys
    providers = PROVIDERS if providers is None else providers
    engines_module = sys.modules[__name__] if engines_module is None \
        else engines_module
    problems = []
    for cmd_id, chain in providers.items():
        if not chain:
            problems.append("%s: empty provider chain" % cmd_id)
            continue
        for p in chain:
            if not isinstance(p, tuple) or len(p) < 2:
                problems.append("%s: malformed provider %r" % (cmd_id, p))
                continue
            if p[0] == "native":
                if not hasattr(engines_module, p[1]):
                    problems.append(
                        "%s: native provider %r missing" % (cmd_id, p[1]))
            elif p[0] == "engine":
                if len(p) != 3 or p[1] not in deps.ENGINES or not p[2]:
                    problems.append(
                        "%s: bad engine provider %r" % (cmd_id, p))
            else:
                problems.append(
                    "%s: unknown provider kind %r" % (cmd_id, p[0]))
    for cmd_id in DELEGATIONS:
        if cmd_id not in providers:
            problems.append("%s: no provider chain" % cmd_id)
    return problems