"""Unit tests for Illustrator-style Click-Drag Pen Tool & SolidWorks-style Spline Handles."""
import sys
import math
import FreeCAD
from FreeCAD import Vector
import Part
from rfstudio import core

def out(s):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()

def test_bezier_handle_collinear_aligned():
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 0, 0)]
    lh = [Vector(-2, 0, 0), Vector(8, 0, 0), Vector(18, 0, 0)]
    rh = [Vector(2, 0, 0), Vector(12, 0, 0), Vector(22, 0, 0)]
    htypes = ["Aligned", "Aligned", "Aligned"]

    # Drag right handle at point 1 to (13, 4, 0)
    rh[1] = Vector(13, 4, 0)
    core.enforce_bezier_handle_constraints(1, "right", lh, rh, htypes, pts)

    # In "Aligned" mode, left handle must be collinear (antiparallel to right handle vector)
    p = pts[1]
    v_right = rh[1] - p  # (3, 4, 0), len 5
    v_left = lh[1] - p
    # Dot product normalized must be -1.0
    dot = (v_right.normalize()).dot(v_left.normalize())
    assert abs(dot - (-1.0)) < 1e-6, f"Aligned handle not collinear: dot={dot}"
    out(f"  bezier aligned collinear OK  dot={dot:.6f}")

def test_bezier_handle_free_cusp():
    pts = [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 0, 0)]
    lh = [Vector(-2, 0, 0), Vector(8, 0, 0), Vector(18, 0, 0)]
    rh = [Vector(2, 0, 0), Vector(12, 0, 0), Vector(22, 0, 0)]
    htypes = ["Free", "Free", "Free"]

    # In Free (Cusp / Alt-held) mode, dragging right handle must NOT move left handle
    orig_lh1 = Vector(lh[1].x, lh[1].y, lh[1].z)
    rh[1] = Vector(14, 5, 2)
    core.enforce_bezier_handle_constraints(1, "right", lh, rh, htypes, pts)

    assert (lh[1] - orig_lh1).Length < 1e-9, f"Left handle moved in Free mode: {lh[1]} vs {orig_lh1}"
    out("  bezier cusp free mode    OK  left handle preserved")

def test_bezier_handle_tension_and_angle():
    anchor = Vector(10, 20, 0)
    handle = Vector(13, 24, 0)
    v = handle - anchor
    tension = v.Length
    angle = math.degrees(math.atan2(v.y, v.x))

    assert abs(tension - 5.0) < 1e-6, f"Expected tension 5.0, got {tension}"
    assert abs(angle - 53.1301) < 1e-3, f"Expected angle ~53.13, got {angle}"
    out(f"  handle tension & angle   OK  tension={tension:.2f}mm angle={angle:.2f}deg")

def test_bezier_rubberband_sampling():
    p0 = Vector(0, 0, 0)
    p1 = Vector(2, 4, 0)   # outgoing handle
    p3 = Vector(10, 0, 0)  # cursor pos
    p2 = p3 - (p3 - p0) * (1.0 / 3.0)  # default incoming handle

    samples = 16
    curve_pts = []
    for s in range(samples + 1):
        t = float(s) / samples
        omt = 1.0 - t
        pt_t = (p0 * (omt * omt * omt) +
                p1 * (3.0 * omt * omt * t) +
                p2 * (3.0 * omt * t * t) +
                p3 * (t * t * t))
        curve_pts.append(pt_t)

    assert len(curve_pts) == 17
    assert (curve_pts[0] - p0).Length < 1e-9
    assert (curve_pts[-1] - p3).Length < 1e-9
    # Midpoint should have positive Y due to outgoing handle
    assert curve_pts[8].y > 0.5
    out(f"  rubberband 16-pt sample  OK  mid_y={curve_pts[8].y:.3f}")

def test_bezier_curve_shape_with_handles():
    pts = [Vector(0, 0, 0), Vector(10, 5, 0), Vector(20, 0, 0)]
    lh = [Vector(0, 0, 0), Vector(8, 5, 0), Vector(18, 2, 0)]
    rh = [Vector(2, 2, 0), Vector(12, 5, 0), Vector(20, 0, 0)]

    shape = core.bezier_curve_shape(pts, lh, rh)
    assert shape is not None and not shape.isNull()
    assert shape.ShapeType == "Wire"
    assert len(shape.Edges) == 2
    out(f"  bezier wire shape        OK  edges={len(shape.Edges)} len={shape.Length:.2f}")

