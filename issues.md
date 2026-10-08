# Railform Studio — known issues / limitations

Tracked gaps, ordered by severity. Each entry links to where the
limitation lives in code so it doesn't have to be rediscovered.

## Suggested priority map (annotation, not a renumber)

Renumbering would break CHANGELOG/commit references — statuses are
appended per issue instead. Cross-issue view as of 2026-10:

- **Done:** #1 (trim guard hardened, see its Update), #2 (GUI harness:
  6 static checks + mutation selftest), #9 (3D-spline handles w/ G2/G3
  feedback), #10 (equation manager + configs), #13 (trim-aware exact G2),
  #14 (associative projection, slice, direction modes, silhouette).
- **Major slice landed, remainder open:** #7 (pierce + on-surface
  via re-derive; tangent-to-face still needs a solver direction
  constraint), #8 (global verdict + per-constraint/point badges;
  per-DOF breakdown, drag-to-diagnose, auto-repair open).
- **P1 next:** #15 (configs, builds on #10), #11 (multi-plane).
- **P2:** #12 (assembly context — reuses the #7 external-ref machinery), #16
  (multi-body).
- **Open small:** #3 (star-patch G2), #4 (field-testing), #5 (mirror
  welding), #6 (startup skip).

## Conventions for closing issues

**Record-on-object (the in-place template).** Parametric behavior that
must survive document reloads is stored as index-aligned string lists
on the FP object (`PolylineFP.Fillets`, `Sketch3DFP.ProjectedRefs`,
`Constraints`) and rebuilt in `execute()` on recompute. Invariants:
keep the record 1:1 with its index base at EVERY mutation point
(add/remove/mirror/undo/template/merge — pad with `''`, pop, snapshot);
store plain strings (repr floats, `|` separators) so documents stay
portable; degrade gracefully on missing/corrupt entries (free point,
never a failed recompute).

**Platform-wrap (decide before building evaluators).** Prefer FreeCAD's
own expression engine / Assembly workbench / TechDraw where they fit.
Split for #10/#15: expression *evaluation* should wrap FreeCAD
expressions (months saved); *suppression state* (config-dependent
on/off) has no platform support and stays in our records. #12 is
external references (cross-document links + the #7/#14 re-derive
machinery) — not Assembly WB constraint solving.

## 1. `match_surface_exact_g1` trim guard is currently inert

`core.match_surface_exact_g1` refuses (ValueError) when the matched
boundary is a genuine trim fragment of a larger surface
(`_side_is_trimmed` guard in `rfstudio/core/surfacing.py`). However,
`core.trim_face` **reparameterizes** the trimmed surface to the wire's
bounding extent, so a trim-face-produced face looks exactly like a fresh
untrimmed rectangular face and the guard never fires.

Consequence: no silent corruption (the cropped surface is self-consistent),
but the documented "exact copy is wrong on trimmed input" protection is
effectively dead for faces produced by our own trim path. `test_core.py`'s
`test_match_surface_exact_g1_rejects_trimmed_boundary` pins the current
behavior so the invariant change is loud if it ever returns.

Fix options: have `trim_face` preserve the original parameterization
(more honest geometry, larger blast radius), or build the guard on
edge/wire shape analysis rather than parameter ranges. Parked for now.

Update: `_exact_match_prep` (shared by exact_g1/_g2) now ALSO checks
matched-edge compatibility (`_matched_edges_compatible` in
`rfstudio/core/util.py`): the two boundary edges must coincide
geometrically (length within 1%, endpoints coincident), else it refuses
with ValueError and falls back to sampled, trim-aware matching. This
catches the trim case the trim guard misses (trimmed plane vs full
plane: lengths 6 vs 10), because row-copying is only valid when the two
edges are the same curve -- regardless of trim history. The trim guard
itself was also fixed to compare against the surface's natural bounds
(BSpline knots, analytic extents) instead of the possibly
reparameterized face range, and a broken world-axis extent check that
false-positived on non-XY-planar faces was removed.

## 2. GUI panels have no automated coverage

