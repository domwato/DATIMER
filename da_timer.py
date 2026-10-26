"""
DA Timer v5 - Python rebuild of the original VB.NET DA Timer.

Operator window  : segment time + show time, RESET / PRESET per timer,
                   segment log, ALL START / ALL STOP, dual-screen control.
Output window    : full-screen clean feed on a second monitor
                   (double-click it to toggle windowed / full-screen).

Keyboard (operator window):
    Space   ALL START / ALL STOP toggle
    N       Next segment (segment RESET)
    L       Show / hide the segment times list
    T       Show / hide the time-of-day (TOD) clock
    G       Edit segment goals, running order and hard out

Stream Deck (direct USB, no Elgato software): see da_deck.py. Plug a deck
in and it's picked up automatically; set keys up with the DECK button.

Stream Deck plugin / remote control: DA Timer runs a small control API on this PC
only (http://127.0.0.1:47800). The DA Timer Stream Deck plugin uses it.
    GET  /api/state               live state as JSON
    POST /api/action/<command>    (header "X-DA-Timer: 1" required)
Commands: see API_COMMANDS below.
    F2      Show the log and jump to the note box
    Ctrl+M  Drop a MARK in the log (works anywhere)
    Ctrl+Z  Undo the last action (reset, next segment, stop/start, preset...)
    D       Output to monitor on / off
    F11     Full-screen operator window
    Esc     Leave full-screen

Shows: each show is a saved profile with its own logos, title text, bottom
message, timer names and colours (Show Setup button on the operator window).

Uses tkinter + ctypes from the standard library. Pillow (pip install pillow)
is recommended for logos: it adds JPG/BMP support and smooth scaling.
Without it, PNG and GIF logos still work.
"""

import json
import os
import queue
import re
import shutil
import sys
import weakref
import threading
import time
from datetime import datetime, timedelta
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageTk
except ImportError:  # Pillow optional
    Image = ImageTk = None

APP_NAME = "DA Timer"
# The author's logo: author_logo.png next to da_timer.py (bundled into the
# Windows exe and the Mac app). Shown in Settings and the header.
AUTHOR_LOGO = "author_logo.png"


def author_logo(height):
    """The author's logo as a Tk image `height` pixels tall, or None."""
    if Image is None:
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (getattr(sys, "_MEIPASS", here), here):
        path = os.path.join(d, AUTHOR_LOGO)
        if os.path.exists(path):
            try:
                im = Image.open(path).convert("RGBA")
                w = max(1, round(im.width * height / im.height))
                return ImageTk.PhotoImage(im.resize((w, height), Image.LANCZOS))
            except Exception:
                return None
    return None
IS_MAC = sys.platform == "darwin"
MOD = "\u2318" if IS_MAC else "Ctrl+"        # shortcut modifier, for help text
VERSION = "1.0.4"

# --------------------------------------------------------------------------
# Look & feel: a broadcast control-room surface. Graphite panels, tally-light
# colours for state (green running, red stopped/over, amber warning) and
# Cyan kept for accents and selection.
# --------------------------------------------------------------------------
BG = "#0d1116"          # app background
SURFACE = "#151b22"     # panels, cards, header, toolbar
SURFACE_2 = "#1c242e"   # buttons, table headings, striped rows
SURFACE_3 = "#25303c"   # hover / progress track
BORDER = "#27313d"
TEXT = "#e8edf2"
MUTED = "#8b97a6"
FAINT = "#4d5967"
CYAN = "#00e5f0"        # accent cyan
CYAN_DIM = "#06363c"
GREEN = "#2bd47d"
GREEN_DIM = "#0f3524"
RED = "#ff4757"
RED_DIM = "#3a161b"
AMBER = "#ffb547"
AMBER_DIM = "#3a2c10"
OUT_BG = BG
OUT_FG = "#ffffff"
OUT_ACCENT = CYAN

# Fonts: DA Sans / DA Clock ship in ./fonts (built from Barlow, SIL OFL, with
# tabular figures so running clocks don't jitter). Falls back to Windows
# fonts if the folder is missing.
FONT_DIR = os.path.join(getattr(sys, "_MEIPASS",
                                os.path.dirname(os.path.abspath(__file__))),
                        "fonts")
UI_FONT = "DA Sans"
UI_BOLD = "DA Sans SemiBold"
UI_HEAVY = "DA Sans Bold"
DIGIT_FONT = "DA Clock"
DIGIT_WEIGHT = "normal"


def register_fonts():
    """Make the bundled fonts available to this process only."""
    if not os.path.isdir(FONT_DIR):
        return
    files = [os.path.join(FONT_DIR, f) for f in os.listdir(FONT_DIR)
             if f.lower().endswith(".ttf")]
    if sys.platform == "win32":
        import ctypes
        for f in files:
            try:
                ctypes.windll.gdi32.AddFontResourceExW(f, 0x10, 0)  # PRIVATE
            except Exception:
                pass
    elif IS_MAC:
        try:
            import ctypes
            import ctypes.util
            ct = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreText"))
            cf = ctypes.cdll.LoadLibrary(
                ctypes.util.find_library("CoreFoundation"))
            cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
            cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
            ct.CTFontManagerRegisterFontsForURL.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
            for f in files:
                b = f.encode("utf-8")
                url = cf.CFURLCreateFromFileSystemRepresentation(
                    None, b, len(b), False)
                ct.CTFontManagerRegisterFontsForURL(url, 1, None)  # process
        except Exception:
            pass
    else:   # Linux: user font folder + fontconfig
        target = os.path.expanduser("~/.local/share/fonts/da_timer")
        try:
            new = False
            os.makedirs(target, exist_ok=True)
            for f in files:
                dest = os.path.join(target, os.path.basename(f))
                if not os.path.exists(dest):
                    shutil.copy2(f, dest)
                    new = True
            if new:
                import subprocess
                subprocess.run(["fc-cache", "-f", target], capture_output=True,
                               timeout=20)
        except Exception:
            pass


register_fonts()


# --------------------------------------------------------------------------
# Interface scaling. Big clocks and panels are sized as a share of the
# window; fixed things (buttons, toolbar, padding, list rows) are designed at
# 100 % and multiplied by UI_SCALE, which follows the Windows display scaling
# (150 % on a 4K screen, 200 % on a Surface) unless set in Settings.
# --------------------------------------------------------------------------
UI_SCALE = 1.0
UI_SCALE_CHOICES = ["auto", 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


def px(v):
    return int(round(v * UI_SCALE))


def set_ui_scale(root, choice="auto"):
    global UI_SCALE
    if IS_MAC:
        # macOS handles Retina itself and Tk measures in points; make point
        # sizes come out like Windows at 100 %
        root.tk.call("tk", "scaling", 96 / 72)
    auto = 1.0 if IS_MAC else root.winfo_fpixels("1i") / 96.0
    env = os.environ.get("DA_TIMER_SCALE")
    try:
        forced = float(env) if env else (None if choice in (None, "auto")
                                           else float(choice))
    except ValueError:
        forced = None
    scale = forced or auto
    UI_SCALE = round(min(max(scale, 0.75), 4.0), 3)
    if forced:
        # make point-sized text follow the chosen size too
        root.tk.call("tk", "scaling", UI_SCALE * 96 / 72)
    return UI_SCALE


def _scale_pads(kw):
    if UI_SCALE == 1.0:
        return kw
    for k in ("padx", "pady", "ipadx", "ipady"):
        v = kw.get(k)
        if isinstance(v, (int, float)):
            kw[k] = px(v)
        elif isinstance(v, tuple):
            kw[k] = tuple(px(x) if isinstance(x, (int, float)) else x
                          for x in v)
    return kw


_orig_grid, _orig_pack = tk.Grid.grid_configure, tk.Pack.pack_configure


def _grid_scaled(self, cnf={}, **kw):
    return _orig_grid(self, cnf, **_scale_pads(kw))


def _pack_scaled(self, cnf={}, **kw):
    return _orig_pack(self, cnf, **_scale_pads(kw))


tk.Grid.grid_configure = tk.Grid.grid = _grid_scaled
tk.Pack.pack_configure = tk.Pack.pack = _pack_scaled


def init_fonts(root):
    global UI_FONT, UI_BOLD, UI_HEAVY, DIGIT_FONT, DIGIT_WEIGHT
    fams = {f.lower() for f in tkfont.families(root)}
    if "da sans" in fams and "da clock" in fams:
        return
    for ui, bold, heavy in (("Segoe UI", "Segoe UI Semibold", "Segoe UI Bold"),
                            ("Arial", "Arial", "Arial"),
                            ("DejaVu Sans", "DejaVu Sans", "DejaVu Sans")):
        if ui.lower() in fams:
            UI_FONT, UI_BOLD, UI_HEAVY = ui, bold, heavy
            break
    for f in ("Bahnschrift SemiBold", "Segoe UI Semibold", "Arial",
              "DejaVu Sans"):
        if f.lower() in fams:
            DIGIT_FONT, DIGIT_WEIGHT = f, "normal" if "semibold" in f.lower() \
                else "bold"
            break

# --------------------------------------------------------------------------
# Font choice (Settings -> Fonts). The code always names the built-in
# families; widgets are remapped to the chosen fonts when they appear, so a
# change applies straight away everywhere, including windows opened later.
# --------------------------------------------------------------------------
FONT_CHOICE = {"text": "", "clock": "", "clock_bold": False}
_FIT_LABELS = weakref.WeakSet()
_FONT_ORIG = weakref.WeakKeyDictionary()      # widget -> family it was built with


def _font_roles():
    regular = {UI_FONT}
    bold = {UI_BOLD, UI_HEAVY} - regular
    return regular, bold, {DIGIT_FONT}


def ft(family, size, *styles):
    """Map a built-in family to the chosen font: ft(UI_BOLD, 12) etc."""
    regular, bold, digits = _font_roles()
    text, clock = FONT_CHOICE.get("text"), FONT_CHOICE.get("clock")
    if family in digits and clock:
        return (clock, size) + (("bold",) if FONT_CHOICE.get("clock_bold")
                                else ()) + styles
    if family in bold and text:
        return (text, size, "bold") + tuple(x for x in styles if x != "bold")
    if family in regular and text:
        return (text, size) + styles
    return (family, size) + styles


def remap_fonts(widget):
    """Apply the chosen fonts to `widget` and everything inside it."""
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            continue
        if isinstance(w, FitLabel) or isinstance(w, (ttk.Widget,)):
            continue
        try:
            spec = w.cget("font")
        except (tk.TclError, AttributeError):
            continue
        try:
            parts = list(w.tk.splitlist(spec))
        except tk.TclError:
            continue
        if len(parts) < 2:
            continue                      # a named font, leave it alone
        fam, size, styles = parts[0], parts[1], tuple(parts[2:])
        orig = _FONT_ORIG.get(w)
        if orig is None:
            regular, bold, digits = _font_roles()
            if fam not in regular | bold | digits:
                continue
            _FONT_ORIG[w] = orig = fam
        elif fam != orig and fam in set().union(*_font_roles()):
            _FONT_ORIG[w] = orig = fam    # re-laid-out with a new role
        new = ft(orig, size, *[x for x in styles if x != "bold"])
        if tuple(str(x) for x in new) != tuple(str(x) for x in parts):
            try:
                w.config(font=new)
            except tk.TclError:
                pass
    for lbl in list(_FIT_LABELS):
        try:
            lbl.apply_font_choice()
        except tk.TclError:
            pass


def font_has_tabular_digits(root, family):
    f = tkfont.Font(root=root, family=family, size=-40)
    widths = {f.measure(d) for d in "0123456789"}
    return len(widths) == 1


# --------------------------------------------------------------------------
# Windows helpers: DPI awareness + monitor enumeration
# --------------------------------------------------------------------------
def enable_dpi_awareness():
    """Make window coordinates real pixels on high-DPI / mixed-DPI setups."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class KeepAwake:
    """Stop the computer sleeping or blanking the screen while the clocks
    run (Windows: SetThreadExecutionState, Mac: caffeinate)."""

    def __init__(self):
        self.on = False
        self._proc = None

    def set(self, on):
        if on == self.on:
            return
        self.on = on
        try:
            if sys.platform == "win32":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM, ES_DISPLAY = \
                    0x80000000, 0x00000001, 0x00000002
                flags = ES_CONTINUOUS | ((ES_SYSTEM | ES_DISPLAY) if on else 0)
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
            elif IS_MAC:
                import subprocess
                if on:
                    self._proc = subprocess.Popen(
                        ["caffeinate", "-d", "-i", "-w", str(os.getpid())])
                elif self._proc is not None:
                    self._proc.terminate()
                    self._proc = None
        except Exception:
            pass


def get_monitors(root):
    """Return a list of dicts: x, y, w, h, primary, name."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFOEXW(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD),
                            ("szDevice", wintypes.WCHAR * 32)]

            monitors = []
            proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

            def callback(hmon, _hdc, _rect, _data):
                info = MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(MONITORINFOEXW)
                ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
                r = info.rcMonitor
                monitors.append({
                    "x": r.left, "y": r.top,
                    "w": r.right - r.left, "h": r.bottom - r.top,
                    "primary": bool(info.dwFlags & 1),
                    "name": info.szDevice.replace("\\\\.\\", ""),
                })
                return 1

            ctypes.windll.user32.EnumDisplayMonitors(
                None, None, proc_type(callback), 0)
            if monitors:
                # Primary first, then left-to-right
                monitors.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
                return monitors
        except Exception:
            pass
    if IS_MAC:
        try:
            from AppKit import NSScreen
            screens = list(NSScreen.screens())
            main_h = screens[0].frame().size.height
            monitors = []
            for i, sc in enumerate(screens):
                fr = sc.frame()
                monitors.append({
                    "x": int(fr.origin.x),
                    "y": int(main_h - (fr.origin.y + fr.size.height)),
                    "w": int(fr.size.width), "h": int(fr.size.height),
                    "primary": i == 0,
                    "name": str(sc.localizedName()) if hasattr(
                        sc, "localizedName") else f"Display {i + 1}"})
            if monitors:
                return monitors
        except Exception:
            pass
    # Fallback (other systems or failure): one screen
    return [{"x": 0, "y": 0, "w": root.winfo_screenwidth(),
             "h": root.winfo_screenheight(), "primary": True,
             "name": "Screen"}]


# --------------------------------------------------------------------------
# Feature switches (Settings screen). Everything is on by default.
# --------------------------------------------------------------------------
FEATURES = [
    # key, section, label, help
    ("running_order", "Features", "Running order",
     "Segment list with goal, actual, +/- and start-by times"),
    ("log", "Features", "Log",
     "Notes, marks and the Excel export. Off: nothing is logged and you're "
     "never asked to export"),
    ("clock", "Features", "Time-of-day clock", "The clock in the header"),
    ("goals", "Features", "Goals and over/under",
     "Goal, time left / over and progress bars on the timer cards"),
    ("backtime", "Features", "Back-timing",
     "Hard out, time to out, projected off air, cut / spare"),
    ("dual", "Features", "Output to monitor",
     "The presenter / floor display on a second monitor"),
    ("sync", "Features", "Time source sync",
     "NTP or LTC timecode. Off: uses the PC clock"),
    ("undo", "Features", "Undo", f"Undo button, {MOD}Z and the undo pop-ups"),
    ("deck", "Devices", "Stream Deck (direct USB)",
     "DA Timer drives a plugged-in Stream Deck itself"),
    ("api", "Devices", "Remote control",
     "For the Elgato Stream Deck plugin and Bitfocus Companion. Takes effect "
     "next time DA Timer starts"),
    ("master_buttons", "Screen", "ALL START / ALL STOP keys",
     "The two big keys on the left"),
    ("logos", "Screen", "Logos in the header", "Show logo and company logo"),
    ("next_hint", "Screen", "Show the next segment",
     "\"Next: ...\" beside the segment number"),
    ("tick_together", "Screen", "Clocks tick together",
     "Time of day, program and segment clocks all change on the same "
     "instant (lined up to within a second, without any visible jump)"),
    ("fullscreen", "Screen", "Start in full screen GUI",
     "Open DA Timer full screen (the Full screen GUI button, F11 or Esc "
     "switch it)"),
    ("dual_on_start", "Screen", "Start output to monitor on launch",
     "When a second monitor is connected"),
    ("confirm_reset", "Safety", "Confirm before a program reset",
     "Ask before RESET clears the timers and segments"),
    ("undo_popups", "Safety", "Undo pop-ups",
     "Offer Undo after a reset, next segment or clearing the log"),
    ("shortcuts", "Safety", "Single-key shortcuts",
     "Space start/stop, N next, D output, L list, T clock, G goals. "
     f"{MOD} shortcuts always work"),
    ("keep_awake", "Safety", "Keep the computer awake",
     "No sleep, screensaver or screen blanking while a clock is running"),
    ("autosave", "Safety", "Crash recovery",
     "Save the timers every 2 seconds so they can be restored after a crash"),
    ("auto_log", "Safety", "Log button presses automatically",
     "Starts, stops, next segment and presets go in the log"),
]
FEATURE_DEFAULTS = {k: k != "fullscreen" for k, *_ in FEATURES}


# --------------------------------------------------------------------------
# Settings (remembers which screen the output goes to)
# --------------------------------------------------------------------------
def data_dir(*parts):
    base = os.environ.get("APPDATA") or (
        os.path.expanduser("~/Library/Application Support") if IS_MAC
        else os.path.expanduser("~"))
    folder = os.path.join(base, "DATimer", *parts)
    os.makedirs(folder, exist_ok=True)
    return folder


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Show profiles: %APPDATA%\DATimer\shows\<name>.json, logos copied into
# %APPDATA%\DATimer\logos so a show keeps working if the original file moves.
# --------------------------------------------------------------------------
DEFAULT_SHOW = {
    "name": "Default",
    "title": "",
    "message": "",
    "segment_label": "Segment",
    "show_label": "Program",
    "logo_left": "",
    "logo_right": "",
    "bg": OUT_BG,
    "fg": OUT_FG,
    "accent": OUT_ACCENT,
    "plan": [],              # [{"name": str, "goal": seconds}, ...]
    "output_goals": False,   # show segment time left/over on the output
    "segment_count": 0,      # segments in the show (0 = count the running order)
    "output_clock": "off",   # clock on the output: "off" | "tod" | "tc"
    "output_segments": True, # "SEGMENT 3 OF 8 - 5 LEFT" on the output
    # Hard out for back-timing: mode "off" | "tod" (off-air time of day)
    # | "trt" (total program length)
    "hard_out": {"mode": "off", "tod": "", "trt": 0},
}

LOGO_TYPES = ([("Images", "*.png *.jpg *.jpeg *.gif *.bmp")]
              if Image else [("PNG / GIF images", "*.png *.gif")])


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "show"


def list_shows():
    names = []
    for fn in os.listdir(data_dir("shows")):
        if fn.lower().endswith(".json"):
            try:
                with open(os.path.join(data_dir("shows"), fn),
                          encoding="utf-8") as f:
                    names.append(json.load(f).get("name", fn[:-5]))
            except Exception:
                pass
    return sorted(names, key=str.lower)


def copy_show(show):
    """Copy a show dict, including its own copy of the running order."""
    out = dict(DEFAULT_SHOW, **show)
    out["plan"] = [dict(row) for row in show.get("plan") or []]
    out["hard_out"] = dict(DEFAULT_SHOW["hard_out"],
                           **(show.get("hard_out") or {}))
    if show.get("output_layout"):
        out["output_layout"] = [dict(r) for r in show["output_layout"]]
    return out


def parse_tod(text):
    """'21:58:30' / '21:58' / '2158' -> seconds of day, or None."""
    t = str(text).strip().replace(".", ":")
    if re.fullmatch(r"\d{3,4}", t):
        t = t[:-2] + ":" + t[-2:]
    parts = t.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    sec = int(parts[2]) if len(parts) == 3 else 0
    if h > 23 or m > 59 or sec > 59:
        return None
    return h * 3600 + m * 60 + sec


def load_show(name):
    show = copy_show(DEFAULT_SHOW)
    if name:
        try:
            with open(os.path.join(data_dir("shows"), _slug(name) + ".json"),
                      encoding="utf-8") as f:
                show = copy_show(json.load(f))
        except Exception:
            pass
    return show


def save_show(show):
    with open(os.path.join(data_dir("shows"), _slug(show["name"]) + ".json"),
              "w", encoding="utf-8") as f:
        json.dump(show, f, indent=2)


def delete_show(name):
    try:
        os.remove(os.path.join(data_dir("shows"), _slug(name) + ".json"))
    except OSError:
        pass


def import_logo(src):
    """Copy a logo into the app's logo store; returns the stored file name."""
    base, ext = os.path.splitext(os.path.basename(src))
    dest = f"{_slug(base)}_{int(time.time())}{ext.lower()}"
    shutil.copy2(src, os.path.join(data_dir("logos"), dest))
    return dest


def logo_path(stored):
    if not stored:
        return ""
    p = os.path.join(data_dir("logos"), stored)
    return p if os.path.exists(p) else ""


