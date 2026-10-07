# -*- coding: utf-8 -*-
"""Headless test of the GUI panels' pure decision logic (rfstudio.panel_logic).

    python test_panel_logic.py
    & "C:\\Program Files\\FreeCAD 1.1\\bin\\freecadcmd.exe" test_panel_logic.py

Unlike test_core.py, this file has NO FreeCAD dependency at all -- panel_logic
is plain Python -- so it runs under a stock `python` interpreter too. It's the
GUI-side counterpart to test_core.py's headless safety net: the panels
themselves (rfstudio.panel_*) can't be imported here (they pull in
FreeCADGui, which pulls in Silk/Curves' Gui-at-load code), but the readiness/
status decisions those panels make are plain data-in/data-out and are
covered here.
"""

import sys, os, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rfstudio import panel_logic


def out(s):
    sys.stdout.write(s + "\n"); sys.stdout.flush()


def test_readiness_ok():
    ok, msg = panel_logic.readiness(
        "Loft", {"Profiles": 3, "GuideRails": 1},
        {"Profiles": (2, None), "GuideRails": (0, 4)})
    assert ok
    assert "Loft ready" in msg
    out("  readiness ok        OK  %s" % msg)


def test_readiness_no_inputs_yet():
    ok, msg = panel_logic.readiness(
        "Gordon", {"Profiles": 0, "Guides": 0},
        {"Profiles": (2, None), "Guides": (2, None)})
    assert not ok
    assert "No inputs yet" in msg
    out("  readiness empty     OK  %s" % msg)


def test_readiness_below_minimum():
    ok, msg = panel_logic.readiness(
        "Coons", {"Boundary": 3}, {"Boundary": (4, 4)})
    assert not ok
    assert "needs >= 4" in msg
    out("  readiness below-min OK  %s" % msg)


def test_readiness_above_maximum():
    ok, msg = panel_logic.readiness(
        "Coons", {"Boundary": 5}, {"Boundary": (4, 4)})
    assert not ok
    assert "allows <= 4" in msg
    out("  readiness above-max OK  %s" % msg)


def test_loft_readiness():
    ok, msg = panel_logic.loft_readiness(1, 0, 2)
    assert not ok and "add curves above" in msg
    ok, msg = panel_logic.loft_readiness(3, 1, 2)
    assert ok and "3 profile(s), 1 guide rail(s)" in msg
    out("  loft readiness      OK")


def test_offset_thicken_readiness():
    ok, msg = panel_logic.offset_thicken_readiness("Offset", False, False)
    assert not ok and "Select a source" in msg
    ok, msg = panel_logic.offset_thicken_readiness("Thicken", True, True)
    assert ok and msg == "Thicken ready."
    out("  offset/thicken      OK")


def test_shell_readiness():
    ok, msg = panel_logic.shell_readiness(False, False)
    assert not ok and "select the solid" in msg and "select >= 1 face" in msg
    ok, msg = panel_logic.shell_readiness(True, True)
    assert ok and msg == "Shell ready."
    out("  shell readiness     OK")


def test_blend_readiness():
    ok, msg = panel_logic.blend_readiness(False)
    assert not ok and "Select two surfaces" in msg
    ok, msg = panel_logic.blend_readiness(True, kind="Surface blend")
    assert ok and msg == "Surface blend ready."
    out("  blend readiness     OK")


def test_infer_pull_direction_averages_normals():
    d = panel_logic.infer_pull_direction([(0, 0, 1), (0, 0, 1)])
    assert d is not None and abs(d[2] - 1.0) < 1e-9
    out("  pull dir average    OK  %s" % (d,))


def test_infer_pull_direction_cancelling_normals_is_none():
    d = panel_logic.infer_pull_direction([(1, 0, 0), (-1, 0, 0)])
    assert d is None
    out("  pull dir cancel     OK  None")


def test_infer_pull_direction_no_faces_is_none():
    assert panel_logic.infer_pull_direction([]) is None
    assert panel_logic.infer_pull_direction([(0, 0, 0)]) is None
    out("  pull dir empty      OK  None")


