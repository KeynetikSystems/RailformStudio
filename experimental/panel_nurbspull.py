# -*- coding: utf-8 -*-
"""NURBS Push/Pull panel for direct manipulation of control points.

Provides a tactile gizmo (dragger) for each control point of a selected
BSpline curve or surface, allowing immediate push/pull deformation.
"""

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui
try:
    from PySide import QtWidgets
except Exception:
    QtWidgets = QtGui

from rfstudio.panel_base import _EditPanelBase, _notify_overlay_unavailable

try:
    import pivy.coin as coin
    HAS_PIVY = True
except Exception:
    HAS_PIVY = False
    coin = None

try:
    from OCC.Core.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt
    from OCC.Core.gp import gp_Pnt, gp_Vec
    HAS_OCC = True
except Exception:
    HAS_OCC = False


class NURBSPushPullPanel(_EditPanelBase):
    """Task panel for interactively moving NURBS control points."""

    def __init__(self):
        title = "NURBS Push/Pull"
        self._start_edit(None, title, lambda: None)
        self.obj = None  # will be set in _setup()
        self._orig_poles = None  # deep copy of poles for abort
        self._dragger_info = []  # list of dicts describing each dragger
        self._pivot = None  # Coin node holding all draggers
        self._is_surface = False
        self._u_poles = 0
        self._v_poles = 0
        self._setup()

    # ------------------------------------------------------------------ #
    # Panel lifecycle
    # ------------------------------------------------------------------ #
    def accept(self):
        """User clicked OK – keep changes."""
        try:
            self._pre_accept()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: NURBS push/pull pre-accept failed: {exc}\n")
        try:
            if self.obj:
                self.obj.recompute()
        except Exception:
            pass
        self._extra_teardown()
        try:
            self.doc.commitTransaction()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: commit failed: {exc}\n")
        self._close()
        return True

    def reject(self):
        """User clicked Cancel – revert changes."""
        try:
            self._pre_reject()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: NURBS push/pull pre-reject failed: {exc}\n")
        self._extra_teardown()
        try:
            self.doc.abortTransaction()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: abort failed: {exc}\n")
        try:
            self._post_abort()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"Railform: post-abort hook failed: {exc}\n")
        self._close()
        return True

    def _extra_teardown(self):
        """Remove Coin overlays and restore original poles on abort."""
        self._remove_draggers()
        if self.obj and self._orig_poles is not None and not self._pre_accept_called:
            self._restore_poles()
            self.obj.recompute()

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #
    def _setup(self):
        """Initialize panel UI and Coin overlays."""
        if not HAS_PIVY:
            _notify_overlay_unavailable(self, "NURBS push/pull (requires pivy/coin)",
                                        RuntimeError("pivy not available"))
            self.reject()
            return

        sel = _selected_objects()
        if not sel:
            FreeCAD.Console.PrintMessage("NURBS Push/Pull: select a BSpline curve or surface.\n")
            self.reject()
            return

        obj = sel[0]
        shape = getattr(obj, "Shape", None)
        if shape is None:
            FreeCAD.Console.PrintMessage("NURBS Push/Pull: selected object has no shape.\n")
            self.reject()
            return

        # Determine type
        if hasattr(shape, "poles"):  # BSplineSurface
            self._is_surface = True
            self._u_poles = shape.nbUPoles
            self._v_poles = shape.nbVPoles
        elif hasattr(shape, "Poles"):  # BSplineCurve
            self._is_surface = False
            self._u_poles = len(shape.Poles)
            self._v_poles = 1
        else:
            FreeCAD.Console.PrintMessage("NURBS Push/Pull: object is not a BSpline curve or surface.\n")
            self.reject()
            return

        self.obj = obj
        self._backup_poles()

        # Add simple UI
        self.form = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(self.form)
        lbl = QtGui.QLabel("Drag the arrows to move control points.\n"
                           "OK to accept, Cancel to revert.")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        # setWidget()/setTitle() are QDockWidget methods -- this is a FreeCAD
        # task panel, which just exposes `self.form`; calling them raised
        # AttributeError while building the panel.
        self.form.setWindowTitle("NURBS Push/Pull")

        # Setup Coin draggers
        self._add_draggers()

    # ------------------------------------------------------------------ #
    # Helpers for selection (copied from panel_base)
    # ------------------------------------------------------------------ #
    def _selected_objects():
        return list(FreeCADGui.Selection.getSelection())

    # ------------------------------------------------------------------ #
    # Pole backup / restore
    # ------------------------------------------------------------------ #
    def _backup_poles(self):
        shape = self.obj.Shape
        if self._is_surface:
            # surface poles as list of list of (x,y,z)
            poles = []
            for i in range(self._u_poles):
                row = []
                for j in range(self._v_poles):
                    p = shape.poles[i+1][j+1]  # OCC arrays are 1-based
                    row.append((float(p.X()), float(p.Y()), float(p.Z())))
                poles.append(row)
            self._orig_poles = poles
        else:
            # curve poles
            poles = []
            for i in range(self._u_poles):
                p = shape.Poles[i+1]
                poles.append((float(p.X()), float(p.Y()), float(p.Z())))
            self._orig_poles = poles

    def _restore_poles(self):
        if not HAS_OCC:
            return
        shape = self.obj.Shape
        if self._is_surface:
            new_poles = TColgp_Array2OfPnt(1, self._u_poles, 1, self._v_poles)
            for i in range(self._u_poles):
                for j in range(self._v_poles):
                    x, y, z = self._orig_poles[i][j]
                    new_poles.SetValue(i+1, j+1, gp_Pnt(x, y, z))
            shape.poles = new_poles
        else:
            new_poles = TColgp_Array1OfPnt(1, self._u_poles)
            for i in range(self._u_poles):
                x, y, z = self._orig_poles[i]
                new_poles.SetValue(i+1, gp_Pnt(x, y, z))
            shape.Poles = new_poles

    # ------------------------------------------------------------------ #
    # Coin dragger handling
    # ------------------------------------------------------------------ #
    def _add_draggers(self):
        if not (HAS_PIVY and HAS_OCC):
            return
        shape = self.obj.Shape
        pl = self.obj.Placement
        base = pl.Base
        rot = pl.Rotation  # gp_Rotation

        # Create a separator to hold all draggers
        root = coin.SoSeparator()
        self._pivot = root
        doc = FreeCADGui.ActiveDocument
        view = doc.ActiveView if doc is not None else None
        if view:
            sg = view.getSceneGraph()
            sg.addChild(root)

        if self._is_surface:
            self._add_surface_draggers(root, base, rot)
        else:
            self._add_curve_draggers(root, base, rot)

    def _add_surface_draggers(self, parent, base, rot):
        shape = self.obj.Shape
        for i in range(self._u_poles):
            for j in range(self._v_poles):
                p = shape.poles[i+1][j+1]
                lx, ly, lz = float(p.X()), float(p.Y()), float(p.Z())
                # local to world: P_world = rot * P_local + base
                loc = rot.multVec(gp_Pnt(lx, ly, lz))  # rotates only
                wx = loc.X() + base.x
                wy = loc.Y() + base.y
                wz = loc.Z() + base.z
                init_world = (wx, wy, wz)

                # transform
                xf = coin.SoTransform()
                xf.translation = init_world

                # dragger
                dragger = coin.SoTranslate1Dragger()
                dragger.translation.setValue(init_world)

                info = {
                    'dragger': dragger,
                    'sep': None,
                    'i': i,
                    'j': j,
                    'init_local': (lx, ly, lz),
                    'init_world': init_world,
                    'base': base,
                    'rot': rot
                }

                # build separator: transform -> dragger
                sep = coin.SoSeparator()
                sep.addChild(xf)
                sep.addChild(dragger)
                parent.addChild(sep)
                info['sep'] = sep
                self._dragger_info.append(info)

                # connect callback
                def make_cb(info):
                    def cb(dragger=dragger, info=info):
                        # get new world position from dragger
                        new_world = dragger.translation.getValue()
                        # world delta
                        dx = new_world[0] - info['init_world'][0]
                        dy = new_world[1] - info['init_world'][1]
                        dz = new_world[2] - info['init_world'][2]
                        world_delta_vec = gp_Vec(dx, dy, dz)
                        # apply inverse rotation to get local delta
                        local_delta_vec = info['rot'].Inverted().multVec(world_delta_vec)
                        # new local point
                        nl = (info['init_local'][0] + local_delta_vec.X(),
                              info['init_local'][1] + local_delta_vec.Y(),
                              info['init_local'][2] + local_delta_vec.Z())
                        # update stored poles
                        if self._is_surface:
                            # update copy
                            row = list(self._orig_poles[info['i']])  # copy
                            row[info['j']] = nl
                            self._orig_poles[info['i']] = row
                        else:
                            self._orig_poles[info['i']] = nl
                        # rebuild shape poles and apply
                        self._apply_poles()
                    return cb
                dragger.valueChanged.connect(make_cb(info))

    def _add_curve_draggers(self, parent, base, rot):
        shape = self.obj.Shape
        for idx in range(self._u_poles):
            p = shape.Poles[idx+1]
            lx, ly, lz = float(p.X()), float(p.Y()), float(p.Z())
            # local to world
            loc = rot.multVec(gp_Pnt(lx, ly, lz))
            wx = loc.X() + base.x
            wy = loc.Y() + base.y
            wz = loc.Z() + base.z
            init_world = (wx, wy, wz)

            xf = coin.SoTransform()
            xf.translation = init_world
            dragger = coin.SoTranslate1Dragger()
            dragger.translation.setValue(init_world)

            info = {
                'dragger': dragger,
                'sep': None,
                'idx': idx,
                'init_local': (lx, ly, lz),
                'init_world': init_world,
                'base': base,
                'rot': rot
            }

            sep = coin.SoSeparator()
            sep.addChild(xf)
            sep.addChild(dragger)
            parent.addChild(sep)
            info['sep'] = sep
            self._dragger_info.append(info)

            def make_cb(info):
                def cb(dragger=dragger, info=info):
                    new_world = dragger.translation.getValue()
                    dx = new_world[0] - info['init_world'][0]
                    dy = new_world[1] - info['init_world'][1]
                    dz = new_world[2] - info['init_world'][2]
                    world_delta_vec = gp_Vec(dx, dy, dz)
                    local_delta_vec = info['rot'].Inverted().multVec(world_delta_vec)
                    nl = (info['init_local'][0] + local_delta_vec.X(),
                          info['init_local'][1] + local_delta_vec.Y(),
                          info['init_local'][2] + local_delta_vec.Z())
                    self._orig_poles[info['idx']] = nl
                    self._apply_poles()
                return cb
            dragger.valueChanged.connect(make_cb(info))

    def _apply_poles(self):
        """Apply current self._orig_poles to the object's shape."""
        if not HAS_OCC:
            return
        shape = self.obj.Shape
        if self._is_surface:
            new_poles = TColgp_Array2OfPnt(1, self._u_poles, 1, self._v_poles)
            for i in range(self._u_poles):
                for j in range(self._v_poles):
                    x, y, z = self._orig_poles[i][j]
                    new_poles.SetValue(i+1, j+1, gp_Pnt(x, y, z))
            shape.poles = new_poles
        else:
            new_poles = TColgp_Array1OfPnt(1, self._u_poles)
            for i in range(self._u_poles):
                x, y, z = self._orig_poles[i]
                new_poles.SetValue(i+1, gp_Pnt(x, y, z))
            shape.Poles = new_poles
        self.obj.recompute()

    def _remove_draggers(self):
        if self._pivot:
            try:
                doc = FreeCADGui.ActiveDocument
                view = doc.ActiveView if doc is not None else None
                if view:
                    sg = view.getSceneGraph()
                    sg.removeChild(self._pivot)
            except Exception:
                pass
            self._pivot = None
            self._dragger_info = []


def createPanel():
    return NURBSPushPullPanel()