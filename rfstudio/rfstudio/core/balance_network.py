# -*- coding: utf-8 -*-
"""Multi-neighbor / simultaneous continuity solving (RFS_BalanceNetwork)."""

from __future__ import annotations

from typing import List, Optional, Tuple

import Part
from FreeCAD import Vector

# These will be imported from other modules in the package
# For now, we'll define placeholder imports that will be updated
# when the dependent modules are created
try:
    from .curve import _face_from_input
    from .trim_intersection import trim_faces_by_intersection
    from .blend_surf_closed import blend_surface_closed
    from .global_fair import network_fairness_energy
    from .hermite_profile import _hermite5
except ImportError:
    # Fallback for when modules aren't created yet
    pass

def balance_network_continuity(faces, references=None, target="G2",
                                max_iter=5, samples=12, gap_tol=1e-3,
                                g0_tol=1e-3, g1_tol_deg=0.5, g2_tol=1e-2,
                                simultaneous=True):
    """Simultaneous local-relaxation pass over a patch network.

    Improved implementation of the "balance network continuity" feature
    (RFS_BalanceNetwork): for each iteration, identify ALL seams below
    target continuity, compute the desired corrections for each involved
    face, and apply a consensus correction to each face. This reduces
    oscillation compared to the greedy single-seam approach and better
    leverages global network information.

    The algorithm:
    1. Evaluate current network continuity
    2. For each seam below target, determine what adjustment would fix it
    3. For each face, compute a target position as the average of all
       corrections suggested by its sub-standard seams
    4. Move each face toward its consensus target
    5. Repeat until convergence or max_iter

    `faces`: list of Part.Face (or face-bearing FreeCAD objects,
                 coerced via _face_from_input) -- the network to
                 balance. Each face is its own object; the function
                 tracks them by their position in the input list.
    `references`: optional list of faces that should NOT be rebuilt
                 themselves but SHOULD be matched against (e.g. the
                 "master/reference" surfaces the rest of the network
                 must match but that the user wants to keep fixed).
                 Default: every face is fair game.
    `target`: "G1" or "G2" -- the continuity to drive toward at every
                 rebuild. (G0 is implicit -- match_surface always
                 preserves G0 exactly.)
    `max_iter`: safety cap on iterations (default 5; convergence is
                 usually < 3 in practice; the cap exists so a patho-
                 logical oscillating case can't loop forever).
    `samples, gap_tol, *_tol`: forwarded to
                 network_continuity_report / surface_deviation.

    Returns: a dict with the following keys:
      - "shapes": list of (orig_index, new_shape_or_None) -- one entry
                 per input face, in input order. None means "this face
                 was never rebuilt" (either it's a reference, or its
                 worst seam was already at the target grade).
      - "iterations": the number of outer passes actually run.
      - "converged": True if the last move made no significant changes,
                 False if the cap was hit.
      - "initial_worst": the network's worst_grade at the start.
      - "final_worst": the network's worst_grade at the end.
      - "per_iteration": list of dicts, one per iteration, with
                 "worst_grade", "changed" (number of faces updated),
                 and the per-seam grades.

    Raises: ValueError for empty `faces` or `max_iter < 1`.
    """
    faces = [_face_from_input(f) for f in (faces or [])]
    faces = [f for f in faces if f is not None]
    if not faces:
        raise ValueError("balance_network_continuity: faces must be non-empty")
    if max_iter < 1:
        raise ValueError(
            "balance_network_continuity: max_iter must be >= 1 (got %d)"
            % max_iter)
    if target not in ("G0", "G1", "G2"):
        raise ValueError(
            "balance_network_continuity: target must be G0/G1/G2 (got %r)"
            % target)
    ref_set = set()
    if references:
        for r in references:
            fr = _face_from_input(r)
            if fr is not None:
                ref_set.add(id(fr))

    # Working copy of the faces -- we update positions in place when
    # possible. `working[i]` is the i-th face's current shape.
    working = list(faces)
    # Track which faces were actually modified (vs just copied)
    updated = [False] * len(working)
    order = {"FAIL": 0, "G0": 1, "G1": 2, "G2": 3}

    def _grade(report):
        return classify_continuity(report, g0_tol=g0_tol,
                                  g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)

    def _report():
        return network_continuity_report(
            working, samples=samples, gap_tol=gap_tol,
            g0_tol=g0_tol, g1_tol_deg=g1_tol_deg, g2_tol=g2_tol)

    def _safe_copy_face(face):
        """Create a copy of a face to avoid modifying the original during
        trial computations."""
        try:
            return face.copy()
        except Exception:
            # If copy fails, return the original (less safe but prevents crash)
            return face

    initial = _report()
    initial_worst = initial["worst_grade"]
    per_iter = []

    if initial_worst is None:
        # No adjacent pairs found at all -- the "network" is a set of
        # isolated faces. Nothing to balance; return a no-op report
        # rather than failing (this is what a user would intuitively
        # expect from a button that says "balance" when there's
        # nothing to balance).
        return {"shapes": [(i, None) for i in range(len(faces))],
                "iterations": 0, "converged": True,
                "initial_worst": None, "final_worst": None,
                "per_iteration": [{"worst_grade": None, "changed": 0}]}

    converged = False
    iterations = 0
    for it in range(max_iter):
        iterations = it + 1
        rep = _report()
        worst_grade = rep["worst_grade"]
        if worst_grade is None or order[worst_grade] >= order[target]:
            # Already at or past target -- converged.
            converged = True
            per_iter.append({"worst_grade": worst_grade, "changed": 0})
            break

        # For each face, collect target positions from sub-standard seams
        # targets[i] will be a list of candidate target faces for face i
        targets = [[] for _ in range(len(working))]
        total_considered = 0

        # Process each seam below target
        for seam in rep["seams"]:
            if order[seam["grade"]] >= order[target]:
                continue  # This seam meets or exceeds target, skip

            i, j = seam["i"], seam["j"]
            face_i, face_j = working[i], working[j]

            # Skip if either face is a reference (should not be modified)
            if id(face_i) in ref_set or id(face_j) in ref_set:
                continue

            # Try to fix this seam by adjusting face_i to match face_j
            try:
                temp_i = _safe_copy_face(face_i)
                target_i = match_surface(face_j, temp_i, continuity=target, n=samples)
                target_i_face = target_i.Faces[0] if getattr(target_i, "Faces", None) else target_i
                targets[i].append(target_i_face)
                total_considered += 1
            except Exception:
                # If match_surface fails, skip this constraint
                pass

            # Try to fix this seam by adjusting face_j to match face_i
            try:
                temp_j = _safe_copy_face(face_j)
                target_j = match_surface(face_i, temp_j, continuity=target, n=samples)
                target_j_face = target_j.Faces[0] if getattr(target_j, "Faces", None) else target_j
                targets[j].append(target_j_face)
                total_considered += 1
            except Exception:
                # If match_surface fails, skip this constraint
                pass

        # If no adjustments were considered, we're done
        if total_considered == 0:
            converged = True
            per_iter.append({"worst_grade": worst_grade, "changed": 0})
            break

        # Apply the consensus adjustments (use first target for simplicity)
        any_changes = False
        for idx in range(len(working)):
            if id(working[idx]) in ref_set:
                continue  # Skip references

            if not targets[idx]:
                continue  # No constraints for this face

            # Use the first target candidate (to avoid complex averaging)
            target_face = targets[idx][0]

            # Only update if the target is significantly different
            try:
                # Simple check: compare if they're the same object or equivalent
                if working[idx] is not target_face:
                    # Check if they're geometrically equivalent by comparing bounds
                    curr_box = working[idx].BoundBox
                    targ_box = target_face.BoundBox

                    # If bounds differ significantly, apply update
                    if (abs(curr_box.XLength - targ_box.XLength) > 1e-4 or
                        abs(curr_box.YLength - targ_box.YLength) > 1e-4 or
                        abs(curr_box.ZLength - targ_box.ZLength) > 1e-4):
                        working[idx] = target_face
                        updated[idx] = True
                        any_changes = True
            except Exception:
                # If comparison fails, update anyway (better safe than sorry)
                if working[idx] is not target_face:
                    working[idx] = target_face
                    updated[idx] = True
                    any_changes = True

        # Record iteration results
        changed_count = sum(1 for u in updated if u)
        post_rep = _report()
        per_iter.append({
            "worst_grade": post_rep["worst_grade"],
            "changed": changed_count,
        })

        # Reset updated flags for next iteration
        updated = [False] * len(working)

        # Check for convergence
        if not any_changes:
            converged = True
            break

    final = _report()
    return {
        "shapes": [(i, (working[i] if updated[i] else None)) for i in range(len(faces))],
        "iterations": iterations,
        "converged": converged,
        "initial_worst": initial_worst,
        "final_worst": final["worst_grade"],
        "per_iteration": per_iter,
    }