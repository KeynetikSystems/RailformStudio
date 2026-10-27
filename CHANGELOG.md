# Changelog

All notable changes to Railform Studio are recorded here, newest first.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

Every session that changes behavior (not pure exploration) should add an
entry under `## Unreleased` before wrapping up. Once a version is tagged
in `package.xml`, rename `Unreleased` to that version + date and start a
fresh `Unreleased` section above it.

## Unreleased

### Fixed (GUI)
- **Viewpanel freeze/crash (multiloft repro): task panels no longer pump the
  Qt event loop from inside signal handlers.** Every panel change funnels
  through `recompute()` → `obj.recompute()` + `FreeCADGui.updateGui()`
  (`panel_base._CurveGroup._commit`, each panel's `_set`) — and a synchronous
  `updateGui()` while a `valueChanged`/`clicked` stack is live lets the next
  input event fire on top of it. A Degree-slider drag fires dozens of
  `_set` calls per pixel, each pumping events: re-entrant recomputes of the
  SAME parametric shape. New shared `_RecomputeGuard` mixin in
  `panel_base.py` shared by both panel bases: `_recompute_obj()` (nested
  re-entrant recomputes are skipped) and `_defer_gui_update()` (the event
  pump moves to a zero-shot `QTimer.singleShot(0, ...)` tick, coalesced —
  a burst of `_set` calls schedules exactly one redraw after the stack
  unwinds). All 43 call sites across panel_surface/panel_curve/panel_solids
  converted.

## 0.1.0 — 2026-10-24

First packaged release (GPL-3.0 LICENSE added; package.xml/README updated to
the native-first, Silk-free architecture).

### Added
- UIX — left **tool shelf**: filter box, collapsible sections with remembered
  state, and buttons greyed when their only engine is missing
  (`engines.missing_engine_for`).
- UIX — **snap feedback**: ranked snapping now shows which kind engaged, as
  FreeCAD Draft-matching markers (filled circle for vertex/intersection, hollow
  for on-curve; shape + size follow Draft's `snapStyle`/`MarkerSize` prefs)
  coloured per kind; Vertex/Edge/Intersection on/off toggles; a live ΔXYZ /
  distance readout on the status bar during a drag; failed tool-shelf clicks
  flash on the status bar instead of only printing to the Report view.
- UIX — **B-spline knots** are shown as cyan diamonds on the curve throughout
  authoring/editing, updating live.
- UIX — the points table now **inherits the active FreeCAD theme** (no
  hard-coded colours); flat hover-red delete button.
- Single-rail pipe sweep: `core.single_rail_sweep(rail, profiles, n_rail,
  n_prof)` -- the classic ONE-profile/ONE-rail sweep the other sweeps
  couldn't do (they all need >= 2 rails because the second rail supplied
  the section's orientation). The rail is the spine; orientation comes
  from a rotation-minimizing frame transported by Wang et al. double
  reflection (`_rmf_frames`, `_rail_tangent`), so the profile rides the
  curve the way makePipeShell(RMF) does: each station rebuilds the
  profile's original offsets in the transported frame (offset lengths
  preserved exactly, sections keep their frame-relative lean instead of
  shearing with world axes), closed profiles close the tube (their own
  param-0 == param-1 seam supplies the wrap column -- an extra duplicate
  col would put two identical points a param nudge apart, which OCC's
  interpolator rejects outright), multiple profiles pin at their own
  nearest-arc station and morph linearly between anchors (the
  two_rail_sweep description-blending mechanism). Stations reuse the
  N-rail fallback's measured midpoint-sag adaptive subdivision (same
  4x-station budget / sag constants), and the finish runs the
  explicit-parameter interpolation grid with a grid_to_surface fallback.
  Railform Loft routes: 1 rail -> single_rail_sweep, 2 -> two_rail_sweep,
  3+ -> multi_rail_sweep, all with the plain-loft fallback on failure.
  New headless tests: straight closed-pipe radius hold (~2.4e-16), curved
  S-spine offset-radius hold (~3.6e-4), RMF no-flip across inflection,
  multi-profile morph (profiles exact to ~9.2e-16), degenerate-rail
  rejection.

### Fixed
- Multi-rail sweep spine fallback desynced stations when rails carried RAW
  edge parameters: `_ArcRail`, the port of the C++ transport's ArcCurve
  arc-length table, kept its parameter table in RAW edge-param space while
  every caller works in normalized [0,1] space -- an edge produced by
  interpolating `BSplineCurve.toShape()` carries its chord length as the
  parameter range (e.g. [0, 10] for a 2-point line 10 units long), so the
  mixed conventions double-scaled every sampled station by the rail length
  (rows landed at x=50/x=100 on 10-unit rails and the finished surface
  blew past the rails). The table is now normalized-parameter end to end
  (sample points from `_eval01(edge, 0..1)`, station fractions in, native
  rail params out), and the profiles-exact-at-stations spine test that
  exposed this reads ~9e-12 instead of 0.14.

