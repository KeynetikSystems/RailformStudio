# Changelog for Railform Studio

## [Unreleased] - 2026-07-17

### Added
- Polyline corner fillet tool (`RFS_FilletPolyline`, in the Railform Draw
  toolbar). Rounds each control-point corner of a polyline with a tangent arc
  at a single, editable radius, producing a new parametric curve. The radius is
  auto-reduced per corner where a segment is too short, and collinear corners
  are left sharp. Implemented as `core.polyline_corner_fillet` (verified
  headless), `parametric.PolylineFilletFP`, and `panel_surface.PolylineFilletPanel`,
  with a dedicated icon `rfs_polyfillet.svg`.
- Per-kind tree/view-provider icons. `CurveVP`/`SurfaceVP.getIcon()` map each
  object's `Kind` to its toolbar glyph (e.g. a Bezier curve shows the Bezier
  badge, a Loft shows the loft glyph), replacing the generic `Part` icon. Icon
  survives save/reload via `__getstate__`/`__setstate__`.
- Double-clicking a curve reopens the full parameter panel again
  (`CurveVP.doubleClicked` opens `CurvePanel`, or `PolylineFilletPanel` for a
  fillet object) instead of a bare in-viewport editor with no panel.
- "Insert point on segment click" toggle in the curve panel, mirroring
  `PointEditor.insert_on_click`.
- **Join curves** tool (`RFS_JoinCurve`, Railform Draw toolbar). Welds several
  curves end-to-end into a single `Wire` -- corners preserved, no refit
  (WYSIWYG). Edges are ordered by connectivity and the join is refused with a
  clear message if the curves form more than one disconnected chain, rather than
  silently building a broken wire. `core.join_curves` + `parametric.JoinCurveFP`
  + `panel_surface.JoinCurvePanel` + `rfs_join.svg`.
- Exact on-curve snapping: a drag can now snap to ANY point along another
  curve's real geometry via perpendicular projection
  (`snaputil.collect_snap_shapes` / `nearest_point_on_shapes` /
  `nearest_exact_snap`), with the discrete candidate list also gaining each
  BSpline edge's actual knot positions.

### Changed
- Inserting a point in edit mode is now opt-in. A plain viewport click
  selects/drags points; **Shift+click** a segment (or enable the panel toggle)
  to insert. Previously any click on the curve body auto-inserted a point. The
  insertion crosshair cursor only shows when a click would actually insert.
- Polyline now builds a single-edge **degree-1 B-spline** instead of a
  multi-edge polygon wire -- one curve edge, uniform with the other Railform
  curves and cleaner for downstream loft/sweep. Geometry is identical (straight
  between points); point-editing and the polyline fillet are unaffected.
- Axis-locked (XYZ gizmo) dragging is now exact and no longer breaks the lock.
  The point is placed at the closest point between the cursor ray and the axis
  line (was a focal-plane delta projected onto the axis -- depth-scale /
  foreshortening error), and snapping is disabled while an axis is locked
  (a nearby snap used to silently override the constraint).
- Curve `FeaturePython` classes now delegate their geometry to the tested
  `core.py` primitives (`interpolate_curve`, `control_poly_curve`,
  `approximate_curve`, `bezier_curve_shape`) instead of inline OCCT calls, and
  gained a `compute()` preview hook. See `CHANGES.md`.
- Curve-panel viewport grid is now a single sparse XY ground plane (minor lines
  every 5 units, heavier every 25, over a 200-unit extent -- ~100 segments).
  It previously drew THREE planes (XY/YZ/XZ) at 0.25 spacing over 500 units
  (~12,000 segments); zoomed out those merged into solid gray sheets that
  obscured the model from every orbit angle.
- Gizmo picking: the point marker is a POINT grab (free drag, snapping active);
  only the OUTER part of each axis arrow is an axis handle. All three arrows
  originate at the point, so a click on the marker used to match every axis.

### Fixed
- Polyline draw raised "module 'Part' has no attribute 'Polyline'" (a
  nonexistent API), so it built no Shape and drew nothing. Now built correctly
  (`makePolygon` was the interim fix, then the degree-1 B-spline above).