# --------------------------------------------------------------------------
# Logo display: scales an image to fit its space, keeping the aspect ratio
# --------------------------------------------------------------------------
class LogoLabel(tk.Label):
    def __init__(self, master, **kw):
        super().__init__(master, bd=0, **kw)
        self._path = ""
        self._src = None
        self._photo = None
        self._last = None
        self.bind("<Configure>", self._refit)

    def set_image(self, path):
        self._path, self._src, self._last = path, None, None
        if path:
            try:
                self._src = (Image.open(path).convert("RGBA") if Image
                             else tk.PhotoImage(file=path))
            except Exception:
                self._src = None
        if self._src is None:
            self._photo = None
            self.config(image="")
        self._refit()

    def _refit(self, _event=None):
        if self._src is None:
            return
        w, h = self.winfo_width() - 8, self.winfo_height() - 8
        if w < 8 or h < 8 or (w, h) == self._last:
            return
        self._last = (w, h)
        if Image:
            iw, ih = self._src.size
            scale = min(w / iw, h / ih)
            size = (max(int(iw * scale), 1), max(int(ih * scale), 1))
            self._photo = ImageTk.PhotoImage(
                self._src.resize(size, Image.LANCZOS))
        else:
            iw, ih = self._src.width(), self._src.height()
            if iw <= w and ih <= h:
                factor = max(1, min(w // iw, h // ih))
                self._photo = self._src.zoom(factor)
            else:
                factor = max(-(-iw // w), -(-ih // h))
                self._photo = self._src.subsample(factor)
        self.config(image=self._photo)


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------
class Stopwatch:
    """Drift-free stopwatch based on the monotonic clock."""

    def __init__(self):
        self.base = 0.0          # seconds accumulated while stopped / preset
        self.started_at = None   # monotonic timestamp when running

    @property
    def running(self):
        return self.started_at is not None

    def start(self):
        if not self.running:
            self.started_at = time.monotonic()

    def stop(self):
        if self.running:
            self.base += time.monotonic() - self.started_at
            self.started_at = None

    def set(self, seconds):
        self.base = float(seconds)
        if self.running:
            self.started_at = time.monotonic()

    def elapsed(self):
        if self.running:
            return self.base + (time.monotonic() - self.started_at)
        return self.base


def fmt_short(seconds):
    """MM:SS under an hour, H:MM:SS above (for over/under readouts)."""
    s = int(round(abs(seconds)))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_delta(seconds):
    """Signed over/under, e.g. +01:23 (over) or -00:42 (under)."""
    if int(round(seconds)) == 0:
        return "00:00"
    return ("+" if seconds > 0 else "-") + fmt_short(seconds)


def parse_duration(text):
    """'2:30' -> 150, '1:02:30' -> 3750, '5' or '5m' -> 300 (minutes),
    '90s' -> 90, '2.5' -> 150. Returns None if it can't be read."""
    t = str(text).strip().lower().replace(" ", "")
    if not t:
        return None
    try:
        if ":" in t:
            parts = [float(x) if x else 0.0 for x in t.split(":")]
            if len(parts) > 3:
                return None
            secs = 0.0
            for part in parts:
                secs = secs * 60 + part
            return int(round(secs))
        if t.endswith("s"):
            return int(round(float(t[:-1])))
        if t.endswith("m"):
            t = t[:-1]
        return int(round(float(t) * 60))
    except ValueError:
        return None


def fmt(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Auto-sizing label: picks the largest font that fits its space
# --------------------------------------------------------------------------
class FitLabel(tk.Label):
    def __init__(self, master, sample, fill=0.92, family=None, weight=None,
                 **kw):
        self._base_family = family or DIGIT_FONT
        self._base_weight = weight
        self._family, self._weight = self._resolve()
        self._font = tkfont.Font(family=self._family, size=-40,
                                 weight=self._weight)
        super().__init__(master, font=self._font, **kw)
        _FIT_LABELS.add(self)
        self._sample = sample
        self._fill = fill
        self._last = None
        self.bind("<Configure>", self._refit)

    def _resolve(self):
        base = self._base_family
        weight = self._base_weight or (DIGIT_WEIGHT if base == DIGIT_FONT
                                       else "normal")
        mapped = ft(base, -40)
        if mapped[0] != base:
            weight = "bold" if "bold" in mapped[2:] else "normal"
        return mapped[0], weight

    def apply_font_choice(self):
        fam, weight = self._resolve()
        if (fam, weight) != (self._family, self._weight):
            self._family, self._weight = fam, weight
            self._font.configure(family=fam, weight=weight)
            self._last = None
            self._refit()

    def _refit(self, _event=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10 or (w, h) == self._last:
            return
        self._last = (w, h)
        probe = tkfont.Font(family=self._family, size=-100,
                            weight=self._weight)
        tw = max(probe.measure(self._sample), 1)
        th = max(probe.metrics("linespace"), 1)
        px = int(100 * min(w * self._fill / tw, h * self._fill / th))
        self._font.configure(size=-max(px, 8))

    def set_text(self, text, sample=None):
        self.config(text=text)
        self._sample = sample or text or " "
        self._last = None
        self._refit()


# --------------------------------------------------------------------------
# Drawn widgets (Pillow): rounded buttons with icons, panels with a tally
# light, status pills. Each falls back to a flat tk look without Pillow.
# --------------------------------------------------------------------------
def _ui_font_file(heavy=True):
    names = ("DASans-Bold.ttf", "DASans-SemiBold.ttf") if heavy else \
        ("DASans-SemiBold.ttf", "DASans.ttf")
    for n in names:
        p = os.path.join(FONT_DIR, n)
        if os.path.exists(p):
            return p
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for p in (os.path.join(windir, "Fonts", "seguisb.ttf"),
              os.path.join(windir, "Fonts", "segoeuib.ttf"),
              os.path.join(windir, "Fonts", "arialbd.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return p
    return None


def _mix(c1, c2, t):
    """Blend two #rrggbb colours (t=0 -> c1, t=1 -> c2)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


_PIL_FONTS = {}


def _pil_font(px, heavy=True):
    key = (px, heavy)
    if key not in _PIL_FONTS:
        from PIL import ImageFont
        path = _ui_font_file(heavy)
        try:
            _PIL_FONTS[key] = (ImageFont.truetype(path, px) if path
                               else ImageFont.load_default(size=px))
        except Exception:
            _PIL_FONTS[key] = ImageFont.load_default()
    return _PIL_FONTS[key]


def _draw_icon(draw, kind, x, cy, s, col, bg):
    """Small vector icons, s = icon height in px, left edge at x."""
    import math
    if kind == "play":
        draw.polygon([(x, cy - s / 2), (x, cy + s / 2), (x + s * 0.86, cy)],
                     fill=col)
        return s * 0.86
    if kind == "stop":
        draw.rounded_rectangle([x, cy - s * 0.42, x + s * 0.84, cy + s * 0.42],
                               radius=s * 0.12, fill=col)
        return s * 0.84
    if kind == "next":
        draw.polygon([(x, cy - s / 2), (x, cy + s / 2), (x + s * 0.7, cy)],
                     fill=col)
        draw.rectangle([x + s * 0.74, cy - s / 2, x + s * 0.9, cy + s / 2],
                       fill=col)
        return s * 0.9
    if kind == "reset":
        r = s * 0.44
        c = (x + r, cy)
        w = max(2, int(s * 0.16))
        draw.arc([c[0] - r, cy - r, c[0] + r, cy + r], 60, 330, fill=col,
                 width=w)
        a = math.radians(330)
        px, py = c[0] + r * math.cos(a), cy + r * math.sin(a)
        draw.polygon([(px - s * 0.22, py - s * 0.08), (px + s * 0.16,
                      py - s * 0.22), (px + s * 0.06, py + s * 0.2)], fill=col)
        return 2 * r
    if kind == "preset":     # stopwatch
        r = s * 0.4
        c = (x + r, cy + s * 0.06)
        w = max(2, int(s * 0.14))
        draw.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=col,
                     width=w)
        draw.line([c[0], c[1], c[0], c[1] - r * 0.6], fill=col, width=w)
        draw.rectangle([c[0] - s * 0.1, c[1] - r - s * 0.16, c[0] + s * 0.1,
                        c[1] - r - s * 0.06], fill=col)
        return 2 * r
    return 0


class ModernButton(tk.Label):
    #             fill        border      text
    STYLES = {"neutral":  (SURFACE_2, BORDER, TEXT),
              "ghost":    (SURFACE, SURFACE, MUTED),
              "primary":  (CYAN, CYAN, "#00181b"),
              "on":       (CYAN_DIM, "#0b5a63", CYAN),
              "go":       (GREEN, GREEN, "#04140b"),
              "stop":     (RED, RED, "#ffffff"),
              "stop_dim": (RED_DIM, "#6b2630", "#ff8592")}

    def __init__(self, master, text, command, page_bg, variant="neutral",
                 font_px=None, size=None, tracking=0.06, icon=None,
                 radius=None, _scaled=False):
        super().__init__(master, bd=0, bg=page_bg, cursor="hand2",
                         highlightthickness=0)
        if not _scaled:
            font_px = px(font_px) if font_px else font_px
            size = (px(size[0]), px(size[1])) if size else size
            radius = px(radius) if radius is not None else radius
        self.caret = text.endswith("\u25be")
        text = text.rstrip("\u25be ").rstrip()
        if any(c.islower() for c in text):
            tracking = min(tracking, 0.01)
        self.text, self.command, self.page_bg = text, command, page_bg
        self.variant, self.enabled = variant, True
        self.pressed = self.hover = False
        self.font_px, self.fixed, self.tracking = font_px, size, tracking
        self.icon, self.radius = icon, radius
        self._photo, self._key = None, None
        self.bind("<Configure>", lambda e: self._render())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        if size:
            self._render()

    @classmethod
    def _font(cls, px):
        return _pil_font(px, heavy=True)

    @classmethod
    def measure(cls, text, px, tracking=0.06):
        caret = px * 0.95 if text.endswith("\u25be") else 0
        text = text.rstrip("\u25be ").rstrip()
        if Image is None:
            return tkfont.Font(family=UI_BOLD, size=-px).measure(text) + caret
        f = _pil_font(px)
        return sum(f.getlength(ch) for ch in text) + px * tracking * \
            max(len(text) - 1, 0) + caret

    @classmethod
    def sized(cls, master, text, command, page_bg, variant="neutral",
              font_px=13, height=34, pad_x=18, tracking=0.06):
        if any(c.islower() for c in text):
            tracking = min(tracking, 0.01)
        font_px = px(font_px)
        w = int(cls.measure(text, font_px, tracking) + 2 * px(pad_x))
        return cls(master, text, command, page_bg, variant, font_px=font_px,
                   size=(w, px(height)), tracking=tracking, _scaled=True)

    def set_variant(self, variant):
        if variant != self.variant:
            self.variant = variant
            self._render()

    def set_enabled(self, enabled):
        if enabled != self.enabled:
            self.enabled = enabled
            self.config(cursor="hand2" if enabled else "arrow")
            self._render()

    def _set_hover(self, on):
        self.hover = on
        self._render()

    def _press(self, _e):
        if self.enabled:
            self.pressed = True
            self._render()

    def _release(self, e):
        was = self.pressed
        self.pressed = False
        self._render()
        w, h = self.fixed or (self.winfo_width(), self.winfo_height())
        if was and 0 <= e.x < w and 0 <= e.y < h and self.enabled \
                and self.command:
            self.command()

    def _colours(self):
        fill, border, text = self.STYLES[self.variant]
        if not self.enabled:
            return (_mix(SURFACE_2, self.page_bg, 0.5),
                    _mix(BORDER, self.page_bg, 0.5), FAINT)
        if self.pressed:
            fill = _mix(fill, "#000000", 0.2)
        elif self.hover:
            if self.variant == "ghost":
                fill, border, text = SURFACE_2, SURFACE_2, TEXT
            else:
                fill = _mix(fill, "#ffffff", 0.07)
                if self.variant == "neutral":
                    border = _mix(border, CYAN, 0.4)
        return fill, border, text

    def _render(self):
        w, h = self.fixed or (self.winfo_width(), self.winfo_height())
        if w < 12 or h < 12:
            return
        fill, border, text_col = self._colours()
        key = (w, h, fill, border, text_col, self.text, self.icon)
        if key == self._key:
            return
        self._key = key
        if Image is None:
            self.config(text=self.text, bg=fill, fg=text_col, image="",
                        font=(UI_BOLD, -(self.font_px or max(10, int(min(
                            h * 0.3, w * 1.25 / max(len(self.text), 1)))))))
            if self.fixed:
                self.config(width=max(1, w // 9))
            return

        from PIL import ImageDraw
        s = 3
        radius = self.radius if self.radius is not None else \
            max(px(4), min(h * 0.18, px(12)))
        big = Image.new("RGBA", (w * s, h * s), self.page_bg)
        ImageDraw.Draw(big).rounded_rectangle(
            [0, 0, w * s - 1, h * s - 1], radius=radius * s, fill=fill,
            outline=border, width=max(2, int(1.0 * s)))
        img = big.resize((w, h), Image.LANCZOS)
        draw = ImageDraw.Draw(img)

        fpx = self.font_px or max(8, int(h * 0.28))
        icon_h = fpx * 0.86 if self.icon else 0
        gap = fpx * 0.6 if self.icon else 0
        while True:
            total = self.measure(self.text, fpx, self.tracking)
            if self.caret:
                total += fpx * 0.95
            full = total + (icon_h + gap if self.icon else 0)
            if self.font_px or full <= w * 0.8 or fpx <= 8:
                break
            fpx -= 1
            icon_h = fpx * 0.86 if self.icon else 0
            gap = fpx * 0.6 if self.icon else 0
        font = self._font(fpx)
        x = (w - full) / 2
        y = h / 2 + (1 if self.pressed else 0)
        if self.icon:
            x += _draw_icon(draw, self.icon, x, y, icon_h, text_col, fill) + gap
        for ch in self.text:
            draw.text((x, y), ch, font=font, fill=text_col, anchor="lm")
            x += font.getlength(ch) + fpx * self.tracking
        if self.caret:
            cw = fpx * 0.5
            x += fpx * 0.35
            draw.polygon([(x, y - cw * 0.3), (x + cw, y - cw * 0.3),
                          (x + cw / 2, y + cw * 0.35)], fill=text_col)
        self._photo = ImageTk.PhotoImage(img.convert("RGB"))
        self.config(image=self._photo, text="")
        if self.fixed:
            self.config(width=w, height=h)


class Panel(tk.Label):
    """Rounded panel background. Optional tally light down the left edge."""

    def __init__(self, master, page_bg=BG, fill=SURFACE, border=BORDER,
                 radius=14):
        super().__init__(master, bd=0, bg=page_bg, highlightthickness=0)
        self.page_bg, self.fill, self.border, self.radius = \
            page_bg, fill, border, px(radius)
        self.tally = None
        self._photo, self._key = None, None
        self.bind("<Configure>", lambda e: self._render())

    def set_tally(self, colour):
        if colour != self.tally:
            self.tally = colour
            self._render()

    def set_border(self, colour):
        if colour != self.border:
            self.border = colour
            self._render()

    def _render(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        key = (w, h, self.tally, self.border, self.fill)
        if key == self._key:
            return
        self._key = key
        if Image is None:
            self.config(bg=self.fill, highlightthickness=1,
                        highlightbackground=self.border)
            return
        from PIL import ImageDraw, ImageFilter
        s = 2
        big = Image.new("RGBA", (w * s, h * s), self.page_bg)
        d = ImageDraw.Draw(big)
        d.rounded_rectangle([0, 0, w * s - 1, h * s - 1],
                            radius=self.radius * s, fill=self.fill,
                            outline=self.border, width=2 * s // 2)
        if self.tally:
            # glow first, then the solid tally bar on top
            glow = Image.new("RGBA", big.size, (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            k = UI_SCALE * s
            x0, y0, y1 = 12 * k, 16 * k, h * s - 16 * k
            gd.rounded_rectangle([x0 - 4 * k, y0 - 2 * k, x0 + 12 * k,
                                  y1 + 2 * k], radius=8 * k,
                                 fill=self.tally + "70")
            glow = glow.filter(ImageFilter.GaussianBlur(7 * k))
            mask = Image.new("L", big.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [s, s, w * s - 2 * s, h * s - 2 * s], radius=self.radius * s,
                fill=255)
            big.paste(Image.alpha_composite(big, glow), (0, 0), mask)
            ImageDraw.Draw(big).rounded_rectangle(
                [x0, y0, x0 + 7 * k, y1], radius=4 * k, fill=self.tally)
        img = big.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img.convert("RGB"))
        self.config(image=self._photo)


class Pill(tk.Label):
    """A status pill: rounded badge with an optional dot."""

    def __init__(self, master, page_bg=SURFACE, height=30, font_px=13):
        super().__init__(master, bd=0, bg=page_bg, highlightthickness=0)
        height, font_px = px(height), px(font_px)
        self.page_bg, self.h, self.px = page_bg, height, font_px
        self._photo, self._key = None, None
        self.width_px = height

    def set(self, text, fill, fg, dot=True):
        key = (text, fill, fg, dot, self.h, self.px)
        if key == self._key:
            return
        self._key = key
        if Image is None:
            self.config(text=("\u25cf " if dot else "") + text, bg=fill, fg=fg,
                        font=(UI_BOLD, -self.px), padx=10)
            return
        from PIL import ImageDraw
        font = _pil_font(self.px, heavy=True)
        tw = font.getlength(text)
        dot_w = self.px * 0.9 if dot else 0
        w = int(tw + dot_w + self.h * 0.9)
        h = self.h
        s = 3
        big = Image.new("RGBA", (w * s, h * s), self.page_bg)
        ImageDraw.Draw(big).rounded_rectangle([0, 0, w * s - 1, h * s - 1],
                                              radius=h * s / 2, fill=fill)
        img = big.resize((w, h), Image.LANCZOS)
        d = ImageDraw.Draw(img)
        x = h * 0.45
        if dot:
            r = self.px * 0.24
            d.ellipse([x, h / 2 - r, x + 2 * r, h / 2 + r], fill=fg)
            x += dot_w
        d.text((x, h / 2), text, font=font, fill=fg, anchor="lm")
        self._photo = ImageTk.PhotoImage(img.convert("RGB"))
        self.config(image=self._photo, width=w, height=h)
        self.width_px = w


class Bar(tk.Canvas):
    """Rounded progress bar."""

    def __init__(self, master, bg=SURFACE, track=SURFACE_3, thickness=8):
        thickness = px(thickness)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=thickness)
        self.track, self.t = track, thickness
        self.ratio, self.colour = 0.0, CYAN
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, ratio, colour):
        ratio = round(min(max(ratio, 0.0), 1.0), 3)
        if (ratio, colour) != (self.ratio, self.colour):
            self.ratio, self.colour = ratio, colour
            self._draw()

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        self.delete("all")
        t = min(self.t, h)
        y = h / 2
        self.create_line(t / 2, y, w - t / 2, y, width=t, fill=self.track,
                         capstyle="round")
        if self.ratio > 0:
            x = t / 2 + (w - t) * self.ratio
            self.create_line(t / 2, y, max(x, t / 2 + 0.1), y, width=t,
                             fill=self.colour, capstyle="round")


# --------------------------------------------------------------------------
# Number pad preset dialog (digits shift in from the right, like the original)
# --------------------------------------------------------------------------
class NumPad(tk.Toplevel):
    def __init__(self, master, title, on_enter):
        super().__init__(master)
        self.title(title)
        self.configure(bg=SURFACE, padx=px(18), pady=px(16))
        self.resizable(False, False)
        self.transient(master)
        self.on_enter = on_enter
        self.digits = ["0"] * 6

        tk.Label(self, text=title, bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 14)).grid(row=0, column=0,
                                                  columnspan=3, sticky="w")
        self.display = tk.Label(self, text="00:00:00", bg=BG, fg=TEXT,
                                font=(DIGIT_FONT, 34, DIGIT_WEIGHT),
                                padx=px(14), pady=px(6), highlightthickness=1,
                                highlightbackground=BORDER)
        self.display.grid(row=1, column=0, columnspan=3, sticky="ew",
                          pady=(6, 12))

        keys = [("7", 2, 0), ("8", 2, 1), ("9", 2, 2),
                ("4", 3, 0), ("5", 3, 1), ("6", 3, 2),
                ("1", 4, 0), ("2", 4, 1), ("3", 4, 2),
                ("C", 5, 0), ("0", 5, 1), ("DEL", 5, 2)]
        for text, r, c in keys:
            if text.isdigit():
                cmd = lambda d=text: self.push(d)
            elif text == "C":
                cmd = self.clear
            else:
                cmd = self.backspace
            ModernButton(self, text, cmd, SURFACE, font_px=22,
                         size=(84, 58), tracking=0).grid(row=r, column=c,
                                                         padx=3, pady=3)

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ModernButton(bottom, "Cancel", self.destroy, SURFACE, font_px=13,
                     size=(110, 48)).pack(side="left", padx=3)
        ModernButton(bottom, "Set time", self.enter, SURFACE, "primary",
                     font_px=14, size=(152, 48)).pack(side="right", padx=3)

        self.bind("<Key>", self._on_key)
        self.bind("<Return>", lambda e: self.enter())
        self.bind("<KP_Enter>", lambda e: self.enter())
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<BackSpace>", lambda e: self.backspace())
        self.bind("<Delete>", lambda e: self.clear())

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.focus_force()

    def _on_key(self, e):
        if e.char and e.char.isdigit():
            self.push(e.char)

    def refresh(self):
        d = self.digits
        self.display.config(text=f"{d[0]}{d[1]}:{d[2]}{d[3]}:{d[4]}{d[5]}")

    def push(self, digit):
        self.digits = self.digits[1:] + [digit]
        self.refresh()

    def backspace(self):
        self.digits = ["0"] + self.digits[:-1]
        self.refresh()

    def clear(self):
        self.digits = ["0"] * 6
        self.refresh()

    def enter(self):
        d = self.digits
        h, m, s = int(d[0] + d[1]), int(d[2] + d[3]), int(d[4] + d[5])
        # Values like 00:90:00 are normalised (= 01:30:00) instead of crashing
        self.on_enter(h * 3600 + m * 60 + s)
        self.destroy()


# --------------------------------------------------------------------------
# Output (dual screen) window
# --------------------------------------------------------------------------
# Output screen elements: id, name in the layout editor, default height
# share, default colour role ("fg" text, "accent" labels, "auto" status)
OUTPUT_ELEMENTS = [
    ("title", "Title and logos", 2.4, "fg"),
    ("seg_title", "Segment name line", 1.4, "accent"),
    ("seg_time", "Segment clock", 5.0, "fg"),
    ("seg_status", "Time left / over", 1.1, "auto"),
    ("show_title", "Program name line", 1.4, "accent"),
    ("show_time", "Program clock", 5.0, "fg"),
    ("clock", "Time of day / timecode", 3.2, "fg"),
    ("message", "Bottom message", 1.4, "fg"),
]
OUTPUT_DEFAULTS = {e[0]: e for e in OUTPUT_ELEMENTS}


def _contrast(colour):
    """Black or white, whichever reads better on `colour` ("#rrggbb")."""
    try:
        c = colour.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 > 140 \
            else "#ffffff"
    except Exception:
        return TEXT


def output_layout(show):
    """The show's output layout: a list of {id, show, size, colour, ...} in
    screen order. Shows saved before the layout editor are converted from
    their old switches."""
    saved = show.get("output_layout")
    if saved:
        known = {r.get("id") for r in saved}
        rows = [dict(r) for r in saved if r.get("id") in OUTPUT_DEFAULTS]
        for eid, *_ in OUTPUT_ELEMENTS:          # elements added later
            if eid not in known:
                rows.append({"id": eid, "show": False, "size": 1.0,
                             "colour": ""})
    else:
        clock = show.get("output_clock") or "off"
        rows = []
        for eid, *_ in OUTPUT_ELEMENTS:
            on = True
            if eid == "seg_status":
                on = bool(show.get("output_goals"))
            elif eid == "clock":
                on = clock != "off"
            rows.append({"id": eid, "show": on, "size": 1.0, "colour": ""})
    for r in rows:
        r.setdefault("show", True)
        r.setdefault("size", 1.0)
        r.setdefault("colour", "")
        if r["id"] == "clock":
            r.setdefault("mode", show.get("output_clock") if
                         show.get("output_clock") in ("tod", "tc") else "tod")
        if r["id"] == "seg_title":
            r.setdefault("segments_left", bool(show.get("output_segments",
                                                        True)))
    return rows


def output_row(show, eid):
    for r in output_layout(show):
        if r["id"] == eid:
            return r
    return {"id": eid, "show": False, "size": 1.0, "colour": ""}


class OutputView(tk.Frame):
    """What the presenter / floor sees. Used full screen by OutputWindow and
    small as the live preview in the layout editor."""

    def __init__(self, master, show):
        super().__init__(master, bg=OUT_BG)
        self.logo_left = LogoLabel(self)
        self.logo_right = LogoLabel(self)
        self.title_lbl = FitLabel(self, " ", fill=0.8, family=UI_BOLD)
        self.seg_title = FitLabel(self, "SEGMENT TIME", fill=0.75,
                                  family=UI_BOLD)
        self.seg_time = FitLabel(self, "00:00:00", text="00:00:00")
        self.seg_status = FitLabel(self, "+00:00 OVER", fill=0.75,
                                   family=UI_BOLD)
        self.show_title = FitLabel(self, "SEGMENT TIME", fill=0.75,
                                   family=UI_BOLD)
        self.show_time = FitLabel(self, "00:00:00", text="00:00:00")
        self.message_lbl = FitLabel(self, " ", fill=0.7, family=UI_BOLD)
        self.clock_cap = FitLabel(self, "TIME OF DAY", fill=0.75,
                                  family=UI_BOLD)
        self.clock_time = FitLabel(self, "00:00:00", fill=0.88)
        self.rule = tk.Frame(self, height=2)
        self.clock_mode = "off"
        self.status_colour = ""
        self.apply_show(show)

    def apply_show(self, show):
        bg, fg = show.get("bg") or OUT_BG, show.get("fg") or OUT_FG
        accent = show.get("accent") or OUT_ACCENT
        layout = output_layout(show)
        rows = {r["id"]: r for r in layout}

        def colour(eid):
            r = rows.get(eid) or {}
            if r.get("colour"):
                return r["colour"]
            role = OUTPUT_DEFAULTS[eid][3]
            return accent if role == "accent" else fg

        self.configure(bg=bg)
        for w in self.winfo_children():
            w.configure(bg=bg)
        for eid, widgets in (("title", (self.title_lbl,)),
                             ("seg_title", (self.seg_title,)),
                             ("seg_time", (self.seg_time,)),
                             ("seg_status", (self.seg_status,)),
                             ("show_title", (self.show_title,)),
                             ("show_time", (self.show_time,)),
                             ("clock", (self.clock_time,)),
                             ("message", (self.message_lbl,))):
            for w in widgets:
                w.configure(fg=colour(eid))
        self.status_colour = (rows.get("seg_status") or {}).get("colour", "")
        self.seg_colour = colour("seg_time")
        self.seg_time.set_text(self.seg_time.cget("text") or "00:00:00",
                               "+00:00:00")
        self.clock_cap.configure(fg=accent)
        self.rule.configure(bg=accent)
        seg_lbl = show["segment_label"].upper()
        show_lbl = show["show_label"].upper()
        longest = max(seg_lbl, show_lbl, "SEGMENT TIME", key=len)
        if (rows.get("seg_title") or {}).get("segments_left"):
            longest = max(longest, f"{seg_lbl} 88 OF 88  ·  88 LEFT",
                          key=len)
        self.seg_title.set_text(seg_lbl, longest)
        self.show_title.set_text(show_lbl, longest)
        self.title_lbl.set_text(show["title"])
        self.message_lbl.set_text(show["message"])
        left, right = logo_path(show["logo_left"]), logo_path(show["logo_right"])
        self.logo_left.set_image(left)
        self.logo_right.set_image(right)

        for w in self.winfo_children():
            w.place_forget()
        bands = []
        for r in layout:
            eid = r["id"]
            if not r.get("show"):
                continue
            if eid == "title" and not (show["title"] or left or right):
                continue
            if eid == "message" and not show["message"]:
                continue
            size = min(max(float(r.get("size") or 1.0), 0.25), 4.0)
            bands.append((eid, OUTPUT_DEFAULTS[eid][2] * size))
        clock = rows.get("clock") or {}
        self.clock_mode = (clock.get("mode") or "tod") if clock.get("show") \
            else "off"
        if self.clock_mode != "off":
            self.clock_time.set_text(
                "", "00:00:00:00" if self.clock_mode == "tc" else "00:00:00")
            self.clock_cap.set_text("TIME OF DAY", longest)
        total = sum(wgt for _, wgt in bands) or 1
        y = 0.0
        for name, wgt in bands:
            h = wgt / total
            if name == "title":
                if left or right:
                    self.logo_left.place(relx=0.01, rely=y, relwidth=0.22,
                                         relheight=h)
                    self.logo_right.place(relx=0.77, rely=y, relwidth=0.22,
                                          relheight=h)
                    self.title_lbl.place(relx=0.24, rely=y, relwidth=0.52,
                                         relheight=h)
                else:
                    self.title_lbl.place(relx=0.02, rely=y, relwidth=0.96,
                                         relheight=h)
            elif name == "clock":
                # caption line above the time, like PROGRAM above its clock
                self.clock_cap.place(relx=0, rely=y, relwidth=1,
                                     relheight=h * 0.34)
                self.clock_time.place(relx=0, rely=y + h * 0.34, relwidth=1,
                                      relheight=h * 0.66)
            elif name == "message":
                self.rule.place(relx=0.25, rely=y, relwidth=0.5, height=2)
                self.message_lbl.place(relx=0, rely=y, relwidth=1,
                                       relheight=h)
                self.rule.lift()
            else:
                widget = {"seg_title": self.seg_title,
                          "seg_time": self.seg_time,
                          "seg_status": self.seg_status,
                          "show_title": self.show_title,
                          "show_time": self.show_time}[name]
                widget.place(relx=0, rely=y, relwidth=1, relheight=h)
            y += h

    def update_extras(self, seg_caption, clock_cap, clock_text):
        if self.seg_title.cget("text") != seg_caption:
            self.seg_title.config(text=seg_caption)
        if self.clock_mode != "off":
            if self.clock_cap.cget("text") != clock_cap:
                self.clock_cap.config(text=clock_cap)
            if self.clock_time.cget("text") != clock_text:
                self.clock_time.config(text=clock_text)

    def update_times(self, seg, show, status="", status_fg=None,
                     seg_fg=None):
        if self.seg_time.cget("text") != seg:
            self.seg_time.config(text=seg)
        want = seg_fg or self.seg_colour
        if self.seg_time.cget("fg") != want:
            self.seg_time.config(fg=want)
        if self.show_time.cget("text") != show:
            self.show_time.config(text=show)
        if self.seg_status.cget("text") != status:
            self.seg_status.config(text=status)
        status_fg = self.status_colour or status_fg
        if status_fg and self.seg_status.cget("fg") != status_fg:
            self.seg_status.config(fg=status_fg)


class OutputWindow(tk.Toplevel):
    """The output screen: an OutputView filling a second monitor."""

    def __init__(self, master, monitor, on_close, show):
        super().__init__(master, bg=OUT_BG)
        self.title(f"{APP_NAME} - Output")
        self.on_close = on_close
        self.monitor = monitor
        self.fullscreen = False
        self.view = OutputView(self, show)
        self.view.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._bind_dbl(self.view)
        self._cursor_job = None
        self._hide_cursor_soon()
        self.bind("<Double-Button-1>", self.toggle_fullscreen)
        self.bind("<Motion>", self._mouse_moved, add="+")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.go_fullscreen()

    def _set_cursor(self, cursor):
        stack = [self]
        while stack:
            w = stack.pop()
            try:
                w.configure(cursor=cursor)
            except tk.TclError:
                pass
            stack.extend(w.winfo_children())

    def _hide_cursor_soon(self):
        if self._cursor_job:
            self.after_cancel(self._cursor_job)
        self._cursor_job = self.after(2000, lambda: self._set_cursor("none"))

    def _mouse_moved(self, _e=None):
        self._set_cursor("")
        self._hide_cursor_soon()

    def _bind_dbl(self, w):
        w.bind("<Double-Button-1>", self.toggle_fullscreen)
        w.bind("<Motion>", self._mouse_moved, add="+")
        for c in w.winfo_children():
            self._bind_dbl(c)

    def __getattr__(self, name):          # seg_title, clock_time, ... on view
        view = self.__dict__.get("view")
        if view is not None and not name.startswith("_"):
            return getattr(view, name)
        raise AttributeError(name)

    def apply_show(self, show):
        self.configure(bg=show.get("bg") or OUT_BG)
        self.view.apply_show(show)

    def update_times(self, *a, **k):
        self.view.update_times(*a, **k)

    def update_extras(self, *a, **k):
        self.view.update_extras(*a, **k)

    def go_fullscreen(self):
        m = self.monitor
        self.overrideredirect(True)
        self.geometry(f"{m['w']}x{m['h']}+{m['x']}+{m['y']}")
        self.lift()
        self.fullscreen = True

    def go_windowed(self):
        m = self.monitor
        self.overrideredirect(False)
        w, h = m["w"] // 2, m["h"] // 2
        self.geometry(f"{w}x{h}+{m['x'] + m['w'] // 4}+{m['y'] + m['h'] // 4}")
        self.fullscreen = False

    def toggle_fullscreen(self, _event=None):
        if self.fullscreen:
            self.go_windowed()
        else:
            self.go_fullscreen()

    def move_to(self, monitor):
        self.monitor = monitor
        if self.fullscreen:
            self.go_fullscreen()
        else:
            self.go_windowed()

    def close(self):
        self.destroy()
        self.on_close()


# --------------------------------------------------------------------------
# Output layout editor: which elements show, how big, what colour, in what
# order - with a live preview, and the real output screen updating as you go
# --------------------------------------------------------------------------
class OutputLayoutEditor(tk.Toplevel):
    SIZES = [0.5, 0.65, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]

    def __init__(self, master, app, show, on_done, live=True):
        super().__init__(master)
        self.app, self.on_done, self.live = app, on_done, live
        self.original = copy_show(show)
        self.show = copy_show(show)
        self.rows = output_layout(self.show)
        self.title(f"Output layout - {show['name']}")
        self.configure(bg=SURFACE, padx=px(22), pady=px(18))
        self.transient(master)
        self.resizable(False, False)

        tk.Label(self, text="Output to monitor: layout", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 16)).grid(row=0, column=0, columnspan=2,
                                          sticky="w")
        tk.Label(self, text=f"{show['name']}  ·  changes show on the "
                            f"output straight away", bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 10)).grid(row=1, column=0, columnspan=2,
                                          sticky="w", pady=(0, 12))

        left = tk.Frame(self, bg=SURFACE)
        left.grid(row=2, column=0, sticky="nw")
        # whole-screen colours
        cols = tk.Frame(left, bg=SURFACE)
        cols.pack(anchor="w", pady=(0, 10))
        tk.Label(cols, text="Colours", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 11)).pack(side="left", padx=(0, 10))
        self.swatches = {}
        for key, text in (("bg", "Background"), ("fg", "Text"),
                          ("accent", "Labels")):
            b = tk.Label(cols, text=text, font=(UI_BOLD, 9), bd=0,
                         padx=px(10), pady=px(5), cursor="hand2")
            b.bind("<Button-1>", lambda e, k=key: self._pick_show_colour(k))
            b.pack(side="left", padx=(0, 6))
            self.swatches[key] = b

        self.list_frame = tk.Frame(left, bg=SURFACE)
        self.list_frame.pack(fill="x")

        # live preview
        right = tk.Frame(self, bg=SURFACE)
        right.grid(row=2, column=1, sticky="n", padx=(px(22), 0))
        tk.Label(right, text="Preview", bg=SURFACE, fg=MUTED,
                 font=(UI_BOLD, 10)).pack(anchor="w", pady=(0, 4))
        mon = app._monitor_choice()
        aspect = (mon["h"] / mon["w"]) if mon.get("w") else 9 / 16
        pw = px(420)
        holder = tk.Frame(right, width=pw, height=int(pw * aspect), bg=BORDER,
                          highlightthickness=1, highlightbackground=BORDER)
        holder.pack()
        holder.pack_propagate(False)
        self.preview = OutputView(holder, self.show)
        self.preview.pack(fill="both", expand=True)
        tk.Label(right, text="Tip: double-click the output to switch it "
                             "between full screen and a window.",
                 bg=SURFACE, fg=FAINT, font=(UI_FONT, 9), wraplength=pw,
                 justify="left").pack(anchor="w", pady=(6, 0))

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ModernButton.sized(bottom, "Reset layout", self.reset, SURFACE,
                           "neutral", font_px=13, height=38).pack(side="left")
        ModernButton.sized(bottom, "Done", self.done, SURFACE, "primary",
                           font_px=14, height=38, pad_x=22).pack(side="right")
        ModernButton.sized(bottom, "Cancel", self.cancel, SURFACE, "neutral",
                           font_px=13, height=38).pack(side="right",
                                                       padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        self._build_rows()
        self._changed(initial=True)
        self._tick()
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    # ----------------------------------------------------------- rows --
    def _build_rows(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        for col, text in enumerate(("Show", "Element", "Size", "Colour", "",
                                    "Order")):
            tk.Label(self.list_frame, text=text, bg=SURFACE, fg=FAINT,
                     anchor="w", font=(UI_BOLD, 9)).grid(
                row=0, column=col, sticky="w", pady=(0, 4),
                padx=(4, 0) if col == 2 else 0)
        for i, r in enumerate(self.rows, start=1):
            eid = r["id"]
            name = OUTPUT_DEFAULTS[eid][1]
            f = self.list_frame
            sw = Switch(f, r.get("show"), lambda v, r=r: self._set(r, show=v))
            sw.grid(row=i, column=0, sticky="w", pady=px(3))
            note = ""
            if eid == "title" and not (self.show["title"] or
                                       self.show["logo_left"] or
                                       self.show["logo_right"]):
                note = "  (no title or logos set)"
            if eid == "message" and not self.show["message"]:
                note = "  (no message set)"
            tk.Label(f, text=name + note, bg=SURFACE,
                     fg=TEXT if not note else MUTED, anchor="w", width=26,
                     font=(UI_FONT, 11)).grid(row=i, column=1, sticky="w")
            size = tk.Frame(f, bg=SURFACE)
            size.grid(row=i, column=2, sticky="w", padx=(4, 10))
            ModernButton(size, "−", lambda r=r: self._size(r, -1),
                         SURFACE, font_px=14, size=(28, 26),
                         tracking=0).pack(side="left")
            tk.Label(size, text=f"{float(r.get('size') or 1) * 100:.0f}%",
                     bg=SURFACE, fg=TEXT, width=5,
                     font=(UI_FONT, 10)).pack(side="left")
            ModernButton(size, "+", lambda r=r: self._size(r, 1), SURFACE,
                         font_px=14, size=(28, 26),
                         tracking=0).pack(side="left")
            colour = r.get("colour") or self._default_colour(eid)
            sw_lbl = tk.Label(f, text="Auto" if (eid == "seg_status" and
                                                  not r.get("colour"))
                              else ("Default" if not r.get("colour")
                                    else " "),
                              bg=colour if colour != "auto" else SURFACE_2,
                              fg=_contrast(colour) if colour != "auto"
                              else MUTED, width=8, cursor="hand2",
                              font=(UI_FONT, 9), highlightthickness=1,
                              highlightbackground=BORDER)
            sw_lbl.grid(row=i, column=3, sticky="w", padx=(0, 4))
            sw_lbl.bind("<Button-1>", lambda e, r=r: self._pick_colour(r))
            extra = tk.Frame(f, bg=SURFACE)
            extra.grid(row=i, column=4, sticky="w", padx=(0, 8))
            if r.get("colour"):
                ModernButton(extra, "Reset", lambda r=r: self._set(
                    r, colour=""), SURFACE, "ghost", font_px=10,
                    size=(52, 24)).pack(side="left", padx=(0, 6))
            if eid == "seg_title":
                v = tk.BooleanVar(value=bool(r.get("segments_left")))
                tk.Checkbutton(extra, text="3 of 8 · 5 left",
                               variable=v, bg=SURFACE, fg=TEXT,
                               selectcolor=BG, activebackground=SURFACE,
                               activeforeground=TEXT, highlightthickness=0,
                               bd=0, font=(UI_FONT, 10),
                               command=lambda r=r, v=v: self._set(
                                   r, segments_left=bool(v.get()))
                               ).pack(side="left")
                extra._var = v
            if eid == "seg_time":
                count = r.get("count") or "up"
                for val, text in (("up", "Count up"), ("down", "Count down")):
                    ModernButton(extra, text, lambda r=r, val=val: self._set(
                        r, count=val), SURFACE,
                        "on" if count == val else "ghost", font_px=10,
                        size=(84, 24)).pack(side="left", padx=(0, 4))
            if eid == "clock":
                mode = r.get("mode") or "tod"
                for val, text in (("tod", "TOD"), ("tc", "Timecode")):
                    ModernButton(extra, text, lambda r=r, val=val: self._set(
                        r, mode=val), SURFACE,
                        "on" if mode == val else "ghost", font_px=10,
                        size=(74, 24)).pack(side="left", padx=(0, 4))
            order = tk.Frame(f, bg=SURFACE)
            order.grid(row=i, column=5, sticky="w")
            ModernButton(order, "Up", lambda i=i: self._move(i - 1, -1),
                         SURFACE, "ghost", font_px=10, size=(40, 24),
                         tracking=0).pack(side="left")
            ModernButton(order, "Down", lambda i=i: self._move(i - 1, 1),
                         SURFACE, "ghost", font_px=10, size=(48, 24),
                         tracking=0).pack(side="left")
        remap_fonts(self.list_frame)

    def _default_colour(self, eid):
        role = OUTPUT_DEFAULTS[eid][3]
        if role == "auto":
            return "auto"
        return self.show.get("accent") if role == "accent" else \
            self.show.get("fg")

    # --------------------------------------------------------- changes --
    def _set(self, r, **kw):
        r.update(kw)
        self._build_rows()
        self._changed()

    def _size(self, r, step):
        cur = float(r.get("size") or 1.0)
        i = min(range(len(self.SIZES)), key=lambda k: abs(self.SIZES[k] - cur))
        r["size"] = self.SIZES[min(max(i + step, 0), len(self.SIZES) - 1)]
        self._build_rows()
        self._changed()

    def _move(self, i, step):
        j = i + step
        if 0 <= j < len(self.rows):
            self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            self._build_rows()
            self._changed()

    def _pick_colour(self, r):
        start = r.get("colour") or self._default_colour(r["id"])
        _rgb, hexcol = colorchooser.askcolor(
            None if start == "auto" else start, parent=self,
            title=f"Colour - {OUTPUT_DEFAULTS[r['id']][1]}")
        if hexcol:
            self._set(r, colour=hexcol)

    def _pick_show_colour(self, key):
        _rgb, hexcol = colorchooser.askcolor(self.show.get(key), parent=self,
                                             title="Choose colour")
        if hexcol:
            self.show[key] = hexcol
            self._build_rows()
            self._changed()

    def _changed(self, initial=False):
        self.show["output_layout"] = [dict(r) for r in self.rows]
        for key, b in self.swatches.items():
            col = self.show.get(key)
            b.config(bg=col, fg=_contrast(col))
        self.preview.apply_show(self.show)
        if self.live and not initial:
            self.app.preview_output_show(self.show)

    def _tick(self):
        if not self.winfo_exists():
            return
        seg, show, status, col, caption, ccap, ctext, scol = \
            self.app.output_values(self.show)
        self.preview.update_times(seg, show, status or "+00:12 OVER",
                                  col or RED, scol)
        self.preview.update_extras(caption, ccap or "TIME OF DAY",
                                   ctext or f"{datetime.now():%H:%M:%S}")
        self.after(250, self._tick)

    def reset(self):
        self.rows = output_layout({k: v for k, v in self.show.items()
                                   if k != "output_layout"})
        for r in self.rows:
            r.update(show=r["id"] not in ("seg_status", "clock"), size=1.0,
                     colour="")
        self._build_rows()
        self._changed()

    def _close(self):
        master = self.master
        self.destroy()
        if isinstance(master, tk.Toplevel):       # hand the grab back
            try:
                master.grab_set()
            except tk.TclError:
                pass

    def cancel(self):
        if self.live:
            self.app.preview_output_show(self.original)
        self._close()

    def done(self):
        self._changed(initial=True)
        self._close()
        self.on_done(self.show)


# --------------------------------------------------------------------------
# Show editor: name, title, message, timer names, logos, colours
# --------------------------------------------------------------------------
class ShowEditor(tk.Toplevel):
    def __init__(self, master, show, on_save, is_new=False):
        super().__init__(master)
        self.title("New Show" if is_new else f"Edit Show - {show['name']}")
        self.configure(bg=SURFACE, padx=px(22), pady=px(18))
        self.resizable(False, False)
        self.transient(master)
        self.on_save = on_save
        self.is_new = is_new
        self.original_name = None if is_new else show["name"]
        self.show = copy_show(show)

        self.vars = {k: tk.StringVar(value=self.show[k]) for k in
                     ("name", "title", "message", "segment_label",
                      "show_label")}
        lab = dict(bg=SURFACE, fg=TEXT, font=(UI_BOLD, 10),
                   anchor="w")
        ent = dict(font=(UI_FONT, 12), width=42, bg=BG, fg=TEXT,
                   insertbackground=CYAN, relief="flat",
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=CYAN)
        hint = dict(bg=SURFACE, fg=MUTED, font=(UI_FONT, 9), anchor="w")

        tk.Label(self, text="New show" if is_new else "Edit show", bg=SURFACE,
                 fg=TEXT, font=(UI_BOLD, 14)).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        rows = [("Show name", "name", "Used to pick this show from the list"),
                ("Title text", "title", "Big text across the top of the output "
                                        "(leave blank for none)"),
                ("Bottom message", "message", "Text bar along the bottom "
                                              "(leave blank for none)"),
                ("Segment timer name", "segment_label", ""),
                ("Program timer name", "show_label", "")]
        r = 1
        for text, key, tip in rows:
            tk.Label(self, text=text, **lab).grid(row=r, column=0, sticky="w",
                                                  pady=(8, 0), padx=(0, 14))
            e = tk.Entry(self, textvariable=self.vars[key], **ent)
            e.grid(row=r, column=1, columnspan=3, sticky="ew", pady=(8, 0),
                   ipady=5)
            if key == "name":
                e.focus_set()
            r += 1
            if tip:
                tk.Label(self, text=tip, **hint).grid(row=r, column=1,
                                                      columnspan=3, sticky="w")
                r += 1

        # Logos
        self.previews = {}
        for side, text in (("logo_left", "Left logo"),
                           ("logo_right", "Right logo")):
            tk.Label(self, text=text, **lab).grid(row=r, column=0, sticky="w",
                                                  pady=(12, 0))
            box = tk.Frame(self, bg=self.show["bg"], width=200, height=80,
                           highlightthickness=1, highlightbackground=BORDER)
            box.grid(row=r, column=1, sticky="w", pady=(12, 0))
            box.grid_propagate(False)
            box.pack_propagate(False)
            prev = LogoLabel(box, bg=self.show["bg"])
            prev.pack(fill="both", expand=True)
            self.previews[side] = (box, prev)
            ModernButton.sized(self, "Import", lambda s=side: self.pick_logo(s),
                               SURFACE, font_px=12).grid(row=r, column=2,
                                                         padx=8, pady=(12, 0))
            ModernButton.sized(self, "Remove", lambda s=side: self.set_logo(s, ""),
                               SURFACE, font_px=12).grid(row=r, column=3,
                                                         pady=(12, 0),
                                                         sticky="w")
            r += 1
        if Image is None:
            tk.Label(self, text="Tip: install Pillow (pip install pillow) for "
                                "JPG logos and smoother scaling.",
                     **hint).grid(row=r, column=1, columnspan=3, sticky="w")
            r += 1

        # Colours
        tk.Label(self, text="Output colours", **lab).grid(
            row=r, column=0, sticky="w", pady=(14, 0))
        cf = tk.Frame(self, bg=SURFACE)
        cf.grid(row=r, column=1, columnspan=3, sticky="w", pady=(14, 0))
        self.swatches = {}
        for key, text in (("bg", "Background"), ("fg", "Text"),
                          ("accent", "Labels")):
            b = tk.Button(cf, text=text, font=(UI_BOLD, 10), width=11,
                          relief="flat", bd=0, highlightthickness=1,
                          cursor="hand2", pady=6,
                          command=lambda k=key: self.pick_colour(k))
            b.pack(side="left", padx=(0, 8))
            self.swatches[key] = b
        ModernButton.sized(cf, "Reset", self.reset_colours, SURFACE,
                           font_px=12).pack(side="left")
        r += 1
        tk.Label(self, text="Output to monitor", **lab).grid(row=r, column=0,
                                                         sticky="w",
                                                         pady=(12, 0))
        lf = tk.Frame(self, bg=SURFACE)
        lf.grid(row=r, column=1, columnspan=3, sticky="w", pady=(12, 0))
        ModernButton.sized(lf, "Layout, sizes and colours\u2026",
                           self.edit_layout, SURFACE, "neutral",
                           font_px=12).pack(side="left")
        self.layout_note = tk.Label(lf, bg=SURFACE, fg=MUTED,
                                    font=(UI_FONT, 9))
        self.layout_note.pack(side="left", padx=(10, 0))
        self._layout_note()
        r += 1

        # Save / cancel
        bf = tk.Frame(self, bg=SURFACE)
        bf.grid(row=r, column=0, columnspan=4, sticky="e", pady=(20, 0))
        ModernButton.sized(bf, "Save and use", self.save, SURFACE, "primary",
                           font_px=13, height=40).pack(side="right")
        ModernButton.sized(bf, "Cancel", self.destroy, SURFACE, font_px=13,
                           height=40).pack(side="right", padx=8)

        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.save())
        self._refresh_visuals()
        for side in self.previews:
            self.set_logo(side, self.show[side])

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def pick_logo(self, side):
        path = filedialog.askopenfilename(parent=self, title="Choose a logo",
                                          filetypes=LOGO_TYPES)
        if not path:
            return
        try:
            stored = import_logo(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't import that file:\n{exc}",
                                 parent=self)
            return
        self.set_logo(side, stored)
        if not self.previews[side][1]._src:
            messagebox.showwarning(APP_NAME, "That image couldn't be opened. "
                                   "Try a PNG file.", parent=self)

    def set_logo(self, side, stored):
        self.show[side] = stored
        self.previews[side][1].set_image(logo_path(stored))

    def _layout_note(self):
        on = [OUTPUT_DEFAULTS[r["id"]][1].lower() for r in
              output_layout(self.show) if r.get("show")]
        self.layout_note.config(text=f"{len(on)} of {len(OUTPUT_ELEMENTS)} "
                                     f"items shown")

    def edit_layout(self):
        def done(show):
            for k in ("output_layout", "bg", "fg", "accent"):
                self.show[k] = show[k]
            self._refresh_visuals()
            self._layout_note()
        app = getattr(self.master, "_da_app", None)
        if app is None:
            return
        OutputLayoutEditor(self, app, self.show, done,
                           live=(self.original_name ==
                                 app.current_show.get("name")))

    def pick_colour(self, key):
        _rgb, hexcol = colorchooser.askcolor(self.show[key], parent=self,
                                             title="Choose colour")
        if hexcol:
            self.show[key] = hexcol
            self._refresh_visuals()

    def reset_colours(self):
        self.show["bg"], self.show["fg"] = OUT_BG, OUT_FG
        self.show["accent"] = OUT_ACCENT
        self._refresh_visuals()

    def _refresh_visuals(self):
        bg = self.show["bg"]
        for key, b in self.swatches.items():
            col = self.show[key]
            # pick readable text for the swatch
            lum = sum(int(col[i:i + 2], 16) for i in (1, 3, 5)) / 3
            txt = "#000000" if lum > 140 else "#ffffff"
            b.config(bg=col, fg=txt, activebackground=col,
                     activeforeground=txt, highlightbackground=BORDER)
        for box, prev in self.previews.values():
            box.config(bg=bg)
            prev.config(bg=bg)

    def save(self):
        for k, v in self.vars.items():
            self.show[k] = v.get().strip()
        if not self.show["name"]:
            messagebox.showwarning(APP_NAME, "Give the show a name.",
                                   parent=self)
            return
        self.show["segment_label"] = self.show["segment_label"] or "Segment"
        self.show["show_label"] = self.show["show_label"] or "Program"
        renamed = self.original_name and \
            _slug(self.original_name) != _slug(self.show["name"])
        if (self.is_new or renamed) and \
                _slug(self.show["name"]).lower() in \
                [_slug(n).lower() for n in list_shows()]:
            if not messagebox.askyesno(
                    APP_NAME, f"A show called '{self.show['name']}' already "
                              f"exists. Replace it?", parent=self):
                return
        save_show(self.show)
        if renamed:
            delete_show(self.original_name)
        self.on_save(self.show)
        self.destroy()


# --------------------------------------------------------------------------
# Settings screen
# --------------------------------------------------------------------------
class Switch(tk.Canvas):
    """On/off switch."""

    def __init__(self, master, value, command, bg=SURFACE):
        super().__init__(master, width=px(46), height=px(26), bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.value, self.command = bool(value), command
        self.bind("<Button-1>", lambda e: self.toggle())
        self._draw()

    def toggle(self):
        self.value = not self.value
        self._draw()
        self.command(self.value)

    def _draw(self):
        self.delete("all")
        track = CYAN if self.value else SURFACE_3
        knob = "#04181b" if self.value else MUTED
        k = UI_SCALE
        self.create_line(13 * k, 13 * k, 33 * k, 13 * k, width=22 * k,
                         capstyle="round", fill=track)
        x = (33 if self.value else 13) * k
        self.create_oval(x - 8 * k, 5 * k, x + 8 * k, 21 * k, fill=knob,
                         outline=knob)


class FontDialog(tk.Toplevel):
    """Settings -> Fonts: pick the clock and text fonts, applied live."""
    BUILTIN_CLOCK = "Built-in (DA Clock)"
    BUILTIN_TEXT = "Built-in (DA Sans)"

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Fonts")
        self.configure(bg=SURFACE, padx=px(26), pady=px(20))
        self.transient(master)
        self.resizable(False, False)
        own = {UI_FONT, UI_BOLD, UI_HEAVY, DIGIT_FONT}
        self.families = sorted({f for f in tkfont.families(self)
                                if not f.startswith("@") and f not in own},
                               key=str.lower)

        tk.Label(self, text="Fonts", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 18)).grid(row=0, column=0, columnspan=3,
                                          sticky="w")
        tk.Label(self, text="Changes apply straight away, here and on the "
                            "output monitor. Type to search the list.",
                 bg=SURFACE, fg=MUTED, font=(UI_FONT, 11)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

        def row(r, label, builtin, current):
            tk.Label(self, text=label, bg=SURFACE, fg=TEXT,
                     font=(UI_BOLD, 12)).grid(row=r, column=0, sticky="w",
                                              padx=(0, 14), pady=4)
            cb = ttk.Combobox(self, width=34, style="Dark.TCombobox",
                              values=[builtin] + self.families,
                              font=(UI_FONT, 11))
            cb.set(current or builtin)
            cb.grid(row=r, column=1, sticky="w", pady=4)
            cb.bind("<<ComboboxSelected>>", lambda e: self._apply())
            cb.bind("<Return>", lambda e: self._apply())
            cb.bind("<FocusOut>", lambda e: self._apply())
            cb.bind("<KeyRelease>", lambda e, c=cb, b=builtin:
                    self._filter(e, c, b))
            return cb

        self.clock_cb = row(2, "Clocks", self.BUILTIN_CLOCK,
                            FONT_CHOICE.get("clock"))
        bf = tk.Frame(self, bg=SURFACE)
        bf.grid(row=2, column=2, sticky="w", padx=(14, 0))
        self.bold = Switch(bf, FONT_CHOICE.get("clock_bold"),
                           lambda v: self._apply())
        self.bold.pack(side="left")
        tk.Label(bf, text="Bold", bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 11)).pack(side="left", padx=(8, 0))
        self.text_cb = row(3, "Text", self.BUILTIN_TEXT,
                           FONT_CHOICE.get("text"))

        pv = tk.Frame(self, bg=BG, padx=px(20), pady=px(14),
                      highlightthickness=1, highlightbackground=BORDER)
        pv.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.pv_cap = tk.Label(pv, text="Segment 3   Next: VT1", bg=BG,
                               fg=MUTED, anchor="w")
        self.pv_cap.pack(anchor="w")
        self.pv_title = tk.Label(pv, text="Panel discussion", bg=BG, fg=TEXT,
                                 anchor="w")
        self.pv_title.pack(anchor="w")
        self.pv_clock = tk.Label(pv, text="00:12:47", bg=BG, fg=TEXT,
                                 anchor="w")
        self.pv_clock.pack(anchor="w")
        self.warn = tk.Label(self, bg=SURFACE, fg=AMBER, font=(UI_FONT, 10),
                             justify="left", wraplength=px(520), anchor="w")
        self.warn.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tk.Label(self, text="Buttons keep the built-in font so they always "
                            "fit.", bg=SURFACE, fg=FAINT,
                 font=(UI_FONT, 10)).grid(row=6, column=0, columnspan=3,
                                          sticky="w", pady=(2, 0))

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        ModernButton.sized(bottom, "Back to built-in", self.reset, SURFACE,
                           "neutral", font_px=13, height=38).pack(side="left")
        ModernButton.sized(bottom, "Done", self.done, SURFACE, "primary",
                           font_px=14, height=38, pad_x=22).pack(side="right")
        self.bind("<Escape>", lambda e: self.done())
        self._preview()
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _filter(self, e, cb, builtin):
        if e.keysym in ("Return", "Escape", "Up", "Down", "Tab"):
            return
        q = cb.get().strip().lower()
        cb["values"] = [builtin] + [f for f in self.families
                                    if q in f.lower()]

    def _pick(self, cb, builtin):
        name = cb.get().strip()
        if not name or name == builtin:
            return ""
        for f in self.families:
            if f.lower() == name.lower():
                return f
        return None                       # not a font (still typing)

    def _apply(self):
        clock = self._pick(self.clock_cb, self.BUILTIN_CLOCK)
        text = self._pick(self.text_cb, self.BUILTIN_TEXT)
        if clock is None or text is None:
            self._preview()
            return
        if (clock, text, self.bold.value) != (
                FONT_CHOICE.get("clock"), FONT_CHOICE.get("text"),
                FONT_CHOICE.get("clock_bold")):
            self.app.set_fonts(text=text, clock=clock,
                               clock_bold=self.bold.value)
        self._preview()

    def _preview(self):
        clock = FONT_CHOICE.get("clock") or DIGIT_FONT
        weight = ("bold" if FONT_CHOICE.get("clock_bold") else "normal") \
            if FONT_CHOICE.get("clock") else DIGIT_WEIGHT
        self.pv_clock.config(font=(clock, -px(64), weight))
        self.pv_cap.config(font=ft(UI_FONT, -px(16)))
        self.pv_title.config(font=ft(UI_BOLD, -px(24)))
        if FONT_CHOICE.get("clock") and not font_has_tabular_digits(
                self, clock):
            self.warn.config(text=f"The numbers in {clock} aren't all the "
                                  f"same width, so the clocks will shift "
                                  f"slightly as they count. Fonts like "
                                  f"Bahnschrift, Consolas or DIN avoid that.")
        else:
            self.warn.config(text="")

    def reset(self):
        self.clock_cb.set(self.BUILTIN_CLOCK)
        self.text_cb.set(self.BUILTIN_TEXT)
        if self.bold.value:
            self.bold.value = False
            self.bold._draw()
        self._apply()

    def done(self):
        self._apply()
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Settings")
        self.configure(bg=SURFACE, padx=px(26), pady=px(20))
        self.transient(master)
        self.resizable(False, False)

        tk.Label(self, text="Settings", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 18)).grid(row=0, column=0, columnspan=2,
                                          sticky="w")
        tk.Label(self, text="Turn off anything you don't need. Changes apply "
                            "straight away and are remembered.",
                 bg=SURFACE, fg=MUTED, font=(UI_FONT, 11)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        cols = {"Features": 0, "Devices": 0, "Screen": 1, "Safety": 1}
        # The switches sit in a scrollable area so the window fits on a
        # laptop screen as well as a 1080p one
        area = tk.Frame(self, bg=SURFACE)
        area.grid(row=2, column=0, columnspan=2, sticky="nsew")
        canvas = tk.Canvas(area, bg=SURFACE, highlightthickness=0, bd=0)
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=SURFACE)
        canvas.create_window(0, 0, window=body, anchor="nw")
        frames = {}
        for col in (0, 1):
            f = tk.Frame(body, bg=SURFACE)
            f.grid(row=0, column=col, sticky="nw", padx=(0, 34) if col == 0
                   else (0, 14))
            frames[col] = f
        rows = {0: 0, 1: 0}
        current = {0: None, 1: None}
        self.switches = {}
        for key, section, label, help_text in FEATURES:
            col = cols[section]
            f = frames[col]
            if current[col] != section:
                current[col] = section
                tk.Label(f, text=section, bg=SURFACE, fg=CYAN,
                         font=(UI_BOLD, 13)).grid(
                    row=rows[col], column=0, columnspan=2, sticky="w",
                    pady=(14 if rows[col] else 0, 6))
                rows[col] += 1
            sw = Switch(f, app.feat(key),
                        lambda v, k=key: self.app.set_feature(k, v))
            sw.grid(row=rows[col], column=0, sticky="nw", pady=(4, 0))
            text = tk.Frame(f, bg=SURFACE)
            text.grid(row=rows[col], column=1, sticky="w", padx=(12, 0),
                      pady=(2, 6))
            name = tk.Label(text, text=label, bg=SURFACE, fg=TEXT,
                            font=(UI_BOLD, 12), anchor="w", cursor="hand2")
            name.pack(anchor="w")
            name.bind("<Button-1>", lambda e, s_=sw: s_.toggle())
            tk.Label(text, text=help_text, bg=SURFACE, fg=MUTED,
                     font=(UI_FONT, 10), anchor="w", justify="left",
                     wraplength=px(360)).pack(anchor="w")
            self.switches[key] = sw
            rows[col] += 1

        body.update_idletasks()
        bw, bh = body.winfo_reqwidth(), body.winfo_reqheight()
        max_h = max(240, self.winfo_screenheight() - 260)
        canvas.config(width=bw, height=min(bh, max_h),
                      scrollregion=(0, 0, bw, bh))
        if bh > max_h:
            sb = ttk.Scrollbar(area, orient="vertical", command=canvas.yview,
                               style="Dark.Vertical.TScrollbar")
            sb.pack(side="right", fill="y")
            canvas.config(yscrollcommand=sb.set)

            def wheel(e):
                step = -1 if (getattr(e, "delta", 0) > 0 or e.num == 4) else 1
                canvas.yview_scroll(step * 3, "units")
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.bind_all(seq, wheel, add="+")
            self.bind("<Destroy>", lambda e: e.widget is self and [
                self.unbind_all(q) for q in
                ("<MouseWheel>", "<Button-4>", "<Button-5>")])

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ModernButton.sized(bottom, "Turn everything back on", self.reset,
                           SURFACE, "neutral", font_px=13,
                           height=38).pack(side="left")
        ModernButton.sized(bottom, "Done", self.destroy, SURFACE, "primary",
                           font_px=14, height=38, pad_x=22).pack(side="right")
        ModernButton.sized(bottom, "Fonts", self.app.open_fonts, SURFACE,
                           "neutral", font_px=13, height=38).pack(
            side="right", padx=(0, 10))

        # Credit
        about = tk.Frame(self, bg=SURFACE)
        about.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self._logo = author_logo(px(22))
        if self._logo is not None:
            tk.Label(about, image=self._logo, bg=SURFACE).pack(side="left",
                                                               padx=(0, 10))
        tk.Label(about, text=f"{APP_NAME} v{VERSION}",
                 bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 10)).pack(side="left")

        # Interface size (needs a restart - every widget is built at a size)
        size = tk.Frame(bottom, bg=SURFACE)
        size.pack(side="left", padx=(28, 0))
        tk.Label(size, text="Interface size", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 12)).pack(side="left", padx=(0, 10))
        ModernButton(size, "\u2212", lambda: self._size(-1), SURFACE,
                     font_px=16, size=(34, 32), tracking=0).pack(side="left")
        self.size_lbl = tk.Label(size, bg=SURFACE, fg=TEXT, width=11,
                                 font=(UI_FONT, 12))
        self.size_lbl.pack(side="left", padx=4)
        ModernButton(size, "+", lambda: self._size(1), SURFACE,
                     font_px=16, size=(34, 32), tracking=0).pack(side="left")
        self.size_note = tk.Label(size, bg=SURFACE, fg=AMBER,
                                  font=(UI_FONT, 10))
        self.size_note.pack(side="left", padx=(10, 0))
        self._show_size()
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        y = min(y, self.winfo_screenheight() - self.winfo_height() - 60)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _show_size(self):
        choice = self.app.settings.get("ui_scale", "auto")
        applied = self.app.settings.get("ui_scale_applied", "auto")
        if choice in (None, "auto"):
            text = "Auto" + (f" ({UI_SCALE * 100:.0f}%)"
                             if applied in (None, "auto") else "")
        else:
            text = f"{float(choice) * 100:.0f}%"
        self.size_lbl.config(text=text)
        self.size_note.config(text="Restart DA Timer to apply"
                              if choice != applied else "")

    def _size(self, step):
        opts = UI_SCALE_CHOICES
        cur = self.app.settings.get("ui_scale", "auto")
        i = opts.index(cur) if cur in opts else 0
        i = min(max(i + step, 0), len(opts) - 1)
        self.app.settings["ui_scale"] = opts[i]
        save_settings(self.app.settings)
        self._show_size()

    def reset(self):
        for key, sw in self.switches.items():
            want = FEATURE_DEFAULTS[key]
            if sw.value != want:
                sw.toggle()


# --------------------------------------------------------------------------
# Time source (timecode sync) settings
# --------------------------------------------------------------------------
class SyncSetup(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import da_sync
        self.ds = da_sync
        self.app = app
        self.title("Time Source")
        self.configure(bg=SURFACE, padx=px(22), pady=px(18))
        self.transient(master)
        self.resizable(False, False)
        st = app.settings
        lab = dict(bg=SURFACE, fg=MUTED, font=(UI_BOLD, 9), anchor="w")

        tk.Label(self, text="Time source", bg=SURFACE,
                 fg=TEXT, font=(UI_BOLD, 14)).grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.clock_lbl = tk.Label(self, bg=SURFACE, fg=TEXT, anchor="w",
                                  font=(DIGIT_FONT, 34, DIGIT_WEIGHT))
        self.clock_lbl.grid(row=1, column=0, columnspan=3, sticky="w")
        self.status_lbl = tk.Label(self, bg=SURFACE, fg=MUTED, anchor="w",
                                   font=(UI_BOLD, 10))
        self.status_lbl.grid(row=2, column=0, columnspan=3, sticky="w",
                             pady=(0, 12))

        self.mode = tk.StringVar(value=st.get("time_source", "pc"))
        rb = dict(variable=self.mode, bg=SURFACE, fg=TEXT, selectcolor=BG,
                  activebackground=SURFACE, activeforeground=TEXT,
                  highlightthickness=0, font=(UI_BOLD, 11),
                  anchor="w", command=self._mode_changed)
        ent = dict(bg=BG, fg=TEXT, insertbackground=CYAN, relief="flat",
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=CYAN, font=(UI_FONT, 11))

        # PC
        tk.Radiobutton(self, text="PC clock", value="pc", **rb).grid(
            row=3, column=0, columnspan=3, sticky="w")
        tk.Label(self, text="Uses Windows time. Fine if the PC is already "
                            "synced to house time.", bg=SURFACE, fg=FAINT,
                 font=(UI_FONT, 9)).grid(row=4, column=0, columnspan=3,
                                         sticky="w", padx=(26, 0))
        # NTP
        tk.Radiobutton(self, text="Network time (NTP)", value="ntp",
                       **rb).grid(row=5, column=0, columnspan=3, sticky="w",
                                  pady=(12, 0))
        tk.Label(self, text="Server", **lab).grid(row=6, column=0, sticky="w",
                                                  padx=(26, 8))
        self.ntp_var = tk.StringVar(value=st.get("ntp_server",
                                                 "time.windows.com"))
        tk.Entry(self, textvariable=self.ntp_var, width=30, **ent).grid(
            row=6, column=1, columnspan=2, sticky="w", ipady=4)
        tk.Label(self, text="Your house NTP server (IP or name). DA Timer "
                            "corrects its own clock - Windows isn't changed.",
                 bg=SURFACE, fg=FAINT, font=(UI_FONT, 9)).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=(26, 0))
        # LTC
        tk.Radiobutton(self, text="LTC timecode (audio input)", value="ltc",
                       **rb).grid(row=8, column=0, columnspan=3, sticky="w",
                                  pady=(12, 0))
        tk.Label(self, text="Input", **lab).grid(row=9, column=0, sticky="w",
                                                 padx=(26, 8))
        try:
            self.inputs = self.ds.TimeSource.audio_inputs()
            err = None
        except Exception as exc:
            self.inputs, err = [], str(exc)
        names = [n for n, _ in self.inputs] or ["(no audio inputs found)"]
        self.dev_cb = ttk.Combobox(self, state="readonly", width=40,
                                   values=names, style="Dark.TCombobox",
                                   font=(UI_FONT, 10))
        cur = st.get("ltc_device")
        self.dev_cb.set(cur if cur in names else names[0])
        self.dev_cb.grid(row=9, column=1, columnspan=2, sticky="w")
        self.dev_cb.bind("<<ComboboxSelected>>", lambda e: self._fill_channels())
        tk.Label(self, text="Channel", **lab).grid(row=10, column=0,
                                                   sticky="w", padx=(26, 8),
                                                   pady=(6, 0))
        self.ch_cb = ttk.Combobox(self, state="readonly", width=12,
                                  style="Dark.TCombobox", font=(UI_FONT, 10))
        self.ch_cb.grid(row=10, column=1, sticky="w", pady=(6, 0))
        self._fill_channels(int(st.get("ltc_channel", 0)))
        self.level_lbl = tk.Label(self, bg=SURFACE, fg=MUTED,
                                  font=(UI_FONT, 9))
        self.level_lbl.grid(row=10, column=2, sticky="w", padx=(12, 0),
                            pady=(6, 0))
        hint = ("Feed house LTC into a line input (any frame rate - "
                "24/25/29.97DF/30 detected automatically). If LTC drops, "
                "DA Timer freewheels for 10 s then falls back to the PC clock.")
        if err:
            hint = ("LTC needs the numpy and sounddevice packages - run "
                    "'Run DA Timer.bat' once to install them.")
        tk.Label(self, text=hint, bg=SURFACE, fg=AMBER if err else FAINT,
                 font=(UI_FONT, 9), wraplength=px(520), justify="left").grid(
            row=11, column=0, columnspan=3, sticky="w", padx=(26, 0))

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        ModernButton.sized(bottom, "Done", self.destroy, SURFACE, font_px=12,
                           height=36).pack(side="right")
        ModernButton.sized(bottom, "Apply", self.apply, SURFACE, "primary",
                           font_px=12, height=36).pack(side="right", padx=8)

        self.bind("<Escape>", lambda e: self.destroy())
        self._tick()
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _fill_channels(self, select=0):
        name = self.dev_cb.get()
        chans = dict(self.inputs).get(name, 1)
        values = [f"Channel {i + 1}" + (" (L)" if i == 0 and chans == 2 else
                                         " (R)" if i == 1 and chans == 2 else "")
                  for i in range(chans)]
        self.ch_cb.config(values=values)
        self.ch_cb.current(min(select, len(values) - 1))

    def _mode_changed(self):
        pass

    def apply(self):
        st = self.app.settings
        st["time_source"] = self.mode.get()
        st["ntp_server"] = self.ntp_var.get().strip() or "time.windows.com"
        if self.inputs:
            st["ltc_device"] = self.dev_cb.get()
            st["ltc_channel"] = max(self.ch_cb.current(), 0)
        save_settings(st)
        self.app.clock.configure(st)
        self.app._log_event("SYNC", f"Time source: {self.app.clock.label()}")

    def _tick(self):
        if not self.winfo_exists():
            return
        clk = self.app.clock
        clk.refresh_status()
        if clk.mode == "ltc" and clk.ltc_tc and clk.state == "ok":
            self.clock_lbl.config(text=clk.ltc_tc)
        else:
            self.clock_lbl.config(text=clk.now().strftime("%H:%M:%S"))
        col = {"ok": GREEN, "warn": AMBER, "error": RED}[clk.state]
        self.status_lbl.config(text=clk.status, fg=col if clk.mode != "pc"
                               else MUTED)
        dec = getattr(clk, "decoder", None)
        if clk.mode == "ltc" and dec is not None and dec.signal_level > 0:
            import math
            db = 20 * math.log10(max(dec.signal_level, 1e-6))
            self.level_lbl.config(text=f"input {db:.0f} dBFS")
        else:
            self.level_lbl.config(text="")
        self.after(100, self._tick)


# --------------------------------------------------------------------------
# Stream Deck setup (direct USB mode)
# --------------------------------------------------------------------------
class DeckSetup(tk.Toplevel):
    PREVIEW = 92                   # scaled in __init__

    def __init__(self, master, app):
        super().__init__(master)
        import da_deck
        self.PREVIEW = px(92)
        self.dd = da_deck
        self.app = app
        self.title("Stream Deck")
        self.configure(bg=SURFACE, padx=px(22), pady=px(18))
        self.transient(master)
        self.resizable(False, False)
        self.selected = 0
        self.grid_dims = None
        self.cells = []
        self.photos = {}
        self._last_sig = {}

        style = ttk.Style()
        style.configure("Dark.TCombobox", fieldbackground=BG, background=SURFACE_2,
                        foreground=TEXT, arrowcolor=CYAN, bordercolor=BORDER,
                        lightcolor=BG, darkcolor=BG, selectbackground=BG,
                        selectforeground=TEXT, padding=4)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", BG)],
                  foreground=[("readonly", TEXT)],
                  background=[("active", SURFACE_3)])
        self.option_add("*TCombobox*Listbox.background", SURFACE_2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", CYAN_DIM)
        self.option_add("*TCombobox*Listbox.selectForeground", CYAN)
        self.option_add("*TCombobox*Listbox.font", (UI_FONT, 11))

        tk.Label(self, text="Stream Deck, direct USB", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 14)).grid(row=0, column=0, columnspan=2,
                                                  sticky="w")
        self.status_lbl = tk.Label(self, bg=SURFACE, fg=TEXT, anchor="w",
                                   justify="left", font=(UI_BOLD, 14),
                                   wraplength=px(760))
        self.status_lbl.grid(row=1, column=0, columnspan=2, sticky="w")
        self.hint_lbl = tk.Label(self, bg=SURFACE, fg=MUTED, anchor="w",
                                 justify="left", font=(UI_FONT, 9),
                                 wraplength=px(760))
        self.hint_lbl.grid(row=2, column=0, columnspan=2, sticky="w",
                           pady=(2, 10))

        opts = tk.Frame(self, bg=SURFACE)
        opts.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.enabled_var = tk.BooleanVar(
            value=bool(app.settings.get("deck_enabled", True)))
        tk.Checkbutton(opts, text="Control the Stream Deck directly from DA "
                                  "Timer", variable=self.enabled_var,
                       command=self._toggle_enabled, bg=SURFACE, fg=TEXT,
                       selectcolor=BG, activebackground=SURFACE,
                       activeforeground=TEXT, highlightthickness=0, bd=0,
                       font=(UI_FONT, 10)).pack(side="left")
        tk.Label(opts, text="Brightness", bg=SURFACE, fg=MUTED,
                 font=(UI_BOLD, 9)).pack(side="left", padx=(24, 6))
        self.bright_val = int(self.app.settings.get("deck_brightness", 70))
        ModernButton(opts, "\u2212", lambda: self._brightness(-10), SURFACE,
                     font_px=16, size=(34, 30), tracking=0).pack(side="left")
        self.bright_lbl = tk.Label(opts, bg=SURFACE, fg=TEXT, width=5,
                                   font=(UI_BOLD, 11))
        self.bright_lbl.pack(side="left")
        ModernButton(opts, "+", lambda: self._brightness(10), SURFACE,
                     font_px=16, size=(34, 30), tracking=0).pack(side="left")
        self.bright_lbl.config(text=f"{self.bright_val}%")

        self.grid_frame = tk.Frame(self, bg=BG, padx=px(12), pady=px(12),
                                   highlightthickness=1,
                                   highlightbackground=BORDER)
        self.grid_frame.grid(row=4, column=0, sticky="n")

        ed = self.editor = tk.Frame(self, bg=SURFACE, padx=px(18))
        ed.grid(row=4, column=1, sticky="nw")
        self.key_lbl = tk.Label(ed, bg=SURFACE, fg=TEXT, anchor="w",
                                font=(UI_BOLD, 13))
        self.key_lbl.grid(row=0, column=0, sticky="w")
        tk.Label(ed, text="Action", bg=SURFACE, fg=MUTED,
                 font=(UI_BOLD, 9)).grid(row=1, column=0, sticky="w",
                                                 pady=(10, 2))
        self.action_names = list(da_deck.ACTIONS.items())
        self.action_cb = ttk.Combobox(ed, state="readonly", width=30,
                                      style="Dark.TCombobox",
                                      values=[v for _, v in self.action_names],
                                      font=(UI_FONT, 11))
        self.action_cb.grid(row=2, column=0, sticky="w")
        self.action_cb.bind("<<ComboboxSelected>>", self._action_changed)
        self.opt_frame = tk.Frame(ed, bg=SURFACE)
        self.opt_frame.grid(row=3, column=0, sticky="w")
        tk.Label(ed, text="Click a key to change it. Changes apply to the "
                          "Stream Deck straight away.", bg=SURFACE, fg=FAINT,
                 font=(UI_FONT, 9), wraplength=px(260), justify="left").grid(
            row=4, column=0, sticky="w", pady=(16, 0))

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ModernButton.sized(bottom, "Restore default layout", self._restore,
                           SURFACE, font_px=11, height=36).pack(side="left")
        ModernButton.sized(bottom, "Done", self.destroy, SURFACE, "primary",
                           font_px=12, height=36).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self._poll()
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # helpers ------------------------------------------------------------
    def _dims(self):
        deck = self.app.deck
        if deck is not None and deck.info:
            i = deck.info
            return i["rows"], i["cols"], i["keys"], i["type"]
        return 3, 5, 15, None

    def _layout(self):
        rows, cols, n, _ = self._dims()
        return self.app._deck_layout(None, n)

    def _save(self, layout):
        _, _, n, _ = self._dims()
        self.app.settings.setdefault("deck_layouts", {})[str(n)] = layout
        save_settings(self.app.settings)
        if self.app.deck:
            self.app.deck.refresh()
        self._last_sig = {}

    def _build_grid(self, rows, cols):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.cells = []
        self._blank = tk.PhotoImage(width=self.PREVIEW, height=self.PREVIEW)
        for i in range(rows * cols):
            lbl = tk.Label(self.grid_frame, bg=BG, bd=0, cursor="hand2",
                           image=self._blank,
                           highlightthickness=3, highlightbackground=BG,
                           width=self.PREVIEW, height=self.PREVIEW)
            lbl.grid(row=i // cols, column=i % cols, padx=4, pady=4)
            lbl.bind("<Button-1>", lambda e, k=i: self._select(k))
            self.cells.append(lbl)
        self.grid_dims = (rows, cols)
        self._last_sig = {}
        self._select(min(self.selected, rows * cols - 1))

    def _select(self, k):
        self.selected = k
        for i, c in enumerate(self.cells):
            c.config(highlightbackground=CYAN if i == k else BG)
        layout = self._layout()
        key = layout[k] if k < len(layout) else {"action": "none"}
        self.key_lbl.config(text=f"Key {k + 1}")
        self.action_cb.set(self.dd.ACTIONS.get(key.get("action", "none")))
        self._build_options(key)

    def _build_options(self, key):
        for w in self.opt_frame.winfo_children():
            w.destroy()
        opts = self.dd.OPTIONS.get(key.get("action"), {})
        titles = {"source": "Shows", "press": "Key press", "target": "Toggles"}
        r = 0
        for name, choices in opts.items():
            tk.Label(self.opt_frame, text=titles.get(name, name), bg=SURFACE,
                     fg=MUTED, font=(UI_BOLD, 9)).grid(
                row=r, column=0, sticky="w", pady=(10, 2))
            cb = ttk.Combobox(self.opt_frame, state="readonly", width=30,
                              style="Dark.TCombobox", font=(UI_FONT, 11),
                              values=[label for _, label in choices])
            current = key.get(name, choices[0][0])
            cb.set(dict(choices).get(current, choices[0][1]))
            cb.grid(row=r + 1, column=0, sticky="w")
            cb.bind("<<ComboboxSelected>>",
                    lambda e, n=name, c=choices, w=cb: self._option_changed(
                        n, c[w.current()][0]))
            r += 2

    def _action_changed(self, _e=None):
        action = self.action_names[self.action_cb.current()][0]
        layout = [dict(k) for k in self._layout()]
        new = {"action": action}
        for name, choices in self.dd.OPTIONS.get(action, {}).items():
            new[name] = choices[0][0]
        layout[self.selected] = new
        self._save(layout)
        self._build_options(new)

    def _option_changed(self, name, value):
        layout = [dict(k) for k in self._layout()]
        layout[self.selected][name] = value
        self._save(layout)

    def _restore(self):
        if messagebox.askyesno(APP_NAME, "Put every key back to the default "
                               "layout?", parent=self):
            _, _, n, _ = self._dims()
            self._save(self.dd.default_layout(n))
            self._select(self.selected)

    def _toggle_enabled(self):
        self.app.set_deck_enabled(bool(self.enabled_var.get()))

    def _brightness(self, step):
        self.bright_val = max(10, min(100, self.bright_val + step))
        self.bright_lbl.config(text=f"{self.bright_val}%")
        self.app.settings["deck_brightness"] = self.bright_val
        save_settings(self.app.settings)
        if self.app.deck:
            self.app.deck.refresh()

    def _poll(self):
        if not self.winfo_exists():
            return
        rows, cols, n, dtype = self._dims()
        if self.grid_dims != (rows, cols):
            self._build_grid(rows, cols)
        deck = self.app.deck
        if deck is None:
            status = "Off" if not self.enabled_var.get() else "Not started"
        else:
            status = deck.status
        self.status_lbl.config(text=status,
                               fg=GREEN if deck and deck.connected else TEXT)
        hints = []
        if deck is not None and not deck.connected and not deck.error:
            hints.append(f"Previewing a {n}-key layout. Plug in a Stream Deck "
                         f"and it will be picked up automatically.")
        if deck is not None and deck.elgato_running:
            hints.append("\u26a0  The Elgato Stream Deck app is running \u2014 "
                         "quit it (right-click its tray icon \u2192 Quit) so it "
                         "doesn't fight DA Timer for the keys.")
        if deck is not None and deck.error:
            hints.append(
                "On a Mac, install hidapi (brew install hidapi) or use the "
                "built DA Timer app." if IS_MAC else
                "Run 'Run DA Timer.bat' once to install what's needed, and "
                "keep hidapi.dll next to da_timer.py.")
        self.hint_lbl.config(text="\n".join(hints),
                             fg=AMBER if deck and deck.elgato_running else MUTED)
        # live key previews
        if Image is not None:
            state = self.app.api.state
            layout = self._layout()
            for i, cell in enumerate(self.cells):
                key = layout[i] if i < len(layout) else {"action": "none"}
                d = self.dd.design(key, state)
                sig = repr(sorted(d.items()))
                if self._last_sig.get(i) == sig:
                    continue
                self._last_sig[i] = sig
                img = self.dd.render(d, self.PREVIEW)
                self.photos[i] = ImageTk.PhotoImage(img)
                cell.config(image=self.photos[i])
        self.after(300, self._poll)


# --------------------------------------------------------------------------
# Running order / segment goals editor
# --------------------------------------------------------------------------
class PlanEditor(tk.Toplevel):
    def __init__(self, master, show_name, plan, on_save, start_at=None,
                 hard_out=None, segment_count=0):
        super().__init__(master)
        self.title(f"Segment Goals - {show_name}")
        self.configure(bg=SURFACE, padx=px(20), pady=px(16))
        self.transient(master)
        self.on_save = on_save
        self.plan = [dict(row) for row in plan]
        self.hard_out = dict(DEFAULT_SHOW["hard_out"], **(hard_out or {}))

        tk.Label(self, text="Goals and hard out", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 14)).grid(row=0, column=0,
                                                  columnspan=2, sticky="w")
        tk.Label(self, text=show_name, bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 15)).grid(row=1, column=0,
                                                  columnspan=2, sticky="w",
                                                  pady=(0, 6))

        # How many segments the show has (drives "segments left")
        sc = tk.Frame(self, bg=BG, padx=px(12), pady=px(10),
                      highlightthickness=1, highlightbackground=BORDER)
        sc.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        tk.Label(sc, text="Segments in show", bg=BG, fg=TEXT,
                 font=(UI_BOLD, 9)).grid(row=0, column=0, sticky="w",
                                         padx=(0, 12))
        self.count_var = tk.StringVar(
            value=str(segment_count) if segment_count else "")
        self.count_entry = tk.Entry(
            sc, textvariable=self.count_var, width=5, justify="center",
            bg=SURFACE, fg=TEXT, insertbackground=CYAN, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=CYAN, font=(DIGIT_FONT, 13))
        self.count_entry.grid(row=0, column=1, ipady=3)
        self.count_hint = tk.Label(sc, bg=BG, fg=FAINT, font=(UI_FONT, 9),
                                   anchor="w")
        self.count_hint.grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.count_var.trace_add("write", lambda *_: self._count_hint())

        # Hard out (back-timing)
        ho = tk.Frame(self, bg=BG, padx=px(12), pady=px(10), highlightthickness=1,
                      highlightbackground=BORDER)
        ho.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        tk.Label(ho, text="Hard out", bg=BG, fg=TEXT,
                 font=(UI_BOLD, 9)).grid(row=0, column=0, sticky="w",
                                                 padx=(0, 12))
        self.ho_mode = tk.StringVar(value=self.hard_out.get("mode", "off"))
        for i, (val, text) in enumerate((("off", "Off"),
                                         ("tod", "Off-air time"),
                                         ("trt", "Program length"))):
            tk.Radiobutton(ho, text=text, value=val, variable=self.ho_mode,
                           command=self._ho_mode_changed, bg=BG, fg=TEXT,
                           selectcolor=SURFACE_2, activebackground=BG,
                           activeforeground=TEXT, highlightthickness=0,
                           font=(UI_FONT, 10)).grid(row=0, column=1 + i,
                                                    padx=(0, 10))
        self.ho_var = tk.StringVar()
        self.ho_entry = tk.Entry(ho, textvariable=self.ho_var, width=10,
                                 justify="center", bg=SURFACE, fg=TEXT,
                                 insertbackground=CYAN, relief="flat",
                                 highlightthickness=1,
                                 highlightbackground=BORDER,
                                 highlightcolor=CYAN, font=(DIGIT_FONT, 13),
                                 disabledbackground=SURFACE_2,
                                 disabledforeground=FAINT)
        self.ho_entry.grid(row=0, column=4, padx=(6, 0), ipady=3)
        self.ho_hint = tk.Label(ho, bg=BG, fg=FAINT, font=(UI_FONT, 9))
        self.ho_hint.grid(row=1, column=1, columnspan=4, sticky="w",
                          pady=(4, 0))
        self._ho_mode_changed(initial=True)
        self._count_hint()

        self.tree = ttk.Treeview(self, columns=("num", "name", "goal"),
                                 show="headings", style="Dark.Treeview",
                                 selectmode="browse",
                                 height=max(4, min(12, (
                                     self.winfo_screenheight() - px(560))
                                     // px(30))))
        for key, title, width, stretch, anchor in (
                ("num", "#", 44, False, "center"),
                ("name", "Segment", 300, True, "w"),
                ("goal", "Goal", 100, False, "center")):
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=px(width), stretch=stretch, anchor=anchor)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview,
                           style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=2, column=0, sticky="nsew")
        sb.grid(row=2, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self.delete())
        # clicking empty space under the rows clears the selection
        self.tree.bind("<Button-1>", self._tree_click, add="+")

        side = tk.Frame(self, bg=SURFACE)
        side.grid(row=2, column=2, sticky="n", padx=(12, 0))
        for text, cmd in (("New segment", self.new_segment),
                          ("Move up", lambda: self.move(-1)),
                          ("Move down", lambda: self.move(1)),
                          ("Delete", self.delete),
                          ("Paste from Excel", self.paste),
                          ("Clear all", self.clear_all)):
            ModernButton(side, text, cmd, SURFACE, font_px=13,
                         size=(170, 36)).pack(pady=(0, 6))

        form = tk.Frame(self, bg=SURFACE)
        form.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        form.grid_columnconfigure(0, weight=1)
        ent = dict(bg=BG, fg=TEXT, insertbackground=CYAN, relief="flat",
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=CYAN, font=(UI_FONT, 12))
        tk.Label(form, text="Name", bg=SURFACE, fg=MUTED,
                 font=(UI_BOLD, 9)).grid(row=0, column=0, sticky="w")
        tk.Label(form, text="Goal", bg=SURFACE, fg=MUTED,
                 font=(UI_BOLD, 9)).grid(row=0, column=1, sticky="w",
                                                 padx=(8, 0))
        self.name_var, self.goal_var = tk.StringVar(), tk.StringVar()
        self.name_entry = tk.Entry(form, textvariable=self.name_var, **ent)
        self.name_entry.grid(row=1, column=0, sticky="ew", ipady=5)
        self.goal_entry = tk.Entry(form, textvariable=self.goal_var, width=10,
                                   justify="center", **ent)
        self.goal_entry.grid(row=1, column=1, padx=(8, 0), ipady=5)
        self.add_btn = ModernButton(form, "Add", self.add_or_update, SURFACE,
                                    "primary", font_px=12, size=(110, 36))
        self.add_btn.grid(row=1, column=2, padx=(8, 0))
        # Only shown while a row is selected
        self.insert_btn = ModernButton(form, "Add below", self.insert_new,
                                       SURFACE, "neutral", font_px=12,
                                       size=(120, 36))
        self.cancel_btn = ModernButton(form, "Cancel edit", self._deselect,
                                       SURFACE, "ghost", font_px=12,
                                       size=(110, 36))
        self.form_hint = tk.Label(form, bg=SURFACE, fg=FAINT,
                                  font=(UI_FONT, 9))
        self.form_hint.grid(row=2, column=0, columnspan=5, sticky="w",
                            pady=(4, 0))
        for e in (self.name_entry, self.goal_entry):
            e.bind("<Return>", lambda ev: self.add_or_update())

        bottom = tk.Frame(self, bg=SURFACE)
        bottom.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.total_lbl = tk.Label(bottom, bg=SURFACE, fg=TEXT,
                                  font=(UI_BOLD, 11))
        self.total_lbl.pack(side="left")
        ModernButton.sized(bottom, "Save goals", self.save, SURFACE,
                           "primary", font_px=13, height=40).pack(side="right")
        ModernButton.sized(bottom, "Cancel", self.destroy, SURFACE,
                           font_px=13, height=40).pack(side="right", padx=8)

        self.bind("<Escape>", self._escape)
        self._refresh()
        self._on_select()
        if start_at and start_at <= len(self.plan):
            self.tree.selection_set(str(start_at - 1))
            self.tree.see(str(start_at - 1))
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.name_entry.focus_set()

    def _refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.plan):
            self.tree.insert("", "end", iid=str(i), values=(
                i + 1, row.get("name", ""),
                fmt(row["goal"]) if row.get("goal") else "\u2014"))
        self._count_hint()
        total = sum(r.get("goal") or 0 for r in self.plan)
        self.total_lbl.config(text=f"{len(self.plan)} segments  \u00b7  "
                                   f"planned total {fmt(total)}")
        if select is not None and 0 <= select < len(self.plan):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _on_select(self, _e=None):
        i = self._selected()
        if i is None:
            self.add_btn.text = "Add"
            self.insert_btn.grid_remove()
            self.cancel_btn.grid_remove()
            self.form_hint.config(
                text="Goal examples: 2:30  \u00b7  1:02:30  \u00b7  5 (= 5 min)"
                     "  \u00b7  90s.   Enter adds to the end; click a row to "
                     "change it.")
        else:
            row = self.plan[i]
            self.name_var.set(row.get("name", ""))
            self.goal_var.set(fmt(row["goal"]) if row.get("goal") else "")
            self.add_btn.text = "Update"
            self.insert_btn.grid(row=1, column=3, padx=(8, 0))
            self.cancel_btn.grid(row=1, column=4, padx=(8, 0))
            self.form_hint.config(
                text=f"Editing segment {i + 1}. Update saves the change, "
                     f"Add below makes a new segment after it, Cancel edit / "
                     f"Esc goes back to adding.")
        self.add_btn._key = None
        self.add_btn._render()

    def _escape(self, _e=None):
        # Esc stops editing a row first; a second Esc closes the window
        if self._selected() is not None:
            self._deselect()
        else:
            self.destroy()

    def _tree_click(self, e):
        if not self.tree.identify_row(e.y):
            self._deselect()

    def new_segment(self):
        self._deselect()
        self.name_entry.focus_set()

    def _read_form(self):
        goal_text = self.goal_var.get().strip()
        goal = parse_duration(goal_text) if goal_text else 0
        if goal is None:
            messagebox.showwarning(APP_NAME, f"Couldn't read the goal "
                                   f"'{goal_text}'. Try 2:30 or 5.",
                                   parent=self)
            self.goal_entry.focus_set()
            return None
        name = self.name_var.get().strip()
        if not name and not goal:
            return None
        return {"name": name, "goal": goal}

    def insert_new(self):
        """Add the form as a new segment straight after the selected one."""
        i = self._selected()
        row = self._read_form()
        if row is None:
            return
        at = len(self.plan) if i is None else i + 1
        self.plan.insert(at, row)
        self._refresh()
        self.tree.see(str(at))
        self._deselect()
        self.name_entry.focus_set()

    def _deselect(self):
        self.tree.selection_remove(self.tree.selection())
        self.name_var.set("")
        self.goal_var.set("")
        self._on_select()

    def add_or_update(self):
        row = self._read_form()
        if row is None:
            return
        i = self._selected()
        if i is None:
            self.plan.append(row)
            self._refresh()
            self.tree.see(str(len(self.plan) - 1))
        else:
            self.plan[i] = row
            self._refresh()
        self._deselect()
        self.name_entry.focus_set()

    def delete(self):
        i = self._selected()
        if i is not None:
            del self.plan[i]
            self._refresh(select=min(i, len(self.plan) - 1))

    def move(self, step):
        i = self._selected()
        if i is None or not 0 <= i + step < len(self.plan):
            return
        self.plan[i], self.plan[i + step] = self.plan[i + step], self.plan[i]
        self._refresh(select=i + step)

    def clear_all(self):
        if self.plan and messagebox.askyesno(APP_NAME, "Remove every segment "
                                             "goal?", parent=self):
            self.plan = []
            self._refresh()

    def paste(self):
        """Paste rows copied from Excel / a rundown: any columns, the last
        one that reads as a duration is the goal, text columns are the
        name (a leading number column is ignored)."""
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        rows = []
        for line in text.splitlines():
            cells = [c.strip() for c in re.split(r"\t|,", line) if c.strip()]
            goal, gi = None, None
            for idx in range(len(cells) - 1, -1, -1):
                if ":" in cells[idx] or idx == len(cells) - 1:
                    goal = parse_duration(cells[idx])
                    if goal is not None:
                        gi = idx
                        break
            if goal is None:
                continue
            words = [c for k, c in enumerate(cells) if k != gi]
            if words and re.fullmatch(r"\d+\.?", words[0]):
                words = words[1:]
            rows.append({"name": " ".join(words), "goal": goal})
        if not rows:
            messagebox.showinfo(APP_NAME, "Nothing to paste. Copy rows from "
                                "Excel with a name column and a duration "
                                "column (e.g. 2:30), then try again.",
                                parent=self)
            return
        if self.plan:
            answer = messagebox.askyesnocancel(
                APP_NAME, f"Paste {len(rows)} segments.\n\nYes = replace the "
                          f"current goals\nNo = add to the end", parent=self)
            if answer is None:
                return
            if answer:
                self.plan = []
        self.plan.extend(rows)
        self._refresh()

    def _read_count(self):
        t = self.count_var.get().strip()
        if not t:
            return 0
        if not t.isdigit() or not 0 <= int(t) <= 999:
            raise ValueError(f"'{t}' isn't a number of segments.")
        return int(t)

    def _count_hint(self):
        if not hasattr(self, "count_hint"):
            return
        listed = len(self.plan)
        try:
            n = self._read_count()
        except ValueError:
            self.count_hint.config(text="Type a number, e.g. 8", fg=AMBER)
            return
        if not n:
            text = (f"Blank = use the {listed} listed below" if listed else
                    "Blank = count the segments listed below")
        elif listed and n != listed:
            text = f"{listed} listed below \u2013 \u201csegments left\u201d " \
                   f"counts from {n}"
        else:
            text = "Used for \u201csegments left\u201d"
        self.count_hint.config(text=text, fg=FAINT)

    def _ho_mode_changed(self, initial=False):
        mode = self.ho_mode.get()
        if mode == "tod":
            if initial or not parse_tod(self.ho_var.get()):
                self.ho_var.set(self.hard_out.get("tod", ""))
            self.ho_entry.config(state="normal")
            self.ho_hint.config(text="Time of day the show must be off air, "
                                     "e.g. 21:58:30")
        elif mode == "trt":
            trt = self.hard_out.get("trt") or 0
            if initial or parse_duration(self.ho_var.get()) is None:
                self.ho_var.set(fmt(trt) if trt else "")
            self.ho_entry.config(state="normal")
            self.ho_hint.config(text="Total program length to hit, "
                                     "e.g. 58:30 or 1:28:00")
        else:
            self.ho_entry.config(state="disabled")
            self.ho_hint.config(text="No back-timing. Pick a mode to set a "
                                     "hard out.")

    def _read_hard_out(self):
        mode = self.ho_mode.get()
        ho = dict(self.hard_out, mode=mode)
        text = self.ho_var.get().strip()
        if mode == "tod":
            sod = parse_tod(text)
            if sod is None:
                raise ValueError(f"'{text}' isn't a time of day. Try 21:58:30.")
            ho["tod"] = f"{sod // 3600:02d}:{sod % 3600 // 60:02d}:{sod % 60:02d}"
        elif mode == "trt":
            secs = parse_duration(text)
            if not secs:
                raise ValueError(f"'{text}' isn't a program length. "
                                 f"Try 58:30.")
            ho["trt"] = secs
        return ho

    def save(self):
        try:
            hard_out = self._read_hard_out()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self)
            self.ho_entry.focus_set()
            return
        try:
            count = self._read_count()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self)
            self.count_entry.focus_set()
            return
        if self.name_var.get().strip() or self.goal_var.get().strip():
            self.add_or_update()       # don't lose a half-typed row
        self.on_save(self.plan, hard_out, count)
        self.destroy()


# --------------------------------------------------------------------------
# Show log: notes + automatic events, each stamped with time of day (TOD),
# program time and segment time. Backed up to disk after every change so a
# crash doesn't lose the record; exported to .xlsx at the end.
# --------------------------------------------------------------------------
from datetime import datetime


class ShowLog:
    clock = staticmethod(datetime.now)     # replaced by DA Timer's time source

    def __init__(self):
        self.entries = []        # dicts: tod, program, seg_no, seg_time, type, note
        self.segments = {}       # seg_no -> {"start": iso, "end": iso}
        self.dirty = False       # True when there are unexported changes

    @staticmethod
    def backup_path():
        return os.path.join(data_dir("logs"), "current_log.json")

    def add(self, kind, note, program, seg_no, seg_time, tod=None):
        self.entries.append({
            "tod": tod or self.clock().isoformat(timespec="seconds"),
            "program": float(program), "seg_no": int(seg_no),
            "seg_time": float(seg_time), "type": kind, "note": note})
        self.dirty = True
        self.save_backup()
        return self.entries[-1]

    def segment_started(self, seg_no):
        info = self.segments.setdefault(str(seg_no), {"start": "", "end": ""})
        if not info["start"]:
            info["start"] = self.clock().isoformat(timespec="seconds")
            self.save_backup()

    def segment_ended(self, seg_no):
        info = self.segments.setdefault(str(seg_no), {"start": "", "end": ""})
        info["end"] = self.clock().isoformat(timespec="seconds")
        self.save_backup()

    def clear(self):
        self.entries, self.segments, self.dirty = [], {}, False
        self.save_backup()

    def save_backup(self):
        try:
            with open(self.backup_path(), "w", encoding="utf-8") as f:
                json.dump({"entries": self.entries, "segments": self.segments,
                           "dirty": self.dirty}, f)
        except Exception:
            pass

    @classmethod
    def load_backup(cls):
        log = cls()
        try:
            with open(cls.backup_path(), encoding="utf-8") as f:
                data = json.load(f)
            log.entries = data.get("entries", [])
            log.segments = data.get("segments", {})
            log.dirty = data.get("dirty", bool(log.entries))
        except Exception:
            pass
        return log


def export_log_xlsx(path, show_name, log, segment_times, plan=None,
                    backtime=None):
    """Write the log to an .xlsx file. Raises ImportError without openpyxl."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    def tod(iso):
        return datetime.fromisoformat(iso).time() if iso else None

    def dur(seconds):          # Excel stores durations as fractions of a day
        return int(seconds) / 86400

    head_fill = PatternFill("solid", fgColor="1F2A44")
    head_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="BFBFBF")
    grid = Border(top=thin, bottom=thin, left=thin, right=thin)
    event_font = Font(italic=True, color="666666")
    mark_fill = PatternFill("solid", fgColor="FFF2CC")

    wb = Workbook()
    wb.properties.creator = APP_NAME
    ws = wb.active
    ws.title = "Log"
    entries = log.entries
    first = entries[0]["tod"] if entries else ""
    last = entries[-1]["tod"] if entries else ""
    ws["A1"] = f"{show_name} — Show Log"
    ws["A1"].font = Font(bold=True, size=16)
    meta = [("Date", datetime.fromisoformat(first).date() if first else
             datetime.now().date(), "dd/mm/yyyy"),
            ("First entry (TOD)", tod(first), "hh:mm:ss"),
            ("Last entry (TOD)", tod(last), "hh:mm:ss"),
            ("Program time at last entry",
             dur(entries[-1]["program"]) if entries else 0, "[h]:mm:ss"),
            ("Planned total (segment goals)",
             dur(sum(p.get("goal") or 0 for p in (plan or []))), "[h]:mm:ss"),
            ("Over / under at export", fmt_delta(sum(
                int(segment_times[i]) - (p.get("goal") or 0)
                for i, p in enumerate(plan or [])
                if i < len(segment_times) and p.get("goal"))), "@"),
            ("Hard out", (backtime or {}).get("out", "not set"), "@"),
            ("Back-time at export", (backtime or {}).get("verdict", "-"), "@"),
            ("Entries", len(entries), "0"),
            ("Exported", datetime.now().replace(microsecond=0),
             "dd/mm/yyyy hh:mm:ss"),
            ("Made with", f"{APP_NAME} v{VERSION}", "@")]
    for i, (label, value, numfmt) in enumerate(meta, start=3):
        ws.cell(row=i, column=1, value=label).font = Font(bold=True)
        ws.merge_cells(start_row=i, start_column=1, end_row=i, end_column=3)
        c = ws.cell(row=i, column=4, value=value)
        ws.merge_cells(start_row=i, start_column=4, end_row=i, end_column=5)
        c.number_format = numfmt
        c.alignment = Alignment(horizontal="left")

    header_row = 3 + len(meta) + 1
    headers = ["#", "TOD", "Program Time", "Segment", "Segment Time",
               "Type", "Note"]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=header_row, column=col, value=h)
        c.fill, c.font, c.border = head_fill, head_font, grid
        c.alignment = Alignment(horizontal="center")
    for n, e in enumerate(entries, start=1):
        r = header_row + n
        values = [n, tod(e["tod"]), dur(e["program"]), e["seg_no"],
                  dur(e["seg_time"]), e["type"], e["note"]]
        fmts = ["0", "hh:mm:ss", "[h]:mm:ss", "0", "[h]:mm:ss", "@", "@"]
        for col, (v, f) in enumerate(zip(values, fmts), start=1):
            c = ws.cell(row=r, column=col, value=v)
            c.number_format, c.border = f, grid
            c.alignment = Alignment(horizontal="left" if col == 7 else
                                    "center", vertical="top",
                                    wrap_text=(col == 7))
            if e["type"] not in ("NOTE", "MARK"):
                c.font = event_font
            if e["type"] == "MARK":
                c.fill = mark_fill
    for col, width in enumerate([6, 11, 14, 10, 14, 14, 70], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    if entries:
        ws.auto_filter.ref = (f"A{header_row}:G{header_row + len(entries)}")
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = f"{header_row}:{header_row}"

    # Segments sheet: running order with goal vs actual
    plan = plan or []
    over_font = Font(bold=True, color="C00000")
    under_font = Font(bold=True, color="1E8449")
    ss = wb.create_sheet("Segments")
    heads = ["Segment", "Name", "Start (TOD)", "End (TOD)", "Goal", "Actual",
             "Over / Under", "Over / Under (sec)"]
    for col, h in enumerate(heads, start=1):
        c = ss.cell(row=1, column=col, value=h)
        c.fill, c.font, c.border = head_fill, head_font, grid
        c.alignment = Alignment(horizontal="center")
    rows = max(len(segment_times), len(plan))
    total_goal = total_actual = total_delta = 0
    for i in range(1, rows + 1):
        info = log.segments.get(str(i), {})
        p = plan[i - 1] if i <= len(plan) else {}
        goal = p.get("goal") or 0
        ran = i <= len(segment_times)
        actual = int(segment_times[i - 1]) if ran else None
        delta = (actual - goal) if (ran and goal) else None
        total_goal += goal
        total_actual += actual or 0
        total_delta += delta or 0
        values = [i, p.get("name", ""), tod(info.get("start")),
                  tod(info.get("end")), dur(goal) if goal else None,
                  dur(actual) if ran else "not run",
                  fmt_delta(delta) if delta is not None else None, delta]
        fmts = ["0", "@", "hh:mm:ss", "hh:mm:ss", "[h]:mm:ss", "[h]:mm:ss",
                "@", "0"]
        for col, (v, f) in enumerate(zip(values, fmts), start=1):
            c = ss.cell(row=i + 1, column=col, value=v)
            c.number_format, c.border = f, grid
            c.alignment = Alignment(horizontal="left" if col == 2 else
                                    "center")
            if col in (7, 8) and delta is not None:
                c.font = over_font if delta > 0 else under_font
            if not ran:
                c.font = Font(italic=True, color="888888")
    t = rows + 2
    ss.cell(row=t, column=4, value="Total").font = Font(bold=True)
    for col, v, f in ((5, dur(total_goal), "[h]:mm:ss"),
                      (6, dur(total_actual), "[h]:mm:ss"),
                      (7, fmt_delta(total_delta), "@"),
                      (8, total_delta, "0")):
        c = ss.cell(row=t, column=col, value=v)
        c.number_format = f
        c.alignment = Alignment(horizontal="center")
        c.font = (over_font if total_delta > 0 else under_font) \
            if col in (7, 8) and total_delta else Font(bold=True)
    for col, width in enumerate([10, 34, 13, 13, 12, 12, 14, 18], start=1):
        ss.column_dimensions[get_column_letter(col)].width = width
    ss.freeze_panes = "A2"
    ss.page_setup.orientation = "landscape"
    ss.sheet_properties.pageSetUpPr.fitToPage = True
    ss.page_setup.fitToWidth = 1
    ss.page_setup.fitToHeight = 0

    wb.save(path)


def export_log_csv(path, show_name, log):
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#", "TOD", "Program Time", "Segment", "Segment Time",
                    "Type", "Note"])
        for n, e in enumerate(log.entries, start=1):
            w.writerow([n, e["tod"][11:], fmt(e["program"]), e["seg_no"],
                        fmt(e["seg_time"]), e["type"], e["note"]])


# --------------------------------------------------------------------------
# Local control API (used by the Stream Deck plugin; also works with
# Bitfocus Companion's generic HTTP module). Listens on 127.0.0.1 only.
# Actions need the "X-DA-Timer" header, so a web page in a browser can't
# trigger them behind your back.
# --------------------------------------------------------------------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

API_PORT = 47800
API_COMMANDS = {
    "all_start", "all_stop", "all_toggle",
    "seg_start", "seg_stop", "seg_toggle",
    "prog_start", "prog_stop", "prog_toggle",
    "next_segment", "mark", "reset_program",
    "dual_toggle", "tod_toggle", "log_toggle", "segments_toggle", "undo",
}


class ControlAPI:
    def __init__(self, port):
        self.port = port
        self.queue = queue.Queue()
        self.state = {"app": APP_NAME, "version": VERSION}
        self.server = None
        self.error = None
        self.last_seen = 0.0          # when a client last asked for state
        if port is None:              # remote control switched off
            return
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port),
                                              self._handler())
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever,
                             daemon=True).start()
        except OSError as exc:
            self.error = str(exc)

    def _handler(api):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _route(self):
                path = urlparse(self.path).path.rstrip("/")
                if path in ("", "/api", "/api/state"):
                    api.last_seen = time.monotonic()
                    return self._send(200, api.state)
                if path.startswith("/api/action/"):
                    if not self.headers.get("X-DA-Timer"):
                        return self._send(403, {"ok": False, "error":
                                                "missing X-DA-Timer header"})
                    cmd = path.rsplit("/", 1)[-1]
                    if cmd not in API_COMMANDS:
                        return self._send(404, {"ok": False,
                                                "error": "unknown command",
                                                "commands": sorted(API_COMMANDS)})
                    api.last_seen = time.monotonic()
                    api.queue.put(cmd)
                    return self._send(200, {"ok": True, "queued": cmd})
                return self._send(404, {"ok": False, "error": "not found"})

            do_GET = _route
            do_POST = _route
        return Handler

    def close(self):
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


# --------------------------------------------------------------------------
# Operator window
# --------------------------------------------------------------------------
class DATimerApp:
    def __init__(self, root):
        self.root = root
        init_fonts(root)
        self.settings = load_settings()
        self.show = Stopwatch()
        self.segment = Stopwatch()
        self.segment_times = [0.0]   # one entry per segment; last = current
        self.output = None
        self.current_show = load_show(self.settings.get("current_show"))
        self.seg_panel_on = bool(self.settings.get("segment_panel", False))
        self.log_on = bool(self.settings.get("log_panel", False))
        self.tod_on = bool(self.settings.get("tod", False))
        import da_sync
        self.clock = da_sync.TimeSource(self.settings)
        ShowLog.clock = staticmethod(self.clock.now)
        self._pending_log = ShowLog.load_backup()   # last session's log
        self.log = ShowLog()
        self._note_stamp = None
        self._last_layout = None
        self._out_status = ("", None)
        root._da_app = self               # lets dialogs reach the app
        self.awake = KeepAwake()
        fc = self.settings.get("fonts") or {}
        fams = set(tkfont.families(root))
        FONT_CHOICE.update(
            text=fc.get("text") if fc.get("text") in fams else "",
            clock=fc.get("clock") if fc.get("clock") in fams else "",
            clock_bold=bool(fc.get("clock_bold")))
        root.bind_class("Toplevel", "<Map>", lambda e: self._fonts_on_map(e),
                        add="+")
        self._undo_stack = []          # most recent last
        self._due = {}                 # timetable for the slower tick jobs
        self._seg_row_h = px(30)
        self._in_action = False
        feats = self.settings.setdefault("features", {})
        # carry older settings over
        feats.setdefault("auto_log", self.settings.get("auto_log", True))
        feats.setdefault("deck", self.settings.get("deck_enabled", True))
        feats.setdefault("dual_on_start", self.settings.get("dual_on_start",
                                                            True))

        root.title(f"{APP_NAME} v{VERSION}")
        root.configure(bg=BG)
        root.minsize(1100, 650)
        self._set_icon()
        self._style_ttk()

        self._build_ui()
        self._bind_keys()
        self._update_visuals()
        self.apply_show()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<Configure>", self._layout)

        try:
            root.state("zoomed")
        except tk.TclError:
            try:
                root.attributes("-zoomed", True)
            except tk.TclError:
                root.geometry(f"{root.winfo_screenwidth()}x"
                              f"{root.winfo_screenheight() - 80}+0+25")
        if IS_MAC:
            # Cmd+Q / the app menu's Quit go through the same checks as Exit
            try:
                root.createcommand("tk::mac::Quit", self.quit)
            except tk.TclError:
                pass
        if self.feat("fullscreen"):
            root.after(200, lambda: self.set_fullscreen(True))

        root.after(300, self._startup_output)
        root.after(700, self._offer_restore)
        self.api = ControlAPI(int(self.settings.get("api_port", API_PORT))
                              if self.feat("api") else None)
        self._api_tick = 0
        self.deck = None
        if self.feat("deck"):
            self._start_deck()
        if not self.feat("sync") and self.clock.mode != "pc":
            self.clock.configure(dict(self.settings, time_source="pc"))
        self._tick()

    # ---------------------------------------------------------------- UI --
    def _set_icon(self):
        here = os.path.dirname(os.path.abspath(sys.argv[0]))
        for p in (os.path.join(getattr(sys, "_MEIPASS", here), "stopwatch.ico"),
                  os.path.join(here, "stopwatch.ico")):
            if os.path.exists(p) and sys.platform == "win32":
                try:
                    self.root.iconbitmap(default=p)
                except tk.TclError:
                    pass
                break

    def _style_ttk(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")      # the theme that honours custom colours
        except tk.TclError:
            pass
        style.configure("Dark.Treeview", background=SURFACE,
                        fieldbackground=SURFACE, foreground=TEXT,
                        bordercolor=SURFACE, lightcolor=SURFACE,
                        darkcolor=SURFACE, borderwidth=0,
                        font=ft(UI_FONT, 11), rowheight=px(30))
        style.configure("Dark.Treeview.Heading", background=SURFACE,
                        foreground=MUTED, bordercolor=SURFACE,
                        lightcolor=SURFACE, darkcolor=SURFACE,
                        relief="flat", font=ft(UI_BOLD, 10), padding=(4, 6))
        style.map("Dark.Treeview.Heading",
                  background=[("active", SURFACE_2)])
        style.map("Dark.Treeview", background=[("selected", CYAN_DIM)],
                  foreground=[("selected", TEXT)])
        style.configure("Seg.Dark.Treeview", font=ft(UI_FONT, 11),
                        rowheight=px(30))
        style.layout("Dark.Treeview", [("Treeview.treearea",
                                        {"sticky": "nswe"})])
        style.configure("Dark.Vertical.TScrollbar", background=SURFACE_2,
                        troughcolor=SURFACE, bordercolor=SURFACE,
                        arrowcolor=MUTED, lightcolor=SURFACE_2,
                        darkcolor=SURFACE_2, gripcount=0, arrowsize=10)
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", SURFACE_3)])
        style.configure("Dark.TCombobox", fieldbackground=BG,
                        background=SURFACE_2, foreground=TEXT, arrowcolor=CYAN,
                        bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                        selectbackground=BG, selectforeground=TEXT, padding=4)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", BG)],
                  foreground=[("readonly", TEXT)],
                  background=[("active", SURFACE_3)])
        r = self.root
        r.option_add("*TCombobox*Listbox.background", SURFACE_2)
        r.option_add("*TCombobox*Listbox.foreground", TEXT)
        r.option_add("*TCombobox*Listbox.selectBackground", CYAN_DIM)
        r.option_add("*TCombobox*Listbox.selectForeground", CYAN)

    def _big(self, text, cmd, page_bg=BG, icon=None):
        return ModernButton(self.root, text, cmd, page_bg, icon=icon,
                            tracking=0.08)

    def _tool(self, text, cmd, variant="ghost"):
        return ModernButton.sized(self.root, text, cmd, SURFACE, variant,
                                  font_px=15, height=38, pad_x=14,
                                  tracking=0.01)

    def _build_ui(self):
        r = self.root
        # Structural surfaces first so everything else stacks on top
        self.header = tk.Frame(r, bg=SURFACE)
        self.header_line = tk.Frame(r, bg=BORDER)
        self.toolbar = tk.Frame(r, bg=SURFACE)
        self.toolbar_line = tk.Frame(r, bg=BORDER)
        self.cards = {k: Panel(r) for k in ("seg", "show")}
        self.bt_panel = Panel(r)
        self.seg_bg = Panel(r)
        self.log_bg = Panel(r)

        # Header: show logo + name on the left, time of day + company logo
        self.top_logo = LogoLabel(r, bg=SURFACE)
        self.app_mark = tk.Label(r, bg=SURFACE, bd=0)
        self._app_mark_h = None
        self.app_lbl = tk.Label(r, text=APP_NAME,
                                bg=SURFACE, fg=MUTED,
                                anchor="w", font=(UI_FONT, 10))
        self.show_name_lbl = tk.Label(r, bg=SURFACE, fg=TEXT, anchor="w",
                                      font=(UI_BOLD, 18))
        self.tod_cap = tk.Label(r, bg=SURFACE, fg=MUTED, anchor="e",
                                font=(UI_FONT, 10))
        self.tod_lbl = FitLabel(r, "00:00:00", fill=0.98, text="",
                                bg=SURFACE, fg=TEXT, anchor="e")
        self.company_logo = LogoLabel(r, bg=SURFACE)

        # Left column: master controls
        self.all_start_btn = self._big("ALL START", self.start_all, icon="play")
        self.all_stop_btn = self._big("ALL STOP", self.stop_all, icon="stop")

        # Timer cards
        def card_widgets(cap):
            caption = tk.Label(r, text=cap, bg=SURFACE, fg=MUTED, anchor="w",
                               font=(UI_FONT, 12))
            title = tk.Label(r, text="", bg=SURFACE, fg=TEXT, anchor="w",
                             font=(UI_BOLD, 20))
            pill = Pill(r, page_bg=SURFACE)
            digits = FitLabel(r, "00:00:00", fill=0.96, text="00:00:00",
                              bg=SURFACE, fg=TEXT, anchor="w")
            return caption, title, pill, digits

        (self.seg_cap_lbl, self.seg_title_lbl, self.seg_chip,
         self.seg_label) = card_widgets("Segment")
        self.seg_start_btn = self._big("START", self.start_segment, SURFACE,
                                       "play")
        self.seg_stop_btn = self._big("STOP", self.stop_segment, SURFACE,
                                      "stop")
        self.seg_reset_btn = self._big("NEXT", self.next_segment, SURFACE,
                                       "next")
        self.seg_preset_btn = self._big(
            "PRESET", lambda: self.open_preset("segment"), SURFACE, "preset")

        (self.show_cap_lbl, self.show_title_lbl, self.show_chip,
         self.show_label) = card_widgets("Program")
        self.show_start_btn = self._big("START", self.start_show, SURFACE,
                                        "play")
        self.show_stop_btn = self._big("STOP", self.stop_show, SURFACE, "stop")
        self.show_reset_btn = self._big("RESET", self.reset_all, SURFACE,
                                        "reset")
        self.show_preset_btn = self._big(
            "PRESET", lambda: self.open_preset("show"), SURFACE, "preset")
        self._pill_pos = {}

        # Back-time band (only shown when the show has a hard out)
        self.bt_parts = {}
        for key in ("out", "to_out", "proj"):
            capl = tk.Label(r, bg=SURFACE, fg=MUTED, anchor="w",
                            font=(UI_FONT, 11), cursor="hand2")
            val = tk.Label(r, bg=SURFACE, fg=TEXT, anchor="w",
                           font=(DIGIT_FONT, 24, DIGIT_WEIGHT), cursor="hand2")
            self.bt_parts[key] = (capl, val)
            for w in (capl, val):
                w.bind("<Button-1>", lambda e: self.open_plan())
        self.bt_pill = Pill(r, page_bg=SURFACE, height=44, font_px=22)
        self.bt_note = tk.Label(r, bg=SURFACE, fg=AMBER, anchor="e",
                                font=(UI_FONT, 10))
        self.bt_panel.bind("<Button-1>", lambda e: self.open_plan())
        self.bt_pill.bind("<Button-1>", lambda e: self.open_plan())
        self._bt = None

        # Over/under rows: goal (left), live +/- (right), progress bar
        self.ou = {}
        for k in ("seg", "show"):
            goal = tk.Label(r, bg=SURFACE, fg=MUTED, anchor="w",
                            cursor="hand2", font=(UI_FONT, 13))
            delta = tk.Label(r, bg=SURFACE, fg=MUTED, anchor="e",
                             font=(UI_BOLD, 13))
            bar = Bar(r)
            self.ou[k] = [goal, delta, bar]
        self.ou["seg"][0].bind("<Button-1>", lambda e: self.set_current_goal())
        self.ou["show"][0].bind("<Button-1>", lambda e: self.open_plan())

        # Bottom toolbar, grouped: output | panels | show | devices
        self.dual_btn = self._tool("Output to monitor", self.toggle_dual)
        # the ▾ half of the output button: which monitor, layout
        self.screen_btn = ModernButton.sized(
            self.root, "\u25be", self.pick_screen, SURFACE, "ghost",
            font_px=15, height=38, pad_x=10, tracking=0.01)
        self.screen_btn.menu_label = "Output to monitor settings\u2026"
        self.segtimes_btn = self._tool("Running order", self.toggle_seg_panel)
        self.log_btn = self._tool("Log", self.toggle_log_panel)
        self.tod_btn = self._tool("Clock", self.toggle_tod)
        self.plan_btn = self._tool("Goals & hard out", self.open_plan)
        self.show_btn = self._tool("Show ▾", self.show_menu)
        self.deck_btn = self._tool("Stream Deck", self.open_deck_setup)
        self.sync_btn = self._tool("Time source", self.open_sync_setup)
        self.exit_btn = self._tool("Exit", self.quit, "neutral")
        self.more_btn = self._tool("More \u25be", self._more_menu)
        self._overflow = []
        self.undo_btn = self._tool("Undo", self.undo, "neutral")
        self.settings_btn = self._tool("Settings", self.open_settings,
                                       "neutral")
        self.full_btn = self._tool("Full screen GUI", self.toggle_fullscreen,
                                   "ghost")
        self.undo_btn.set_enabled(False)
        self.tool_groups = [(self.undo_btn,),
                            (self.dual_btn, self.screen_btn),
                            (self.segtimes_btn, self.log_btn, self.tod_btn),
                            (self.plan_btn, self.show_btn),
                            (self.deck_btn, self.sync_btn)]
        self.tool_divs = [tk.Frame(r, bg=BORDER) for _ in self.tool_groups[1:]]
        self.deck_pill = Pill(r, page_bg=SURFACE, height=30, font_px=14)
        self.sync_pill = Pill(r, page_bg=SURFACE, height=30, font_px=14)
        self.status_lbl = tk.Label(r, bg=SURFACE, fg=FAINT, anchor="e",
                                   font=(UI_FONT, 10),
                                   text=f"v{VERSION}")

        # Running order panel
        sp = self.seg_panel = tk.Frame(r, bg=SURFACE)
        sp.grid_rowconfigure(1, weight=1)
        sp.grid_columnconfigure(0, weight=1)
        sh = tk.Frame(sp, bg=SURFACE)
        sh.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8,
                pady=(4, 8))
        tk.Label(sh, text="Running order", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 14)).pack(side="left")
        ModernButton.sized(sh, "Edit goals", self.open_plan, SURFACE, "neutral",
                           font_px=13, height=32, pad_x=12,
                           tracking=0.01).pack(side="right")
        self.seg_list = ttk.Treeview(sp, columns=("num", "name", "goal", "act",
                                                  "diff", "back"),
                                     show="headings",
                                     style="Seg.Dark.Treeview",
                                     selectmode="none")
        for key, title, width, stretch in (("num", "#", 34, False),
                                           ("name", "Segment", 90, True),
                                           ("goal", "Goal", 64, False),
                                           ("act", "Actual", 64, False),
                                           ("diff", "+/−", 64, False),
                                           ("back", "Start by", 76, False)):
            self.seg_list.heading(key, text=title,
                                  anchor="w" if key == "name" else "center")
            self.seg_list.column(key, width=px(width), stretch=stretch,
                                 anchor="w" if key == "name" else "center")
        self.seg_list.tag_configure("odd", background="#18202a")
        self.seg_list.tag_configure("current", background=CYAN_DIM,
                                    foreground=CYAN)
        self.seg_list.tag_configure("over", foreground="#ff8592")
        self.seg_list.tag_configure("under", foreground=GREEN)
        self.seg_list.tag_configure("planned", foreground=FAINT)
        self.seg_list.bind("<Double-Button-1>", self._seg_list_click)
        sb = ttk.Scrollbar(sp, orient="vertical", command=self.seg_list.yview,
                           style="Dark.Vertical.TScrollbar")
        self.seg_list.configure(yscrollcommand=sb.set)
        self.seg_list.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns", padx=(4, 0))
        self.seg_total_lbl = tk.Label(sp, bg=SURFACE, fg=TEXT, anchor="w",
                                      font=(UI_BOLD, 12))
        self.seg_total_lbl.grid(row=2, column=0, columnspan=2, sticky="ew",
                                padx=8, pady=(8, 2))
        self._rebuild_seg_list()
        self._build_log_panel()
        self._set_company_logo()

        # Undo pop-up (created last so it sits above everything)
        self.toast_bg = Panel(r, page_bg=BG, fill=SURFACE_2, border="#3a4655",
                              radius=12)
        self.toast_lbl = tk.Label(r, bg=SURFACE_2, fg=TEXT, anchor="w",
                                  font=(UI_BOLD, 14))
        self.toast_btn = ModernButton.sized(r, "Undo", self.undo, SURFACE_2,
                                            "primary", font_px=14, height=36,
                                            pad_x=18)
        self.toast_close = ModernButton.sized(r, "Dismiss", self._hide_toast,
                                              SURFACE_2, "neutral", font_px=13,
                                              height=36, pad_x=12)
        self._toast_job = None

    def _build_log_panel(self):
        lp = self.log_panel = tk.Frame(self.root, bg=SURFACE)
        lp.grid_columnconfigure(0, weight=1)
        lp.grid_rowconfigure(1, weight=1)

        hdr = tk.Frame(lp, bg=SURFACE)
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8,
                 pady=(4, 8))
        tk.Label(hdr, text="Log", bg=SURFACE, fg=TEXT,
                 font=(UI_BOLD, 14)).pack(side="left")
        self.log_count_lbl = tk.Label(hdr, bg=SURFACE, fg=MUTED,
                                      font=(UI_FONT, 11))
        self.log_count_lbl.pack(side="left", padx=10)
        ModernButton.sized(hdr, "Export to Excel", self.export_log, SURFACE,
                           "primary", font_px=13, height=32, pad_x=12,
                           tracking=0.01).pack(side="right")
        ModernButton.sized(hdr, "Clear", self.clear_log, SURFACE, "neutral",
                           font_px=13, height=32, pad_x=12,
                           tracking=0.01).pack(side="right", padx=8)

        cols = (("tod", "Time", 74, False), ("prog", "Program", 76, False),
                ("seg", "Seg", 40, False), ("type", "Type", 92, False),
                ("note", "Note", 200, True))
        self.log_tree = ttk.Treeview(lp, columns=[c[0] for c in cols],
                                     show="headings", style="Dark.Treeview",
                                     selectmode="browse")
        for key, title, width, stretch in cols:
            self.log_tree.heading(key, text=title,
                                  anchor="w" if key == "note" else "center")
            self.log_tree.column(key, width=px(width), stretch=stretch,
                                 anchor="w" if key == "note" else "center")
        self.log_tree.tag_configure("odd", background="#18202a")
        self.log_tree.tag_configure("event", foreground=MUTED)
        self.log_tree.tag_configure("mark", background=AMBER_DIM,
                                    foreground=AMBER)
        sb = ttk.Scrollbar(lp, orient="vertical", command=self.log_tree.yview,
                           style="Dark.Vertical.TScrollbar")
        self.log_tree.configure(yscrollcommand=sb.set)
        self.log_tree.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns", padx=(4, 0))
        self.log_tree.bind("<Double-Button-1>", self.edit_log_entry)
        self.log_tree.bind("<Delete>", self.delete_log_entry)

        entry_row = tk.Frame(lp, bg=SURFACE)
        entry_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4,
                       pady=(10, 0))
        entry_row.grid_columnconfigure(0, weight=1)
        self.note_var = tk.StringVar()
        self.note_entry = tk.Entry(entry_row, textvariable=self.note_var,
                                   font=(UI_FONT, 14), bg=BG, fg=TEXT,
                                   insertbackground=CYAN, relief="flat",
                                   highlightthickness=1,
                                   highlightbackground=BORDER,
                                   highlightcolor=CYAN)
        self.note_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        ModernButton.sized(entry_row, "Log note", self.add_note, SURFACE,
                           "primary", font_px=13, height=40, pad_x=14,
                           tracking=0.01).grid(row=0, column=1, padx=(8, 0))
        ModernButton.sized(entry_row, "Mark", self.add_mark, SURFACE,
                           "neutral", font_px=13, height=40, pad_x=14,
                           tracking=0.01).grid(row=0, column=2, padx=(8, 0))
        self.stamp_lbl = tk.Label(entry_row, bg=SURFACE, fg=CYAN,
                                  font=(UI_FONT, 10), anchor="w")
        self.stamp_lbl.grid(row=1, column=0, columnspan=3, sticky="w",
                            pady=(4, 0))
        self.note_entry.bind("<Return>", lambda e: self.add_note())
        self.note_entry.bind("<KP_Enter>", lambda e: self.add_note())
        self.note_entry.bind("<Escape>", lambda e: self._cancel_note())
        self.note_entry.bind("<Key>", self._note_key, add="+")

        opts = tk.Frame(lp, bg=SURFACE)
        opts.grid(row=3, column=0, columnspan=2, sticky="ew", padx=4,
                  pady=(2, 0))
        self.autolog_var = tk.BooleanVar(
            value=self.settings.get("auto_log", True))
        tk.Checkbutton(opts, text="Log button presses automatically",
                       variable=self.autolog_var, bg=SURFACE, fg=MUTED,
                       selectcolor=BG, activebackground=SURFACE,
                       activeforeground=TEXT, highlightthickness=0, bd=0,
                       font=(UI_FONT, 10), takefocus=0,
                       command=self._save_autolog).pack(side="left")
        self._note_hint()
        self.note_var.trace_add("write", lambda *_: self._note_changed())
        self._rebuild_log_tree()

    def _layout(self, event=None, force=False):
        if event is not None and event.widget is not self.root:
            return
        W, H = self.root.winfo_width(), self.root.winfo_height()
        seg_on = self.seg_panel_on and self.feat("running_order")
        log_on = self.log_on and self.feat("log")
        tod_on = self.tod_on and self.feat("clock")
        logos = self.feat("logos")
        has_logo = bool(self.top_logo._src) and logos
        rows = min(max(len(self._plan()), len(self.segment_times), 2), 6)
        key = (W, H, seg_on, log_on, tod_on, has_logo, self._backtime_on(), rows,
               tuple(sorted(self.settings.get("features", {}).items())))
        if W < 50 or H < 50 or (key == self._last_layout and not force):
            return
        self._last_layout = key

        def put(w, x, y, ww, hh):
            w.place(x=int(x), y=int(y), width=max(int(ww), 1),
                    height=max(int(hh), 1))

        pad = max(px(14), int(min(W, H) * 0.016))
        hh = max(px(64), int(H * 0.08))           # header height
        tbh = px(56)                              # toolbar height
        if log_on:
            panel_w = max(px(480), int(W * 0.3))
        elif seg_on:
            panel_w = max(px(500), int(W * 0.27))
        else:
            panel_w = 0
        A = W - panel_w                        # main area width

        # Header ---------------------------------------------------------
        put(self.header, 0, 0, W, hh)
        put(self.header_line, 0, hh, W, 1)
        x = pad + 4
        if has_logo:
            put(self.top_logo, x, hh * 0.16, hh * 1.8, hh * 0.68)
            x += hh * 1.8 + pad
        else:
            self.top_logo.place_forget()
        self.app_lbl.config(font=(UI_FONT, -int(hh * 0.2)))
        self.show_name_lbl.config(font=(UI_BOLD, -int(hh * 0.36)))
        mark_h = max(8, int(hh * 0.16))
        if mark_h != self._app_mark_h:
            self._app_mark_h = mark_h
            self._app_mark_img = author_logo(mark_h)
            self.app_mark.config(image=self._app_mark_img or "")
        if self._app_mark_img is not None:
            mw = self._app_mark_img.width()
            put(self.app_mark, x, hh * 0.12 + (hh * 0.3 - mark_h) / 2, mw,
                mark_h)
            put(self.app_lbl, x + mw + px(8), hh * 0.12, W * 0.3, hh * 0.3)
        else:
            self.app_mark.place_forget()
            put(self.app_lbl, x, hh * 0.12, W * 0.3, hh * 0.3)
        put(self.show_name_lbl, x, hh * 0.38, W * 0.4, hh * 0.5)
        cl_w = hh * 2.6
        if logos:
            put(self.company_logo, W - pad - cl_w, hh * 0.16, cl_w, hh * 0.68)
        else:
            self.company_logo.place_forget()
            cl_w = -pad
        if tod_on:
            tw = hh * 3.2
            tx = W - pad - cl_w - pad * 2 - tw
            self.tod_cap.config(font=(UI_FONT, -int(hh * 0.18)))
            put(self.tod_cap, tx, hh * 0.08, tw, hh * 0.26)
            put(self.tod_lbl, tx, hh * 0.3, tw, hh * 0.62)
        else:
            self.tod_cap.place_forget()
            self.tod_lbl.place_forget()

        # Toolbar --------------------------------------------------------
        ty = H - tbh
        put(self.toolbar, 0, ty, W, tbh)
        put(self.toolbar_line, 0, ty - 1, W, 1)
        bx = pad
        need = {self.undo_btn: "undo", self.dual_btn: "dual",
                self.screen_btn: "dual", self.segtimes_btn: "running_order",
                self.log_btn: "log", self.tod_btn: "clock",
                self.deck_btn: "deck", self.sync_btn: "sync"}
        shown_groups = []
        for group in self.tool_groups:
            keep = []
            for b in group:
                if b is self.plan_btn:
                    ok = self.feat("goals") or self.feat("backtime")
                else:
                    ok = self.feat(need[b]) if b in need else True
                if ok:
                    keep.append(b)
                else:
                    b.place_forget()
            if keep:
                shown_groups.append(keep)
        for div in self.tool_divs:
            div.place_forget()
        ew, eh = self.exit_btn.fixed
        ex = W - pad - ew
        put(self.exit_btn, ex, ty + (tbh - eh) / 2, ew, eh)
        sw_, sh_ = self.settings_btn.fixed
        sx = ex - px(6) - sw_
        put(self.settings_btn, sx, ty + (tbh - sh_) / 2, sw_, sh_)
        fw, fh = self.full_btn.fixed
        sx = sx - px(6) - fw
        put(self.full_btn, sx, ty + (tbh - fh) / 2, fw, fh)
        # Buttons that don't fit (narrow window) go into a "More" menu
        mw, mh = self.more_btn.fixed
        limit = sx - px(10)
        total = bx + sum(b.fixed[0] + px(4) for g in shown_groups for b in g) \
            + px(12) * (len(shown_groups) - 1)
        if total > limit:
            limit -= mw + px(10)
        self._overflow = []
        for gi, group in enumerate(shown_groups):
            if gi and not self._overflow:
                if bx + px(12) < limit:
                    put(self.tool_divs[gi - 1], bx + px(4), ty + px(14), 1,
                        tbh - px(28))
                bx += px(12)
            for b in group:
                bw, bh = b.fixed
                if self._overflow or bx + bw > limit:
                    self._overflow.append(b)
                    b.place_forget()
                    continue
                if b is self.screen_btn:     # ▾ sits against its button
                    bx -= px(3)
                put(b, bx, ty + (tbh - bh) / 2, bw, bh)
                bx += bw + px(4)
        if self._overflow:
            put(self.more_btn, bx + px(4), ty + (tbh - mh) / 2, mw, mh)
            bx += mw + px(8)
        else:
            self.more_btn.place_forget()
        self._status_anchor = (sx - px(12), ty + tbh / 2, bx + px(12))
        self._place_status()

        # Body -----------------------------------------------------------
        top, bottom = hh + pad, ty - pad
        side_top = top
        if self._backtime_on():
            band_h = max(px(76), int(H * 0.085))
            put(self.bt_panel, pad, top, A - 2 * pad, band_h)
            pill_area = max(px(260), (A - 2 * pad) * 0.3)
            cell = (A - 2 * pad - pill_area - px(24)) / 3
            for i, key in enumerate(("out", "to_out", "proj")):
                capl, val = self.bt_parts[key]
                capl.config(font=(UI_FONT, -int(max(px(12), band_h * 0.17))))
                val.config(font=(DIGIT_FONT, -int(max(px(22), band_h * 0.44)),
                                 DIGIT_WEIGHT))
                cx0 = pad + px(24) + i * cell
                put(capl, cx0, top + band_h * 0.12, cell - px(12), band_h * 0.26)
                put(val, cx0, top + band_h * 0.36, cell - px(12), band_h * 0.52)
            self.bt_pill.h = int(band_h * 0.5)
            self.bt_pill.px = int(band_h * 0.24)
            self.bt_pill._key = None
            self._bt_pill_anchor = (pad + A - 2 * pad - px(20), top + band_h / 2)
            put(self.bt_note, pad + A - 2 * pad - pill_area, top + px(4),
                pill_area - px(20), band_h * 0.22)
            top += band_h + pad
        else:
            for w in [self.bt_panel, self.bt_pill, self.bt_note] + \
                    [x for pair in self.bt_parts.values() for x in pair]:
                w.place_forget()
        body_h = bottom - top
        card_h = (body_h - pad) / 2
        lw = min(max(A * 0.17, px(180)), px(360))

        # Master buttons, each centred alongside its card
        if self.feat("master_buttons"):
            big_h = min(card_h * 0.55, lw * 0.6)
            for i, b in enumerate((self.all_start_btn, self.all_stop_btn)):
                cy = top + i * (card_h + pad) + card_h / 2
                put(b, pad, cy - big_h / 2, lw, big_h)
            cx = pad + lw + pad
        else:
            self.all_start_btn.place_forget()
            self.all_stop_btn.place_forget()
            cx = pad
        cw = A - cx - pad
        groups = (("seg", self.seg_cap_lbl, self.seg_title_lbl, self.seg_chip,
                   self.seg_label,
                   (self.seg_start_btn, self.seg_stop_btn,
                    self.seg_reset_btn, self.seg_preset_btn)),
                  ("show", self.show_cap_lbl, self.show_title_lbl,
                   self.show_chip, self.show_label,
                   (self.show_start_btn, self.show_stop_btn,
                    self.show_reset_btn, self.show_preset_btn)))
        for i, (k, cap, title, chip, digits, btns) in enumerate(groups):
            cy = top + i * (card_h + pad)
            put(self.cards[k], cx, cy, cw, card_h)
            ip = max(px(16), card_h * 0.07)
            x0 = cx + px(40)                      # clear of the tally light
            # 2x2 control grid on the right of the card
            gap = px(10)
            bh = min(max(card_h * 0.22, px(40)), px(92))
            bw = min(bh * 2.2, cw * 0.15)
            gx = cx + cw - ip - (2 * bw + gap)
            gy = cy + (card_h - (2 * bh + gap)) / 2
            for j, b in enumerate(btns):
                put(b, gx + (j % 2) * (bw + gap), gy + (j // 2) * (bh + gap),
                    bw, bh)
            content_w = gx - x0 - ip
            c_h = max(px(18), card_h * 0.075)
            t_h = max(px(26), card_h * 0.12)
            cap.config(font=(UI_FONT, -int(c_h * 0.8)))
            title.config(font=(UI_BOLD, -int(t_h * 0.78)))
            put(cap, x0, cy + ip * 0.8, content_w * 0.65, c_h)
            put(title, x0, cy + ip * 0.8 + c_h, content_w, t_h)
            chip.h = int(min(max(card_h * 0.085, px(30)), px(42)))
            chip.px = int(chip.h * 0.48)
            chip._key = None
            self._pill_pos[k] = (x0 + content_w, cy + ip * 0.8 + c_h * 0.4)
            row_h = max(px(22), card_h * 0.075)
            bar_t = px(8)
            goals = self.feat("goals")
            oy = cy + card_h - ip * 0.9 - ((bar_t + px(8) + row_h) if goals else 0)
            dtop = cy + ip * 0.8 + c_h + t_h
            put(digits, x0 - 4, dtop, content_w, oy - dtop)
            goal_l, delta_l, bar = self.ou[k]
            if not goals:
                for w_ in (goal_l, delta_l, bar):
                    w_.place_forget()
                continue
            goal_l.config(font=(UI_FONT, -int(min(max(row_h * 0.72, px(13)), px(26)))))
            delta_l.config(font=(UI_BOLD, -int(min(max(row_h * 0.72, px(13)), px(26)))))
            put(goal_l, x0, oy, content_w * 0.55, row_h)
            put(delta_l, x0 + content_w * 0.45, oy, content_w * 0.55, row_h)
            put(bar, x0, oy + row_h + px(8), content_w, bar_t)

        # Side panels: running order and/or log
        spx, pw = A, panel_w - pad
        for w in (self.seg_panel, self.log_panel, self.seg_bg, self.log_bg):
            w.place_forget()
        inset = px(12)

        def panel(bg, frame, y, h):
            put(bg, spx, y, pw, h)
            put(frame, spx + inset, y + inset, pw - 2 * inset, h - 2 * inset)

        side_h = bottom - side_top
        if log_on and seg_on:
            # running order sized to its rows (up to 6), the log gets the rest
            rows = min(max(len(self._plan()), len(self.segment_times), 2), 6)
            want = px(150) + rows * self._seg_row_h
            sh = min(max(want, side_h * 0.3), side_h * 0.55)
            panel(self.seg_bg, self.seg_panel, side_top, sh)
            panel(self.log_bg, self.log_panel, side_top + sh + pad,
                  side_h - sh - pad)
        elif log_on:
            panel(self.log_bg, self.log_panel, side_top, side_h)
        elif seg_on:
            panel(self.seg_bg, self.seg_panel, side_top, side_h)
        if self._fonts_custom():
            remap_fonts(self.root)
        self._update_visuals()

    def _place_status(self):
        anchor = getattr(self, "_status_anchor", None)
        if not anchor:
            return
        right, cy, left_limit = anchor
        x = right
        for w in (self.status_lbl, self.sync_pill, self.deck_pill):
            if (w is self.sync_pill and not self.feat("sync")) or \
                    (w is self.deck_pill and not self.feat("deck")):
                w.place_forget()
                continue
            if w is self.status_lbl:
                ww = tkfont.Font(font=w.cget("font")).measure(
                    w.cget("text")) + px(6)
                hh = px(20)
            else:
                ww, hh = w.width_px, w.h
            x -= ww
            if x < left_limit:
                w.place_forget()
                continue
            w.place(x=int(x), y=int(cy - hh / 2), width=ww, height=hh)
            x -= px(8)

    def _bind_keys(self):
        r = self.root
        self._keys_down = set()
        r.bind_all("<KeyRelease>",
                   lambda e: self._keys_down.discard(e.keysym), add="+")
        r.bind("<FocusIn>", lambda e: self._keys_down.clear(), add="+")

        def hot(fn):
            # Single-key shortcuts are ignored while typing a note
            def handler(e):
                if isinstance(e.widget, (tk.Entry, ttk.Entry, tk.Text)):
                    return None
                if not self.feat("shortcuts"):
                    return None
                # Ignore key auto-repeat while a key is held down
                if e.keysym in self._keys_down:
                    return None
                self._keys_down.add(e.keysym)
                fn()
                return None
            return handler

        for keys, fn in ((("<space>",), self.toggle_running),
                         (("<Key-n>", "<Key-N>"), self.next_segment),
                         (("<Key-d>", "<Key-D>"), self.toggle_dual),
                         (("<Key-l>", "<Key-L>"), self.toggle_seg_panel),
                         (("<Key-t>", "<Key-T>"), self.toggle_tod),
                         (("<Key-g>", "<Key-G>"), self.open_plan)):
            for k in keys:
                r.bind(k, hot(fn))
        r.bind("<Control-z>", lambda e: self.feat("undo") and self.undo())
        r.bind("<Control-Z>", lambda e: self.feat("undo") and self.undo())
        r.bind("<Control-comma>", lambda e: self.open_settings())
        r.bind("<Control-m>", lambda e: self.add_mark())
        r.bind("<Control-M>", lambda e: self.add_mark())
        if IS_MAC:
            r.bind("<Command-z>", lambda e: self.feat("undo") and self.undo())
            r.bind("<Command-comma>", lambda e: self.open_settings())
            r.bind("<Command-m>", lambda e: self.add_mark())
        r.bind("<F2>", lambda e: self.focus_note())
        r.bind("<F11>", lambda e: self.toggle_fullscreen())
        r.bind("<Escape>", lambda e: None if isinstance(
            e.widget, (tk.Entry, ttk.Entry, tk.Text))
            else self.set_fullscreen(False))

    def is_fullscreen(self):
        try:
            return bool(int(self.root.attributes("-fullscreen")))
        except (tk.TclError, ValueError):
            return False

    def set_fullscreen(self, on):
        try:
            self.root.attributes("-fullscreen", bool(on))
        except tk.TclError:
            pass
        # the window manager applies it a moment later
        self.root.after(150, lambda: self._toggle_look(
            self.full_btn, self.is_fullscreen()))

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.is_fullscreen())

    def _toggle_look(self, button, on):
        button.set_variant("on" if on else "ghost")

    def toggle_seg_panel(self):
        self.seg_panel_on = not self.seg_panel_on
        self.settings["segment_panel"] = self.seg_panel_on
        save_settings(self.settings)
        self._toggle_look(self.segtimes_btn, self.seg_panel_on)
        self._layout(force=True)

    # ------------------------------------------------------------ timing --
    @property
    def running(self):
        return self.show.running or self.segment.running

    def _lock_phase(self):
        """Keep every clock ticking over on the same instant: the program
        clock follows the time of day, the segment clock follows the
        program. Each is nudged by under a second, without changing what it
        shows, so nothing jumps or goes backwards."""
        if not self.feat("tick_together"):
            return
        if self.show.running:
            tod = self.clock.now().microsecond / 1e6
            p = self.show.elapsed()
            d = tod - (p % 1.0)
            if abs(d) > 0.002 and 0 <= (p % 1.0) + d < 1.0:
                self.show.started_at -= d
        if not (self.show.running and self.segment.running):
            return
        p, s_ = self.show.elapsed(), self.segment.elapsed()
        # Keep the second on screen, just move where in that second we are,
        # so the display never jumps or goes backwards.
        d = (p % 1.0) - (s_ % 1.0)
        if abs(d) > 0.002:
            self.segment.started_at -= d

    def _tick(self):
        self._process_api()
        self._lock_phase()
        seg, show = fmt(self.segment.elapsed()), fmt(self.show.elapsed())
        changed = False
        if self.seg_label.cget("text") != seg:
            self.seg_label.config(text=seg)
            self.segment_times[-1] = self.segment.elapsed()
            n = len(self.segment_times)
            if self.seg_list.exists(str(n)):
                self.seg_list.item(str(n), values=self._seg_row(n))
            changed = True
        if self.show_label.cget("text") != show:
            self.show_label.config(text=show)
            changed = True
        if changed:
            self._update_overunder()
        if self.output is not None:
            status, col = self._out_status
            oseg, ocol = self._out_seg_clock()
            self.output.update_times(oseg, show, status, col, ocol)
            self.output.update_extras(self._out_seg_caption(),
                                      *self._out_clock())
        if self.tod_on:
            tod = f"{self.clock.now():%H:%M:%S}"
            if self.tod_lbl.cget("text") != tod:
                self.tod_lbl.config(text=tod)
            state = "ok" if self.clock.mode == "pc" else self.clock.state
            cap = {"pc": "Time of day", "ntp": "Network time",
                   "ltc": "LTC timecode"}[self.clock.mode]
            if state == "warn":
                cap += " \u2013 freewheeling" if self.clock.mode == "ltc" \
                    else " \u2013 checking"
            elif state == "error":
                cap += " \u2013 lost, using PC clock" \
                    if self.clock.mode == "ltc" else " \u2013 no reply"
            self._cfg(self.tod_cap, text=cap,
                      fg={"ok": MUTED, "warn": AMBER, "error": RED}[state])
            self._cfg(self.tod_lbl, fg={"ok": TEXT, "warn": AMBER,
                                        "error": RED}[state])
        # Slower jobs on a timetable (the tick rate itself varies, below)
        now = time.monotonic()
        due = self._due
        if now >= due.get("status", 0):
            due["status"] = now + 1.0
            self.clock.refresh_status()
        if now >= due.get("state", 0):        # crash-safe state every 2 s
            due["state"] = now + 2.0
            self._save_show_state()
        if now >= due.get("backtime", 0):     # back-time band ~5x a second
            due["backtime"] = now + 0.2
            self._update_backtime()
        if now >= due.get("api", 0):          # publish state ~6x a second
            due["api"] = now + 0.15
            self.api.state = self._api_snapshot()
            self._update_deck_status()
        self.root.after(self._next_tick_ms(), self._tick)

    def _next_tick_ms(self):
        """Wake up just after the next second boundary of whatever is on
        screen, so every clock changes exactly on its second instead of up
        to 50 ms late. Never sleep longer than 50 ms (buttons, API)."""
        frac = []
        if self.show.running:
            frac.append(self.show.elapsed() % 1.0)
        if self.segment.running:
            frac.append(self.segment.elapsed() % 1.0)
        if self.tod_on or self.output is not None:
            frac.append(self.clock.now().microsecond / 1e6)
        if not frac:
            return 50
        to_next = min(1.0 - f for f in frac)
        return max(1, min(50, int(to_next * 1000) + 3))

    def _update_visuals(self):
        seg, show = self.segment.running, self.show.running
        # "Lit" buttons like a broadcast panel: green START while running,
        # soft red STOP while stopped
        self.seg_start_btn.set_variant("go" if seg else "neutral")
        self.seg_stop_btn.set_variant("neutral" if seg else "stop_dim")
        self.show_start_btn.set_variant("go" if show else "neutral")
        self.show_stop_btn.set_variant("neutral" if show else "stop_dim")
        self.all_start_btn.set_variant("go" if seg and show else "neutral")
        self.all_stop_btn.set_variant(
            "stop_dim" if not seg and not show else "neutral")
        for k, running, chip, digits in (
                ("seg", seg, self.seg_chip, self.seg_label),
                ("show", show, self.show_chip, self.show_label)):
            self.cards[k].set_tally(GREEN if running else RED)
            if running:
                chip.set("Running", GREEN_DIM, GREEN)
            else:
                chip.set("Stopped", RED_DIM, "#ff8592")
            pos = self._pill_pos.get(k)
            if pos:
                chip.place(x=int(pos[0] - chip.width_px), y=int(pos[1]),
                           width=chip.width_px, height=chip.h)
            digits.config(fg=TEXT if running else "#c4ccd6")
        self.awake.set((seg or show) and self.feat("keep_awake"))
        self._update_overunder()

    def start_segment(self):
        if not self.segment.running:
            self.segment.start()
            self.log.segment_started(len(self.segment_times))
            self._log_event("SEG START",
                            f"{self.current_show['segment_label']} started")
        self._update_visuals()

    def stop_segment(self):
        if self.segment.running:
            self.segment.stop()
            self._log_event("SEG STOP",
                            f"{self.current_show['segment_label']} stopped")
        self._update_visuals()

    def start_show(self):
        if not self.show.running:
            self.show.start()
            self._log_event("PROG START",
                            f"{self.current_show['show_label']} started")
        self._update_visuals()

    def stop_show(self):
        if self.show.running:
            self.show.stop()
            self._log_event("PROG STOP",
                            f"{self.current_show['show_label']} stopped")
        self._update_visuals()

    def start_all(self):
        if self.show.running and self.segment.running:
            return
        self.show.start()
        self.segment.start()
        self.log.segment_started(len(self.segment_times))
        self._log_event("ALL START", "All timers started")
        self._update_visuals()

    def stop_all(self, log=True):
        if not self.show.running and not self.segment.running:
            self._update_visuals()
            return
        self.show.stop()
        self.segment.stop()
        if log:
            self._log_event("ALL STOP", "All timers stopped")
        self._update_visuals()

    def toggle_running(self):
        self.stop_all() if self.running else self.start_all()

    def next_segment(self):
        """Segment RESET: log the current segment and start a new one."""
        if int(self.segment.elapsed()) == 0:
            return
        done = len(self.segment_times)
        self.segment_times[-1] = self.segment.elapsed()
        goal = self._goal(done)
        result = ""
        if goal:
            d = int(self.segment_times[-1]) - goal
            result = (f" (goal {fmt(goal)}, " + (
                f"{fmt_short(d)} over)" if d > 0 else
                f"{fmt_short(d)} under)" if d < 0 else "on time)"))
        nxt = self._seg_name(done + 1)
        self._log_event("SEGMENT", f"Segment {done} ended at "
                        f"{fmt(self.segment_times[-1])}{result} \u2014 "
                        f"segment {done + 1}{': ' + nxt if nxt else ''} "
                        f"begins")
        self.log.segment_ended(done)
        self.segment_times.append(0.0)
        self.segment.set(0)
        if self.segment.running:
            self.log.segment_started(done + 1)
        self._rebuild_seg_list()

    def reset_all(self, confirm=True):
        """Program RESET: stop everything, clear both timers + segment log."""
        confirm = confirm and self.feat("confirm_reset")
        if confirm and (self.running or self.show.elapsed() >= 1):
            if not messagebox.askyesno(
                    APP_NAME, "Reset the program? This stops both timers and "
                              "clears the segment times.", parent=self.root):
                return
        if not self._log_handled("resetting"):
            return
        self.stop_all(log=False)
        self.show.set(0)
        self.segment.set(0)
        self.segment_times = [0.0]
        self._rebuild_seg_list()
        self.log.clear()
        self._rebuild_log_tree()

    def _rebuild_seg_list(self):
        self.seg_list.delete(*self.seg_list.get_children())
        n = len(self.segment_times)
        rows = max(n, len(self._plan()))
        for i in range(1, rows + 1):
            self.seg_list.insert("", "end", iid=str(i),
                                 values=self._seg_row(i),
                                 tags=(("odd",) if i % 2 else ()) +
                                 self._seg_tags(i))
        if self.seg_list.exists(str(n)) and n > 6:
            self.seg_list.see(str(n))
        self._update_overunder()
        if getattr(self, "_last_layout", None) is not None:
            self._layout()

    # ------------------------------------------------------- over / under --
    def _plan(self):
        return self.current_show.setdefault("plan", [])

    def _out_seg_clock(self, show=None):
        """The output's segment clock: counting up, or down to the goal and
        then up again in red once it's over."""
        show = show or self.current_show
        el = self.segment.elapsed()
        goal = self._goal(len(self.segment_times))
        if output_row(show, "seg_time").get("count") != "down" or not goal:
            return fmt(el), None
        left = goal - int(el)
        if left >= 0:
            return fmt(left), (AMBER if left <= 15 else None)
        return "+" + fmt(-left), RED

    def output_values(self, show=None):
        """Live values for an output view showing `show`."""
        show = show or self.current_show
        status, col = self._out_status
        clock = output_row(show, "clock")
        mode = (clock.get("mode") or "tod") if clock.get("show") else "off"
        ccap, ctext = "", ""
        if mode != "off":
            clk = self.clock
            if mode == "tc" and clk.mode == "ltc" and clk.ltc_tc and \
                    clk.state == "ok":
                ccap, ctext = "TIMECODE", clk.ltc_tc
            else:
                ccap, ctext = "TIME OF DAY", f"{clk.now():%H:%M:%S}"
        oseg, ocol = self._out_seg_clock(show)
        return (oseg, fmt(self.show.elapsed()), status, col,
                self._out_seg_caption(show), ccap, ctext, ocol)

    def preview_output_show(self, show):
        """Show a layout being edited on the real output, live."""
        if self.output is not None and \
                show.get("name") == self.current_show.get("name"):
            self.output.apply_show(show)

    def open_output_layout(self):
        def done(show):
            for k in ("output_layout", "bg", "fg", "accent"):
                self.current_show[k] = show[k]
            if self.current_show.get("name"):
                save_show(self.current_show)
            self.apply_show()
        OutputLayoutEditor(self.root, self, self.current_show, done)

    def _out_seg_caption(self, show=None):
        show = show or self.current_show
        label = show["segment_label"].upper()
        prog = self._seg_progress()
        if not output_row(show, "seg_title").get("segments_left") or not prog:
            return label
        n, total, left = prog
        if n > total:
            return f"{label} {n} OF {total}  \u00b7  {n - total} EXTRA"
        return f"{label} {n} OF {total}  \u00b7  {left} LEFT"

    def _out_clock(self):
        """(caption, time text) for the output's clock band."""
        row = output_row(self.current_show, "clock")
        mode = (row.get("mode") or "tod") if row.get("show") else "off"
        if mode == "off":
            return "", ""
        clk = self.clock
        if mode == "tc" and clk.mode == "ltc" and clk.ltc_tc and \
                clk.state == "ok":
            return "TIMECODE", clk.ltc_tc
        return "TIME OF DAY", f"{clk.now():%H:%M:%S}"

    def _seg_total(self):
        """Segments in the show: the number set in Goals, else the running
        order's length (0 = unknown)."""
        try:
            count = int(self.current_show.get("segment_count") or 0)
        except (TypeError, ValueError):
            count = 0
        return count or len(self._plan())

    def _seg_progress(self):
        """(current segment, total, segments left after this one) or None."""
        total = self._seg_total()
        if not total:
            return None
        n = len(self.segment_times)
        return n, total, max(total - n, 0)

    def _goal(self, n):
        plan = self._plan()
        return (plan[n - 1].get("goal") or None) if 0 < n <= len(plan) else None

    def _seg_name(self, n):
        plan = self._plan()
        return plan[n - 1].get("name", "") if 0 < n <= len(plan) else ""

    def _seg_actual(self, n):
        if n == len(self.segment_times):
            return self.segment.elapsed()
        return self.segment_times[n - 1] if n <= len(self.segment_times) \
            else None

    def _seg_row(self, n):
        goal, actual = self._goal(n), self._seg_actual(n)
        diff = ""
        if goal and actual is not None:
            diff = fmt_delta(int(actual) - goal)
        back = ""
        bt = getattr(self, "_bt", None)
        if bt and n > len(self.segment_times) and n - 1 < len(bt["start_by"]):
            back = bt["start_by"][n - 1] or ""
        return (n, self._seg_name(n), fmt_short(goal) if goal else "",
                fmt_short(actual) if actual is not None else "", diff, back)

    def _seg_tags(self, n):
        cur = len(self.segment_times)
        if n == cur:
            return ("current",)
        if n > cur:
            return ("planned",)
        goal = self._goal(n)
        if not goal:
            return ()
        return ("over",) if int(self.segment_times[n - 1]) > goal \
            else ("under",)

    def _total_delta(self):
        """Running over/under: finished segments count in full, the current
        one only once it's past its goal."""
        total, any_goal = 0, False
        n = len(self.segment_times)
        for i in range(1, n + 1):
            goal = self._goal(i)
            if not goal:
                continue
            any_goal = True
            d = int(self._seg_actual(i)) - goal
            total += d if i < n else max(d, 0)
        return total if any_goal else None

    def _cfg(self, widget, **kw):
        if any(str(widget.cget(k)) != str(v) for k, v in kw.items()):
            widget.config(**kw)

    def _set_bar(self, k, ratio, colour):
        self.ou[k][2].set(ratio, colour)

    def _update_overunder(self):
        if not hasattr(self, "ou"):
            return
        show = self.current_show
        n = len(self.segment_times)
        cur = self.segment.elapsed()
        goal, name = self._goal(n), self._seg_name(n)

        cap = f"{show['segment_label']} {n}"
        prog = self._seg_progress()
        if prog:
            cap += (f" of {prog[1]}  \u00b7  {prog[0] - prog[1]} extra"
                    if prog[0] > prog[1] else
                    f" of {prog[1]}  \u00b7  {prog[2]} left")
        nxt = self._seg_name(n + 1)
        if nxt and self.feat("next_hint"):
            cap += f"      Next: {nxt}"
        self._cfg(self.seg_cap_lbl, text=cap)
        self._cfg(self.seg_title_lbl, text=name or f"{show['segment_label']} {n}")

        # Segment card
        g_lbl, d_lbl = self.ou["seg"][0], self.ou["seg"][1]
        warn = 15
        if goal and self.feat("goals"):
            d = int(cur) - goal
            self._cfg(g_lbl, text=f"Goal {fmt_short(goal)}", fg=MUTED)
            if d > 0:
                txt, col = f"+{fmt_short(d)} over", RED
            elif d == 0:
                txt, col = "On goal", AMBER
            else:
                txt = f"{fmt_short(-d)} left"
                col = AMBER if -d <= warn else GREEN
            self._cfg(d_lbl, text=txt, fg=col)
            bar = RED if d > 0 else AMBER if -d <= warn else GREEN
            self._set_bar("seg", cur / goal, bar)
            digit_col = RED if d > 0 else AMBER if -d <= warn else None
            self._out_status = (txt.upper(), col)
            if self.segment.running:
                self.cards["seg"].set_tally(RED if d > 0 else AMBER
                                            if -d <= warn else GREEN)
        else:
            self._cfg(g_lbl, text="No goal set \u2013 click to add one",
                      fg=FAINT)
            self._cfg(d_lbl, text="")
            self._set_bar("seg", 0, CYAN)
            digit_col = None
            self._out_status = ("", None)
            if self.segment.running:
                self.cards["seg"].set_tally(GREEN)
        if digit_col is None:
            digit_col = TEXT if self.segment.running else "#c4ccd6"
        self._cfg(self.seg_label, fg=digit_col)

        # Program card: planned total + running over/under
        g_lbl, d_lbl = self.ou["show"][0], self.ou["show"][1]
        plan_total = sum(p.get("goal") or 0 for p in self._plan())
        delta = self._total_delta()
        self._cfg(self.show_cap_lbl, text=show["show_label"])
        prog = self._seg_progress()
        if prog:
            self._cfg(self.show_title_lbl,
                      text=f"{prog[1]} segments  \u00b7  "
                           + (f"{prog[0] - prog[1]} extra" if prog[0] > prog[1]
                              else f"{prog[2]} left"))
        if plan_total:
            self._cfg(g_lbl, text=f"Plan {fmt_short(plan_total)}", fg=MUTED)
            if not prog:
                self._cfg(self.show_title_lbl,
                          text=f"{len(self._plan())} segments planned")
            ratio = self.show.elapsed() / plan_total
            self._set_bar("show", ratio, RED if ratio > 1 else
                          AMBER if ratio > 0.95 else CYAN)
        else:
            self._cfg(g_lbl, text="No segment goals \u2013 click to plan the "
                                  "show", fg=FAINT)
            if not prog:
                self._cfg(self.show_title_lbl, text="Running time")
            self._set_bar("show", 0, CYAN)
        if delta is None:
            self._cfg(d_lbl, text="")
        elif delta > 0:
            self._cfg(d_lbl, text=f"+{fmt_short(delta)} over", fg=RED)
        elif delta < 0:
            self._cfg(d_lbl, text=f"{fmt_short(-delta)} under", fg=GREEN)
        else:
            self._cfg(d_lbl, text="On time", fg=CYAN)

        # Segment panel footer
        if hasattr(self, "seg_total_lbl"):
            actual = sum(int(self._seg_actual(i) or 0)
                         for i in range(1, n + 1))
            txt = f"Plan {fmt_short(plan_total)}      Actual {fmt_short(actual)}"
            col = TEXT
            if delta is not None:
                txt += f"      {fmt_delta(delta)}"
                col = RED if delta > 0 else GREEN if delta < 0 else TEXT
            self._cfg(self.seg_total_lbl, text=txt, fg=col)

    def _save_plan(self, plan, hard_out=None, segment_count=None):
        self.current_show["plan"] = [dict(r) for r in plan]
        if segment_count is not None:
            self.current_show["segment_count"] = int(segment_count)
        if hard_out is not None:
            old = self.current_show.get("hard_out") or {}
            self.current_show["hard_out"] = hard_out
            if hard_out != old:
                desc = {"off": "off",
                        "tod": f"off air at {hard_out.get('tod')}",
                        "trt": f"program length "
                               f"{fmt(hard_out.get('trt') or 0)}"}[
                    hard_out["mode"]]
                self._log_event("HARD OUT", f"Hard out set: {desc}")
        if self.current_show.get("name"):
            save_show(self.current_show)
            self.settings["current_show"] = self.current_show["name"]
            save_settings(self.settings)
        self._seg_title_cache = None
        self._rebuild_seg_list()

    def open_plan(self, start_at=None):
        PlanEditor(self.root, self.current_show["name"], self._plan(),
                   self._save_plan, start_at=start_at,
                   hard_out=self.current_show.get("hard_out"),
                   segment_count=self.current_show.get("segment_count") or 0)

    def _seg_list_click(self, event):
        row = self.seg_list.identify_row(event.y)
        if row:
            self.open_plan(start_at=int(row))

    def set_current_goal(self):
        n = len(self.segment_times)

        def apply(secs):
            plan = self._plan()
            while len(plan) < n:
                plan.append({"name": "", "goal": 0})
            plan[n - 1]["goal"] = secs
            self._save_plan(plan)
            self._log_event("GOAL", f"Segment {n} goal set to {fmt(secs)}")
        NumPad(self.root, f"Goal for segment {n}", apply)

    def open_preset(self, target):
        show = self.current_show
        if target == "show":
            def set_show(s):
                def do():
                    self.show.set(s)
                    self._log_event("PRESET", f"{show['show_label']} preset "
                                    f"to {fmt(s)}")
                self._undoable(f"{show['show_label']} preset", do)
            NumPad(self.root, f"Set {show['show_label']}" +
                   ("  (keeps running)" if self.show.running else ""),
                   set_show)
        else:
            def set_seg(s):
                def do():
                    self.segment.set(s)
                    self.segment_times[-1] = s
                    self._log_event("PRESET", f"{show['segment_label']} "
                                    f"preset to {fmt(s)}")
                self._undoable(f"{show['segment_label']} preset", do)
            NumPad(self.root, f"Set {show['segment_label']}" +
                   ("  (keeps running)" if self.segment.running else ""),
                   set_seg)

    # ------------------------------------------------------- dual screen --
    def _monitor_choice(self):
        monitors = get_monitors(self.root)
        idx = self.settings.get("output_monitor")
        if isinstance(idx, int) and 0 <= idx < len(monitors):
            return monitors[idx]
        for m in monitors:
            if not m["primary"]:
                return m
        return monitors[0]

    def _startup_output(self):
        self._toggle_look(self.segtimes_btn, self.seg_panel_on)
        self._toggle_look(self.log_btn, self.log_on)
        self._toggle_look(self.tod_btn, self.tod_on)
        if self.feat("dual") and self.feat("dual_on_start"):
            monitors = get_monitors(self.root)
            if len(monitors) > 1 or self.settings.get("output_monitor") is not None:
                self.open_output()

    def open_output(self):
        if self.output is None:
            mon = self._monitor_choice()
            self.output = OutputWindow(self.root, mon, self._output_closed,
                                       self.current_show)
            if len(get_monitors(self.root)) == 1:
                self.output.go_windowed()
            self.root.after(100, self.root.focus_force)
        self._toggle_look(self.dual_btn, True)

    def close_output(self):
        if self.output is not None:
            out, self.output = self.output, None
            out.destroy()
        self._toggle_look(self.dual_btn, False)

    def _output_closed(self):
        self.output = None
        self._toggle_look(self.dual_btn, False)

    def toggle_dual(self):
        if self.output is None:
            self.open_output()
        else:
            self.close_output()

    def _more_menu(self):
        menu = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 11), bg=SURFACE_2,
                       fg=TEXT, activebackground=CYAN_DIM,
                       activeforeground=CYAN, bd=0, relief="flat")
        for b in self._overflow:
            label = getattr(b, "menu_label", None) or b.text
            if b.variant == "on":
                label = "\u2714 " + label
            menu.add_command(label=label, command=b.command)
        self._popup(menu, self.more_btn)

    def _popup(self, menu, button):
        if not button.winfo_ismapped() and self.more_btn.winfo_ismapped():
            button = self.more_btn
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() -
                      menu.winfo_reqheight())

    def pick_screen(self):
        monitors = get_monitors(self.root)
        current = self.settings.get("output_monitor")
        menu = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 11), bg=SURFACE_2, fg=TEXT, activebackground=CYAN_DIM, activeforeground=CYAN, bd=0, relief="flat")
        for i, m in enumerate(monitors):
            label = (f"Screen {i + 1}: {m['w']}\u00d7{m['h']}"
                     f"{'  (main)' if m['primary'] else ''}")
            if current == i:
                label = "\u2714 " + label
            menu.add_command(label=label,
                             command=lambda i=i, m=m: self._use_screen(i, m))
        menu.add_separator()
        menu.add_command(label="Layout, sizes and colours\u2026",
                         command=self.open_output_layout)
        menu.add_command(label="Refresh screens", command=self.pick_screen)
        self._popup(menu, self.screen_btn)

    def _use_screen(self, index, monitor):
        self.settings["output_monitor"] = index
        save_settings(self.settings)
        if self.output is None:
            self.open_output()
        else:
            self.output.move_to(monitor)

    # ------------------------------------------------------ control API --
    def _process_api(self):
        api = getattr(self, "api", None)
        if api is None:
            return
        while True:
            try:
                cmd = api.queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._run_command(cmd)
            except Exception:
                pass

    def _run_command(self, cmd):
        actions = {
            "all_start": self.start_all,
            "all_stop": self.stop_all,
            "all_toggle": self.toggle_running,
            "seg_start": self.start_segment,
            "seg_stop": self.stop_segment,
            "seg_toggle": lambda: (self.stop_segment() if self.segment.running
                                   else self.start_segment()),
            "prog_start": self.start_show,
            "prog_stop": self.stop_show,
            "prog_toggle": lambda: (self.stop_show() if self.show.running
                                    else self.start_show()),
            "next_segment": self.next_segment,
            "mark": self.add_mark,
            # The Stream Deck key must be held to send this, so skip the
            # "are you sure" box (the unexported-log prompt still appears)
            "reset_program": lambda: self.reset_all(confirm=False),
            "dual_toggle": self.toggle_dual,
            "tod_toggle": self.toggle_tod,
            "log_toggle": self.toggle_log_panel,
            "segments_toggle": self.toggle_seg_panel,
            "undo": self.undo,
        }
        fn = actions.get(cmd)
        if fn:
            fn()

    def _api_snapshot(self):
        show = self.current_show
        n = len(self.segment_times)
        seg_el, prog_el = self.segment.elapsed(), self.show.elapsed()
        goal = self._goal(n)
        seg_state, seg_status, seg_delta = "none", "", None
        if goal:
            seg_delta = int(seg_el) - goal
            seg_state = ("over" if seg_delta > 0 else
                         "warn" if -seg_delta <= 15 else "ok")
            seg_status = self._out_status[0]
        plan_total = sum(p.get("goal") or 0 for p in self._plan())
        delta = self._total_delta()
        if delta is None:
            prog_state, prog_status = "none", ""
        elif delta > 0:
            prog_state, prog_status = "over", f"+{fmt_short(delta)} OVER"
        elif delta < 0:
            prog_state, prog_status = "under", f"{fmt_short(-delta)} UNDER"
        else:
            prog_state, prog_status = "ontime", "ON TIME"
        return {
            "app": APP_NAME, "version": VERSION, "show": show["name"],
            "tod": self.clock.now().strftime("%H:%M:%S"),
            "time_source": self.clock.mode,
            "backtime": self._backtime_api(),
            "segment": {
                "label": show["segment_label"], "number": n,
                "name": self._seg_name(n), "next_name": self._seg_name(n + 1),
                "seconds": int(seg_el), "time": fmt(seg_el),
                "running": self.segment.running,
                "goal": goal, "delta": seg_delta, "state": seg_state,
                "status": seg_status,
                "total": self._seg_total() or None,
                "left": (self._seg_progress() or (0, 0, None))[2],
            },
            "program": {
                "label": show["show_label"], "seconds": int(prog_el),
                "time": fmt(prog_el), "running": self.show.running,
                "plan": plan_total or None, "delta": delta,
                "state": prog_state, "status": prog_status,
            },
            "undo": self._undo_stack[-1]["label"] if self._undo_stack
            else None,
            "toggles": {"dual": self.output is not None, "tod": self.tod_on,
                        "log": self.log_on, "segments": self.seg_panel_on},
        }

    # -------------------------------------------------------------- fonts --
    def _fonts_custom(self):
        return bool(FONT_CHOICE.get("text") or FONT_CHOICE.get("clock"))

    def _fonts_on_map(self, e):
        if self._fonts_custom() or getattr(self, "_fonts_were_custom", False):
            remap_fonts(e.widget)

    def set_fonts(self, text=None, clock=None, clock_bold=None):
        if text is not None:
            FONT_CHOICE["text"] = text
        if clock is not None:
            FONT_CHOICE["clock"] = clock
        if clock_bold is not None:
            FONT_CHOICE["clock_bold"] = bool(clock_bold)
        self._fonts_were_custom = True     # keep remapping back to default
        self.settings["fonts"] = dict(FONT_CHOICE)
        save_settings(self.settings)
        self._style_ttk()
        remap_fonts(self.root)
        self._layout(force=True)

    def open_fonts(self):
        FontDialog(self.root, self)

    # ----------------------------------------------------------- settings --
    def feat(self, key):
        return bool(self.settings.get("features", {}).get(
            key, FEATURE_DEFAULTS.get(key, True)))

    def open_settings(self):
        SettingsDialog(self.root, self)

    def set_feature(self, key, value):
        self.settings.setdefault("features", {})[key] = bool(value)
        if key == "auto_log":
            self.settings["auto_log"] = bool(value)
            self.autolog_var.set(bool(value))
        if key == "dual_on_start":
            self.settings["dual_on_start"] = bool(value)
        save_settings(self.settings)
        # side effects
        if key == "dual" and not value:
            self.close_output()
        elif key == "deck":
            self.set_deck_enabled(bool(value))
        elif key == "sync":
            self.clock.configure(self.settings if value else
                                 dict(self.settings, time_source="pc"))
        elif key == "fullscreen":
            self.set_fullscreen(bool(value))
        elif key == "autosave":
            self._save_show_state()
        elif key in ("undo", "undo_popups") and not value:
            self._hide_toast()
        self._seg_title_cache = None
        self._update_overunder()
        self._update_backtime()
        self._deck_status_cache = None
        self._layout(force=True)

    # --------------------------------------------------------------- undo --
    UNDO_DEPTH = 15

    def _state_sig(self):
        return (self.show.base, self.show.started_at, self.segment.base,
                self.segment.started_at, tuple(self.segment_times),
                len(self.log.entries))

    def _undoable(self, label, fn, *args, **kwargs):
        """Run an action; if it changed anything, remember how to undo it."""
        if self._in_action:                    # nested call inside an action
            return fn(*args, **kwargs)
        snap = {
            "label": label,
            "show": (self.show.base, self.show.started_at),
            "seg": (self.segment.base, self.segment.started_at),
            "segment_times": list(self.segment_times),
            "log": [dict(e) for e in self.log.entries],
            "log_segments": json.loads(json.dumps(self.log.segments)),
            "log_dirty": self.log.dirty,
        }
        before = self._state_sig()
        self._in_action = True
        try:
            result = fn(*args, **kwargs)
        finally:
            self._in_action = False
        if self._state_sig() != before:
            snap["after_count"] = len(self.log.entries)
            self._undo_stack.append(snap)
            del self._undo_stack[:-self.UNDO_DEPTH]
            self._update_undo_button()
            if label == "Program reset":
                self._show_toast("Program reset – timers and segments "
                                 "cleared.", seconds=25)
            elif label == "Next segment":
                self._show_toast(f"Now on segment {len(self.segment_times)}.",
                                 seconds=8)
            elif label == "Clear log":
                self._show_toast("Log cleared.", seconds=15)
        return result

    def undo(self):
        if not self._undo_stack:
            self._show_toast("Nothing to undo.", undo=False)
            return
        snap = self._undo_stack.pop()
        # entries logged after the action are kept
        later = self.log.entries[snap["after_count"]:] \
            if snap["after_count"] <= len(self.log.entries) else []
        # monotonic start times make the clocks carry on as if the action
        # never happened, including the time since
        self.show.base, self.show.started_at = snap["show"]
        self.segment.base, self.segment.started_at = snap["seg"]
        self.segment_times = list(snap["segment_times"])
        self.log.entries = snap["log"] + later
        self.log.segments = snap["log_segments"]
        self.log.dirty = True
        self.log.save_backup()
        self._rebuild_seg_list()
        self._rebuild_log_tree()
        self._update_visuals()
        self._add_entry("UNDO", f"Undid: {snap['label']}")
        self._update_undo_button()
        self._show_toast(f"Undid {snap['label'].lower()}.", undo=False)
        self._save_show_state()

    def _update_undo_button(self):
        has = bool(self._undo_stack)
        self.undo_btn.set_enabled(has)

    def _show_toast(self, text, undo=True, seconds=None):
        if not hasattr(self, "toast_bg"):
            return
        if undo and not (self.feat("undo") and self.feat("undo_popups")):
            return
        W, H = self.root.winfo_width(), self.root.winfo_height()
        if W < 100:
            return
        self.toast_lbl.config(text=text)
        bw, bh = self.toast_btn.fixed
        cw, ch = self.toast_close.fixed
        tw = tkfont.Font(font=self.toast_lbl.cget("font")).measure(text)
        w = int(tw + px(48) + (bw + px(12) if undo else 0) + cw + px(12))
        h = px(60)
        x = int((W - w) / 2)
        y = int(H - px(56) - h - px(18))
        self.toast_bg.place(x=x, y=y, width=w, height=h)
        self.toast_lbl.place(x=x + px(22), y=y + px(12), width=tw + px(8),
                            height=h - px(24))
        bx = x + px(22) + tw + px(18)
        if undo:
            self.toast_btn.place(x=bx, y=y + (h - bh) // 2, width=bw, height=bh)
            bx += bw + px(8)
        else:
            self.toast_btn.place_forget()
        self.toast_close.place(x=bx, y=y + (h - ch) // 2, width=cw, height=ch)
        for wdg in (self.toast_bg, self.toast_lbl, self.toast_btn,
                    self.toast_close):
            wdg.lift()
        if self._toast_job:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(
            int((seconds or (15 if undo else 4)) * 1000), self._hide_toast)

    def _hide_toast(self):
        for wdg in (self.toast_bg, self.toast_lbl, self.toast_btn,
                    self.toast_close):
            wdg.place_forget()
        self._toast_job = None

    # ------------------------------------------------- crash-safe show state --
    def _state_file(self):
        return os.path.join(data_dir(), "show_state.json")

    def _save_show_state(self):
        if not self.feat("autosave"):
            try:
                os.remove(self._state_file())
            except OSError:
                pass
            return
        active = self.show.elapsed() >= 1 or self.segment.elapsed() >= 1 or \
            len(self.segment_times) > 1
        try:
            if not active:
                if os.path.exists(self._state_file()):
                    os.remove(self._state_file())
                return
            data = {"saved_wall": time.time(),
                    "show_name": self.current_show["name"],
                    "show": {"elapsed": self.show.elapsed(),
                             "running": self.show.running},
                    "seg": {"elapsed": self.segment.elapsed(),
                            "running": self.segment.running},
                    "segment_times": self.segment_times}
            tmp = self._state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_file())
        except Exception:
            pass

    def _offer_restore(self):
        """After a crash / accidental exit: offer the timers, then the log."""
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        if data and not self.running and self.show.elapsed() < 1:
            gap = max(0.0, time.time() - data["saved_wall"])
            sh, sg = data["show"], data["seg"]
            prog_now = sh["elapsed"] + (gap if sh["running"] else 0)
            n = len(data.get("segment_times") or [0])
            state = "running" if sh["running"] or sg["running"] else "stopped"
            msg = (f"DA Timer closed during a show.\n\n"
                   f"Program {fmt(sh['elapsed'])}, segment {n}, {state}"
                   + (f" \u2013 {fmt(gap)} ago" if gap >= 1 else "") + ".\n\n"
                   f"Restore the timers?" + (
                       f"\n(Running clocks will include the time since, so "
                       f"the program will be at {fmt(prog_now)}.)"
                       if state == "running" else ""))
            if messagebox.askyesno(APP_NAME, msg, parent=self.root):
                for sw, d in ((self.show, sh), (self.segment, sg)):
                    sw.base = d["elapsed"] + (gap if d["running"] else 0)
                    sw.started_at = time.monotonic() if d["running"] else None
                self.segment_times = list(data.get("segment_times") or [0.0])
                self.segment_times[-1] = self.segment.elapsed()
                self._rebuild_seg_list()
                self._update_visuals()
                self.root.after(50, lambda: self._add_entry(
                    "RESTORE", f"Timers restored after DA Timer closed "
                               f"({fmt(gap)} gap)"))
            else:
                try:
                    os.remove(self._state_file())
                except OSError:
                    pass
        self._offer_log_restore()

    # ---------------------------------------------------------- back-time --
    def _backtime_on(self):
        # Same test as _backtime(): the band shows exactly when there is
        # something to show (a mismatch made the screen re-lay out non-stop)
        return self._backtime() is not None

    def _backtime(self):
        """Live back-timing against the show's hard out (or None)."""
        if not self.feat("backtime"):
            return None
        ho = self.current_show.get("hard_out") or {}
        mode = ho.get("mode")
        if mode not in ("tod", "trt"):
            return None
        plan = self._plan()
        n = len(self.segment_times)
        cur = self.segment.elapsed()
        goals = [p.get("goal") or 0 for p in plan]
        cur_goal = goals[n - 1] if n <= len(goals) else 0
        after = sum(goals[n:])                 # segments still to come
        remaining = max(cur_goal - cur, 0) + after
        missing = sum(1 for g in goals[n - 1:] if not g)
        now = self.clock.now()
        start_by = [None] * len(goals)
        if mode == "tod":
            sod = parse_tod(ho.get("tod", ""))
            if sod is None:
                return None
            out = now.replace(hour=0, minute=0, second=0, microsecond=0) + \
                timedelta(seconds=sod)
            if (out - now).total_seconds() < -43200:
                out += timedelta(days=1)
            elif (out - now).total_seconds() > 43200:
                out -= timedelta(days=1)
            to_out = (out - now).total_seconds()
            out_txt = out.strftime("%H:%M:%S")
            proj_txt = (now + timedelta(seconds=remaining)).strftime("%H:%M:%S")
            proj_cap = "PROJECTED OUT"
            for i in range(n, len(goals)):
                t = out - timedelta(seconds=sum(goals[i:]))
                start_by[i] = t.strftime("%H:%M:%S")
        else:
            trt = ho.get("trt") or 0
            if not trt:
                return None
            prog = self.show.elapsed()
            to_out = trt - prog
            out_txt = fmt(trt)
            proj_txt = fmt(prog + remaining)
            proj_cap = "PROJECTED LENGTH"
            for i in range(n, len(goals)):
                start_by[i] = fmt(max(trt - sum(goals[i:]), 0))
        slack = to_out - remaining           # + spare time, - must cut
        if to_out < 0:
            state, verdict = "past", f"PAST OUT +{fmt_short(-to_out)}"
        elif abs(slack) < 5:
            state, verdict = "ontime", "ON TIME"
        elif slack < 0:
            state, verdict = "cut", f"CUT {fmt_short(-slack)}"
        else:
            state, verdict = "spare", f"SPARE {fmt_short(slack)}"
        return {"mode": mode, "out": out_txt, "to_out": to_out,
                "remaining": remaining, "projected": proj_txt,
                "proj_caption": proj_cap, "slack": slack, "state": state,
                "verdict": verdict, "start_by": start_by,
                "missing_goals": missing}

    def _update_backtime(self):
        bt = self._bt = self._backtime()
        # Compare with what the last layout did, not winfo_ismapped(): a
        # widget only reports mapped once Tk has drawn it, so asking right
        # after a layout re-ran the layout over and over
        if (bt is not None) != (self._last_layout or (None,) * 7)[6]:
            self._layout(force=True)
        if bt is None:
            return
        col = {"past": RED, "cut": RED, "spare": GREEN,
               "ontime": CYAN}[bt["state"]]
        dim = {"past": RED_DIM, "cut": RED_DIM, "spare": GREEN_DIM,
               "ontime": CYAN_DIM}[bt["state"]]
        to = bt["to_out"]
        parts = {
            "out": ("Hard out" if bt["mode"] == "tod" else "Program length",
                    bt["out"], TEXT),
            "to_out": ("Time to out", fmt(max(to, 0)) if to >= 0
                       else "+" + fmt(-to), RED if to < 0 else TEXT),
            "proj": ("Projected off air" if bt["mode"] == "tod"
                     else "Projected length", bt["projected"], col),
        }
        for key, (cap, val, fg) in parts.items():
            capl, vall = self.bt_parts[key]
            self._cfg(capl, text=cap)
            self._cfg(vall, text=val, fg=fg)
        slack = abs(bt["slack"])
        pill = {"past": f"Past out +{fmt_short(-to)}",
                "cut": f"Cut {fmt_short(slack)}",
                "spare": f"Spare {fmt_short(slack)}",
                "ontime": "On time"}[bt["state"]]
        self.bt_pill.set(pill, dim, col, dot=False)
        anchor = getattr(self, "_bt_pill_anchor", None)
        if anchor:
            self.bt_pill.place(x=int(anchor[0] - self.bt_pill.width_px),
                               y=int(anchor[1] - self.bt_pill.h / 2),
                               width=self.bt_pill.width_px,
                               height=self.bt_pill.h)
        self._cfg(self.bt_note, text=(
            f"{bt['missing_goals']} upcoming segment(s) have no goal"
            if bt["missing_goals"] else ""))
        self.bt_panel.set_border(col if bt["state"] in ("cut", "past")
                                 else BORDER)
        # keep the running order's START BY column fresh
        now = time.monotonic()
        if self.seg_panel_on and now >= self._due.get("startby", 0):
            self._due["startby"] = now + 1.0
            for i in range(len(self.segment_times) + 1,
                           len(self._plan()) + 1):
                if self.seg_list.exists(str(i)):
                    self.seg_list.item(str(i), values=self._seg_row(i))

    def _backtime_api(self):
        bt = getattr(self, "_bt", None)
        if not bt:
            return None
        return {k: bt[k] for k in ("mode", "out", "to_out", "remaining",
                                   "projected", "slack", "state", "verdict")}

    def open_sync_setup(self):
        SyncSetup(self.root, self)

    # ---------------------------------------------------- direct Stream Deck --
    def _start_deck(self):
        try:
            import da_deck
        except Exception:
            self.deck = None
            return
        self.deck = da_deck.DeckController(
            get_state=lambda: self.api.state,
            send_command=self.api.queue.put,
            get_layout=self._deck_layout,
            get_brightness=lambda: int(self.settings.get("deck_brightness", 70)))

    def _deck_layout(self, _deck_type, key_count):
        import da_deck
        saved = self.settings.get("deck_layouts", {}).get(str(key_count))
        if isinstance(saved, list) and len(saved) == key_count:
            return saved
        return da_deck.default_layout(key_count)

    def set_deck_enabled(self, on):
        self.settings["deck_enabled"] = on
        self.settings.setdefault("features", {})["deck"] = bool(on)
        save_settings(self.settings)
        if on and self.deck is None:
            self._start_deck()
        elif self.deck is not None:
            self.deck.set_enabled(on)

    def open_deck_setup(self):
        try:
            DeckSetup(self.root, self)
        except ImportError:
            messagebox.showerror(APP_NAME, "da_deck.py is missing - keep it "
                                 "next to da_timer.py.", parent=self.root)

    def _update_deck_status(self):
        api = self.api
        deck = self.deck
        if deck is not None and deck.connected:
            d = ("Stream Deck", GREEN_DIM, GREEN)
        elif deck is not None and deck.elgato_running and \
                time.monotonic() - api.last_seen >= 3:
            d = ("Elgato app running", AMBER_DIM, AMBER)
        elif api.error:
            d = ("Deck API off", RED_DIM, "#ff8592")
        elif time.monotonic() - api.last_seen < 3:
            d = ("Stream Deck plugin", GREEN_DIM, GREEN)
        else:
            d = ("No Stream Deck", SURFACE_2, MUTED)
        clk = self.clock
        if clk.mode == "pc":
            t = ("PC clock", SURFACE_2, MUTED)
        else:
            name = "LTC" if clk.mode == "ltc" else "NTP"
            t = {"ok": (f"{name} locked" if clk.mode == "ltc"
                        else f"{name} synced", GREEN_DIM, GREEN),
                 "warn": (f"{name} freewheel" if clk.mode == "ltc"
                          else f"{name} checking", AMBER_DIM, AMBER),
                 "error": (f"{name} lost", RED_DIM, "#ff8592")}[clk.state]
        key = (d, t)
        if getattr(self, "_deck_status_cache", None) != key:
            self._deck_status_cache = key
            self.deck_pill.set(*d)
            self.sync_pill.set(*t)
            self._place_status()

    # ------------------------------------------------------------- shows --
    def apply_show(self):
        show = self.current_show
        self._cfg(self.show_cap_lbl, text=show["show_label"])
        self._seg_title_cache = None
        self._rebuild_seg_list()
        self.top_logo.set_image(logo_path(show["logo_left"]))
        self.show_name_lbl.config(text=show["name"])
        self._deck_status_cache = None
        self.root.title(f"{APP_NAME} v{VERSION} \u2014 {show['name']}")
        if self.output is not None:
            self.output.apply_show(show)
        self._layout(force=True)

    def use_show(self, show):
        self.current_show = copy_show(show)
        self.settings["current_show"] = show["name"]
        save_settings(self.settings)
        self.apply_show()

    def _set_company_logo(self):
        self.company_logo.set_image(logo_path(self.settings.get("company_logo", "")))

    def pick_company_logo(self):
        path = filedialog.askopenfilename(parent=self.root,
                                          title="Choose company logo",
                                          filetypes=LOGO_TYPES)
        if path:
            try:
                self.settings["company_logo"] = import_logo(path)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Couldn't import:\n{exc}",
                                     parent=self.root)
                return
            save_settings(self.settings)
            self._set_company_logo()

    def remove_company_logo(self):
        self.settings["company_logo"] = ""
        save_settings(self.settings)
        self._set_company_logo()

    def show_menu(self):
        menu = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 11), bg=SURFACE_2, fg=TEXT, activebackground=CYAN_DIM, activeforeground=CYAN, bd=0, relief="flat")
        menu.add_command(label="New show\u2026", command=self.new_show)
        menu.add_command(label=f"Edit '{self.current_show['name']}'\u2026",
                         command=self.edit_show)
        shows = list_shows()
        if shows:
            menu.add_separator()
            for name in shows:
                tick = "\u2714 " if name == self.current_show["name"] else "     "
                menu.add_command(label=tick + name,
                                 command=lambda n=name: self.use_show(load_show(n)))
        menu.add_separator()
        menu.add_command(label="Duplicate this show\u2026",
                         command=self.duplicate_show)
        if self.current_show["name"] in shows:
            menu.add_command(label=f"Delete '{self.current_show['name']}'",
                             command=self.delete_current_show)
        menu.add_separator()
        menu.add_command(label="Set company logo (bottom centre)\u2026",
                         command=self.pick_company_logo)
        if self.settings.get("company_logo"):
            menu.add_command(label="Remove company logo",
                             command=self.remove_company_logo)
        self._popup(menu, self.show_btn)

    def new_show(self):
        ShowEditor(self.root, dict(DEFAULT_SHOW, name=""), self.use_show,
                   is_new=True)

    def edit_show(self):
        ShowEditor(self.root, self.current_show, self.use_show)

    def duplicate_show(self):
        name = simpledialog.askstring(
            APP_NAME, "Name for the copy:", parent=self.root,
            initialvalue=f"{self.current_show['name']} copy")
        if name and name.strip():
            if name.strip() in list_shows() and not messagebox.askyesno(
                    APP_NAME, f"Replace the existing show '{name.strip()}'?",
                    parent=self.root):
                return
            show = dict(self.current_show, name=name.strip())
            save_show(show)
            self.use_show(show)

    def delete_current_show(self):
        name = self.current_show["name"]
        if messagebox.askyesno(APP_NAME, f"Delete the show '{name}'?",
                               parent=self.root):
            delete_show(name)
            self.use_show(dict(DEFAULT_SHOW))

    # ----------------------------------------------------------- logging --
    def _snapshot(self):
        return {"tod": self.clock.now().isoformat(timespec="seconds"),
                "program": self.show.elapsed(),
                "seg_no": len(self.segment_times),
                "seg_time": self.segment.elapsed()}

    def _add_entry(self, kind, note, snap=None):
        snap = snap or self._snapshot()
        entry = self.log.add(kind, note, snap["program"], snap["seg_no"],
                             snap["seg_time"], tod=snap["tod"])
        self._insert_log_row(len(self.log.entries), entry)
        self._update_log_count()

    def _log_event(self, kind, note):
        if self.feat("log") and self.feat("auto_log"):
            self._add_entry(kind, note)

    def _note_key(self, event):
        # The note is stamped when you start typing, not when you hit Enter
        if self._note_stamp is None and event.char and event.char.isprintable():
            self._note_stamp = self._snapshot()
            self.stamp_lbl.config(
                fg=CYAN,
                text=f"Stamped  TOD {self._note_stamp['tod'][11:]}   \u00b7   "
                     f"Program {fmt(self._note_stamp['program'])}")

    NOTE_HINT = ("Typing stamps the time  \u00b7  Enter logs  \u00b7  "
                 "double-click an entry to edit")

    def _note_hint(self):
        self.stamp_lbl.config(text=self.NOTE_HINT, fg=FAINT)

    def _note_changed(self):
        # Clearing the box drops the old stamp, so the next note gets a
        # fresh time instead of the one from when you first started typing
        if not self.note_var.get().strip() and self._note_stamp is not None:
            self._note_stamp = None
            self._note_hint()

    def _cancel_note(self):
        self.note_var.set("")
        self._note_stamp = None
        self._note_hint()
        self.root.focus_set()

    def add_note(self):
        text = self.note_var.get().strip()
        if not text:
            return
        self._add_entry("NOTE", text, self._note_stamp)
        self.note_var.set("")
        self._note_stamp = None
        self._note_hint()

    def add_mark(self):
        self._add_entry("MARK", self.note_var.get().strip() if
                        self._note_stamp is None else "")
        if self._note_stamp is None:
            self.note_var.set("")
        return "break"

    def focus_note(self):
        if not self.log_on:
            self.toggle_log_panel()
        self.note_entry.focus_set()

    def _insert_log_row(self, n, e):
        tag = (("odd",) if n % 2 else ()) + (
            ("mark",) if e["type"] == "MARK" else
            () if e["type"] == "NOTE" else ("event",))
        self.log_tree.insert("", "end", iid=str(n), tags=tag, values=(
            e["tod"][11:19], fmt(e["program"]), e["seg_no"], e["type"],
            e["note"]))
        self.log_tree.see(str(n))

    def _rebuild_log_tree(self):
        self.log_tree.delete(*self.log_tree.get_children())
        for n, e in enumerate(self.log.entries, start=1):
            self._insert_log_row(n, e)
        self._update_log_count()

    def _update_log_count(self):
        n = len(self.log.entries)
        self.log_count_lbl.config(text=f"{n} entr{'y' if n == 1 else 'ies'}")

    def edit_log_entry(self, _event=None):
        sel = self.log_tree.selection()
        if not sel:
            return
        e = self.log.entries[int(sel[0]) - 1]
        text = simpledialog.askstring(
            APP_NAME, f"Edit note ({e['tod'][11:19]}  \u00b7  "
                      f"Program {fmt(e['program'])}):",
            initialvalue=e["note"], parent=self.root)
        if text is not None:
            e["note"] = text.strip()
            self.log.dirty = True
            self.log.save_backup()
            self._rebuild_log_tree()

    def delete_log_entry(self, _event=None):
        sel = self.log_tree.selection()
        if not sel:
            return
        e = self.log.entries[int(sel[0]) - 1]
        if messagebox.askyesno(APP_NAME, f"Remove this log entry?\n\n"
                               f"{e['tod'][11:19]}  {e['type']}  {e['note']}",
                               parent=self.root):
            del self.log.entries[int(sel[0]) - 1]
            self.log.dirty = True
            self.log.save_backup()
            self._rebuild_log_tree()

    def clear_log(self):
        if self.log.entries and messagebox.askyesno(
                APP_NAME, "Clear every entry from the log?", parent=self.root):
            if self._log_handled("clearing"):
                self.log.clear()
                self._rebuild_log_tree()

    def _save_autolog(self):
        self.settings["auto_log"] = bool(self.autolog_var.get())
        self.settings.setdefault("features", {})["auto_log"] = \
            self.settings["auto_log"]
        save_settings(self.settings)

    def toggle_log_panel(self):
        self.log_on = not self.log_on
        self.settings["log_panel"] = self.log_on
        save_settings(self.settings)
        self._toggle_look(self.log_btn, self.log_on)
        self._layout(force=True)
        if not self.log_on:
            self.root.focus_set()

    def toggle_tod(self):
        self.tod_on = not self.tod_on
        self.settings["tod"] = self.tod_on
        save_settings(self.settings)
        self._toggle_look(self.tod_btn, self.tod_on)
        self._layout(force=True)

    def _offer_log_restore(self):
        old, self._pending_log = self._pending_log, None
        if old.entries and old.dirty:
            n = len(old.entries)
            if messagebox.askyesno(
                    APP_NAME, f"There's an unexported log from last time "
                              f"({n} entries). Restore it?", parent=self.root):
                # Old entries first, then anything logged since startup
                self.log.entries = old.entries + self.log.entries
                old.segments.update(self.log.segments)
                self.log.segments = old.segments
                self.log.dirty = True
                self._rebuild_log_tree()
                if not self.log_on:
                    self.toggle_log_panel()
        self.log.save_backup()

    def _log_handled(self, action):
        """Ask to export unexported log entries. False = cancel the action."""
        if not self.feat("log") or not (self.log.entries and self.log.dirty):
            return True
        answer = messagebox.askyesnocancel(
            APP_NAME, f"The log has entries that haven't been exported.\n\n"
                      f"Export to Excel before {action}?", parent=self.root)
        if answer is None:
            return False
        if answer:
            return self.export_log()
        return True

    def export_log(self):
        if not self.log.entries:
            messagebox.showinfo(APP_NAME, "The log is empty.", parent=self.root)
            return False
        try:
            import openpyxl  # noqa: F401
            ext, types = ".xlsx", [("Excel workbook", "*.xlsx")]
        except ImportError:
            ext, types = ".csv", [("CSV file", "*.csv")]
        folder = os.path.join(os.path.expanduser("~"), "Documents",
                              "DA Timer Logs")
        os.makedirs(folder, exist_ok=True)
        first = datetime.fromisoformat(self.log.entries[0]["tod"])
        name = f"{_slug(self.current_show['name'])} {first:%Y-%m-%d %H%M}{ext}"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export log", initialdir=folder,
            initialfile=name, defaultextension=ext, filetypes=types)
        if not path:
            return False
        try:
            if path.lower().endswith(".xlsx"):
                export_log_xlsx(path, self.current_show["name"], self.log,
                                self.segment_times, self._plan(),
                                self._backtime())
            else:
                export_log_csv(path, self.current_show["name"], self.log)
        except PermissionError:
            messagebox.showerror(APP_NAME, "Couldn't save - is that file open "
                                 "in Excel? Close it and try again.",
                                 parent=self.root)
            return False
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export failed:\n{exc}",
                                 parent=self.root)
            return False
        self.log.dirty = False
        self.log.save_backup()
        if ext == ".csv":
            messagebox.showinfo(APP_NAME, "Saved as CSV. Install openpyxl "
                                "(pip install openpyxl) for Excel export.",
                                parent=self.root)
        elif sys.platform in ("win32", "darwin") and messagebox.askyesno(
                APP_NAME, "Log exported. Open it now?", parent=self.root):
            try:
                if IS_MAC:
                    import subprocess
                    subprocess.Popen(["open", path])
                else:
                    os.startfile(path)
            except OSError:
                pass
        return True

    # -------------------------------------------------------------- quit --
    def quit(self):
        if self.running and not messagebox.askyesno(
                APP_NAME, "The timers are running. Quit anyway?",
                parent=self.root):
            return
        if not self._log_handled("exiting"):
            return
        if not self.running:
            try:
                os.remove(self._state_file())
            except OSError:
                pass
        else:
            self._save_show_state()
        self.api.close()
        self.clock.stop()
        if self.deck is not None:
            self.deck.stop()
        self.root.destroy()


