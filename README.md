# Railform Studio (FreeCAD workbench)

A **curated, depend-and-wrap** curve→surface workbench. It does **not** contain
its own surfacing kernel code — it drives two engines you already have:

- **Curves** (Grellier) — curve tools, engineering surfaces (sweep/loft/gordon/
  blend), diagnostics (combs, zebra, reflect-lines, surface analysis).
- **Silk** (Mills) — clean low-control-point patches + n-sided G2 star points.

…and presents them under one small, workflow-ordered UX, plus **Blender
Railform rail import**.

See [CHANGELOG.md](CHANGELOG.md) for what's changed recently.

## Why depend-and-wrap
Curves + Silk already do the geometry (well). Their weakness is UX:
~130 flat commands, most tuned via the Property editor, no workflow guidance.
Railform Studio adds the missing curated layer + orchestration without
inheriting their maintenance or forking their code.

## Layout
| File | Role | Testable headless? |
|------|------|--------------------|
| `rfstudio/core.py` | **Gui-free** geometry: OCCT loft fallback + Blender rail interchange (JSON → exact NURBS) | ✅ `test_core.py` |
| `rfstudio/deps.py` | Engine detection + lazy command registration | partial |
| `rfstudio/engines.py` | Delegation map (curated action → engine command id) | no |
| `rfstudio/panel_base.py` | Shared panel scaffolding: selection helpers, `_CurveGroup` widget, `_EditPanelBase`/`_ReportPanelBase` (open/commit/abort-transaction + resetEdit/closeDialog teardown, once) | ❌ GUI only |
| `rfstudio/panel_curve.py` | `CurvePanel`, `BlendPanel` | ❌ GUI only |
| `rfstudio/panel_surface.py` | `SurfacePanel`, `LoftPanel`, `OffsetThickenPanel`, `ShellPanel`, `BlendSurfPanel` | ❌ GUI only |
| `rfstudio/panel_qa.py` | `ContinuityCheckPanel`, `NetworkAuditPanel`, `SurfaceReviewPanel`, `WorkflowGuidePanel` | ❌ GUI only |
| `rfstudio/panel_import.py` | `ImportRailsPanel` | ❌ GUI only |
| `rfstudio/taskpanels.py` | Re-export shim over the `panel_*` modules above, kept for backward-compatible imports | ❌ GUI only |
| `rfstudio/commands.py` | `ProviderCommand` + `PanelCommand` + curated toolbars | ❌ GUI only |
| `InitGui.py` | Workbench registration | ❌ GUI only |

> **Design rule:** Silk/Curves import `FreeCAD.Gui` at load, so they can't run
> under `freecadcmd`, and neither can anything that imports them (which
> includes every `rfstudio.panel_*` module, transitively via `FreeCADGui`
> itself). Anything we want to verify headlessly therefore lives in
> `core.py` (geometry) or `panel_logic.py` (panel decision logic, no
> FreeCADGui/PySide dependency); the rest -- widget layout, dialogs opening,
> viewport overlays -- is GUI-tested by hand.

## Verify headlessly
```powershell
& "C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe" test_core.py
& "C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe" test_panel_logic.py
```
Expect `core tests done` / `panel_logic tests done` with no `FAIL` lines.
(`test_panel_logic.py` has no FreeCAD dependency at all -- `python
test_panel_logic.py` also works.)

## GUI smoke test (needs your eyes — can't be automated here)
1. Restart FreeCAD → pick **Railform Studio** in the workbench dropdown.
2. Report panel should say "Curves + Silk detected" (or warn what's missing).
3. Toolbars appear: **Rails / Surface / Clean / Inspect / Exchange**.
4. **Delegation:** draw 2 sketches, click *Two-rail sweep* → it should bounce
   into the Curves command. (Tests `deps.ensure_engine_commands` + `runCommand`.)
5. **Custom panel:** draw 3 spline rails, click *Loft through rails* → a task
   panel opens → "Set rails from selection" → OK → a surface is built.
6. **Silk delegation:** click *Clean patch 4x4* → should fire Silk's command.

### Most likely things to break (and where to look)
- Workbench doesn't appear → `InitGui.py` import error; check the Report view.
- Delegating command does nothing → engine command id mismatch or the
  workbench-bounce in `deps.ensure_engine_commands` failed; check the warning.
- Task panel doesn't open → PySide import form (`from PySide import QtGui`);
  on some 1.1 builds widgets are under `QtWidgets`.

## License
GPL-3.0-or-later (conservative: chosen so Silk code could later be vendored).
Pure runtime delegation doesn't strictly require it — relax if you never bundle
Silk.