- BezierCurve would not draw: `HandleTypes` was declared `App::PropertyEnumeration`
  (which returns `None` when empty) but used as a per-point list, so `execute()`
  raised "object of type 'NoneType' has no len()" and aborted the recompute
  before building a Shape. Now `App::PropertyStringList`, matching `editmode.py`.
- Loft (`RFS_Loft`) crashed with `'FeaturePython' object has no attribute
  'ShowImports'` -- a typo for `ShowInputs` in `LoftFP.__init__`.
- Knot editor ("Edit knots") never started: `KnotEditor._on_button` referenced
  `PointEditor`-only `self.is_bezier` / `self._pick_handle`, and the resulting
  `AttributeError` was swallowed by a bare `except`, so every click silently did
  nothing. Removed the stray Bezier branch. Also repointed 7 knot/degree
  operations from the non-existent `KnotEditor._refresh_arrows()` to
  `_refresh_markers()`.
- Point dragging regression: `_press` had its point-selection/drag tail stranded
  as unreachable code after `_shift_held`; restored so plain-click select/drag
  works.
- **3D control-point dragging never persisted.** `PointEditor._release` never
  called `commit()`, and `commit()` referenced an undefined `idx`, so a drag
  updated only the transient Coin overlay and was discarded on mouse-up
  (`obj.Points` unchanged). `_release` now commits the dragged index and
  `commit(idx)` is repaired.
- **Knot dragging never persisted.** `KnotEditor._on_move` updated only a local
  list and the marker, and the deferred commit re-read and wrote back the
  unchanged `obj.Knots`. It now stashes the dragged value and applies it on
  release.
- Bezier-handle drags raised a swallowed `KeyError` in `_release`
  (`self.drag["idx"]` -- handle drags carry no `"idx"`); `_release` now skips the
  point-commit for handle drags, which already persist live.
- Control-point curve (`ControlCurveFP`) never drew: `execute()` called a
  non-existent `Part.BSplineCurve.createFromPolesWeightsKnots`, silently
  swallowed, so `obj.Shape` was never set. Now via `core.control_poly_curve`;
  added `Knots`/`Mults` properties so the knot editor has real data to edit.
- Bezier curve ignored its handles: `execute()` interpolated the points, so
  dragging a handle changed nothing. New `core.bezier_curve_shape` builds a real
  two-handle composite Bézier; handle drags now reshape the curve.
- Live drag-preview never rendered: FP proxies defined only `execute()`, so
  `_live`/`_preview`'s `obj.Proxy.compute(...)` raised `AttributeError`. Added
  `_BaseFP.compute()` + per-kind overrides.
- `_enforce_bezier_handle_constraints` referenced a bare `which` (`NameError`)
  and `.length` instead of `.Length`, leaving Aligned/Vector handle types inert.
- ControlCurve failed to draw with "'Part.BSplineCurve' object has no attribute
  'buildFromPolesMultsWeightsKnots'" -- that method does not exist in this OCCT
  build. The rational case passes `weights` as the LAST argument to
  `buildFromPolesMultsKnots`. Same fix in `clean_patch_surface`, which
  additionally flattened its weight grid (the surface API wants a 2D grid).
- **`snaputil.nearest_snap` had lost its `def` line**, orphaning its body as
  unreachable code inside `nearest_exact_snap`. The module still imported
  cleanly, but both callers hit `AttributeError` -- swallowed by `_on_move`'s
  blanket `except` -- so the discrete-snap fallback AND the whole drag-move
  handler silently died whenever the exact projection missed. Signature restored.
- Snap candidates included the object being edited, re-entering through its
  parent group, whose `Shape` is a compound of its children and so bypassed
  `exclude`. A drag's nearest candidate was then the point's own original
  position, making it stick to itself instead of snapping to the target curve.
  Container objects are now skipped, which also removed duplicate candidates
  (54 -> 23 points, 3 -> 1 edges in a two-curve document).
- Every point grab registered as an axis-locked drag (see the gizmo-picking
  change above); since axis drags disable snapping, snap-to-curve was unusable.
- Curve table: the Y and Z spinboxes could not be typed into. `_focus_row` ran
  on every spinbox `focusInEvent` and called `table.selectRow()`, which moves
  the current cell to column 0 and steals keyboard focus -- so clicking Y or Z
  bounced focus straight to X. The row is now selected through the selection
  model, without moving the current index.
