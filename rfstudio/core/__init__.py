# -*- coding: utf-8 -*-
"""Gui-free geometry core (package).

Formerly the single module rfstudio/core.py. Split into cohesive
submodules for testability; this __init__ re-exports the full public
(and private) API so existing `from rfstudio import core; core.foo()`
call sites keep working unchanged.

CRITICAL design rule: Silk and Curves import FreeCAD.Gui at load, so
they CANNOT be imported under freecadcmd. Everything here depends only
on Part / FreeCAD and must stay importable by `freecadcmd test_core.py`.
"""

G0, G1, G2 = 0, 1, 2

from .util import *  # noqa: F401,F403
from .curves import *  # noqa: F401,F403
from .patches import *  # noqa: F401,F403
from .surfacing import *  # noqa: F401,F403
from .manufacturing import *  # noqa: F401,F403
from .trimming import *  # noqa: F401,F403
from .constraints import *  # noqa: F401,F403
from .qa import *  # noqa: F401,F403
from .io import *  # noqa: F401,F403
