# Changelog

All notable changes to Railform Studio are recorded here, newest first.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

Every session that changes behavior (not pure exploration) should add an
entry under `## Unreleased` before wrapping up. Once a version is tagged
in `package.xml`, rename `Unreleased` to that version + date and start a
fresh `Unreleased` section above it.

## Unreleased

### Fixed
- `core._chain_loop` (Coons patch): fixed a `Part.Wire`/`Part.Edge` type
  mismatch that made every `RFS_Fill`-style Coons patch call raise
  `AttributeError: 'Part.Wire' object has no attribute 'valueAt'`.
  Rewritten to operate on edges directly, matching `_orient_family`'s
  `(edge, flip)` convention that `_pt()` actually expects.
- `core.control_poly_curve`: restored. The `def` line had been deleted
  at some point, leaving the function's own body as dead/unreachable
  code silently tacked onto the end of `clean_patch_66`. This was a
  **live production bug**, not just a test gap: `parametric.py`'s
  `ControlCurveFP` calls `core.control_poly_curve` on every recompute,
  so the "Control-point curve" draw tool (`RFS_DrawControl`) was
  completely broken.
- `core.match_surface_exact_g1`: analytic surfaces (planes, cylinders,
  ...) now convert to a bounded `BSplineSurface` by fitting a grid of
  points sampled over the face's actual `ParameterRange`, instead of
  calling the surface's raw (mathematically infinite/unbounded)
  `toBSpline()` with no bounds, which always raised `Part.OCCError`
  ("Will raise an exception if surface is infinite in U or V").
- `core._contour_at_level` (zebra/draft-angle/reflection contouring):
  guard against degenerate zero-length segments -- when an iso-level
  exactly equals a shared grid-corner value, two "different" crossing
  edges can collapse onto the same point, and `Part.makeLine` rejects
  that with `"Line through identical points"`. Such segments are now
  skipped instead of crashing.
