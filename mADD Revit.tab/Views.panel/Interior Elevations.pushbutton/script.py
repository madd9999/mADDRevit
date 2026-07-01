# -*- coding: utf-8 -*-
from __future__ import print_function
import sys, math, traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, ViewPlan, View,
    ViewFamilyType, ViewFamily, ElevationMarker, XYZ, Transaction,
    BuiltInParameter, ElementTransformUtils, Line
)
from Autodesk.Revit.UI import Selection
from Autodesk.Revit.DB.Architecture import Room
from pyrevit import forms, revit

uidoc = revit.uidoc
doc   = revit.doc

INVALID_NAME_CHARS = u'<>:"/\\|?*'
CROP_MARGIN_FT = 0.75   # breathing room around the room in the elevation (feet)

# ----------------------------------------------------------------------
#  NAMING CONVENTION
#  A view is named by the direction the camera LOOKS (the wall you face).
#  Camera looking +Y -> "N"  |  -Y -> "S"  |  +X -> "E"  |  -X -> "W"
#  To use the opposite convention, swap the letters in cardinal_from_view().
# ----------------------------------------------------------------------

SIDE_OPTIONS  = ["North", "South", "East", "West"]
SIDE_TO_CARD  = {"North": u"N", "South": u"S", "East": u"E", "West": u"W"}
# look-direction angle (radians) measured from +X, CCW
LOOK_ANGLE    = {u"E": 0.0, u"N": math.pi/2.0, u"W": math.pi, u"S": 3.0*math.pi/2.0}


def sanitize(s):
    if not s:
        return u"Unnamed"
    return (u"".join(u"-" if ch in INVALID_NAME_CHARS else ch for ch in s)).strip()

def get_room_param(room, bip):
    try:
        p = room.get_Parameter(bip)
        if p:
            return p.AsString()
    except: pass
    return None

def get_room_name(room):
    name = get_room_param(room, BuiltInParameter.ROOM_NAME)
    if not name:
        try: name = room.Name
        except: name = None
    return sanitize(name)

def get_room_number(room):
    num = get_room_param(room, BuiltInParameter.ROOM_NUMBER)
    if not num:
        try: num = room.Number
        except: num = None
    return sanitize(num)

def view_name_exists(name):
    for v in FilteredElementCollector(doc).OfClass(View).ToElements():
        try:
            if not v.IsTemplate and v.Name == name:
                return True
        except: pass
    return False

def unique_view_name(base_name):
    name = base_name
    i = 1
    while view_name_exists(name):
        name = u"{}-{}".format(base_name, i)
        i += 1
    return name

def find_plan_on_level(level_id):
    plans = FilteredElementCollector(doc).OfClass(ViewPlan).ToElements()
    for p in plans:
        try:
            if p and not p.IsTemplate and p.ViewType == p.ViewType.FloorPlan \
               and p.GenLevel and p.GenLevel.Id == level_id:
                return p
        except: pass
    for p in plans:
        try:
            if p and not p.IsTemplate and p.ViewType == p.ViewType.FloorPlan:
                return p
        except: pass
    return None

def elevation_vfts():
    """All elevation view types, interior-named ones first."""
    out = []
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if vft.ViewFamily == ViewFamily.Elevation:
                out.append(vft)
        except: pass
    def key(v):
        n = ((getattr(v, 'FamilyName', '') or '') + ' ' + (getattr(v, 'Name', '') or '')).lower()
        return 0 if 'interior' in n else 1
    out.sort(key=key)
    return out

def get_room_center(room, ref_view=None):
    try:
        if room.Location and hasattr(room.Location, 'Point') and room.Location.Point:
            return room.Location.Point
    except: pass
    if ref_view is None:
        ref_view = find_plan_on_level(room.LevelId)
    bb = room.get_BoundingBox(ref_view) if ref_view else room.get_BoundingBox(None)
    if not bb:
        return None
    mn, mx = bb.Min, bb.Max
    return XYZ((mn.X+mx.X)/2.0, (mn.Y+mx.Y)/2.0, mn.Z)

def ask_template_name():
    tmpl = forms.ask_for_string(prompt="View template name (blank to skip):",
                                default="Interior Elevation")
    return "" if tmpl is None else tmpl.strip()

def find_template_view_by_name(name):
    if not name:
        return None
    for v in FilteredElementCollector(doc).OfClass(View).ToElements():
        try:
            if v.IsTemplate and v.Name.strip().lower() == name.strip().lower():
                return v
        except: pass
    return None

