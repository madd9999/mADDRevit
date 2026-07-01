# -*- coding: utf-8 -*-
"""A launcher for Dynamo graphs. Stores a list of .dyn files (added with +),
and runs the selected one headlessly via Dynamo's automation API. The list
persists across sessions in pyRevit config."""

from pyrevit import forms, script
import os
import json
import clr

config = script.get_config('LoadDetailsLibrary')
XAML_FILE = script.get_bundle_file('ui.xaml')
logger = script.get_logger()


# ---------- persistence ----------

def load_scripts():
    raw = getattr(config, 'dynamo_scripts_json', None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [d for d in data if 'name' in d and 'path' in d]
    except Exception:
        return []


def save_scripts(items):
    config.dynamo_scripts_json = json.dumps(items)
    script.save_config()


def pick_dyn_file():
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import OpenFileDialog, DialogResult
    dlg = OpenFileDialog()
    dlg.Filter = "Dynamo Graph (*.dyn)|*.dyn"
    dlg.Title = "Select a Dynamo graph (.dyn)"
    if dlg.ShowDialog() == DialogResult.OK:
        return dlg.FileName
    return None


# ---------- run a Dynamo graph (headless / automation) ----------

def run_dynamo_graph(dyn_path):
    """Execute a .dyn through Dynamo's automation API, no Dynamo UI."""
    clr.AddReference('DynamoRevitDS')
    from Dynamo.Applications import DynamoRevit, DynamoRevitCommandData
    from System.Collections.Generic import Dictionary

    jd = Dictionary[str, str]()
    jd['dynPath'] = dyn_path
    jd['dynShowUI'] = 'false'          # run headless
    jd['dynAutomation'] = 'true'
    jd['dynPathExecute'] = 'true'      # run on open
    jd['dynForceManualRun'] = 'true'
    jd['dynModelShutDown'] = 'true'    # close the Dynamo model after running

    cmd = DynamoRevitCommandData()
    cmd.Application = __revit__         # UIApplication
    cmd.JournalData = jd

    DynamoRevit().ExecuteCommand(cmd)


# ---------- launcher window ----------

class LauncherWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        self.scripts = load_scripts()
        self.run_path = None
        self._refresh()
        self.script_list.SelectionChanged += self.on_select

    def _refresh(self, select_index=None):
        self.script_list.ItemsSource = [s['name'] for s in self.scripts]
        if select_index is not None and 0 <= select_index < len(self.scripts):
            self.script_list.SelectedIndex = select_index
        if not self.scripts:
            self.path_text.Text = 'No graphs yet - click "+ Add" to store one.'

    def _selected(self):
        i = self.script_list.SelectedIndex
        return self.scripts[i] if 0 <= i < len(self.scripts) else None

    def on_select(self, sender, args):
        s = self._selected()
        if s:
            missing = '' if os.path.isfile(s['path']) else '   [FILE MISSING]'
            self.path_text.Text = s['path'] + missing

    def on_add(self, sender, args):
        path = pick_dyn_file()
        if not path:
            return
        default = os.path.splitext(os.path.basename(path))[0]
        name = forms.ask_for_string(default=default,
                                    prompt='Name for this graph:',
                                    title='Add Dynamo Script')
        if not name:
            return
        self.scripts.append({'name': name, 'path': path})
        save_scripts(self.scripts)
        self._refresh(select_index=len(self.scripts) - 1)

    def on_remove(self, sender, args):
        i = self.script_list.SelectedIndex
        if i < 0:
            return
        del self.scripts[i]
        save_scripts(self.scripts)
        self._refresh()

    def on_rename(self, sender, args):
        s = self._selected()
        if not s:
            return
        name = forms.ask_for_string(default=s['name'],
                                    prompt='New name:', title='Rename')
        if name:
            s['name'] = name
            save_scripts(self.scripts)
            self._refresh(select_index=self.script_list.SelectedIndex)

    def on_run(self, sender, args):
        s = self._selected()
        if not s:
            return
        if not os.path.isfile(s['path']):
            forms.alert("That .dyn file no longer exists:\n{0}".format(s['path']),
                        title="Dynamo Scripts")
            return
        self.run_path = s['path']     # run after the window closes
        self.Close()

    def on_close(self, sender, args):
        self.run_path = None
        self.Close()


# ---------- main ----------

win = LauncherWindow(XAML_FILE)
win.show_dialog()

if win.run_path:
    try:
        run_dynamo_graph(win.run_path)
    except Exception as ex:
        logger.debug('dynamo run failed: {0}'.format(ex))
        forms.alert(
            "Couldn't run the Dynamo graph.\n\n"
            "Most likely causes:\n"
            "  - Dynamo for Revit isn't installed, or\n"
            "  - the automation API differs on your Dynamo version.\n\n"
            "Tip: open Dynamo once this session, then try again.\n\n"
            "Details: {0}".format(ex),
            title="Dynamo Scripts")
