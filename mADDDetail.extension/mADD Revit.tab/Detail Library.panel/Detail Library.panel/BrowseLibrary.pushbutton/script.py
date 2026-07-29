# -*- coding: utf-8 -*-
"""Browse the shared Detail Library folder -- one small .rvt per detail,
organized in category subfolders -- and import the selected detail(s), with
their content, into the current project. Maintains a catalog.json cache
(with cached thumbnails) in the library folder so browsing stays fast even
with many files; only files that are new or changed since the last browse
get re-opened and re-indexed."""

from pyrevit import revit, DB, forms, script
import os
import re
import json
import glob
import tempfile
import hashlib
import clr
from collections import defaultdict

from System.Collections.Generic import List as NetList
from System import Uri, UriKind, Guid, Int32
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from Autodesk.Revit.DB.ExtensibleStorage import Schema, SchemaBuilder, Entity, AccessLevel

doc = revit.doc
try:
    app = __revit__.Application
except Exception:
    from pyrevit import HOST_APP
    app = HOST_APP.app

logger = script.get_logger()

# Separate config namespace from the older single-file "Load Details" tool.
config = script.get_config('DetailLibraryFolder')

XAML_FILE = script.get_bundle_file('ui.xaml')

# Same fixed GUID as the single-file Import buttons, so everything files
# into the SAME 'Imported' drafting view type regardless of which tool
# brought it in.
_SCHEMA_GUID = Guid("d7d9a9e2-6f1a-4b3c-9c2e-2a1f7e6b5c40")
_SCHEMA_NAME = "LoadDetailsLibrary_ImportedViewType"
_FIELD_NAME = "ImportedVFTId"
IMPORTED_VFT_NAME = "Imported"

CATALOG_NAME = "catalog.json"
THUMB_DIR = "_thumbnails"


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


# ---------- delete password ----------
# Same fixed password as Push to Library -- required to delete details.
PUSH_PASSWORD = "madd"


def verify_password(password):
    return (password or "") == PUSH_PASSWORD


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


# ---------- catalog ----------

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
        # Shared file may be locked by someone else mid-write -- not fatal,
        # this session just uses its in-memory version; next refresh by
        # anyone will retry the write.
        logger.debug('could not write catalog: {0}'.format(ex))


def find_rvt_files(folder):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('_') and not d.startswith('.')]
        for fn in files:
            if fn.lower().endswith('.rvt') and not fn.startswith('~'):
                out.append(os.path.join(root, fn))
    return out


def relpath_of(folder, full):
    return os.path.relpath(full, folder).replace('\\', '/')


def category_of(relpath):
    parts = relpath.split('/')
    return parts[0] if len(parts) > 1 else "(uncategorized)"


_VERSION_RE = re.compile(r'^(.*)_v(\d+)(?:_.*)?$')


def parse_base_name(filename_no_ext):
    m = _VERSION_RE.match(filename_no_ext)
    if m:
        try:
            return m.group(1), int(m.group(2))
        except Exception:
            pass
    return filename_no_ext, 1


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
    tmp_folder = tempfile.mkdtemp(prefix='dl_thumb_')
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
        logger.debug('thumb export failed for {0}: {1}'.format(view.Name, ex))
    return None


def index_file(folder, full_path, catalog):
    relpath = relpath_of(folder, full_path)
    mtime = os.path.getmtime(full_path)
    category = category_of(relpath)
    base, ver = parse_base_name(os.path.splitext(os.path.basename(full_path))[0])

    srcdoc, _ = get_or_open(full_path)
    if not srcdoc:
        return
    try:
        views = [v for v in DB.FilteredElementCollector(srcdoc).OfClass(DB.ViewDrafting)
                 if not v.IsTemplate]
        seen_names = []
        for v in views:
            seen_names.append(v.Name)
            key = u"{0}::{1}".format(relpath, v.Name)
            thumb = export_thumb(srcdoc, v, folder, relpath)
            catalog['entries'][key] = {
                "relpath": relpath,
                "view_name": v.Name,
                "category": category,
                "mtime": mtime,
                "thumb": thumb,
                "base_name": base,
                "version": ver,
            }
        stale = [k for k, e in catalog['entries'].items()
                 if e.get('relpath') == relpath and e.get('view_name') not in seen_names]
        for k in stale:
            del catalog['entries'][k]
    finally:
        if srcdoc is not doc:
            try:
                srcdoc.Close(False)
            except Exception as ex:
                logger.debug('could not close {0}: {1}'.format(full_path, ex))


