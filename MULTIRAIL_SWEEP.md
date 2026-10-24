# Multi-rail sweep — RailformStudio

## Summary

RailformStudio previously had only a **two-rail sweep** (`two_rail_sweep`, exactly 2 rails) and a `multi_rail_fillet` (a fillet, not a sweep). There was **no N-rail sweep** for N > 2.

This session added `multi_rail_sweep(rails, profiles, n_rail=40, n_prof=20)` — a headless, dependency-free sweep that skins profile curves across N >= 2 rail curves, with intermediate rails shaping the surface. The 2-rail case is mathematically identical to `two_rail_sweep`, so both coexist.

The spine fallback route (below) was later upgraded by porting the strengths
of the C++ `FeatureMultiRailSweep`/`MultiRailSweep` transport: arc-length
stations, profile-anchored exact stations, adaptive station subdivision,
C2 between-anchor remapping, a zero-Jacobian guard, and a measured final
deviation gate.

## Files changed

| File | Change |
|------|--------|
| `rfstudio/core/patches.py` | Added `multi_rail_sweep` + exported it in `__all__` |
| `rfstudio/core/__init__.py` | No change (already does `from .patches import *`) |
| `rfstudio/parametric.py` | `Railform Loft` now uses `multi_rail_sweep` when 3+ guide rails; `two_rail_sweep` for exactly 2 |
| `test_core.py` | Added 5 multi-rail sweep tests + registered them |
| `MULTIRAIL_SWEEP.md` | This document |

## Implementation — `rfstudio/core/patches.py`

`multi_rail_sweep(rails, profiles, n_rail=40, n_prof=20)`:

- **Validation**: raises `ValueError` if fewer than 2 rails, or no usable profiles.
- **Bounds**: `rails[0]` and `rails[-1]` bound the sweep — the surface passes through both at every profile station.
- **N == 2**: delegates directly to `two_rail_sweep` (bit-for-bit same
  shape; verified by an `Area` equivalence test to ~1e-14).