def test_construction_plane_offsets_along_normal():
    origin, normal = panel_logic.construction_plane(
        (0, 0, 0), (0, 0, 2), offset=10)
    assert abs(normal[2] - 1.0) < 1e-9
    assert abs(origin[2] - 10.0) < 1e-9
    # Zero-length view dir falls back to +Z, never raises
    origin2, normal2 = panel_logic.construction_plane((1, 2, 3), (0, 0, 0))
    assert normal2 == (0.0, 0.0, 1.0)
    assert origin2 == (1, 2, 3)
    out("  construction plane OK")


def test_ray_plane_intersect_hit_and_parallel():
    hit = panel_logic.ray_plane_intersect(
        (0, 0, 10), (0, 0, -1), (0, 0, 0), (0, 0, 1))
    assert hit is not None and abs(hit[2]) < 1e-9, hit
    assert abs(hit[0]) < 1e-9 and abs(hit[1]) < 1e-9
    # Ray parallel to the plane -> None
    miss = panel_logic.ray_plane_intersect(
        (0, 0, 10), (1, 0, 0), (0, 0, 0), (0, 0, 1))
    assert miss is None
    # Off-plane ray through a tilted plane still lands ON the plane
    hit2 = panel_logic.ray_plane_intersect(
        (0, 0, 10), (0.5, 0, -1), (0, 0, 0), (0, 0, 1))
    assert hit2 is not None and abs(hit2[2]) < 1e-9, hit2
    out("  ray->plane intersect OK")


def test_sketch3d_readiness():
    ok, msg = panel_logic.sketch3d_readiness("InterpCurve", 1)
    assert not ok and "at least 2 points" in msg
    ok, msg = panel_logic.sketch3d_readiness("InterpCurve", 4)
    assert ok and "4 point(s)" in msg
    out("  sketch3d readiness OK")


def test_solver_status_text():
    # over-defined -> actionable conflict, includes the residual
    t = panel_logic.solver_status_text(
        {"state": "over", "residual": 3.2e-3, "dof": 6, "rank": 6})
    assert "over-defined" in t and "remove" in t and "3.2e-03" in t, t
    # under-defined -> remaining DOF derived from dof - rank
    t = panel_logic.solver_status_text(
        {"state": "under", "residual": 0.0, "dof": 6, "rank": 4})
    assert "under-defined" in t and "2 of 6" in t, t
    # well-defined -> fully constrained, shows the DOF count
    t = panel_logic.solver_status_text(
        {"state": "well", "residual": 1e-9, "dof": 6, "rank": 6})
    assert "well-defined" in t and "6 DOF" in t, t
    # graceful degradation: numpy fallback and empty input
    assert "numpy" in panel_logic.solver_status_text({"state": "unknown"})
    assert panel_logic.solver_status_text({}) == ""
    assert panel_logic.solver_status_text(None) == ""
    out("  solver status   OK")


def test_points_are_planar():
    sq = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
    assert panel_logic.points_are_planar(sq)
    skew = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 5)]
    assert not panel_logic.points_are_planar(skew)
    out("  planar check    OK")


def test_plane_normal_for_and_basis():
    assert panel_logic.plane_normal_for("XY") == (0.0, 0.0, 1.0)
    assert panel_logic.plane_normal_for("YZ") == (1.0, 0.0, 0.0)
    assert panel_logic.plane_normal_for("ZX") == (0.0, 1.0, 0.0)
    n = panel_logic.plane_normal_for("Camera", (0, 0, -2))
    assert abs(n[2] + 1.0) < 1e-9
    n_floor = panel_logic.plane_normal_for("Camera", (0, 0, 0))
    assert n_floor == (0.0, 0.0, 1.0)   # degenerate -> safe fallback
    # Basis must be a right-handed-ish orthonormal pair spanning the plane
    u, v = panel_logic.plane_basis((0, 0, 1))
    assert abs(u[0] * 0 + u[1] * 0 + u[2] * 1) < 1e-9       # u perp to n
    assert abs(v[0] * 0 + v[1] * 0 + v[2] * 1) < 1e-9       # v perp to n
    assert abs(u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) < 1e-9  # u perp v
    assert abs(sum(c * c for c in u) - 1) < 1e-9 and abs(sum(c * c for c in v) - 1) < 1e-9
    out("  plane normal/basis OK")


def test_points_form_closed_loop():
    open_pts = [(0, 0, 0), (1, 0, 0), (1, 1, 0)]
    assert not panel_logic.points_form_closed_loop(open_pts)
    closed = open_pts + [(0, 0, 0)]
    assert panel_logic.points_form_closed_loop(closed)
    near = open_pts + [(1e-3, 0, 0)]
    assert not panel_logic.points_form_closed_loop(near)
    assert panel_logic.points_form_closed_loop(near, tol=1e-2)
    out("  closed-loop check OK")


