# -*- coding: utf-8 -*-
"""
Falling Image pyRevit button
Shows an image that falls down the Revit window and disappears after 3 seconds.

Place popup.png inside this .pushbutton folder.
"""

import os
import clr

clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System')
clr.AddReference('System.Windows.Forms')

from System import Uri, UriKind, TimeSpan
from System.Windows import (
    Window,
    WindowStyle,
    ResizeMode,
    WindowStartupLocation,
    Thickness,
    HorizontalAlignment,
    VerticalAlignment
)
from System.Windows.Controls import Grid, Image
from System.Windows.Media import Brushes
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption, BitmapCreateOptions
from System.Windows.Threading import DispatcherTimer
from System.Windows.Media.Animation import DoubleAnimation, QuadraticEase, EasingMode
from System.Windows.Forms import Screen
from System.Windows.Interop import WindowInteropHelper

try:
    from pyrevit import HOST_APP
except Exception:
    HOST_APP = None


IMAGE_FILE = "popup.png"

# Overall popup lifetime.
TOTAL_SECONDS = 3.0

# Falling animation duration. Window closes after TOTAL_SECONDS.
FALL_SECONDS = 2.1

# Image/window size.
IMAGE_WIDTH = 220
IMAGE_HEIGHT = 220

# Approximate start point under the ribbon/button area.
TOP_MARGIN = 95

# Bottom padding from screen working area.
BOTTOM_MARGIN = 20


def get_script_dir():
    return os.path.dirname(__file__)


def get_image_path():
    return os.path.join(get_script_dir(), IMAGE_FILE)


def make_bitmap(image_path):
    """Load image safely for IronPython/Revit WPF."""
    bitmap = BitmapImage()
    bitmap.BeginInit()

    # IMPORTANT:
    # Do not use numeric enum values like 1 here.
    # IronPython can throw:
    # TypeError: Cannot convert numeric value 1 to BitmapCacheOption.
    bitmap.CacheOption = BitmapCacheOption.OnLoad
    bitmap.CreateOptions = BitmapCreateOptions.IgnoreImageCache

    bitmap.UriSource = Uri(image_path, UriKind.Absolute)
    bitmap.EndInit()
    bitmap.Freeze()
    return bitmap


def get_revit_owner_handle():
    try:
        if HOST_APP and HOST_APP.app:
            return HOST_APP.app.MainWindowHandle
    except Exception:
        pass
    return None


def get_revit_screen():
    """Get the screen containing Revit's main window."""
    handle = get_revit_owner_handle()
    try:
        if handle:
            return Screen.FromHandle(handle)
    except Exception:
        pass
    return Screen.PrimaryScreen


def get_start_x(screen):
    """
    Starts near the horizontal center of the Revit screen.
    Exact button coordinates are not exposed reliably through pyRevit,
    so this approximates falling from the ribbon/top region.
    """
    work = screen.WorkingArea
    return work.Left + (work.Width - IMAGE_WIDTH) / 2.0


def get_start_y(screen):
    work = screen.WorkingArea
    return work.Top + TOP_MARGIN


def get_end_y(screen):
    work = screen.WorkingArea
    return work.Bottom - IMAGE_HEIGHT - BOTTOM_MARGIN


class FallingImageWindow(Window):
    def __init__(self, image_path):
        self.screen = get_revit_screen()
        work = self.screen.WorkingArea

        self.Width = IMAGE_WIDTH
        self.Height = IMAGE_HEIGHT
        self.Left = get_start_x(self.screen)
        self.Top = get_start_y(self.screen)

        self.WindowStyle = WindowStyle.None
        self.ResizeMode = ResizeMode.NoResize
        self.WindowStartupLocation = WindowStartupLocation.Manual
        self.ShowInTaskbar = False
        self.Topmost = True
        self.AllowsTransparency = True
        self.Background = Brushes.Transparent

        grid = Grid()
        grid.Margin = Thickness(0)

        img = Image()
        img.Width = IMAGE_WIDTH
        img.Height = IMAGE_HEIGHT
        img.HorizontalAlignment = HorizontalAlignment.Center
        img.VerticalAlignment = VerticalAlignment.Center
        img.Source = make_bitmap(image_path)

        grid.Children.Add(img)
        self.Content = grid

        self.Loaded += self.on_loaded

    def on_loaded(self, sender, args):
        # Try to make Revit the owner window where possible.
        handle = get_revit_owner_handle()
        try:
            if handle:
                helper = WindowInteropHelper(self)
                helper.Owner = handle
        except Exception:
            pass

        end_top = get_end_y(self.screen)
        if end_top < self.Top:
            end_top = self.Top + 100

        ease = QuadraticEase()
        ease.EasingMode = EasingMode.EaseIn

        fall = DoubleAnimation()
        fall.From = self.Top
        fall.To = end_top
        fall.Duration = TimeSpan.FromSeconds(FALL_SECONDS)
        fall.EasingFunction = ease

        self.BeginAnimation(Window.TopProperty, fall)

        close_timer = DispatcherTimer()
        close_timer.Interval = TimeSpan.FromSeconds(TOTAL_SECONDS)

        def close_window(sender, event_args):
            close_timer.Stop()
            self.Close()

        close_timer.Tick += close_window
        close_timer.Start()


def main():
    image_path = get_image_path()

    if not os.path.exists(image_path):
        from pyrevit import forms
        forms.alert("Missing image file:\n\n{}".format(image_path), title="Falling Image")
        return

    win = FallingImageWindow(image_path)
    win.ShowDialog()


if __name__ == "__main__":
    main()
