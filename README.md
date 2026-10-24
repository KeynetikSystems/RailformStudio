# Railform Studio (FreeCAD workbench)

A curated **curve → surface** workbench for automotive-grade modeling. It ships
**native OCCT implementations of every curated action**, so it runs fully
standalone — no mandatory third-party workbench. The optional **Curves**
workbench (Grellier) is used only as a *fallback engine* for a handful of
selections the native path doesn't yet resolve. (Earlier versions depended on
Curves + **Silk**; Silk has been removed entirely and every Silk-backed action
now has a native OCCT replacement.)

Railform Studio presents these tools under one small, workflow-ordered UX
(a docked tool shelf grouped Draw → Surface → Clean → Solids → Inspect →
Exchange), adds viewport authoring/editing aids (Blender-style point editing,
kind-coloured snapping, in-view knot display), and imports **Blender Railform
rails**.

See [CHANGELOG.md](CHANGELOG.md) for what's changed recently.

## Dependencies
- **Required:** none beyond FreeCAD itself (geometry is pure OCCT via `Part`).
- **Optional:** the **Curves** workbench — install it only for the engine-first
  fallback path on a few actions (e.g. `RFS_StarPatch`: Curves' true-G2 star
  vs. the native near-G2 Gregory construction). Everything works without it;
  the workbench reports at activation which optional engine, if any, it found.

Every curated action has an ordered **provider chain** (`engines.PROVIDERS`):
a native implementation first, the Curves command as a fallback where one
exists. `deps.report_single_points_of_failure` therefore reports an empty list
— no curated action depends on a single external engine any more.
`engines.validate_providers()` statically checks every chain (typo'd native
names, unknown provider kinds, DELEGATIONS/PROVIDERS drift) at workbench
activation and in `test_core.py`.

## Layout
| Path | Role | Testable headless? |
|------|------|--------------------|
| `rfstudio/core/` | **Gui-free** geometry package: OCCT surfacing, curves, trimming, manufacturing, QA, Blender rail interchange (JSON → exact NURBS) | ✅ `test_core.py` |
| `rfstudio/parametric.py` | Parametric FeaturePython objects (Loft, Gordon, Coons, sweeps, mirror, …) | partial |
| `rfstudio/engines.py` | Provider chains (native-first) + the Curves fallback map | partial |
| `rfstudio/deps.py` | Optional-engine detection + lazy command registration | partial |
| `rfstudio/panel_base.py` | Shared panel scaffolding: selection helpers, `_CurveGroup`, `_EditPanelBase`/`_ReportPanelBase` (open/commit/abort-transaction + teardown) | ❌ GUI only |
| `rfstudio/panel_*.py` | Task panels: curve, surface, solids, QA, import, sketch3d, nurbspull, lawcurve | ❌ GUI only |
| `rfstudio/editmode.py` | In-viewport editors (control points, knots, Bezier handles) + snapping | ❌ GUI only |
| `rfstudio/toolshelf.py` | Left-docked tool shelf (filter, collapsible sections, engine-aware greying) | ❌ GUI only |
| `rfstudio/snaputil.py` · `snapsettings.py` · `statusbar.py` | Ranked snapping, snap on/off settings, status-bar messaging | snaputil ✅ |
| `rfstudio/commands.py` | `ProviderCommand` + `PanelCommand` + curated toolbars | ❌ GUI only |
| `InitGui.py` · `rfstudio/workbench.py` | Workbench registration | ❌ GUI only |

> **Design rule:** Curves imports `FreeCAD.Gui` at load, and so does anything
> that imports a `rfstudio.panel_*` module (transitively via `FreeCADGui`), so
> those can't run under `freecadcmd`. Everything we verify headlessly therefore
> lives in `rfstudio/core/` (geometry) or `panel_logic.py` (panel decision
> logic, no FreeCADGui/PySide dependency); the rest — widget layout, dialogs,
> viewport overlays — is GUI-tested by hand.

## Verify headlessly
```powershell
& "C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe" test_core.py
& "C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe" test_panel_logic.py
python rfstudio\test_gui_imports.py        # GUI static smoke (<1s, no FreeCAD)
python test_panel_base.py                  # panel scaffolding (mocked Qt)
```
Expect `core tests done` / `panel_logic tests done` with no `FAIL` lines, and
`All checks passed (27 modules, ...)` from the GUI harness. That harness fakes
FreeCAD/Qt/Coin and statically checks the whole panel layer — mocked imports,
undefined self-calls, bare undefined names, cross-module references,
import-time Qt class bases, and event-callback add/remove/null symmetry. It has
caught real shipped bugs (deleted class headers, a stranded widget block in the
sketch panel). It cannot verify runtime behavior — that needs a live FreeCAD
session (see below).

## GUI smoke test (what only real Qt/Coin can show)
1. Restart FreeCAD → pick **Railform Studio** in the workbench dropdown.
2. The Report view should say which optional engine was detected, or that it's
   running standalone (no Curves).
3. The left **Railform Tools** shelf appears, grouped Draw / Surface / Clean /
   Solids / Inspect / Exchange; the filter box and collapsible sections work.
4. **Native action:** draw ≥ 3 spline points, create a curve, open it for
   editing — control points, knots (cyan diamonds) and axis arrows show in the
   viewport; dragging a point updates the curve live.
5. **Surface:** build a loft/sweep from profiles → a surface is created and
   re-editable.
6. **Fallback (optional):** with Curves installed, an action whose native path
   refuses a selection should bounce into the Curves command.

### Most likely things to break (and where to look)
- Workbench doesn't appear → `InitGui.py`/`workbench.py` import error; check the
  Report view.
- A toolbar button errors → provider-chain failure; it now also flashes on the
  status bar (see `statusbar.flash`), with detail in the Report view.
- Task panel doesn't open → PySide import form; on some 1.1 builds widgets are
  under `QtWidgets` (handled by `compat.get_qt_widgets`).

## License
[GPL-3.0-or-later](LICENSE).