def refresh_catalog(folder):
    catalog = load_catalog(folder)
    files = find_rvt_files(folder)
    known_relpaths = set(relpath_of(folder, f) for f in files)
    changed = False

    cached_by_relpath = {}
    for e in catalog['entries'].values():
        cached_by_relpath[e.get('relpath')] = e

    for full_path in files:
        relpath = relpath_of(folder, full_path)
        mtime = os.path.getmtime(full_path)
        cached = cached_by_relpath.get(relpath)
        if cached is None or abs(cached.get('mtime', -1) - mtime) > 1.0:
            index_file(folder, full_path, catalog)
            changed = True

    to_delete = [k for k, e in catalog['entries'].items() if e.get('relpath') not in known_relpaths]
    for k in to_delete:
        del catalog['entries'][k]
        changed = True

    if changed:
        save_catalog(folder, catalog)
    return catalog


# ---------- 'Imported' view type (shared with the single-file tool) ----------

def to_element_id(x):
    return x if isinstance(x, DB.ElementId) else x.Id


def get_drafting_vft(tdoc):
    for vft in DB.FilteredElementCollector(tdoc).OfClass(DB.ViewFamilyType):
        try:
            if vft.ViewFamily == DB.ViewFamily.Drafting:
                return vft
        except Exception:
            pass
    return None


def _get_or_build_schema():
    existing = Schema.Lookup(_SCHEMA_GUID)
    if existing:
        return existing
    sb = SchemaBuilder(_SCHEMA_GUID)
    sb.SetSchemaName(_SCHEMA_NAME)
    sb.SetReadAccessLevel(AccessLevel.Public)
    sb.SetWriteAccessLevel(AccessLevel.Public)
    sb.AddSimpleField(_FIELD_NAME, Int32)
    return sb.Finish()


def load_stored_vft_id(tdoc):
    schema = Schema.Lookup(_SCHEMA_GUID)
    if not schema:
        return None
    try:
        pinfo = tdoc.ProjectInformation
        ent = pinfo.GetEntity(schema)
        if ent is None or not ent.IsValid():
            return None
        val = ent.Get[Int32](_FIELD_NAME)
        if val:
            return DB.ElementId(val)
    except Exception as ex:
        logger.debug('could not read stored vft id: {0}'.format(ex))
    return None


def store_vft_id(tdoc, eid):
    try:
        schema = _get_or_build_schema()
        pinfo = tdoc.ProjectInformation
        ent = Entity(schema)
        ent.Set[Int32](_FIELD_NAME, eid.IntegerValue)
        pinfo.SetEntity(ent)
    except Exception as ex:
        logger.debug('could not store vft id: {0}'.format(ex))


def find_imported_vft(tdoc):
    for vft in DB.FilteredElementCollector(tdoc).OfClass(DB.ViewFamilyType):
        try:
            if vft.ViewFamily == DB.ViewFamily.Drafting and vft.Name.startswith(IMPORTED_VFT_NAME):
                return vft
        except Exception:
            pass
    return None


def get_or_create_imported_vft(tdoc, base_vft):
    stored_id = load_stored_vft_id(tdoc)
    if stored_id:
        el = tdoc.GetElement(stored_id)
        if isinstance(el, DB.ViewFamilyType):
            return el

    existing = find_imported_vft(tdoc)
    if existing:
        store_vft_id(tdoc, existing.Id)
        return existing

    last_ex = None
    for i in range(1, 51):
        name = IMPORTED_VFT_NAME if i == 1 else u"{0} ({1})".format(IMPORTED_VFT_NAME, i)
        try:
            new_id = to_element_id(base_vft.Duplicate(name))
            new_vft = tdoc.GetElement(new_id)
            store_vft_id(tdoc, new_id)
            return new_vft
        except Exception as ex:
            last_ex = ex
            found = find_imported_vft(tdoc)
            if found:
                store_vft_id(tdoc, found.Id)
                return found
            continue
    raise last_ex


# ---------- copy machinery (same proven technique as the single-file tool) ----------

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


def set_unique_name(nv, tdoc, base):
    try:
        nv.Name = unique_view_name(tdoc, base)
        return
    except Exception:
        pass
    import random
    for _ in range(5):
        try:
            nv.Name = u"{0} ({1})".format(base, random.randint(1000, 9999))
            return
        except Exception:
            continue


def member_ids(srcdoc, srcview):
    ids = []
    col = DB.FilteredElementCollector(srcdoc, srcview.Id).WhereElementIsNotElementType()
    for e in col:
        if e.Id == srcview.Id:
            continue
        ids.append(e.Id)
    return ids


def _find_view_by_exact_name(tdoc, name):
    for v in DB.FilteredElementCollector(tdoc).OfClass(DB.View):
        try:
            if v.Name == name:
                return v
        except Exception:
            pass
    return None


