# BezierCurve Implementation Plan for Railform Studio

## Goal
Add a dedicated `BezierCurve` curve type with full Bezier handle editing capabilities (like Blender/Illustrator) while keeping all existing curve types (InterpCurve, ControlCurve, ApproxCurve, Polyline) unchanged and simple point-based.

## Files to Modify
1. `rfstudio/parametric.py` - Core BezierCurve implementation
2. `rfstudio/panel_curve.py` - UI integration and converter controls
3. `rfstudio/editmode.py` - Bezier-specific point/handle editing logic
4. `rfstudio/changelog.md` - Document the new feature

## Detailed Implementation Steps

### 1. `rfstudio/parametric.py` - Add BezierCurve Class

Add after existing curve classes or in appropriate section:

```python
class BezierCurve:
    """Bezier curve with editable handles for precise curvature control."""
    
    def __init__(self, obj):
        obj.Proxy = self
        self.Type = "BezierCurve"
        
    def execute(self, fp):
        """Generate the Bezier spline from points and handle data."""
        import Part
        from FreeCAD import Vector
        
        points = fp.Points
        if not points or len(points) < 2:
            fp.Shape = Part.Shape()
            return
            
        # Ensure we have handle data
        if not hasattr(fp, "LeftHandles") or not fp.LeftHandles:
            fp.LeftHandles = [Vector(0,0,0)] * len(points)
        if not hasattr(fp, "RightHandles") or not fp.RightHandles:
            fp.RightHandles = [Vector(0,0,0)] * len(points)
        if not hasattr(fp, "HandleTypes") or not fp.HandleTypes:
            fp.HandleTypes = ["Auto"] * len(points)
            
        # Create Bezier spline segments
        try:
            # For n points, we create n-1 cubic Bezier segments
            segments = []
            for i in range(len(points) - 1):
                # Segment from points[i] to points[i+1]
                # Control points: Right handle of i, Left handle of i+1
                seg = Part.BSplineCurve()
                seg.buildFromPolesMultsKnots(
                    [points[i], fp.RightHandles[i], fp.LeftHandles[i+1], points[i+1]],
                    [1, 1, 1, 1],  # Multiplicities
                    [0, 1/3, 2/3, 1],  # Knots for uniform cubic Bezier
                    False, False, 3  # Not periodic, not rational, degree 3
                )
                segments.append(seg.toShape())
            
            # Join all segments
            if segments:
                fp.Shape = Part.Compound(segments)
            else:
                fp.Shape = Part.Shape()
                
        except Exception as e:
            FreeCAD.Console.PrintError(f"Railform: BezierCurve execution failed: {e}\n")
            fp.Shape = Part.Shape()
            
    def onChanged(self, fp, prop):
        """Handle property changes - ensure handle data consistency."""
        if prop == "Points":
            # Initialize handle data when points change
            n = len(fp.Points)
            if not hasattr(fp, "LeftHandles") or len(fp.LeftHandles) != n:
                fp.LeftHandles = [Vector(0,0,0)] * n
            if not hasattr(fp, "RightHandles") or len(fp.RightHandles) != n:
                fp.RightHandles = [Vector(0,0,0)] * n
            if not hasattr(fp, "HandleTypes") or len(fp.HandleTypes) != n:
                fp.HandleTypes = ["Auto"] * n
                
        elif prop in ["LeftHandles", "RightHandles", "HandleTypes"]:
            # When handle data changes, enforce constraints based on type
            self._enforce_handle_constraints(fp)
            
    def _enforce_handle_constraints(self, fp):
        """Ensure handle relationships match HandleTypes."""
        if not hasattr(fp, "HandleTypes"):
            return
            
        for i in range(len(fp.Points)):
            htype = fp.HandleTypes[i] if i < len(fp.HandleTypes) else "Auto"
            left = fp.LeftHandles[i] if i < len(fp.LeftHandles[i] if i < len(fp.LeftHandles) else Vector(0,0,0)
            right = fp.RightHandles[i] if i < len(fp.RightHandles) else Vector(0,0,0)
            point = fp.Points[i] if i < len(fp.Points) else Vector(0,0,0)
            
            if htype == "Auto":
                # Auto: Handles adjust to maintain smooth curve (simplified)
                # In reality, this would calculate based on neighbors
                pass
            elif htype == "Aligned":
                # Aligned: Handles opposite (180°) but lengths can vary
                if left.length > 0 and right.length > 0:
                    # Keep opposite direction but allow independent scaling
                    left_dir = (left - point).normalize()
                    right_dir = (right - point).normalize()
                    # If not roughly opposite, adjust right to match left's opposite
                    if abs(left_dir.dot(right_dir) + 1) > 0.1:  # Not close to -1
                        fp.RightHandles[i] = point - (left - point)
            elif htype == "Vector":
                # Vector: Handles locked together (same angle, can scale independently)
                if left.length > 0 and right.length > 0:
                    left_dir = (left - point).normalize()
                    right_dir = (right - point).normalize()
                    # If not same direction, adjust right to match left
                    if abs(left_dir.dot(right_dir) - 1) > 0.1:  # Not close to 1
                        fp.RightHandles[i] = point + (left - point)
            # "Free" type: No constraints - handles independent
            
        # Touch the object to trigger recompute if constraints were adjusted
        # (This is simplified - in practice we'd need to detect actual changes)
```