Silk/Curves import `FreeCAD.Gui` at load, so anything importing them
(every `rfstudio.panel_*`) cannot run under `freecadcmd`. The curated
panels are therefore validated only by the manual smoke test in
`README.md`. Decision logic that CAN be checked headlessly lives in
`rfstudio/panel_logic.py` + `test_panel_logic.py`; keep pushing
triage-style decisions there when adding panels.

**Status (W1 fix):** the static half is now automated —
`rfstudio/test_gui_imports.py` fakes FreeCAD/Qt/Coin and runs six
checks over all 25 modules (mocked imports, undefined self-calls,
scope-aware bare names, cross-module reference resolution, no
import-time Qt class bases, callback add/remove/null symmetry), with
a planted-bug self-test. It caught the stranded `dim_preview_btn`
block in `panel_sketch3d` the same day. FUNCTIONAL GUI behavior
(widgets rendering, viewport interactions) still needs the manual
smoke test — that half remains open.

## 3. RFS_StarPatch is true-G2-only-via-Silk

It now has a native last resort (`engines.native_star_patch` over
`core.n_sided_patch`), but that path is exact G0 + tangent-plane G1 only
by construction. If you need Silk's G2 star point, Silk must be
installed or its `ControlGridNStar66` command vendored frozen.

## 4. Native providers are geometry-tested, not field-tested

`native_trim`, `native_sketch_on_surf`, `native_clean_patch_*`, and the
newer `native_analysis`/`native_control_poly4`/`native_star_patch` are
covered by `test_core.py` at the geometry level and raised-value-error
convention, but have not been exercised against every selection shape a
real user will throw at them. Bug reports welcome; each raises ValueError
on bad selections so ProviderCommand falls through to the engine command
instead of silently no-op'ing.

## 5. Mirroring welding is best-effort

`rfstudio.mirror.mirror_shape(..., weld=True)` stitches original+mirror
via the injected `stitch_fn` and returns `(shape, False)` on any failure
— welding visually reports success-shape only when `welded=True` comes
back. There is no automated box-level weld verification yet
(`stitch_shell` itself is tested).

## 6. `verify_delegations` may be skipped early in GUI startup

`workbench.Initialize` wraps `deps.verify_delegations` in try/except
because a fresh GUI session sometimes hasn't finished registering engine
commands yet; in that case the startup health check prints "skipping"
and the check effectively does not run. Acceptable for now, but it means
the "whole curated surface validated at activation" promise is
best-effort.

## 7. Pierce / On Surface / Tangent to Face relations — unlocks in-context design

The 3D sketch constraint system (`rfstudio/panel_sketch3d.py` +
`rfstudio/core/constraints.py`) currently supports distance/angle/coincident/
horizontal/vertical/tangent/equal/fix/symmetric/mirror. Missing the
in-context workhorse relations:

- **Pierce** — sketch point constrained to intersection of sketch plane
  with a non-planar face/edge from another body
- **On Surface** — point constrained to lie on a non-planar face (not
  just sketch construction plane)
- **Tangent to Face** — curve/edge tangent to a non-planar face along
  its extent (not just 2D tangent)

These require per-constraint projection/nearest-point queries against
arbitrary topology, not just the sketch's construction plane.
`rfstudio/core/snaputil.py`'s `pick_ray` / `nearest_point_on_shapes_to_ray`
infrastructure is the starting point, but the constraint solver
(`core/constraints.py`) would need new constraint tuple formats and
Jacobian contributions.

Gate: enables true in-context design (brackets, ribs, cutouts referencing
adjacent parts).

