# -*- coding: utf-8 -*-
"""Curated task panels -- re-export shim.

This used to be one ~2500-line file doing the job of four. The panel
implementations now live in a handful of focused modules:
  * rfstudio.panel_base    -- shared selection helpers, _CurveGroup widget,
                              and the _EditPanelBase / _ReportPanelBase
                              scaffolding (open/commit/abort-transaction,
                              resetEdit/closeDialog teardown) every panel
                              below now derives from instead of
                              reimplementing.
  * rfstudio.panel_curve   -- CurvePanel, BlendPanel.
  * rfstudio.panel_surface -- SurfacePanel, LoftPanel, OffsetThickenPanel,
                              ShellPanel, BlendSurfPanel,
                              VariableFilletPanel, RollingBallFilletPanel,
                              GlobalFairPanel.
  * rfstudio.panel_qa      -- ContinuityCheckPanel, NetworkAuditPanel,
                              SurfaceReviewPanel, WorkflowGuidePanel.
  * rfstudio.panel_import  -- ImportRailsPanel.
  * rfstudio.panel_solids  -- BooleanPanel, MeshToSurfacePanel,
                              HiddenLineExportPanel, MirrorPanel.

This module re-exports everything under the old `rfstudio.taskpanels` name
so existing call sites keep working unchanged, both
`from rfstudio import taskpanels; taskpanels.LoftPanel(...)` and
`from rfstudio.taskpanels import LoftPanel`.

GUI-only. The geometry these panels drive lives in rfstudio.core
(headless-tested).
"""

from rfstudio.panel_base import (  # noqa: F401
    _std_buttons, _selected_objects, _selected_faces, _selected_points,
    _CurveGroup, _EditPanelBase, _ReportPanelBase,
)
from rfstudio.panel_curve import _CoordSpin, CurvePanel, BlendPanel  # noqa: F401
from rfstudio.panel_surface import (  # noqa: F401
    SurfacePanel, LoftPanel, OffsetThickenPanel, ShellPanel, BlendSurfPanel,
    StitchShellPanel, FilletPanel, ChamferPanel, PolylineFilletPanel,
    JoinCurvePanel, BlendSurfClosedPanel,
    TrimByIntersectionPanel, VariableFilletPanel, RollingBallFilletPanel,
    GlobalFairPanel,
)
from rfstudio.panel_qa import (  # noqa: F401
    ContinuityCheckPanel, NetworkAuditPanel, SurfaceReviewPanel,
    WorkflowGuidePanel, GaussianCurvaturePanel, BalanceNetworkPanel,
)
from rfstudio.panel_import import ImportRailsPanel  # noqa: F401
from rfstudio.panel_solids import (  # noqa: F401
    BooleanPanel, MeshToSurfacePanel, HiddenLineExportPanel, MirrorPanel,
)