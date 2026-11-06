# -*- coding: utf-8 -*-
"""Command layer + curated toolbar/menu registry -- RE-EXPORT SHIM.

This file used to be a ~3000-line monolith. It has been split into
focused modules under rfstudio/commands/:

  * health.py       -- engine health tracking
  * provider.py     -- ProviderCommand, PanelCommand, HealthResetCommand
  * metadata.py     -- selection hints, panel/delegate command definitions
  * toolbars.py     -- TOOLBARS, TOOLBAR_PRIMARY
  * selection.py    -- _confirm_destructive
  * registry.py     -- register(), command_info()

This shim re-exports everything so existing call sites keep working
unchanged. The real implementations are in the submodules above.
"""

from rfstudio.commands import (  # noqa: F401,F403
    engine_health,
    engine_health_disabled,
    engine_health_record_failure,
    engine_health_record_success,
    engine_health_reset,
    _DELEGATE_META,
    _DESTRUCTIVE,
    _GENERIC_SELECTION_HINT,
    _OPENS_PANEL,
    _PANEL_COMMANDS,
    _PANEL_NEEDS_SELECTION,
    SELECTION_HINTS,
    _confirm_destructive,
    _draw_tool_panel,
    _icon,
    _selection,
    _selection_count,
    _selection_has_edge,
    _selection_has_external_sources,
    _selection_has_face,
    _selection_has_solid,
    _selection_ok_for,
    _surface_panel,
    _tip_with_hint,
    _curve_panel,
    command_info,
    selection_requirement,
    HealthResetCommand,
    PanelCommand,
    ProviderCommand,
    register,
    TOOLBAR_PRIMARY,
    TOOLBARS,
)
