"""
DA Timer - direct Stream Deck support (no Elgato software needed).

DA Timer talks to the Stream Deck over USB itself (via hidapi + the open
source `streamdeck` library, both shipped alongside), draws every key and
reads the key presses. Supports Stream Deck Original/MK.2, Mini, XL, Neo,
Plus and Plus XL (keys only).

Note: quit the Elgato Stream Deck app while using this, otherwise both apps
fight over the keys.
"""
import os
import subprocess
import sys
import threading
import time

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Pillow is required for the deck
    Image = ImageDraw = ImageFont = None

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = getattr(sys, "_MEIPASS", HERE)
HOLD_SECONDS = 1.0
RENDER_INTERVAL = 0.12
RESCAN_INTERVAL = 2.0

# ------------------------------------------------------------------ catalogue
ACTIONS = {
    "none":       "Empty",
    "clock":      "Live Clock",
    "alltoggle":  "All Start / Stop",
    "allstart":   "All Start",
    "allstop":    "All Stop",
    "segtoggle":  "Segment Start / Stop",
    "progtoggle": "Program Start / Stop",
    "next":       "Next Segment",
    "mark":       "Log Mark",
    "reset":      "Reset Program (hold 1s)",
    "undo":       "Undo Last Action",
    "display":    "Display Toggle",
}
OPTIONS = {
    "clock": {
        "source": [("segment", "Segment time"), ("program", "Program time"),
                   ("seg_delta", "Segment over / under"),
                   ("prog_delta", "Total over / under"),
                   ("to_out", "Time to hard out"),
                   ("backtime", "Back-time: cut / spare"),
                   ("tod", "Time of day (TOD)")],
        "press": [("toggle", "Start / stop that timer"),
                  ("next", "Next segment"), ("mark", "Log a MARK"),
                  ("none", "Nothing (display only)")],
    },
    "display": {
        "target": [("dual", "Dual screen output"), ("tod", "TOD clock"),
                   ("log", "Log panel"), ("segments", "Segments panel")],
    },
}


def K(action, **opts):
    return dict(action=action, **opts)


_DEFAULT_15 = [
    K("clock", source="segment"), K("clock", source="program"),
    K("clock", source="seg_delta", press="none"),
    K("clock", source="prog_delta", press="none"),
    K("clock", source="tod", press="none"),
    K("allstart"), K("allstop"), K("segtoggle"), K("progtoggle"), K("next"),
    K("mark"), K("display", target="dual"), K("display", target="segments"),
    K("display", target="log"), K("reset"),
]
_DEFAULTS = {
    6: [K("clock", source="segment"), K("clock", source="program"),
        K("next"), K("alltoggle"), K("mark"), K("reset")],
    8: [K("clock", source="segment"), K("clock", source="program"),
        K("clock", source="prog_delta", press="none"), K("next"),
        K("alltoggle"), K("segtoggle"), K("mark"), K("reset")],
    15: _DEFAULT_15,
    32: [K("clock", source="segment"), K("clock", source="program"),
         K("clock", source="seg_delta", press="none"),
         K("clock", source="prog_delta", press="none"),
         K("clock", source="tod", press="none"), K("display", target="dual"),
         K("display", target="tod"), K("display", target="log"),
         K("allstart"), K("allstop"), K("segtoggle"), K("progtoggle"),
         K("next"), K("mark"), K("display", target="segments"), K("reset")],
}


def default_layout(key_count):
    base = _DEFAULTS.get(key_count) or _DEFAULT_15
    layout = [dict(k) for k in base[:key_count]]
    while len(layout) < key_count:
        layout.append(K("none"))
    return layout


# ------------------------------------------------------------------ drawing
C = {
    "bg": "#0a0d11", "text": "#eef3f6", "muted": "#8592a3", "faint": "#4a5566",
    "cyan": "#00e5f0", "green": "#22d37f", "red": "#ff4d5e", "amber": "#ffb547",
    "greenBg": "#0d3a24", "redBg": "#3a1419", "cyanBg": "#073a40",
    "dim": "#b9c2cc",
}
_FONT_CACHE = {}


def _font_file(clock=False):
    fonts = os.path.join(BASE, "fonts")
    for n in (("DAClock.ttf",) if clock else ()) + ("DASans-Bold.ttf",):
        p = os.path.join(fonts, n)
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


