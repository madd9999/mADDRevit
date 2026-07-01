# -*- coding: utf-8 -*-
"""Pick and save the library .RVT file used by Import Drafting View."""

from pyrevit import forms, script
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import OpenFileDialog, DialogResult

# MUST match the section name used by the Import Drafting View button.
config = script.get_config('LoadDetailsLibrary')

dlg = OpenFileDialog()
dlg.Filter = "Revit Project (*.rvt)|*.rvt"
dlg.Title = "Pick new Library Model (.rvt)"
if dlg.ShowDialog() == DialogResult.OK:
    config.library_rvt_path = dlg.FileName
    script.save_config()
    forms.alert("Library file updated to:\n{0}".format(dlg.FileName),
                title="Set Library File")
else:
    forms.alert("Canceled.", title="Set Library File")