**Status (implemented, 2 of 3 bullets):** pierce and On Surface landed
WITHOUT the solver Jacobian work this issue originally assumed — they
use the #14 record/re-derive machinery: refs in `ProjectedRefs`
(`pierce|obj|EdgeN/FaceN|0|n|`, `onsurf|obj|FaceN@u,v|0|n|`) resolved in
`Sketch3DFP.execute()` and pinned in the solve (enforcement =
re-projection). Pierce = plane-edge crossing via sampled bisection
(`_pierce_point`) or, for a face, the nearest point of the plane ∩ face
section (`_nearest_on_edges`); never-crossing edges leave the point
free. On Surface anchors in the face's **parameter space** (UV learned
on first derive, range-checked every recompute, nearest-point fallback
when the source rebuilds) so the point travels with the face through
translation AND rotation instead of clamping at the face boundary.
Panel: `pt#` spinbox + "Pierce to selection" / "On face to selection"
in Advanced. Headless: `test_sketch3d_pierce_and_onsurf_follow_source`.
Still open: **Tangent to Face** — a direction constraint needs a real
Jacobian contribution in `core/constraints.py` (not expressible as a
point re-derive).

## 8. Per-entity under/over-defined diagnostics — single biggest daily usability gap

Current `rfstudio/panel_sketch3d.py` shows only a global readiness string
(`_update_status` → `panel_logic.sketch3d_readiness`). SolidWorks
per-entity diagnostics need:

- Per-row under/over/fully-defined badges in the point table
- Highlight the specific constraints/points causing over-definition
- Drag-to-diagnose: click an over-constrained point → show conflicting
  constraints
- Auto-repair suggestions (delete redundant constraint, relax dimension)

