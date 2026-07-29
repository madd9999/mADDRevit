# -*- coding: utf-8 -*-
"""Legend -> Drafting View
Pick an existing Legend from a list. The tool creates a new, correctly
named and scaled Drafting View for you, then switches the active view
back to the source Legend so you can bring the content across yourself.

WHY NOT FULLY AUTOMATIC: Legend view content (even plain text, lines, and
filled regions placed inside a Legend) is not reliably readable through
the Revit API's normal element-enumeration methods on every Revit
version - this has been confirmed by testing on this project (both a
view-scoped collector and an OwnerViewId scan came back empty for a
Legend that visibly has content). Rather than silently produce an empty
view, this tool sets everything up and hands you the one manual step
that's guaranteed to work: Revit's own copy/paste.

The original Legend is left completely unchanged.
"""
from __future__ import print_function
import sys, traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, ViewFamilyType, ViewFamily,
    ViewDrafting, Transaction
)
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

def get_view_family_type(view_family):
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if vft.ViewFamily == view_family:
                return vft
        except Exception:
            pass
    return None


# ---------------------------------------------------------- pick source
legend_views = []
for v in FilteredElementCollector(doc).OfClass(View).ToElements():
    try:
        if not v.IsTemplate and v.ViewType == ViewType.Legend:
            legend_views.append(v)
    except Exception:
        pass

if not legend_views:
    forms.alert("No Legend views were found in this project.",
                title="Legend to Drafting View", warn_icon=True)
    sys.exit(0)

name_to_view = {}
for v in sorted(legend_views, key=lambda vv: vv.Name.lower()):
    label = v.Name
    if label in name_to_view:
        label = u"{} (id {})".format(v.Name, v.Id.IntegerValue)
    name_to_view[label] = v

picked = forms.SelectFromList.show(
    sorted(name_to_view.keys()),
    title="Select a Legend to convert to a Drafting View",
    button_name="Create Drafting View")
if not picked:
    sys.exit(0)

source_view = name_to_view[picked]

drafting_vft = get_view_family_type(ViewFamily.Drafting)
if not drafting_vft:
    forms.alert("This project doesn't have a Drafting view type available.",
                title="Legend to Drafting View", warn_icon=True)
    sys.exit(0)


# ---------------------------------------------------------------- build
taken_names = all_view_names()
new_name = unique_name(source_view.Name, taken_names)

t = Transaction(doc, "Create Drafting View for Legend Conversion")
t.Start()
try:
    new_view = ViewDrafting.Create(doc, drafting_vft.Id)
    try:
        scale = source_view.Scale if getattr(source_view, "Scale", None) else 100
        new_view.Scale = scale
    except Exception:
        pass
    try:
        new_view.Name = new_name
    except Exception:
        pass
    t.Commit()
except Exception as e:
    t.RollBack()
    forms.alert(u"Error creating Drafting View:\n{}\n\n{}".format(e, traceback.format_exc()),
                title="Legend to Drafting View", warn_icon=True)
    sys.exit(0)

# jump back to the source Legend so the user can immediately Select All + Copy
try:
    uidoc.ActiveView = source_view
except Exception:
    pass


# --------------------------------------------------------------- report
msg = (u"Created Drafting View \"{new_name}\" (matching the scale of "
       u"\"{src_name}\").\n\n"
       u"You're now looking at the Legend \"{src_name}\". To bring its "
       u"content across:\n\n"
       u"1. Press Ctrl+A to select everything in this view.\n"
       u"2. Press Ctrl+C to copy.\n"
       u"3. Open \"{new_name}\" from the Project Browser.\n"
       u"4. Paste > Aligned to Same Place (Ctrl+Alt+V).\n\n"
       u"The original Legend has not been touched.").format(
           new_name=new_view.Name, src_name=source_view.Name)
forms.alert(msg, title="Legend to Drafting View", warn_icon=False)