def test_endpoint_continuity_g1_to_circle():
    circle = Part.makeCircle(10, Vector(0,0,0), Vector(0,0,1))
    p_anchor = Vector(10, 0, 0)
    # Cursor dragged handle upward with slight X drift (10.5, 4.0, 0.0)
    p_handle = Vector(10.5, 4.0, 0.0)
    res = core.snap_endpoint_continuity(p_anchor, p_handle, circle, continuity="G1")

    p1 = res['p_handle']
    # Must be snapped purely along target tangent (0, 1, 0)
    assert abs(p1.x - 10.0) < 1e-9, f"Expected X=10.0, got {p1.x}"
    assert p1.y > 0.0, f"Expected positive Y, got {p1.y}"
    qa = core.classify_endpoint_continuity(p_anchor, p1, circle)
    assert qa['grade'] == "G1", qa
    out(f"  endpoint snap G1 circle  OK  grade={qa['grade']} angle_err={qa['g1_angle_deg']:.4f}deg")

def test_endpoint_continuity_g2_curvature_match():
    circle = Part.makeCircle(10, Vector(0,0,0), Vector(0,0,1))
    p_anchor = Vector(10, 0, 0)
    p_handle = Vector(10.0, 4.0, 0.0)
    # P2 placed roughly
    p2_init = Vector(8.0, 8.0, 0.0)
    res = core.snap_endpoint_continuity(p_anchor, p_handle, circle, continuity="G2", p2=p2_init)

    p1 = res['p_handle']
    p2 = res['p2']
    assert p2 is not None
    qa = core.classify_endpoint_continuity(p_anchor, p1, circle, p2=p2)
    assert qa['grade'] == "G2", qa
    out(f"  endpoint snap G2 curv    OK  grade={qa['grade']} target_k={qa['target_curvature']:.4f}")

def test_endpoint_continuity_g3_jerk_match():
    circle = Part.makeCircle(10, Vector(0,0,0), Vector(0,0,1))
    p_anchor = Vector(10, 0, 0)
    p_handle = Vector(10.0, 3.5, 0.0)
    p2_init = Vector(8.0, 7.0, 0.0)
    p3_init = Vector(4.0, 9.0, 0.0)
    res = core.snap_endpoint_continuity(p_anchor, p_handle, circle, continuity="G3", p2=p2_init, p3=p3_init)

    p1 = res['p_handle']
    p2 = res['p2']
    p3 = res['p3']
    assert p3 is not None
    qa = core.classify_endpoint_continuity(p_anchor, p1, circle, p2=p2, p3=p3)
    assert qa['grade'] == "G3", qa
    out(f"  endpoint snap G3 jerk    OK  grade={qa['grade']}")

def test_endpoint_continuity_g1_to_face():
    face = Part.Face(Part.makePlane(20, 20, Vector(0,0,0), Vector(0,0,1)))
    p_anchor = Vector(5, 5, 0)
    # Handle pulled off-plane with Z=3
    p_handle = Vector(7, 8, 3)
    res = core.snap_endpoint_continuity(p_anchor, p_handle, face, continuity="G1")

    p1 = res['p_handle']
    # Snapped handle must lie strictly on face tangent plane (Z=0)
    assert abs(p1.z - 0.0) < 1e-9, f"Expected Z=0.0 on face plane, got {p1.z}"
    out(f"  endpoint snap G1 face    OK  handle={p1}")

if __name__ == "__main__":
    test_bezier_handle_collinear_aligned()
    test_bezier_handle_free_cusp()
    test_bezier_handle_tension_and_angle()
    test_bezier_rubberband_sampling()
    test_bezier_curve_shape_with_handles()
    test_endpoint_continuity_g1_to_circle()
    test_endpoint_continuity_g2_curvature_match()
    test_endpoint_continuity_g3_jerk_match()
    test_endpoint_continuity_g1_to_face()
    print("All Bezier Handle & Pen Tool tests passed!")