### 2. Update `parametric.make_curve()` to Handle BezierCurve

Find the `make_curve` function and add:
```python
elif kind == "BezierCurve":
    obj = FreeCAD.ActiveDocument.addObject("Part::Feature", "BezierCurve")
    obj.Label = "BezierCurve"
    obj.addProperty("App::PropertyVectorList", "Points", "Bezier", 
                   "Control points of the Bezier curve")
    obj.addProperty("App::PropertyVectorList", "LeftHandles", "Bezier", 
                   "Left handle positions for each point")
    obj.addProperty("App::PropertyVectorList", "RightHandles", "Bezier", 
                   "Right handle positions for each point")
    obj.addProperty("App::PropertyStringList", "HandleTypes", "Bezier", 
                   "Handle type for each point: Auto, Aligned, Vector, Free")
    
    # Set proxy
    obj.Proxy = BezierCurve(obj)
```

### 3. `rfstudio/panel_curve.py` - Add BezierCurve Support

In `_add_converter_controls()`:
```python
self.convert_type_combo.addItems(["InterpCurve", "ControlCurve", "ApproxCurve", "Polyline", "BezierCurve"])
```

Add Bezier-specific UI when selected:
```python
# In __init__ or a new method like _setup_bezier_controls
if self.kind == "BezierCurve":
    # Add handle type controls
    bezier_row = QtWidgets.QHBoxLayout()
    bezier_row.addWidget(QtWidgets.QLabel("Handle Type:"))
    self.handle_type_combo = QtWidgets.QComboBox()
    self.handle_type_combo.addItems(["Auto", "Aligned", "Vector", "Free"])
    self.handle_type_combo.currentTextChanged.connect(self._on_handle_type_changed)
    bezier_row.addWidget(self.handle_type_combo)
    bezier_row.addStretch()
    self._lay.addLayout(bezier_row)
    
    # Add button to toggle handle visibility
    self.toggle_handles_btn = QtWidgets.QPushButton("Show Handles: On")
    self.toggle_handles_btn.setCheckable(True)
    self.toggle_handles_btn.setChecked(True)
    self.toggle_handles_btn.toggled.connect(self._toggle_handle_visibility)
    self._lay.addWidget(self.toggle_handles_btn)
```