def test_auto_plane_normal():
    # Segment along +Z -> new plane must contain Z -> normal is X or Y
    n = panel_logic.auto_plane_normal((0, 0, 5), (0, 0, 1))
    assert n in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), n
    # Segment along +X -> normal is Y or Z
    n = panel_logic.auto_plane_normal((5, 0, 0), (0, 0, 1))
    assert n in ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), n
    # Degenerate chord -> keep previous
    assert panel_logic.auto_plane_normal((0, 0, 0), (0, 0, 1)) == (0, 0, 1)
    out("  auto plane flip OK")


def test_apply_axis_lock():
    assert panel_logic.apply_axis_lock((5, 6, 7), (1, 2, 3), "X") == (5, 2, 3)
    assert panel_logic.apply_axis_lock((5, 6, 7), (1, 2, 3), "Y") == (1, 6, 3)
    assert panel_logic.apply_axis_lock((5, 6, 7), (1, 2, 3), "Z") == (1, 2, 7)
    assert panel_logic.apply_axis_lock((5, 6, 7), (1, 2, 3), None) == (5, 6, 7)
    assert panel_logic.apply_axis_lock((5, 6, 7), (1, 2, 3), "bogus") == (5, 6, 7)
    out("  axis lock       OK")


def test_cycle_plane():
    assert panel_logic.cycle_plane("Camera") == "XY"
    assert panel_logic.cycle_plane("ZX") == "Auto"
    assert panel_logic.cycle_plane("Auto") == "Camera"  # wraps
    assert panel_logic.cycle_plane("bogus") == "Camera"  # safe fallback
    out("  plane cycle     OK")


def test_constraint_anchor_and_glyph():
    pts = [(0, 0, 0), (10, 0, 0), (10, 10, 0)]
    assert panel_logic.constraint_anchor(("distance", 0, 1, 10.0), pts) == (5.0, 0.0, 0.0)
    assert panel_logic.constraint_anchor(("fix", 2, None), pts) == (10, 10, 0)
    assert panel_logic.constraint_anchor(("angle", 0, 1, 2, 90.0), pts) == (10, 0, 0)
    assert panel_logic.constraint_anchor(("distance", 0, 9, 1.0), pts) is None
    assert panel_logic.constraint_glyph("distance") == "D"
    assert panel_logic.constraint_glyph("symmetric") == "S"
    assert panel_logic.constraint_glyph("bogus") == "?"
    out("  anchor/glyph    OK")


def test_constraint_tooltip():
    pts = [(0, 0, 0), (10, 0, 0)]
    tip = panel_logic.constraint_tooltip(("distance", 0, 1, 12.0), pts)
    assert "target 12.00" in tip and "now 10.00" in tip, tip
    tip = panel_logic.constraint_tooltip(("fix", 1, None), pts)
    assert "fixed P1" in tip, tip
    tip = panel_logic.constraint_tooltip(("distance", 0, 9, 1.0), pts)
    assert "bad indices" in tip, tip
    out("  tooltip         OK")


def test_evaluate_expression():
    assert panel_logic.evaluate_expression("2+3*4", {}) == 14.0
    assert panel_logic.evaluate_expression("=10/4", {}) == 2.5
    assert panel_logic.evaluate_expression("sqrt(16)+P0x", {"P0x": 3.0}) == 7.0
    assert panel_logic.evaluate_expression("2*(width+1)", {"width": 4.0}) == 10.0
    assert abs(panel_logic.evaluate_expression("sin(pi/2)", {}) - 1.0) < 1e-9
    for bad in ("__import__('os')", "os.system('x')", "lambda: 1",
                "a.b", "P9x", "1/0", "'str'", "[1,2]"):
        try:
            panel_logic.evaluate_expression(bad, {"P0x": 1.0})
            assert False, "expected ValueError for %r" % bad
        except ValueError:
            pass
    out("  expressions     OK")


