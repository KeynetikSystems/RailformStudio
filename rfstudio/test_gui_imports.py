# -*- coding: utf-8 -*-
"""Headless *import* + *undefined-self-call* smoke test for the GUI layer
(rfstudio.panel_*, taskpanels, commands, engines, workbench, toolshelf,
gizmo, editmode).

    python test_gui_imports.py

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
  2. `self.<method>()` calls that resolve to nothing defined anywhere in
     the calling class or its (locally-resolvable) base classes -- a
     static, source-level check, since a fake Qt object would happily
     swallow the AttributeError this catches in real life.

This file exists because of THREE real bugs that shipped past manual
review, all in rfstudio.panel_curve / rfstudio.panel_qa:
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
     instant a BlendCurve conversion was attempted. Caught by check #1
     (pyflakes territory; this file's ast-based checker below also
     covers bare-name undefined references, see _check_undefined_names).

None of these needed a running FreeCAD to catch. This test would have
failed immediately on all three, in CI, before any of them reached a user.
"""

import ast
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


# Every module a normal workbench Initialize() eventually pulls in,
# listed explicitly (not glob-discovered) so a NEW module someone adds
# and forgets to wire in here shows up as an obvious gap in this list
# during review, rather than a silent hole in coverage.
_MODULES = [
    "rfstudio",
    "rfstudio.deps",
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
    "rfstudio.taskpanels",
    "rfstudio.commands",
    "rfstudio.toolshelf",
    "rfstudio.workbench",
]


def check_imports():
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
_BUILTIN_NAMES = set(dir(__builtins__)) | {"self", "cls", "__class__"}


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

def _function_local_names(func_node, module_globals):
    """Names available inside `func_node`: its own params, every local
    assignment (including walrus/for-targets), and anything at module
    scope (globals, imports, other functions/classes)."""
    names = set(module_globals)
    args = func_node.args
    for a in (args.args + args.kwonlyargs + args.posonlyargs
              + ([args.vararg] if args.vararg else [])
              + ([args.kwarg] if args.kwarg else [])):
        names.add(a.arg)
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.For, ast.comprehension)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _collect_module_globals(root_dir):
    """{filename: set(names)} of module-level defined names (classes,
    functions, assigned names, imports) per file -- used as the 'global
    scope' each function in that file can see, in addition to builtins."""
    out = {}
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        names = set(_BUILTIN_NAMES)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
        out[fname] = names
    return out


def check_bare_undefined_names(root_dir=None):
    """Flag bare `some_name.attr` / `some_name(...)` references inside a
    method body where `some_name` is never a parameter, local assignment,
    import, or module-level global -- the class of bug self-call
    checking above does NOT catch (e.g. writing `obj.Scale` instead of
    `self.obj.Scale` inside a bound method). Class attributes accessed
    via `self.` are out of scope here (that's check #2's job); this only
    looks at plain bare-name Loads."""
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
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            local_names = _function_local_names(node, glbs)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id not in local_names:
                        problems.append(
                            "%s:%d in %s() -- bare name '%s' is never a "
                            "parameter, local assignment, import, or "
                            "module global (likely missing 'self.')"
                            % (fname, sub.lineno, node.name, sub.id))
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

    if import_failures or self_call_problems or bare_name_problems:
        print("\nFAILED: %d import failure(s), %d undefined self-call(s), "
              "%d bare-name issue(s)"
              % (len(import_failures), len(self_call_problems),
                 len(bare_name_problems)))
        sys.exit(1)
    print("\nAll checks passed (%d modules, %s)."
          % (len(_MODULES), _rfstudio_dir()))



# --------------------------------------------------------------------- #
# Check #3: bare-name references that resolve to nothing in scope -- the
# class of bug self-call checking (#2) does NOT catch, since it only
# looks at `self.foo()`. This is what would have caught the real shipped
# bug in BlendPanel._sample_blend_curve, which referenced bare
# `obj.Continuity`/`obj.Scale` instead of `self.obj.Continuity`/
# `self.obj.Scale` -- a NameError silently swallowed by a broad
# `except Exception: return []` around it.
# --------------------------------------------------------------------- #
def _function_local_names(func_node, module_globals):
    """Names available inside `func_node`: its own params, every local
    assignment (including walrus/for-targets/except-as), and anything
    at module scope (globals, imports, other functions/classes)."""
    names = set(module_globals)
    args = func_node.args
    for a in (list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs)
              + ([args.vararg] if args.vararg else [])
              + ([args.kwarg] if args.kwarg else [])):
        names.add(a.arg)
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.For, ast.comprehension)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _collect_module_globals(root_dir):
    """{filename: set(names)} of module-level defined names (classes,
    functions, assigned names, imports) per file -- the 'global scope'
    each function in that file can see, in addition to builtins."""
    out = {}
    for fname in sorted(os.listdir(root_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root_dir, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        names = set(_BUILTIN_NAMES)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
        out[fname] = names
    return out


def check_bare_undefined_names(root_dir=None):
    """Flag bare `some_name.attr` / `some_name(...)` references inside a
    method body where `some_name` is never a parameter, local
    assignment, import, or module-level global -- e.g. writing
    `obj.Scale` instead of `self.obj.Scale` inside a bound method.
    Class attributes accessed via `self.` are check #2's job; this only
    looks at plain bare-name Loads."""
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
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            local_names = _function_local_names(node, glbs)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id not in local_names:
                        problems.append(
                            "%s:%d in %s() -- bare name '%s' is never a "
                            "parameter, local assignment, import, or "
                            "module global (likely missing 'self.')"
                            % (fname, sub.lineno, node.name, sub.id))
    return problems