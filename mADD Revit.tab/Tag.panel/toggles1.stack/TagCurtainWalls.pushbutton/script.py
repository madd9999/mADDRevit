# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script
import sys

doc = revit.doc
view = doc.ActiveView
logger = script.get_logger()

# Guardrails
if isinstance(view, DB.ViewSchedule) or view.ViewType in [DB.ViewType.Legend, DB.ViewType.Rendering, DB.ViewType.DraftingView, DB.ViewType.ProjectBrowser]:
    forms.alert("Open a model view (plan/section/elevation/3D).", title="Tag Curtain Walls", exitscript=True)

# Visible in view collector helper
def _collect_visible_of_categories(doc, view, cats):
    try:
        vis = DB.VisibleInViewFilter(doc, view.Id)
        col = DB.FilteredElementCollector(doc).WherePasses(vis)
        outs = []
        for bic in cats:
            try:
                c = DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType()
                c = c.WherePasses(vis)
            except:
                c = DB.FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType()
            for e in c:
                outs.append(e)
        return outs
    except:
        outs = []
        for bic in cats:
            c = DB.FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType()
            for e in c:
                outs.append(e)
        return outs

def _already_tagged_ids(doc, view):
    tagged_ids = set()
    for tag in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.IndependentTag):
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
    return tagged_ids

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

def _create_tags(elements, title):
    if not elements:
        forms.toast("No target elements visible.", title=title, appid='TagSuite')
        sys.exit()
    tagged_ids = _already_tagged_ids(doc, view)
    t = DB.Transaction(doc, title)
    t.Start()
    created = 0
    skipped = 0
    errors = 0
    for el in elements:
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
    forms.toast("Tagged: {} | Skipped: {} | Errors: {}".format(created, skipped, errors), title=title, appid='TagSuite')

# Curtain Walls only: WallType.Kind == Curtain
all_walls = _collect_visible_of_categories(doc, view, [DB.BuiltInCategory.OST_Walls])
curtain_only = []
for w in all_walls:
    try:
        wt = doc.GetElement(w.GetTypeId())
        if isinstance(wt, DB.WallType) and getattr(wt, "Kind", None) == DB.WallKind.Curtain:
            curtain_only.append(w)
    except:
        pass
_create_tags(curtain_only, "Tag Curtain Walls")
