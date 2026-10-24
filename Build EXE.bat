@echo off
REM Builds a standalone DA_Timer.exe (no Python needed on the show PC)
REM Includes direct Stream Deck support and LTC timecode input
cd /d "%~dp0"
set PY=
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"
%PY% -m pip install --upgrade pyinstaller pillow openpyxl numpy sounddevice
%PY% -m PyInstaller --noconfirm --onefile --windowed --name DA_Timer --icon stopwatch.ico ^
  --version-file version_info.txt ^
  --add-data "stopwatch.ico;." ^
  --add-data "author_logo.png;." ^
  --add-data "fonts;fonts" ^
  --add-binary "hidapi.dll;." ^
  --paths vendor --collect-submodules StreamDeck ^
  --hidden-import da_deck --hidden-import da_sync ^
  --hidden-import PIL.ImageTk --hidden-import PIL._tkinter_finder ^
  --collect-all sounddevice ^
  da_timer.py
echo.
echo Done. Your exe is in the "dist" folder.
pause
