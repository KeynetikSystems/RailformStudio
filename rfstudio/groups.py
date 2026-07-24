# -*- coding: utf-8 -*-
"""Tree-organization helpers.

Railform Studio object trees get unwieldy fast on a real job (tens of
curves/surfaces): every created object used to land as a flat sibling at
the document root, with no distinction between "raw input curve" and
"finished surface", and QA tools generated a fresh throwaway object every
time they were re-run. This module is the fix -- a small, Gui-light
layer (imports only FreeCAD, never Silk/Curves) used by parametric.py,
engines.py, and panel_import.py to:

  * file every created object into a category group
    (App::DocumentObjectGroup) instead of the document root,
  * hide/show a surface's consumed input curves as a single toggle
    (ShowInputs property, flipped from the panel), and
  * let QA snapshot commands (curvature comb, zebra, reflection lines,
    draft analysis) update an existing object in place instead of
    spawning a new one on every re-run.

Deliberately simple: one flat group per category, not a full per-body-
panel hierarchy -- the user is expected to drag objects into their own
sub-groups for real project structure (FreeCAD groups nest inside
groups). This only solves "everything is an undifferentiated pile at
the root", not project-specific organization, which only the user can
know.
"""

import FreeCAD

#: category -> group label. Kept in one place so every call site agrees
#: on the same tree layout.
CATEGORY_LABELS = {
    "Curves":   "Railform Curves",
    "Surfaces": "Railform Surfaces",
    "QA":       "Railform QA",
    "Imported": "Railform Imported Rails",
}


def ensure_group(doc, label):
    """Return the App::DocumentObjectGroup with this Label, creating it
    (at the document root) if it doesn't exist yet. Matches by Label
    (not Name) since Label is what the user sees and renames, and what a
    later call needs to find again."""
    for o in doc.Objects:
        if o.TypeId == "App::DocumentObjectGroup" and o.Label == label:
            return o
    grp = doc.addObject("App::DocumentObjectGroup", label.replace(" ", ""))
    grp.Label = label
    return grp


def add_to_group(obj, category):
    """File `obj` into its category's group. `category` is normally a key
    in CATEGORY_LABELS, but any literal group label works too, so a
    caller with a project-specific scheme (e.g. a per-body-panel name)
    can reuse this helper directly. Never raises -- grouping is a
    convenience, not something that should be able to abort object
    creation if it fails."""
    doc = getattr(obj, "Document", None)
    if doc is None:
        return
    label = CATEGORY_LABELS.get(category, category)
    try:
        grp = ensure_group(doc, label)
        grp.addObject(obj)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Railform: could not group %s under '%s' (%s)\n"
            % (getattr(obj, "Label", obj.Name), label, exc))


def set_inputs_visibility(link_objs, visible):
    """Show/hide a list of linked FreeCAD objects (the raw curves/
    surfaces a parametric object consumes). Best-effort per object -- a
    missing ViewObject (e.g. headless) must not abort the rest of the
    list."""
    for o in link_objs or []:
        vobj = getattr(o, "ViewObject", None)
        if vobj is None:
            continue
        try:
            vobj.Visibility = bool(visible)
        except Exception:
            pass


def apply_input_visibility(obj, link_props, show_attr="ShowInputs"):
    """Hide (or show) every object linked via the property names in
    `link_props` on `obj`, driven by obj's own ShowInputs bool property.

    Meant to be called at the end of a parametric FeaturePython's
    execute(), once the new surface has a valid Shape -- so raw input
    curves disappear from the tree/viewport the moment they've been
    consumed into a finished surface, with one property (exposed as a
    panel checkbox -- see panel_surface.py) to bring them back when the
    user actually wants to see/select them again.

    No-op if `obj` doesn't have `show_attr` yet (e.g. an older document
    not yet migrated to carry it) or if nothing is linked.
    """
    if not hasattr(obj, show_attr):
        return
    show = bool(getattr(obj, show_attr))
    links = []
    for prop in link_props:
        links.extend(getattr(obj, prop, None) or [])
    set_inputs_visibility(links, show)


def find_or_create(doc, label, type_id="Part::Feature"):
    """Find an existing object by Label (not Name -- Label is what a
    caller passing a human-meaningful, stable name actually wants
    matched), or create a new one of `type_id` with that Label.

    This is the fix for QA snapshot commands (curvature comb / zebra /
    reflection lines / draft analysis) that previously called
    `doc.addObject(...)` unconditionally on every run -- re-running QA
    during an iterative review used to leave a growing trail of stale
    `ZebraStripes014`-style objects behind. Callers pass a NAME DERIVED
    FROM THE SOURCE OBJECT (e.g. "<source label>_Zebra"), so re-running
    the same check on the same face updates the same object in place
    instead of creating a new one; running it on a *different* face
    still gets its own object, since the derived name differs.
    """
    for o in doc.Objects:
        if o.Label == label:
            return o, False
    obj = doc.addObject(type_id, label.replace(" ", "_"))
    obj.Label = label
    return obj, True
