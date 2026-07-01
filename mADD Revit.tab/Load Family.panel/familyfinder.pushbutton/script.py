# -*- coding: utf-8 -*-
"""Browse the family library as a LAZY folder tree: only top-level folders are
built at startup; a folder's contents are built the first time it is expanded,
so opening is instant even with thousands of families. Tick families to
multi-select, preview from the thumbnail cache (rendered on first view), and
load the checked families. Search flattens to a filtered list across the whole
library. Folder set with "Set Family Folder"."""

from pyrevit import revit, DB, forms, script
import os
import re
import glob
import shutil
import hashlib
import tempfile

from System.Collections.Generic import List as NetList
from System import Uri, UriKind
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Controls import TreeViewItem, CheckBox

doc = revit.doc
uidoc = revit.uidoc
try:
    app = __revit__.Application
except Exception:
    from pyrevit import HOST_APP
    app = HOST_APP.app

logger = script.get_logger()
config = script.get_config('LoadDetailsLibrary')
XAML_FILE = script.get_bundle_file('ui.xaml')

_BACKUP = re.compile(r'\.\d{4}\.rfa$', re.IGNORECASE)


# ---------- shared helpers (identical to other buttons) ----------

def get_lib_dir():
    p = getattr(config, 'family_library_dir', None)
    return p if (p and os.path.isdir(p)) else None


def get_cache_dir(root):
    candidate = os.path.join(root, '_thumbnails')
    try:
        if not os.path.isdir(candidate):
            os.makedirs(candidate)
        testf = os.path.join(candidate, '.wtest')
        with open(testf, 'w') as f:
            f.write('x')
        os.remove(testf)
        return candidate
    except Exception:
        base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
        tag = hashlib.sha1(os.path.normcase(root).encode('utf-8')).hexdigest()[:12]
        alt = os.path.join(base, 'LoadDetails', 'thumbs', tag)
        try:
            if not os.path.isdir(alt):
                os.makedirs(alt)
        except Exception:
            pass
        return alt


def cache_path_for(rfa_path, cache_dir):
    h = hashlib.sha1(os.path.normcase(rfa_path).encode('utf-8')).hexdigest()
    return os.path.join(cache_dir, h + '.png')


def is_fresh(cache_png, rfa_path):
    try:
        return (os.path.isfile(cache_png)
                and os.path.getmtime(cache_png) >= os.path.getmtime(rfa_path))
    except Exception:
        return False