Add the handler methods:
```python
def _on_handle_type_changed(self, htype):
    """Handle handle type selection change."""
    if self._editor and hasattr(self._editor, "set_bezier_handle_type"):
        rows = self._selected_rows()
        if rows:
            self._editor.set_bezier_handle_type(rows[0], htype)
            self._fill_table()  # Refresh to show any visual changes

def _toggle_handle_visibility(self, visible):
    """Toggle Bezier handle visualization."""
    if self._editor and hasattr(self._editor, "set_handle_visibility"):
        self._editor.set_handle_visibility(visible)
```

### 4. `rfstudio/editmode.py` - Bezier-Specific Editing Logic

Add to `__init__`:
```python
# Detect if we're editing a BezierCurve
self.is_bezier = (hasattr(self.obj, "Proxy") and 
                 getattr(self.obj.Proxy, "Type", None) == "BezierCurve")
# Bezier handle visualization
self.handle_vis_root = None
self.handles_visible = True
# For handle dragging
self.dragging_handle = None  # (point_idx, 'left'|'right') or None
```

Add handle visualization methods:
```python
def _build_bezier_handles(self):
    """Build Coin visualization for Bezier handles."""
    if not self.is_bezier or not self.coin:
        return
        
    try:
        from pivy import coin
        self.handle_vis_root = coin.SoSeparator()
        
        # Left handles (blue squares)
        left_sep = coin.SoSeparator()
        left_col = coin.SoBaseColor()
        left_col.rgb = (0.2, 0.6, 1.0)  # Blue
        left_sep.addChild(left_col)
        left_style = coin.SoDrawStyle()
        left_style.pointWidth = 8
        left_sep.addChild(left_style)
        left_coords = coin.SoCoordinate3()
        left_markers = coin.SoMarkerSet()
        left_markers.markerIndex = coin.SoMarkerSet.SQUARE
        left_sep.addChild(left_coords)
        left_sep.addChild(left_markers)
        self.handle_vis_root.addChild(left_sep)
        self.left_handle_coords = left_coords
        self.left_handle_markers = left_markers
        
        # Right handles (green squares)
        right_sep = coin.SoSeparator()
        right_col = coin.SoBaseColor()
        right_col.rgb = (0.2, 1.0, 0.6)  # Green
        right_sep.addChild(right_col)
        right_style = coin.SoDrawStyle()
        right_style.pointWidth = 8
        right_sep.addChild(right_style)
        right_coords = coin.SoCoordinate3()
        right_markers = coin.SoMarkerSet()
        right_markers.markerIndex = coin.SoMarkerSet.SQUARE
        right_sep.addChild(right_coords)
        right_sep.addChild(right_markers)
        self.handle_vis_root.addChild(right_sep)
        self.right_handle_coords = right_coords
        self.right_handle_markers = right_markers
        
        # Connect lines from points to handles (light gray)
        line_sep = coin.SoSeparator()
        line_col = coin.SoBaseColor()
        line_col.rgb = (0.7, 0.7, 0.7)
        line_sep.addChild(line_col)
        line_style = coin.SoDrawStyle()
        line_style.lineWidth = 1
        line_style.linePattern = coin.SoDrawStyle.DOT  # Dashed
        line_sep.addChild(line_style)
        line_coords = coin.SoCoordinate3()
        line_set = coin.SoLineSet()
        line_sep.addChild(line_coords)
        line_sep.addChild(line_set)
        self.handle_vis_root.addChild(line_sep)
        self.line_coords = line_coords
        self.line_set = line_set
        
        # Add to scene
        view = FreeCADGui.ActiveDocument.ActiveView
        if view:
            view.getSceneGraph().addChild(self.handle_vis_root)
            
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"Railform: Bezier handle visualization unavailable ({exc})\n")

def _update_bezier_handles(self):
    """Update Bezier handle visualization."""
    if not self.is_bezier or not self.handle_vis_root or not self.handles_visible:
        return
        
    try:
        pts = self._points()
        if not pts:
            return
            
        # Get handle data
        left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
        right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))
        
        # Update left handle positions
        left_pts = [(h.x, h.y, h.z) for h in left_handles]
        self.left_handle_coords.point.setValues(0, len(left_pts), left_pts)
        self.left_handle_markers.numPoints = len(left_pts)
        
        # Update right handle positions
        right_pts = [(h.x, h.y, h.z) for h in right_handles]
        self.right_handle_coords.point.setValues(0, len(right_pts), right_pts)
        self.right_handle_markers.numPoints = len(right_pts)
        
        # Update connecting lines (point to left handle, point to right handle)
        line_pts = []
        for i, (p, lh, rh) in enumerate(zip(pts, left_handles, right_handles)):
            line_pts.append((p.x, p.y, p.z))  # Point to left
            line_pts.append((lh.x, lh.y, lh.z))
            line_pts.append((p.x, p.y, p.z))  # Point to right
            line_pts.append((rh.x, rh.y, rh.z))
        self.line_coords.point.setValues(0, len(line_pts), line_pts)
        # Each handle connection is 2 points, so numVertices = 2 per connection
        num_vertices = [2] * (len(pts) * 2)  # Left and right for each point
        self.line_set.numValues = len(num_vertices)
        self.line_set.numVertices.setValues(0, len(num_vertices), num_vertices)
        
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"Railform: Error updating Bezier handles: {exc}\n")

def _destroy_bezier_handles(self):
    """Destroy Bezier handle visualization."""
    if self.handle_vis_root:
        try:
            view = FreeCADGui.ActiveDocument.ActiveView
            if view:
                view.getSceneGraph().removeChild(self.handle_vis_root)
        except Exception:
            pass
        self.handle_vis_root = None
```

