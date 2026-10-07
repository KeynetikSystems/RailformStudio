# -*- coding: utf-8 -*-
"""Headless import + static-reference smoke test for the GUI layer
(rfstudio.panel_*, taskpanels, commands, engines, workbench, toolshelf,
gizmo, editmode, sketch3d, nurbspull).

    python rfstudio/test_gui_imports.py        (from anywhere; works)

Unlike test_core.py / test_panel_logic.py, this needs NO FreeCAD install at
all -- FreeCAD, FreeCADGui, Part, PySide, and pivy are faked out below with
permissive stand-ins good for exactly one thing: letting Python's import
machinery execute every module's top-level statements (imports, class
definitions -- including base classes -- module-level dict/list literals)
without raising.

This is deliberately NOT a functional test. It cannot verify that a panel
actually *works* (see panel_base.py's own docstring: Silk/Curves import
FreeCAD.Gui at load, so nothing that transitively imports FreeCADGui can
run under freecadcmd either -- there's no way to functionally exercise this
layer outside a live FreeCAD session). What it catches, cheaply, in well
under a second, with zero FreeCAD dependency:

  1. A NameError at class-definition time or any other module-level
     statement -- e.g. `class Foo(_BaseFP):` when _BaseFP was never
     imported, or a dict literal built from names that don't exist.
     Also: a module missing from sys.path entirely (`import rfstudio`
     failing) -- _ensure_repo_on_path() makes any invocation work.
  2. `self.<method>()` calls that resolve to nothing defined anywhere in
     the calling class or its (locally-resolvable) base classes -- a
     static, source-level check, since a fake Qt object would happily
     swallow the AttributeError this catches in real life.
  3. Bare names inside functions that resolve to nothing in scope
     (missing `self.`, a deleted assignment, ...). Scope-aware: lambda
     parameters, closures over enclosing-function locals, nested-def
     default arguments, try/except-bound module globals (toolshelf's
     QtWidgets) and module dunders (__file__) all resolve first, so a
     clean run means real issues. This is the pyflakes-grade net.
  4. `rfstudio_module.attr` references that don't resolve on the real
     module -- catches a class or function being renamed/deleted while
     every module still imports fine (the KnotEditor incident: a
     scripted edit deleted the class header and the failure only showed
     at runtime as `module has no attribute 'KnotEditor'`).
  5. Qt class bases (class Foo(QtWidgets.Bar)) in modules that never
     import PySide at module scope -- i.e. subclasses that evaluate Qt
     names at import time despite Qt being loaded lazily (W5: the
     workbench-load crash class).

This file exists because of real bugs that shipped past manual review:

  1. panel_curve.py had ~440 lines of parametric.py's FeaturePython/
     ViewProvider classes accidentally duplicated onto the end of the
     file, with none of the required imports -- a NameError the instant
     the module was imported, which took the whole workbench down
     (commands.py -> taskpanels.py -> panel_curve.py import chain).
     Caught by check #1 below.
  2. CurvePanel._build() called self._scalar_controls(), a method that
     was only ever implemented on the unrelated SurfacePanel class -- an
     AttributeError the instant any curve-drawing panel was opened.
     Caught by check #2 below.
  3. BlendPanel._sample_blend_curve referenced a bare `obj` that was
     never bound (should have been `self.obj`) -- a NameError the
     instant a BlendCurve conversion was attempted. Caught by checks
     #1/#3 (see check_bare_undefined_names).
  4. A scripted class edit deleted `class KnotEditor(object):` in
     editmode.py, folding 35 methods into the previous class -- every
     module still imported cleanly; the panel died at runtime with
     `has no attribute 'KnotEditor'`. Caught by check #4.
  5. Sketch3DPanel's dim-preview row (`dim_preview_btn`, `_ready =
     True`, `_update_status()`) was stranded inside
     `_remove_constraint_highlight()` referencing an undefined `lay`,
     so the button never appeared, `_ready` stayed False (status stuck)
     and dim-list clicks died inside a swallow-everything except.
     Caught by check #3 -- the ONLY check in this suite that saw it.

None of these needed a running FreeCAD to catch. This test would have
failed immediately on all five, in CI, before any of them reached a user.
"""

