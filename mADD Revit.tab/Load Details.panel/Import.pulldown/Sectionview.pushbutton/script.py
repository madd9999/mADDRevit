# -*- coding: utf-8 -*-
"""Preview wall sections / detail callouts from the saved library model and
import ONLY the drawn 2D detailing -- detail items, detail components, filled
& masking regions, insulation, detail groups, text -- into new drafting views.
Model elements AND tags/dimensions of model elements are excluded."""

from pyrevit import revit, DB, forms, script
import os
import tempfile
import glob
import clr

from System.Collections.Generic import List as NetList
from System import Uri, UriKind
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption

doc = revit.doc
try:
    app = __revit__.Application
except Exception:
    from pyrevit import HOST_APP
    app = HOST_APP.app

logger = script.get_logger()

# Same config section as the "Set Library File" button.
config = script.get_config('LoadDetailsLibrary')

XAML_FILE = script.get_bundle_file('ui.xaml')

_preview_cache = {}   # view_id -> png path (or None)


# ============================================================
# WHAT GETS IMPORTED  --  edit these two sets to taste
# ============================================================

# Genuine 2D detailing categories that are KEPT.
_KEEP_BICS = set([
    int(DB.BuiltInCategory.OST_DetailComponents),     # detail components / items
    int(DB.BuiltInCategory.OST_Lines),                # detail lines
    int(DB.BuiltInCategory.OST_DetailComponentTags),  # detail-item tags (self-contained)
    int(DB.BuiltInCategory.OST_FilledRegion),         # filled regions
    int(DB.BuiltInCategory.OST_MaskingRegion),        # masking regions
    int(DB.BuiltInCategory.OST_InsulationLines),      # batt insulation
    int(DB.BuiltInCategory.OST_TextNotes),            # text notes
    int(DB.BuiltInCategory.OST_IOSDetailGroups),      # detail groups
    int(DB.BuiltInCategory.OST_RasterImages),         # placed images
])
# To import EVERY 2D element except the drop-list below, set: _KEEP_BICS = set()
# To exclude detail-item tags too, remove OST_DetailComponentTags above.

# Tag / dimension categories that annotate MODEL elements -- always DROPPED.
_DROP_BICS = set([
    int(DB.BuiltInCategory.OST_Dimensions),
    int(DB.BuiltInCategory.OST_SpotElevations),
    int(DB.BuiltInCategory.OST_SpotCoordinates),
    int(DB.BuiltInCategory.OST_KeynoteTags),
    int(DB.BuiltInCategory.OST_MaterialTags),
    int(DB.BuiltInCategory.OST_MultiCategoryTags),
    int(DB.BuiltInCategory.OST_WallTags),
    int(DB.BuiltInCategory.OST_FloorTags),
    int(DB.BuiltInCategory.OST_RoomTags),
    int(DB.BuiltInCategory.OST_AreaTags),
    int(DB.BuiltInCategory.OST_DoorTags),
    int(DB.BuiltInCategory.OST_WindowTags),
    int(DB.BuiltInCategory.OST_StructuralFramingTags),
    int(DB.BuiltInCategory.OST_StructuralColumnTags),
])


# ---------- library path ----------

def get_saved_path():
    p = getattr(config, 'library_rvt_path', None)
    return p if (p and os.path.isfile(p)) else None


def pick_and_save_path():
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import OpenFileDialog, DialogResult
    dlg = OpenFileDialog()
    dlg.Filter = "Revit Project (*.rvt)|*.rvt"
    dlg.Title = "Select Library Model (.rvt) containing Sections / Details"
    if dlg.ShowDialog() == DialogResult.OK:
        config.library_rvt_path = dlg.FileName
        script.save_config()
        return dlg.FileName
    return None


# ---------- open / collect ----------

def get_or_open(path):
    """Return (document, opened_by_us). Reuses the doc if already open."""
    for d in app.Documents:
        try:
            if d.PathName and os.path.normcase(d.PathName) == os.path.normcase(path):
                return d, False
        except Exception:
            pass
    info = DB.BasicFileInfo.Extract(path)
    mp = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(path)
    opts = DB.OpenOptions()
    if info.IsWorkshared:
        opts.DetachFromCentralOption = DB.DetachFromCentralOption.DetachAndPreserveWorksets
    else:
        opts.DetachFromCentralOption = DB.DetachFromCentralOption.DoNotDetach
    opts.Audit = False
    return app.OpenDocumentFile(mp, opts), True


