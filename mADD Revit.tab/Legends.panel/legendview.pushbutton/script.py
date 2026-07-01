# -*- coding: utf-8 -*-
"""List every legend in the model and which sheet(s) it is placed on.
Legend names and sheet names are clickable - click to jump to them in Revit."""

from pyrevit import revit, DB, script
from collections import defaultdict

doc = revit.doc
output = script.get_output()
output.set_title("Legends on Sheets")

# all legends (exclude templates)
legends = [v for v in DB.FilteredElementCollector(doc).OfClass(DB.View)
           if v.ViewType == DB.ViewType.Legend and not v.IsTemplate]

# map: legend id -> list of sheets it is placed on
placed = defaultdict(list)
for sh in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
    try:
        if sh.IsPlaceholder:
            continue
    except Exception:
        pass
    try:
        vp_ids = sh.GetAllViewports()
    except Exception:
        vp_ids = []
    for vpid in vp_ids:
        vp = doc.GetElement(vpid)
        if vp is None:
            continue
        v = doc.GetElement(vp.ViewId)
        if v is not None and v.ViewType == DB.ViewType.Legend:
            placed[v.Id.IntegerValue].append(sh)

if not legends:
    output.print_md("**No legends found in this model.**")
    script.exit()

rows = []
unplaced = 0
for lg in sorted(legends, key=lambda v: v.Name.lower()):
    sheets_for = placed.get(lg.Id.IntegerValue, [])
    if sheets_for:
        links = []
        for sh in sorted(sheets_for, key=lambda s: s.SheetNumber):
            label = u"{0} - {1}".format(sh.SheetNumber, sh.Name)
            links.append(output.linkify(sh.Id, label))
        sheet_cell = u" , ".join(links)
        count = len(sheets_for)
    else:
        sheet_cell = u"— not placed —"
        count = 0
        unplaced += 1
    rows.append([output.linkify(lg.Id, lg.Name), count, sheet_cell])

output.print_table(
    rows,
    title="Legends and the sheets they are placed on",
    columns=["Legend", "# Sheets", "Placed on Sheets"])

output.print_md(
    "**{0}** legends total — **{1}** placed, **{2}** not placed. "
    "Click any legend or sheet to jump to it.".format(
        len(legends), len(legends) - unplaced, unplaced))