def pick_sides():
    chosen = forms.SelectFromList.show(
        SIDE_OPTIONS, title="Which elevations to create?",
        multiselect=True, button_name="Create elevations")
    if not chosen:
        return None
    return set(SIDE_TO_CARD[c] for c in chosen)

def cardinal_from_view(v):
    vd = v.ViewDirection            # points toward the viewer
    lx, ly = -vd.X, -vd.Y           # look direction
    if abs(ly) >= abs(lx):
        return u"N" if ly > 0 else u"S"
    return u"E" if lx > 0 else u"W"

def crop_and_clip(v, room, margin=CROP_MARGIN_FT):
    try:
        bb = room.get_BoundingBox(None)
        if not bb:
            return
        mn, mx = bb.Min, bb.Max
        corners = [XYZ(x, y, z)
                   for x in (mn.X, mx.X) for y in (mn.Y, mx.Y) for z in (mn.Z, mx.Z)]
        cb  = v.CropBox
        inv = cb.Transform.Inverse
        us, vs, ws = [], [], []
        for c in corners:
            p = inv.OfPoint(c)
            us.append(p.X); vs.append(p.Y); ws.append(p.Z)
        cb.Min = XYZ(min(us) - margin, min(vs) - margin, cb.Min.Z)
        cb.Max = XYZ(max(us) + margin, max(vs) + margin, cb.Max.Z)
        v.CropBox = cb
        v.CropBoxActive = True
        v.CropBoxVisible = True
        depth = (max(ws) - min(ws)) + margin
        try:
            pa = v.get_Parameter(BuiltInParameter.VIEWER_BOUND_ACTIVE_FAR)
            if pa: pa.Set(1)
            pf = v.get_Parameter(BuiltInParameter.VIEWER_BOUND_OFFSET_FAR)
            if pf: pf.Set(depth)
        except: pass
    except: pass

def finalize_view(v, room, template_view, roomnum, roomname, card):
    try:
        v.Name = unique_view_name(u"{}-{}-{}".format(roomnum, roomname, card))
    except: pass
    if template_view:
        try: v.ViewTemplateId = template_view.Id
        except: pass
    crop_and_clip(v, room)

# ---------- capability probe: how many sides does a type's marker support? ----------
def probe_capacity(vft, center, scale, plan_id):
    try:
        m = ElevationMarker.CreateElevationMarker(doc, vft.Id, center, scale)
    except:
        return 0
    if not m:
        return 0
    cnt = 0
    for i in range(4):
        try:
            v = m.CreateElevation(doc, plan_id, i)
            if v:
                cnt += 1
                doc.Regenerate()
        except:
            pass
    try: doc.Delete(m.Id)          # deleting the marker removes its probe views too
    except: pass
    return cnt

def choose_vft(center, scale, plan_id):
    """Return (vft, capacity). Prefers a 4-slot (interior) type."""
    best = None
    for vft in elevation_vfts():
        cap = probe_capacity(vft, center, scale, plan_id)
        if best is None or cap > best[1]:
            best = (vft, cap)
        if cap >= 4:
            break
    return best if best else (None, 0)

# ---------- creation ----------
def make_multi(room, plan, center, vft, template_view, roomnum, roomname, scale, wanted):
    """4-slot marker: create all sides, keep the wanted ones."""
    created = []
    marker = ElevationMarker.CreateElevationMarker(doc, vft.Id, center, scale)
    if not marker:
        return created
    raw = []
    for i in range(4):
        try:
            v = marker.CreateElevation(doc, plan.Id, i)
        except:
            v = None
        if v:
            raw.append(v)
            doc.Regenerate()
    for v in raw:
        card = cardinal_from_view(v)
        if card not in wanted:
            try: doc.Delete(v.Id)
            except: pass
            continue
        finalize_view(v, room, template_view, roomnum, roomname, card)
        created.append(v)
    if not created:
        try: doc.Delete(marker.Id)
        except: pass
    return created

def make_single(room, plan, center, vft, template_view, roomnum, roomname, scale, wanted):
    """1-slot type fallback: one marker per side, rotated to face it."""
    created = []
    axis = Line.CreateBound(center, XYZ(center.X, center.Y, center.Z + 10.0))
    for card in sorted(wanted):
        try:
            marker = ElevationMarker.CreateElevationMarker(doc, vft.Id, center, scale)
        except:
            marker = None
        if not marker:
            continue
        try:
            v = marker.CreateElevation(doc, plan.Id, 0)
        except:
            v = None
        if not v:
            try: doc.Delete(marker.Id)
            except: pass
            continue
        cur = cardinal_from_view(v)
        if cur != card:
            ang = LOOK_ANGLE[card] - LOOK_ANGLE[cur]
            try:
                ElementTransformUtils.RotateElement(doc, marker.Id, axis, ang)
                doc.Regenerate()
            except: pass
        actual = cardinal_from_view(v)      # truthful label even if rotation failed
        finalize_view(v, room, template_view, roomnum, roomname, actual)
        created.append(v)
    return created

