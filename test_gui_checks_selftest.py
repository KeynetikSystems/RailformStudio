# -*- coding: utf-8 -*-
"""Planted-bug self-test for rfstudio/test_gui_imports.py (checks 3-6).

    python test_gui_checks_selftest.py

The GUI harness is only worth trusting if its checks actually FIRE, so
this file plants each class of bug in a temp directory and asserts the
matching check flags it -- and that the known false-positive classes
(lambda args, closures, real attributes, properly imported Qt) stay
clean. Run it whenever you touch test_gui_imports.py's checkers."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "rfstudio"))

import test_gui_imports as t  # noqa: E402  (runs its own runner on import)

tmp = tempfile.mkdtemp()
ok = True


def plant(name, src):
    with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
        fh.write(src)


# check 4: reference to a real rfstudio module, nonexistent attribute
plant("bad_ref.py",
      "from rfstudio import editmode\n"
      "x = editmode.NotARealNameAtAll\n"
      "y = editmode.KnotEditor\n"          # real one -> must NOT flag
      "\n")

# check 5: Qt class base, module has no top-level PySide import
plant("bad_qt.py",
      "class Foo(QtWidgets.QWidget):\n"
      "    pass\n"
      "\n")
plant("good_qt.py",
      "from PySide import QtWidgets\n"
      "class Bar(QtWidgets.QWidget):\n"
      "    pass\n"
      "\n")

# check 3: genuinely undefined bare name (+ a lambda/closure that the
# OLD checker flagged as a false positive must stay clean)
plant("bad_name.py",
      "def f():\n"
      "    return undefined_name_xyz\n"
      "\n"
      "def g():\n"
      "    items = []\n"
      "    fn = lambda v: v + 1\n"
      "    def inner():\n"
      "        return items, fn\n"
      "    return inner\n"
      "\n")

# check 6: callback add/remove/null symmetry
plant("bad_cb.py",
      "class Leaky(object):\n"
      "    def start(self, view):\n"
      "        self._cb_a = view.addEventCallback('SoMouseButtonEvent', self.h)\n"
      "        self._cb_b = view.addEventCallback('SoLocation2Event', self.h)\n"
      "        self._cb_c = view.addEventCallback('SoKeyboardEvent', self.h)\n"
      "    def stop(self, view):\n"
      "        view.removeEventCallback('SoMouseButtonEvent', self._cb_a)\n"
      "        self._cb_a = None\n"
      "        view.removeEventCallback('SoLocation2Event', self._cb_b)\n"
      "        # _cb_b never nulled; _cb_c never removed\n"
      "\n"
      "class Clean(object):\n"
      "    def start(self, view):\n"
      "        self._cb_d = view.addEventCallback('SoMouseButtonEvent', self.h)\n"
      "    def stop(self, view):\n"
      "        drop_callbacks(view, [('SoMouseButtonEvent', self, '_cb_d')])\n"
      "\n"
      )

p4 = t.check_cross_module_refs(tmp)
p5 = t.check_lazy_qt_class_bases(tmp)
p3 = t.check_bare_undefined_names(tmp)
p6 = t.check_callback_symmetry(tmp)

cases = [
    ("check4 flags planted bad ref", any("NotARealNameAtAll" in s for s in p4)),
    ("check4 ignores real attribute", not any("KnotEditor" in s for s in p4)),
    ("check5 flags lazy Qt base", any("Foo" in s for s in p5)),
    ("check5 ignores module with PySide import",
     not any("Bar" in s for s in p5)),
    ("check3 flags undefined name",
     any("undefined_name_xyz" in s for s in p3)),
    ("check3 no FP for lambda/closure",
     not any(("lambda" in s) or ("'v'" in s) or ("'items'" in s)
             or ("'fn'" in s) for s in p3)),
    ("check6 flags never-removed callback",
     any("_cb_c" in s and "never removes" in s for s in p6)),
    ("check6 flags removed-but-not-nulled callback",
     any("_cb_b" in s and "never nulls" in s for s in p6)),
    ("check6 accepts raw remove+null pair", not any("_cb_a" in s for s in p6)),
    ("check6 accepts drop_callbacks usage", not any("_cb_d" in s for s in p6)),
]
for label, passed in cases:
    print("%s %s" % ("PASS:" if passed else "FAIL:", label))
    ok = ok and passed

if not ok:
    print("\nplanted bugs NOT detected correctly")
    print("p3=%r" % (p3,))
    print("p4=%r" % (p4,))
    print("p5=%r" % (p5,))
    print("p6=%r" % (p6,))
    sys.exit(1)
print("\nall planted-bug checks behaved correctly")