# Wall sections (ViewType.Section) and detail callouts (ViewType.Detail) are
# both ViewSection instances; we filter by ViewType.
_WANTED_TYPES = (DB.ViewType.Section, DB.ViewType.Detail)


def collect_section_detail(srcdoc):
    out = []
    for v in DB.FilteredElementCollector(srcdoc).OfClass(DB.ViewSection):
        try:
            if v.IsTemplate:
                continue
            if v.ViewType in _WANTED_TYPES:
                out.append(v)
        except Exception:
            pass
    return out


def vt_label(v):
    t = v.ViewType
    if t == DB.ViewType.Section:
        tag = "Section"
    elif t == DB.ViewType.Detail:
        tag = "Detail"
    else:
        tag = str(t)
    return u"[{0}] {1}".format(tag, v.Name)


# ---------- destination view helpers ----------

def get_drafting_vft(tdoc):
    for vft in DB.FilteredElementCollector(tdoc).OfClass(DB.ViewFamilyType):
        try:
            if vft.ViewFamily == DB.ViewFamily.Drafting:
                return vft
        except Exception:
            pass
    return None


def unique_view_name(tdoc, base):
    existing = set()
    for v in DB.FilteredElementCollector(tdoc).OfClass(DB.View):
        try:
            existing.add(v.Name)
        except Exception:
            pass
    name = base
    i = 2
    while name in existing:
        name = u"{0} ({1})".format(base, i)
        i += 1
        if i > 500:
            break
    return name


def detail_member_ids(srcdoc, srcview):
    """Detail items / detail components / 2D detailing ONLY.
    Keeps view-specific elements whose category is detailing; drops model
    elements and drops tags/dimensions that reference model elements."""
    ids = []
    col = DB.FilteredElementCollector(srcdoc, srcview.Id).WhereElementIsNotElementType()
    for e in col:
        if e.Id == srcview.Id:
            continue
        try:
            if not e.ViewSpecific:                       # model element -> skip
                continue
            cat = e.Category
            if cat is None:
                continue
            bic = cat.Id.IntegerValue
            if bic in _DROP_BICS:                         # tag/dim of model -> skip
                continue
            if _KEEP_BICS and bic not in _KEEP_BICS:      # not detailing -> skip
                continue
            ids.append(e.Id)
        except Exception:
            pass
    return ids


def copy_detail_contents(srcview, srcdoc, dst_view, cpo):
    """Copy detailing into dst_view. Bulk first, then per-element fallback so
    one un-copyable element can't blank the whole view. Returns count copied."""
    ids = detail_member_ids(srcdoc, srcview)
    if not ids:
        return 0

    net = NetList[DB.ElementId](ids)
    try:
        copied = DB.ElementTransformUtils.CopyElements(
            srcview, net, dst_view, DB.Transform.Identity, cpo)
        return len(list(copied)) if copied else len(ids)
    except Exception as ex:
        logger.debug('bulk copy failed, per-element fallback: {0}'.format(ex))

    ok = 0
    for eid in ids:
        single = NetList[DB.ElementId]()
        single.Add(eid)
        try:
            DB.ElementTransformUtils.CopyElements(
                srcview, single, dst_view, DB.Transform.Identity, cpo)
            ok += 1
        except Exception:
            pass
    return ok


# ---------- preview rendering ----------

def export_preview(srcdoc, view):
    key = view.Id.IntegerValue
    if key in _preview_cache:
        return _preview_cache[key]

    folder = tempfile.mkdtemp(prefix='sd_prev_')
    opts = DB.ImageExportOptions()
    opts.ExportRange = DB.ExportRange.SetOfViews
    opts.SetViewsAndSheets(NetList[DB.ElementId]([view.Id]))
    opts.ZoomType = DB.ZoomFitType.FitToPage
    opts.PixelSize = 1200
    opts.HLRandWFViewsFileType = DB.ImageFileType.PNG
    opts.ImageResolution = DB.ImageResolution.DPI_72
    opts.FilePath = os.path.join(folder, 'preview')

    result = None
    try:
        srcdoc.ExportImage(opts)
        hits = glob.glob(os.path.join(folder, '*.png'))
        result = hits[0] if hits else None
    except Exception as ex:
        logger.debug('preview export failed: {0}'.format(ex))
        result = None

    _preview_cache[key] = result
    return result