def copy_view_with_contents(srcdoc, srcview, tdoc, cpo):
    """Copy the drafting view AND its owned content together in one
    document-to-document call, so Revit recreates them as one related unit.
    Handles the case where a view of the same name already exists in tdoc
    by temporarily renaming it out of the way for the duration of the copy.
    Returns (new_view_or_None, error_string_or_None)."""
    ids = [srcview.Id]
    ids.extend(member_ids(srcdoc, srcview))
    net = NetList[DB.ElementId](ids)

    conflict = _find_view_by_exact_name(tdoc, srcview.Name)
    conflict_original_name = None
    if conflict:
        try:
            conflict_original_name = conflict.Name
            conflict.Name = u"{0}__tmp_{1}".format(conflict_original_name, conflict.Id.IntegerValue)
        except Exception:
            conflict = None

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
    finally:
        if conflict and conflict_original_name:
            try:
                conflict.Name = conflict_original_name
            except Exception:
                pass

    return new_view, err


def cleanup_stray_views(tdoc, before_ids, keep_id):
    after_ids = set(v.Id.IntegerValue for v in
                     DB.FilteredElementCollector(tdoc).OfClass(DB.ViewDrafting))
    stray = after_ids - before_ids - {keep_id.IntegerValue}
    for sid in stray:
        try:
            tdoc.Delete(DB.ElementId(sid))
        except Exception:
            pass


class _DupTypeHandler(DB.IDuplicateTypeNamesHandler):
    def OnDuplicateTypeNamesFound(self, args):
        return DB.DuplicateTypeAction.UseDestinationTypes


# ---------- WPF window ----------

class BrowseWindow(forms.WPFWindow):
    def __init__(self, xaml_file, folder, catalog):
        forms.WPFWindow.__init__(self, xaml_file)
        self.folder = folder
        self.catalog = catalog
        self.all_entries = sorted(
            catalog['entries'].values(),
            key=lambda e: (e.get('category', ''), e.get('view_name', '').lower()))
        self.label_to_entry = {}
        self._rebuild_list(self.all_entries)
        self.selected_entries = []

    def _label(self, e):
        return u"[{0}] {1}  (v{2})".format(
            e.get('category', '?'), e.get('view_name', '?'), e.get('version', 1))

    def _rebuild_list(self, entries):
        self.label_to_entry = {}
        labels = []
        for e in entries:
            lbl = self._label(e)
            self.label_to_entry[lbl] = e
            labels.append(lbl)
        self.view_list.ItemsSource = labels

    def _current_filtered(self):
        q = (self.search_box.Text or '').strip().lower()
        if not q:
            return self.all_entries
        return [e for e in self.all_entries
                if q in e.get('view_name', '').lower() or q in e.get('category', '').lower()]

    def on_search_changed(self, sender, args):
        self._rebuild_list(self._current_filtered())

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
        e = self.label_to_entry.get(name)
        if not e:
            return
        thumb_rel = e.get('thumb')
        shown = False
        if thumb_rel:
            thumb_path = os.path.join(self.folder, thumb_rel)
            if os.path.isfile(thumb_path):
                self.preview_image.Source = self._bitmap(thumb_path)
                self.preview_label.Text = self._label(e)
                shown = True
        if not shown:
            self.preview_image.Source = None
            self.preview_label.Text = u'{0}  (no preview available)'.format(self._label(e))

    def on_import(self, sender, args):
        picks = list(self.view_list.SelectedItems)
        self.selected_entries = [self.label_to_entry[p] for p in picks if p in self.label_to_entry]
        self.Close()

    def on_delete(self, sender, args):
        picks = list(self.view_list.SelectedItems)
        entries = [self.label_to_entry[p] for p in picks if p in self.label_to_entry]
        if not entries:
            forms.alert("Select at least one detail to delete.", title="Browse Detail Library")
            return
        if not verify_password(self.password_box.Password):
            forms.alert("Incorrect password.", title="Browse Detail Library")
            self.password_box.Password = ""
            return

        names = u"\n".join([u"  - {0}".format(self._label(e)) for e in entries])
        confirmed = forms.alert(
            u"Delete {0} detail(s) from the library? This removes the underlying "
            u".rvt file(s) and cannot be undone:\n\n{1}".format(len(entries), names),
            title="Browse Detail Library", ok=False, yes=True, no=True)
        if not confirmed:
            return

        # Group by source file -- deleting removes the whole file, since
        # each mini .rvt is meant to hold one detail.
        by_relpath = defaultdict(list)
        for e in entries:
            by_relpath[e['relpath']].append(e)

        removed_relpaths = []
        failed = []
        for relpath, ents in by_relpath.items():
            full_path = os.path.join(self.folder, relpath)
            try:
                if os.path.isfile(full_path):
                    os.remove(full_path)
                for e in ents:
                    thumb_rel = e.get('thumb')
                    if thumb_rel:
                        thumb_path = os.path.join(self.folder, thumb_rel)
                        try:
                            if os.path.isfile(thumb_path):
                                os.remove(thumb_path)
                        except Exception:
                            pass
                removed_relpaths.append(relpath)
            except Exception as ex:
                failed.append(u"{0}  ({1})".format(relpath, ex))

        if removed_relpaths:
            to_delete_keys = [k for k, e in self.catalog['entries'].items()
                               if e.get('relpath') in removed_relpaths]
            for k in to_delete_keys:
                del self.catalog['entries'][k]
            save_catalog(self.folder, self.catalog)

            self.all_entries = [e for e in self.all_entries if e.get('relpath') not in removed_relpaths]
            self._rebuild_list(self._current_filtered())
            self.preview_image.Source = None
            self.preview_label.Text = "Select a detail to preview"

        if failed:
            forms.alert(u"Some files could not be deleted (they may be open elsewhere):\n\n" +
                        u"\n".join([u"  - {0}".format(f) for f in failed]),
                        title="Browse Detail Library")

    def on_cancel(self, sender, args):
        self.selected_entries = []
        self.Close()


