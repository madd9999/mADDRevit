# -*- coding: utf-8 -*-
"""Preview drafting views from the saved library model and import the
selected one(s) -- WITH their detail contents -- into the current document."""

from pyrevit import revit, DB, forms, script
import os
import tempfile
import glob
import clr

from System.Collections.Generic import List as NetList
from System import Uri, UriKind, Guid, Int32
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from Autodesk.Revit.DB.ExtensibleStorage import Schema, SchemaBuilder, Entity, AccessLevel

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

# Fixed GUID identifying our Extensible Storage schema, used to remember the
# ElementId of the 'Imported' view type inside the project file itself, so we
# never have to re-search/re-guess for it on later runs -- avoids any chance
# of creating a second type due to a name-matching mismatch.
_SCHEMA_GUID = Guid("d7d9a9e2-6f1a-4b3c-9c2e-2a1f7e6b5c40")
_SCHEMA_NAME = "LoadDetailsLibrary_ImportedViewType"
_FIELD_NAME = "ImportedVFTId"


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

IMPORTED_VFT_NAME = "Imported"


def get_drafting_vft(tdoc):
    """Any Drafting ViewFamilyType -- used as a template to duplicate from."""
    for vft in DB.FilteredElementCollector(tdoc).OfClass(DB.ViewFamilyType):
        try:
            if vft.ViewFamily == DB.ViewFamily.Drafting:
                return vft
        except Exception:
            pass
    return None


def to_element_id(x):
    """Coerce a value that should represent an element into a plain
    ElementId, whether the API/pythonnet handed us the id itself or the
    element wrapping it."""
    return x if isinstance(x, DB.ElementId) else x.Id


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
    """Read the Imported view type's ElementId back from the project file
    itself (stored on Project Information). Returns None if never stored."""
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
    """Remember the Imported view type's ElementId inside the project file
    so future runs never need to search or re-create it."""
    try:
        schema = _get_or_build_schema()
        pinfo = tdoc.ProjectInformation
        ent = Entity(schema)
        ent.Set[Int32](_FIELD_NAME, eid.IntegerValue)
        pinfo.SetEntity(ent)
    except Exception as ex:
        logger.debug('could not store vft id: {0}'.format(ex))


def find_imported_vft(tdoc):
    """Fallback name search -- only used if nothing has been stored yet
    (e.g. a model that used an earlier version of this tool)."""
    for vft in DB.FilteredElementCollector(tdoc).OfClass(DB.ViewFamilyType):
        try:
            if vft.ViewFamily == DB.ViewFamily.Drafting and vft.Name.startswith(IMPORTED_VFT_NAME):
                return vft
        except Exception:
            pass
    return None


def get_or_create_imported_vft(tdoc, base_vft):
    """Return the 'Imported' Drafting view type. Must be called inside a
    transaction. Priority order:
      1. Read the ElementId we stored last time -- if it still resolves to a
         valid ViewFamilyType, use it directly. No searching, no ambiguity.
      2. Otherwise fall back to a name search (covers models from an earlier
         version of this tool that never stored an id).
      3. Otherwise create it, trying 'Imported', 'Imported (2)', etc. until
         Revit accepts one -- then store its id for every future run."""
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
    """Rename nv to a unique name, retrying with a random suffix in the rare
    case Revit still rejects the computed name (stale/parallel changes)."""
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
    """All non-type elements owned by the source drafting view, minus the
    view element itself."""
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
    """Copy the drafting view AND its owned content together, in a single
    document-to-document CopyElements call. Passing the view id alongside
    its member ids in the SAME call lets Revit recreate them as one related
    unit -- a new view, correctly owning its new content -- rather than us
    creating an empty view and asking Revit to paste content into it across
    two different documents (which was landing content elsewhere, e.g. the
    active view of the destination document, instead of the intended view).

    Revit assigns the new view the same Name as the source view, and throws
    if that name is already taken in the destination. Since we don't control
    that name choice, we work around it here: if a view with that exact name
    already exists, temporarily rename it out of the way, do the copy, then
    restore its original name.

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
    """Safety net: delete any extra ViewDrafting Revit may have created as a
    side effect that isn't the view we actually wanted."""
    after_ids = set(v.Id.IntegerValue for v in
                     DB.FilteredElementCollector(tdoc).OfClass(DB.ViewDrafting))
    stray = after_ids - before_ids - {keep_id.IntegerValue}
    for sid in stray:
        try:
            tdoc.Delete(DB.ElementId(sid))
        except Exception:
            pass


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

    base_vft = get_drafting_vft(doc)
    if not base_vft:
        forms.alert("No Drafting view type found in the current model.",
                    title="Import Drafting View", exitscript=True)

    cpo = DB.CopyPasteOptions()
    cpo.SetDuplicateTypeNamesHandler(_DupTypeHandler())

    results = []
    skipped = []
    with revit.Transaction("Import Drafting View(s)", doc=doc):
        try:
            vft = get_or_create_imported_vft(doc, base_vft)
        except Exception as ex:
            forms.alert(u"Could not create/find the 'Imported' view type:\n{0}".format(ex),
                        title="Import Drafting View", exitscript=True)

        # If this type inherited a 'Default View Template' from base_vft,
        # every new view made from it would silently get that template's
        # Visibility/Graphics overrides -- which can hide everything even
        # though the content copied correctly. Strip it so nothing hides it.
        try:
            vft.DefaultTemplateId = DB.ElementId.InvalidElementId
        except Exception:
            pass
        doc.Regenerate()

        for sv in picked:
            before_ids = set(v.Id.IntegerValue for v in
                              DB.FilteredElementCollector(doc).OfClass(DB.ViewDrafting))
            try:
                nv, copy_err = copy_view_with_contents(srcdoc, sv, doc, cpo)
                if nv is None:
                    cleanup_stray_views(doc, before_ids, DB.ElementId.InvalidElementId)
                    skipped.append(u"{0}  (failed: {1})".format(sv.Name, copy_err))
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

                # Ground truth: count what's actually IN the view now, rather
                # than trusting whatever CopyElements' return value implied.
                count = len(list(DB.FilteredElementCollector(doc, nv.Id).WhereElementIsNotElementType()))

                results.append((nv.Name, count))
                if count == 0:
                    skipped.append(u"{0}  -- 0 elements ended up in the view".format(nv.Name))
            except Exception as ex:
                logger.debug('failed importing {0}: {1}'.format(sv.Name, ex))
                cleanup_stray_views(doc, before_ids, DB.ElementId.InvalidElementId)
                skipped.append(u"{0}  (failed: {1})".format(sv.Name, ex))

    msg = "\n".join([u"  - {0}  ({1} elements)".format(n, c) for n, c in results])
    if skipped:
        msg += u"\n\nIssues:\n" + u"\n".join([u"  - {0}".format(s) for s in skipped])
    forms.alert(u"Imported {0} view(s):\n{1}".format(len(results), msg),
                title="Import Drafting View")
finally:
    # The library file is a background reference, not something the user
    # works in directly -- always close it when we're done, even if it was
    # already open (e.g. left open by an earlier run), so it doesn't keep
    # accumulating as an open document in the Revit session.
    if srcdoc is not doc:
        try:
            srcdoc.Close(False)
        except Exception as ex:
            logger.debug('could not close library document: {0}'.format(ex))