import ast
import builtins
import os
import sys
import traceback
import types


# --------------------------------------------------------------------- #
# Check #1: every rfstudio module imports cleanly against faked Qt/FreeCAD.
# --------------------------------------------------------------------- #
class _FakeQtObject(object):
    """Base class every faked Qt/Coin/FreeCAD attribute ultimately
    derives from. Permissive by design: any attribute access returns a
    callable no-op (so `self.setLayout(...)`, `self.addWidget(...)`,
    etc. never raise), and instantiation never requires particular
    arguments. We only need modules to IMPORT here, not to render."""

    def __init__(self, *a, **kw):
        pass

    def __getattr__(self, name):
        def _noop(*a, **kw):
            return _FakeQtObject()
        return _noop

    def __call__(self, *a, **kw):
        return _FakeQtObject()

    def __or__(self, other):        # e.g. QDialogButtonBox.Ok | .Cancel
        return _FakeQtObject()


def _fake_module(name, parent=None):
    """A module stand-in whose attribute access (`mod.X`, and
    `from mod import X`) transparently returns a freshly-minted,
    SUBCLASSABLE class for any name -- so `class Foo(qt.QWidget):` and
    `qt.SomeEnum.VALUE` both just work."""
    mod = types.ModuleType(name)
    _cache = {}

    def __getattr__(attr):
        if attr not in _cache:
            _cache[attr] = type(attr, (_FakeQtObject,), {})
        return _cache[attr]

    mod.__getattr__ = __getattr__
    sys.modules[name] = mod
    if parent is not None:
        setattr(sys.modules[parent], name.rsplit(".", 1)[-1], mod)
    return mod


def _install_fakes():
    _fake_module("FreeCAD")
    _fake_module("FreeCADGui")
    _fake_module("Part")
    _fake_module("PySide")
    _fake_module("PySide.QtCore", parent="PySide")
    _fake_module("PySide.QtGui", parent="PySide")
    _fake_module("PySide.QtWidgets", parent="PySide")
    _fake_module("pivy")
    _fake_module("pivy.coin", parent="pivy")


def _ensure_repo_on_path():
    """Make `import rfstudio` work no matter how this file is invoked
    (`python rfstudio/test_gui_imports.py`, `python -m
    rfstudio.test_gui_imports`, or from inside rfstudio/): the package
    lives one level ABOVE this file, which is NOT on sys.path when the
    script is run directly -- the original reason every mocked import
    failed with `No module named 'rfstudio'`."""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(pkg_dir)
    if repo_root and repo_root not in sys.path:
        sys.path.insert(0, repo_root)


# Every module a normal workbench Initialize() eventually pulls in,
# listed explicitly (not glob-discovered) so a NEW module someone adds
# and forgets to wire in here shows up as an obvious gap in this list
# during review, rather than a silent hole in coverage.
_MODULES = [
    "rfstudio",
    "rfstudio.deps",
    "rfstudio.compat",
    "rfstudio.groups",
    "rfstudio.snaputil",
    "rfstudio.mirror",
    "rfstudio.panel_logic",
    "rfstudio.core",
    "rfstudio.parametric",
    "rfstudio.engines",
    "rfstudio.gizmo",
    "rfstudio.editmode",
    "rfstudio.panel_base",
    "rfstudio.panel_lawcurve",
    "rfstudio.panel_curve",
    "rfstudio.panel_surface",
    "rfstudio.panel_qa",
    "rfstudio.panel_import",
    "rfstudio.panel_solids",
    "rfstudio.panel_sketch3d",
    "rfstudio.panel_nurbspull",
    "rfstudio.taskpanels",
    "rfstudio.commands",
    "rfstudio.toolshelf",
    "rfstudio.workbench",
]