# ---------- import ----------

def do_import(folder, entries, cpo, vft):
    by_file = defaultdict(list)
    for e in entries:
        by_file[e['relpath']].append(e)

    results = []
    skipped = []
    for relpath, ents in by_file.items():
        full_path = os.path.join(folder, relpath)
        srcdoc, _ = get_or_open(full_path)
        if not srcdoc:
            for e in ents:
                skipped.append(u"{0} (could not open source file)".format(e['view_name']))
            continue
        try:
            views_by_name = {}
            for v in DB.FilteredElementCollector(srcdoc).OfClass(DB.ViewDrafting):
                if not v.IsTemplate:
                    views_by_name[v.Name] = v
            for e in ents:
                sv = views_by_name.get(e['view_name'])
                if not sv:
                    skipped.append(u"{0} (view no longer found in {1})".format(e['view_name'], relpath))
                    continue
                before_ids = set(x.Id.IntegerValue for x in
                                  DB.FilteredElementCollector(doc).OfClass(DB.ViewDrafting))
                nv, err = copy_view_with_contents(srcdoc, sv, doc, cpo)
                if nv is None:
                    cleanup_stray_views(doc, before_ids, DB.ElementId.InvalidElementId)
                    skipped.append(u"{0} (failed: {1})".format(e['view_name'], err))
                    continue
                set_unique_name(nv, doc, sv.Name)
                doc.Regenerate()
                try:
                    nv.ChangeTypeId(to_element_id(vft))
                except Exception:
                    pass
                try:
                    nv.ViewTemplateId = DB.ElementId.InvalidElementId
                except Exception:
                    pass
                try:
                    nv.DetailLevel = DB.ViewDetailLevel.Fine
                except Exception:
                    pass
                cleanup_stray_views(doc, before_ids, nv.Id)
                count = len(list(DB.FilteredElementCollector(doc, nv.Id).WhereElementIsNotElementType()))
                results.append((nv.Name, count))
                if count == 0:
                    skipped.append(u"{0}  -- 0 elements ended up in the view".format(nv.Name))
        finally:
            if srcdoc is not doc:
                try:
                    srcdoc.Close(False)
                except Exception as ex:
                    logger.debug('could not close {0}: {1}'.format(full_path, ex))
    return results, skipped


# ---------- main ----------

folder = get_saved_folder() or pick_and_save_folder()
if not folder:
    forms.alert("No library folder selected.", title="Browse Detail Library", exitscript=True)

catalog = refresh_catalog(folder)
if not catalog['entries']:
    forms.alert("No detail views found in:\n{0}".format(folder),
                title="Browse Detail Library", exitscript=True)

win = BrowseWindow(XAML_FILE, folder, catalog)
win.show_dialog()
picked = win.selected_entries
if not picked:
    script.exit()

base_vft = get_drafting_vft(doc)
if not base_vft:
    forms.alert("No Drafting view type found in the current model.",
                title="Browse Detail Library", exitscript=True)

cpo = DB.CopyPasteOptions()
cpo.SetDuplicateTypeNamesHandler(_DupTypeHandler())

with revit.Transaction("Import from Detail Library", doc=doc):
    try:
        vft = get_or_create_imported_vft(doc, base_vft)
    except Exception as ex:
        forms.alert(u"Could not create/find the 'Imported' view type:\n{0}".format(ex),
                    title="Browse Detail Library", exitscript=True)
    try:
        vft.DefaultTemplateId = DB.ElementId.InvalidElementId
    except Exception:
        pass
    doc.Regenerate()

    results, skipped = do_import(folder, picked, cpo, vft)

msg = "\n".join([u"  - {0}  ({1} elements)".format(n, c) for n, c in results])
if skipped:
    msg += u"\n\nIssues:\n" + u"\n".join([u"  - {0}".format(s) for s in skipped])
forms.alert(u"Imported {0} view(s):\n{1}".format(len(results), msg),
            title="Browse Detail Library")
