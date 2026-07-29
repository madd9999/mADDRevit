# -*- coding: utf-8 -*-
"""Create Interior Elevations
Select one or more Rooms in a floor plan view, click this button, and the
tool automatically creates 4 interior elevation views (North, South, East,
West) for each selected room.

View name format:
    ROOM NAME - Interior Elevation - DIRECTION
    (falls back to "Room <number>" if the room has no name)
"""
from __future__ import print_function
import sys, math, traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ViewPlan, ViewType,
    ViewFamilyType, ViewFamily, ElevationMarker, XYZ, Transaction,
    BuiltInParameter, ElementTransformUtils, Line
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.DB.Architecture import Room
from pyrevit import forms, revit

uidoc = revit.uidoc
doc   = revit.doc

INVALID_NAME_CHARS = u'{}[]|;<>?`~\\/:'
CROP_MARGIN_FT      = 1.0     # breathing room around the room in the elevation

# Camera "looks" opposite to ViewDirection. We bucket the look direction
# into the nearest of the 4 cardinal directions.
def cardinal_from_view(v):
    look = v.ViewDirection.Negate()
    if abs(look.Y) >= abs(look.X):
        return u"North" if look.Y > 0 else u"South"
    return u"East" if look.X > 0 else u"West"

# Order we create elevations in, and the look-direction angle (radians,
# measured from +X, counter-clockwise) each one needs to end up at.
CARDINALS  = [u"North", u"East", u"South", u"West"]
LOOK_ANGLE = {u"East": 0.0, u"North": math.pi / 2.0,
              u"West": math.pi, u"South": 3.0 * math.pi / 2.0}


# ---------------------------------------------------------------- helpers
def sanitize(s):
    if not s:
        return u""
    out = u"".join(u"-" if ch in INVALID_NAME_CHARS else ch for ch in s)
    return out.strip()

def get_room_param_string(room, bip):
    try:
        p = room.get_Parameter(bip)
        if p:
            val = p.AsString()
            if val:
                return val.strip()
    except Exception:
        pass
    return None

def get_room_name(room):
    name = get_room_param_string(room, BuiltInParameter.ROOM_NAME)
    if not name:
        try:
            name = room.Name
        except Exception:
            name = None
    return sanitize(name) if name else None

def get_room_number(room):
    num = get_room_param_string(room, BuiltInParameter.ROOM_NUMBER)
    if not num:
        try:
            num = room.Number
        except Exception:
            num = None
    return sanitize(num) if num else None

def room_label(room):
    """Label used in the view name and in status messages."""
    name = get_room_name(room)
    if name:
        return name
    num = get_room_number(room)
    return u"Room {}".format(num) if num else u"Room {}".format(room.Id.IntegerValue)

def all_view_names():
    names = set()
    for v in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Views).WhereElementIsNotElementType().ToElements():
        try:
            if not v.IsTemplate:
                names.add(v.Name)
        except Exception:
            pass
    return names

def unique_view_name(base_name, taken):
    name = base_name
    i = 1
    while name in taken:
        name = u"{} ({})".format(base_name, i)
        i += 1
    taken.add(name)
    return name

def find_plan_for_room(room):
    """Prefer the active view if it's the plan the room lives on,
    otherwise find any floor/ceiling plan view on the room's level."""
    av = uidoc.ActiveView
    try:
        if isinstance(av, ViewPlan) and not av.IsTemplate and av.GenLevel \
           and av.GenLevel.Id == room.LevelId:
            return av
    except Exception:
        pass

    plans = FilteredElementCollector(doc).OfClass(ViewPlan).ToElements()
    for p in plans:
        try:
            if p.IsTemplate:
                continue
            if p.ViewType not in (ViewType.FloorPlan, ViewType.CeilingPlan):
                continue
            if p.GenLevel and p.GenLevel.Id == room.LevelId:
                return p
        except Exception:
            pass
    return None

def get_room_center(room, ref_view):
    try:
        loc = room.Location
        if loc and hasattr(loc, "Point") and loc.Point:
            return loc.Point
    except Exception:
        pass
    bb = room.get_BoundingBox(ref_view) if ref_view else room.get_BoundingBox(None)
    if not bb:
        return None
    mn, mx = bb.Min, bb.Max
    return XYZ((mn.X + mx.X) / 2.0, (mn.Y + mx.Y) / 2.0, mn.Z)

def best_elevation_type():
    """Pick an Elevation ViewFamilyType, preferring one named 'Interior Elevation'."""
    types = [vft for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType)
             if vft.ViewFamily == ViewFamily.Elevation]
    if not types:
        return None
    for vft in types:
        try:
            if "interior" in vft.Name.lower():
                return vft
        except Exception:
            pass
    for vft in types:
        try:
            if "interior" in (getattr(vft, "FamilyName", "") or "").lower():
                return vft
        except Exception:
            pass
    return types[0]