def check_imports():
    _ensure_repo_on_path()
    _install_fakes()
    failures = []
    for modname in _MODULES:
        try:
            __import__(modname)
            print("  import %-28s OK" % modname)
        except Exception as exc:
            print("  import %-28s FAIL: %s" % (modname, exc))
            traceback.print_exc()
            failures.append(modname)
    return failures


# --------------------------------------------------------------------- #
# Check #2: static self.<method>() resolution + bare undefined names.
# Pure source-level AST analysis -- no imports, no FreeCAD, no fakes.
# --------------------------------------------------------------------- #
# Extra module-level dunder names every module implicitly has (used by
# e.g. parametric._icon()'s __file__ reference).
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__",
                   "__path__", "__cached__", "__qualname__", "__all__"}

_BUILTIN_NAMES = set(dir(builtins)) | {"self", "cls", "__class__"} \
    | _MODULE_DUNDERS


def _rfstudio_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, "rfstudio")
    return candidate if os.path.isdir(candidate) else here


def _collect_class_info(root_dir):
    """AST-parse every .py file in `root_dir`. Returns
    {class_name: {'bases': [...], 'external_base': bool, 'attrs': set(...),
    'calls': [...]}}.

    'external_base' is True when any base class is expressed as a
    qualified/attribute reference (e.g. `QtWidgets.QDoubleSpinBox`,
    `FreeCADGui.Workbench`) rather than a plain name -- i.e. inherited
    from outside this codebase. We have no visibility into what such a
    base provides, so classes with one are skipped by the self-call
    check entirely (never flagged) rather than silently treated as
    having no base at all, which would produce false positives on every
    Qt-subclassed widget."""
    classes = {}
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            has_external_base = any(
                not isinstance(b, ast.Name) for b in node.bases)
            info = classes.setdefault(
                node.name, {"bases": [], "external_base": False,
                           "attrs": set(), "calls": []})
            info["bases"].extend(bases)
            info["external_base"] = info["external_base"] or has_external_base
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    info["attrs"].add(item.name)
                    called = set()
                    for sub in ast.walk(item):
                        if isinstance(sub, ast.Assign):
                            for tgt in sub.targets:
                                if (isinstance(tgt, ast.Attribute)
                                        and isinstance(tgt.value, ast.Name)
                                        and tgt.value.id == "self"):
                                    info["attrs"].add(tgt.attr)
                        if (isinstance(sub, ast.Call)
                                and isinstance(sub.func, ast.Attribute)
                                and isinstance(sub.func.value, ast.Name)
                                and sub.func.value.id == "self"):
                            called.add(sub.func.attr)
                    info["calls"].append((item.name, called))
                elif isinstance(item, ast.Assign):
                    for tgt in item.targets:
                        if isinstance(tgt, ast.Name):
                            info["attrs"].add(tgt.id)
    return classes


def _resolve_upward(name, classes, seen=None):
    """attrs available on `name` via its own body + every ancestor."""
    seen = seen if seen is not None else set()
    if name in seen or name not in classes:
        return set()
    seen.add(name)
    info = classes[name]
    out = set(info["attrs"])
    for base in info["bases"]:
        out |= _resolve_upward(base, classes, seen)
    return out


def _build_subclass_map(classes):
    sub = {}
    for cname, info in classes.items():
        for base in info["bases"]:
            sub.setdefault(base, []).append(cname)
    return sub


def _resolve_downward(name, classes, subclass_map, seen=None):
    """attrs available on `name` via every known subclass -- covers the
    template-method pattern (an abstract base calls self.foo(), and
    every concrete subclass supplies its own foo(), e.g.
    parametric._PointCurveFP.execute() calling self.compute())."""
    seen = seen if seen is not None else set()
    if name in seen:
        return set()
    seen.add(name)
    out = set()
    for child in subclass_map.get(name, []):
        if child in classes:
            out |= classes[child]["attrs"]
            out |= _resolve_downward(child, classes, subclass_map, seen)
    return out


