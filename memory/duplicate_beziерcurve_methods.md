---
name: duplicate_beziercurve_methods
description: Duplicate method definitions in BezierCurveFP class in parametric.py - FIXED
metadata:
  type: project
  status: resolved
  fix_date: 2025-07-15
---

In rfstudio/parametric.py, the BezierCurveFP class had duplicate method definitions:

BEFORE FIX:
- Lines 350-433: Correct method definitions in BezierCurveFP class (_add_props, execute, onChanged, _enforce_handle_constraints)
- Lines 507-633: Duplicate method definitions (exact copies) that appeared after the BlendCurveFP class

ISSUE: The duplicate methods (lines 507-633) were incorrectly placed outside any class, causing code duplication and potential confusion.

FIX: Removed the duplicate method definitions (lines 507-633) while preserving the correct definitions in the BezierCurveFP class (lines 350-433).

AFTER FIX:
- Lines 350-433: Correct method definitions remain in BezierCurveFP class
- Lines 504-504: End of BlendCurveFP class now directly connects to _CURVE_ICONS definition (line 505)
- No duplicate method definitions remain

The fix ensures that each method is defined exactly once in the correct location, maintaining code clarity and preventing potential execution issues.