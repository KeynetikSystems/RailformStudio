// rfstudio/native/rfs_native.cpp -- compiled deviation gate for Railform Studio
//
// max_surface_deviation(points, faces) -> float
//   Max over the query points of the minimum distance to the given
//   B-spline surfaces. Each face arrives as a 5-tuple
//     (poles, knots_u, knots_v, degree_u, degree_v)
//   with poles an (Nu, Nv, 4) float64 HOMOGENEOUS array ((x*w, y*w, z*w, w)
//   per pole; axis0 = u poles, verified by the Python wiring's probe) and
//   knots_* the FLAT knot sequences (len == npoles + degree + 1).
//
//   This replaces the per-sample Part.Vertex(...).distToShape(shape) loop,
//   which rebuilt an OCCT extrema context PER SAMPLE through FreeCAD's
//   Python binding (measured 4..13 ms per sample; the gate was ~69% of a
//   3-rail sweep call). Here the surface data crosses the binding ONCE per
//   call; each solve is pure arithmetic: a coarse surface-lattice scan for
//   an initial guess, with multi-start projected Newton on
//   f(u,v) = |S(u,v) - P|^2 (full 2x2 Hessian from analytic 1st/2nd
//   partials, backtracking line search, domain clamping).
//
//   Domain handling: parameters are clamped to the surface's natural
//   bounds [knot[deg], knot[npoles]] on both axes -- exactly the geometry
//   a natural-boundary face covers. The Python side only hands over
//   BSplineSurface faces whose parameter box equals those natural bounds
//   and which are not periodic; anything else makes the whole call fall
//   back to the pure-Python reference loop (patches._py_deviation_of_curves).
//   Rational surfaces are handled exactly through the homogeneous form;
//   nonpositive weights raise (Python falls back).
//
// debug_eval(poles, knots_u, knots_v, degree_u, degree_v, u, v) -> (3,)
//   Direct Euclidean surface evaluation, used by the wiring's one-time
//   pole-orientation probe and by verification scripts.
//
// project_points(points, poles, knots_u, knots_v, degree_u, degree_v)
//   -> (N, 3)
//   Project every one of the N query points onto a SINGLE B-spline surface,
//   returning [distance, u, v] per point. Same per-point solve as
//   max_surface_deviation, but the projection lattice is built ONCE for the
//   whole batch (not once per point, as repeated debug_project calls would),
//   and the converged (u, v) is returned so the caller can take cross-
//   boundary tangents/curvatures at the foot point (qa.surface_deviation).
//
// Build: build_devgate.py in this directory (MSVC + pybind11; output is
// named for the running Python's ABI tag). The extension is OPTIONAL: a
// missing or ABI-mismatched pyd never breaks the workbench.

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

