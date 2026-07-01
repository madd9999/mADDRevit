# -*- coding: utf-8 -*-
"""
pyRevit button: Uppercase all TextNotes in the active Revit view or sheet.

Behavior:
- In a normal view: affects TextNote elements visible/owned by the active view.
- In a sheet: asks whether to affect sheet annotations only, or sheet annotations
  plus text notes inside views placed on that sheet.
- Preserves Revit rich text formatting by applying the TextNote "All Caps" formatted
  text setting when available.
"""

from pyrevit import revit, DB, forms, script


doc = revit.doc
active_view = doc.ActiveView
output = script.get_output()


def get_view_name(view):
    try:
        return view.Name
    except Exception:
        return str(view.Id.IntegerValue)


def collect_textnotes_in_view(view_id):
    """Return TextNote elements collected in the given view."""
    return list(
        DB.FilteredElementCollector(doc, view_id)
        .OfClass(DB.TextNote)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def get_sheet_placed_view_ids(sheet):
    """Return IDs of model/drafting/legend/etc. views placed on this sheet."""
    view_ids = []

    viewports = (
        DB.FilteredElementCollector(doc, sheet.Id)
        .OfClass(DB.Viewport)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    for viewport in viewports:
        try:
            view_ids.append(viewport.ViewId)
        except Exception:
            pass

    return view_ids


def uppercase_textnote(text_note):
    """
    Uppercase a TextNote.

    Preferred method:
    - Use FormattedText.SetAllCapsStatus(True), which preserves per-character
      formatting and makes the note display uppercase.

    Fallback:
    - Directly set TextNote.Text to uppercase if FormattedText is unavailable.
      This may not preserve all rich formatting in older/problem cases.
    """
    original_text = text_note.Text

    if not original_text:
        return False, None

    try:
        formatted_text = text_note.GetFormattedText()
        formatted_text.SetAllCapsStatus(True)
        text_note.SetFormattedText(formatted_text)
        return True, None
    except Exception:
        try:
            upper_text = original_text.upper()
            if upper_text != original_text:
                text_note.Text = upper_text
                return True, None
            return False, None
        except Exception as err:
            return False, err


def main():
    target_view_ids = [active_view.Id]
    scope_label = "active view"

    if isinstance(active_view, DB.ViewSheet):
        choice = forms.CommandSwitchWindow.show(
            [
                "Sheet annotations only",
                "Sheet annotations + views placed on this sheet",
            ],
            message="Choose what text to uppercase:"
        )

        if not choice:
            forms.alert("Cancelled.", exitscript=True)

        scope_label = choice

        if choice == "Sheet annotations + views placed on this sheet":
            target_view_ids.extend(get_sheet_placed_view_ids(active_view))

    # De-duplicate view IDs
    unique_view_ids = []
    seen = set()
    for view_id in target_view_ids:
        key = view_id.IntegerValue
        if key not in seen:
            seen.add(key)
            unique_view_ids.append(view_id)

    text_notes = []
    for view_id in unique_view_ids:
        text_notes.extend(collect_textnotes_in_view(view_id))

    # De-duplicate notes
    unique_notes = []
    seen_note_ids = set()
    for note in text_notes:
        key = note.Id.IntegerValue
        if key not in seen_note_ids:
            seen_note_ids.add(key)
            unique_notes.append(note)

    if not unique_notes:
        forms.alert(
            "No TextNote elements found in {}.".format(scope_label),
            title="Uppercase Text",
            exitscript=True
        )

    changed_count = 0
    skipped_count = 0
    failed = []

    with revit.Transaction("Uppercase text notes"):
        for note in unique_notes:
            if hasattr(note, "IsModifiable") and not note.IsModifiable:
                skipped_count += 1
                continue

            changed, err = uppercase_textnote(note)

            if changed:
                changed_count += 1
            elif err:
                failed.append((note.Id.IntegerValue, err))
            else:
                skipped_count += 1

    msg = "Uppercase complete.\n\nView/Sheet: {}\nScope: {}\nChanged: {}\nSkipped/no text: {}\nFailed: {}".format(
        get_view_name(active_view),
        scope_label,
        changed_count,
        skipped_count,
        len(failed)
    )

    if failed:
        output.print_md("### Failed TextNotes")
        for note_id, err in failed:
            output.print_md("- Element ID `{}`: `{}`".format(note_id, err))

    forms.alert(msg, title="Uppercase Text")


if __name__ == "__main__":
    main()
