# -*- coding: utf-8 -*-
"""Shared helpers for the Load Details panel.

Put this file at <YourExtension>.extension/lib/loaddetails_common.py --
pyRevit automatically adds any 'lib' folder at the extension root to
sys.path, so every button (SetLibraryFile, DraftingView, Sectionview) can
do:

    import loaddetails_common as ld

WHY THIS FILE EXISTS (the fix for the lag)
-------------------------------------------
Each button click in pyRevit can spin up a *new* IronPython engine, so a
plain module-level dict does NOT survive between clicks. The old code's
`get_or_open()` checked app.Documents for an already-open copy of the
library file -- which is a good idea -- but the `finally: srcdoc.Close(False)`
at the end of every script closed it again immediately. Net effect: an old
file that needs upgrading pays that full cost on EVERY click, not just the
first.

pyRevit ships a small, documented mechanism for exactly this situation --
`script.get_envvar` / `script.set_envvar` -- which stores small values in
the Revit process's AppDomain, so they persist across separate script runs
for the lifetime of the Revit session (until Revit closes or the extension
reloads). We use it to remember which Document is already open for a given
file path, so repeat clicks reuse it instantly instead of reopening it.
"""
import os
import re
import hashlib
import tempfile
import glob

from pyrevit import DB, script

try:
    app = __revit__.Application
except Exception:
    from pyrevit import HOST_APP
    app = HOST_APP.app

logger = script.get_logger()

CONFIG_SECTION = 'LoadDetailsLibrary'
_OPENDOCS_ENVVAR = 'LoadDetailsLibrary_OpenDocs'


# ---------------------------------------------------------------
# saved library path (same config both buttons already used)
# ---------------------------------------------------------------

def get_config():
    return script.get_config(CONFIG_SECTION)


def get_saved_path():
    cfg = get_config()
    p = getattr(cfg, 'library_rvt_path', None)
    return p if (p and os.path.isfile(p)) else None


def save_path(path):
    cfg = get_config()
    cfg.library_rvt_path = path
    script.save_config()


def pick_path(title="Select Library Model (.rvt)"):
    import clr
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import OpenFileDialog, DialogResult
    dlg = OpenFileDialog()
    dlg.Filter = "Revit Project (*.rvt)|*.rvt"
    dlg.Title = title
    if dlg.ShowDialog() == DialogResult.OK:
        save_path(dlg.FileName)
        return dlg.FileName
    return None


# ---------------------------------------------------------------
# background-document cache
# ---------------------------------------------------------------

def _registry():
    reg = script.get_envvar(_OPENDOCS_ENVVAR)
    if reg is None:
        reg = {}
        script.set_envvar(_OPENDOCS_ENVVAR, reg)
    return reg


def get_or_open(path):
    """Return (document, opened_fresh).

    Reuses a cached / already-open Document whenever possible. This is the
    important bit for old files: the slow open+upgrade only happens the
    FIRST time a given file is picked in a Revit session. Every click after
    that reuses the same in-memory Document.
    """
    norm = os.path.normcase(path)
    reg = _registry()

    cached = reg.get(norm)
    if cached is not None:
        try:
            if cached.IsValidObject:
                return cached, False
        except Exception:
            pass
        reg.pop(norm, None)

    # fall back to scanning open documents (e.g. user opened it manually)
    for d in app.Documents:
        try:
            if d.PathName and os.path.normcase(d.PathName) == norm:
                reg[norm] = d
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

    d = app.OpenDocumentFile(mp, opts)
    reg[norm] = d
    return d, True


def close_cached(path=None):
    """Close and evict cached background document(s).

    Call this:
      - from SetLibraryFile.pushbutton, right after the user picks a NEW
        library file, passing the OLD path (frees the old file's memory).
      - from an optional 'Clear Library Cache' button if you want a manual
        escape hatch.

    path=None closes everything that's cached.
    """
    reg = _registry()
    targets = [os.path.normcase(path)] if path else list(reg.keys())
    for norm in targets:
        d = reg.pop(norm, None)
        if d is None:
            continue
        try:
            if d.IsValidObject:
                d.Close(False)
        except Exception:
            pass
    script.set_envvar(_OPENDOCS_ENVVAR, reg)


# ---------------------------------------------------------------
# disk-backed preview cache (survives across engine reloads AND is
# shared between DraftingView and Sectionview instead of each button
# keeping its own throwaway in-memory dict)
# ---------------------------------------------------------------

def _cache_root():
    root = os.path.join(tempfile.gettempdir(), 'pyrevit_loaddetails_previews')
    if not os.path.isdir(root):
        os.makedirs(root)
    return root


def _cache_key(lib_path, view):
    """Keyed by file path + file's last-modified time + view id, so a
    stale preview from a since-changed library file is never reused."""
    try:
        mtime = os.path.getmtime(lib_path)
    except OSError:
        mtime = 0
    raw = u"{0}|{1}|{2}".format(lib_path, mtime, view.Id.IntegerValue)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def get_cached_preview(lib_path, view):
    key = _cache_key(lib_path, view)
    hit = os.path.join(_cache_root(), key + '.png')
    return hit if os.path.isfile(hit) else None


def export_preview(srcdoc, lib_path, view):
    """Export (or reuse a cached) PNG preview for a view. Returns a file
    path or None."""
    cached = get_cached_preview(lib_path, view)
    if cached:
        return cached

    key = _cache_key(lib_path, view)
    dest = os.path.join(_cache_root(), key + '.png')

    folder = tempfile.mkdtemp(prefix='dv_prev_')
    opts = DB.ImageExportOptions()
    from System.Collections.Generic import List as NetList
    opts.ExportRange = DB.ExportRange.SetOfViews
    opts.SetViewsAndSheets(NetList[DB.ElementId]([view.Id]))
    opts.ZoomType = DB.ZoomFitType.FitToPage
    opts.PixelSize = 1200
    opts.HLRandWFViewsFileType = DB.ImageFileType.PNG
    opts.ImageResolution = DB.ImageResolution.DPI_72
    opts.FilePath = os.path.join(folder, 'preview')

    try:
        srcdoc.ExportImage(opts)
        hits = glob.glob(os.path.join(folder, '*.png'))
        if hits:
            if os.path.isfile(dest):
                os.remove(dest)
            os.rename(hits[0], dest)
            return dest
    except Exception as ex:
        logger.debug('preview export failed: {0}'.format(ex))
    return None


# ---------------------------------------------------------------
# small shared helpers used by both import buttons
# ---------------------------------------------------------------

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


class DupTypeHandler(DB.IDuplicateTypeNamesHandler):
    def OnDuplicateTypeNamesFound(self, args):
        return DB.DuplicateTypeAction.UseDestinationTypes