def test_resolve_constraint_expressions():
    pts = [(0, 0, 0), (10, 0, 0)]
    cs = [("distance", 0, 1, "=P1x*2"), ("distance", 0, 1, 5.0)]
    got = panel_logic.resolve_constraint_expressions(cs, {}, pts)
    assert got[0] == ("distance", 0, 1, 20.0), got
    assert got[1] == ("distance", 0, 1, 5.0), got
    try:
        panel_logic.resolve_constraint_expressions(
            [("distance", 0, 1, "=nope")], {}, pts)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "distance" in str(exc), exc
    out("  resolve exprs   OK")


def test_mirror_plane_params_and_reflect():
    assert panel_logic.mirror_plane_params("YZ") == (1.0, 0.0, 0.0, 0.0)
    assert panel_logic.mirror_plane_params("bogus") is None
    q = panel_logic.reflect_point((4, 1, 2), (1.0, 0.0, 0.0), 0.0)
    assert q == (-4.0, 1.0, 2.0), q
    assert panel_logic.reflect_point((1, 1, 1), (0, 0, 0), 0.0) is None
    out("  mirror helpers  OK")


def test_project_point_to_plane():
    q = panel_logic.project_point_to_plane(
        (1, 2, 5), (0, 0, 0), (0.0, 0.0, 1.0))
    assert q == (1.0, 2.0, 0.0), q
    assert panel_logic.project_point_to_plane(
        (1, 1, 1), (0, 0, 0), (0, 0, 0)) is None
    out("  plane project   OK")


def test_project_point_along():
    """Issue #14: direction projection. direction=None must be exactly
    the legacy orthogonal drop; a real direction slides p along d until
    it lands on the plane; parallel d never lands (None)."""
    o, n = (0, 0, 0), (0.0, 0.0, 1.0)
    # None mode == legacy project_point_to_plane
    a = panel_logic.project_point_along((1, 2, 5), o, n, None)
    b = panel_logic.project_point_to_plane((1, 2, 5), o, n)
    assert a == b == (1.0, 2.0, 0.0), (a, b)
    # along (0,1,-1): s solves 5 + s*(-1) = 0 -> s = 5 -> (1, 7, 0)
    q = panel_logic.project_point_along((1, 2, 5), o, n, (0, 1, -1))
    assert q is not None
    assert abs(q[0] - 1.0) < 1e-9 and abs(q[1] - 7.0) < 1e-9 and \
        abs(q[2]) < 1e-9, q
    # non-unit direction normalizes to the same landing spot
    q2 = panel_logic.project_point_along((1, 2, 5), o, n, (0, 2, -2))
    assert q2 is not None and all(abs(x - y) < 1e-9 for x, y in zip(q, q2))
    # direction parallel to the plane never lands
    assert panel_logic.project_point_along((1, 2, 5), o, n, (1, 0, 0)) is None
    # degenerate inputs
    assert panel_logic.project_point_along((1, 2, 5), o, (0, 0, 0),
                                           (0, 0, -1)) is None
    assert panel_logic.project_point_along((1, 2, 5), o, n, (0, 0, 0)) is None
    out("  project along   OK")


def test_proj_ref_roundtrip():
    """Issue #14: associative-ref encoding survives the string property
    round trip; malformed input degrades to 'free point' (None), never
    raises."""
    edge = panel_logic.encode_proj_ref("edge", "Box001", "Edge3",
                                       0.5, None)
    assert edge == "edge|Box001|Edge3|0.5|n|", edge
    info = panel_logic.parse_proj_ref(edge)
    assert info == {"kind": "edge", "obj": "Box001", "locator": "Edge3",
                    "t": 0.5, "direction": None}, info
    # captured view direction round trip
    sl = panel_logic.encode_proj_ref("slice", "Pad002", "7",
                                     1.0 / 3.0, (1.0, 0.0, -1.0))
    info = panel_logic.parse_proj_ref(sl)
    assert info is not None and info["kind"] == "slice"
    assert info["locator"] == "7" and abs(info["t"] - 1.0 / 3.0) < 1e-12
    assert info["direction"] == (1.0, 0.0, -1.0), info
    # free point + malformed inputs -> None (never an exception)
    for bad in ("", None, "junk", "a|b", "edge|Box001|Edge3|x|n|",
                "edge|Box001|Edge3|0.5|z|", "edge|Box001|Edge3|2.0|n|",
                "edge|Box001|Edge3|0.5|d|1,2",
                "edge|Box001|Edge3|0.5|d|a,b,c",
                "other|Box001|Edge3|0.5|n|", "|Box001|Edge3|0.5|n|"):
        assert panel_logic.parse_proj_ref(bad) is None, bad
    out("  proj ref codec  OK")