- **N >= 3, well-formed network** (every profile genuinely crosses every rail): the sweep is solved on the **exact continuous Gordon route** — see below.
- **N >= 3, otherwise** (the "spine" fallback — a port of the C++
  `FeatureMultiRailSweep` transport's strengths):
  every rail is wrapped in an `_ArcRail` cumulative chord-length table so
  stations are sweep-axis ARC FRACTIONS and rails of different lengths or
  parameterizations stay in sync; each profile pins its own station (the
  sample's nearest-t on the base rail, as an arc fraction) and the fallback
  skinning interpolates between profiles through those anchor stations, with
  proximity-matched hard-station rows reconstructing each profile curve
  exactly (~9e-12, the "profile wins at its own station" contract); gaps
  between stations are adaptively subdivided by measured chord deviation of
  the midpoint row against the bracketing rows (budget 4x `n_rail`
  stations, 10 passes, sag factor 4 over the 1e-4 target); rows interpolated
  between two anchors remap their spine positions with the C2 smootherstep
  map `_spine_map_c2` (no kink at the rail pins; anchor rows keep the plain
  linear map — the identity that preserves profile exactness — and the
  section parameterization bookkeeping `v_vals` is accumulated with the
  linear map too, which is what lets the final interpolation reconstruct
  the profile curves to machine precision); a zero-Jacobian guard refuses
  stations whose rail chords collapse (rails touch mid-sweep); the finished
  surface is finally measured against both boundary rails and a deviation
  over 10x the target only WARNS (this path already runs on input the
  exact solve refused — like the C++ `maxRailDeviation` gate, it never
  throws). The grid is finished with explicit-parameter
  `_grid_interpolation_surface` (station params along the sweep axis, each
  column's mean spine position along the section axis) with
  `grid_to_surface()` as the fallback.
- **No Curves / libS2R dependency**; robust and headless-testable.

Key helper functions (all in `patches.py` / `util.py`): `_eval01`,
`_NearestTCache`, `_polygon_normal`, `_perp`, `_smootherstep`, `_ArcRail`,
`_spine_*` (incl. `_spine_map_c2`), `_sweep_columns`, `_dedupe_stations`,
`_intersect_grid`, `gordon_network`, `_grid_interpolation_surface`,
`grid_to_surface`.

## The continuous (Gordon) route — `_try_gordon_route`

For a COMPATIBLE network — every profile genuinely crosses every rail, the
well-formed input — `multi_rail_sweep` first attempts an exact continuous
solve instead of the station-skinned spine approximation:

1. **Compatibility is measured**, not assumed: `_intersect_grid` over every
   (profile, rail) pair; any pair whose closest approach exceeds
   `_GORDON_CROSS_TOL` (0.02) disqualifies the Gordon solve. On
   disqualification the user gets a precise console warning naming the worst
   pair (and, via `_rail_order_warning`, a heads-up when the rails LIST order
   disagrees with how the sections traverse them — the spine fallback is
   order-sensitive, the Gordon route is not).
2. **Solve**: `gordon_network(profiles, rails, extra_per_gap=8)` — the
   boolean sum S1+S2-S3 built on one shared parameter grid, with the
   shape-fidelity knob `extra_per_gap` raised for denser between-crossing
   sampling. It passes through the FULL shape of every rail and profile
   continuously, not just at the `n_rail` sample stations, and it relabels
   each family onto its own shared axis, so rail ORDER does not affect the
   geometry.
3. **Result is measured before shipping**: worst deviation of rail samples
   (`_MULTIRAIL_GORDON_RAIL_TOL` = 1e-5 over 33 samples) and profile samples
   (`_MULTIRAIL_GORDON_PROFILE_TOL` = 5e-4 over 13 samples). Anything worse
   falls back to station skinning with a console warning — the spine path
   lands ON every rail at every station by construction, so the fallback is
   never a downgrade of the station-wise guarantee.

On the headless fixtures the accepted Gordon solves hold every rail and
profile to ~1e-15 (e.g. `test_multi_rail_sweep_gordon_route_holds_rails_continuously`).

## Perf notes

- `_NearestTCache`: the sweep functions used to re-evaluate the same rails
  through OCCT 101 times per profile per query (flip heuristics, station
  params). The cache samples each rail once per call; results are
  bit-identical to `_nearest_t`.
- Rail/profile deviation measurement posts ~200 `Part.Vertex.distToShape`
  projections against a single-face shape — ~0.25 s worst case on a
  3x3 network with the Gordon route, sub-0.1 s for the spine path.

## Wiring — `rfstudio/parametric.py:135`

`Railform Loft` execute path:

```python
if rails and profiles:
    try:
        if len(rails) == 1:
            obj.Shape = core.single_rail_sweep(
                rails[0], profiles, n_rail=40, n_prof=20)
        elif len(rails) >= 3:
            obj.Shape = core.multi_rail_sweep(
                rails, profiles, n_rail=40, n_prof=20)
        else:
            obj.Shape = core.two_rail_sweep(
                rails[0], rails[1], profiles,
                n_rail=40, n_prof=20)
        groups.apply_input_visibility(obj, ["Profiles", "GuideRails"])
        return
    except Exception as exc:
        ...fall back to plain loft...
```

The 1-rail branch is the single-rail pipe sweep (`single_rail_sweep`,
`_rmf_frames`, `_rail_tangent` live next to the multi-rail helpers in
`patches.py`): the rail itself is the spine and a rotation-minimizing
frame (Wang double reflection) supplies the orientation the second rail
would have. It reuses this document's fallback machinery for everything
downstream of the frames — `_ArcRail` stations, adaptive midpoint sag
subdivision, `_NearestTCache`, the interpolation finisher with the
`grid_to_surface` fallback — so see the sections above for those parts.

Previously any number of guide rails silently used only `rails[0]`/`rails[1]`; now 3+ actually use all rails.

## Tests — `test_core.py`

Registered in the headless test list:

1. `test_multi_rail_sweep_two_rail_equiv` — 2-rail `multi_rail_sweep` matches `two_rail_sweep` Area.
2. `test_multi_rail_sweep_three_rail` — 3 rails + profiles builds valid surface.
3. `test_multi_rail_sweep_more_rails` — 5 rails builds valid surface.
4. `test_multi_rail_sweep_rejects_fewer_than_two_rails` — `[]` and `[r0]` raise `ValueError`.
5. `test_multi_rail_sweep_rejects_no_profiles` — empty profiles raises `ValueError`.
6. `test_multi_rail_sweep_interpolates_every_rail` — all rails land ON the surface at every station.
7. `test_multi_rail_sweep_still_follows_the_profiles` — profiles stay on the sweep.
8. `test_multi_rail_sweep_interior_rail_changes_the_surface` — an interior rail measurably shapes the surface.
9. `test_multi_rail_sweep_gordon_route_holds_rails_continuously` — the well-formed network is solved continuously (~1e-15 on both families).
10. `test_multi_rail_sweep_gordon_route_curved_interior_rail` — an interior rail's sag BETWEEN profile stations is followed.
11. `test_multi_rail_sweep_gordon_route_is_order_insensitive` — zigzag rails order gives the same surface, every rail still held.
12. `test_multi_rail_sweep_spine_fallback_keeps_station_bites` — an incompatible network falls back to station skinning without losing the outer-rail guarantee.
13. `test_spine_invert_first_crossing_on_swaying_profile` — S-sway inversion anchors to the FIRST crossing; monotone profiles unchanged.
14. `test_rail_order_warning_flags_zigzag` — the order heuristic flags mismatched rail order and stays silent otherwise.
15. `test_try_gordon_route_gates` — `_try_gordon_route` ships only measured-good Gordon solves; incompatible networks return None (fall back).
16. `test_multi_rail_sweep_two_rail_exact_delegation` — N=2 routes to `two_rail_sweep` outright (same shape bytes as the direct call).
17. `test_multi_rail_sweep_spine_profiles_exact_at_stations` — the spine fallback's anchor stations reconstruct each profile curve (~9e-12 vs a 1e-3 gate).
18. `test_multi_rail_sweep_spine_adaptive_bounds_rail_sag` — adaptive station subdivision holds the boundary rail to ~8e-8 (5e-3 gate).
19. `test_multi_rail_sweep_spine_rejects_collapsed_rails` — rails that touch mid-sweep raise instead of skinning garbage.
20. `test_single_rail_sweep_straight_pipe_closed_profile` — 1-rail pipe sweep over a straight spine holds tube radius (~2.4e-16 at the anchor ring; radius within 5e-3 everywhere).
21. `test_single_rail_sweep_preserves_offsets_on_curved_rail` — RMF transport on an inflected S-spine keeps the offset radius within 5e-3 (~3.6e-4 measured).
22. `test_rmf_frames_no_twist_flip_on_inflected_spine` — `_rmf_frames` up vector never flips across the inflection, frames stay orthonormal.
23. `test_single_rail_sweep_morphs_multiple_profiles` — two pinned profiles land exactly on the morph sweep (~9.2e-16), sections morph between anchors.
24. `test_single_rail_sweep_rejects_degenerate_rail` — zero-length rail raises `ValueError` instead of sweeping garbage.

Fixtures use `_rails_profiles()`: two X-aligned boundary rails (y=0, y=5), optional middle rail (y=2.5), three cross profiles spanning rail1→rail2 at x=0/5/10 with a central bump; `_consistent_rails_profiles()` is the well-formed variant whose profiles genuinely run through all three rails.

Single-rail fixtures plant the profile perpendicular to the rail's START TANGENT (`_rail_start_axis`), not the chord — an oblique ring carries large along-tangent offset components and RMF transport correctly keeps that lean forever (rigid pipe behaviour), which would defeat a radius probe.

## How to run

The test module is headless (core only, no PySide/Gui):

```
"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe" test_core.py
```

## Contribution — should this go to the FreeCAD community?

**Clarification**: this code lives in **RailformStudio**, a FreeCAD *Module* (your `Mod\RailformStudio` folder), not FreeCAD core. FreeCAD core already provides `Part Sweep` / `Part Loft`.

So the natural contribution target is:

- **RailformStudio's own upstream repo** (if it has one) — this is a clean, tested feature addition.
- **FreeCAD core** only if you want to pitch "N-rail sweep with intermediate rail shaping" as a new core tool, which would be a separate discussion.

If you want to PR it, this document plus the test coverage gives you a good starting point for a PR description and README entry.

## Design notes / limitations

- Well-formed networks (every profile crossing every rail) are solved
  exactly and continuously via the Gordon route (above). Incompatible
  networks use station skinning: the surface passes through every rail at
  every adaptive station (the uniform `n_rail` grid plus each profile's own
  pinned station and the subdivisions the sag probe inserts), and the
  profiles' own stations reconstruct them exactly; between stations it
  interpolates through the grid. Rails are only "held" between stations to
  the deviation the adaptive budget leaves -- the final gate reports the
  measured boundary-rail deviation.
- Profiles are expected to span `rails[0]` → `rails[-1]`; they are
  auto-flipped if needed based on nearest-t to the boundary rails.
- `_spine_invert` assumes the profile's projection onto the reference spine
  is monotone only LOCALLY: a coarse scan picks the FIRST crossing of a
  rail's spine position before a local bisection, so an S-swaying profile
  anchors its rail column to its earliest crossing instead of bisecting
  around an arbitrary mid-parameter (see
  `test_spine_invert_first_crossing_on_swaying_profile`).
- Rail order is semantically meaningful on the spine path (rail 0 and rail
  N-1 bound the sweep; the spine walks intermediate rails in list order).
  The Gordon route is order-insensitive; the SweepN panel therefore exposes
  reorder buttons on the Rails list, and an order/traversal mismatch is
  reported by `_rail_order_warning` when the spine fallback engages.
- `n_rail`/`n_prof` default to 40/20, matching `two_rail_sweep` (they also
  set the base station grid of the spine fallback; adaptive subdivision can
  grow it to 4x `n_rail` stations, and the Gordon route's fidelity is
  governed by its own sampling).
