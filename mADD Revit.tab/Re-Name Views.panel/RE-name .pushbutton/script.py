# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script

doc = revit.doc
output = script.get_output()

# Only rename these view types
ALLOWED_VIEW_TYPES = {
    DB.ViewType.FloorPlan,
    DB.ViewType.Section,
    DB.ViewType.Elevation
}


def is_valid_view(view):
    """Check if the view is one we want to rename."""
    if not view:
        return False

    if not isinstance(view, DB.View):
        return False

    if view.IsTemplate:
        return False

    if view.ViewType not in ALLOWED_VIEW_TYPES:
        return False

    return True


def strip_old_sheet_prefix(view_name, all_sheet_numbers):
    """
    Remove existing prefix like:
    A3.0 - First Floor Plan  -> First Floor Plan
    """
    for sheet_no in sorted(all_sheet_numbers, key=len, reverse=True):
        prefix = sheet_no + " - "
        if view_name.startswith(prefix):
            return view_name[len(prefix):]
    return view_name


def get_all_view_names_except_current(doc, current_view_id):
    """Collect all view names except current one."""
    names = set()
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()

    for v in views:
        if v.Id != current_view_id:
            try:
                names.add(v.Name)
            except:
                pass
    return names


def make_unique_name(base_name, existing_names):
    """Make name unique if it already exists."""
    if base_name not in existing_names:
        return base_name

    i = 1
    while True:
        new_name = "{} ({})".format(base_name, i)
        if new_name not in existing_names:
            return new_name
        i += 1


def main():
    sheets = list(DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet).ToElements())

    if not sheets:
        forms.alert("No sheets found in project.", exitscript=True)
        return

    all_sheet_numbers = set()
    for s in sheets:
        try:
            all_sheet_numbers.add(s.SheetNumber.strip())
        except:
            pass

    renamed = 0
    skipped = 0
    failed = 0
    report = []

    with revit.Transaction("Rename Views By Sheet"):
        for sheet in sheets:
            sheet_number = sheet.SheetNumber.strip()

            try:
                placed_view_ids = sheet.GetAllPlacedViews()
            except:
                continue

            for view_id in placed_view_ids:
                view = doc.GetElement(view_id)

                if not is_valid_view(view):
                    skipped += 1
                    continue

                try:
                    old_name = view.Name
                    clean_name = strip_old_sheet_prefix(old_name, all_sheet_numbers)
                    target_name = "{} - {}".format(sheet_number, clean_name)

                    if old_name == target_name:
                        skipped += 1
                        continue

                    existing_names = get_all_view_names_except_current(doc, view.Id)
                    final_name = make_unique_name(target_name, existing_names)

                    view.Name = final_name
                    renamed += 1

                    report.append([
                        sheet_number,
                        str(view.ViewType),
                        old_name,
                        final_name
                    ])

                except Exception as ex:
                    failed += 1
                    report.append([
                        sheet_number,
                        str(view.ViewType) if view else "Unknown",
                        getattr(view, "Name", "Unknown"),
                        "FAILED: {}".format(str(ex))
                    ])

    output.print_md("# Rename Views By Sheet")
    output.print_md("**Renamed:** {}".format(renamed))
    output.print_md("**Skipped:** {}".format(skipped))
    output.print_md("**Failed:** {}".format(failed))
    output.print_md("---")

    if report:
        output.print_table(
            table_data=report,
            columns=["Sheet", "View Type", "Old Name", "New Name / Status"]
        )
    else:
        output.print_md("No matching views found.")


if __name__ == "__main__":
    main()