In `_build()` method, add after existing visualization:
```python
# ... existing _build() existing code ...
 
        # ---- snap_indicator --->      # --- code appears        # snap indicator: cyan ring that appears on a nearby existing point
        # ... existing snap indicator code ...
        
        # Add Bezier handle visualization if applicable
        if self.is_bezier:
            self._build_bezier_handles()
            
        self.view.getSceneGraph().addChild(self.root)
        self._refresh_markers()
        self._refresh_arrows()
        if self.is_bezier:
            self._update_bezier_handles()
```

In `finish()` method, add cleanup:
```python
# ... existing finish code ...
    def finish(self):
        self._stop_watchdog()
        self._destroy_symmetry_preview()
        self._destroy_bezier_handles()  # <-- ADD THIS
        try:
            if self._cb_btn is not None:
                self.view.removeEventCallback("SoMouseButtonEvent", self._cb_btn)
            if self._cb_mov is not None:
                self.view.removeEventCallback("SoLocation2Event", self._cb_mov)
            if self._cb_key is not None:
                self.view.removeEventCallback("SoKeyboardEvent", self._cb_key)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: point editor callback teardown failed (%s)\n" % exc)
        try:
            self.view.getSceneGraph().removeChild(self.root)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Railform: point editor scene teardown failed (%s)\n" % exc)
```

Add handle picking logic:
```python
def _pick_handle(self, pos):
    """Check if cursor is near a Bezier handle, return (point_idx, 'left'|'right') or None."""
    if not self.is_bezier:
        return None
        
    pts = self._points()
    if not pts:
        return None
        
    left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
    right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))
    
    # Convert screen pos to 3D point on view plane
    view_point = self.view.getPoint(pos[0], pos[1])
    if view_point is None:
        return None
        
    # Check each handle
    min_dist = float('inf')
    closest = None
    
    # Check left handles (blue)
    for i, handle in enumerate(left_handles):
        dist = self._get_screen_distance(view_point, handle)
        if dist < min_dist and dist < 12:  # 12 pixel threshold for handles
            min_dist = dist
            closest = (i, 'left')
            
    # Check right handles (green)
    for i, handle in enumerate(right_handles):
        dist = self._get_screen_distance(view_point, handle)
        if dist < min_dist and dist < 12:
            min_dist = dist
            closest = (i, 'right')
            
    return closest

def _get_screen_distance(self, point1, point2):
    """Calculate screen distance between two 3D points."""
    if point1 is None or point2 is None:
        return float('inf')
    try:
        screen1 = self._screen(point1)
        screen2 = self._screen(point2)
        if screen1 is None or screen2 is None:
            return float('inf')
        dx = screen1[0] - screen2[0]
        dy = screen1[1] - screen2[1]
        return (dx * dx + dy * dy) ** 0.5
    except Exception:
        return float('inf')
```