namespace {

constexpr int kMaxDegree = 25;         // OCC bspline ceiling
constexpr int kRow = kMaxDegree + 1;   // basis stack array size

// --- one B-spline surface, homogeneous (4D) --------------------------------

struct Knots {
    int deg = 0;
    std::vector<double> kv;    // FLAT (multiplicity-expanded) knot vector
    int npoles() const { return static_cast<int>(kv.size()) - deg - 1; }
    double lo() const { return kv[deg]; }
    double hi() const { return kv[kv.size() - deg - 1]; }
};

struct Surf {
    Knots ku, kv;
    int nu = 0, nv = 0;
    // poles in HOMOGENEOUS coords, row-major, axis0 = u:
    //   hp[(i * nv + j) * 4 + c],  i in [0,nu), j in [0,nv), c in [0,4)
    std::vector<double> hp;
    double u0 = 0.0, u1 = 0.0, v0 = 0.0, v1 = 0.0;
    // coarse lattice (initializer for Newton), uniform in parameter space
    int lu = 0, lv = 0;
    std::vector<double> lpu, lpv;   // lattice parameter values
    std::vector<double> lpos;       // lattice 3D positions, 3 per node
};

struct Eval3 {
    double E[3];
    double Eu[3], Ev[3], Euu[3], Euv[3], Evv[3];
};

// Largest span index s with U[s] <= u, clamped to the valid basis range
// [deg, npoles-1].
int span_for(const Knots& k, double u) {
    const int n = static_cast<int>(k.kv.size());
    const double* U = k.kv.data();
    if (u >= k.hi())
        return k.npoles() - 1;
    if (u <= k.lo())
        return k.deg;
    const double* it = std::upper_bound(U, U + n, u);
    int s = static_cast<int>(it - U) - 1;
    if (s < k.deg) s = k.deg;
    if (s > k.npoles() - 1) s = k.npoles() - 1;
    return s;
}

// Cox-de Boor (Nurbs Book A2.2) for degree q on span `s` of the flat knot
// vector. N[0..q] = N_{s-q,q}, ..., N_{s,q}(u). Zero denominators (repeated
// knots) contribute 0 by the standard 0/0 := 0 convention.
void basis_funs(const Knots& k, int s, double u, int q, double* N) {
    N[0] = 1.0;
    const double* U = k.kv.data();
    double left[kRow], right[kRow];
    for (int j = 1; j <= q; ++j) {
        left[j] = u - U[s + 1 - j];
        right[j] = U[s + j] - u;
        double saved = 0.0;
        for (int r = 0; r < j; ++r) {
            const double den = right[r + 1] + left[j - r];
            const double temp = den == 0.0 ? 0.0 : N[r] / den;
            N[r] = saved + right[r + 1] * temp;
            saved = left[j - r] * temp;
        }
        N[j] = saved;
    }
}

// Basis functions of degree p plus 1st/2nd derivatives on span `s`.
// out[j*3 + d] = value / d1 / d2 of N_{s-p+j, p}(u), j in [0, p].
// Assembled from Cox-de Boor runs at degrees p, p-1, p-2 via
//   N'_{i,p}  = p * ( N_{i,p-1}/(U[i+p]-U[i]) - N_{i+1,p-1}/(U[i+p+1]-U[i+1]) )
// differentiated once more for the second derivative.
void basis_ders(const Knots& k, int s, double u, double* out) {
    const int p = k.deg;
    const double* U = k.kv.data();
    double b0[kRow], b1[kRow], b2[kRow];
    basis_funs(k, s, u, p, b0);
    basis_funs(k, s, u, p - 1, b1);            // degree 0 => N[0] = 1
    if (p >= 2)
        basis_funs(k, s, u, p - 2, b2);

    const int q = p - 1;
    for (int j = 0; j <= p; ++j) {
        const int i = s - p + j;               // pole index of this basis fn
        out[j * 3 + 0] = b0[j];

        // --- first derivative ------------------------------------------------
        double d1 = 0.0;
        {
            const double den = U[i + p] - U[i];         // N_{i,p-1} weight
            if (j >= 1 && den != 0.0)
                d1 += p * b1[j - 1] / den;
            const double den2 = U[i + p + 1] - U[i + 1]; // N_{i+1,p-1} weight
            if (j <= p - 1 && den2 != 0.0)
                d1 -= p * b1[j] / den2;
        }
        out[j * 3 + 1] = d1;

        // --- second derivative -----------------------------------------------
        double d2 = 0.0;
        if (p >= 2) {
            // N'_{i,q} (q = p-1) from the degree-(p-2) basis b2 (poles s-q..s)
            const double den = U[i + p] - U[i];
            if (j >= 2 && den != 0.0) {
                double t = 0.0;
                const double denA = U[i + q] - U[i];
                if (denA != 0.0) t += q * b2[j - 2] / denA;
                const double denB = U[i + q + 1] - U[i + 1];
                if (denB != 0.0) t -= q * b2[j - 1] / denB;
                d2 += p * t / den;
            }
            // N'_{i+1,q}
            const double den2 = U[i + p + 1] - U[i + 1];
            if (j <= p - 1 && den2 != 0.0) {
                const int a = i + 1;
                double t = 0.0;
                const double denA = U[a + q] - U[a];
                if (denA != 0.0) t += q * b2[j - 1] / denA;
                const double denB = U[a + q + 1] - U[a + 1];
                if (denB != 0.0) t -= q * b2[j] / denB;
                d2 -= p * t / den2;
            }
        }
        out[j * 3 + 2] = d2;
    }
}

// Euclidean surface value and 1st/2nd partials at (u, v).
// Homogeneous de Boor accumulation per axis, then quotient rule:
//   E = S/w,  E_u = (S_u - E w_u)/w,
//   E_uu = (S_uu - 2 E_u w_u - E w_uu)/w,  and cyclic variants.
void eval_surface(const Surf& s, double u, double v, Eval3& e) {
    const int su = span_for(s.ku, u);
    const int sv = span_for(s.kv, v);
    double bu[3 * kRow], bv[3 * kRow];
    basis_ders(s.ku, su, u, bu);
    basis_ders(s.kv, sv, v, bv);
    const int pu = s.ku.deg, pv = s.kv.deg;
    const int iu0 = su - pu, iv0 = sv - pv;

    double h[4], hu[4], hv[4], huu[4], huv[4], hvv[4];
    for (int c = 0; c < 4; ++c)
        h[c] = hu[c] = hv[c] = huu[c] = huv[c] = hvv[c] = 0.0;

    for (int i = 0; i <= pu; ++i) {
        const double* row = &s.hp[static_cast<size_t>(iu0 + i) * s.nv * 4];
        const double nu0 = bu[i * 3], nu1 = bu[i * 3 + 1], nu2 = bu[i * 3 + 2];
        for (int j = 0; j <= pv; ++j) {
            const double* H = row + (iv0 + j) * 4;
            const double mv0 = bv[j * 3], mv1 = bv[j * 3 + 1], mv2 = bv[j * 3 + 2];
            const double w00 = nu0 * mv0, w10 = nu1 * mv0, w01 = nu0 * mv1;
            const double w11 = nu1 * mv1, w20 = nu2 * mv0, w02 = nu0 * mv2;
            for (int c = 0; c < 4; ++c) {
                const double val = H[c];
                h[c]   += w00 * val;
                hu[c]  += w10 * val;
                hv[c]  += w01 * val;
                huu[c] += w20 * val;
                huv[c] += w11 * val;
                hvv[c] += w02 * val;
            }
        }
    }

    const double w = h[3];
    if (!(w > 0.0))
        throw std::runtime_error("nonpositive surface weight");
    const double iw = 1.0 / w;
    double wu = hu[3], wv = hv[3], wuu = huu[3], wuv = huv[3], wvv = hvv[3];

    for (int c = 0; c < 3; ++c) {
        const double E = h[c] * iw;
        e.E[c] = E;
        const double Eu = (hu[c] - E * wu) * iw;
        const double Ev = (hv[c] - E * wv) * iw;
        e.Eu[c] = Eu;
        e.Ev[c] = Ev;
        e.Euu[c] = (huu[c] - 2.0 * Eu * wu - E * wuu) * iw;
        e.Euv[c] = (huv[c] - Eu * wv - Ev * wu - E * wuv) * iw;
        e.Evv[c] = (hvv[c] - 2.0 * Ev * wv - E * wvv) * iw;
    }
}

// --- point projection --------------------------------------------------------

inline double d2_of(const double* a, const double* b) {
    const double x = a[0] - b[0], y = a[1] - b[1], z = a[2] - b[2];
    return x * x + y * y + z * z;
}

// Coarse lattice initializer: uniform in parameter space, denser for
// more poles (cap 33 per axis, ~9..625 points per face, built once per
// face per call).
void build_lattice(Surf& s) {
    s.u0 = s.ku.lo();
    s.u1 = s.ku.hi();
    s.v0 = s.kv.lo();
    s.v1 = s.kv.hi();
    if (!(s.u1 > s.u0) || !(s.v1 > s.v0))
        throw std::runtime_error("degenerate surface domain");
    s.lu = std::min(33, std::max(9, 2 * s.ku.npoles() + 1));
    s.lv = std::min(33, std::max(9, 2 * s.kv.npoles() + 1));
    s.lpu.resize(s.lu);
    s.lpv.resize(s.lv);
    for (int i = 0; i < s.lu; ++i)
        s.lpu[i] = s.u0 + (s.u1 - s.u0) * double(i) / double(s.lu - 1);
    for (int j = 0; j < s.lv; ++j)
        s.lpv[j] = s.v0 + (s.v1 - s.v0) * double(j) / double(s.lv - 1);
    s.lpos.assign(static_cast<size_t>(s.lu) * s.lv * 3, 0.0);
    Eval3 e;
    for (int i = 0; i < s.lu; ++i) {
        for (int j = 0; j < s.lv; ++j) {
            eval_surface(s, s.lpu[i], s.lpv[j], e);
            double* q = &s.lpos[(static_cast<size_t>(i) * s.lv + j) * 3];
            q[0] = e.E[0];
            q[1] = e.E[1];
            q[2] = e.E[2];
        }
    }
}

// Projected Newton from (u, v): full 2x2 Hessian
//   A = Eu.Eu + d.Euu, B = Eu.Ev + d.Euv, C = Ev.Ev + d.Evv
//   (since d2f/2 = d2|E-P|^2/2 = J^T d' ... in effect:
//    grad/2 = d.Eu, d.Ev ; H/2 = [[A,B],[B,C]])
// with Gauss-Newton / steepest-descent fallbacks when the system goes
// ill-conditioned, backtracking line search, and parameter clamping.
// Returns min |E(u,v) - P|^2 reached from this start.
double newton_min(const Surf& s, const double* P, double u, double v,
                  double* out_u = nullptr, double* out_v = nullptr) {
    const double uspan = s.u1 - s.u0, vspan = s.v1 - s.v0;
    const double eps_u = 1e-13 * uspan, eps_v = 1e-13 * vspan;
    Eval3 ev;
    eval_surface(s, u, v, ev);
    double f = d2_of(ev.E, P);
    for (int it = 0; it < 30; ++it) {
        const double* E = ev.E;
        const double dn[3] = {E[0] - P[0], E[1] - P[1], E[2] - P[2]};
        double a = 0, b = 0, c2 = 0, gu = 0, gv = 0;
        for (int k = 0; k < 3; ++k) {
            a += ev.Eu[k] * ev.Eu[k];
            b += ev.Eu[k] * ev.Ev[k];
            c2 += ev.Ev[k] * ev.Ev[k];
            gu += dn[k] * ev.Eu[k];
            gv += dn[k] * ev.Ev[k];
        }
        double A = a, B = b, C = c2, dB = 0;
        for (int k = 0; k < 3; ++k) {
            A += dn[k] * ev.Euu[k];
            B += dn[k] * ev.Euv[k];
            C += dn[k] * ev.Evv[k];
            dB += dn[k] * dn[k];
        }
        (void)dB;
        // default step: Gauss-Newton (solves [a b; b c] st = [-gu -gv])
        double su = -gu, sv = -gv;
        const double detg = a * c2 - b * b;
        if (detg > 1e-12 * (a * c2 + 1e-300) && a > 0.0) {
            const double inv = 1.0 / detg;
            su = (-c2 * gu + b * gv) * inv;
            sv = (b * gu - a * gv) * inv;
        }
        // full Newton when its 2x2 Hessian is safely positive definite
        const double detH = A * C - B * B;
        if (detH > 1e-10 * (A * C + 1e-300) && A > 0.0) {
            const double inv = 1.0 / detH;
            su = -(C * gu - B * gv) * inv;
            sv = (B * gu - A * gv) * inv;
        }

        // backtracking line search with domain clamping
        double f0 = f, alpha = 1.0, su_acc = 0.0, sv_acc = 0.0;
        bool improved = false;
        for (int ls = 0; ls < 12; ++ls) {
            const double ut = std::min(s.u1, std::max(s.u0, u + alpha * su));
            const double vt = std::min(s.v1, std::max(s.v0, v + alpha * sv));
            try {
                eval_surface(s, ut, vt, ev);
            } catch (const std::runtime_error&) {
                break;                       // weight went nonpositive mid-step
            }
            const double f2 = d2_of(ev.E, P);
            if (f2 < f0) {
                su_acc = alpha * su;
                sv_acc = alpha * sv;
                u = ut;
                v = vt;
                f = f2;
                improved = true;
                break;
            }
            alpha *= 0.5;
        }
        if (!improved)
            break;
        const double conv_u = std::fabs(su_acc) <= eps_u;
        const double conv_v = std::fabs(sv_acc) <= eps_v;
        const double relf = f0 - f <= 1e-15 * (f0 + 1e-300);
        if ((conv_u && conv_v) || (conv_u && conv_v && relf) || relf)
            break;
    }
    if (out_u) *out_u = u;   // converged parameters (for project_point_uv)
    if (out_v) *out_v = v;
    return f;
}

// Per point: lattice scan -> best node, Newton from it and its (up to) four
// axial neighbors; returns min |E - P|^2 over all starts.
double project_point(const Surf& s, const double* P) {
    int bi = 0, bj = 0;
    double best = std::numeric_limits<double>::infinity();
    for (int i = 0; i < s.lu; ++i) {
        for (int j = 0; j < s.lv; ++j) {
            const double* q = &s.lpos[(static_cast<size_t>(i) * s.lv + j) * 3];
            const double d = d2_of(q, P);
            if (d < best) {
                best = d;
                bi = i;
                bj = j;
            }
        }
    }
    double f = newton_min(s, P, s.lpu[bi], s.lpv[bj]);
    static const int nb[4][2] = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
    for (int n = 0; n < 4; ++n) {
        const int i = bi + nb[n][0], j = bj + nb[n][1];
        if (i < 0 || i >= s.lu || j < 0 || j >= s.lv)
            continue;
        f = std::min(f, newton_min(s, P, s.lpu[i], s.lpv[j]));
    }
    return f;
}

// Same multi-start projection as project_point, but also returns the
// converged (u, v) of the best start (bu, bv). Used by project_points so a
// caller can take cross-boundary tangents/curvatures at the foot point.
double project_point_uv(const Surf& s, const double* P,
                        double& bu, double& bv) {
    int bi = 0, bj = 0;
    double best = std::numeric_limits<double>::infinity();
    for (int i = 0; i < s.lu; ++i) {
        for (int j = 0; j < s.lv; ++j) {
            const double* q = &s.lpos[(static_cast<size_t>(i) * s.lv + j) * 3];
            const double d = d2_of(q, P);
            if (d < best) {
                best = d;
                bi = i;
                bj = j;
            }
        }
    }
    double cu = s.lpu[bi], cv = s.lpv[bj];
    double f = newton_min(s, P, cu, cv, &cu, &cv);
    bu = cu;
    bv = cv;
    static const int nb[4][2] = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
    for (int n = 0; n < 4; ++n) {
        const int i = bi + nb[n][0], j = bj + nb[n][1];
        if (i < 0 || i >= s.lu || j < 0 || j >= s.lv)
            continue;
        double nu_ = 0.0, nv_ = 0.0;
        const double f2 = newton_min(s, P, s.lpu[i], s.lpv[j], &nu_, &nv_);
        if (f2 < f) {
            f = f2;
            bu = nu_;
            bv = nv_;
        }
    }
    return f;
}

// --- parsing (pybind -> Surf) -------------------------------------------------

inline void read_flat(const py::object& o, std::vector<double>& out) {
    auto a = o.cast<py::array_t<double, py::array::c_style | py::array::forcecast>>();
    if (a.ndim() != 1)
        throw std::invalid_argument("knots must be a flat (1-d) sequence");
    const double* d = a.data();
    out.assign(d, d + a.size());
}

void surf_from_arrays(Surf& s, const py::array_t<double,
                                              py::array::c_style | py::array::forcecast>& poles,
                      const py::object& ku, const py::object& kv,
                      int degu, int degv) {
    if (poles.ndim() != 3 || poles.shape(2) != 4)
        throw std::invalid_argument("poles must have shape (Nu, Nv, 4)");
    if (degu < 1 || degu > kMaxDegree || degv < 1 || degv > kMaxDegree)
        throw std::invalid_argument("unsupported surface degree");
    s.ku.deg = degu;
    s.kv.deg = degv;
    read_flat(ku, s.ku.kv);
    read_flat(kv, s.kv.kv);
    s.nu = static_cast<int>(poles.shape(0));
    s.nv = static_cast<int>(poles.shape(1));
    if (s.ku.npoles() != s.nu || s.kv.npoles() != s.nv)
        throw std::invalid_argument(
                "flat knot length must equal npoles + degree + 1 per axis");
    const double* d = poles.data();
    s.hp.assign(d, d + static_cast<size_t>(s.nu) * s.nv * 4);
    for (size_t i = 3; i < s.hp.size(); i += 4)
        if (!(s.hp[i] > 0.0))
            throw std::runtime_error("nonpositive pole weight");
}

Surf surf_from_tuple(const py::handle& item) {
    // face tuple: (poles (Nu,Nv,4), knots_u flat, knots_v flat, deg_u, deg_v)
    py::tuple t = item.cast<py::tuple>();
    if (t.size() != 5)
        throw std::invalid_argument("face tuple must be (poles, ku, kv, degu, degv)");
    Surf s;
    auto poles = t[0].cast<py::array_t<double, py::array::c_style | py::array::forcecast>>();
    surf_from_arrays(s, poles, t[1], t[2], t[3].cast<int>(), t[4].cast<int>());
    return s;
}

double max_surface_deviation(
        const py::array_t<double, py::array::c_style | py::array::forcecast>& pts,
        const py::list& faces) {
    if (pts.ndim() != 2 || pts.shape(1) != 3)
        throw std::invalid_argument("points must have shape (N, 3)");
    const py::ssize_t n_pts = pts.shape(0);
    if (n_pts == 0)
        return 0.0;

    std::vector<Surf> surfs;
    surfs.reserve(faces.size());
    for (py::handle item : faces) {
        surfs.emplace_back(surf_from_tuple(item));
        build_lattice(surfs.back());
    }
    if (surfs.empty())
        throw std::invalid_argument("no faces given");

    const double* p = pts.data();
    double worst = 0.0;
    for (py::ssize_t i = 0; i < n_pts; ++i) {
        double best = std::numeric_limits<double>::infinity();
        for (const Surf& s : surfs)
            best = std::min(best, project_point(s, p + i * 3));
        worst = std::max(worst, best);
    }
    return std::sqrt(std::max(0.0, worst));
}

py::array_t<double> debug_eval(
        const py::array_t<double, py::array::c_style | py::array::forcecast>& poles,
        const py::object& ku, const py::object& kv, int degu, int degv,
        double u, double v) {
    Surf s;
    surf_from_arrays(s, poles, ku, kv, degu, degv);
    Eval3 e;
    eval_surface(s, u, v, e);
    py::array_t<double> out(3);
    double* o = out.mutable_data();
    o[0] = e.E[0];
    o[1] = e.E[1];
    o[2] = e.E[2];
    return out;
}

py::array_t<double> debug_project(
        const py::array_t<double, py::array::c_style | py::array::forcecast>& poles,
        const py::object& ku, const py::object& kv, int degu, int degv,
        const py::array_t<double, py::array::c_style | py::array::forcecast>& p) {
    Surf s;
    surf_from_arrays(s, poles, ku, kv, degu, degv);
    build_lattice(s);
    if (p.size() != 3)
        throw std::invalid_argument("query point must be length 3");
    // lattice best node, then a tracked Newton (same stepping as newton_min)
    int bi = 0, bj = 0;
    double best = std::numeric_limits<double>::infinity();
    for (int i = 0; i < s.lu; ++i)
        for (int j = 0; j < s.lv; ++j) {
            const double* q = &s.lpos[(static_cast<size_t>(i) * s.lv + j) * 3];
            const double d = d2_of(q, p.data());
            if (d < best) {
                best = d;
                bi = i;
                bj = j;
            }
        }
    double u = s.lpu[bi], v = s.lpv[bj];
    Eval3 ev;
    eval_surface(s, u, v, ev);
    double f = d2_of(ev.E, p.data());
    const double* pp = p.data();
    for (int it = 0; it < 30; ++it) {
        const double dn[3] = {ev.E[0] - pp[0], ev.E[1] - pp[1],
                              ev.E[2] - pp[2]};
        double a = 0, b = 0, c2 = 0, gu = 0, gv = 0;
        double A = 0, B = 0, C = 0;
        for (int k = 0; k < 3; ++k) {
            a += ev.Eu[k] * ev.Eu[k];
            b += ev.Eu[k] * ev.Ev[k];
            c2 += ev.Ev[k] * ev.Ev[k];
            gu += dn[k] * ev.Eu[k];
            gv += dn[k] * ev.Ev[k];
            A += dn[k] * ev.Euu[k];
            B += dn[k] * ev.Euv[k];
            C += dn[k] * ev.Evv[k];
        }
        A += a; B += b; C += c2;
        double su = -gu, sv = -gv;
        const double detg = a * c2 - b * b;
        if (detg > 1e-12 * (a * c2 + 1e-300) && a > 0.0) {
            const double inv = 1.0 / detg;
            su = (-c2 * gu + b * gv) * inv;
            sv = (b * gu - a * gv) * inv;
        }
        const double detH = A * C - B * B;
        if (detH > 1e-10 * (A * C + 1e-300) && A > 0.0) {
            const double inv = 1.0 / detH;
            su = -(C * gu - B * gv) * inv;
            sv = (B * gu - A * gv) * inv;
        }
        double fu = u, fv = v, alpha = 1.0;
        bool improved = false;
        for (int ls = 0; ls < 12; ++ls) {
            const double ut = std::min(s.u1, std::max(s.u0, u + alpha * su));
            const double vt = std::min(s.v1, std::max(s.v0, v + alpha * sv));
            try {
                eval_surface(s, ut, vt, ev);
            } catch (const std::runtime_error&) {
                break;
            }
            const double f2 = d2_of(ev.E, p.data());
            if (f2 < f) {
                fu = ut;
                fv = vt;
                f = f2;
                improved = true;
                break;
            }
            alpha *= 0.5;
        }
        if (!improved)
            break;
        u = fu;
        v = fv;
        if ((std::fabs(alpha * su) <= 1e-13 * (s.u1 - s.u0) &&
             std::fabs(alpha * sv) <= 1e-13 * (s.v1 - s.v0)))
            break;
    }
    py::array_t<double> out(4);
    double* o = out.mutable_data();
    o[0] = std::sqrt(std::max(0.0, f));
    o[1] = u;
    o[2] = v;
    o[3] = best;   // squared distance of the raw lattice best node
    return out;
}

// Batched projection onto ONE surface: the lattice is built once for the
// whole point set (not once per point, as repeated debug_project calls
// would), then every point is projected with the same multi-start Newton
// as max_surface_deviation. Returns an (N, 3) array of [distance, u, v].
py::array_t<double> project_points(
        const py::array_t<double, py::array::c_style | py::array::forcecast>& pts,
        const py::array_t<double, py::array::c_style | py::array::forcecast>& poles,
        const py::object& ku, const py::object& kv, int degu, int degv) {
    if (pts.ndim() != 2 || pts.shape(1) != 3)
        throw std::invalid_argument("points must have shape (N, 3)");
    Surf s;
    surf_from_arrays(s, poles, ku, kv, degu, degv);
    build_lattice(s);                    // ONCE for the whole batch

    const py::ssize_t n = pts.shape(0);
    std::vector<py::ssize_t> shape{n, 3};
    py::array_t<double> out(shape);
    double* o = out.mutable_data();
    if (n == 0)
        return out;
    const double* p = pts.data();
    for (py::ssize_t i = 0; i < n; ++i) {
        double u = 0.0, v = 0.0;
        const double f = project_point_uv(s, p + i * 3, u, v);
        o[i * 3 + 0] = std::sqrt(std::max(0.0, f));
        o[i * 3 + 1] = u;
        o[i * 3 + 2] = v;
    }
    return out;
}

} // namespace

