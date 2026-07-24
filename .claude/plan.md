# Bezier Curve Implementation Plan

## Overall Goal
Implement a dedicated BezierCurve curve type with full Bezier handle editing capabilities (like Blender/Illustrator) while keeping all existing curve types unchanged.

## Files to Modify
1. `rfstudio/parametric.py` - Core BezierCurve implementation
2. `rfstudio/panel_curve.py` - UI integration and converter controls
3. `rfstudio/editmode.py` - Bezier-specific point/handle editing logic
4. `rfstudio/changelog.md` - Document the new feature

## Detailed Implementation Steps

### 1. `rfstudio/parametric.py` - Add BezierCurve Class
- Add BezierCurve class after existing curve classes
- Implement execute() method to generate Bezier spline from points and handles
- Implement onChanged() method to handle property changes and initialize handle data
- Implement _enforce_handle_constraints() method to maintain handle relationships based on HandleTypes
- Update make_curve() function to handle BezierCurve type

### 2. `rfstudio/panel_curve.py` - Add BezierCurve Support
- Add "BezierCurve" to converter type combobox
- Add Bezier-specific UI controls when BezierCurve is selected:
  - Handle type dropdown (Auto, Aligned, Vector, Free)
  - Show/Hide handles toggle button
- Add handler methods for handle type changes and visibility toggles

### 3. `rfstudio/editmode.py` - Bezier-Specific Editing Logic
- Add Bezier detection in __init__
- Implement handle visualization methods:
  - _build_bezier_handles() - create Coin visualization for handles
  - _update_bezier_handles() - update handle positions
  - _destroy_bezier_handles() - cleanup visualization
- Add handle picking and editing logic:
  - _pick_handle() - detect if cursor is near a handle
  - _edit_bezier_handle() - handle handle dragging with constraint enforcement
  - _enforce_bezier_handle_constraints() - apply handle type constraints
- Enhance event handlers:
  - _on_move() - handle handle dragging vs point dragging
  - _on_button() - handle mouse clicks
  - _on_hover()/_update_cursor() - handle hover states and cursor changes
- Add cleanup in finish() method

### 4. `rfstudio/changelog.md` - Documentation
- Add BezierCurve feature to the "Added" section
- Describe handle types and their behaviors
- Explain user interaction methods
- Note backward compatibility

## Current Progress

### Phase 1: Foundation - COMPLETE
- [x] Reviewed existing curve implementations in parametric.py
- [x] Examined editmode.py editing logic
- [x] Reviewed panel_curve.py UI structure
- [x] Examined BEZIER_IMPLEMENTATION_PLAN.md for reference

### Phase 2: Implementation - IN PROGRESS
- [x] Create BezierCurve class in parametric.py
- [x] Update make_curve() function in parametric.py
- [ ] Add BezierCurve to converter combobox in panel_curve.py
- [ ] Add Bezier-specific UI controls in panel_curve.py
- [ ] Implement handler methods in panel_curve.py
- [ ] Add Bezier detection and visualization in editmode.py
- [ ] Implement handle picking and editing logic in editmode.py
- [ ] Enhance event handlers for Bezier editing in editmode.py
- [ ] Add cleanup in editmode.py finish() method
- [ ] Update changelog.md

### Phase 3: Testing & Integration - NOT STARTED
- [ ] Test Bezier curve creation and basic functionality
- [ ] Test handle editing with different constraint types
- [ ] Test UI controls and visual feedback
- [ ] Verify backward compatibility with existing curve types
- [ ] Test integration with curve converter
- [ ] Test persistence (save/load)

## Current Task
Continue implementing the Bezier curve functionality by adding the BezierCurve class to parametric.py and updating the make_curve() function.

## Next Steps
After completing the parametric.py changes, move on to updating panel_curve.py to add BezierCurve support to the converter and UI controls.