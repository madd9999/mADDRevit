# -*- coding: utf-8 -*-
"""Text Organizer
Automatically aligns, spaces, and (optionally) re-cases all TextNotes in the
active view.

- Leaders are preserved because notes are moved (not recreated) - Revit
  keeps the leader arrow attached to its target and only the text box
  position changes.
- Notes are stacked in the same top-to-bottom order as their leader
  targets, which is what keeps the leader lines from crossing each other.
- Vertical spacing nudges a note down only enough to stop it overlapping
  the note above it, so text never overlaps either.

Supported views: Floor Plans, Ceiling Plans, Sections, Elevations,
Drafting Views, Detail Views.
"""
from __future__ import print_function
import sys, re, traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, TextNote, Transaction, ElementTransformUtils,
    XYZ, ViewType
)
from pyrevit import forms, revit, script

uidoc = revit.uidoc
doc   = revit.doc

# ----------------------------------------------------------------------
# Tunables - calibrated from sample office sheets (roof/casework/wall
# details): notes are left-justified, ALL CAPS, and NOT forced into a
# rigid equal-spacing grid - they stay near their natural/leader-target
# height and only get nudged apart when two would otherwise overlap.
# GAP_RATIO is relative to note height so it scales with view/annotation
# scale instead of being a fixed real-world distance.
# ----------------------------------------------------------------------
GAP_RATIO = 0.45   # buffer between stacked notes, as a fraction of note height

SUPPORTED_VIEW_TYPES = (
    ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.Section,
    ViewType.Elevation, ViewType.DraftingView, ViewType.Detail,
)


# ----------------------------------------------------------------------
# Settings dialog
# ----------------------------------------------------------------------
xaml_file = script.get_bundle_file("ui.xaml")

class SettingsWindow(forms.WPFWindow):
    def __init__(self, xaml_source):
        forms.WPFWindow.__init__(self, xaml_source)
        self.result = None

    def btnApply_Click(self, sender, args):
        self.result = {
            "align": ("Left" if self.rbAlignLeft.IsChecked else
                       "Right" if self.rbAlignRight.IsChecked else "Center"),
            "auto_space": bool(self.chkAutoSpace.IsChecked),
            "prevent_overlap": bool(self.chkPreventOverlap.IsChecked),
            "text_format": ("None" if self.rbFmtNone.IsChecked else
                             "CAPS" if self.rbFmtCaps.IsChecked else
                             "lower" if self.rbFmtLower.IsChecked else
                             "Title" if self.rbFmtTitle.IsChecked else "Sentence"),
            "scope": ("All" if self.rbScopeAll.IsChecked else
                      "Left" if self.rbScopeLeft.IsChecked else "Right"),
        }
        self.Close()

    def btnCancel_Click(self, sender, args):
        self.result = None
        self.Close()


# ----------------------------------------------------------------------
# Text formatting helpers
# ----------------------------------------------------------------------
def to_title_case(s):
    return u" ".join(w[:1].upper() + w[1:] if w else w for w in s.split(u" "))

def to_sentence_case(s):
    """Capitalize the first letter after each sentence-ending punctuation."""
    def cap_match(m):
        return m.group(1) + m.group(2).upper()
    s = s.lower()
    # capitalize very first character
    s = s[:1].upper() + s[1:] if s else s
    # capitalize after '.', '!', '?' followed by whitespace
    s = re.sub(r'([.!?]\s+)([a-z])', cap_match, s)
    return s

def apply_text_format(text, mode):
    if not text:
        return text
    if mode == "CAPS":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode == "Title":
        return to_title_case(text)
    if mode == "Sentence":
        return to_sentence_case(text)
    return text


# ----------------------------------------------------------------------
# Geometry helpers
# View.get_BoundingBox already returns coordinates in the view's own
# 2D system: X = view's Right direction, Y = view's Up direction.
# ----------------------------------------------------------------------
def note_bbox_uv(note, view):
    bb = note.get_BoundingBox(view)
    if not bb:
        return None
    return bb.Min.X, bb.Max.X, bb.Min.Y, bb.Max.Y   # left, right, bottom, top

def leader_target_v(note, view):
    """Average vertical (view-Up) position of everywhere this note's
    leader(s) point to. Returns None if the note has no leaders."""
    try:
        leaders = list(note.GetLeaders())
    except Exception:
        leaders = []
    if not leaders:
        return None
    origin, up = view.Origin, view.UpDirection
    vs = []
    for ld in leaders:
        try:
            end = ld.End
        except Exception:
            continue
        vs.append((end - origin).DotProduct(up))
    return sum(vs) / len(vs) if vs else None

def move_note(note, view, du, dv):
    if abs(du) < 1e-9 and abs(dv) < 1e-9:
        return
    translation = view.RightDirection.Multiply(du).Add(view.UpDirection.Multiply(dv))
    ElementTransformUtils.MoveElement(doc, note.Id, translation)