def is_room(el):
    try:
        if isinstance(el, Room):
            return True
    except: pass
    try:
        return el.Category and el.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms)
    except:
        return False

# -------------------- Selection --------------------
rooms = []
for eid in list(uidoc.Selection.GetElementIds()):
    el = doc.GetElement(eid)
    if is_room(el):
        rooms.append(el)

if not rooms:
    class RoomFilter(Selection.ISelectionFilter):
        def AllowElement(self, e):
            try: return e.Category and e.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms)
            except: return False
        def AllowReference(self, ref, pt): return False
    try:
        picked = uidoc.Selection.PickObjects(Selection.ObjectType.Element, RoomFilter(), "Pick Rooms")
        for r in picked:
            el = doc.GetElement(r.ElementId)
            if is_room(el):
                rooms.append(el)
    except:
        forms.alert("No rooms selected.", title="Interior Elevations", warn_icon=True); sys.exit(0)

if not rooms:
    forms.alert("No valid Room elements in selection.", title="Interior Elevations", warn_icon=True); sys.exit(0)

# -------------------- Ask sides + template --------------------
if not list(elevation_vfts()):
    forms.alert("No Elevation view type found in this project.", title="Interior Elevations", warn_icon=True); sys.exit(0)

template_name = ask_template_name()
template_view = find_template_view_by_name(template_name) if template_name else None

wanted = pick_sides()
if not wanted:
    forms.alert("No sides selected. Nothing to do.", title="Interior Elevations", warn_icon=True); sys.exit(0)

# -------------------- Main --------------------
created, skipped = [], []
chosen_vft, chosen_cap = None, 0
warned_single = False

t = Transaction(doc, "Interior Elevations from Rooms")
t.Start()
try:
    for room in rooms:
        plan = find_plan_on_level(room.LevelId)
        if not plan:
            skipped.append((room, "No plan view on the room's level.")); continue
        center = get_room_center(room, ref_view=plan)
        if center is None:
            skipped.append((room, "Unable to compute room center.")); continue
        scale = plan.Scale if getattr(plan, 'Scale', None) else 100

        if chosen_vft is None:
            chosen_vft, chosen_cap = choose_vft(center, scale, plan.Id)
            if chosen_vft is None or chosen_cap < 1:
                forms.alert("Could not create elevations with any elevation type in this project.",
                            title="Interior Elevations", warn_icon=True)
                t.RollBack(); sys.exit(0)

        roomnum, roomname = get_room_number(room), get_room_name(room)

        if chosen_cap >= 2:
            made = make_multi(room, plan, center, chosen_vft, template_view, roomnum, roomname, scale, wanted)
        else:
            made = make_single(room, plan, center, chosen_vft, template_view, roomnum, roomname, scale, wanted)
            warned_single = True

        if made:
            for v in made:
                created.append((room, v))
        else:
            skipped.append((room, "Failed to create elevations."))
    t.Commit()
except Exception as e:
    t.RollBack()
    forms.alert("Error:\n{}\n\n{}".format(e, traceback.format_exc()),
                title="Interior Elevations", warn_icon=True)
    sys.exit(0)

# -------------------- Report --------------------
lines = ["Created {} elevation views.".format(len(created))]
by_room = {}
for r, v in created:
    by_room.setdefault(r, []).append(v.Name)
for r, names in by_room.items():
    lines.append(u"- Room {} \"{}\": {}".format(get_room_number(r), get_room_name(r), u", ".join(names)))
if skipped:
    lines.append("\nSkipped/Issues:")
    for r, why in skipped:
        lines.append(u"- Room {} \"{}\": {}".format(get_room_number(r), get_room_name(r), why))
if warned_single:
    lines.append("\nNote: this project has no 4-sided (Interior) elevation type, so each side "
                 "was made with a separate rotated marker. For best results, add an "
                 "'Interior Elevation' type to your project.")

forms.alert(u"\n".join(lines), title="Interior Elevations", warn_icon=False)