def test_proj_ref_relation_kinds():
    """Issues #7/#14: the relation kinds (pierce, onsurf, sil) share the
    same codec; t/mode/dir stay deterministic (0 / 'n' / '') because
    pierce and on-surface are computed against the persisted plane /
    raw point, not against a captured camera direction."""
    for kind, loc in (("pierce", "Edge5"), ("pierce", "Face2"),
                      ("onsurf", "Face6"), ("sil", "3")):
        ref = panel_logic.encode_proj_ref(kind, "Pad001", loc, 0.0, None)
        info = panel_logic.parse_proj_ref(ref)
        assert info is not None, ref
        assert info["kind"] == kind and info["locator"] == loc, info
        assert info["t"] == 0.0 and info["direction"] is None, info
    # a sil ref MAY carry a captured view direction
    info = panel_logic.parse_proj_ref(panel_logic.encode_proj_ref(
        "sil", "Pad001", "3", 0.5, (0.0, 0.0, -1.0)))
    assert info["direction"] == (0.0, 0.0, -1.0), info
    out("  relation ref kinds OK")


def test_constraint_badges():
    """issues.md #8: badge thresholding -- satisfied sets (even with
    solver-noise residuals) stay green; any human-scale violation turns
    red; empty/None input degrades to no badges."""
    assert panel_logic.constraint_badges([]) == []
    assert panel_logic.constraint_badges(None) == []
    # post-solve noise (1e-9) and borderline solver convergence stay OK
    assert panel_logic.constraint_badges([0.0, 1e-9, 1e-6]) == \
        [False, False, False]
    # a dragged-off dim (mm) and a skewed angle (rad) go red
    assert panel_logic.constraint_badges([0.0, 0.25, 1e-3]) == \
        [False, True, True]
    # custom tolerance respected
    assert panel_logic.constraint_badges([0.01], tol=0.1) == [False]
    out("  constraint badges OK")


# --- Equation Manager tests (issues.md #10 / #15) --- #

def test_equation_manager_basic():
    """EquationManager: variable add/get/set, flat namespace for eval."""
    class MockDoc:
        Name = "TestDoc"
    em = panel_logic.EquationManager(MockDoc())
    em.add_variable("width", 100.0, "mm", "plate width")
    em.add_variable("height", 50.0, "mm", "plate height")
    assert em.get_variable_value("width") == 100.0
    assert em.get_variable_meta("width")["unit"] == "mm"
    assert em.get_variable_meta("width")["comment"] == "plate width"
    flat = em.get_all_variables()
    assert flat["width"] == 100.0
    assert flat["height"] == 50.0
    # update value
    em.set_variable_value("width", 120.0)
    assert em.get_variable_value("width") == 120.0
    assert em.get_all_variables()["width"] == 120.0
    out("  equation manager basic OK")


def test_equation_manager_configs():
    """EquationManager: design table / configurations."""
    class MockDoc:
        Name = "TestDoc"
    em = panel_logic.EquationManager(MockDoc())
    em.add_variable("width", 100.0)
    em.add_variable("height", 50.0)
    # Add configs
    idx1 = em.add_config("ConfigA", variables={"width": 80.0, "height": 60.0})
    idx2 = em.add_config("ConfigB", variables={"width": 120.0})
    assert idx1 == 0 and idx2 == 1
    # Active config overrides
    em.set_active_config(0)
    assert em.get_all_variables()["width"] == 80.0
    assert em.get_all_variables()["height"] == 60.0
    em.set_active_config(1)
    assert em.get_all_variables()["width"] == 120.0
    assert em.get_all_variables()["height"] == 50.0  # falls back to global
    out("  equation manager configs OK")


def test_equation_manager_suppression():
    """EquationManager: suppression per constraint/point/handle per config."""
    class MockDoc:
        Name = "TestDoc"
    em = panel_logic.EquationManager(MockDoc())
    em.add_config("Default")
    em.add_config("ConfigB", suppressed_constraints=[0, 2],
                  suppressed_points=[1], suppressed_handles=[3])
    em.set_active_config(1)
    assert em.is_suppressed("constraint", 0) == True
    assert em.is_suppressed("constraint", 1) == False
    assert em.is_suppressed("constraint", 2) == True
    assert em.is_suppressed("point", 1) == True
    assert em.is_suppressed("point", 0) == False
    assert em.is_suppressed("handle", 3) == True
    out("  equation manager suppression OK")