def _font(px, clock=False):
    key = (px, clock)
    if key not in _FONT_CACHE:
        path = _font_file(clock)
        try:
            _FONT_CACHE[key] = (ImageFont.truetype(path, px) if path
                                else ImageFont.load_default(size=px))
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def short(sec):
    sec = abs(int(round(sec or 0)))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def signed(sec):
    return ("+" if sec > 0 else "\u2212" if sec < 0 else "") + short(sec)


def _fit(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def _main_size(text):
    n = len(str(text))
    return 64 if n <= 2 else 50 if n <= 4 else 44 if n <= 5 else \
        38 if n <= 6 else 33 if n <= 7 else 29


def _state_color(st):
    return {"over": C["red"], "warn": C["amber"], "under": C["green"],
            "ok": C["green"], "ontime": C["cyan"]}.get(st, C["muted"])


def _seg_clock(seg):
    color = (C["red"] if seg["state"] == "over" else
             C["amber"] if seg["state"] == "warn" else
             C["text"] if seg["running"] else C["dim"])
    return dict(label=f"{(seg.get('label') or 'SEG').upper()[:7]} {seg['number']}",
                main=short(seg["seconds"]), main_color=color,
                sub=seg.get("status") or ("" if seg["running"] else "STOPPED"),
                sub_color=_state_color(seg["state"]) if seg.get("status")
                else C["muted"],
                bar=C["green"] if seg["running"] else C["red"])


def _prog_clock(prog):
    return dict(label=(prog.get("label") or "PROGRAM").upper(),
                main=short(prog["seconds"]),
                main_color=C["text"] if prog["running"] else C["dim"],
                sub=prog.get("status") or ("" if prog["running"] else "STOPPED"),
                sub_color=_state_color(prog["state"]) if prog.get("status")
                else C["muted"],
                bar=C["green"] if prog["running"] else C["red"])


def design(key, state, holding=False):
    """What a key should show for the current DA Timer state."""
    action = key.get("action", "none")
    if action == "none":
        return dict(blank=True)
    if not state or "segment" not in state:
        return dict(label="DA TIMER", main="\u2026", main_color=C["faint"],
                    label_color=C["faint"])
    seg, prog = state["segment"], state["program"]
    t = state.get("toggles", {})
    both = seg["running"] and prog["running"]
    none = not seg["running"] and not prog["running"]
    if action == "clock":
        src = key.get("source", "segment")
        if src == "program":
            return _prog_clock(prog)
        if src == "tod":
            tag = {"ntp": "TOD \u00b7 NTP", "ltc": "TOD \u00b7 LTC"}.get(
                state.get("time_source"), "TOD")
            return dict(label=tag, main=state.get("tod", ""), size=31)
        if src in ("to_out", "backtime"):
            bt = state.get("backtime")
            if not bt:
                return dict(label="HARD OUT", main="\u2014", sub="NOT SET")
            col = {"past": C["red"], "cut": C["red"], "spare": C["green"],
                   "ontime": C["cyan"]}.get(bt["state"], C["text"])
            if src == "to_out":
                to = bt["to_out"]
                return dict(label="TO OUT", main=short(to) if to >= 0
                            else "+" + short(to), main_color=C["red"]
                            if to < 0 else C["text"], sub=f"out {bt['out']}",
                            bar=col)
            word, _, amount = bt["verdict"].partition(" ")
            if bt["state"] == "ontime":
                return dict(label="BACK-TIME", main="ON", sub="ON TIME",
                            main_color=col, sub_color=col, bar=col, size=50)
            if bt["state"] == "past":
                return dict(label="BACK-TIME", main="+" + short(-bt["to_out"]),
                            sub="PAST OUT", main_color=col, sub_color=col,
                            bar=col)
            return dict(label="BACK-TIME", main=short(abs(bt["slack"])),
                        sub=word, main_color=col, sub_color=col, bar=col)
        if src == "seg_delta":
            if seg.get("delta") is None:
                return dict(label=f"SEG {seg['number']}", main="\u2014",
                            sub="NO GOAL")
            col = _state_color(seg["state"])
            return dict(label=f"SEG {seg['number']}", main=signed(seg["delta"]),
                        main_color=col, sub="OVER" if seg["delta"] > 0
                        else "LEFT", sub_color=col, bar=col)
        if src == "prog_delta":
            if prog.get("delta") is None:
                return dict(label="TOTAL", main="\u2014", sub="NO GOALS")
            col = _state_color(prog["state"])
            d = prog["delta"]
            return dict(label="TOTAL", main=signed(d), main_color=col,
                        sub="OVER" if d > 0 else "UNDER" if d < 0 else "ON TIME",
                        sub_color=col, bar=col)
        return _seg_clock(seg)
    if action == "alltoggle":
        return dict(label="ALL", main="STOP" if both else "START", size=40,
                    bg=C["greenBg"] if both else C["redBg"] if none else C["bg"],
                    main_color=C["green"] if both else C["red"] if none
                    else C["text"],
                    sub="RUNNING" if both else "STOPPED" if none else "MIXED",
                    sub_color=C["green"] if both else "#ff8592" if none
                    else C["amber"],
                    bar=C["green"] if both else C["red"] if none else C["amber"])
    if action == "allstart":
        return dict(label="ALL", main="START", size=40,
                    bg=C["green"] if both else C["bg"],
                    label_color="#04140b" if both else C["cyan"],
                    main_color="#04140b" if both else C["text"],
                    sub="RUNNING" if both else "", sub_color="#04140b")
    if action == "allstop":
        return dict(label="ALL", main="STOP", size=40,
                    bg=C["redBg"] if none else C["bg"],
                    main_color="#ff8592" if none else C["text"],
                    sub="STOPPED" if none else "", sub_color="#ff8592")
    if action == "segtoggle":
        return dict(_seg_clock(seg), bg=C["greenBg"] if seg["running"]
                    else C["bg"])
    if action == "progtoggle":
        return dict(_prog_clock(prog), bg=C["greenBg"] if prog["running"]
                    else C["bg"])
    if action == "next":
        nxt = seg.get("next_name") or ""
        return dict(label="NEXT SEG", main=f"{seg['number'] + 1}", size=56,
                    sub=nxt or f"after seg {seg['number']}",
                    sub_color=C["text"] if nxt else C["muted"])
    if action == "mark":
        return dict(label="LOG", main="MARK", main_color=C["amber"], size=40,
                    sub=state.get("show", ""))
    if action == "reset":
        if holding:
            return dict(label="PROGRAM", main="HOLD\u2026", main_color=C["red"],
                        bg=C["redBg"], sub="keep holding",
                        sub_color="#ff8592", size=36)
        return dict(label="PROGRAM", main="RESET", main_color="#ff8592",
                    size=38, sub="hold 1 sec", bar=C["red"])
    if action == "undo":
        last = state.get("undo")
        return dict(label="UNDO", main="UNDO", size=40,
                    main_color=C["amber"] if last else C["faint"],
                    sub=last or "nothing to undo",
                    sub_color=C["text"] if last else C["faint"])
    if action == "display":
        target = key.get("target", "dual")
        names = {"dual": "DUAL SCRN", "tod": "TOD", "log": "LOG",
                 "segments": "SEGMENTS"}
        on = bool(t.get(target))
        return dict(label=names.get(target, target.upper()),
                    main="ON" if on else "OFF", size=52,
                    bg=C["cyanBg"] if on else C["bg"],
                    main_color=C["cyan"] if on else C["faint"],
                    bar=C["cyan"] if on else None)
    return dict(label="?", main="?")


def render(d, size=144, flash=None):
    """Draw a key design to a square PIL image of `size` pixels."""
    S = 144
    im = Image.new("RGB", (S, S), d.get("bg", C["bg"]))
    dr = ImageDraw.Draw(im)
    if not d.get("blank"):
        dr.text((S / 2, 22), _fit(d.get("label", ""), 12), anchor="mm",
                font=_font(19), fill=d.get("label_color", C["cyan"]))
        main = d.get("main", "")
        ms = int((d.get("size") or _main_size(main)) * 1.12)
        f = _font(ms, clock=True)
        while ms > 16 and dr.textlength(main, font=f) > S - 10:
            ms -= 2
            f = _font(ms, clock=True)
        sub = d.get("sub", "")
        dr.text((S / 2, 80 if sub else 86), main, anchor="mm", font=f,
                fill=d.get("main_color", C["text"]))
        if sub:
            dr.text((S / 2, 118), _fit(sub, 13), anchor="mm", font=_font(17),
                    fill=d.get("sub_color", C["muted"]))
        if d.get("bar"):
            dr.rectangle([0, 136, S, S], fill=d["bar"])
    if flash == "ok":
        dr.rectangle([3, 3, S - 4, S - 4], outline=C["green"], width=7)
    elif flash == "alert":
        dr.rectangle([3, 3, S - 4, S - 4], outline=C["red"], width=7)
        dr.line([40, 40, 104, 104], fill=C["red"], width=10)
        dr.line([104, 40, 40, 104], fill=C["red"], width=10)
    if size != S:
        im = im.resize((size, size), Image.LANCZOS)
    return im


def press_command(key, state):
    a = key.get("action", "none")
    if a == "clock":
        press = key.get("press", "toggle")
        if press == "none":
            return None
        if press == "next":
            return "next_segment"
        if press == "mark":
            return "mark"
        return "prog_toggle" if key.get("source", "segment").startswith("prog") \
            else "seg_toggle"
    return {"alltoggle": "all_toggle", "allstart": "all_start",
            "allstop": "all_stop", "segtoggle": "seg_toggle",
            "progtoggle": "prog_toggle", "next": "next_segment",
            "mark": "mark", "undo": "undo"}.get(a) or (
        f"{key.get('target', 'dual')}_toggle" if a == "display" else None)


# ------------------------------------------------------------------ library
def load_library():
    """Import the vendored streamdeck library and make hidapi.dll findable.
    Returns (DeviceManager, PILHelper, error_text)."""
    vendor = os.path.join(HERE, "vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)
    for d in {BASE, HERE}:
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
    for d in (BASE, HERE, os.path.dirname(sys.executable)):
        lib = os.path.join(d, "libhidapi.dylib")
        if sys.platform == "darwin" and os.path.exists(lib):
            os.environ.setdefault("DA_HIDAPI", lib)
    if Image is None:
        return None, None, "Pillow isn't installed (pip install pillow)"
    try:
        from StreamDeck.DeviceManager import DeviceManager
        from StreamDeck.ImageHelpers import PILHelper
        return DeviceManager, PILHelper, None
    except Exception as exc:  # missing library / python too old
        return None, None, f"Stream Deck library unavailable: {exc}"


def elgato_app_running():
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq StreamDeck.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "streamdeck.exe" in out.lower()
    except Exception:
        return False


# ------------------------------------------------------------------ controller
class DeckController:
    """Runs in its own thread: finds a Stream Deck, draws keys from DA Timer's
    state and turns key presses into DA Timer commands."""

    def __init__(self, get_state, send_command, get_layout, get_brightness):
        self.get_state = get_state
        self.send_command = send_command
        self.get_layout = get_layout          # fn(deck_type, key_count)
        self.get_brightness = get_brightness
        self.deck = None
        self.info = None                       # dict about the connected deck
        self.status = "Starting\u2026"
        self.error = None
        self.elgato_running = False
        self._enabled = True
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._down = {}                        # key -> press time
        self._fired = set()
        self._flash = {}                       # key -> (kind, until)
        self._last = {}
        self._dirty = True
        self.DeviceManager, self.PILHelper, self.error = load_library()
        if self.error:
            self.status = self.error
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # public --------------------------------------------------------------
    @property
    def connected(self):
        return self.deck is not None

    def set_enabled(self, on):
        self._enabled = on
        if not on:
            self._close("Turned off")

    def refresh(self):
        """Layout/brightness changed - redraw everything."""
        self._dirty = True

    def stop(self):
        self._stop.set()
        self._close("Stopped", blank=True)

    # thread ----------------------------------------------------------------
    def _run(self):
        last_scan = last_app_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_app_check > 10:
                last_app_check = now
                self.elgato_running = elgato_app_running()
            if self.error or not self._enabled:
                time.sleep(0.5)
                continue
            if self.deck is None:
                if now - last_scan >= RESCAN_INTERVAL:
                    last_scan = now
                    self._connect()
                time.sleep(0.2)
                continue
            try:
                if not self.deck.connected():
                    raise IOError("unplugged")
                self._draw()
            except Exception:
                self._close("Disconnected \u2014 waiting for a Stream Deck")
            time.sleep(RENDER_INTERVAL)

    def _connect(self):
        try:
            decks = [d for d in self.DeviceManager().enumerate()
                     if d.is_visual()]
        except Exception as exc:
            msg = str(exc).lower()
            if "hid" in msg or "libusb" in msg or "backend" in msg:
                self.status = ("Can't reach USB \u2014 hidapi.dll is missing "
                               "or blocked")
            else:
                self.status = "USB error \u2014 " + str(exc)[:80]
            return
        if not decks:
            self.status = "No Stream Deck found \u2014 plug one in"
            return
        deck = decks[0]
        try:
            deck.open()
            deck.reset()
            deck.set_brightness(self.get_brightness())
            rows, cols = deck.key_layout()
            fmt = deck.key_image_format()
            try:
                serial = deck.get_serial_number()
            except Exception:
                serial = ""
            with self._lock:
                self.deck = deck
                self.info = dict(type=deck.deck_type(), keys=deck.key_count(),
                                 rows=rows, cols=cols, size=fmt["size"],
                                 serial=serial.strip("\x00 "))
                self._last, self._down, self._fired = {}, {}, set()
                self._dirty = True
            deck.set_key_callback(self._on_key)
            self.status = f"Connected: {self.info['type']} ({self.info['keys']} keys)"
        except Exception as exc:
            self.status = ("Couldn't open the Stream Deck \u2014 is another "
                           "app using it?")
            try:
                deck.close()
            except Exception:
                pass

    def _close(self, status, blank=False):
        with self._lock:
            deck, self.deck, self.info = self.deck, None, None
        if deck is not None:
            try:
                with deck:
                    if blank:
                        deck.reset()
                    deck.close()
            except Exception:
                pass
        self.status = status

    def _on_key(self, deck, key, pressed):
        # Runs on the library's reader thread
        info = self.info
        if not info:
            return
        layout = self.get_layout(info["type"], info["keys"])
        if key >= len(layout):
            return
        k = layout[key]
        now = time.monotonic()
        if pressed:
            self._down[key] = now
            if k.get("action") == "reset":
                return                       # handled while held
            cmd = press_command(k, self.get_state())
            if cmd:
                self.send_command(cmd)
                if k.get("action") == "mark":
                    self._flash[key] = ("ok", now + 0.5)
        else:
            self._down.pop(key, None)
            if k.get("action") == "reset":
                if key in self._fired:
                    self._fired.discard(key)
                else:
                    self._flash[key] = ("alert", now + 0.6)  # released early

    def _draw(self):
        info = self.info
        deck = self.deck
        if not info or deck is None:
            return
        if self._dirty:
            self._dirty = False
            self._last = {}
            with deck:
                deck.set_brightness(self.get_brightness())
        state = self.get_state()
        layout = self.get_layout(info["type"], info["keys"])
        now = time.monotonic()
        for i in range(info["keys"]):
            k = layout[i] if i < len(layout) else {"action": "none"}
            held = i in self._down
            if k.get("action") == "reset" and held and i not in self._fired \
                    and now - self._down[i] >= HOLD_SECONDS:
                self._fired.add(i)
                self.send_command("reset_program")
                self._flash[i] = ("ok", now + 0.6)
            flash = self._flash.get(i)
            if flash and flash[1] < now:
                self._flash.pop(i, None)
                flash = None
            d = design(k, state, holding=held and k.get("action") == "reset"
                       and i not in self._fired)
            sig = (repr(sorted(d.items())), flash and flash[0])
            if self._last.get(i) == sig:
                continue
            self._last[i] = sig
            w, h = info["size"]
            img = render(d, min(w, h), flash and flash[0])
            if (w, h) != img.size:          # non-square keys (Studio)
                canvas = Image.new("RGB", (w, h), d.get("bg", C["bg"]))
                canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
                img = canvas
            native = self.PILHelper.to_native_key_format(deck, img)
            with deck:
                deck.set_key_image(i, native)
