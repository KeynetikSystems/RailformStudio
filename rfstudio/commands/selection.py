# -*- coding: utf-8 -*-
"""Selection-related UI helpers."""

from typing import Any

import FreeCADGui

try:
    from PySide import QtWidgets
except Exception:
    from PySide import QtGui as QtWidgets  # type: ignore


def _confirm_destructive(cmd_id: str, menu_text: str) -> bool:
    """Show a confirmation dialog before running a destructive command.
    Returns True when the user confirms, False to abort."""
    try:
        reply = QtWidgets.QMessageBox.question(
            FreeCADGui.getMainWindow(), "Railform Studio",
            "\u201c%s\u201d will modify the selected geometry and may be hard to "
            "undo.  Continue?" % menu_text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return reply == QtWidgets.QMessageBox.Yes
    except Exception:
        return True