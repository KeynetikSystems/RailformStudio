# -*- coding: utf-8 -*-
"""Engine dependency detection + lazy registration.

Railform Studio is a DEPEND-AND-WRAP workbench: it does not contain Curves' or
Silk's geometry, it drives them. So at startup we (a) detect whether each engine
is installed, and (b) provide a way to force an engine's commands to register,
because FreeCAD only runs a workbench's Initialize() (which calls addCommand)
the first time that workbench is activated -- so a freshly-started session won't
have Curves'/Silk's command ids registered until the user visits them once.
"""

import os
import FreeCAD

# Mod folder name -> (workbench registration name used by activateWorkbench)
ENGINES = {
    "Curves": "CurvesWorkbench",
    "Silk": "Silk",
}


def _mod_roots():
    roots = []
    for getter in ("getUserAppDataDir", "getResourceDir", "getHomePath"):
        try:
            d = getattr(FreeCAD, getter)()
        except Exception:
            d = None
        if d:
            roots.append(os.path.join(d, "Mod"))
    return roots


def installed(mod_name):
    """True if a Mod/<mod_name> folder exists in any FreeCAD mod root.
    Gui-free, so it works under freecadcmd too."""
    return any(os.path.isdir(os.path.join(r, mod_name)) for r in _mod_roots())


def status():
    """{'Curves': bool, 'Silk': bool} -- which engines are present."""
    return {name: installed(name) for name in ENGINES}


# --- lazy command registration (GUI only) --------------------------------- #
_loaded = set()


def ensure_engine_commands(mod_name):
    """Force <mod_name>'s commands to register by bouncing its workbench once.

    Activating a workbench runs its Initialize() (-> addCommand). We flip to the
    engine WB then back to ours. Guarded so it happens at most once per engine
    per session. GUI-only; no-op if the engine isn't installed.
    """
    if mod_name in _loaded or not installed(mod_name):
        return
    import FreeCADGui
    wb_name = ENGINES.get(mod_name)
    try:
        current = FreeCADGui.activeWorkbench().__class__.__name__
        FreeCADGui.activateWorkbench(wb_name)
        FreeCADGui.activateWorkbench(current)
        _loaded.add(mod_name)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Railform Studio: could not pre-load %s commands (%s)\n"
            % (mod_name, exc))


def command_available(cmd_id):
    """True if a command id is currently registered with the Gui."""
    import FreeCADGui
    return cmd_id in FreeCADGui.listCommands()


def verify_delegations(delegations):
    """Check every curated delegation up front and return the broken ones.

    Without this, a renamed/removed upstream command (e.g. Curves dropping
    "Curves_BlendSurf2") only surfaces the first time a user happens to
    click that specific curated button -- a single PrintError, easy to
    miss, with no indication anything else might also be broken.

    Here we force-load every required engine's commands once (the same
    bounce `ensure_engine_commands` already does, deduped per engine) and
    check every (cmd_id -> target) pair from `delegations`
    (engines.DELEGATIONS) in one pass. Called from workbench.Initialize so
    the *entire* curated surface is validated at workbench-activation time,
    not lazily per click. This is the closest thing to a build-time check
    available in a pure-Python FreeCAD addon: it can't run at import time
    because the engine commands aren't registered until a workbench has
    been activated at least once, but it can run once, eagerly, instead of
    silently failing tool-by-tool over a session.

    Returns a list of (cmd_id, mod_name, target_cmd_id) tuples for every
    delegation whose target command is missing after its engine's commands
    were loaded. Delegations whose engine isn't installed at all are
    skipped (that's already reported separately by `status()`).
    """
    broken = []
    mods_needed = {mod for mod, _target in delegations.values()
                   if installed(mod)}
    for mod in mods_needed:
        ensure_engine_commands(mod)
    for cmd_id, (mod, target) in delegations.items():
        if not installed(mod):
            continue
        if not command_available(target):
            broken.append((cmd_id, mod, target))
    if broken:
        lines = ", ".join("%s -> %s::%s" % (c, m, t) for c, m, t in broken)
        FreeCAD.Console.PrintWarning(
            "Railform Studio: %d curated command(s) point at missing "
            "engine commands -- their buttons will report an error when "
            "clicked: %s\n" % (len(broken), lines))
    return broken


def report_single_points_of_failure(providers):
    """Curated actions whose ENTIRE provider chain (engines.PROVIDERS) is
    engine-only, with no native fallback -- i.e. if Curves or Silk drops
    or renames that command, the button is permanently dead until someone
    writes a native replacement, not just until the addon is reinstalled.

    ProviderCommand (commands.py) already survives a single broken
    delegation by falling through its chain; this is the visibility
    layer one level up -- which curated actions HAVE no chain to fall
    through at all. Without this, that fact only lived in engines.py's
    NATIVE_OVERRIDES triage comments, invisible unless someone went
    reading source. Called once at workbench Initialize, mirroring
    verify_delegations' "check the whole curated surface up front, not
    lazily per click" approach.

    Returns the list of affected command ids.
    """
    single_point = sorted(
        cmd_id for cmd_id, chain in providers.items()
        if not any(p[0] == "native" for p in chain))
    if single_point:
        FreeCAD.Console.PrintMessage(
            "Railform Studio: %d curated command(s) rely ENTIRELY on an "
            "installed engine (Curves/Silk) with no native fallback -- "
            "if that engine drops or renames the command, these stay "
            "broken until a native replacement is written: %s\n"
            % (len(single_point), ", ".join(single_point)))
    return single_point
