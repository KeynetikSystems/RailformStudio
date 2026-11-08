# -*- coding: utf-8 -*-
"""Command registration and query helpers."""

import os
from typing import Any

import FreeCADGui

from rfstudio.commands.metadata import (_DELEGATE_META, _PANEL_COMMANDS, _icon)
from rfstudio.commands.provider import (HealthResetCommand, PanelCommand,
                                         ProviderCommand)


def command_info(cmd_id: str) -> tuple[str, str, str]:
    """(menu_text, icon_path, tooltip) for a curated command -- used by the
    docked tool shelf to build its buttons."""
    if cmd_id in _PANEL_COMMANDS:
        _, menu, tip, icon = _PANEL_COMMANDS[cmd_id]
        return menu, _icon(icon), tip
    if cmd_id in _DELEGATE_META:
        menu, tip, icon = _DELEGATE_META[cmd_id]
        return menu, _icon(icon), tip
    return cmd_id, "", ""


def register() -> None:
    """Instantiate + addCommand every curated command. Called from Initialize."""
    for cmd_id, (panel, menu, tip, icon) in _PANEL_COMMANDS.items():
        FreeCADGui.addCommand(cmd_id, PanelCommand(cmd_id, panel, menu, tip, _icon(icon)))
    for cmd_id, (menu, tip, icon) in _DELEGATE_META.items():
        if cmd_id == "RFS_ResetHealth":
            continue   # registered below as a plain command, not a provider
        FreeCADGui.addCommand(cmd_id, ProviderCommand(cmd_id, menu, tip, _icon(icon)))
    FreeCADGui.addCommand("RFS_ResetHealth", HealthResetCommand())