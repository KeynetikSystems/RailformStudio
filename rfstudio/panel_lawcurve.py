# -*- coding: utf-8 -*-
"""A small, self-contained "law curve" graph editor widget.

Plasticity (and most serious CAD packages -- Rhino's `_EditPointsOn` radius
graph, Fusion 360's variable-radius handles, etc.) let you shape a
variable-radius fillet by dragging POINTS ON A 2D GRAPH: parameter along the
edge on X, radius on Y. This widget is that graph, built as a plain
QWidget + paintEvent/mouse-event pair (no QGraphicsView/QGraphicsScene --
consistent with this codebase's existing preference for direct
addEventCallback-style interaction over heavier Qt machinery, see
editmode.py's own module docstring on why draggers were avoided there).

HONESTY ABOUT WHAT THIS DRAWS: core.variable_fillet_solid / core.
multi_rail_fillet both interpolate stations PIECEWISE-LINEARLY (FreeCAD's
Part.Shape.makeFillet binding has no smoothed/spline radius law -- see
core.py's docstrings for both functions). This widget draws the exact same
straight-segment polyline between stations, not a smoothed curve, so what
you see here is what you get geometrically -- it does not oversell a
continuity the underlying OCCT call doesn't provide.

Usage:
    from rfstudio.panel_lawcurve import LawCurveWidget
    graph = LawCurveWidget(y_label="Radius", y_min=0.1, y_max=10.0)
    graph.set_points([(0.0, 1.0), (0.5, 3.0), (1.0, 1.0)])
    graph.on_change = lambda pts: ...   # called (list of (t, value)) on any edit
    layout.addWidget(graph)

Interaction:
    * Left-click empty plot area  -> add a station there
    * Left-drag a station         -> move it (clamped to [0,1] x [y_min,y_max])
    * Right-click a station       -> delete it (station count never drops below 2)
    * Hover                       -> highlights the nearest station and shows
                                     its (t, value) readout

GUI-only (QWidget); no FreeCAD dependency, so this module could in principle
be exercised by test_gui_imports.py's mocked-Qt import check like every
other rfstudio.panel_* module.
"""

from PySide import QtCore, QtGui
try:                                  # Qt5/PySide2: widgets are separate
    from PySide import QtWidgets
except Exception:                     # older shim: widgets live in QtGui
    QtWidgets = QtGui


_POINT_HIT_RADIUS_PX = 10
_POINT_DRAW_RADIUS_PX = 5
_POINT_DRAW_RADIUS_ACTIVE_PX = 7


