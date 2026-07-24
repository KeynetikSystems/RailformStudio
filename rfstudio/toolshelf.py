# -*- coding: utf-8 -*-
"""Left-docked tool shelf (Blender 'T panel' style).

A persistent QDockWidget on the left of FreeCAD's main window, holding the
curated commands as labelled tool buttons grouped into sections (Draw first).
Shown when the Railform Studio workbench activates, hidden when it deactivates.
GUI-only.
"""

import FreeCAD
import FreeCADGui

from PySide import QtCore, QtGui
try:
    from PySide import QtWidgets
except Exception:
    QtWidgets = QtGui

from rfstudio import commands

_DOCK = None


class _ToolShelf(QtWidgets.QDockWidget):

    def __init__(self, mw):
        super(_ToolShelf, self).__init__("Railform Tools", mw)
        self.setObjectName("RailformToolShelf")
        self.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea
                             | QtCore.Qt.RightDockWidgetArea)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(body)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        for group_name, cmd_ids in commands.TOOLBARS:
            lay.addWidget(self._section(group_name.replace("Railform ", ""),
                                        cmd_ids))
        lay.addStretch(1)
        scroll.setWidget(body)
        self.setWidget(scroll)
        self.setMinimumWidth(150)

    def _section(self, title, cmd_ids):
        box = QtWidgets.QGroupBox(title)
        v = QtWidgets.QVBoxLayout(box)
        v.setSpacing(2)
        v.setContentsMargins(4, 6, 4, 6)
        for cid in cmd_ids:
            v.addWidget(self._button(cid))
        return box

    def _button(self, cmd_id):
        menu, icon, tip = commands.command_info(cmd_id)
        btn = QtWidgets.QToolButton()
        btn.setText("  " + menu)
        if icon:
            btn.setIcon(QtGui.QIcon(icon))
            btn.setIconSize(QtCore.QSize(22, 22))
        btn.setToolTip(tip)
        btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        btn.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                          QtWidgets.QSizePolicy.Fixed)
        btn.setAutoRaise(True)
        btn.clicked.connect(lambda chk=False, c=cmd_id: self._run(c))
        return btn

    @staticmethod
    def _run(cmd_id):
        try:
            FreeCADGui.runCommand(cmd_id, 0)
        except Exception as exc:
            FreeCAD.Console.PrintError("Railform: %s failed (%s)\n"
                                       % (cmd_id, exc))


def show():
    global _DOCK
    mw = FreeCADGui.getMainWindow()
    if mw is None:
        return
    if _DOCK is None:
        _DOCK = _ToolShelf(mw)
        mw.addDockWidget(QtCore.Qt.LeftDockWidgetArea, _DOCK)
    _DOCK.setVisible(True)
    _DOCK.raise_()


def hide():
    if _DOCK is not None:
        _DOCK.setVisible(False)
