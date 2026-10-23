# -*- coding: utf-8 -*-
"""Transient + live messages on FreeCAD's main-window status bar.

Two jobs, both so feedback reaches a user who (like most) keeps the Report
view closed:

  * flash(text)        -- a timed notice that a click did something (or
                          failed). Replaces a lone Console.PrintError that
                          leaves a toolbar button looking like it did
                          nothing.
  * show(text)/clear() -- a persistent line used for a live drag readout
                          (delta / distance), updated every move and cleared
                          when the drag ends.

Everything is best-effort and never raises: no main window (headless /
early startup) falls back to the Console for flash() and is a silent no-op
for the live line. FreeCADGui is imported lazily inside _bar() so this
module imports cleanly headless (there is no main window to talk to there
anyway).
"""

import FreeCAD

_PREFIX = "Railform: "


def _bar():
    """The Qt status bar of FreeCAD's main window, or None if there isn't
    one (headless, or the window isn't up yet)."""
    try:
        import FreeCADGui
        mw = FreeCADGui.getMainWindow()
        if mw is None:
            return None
        return mw.statusBar()
    except Exception:
        return None


def flash(text, msec=5000, error=False):
    """Show `text` on the status bar for `msec` milliseconds. With no status
    bar available, fall back to the Console so the message is never lost."""
    bar = _bar()
    if bar is None:
        if error:
            FreeCAD.Console.PrintError(_PREFIX + text + "\n")
        else:
            FreeCAD.Console.PrintWarning(_PREFIX + text + "\n")
        return
    try:
        bar.showMessage(_PREFIX + text, int(msec))
    except Exception:
        FreeCAD.Console.PrintWarning(_PREFIX + text + "\n")


def show(text):
    """Set a persistent status-bar line (no timeout) -- for a live readout
    that the caller will clear() when done. Silent no-op without a bar."""
    bar = _bar()
    if bar is None:
        return
    try:
        bar.showMessage(text)
    except Exception:
        pass


def clear():
    """Clear a line set by show(). Silent no-op without a bar."""
    bar = _bar()
    if bar is None:
        return
    try:
        bar.clearMessage()
    except Exception:
        pass