class LawCurveWidget(QtWidgets.QWidget):
    """A draggable-point (param 0..1, value) graph, styled to match the
    rest of the workbench's dark panel theme.

    `on_change`, if set, is called as `on_change(points)` after every add /
    drag / delete, with `points` a list of (t, value) tuples in
    ARBITRARY order (matching insertion/drag order, not sorted -- callers
    that need sorted stations should call `.points()`, which always
    returns them sorted by t; core.py's own station consumers sort
    internally anyway, see variable_fillet_solid/multi_rail_fillet).
    """

    def __init__(self, x_label="Param", y_label="Value",
                y_min=0.0, y_max=1.0, parent=None):
        super(LawCurveWidget, self).__init__(parent)
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.x_label = x_label
        self.y_label = y_label
        self.y_min = float(y_min)
        self.y_max = float(y_max) if y_max > y_min else y_min + 1.0
        self.on_change = None
        # internal storage: list of [x, y] MUTABLE lists, kept in insertion/
        # drag order (NOT re-sorted on every edit) so a drag index stays
        # valid for the whole gesture. Sorted views are produced on demand
        # for painting the polyline and for the public points() getter.
        self._points = [[0.0, (y_min + y_max) / 2.0],
                        [1.0, (y_min + y_max) / 2.0]]
        self._drag_idx = None
        self._hover_idx = None
        self._margin = {"left": 46, "right": 16, "top": 14, "bottom": 28}

    # ---- public API ------------------------------------------------- #
    def set_points(self, pts):
        """Replace all stations. `pts`: iterable of (t, value)."""
        pts = list(pts or [])
        if len(pts) < 2:
            pts = [(0.0, self.y_min), (1.0, self.y_min)]
        self._points = [[self._clamp_x(x), self._clamp_y(y)] for x, y in pts]
        self._drag_idx = None
        self._hover_idx = None
        self.update()

    def points(self):
        """Current stations, sorted by parameter -- what callers should
        feed to core.variable_fillet_solid / core.multi_rail_fillet."""
        return sorted([(p[0], p[1]) for p in self._points],
                      key=lambda p: p[0])

    def set_y_range(self, y_min, y_max):
        self.y_min = float(y_min)
        self.y_max = float(y_max) if y_max > y_min else y_min + 1.0
        for p in self._points:
            p[1] = self._clamp_y(p[1])
        self.update()

    def add_point(self, x, y):
        self._points.append([self._clamp_x(x), self._clamp_y(y)])
        self._fire_change()
        self.update()

    def remove_point(self, index):
        if len(self._points) > 2 and 0 <= index < len(self._points):
            del self._points[index]
            self._fire_change()
            self.update()

    # ---- geometry mapping -------------------------------------------- #
    def _clamp_x(self, x):
        return max(0.0, min(1.0, float(x)))

    def _clamp_y(self, y):
        return max(self.y_min, min(self.y_max, float(y)))

    def _plot_rect(self):
        m = self._margin
        return QtCore.QRect(m["left"], m["top"],
                            max(1, self.width() - m["left"] - m["right"]),
                            max(1, self.height() - m["top"] - m["bottom"]))

    def _to_screen(self, x, y):
        r = self._plot_rect()
        sx = r.left() + x * r.width()
        t = (y - self.y_min) / max(1e-9, (self.y_max - self.y_min))
        sy = r.bottom() - t * r.height()
        return QtCore.QPointF(sx, sy)

    def _from_screen(self, sx, sy):
        r = self._plot_rect()
        x = (sx - r.left()) / float(max(1, r.width()))
        t = (r.bottom() - sy) / float(max(1, r.height()))
        y = self.y_min + t * (self.y_max - self.y_min)
        return self._clamp_x(x), self._clamp_y(y)

    def _hit_test(self, pos):
        best, best_d = None, _POINT_HIT_RADIUS_PX
        for i, (x, y) in enumerate(self._points):
            sp = self._to_screen(x, y)
            d = ((sp.x() - pos.x()) ** 2 + (sp.y() - pos.y()) ** 2) ** 0.5
            if d < best_d:
                best, best_d = i, d
        return best

    def _fire_change(self):
        if callable(self.on_change):
            try:
                self.on_change(self.points())
            except Exception:
                pass

    # ---- painting ------------------------------------------------------ #
    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self._plot_rect()

        p.fillRect(self.rect(), QtGui.QColor("#242b33"))
        p.fillRect(r, QtGui.QColor("#1c222a"))

        pen_grid = QtGui.QPen(QtGui.QColor("#333d48"))
        pen_grid.setWidth(1)
        p.setPen(pen_grid)
        for i in range(1, 4):
            gx = r.left() + i / 4.0 * r.width()
            p.drawLine(int(gx), r.top(), int(gx), r.bottom())
        for i in range(1, 4):
            gy = r.top() + i / 4.0 * r.height()
            p.drawLine(r.left(), int(gy), r.right(), int(gy))

        p.setPen(QtGui.QPen(QtGui.QColor("#4a5563")))
        p.drawRect(r)

        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(QtGui.QColor("#9aa6b2"))
        p.drawText(QtCore.QRect(0, self.height() - 16, self.width(), 16),
                   QtCore.Qt.AlignHCenter,
                   "%s  (0 \u2192 1 along edge/seam)" % self.x_label)
        p.save()
        p.translate(12, r.center().y())
        p.rotate(-90)
        p.drawText(QtCore.QRect(-r.height() // 2, -10, r.height(), 20),
                   QtCore.Qt.AlignHCenter, self.y_label)
        p.restore()
        p.drawText(QtCore.QRect(0, r.top() - 2, self._margin["left"] - 6, 14),
                   QtCore.Qt.AlignRight, "%.3g" % self.y_max)
        p.drawText(QtCore.QRect(0, r.bottom() - 12, self._margin["left"] - 6, 14),
                   QtCore.Qt.AlignRight, "%.3g" % self.y_min)

        ordered = self.points()
        if len(ordered) >= 2:
            pen_curve = QtGui.QPen(QtGui.QColor("#5fb0ff"))
            pen_curve.setWidth(2)
            p.setPen(pen_curve)
            screen_pts = [self._to_screen(x, y) for x, y in ordered]
            for i in range(len(screen_pts) - 1):
                p.drawLine(screen_pts[i], screen_pts[i + 1])

        for i, (x, y) in enumerate(self._points):
            sp = self._to_screen(x, y)
            active = (i == self._drag_idx or i == self._hover_idx)
            color = QtGui.QColor("#ffcf3a") if active else QtGui.QColor("#5fb0ff")
            p.setBrush(QtGui.QBrush(color))
            p.setPen(QtGui.QPen(QtGui.QColor("#101418"), 1.5))
            rad = _POINT_DRAW_RADIUS_ACTIVE_PX if active else _POINT_DRAW_RADIUS_PX
            p.drawEllipse(sp, rad, rad)
            if active:
                p.setPen(QtGui.QColor("#e8edf2"))
                p.drawText(sp + QtCore.QPointF(9, -8),
                          "t=%.2f  %s=%.3g" % (x, self.y_label.lower(), y))
        p.end()

    # ---- interaction ----------------------------------------------- #
    def mousePressEvent(self, ev):
        pos = ev.pos()
        idx = self._hit_test(pos)
        if ev.button() == QtCore.Qt.LeftButton:
            if idx is not None:
                self._drag_idx = idx
            elif self._plot_rect().contains(pos):
                x, y = self._from_screen(pos.x(), pos.y())
                self._points.append([x, y])
                self._drag_idx = len(self._points) - 1
                self._fire_change()
            self.update()
        elif ev.button() == QtCore.Qt.RightButton:
            if idx is not None:
                self.remove_point(idx)

    def mouseMoveEvent(self, ev):
        pos = ev.pos()
        if self._drag_idx is not None:
            x, y = self._from_screen(pos.x(), pos.y())
            self._points[self._drag_idx][0] = x
            self._points[self._drag_idx][1] = y
            self._fire_change()
            self.update()
        else:
            new_hover = self._hit_test(pos)
            if new_hover != self._hover_idx:
                self._hover_idx = new_hover
                self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and self._drag_idx is not None:
            self._drag_idx = None
            self.update()

    def leaveEvent(self, _event):
        self._hover_idx = None
        self.update()