def check_undefined_self_calls(root_dir=None):
    """Flag `self.foo()` calls where `foo` resolves to nothing defined
    anywhere in the calling class's own body, its ancestors (standard
    inheritance), or its known subclasses (template-method pattern).
    Skipped entirely for any class with an externally-inherited
    (module-qualified, e.g. Qt/FreeCAD) base anywhere in its ancestry --
    we have no visibility into what such a base provides, so a miss
    there is not evidence of anything."""
    root_dir = root_dir or _rfstudio_dir()
    classes = _collect_class_info(root_dir)
    subclass_map = _build_subclass_map(classes)
    problems = []
    for cname, info in classes.items():
        if _has_external_ancestor(cname, classes):
            continue
        resolved = (_resolve_upward(cname, classes)
                   | _resolve_downward(cname, classes, subclass_map))
        for method_name, called in info["calls"]:
            for callee in called:
                if callee.startswith("__") or callee in resolved:
                    continue
                problems.append(
                    "%s.%s() calls self.%s(), which is never defined on "
                    "%s, its known bases %s, or its known subclasses"
                    % (cname, method_name, callee, cname, info["bases"]))
    return problems

def _iter_module_statements(body, _depth=0):
    """Module-level statements, recursing through conditional/wrapped
    blocks (If/Try/With/For/While) but NOT into function/class bodies.

    Needed because real modules bind names inside such blocks -- e.g.
    toolshelf.py binds QtWidgets via `try: from PySide import QtWidgets
    / except: QtWidgets = QtGui`, which a flat scan of tree.body alone
    never sees (it then flags every QtWidgets use as undefined)."""
    for st in body:
        yield st
        if _depth > 8:
            continue
        parts = []
        if isinstance(st, (ast.If, ast.With, ast.AsyncWith, ast.For,
                           ast.AsyncFor, ast.While)):
            parts = list(st.body) + list(getattr(st, "orelse", []))
        elif isinstance(st, ast.Try):
            parts = list(st.body) + list(st.orelse) + list(st.finalbody)
            for h in st.handlers:
                parts += list(h.body)
        for sub in parts:
            for inner in _iter_module_statements([sub], _depth + 1):
                yield inner


