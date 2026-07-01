# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script
import math

doc = revit.doc
uidoc = revit.uidoc
av = doc.ActiveView
logger = script.get_logger()

# Settings
OFFSET_FEET = 1.5   # offset of dimension line from the door/window along wall normal
LINE_HALF_LEN = 10  # half-length (ft) of the dimension line segment to create

def _is_plan(v):
    try:
        return isinstance(v, DB.ViewPlan) and not v.IsTemplate
    except:
        return False

def _selected_family_instances():
    try:
        ids = list(uidoc.Selection.GetElementIds())
    except:
        ids = []
    insts = []
    for _id in ids:
        el = doc.GetElement(_id)
        if isinstance(el, DB.FamilyInstance):
            # only doors/windows
            try:
                bic = el.Category.BuiltInCategory
            except:
                bic = None
            if bic in (DB.BuiltInCategory.OST_Doors, DB.BuiltInCategory.OST_Windows):
                insts.append(el)
    return insts

def _normalize(xyz):
    mag = math.sqrt(xyz.X*xyz.X + xyz.Y*xyz.Y + xyz.Z*xyz.Z)
    if mag == 0: return DB.XYZ(1,0,0)
    return DB.XYZ(xyz.X/mag, xyz.Y/mag, xyz.Z/mag)

def _perp2d(vec):
    return DB.XYZ(-vec.Y, vec.X, 0.0)

def _host_wall(inst):
    try:
        host = inst.Host
        if isinstance(host, DB.Wall):
            return host
    except:
        pass
    # try from reference
    try:
        return doc.GetElement(inst.HostFace.Reference.ElementId)
    except:
        return None

def _wall_direction(wall):
    try:
        crv = wall.Location.Curve
        p0 = crv.GetEndPoint(0); p1 = crv.GetEndPoint(1)
        return _normalize(DB.XYZ(p1.X-p0.X, p1.Y-p0.Y, 0.0))
    except:
        # fallback to instance facing if present
        return DB.XYZ(1,0,0)

def _get_width_refs(inst):
    # Prefer explicit Left/Right refs
    refs_left = inst.GetReferences(DB.FamilyInstanceReferenceType.Left)
    refs_right = inst.GetReferences(DB.FamilyInstanceReferenceType.Right)
    lref = refs_left[0] if refs_left and len(refs_left) > 0 else None
    rref = refs_right[0] if refs_right and len(refs_right) > 0 else None

    # Fallback: Center Left/Right + one side
    if lref is None or rref is None:
        refs_clr = inst.GetReferences(DB.FamilyInstanceReferenceType.CenterLeftRight)
        if refs_clr and len(refs_clr) > 0:
            # try pairing center with one side if only one side found
            if lref and not rref:
                rref = refs_clr[0]
            elif rref and not lref:
                lref = refs_clr[0]

    if lref is not None and rref is not None and lref != rref:
        return (lref, rref)
    else:
        return (None, None)

def _instance_point(inst):
    try:
        lp = inst.Location
        if isinstance(lp, DB.LocationPoint):
            return lp.Point
    except:
        pass
    # bbox center fallback
    try:
        bb = inst.get_BoundingBox(av)
        if bb:
            return DB.XYZ( (bb.Min.X+bb.Max.X)*0.5, (bb.Min.Y+bb.Max.Y)*0.5, (bb.Min.Z+bb.Max.Z)*0.5 )
    except:
        pass
    return DB.XYZ(0,0,0)

def _make_dim(inst):
    # get references
    lref, rref = _get_width_refs(inst)
    if not (lref and rref):
        return False, "Missing left/right references"

    wall = _host_wall(inst)
    if wall is None:
        return False, "No host wall"

    wdir = _wall_direction(wall)
    nrm  = _perp2d(wdir)  # outward normal in plan
    base = _instance_point(inst)
    # move offset along normal at view's level Z
    try:
        lvl = doc.GetElement(inst.LevelId)
        z = lvl.Elevation if isinstance(lvl, DB.Level) else base.Z
    except:
        z = base.Z

    offset_pt = DB.XYZ(base.X + nrm.X*OFFSET_FEET, base.Y + nrm.Y*OFFSET_FEET, z)
    p0 = DB.XYZ(offset_pt.X - wdir.X*LINE_HALF_LEN, offset_pt.Y - wdir.Y*LINE_HALF_LEN, z)
    p1 = DB.XYZ(offset_pt.X + wdir.X*LINE_HALF_LEN, offset_pt.Y + wdir.Y*LINE_HALF_LEN, z)
    line = DB.Line.CreateBound(p0, p1)

    ra = DB.ReferenceArray()
    try:
        ra.Append(lref); ra.Append(rref)
    except:
        return False, "Could not append refs"

    t = DB.Transaction(doc, "Auto width dim")
    t.Start()
    try:
        dim = doc.Create.NewDimension(av, line, ra) if hasattr(doc.Create, "NewDimension") else DB.Dimension.Create(doc, av.Id, line, ra)
        t.Commit()
        return True, None
    except Exception as ex:
        t.RollBack()
        return False, str(ex)

# --- main ---
if not _is_plan(av):
    forms.alert("Run in a plan view.", title="Auto-Width Dims", exitscript=True)

insts = _selected_family_instances()
if not insts:
    forms.alert("Select Door/Window instances first.", title="Auto-Width Dims", exitscript=True)

ok = 0; fail = 0; notes = []
for inst in insts:
    success, err = _make_dim(inst)
    if success: ok += 1
    else:
        fail += 1
        if err: notes.append("{}: {}".format(inst.Id.IntegerValue, err))

if fail and notes:
    forms.alert("Created: {}  | Failed: {}\n\n{}".format(ok, fail, "\n".join(notes[:15])), title="Auto-Width Dims")
else:
    from pyrevit import forms as f
    f.toast("Created: {}  | Failed: {}".format(ok, fail), title="Auto-Width Dims")
