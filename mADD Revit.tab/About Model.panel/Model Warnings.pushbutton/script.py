# -*- coding: utf-8 -*-
"""Model Warnings
Opens a window listing every warning currently in the model, grouped by
warning type so repeated instances (e.g. "Room is not in a properly
enclosed region" x40) are collapsed into one entry with a count instead
of forty separate lines. Click a warning type to see exactly which
elements it applies to; click one of those elements to select it, zoom
to it in the real model view, and see a live preview image right in this
window. "Select All" selects every element tied to a warning type at
once. "Export CSV" writes the full list out for QA/QC records.
"""
from __future__ import print_function
import sys, csv, traceback, os, glob, tempfile, shutil, json

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from pyrevit import forms, revit, script
from Autodesk.Revit.DB import (
    ElementId, ImageExportOptions, ExportRange, ZoomFitType,
    ImageFileType, ImageResolution, XYZ, BoundingBoxXYZ,
    OverrideGraphicSettings, Color, FillPatternElement, Transaction,
    FilteredElementCollector, ViewPlan, ViewType, Level, ViewDuplicateOption
)
from System.Collections.Generic import List
from System.Windows.Controls import ListBoxItem, CheckBox, StackPanel, TextBlock, Orientation
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Media import SolidColorBrush, Colors
from System.Windows import Visibility, Thickness, FontWeights, TextWrapping
from System import Uri, UriKind

uidoc = revit.uidoc
doc   = revit.doc
logger = script.get_logger()
config = script.get_config('ModelWarningsQC')
PROJECT_KEY = doc.Title or "untitled"


# ----------------------------------------------------------------------
# Severity classification (heuristic - Revit's own GetWarnings() doesn't
# expose a useful severity level, they're all just "Warning")
# ----------------------------------------------------------------------
SEVERITY_RULES = [
    # (level, color, keywords) - checked in order, first match wins
    (3, "#D32F2F", ["overlap", "not automatically joined", "not joined",
                     "could not create", "conflict", "attached to, but miss",
                     "highlighted walls", "highlighted floors", "highlighted lines"]),
    (2, "#F57C00", ["duplicate", "identical instances", "off axis", "slightly"]),
]
DEFAULT_SEVERITY = (1, "#757575")   # informational / everything else

def classify_severity(desc):
    d = desc.lower()
    for level, color, keywords in SEVERITY_RULES:
        for kw in keywords:
            if kw in d:
                return level, color
    return DEFAULT_SEVERITY


# ----------------------------------------------------------------------
# Persistent "reviewed" tracking (per project, saved across sessions)
# ----------------------------------------------------------------------
def load_reviewed():
    try:
        raw = config.get_option('reviewed_json', '{}')
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return set(data.get(PROJECT_KEY, []))

def save_reviewed(reviewed_set):
    try:
        raw = config.get_option('reviewed_json', '{}')
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    data[PROJECT_KEY] = list(reviewed_set)
    config.reviewed_json = json.dumps(data)
    script.save_config()


# ----------------------------------------------------------------------
# Gather + group warnings
# ----------------------------------------------------------------------
def element_label(eid):
    el = doc.GetElement(eid)
    if el is None:
        return ("(deleted)", "", eid.IntegerValue)
    try:
        cat = el.Category.Name if el.Category else "N/A"
    except Exception:
        cat = "N/A"
    try:
        nm = el.Name
    except Exception:
        nm = ""
    return (cat, nm or "(unnamed)", eid.IntegerValue)


_solid_fill_id = None
def find_solid_fill_pattern_id():
    global _solid_fill_id
    if _solid_fill_id is not None:
        return _solid_fill_id
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            if fp.GetFillPattern().IsSolidFill:
                _solid_fill_id = fp.Id
                return _solid_fill_id
        except Exception:
            pass
    return None


def get_element_level_id(el, bb):
    """The element's own level if it has one, otherwise the nearest level
    at or below its bounding box (works for most host/hosted elements
    even when LevelId isn't directly set)."""
    try:
        lid = getattr(el, 'LevelId', None)
        if lid is not None and lid != ElementId.InvalidElementId:
            return lid
    except Exception:
        pass
    if bb is not None:
        try:
            z = bb.Min.Z
            best, best_dz = None, None
            for lvl in FilteredElementCollector(doc).OfClass(Level):
                dz = z - lvl.Elevation
                if dz >= -1.0 and (best_dz is None or dz < best_dz):
                    best, best_dz = lvl, dz
            if best is not None:
                return best.Id
        except Exception:
            pass
    return None