Enhance `_on_move` to handle Bezier editing:
```python
def _on_move(self, ev):
    try:
        pos = ev["Position"]
        if self.drag:
            # Handle dragging (point, handle, or axis-constrained)
            if self.drag.get("type") == "handle":
                # Handle dragging
                self._edit_bezier_handle(self.drag["info"], pos)
            elif self.drag.get("type") == "point":
                # Point dragging (existing logic)
                g = self.view.getPoint(pos[0], pos[1])
                delta = g - self.drag["g0"]
                if self.drag["axis"] is not None:
                    a = _AXES[self.drag["axis"]]
                    delta = a * delta.dot(a)
                newp = self.drag["p0"] + delta
                snapped = self._snap(pos)
                if snapped is not None:
                    newp = snapped
                    self._snap_coord.point.set1Value(0, snapped.x, snapped.y, snapped.z)
                    self._snap_switch.whichChild = 0
                else:
                    self._snap_switch.whichChild = -1
                self._live(self.drag["idx"], newp)
            else:
                # Regular dragging (existing logic)
                g = self.view.getPoint(pos[0], pos[1])
                delta = g - self.drag["g0"]
                if self.drag["axis"] is not None:
                    a = _AXES[self.drag["axis"]]
                    delta = a * delta.dot(a)
                newp = self.drag["p0"] + delta
                snapped = self._snap(pos)
                if snapped is not None:
                    newp = snapped
                    self._snap_coord.point.set1Value(0, snapped.x, snapped.y, snapped.z)
                    self._snap_switch.whichChild = 0
                else:
                    self._snap_switch.whichChild = -1
                self._live(self.drag["idx"], newp)
        else:
            # Handle hover detection when not dragging
            self._on_hover(pos)
    except Exception:
        pass
```

Add Bezier handle editing:
```python
def _edit_bezier_handle(self, handle_info, pos):
    """Edit a Bezier handle based on handle type constraints."""
    point_idx, which = handle_info  # which is 'left' or 'right'
    view_point = self.view.getPoint(pos[0], pos[1])
    if view_point is None:
        return
        
    # Get current data
    points = self._points()
    left_handles = list(getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(points)))
    right_handles = list(getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(points)))
    handle_types = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(points)))
    
    if point_idx >= len(points):
        return
        
    point = points[point_idx]
    
    # Update the appropriate handle
    if which == 'left':
        left_handles[point_idx] = view_point
    else:  # right
        right_handles[point_idx] = view_point
        
    # Enforce handle type constraints
    self._enforce_bezier_handle_constraints(point_idx, left_handles, right_handles, handle_types)
    
    # Update the object
    obj = self.obj
    obj.LeftHandles = left_handles
    obj.RightHandles = right_handles
    obj.HandleTypes = handle_types
    obj.recompute()
    obj.Document.recompute()
    
    # Update visualization
    self._update_bezier_handles()

def _enforce_bezier_handle_constraints(self, point_idx, left_handles, right_handles, handle_types):
    """Apply handle type constraints after editing a handle."""
    if point_idx >= len(handle_types):
        return
        
    htype = handle_types[point_idx]
    point = self._points()[point_idx] if point_idx < len(self._points()) else Vector(0,0,0)
    left = left_handles[point_idx]
    right = right_handles[point_idx]
    
    if htype == "Auto":
        # Auto: Simplified - in reality would calculate smooth tangents
        # For now, we'll just leave as-is (user can adjust both handles)
        pass
    elif htype == "Aligned":
        # Aligned: Keep handles opposite (180°)
        if which == 'left' and right.length > 0:
            # Left handle moved - set right to be opposite
            left_dir = (left - point).normalize()
            right_handles[point_idx] = point - left_dir * (right - point).length
        elif which == 'right' and left.length > 0:
            # Right handle moved - set left to be opposite
            right_dir = (right - point).normalize()
            left_handles[point_idx] = point - right_dir * (left - point).length
    elif htype == "Vector":
        # Vector: Keep handles parallel (same angle)
        if which == 'left' and right.length > 0:
            # Left handle moved - set right to same direction
            left_dir = (left - point).normalize()
            right_handles[point_idx] = point + left_dir * (right - point).length
        elif which == 'right' and left.length > 0:
            # Right handle moved - set left to same direction
            right_dir = (right - point).normalize()
            left_handles[point_idx] = point + right_dir * (left - point).length
    # "Free": No constraints - leave as is
```