# ---------- WPF window ----------

class PreviewWindow(forms.WPFWindow):
    def __init__(self, xaml_file, srcdoc, views):
        forms.WPFWindow.__init__(self, xaml_file)
        self.srcdoc = srcdoc
        self.label_to_view = {}
        labels = []
        for v in sorted(views, key=lambda x: vt_label(x).lower()):
            lbl = vt_label(v)
            self.label_to_view[lbl] = v
            labels.append(lbl)
        self.view_list.ItemsSource = labels
        self.selected_views = []

    def _bitmap(self, path):
        bi = BitmapImage()
        bi.BeginInit()
        bi.CacheOption = BitmapCacheOption.OnLoad
        bi.UriSource = Uri(path, UriKind.Absolute)
        bi.EndInit()
        return bi

    def on_selection_changed(self, sender, args):
        lbl = None
        if args.AddedItems is not None and args.AddedItems.Count > 0:
            lbl = args.AddedItems[args.AddedItems.Count - 1]
        else:
            lbl = self.view_list.SelectedItem
        if not lbl:
            return

        self.preview_label.Text = u'{0}  (rendering...)'.format(lbl)
        view = self.label_to_view.get(lbl)
        png = export_preview(self.srcdoc, view)
        if png and os.path.isfile(png):
            self.preview_image.Source = self._bitmap(png)
            self.preview_label.Text = lbl
        else:
            self.preview_image.Source = None
            self.preview_label.Text = u'{0}  (no preview available)'.format(lbl)

    def on_import(self, sender, args):
        picks = list(self.view_list.SelectedItems)
        self.selected_views = [self.label_to_view[l] for l in picks]
        self.Close()

    def on_cancel(self, sender, args):
        self.selected_views = []
        self.Close()


class _DupTypeHandler(DB.IDuplicateTypeNamesHandler):
    def OnDuplicateTypeNamesFound(self, args):
        return DB.DuplicateTypeAction.UseDestinationTypes


# ---------- main ----------

path = get_saved_path() or pick_and_save_path()
if not path:
    forms.alert("No library model selected.",
                title="Import Section / Detail", exitscript=True)

srcdoc, opened_by_us = get_or_open(path)
if not srcdoc:
    forms.alert("Failed to open library model.",
                title="Import Section / Detail", exitscript=True)

try:
    views = collect_section_detail(srcdoc)
    if not views:
        forms.alert("No wall sections or detail callouts found in:\n{0}".format(path),
                    title="Import Section / Detail", exitscript=True)

    win = PreviewWindow(XAML_FILE, srcdoc, views)
    win.show_dialog()
    picked = win.selected_views
    if not picked:
        script.exit()

    vft = get_drafting_vft(doc)
    if not vft:
        forms.alert("No Drafting view type found in the current model.",
                    title="Import Section / Detail", exitscript=True)

    cpo = DB.CopyPasteOptions()
    cpo.SetDuplicateTypeNamesHandler(_DupTypeHandler())

    results = []
    empties = []
    with revit.Transaction("Import Section/Detail (2D)", doc=doc):
        for sv in picked:
            nv = DB.ViewDrafting.Create(doc, vft.Id)
            try:
                nv.Name = unique_view_name(doc, sv.Name)
            except Exception:
                pass
            try:
                nv.Scale = sv.Scale
            except Exception:
                pass
            count = copy_detail_contents(sv, srcdoc, nv, cpo)
            results.append((nv.Name, count))
            if count == 0:
                empties.append(nv.Name)

    msg = "\n".join([u"  - {0}  ({1} detail elements)".format(n, c) for n, c in results])
    if empties:
        msg += ("\n\nNote: view(s) with 0 elements had no copyable detailing "
                "(only model linework and/or model tags, which can't transfer).")
    forms.alert(u"Imported {0} view(s) as drafting views:\n{1}".format(len(results), msg),
                title="Import Section / Detail")
finally:
    if opened_by_us:
        try:
            srcdoc.Close(False)
        except Exception:
            pass