def find_plan_view_for_level(level_id):
    if level_id is None:
        return None
    for v in FilteredElementCollector(doc).OfClass(ViewPlan):
        try:
            if v.IsTemplate:
                continue
            if v.ViewType not in (ViewType.FloorPlan, ViewType.CeilingPlan):
                continue
            if v.GenLevel and v.GenLevel.Id == level_id:
                return v
        except Exception:
            pass
    return None


def generate_preview(eid):
    """Build a scratch preview independent of whatever view is currently
    open: duplicate the real floor/ceiling plan for the element's own
    level, crop that duplicate TIGHT to the element, paint the element
    bright red, export it, then roll the whole transaction back so
    nothing - the duplicate view, the crop, the highlight - is ever kept.
    Falls back to the active view only if no matching level/plan exists.
    Returns the file path, or None if a preview couldn't be produced."""
    tmp_dir = None
    t = Transaction(doc, "Preview (temporary, always rolled back)")
    t.Start()
    try:
        el = doc.GetElement(eid)
        try:
            uidoc.Selection.SetElementIds(List[ElementId]([eid]))
        except Exception:
            pass

        # --- bounding box, with a location-point fallback for elements
        # that don't have one (e.g. unenclosed/unplaced rooms) ---
        bb = None
        if el is not None:
            try:
                bb = el.get_BoundingBox(None)
            except Exception:
                bb = None
        if bb is None and el is not None:
            try:
                loc = el.Location
                pt = None
                if loc is not None:
                    if hasattr(loc, 'Point') and loc.Point is not None:
                        pt = loc.Point
                    elif hasattr(loc, 'Curve') and loc.Curve is not None:
                        pt = loc.Curve.Evaluate(0.5, True)
                if pt is not None:
                    half = 8.0
                    bb = BoundingBoxXYZ()
                    bb.Min = XYZ(pt.X - half, pt.Y - half, pt.Z - half)
                    bb.Max = XYZ(pt.X + half, pt.Y + half, pt.Z + half)
            except Exception as ex:
                logger.debug('location fallback failed for {0}: {1}'.format(eid, ex))
        if bb is None:
            return None

        # --- find the right level's plan and duplicate it - a disposable
        # scratch copy, so the real view is never touched ---
        level_id = get_element_level_id(el, bb)
        base_view = find_plan_view_for_level(level_id)
        prev_view = None
        if base_view is not None:
            try:
                prev_view = doc.GetElement(base_view.Duplicate(ViewDuplicateOption.Duplicate))
            except Exception as ex:
                logger.debug('scratch view duplicate failed: {0}'.format(ex))
        if prev_view is None:
            prev_view = uidoc.ActiveView   # last-resort fallback only
        if prev_view is None:
            return None

        # --- paint the element bright red ---
        try:
            ogs = OverrideGraphicSettings()
            red = Color(255, 0, 0)
            ogs.SetProjectionLineColor(red)
            ogs.SetProjectionLineWeight(8)
            solid_id = find_solid_fill_pattern_id()
            if solid_id is not None:
                ogs.SetSurfaceForegroundPatternId(solid_id)
                ogs.SetSurfaceForegroundPatternColor(red)
                ogs.SetCutForegroundPatternId(solid_id)
                ogs.SetCutForegroundPatternColor(red)
            prev_view.SetElementOverrides(eid, ogs)
        except Exception as ex:
            logger.debug('highlight override failed for {0}: {1}'.format(eid, ex))

        # --- crop TIGHT to the element's own bounding box, regardless of
        # whatever the view's own crop/zoom was set to beforehand ---
        try:
            size = max(bb.Max.X - bb.Min.X, bb.Max.Y - bb.Min.Y, 1.0)
            pad = max(size * 0.6, 4.0)
            cb = prev_view.CropBox
            inv = cb.Transform.Inverse
            corners = [XYZ(x, y, bb.Min.Z)
                       for x in (bb.Min.X, bb.Max.X) for y in (bb.Min.Y, bb.Max.Y)]
            us = [inv.OfPoint(c).X for c in corners]
            vs = [inv.OfPoint(c).Y for c in corners]
            cb.Min = XYZ(min(us) - pad, min(vs) - pad, cb.Min.Z)
            cb.Max = XYZ(max(us) + pad, max(vs) + pad, cb.Max.Z)
            prev_view.CropBox = cb
            prev_view.CropBoxActive = True
            prev_view.CropBoxVisible = False
        except Exception as ex:
            logger.debug('crop failed for {0}: {1}'.format(eid, ex))

        doc.Regenerate()

        # --- export THIS view directly by id - no dependency on the
        # active UI view or whatever it currently happens to show ---
        tmp_dir = tempfile.mkdtemp(prefix='warn_prev_')
        opts = ImageExportOptions()
        opts.ExportRange = ExportRange.SetOfViews
        opts.SetViewsAndSheets(List[ElementId]([prev_view.Id]))
        opts.ZoomType = ZoomFitType.FitToPage
        opts.PixelSize = 900
        opts.HLRandWFViewsFileType = ImageFileType.PNG
        opts.ImageResolution = ImageResolution.DPI_300
        opts.FilePath = os.path.join(tmp_dir, 'p')
        doc.ExportImage(opts)

        hits = glob.glob(os.path.join(tmp_dir, '*.png'))
        return hits[0] if hits else None
    except Exception as ex:
        logger.debug('preview generation failed for {0}: {1}'.format(eid, ex))
        return None
    finally:
        # ALWAYS discard the scratch view, crop, and highlight - the model
        # must come out of this exactly as it went in.
        t.RollBack()