Add handle picking to `_on_button` or create a new method:
```python
def _on_button(self, ev):
    try:
        if ev.get("Type") != "SoMouseButtonEvent":
            return
        if ev.get("Button") != "BUTTON1":
            return
        if ev.get("State") == "DOWN":
            self._press(ev["Position"])
        elif ev.get("State") == "UP":
            self._release()
    except Exception:
        pass
```

Enhance `_press` to handle handle selection:
```python
def _press(self, pos):
    if self.drag:
        # A previous drag never got its mouse-up -- force it closed
        self._release()

    # Check for Ctrl+click for coordinate input (existing)
    if self.dragging_with_ctrl and not self.drag:
        self._show_input_field(pos)
        return

    # Check for handle selection (Bezier only)
    if not self.drag and self.is_bezier:
        handle_info = self._pick_handle(pos)
        if handle_info is not None:
            # Start handle dragging
            self.drag = {"type": "handle", "info": handle_info, "g0": self.view.getPoint(pos[0], pos[1])}
            self._start_watchdog()
            return

    # Check for click on segment for insertion (existing)
    if not self.drag:
        segment_idx = self._pick_segment(pos)
        if segment_idx is not None:
            view_point = self.view.getPoint(pos[0], pos[1])
            if view_point is not None:
                self.insert_point(segment_idx, view_point)
                return

    # Handle point selection for dragging/deletion (existing)
    axis = self._pick_axis(pos)
    if axis is not None:
        self._begin(self.sel, axis, pos)
        return
    idx = self._pick_point(pos)
    if idx is not None:
        self.sel = idx
        self._refresh_arrows()
        self._begin(idx, None, pos)
```

Add handle type setting method:
```python
def set_bezier_handle_type(self, point_idx, htype):
    """Set the handle type for a specific point."""
    if not self.is_bezier or point_idx < 0 or point_idx >= len(self._points()):
        return
        
    handle_types = list(getattr(self.obj, "HandleTypes", ["Auto"] * len(self._points())))
    if point_idx < len(handle_types):
        handle_types[point_idx] = htype
        self.obj.HandleTypes = handle_types
        self.obj.recompute()
```

Add handle visibility toggling:
```python
def set_handle_visibility(self, visible):
    """Show or hide Bezier handle visualization."""
    self.handles_visible = visible
    if self.is_bezier:
        if visible and self.handle_vis_root:
            self._update_bezier_handles()
        elif not visible:
            self._destroy_bezier_handles()
```

