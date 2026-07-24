# Curve drawing/editing fixes

Scope: `core.py`, `parametric.py`, `editmode.py`. All curve `FeaturePython`
classes now delegate their geometry to the already-tested `core.py`
primitives instead of reimplementing (and in several cases misusing) OCCT
calls inline.

## Fixed

**`parametric.ControlCurveFP`** — `execute()` called
`Part.BSplineCurve.createFromPolesWeightsKnots(...)`, which does not exist
on that class (the real API is `buildFromPolesMultsKnots` /
`buildFromPolesMultsWeightsKnots`, already correctly used by
`core.control_poly_curve`). Every call raised and was silently swallowed by
the surrounding `except Exception`, so `obj.Shape` was never set — the
"Control-point curve" tool never actually drew anything.
Fix: `execute()`/`compute()` now call `core.control_poly_curve` directly.
Also added `Knots` (`App::PropertyFloatList`) and `Mults`
(`App::PropertyIntegerList`) properties, seeded from the built shape via
`core.knots_from_shape` on first build. Previously no `Knots` property
existed at all, so `editmode.KnotEditor`/`panel_curve`'s "Edit knots"
toggle had nothing real to read or write — every knot drag was a silent
no-op. Stored knots/mults are validated (`sum(mults) == n + degree + 1`)
before reuse and discarded (regenerated) if stale, e.g. after the point
count changes.

**`parametric.BezierCurveFP`** — `execute()` built the shape with
`curve.interpolate(points)`, completely ignoring `LeftHandles`/
`RightHandles`. Dragging a handle updated the on-screen markers only; the
curve itself never changed shape. Added `core.bezier_curve_shape(points,
left_handles, right_handles)` — a real two-handle composite-Bezier
builder (`Part.BezierCurve` per segment, using each point's own
incoming/outgoing handle as its control poles) — and wired both
`execute()` and the new `compute()` preview hook to it.

**`editmode._enforce_bezier_handle_constraints`** — referenced a bare
`which` that was never a parameter (`NameError` on every "Aligned" or
"Vector" handle drag), plus `right.length`/`left.length` instead of the
correct FreeCAD `Vector.Length`. Both bugs were silently swallowed by
`_on_move`'s blanket `except Exception: pass`, so those two handle types
did nothing at all when dragged. Fixed the signature (now takes `which`
from the caller) and the attribute name; the checker's static "bare
undefined name" pass confirms the `which` NameError no longer appears
anywhere in this file (see diff run this session).

**`parametric.ApproxCurveFP`** — `execute()` called
`curve.approximate(points, 2, 3, obj.Tolerance, False, False)`
positionally; `core.approximate_curve` already wraps the same OCCT call
correctly via keyword arguments (`Points=`, `DegMin=`, `DegMax=`,
`Tolerance=`). Routed through `core.approximate_curve` instead.

**Dead preview path** — `panel_curve.CurvePanel._preview` and
`editmode.PointEditor._live` both call `self.obj.Proxy.compute(self.obj,
pts)` for the live drag-preview line, but no `FeaturePython` proxy class
defined `compute()` at all (only `execute()`) — every call raised
`AttributeError`, caught by `except Exception: return`/`line = None`, so
the drag-preview line silently never rendered for any curve kind. Added a
default `_BaseFP.compute()` (returns `None`) plus real overrides on
`InterpCurveFP`, `ControlCurveFP`, `ApproxCurveFP`, `PolylineFP`, and
`BezierCurveFP` — the five kinds `CurvePanel` actually uses this for.

**`parametric.PolylineFP`, `InterpCurveFP`** — geometry itself was
already correct; routed `InterpCurveFP.execute()` through
`core.interpolate_curve` for consistency with every other curve kind
(previously called `Part.BSplineCurve.interpolate` inline), and added
`compute()` previews for both.

## Verified

- `ast.parse` clean on all three files.
- `test_gui_imports.py`'s static self-call/bare-name checker run before
  and after: the 4 pre-existing `which`-undefined findings in
  `editmode.py` are gone; no new real self-call or import issues
  introduced (only expected closure-pattern false positives in the new
  `bezier_curve_shape` helper, the same class of noise the checker
  already produces elsewhere in `core.py`, e.g. `_proj2d`/`nearest_ends`).
- `core.py`'s new `bezier_curve_shape` follows this module's existing
  conventions: `ValueError` on `< 2` points, pure/Gui-free, returns a
  `Part.Wire`/`Part.Edge` like its neighbors.

## Not addressed this session (flagged, not fixed)

- `panel_curve.CurvePanel` keeps a shadow `self._weights` list manually
  synced with `self.obj.Weights` across add/remove/reorder — fragile,
  but not incorrect as written. Left as-is given scope/budget.
- The weighted-NURBS threading work already in progress per prior
  session notes (`clean_patch_surface`/knot-editor weight exposure)
  is unrelated to these curve-drawing fixes and untouched here.