def _collect_module_globals(root_dir):
    """{filename: set(names)} of module-level defined names (classes,
    functions, assigned names, imports -- including ones bound inside
    if/try/with blocks) per file -- used as the 'global scope' each
    function in that file can see, in addition to builtins."""
    out = {}
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        names = set(_BUILTIN_NAMES)
        for node in _iter_module_statements(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for tgt in ([node.target] if isinstance(node, ast.AnnAssign)
                            else node.targets):
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
        out[fname] = names
    return out


def _iter_shallow(node):
    """DFS over `node`'s descendants, yielding every node but only
    descending into non-scope nodes: nested def/class/lambda subtrees
    are yielded once and not entered (their bodies belong to their own
    scope and are checked separately, with this scope as their outer)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _collect_locals(node):
    """Names bound in `node`'s OWN scope: parameters, assignment / for /
    with / comprehension / walrus targets (all Name Store nodes),
    imports, nested def/class names, except-as, global/nonlocal."""
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        arglist = (list(a.args) + list(a.kwonlyargs)
                   + list(getattr(a, "posonlyargs", [])))
        if a.vararg:
            arglist.append(a.vararg)
        if a.kwarg:
            arglist.append(a.kwarg)
        names.update(x.arg for x in arglist)
    for sub in _iter_shallow(node):
        if isinstance(sub, ast.Name) and isinstance(
                sub.ctx, (ast.Store, ast.Del)):
            names.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            names.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            names.update(sub.names)
    return names


def _flag_bare_name(sub, fname, where, problems):
    problems.append(
        "%s:%d in %s() -- bare name '%s' is never a parameter, local "
        "assignment, import, or module global (likely missing 'self.')"
        % (fname, sub.lineno, where, sub.id))


def _check_expr(expr, allowed, fname, where, problems):
    """Bare-name Loads inside one expression (decorator, default value,
    annotation, base class, lambda body, ...) against `allowed`;
    nested lambdas get their own args added on top."""
    for sub in _iter_shallow(expr):
        if isinstance(sub, ast.Lambda):
            for d in list(sub.args.defaults) + [x for x in
                                                (sub.args.kw_defaults or [])
                                                if x]:
                _check_expr(d, allowed, fname, where, problems)
            _check_expr(sub.body, allowed | _collect_locals(sub),
                        fname, where, problems)
            continue
        if (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                and sub.id not in allowed):
            _flag_bare_name(sub, fname, where, problems)


def _check_scope(node, outer, glbs, fname, problems):
    """Check bare-name Loads in `node`'s own body against
    outer | node's own locals | module globals, recursing into nested
    scopes with that enlarged set as their `outer`.

    Scope-awareness is what makes this check actually usable: the three
    FP classes the original checker hit all disappear -- lambda
    parameters (`lambda v: ...`), closures over enclosing-function
    locals (`_wrapped` capturing `_orig`), and names used in a nested
    function's own default arguments (`def _commit(n=name, sp=spins)`)."""
    full = outer | _collect_locals(node) | glbs
    where = getattr(node, "name", "<lambda>")
    for sub in _iter_shallow(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef, ast.Lambda)):
            # Decorators / bases / default values evaluate in the
            # CURRENT scope, not the nested one: check them here.
            early = []
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                early = list(sub.decorator_list)
                early += [d for d in sub.args.defaults if d]
                early += [d for d in (sub.args.kw_defaults or []) if d]
                if sub.returns is not None:
                    early.append(sub.returns)
            elif isinstance(sub, ast.ClassDef):
                early = (list(sub.decorator_list) + list(sub.bases)
                         + [kw.value for kw in sub.keywords])
            elif isinstance(sub, ast.Lambda):
                early = ([d for d in sub.args.defaults if d]
                         + [d for d in (sub.args.kw_defaults or []) if d])
            for e in early:
                _check_expr(e, full, fname, where, problems)
            if not isinstance(sub, ast.Lambda):
                _check_scope(sub, full, glbs, fname, problems)
            continue
        if (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                and sub.id not in full):
            _flag_bare_name(sub, fname, where, problems)


def check_bare_undefined_names(root_dir=None):
    """Flag bare `some_name.attr` / `some_name(...)` references inside a
    function/method body where `some_name` is never a parameter, local
    assignment, import, or module-level global -- the class of bug
    self-call checking above does NOT catch (e.g. writing `obj.Scale`
    instead of `self.obj.Scale` inside a bound method).

    Scope-aware (see _check_scope) and driven from module-level
    statements through if/try wrappers (see _iter_module_statements),
    so try/except-bound names like toolshelf's QtWidgets resolve too.
    A clean run means the remaining bare-name references are real."""
    root_dir = root_dir or _rfstudio_dir()
    module_globals = _collect_module_globals(root_dir)
    problems = []
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        glbs = module_globals.get(fname, set(_BUILTIN_NAMES))
        for node in _iter_module_statements(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                _check_scope(node, set(), glbs, fname, problems)
    return problems


def _has_external_ancestor(name, classes, seen=None):
    seen = seen if seen is not None else set()
    if name in seen or name not in classes:
        return name not in classes and name != "object"
    seen.add(name)
    info = classes[name]
    if info["external_base"]:
        return True
    return any(_has_external_ancestor(b, classes, seen) for b in info["bases"])


# --------------------------------------------------------------------- #
# Check #4: every `rfstudio_module.attr` reference must resolve on the
# real module (after check #1 imported everything). This is the exact
# regression the KnotEditor incident shipped: a scripted edit deleted a
# class header, every module still *imported* fine, and the failure only
# appeared at runtime as `module 'rfstudio.editmode' has no attribute
# 'KnotEditor'`. Bare `from rfstudio.x import Name` references are
# already covered by check #1 (ImportError at import time), so this only
# has to chase the dotted-attribute form.
# --------------------------------------------------------------------- #
def check_cross_module_refs(root_dir=None):
    root_dir = root_dir or _rfstudio_dir()
    problems = []
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        # local name -> rfstudio submodule name, only for names that are
        # really modules (a `from rfstudio.parametric import PolylineFP`
        # binds a class, not a module, and must not become an alias).
        aliases = {}
        for n in _iter_module_statements(tree.body):
            if isinstance(n, ast.ImportFrom) and n.module and \
                    n.module.split(".")[0] == "rfstudio":
                for a in n.names:
                    cand = n.module + "." + a.name
                    if isinstance(sys.modules.get(cand), types.ModuleType):
                        aliases[a.asname or a.name] = cand
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.split(".")[0] != "rfstudio":
                        continue
                    if a.asname:
                        if isinstance(sys.modules.get(a.name),
                                      types.ModuleType):
                            aliases[a.asname] = a.name
                    else:
                        aliases["rfstudio"] = "rfstudio"
        if not aliases:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases):
                continue
            modname = aliases[node.value.id]
            mod = sys.modules.get(modname)
            if not hasattr(mod, node.attr):
                problems.append(
                    "%s:%d references %s.%s -- %s has no attribute %r "
                    "(renamed or deleted?)" % (fname, node.lineno,
                                               node.value.id, node.attr,
                                               modname, node.attr))
    return problems


