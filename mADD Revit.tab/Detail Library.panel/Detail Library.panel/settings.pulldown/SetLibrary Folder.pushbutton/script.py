# -*- coding: utf-8 -*-
"""Pick and save the shared Detail Library folder used by Browse Library and
Push to Library. This is a FOLDER, not a single file: it holds one small
.rvt per detail, organized into category subfolders, plus a catalog.json
cache and a _thumbnails folder that Browse Library maintains."""

from pyrevit import forms, script
import os
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import FolderBrowserDialog, DialogResult

# Separate config namespace from the older single-file "Load Details" tool,
# so setting one doesn't affect the other.
config = script.get_config('DetailLibraryFolder')

dlg = FolderBrowserDialog()
dlg.Description = "Select the shared Detail Library folder (e.g. a network drive location the whole team can reach)"

existing = getattr(config, 'library_folder', None)
if existing and os.path.isdir(existing):
    dlg.SelectedPath = existing

if dlg.ShowDialog() == DialogResult.OK:
    config.library_folder = dlg.SelectedPath
    script.save_config()
    forms.alert(u"Detail Library folder set to:\n{0}\n\n"
                u"Browse Library will scan this folder (and its subfolders) for detail "
                u".rvt files, and Push to Library will save new details here.".format(dlg.SelectedPath),
                title="Set Library")
else:
    forms.alert("Canceled.", title="Set Library")