# Actions that can be undone (wrapped once, here, so buttons, keys, the
# Stream Deck and the control API all get undo for free)
def _make_undoable(name, label):
    orig = getattr(DATimerApp, name)

    def wrapper(self, *args, **kwargs):
        return self._undoable(label, orig, self, *args, **kwargs)
    wrapper.__name__ = name
    setattr(DATimerApp, name, wrapper)


for _name, _label in (("start_all", "All start"), ("stop_all", "All stop"),
                      ("start_segment", "Segment start"),
                      ("stop_segment", "Segment stop"),
                      ("start_show", "Program start"),
                      ("stop_show", "Program stop"),
                      ("next_segment", "Next segment"),
                      ("reset_all", "Program reset"),
                      ("clear_log", "Clear log"),
                      ("delete_log_entry", "Delete log entry")):
    _make_undoable(_name, _label)


def main():
    enable_dpi_awareness()
    if sys.platform == "win32":
        # Windows wakes timers every 15.6 ms by default, which makes the
        # clocks change unevenly; ask for 1 ms while DA Timer is open
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
    root = tk.Tk()
    init_fonts(root)
    choice = load_settings().get("ui_scale", "auto")
    set_ui_scale(root, choice)
    app = DATimerApp(root)
    app.settings["ui_scale_applied"] = choice
    root.mainloop()


if __name__ == "__main__":
    main()