# --------------------------------------------------------------------- #
# Check #5: import-time Qt in lazily-loaded modules. panel_base defers
# all Qt access behind _get_qt_widgets() precisely so the module imports
# without PySide; a class body that subclasses QtWidgets./QtGui./QtCore.
# still evaluates that name AT IMPORT, so it must never appear in a
# module that has no module-scope `from PySide import ...`. This is the
# static net for the workbench-load crash class (W5).
# --------------------------------------------------------------------- #
def check_lazy_qt_class_bases(root_dir=None):
    root_dir = root_dir or _rfstudio_dir()
    qt_names = {"QtCore", "QtGui", "QtWidgets"}
    problems = []
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        imports_py_side = False
        for n in _iter_module_statements(tree.body):
            if isinstance(n, ast.ImportFrom) and n.module and \
                    n.module.split(".")[0] == "PySide":
                imports_py_side = True
                break
            if isinstance(n, ast.Import) and any(
                    a.name.split(".")[0] == "PySide" for a in n.names):
                imports_py_side = True
                break
        if imports_py_side:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in list(node.bases) + [kw.value for kw in node.keywords]:
                for sub in ast.walk(base):
                    if isinstance(sub, ast.Name) and sub.id in qt_names:
                        problems.append(
                            "%s:%d class %s subclasses %s... at import time "
                            "but the module never imports PySide at module "
                            "scope (Qt loads lazily here -- move the Qt base "
                            "behind _get_qt_widgets or import PySide at the "
                            "top)" % (fname, node.lineno, node.name, sub.id))
    return problems