- `core.gordon_network`: rebuilt S1/S2/S3 on one shared, per-index
  averaged parametrization (`u_bar`/`v_bar`), using the standard
  two-pass global surface interpolation (Piegl & Tiller, *The NURBS
  Book*, 9.2.5) via new `_skin_through_family` / `_grid_interpolation_surface`
  helpers, instead of an unparametrization`.
  Measured worst-case rail-passthrough error dropped from ~0.93 world
  units (way outside the 0.01 tolerance) to ~0.0001 on a genuinely
  compatible 4x4 network. Along the way, fixed: `u_bar`/`v_bar` were
  being averaged from the wrong parameter matrix (a curve's own
  crossing parameter doesn't vary meaningfully across the OTHER family
  -- it's the partner family's own parametrization that separates
  index 0 from index 1 from index 2); curve input order isn't
  guaranteed to be monotonic in the resulting shared axis (a family
  drawn in the opposite direction breaks `interpolate(Parameters=...)`'s
  strictly-increasing requirement), fixed by re-sorting the index order
  before use; and knot insertion must happen after degree-matching, not
  before (inserting degree-3 multiplicities into a degree-2 curve --
  which happens with as few as 3 curves in a family -- doesn't raise,
  it hangs OCCT's insertion routine.
- `test_core.py`: added the missing `import FreeCAD` (only `from FreeCAD
  import Vector` was present, so `test_snaputil_collect_snap_points`
  raised `NameError` on `FreeCAD.newDocument`); fixed
  `test_draft_angle_analysis_vertical_wall_is_zero_draft`'s wall
  geometry, which had its normal PARALLEL to the pull direction (draft
  angle formula is 90 - angle(normal, pull), so that reads ±90°, not 0
  -- "zero draft" needs the normal PERPENDICULAR to pull); fixed the
  "incompatible boundaries" `match_surface_exact_g1` tests, which
  called `.Surface.toBSpline()` on a raw unbounded plane (same root
  cause as the core.py fix above) and, separately, weren't actually
  parameter-incompatible under this FreeCAD/OCCT version (
  `BSplineSurface.interpolate()` always normalizes its domain to
  [0,1] here, so two differently-shaped interpolated surfaces share a
  domain regardless of their point spacing) -- now built with an
  explicitly rescaled knot domain so they're genuinely incompatible;
  and rewrote `test_gordon_rails_pass_through`'s curve network, which
  built profiles and guides independently from separate 3-point splines
  that only shared a bump *shape*, not an actual intersecting surface
  -- most grid crossings were up to 0.83 world units apart, meaning the
  test's own 0.01 tolerance was geometrically unsatisfiable by *any*
  Gordon-surface algorithm. Curves are now sampled from one shared
  height function so every crossing is genuine.

### Added
- BezierCurve type with handle editing (New Feature)
  - Added BezierCurve as a distinct curve type offering precise curvature control via editable handles
  - Handle types: Auto (smooth), Aligned (opposite tangents), Vector (locked angle), Free (independent)
  - Visual feedback: Blue squares (left handles), green squares (right handles), dashed lines connecting to points
  - Interaction: Click handles to drag, hover for cursor feedback, keyboard shortcuts (A/L/V/F for handle types)
  - Available alongside existing curve types via the curve type converter
  - Implemented in `rfstudio/parametric.py` (BezierCurveFP class) and `rfstudio/editmode.py` (editing logic)
- Direct surface control point manipulation (New Feature)
  - Added SurfaceEditor for direct manipulation of surface control points (vertices of the control net)
  - Enables direct editing of NURBS surface control vertices in the 3D view
  - Visual feedback: Yellow cubes for control points, red cubes for selected points
  - Works with surface types: Loft, Gordon, Coons, Fill, Match, BlendSurf, etc.
  - Implemented in `rfstudio/editmode.py` (SurfaceEditor class)
- **Curve types framework** (New Feature)
  - Added complete framework for parametric curve objects: InterpCurve, ControlCurve, ApproxCurve, Polyline, BezierCurve
  - Each curve type includes proper Property definitions, execution logic, and ViewProvider integration
  - Added `make_curve()` function mirroring `make_surface()` for curve creation
  - Added CurveVP ViewProvider for curve editing integration
  - Implemented in `rfstudio/parametric.py`
- **Blender-style curve draw tool** (New Feature)
  - Added RFS_DrawFreehand tool for drawing curves by clicking to place points continuously
  - Works like Blender's curve tool: click to add points, real-time preview, snap support
  - Creates parametric curve objects that remain fully editable after creation
  - Integrated with existing constraint system and curve type framework
  - Implemented in `rfstudio/commands.py` (command definition) and `rfstudio/panel_curve.py` (draw mode logic)
- `rfstudio/panel_base.py`: `_EditPanelBase` / `_ReportPanelBase` --
  shared task-panel scaffolding (open/commit/abort-transaction +
  resetEdit/closeDialog teardown, done once and correctly) that all 10
  curated panels now derive from, replacing ~10 hand-rolled and
  sometimes-inconsistently-named (`_end` vs `_end_edit`) copies.
- `rfstudio/panel_logic.py` + `test_panel_logic.py`: pure, headlessly
  testable panel decision logic (readiness diagnostics, pull-direction
  averaging) pulled out of the GUI panels. This is new automated
  coverage for the part of the "GUI layer has zero automated coverage"
  gap that's actually tractable to close -- widget rendering still
  needs manual smoke testing (Curves/Silk import `FreeCAD.Gui` at load,
  so nothing that imports them can run under `freecadcmd`), but the
  *decisions* those widgets display no longer can regress silently.
- `deps.report_single_points_of_failure()`: reports, at workbench
  Initialize, which curated actions have NO native fallback at all
  (currently: `RFS_Analysis`, `RFS_ControlPoly4`, `RFS_StarPatch`) --
  visibility for the "two hard external dependencies" risk that
  previously only lived in `engines.py`'s triage comments.
  `report_single_points_of_failure` is covered by
  `test_report_single_points_of_failure` in `test_core.py`.
- In-panel visible notices (`panel_base._notify_overlay_unavailable`),
  not just a console warning, when a Coin3D/pivy viewport overlay
  (Gordon row-click highlight, CurvePanel's 3D point highlight/gizmo)
  can't be built.
- `QMessageBox` warnings for panel commit/abort failures
  (`_EditPanelBase._report_error`) and for `ProviderCommand`'s
  fallback chain being fully exhausted (`commands.py`), replacing
  console-only `PrintError` calls that were easy to miss.
- 3-spinbox (X/Y/Z) draft pull-direction control on
  `SurfaceReviewPanel`, replacing a comma-separated `QLineEdit` --
  the one place in the workbench that broke from the spinbox
  convention every other panel uses.

### Changed
- `taskpanels.py` (~2500 lines, doing the job of ~4 files) split into
  `panel_base.py`, `panel_curve.py` (CurvePanel, BlendPanel),
  `panel_surface.py` (SurfacePanel, LoftPanel, OffsetThickenPanel,
  ShellPanel, BlendSurfPanel), `panel_qa.py` (ContinuityCheckPanel,
  NetworkAuditPanel, SurfaceReviewPanel, WorkflowGuidePanel), and
  `panel_import.py` (ImportRailsPanel). `taskpanels.py` itself is now a
  thin re-export shim, so existing imports
  (`from rfstudio import taskpanels`, `from rfstudio.taskpanels import
  LoftPanel`) keep working unchanged.