Update `_on_key` for handle type shortcuts:
```python
def _on_key(self, ev):
    """Handle key presses for point deletion, Ctrl key tracking, and handle types."""
    try:
        if ev.get("Type") != "SoKeyboardEvent":
            return
        if ev.get("State") == "DOWN":
            key = ev.get("Key")
            # Track Ctrl key state
            if key == 17:  # Ctrl key
                self.dragging_with_ctrl = True
            # Handle type shortcuts (when not creating)
            elif not self.creating and self.is_bezier:
                self.creating:
                    if key == 65:  # A - Auto
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Auto")
                    elif key == 76:  # L - Aligned
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Aligned")
                    elif key == 86:  # V - Vector
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Vector")
                    elif key == 70:  # F - Free
                        if self.hover_point is not None:
                            self.set_bezier_handle_type(self.hover_point, "Free")
            # Delete or Backspace key
            elif key == 127 or key == 8:
                if self.hover_point is not None and not self.creating:
                    self.delete_point(self.hover_point)
        elif ev.get("State") == "UP":
            key = ev.get("Key")
            # Track Ctrl key state
            if key == 17:  # Ctrl key
                self.dragging_with_ctrl = False
                # Hide input field if visible when Ctrl is released
                self._hide_input_field()
    except Exception:
        pass
```

Update `_on_hover` to also highlight handles:
```python
def _on_hover(self, pos):
    """Handle mouse movement when not dragging for hover detection."""
    # Reset hover states
    old_hover_point = self.hover_point
    old_hover_segment = self.hover_segment
    old_hover_handle = self.hover_handle
    self.hover_point = None
    self.hover_segment = None
    self.hover_handle = None  # (point_idx, 'left'|'right') or None

    try:
        # Check for hover over handles (for editing)
        if not self.creating and self.is_bezier:
            self.hover_handle = self._pick_handle(pos)

        # Check for hover over points (for deletion)
        if not self.creating and self.hover_handle is None:
            self.hover_point = self._pick_point(pos)

            # If not hovering over a point, check for hover over segment (for insertion)
            if self.hover_point is None:
                self.hover_segment = self._pick_segment(pos)

        # Update cursor based on hover state
        self._update_cursor()

        # Update hover state if changed
        if (self.hover_point != old_hover_point or
            self.hover_segment != old_hover_segment or
            self.hover_handle != old_hover_handle):
            # Trigger a redraw to show hover highlight
            self._update_hover_highlight()
            self._update_bezier_handle_highlight()  # <-- ADD THIS

    except Exception:
        pass
```

Add Bezier handle highlight method:
```python
def _update_bezier_handle_highlight(self):
    """Update visual highlight for Bezier handle hover state."""
    if not self.is_bezier or not self.handle_vis_root:
        return
        
    # Reset all highlights
    self.left_handle_markers.numPoints = 0
    self.right_handle_markers.numPoints = 0
    
    if self.hover_handle is not None:
        point_idx, which = self.hover_handle
        pts = self._points()
        if 0 <= point_idx < len(pts):
            if which == 'left':
                # Highlight left handle
                left_handles = getattr(self.obj, "LeftHandles", [Vector(0,0,0)] * len(pts))
                if point_idx < len(left_handles):
                    handle_pos = left_handles[point_idx]
                    self.left_handle_coords.point.set1Value(0, handle_pos.x, handle_pos.y, handle_pos.z)
                    self.left_handle_markers.numPoints = 1
            else:  # right
                # Highlight right handle
                right_handles = getattr(self.obj, "RightHandles", [Vector(0,0,0)] * len(pts))
                if point_idx < len(right_handles):
                    handle_pos = right_handles[point_idx]
                    self.right_handle_coords.point.set1Value(0, handle_pos.x, handle_pos.y, handle_pos.z)
                    self.right_handle_markers.numPoints = 1
```

Update `_update_cursor` to include handle cursors:
```python
def _update_cursor(self):
    """Update cursor based on hover state."""
    try:
        from PySide import QtGui
        if self.hover_handle is not None:
            # Drag handle cursor (closed hand or similar)
            QtGui.QGuiApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.ClosedHandCursor))
        elif self.hover_point is not None:
            # Pointing hand cursor for deletion
            QtGui.QGuiApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        elif self.hover_segment is not None:
            # Crosshair cursor for insertion
            QtGui.QGuiApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CrossCursor))
        else:
            # Restore default cursor
            QtGui.QGuiApplication.restoreOverrideCursor()
    except Exception:
        pass
```