# --------------------------------------------------------------------- #
# Check #6: Coin event-callback hygiene. Every `self.<attr> = ...addEventCallback(...)`
# must be matched by a removal in the same class (raw removeEventCallback
# or a drop_callbacks spec) AND a nulling (`= None` or drop_callbacks,
# which nulls in a finally). Missing removals leak handlers across edit
# sessions; missing nulling hands a STALE numeric id back on double
# teardown (panel close + document close) -- ids are reused, so the
# stale one can unregister someone else's handler: a known AV ingredient.
# This is the static enforcement of panel_base.drop_callbacks' contract.
# --------------------------------------------------------------------- #
def check_callback_symmetry(root_dir=None):
    root_dir = root_dir or _rfstudio_dir()
    problems = []
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            added = {}      # attr name -> add-site line
            removed = set()
            nulled = set()
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign):
                    is_add = (isinstance(node.value, ast.Call)
                              and isinstance(node.value.func, ast.Attribute)
                              and node.value.func.attr == "addEventCallback")
                    is_none = (isinstance(node.value, ast.Constant)
                               and node.value.value is None)
                    for tgt in node.targets:
                        if not (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"):
                            continue
                        if is_add:
                            added[tgt.attr] = node.lineno
                        elif is_none:
                            nulled.add(tgt.attr)
                elif isinstance(node, ast.Call):
                    f = node.func
                    if (isinstance(f, ast.Attribute)
                            and f.attr == "removeEventCallback"
                            and len(node.args) >= 2):
                        a = node.args[1]
                        if (isinstance(a, ast.Attribute)
                                and isinstance(a.value, ast.Name)
                                and a.value.id == "self"):
                            removed.add(a.attr)
                    elif isinstance(f, ast.Name) and f.id == "drop_callbacks":
                        # Spec strings name both the attr to remove and
                        # the attr drop_callbacks will null; event-type
                        # strings in the same call can't collide with
                        # `self._cb_*` attribute names.
                        for sub in ast.walk(node):
                            if (isinstance(sub, ast.Constant)
                                    and isinstance(sub.value, str)):
                                removed.add(sub.value)
                                nulled.add(sub.value)
            for attr, lineno in sorted(added.items()):
                if attr not in removed:
                    problems.append(
                        "%s:%d class %s adds self.%s via addEventCallback "
                        "but never removes it" % (fname, lineno, cls.name,
                                                  attr))
                elif attr not in nulled:
                    problems.append(
                        "%s:%d class %s removes self.%s but never nulls "
                        "it (stale id reuse risk -- route removal through "
                        "panel_base.drop_callbacks)"
                        % (fname, lineno, cls.name, attr))
    return problems


if __name__ == "__main__" or True:
    print("== check 1: mocked imports ==")
    import_failures = check_imports()

    print("\n== check 2: undefined self.<method>() calls ==")
    self_call_problems = check_undefined_self_calls()
    if self_call_problems:
        for p in self_call_problems:
            print("  FAIL: %s" % p)
    else:
        print("  none found")

    print("\n== check 3: bare undefined names (missing 'self.'?) ==")
    bare_name_problems = check_bare_undefined_names()
    if bare_name_problems:
        for p in bare_name_problems:
            print("  FAIL: %s" % p)
    else:
        print("  none found")

    # Checks 4-5 need check 1's imports done first (they resolve against
    # the real rfstudio.* modules left in sys.modules).
    print("\n== check 4: cross-module rfstudio.<mod>.<attr> references ==")
    xref_problems = check_cross_module_refs()
    if xref_problems:
        for p in xref_problems:
            print("  FAIL: %s" % p)
    else:
        print("  none found")

    print("\n== check 5: Qt class bases in lazily-loaded modules ==")
    lazy_qt_problems = check_lazy_qt_class_bases()
    if lazy_qt_problems:
        for p in lazy_qt_problems:
            print("  FAIL: %s" % p)
    else:
        print("  none found")

    print("\n== check 6: addEventCallback add/remove/null symmetry ==")
    cb_sym_problems = check_callback_symmetry()
    if cb_sym_problems:
        for p in cb_sym_problems:
            print("  FAIL: %s" % p)
    else:
        print("  none found")

    if (import_failures or self_call_problems or bare_name_problems
            or xref_problems or lazy_qt_problems or cb_sym_problems):
        print("\nFAILED: %d import failure(s), %d undefined self-call(s), "
              "%d bare-name issue(s), %d cross-module ref(s), "
              "%d lazy-Qt base(s), %d callback symmetry issue(s)"
              % (len(import_failures), len(self_call_problems),
                 len(bare_name_problems), len(xref_problems),
                 len(lazy_qt_problems), len(cb_sym_problems)))
        sys.exit(1)
    print("\nAll checks passed (%d modules, %s)."
          % (len(_MODULES), _rfstudio_dir()))
