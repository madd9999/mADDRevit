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

# Same fixed GUID as DraftingView.pushbutton -- both buttons share the same
# stored 'Imported' view type via Extensible Storage on Project Information.
_SCHEMA_GUID = Guid("d7d9a9e2-6f1a-4b3c-9c2e-2a1f7e6b5c40")
_SCHEMA_NAME = "LoadDetailsLibrary_ImportedViewType"
_FIELD_NAME = "ImportedVFTId"


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
    transaction. Shared with DraftingView.pushbutton (same Extensible
    Storage schema) so both tools file their output under the same type.
    Priority order:
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


def cleanup_stray_views(tdoc, before_ids, keep_id):
    """Safety net: delete any extra ViewDrafting Revit may have created as a
    side effect of the copy that isn't the view we actually wanted."""
    after_ids = set(v.Id.IntegerValue for v in
                     DB.FilteredElementCollector(tdoc).OfClass(DB.ViewDrafting))
    stray = after_ids - before_ids - {keep_id.IntegerValue}
    for sid in stray:
        try:
            tdoc.Delete(DB.ElementId(sid))
        except Exception:
            pass


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
    one un-copyable element can't blank the whole view.
    Returns a list of error strings (empty if the bulk copy raised no
    exception). Does NOT report a count here -- CopyElements' return value
    isn't a reliable signal of what actually landed in dst_view, so the
    caller measures the real post-copy element count instead."""
    ids = detail_member_ids(srcdoc, srcview)
    if not ids:
        return ["no 2D detailing elements matched the keep/drop category filters"]

    net = NetList[DB.ElementId](ids)
    try:
        DB.ElementTransformUtils.CopyElements(
            srcview, net, dst_view, DB.Transform.Identity, cpo)
        return []
    except Exception as ex:
        bulk_err = u"{0}".format(ex)
        logger.debug('bulk copy failed, per-element fallback: {0}'.format(ex))

    ok = 0
    errors = []
    for eid in ids:
        single = NetList[DB.ElementId]()
        single.Add(eid)
        try:
            DB.ElementTransformUtils.CopyElements(
                srcview, single, dst_view, DB.Transform.Identity, cpo)
            ok += 1
        except Exception as ex:
            errors.append(u"{0}".format(ex))
    if ok == 0 and not errors:
        errors.append(bulk_err)
    elif ok == 0:
        errors.insert(0, u"bulk copy failed: {0}".format(bulk_err))
    return errors


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

    base_vft = get_drafting_vft(doc)
    if not base_vft:
        forms.alert("No Drafting view type found in the current model.",
                    title="Import Section / Detail", exitscript=True)

    cpo = DB.CopyPasteOptions()
    cpo.SetDuplicateTypeNamesHandler(_DupTypeHandler())

    results = []
    empties = []
    with revit.Transaction("Import Section/Detail (2D)", doc=doc):
        try:
            vft = get_or_create_imported_vft(doc, base_vft)
        except Exception as ex:
            forms.alert(u"Could not create/find the 'Imported' view type:\n{0}".format(ex),
                        title="Import Section / Detail", exitscript=True)

        # If this type inherited a 'Default View Template' from base_vft,
        # every new view made from it would silently get that template's
        # Visibility/Graphics overrides -- which can hide everything even
        # though the content copied correctly. Strip it so nothing hides it.
        try:
            vft.DefaultTemplateId = DB.ElementId.InvalidElementId
        except Exception:
            pass

        for sv in picked:
            before_ids = set(v.Id.IntegerValue for v in
                              DB.FilteredElementCollector(doc).OfClass(DB.ViewDrafting))
            try:
                nv = DB.ViewDrafting.Create(doc, to_element_id(vft))
                # A brand-new element can be not-yet-fully-recognized by
                # Revit's internals within the same transaction; regenerate
                # so it's a fully real view before we paste content into it.
                doc.Regenerate()
                set_unique_name(nv, doc, sv.Name)
                try:
                    nv.Scale = sv.Scale
                except Exception:
                    pass
                # Belt-and-suspenders: make sure the new view itself has no
                # view template applied either.
                try:
                    nv.ViewTemplateId = DB.ElementId.InvalidElementId
                except Exception:
                    pass
                try:
                    nv.DetailLevel = DB.ViewDetailLevel.Fine
                except Exception:
                    pass
                errors = copy_detail_contents(sv, srcdoc, nv, cpo)

                cleanup_stray_views(doc, before_ids, nv.Id)

                # Ground truth: count what's actually IN the view now, rather
                # than trusting whatever CopyElements' return value implied.
                count = len(list(DB.FilteredElementCollector(doc, nv.Id).WhereElementIsNotElementType()))

                results.append((nv.Name, count))
                if count == 0:
                    reason = errors[0] if errors else "0 elements ended up in the view"
                    empties.append(u"{0}  -- 0 copied: {1}".format(nv.Name, reason))
            except Exception as ex:
                logger.debug('failed importing {0}: {1}'.format(sv.Name, ex))
                cleanup_stray_views(doc, before_ids, DB.ElementId.InvalidElementId)
                empties.append(u"{0}  (failed: {1})".format(sv.Name, ex))

    msg = "\n".join([u"  - {0}  ({1} detail elements)".format(n, c) for n, c in results])
    if empties:
        msg += u"\n\nIssues:\n" + u"\n".join([u"  - {0}".format(s) for s in empties])
    forms.alert(u"Imported {0} view(s) as drafting views:\n{1}".format(len(results), msg),
                title="Import Section / Detail")
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
