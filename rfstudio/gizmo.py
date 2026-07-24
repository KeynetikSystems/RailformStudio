# -*- coding: utf-8 -*-
"""Blender-style 3-axis move gizmo (RGB arrows) built on Coin3D.

Three SoTranslate1Dragger handles, one per world axis, dressed with colored
arrow geometry (X=red, Y=green, Z=blue). Dragging an arrow translates the point
along that axis; the panel gets a callback with the new world position.

GUI-only (needs pivy/Coin). Everything is guarded: if the Coin build differs,
`ok` stays False and the panel falls back to its yellow marker with no gizmo.

This already self-detects per-instance (try/except around _build()), unlike
editmode.py's PointEditor/KnotEditor which assume native draggers are unsafe
unconditionally -- see editmode.native_draggers_enabled() for the equivalent
override there.
"""

import FreeCAD
from FreeCAD import Vector

_X = (1.0, 0.25, 0.25)
_Y = (0.35, 0.9, 0.35)
_Z = (0.35, 0.55, 1.0)
_ACTIVE = (1.0, 0.95, 0.3)
_HALF_PI = 1.5707963267948966


class AxisGizmo(object):

    def __init__(self, on_change, on_finish=None):
        self.on_change = on_change      # callable(Vector) during a drag
        self.on_finish = on_finish      # callable() when a drag ends
        self.ok = False
        self.root = None
        self._view = None
        self._sync = False
        self.base = None
        self.s = 1.0
        try:
            self._build()
            self.ok = True
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: axis gizmo unavailable (%s)\n" % exc)

    # ---- construction ---- #
    def _build(self):
        from pivy import coin
        self.coin = coin
        self.root = coin.SoSeparator()
        self.switch = coin.SoSwitch()
        self.switch.whichChild = -1

        giz = coin.SoSeparator()
        self.trans = coin.SoTranslation()      # position only -- NO scale node
        giz.addChild(self.trans)               # above the draggers (that crashes)

        # (color, rotation axis to map local +X -> world axis, angle)
        specs = [(_X, None, 0.0),
                 (_Y, coin.SoRotationXYZ.Z, _HALF_PI),
                 (_Z, coin.SoRotationXYZ.Y, -_HALF_PI)]
        self.draggers = []
        self.arrow_scales = []                 # scale lives INSIDE each arrow
        for rgb, axis, angle in specs:
            asep = coin.SoSeparator()
            if axis is not None:
                rot = coin.SoRotationXYZ()
                rot.axis = axis
                rot.angle = angle
                asep.addChild(rot)
            d = coin.SoTranslate1Dragger()
            self._dress(d, rgb)
            d.addValueChangedCallback(self._changed)
            d.addFinishCallback(self._finish)
            asep.addChild(d)
            giz.addChild(asep)
            self.draggers.append(d)

        self.switch.addChild(giz)
        self.root.addChild(self.switch)

    def _dress(self, dragger, rgb):
        coin = self.coin
        a1, s1 = self._arrow(rgb)
        a2, s2 = self._arrow(_ACTIVE)
        dragger.setPart("translator", a1)
        dragger.setPart("translatorActive", a2)
        self.arrow_scales.extend((s1, s2))
        for part in ("feedback", "feedbackActive"):
            try:
                dragger.setPart(part, coin.SoSeparator())   # hide long lines
            except Exception:
                pass

    def _arrow(self, rgb):
        """An arrow (shaft + cone) pointing along local +X. Returns (sep, scale)
        where `scale` sizes the VISUAL only -- below the dragger, so the
        dragger's projector stays in unscaled world units."""
        coin = self.coin
        sep = coin.SoSeparator()
        sc = coin.SoScale()
        sc.scaleFactor.setValue(1.0, 1.0, 1.0)
        sep.addChild(sc)
        mat = coin.SoMaterial()
        mat.diffuseColor.setValue(*rgb)
        mat.emissiveColor.setValue(rgb[0] * 0.45, rgb[1] * 0.45, rgb[2] * 0.45)
        sep.addChild(mat)
        rot = coin.SoRotationXYZ()       # default cyl/cone axis is +Y -> rotate to +X
        rot.axis = coin.SoRotationXYZ.Z
        rot.angle = -_HALF_PI
        sep.addChild(rot)
        shaft_t = coin.SoTranslation()
        shaft_t.translation.setValue(0.0, 0.55, 0.0)
        sep.addChild(shaft_t)
        cyl = coin.SoCylinder()
        cyl.radius = 0.045
        cyl.height = 1.1
        sep.addChild(cyl)
        tip_t = coin.SoTranslation()
        tip_t.translation.setValue(0.0, 0.7, 0.0)
        sep.addChild(tip_t)
        cone = coin.SoCone()
        cone.bottomRadius = 0.14
        cone.height = 0.36
        sep.addChild(cone)
        return sep, sc

    # ---- scene attach ---- #
    def attach(self, view):
        if not self.ok or view is None:
            return
        self._view = view
        view.getSceneGraph().addChild(self.root)

    def detach(self):
        if self.ok and self._view is not None:
            try:
                self._view.getSceneGraph().removeChild(self.root)
            except Exception as exc:
                # Safe to log: this runs from panel teardown (Qt event,
                # not a Coin drag callback), unlike _changed/_finish below
                # where even a print() risks the same crash the silence
                # there is protecting against.
                FreeCAD.Console.PrintWarning(
                    "Railform: gizmo detach failed (%s)\n" % exc)
            self._view = None

    # ---- placement ---- #
    def show(self, vec, scale):
        if not self.ok:
            return
        self.base = Vector(vec.x, vec.y, vec.z)
        self.s = scale
        self._sync = True
        try:
            self.trans.translation.setValue(vec.x, vec.y, vec.z)
            for sc in self.arrow_scales:        # size visuals only
                sc.scaleFactor.setValue(scale, scale, scale)
            for d in self.draggers:
                d.translation.setValue(0.0, 0.0, 0.0)
        finally:
            self._sync = False
        self.switch.whichChild = 0

    def hide(self):
        if self.ok:
            self.switch.whichChild = -1
            self.base = None

    # ---- drag handling ---- #
    def _read(self):
        # draggers now live in unscaled world space -> translation IS the delta
        tx = self.draggers[0].translation.getValue().getValue()[0]
        ty = self.draggers[1].translation.getValue().getValue()[0]
        tz = self.draggers[2].translation.getValue().getValue()[0]
        return Vector(self.base.x + tx, self.base.y + ty, self.base.z + tz)

    def _changed(self, *args):
        # base stays fixed between show() calls; _read sums all three axis
        # offsets, so dragging X then Y composes correctly with no rebasing.
        # IMPORTANT: on_change must DEFER the recompute (QTimer) -- rebuilding
        # the curve's scene node here, mid Coin drag-event, is an access
        # violation.
        if self._sync or self.base is None:
            return
        try:                       # never let a Python error reach Coin
            self.on_change(self._read())
        except Exception:
            pass

    def _finish(self, *args):
        if self._sync or self.base is None:
            return
        try:
            if self.on_finish is not None:
                self.on_finish()
        except Exception:
            pass
