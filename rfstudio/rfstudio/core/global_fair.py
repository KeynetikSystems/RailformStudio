# -*- coding: utf-8 -*-
"""Global fairing / multi-patch optimization (RFS_GlobalFair)."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import Part
from FreeCAD import Vector

from .constraint import adjacency_graph
from .curve import _param_map
from .derivatives import _compute_finite_difference_epsilon
from .gordon import _face_from_input, _face_control_grid, _harmonize_surface_knots, _match_degrees
from .hermite_profile import _hermite5
from .grid_surface import grid_to_surface


def _face_control_grid(face):
    """(surf_copy, grid) where grid[i][j] is the (0-based) control point
    of a FRESH COPY of face's underlying surface, expressed as a
    BSplineSurface -- callers mutate `surf_copy`'s poles freely without
    touching `face` itself. Raises ValueError if the surface can't be
    expressed as, or converted/fit to, a BSplineSurface (mirrors
    match_surface_exact_g1's _as_bspline helper, generalized into a
    standalone function since global_fair_network needs it outside that
    function's ref/tgt framing)."""
    surf = face.Surface
    if isinstance(surf, Part.BSplineSurface):
        surf = surf.copy()
    else:
        try:
            surf = surf.toBSpline()
        except Exception:
            try:
                u0, u1, v0, v1 = face.ParameterRange
                # Adaptive sampling density based on parameter range size
                # Ensure we have enough points for derivative calculations
                u_range = max(u1 - u0, 1e-6)  # Avoid division by zero
                v_range = max(v1 - v0, 1e-6)
                # Base density on range size, with reasonable bounds
                nu = max(8, min(32, int(u_range * 4)))
                nv = max(8, min(32, int(v_range * 4)))
                grid_pts = [[surf.value(u0 + i * u_range / (nu - 1.0),
                                        v0 + j * v_range / (nv - 1.0))
                            for j in range(nv)] for i in range(nu)]
                bs = Part.BSplineSurface()
                bs.interpolate(grid_pts)
                surf = bs
            except Exception as exc:
                raise ValueError(
                    "_face_control_grid: surface type %s can't be "
                    "converted to a BSplineSurface (%s)"
                    % (type(face.Surface).__name__, exc))
    nu, nv = surf.NbUPoles, surf.NbVPoles
    grid = [[surf.getPole(i + 1, j + 1) for j in range(nv)]
            for i in range(nu)]
    return surf, grid


def _pole_ij_boundary(surf, side, along):
    """1-based (i, j) pole index on `surf`'s boundary `side` (0=umin,
    1=umax,2=vmin,3=vmax) at position `along` running along that side --
    the depth-0 case of match_surface_exact_g1's _pole_ij, pulled out as
    a standalone helper since _resync_shared_boundaries needs it without
    that function's ref/tgt framing."""
    nu, nv = surf.NbUPoles, surf.NbVPoles
    if side == 0:
        return (1, 1 + along)
    if side == 1:
        return (nu, 1 + along)
    if side == 2:
        return (1 + along, 1)
    return (1 + along, nv)


def _resync_shared_boundaries(orig_faces, surfs, pairs, gap_tol):
    """Best-effort re-synchronization of shared boundary poles after a
    fairing pass with fix_boundary=False (see global_fair_network's
    docstring). For each adjacent pair, harmonize their knot vectors and
    degrees along the shared edge direction, then average the boundary rows
    together in place so relaxing one face's boundary doesn't pull it away
    from its neighbor and reopen a seam gap.

    This extends the original approach by using knot harmonization (like
    match_surface_exact_g1) to handle mismatched pole counts between
    adjacent faces, enabling resynchronization even when refinements differ.
    """
    for i, j in pairs:
        sa, sb, a_pr, b_pr = _nearest_facing_sides(orig_faces[i], orig_faces[j])
        si, sj = surfs[i], surfs[j]
        if si is None or sj is None:
            continue

        # Determine which parameter varies along the shared edge
        # For side 0 (umin) or 1 (umax): U is constant, V varies
        # For side 2 (vmin) or 3 (vmax): V is constant, U varies
        if sa in (0, 1):  # U-min or U-max -> V varies along edge
            u_surf_i, u_surf_j = None, None  # Not harmonizing U
            v_surf_i, v_surf_j = si, sj      # Harmonizing V
        else:  # side in (2, 3) -> V-min or V-max -> U varies along edge
            u_surf_i, u_surf_j = si, sj      # Harmonizing U
            v_surf_i, v_surf_j = None, None  # Not harmonizing V

        # Harmonize knots and match degrees in the varying direction
        if u_surf_i is not None:
            try:
                # Harmonize knot vectors in U direction
                _harmonize(u_surf_i, "U", u_surf_j, "U")

                # Match degrees in U direction
                deg_u_i = u_surf_i.UDegree
                deg_u_j = u_surf_j.UDegree
                if deg_u_i != deg_u_j:
                    target_deg_u = max(deg_u_i, deg_u_j)
                    try:
                        u_surf_i.increaseDegree(target_deg_u, u_surf_i.VDegree)
                        u_surf_j.increaseDegree(target_deg_u, u_surf_j.VDegree)
                    except Exception as exc:
                        # If degree matching fails, skip this pair
                        continue
            except Exception:
                # If harmonization fails, skip this pair
                continue
        if v_surf_i is not None:
            try:
                # Harmonize knot vectors in V direction
                _harmonize(v_surf_i, "V", v_surf_j, "V")

                # Match degrees in V direction
                deg_v_i = v_surf_i.VDegree
                deg_v_j = v_surf_j.VDegree
                if deg_v_i != deg_v_j:
                    target_deg_v = max(deg_v_i, deg_v_j)
                    try:
                        v_surf_i.increaseDegree(v_surf_i.UDegree, target_deg_v)
                        v_surf_j.increaseDegree(v_surf_j.UDegree, target_deg_v)
                    except Exception as exc:
                        # If degree matching fails, skip this pair
                        continue
            except Exception:
                # If harmonization fails, skip this pair
                continue

        # Get pole counts after potential harmonization
        def _side_pole_count(surf, side):
            return surf.NbVPoles if side < 2 else surf.NbUPoles

        na = _side_pole_count(si, sa)
        nb = _side_pole_count(sj, sb)
        # After harmonization, pole counts should match (or we'd have skipped above)
        for along in range(na):
            pi = _pole_ij_boundary(si, sa, along)
            pj = _pole_ij_boundary(sj, sb, along)
            pa = si.getPole(*pi)
            pb = sj.getPole(*pj)
            avg = (pa + pb) * 0.5
            si.setPole(pi[0], pi[1], avg)
            sj.setPole(pj[0], pj[1], avg)


def _variational_fair_direct(orig_faces, surfs, fix_boundary, energy, gap_tol):
    """Solve the WHOLE patch network's fairing in one linear system --
    the "assemble and solve one global system across every shared pole
    simultaneously" step the Jacobi relaxation only approximates.

    Mutates the BSplineSurface objects in `surfs` IN PLACE and returns
    True on success, or returns False WITHOUT touching any pole when the
    solve can't be done (numpy/scipy missing, no free poles, no fixed
    anchor, or a singular/non-finite system) so the caller can fall back
    to the iterative path with nothing half-modified.

    How the system is built:
      * Every control pole (patch p, row i, col j) is a node. When
        `fix_boundary` is False, poles that COINCIDE across a shared seam
        (matched via _nearest_facing_sides + _pole_ij_boundary, same
        correspondence _resync_shared_boundaries uses, and only where the
        two sides have equal pole counts) are merged into a SINGLE unknown
        by union-find -- that merge is what makes the seam solved jointly
        by both patches at once instead of averaged after the fact.
      * Fixed nodes (Dirichlet data): with `fix_boundary` True, every
        patch-boundary pole; with it False, only NAKED outer-boundary
        poles (boundary poles that never got merged with a neighbour).
        All remaining nodes are free unknowns.
      * A linear fairing operator is applied per patch and stacked over
        the merged node set:
          energy="membrane"   -> first differences along grid edges;
                                 its minimiser is exactly the fully-
                                 converged Jacobi (average-of-neighbours)
                                 fixed point, reached directly here.
          energy="thin_plate" -> second differences along each u- and
                                 v-line, i.e. the SAME discrete bending
                                 term network_fairness_energy sums, so the
                                 result is the true global minimiser of the
                                 energy the report measures.
        With operator O and unknown/fixed split, minimising ||O x||^2
        gives (O^T O)_ff x_f = -(O^T O)_fb x_b, solved per coordinate.

    Because merged seam nodes get ONE solved position written back to every
    constituent pole, shared seams close exactly (better than the
    averaging _resync_shared_boundaries does).
    """
    try:
        import numpy as np
        import scipy.sparse as sp
        import scipy.sparse.linalg as spla
    except Exception:
        return False

    dims = []
    for surf in surfs:
        dims.append((surf.NbUPoles, surf.NbVPoles) if surf is not None
                    else None)

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for p, d in enumerate(dims):
        if d is None:
            continue
        nu, nv = d
        for i in range(nu):
            for j in range(nv):
                find((p, i, j))

    if not fix_boundary:
        pairs = adjacency_graph(orig_faces, gap_tol=gap_tol)
        for a, b in pairs:
            if surfs[a] is None or surfs[b] is None:
                continue
            sa, sb, _apr, _bpr = _nearest_facing_sides(orig_faces[a],
                                                       orig_faces[b])
            na = surfs[a].NbVPoles if sa < 2 else surfs[a].NbUPoles
            nb = surfs[b].NbVPoles if sb < 2 else surfs[b].NbUPoles
            if na != nb:
                continue
            for along in range(na):
                ia, ja = _pole_ij_boundary(surfs[a], sa, along)
                ib, jb = _pole_ij_boundary(surfs[b], sb, along)
                union((a, ia - 1, ja - 1), (b, ib - 1, jb - 1))

    def is_boundary(node):
        p, i, j = node
        nu, nv = dims[p]
        return i == 0 or i == nu - 1 or j == 0 or j == nv - 1

    from collections import defaultdict
    members = defaultdict(list)
    for node in list(parent.keys()):
        members[find(node)].append(node)

    fixed_roots = set()
    for root, nodes in members.items():
        any_boundary = any(is_boundary(n) for n in nodes)
        patches = {n[0] for n in nodes}
        if fix_boundary:
            if any_boundary:
                fixed_roots.add(root)
        else:
            if any_boundary and len(patches) == 1:
                fixed_roots.add(root)

    free_roots = [r for r in members if r not in fixed_roots]
    fixed_list = [r for r in members if r in fixed_roots]
    if not free_roots or not fixed_list:
        # nothing to move, or nothing to anchor the free poles to
        return not free_roots

    col = {r: k for k, r in enumerate(free_roots)}
    fcol = {r: k for k, r in enumerate(fixed_list)}

    rows = []
    for p, d in enumerate(dims):
        if d is None:
            continue
        nu, nv = d
        if energy == "membrane":
            for i in range(nu):
                for j in range(nv):
                    if i + 1 < nu:
                        rows.append([(find((p, i, j)), 1.0),
                                     (find((p, i + 1, j)), -1.0)])
                    if j + 1 < nv:
                        rows.append([(find((p, i, j)), 1.0),
                                     (find((p, i, j + 1)), -1.0)])
        else:  # thin_plate
            for i in range(1, nu - 1):
                for j in range(nv):
                    rows.append([(find((p, i - 1, j)), 1.0),
                                 (find((p, i, j)), -2.0),
                                 (find((p, i + 1, j)), 1.0)])
            for i in range(nu):
                for j in range(1, nv - 1):
                    rows.append([(find((p, i, j - 1)), 1.0),
                                 (find((p, i, j)), -2.0),
                                 (find((p, i, j + 1)), 1.0)])
    if not rows:
        return True

    nf, nb = len(free_roots), len(fixed_list)
    rf, cf, vf = [], [], []
    rb, cb, vb = [], [], []
    for ri, terms in enumerate(rows):
        acc = {}
        for root, coeff in terms:
            acc[root] = acc.get(root, 0.0) + coeff
        for root, coeff in acc.items():
            if root in col:
                rf.append(ri); cf.append(col[root]); vf.append(coeff)
            else:
                rb.append(ri); cb.append(fcol[root]); vb.append(coeff)

    nrows = len(rows)
    Of = sp.csr_matrix((vf, (rf, cf)), shape=(nrows, nf))
    Ob = sp.csr_matrix((vb, (rb, cb)), shape=(nrows, nb))

    xb = np.empty((nb, 3))
    for r, k in fcol.items():
        p, i, j = r
        pole = surfs[p].getPole(i + 1, j + 1)
        xb[k] = (pole.x, pole.y, pole.z)

    A = (Of.T @ Of).tocsc()
    rhs = -(Of.T @ (Ob @ xb))
    try:
        X = spla.spsolve(A, rhs)
    except Exception:
        return False
    X = np.asarray(X)
    if X.ndim == 1:
        X = X.reshape(nf, 3)
    if not np.all(np.isfinite(X)):
        return False

    for root, k in col.items():
        v = Vector(float(X[k, 0]), float(X[k, 1]), float(X[k, 2]))
        for (p, i, j) in members[root]:
            surfs[p].setPole(i + 1, j + 1, v)
    return True


def global_fair_network(faces, iterations=20, lambda_=0.5, fix_boundary=True,
                        gap_tol=1e-3, samples=8, tol=None,
                        solver="auto", energy="thin_plate"):
    """Global fairing over a whole patch network's control nets -- the
    native implementation of "global fairing / multi-patch optimization"
    (RFS_GlobalFair).

    Two solvers are available via `solver`:

    * solver="direct"/"auto" (DEFAULT, when numpy+scipy are present) --
      a real VARIATIONAL, energy-minimizing least-squares network solve:
      assemble ONE global linear system over every free pole (with poles
      shared across a seam merged into a single unknown, so seams are
      solved by both patches simultaneously) and solve it in a single
      shot for the exact minimiser of the chosen discrete energy. This is
      the "assemble and solve one global system across every shared pole
      simultaneously" step that real Class-A fairing tools (Alias, ICEM
      Surf) do and that the relaxation below only approaches. `energy`
      selects the functional:
        "thin_plate" (default) minimises the SAME discrete second-
            difference bending term network_fairness_energy reports, so
            energy_after is its true constrained global minimum;
        "membrane" minimises first-difference (stretch) energy, whose
            minimiser is exactly the fully-converged Jacobi fixed point.
      `iterations`/`lambda_`/`tol` are not used by this path (there is
      nothing to iterate -- it is a direct solve); the returned
      "iterations" is 0 and "converged" is True. If numpy/scipy are
      missing, or the assembled system has no fixed anchor or comes out
      singular, the direct path cleanly declines and (for solver="auto")
      the Jacobi path below runs instead, with nothing half-modified.

    * solver="jacobi" (or the automatic fallback) -- the original
      discrete Laplacian smoothing pass (the same "average with your
      neighbors" idea mesh-fairing tools apply to vertices, applied here
      to each surface's B-spline CONTROL POLES) repeated for
      `iterations` passes. It is honest local relaxation, not a global
      solve; it approaches the same membrane minimiser the direct path
      reaches exactly. Its per-pass behaviour:

      - INTERIOR poles of each face are relaxed toward the average of
        their up-to-4 neighbors in the control net, blended by
        `lambda_` (0 = no change, 1 = fully replace with the neighbor
        average -- 0.3-0.6 is a reasonable working range; pushing it
        much higher tends to overshoot/oscillate rather than converge
        faster).
      - BOUNDARY poles are either left completely untouched
        (`fix_boundary=True`, the default -- every seam and outer edge
        stays exactly where it was, so this only smooths each patch's
        INTERIOR shape and can never reopen a seam gap) or, when
        `fix_boundary=False`, relaxed the same way as interior poles and
        then re-synchronized across shared seams (see
        _resync_shared_boundaries) -- this CAN shift seams and will
        generally disturb any exact G0/G1 a prior match_surface/
        blend_surface pass established, so treat it as an advanced
        option and re-run network_continuity_report() afterward.

    `tol`, if given, adds a REAL convergence check on top of the
    `iterations` cap: after each pass, every surface's largest single-pole
    displacement (max over every relaxed pole of |new - old|) is tracked,
    and the whole pass stops as soon as the largest displacement across
    ALL surfaces falls below `tol` -- i.e. this no longer blindly runs a
    fixed count, it runs UNTIL the Jacobi relaxation has actually settled
    (or the `iterations` cap is hit first, whichever comes first). This is
    still the same discrete-Laplacian relaxation described above (each
    pass already updates every pole SIMULTANEOUSLY from the previous
    pass's snapshot -- true Jacobi, not Gauss-Seidel), not a direct
    (single-shot) linear solve of the fixed-point system; `tol` only
    changes WHEN it stops, not what it's iterating. `tol=None` (default)
    preserves the exact previous fixed-iteration-count behaviour.

    Returns a dict:
      {"faces": [new_face_or_None, ...] -- one entry per input face, in
               input order; None means that face's surface couldn't be
               expressed as a BSplineSurface and was left untouched,
       "energy_before": network_fairness_energy(faces) on the input,
       "energy_after": the same, computed on the result,
       "iterations": the number of passes actually run (== `iterations`
               unless `tol` is given and convergence is reached early),
       "converged": True for the direct solver (it computes the exact
               minimiser), or for the Jacobi path True only if `tol` was
               given and every pole displacement fell below it,
       "solver": "direct" or "jacobi" -- which path actually ran,
       "energy": the energy functional used ("thin_plate"/"membrane") for
               the direct path, else None}

    Raises ValueError for an empty `faces` list, `iterations < 1`,
    `lambda_` outside [0, 1], a non-positive `tol`, an unknown `solver`,
    or an unknown `energy`. Raises RuntimeError only if solver="direct"
    is explicitly requested but the direct solve is unavailable (numpy/
    scipy missing or singular system) -- use solver="auto" to allow the
    Jacobi fallback instead.
    """
    orig_faces = [_face_from_input(f) for f in (faces or [])]
    orig_faces = [f for f in orig_faces if f is not None]
    if not orig_faces:
        raise ValueError("global_fair_network: faces must be non-empty")
    if iterations < 1:
        raise ValueError("global_fair_network: iterations must be >= 1")
    if not (0.0 <= lambda_ <= 1.0):
        raise ValueError("global_fair_network: lambda_ must be in [0, 1]")
    if tol is not None and tol <= 0:
        raise ValueError("global_fair_network: tol must be positive if given")
    if solver not in ("auto", "direct", "jacobi"):
        raise ValueError("global_fair_network: solver must be 'auto', "
                         "'direct', or 'jacobi' (got %r)" % (solver,))
    if energy not in ("thin_plate", "membrane"):
        raise ValueError("global_fair_network: energy must be 'thin_plate' "
                         "or 'membrane' (got %r)" % (energy,))

    energy_before = network_fairness_energy(orig_faces)

    surfs = []
    for f in orig_faces:
        try:
            s, _grid = _face_control_grid(f)
            surfs.append(s)
        except Exception:
            surfs.append(None)

    # --- direct variational solve (default) ---------------------------- #
    if solver in ("auto", "direct"):
        if _variational_fair_direct(orig_faces, surfs, fix_boundary,
                                    energy, gap_tol):
            new_faces = [None if s is None else s.toShape().Faces[0]
                         for s in surfs]
            energy_faces = [nf if nf is not None else orig_faces[i]
                            for i, nf in enumerate(new_faces)]
            return {"faces": new_faces, "energy_before": energy_before,
                    "energy_after": network_fairness_energy(energy_faces),
                    "iterations": 0, "converged": True,
                    "solver": "direct", "energy": energy}
        if solver == "direct":
            raise RuntimeError(
                "global_fair_network: direct solver unavailable (numpy/"
                "scipy missing, or the system has no anchor / is singular) "
                "and solver='direct' forbids the Jacobi fallback -- use "
                "solver='auto' to allow it")
        # solver == "auto": rebuild fresh surfs (the failed direct attempt
        # guarantees it left poles untouched, but re-derive to be safe) and
        # fall through to Jacobi.
        surfs = []
        for f in orig_faces:
            try:
                s, _grid = _face_control_grid(f)
                surfs.append(s)
            except Exception:
                surfs.append(None)

    pairs = adjacency_graph(orig_faces, gap_tol=gap_tol) if not fix_boundary \
        else []

    converged = False
    ran = 0
    for _ in range(int(iterations)):
        ran += 1
        max_disp = 0.0
        for surf in surfs:
            if surf is None:
                continue
            nu, nv = surf.NbUPoles, surf.NbVPoles
            old = [[surf.getPole(i + 1, j + 1) for j in range(nv)]
                  for i in range(nu)]
            lo_i, hi_i = (0, nu) if not fix_boundary else (1, nu - 1)
            lo_j, hi_j = (0, nv) if not fix_boundary else (1, nv - 1)
            for i in range(lo_i, hi_i):
                for j in range(lo_j, hi_j):
                    neighbors = []
                    if i > 0:
                        neighbors.append(old[i - 1][j])
                    if i < nu - 1:
                        neighbors.append(old[i + 1][j])
                    if j > 0:
                        neighbors.append(old[i][j - 1])
                    if j < nv - 1:
                        neighbors.append(old[i][j + 1])
                    if not neighbors:
                        continue
                    avg = neighbors[0]
                    for extra in neighbors[1:]:
                        avg = avg + extra
                    avg = avg * (1.0 / len(neighbors))
                    new_p = old[i][j] * (1.0 - lambda_) + avg * lambda_
                    if tol is not None:
                        disp = new_p.distanceToPoint(old[i][j])
                        if disp > max_disp:
                            max_disp = disp
                    surf.setPole(i + 1, j + 1, new_p)
        if not fix_boundary:
            _resync_shared_boundaries(orig_faces, surfs, pairs, gap_tol)
        if tol is not None and max_disp < tol:
            converged = True
            break

    new_faces = [None if s is None else s.toShape().Faces[0] for s in surfs]
    energy_faces = [nf if nf is not None else orig_faces[i]
                    for i, nf in enumerate(new_faces)]
    energy_after = network_fairness_energy(energy_faces)

    return {"faces": new_faces, "energy_before": energy_before,
            "energy_after": energy_after, "iterations": ran,
            "converged": converged, "solver": "jacobi", "energy": None}


def network_fairness_energy(faces):
    """Sum, over every face's control net, of the discrete second-
    difference ("bending energy") of its INTERIOR poles -- a cheap
    numeric proxy for how rippled a patch network's control nets are.
    Lower is fairer; zero only for a perfectly planar/ruled grid of
    poles. This is NOT a physical bending energy (no arc-length or
    material stiffness involved), it is the same discrete Laplacian
    magnitude global_fair_network minimizes, exposed standalone so a
    caller (or a QA panel) can report a before/after number without
    re-running the whole fairing pass.

    Faces whose surface can't be expressed as a BSplineSurface are
    silently skipped (contribute 0) rather than raising -- this is a
    diagnostic aggregate, not a correctness-critical path.
    """
    faces = [_face_from_input(f) for f in (faces or [])]
    faces = [f for f in faces if f is not None]
    total = 0.0
    for face in faces:
        try:
            _surf, grid = _face_control_grid(face)
        except Exception:
            continue
        nu = len(grid)
        nv = len(grid[0]) if nu else 0
        for i in range(1, nu - 1):
            for j in range(nv):
                lap = grid[i - 1][j] + grid[i + 1][j] - grid[i][j] * 2.0
                total += lap.Length ** 2
        for i in range(nu):
            for j in range(1, nv - 1):
                lap = grid[i][j - 1] + grid[i][j + 1] - grid[i][j] * 2.0
                total += lap.Length ** 2
    return total