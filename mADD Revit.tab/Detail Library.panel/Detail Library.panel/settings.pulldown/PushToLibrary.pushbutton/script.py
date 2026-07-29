# -*- coding: utf-8 -*-
"""Push a drafting view from this project into the shared Detail Library as
its own small, standalone .rvt file -- versioned and attributed to you.
Writing a brand-new file (rather than modifying a shared master file) means
no lock contention even if many people push at the same time."""

from pyrevit import revit, DB, forms, script
import os
import re
import json
import glob
import tempfile
import hashlib
import datetime
import clr

from System.Collections.Generic import List as NetList
from System import Uri, UriKind
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption

doc = revit.doc
uidoc = revit.uidoc
try:
    app = __revit__.Application
except Exception:
    from pyrevit import HOST_APP
    app = HOST_APP.app

logger = script.get_logger()
config = script.get_config('DetailLibraryFolder')
XAML_FILE = script.get_bundle_file('ui.xaml')

CATALOG_NAME = "catalog.json"
THUMB_DIR = "_thumbnails"
_preview_cache = {}


# ---------- library folder ----------

def get_saved_folder():
    p = getattr(config, 'library_folder', None)
    return p if (p and os.path.isdir(p)) else None


def pick_and_save_folder():
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import FolderBrowserDialog, DialogResult
    dlg = FolderBrowserDialog()
    dlg.Description = "Select the shared Detail Library folder"
    if dlg.ShowDialog() == DialogResult.OK:
        config.library_folder = dlg.SelectedPath
        script.save_config()
        return dlg.SelectedPath
    return None


# ---------- push password ----------
# Fixed password required to use Push to Library. Change this string to
# change the password -- it applies to everyone using this plugin.
PUSH_PASSWORD = "madd"


def verify_password(password):
    return (password or "") == PUSH_PASSWORD


# ---------- copy machinery (same proven technique as the other tools) ----------

def to_element_id(x):
    return x if isinstance(x, DB.ElementId) else x.Id


def collect_drafting_views(tdoc):
    return [v for v in DB.FilteredElementCollector(tdoc).OfClass(DB.ViewDrafting) if not v.IsTemplate]


def member_ids(srcdoc, srcview):
    ids = []
    col = DB.FilteredElementCollector(srcdoc, srcview.Id).WhereElementIsNotElementType()
    for e in col:
        if e.Id == srcview.Id:
            continue
        ids.append(e.Id)
    return ids


def copy_view_with_contents(srcdoc, srcview, tdoc, cpo):
    """Same technique used by Browse Library / Import Drafting View: copy
    the view and its owned content together in one document-to-document
    call so Revit recreates them as one related, correctly-owned unit."""
    ids = [srcview.Id]
    ids.extend(member_ids(srcdoc, srcview))
    net = NetList[DB.ElementId](ids)
    new_view = None
    err = None
    try:
        copied = DB.ElementTransformUtils.CopyElements(srcdoc, net, tdoc, DB.Transform.Identity, cpo)
        for cid in copied:
            el = tdoc.GetElement(cid)
            if isinstance(el, DB.ViewDrafting):
                new_view = el
                break
        if new_view is None:
            err = "copy completed but no new drafting view was found in the result"
    except Exception as ex:
        err = u"{0}".format(ex)
    return new_view, err


class _DupTypeHandler(DB.IDuplicateTypeNamesHandler):
    def OnDuplicateTypeNamesFound(self, args):
        return DB.DuplicateTypeAction.UseDestinationTypes


# ---------- naming / versioning ----------

def sanitize_filename(name):
    bad = '<>:"/\\|?*'
    out = u''.join('_' if c in bad else c for c in name)
    out = out.strip()
    return out or "Detail"


_VERSION_RE = re.compile(r'^(.*)_v(\d+)(?:_.*)?$')


def next_version(folder, category, base_name):
    cat_dir = os.path.join(folder, category)
    if not os.path.isdir(cat_dir):
        return 1
    best = 0
    for fn in os.listdir(cat_dir):
        if not fn.lower().endswith('.rvt'):
            continue
        stem = os.path.splitext(fn)[0]
        m = _VERSION_RE.match(stem)
        if m and m.group(1) == base_name:
            try:
                best = max(best, int(m.group(2)))
            except Exception:
                pass
    return best + 1


def list_categories(folder):
    try:
        return sorted([d for d in os.listdir(folder)
                        if os.path.isdir(os.path.join(folder, d))
                        and not d.startswith('_') and not d.startswith('.')])
    except Exception:
        return []


# ---------- catalog (so Browse Library sees the push immediately) ----------

