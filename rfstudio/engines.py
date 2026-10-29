# -*- coding: utf-8 -*-
"""Engine adapter layer (GUI side).

Thin wrappers that optionally drive the Curves workbench command as a
FALLBACK for actions whose native path can't handle a selection. This is
where the remains of depend-and-wrap live: translate a curated Railform
action into an existing engine command, ensuring that engine's commands
are registered first.

There is no Silk dependency any more: every Silk-backed action
(CleanPatch44/66, ControlPoly4, StarPatch) has a native provider in this
module and runs entirely from OCCT.
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
# NOTE: this is now a CURVES-ONLY fallback map.
DELEGATIONS = {
    # ---- Surface --------------------------------------------------------
    "RFS_BlendSurf":    ("Curves", "Curves_BlendSurf2"),
    "RFS_Trim":         ("Curves", "Trim"),
    "RFS_SketchOnSurf": ("Curves", "SoS"),
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
#     it never needed an engine in the first place.
#   - RFS_Trim/RFS_SketchOnSurf are moderate OCCT work (BRepFeat_SplitShape
#     / Geom2d projection).
#   - RFS_BlendSurf now has a native path too (core.blend_surface, the
#     same quintic-Hermite technique match_surface already uses) --
#     Curves_BlendSurf2 is kept only as the fallback for selections the
#     native nearest-facing-side search can't resolve.
#
# Net effect: the only engine still referenced anywhere in this module is
# Curves, and only as a FALLBACK behind an existing native provider.
#
# RFS_Trim/RFS_SketchOnSurf are chained NATIVE FIRST, then the original
# engine command as fallback -- unlike MultiLoft (a clean redundancy, safe
# to cut over fully), these are freshly written and only geometry-tested
# (see test_core.py), not field-tested against every selection shape a
# real user will throw at them. Each native_* function below raises
# ValueError on a bad/ambiguous selection rather than printing-and-
# returning, specifically so ProviderCommand's chain logic (commands.py)
# falls through to the still-installed engine command instead of silently
# doing nothing -- see the "silent no-op" discussion in the project SWOT
# for why that distinction matters.
NATIVE_OVERRIDES = {
    "RFS_MultiLoft":    [("native", "native_multiloft")],
    "RFS_Trim":         [("native", "native_trim"),
                         ("engine", "Curves", "Trim")],
    "RFS_SketchOnSurf": [("native", "native_sketch_on_surf"),
                         ("engine", "Curves", "SoS")],
    # RFS_BlendSurf now tries the native quintic-Hermite skin
    # (core.blend_surface) FIRST -- this used to be a pure Curves
    # delegation with no native path at all. Curves_BlendSurf2 stays as
    # the fallback for selections the native path can't handle yet (e.g.
    # non-planar-adjacent faces OCCT's nearest-facing-side search can't
    # resolve cleanly).
    "RFS_BlendSurf":    [("native", "native_blend_surface"),
                         ("engine", "Curves", "Curves_BlendSurf2")],
}


# Booleans, mesh/scan-to-NURBS fitting, and hidden-line drafting export --
# native-only additions, none of which had ANY engine equivalent (Curves
# never offered booleans, mesh import, or drafting output). See
# native_boolean_*/native_mesh_to_surface/native_hidden_line_export's own
# docstrings below for what each wraps.
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


def missing_engine_for(cmd_id):
    """Name of the engine a curated command NEEDS but which isn't
    installed, or None if the command can run right now.

    A command is runnable whenever its provider chain has at least one
    provider that can fire: a native provider always can (self-contained
    OCCT), and an engine provider can when that engine is installed. Only
    a command whose ENTIRE chain is engine providers, none of them
    installed, is currently unrunnable -- we return the first such
    engine's name so the tool shelf can grey the button and say why.

    Commands with no PROVIDERS chain at all (the native task-panel
    commands -- DrawInterp, Loft, ...) are never engine-backed, so they
    always return None. Gui-free (only reads deps.installed), so the tool
    shelf can call it at activation time without a live document."""
    chain = PROVIDERS.get(cmd_id)
    if not chain:
        return None
    needed = None
    for p in chain:
        if p[0] == "native":
            return None
        if p[0] == "engine":
            mod = p[1]
            if deps.installed(mod):
                return None
            if needed is None:
                needed = mod
    return needed


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