# Plan: Split rfstudio/core.py into Focused Modules

## Context
The file `rfstudio/core.py` has grown to ~5,883 lines and contains a wide variety of geometry utilities:
- Vector math (`dot`, `cross`, `norm`)
- Curve/wire helpers (`as_wire`, `join_curves`, `_eval01`, `_orient_family`, `_pt`)
- Loft and surface construction (`loft`, `_to_bspline`, `_intersect_pair`, `_intersect_grid`, `_strictly_increasing`)
- Grid‑to‑surface and Coons/Gordon helpers (`grid_to_surface`, `_chain_loop`, `_chain_loop` helpers, `coons_patch`, `gordon_network`)
- Cross‑boundary derivative helpers (finite‑difference epsilon, first/second/third derivative, `_cross_tan_from_surface_tangent`)
- N‑sided patches, offset/thicken/shell, filleting, constraint solving, blend strips, trimming, global fairing, analytic derivatives, balance networks, stitching, etc.

Keeping all of these in a single module makes navigation difficult, increases import overhead, and hinders focused unit testing. The module must remain importable by `freecadcmd test_core.py` (head‑less verification) and by the rest of the Railform Studio workbench.

## Goal
Refactor `rfstudio/core.py` into a package `rfstudio/core/` composed of cohesive sub‑modules, each with a clear responsibility, while preserving the existing public API via a compatibility shim in `rfstudio/core/__init__.py`.

## Proposed Module Layout
```
rfstudio/
└── core/
    ├── __init__.py          # re‑exports all public names for backward compatibility
    ├── vector.py            # dot, cross, norm
    ├── curve.py             # as_wire, join_curves, _eval01, _orient_family, _pt
    ├── loft.py              # loft, _to_bspline, _intersect_pair, _intersect_grid, _strictly_increasing
    ├── grid_surface.py      # grid_to_surface, _chain_loop (and related helpers)
    ├── coons.py             # coons_patch and related helpers
    ├── gordon.py            # gordon_network and related helpers (_closest_approach_pair, etc.)
    ├── derivatives.py       # _compute_finite_difference_epsilon, finite‑difference derivative helpers, _cross_tan_from_surface_tangent
    ├── n_sided.py           # N‑sided fill (GeomPlate G0) and related helpers
    ├── offset_thicken.py    # offset_surface, thicken_surface, shell_solid, manufacturability helpers
    ├── fillet.py            # polyline corner fillet, radius‑driven fillet/chamfer, variable‑radius fillets
    ├── constraint.py        # geometric constraint solving (sketch/curve panel)
    ├── blend_surf_closed.py # closed‑loop blend strip (RFS_BlendSurfClosed)
    ├── trim_intersection.py # true surface‑surface trim (RFS_TrimByIntersection)
    ├── global_fair.py       # global fairing / multi‑patch optimization (RFS_GlobalFair)
    ├── analytic_deriv.py    # analytic (non‑finite‑difference) surface derivatives
    ├── balance_network.py   # multi‑neighbor / simultaneous continuity solving (RFS_BalanceNetwork)
    ├── stitch_shell.py      # stitch a finished patch network into one shell (RFS_StitchShell)
    ├── trim_sketch.py       # surface trim + sketch‑on‑surface (native RFS_Trim / RFS_SketchOnSurf)
    └── hermit_profile.py    # G1/G2 surface match (Hermite‑profile skinning)
```

*Exact allocation of functions to modules follows the section markers already present in `core.py` (e.g., “--- OCCT fallback surfaces (verified headless) ---”, “--- shared grid helpers ---”, etc.). Each module will contain the functions and helpers that belong to that section, plus any private helpers used only within that module.

## Backward‑Compatibility Shim
`rfstudio/core/__init__.py` will import and re‑export every public name that the existing codebase expects, for example:
```python
from .vector import dot, cross, norm
from .curve import as_wire, join_curves
from .loft import loft
from .grid_surface import grid_to_surface
from .coons import coons_patch
from .gordon import gordon_network
# … and so on for every public symbol
```
All imports elsewhere in the workbench (`from rfstudio import core` or `from rfstudio.core import …`) will continue to work unchanged.

## Implementation Steps
1. **Create the package directory**  
   ```bash
   mkdir -p rfstudio/core
   ```
2. **Copy the original file as a backup** (for reference)  
   ```bash
   cp rfstudio/core.py rfstudio/core.py.bak
   ```
3. **Create each sub‑module**  
   For each module listed above, create a new file under `rfstudio/core/` and copy the relevant functions and helpers from `core.py` into it, preserving docstrings and adding an `__all__` list that explicitly exports the public names.
4. **Define `__all__` in each sub‑module** to make the public API explicit.
5. **Write `rfstudio/core/__init__.py** that imports from the sub‑modules and builds a flat namespace matching the original `core.py`’s public attributes.
6. **Run the head‑less test suite** to verify nothing is broken:  
   ```bash
   freecadcmd test_core.py
   ```
   Ensure all tests pass.
7. **Spot‑check imports** in other workbench modules (e.g., `commands.py`, `engines.py`, `panel_*.py`) to confirm they still resolve correctly.
8. **Optional: Remove the backup** once confidence is high.
9. **Update documentation** (if any) to reflect the new internal structure (optional).

## Verification & Validation
- **Unit‑level**: Each new module can be imported in isolation and its public functions inspected.
- **Integration**: The existing `test_core.py` exercises a broad subset of the original API; a full pass confirms the shim works.
- **Manual sanity**: Launch FreeCAD with the Railform Studio workbench and perform a few typical operations (loft, Coons patch, Gordon network, offset, etc.) to ensure the GUI‑dependent code still finds the needed functions.
- **API compatibility**: Verify that `dir(core)` after `from rfstudio import core` contains the same attributes as before (ignoring any newly added private symbols).

## Risks & Mitigations
- **Missing import**: If a helper is accidentally omitted, the import test will fail with `AttributeError`. The backup file allows quick recovery.
- **Circular imports**: By keeping the split aligned with the existing section boundaries (which already have low inter‑section coupling) we minimize risk. Any needed cross‑module calls will be explicit imports within the `core` package.
- **Performance**: Import time may improve due to smaller modules; no negative impact expected.

## Estimated Effort
- Module creation & copying: ~2‑3 hours
- Writing `__init__.py` and fixing imports: ~1 hour
- Test runs and debugging: ~1‑2 hours
- Total: ~4‑6 hours (can be split across sessions).