def scan_families(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        for f in files:
            if not f.lower().endswith('.rfa'):
                continue
            if _BACKUP.search(f):
                continue
            name = f[:-4]
            rel = os.path.relpath(dirpath, root)
            category = '(root)' if rel == '.' else rel
            out.append((name, category, os.path.join(dirpath, f)))
    return out


def build_tree(items):
    """Nested dict: node = {'subs': {name: node}, 'files': [(name, path)]}."""
    root = {'subs': {}, 'files': []}
    for name, category, path in items:
        node = root
        if category != '(root)':
            for part in category.split(os.sep):
                node = node['subs'].setdefault(part, {'subs': {}, 'files': []})
        node['files'].append((name, path))
    return root


def pick_preview_view(famdoc):
    views = [v for v in DB.FilteredElementCollector(famdoc).OfClass(DB.View)
             if not v.IsTemplate]
    order = (DB.ViewType.ThreeD, DB.ViewType.Elevation, DB.ViewType.FloorPlan,
             DB.ViewType.Detail, DB.ViewType.DraftingView, DB.ViewType.CeilingPlan)
    for t in order:
        cands = [v for v in views if v.ViewType == t]
        if cands:
            if t == DB.ViewType.Elevation:
                fronts = [v for v in cands if 'front' in v.Name.lower()]
                if fronts:
                    return fronts[0]
            return cands[0]
    return views[0] if views else None


def live_render_to_cache(path, cache_png):
    famdoc = None
    opened = False
    try:
        for d in app.Documents:
            try:
                if d.PathName and os.path.normcase(d.PathName) == os.path.normcase(path):
                    famdoc = d
                    break
            except Exception:
                pass
        if famdoc is None:
            mp = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(path)
            famdoc = app.OpenDocumentFile(mp, DB.OpenOptions())
            opened = True
        view = pick_preview_view(famdoc)
        if view is None:
            return None
        tmp = tempfile.mkdtemp(prefix='fam_prev_')
        opts = DB.ImageExportOptions()
        opts.ExportRange = DB.ExportRange.SetOfViews
        opts.SetViewsAndSheets(NetList[DB.ElementId]([view.Id]))
        opts.ZoomType = DB.ZoomFitType.FitToPage
        opts.PixelSize = 400
        opts.HLRandWFViewsFileType = DB.ImageFileType.PNG
        opts.ImageResolution = DB.ImageResolution.DPI_72
        opts.FilePath = os.path.join(tmp, 'p')
        famdoc.ExportImage(opts)
        hits = glob.glob(os.path.join(tmp, '*.png'))
        if not hits:
            return None
        try:
            cdir = os.path.dirname(cache_png)
            if not os.path.isdir(cdir):
                os.makedirs(cdir)
            shutil.copyfile(hits[0], cache_png)
            return cache_png
        except Exception:
            return hits[0]
    except Exception as ex:
        logger.debug('live render failed for {0}: {1}'.format(path, ex))
        return None
    finally:
        if opened and famdoc is not None:
            try:
                famdoc.Close(False)
            except Exception:
                pass


# ---------- family load options (silent overwrite) ----------

class _FamLoadOptions(DB.IFamilyLoadOptions):
    def OnFamilyFound(self, familyInUse, overwriteParameterValues):
        return (True, False)

    def OnSharedFamilyFound(self, sharedFamily, familyInUse, source, overwriteParameterValues):
        return (True, DB.FamilySource.Family, False)


# ---------- WPF window ----------

class FamilyWindow(forms.WPFWindow):
    def __init__(self, xaml_file, items, cache_dir):
        forms.WPFWindow.__init__(self, xaml_file)
        self.cache_dir = cache_dir
        self.flat_items = sorted(items, key=lambda x: (x[1].lower(), x[0].lower()))
        self.path_to_name = dict((p, n) for n, c, p in self.flat_items)
        self.checked_paths = set()
        self.selected_paths = []
        self.current_path = None
        self._syncing = False
        # Only ever render one preview at a time. (Ticking a checkbox no longer
        # renders anything -- that was the real freeze cause.)
        self._rendering = False
        self._current_cb = None   # the one checkbox currently ticked (single-select)
        self.tree_root = build_tree(self.flat_items)
        self._show_tree()

    def _preview_guarded(self, path, name):
        """Show cached preview instantly; otherwise render this one family once,
        skipping if a render is already running so clicks can't pile up."""
        cpng = cache_path_for(path, self.cache_dir)
        if is_fresh(cpng, path):
            self._show(cpng, name)
            return
        if self._rendering:
            return
        self._rendering = True
        try:
            self.preview_label.Text = u'{0}  (rendering...)'.format(name)
            self._show(live_render_to_cache(path, cpng), name)
        finally:
            self._rendering = False

    # ----- leaf / folder builders -----
    def _family_checkbox(self, label, path):
        cb = CheckBox()
        cb.Content = label
        cb.Tag = path
        cb.IsChecked = path in self.checked_paths
        cb.Checked += self.on_check
        cb.Unchecked += self.on_check
        return cb

    def _family_item(self, name, path, label=None):
        tvi = TreeViewItem()
        tvi.Header = self._family_checkbox(label if label else name, path)
        tvi.Tag = path                    # non-None Tag = family node
        return tvi

    def _folder_item(self, folder_name, node):
        """Build a folder node WITHOUT its children -- add a dummy so the
        expand arrow shows; real children are built on first expand."""
        tvi = TreeViewItem()
        tvi.Header = folder_name
        tvi.Tag = node                    # dict Tag = unexpanded folder
        tvi.IsExpanded = False
        if node['subs'] or node['files']:
            tvi.Items.Add(TreeViewItem())  # dummy placeholder
        tvi.Expanded += self.on_folder_expand
        return tvi

    def _populate_folder(self, tvi):
        """Replace the dummy with real children (once)."""
        node = tvi.Tag
        if not isinstance(node, dict):
            return                         # already populated (Tag cleared)
        tvi.Items.Clear()
        for sub in sorted(node['subs'].keys(), key=lambda s: s.lower()):
            tvi.Items.Add(self._folder_item(sub, node['subs'][sub]))
        for name, path in sorted(node['files'], key=lambda x: x[0].lower()):
            tvi.Items.Add(self._family_item(name, path))
        tvi.Tag = '__folder__'             # mark populated (Tag no longer a dict)

    # ----- mode switching -----
    def _show_tree(self):
        self.tree.Items.Clear()
        for sub in sorted(self.tree_root['subs'].keys(), key=lambda s: s.lower()):
            self.tree.Items.Add(self._folder_item(sub, self.tree_root['subs'][sub]))
        for name, path in sorted(self.tree_root['files'], key=lambda x: x[0].lower()):
            self.tree.Items.Add(self._family_item(name, path))
        self.status_text.Text = u"{0} families  |  {1} checked".format(
            len(self.flat_items), len(self.checked_paths))

    def _show_search(self, q):
        self.tree.Items.Clear()
        toks = q.lower().split()
        count = 0
        for name, category, path in self.flat_items:
            hay = (name + u' ' + category).lower()
            if all(t in hay for t in toks):
                cat = category.replace(os.sep, ' / ')
                label = u"{0}    [{1}]".format(name, cat)
                self.tree.Items.Add(self._family_item(name, path, label=label))
                count += 1
                if count >= 500:           # cap for responsiveness; keep typing to narrow
                    break
        more = '  (showing first 500 - refine search)' if count >= 500 else ''
        self.status_text.Text = u"{0} matches{1}  |  {2} checked".format(
            count, more, len(self.checked_paths))

    # ----- preview -----
    def _bitmap(self, path):
        bi = BitmapImage()
        bi.BeginInit()
        bi.CacheOption = BitmapCacheOption.OnLoad
        bi.UriSource = Uri(path, UriKind.Absolute)
        bi.EndInit()
        return bi

    def _show(self, png, name):
        if png and os.path.isfile(png):
            self.preview_image.Source = self._bitmap(png)
            self.preview_label.Text = name
        else:
            self.preview_image.Source = None
            self.preview_label.Text = u'{0}  (no preview available)'.format(name)

    def _preview(self, path, name):
        cpng = cache_path_for(path, self.cache_dir)
        if is_fresh(cpng, path):
            self._show(cpng, name)
        else:
            self.preview_label.Text = u'{0}  (rendering...)'.format(name)
            self._show(live_render_to_cache(path, cpng), name)

    # ----- events -----
    def on_folder_expand(self, sender, args):
        self._populate_folder(sender)

    def on_search(self, sender, args):
        q = (self.search_box.Text or '').strip()
        if not q:
            self._show_tree()
        else:
            self._show_search(q)

    def on_tree_select(self, sender, args):
        item = args.NewValue
        if item is None:
            return
        tag = item.Tag
        if not isinstance(tag, str) or tag == '__folder__':
            return                         # folder node -> no preview
        # family node: Tag is the file path (a str), but not the marker
        if tag == '__folder__':
            return
        self.current_path = tag
        self._preview_guarded(tag, item.Header.Content)

    def on_check(self, sender, args):
        if self._syncing:
            return
        if sender.IsChecked:
            # single-select: uncheck whatever was ticked before
            if self._current_cb is not None and self._current_cb is not sender:
                self._syncing = True
                try:
                    self._current_cb.IsChecked = False
                finally:
                    self._syncing = False
            self._current_cb = sender
            self.checked_paths = set([sender.Tag])
            self.current_path = sender.Tag
            self._preview_guarded(sender.Tag, sender.Content)
        else:
            self.checked_paths.discard(sender.Tag)
            if self._current_cb is sender:
                self._current_cb = None
        self.status_text.Text = u"{0} checked".format(len(self.checked_paths))

    def on_import(self, sender, args):
        picks = list(self.checked_paths)
        if not picks and self.current_path:
            picks = [self.current_path]
        self.selected_paths = [(self.path_to_name.get(
            p, os.path.splitext(os.path.basename(p))[0]), p) for p in picks]
        self.Close()

    def on_cancel(self, sender, args):
        self.selected_paths = []
        self.Close()


# ---------- main ----------

root = get_lib_dir()
if not root:
    forms.alert('No family library folder set.\nUse the "Set Family Folder" '
                'button first.', title="Import Family", exitscript=True)

items = scan_families(root)
if not items:
    forms.alert("No .rfa families found under:\n{0}".format(root),
                title="Import Family", exitscript=True)

cache_dir = get_cache_dir(root)

win = FamilyWindow(XAML_FILE, items, cache_dir)
win.show_dialog()
picks = win.selected_paths
if not picks:
    script.exit()

# LoadFamily manages its own transaction -- do NOT wrap it in one.
opts = _FamLoadOptions()
loaded, skipped, failed = [], [], []
last_family = None
total = len(picks)
with forms.ProgressBar(title='Loading families... ({value} of {max_value})',
                       cancellable=True) as pb:
    for i, (name, path) in enumerate(picks, 1):
        if pb.cancelled:
            break
        try:
            res = doc.LoadFamily(path, opts)
            # IronPython returns the out-param too, as (bool, Family)
            if isinstance(res, tuple):
                ok = bool(res[0])
                fam = res[1] if len(res) > 1 else None
            else:
                ok = bool(res)
                fam = None
            if ok:
                loaded.append(name)
                if fam is not None:
                    last_family = fam
            else:
                skipped.append(name)
        except Exception as ex:
            logger.debug('load failed for {0}: {1}'.format(path, ex))
            failed.append(name)
        pb.update_progress(i, total)

# --- try to jump straight into placing it (only makes sense for one family) ---
placed_prompt = False
if len(loaded) == 1 and last_family is not None:
    try:
        sym_ids = list(last_family.GetFamilySymbolIds())
        if sym_ids:
            t = DB.Transaction(doc, "Activate family type")
            t.Start()
            sym = doc.GetElement(sym_ids[0])
            if not sym.IsActive:
                sym.Activate()
            doc.Regenerate()
            t.Commit()
            uidoc.PostRequestForElementTypePlacement(sym)
            placed_prompt = True
    except Exception as ex:
        logger.debug('auto-place skipped: {0}'.format(ex))

lines = [u"Loaded: {0}".format(len(loaded))]
if loaded:
    lines.append(u"  - " + u"\n  - ".join(loaded))
if skipped:
    lines.append(u"Already present: {0}".format(len(skipped)))
if failed:
    lines.append(u"Failed: {0}\n  - {1}".format(len(failed), u"\n  - ".join(failed)))
if loaded and not placed_prompt:
    lines.append(u"\nFind them in Project Browser > Families. To place one, use "
                 u"Architecture > Component (or the matching tool) and pick it "
                 u"from the Type Selector.")
elif placed_prompt:
    lines.append(u"\nReady to place - click in the model to drop it in.")
forms.alert(u"\n".join(lines), title="Import Family")
