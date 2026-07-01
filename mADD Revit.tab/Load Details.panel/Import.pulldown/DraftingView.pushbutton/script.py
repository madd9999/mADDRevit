# -*- coding: utf-8 -*-
"""Preview drafting views from the saved library model and import the
selected one(s) -- WITH their detail contents -- into the current document."""

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


# ---------- library path ----------

def get_saved_path():
    p = getattr(config, 'library_rvt_path', None)
    return p if (p and os.path.isfile(p)) else None


def pick_and_save_path():
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import OpenFileDialog, DialogResult
    dlg = OpenFileDialog()
    dlg.Filter = "Revit Project (*.rvt)|*.rvt"
    dlg.Title = "Select Library Model (.rvt) containing Drafting Views"
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


def collect_drafting_views(srcdoc):
    return [v for v in DB.FilteredElementCollector(srcdoc).OfClass(DB.ViewDrafting)
            if not v.IsTemplate]


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


def member_ids(srcdoc, srcview):
    """All non-type elements owned by the source drafting view, minus the
    view element itself."""
    ids = []
    col = DB.FilteredElementCollector(srcdoc, srcview.Id).WhereElementIsNotElementType()
    for e in col:
        if e.Id == srcview.Id:
            continue
        ids.append(e.Id)
    return ids


def copy_contents(srcview, srcdoc, dst_view, cpo):
    """Copy the source view's detail elements into dst_view. Tries a bulk
    copy first; if that fails, copies one-by-one and skips the few elements
    Revit refuses to copy, so a single bad element can't blank the view.
    Returns number of elements copied."""
    ids = member_ids(srcdoc, srcview)
    if not ids:
        return 0

    net = NetList[DB.ElementId](ids)
    try:
        copied = DB.ElementTransformUtils.CopyElements(
            srcview, net, dst_view, DB.Transform.Identity, cpo)
        return len(list(copied)) if copied else len(ids)
    except Exception as ex:
        logger.debug('bulk copy failed, falling back per-element: {0}'.format(ex))

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

    folder = tempfile.mkdtemp(prefix='dv_prev_')
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
        self.name_to_view = {}
        names = []
        for v in sorted(views, key=lambda x: x.Name.lower()):
            self.name_to_view[v.Name] = v
            names.append(v.Name)
        self.view_list.ItemsSource = names
        self.selected_views = []

    def _bitmap(self, path):
        bi = BitmapImage()
        bi.BeginInit()
        bi.CacheOption = BitmapCacheOption.OnLoad
        bi.UriSource = Uri(path, UriKind.Absolute)
        bi.EndInit()
        return bi

    def on_selection_changed(self, sender, args):
        name = None
        if args.AddedItems is not None and args.AddedItems.Count > 0:
            name = args.AddedItems[args.AddedItems.Count - 1]
        else:
            name = self.view_list.SelectedItem
        if not name:
            return

        self.preview_label.Text = u'{0}  (rendering...)'.format(name)
        view = self.name_to_view.get(name)
        png = export_preview(self.srcdoc, view)
        if png and os.path.isfile(png):
            self.preview_image.Source = self._bitmap(png)
            self.preview_label.Text = name
        else:
            self.preview_image.Source = None
            self.preview_label.Text = u'{0}  (no preview available)'.format(name)

    def on_import(self, sender, args):
        picks = list(self.view_list.SelectedItems)
        self.selected_views = [self.name_to_view[n] for n in picks]
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
                title="Import Drafting View", exitscript=True)

srcdoc, opened_by_us = get_or_open(path)
if not srcdoc:
    forms.alert("Failed to open library model.",
                title="Import Drafting View", exitscript=True)

try:
    views = collect_drafting_views(srcdoc)
    if not views:
        forms.alert("No drafting views found in:\n{0}".format(path),
                    title="Import Drafting View", exitscript=True)

    win = PreviewWindow(XAML_FILE, srcdoc, views)
    win.show_dialog()
    picked = win.selected_views
    if not picked:
        script.exit()

    vft = get_drafting_vft(doc)
    if not vft:
        forms.alert("No Drafting view type found in the current model.",
                    title="Import Drafting View", exitscript=True)

    cpo = DB.CopyPasteOptions()
    cpo.SetDuplicateTypeNamesHandler(_DupTypeHandler())

    results = []
    with revit.Transaction("Import Drafting View(s)", doc=doc):
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
            count = copy_contents(sv, srcdoc, nv, cpo)
            results.append((nv.Name, count))

    msg = "\n".join([u"  - {0}  ({1} elements)".format(n, c) for n, c in results])
    forms.alert(u"Imported {0} view(s):\n{1}".format(len(results), msg),
                title="Import Drafting View")
finally:
    if opened_by_us:
        try:
            srcdoc.Close(False)
        except Exception:
            pass
