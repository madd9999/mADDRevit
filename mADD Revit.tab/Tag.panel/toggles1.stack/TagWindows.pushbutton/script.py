# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script
import sys

doc = revit.doc
view = doc.ActiveView
logger = script.get_logger()

# Guardrails
if isinstance(view, DB.ViewSchedule) or view.ViewType in [DB.ViewType.Legend, DB.ViewType.Rendering, DB.ViewType.DraftingView, DB.ViewType.ProjectBrowser]:
    forms.alert("Open a model view (plan/section/elevation/3D).", title="Tag Windows", exitscript=True)

# Build visibility filter if available
try:
    vis_filter = DB.VisibleInViewFilter(doc, view.Id)
    col_all = DB.FilteredElementCollector(doc).WherePasses(vis_filter)
except:
    vis_filter = None
    col_all = DB.FilteredElementCollector(doc, view.Id)

# Target categories
cats = [DB.BuiltInCategory.OST_Windows]

# Collect target elements
targets = []
for bic in cats:
    try:
        col = DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType()
        if vis_filter:
            col = col.WherePasses(vis_filter)
    except:
        col = DB.FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType()
    for e in col:
        targets.append(e)

if not targets:
    forms.toast("No target elements visible.", title="Tag Windows", appid='TagSuite')
    sys.exit()

# Collect already-tagged element ids in this view
tagged_ids = set()
for tag in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.IndependentTag):
    # Try APIs
    try:
        ids = tag.GetTaggedLocalElementIds()
        if ids:
            for _id in ids:
                if _id and _id.IntegerValue > 0:
                    tagged_ids.add(_id.IntegerValue)
            continue
    except:
        pass
    try:
        ids2 = tag.GetTaggedElementIds()
        if ids2:
            for _ref in ids2:
                try:
                    lid = getattr(_ref, "HostElementId", None)
                    if lid and lid.IntegerValue > 0:
                        tagged_ids.add(lid.IntegerValue)
                    else:
                        eid = getattr(_ref, "ElementId", None)
                        if eid and eid.IntegerValue > 0:
                            tagged_ids.add(eid.IntegerValue)
                except:
                    pass
            continue
    except:
        pass
    try:
        tid = tag.TaggedLocalElementId
        if tid and tid.IntegerValue > 0:
            tagged_ids.add(tid.IntegerValue)
    except:
        pass

def _tag_point(el):
    try:
        loc = el.Location
        if isinstance(loc, DB.LocationPoint):
            return loc.Point
        if isinstance(loc, DB.LocationCurve):
            return loc.Curve.Evaluate(0.5, True)
    except:
        pass
    try:
        bb = el.get_BoundingBox(view)
        if bb:
            return DB.XYZ((bb.Min.X+bb.Max.X)*0.5, (bb.Min.Y+bb.Max.Y)*0.5, (bb.Min.Z+bb.Max.Z)*0.5)
    except:
        pass
    return DB.XYZ(0,0,0)

def _orientation(v):
    try:
        if v.ViewType in [DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.EngineeringPlan, DB.ViewType.AreaPlan]:
            return DB.TagOrientation.Horizontal
    except:
        pass
    return DB.TagOrientation.Horizontal

t = DB.Transaction(doc, "Tag Windows")
t.Start()
created = 0
skipped = 0
errors = 0
for el in targets:
    try:
        if el.Id.IntegerValue in tagged_ids:
            skipped += 1
            continue
        ref = DB.Reference(el)
        head = _tag_point(el)
        DB.IndependentTag.Create(doc, view.Id, ref, False, DB.TagMode.TM_ADDBY_CATEGORY, _orientation(view), head)
        created += 1
    except Exception as ex:
        logger.debug("Tag fail {}: {}".format(el.Id.IntegerValue, ex))
        errors += 1
t.Commit()

forms.toast("Tagged: {} | Skipped: {} | Errors: {}".format(created, skipped, errors), title="Tag Windows", appid='TagSuite')
