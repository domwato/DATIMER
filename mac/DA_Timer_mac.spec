# PyInstaller spec for the macOS app. Run from the project folder on a Mac
# (or let GitHub Actions do it - see .github/workflows/build-mac.yml):
#     pyinstaller --noconfirm mac/DA_Timer_mac.spec
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = "7.7"
sys.path.insert(0, os.path.join(ROOT, "vendor"))   # vendored StreamDeck lib

datas = [(os.path.join(ROOT, "fonts"), "fonts"),
         (os.path.join(ROOT, "stopwatch.ico"), "."),
         (os.path.join(ROOT, "author_logo.png"), ".")]
binaries = []
hidapi = os.path.join(ROOT, "libhidapi.dylib")      # copied in by the build
if os.path.exists(hidapi):
    binaries.append((hidapi, "."))
hiddenimports = ["da_deck", "da_sync"] + collect_submodules("StreamDeck")
sd = collect_all("sounddevice")
datas += sd[0]; binaries += sd[1]; hiddenimports += sd[2]
hiddenimports += ["AppKit", "Foundation", "objc", "PIL.ImageTk", "PIL._tkinter_finder"]

a = Analysis([os.path.join(ROOT, "da_timer.py")],
             pathex=[ROOT, os.path.join(ROOT, "vendor")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DA Timer",
          console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="DA Timer")
app = BUNDLE(
    coll, name="DA Timer.app",
    icon=os.path.join(ROOT, "stopwatch.ico"),
    bundle_identifier="io.github.domwato.datimer",
    version=VERSION,
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        # LTC timecode comes in through an audio input
        "NSMicrophoneUsageDescription":
            "DA Timer listens to an audio input for LTC timecode.",
        "LSMinimumSystemVersion": "11.0",
    })