def test_equation_manager_export_import():
    """EquationManager: SolidWorks equations.txt round-trip."""
    class MockDoc:
        Name = "TestDoc"
    em = panel_logic.EquationManager(MockDoc())
    em.add_variable("width", 100.0, "mm", "width")
    em.add_variable("height", 50.0, "mm", "height")
    em.add_config("ConfigA", variables={"width": 80.0})
    em.configs[0]["suppressed_constraints"] = [0]
    em.configs[0]["suppressed_points"] = [1]
    em.configs[0]["suppressed_handles"] = [2]

    txt = em.export_equations_txt()
    # Check export format
    assert '"width" = 100.0 "mm"' in txt
    assert '"height" = 50.0 "mm"' in txt
    assert '$CONFIG "ConfigA"' in txt
    assert '"width" = 80.0' in txt
    assert '$SUPPRESS_CONSTRAINTS 0' in txt
    assert '$SUPPRESS_POINTS 1' in txt
    assert '$SUPPRESS_HANDLES 2' in txt

    # Import into new manager
    em2 = panel_logic.EquationManager(MockDoc())
    ok, errors = em2.import_equations_txt(txt)
    assert ok, f"Import errors: {errors}"
    assert em2.get_variable_value("width") == 100.0
    assert em2.get_variable_meta("width")["unit"] == "mm"
    assert em2.get_variable_meta("width")["comment"] == "width"
    assert len(em2.configs) == 1
    assert em2.configs[0]["name"] == "ConfigA"
    assert em2.configs[0]["variables"]["width"] == 80.0
    assert em2.configs[0]["suppressed_constraints"] == [0]
    assert em2.configs[0]["suppressed_points"] == [1]
    assert em2.configs[0]["suppressed_handles"] == [2]
    out("  equation manager export/import OK")


def test_equation_manager_persistence():
    """EquationManager: serialize/deserialize round-trip."""
    class MockDoc:
        Name = "TestDoc"
    em = panel_logic.EquationManager(MockDoc())
    em.add_variable("width", 100.0, "mm", "width")
    em.add_config("ConfigA", variables={"width": 80.0})
    data = panel_logic.serialize_equation_manager(em)
    em2 = panel_logic.EquationManager(MockDoc())
    panel_logic.deserialize_equation_manager(MockDoc(), data)
    # The registry returns the same instance for same doc
    reg = panel_logic._equation_manager_registry()
    assert reg["TestDoc"].get_variable_value("width") == 100.0
    out("  equation manager persistence OK")


if __name__ == "__main__" or True:  # freecadcmd may not set __main__
    for t in (test_readiness_ok, test_readiness_no_inputs_yet,
              test_readiness_below_minimum, test_readiness_above_maximum,
              test_loft_readiness, test_offset_thicken_readiness,
              test_shell_readiness, test_blend_readiness,
              test_infer_pull_direction_averages_normals,
              test_infer_pull_direction_cancelling_normals_is_none,
              test_infer_pull_direction_no_faces_is_none,
              test_construction_plane_offsets_along_normal,
              test_ray_plane_intersect_hit_and_parallel,
              test_sketch3d_readiness,
              test_solver_status_text,
              test_points_are_planar,
              test_plane_normal_for_and_basis,
              test_points_form_closed_loop,
              test_auto_plane_normal,
              test_apply_axis_lock,
              test_cycle_plane,
              test_constraint_anchor_and_glyph,
              test_constraint_tooltip,
              test_evaluate_expression,
              test_resolve_constraint_expressions,
              test_mirror_plane_params_and_reflect,
              test_project_point_to_plane,
              test_project_point_along,
              test_proj_ref_roundtrip,
              test_proj_ref_relation_kinds,
              test_constraint_badges,
              test_equation_manager_basic,
              test_equation_manager_configs,
              test_equation_manager_suppression,
              test_equation_manager_export_import,
              test_equation_manager_persistence):
        try:
            t()
        except Exception as e:
            out("  %-28s FAIL: %s" % (t.__name__, e))
            traceback.print_exc()
    out("panel_logic tests done")