### Improved
- Multi-rail sweep spine fallback is now a Python mashup of the strengths
  measured in the C++ FeatureMultiRailSweep/MultiRailSweep transport
  (n_rail=N still keeps the 2-rail == two_rail_sweep exact delegation and
  any Gordon-route network ships unchanged):
  `_ArcRail` arc-fraction stations so rails of different lengths or
  parameterizations stay in sync (the old normalized-param grid silently
  desynced them); per-profile stations pinned from the profile's own
  near-t on the base rail so "profile wins at its own station" is EXACT
  (the anchor rows reconstruct the profile curve to ~9e-12, guarded
  against uniform-grid/arc-table float ties by proximity matching);
  adaptive midpoint subdivision of station gaps measured by chord
  deviation over the whole pinned-column set (budget 4x n_rail
  stations, 10 passes, sag factor 4 over the 1e-4 target -- measured
  worst sag on the sag-bound fixture: 7.8e-8); a C2 smootherstep remap
  between anchor pins (`_spine_map_c2`, rows between anchors only -- at
  anchor rows the plain linear map stays the identity that preserves
  profile exactness) plus a smootherstep shape blend between bracketing
  profiles, so section transitions ease at each rail instead of kinking;
  a zero-Jacobian guard that refuses loosely-bounded sweeps where the
  rails touch mid-sweep instead of skinning garbage; and a final
  measured boundary-rail deviation gate (the C++ maxRailDeviation
  behaviour: warn on the result, never throw -- this path already runs
  on input the exact solve refused). New headless tests:
  2-rail exact delegation, profile exactness at stations, adaptive sag
  bound, collapsed-rail rejection.

### Fixed (GUI session)
- GUI: every curated TASK-PANEL command (the Draw and Surface toolbars:
  Interp/Control/Approx/Polyline/Bezier, Blend, FilletPolyline, JoinCurve,
  3D sketch, Loft, Gordon, Coons, ImportRails, Sweep2Rail, SweepN, Fill,
  MatchSurf, Offset, Thicken, Shell, ExtendSurf, Mirror, MirrorGroup,
  StitchShell, BalanceNetwork, WorkflowGuide, SurfaceReview,
  Continuity/NetworkAudit/curvature panels, Fillet, Chamfer,
  BlendSurfClosed, TrimByIntersection) was permanently GREYED OUT:
  `PanelCommand.__init__` never stored the command id, and
  `PanelCommand.IsActive` referenced it -- every availability poll
  therefore raised AttributeError, which FreeCAD treats as "command
  disabled". `IsActive` stores the id now (passed by `commands.register`).
  Guarded by `test_gui_commands.py`, a real-GUI regression test
  (`freecad.exe test_gui_commands.py`) that polls IsActive for all 57
  curated commands across three selection states with spot checks.
- Workbench console text no longer claims "Silk detected" (Silk was
  removed; the message now names the engine(s) actually detected).

### Added
 - Multi-rail sweep: exact continuous Gordon route. With >= 3 rails and
   >= 2 profiles, and every profile genuinely crossing every rail
   (measured via the curve-network intersection grid, not assumed),
   `multi_rail_sweep` now solves the sweep as a Gordon boolean sum
   (`gordon_network` with `extra_per_gap=8`) instead of the
   station-skinned spine approximation: the surface passes through the
   FULL shape of every rail and profile continuously, between the
   `RailSamples` stations too, and rail LIST order no longer affects the
   geometry. The result is measured against both curve families before
   shipping (rails <= 1e-5 over 33 samples, profiles <= 5e-4 over 13
   samples); anything worse, an incompatible network, or a failed solve
   falls back to the station-skinned spine path with a console warning.
   Headless tests: continuous-hold, curved-interior-rail,
   order-insensitivity, spine-fallback, gate tests
   (`test_multi_rail_sweep_gordon_route_*`, `test_try_gordon_route_gates`).
 - `gordon_network(extra_per_gap=4)` -- new keyword on the public Gordon
   API controlling between-crossing sampling density (default preserves
   prior behavior); the multi-rail Gordon route raises it to 8.
   `_skin_through_family` threads it into both family skins.
 - Multi-rail sweep spine-path validation/UX: `_rail_order_warning`
   reports a rails-list order that disagrees with how the sections
   traverse them whenever the spine fallback engages (the spine path is
   order-sensitive; the Gordon route is not). The SweepN task panel now
   exposes Up/Down reorder buttons on the Rails list, and the Rails hint
   documents the ordering.
 - Multi-rail sweep spine-path robustness: `_spine_invert` now assumes
   monotonicity only LOCALLY -- a coarse scan picks the FIRST
   profile-param crossing of a rail's spine position and bisects inside
   that bracket. An S-swaying profile used to silently bisect around
   whichever root the monotone assumption exposed; profiles that never
   reach the spine now fall back to the nearest sampled correspondence.
   Verified headless (`test_spine_invert_first_crossing_on_swaying_profile`).