- Curve reorder: the up/down buttons dropped the selection on every move
  (`_fill_table` rebuilds the table), so a point bounced between two rows
  instead of travelling. The moved point now stays selected at its new row, so
  repeated presses carry it (e.g. row 8 -> 4).
- Boolean / Mesh-to-surface / Mirror panels: choosing "Pick from model" for the
  plane called `self._layout()`, which never existed -- the layout is the one
  `QVBoxLayout(self.form)` installed on the form -- so entering pick mode raised
  `AttributeError`. Now uses `self.form.layout()` (3 sites).
- NURBS Push/Pull panel called `self.setWidget()` / `self.setTitle()`, which are
  `QDockWidget` methods and don't exist on a FreeCAD task panel, raising
  `AttributeError` while the panel was being built. The title is now set on
  `self.form`.
- Gordon surfaces silently produced a surface that doesn't touch its rails, with
  no way to tell why. Two causes, both now reported in the Report view:
  (a) the curve network is not COMPATIBLE -- some profile/guide pair never
  actually crosses, so `_intersect_grid` falls back to closest approach and the
  surface splits the difference. The per-pair gap matrix was already computed
  and then discarded; it now drives a warning naming the worst pair and its gap.
  No boolean-sum construction can pass within t of two rails that are more than
  t apart, so this is a modelling issue, not a solver one -- but it was
  invisible. (b) If pole counts diverge after knot harmonisation the code falls
  back to the crossing-grid interpolation (S3), which hits the crossings but
  does NOT follow the rails between them; that `return` was silent and now warns
  with the pole counts.
- `SurfaceEditor` carried a copied Ctrl+click coordinate-input branch calling
  `self._show_input_field()`, which it does not define (and it never sets
  `dragging_with_ctrl`), in BOTH of its duplicate `_on_button` methods. The
  branch was unreachable today but would have raised the moment Ctrl-drag was
  wired up; removed. (The duplicate `_on_button` itself remains -- Python uses
  the second -- and is left for the SurfaceEditor rebuild.)

## [Unreleased] - 2026-07-15

### Added
- Symmetry preview during curve creation (Priority 1)
  - When symmetric editing is enabled and a new curve being drawn, a real-time mirrored preview is shown.
  - Implemented in `editmode.PointEditor` with methods to create, update, and destroy the preview object.
  - The `CurvePanel` sets the `creating` flag on the point editor to enable this feature during curve creation.

- Context-aware point insertion/deletion (Priority 2)
  - Hover over existing points to delete them (hand cursor, press Delete)
  - Hover over curve segments to insert new points (crosshair cursor, click)
  - Implemented in `editmode.PointEditor` with hover detection, visual feedback, and insertion/deletion logic

- Dynamic precision input during curve creation (Priority 3)
  - Hold Ctrl and click to show coordinate input box for exact point placement
  - Implemented in `editmode.PointEditor` with Ctrl+click detection and QLineEdit for coordinate entry

- BezierCurve type with handle editing (New Feature)
  - Added BezierCurve as a distinct curve type offering precise curvature control via editable handles
  - Handle types: Auto (smooth), Aligned (opposite tangents), Vector (locked angle), Free (independent)
  - Visual feedback: Blue squares (left handles), green squares (right handles), dashed lines connecting to points
  - Interaction: Click handles to drag, hover for cursor feedback, keyboard shortcuts (A/L/V/F for handle types)
  - Available alongside existing curve types via the curve type converter
  - Fixed duplicate class definition in `rfstudio/parametric.py`
  - Implemented in `rfstudio/parametric.py` (BezierCurveFP class) and `rfstudio/editmode.py` (editing logic)

### Changed
- Modified `editmode.PointEditor` to support all three enhancements:
  - Added `creating` flag and `preview_object` for symmetry preview
  - Added `hover_point` and `hover_segment` for context-aware editing
  - Added `input_field` and `dragging_with_ctrl` for precision input
  - Added methods for hover detection, cursor updates, and input handling
  - Modified `set_symmetry`, `finish`, and `_press` to integrate new features
- Modified `rfstudio/panel_curve.py`:
  - Set the `creating` flag on the point editor when creating a new curve
  - Added calls to `update_symmetry` in `_append_point` and `_add_sel` to refresh the preview when points are added