def catalog_path(folder):
    return os.path.join(folder, CATALOG_NAME)


def load_catalog(folder):
    p = catalog_path(folder)
    if os.path.isfile(p):
        try:
            with open(p, 'r') as f:
                return json.load(f)
        except Exception as ex:
            logger.debug('could not read catalog: {0}'.format(ex))
    return {"version": 1, "entries": {}}


def save_catalog(folder, catalog):
    p = catalog_path(folder)
    try:
        tmp = p + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(catalog, f, indent=2)
        if os.path.isfile(p):
            os.remove(p)
        os.rename(tmp, p)
    except Exception as ex:
        logger.debug('could not write catalog (shared file may be locked -- '
                      'Browse Library will still pick this up on its next refresh): {0}'.format(ex))


def thumb_filename(relpath, view_name):
    key = u"{0}::{1}".format(relpath, view_name)
    h = hashlib.md5(key.encode('utf-8')).hexdigest()
    return h + ".png"


def export_thumb(srcdoc, view, folder, relpath):
    thumb_dir = os.path.join(folder, THUMB_DIR)
    try:
        if not os.path.isdir(thumb_dir):
            os.makedirs(thumb_dir)
    except Exception:
        pass
    fname = thumb_filename(relpath, view.Name)
    out_path = os.path.join(thumb_dir, fname)
    tmp_folder = tempfile.mkdtemp(prefix='dl_push_thumb_')
    opts = DB.ImageExportOptions()
    opts.ExportRange = DB.ExportRange.SetOfViews
    opts.SetViewsAndSheets(NetList[DB.ElementId]([view.Id]))
    opts.ZoomType = DB.ZoomFitType.FitToPage
    opts.PixelSize = 400
    opts.HLRandWFViewsFileType = DB.ImageFileType.PNG
    opts.ImageResolution = DB.ImageResolution.DPI_72
    opts.FilePath = os.path.join(tmp_folder, 'preview')
    try:
        srcdoc.ExportImage(opts)
        hits = glob.glob(os.path.join(tmp_folder, '*.png'))
        if hits:
            try:
                if os.path.isfile(out_path):
                    os.remove(out_path)
                os.rename(hits[0], out_path)
            except Exception:
                import shutil
                shutil.copyfile(hits[0], out_path)
            return os.path.join(THUMB_DIR, fname).replace('\\', '/')
    except Exception as ex:
        logger.debug('thumb export failed: {0}'.format(ex))
    return None


# ---------- preview (from the CURRENT project, already open -- cheap) ----------

def export_preview(view):
    key = view.Id.IntegerValue
    if key in _preview_cache:
        return _preview_cache[key]
    folder = tempfile.mkdtemp(prefix='dl_push_prev_')
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
        doc.ExportImage(opts)
        hits = glob.glob(os.path.join(folder, '*.png'))
        result = hits[0] if hits else None
    except Exception as ex:
        logger.debug('preview export failed: {0}'.format(ex))
    _preview_cache[key] = result
    return result


# ---------- WPF window ----------

class PushWindow(forms.WPFWindow):
    def __init__(self, xaml_file, views, categories, default_view):
        forms.WPFWindow.__init__(self, xaml_file)
        self.name_to_view = {}
        names = []
        for v in sorted(views, key=lambda x: x.Name.lower()):
            self.name_to_view[v.Name] = v
            names.append(v.Name)
        self.view_list.ItemsSource = names
        self.category_box.ItemsSource = categories
        if categories:
            self.category_box.SelectedIndex = 0
        self.selected_views = []
        self.category = None
        if default_view and default_view.Name in names:
            self.view_list.SelectedItem = default_view.Name

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
        png = export_preview(view)
        if png and os.path.isfile(png):
            self.preview_image.Source = self._bitmap(png)
            self.preview_label.Text = name
        else:
            self.preview_image.Source = None
            self.preview_label.Text = u'{0}  (no preview available)'.format(name)

    def on_push(self, sender, args):
        picks = list(self.view_list.SelectedItems)
        cat = (self.category_box.Text or '').strip()
        if not picks or not cat:
            forms.alert("Pick at least one view and a category first.", title="Push to Library")
            return
        if not verify_password(self.password_box.Password):
            forms.alert("Incorrect password.", title="Push to Library")
            self.password_box.Password = ""
            return
        self.selected_views = [self.name_to_view[p] for p in picks if p in self.name_to_view]
        self.category = cat
        self.Close()

    def on_cancel(self, sender, args):
        self.selected_views = []
        self.Close()


# ---------- push a single view (used per-selection below) ----------