def load_bitmap(path):
    bmp = BitmapImage()
    bmp.BeginInit()
    bmp.UriSource = Uri(path, UriKind.Absolute)
    bmp.CacheOption = BitmapCacheOption.OnLoad   # loads fully so file can be deleted after
    bmp.EndInit()
    bmp.Freeze()
    return bmp


warnings = list(doc.GetWarnings())
if not warnings:
    forms.alert("No warnings found in this model. Nice and clean!",
                title="Model Warnings", warn_icon=False)
    sys.exit(0)

# description -> {"count": int, "elem_ids": set(ElementId)}
groups = {}
for w in warnings:
    try:
        desc = w.GetDescriptionText() or "(no description provided)"
    except Exception:
        desc = "(no description provided)"
    try:
        eids = list(w.GetFailingElements())
    except Exception:
        eids = []
    g = groups.setdefault(desc, {"count": 0, "elem_ids": set()})
    g["count"] += 1
    for eid in eids:
        g["elem_ids"].add(eid)

sorted_descs = sorted(groups.keys(),
                      key=lambda d: (-classify_severity(d)[0], -groups[d]["count"]))
total_unique_elements = len(set().union(*[g["elem_ids"] for g in groups.values()])) \
    if groups else 0


# ----------------------------------------------------------------------
# Window
# ----------------------------------------------------------------------
xaml_file = script.get_bundle_file("ui.xaml")

