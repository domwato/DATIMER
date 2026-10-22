@echo off
cd /d "%~dp0"
REM Find Python: the "py" launcher (always installed by python.org / winget),
REM otherwise python on the PATH.
set PY=
set PYW=
py -3 -c "import sys" >nul 2>&1 && (set "PY=py -3" & set "PYW=pyw -3")
if not defined PY python -c "import sys" >nul 2>&1 && (set "PY=python" & set "PYW=pythonw")
if not defined PY (
  echo Python isn't installed. Open PowerShell and run:
  echo     winget install -e --id Python.Python.3.12
  echo then run this file again.
  pause
  exit /b 1
)
REM Install Pillow (logos/buttons/Stream Deck), openpyxl (Excel log export)
REM and numpy + sounddevice (LTC timecode input) if missing
%PY% -c "import PIL, openpyxl, numpy, sounddevice" 2>nul || (
  echo Installing what DA Timer needs - one time only...
  %PY% -m pip install --quiet pillow openpyxl numpy sounddevice
)
start "" %PYW% da_timer.py