def push_one(sv, category, cpo):
    """Push one drafting view to the library as its own file. Returns
    (filename_or_None, error_string_or_None). Always closes the temporary
    blank document it creates, even on failure, so nothing is left open in
    the Revit session."""
    base_name = sanitize_filename(sv.Name)
    version = next_version(folder, category, base_name)
    filename = u"{0}_v{1}_{2}_{3}.rvt".format(base_name, version, username, date_str)

    cat_dir = os.path.join(folder, category)
    try:
        if not os.path.isdir(cat_dir):
            os.makedirs(cat_dir)
    except Exception as ex:
        return None, u"could not create category folder: {0}".format(ex)

    out_path = os.path.join(cat_dir, filename)

    newdoc = None
    try:
        units = DB.UnitSystem.Imperial
        try:
            du = doc.DisplayUnitSystem
            units = DB.UnitSystem.Metric if du == DB.DisplayUnit.METRIC else DB.UnitSystem.Imperial
        except Exception:
            pass
        newdoc = app.NewProjectDocument(units)

        nv = None
        copy_err = None
        with revit.Transaction("Prepare pushed detail", doc=newdoc):
            nv, copy_err = copy_view_with_contents(doc, sv, newdoc, cpo)
            if nv is not None:
                try:
                    nv.ViewTemplateId = DB.ElementId.InvalidElementId
                except Exception:
                    pass
                try:
                    nv.DetailLevel = DB.ViewDetailLevel.Fine
                except Exception:
                    pass

        if nv is None:
            return None, u"could not prepare the detail: {0}".format(copy_err)

        count = len(list(DB.FilteredElementCollector(newdoc, nv.Id).WhereElementIsNotElementType()))

        save_opts = DB.SaveAsOptions()
        save_opts.OverwriteExistingFile = True
        newdoc.SaveAs(out_path, save_opts)

        relpath = os.path.relpath(out_path, folder).replace('\\', '/')
        thumb = export_thumb(newdoc, nv, folder, relpath)

        catalog = load_catalog(folder)
        key = u"{0}::{1}".format(relpath, nv.Name)
        catalog['entries'][key] = {
            "relpath": relpath,
            "view_name": nv.Name,
            "category": category,
            "mtime": os.path.getmtime(out_path),
            "thumb": thumb,
            "base_name": base_name,
            "version": version,
            "pushed_by": username,
            "pushed_date": date_str,
        }
        save_catalog(folder, catalog)

        if count == 0:
            return filename, u"pushed, but 0 elements ended up in the view -- check the source has content"
        return filename, None
    except Exception as ex:
        logger.debug('push failed for {0}: {1}'.format(sv.Name, ex))
        return None, u"{0}".format(ex)
    finally:
        if newdoc is not None:
            try:
                newdoc.Close(False)
            except Exception as ex:
                logger.debug('could not close pushed document: {0}'.format(ex))


# ---------- main ----------

folder = get_saved_folder() or pick_and_save_folder()
if not folder:
    forms.alert("No library folder selected.", title="Push to Library", exitscript=True)

views = collect_drafting_views(doc)
if not views:
    forms.alert("No drafting views found in this project.", title="Push to Library", exitscript=True)

active_view = None
try:
    if isinstance(uidoc.ActiveView, DB.ViewDrafting):
        active_view = uidoc.ActiveView
except Exception:
    pass

win = PushWindow(XAML_FILE, views, list_categories(folder), active_view)
win.show_dialog()
picked_views = win.selected_views
category = win.category
if not picked_views or not category:
    script.exit()

username = None
try:
    username = app.Username
except Exception:
    pass
username = re.sub(r'[^A-Za-z0-9]+', '', username or '') or "user"
date_str = datetime.datetime.now().strftime('%Y%m%d')

cpo = DB.CopyPasteOptions()
cpo.SetDuplicateTypeNamesHandler(_DupTypeHandler())

pushed = []
failed = []
for sv in picked_views:
    filename, note = push_one(sv, category, cpo)
    if filename and not note:
        pushed.append(filename)
    elif filename and note:
        pushed.append(u"{0}  ({1})".format(filename, note))
    else:
        failed.append(u"{0}  -- {1}".format(sv.Name, note))

msg = u""
if pushed:
    msg += u"Pushed:\n" + u"\n".join([u"  - {0}".format(p) for p in pushed])
if failed:
    msg += u"\n\nFailed:\n" + u"\n".join([u"  - {0}".format(f) for f in failed])
forms.alert(u"Category: {0}\n\n{1}".format(category, msg.strip()), title="Push to Library")