### Improved
 - Multi-rail sweep spine-path final interpolation: the N >= 3 station
   grid is now interpolated at EXPLICIT parameters (the station values
   along the sweep axis, each column's mean spine position across
   stations along the section axis) via `_grid_interpolation_surface`, so
   the between-node shape tracks the real rail/profile correspondence
   instead of `BSplineSurface.interpolate()`'s internal re-parametrization
   (with `grid_to_surface` still as the failure fallback). The exactly-2
   rails case intentionally keeps the legacy call so `multi_rail_sweep`
   and `two_rail_sweep` stay bit-for-bit equivalent (verified to ~1e-14).
 - Multi-rail/two-rail sweep perf: `_NearestTCache` samples each rail
   once per sweep call instead of 101 OCCT evaluations per profile per
   query site (`_nearest_t` queries: flip heuristics, stations).
   Bit-identical results.

### Added
 - Multi-rail sweep (N>=2, N>2 is the new case): `core.multi_rail_sweep`
   skins profiles across an ordered list of rails where **every** rail --
   not just the outer two -- anchors the surface. Each station samples
   all rails; the polyline through those points is the section spine.
   Column offsets (full 3-vector in the profile's own orthonormal frame)
   are measured against that spine at the profile's own station and
   remapped vertex-wise onto each station's spine, so the surface passes
   through every rail at every station and through each profile at that
   profile's station (they coincide when profiles run through the rails,
   the well-formed input). Two rails reduces to `two_rail_sweep` to
   floating-point noise. Headless-tested in `test_core.py` (2/3/5-rail
   areas, both reject paths, all-rail interp ~6e-10, profile interp
   ~4e-6, interior-rail-bites-surface).
   Parametric wiring: new `SweepN` kind (`SweepNFP`, one ordered `Rails`
   link list >= 2 + `Profiles`), surfaced as `RFS_SweepN` ("Multi-rail
   sweep", reuses `rfs_sweep.svg`) in the Railform Surface toolbar and
   the type-converter, alongside the existing two-rail `Sweep`. Also
   reachable through Loft's GuideRails when >= 3 rails are supplied
   (`LoftFP.execute` routes those to `multi_rail_sweep`).

### Added
 - GUI smoke harness hardened into a real regression net
   (`rfstudio/test_gui_imports.py`, <1s, zero FreeCAD dependency,
   `python rfstudio/test_gui_imports.py` from anywhere): six checks --
   (1) mocked import of all 25 rfstudio modules (repo-root path
   auto-fixed -- previously every import failed with `No module named
   'rfstudio'`; `panel_sketch3d`/`panel_nurbspull`/`compat` coverage
   added), (2) undefined `self.<method>()` calls, (3) scope-aware
   bare-undefined names (lambda args, closures, try/except-bound
   imports, module dunders -- the old checker had 829 false
   positives), (4) `rfstudio.<mod>.<attr>` cross-module references
   resolve (catches the KnotEditor class of silent rename/delete
   regressions), (5) no Qt class bases in lazily-loaded modules (the
   workbench-load crash class), (6) addEventCallback
   add/remove/null symmetry per class. Checks 3-6 are mutation-tested
   by `test_gui_checks_selftest.py` (planted-bug self-test).
 - `panel_base.drop_callbacks(view, specs)`: single defensive
   `removeEventCallback` path -- per-callback try/except (one failure
   no longer skips every later removal or the scene cleanup after
   it), IDs always nulled in `finally` (a double teardown can no
   longer hand a stale numeric id -- which FreeCAD reuses -- back to
   the view), idempotent, missing attributes tolerated. All 15 raw
   call sites in `editmode`/`panel_curve`/`panel_sketch3d`/
   `panel_surface`/`panel_solids` route through it; statically
   enforced by harness check #6.
 - `panel_base._status_label()` + `_STATUS_STYLE`/`_ERROR_STYLE`: one
   source for the grey/red status line previously inlined ~20 times
   across panel builds with drifting padding (adopted in
   `PolylineFilletPanel`; other panels can migrate incrementally).
 - 3D-sketch status line now carries the solver verdict (issues.md #8
   slice): `panel_logic.solver_status_text` translates the existing
   (previously unread) `solve_point_constraints` status dict into
   "well-defined: fully constrained (N DOF)" / "under-defined: X of Y
   coordinate DOF still free" / "over-defined: constraints conflict
   (residual ...) -- remove or relax one", appended by
   `Sketch3DPanel._solver_diag` whenever dims exist (no dims = zero
   cost; failure degrades to no suffix). Headless:
   `test_solver_status_text`.
 - IN-PLACE polyline fillet (SolidWorks-sketch behavior): running
   *Fillet polyline* with a polyline selected now records the fillet
   ON that polyline instead of creating a second, linked curve object.
   `PolylineFP` gained a `Fillets` record (per-corner radii,
   index-aligned with `Points`) + `FilletClosed`; `Points` keep the
   sharp corners and `execute()`/`compute()` rebuild the tangent arcs
   from the record on every recompute (including live point-drag
   preview). The corner-handle editor (`CornerFilletEditor`) and
   `PolylineFilletPanel` have in-place modes that read/write the
   record; the panel adds *Clear all*. The legacy derived
   `PolylineFillet` object is still fully supported: created when
   nothing is selected, and edited in place when one is selected.
   Headless: `test_polyline_fillet_record_in_place`.
 - Exact G2 surface matching (`core.match_surface_exact_g2`): copies
   the first three control rows across knot-compatible boundaries
   (shared prep pipeline `_exact_match_prep`: side detection, trim
   guard, BSpline conversion, along- AND cross-knot harmonization,
   degree elevation), giving G0/G1/G2 by construction -- proven to
   12 digits on curved geometry (cylinder-vs-plane seam:
   max_g2=2.54e-12). `match_surface_to_references` tries it first for
   G2 requests (report method `exact_g2`, surfaced in the Match panel
   diagnostics); `MatchSurfaceFP.execute` was fixed to route through
   it (it previously passed a LIST as the target face and always
   errored) with persistent MatchMethod/MatchGrade string properties.
   Five headless tests (identical, curved, reject, method, FP).
 - Fixed two real bugs the G2 work exposed: (1) `_nearest_facing_sides`
   paired transverse edges on midpoint luck, folding seams --
   pairing is now direction-aware (parallel preferred, distance
   fallback preserved); (2) QA's G2 finite differences stepped
   outside the parameter domain AND carried O(J*eps) jerk-truncation
   error that swamped genuine G2 seams -- `surface_deviation` now
   uses analytic `getDN` first (inward FD fallback); (3)
   `groups.apply_input_visibility` crashed on single-Link properties
   (`'Part.Feature' object is not iterable`), which had silently
   broken every Match execute.
 - Hardened the per-move snap sweep further (`Access violation`
   persisted after degenerate-edge filtering): the stand-in ray/axis
   segment fed to OCCT is now bounded by the candidates' bbox
   (`_bounded_ray_half_len`: eye-to-box + span, e.g. 61 units for a
   small model) instead of a fixed 2e7-unit edge, which forced
   BRepExtrema over an enormous parameter range; plus normalized
   two `SoLineSet.numVertices.setValues(0, [...])` calls to the
   proven 3-arg form (headless `test_snaputil_ray_span_is_bbox_bounded`).
 - Fixed repeating `Access violation` while drawing with Snap on:
   every mousemove ran OCCT `distToShape` against every document
   edge with no validity filtering, and BRepExtrema segfaults
   (instead of raising -- uncatchable by try/except) on degenerate
   input such as null shapes or zero-length edges. New
   `snaputil._valid_dist_shape` rejects those up front; applied at
   collection time in `collect_snap_shapes` plus defensively in all
   three `distToShape` loops and the intersection-pair scan
   (headless `test_snaputil_skips_degenerate_edges`). The panel
   also recomputes the OCCT snap only after the cursor moves > 2 px
   instead of on every mousemove event.
 - Hardened `RFS_Sketch3D` against repeated `Access violation`
   crashes while drawing: all Coin3D scene-graph removals now go
   through `_safe_remove_child` (membership-checked via `findChild`
   -- a blind `removeChild` on a detached node faults inside Coin
   instead of raising, surfacing as `Access violation` rather than a
   catchable exception); the snap marker overlay now rebuilds only
   when the snap point actually moves instead of on every mousemove;
   the triad pick logic was factored from three duplicated
   `SoRayPickAction` copies into one `_pick_triad` helper with null
   guards, and placement clicks landing on the triad now go to the
   gizmo instead of also dropping a point.
 - Fixed `RFS_Sketch3D` plane-triad crash on panel open
   (`SoMFInt32_setValue expected at most 2 arguments, got 3`):
   `SoLineSet.numVertices.setValue(0, 2)` is not a valid pivy
   overload -- replaced with `setValues(0, 1, [2])`, matching every
   other `numVertices` call in the file. Also deleted a ~105-line
   dead duplicate of the triad methods (`_setup_plane_triad` /
   `_make_triad_axis` / `_make_triad_center` / `_remove_plane_triad`)
   that was shadowed by the newer copies; the live versions are the
   ones with node names and event-callback cleanup.
 - Tier 3 ecosystem in `RFS_Sketch3D`: sketch templates (Save/Load
   template buttons serialize points, constraints, segment types,
   bulges, and variables to a JSON file via file dialog; load
   rebuilds the sketch under a single undo step), break-apart
   (one Part::Feature per detected closed loop) and merge-selected
   (append selected wires' edge endpoints as Line rows, undoable),
   plus a section-view toggle (Coin3D clipping plane aligned to the
   construction plane, cleaned up on accept/reject).
 - Fixed `RFS_Sketch3D` panel-open crash (`AttributeError:
   _plane_grid_root` / `_drawing`, repeated ~21x in the Report view):
   combo population emits signals during `__init__`, but the state
   attributes lived halfway down the constructor. All panel state now
   initializes first, and `_update_status` / `_on_plane_changed` /
   `_table_changed` return early until the panel is fully built
   (`_ready` flag).
 - Tier 1 interaction polish in `RFS_Sketch3D`: snapshot-based
   undo (Ctrl+Z / Undo button covers points, constraints, solves,
   drags, variables), constraint badges (3D crosses at every
   constraint anchor in the dim overlay), hover readout (status line
   shows the live tooltip when the cursor nears a badge), Enter
   loads the hovered constraint into the editors, and list-item
   tooltips stay live across edits (`constraint_anchor`,
   `constraint_glyph`, `constraint_tooltip` in `panel_logic`,
   covered by new headless tests).
 - Tier 2 power features in `RFS_Sketch3D`: mirror about XY/YZ/ZX
   via a new `mirror` solver constraint (LM + relaxation, headless
   `test_solve_point_constraints_mirror`) with Mirror-now plus Live
   auto-pairing and drag-partner sync; corner chamfer
   (`core.wire_corner_chamfer`, constant or per-corner setback,
   headless test) beside the fillet checkbox; Bezier segment type
   with per-span bulge tension in `core.sketch3d_wire` (headless
   test); driving-dimension `=expressions` with named variables
   (safe AST evaluator, persisted on the Sketch3D object and
   re-resolved on every recompute, headless tests); project-selected-edges
   onto the construction plane; and Use-as-Loft-profile/rail handoff
   buttons that accept the sketch and open Loft prefilled.
 - Live drag-to-edit points plus keyboard shortcuts in `RFS_Sketch3D`:
   a "Drag points" toggle grabs any sketch point in the 3D view and
   moves it on the construction plane (snap-aware, axis-lock aware)
   with the X/Y/Z spinboxes updating live and the dimension preview
   rebuilding during the drag. Viewport shortcuts: X/Y/Z toggles the
   axis lock, Tab cycles the plane family (Camera/XY/YZ/ZX/Auto), Esc
   drops the active drag. Pure helpers (`apply_axis_lock`,
   `cycle_plane`) live in `panel_logic`, covered by
   `test_apply_axis_lock` / `test_cycle_plane`.
 - `RFS_Sketch3D`: true 3D sketch authoring. New
   `rfstudio/panel_sketch3d.py` task panel -- click points onto an
   offset construction plane parallel to the camera, snap to arbitrary
   existing 3D geometry (reuses snaputil), or type X/Y/Z directly
   per point, then bake to any supported parametric curve kind via
   `parametric.make_curve`. Pure decision logic (construction_plane /
   ray_plane_intersect / sketch3d_readiness / points_are_planar)
   extracted to `rfstudio/panel_logic.py` and covered headlessly by
   `test_panel_logic.py`; end-to-end geometry covered by
   `test_sketch3d_interp_curve_through_nonplanar_points` in
   `test_core.py`. Inspired by the limitation that the existing
   draw tools place points on the view plane only (always planar).
 - Dimension-driven sketch: the 3D sketch panel now has a
   `Dim P_i -> P_j = d` constraint list; "Solve dims" runs the
   points through `core.solve_point_constraints` (damped LM), so a
   typed length actually moves the movable points — the SW
   "dimensions drive the sketch" workflow. Headless test:
   `test_sketch3d_dim_solve_drives_points`.
 - Mixed drafting entities in one sketch: per-segment entity tags
   (Spline / Line / Arc, AutoCAD-bulge arcs) on each point row;
   accept emits either the classic single parametric curve (all-Spline)
   or a new parametric `Sketch3DWireFP` object rebuilt from
   `core.sketch3d_wire`. Headless test:
   `test_sketch3d_wire_mixed_entities`.
 - **Tangent / concentric / symmetric constraints** added to
   core.solve_point_constraints (both damped LM and relaxation
   fallback) and exposed as panel constraint rows. Headless tests:
   test_solve_point_constraints_tangent/concentric/symmetric.
 - **Wire corner fillet**: `core.wire_corner_fillet` rounds every interior
   corner of a general Wire (Line, Arc, BSpline, Ellipse, etc.) by
   inserting tangent circular arcs. Supports constant radius or a
   per-corner dict for Illustrator-style live-corner radii. Integrated
   into the RFS_Sketch3D panel with a "Fillet corners" checkbox and
   radius spinbox (enabled for closed loops). Headless tests:
   test_wire_corner_fillet_open_polyline / closed_polyline /
   per_corner_dict / mixed_wire.
 - **Ellipse / Rectangle segment types** added to the 3D sketch
   panel. The `sketch3d_wire` core function now supports "Ellipse"
   (half-ellipse with bulge = minor/major ratio, sign chooses side)
   and "Rectangle" (right-angled corner, bulge = turn direction).
   These integrate with the existing mixed-entity wire and parametric
   Sketch3D object. Headless test: test_sketch3d_wire_ellipse_rectangle.
   now create the object's view provider with a SolidWorks-style wire
   display: blue line color (0.2, 0.45, 1.0) and 3 px width, instead
   of the default gray hairline.
  - **On-canvas constraint creation**: new "Add Constraint" mode in the
     3D Sketch panel -- click two points in the 3D view to create a
     distance/coincide/horizontal/vertical constraint with a live
     preview line and drag-to-set-value; the constraint is added to
     the list and solved on "Solve dims". Constraint list items now
     highlight their points in the viewport when clicked.
  - **Drag-to-fillet**: when a closed loop exists, hovering near a
     corner shows a fillet handle; drag to set the radius live (like
     Illustrator/SW corner widget). Creates/updates a fillet constraint
     in the list.
  - `RFS_Sketch3D` follow-up: visible construction-plane grid in the
   3D view (two-tier, world-space lines through the plane's parametric
   basis) plus a Plane combo (Camera / XY / YZ / ZX) that switches the
   point-placement plane family on the fly -- closer to SolidWorks'
   3D-sketch plane flipping. `plane_normal_for` / `plane_basis`
   extracted to `panel_logic` and covered in `test_panel_logic.py`.
 - Native fallback providers for the last three
   single-point-of-failure actions:
   - `RFS_Analysis` → `engines.native_analysis`
     (core.surface_review_summary + review_recommendation, printed to
     the Report view), Curves delegation kept as fallback.
   - `RFS_ControlPoly4` → `engines.native_control_poly4`
     (core.control_poly_curve, degree-3 from 4 points), Silk's
     ControlPoly4 kept as fallback.
   - `RFS_StarPatch` → degraded native last resort
     `engines.native_star_patch` (core.n_sided_patch, exact G0/G1
     only) behind Silk's true-G2 ControlGridNStar66 in the chain.
   Every curated action now has a native provider in its chain
   (`deps.report_single_points_of_failure` reports `[]`).
 - `engines.validate_providers()`: static, headlessly-runnable wiring
   check of every provider chain (known provider kind, native names
   resolve on the module, engine mod is a known mod, every DELEGATIONS
   entry has a chain). Wired into workbench.Initialize and covered by
   `test_engines_validate_providers_healthy` /
   `test_engines_validate_providers_catches_typos`.
 - `native_analysis` face-selection helper `_selected_faces()`.
 - New core tests: n_sided_patch square smoke test, 4-point degree-3
   control_poly_curve, last-three native fallback chain assertions.
 - `issues.md` populated with the six tracked limitations (trim guard,
   GUI coverage, star-patch G2, native field-testing, mirror welding,
   early-GUI verification skip).
 - Associative projection / convert-entities (issues.md #14, 3 of 4
   bullets): "Project selected edges" now records a per-point source
   reference instead of a dumb coordinate copy. `Sketch3DFP` gained
   `ProjectedRefs` (index-aligned with Points: `edge|obj|EdgeN|frac|
   mode|dir` / `slice|...`), `ExternalSources` (link deps so a moved
   source touches the sketch), and `PlaneOrigin`/`PlaneNormal`
   (execute() has no access to the panel's live construction plane);
   `execute()` re-projects every referenced point from CURRENT source
   geometry and pins those points in the solve (source drives them,
   like an on-curve constraint -- a distance dim between a projected
   and a free point moves only the free one); missing/deleted sources
   degrade to free points. New "Along plane normal / Along view
   direction" combo (captured direction stored per ref, so rebuilds
   stay deterministic) via `panel_logic.project_point_along`; new
   "Slice by sketch plane" button cuts selected solids with the
   construction plane as associative `slice` refs (re-cut on
   recompute). Panel keeps refs 1:1 with rows across add/remove/
   mirror/undo/template/merge, and the accept() gate now routes
   projected-only sketches through `Sketch3DFP`. Headless:
   `test_project_point_along`, `test_proj_ref_roundtrip`,
   `test_sketch3d_assoc_projection_follows_source`,
   `test_sketch3d_slice_projection_follows_source`.
 - Silhouette projection (issues.md #14, last bullet): "Silhouette"
   button samples the selected solids' edge-level silhouette along the
   current projection direction (`panel_sketch3d._silhouette_edges` —
   edges whose adjacent faces disagree in facing; front/back/grazing
   sign test on face-center normals) as associative `sil` refs,
   re-derived on recompute. Exact for polyhedral solids; smooth
   surfaces only contribute their edge-level approximation (their true
   silhouette crosses face interiors). Headless:
   `test_sketch3d_silhouette_follows_source` (box = 8 rim edges,
   follows a moved source).
 - Pierce / On Surface relations (issues.md #7, 2 of 3 bullets): the
   `pt#` spinbox + "Pierce to selection" / "On face to selection"
   buttons attach an existing point to external geometry through the
   same record/re-derive/pin machinery as #14 — no solver Jacobian
   needed. Pierce = plane-edge crossing via sampled bisection
   (`_pierce_point`), or for a face the nearest point of the plane n
   face section (`_nearest_on_edges`); never-crossing sources leave the
   point free. On Surface anchors in the face's parameter space (UV
   learned on first derive, range-checked every recompute,
   nearest-point fallback when the source rebuilds) so the point
   travels with the face through translation AND rotation instead of
   clamping at the face boundary. Ref kinds `pierce`/`onsurf` in
   `panel_logic.parse_proj_ref`. Tangent-to-face remains open (needs a
   direction constraint in the solver). Headless:
   `test_sketch3d_pierce_and_onsurf_follow_source`,
   `test_proj_ref_relation_kinds`.
 - Per-constraint conflict badges (issues.md #8 slice):
   `core.constraint_violations` (per-constraint max-|residual|,
   aligned 1:1 with the constraint list; `fix` = 0, corrupt entries =
   0, never raises) + `panel_logic.constraint_badges` (1e-4
   threshold); `_apply_constraint_badges` paints violated dims
   light-red in the dim list and tints the point-table rows of the
   points they touch, live at the un-solved positions (drag a point
   off a dim → red before accept; recompute clears it), with a count
   on the status line ("N off-target (red)"). Headless:
   `test_constraint_violations_per_constraint`, `test_constraint_badges`.
 - `issues.md`: suggested priority map (annotation, no renumbering)
   + closing-issue conventions — the record-on-object invariant
   (index-aligned string lists rebuilt in `execute()`, graceful
   degradation) and the platform-wrap split for #10/#15 (expression
   evaluation wraps FreeCAD's engine; suppression state stays ours).
 - 3D-spline handles with G2/G3 feedback (issues.md #9): BezierCurve
    edit mode now supports G2/G3 handle types with live curvature comb
    feedback. New handle types: **G2** (curvature continuity),
    **G3** (jerk continuity), **TangentFace** (virtual intersection for
    face tangency) alongside existing Auto/Aligned/Vector/Free.
    Keyboard shortcuts: G=G2, J=G3, T=TangentFace (A/L/V/F unchanged).
    `core.enforce_bezier_handle_constraints` now computes proper G2
    condition (rh - lh = (lh_next - rh_prev)/2) and stubs G3; curvature
    comb uses analytic cubic Bezier derivatives for exact G2 (magenta)
    and G3 jerk (orange dotted). Virtual intersection handles: select a
    face then press T on a handle — the opposite handle snaps to the
    projection onto the face's tangent plane at the closest point,
    enforcing tangency during drag. Headless: `_bezier_g2_condition`,
    `_bezier_g3_condition` in `core/io.py`.
 - Equation manager + configurations (issues.md #10): headless
   `panel_logic.EquationManager` with variables (value, unit, comment,
   suppressed, linked), design table (configs with variable overrides +
   suppression per constraint/point/handle), active config switching,
   SolidWorks equations.txt import/export (round-trip), JSON
   serialization for persistence. Panel UI: "Equations / Configurations"
   group with config combo (+/- config), suppression table (kind
   constraint/point/handle + idx toggle), Export/Import equations.txt.
   Variables UI: unit combo (mm/cm/m/in/deg/rad), comment field.
   Sketch3DFP.execute() loads EqManagerState, applies active config
   variable overrides, filters suppressed constraints/points from solver.
   Headless: `test_equation_manager_basic`, `test_equation_manager_configs`,
   `test_equation_manager_suppression`, `test_equation_manager_export_import`,
   `test_equation_manager_persistence`. Linked values across objects still
   open (needs doc-level EquationManager sharing).
 - Tangent-to-face constraint (issues.md #7, 3 of 3 bullets): new
   `tangent_to_face` solver constraint (`"tangent_to_face", i, j, face`)
   enforces that the direction p_i->p_j is tangent to the given face at
   the closest point to p_i. Uses numeric Jacobian (no analytic
   derivative needed). Panel: "Tangent to face" button in Advanced
   relations row — select a face, set pt#, click to add constraint.
   Relaxation fallback: projects p_j onto face's tangent plane at
   closest point to p_i. Headless: `test_sketch3d_tangent_face_follows_source`.
 - Multi-plane 3D sketch (issues.md #11): per-point plane
    assignment in `_PointRow` ("Pi" combo: Global + dynamic Local N).
    Local plane creation UI: "Through 3 points", "Offset from face",
    "Angle to plane" modes creating (origin, normal, u, v) in
    `_local_planes`. Per-row plane indices in `_point_planes` (1:1 with
    rows). Plane switching toggle "Switch: Global/Local" for Tab-cycling.
    Storage: `_plane_data` local-plane registry (index 0 = None =
     Global, 1..n = local planes) + each row's Pi combo selection
     (combo index == registry index), serialized row-aligned by
     `_serialize_point_planes()`.
    **Solver integration**: `core.solve_point_constraints` accepts
    optional `point_planes` argument; points on local planes are
    orthogonally projected each iteration, horizontal/vertical
    constraints evaluated in local coordinates. `Sketch3DFP.PointPlanes`
    StringList persists per-point planes (index-aligned with Points).
    Headless: existing tests pass; integration verified by existing
    projection tests continuing to pass.
 - Live placement preview in the Sketch3D panel: clicking points now
   draws as you go. A pale-cyan polyline connects every placed row
   (repainted from `_update_status()`, the funnel every row mutation
   already goes through) and a pale-yellow rubber band runs from the
   last placed point to the cursor (honors snap target, axis lock and
   plane offset, hides when placement stops). Torn down with the other
   overlays on accept/reject. Previously the viewport showed only the
   plane grid/triad/snap crosshair, so a sketch looked blank until
   Accept created the curve.

### Changed
 - Fillet panel dual-model clarity: the legacy (nothing-selected)
   flow now states up front that it creates a LINKED `PolylineFillet`
   object and how to get in-place filleting instead (select the
   polyline BEFORE running the tool); the no-source status line gives
   the exact next action ("select one in the 3D view, then press
   'Set source from selection'") instead of "Pick a polyline to
   round.".
 - `engines.run_engine_command` now catches exceptions raised while an
   engine command runs and returns False, so ProviderCommand falls
   through to the next provider instead of propagating a traceback.
 - `commands.py` delegate tooltips reflect the new fallback reality
   (native-first / native-last-resort per action).
 - `issues.md` / `package.xml` version bump to 0.1.0.

### Fixed
- `Sketch3DPanel` dim-preview row was stranded inside
  `_remove_constraint_highlight()` -- built against an undefined `lay`
  (NameError swallowed by that method's caller's `except Exception`),
  so `dim_preview_btn` was never added to the panel, `_ready` never
  became True (the status line never updated after build), dim-list
  clicks died before drawing their highlight, and every other
  `dim_preview_btn` use raised AttributeError. The block (`dim_preview
  btn` + `_ready = True` + `_update_status()`) is back at the end of
  `__init__` where it belongs. Caught by the GUI harness's bare-name
  check -- the only check in the suite that saw it.
- Harness itself: `_BUILTIN_NAMES = set(dir(__builtins__))` silently
  became *dict* methods when `test_gui_imports` was imported instead of
  run (2066 false positives) -- now `dir(builtins)`; dead duplicate
  helper copies (three functions defined twice) removed.
  `panel_logic.points_form_closed_loop` was defined twice with
  identical bodies (second shadowed first) -- one copy removed.
- Workbench load failure (`name 'QtWidgets' is not defined`): the
  shared `_CollapsibleGroup`/`_ShortcutHintBar` helpers were classes
  subclassing `QtWidgets.*` at import time, but `panel_base.py` loads
  Qt lazily. They are now factory functions (same call API:
  `_CollapsibleGroup("Advanced")`, `.content_layout`,
  `.set_shortcuts`), so plain `import panel_base` works headless and
  at startup again.
- `rfstudio.editmode.KnotEditor` restored: a scripted edit deleted the
  `class KnotEditor(object):` header line, silently folding the whole
  class body (35 methods) into `PointEditor` as stray methods. The
  module still imported and `PointEditor` worked, but the curve
  panel's "Edit knots" path raised
  `module 'rfstudio.editmode' has no attribute 'KnotEditor'` roughly
  19 times per session from `panel_curve.py`. Verified after the
  one-line restore: all four editor classes present, no within-class
  method shadowing introduced by the fold-in, `panel_curve` imports
  clean headless.
- `panel_sketch3d._remove_plane_triad`: stale event-callback IDs are
  now nulled after removal (and on the early-return path), so a failed
  triad setup can no longer leave dangling `addEventCallback`
  registrations behind on the view.
- The inert `match_surface_exact_g1` trim guard (issues.md #1):
  `_exact_match_prep` (shared by exact_g1/_g2) now checks
  matched-edge compatibility (`_matched_edges_compatible` in
  `rfstudio/core/util.py`): the two boundary edges must coincide
  geometrically (length within 1%, endpoints coincident), else it
  refuses with ValueError and falls back to sampled, trim-aware
  matching. Row-copying is only valid when the two edges are the same
  curve, regardless of trim history -- this catches the trim case
  (lengths 10 vs 6) that parameter-range checks cannot see, since
  `trim_face` keeps the original surface object. `_side_is_trimmed`
  was also fixed to compare against the surface's natural bounds
  (BSpline knots, analytic extents) instead of the possibly
  reparameterized face range, and a broken world-axis extent check
  that false-positived on non-XY-planar faces was removed.
- Mirror-module headless tests re-aligned with the current
  `(base, normal)` plane API and `(shape, welded)` return contract of
  `rfstudio.mirror` — `test_plane_base_normal`, `test_mirror_point`,
  `test_find_symmetric_pairs`, `test_mirror_shape` all pass again.
- `test_match_surface_exact_g1_rejects_trimmed_boundary`: was asserting
  a refusal the old parameter-range guard could never trigger. Since
  the matched-edge compatibility check landed (above), the guard is no
  longer inert, and the test now asserts the real `ValueError` again
  (lengths 10 vs 6).
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
- `Sketch3DPanel.accept()` created no curve for simple sketches: the
  9b2d671 tech-debt pass deleted the `else:` branch
  (`obj = parametric.make_curve(...)`) while rewriting the PointPlanes
  serialization, so an unconstrained spline sketch (no mixed segments,
  no projections) hit `NameError: name 'obj' is not defined` at
  `self._handoff_obj = obj` -> "create failed (name 'obj' is not
  defined)", the dialog stayed open, and no object was ever created.
  Branch restored; planes set best-effort via the new
  `_serialize_point_planes()`.
- Issue #11 plane storage (9b2d671 regression): the per-row
  `_point_planes` list and the `_local_planes` registry were merged
  into one `_plane_data` array, so `_add_row` appended one entry per
  row WHILE `_create_local_plane` appended plane tuples to that same
  list -- `_refresh_plane_combos` then offered one phantom "Local N"
  item per placed row, accept() serialized registry entries misaligned
  with Points, and the row's Pi selection was never serialized at all
  (nothing ever read `currentIndex()`). `_plane_data` is now purely
  the local-plane registry (`[None, local1, ...]`; combo index ==
  registry index): rows build their combo items from it, the combo is
  the single source of truth read by `_serialize_point_planes()` at
  accept, and undo snapshots + JSON templates carry the registry and
  per-row indices. Also fixes the pre-existing off-by-one that
  rejected the LAST local plane (`plane_idx >= len(_local_planes)`).

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