### 5. `rfstudio/changelog.md` - Add Documentation

Add to the "Added" section:
- **BezierCurve type with handle editing (New Feature)**
  - Added BezierCurve as a distinct curve type offering precise curvature control via editable handles
  - Handle types: Auto (smooth), Aligned (opposite tangents), Vector (locked angle), Free (independent)
  - Visual feedback: Blue squares (left handles), green squares (right handles), dashed lines connecting to points
  - Interaction: Click handles to drag, hover for cursor feedback, keyboard shortcuts (A/L/V/F for handle types)
  - Available alongside existing curve types via the curve type converter
  - Implemented in `rfstudio/parametric.py` (BezierCurve class) and `rfstudio/editmode.py` (editing logic)

## 📋 Implementation Notes

### Data Storage Approach
- **Points**: Main knot points (stored in `obj.Points` as before)
- **LeftHandles/RightHandles**: Absolute positions of handles (stored as `obj.LeftHandles`/`obj.RightHandles`)
- **HandleTypes**: String list indicating constraint type for each point (`obj.HandleTypes`)

### Curve Generation
- Each segment between `point[i]` and `point[i+1]` uses:
  - Start: `point[i]`
  - Control 1: `RightHandles[i]` (right handle of start point)
  - Control 2: `LeftHandles[i+1]` (left handle of end point)
  - End: `point[i+1]`
- Uses `Part.BSplineCurve` to generate cubic Bezier segments

### Handle Constraints
- **Auto**: Basic implementation (keeps handles as positioned - could be enhanced to auto-calculate smooth tangents)
- **Aligned**: Handles maintain 180° opposition (adjusting one handle updates the opposite to maintain angle)
- **Vector**: Handles maintain same angle (adjusting one handle updates the other to match direction)
- **Free**: No constraints - handles move independently

### User Experience
- When BezierCurve is selected in the converter, users get:
  - Handle type dropdown (Auto/Aligned/Vector/Free)
  - "Show Handles" toggle button
  - Visual handle indicators in 3D view
  - Hover over handles → closed hand cursor → drag to edit
  - Hover over points → hand cursor → Delete to remove point
  - Hover over segments → crosshair cursor → click to insert point
  - Hold Ctrl + click → coordinate input box for exact placement
  - Handle type shortcuts: A=Auto, L=Aligned, V=Vector, F=Free

### Backward Compatibility
- ✅ All existing curve types (InterpCurve, ControlCurve, ApproxCurve, Polyline) work exactly as before
- ✅ No changes to their data structures or editing behavior
- ✅ BezierCurve is opt-in via the curve type converter
- ✅ Existing projects remain unaffected

## ⏱️ Estimated Effort
- **Parametric.py changes**: 2-3 hours
- **Panel_curve.py changes**: 1-2 hours  
- **Editmode.py changes**: 4-6 hours (most complex part)
- **Testing & integration**: 2-3 hours
- **Total**: ~9-14 hours of focused development

## ✅ Advantages of This Approach
1. **Preserves existing workflow** - zero impact on current users
2. **Clear separation of concerns** - simple point editing vs. advanced handle editing
3. **Discoverable power** - users choose BezierCurve when they need precision control
4. **Professional standard** - matches industry tools like Illustrator, Inkscape, Blender
5. **Extensible** - easy to add other specialized curve types later (BSpline, NURBS, etc.)
6. **Risk mitigation** - if issues arise, users can simply not use BezierCurve

This implementation gives Railform Studio a **purpose-built precision curve tool** for when users need sophisticated profile control, while maintaining the streamlined, efficient workflow for rapid symmetric profile design that's the core strength of the workbench.