class WarningsWindow(forms.WPFWindow):
    def __init__(self, xaml_source):
        forms.WPFWindow.__init__(self, xaml_source)
        self.reviewed = load_reviewed()
        reviewed_now = len([d for d in sorted_descs if d in self.reviewed])
        self.txtSummary.Text = (u"{0} warning instance(s) across {1} warning "
                                 u"type(s), affecting {2} unique element(s). "
                                 u"{3} type(s) already marked reviewed."
                                 ).format(len(warnings), len(groups),
                                          total_unique_elements, reviewed_now)
        self._populate_groups()

    def _populate_groups(self):
        self.lstGroups.Items.Clear()
        hide_reviewed = bool(self.chkHideReviewed.IsChecked)
        for desc in sorted_descs:
            if hide_reviewed and desc in self.reviewed:
                continue
            g = groups[desc]
            level, color = classify_severity(desc)

            row = StackPanel()
            row.Orientation = Orientation.Horizontal

            cb = CheckBox()
            cb.IsChecked = desc in self.reviewed
            cb.Margin = Thickness(0, 0, 6, 0)
            cb.Tag = desc
            cb.Click += self.chkReviewed_Click

            txt = TextBlock()
            txt.Text = u"[{0}]  {1}".format(g["count"], desc)
            txt.TextWrapping = TextWrapping.NoWrap
            brush = SolidColorBrush()
            try:
                brush = self._brush_from_hex(color)
            except Exception:
                pass
            txt.Foreground = brush
            if level == 3:
                txt.FontWeight = FontWeights.Bold

            row.Children.Add(cb)
            row.Children.Add(txt)

            lbi = ListBoxItem()
            lbi.Content = row
            lbi.Tag = desc
            self.lstGroups.Items.Add(lbi)

    def _brush_from_hex(self, hex_color):
        from System.Windows.Media import ColorConverter
        c = ColorConverter.ConvertFromString(hex_color)
        return SolidColorBrush(c)

    def chkReviewed_Click(self, sender, args):
        desc = sender.Tag
        if sender.IsChecked:
            self.reviewed.add(desc)
        else:
            self.reviewed.discard(desc)
        save_reviewed(self.reviewed)
        if self.chkHideReviewed.IsChecked:
            self._populate_groups()   # re-filter immediately if hiding reviewed ones

    def chkHideReviewed_Changed(self, sender, args):
        self._populate_groups()

    def _current_group_desc(self):
        sel = self.lstGroups.SelectedItem
        return sel.Tag if sel else None

    def lstGroups_SelectionChanged(self, sender, args):
        desc = self._current_group_desc()
        self.lstElements.Items.Clear()
        self._clear_preview()
        if not desc:
            return
        eids = sorted(groups[desc]["elem_ids"], key=lambda e: e.IntegerValue)
        for eid in eids:
            cat, nm, idnum = element_label(eid)
            lbi = ListBoxItem()
            lbi.Content = u"{0}  |  {1}  |  id {2}".format(cat, nm, idnum)
            lbi.Tag = eid
            self.lstElements.Items.Add(lbi)
        if self.lstElements.Items.Count > 0:
            self.lstElements.SelectedIndex = 0   # auto-preview the first one

    def _clear_preview(self):
        self.imgPreview.Source = None
        self.txtPreviewCaption.Text = u""
        self.txtPreviewStatus.Visibility = Visibility.Visible
        self.txtPreviewStatus.Text = "Pick a warning, then an element, to see a preview here."

    def lstElements_SelectionChanged(self, sender, args):
        sel = self.lstElements.SelectedItem
        if not sel:
            self._clear_preview()
            return
        eid = sel.Tag
        cat, nm, idnum = element_label(eid)
        self.txtPreviewCaption.Text = u"{0}  |  {1}  |  id {2}".format(cat, nm, idnum)
        self.imgPreview.Source = None
        self.txtPreviewStatus.Visibility = Visibility.Visible
        self.txtPreviewStatus.Text = "Loading preview..."

        path = generate_preview(eid)
        if path:
            try:
                self.imgPreview.Source = load_bitmap(path)
                self.txtPreviewStatus.Visibility = Visibility.Collapsed
            except Exception as ex:
                logger.debug('preview load failed: {0}'.format(ex))
                self.txtPreviewStatus.Text = "Preview failed to load."
            finally:
                try:
                    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
                except Exception:
                    pass
        else:
            self.txtPreviewStatus.Text = ("No preview available - couldn't find a "
                                          "matching plan view for this element's "
                                          "level (it has still been selected).")

    def btnSelectAll_Click(self, sender, args):
        desc = self._current_group_desc()
        if not desc:
            forms.alert("Pick a warning type on the left first.",
                        title="Model Warnings", warn_icon=True)
            return
        eids = list(groups[desc]["elem_ids"])
        if not eids:
            forms.alert("This warning type has no associated elements to select.",
                        title="Model Warnings", warn_icon=True)
            return
        try:
            id_list = List[ElementId](eids)
            uidoc.Selection.SetElementIds(id_list)
            uidoc.ShowElements(id_list)
        except Exception as ex:
            forms.alert(u"Could not select elements:\n{0}".format(ex),
                        title="Model Warnings", warn_icon=True)

    def btnExport_Click(self, sender, args):
        path = forms.save_file(file_ext='csv')
        if not path:
            return
        try:
            with open(path, 'wb') as f:
                writer = csv.writer(f)
                writer.writerow(["Warning", "Severity", "Reviewed", "Instance Count",
                                  "Element Category", "Element Name", "Element Id"])
                for desc in sorted_descs:
                    g = groups[desc]
                    level, _ = classify_severity(desc)
                    sev_label = {3: "Critical", 2: "Moderate", 1: "Info"}.get(level, "Info")
                    rev_label = "Yes" if desc in self.reviewed else "No"
                    eids = sorted(g["elem_ids"], key=lambda e: e.IntegerValue)
                    if not eids:
                        writer.writerow([desc, sev_label, rev_label, g["count"], "", "", ""])
                        continue
                    for eid in eids:
                        cat, nm, idnum = element_label(eid)
                        writer.writerow([desc, sev_label, rev_label, g["count"], cat, nm, idnum])
            forms.alert(u"Exported to:\n{0}".format(path),
                        title="Model Warnings", warn_icon=False)
        except Exception as ex:
            forms.alert(u"Export failed:\n{0}".format(ex),
                        title="Model Warnings", warn_icon=True)

    def btnClose_Click(self, sender, args):
        self.Close()


try:
    win = WarningsWindow(xaml_file)
    win.ShowDialog()
except Exception as ex:
    forms.alert(u"Model Warnings failed to open:\n{0}\n\n{1}".format(
                ex, traceback.format_exc()),
                title="Model Warnings", warn_icon=True)
