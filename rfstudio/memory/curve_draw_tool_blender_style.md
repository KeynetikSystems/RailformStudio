---
name: curve_draw_tool_blender_style
description: Implementation of Blender-style curve draw tool for Railform Studio
metadata:
  type: project
---

Implemented a curve draw tool similar to Blender's functionality in the Railform Studio FreeCAD workbench. This tool allows users to draw curves freely in 3D space by clicking to place points with real-time preview and parametric curve creation.

Key features:
- Continuous point placement through mouse clicks in draw mode
- Real-time curve preview as points are added
- Full snap functionality support during drawing
- Mutual exclusion between drawing and picking modes
- Proper transaction management for undo/redo support
- Works with all existing point-based curve types (InterpCurve, ControlCurve, ApproxCurve, Polyline, BezierCurve)

Implementation details:
- Added draw mode toggle button to CurvePanel UI
- Implemented _on_draw_click() for handling mouse clicks to add points continuously
- Implemented _on_draw_move() for tracking cursor and displaying snap points during drawing
- Enhanced _pre_accept() and _pre_reject() to handle draw mode completion/cancellation properly
- Added RFS_DrawFreehand command to toolshelf with pencil icon
- Factory function ensures proper panel creation with draw mode auto-activated

Files modified:
- rfstudio/panel_curve.py: Core draw mode logic
- rfstudio/commands.py: New command definition and toolbar entry

Related memories:
- [[project_railform_studio]]
- [[swot_railform_studio]]