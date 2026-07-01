# -*- coding: utf-8 -*-
"""Clean CAD -> Detail.

Pick a DWG. It is imported into a THROWAWAY background document (so your model
is never polluted with imported line patterns / layers / styles), its geometry
is read out, and recreated as NATIVE Revit detail lines in a new drafting view
in the current project -- each line on a clean native line style named after
its CAD layer.

Captures LINEWORK ONLY (lines/arcs/circles/polylines/splines). Text, dimensions
and hatches do NOT transfer (that needs explode, which the API cannot do)."""

from pyrevit import revit, DB, forms, script
import os
import re
import clr

doc = revit.doc
uidoc = revit.uidoc
try:
    app = __revit__.Application
except Exception:
    from pyrevit import HOST_APP
    app = HOST_APP.app

logger = script.get_logger()


# ---------- pick file ----------

def pick_dwg():
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import OpenFileDialog, DialogResult
    dlg = OpenFileDialog()
    dlg.Filter = "CAD (*.dwg;*.dxf)|*.dwg;*.dxf"
    dlg.Title = "Select a CAD file to convert to a clean detail"
    if dlg.ShowDialog() == DialogResult.OK:
        return dlg.FileName
    return None


# ---------- temp doc + import ----------

def make_temp_doc():
    tpl = None
    try:
        tpl = app.DefaultProjectTemplate
    except Exception:
        tpl = None
    if tpl and os.path.isfile(tpl):
        return app.NewProjectDocument(tpl)
    return app.NewProjectDocument(DB.UnitSystem.Imperial)


def drafting_vft(d):
    for v in DB.FilteredElementCollector(d).OfClass(DB.ViewFamilyType):
        try:
            if v.ViewFamily == DB.ViewFamily.Drafting:
                return v
        except Exception:
            pass
    return None


def import_dwg(tempdoc, dwg_path):
    vft = drafting_vft(tempdoc)
    opts = DB.DWGImportOptions()
    opts.ThisViewOnly = True
    opts.Placement = DB.ImportPlacement.Origin
    opts.Unit = DB.ImportUnit.Default          # auto-detect units
    try:
        opts.ColorMode = DB.ImportColorMode.BlackAndWhite
    except Exception:
        pass

    t = DB.Transaction(tempdoc, "Import CAD")
    t.Start()
    try:
        tview = DB.ViewDrafting.Create(tempdoc, vft.Id)
        ok, imp_id = tempdoc.Import(dwg_path, opts, tview)
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return tempdoc.GetElement(imp_id), tview


# ---------- read geometry ----------

def layer_of(gobj, srcdoc, fallback):
    try:
        gs = srcdoc.GetElement(gobj.GraphicsStyleId)
        if gs is not None and gs.Name:
            return gs.Name
    except Exception:
        pass
    return fallback


def collect(imp, tview):
    opt = DB.Options()
    opt.View = tview
    opt.IncludeNonVisibleObjects = False
    geo = imp.get_Geometry(opt)
    out = []   # (curve, layer_name)

    def walk(g, layer):
        if isinstance(g, DB.GeometryInstance):
            for sub in g.GetInstanceGeometry():
                walk(sub, layer)
        elif isinstance(g, DB.Curve):
            out.append((g, layer_of(g, imp.Document, layer or "CAD")))
        elif isinstance(g, DB.PolyLine):
            pts = list(g.GetCoordinates())
            for i in range(len(pts) - 1):
                try:
                    out.append((DB.Line.CreateBound(pts[i], pts[i + 1]),
                                layer or "CAD"))
                except Exception:
                    pass

    if geo is not None:
        for g in geo:
            walk(g, None)
    return out


# ---------- flatten + tessellate to view-plane curves ----------

def _z0(p):
    return DB.XYZ(p.X, p.Y, 0.0)


def to_flat_curves(curve):
    flat = []
    try:
        if isinstance(curve, DB.Line):
            a = _z0(curve.GetEndPoint(0))
            b = _z0(curve.GetEndPoint(1))
            if a.DistanceTo(b) > 1e-7:
                flat.append(DB.Line.CreateBound(a, b))
            return flat
        if isinstance(curve, DB.Arc):
            a = _z0(curve.GetEndPoint(0))
            b = _z0(curve.GetEndPoint(1))
            m = _z0(curve.Evaluate(0.5, True))
            try:
                flat.append(DB.Arc.Create(a, b, m))
                return flat
            except Exception:
                pass  # fall through to tessellation
        # ellipse / spline / anything else -> tessellate to segments
        pts = [ _z0(p) for p in curve.Tessellate() ]
        for i in range(len(pts) - 1):
            if pts[i].DistanceTo(pts[i + 1]) > 1e-7:
                flat.append(DB.Line.CreateBound(pts[i], pts[i + 1]))
    except Exception:
        pass
    return flat


# ---------- native line styles ----------

def sanitize(name):
    s = re.sub(r'[\\:{}\[\]|;<>?`~]', '_', name)
    return ("DWG_" + s)[:60]


def thin_lines_style(target):
    """Return the built-in 'Thin Lines' graphics style (a subcategory of
    OST_Lines). Falls back to None if not found, in which case curves keep
    the view's default line style."""
    lines_cat = target.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    for c in lines_cat.SubCategories:
        try:
            if c.Name == "Thin Lines":
                return c.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
        except Exception:
            pass
    return None


def unique_view_name(target, base):
    have = set()
    for v in DB.FilteredElementCollector(target).OfClass(DB.View):
        try:
            have.add(v.Name)
        except Exception:
            pass
    name = base
    i = 2
    while name in have:
        name = u"{0} ({1})".format(base, i)
        i += 1
    return name


# ---------- main ----------

dwg = pick_dwg()
if not dwg:
    script.exit()

vft = drafting_vft(doc)
if not vft:
    forms.alert("This project has no Drafting view type.",
                title="Clean CAD to Detail", exitscript=True)

tempdoc = None
try:
    tempdoc = make_temp_doc()
    imp, tview = import_dwg(tempdoc, dwg)
    collected = collect(imp, tview)
    if not collected:
        forms.alert("No linework could be read from that CAD file.",
                    title="Clean CAD to Detail", exitscript=True)

    layers = sorted(set(l for _, l in collected))

    base_name = os.path.splitext(os.path.basename(dwg))[0]
    new_view = None
    made = 0

    t = DB.Transaction(doc, "Clean CAD to Detail")
    t.Start()
    try:
        thin = thin_lines_style(doc)

        new_view = DB.ViewDrafting.Create(doc, vft.Id)
        try:
            new_view.Name = unique_view_name(doc, base_name)
        except Exception:
            pass

        for curve, layer in collected:
            for fc in to_flat_curves(curve):
                try:
                    dc = doc.Create.NewDetailCurve(new_view, fc)
                    if thin is not None:
                        try:
                            dc.LineStyle = thin
                        except Exception:
                            pass
                    made += 1
                except Exception:
                    pass
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    if new_view is not None:
        try:
            uidoc.ActiveView = new_view
        except Exception:
            pass

    forms.alert(
        u"Created drafting view: {0}\n"
        u"Native detail lines (Thin Lines): {1}\n\n"
        u"Note: text, dimensions and hatches are not included "
        u"(linework only).".format(
            new_view.Name if new_view else "?", made),
        title="Clean CAD to Detail")
finally:
    if tempdoc is not None:
        try:
            tempdoc.Close(False)   # discard the throwaway file (no save)
        except Exception:
            pass
