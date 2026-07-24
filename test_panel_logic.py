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


if __name__ == "__main__" or True:  # freecadcmd may not set __main__
    for t in (test_readiness_ok, test_readiness_no_inputs_yet,
              test_readiness_below_minimum, test_readiness_above_maximum,
              test_loft_readiness, test_offset_thicken_readiness,
              test_shell_readiness, test_blend_readiness,
              test_infer_pull_direction_averages_normals,
              test_infer_pull_direction_cancelling_normals_is_none,
              test_infer_pull_direction_no_faces_is_none):
        try:
            t()
        except Exception as e:
            out("  %-28s FAIL: %s" % (t.__name__, e))
            traceback.print_exc()
    out("panel_logic tests done")
