# rfstudio/native/ -- compiled geometry gates (optional)

`rfs_native` is an optional compiled extension. The workbench is
functional without it: every gate that can use it has a pure-Python
fallback (see `rfstudio/core/patches.py::_deviation_of_curves` and
`rfstudio/core/qa.py::surface_deviation`).

## What's in it

`rfs_native.cpp` is the whole module -- exact projected-Newton projection
against B-spline surfaces (no tessellation). Each solve is a coarse
surface-lattice scan for an initial guess plus multi-start projected
Newton on `|S(u,v) - P|^2` (analytic 1st/2nd partials, backtracking line
search, domain clamping) -- pure arithmetic. It replaces the per-sample
`Part.Vertex.distToShape`, which rebuilt an OCCT extrema context per
sample (~4-13 ms each) through the Python binding; the compiled path is
orders faster and the surface data crosses the binding once per call.

Exposed functions:

- `max_surface_deviation(points, faces)` -- max over the points of the
  min distance to the given B-spline faces. Backs the sweep/loft/gordon
  deviation gate (`patches._deviation_of_curves`). Returns the worst
  distance (float).
- `project_points(points, poles, knots_u, knots_v, degree_u, degree_v)`
  -- project every point onto ONE B-spline surface, building the
  projection lattice **once** for the whole batch; returns an `(N, 3)`
  array of `[distance, u, v]` per point. Backs `qa.surface_deviation`
  (continuity review / blend / match seam grading), replacing its
  per-sample `distToShape`. Added in 0.3.0.
- `debug_eval` / `debug_project` -- single-point surface evaluation /
  projection, used by the one-time pole-orientation probe
  (`patches._native_gate`) and verification scripts.

Only non-periodic B-spline faces spanning their surface's natural
parameter box are eligible (the geometry the sweep/loft/gordon/blend
builders produce); anything else makes the Python caller fall back.

- `build_devgate.py` -- MSVC + pybind11 build for Windows
  (`python rfstudio\\native\\build_devgate.py`). The output ABI tag must
  match FreeCAD's Python (cp311 for FreeCAD 1.1); the tagged filename is
  what importlib loads, `build_devgate.py` derives it automatically
  whatever Python ran the build.

## Constraints

- The compiled result must keep the contracts pinned in `test_core.py`
  (trim-awareness: only the shape's trimmed faces land in the
  tessellation; deviation fidelity --- deflection is orders below the
  warning thresholds).
- Linux/macOS are free to use the pure-Python path.