PYBIND11_MODULE(rfs_native, m) {
    m.doc() = "Railform Studio: compiled geometry gates "
              "(exact projected-Newton deviation against B-spline faces).";
    m.attr("__version__") = "0.3.0";
    m.def("max_surface_deviation", &max_surface_deviation,
          py::arg("points"), py::arg("faces"),
          "Max over points of the min distance to the given B-spline "
          "surfaces. points (N,3) float64; faces: list of tuples "
          "(poles (Nu,Nv,4) homogeneous float64, knots_u flat, knots_v flat, "
          "degree_u, degree_v). Returns the worst distance (float).");
    m.def("project_points", &project_points,
          py::arg("points"), py::arg("poles"), py::arg("knots_u"),
          py::arg("knots_v"), py::arg("degree_u"), py::arg("degree_v"),
          "Project every point onto ONE B-spline surface, building the "
          "projection lattice once for the whole batch. points (N,3) "
          "float64; poles (Nu,Nv,4) homogeneous float64; knots_* flat. "
          "Returns an (N,3) array of [distance, u, v] per point.");
    m.def("debug_eval", &debug_eval,
          py::arg("poles"), py::arg("knots_u"), py::arg("knots_v"),
          py::arg("degree_u"), py::arg("degree_v"), py::arg("u"), py::arg("v"),
          "Evaluate the homogeneous-pole surface at (u, v); returns (3,) "
          "Euclidean position for orientation probes and verification.");
    m.def("debug_project", &debug_project,
          py::arg("poles"), py::arg("knots_u"), py::arg("knots_v"),
          py::arg("degree_u"), py::arg("degree_v"), py::arg("point"),
          "Diagnostic projection: returns (dist, u*, v*, lattice_best2) for "
          "the single-start Newton from the lattice's best node.");
}