def crop_to_room(elev_view, room, margin=CROP_MARGIN_FT):
    try:
        bb = room.get_BoundingBox(None)
        if not bb:
            return
        mn, mx = bb.Min, bb.Max
        corners = [XYZ(x, y, z)
                   for x in (mn.X, mx.X)
                   for y in (mn.Y, mx.Y)
                   for z in (mn.Z, mx.Z)]
        cb  = elev_view.CropBox
        inv = cb.Transform.Inverse
        us, vs, ws = [], [], []
        for c in corners:
            p = inv.OfPoint(c)
            us.append(p.X); vs.append(p.Y); ws.append(p.Z)
        cb.Min = XYZ(min(us) - margin, min(vs) - margin, cb.Min.Z)
        cb.Max = XYZ(max(us) + margin, max(vs) + margin, cb.Max.Z)
        elev_view.CropBox = cb
        elev_view.CropBoxActive = True
        elev_view.CropBoxVisible = True

        depth = (max(ws) - min(ws)) + margin
        far_active = elev_view.get_Parameter(BuiltInParameter.VIEWER_BOUND_ACTIVE_FAR)
        if far_active:
            far_active.Set(1)
        far_offset = elev_view.get_Parameter(BuiltInParameter.VIEWER_BOUND_OFFSET_FAR)
        if far_offset:
            far_offset.Set(max(depth, 1.0))
    except Exception:
        pass

def is_room(el):
    try:
        if isinstance(el, Room):
            return el.Area > 0
    except Exception:
        pass
    return False


# ------------------------------------------------------------- selection
rooms = []
for eid in list(uidoc.Selection.GetElementIds()):
    el = doc.GetElement(eid)
    if is_room(el):
        rooms.append(el)

if not rooms:
    class RoomFilter(ISelectionFilter):
        def AllowElement(self, e):
            return is_room(e)
        def AllowReference(self, ref, pt):
            return False
    try:
        picked = uidoc.Selection.PickObjects(ObjectType.Element, RoomFilter(), "Select room(s), then click Finish")
        for r in picked:
            el = doc.GetElement(r.ElementId)
            if is_room(el):
                rooms.append(el)
    except Exception:
        rooms = []

if not rooms:
    forms.alert("Select one or more rooms first (either before clicking the "
                "button, or when prompted), then try again.",
                title="Interior Elevations", warn_icon=True)
    sys.exit(0)

elev_vft = best_elevation_type()
if not elev_vft:
    forms.alert("No Elevation view type was found in this project.\n"
                "Load/duplicate an Elevation view family type and try again.",
                title="Interior Elevations", warn_icon=True)
    sys.exit(0)


# ------------------------------------------------------------------ main
created  = []   # (room, view)
skipped  = []   # (room, reason)
taken_names = all_view_names()

t = Transaction(doc, "Create Interior Elevations")
t.Start()
try:
    for room in rooms:
        plan = find_plan_for_room(room)
        if not plan:
            skipped.append((room, "No floor/ceiling plan view found on the room's level."))
            continue

        center = get_room_center(room, plan)
        if center is None:
            skipped.append((room, "Could not determine the room's location."))
            continue

        scale = plan.Scale if getattr(plan, "Scale", None) else 100
        label = room_label(room)
        # vertical axis through the room center, used to spin each marker
        axis = Line.CreateBound(center, XYZ(center.X, center.Y, center.Z + 10.0))

        made_any = False
        for card in CARDINALS:
            try:
                marker = ElevationMarker.CreateElevationMarker(doc, elev_vft.Id, center, scale)
            except Exception as ex:
                skipped.append((room, u"{}: could not place marker ({})".format(card, ex)))
                continue
            if not marker:
                skipped.append((room, u"{}: could not place marker.".format(card)))
                continue

            try:
                view = marker.CreateElevation(doc, plan.Id, 0)
            except Exception as ex:
                view = None
            if not view:
                skipped.append((room, u"{}: could not create elevation view.".format(card)))
                try: doc.Delete(marker.Id)
                except Exception: pass
                continue

            doc.Regenerate()

            # Rotate the marker (and its view) about the vertical axis until
            # it faces the direction we want.
            cur = cardinal_from_view(view)
            if cur != card:
                ang = LOOK_ANGLE[card] - LOOK_ANGLE[cur]
                try:
                    ElementTransformUtils.RotateElement(doc, marker.Id, axis, ang)
                    doc.Regenerate()
                except Exception:
                    pass

            # Use the direction the view actually ends up facing as the
            # truthful label, in case rotation didn't land exactly.
            direction = cardinal_from_view(view)
            base_name = u"{} - Interior Elevation - {}".format(label, direction)
            try:
                view.Name = unique_view_name(base_name, taken_names)
            except Exception:
                pass
            crop_to_room(view, room)

            created.append((room, view))
            made_any = True

        if not made_any:
            skipped.append((room, "No elevation views could be generated for this room."))

    t.Commit()
except Exception as e:
    t.RollBack()
    forms.alert(u"Error creating interior elevations:\n{}\n\n{}".format(e, traceback.format_exc()),
                title="Interior Elevations", warn_icon=True)
    sys.exit(0)


# --------------------------------------------------------------- report
lines = [u"Created {} interior elevation view(s).".format(len(created))]
by_room = {}
for r, v in created:
    by_room.setdefault(r, []).append(v.Name)
for r, names in by_room.items():
    lines.append(u"- {}: {}".format(room_label(r), u", ".join(names)))

if skipped:
    lines.append(u"\nSkipped:")
    for r, why in skipped:
        lines.append(u"- {}: {}".format(room_label(r), why))

forms.alert(u"\n".join(lines), title="Interior Elevations", warn_icon=False)
