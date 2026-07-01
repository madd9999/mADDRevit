# -*- coding: utf-8 -*-
# IronPython-friendly Silent Version (v1.2) with swapped origin offsets and letter-first code
from pyrevit import revit, DB, forms, script

__doc__ = "Silent: Set each viewport's Detail Number to grid zone at view title start. No prompts. Y-letter first, then X-number."

logger = script.get_logger()
doc = revit.doc
uidoc = revit.uidoc

# --------------------------- Fixed Config ---------------------------
GRID_X_IN = 2.0            # X grid size: 2"
GRID_Y_IN = 1.75           # Y grid size: 1 3/4"
ORIGIN_X_IN = 1.546875     # X origin offset: 1 35/64"
ORIGIN_Y_IN = 0.625        # Y origin offset: 5/8"
FORMAT = "{y}{x}"          # e.g., C3 (letter first), skip 'I'

INCH_TO_FT = 1.0/12.0

def letters_index(idx):
    # 0->A, 1->B, ... skip 'I'
    alphabet = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c != "I"]
    if idx < 0:
        idx = 0
    if idx < len(alphabet):
        return alphabet[idx]
    left = idx
    base = len(alphabet)
    s = ""
    while True:
        s = alphabet[left % base] + s
        left = left // base - 1
        if left < 0:
            break
    return s

def _outline_minmax(outline):
    if outline is None:
        return None, None
    try:
        pmin = outline.MinimumPoint
        pmax = outline.MaximumPoint
        return pmin, pmax
    except Exception as ex:
        logger.debug("Outline access failed: {0}".format(ex))
        return None, None

def get_label_start_point(vp):
    # Try Revit's label outline (preferred)
    try:
        o = vp.GetLabelOutline()
        pmin, pmax = _outline_minmax(o)
        if pmin and pmax:
            x = pmin.X  # left edge
            y = (pmin.Y + pmax.Y) * 0.5  # middle
            return DB.XYZ(x, y, 0.0)
    except Exception as ex:
        logger.debug("GetLabelOutline unavailable: {0}".format(ex))
    # Fallback: estimate from viewport box
    o2 = vp.GetBoxOutline()
    pmin, pmax = _outline_minmax(o2)
    if pmin and pmax:
        width = (pmax.X - pmin.X)
        # Label offset (feet)
        p = vp.get_Parameter(DB.BuiltInParameter.VIEWPORT_LABEL_OFFSET)
        try:
            label_off = p.AsDouble() if p else INCH_TO_FT * 0.25
        except Exception:
            label_off = INCH_TO_FT * 0.25
        y = pmin.Y - label_off
        x = pmin.X + width * 0.1
        return DB.XYZ(x, y, 0.0)
    # Last resort: sheet origin
    return DB.XYZ(0,0,0)

def compute_grid_code(pt_sheet):
    gx = GRID_X_IN * INCH_TO_FT
    gy = GRID_Y_IN * INCH_TO_FT
    ox = ORIGIN_X_IN * INCH_TO_FT
    oy = ORIGIN_Y_IN * INCH_TO_FT
    import math
    ix = int(math.floor((pt_sheet.X - ox) / gx)) + 1
    iy0 = int(math.floor((pt_sheet.Y - oy) / gy))
    letter = letters_index(iy0)
    return FORMAT.format(x=ix, y=letter)

def ensure_unique_on_sheet(sheet_id, desired_code, skip_vp_id):
    taken = set()
    vps = DB.FilteredElementCollector(doc, sheet_id).OfClass(DB.Viewport)
    for other in vps:
        if other.Id != skip_vp_id:
            p = other.get_Parameter(DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
            if p:
                taken.add(p.AsString())
    if desired_code not in taken:
        return desired_code
    n = 2
    while True:
        candidate = "{0}-{1}".format(desired_code, n)
        if candidate not in taken:
            return candidate
        n += 1

def collect_target_viewports():
    ids = list(uidoc.Selection.GetElementIds())
    vps = []
    for i in ids:
        el = doc.GetElement(i)
        if isinstance(el, DB.Viewport):
            vps.append(el)
    if vps:
        return vps
    curview = doc.ActiveView
    if not isinstance(curview, DB.ViewSheet):
        forms.alert("Open a sheet or select viewports on a sheet, then run again.", exitscript=True)
    vps = list(DB.FilteredElementCollector(doc, curview.Id).OfClass(DB.Viewport))
    if not vps:
        forms.alert("No viewports on the active sheet.", exitscript=True)
    return vps

vps = collect_target_viewports()

t = DB.Transaction(doc, "pyRevit: Auto Grid Detail Number (Silent v1.2)")
t.Start()
updated, skipped = 0, 0
for vp in vps:
    try:
        pt = get_label_start_point(vp)
        code = compute_grid_code(pt)
        sheet_id = vp.SheetId
        final_code = ensure_unique_on_sheet(sheet_id, code, vp.Id)
        p = vp.get_Parameter(DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
        if p and not p.IsReadOnly:
            p.Set(final_code)
            updated += 1
        else:
            skipped += 1
    except Exception as ex:
        logger.warning("Viewport {0} failed: {1}".format(vp.Id.IntegerValue, ex))
        skipped += 1
t.Commit()

forms.alert("Auto Grid Detail Number v1.2: Updated {0}, Skipped {1}.".format(updated, skipped))