# ----------------------------------------------------------------------
# Grouping / alignment / spacing
# ----------------------------------------------------------------------
def build_note_records(notes, view):
    """One record per note: {note, left, right, bottom, top, leader_v}."""
    records = []
    for n in notes:
        uv = note_bbox_uv(n, view)
        if uv is None:
            continue
        left, right, bottom, top = uv
        lv = leader_target_v(n, view)
        records.append({"note": n, "left": left, "right": right,
                         "bottom": bottom, "top": top, "leader_v": lv})
    return records

def split_left_right(records):
    if not records:
        return [], 0.0
    centers = [(r["left"] + r["right"]) / 2.0 for r in records]
    centerline = (min(centers) + max(centers)) / 2.0
    left_group  = [r for r in records if (r["left"] + r["right"]) / 2.0 <= centerline]
    right_group = [r for r in records if (r["left"] + r["right"]) / 2.0 >  centerline]
    return left_group, right_group

def sort_top_to_bottom(group):
    """Order notes top-to-bottom by where their LEADER POINTS TO, not by
    their current text position. Keeping text order in sync with target
    order is what keeps the leader lines from crossing each other -
    notes with no leader fall back to their own current position."""
    return sorted(group, key=lambda r: -(r["leader_v"] if r["leader_v"] is not None else r["top"]))

def align_group(group, mode):
    """Compute target left-edge (du) for every record in the group."""
    if not group:
        return
    if mode == "Left":
        target = min(r["left"] for r in group)
        for r in group:
            r["target_left"] = target
    elif mode == "Right":
        target = max(r["right"] for r in group)
        for r in group:
            width = r["right"] - r["left"]
            r["target_left"] = target - width
    else:  # Center
        target = sum((r["left"] + r["right"]) / 2.0 for r in group) / len(group)
        for r in group:
            width = r["right"] - r["left"]
            r["target_left"] = target - width / 2.0

def space_group_vertically(group, auto_space, prevent_overlap):
    """Group is already sorted top-first. Sets target_top for every record."""
    if not group:
        return

    if auto_space:
        # Rigid equal-spacing grid: keep the topmost note in place, stack
        # the rest beneath it with a uniform gap.
        cursor_top = group[0]["top"]
        for r in group:
            height = r["top"] - r["bottom"]
            gap = height * GAP_RATIO
            r["target_top"] = cursor_top
            cursor_top = cursor_top - height - gap
        return

    if prevent_overlap:
        # Keep each note near its natural/leader-target height; only push
        # a note down if it would overlap the one stacked above it.
        prev_bottom = None
        for r in group:
            height = r["top"] - r["bottom"]
            gap = height * GAP_RATIO
            if prev_bottom is None:
                new_top = r["top"]
            else:
                max_allowed_top = prev_bottom - gap
                new_top = min(r["top"], max_allowed_top)
            r["target_top"] = new_top
            prev_bottom = new_top - height
        return

    # neither option selected: leave vertical position untouched
    for r in group:
        r["target_top"] = r["top"]


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
view = doc.ActiveView
if view is None or view.ViewType not in SUPPORTED_VIEW_TYPES:
    forms.alert("Open a Floor Plan, Ceiling Plan, Section, Elevation, "
                "Drafting, or Detail view first.",
                title="Text Organizer", warn_icon=True)
    sys.exit(0)

notes = list(FilteredElementCollector(doc, view.Id)
             .OfClass(TextNote)
             .WhereElementIsNotElementType()
             .ToElements())

if not notes:
    forms.alert("No text notes were found in the active view.",
                title="Text Organizer", warn_icon=True)
    sys.exit(0)

win = SettingsWindow(xaml_file)
win.ShowDialog()
settings = win.result
if not settings:
    sys.exit(0)   # user cancelled

records = build_note_records(notes, view)
left_group, right_group = split_left_right(records)

groups_to_process = []
if settings["scope"] in ("All", "Left"):
    groups_to_process.append(left_group)
if settings["scope"] in ("All", "Right"):
    groups_to_process.append(right_group)

t = Transaction(doc, "Organize Text Notes")
t.Start()
try:
    processed = 0
    for group in groups_to_process:
        group = sort_top_to_bottom(group)
        align_group(group, settings["align"])
        space_group_vertically(group, settings["auto_space"], settings["prevent_overlap"])

        for r in group:
            note = r["note"]

            # text formatting first (doesn't affect position math already computed)
            if settings["text_format"] != "None":
                try:
                    note.Text = apply_text_format(note.Text, settings["text_format"])
                except Exception:
                    pass

            du = r.get("target_left", r["left"]) - r["left"]
            dv = r.get("target_top", r["top"]) - r["top"]
            try:
                move_note(note, view, du, dv)
            except Exception:
                pass
            processed += 1

    doc.Regenerate()
    t.Commit()
except Exception as e:
    t.RollBack()
    forms.alert(u"Error organizing text notes:\n{}\n\n{}".format(e, traceback.format_exc()),
                title="Text Organizer", warn_icon=True)
    sys.exit(0)

forms.alert(u"Organized {} of {} text note(s) in the active view.".format(
            processed, len(notes)), title="Text Organizer", warn_icon=False)