Requires constraint Jacobian rank analysis per DOF (already partially
available in `core/constraints.py`'s status dict) surfaced to the
table widget rows (`panel_sketch3d.py` `_PointRow` widgets).

Gate: turns "why won't this solve?" from minutes of guessing into
seconds of looking.

**Status:** global slice landed — `_solver_diag` runs the solver on
the current points/constraints and `panel_logic.solver_status_text`
appends "well-defined (N DOF)" / "under-defined: X of Y DOF free" /
"over-defined: constraints conflict — remove or relax one" to the
status line whenever dims exist. **Per-constraint badges landed:**
`core.constraint_violations` (per-constraint max-|residual|, aligned
with the constraint list; `fix` = 0, corrupt entries = 0, never
raises) + `panel_logic.constraint_badges` (1e-4 threshold) paint
violated dims light-red in the dim list AND tint the point-table rows
of the points they touch (`_apply_constraint_badges`), live at the
un-solved positions — drag a point off a dim and it goes red before
accept (recompute clears it); the status line counts them
("N off-target (red)"). Headless: `test_constraint_violations_per_constraint`,
`test_constraint_badges`. Still open: true per-row under/over/fully
badges, drag-to-diagnose, auto-repair (need the per-DOF Jacobian rank
breakdown the status dict doesn't expose yet).

## 9. 3D spline handles with G2/G3 feedback — makes 3D splines actually editable

Current 3D spline editing (`rfstudio/editmode.py` + `panel_sketch3d.py`'s
drag callbacks) supports point drag only. Missing:

- **Handles per control point** — tangent vectors (G1) and curvature
  vectors (G2) as draggable handles in the 3D viewport
- **Live G2/G3 curvature comb feedback** on the spline during drag
  (reusing `rfstudio/core/qa.py`'s analytic `getDN` path)
- **Handle snapping** to G1/G2 continuity with adjacent spans
- **Virtual intersection handles** for tangency-to-face constraints

`rfstudio/editmode.py`'s `PointEditor` already has a gizmo framework;
the missing piece is the handle-to-control-point mapping and the
curvature comb overlay (`rfstudio/core/qa.py` has the analytic
derivatives via `_surf_dn` / `_analytic_cross_d2`).

Gate: makes 3D splines usable for Class-A work instead of "place
points, hope the interpolation behaves."

**Status (implemented):**
- **Handle types**: Auto (smart G1/G2), Aligned (G1 opposite), Vector (G1 aligned), Free, **G2** (curvature continuity), **G3** (jerk continuity), **TangentFace** (virtual intersection for face tangency)
- **Keyboard shortcuts**: A=Auto, L=Aligned, V=Vector, G=G2, J=G3, F=Free, T=TangentFace
- **Curvature comb visualization**: Live G2 (magenta) and G3 (orange) combs updated during drag using analytic cubic Bezier derivatives
- **G2/G3 handle snapping**: `core.enforce_bezier_handle_constraints` now computes proper G2 condition (rh - lh = (lh_next - rh_prev)/2) and stubs G3
- **Virtual intersection handles**: `PointEditor._tangent_targets` stores face targets; `_compute_virtual_intersection` projects opposite handle onto target face's tangent plane; `_enforce_tangent_face` enforces during drag
- **Headless**: `_bezier_g2_condition`, `_bezier_g3_condition`, `enforce_bezier_handle_constraints` in `core/io.py`

Verified: `test_core.py` passes; live drag with curvature comb feedback; handle type switching via keyboard shortcuts.

## 10. Equation manager + configurations — moves from "one-off" to "reusable design"

Current `rfstudio/panel_sketch3d.py` supports inline expressions
(`=P0x+25`, `=2*width`) and named variables (`_variables` dict).
Missing for production reuse:

- **Global equation manager dialog** — named variables with units,
  comments, suppression state, import/export CSV
- **Design table / configuration table** — rows = configurations,
  columns = variables + suppression; switch configs without rebuilding
- **Suppression state per constraint/point** — "suppressed in Config B"
- **Linked values / shared variables** across multiple sketch objects
- **Import/export SolidWorks-style equations.txt** for round-trip

The expression evaluator (`rfstudio/panel_logic.py` `evaluate_expression`)
and variable storage (`_variables` dict + `_variables` addProperty on
the FP object) are the foundation. The missing piece is the
config-table data model and the UI.

Gate: turns single-use sketches into parametric families.

**Status (implemented):**
- **EquationManager** (headless `panel_logic.EquationManager`): variables with metadata (value, unit, comment, suppressed, linked), configurations (design table), suppression per constraint/point/handle, active config switching, SolidWorks equations.txt import/export (round-trip), JSON serialization for persistence.
- **Panel UI** (issues.md #10 / #15): "Equations / Configurations" collapsible group with config combo (+/- config), suppression table (kind: constraint/point/handle + idx toggle), Export/Import equations.txt buttons.
- **Sketch3DFP.execute()** integration: loads EquationManager state from `EqManagerState` property, applies active config variable overrides, filters suppressed constraints/points from solver.
- **Variables UI**: unit combo (mm/cm/m/in/deg/rad), comment field, metadata shown in list.
- **Headless tests**: `test_equation_manager_basic`, `test_equation_manager_configs`, `test_equation_manager_suppression`, `test_equation_manager_export_import`, `test_equation_manager_persistence`.
- **Still open**: Linked values across multiple sketch objects (needs document-level EquationManager sharing + UI for linking).

## 11. Multi-plane 3D sketch — eliminates the single-plane limitation

Current `rfstudio/panel_sketch3d.py` uses one construction plane per
session (`_plane_combo` + `_offset` + `_plane_base`/`_plane_normal`).
SolidWorks-style multi-plane 3D sketch needs:

- **Per-point plane assignment** — each point can live on a different
  reference plane (existing plane, created plane, or inferred from
  adjacent geometry)
- **Plane creation in-sketch** — "create plane through 3 points" /
  "offset from face" / "angle to plane" without leaving the sketch
- **Plane-aware constraints** — distance/angle measured along/normal to
  the point's local plane, not the sketch's global construction plane
- **Plane switching** — Tab cycles through planes used in the sketch,
  not just Camera/XY/YZ/ZX/Auto

`rfstudio/panel_logic.py` already has `construction_plane`,
`ray_plane_intersect`, `plane_normal_for` — the building blocks exist.
The missing piece is per-point plane metadata in the point table
(`panel_sketch3d.py` `_PointRow` adding a plane selector) and the
constraint solver respecting per-point planes.

Gate: removes the "flatten to one plane" workaround that forces
workarounds for non-planar 3D wireframes.

workarounds for non-planar 3D wireframes.

**Status (implemented):**
- **Per-point plane selector**: `_PointRow` now has a "Pi" combo with "Global" + dynamic "Local N" entries, built from the `_plane_data` registry; the selection lives in the combo itself (combo index == registry index).
- **Local plane creation**: "Planes" UI row with modes "Through 3 points", "Offset from face", "Angle to plane" — builds local plane (origin, normal, u, v) appended to the `_plane_data` registry (index 1..n; index 0 = None = Global).
- **Plane switching**: "Switch: Global/Local" toggle button for Tab-cycling planes used in the sketch.
- **Storage**: `_plane_data` local-plane registry `[None, (origin, normal, u, v), ...]` (combo index == registry index) + each row's combo selection as the per-point plane; accept() writes row-aligned `PointPlanes` via `_serialize_point_planes()`, with undo snapshots and JSON templates carrying both the registry and per-row indices.
- **Solver integration**: `core.solve_point_constraints` now accepts optional `point_planes` argument. Points assigned to local planes are orthogonally projected to their plane on each solver iteration, and horizontal/vertical constraints are evaluated in local plane coordinates. `Sketch3DFP.PointPlanes` StringList property persists the per-point planes (index-aligned with Points).
- **Headless**: existing tests pass; new integration verified by `test_sketch3d_assoc_projection_follows_source` and `test_sketch3d_slice_projection_follows_source` continuing to pass.
- **Still open**: Plane creation from 3 picked vertices during placement mode (UI flow).

## 12. Assembly context / convert entities — in-context design
## 12. Assembly context / convert entities — in-context design

No mechanism exists to reference geometry from other parts in the
assembly while editing a sketch or surface. Required for true
in-context design (brackets, ribs, cutouts referencing adjacent parts):

- **Convert entities** — project edges/vertices from other parts into
  the active sketch as reference geometry (with or without external
  references)
- **External references** — track dependencies on external geometry;
  flag when referenced parts change; allow update/break/replace
- **In-context editing** — open a part in the context of its assembly,
  with other parts visible (transparent/ghosted) for reference
- **Reference plane/axis from assembly geometry** — create sketch
  planes/axes from faces/edges of other components

Requires: assembly document structure awareness, external reference
tracking (property `ExternalReferences` on sketch objects), and UI
for managing reference state (update/replace/break).

Gate: without this, every bracket/rib/cutout must be modeled
isolated then manually positioned — the #1 productivity killer for
assembly-driven design.

## 13. Trim-aware exact G2 — currently falls back to sampled on trimmed faces

`match_surface_exact_g2` (like its G1 sibling) refuses trimmed
boundaries via `_side_is_trimmed` guard. However, `trim_face`
reparameterizes the trimmed surface to the wire's bounding box, so
a trimmed face looks identical to an untrimmed one to the guard —
the exact G2 path is silently skipped and falls back to sampled
`match_surface()`, losing exact G2.

Fix: either (a) make `trim_face` preserve original parameterization
(larger blast radius), or (b) detect trimming via edge/wire topology
rather than parameter ranges. The latter is cleaner: check if the
matched boundary edge is a subset of the surface's natural boundary.

Gate: without this, any Class-A patch meeting a trimmed edge
falls back to sampled approximation, losing exact G2 at the seam.

**Status (implemented):** `_side_is_trimmed` compares against the
surface's natural parameterization bounds (BSpline knots, analytic
extents) instead of the possibly-reparameterized face range, and both
exact-g1/_g2 additionally run `_matched_edges_compatible` (geometric
edge-congruence check) — trimmed or mismatched inputs now refuse with
ValueError and fall back to sampled, trim-aware matching instead of
silently pretending. Pinned by `test_match_surface_exact_g1_rejects_
trimmed_boundary` and the exact-g2 reject/fallback tests.

## 14. Convert entities / project to sketch — project 3D edges into active sketch

Sketch3D panel has "Project selected edges" but it only samples
points and drops them on the construction plane. Missing:

- **Project with constraints** — projected points should optionally
  carry coincident/coincident-on-curve constraints to the source
  edges, so the sketch updates when the source moves
- **Project to construction plane vs. project along direction** —
  orthographic (view direction) vs. normal-to-plane projection
- **Silhouette projection** — project the silhouette of a solid
  onto the sketch plane
- **Intersection curve projection** — project the intersection of
  the sketch plane with a solid/face

Gate: without constrained projection, projected geometry is
"dumb" — it doesn't update when the source geometry changes.

**Status (implemented, 4 of 4 bullets):**

- **Project with constraints** — each projected point now stores an
  associative ref (`Sketch3DFP.ProjectedRefs`, `edge|obj|EdgeN|frac|
  mode|dir`) with `ExternalSources` link dependencies and a persisted
  `PlaneOrigin`/`PlaneNormal`; `execute()` re-projects the point from
  its current source on every recompute, and re-projected points join
  the solver's pinned set (source drives them exactly like an on-curve
  constraint). Missing/deleted sources degrade to a free point.
- **Plane normal vs. along direction** — "Along plane normal / Along
  view direction" combo in the panel; the captured view direction is
  stored per ref so rebuilds stay deterministic. Pure math in
  `panel_logic.project_point_along`.
- **Intersection curve projection** — new "Slice by sketch plane"
  button cuts the selected solids with the construction plane
  (`slice` refs, re-cut on recompute).
- **Silhouette projection** — "Silhouette" button: edge-level
  silhouette (`_silhouette_edges` — edges whose adjacent faces disagree
  in facing along the projection direction; front/back/grazing sign
  test on face-center normals), stored as `sil` refs and re-derived on
  recompute. Exact for polyhedral solids; on smooth surfaces the true
  silhouette crosses face interiors, which BREP edges can't represent —
  documented approximation, use Slice there.

Verified: `test_panel_logic.py` (project-along + ref codec),
`test_core.py` (assoc projection follows a moved source with the dim
still solved; slice section follows a moved solid; box silhouette = 8
rim edges and follows its source). Refs stay 1:1 with rows across
add/remove/mirror/undo/template/merge.

## 15. Configuration-aware equation manager — design tables with suppression

`panel_sketch3d.py` has inline expressions (`=P0x+25`) and named
variables (`_variables` dict), but no configuration table:

- **Design table** — rows = configurations, columns = variables +
  dimensions + suppression state; switch configs without rebuild
- **Suppression per constraint/point/handle** — "suppressed in Config B"
- **Configuration table UI** — spreadsheet-like grid in the panel,
  add/remove configs, copy/paste from Excel/CSV
- **Suppression per entity** — points, constraints, handles can be
  suppressed per configuration
- **Import/export equations.txt** — round-trip with SolidWorks-style
  equation files

Gate: without this, every variant requires a separate sketch file
or manual parameter editing — no parametric families.

**Status (implemented via #10 EquationManager):**
- `EquationManager` already provides: configs with variable overrides + suppression per constraint/point/handle, active config switching, equations.txt round-trip.
- Panel UI: "Equations / Configurations" group has config combo (+/- config), suppression table (kind: constraint/point/handle + idx toggle), Export/Import equations.txt.
- Sketch3DFP.execute() filters suppressed constraints/points per active config.
- **Still open**: Spreadsheet-like grid view for the design table (rows=configs, cols=variables+suppression), copy/paste from Excel/CSV, linked variables across multiple sketch objects (needs doc-level EquationManager sharing + UI).

## 16. Multi-body part support — single document, multiple solids

All parametric FP objects (`*FP` classes) assume one `Part::Feature`
= one surface/solid. Real parts often need multiple bodies (weldment
cut list, multi-body Boolean, separate manufacture bodies):

- **Multi-body FP base class** — `Part::Feature` with `Solids` list
  property instead of single `Shape`
- **Per-body properties** — material, appearance, manufacture flags
  per solid
- **Cut list / weldment BOM** — export per-body metadata
- **Body visibility/selection** — show/hide/select individual bodies
  in the tree/viewport

Gate: without this, multi-body parts (weldments, mold tools,
multi-lumen medical) require separate files or clumsy Compounds.