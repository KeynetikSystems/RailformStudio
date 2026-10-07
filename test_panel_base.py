# -*- coding: utf-8 -*-
"""Headless test for panel base classes with FreeCAD mocking."""

import sys
import os

# Add the current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock FreeCAD modules before importing our modules
class MockFreeCAD:
    class Console:
        @staticmethod
        def PrintWarning(msg):
            pass
        @staticmethod
        def PrintError(msg):
            pass
        @staticmethod
        def PrintMessage(msg):
            pass
    
    ActiveDocument = None
    Version = lambda: ["0.0.0", ""]

class MockFreeCADGui:
    ActiveDocument = None
    
    class Selection:
        @staticmethod
        def getSelection():
            return []
        @staticmethod
        def getSelectionEx():
            return []
    
    class Control:
        @staticmethod
        def closeDialog():
            pass
    
    @staticmethod
    def getMainWindow():
        return None

# Mock PySide modules
class MockPySide:
    class QtCore:
        pass
    class QtGui:
        pass
    class QtWidgets:
        class QDialogButtonBox:
            Ok = 1
            Cancel = 2
            @staticmethod
            def StandardButton():
                pass

# Apply mocks
sys.modules['FreeCAD'] = MockFreeCAD
sys.modules['FreeCADGui'] = MockFreeCADGui
sys.modules['PySide'] = MockPySide

# Now we can import our modules
try:
    from rfstudio.panel_base import _EditPanelBase, _ReportPanelBase, _CurveGroup
    print("SUCCESS: Imported panel base classes with mocking")
except Exception as e:
    print(f"FAILED: Could not import panel base classes: {e}")
    sys.exit(1)

def test_basic_functionality():
    """Test basic functionality without GUI."""
    try:
        # Test instantiation
        panel = _EditPanelBase()
        print("SUCCESS: Created _EditPanelBase instance")
        
        # Test attributes
        assert panel.doc is None
        assert panel.creating == False
        assert panel.obj is None
        print("SUCCESS: _EditPanelBase attributes correct")
        
        return True
    except Exception as e:
        print(f"FAILED: Basic functionality test failed: {e}")
        return False

def test_selection_helpers():
    """Test selection helpers return safe values."""
    try:
        from rfstudio.panel_base import _selected_objects, _selected_faces, _selected_points
        
        # Should return empty lists, not crash
        objects = _selected_objects()
        faces = _selected_faces()
        points = _selected_points()
        
        assert isinstance(objects, list)
        assert isinstance(faces, list)
        assert isinstance(points, list)
        print("SUCCESS: Selection helpers return lists")
        return True
    except Exception as e:
        print(f"FAILED: Selection helpers test failed: {e}")
        return False

def test_notify_overlay():
    """Test overlay notification doesn't crash."""
    try:
        from rfstudio.panel_base import _notify_overlay_unavailable
        
        # Should not raise exception
        _notify_overlay_unavailable(None, "test", Exception("test"))
        print("SUCCESS: Notify overlay unavailable works")
        return True
    except Exception as e:
        print(f"FAILED: Notify overlay test failed: {e}")
        return False

def test_std_buttons():
    """Test std buttons function."""
    try:
        from rfstudio.panel_base import _std_buttons
        
        result = _std_buttons()
        assert isinstance(result, int)
        print("SUCCESS: Std buttons returns integer")
        return True
    except Exception as e:
        print(f"FAILED: Std buttons test failed: {e}")
        return False

if __name__ == "__main__":
    print("Running headless panel base tests...")
    
    tests = [
        test_basic_functionality,
        test_selection_helpers,
        test_notify_overlay,
        test_std_buttons
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print(f"\nResults: {passed}/{total} tests passed")
    
    if passed == total:
        print("All tests passed!")
        sys.exit(0)
    else:
        print("Some tests failed!")
        sys.exit(1)