# -*- coding: utf-8 -*-
"""Compatibility layer for FreeCAD version differences."""

import FreeCAD

def get_qt_widgets():
    """Return QtWidgets module handling PySide/PySide2/Qt5/Qt6 differences."""
    try:
        from PySide import QtWidgets
        return QtWidgets
    except ImportError:
        try:
            from PySide2 import QtWidgets  # type: ignore
            return QtWidgets
        except ImportError:
            try:
                from PySide6 import QtWidgets  # type: ignore
                return QtWidgets
            except ImportError:
                # Fallback to QtGui for very old versions
                from PySide import QtGui
                return QtGui

def get_dialog_button_value(btns):
    """Get dialog button value handling PySide2/Qt5 vs PySide6/Qt6 differences."""
    return int(getattr(btns, "value", btns))

def freecad_version_tuple():
    """Parse FreeCAD version into comparable tuple."""
    try:
        version_str = FreeCAD.Version()[0]  # Returns (string, string)
        # Parse "0.1.1" -> (0, 1, 1)
        parts = []
        for part in version_str.split('.'):
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(part)  # Keep non-numeric parts as strings
        return tuple(parts)
    except Exception:
        return (0, 0, 0)  # Assume old version on error
