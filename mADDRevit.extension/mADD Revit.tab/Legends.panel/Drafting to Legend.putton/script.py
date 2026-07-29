# -*- coding: utf-8 -*-
"""Drafting View -> Legend
Pick an existing Drafting View from a list; the tool creates a new Legend
view and copies every view-specific element (text, lines, filled regions,
detail components, dimensions, etc.) from the Drafting View into it.

IMPORTANT REVIT API LIMITATION: there is no API to create a Legend view
from nothing. The only supported way is to duplicate an EXISTING Legend
view. So this tool needs at least one Legend view (can be blank) already
in the project to duplicate from - if the project has none, create one
manually first (View tab > Legends > Legend), then run this again.

The original Drafting View is left completely unchanged.
"""
from __future__ import print_function
import sys, traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, ViewDuplicateOption,
    ElementTransformUtils, Transaction, ElementId, Transform, CopyPasteOptions
)
from System.Collections.Generic import List
from pyrevit import forms, revit

uidoc = revit.uidoc
doc   = revit.doc


# ---------------------------------------------------------------- helpers
def all_view_names():
    names = set()
    for v in FilteredElementCollector(doc).OfClass(View).ToElements():
        try:
            if not v.IsTemplate:
                names.add(v.Name)
        except Exception:
            pass
    return names

def unique_name(base_name, taken):
    name = base_name
    i = 1
    while name in taken:
        name = u"{} ({})".format(base_name, i)
        i += 1
    taken.add(name)
    return name

def owned_element_ids(view_id):
    """Every view-specific element that belongs to this view. Uses two
    strategies and merges the results, since a view-scoped
    FilteredElementCollector doesn't reliably enumerate every view type
    (Legends in particular) on every Revit version."""
    ids = set()
    try:
        for eid in (FilteredElementCollector(doc, view_id)
                    .WhereElementIsNotElementType().ToElementIds()):
            ids.add(eid)
    except Exception:
        pass
    for el in FilteredElementCollector(doc).WhereElementIsNotElementType():
        try:
            if el.OwnerViewId.IntegerValue == view_id.IntegerValue:
                ids.add(el.Id)
        except Exception:
            pass
    return list(ids)

def find_seed_legend():
    for v in FilteredElementCollector(doc).OfClass(View).ToElements():
        try:
            if not v.IsTemplate and v.ViewType == ViewType.Legend:
                return v
        except Exception:
            pass
    return None


# ---------------------------------------------------------- pick source
drafting_views = []
for v in FilteredElementCollector(doc).OfClass(View).ToElements():
    try:
        if not v.IsTemplate and v.ViewType == ViewType.DraftingView:
            drafting_views.append(v)
    except Exception:
        pass

if not drafting_views:
    forms.alert("No Drafting Views were found in this project.",
                title="Drafting View to Legend", warn_icon=True)
    sys.exit(0)

name_to_view = {}
for v in sorted(drafting_views, key=lambda vv: vv.Name.lower()):
    label = v.Name
    if label in name_to_view:
        label = u"{} (id {})".format(v.Name, v.Id.IntegerValue)
    name_to_view[label] = v

picked = forms.SelectFromList.show(
    sorted(name_to_view.keys()),
    title="Select a Drafting View to convert to a Legend",
    button_name="Create Legend")
if not picked:
    sys.exit(0)

source_view = name_to_view[picked]

seed_legend = find_seed_legend()
if not seed_legend:
    forms.alert("This project has no Legend view to duplicate from.\n\n"
                "Revit's API can only create a new Legend by duplicating an "
                "existing one - it can't build one from nothing. Create a "
                "blank Legend first (View tab > Create > Legends > Legend), "
                "then run this tool again.",
                title="Drafting View to Legend", warn_icon=True)
    sys.exit(0)


# ---------------------------------------------------------------- build
taken_names = all_view_names()
new_name = unique_name(source_view.Name, taken_names)
source_element_ids = owned_element_ids(source_view.Id)

t = Transaction(doc, "Convert Drafting View to Legend")
t.Start()
try:
    new_view_id = seed_legend.Duplicate(ViewDuplicateOption.Duplicate)
    new_view = doc.GetElement(new_view_id)

    # the duplicate starts out with a copy of the seed legend's own
    # content - clear that out so we start from a blank legend
    stale_ids = owned_element_ids(new_view.Id)
    if stale_ids:
        doc.Delete(List[ElementId](stale_ids))

    try:
        scale = source_view.Scale if getattr(source_view, "Scale", None) else 100
        new_view.Scale = scale
    except Exception:
        pass
    try:
        new_view.Name = new_name
    except Exception:
        pass

    copied, failed, last_error = 0, 0, None
    if source_element_ids:
        try:
            ElementTransformUtils.CopyElements(
                source_view, List[ElementId](source_element_ids), new_view,
                Transform.Identity, CopyPasteOptions())
            copied = len(source_element_ids)
        except Exception as ex:
            last_error = str(ex)
            # bulk copy failed (often one bad element) - retry one at a
            # time so a single unsupported element doesn't block the rest
            for eid in source_element_ids:
                try:
                    ElementTransformUtils.CopyElements(
                        source_view, List[ElementId]([eid]), new_view,
                        Transform.Identity, CopyPasteOptions())
                    copied += 1
                except Exception as ex2:
                    failed += 1
                    last_error = str(ex2)

    doc.Regenerate()
    t.Commit()
except Exception as e:
    t.RollBack()
    forms.alert(u"Error creating Legend view:\n{}\n\n{}".format(e, traceback.format_exc()),
                title="Drafting View to Legend", warn_icon=True)
    sys.exit(0)


# --------------------------------------------------------------- report
msg = u"Created Legend view \"{}\".\nCopied {} of {} element(s) from \"{}\".".format(
    new_view.Name, copied, len(source_element_ids), source_view.Name)
if not source_element_ids:
    msg += u"\n\nNo view-specific elements were found in the source view."
if failed:
    msg += (u"\n\n{} element(s) could not be copied - some element types "
            u"(e.g. certain detail components) are not supported inside "
            u"Legend views.").format(failed)
if last_error:
    msg += u"\n\nLast error detail: {}".format(last_error)
msg += u"\n\nThe original Drafting View was left unchanged."
forms.alert(msg, title="Drafting View to Legend", warn_icon=False)
