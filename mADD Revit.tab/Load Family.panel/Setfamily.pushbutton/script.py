# -*- coding: utf-8 -*-
"""Pick and save the root folder of the family library used by Import Family."""

from pyrevit import forms, script
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, DialogResult

# Same config section the other buttons use.
config = script.get_config('LoadDetailsLibrary')

dlg = FolderBrowserDialog()
dlg.Description = "Select your Revit family library root folder"
if dlg.ShowDialog() == DialogResult.OK:
    config.family_library_dir = dlg.SelectedPath
    script.save_config()
    forms.alert("Family library folder set to:\n{0}".format(dlg.SelectedPath),
                title="Set Family Folder")
else:
    forms.alert("Canceled.", title="Set Family Folder")
