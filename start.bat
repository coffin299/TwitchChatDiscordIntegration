@echo off
rem Launcher for the sub PC.
rem Creates .venv on first run and starts the bot without writing __pycache__.
cd /d %~dp0
rem Prevent Python from writing .pyc files
set PYTHONDONTWRITEBYTECODE=1

set VENV_PY=.venv\Scripts\python.exe

rem Create .venv only on first run
if not exist "%VENV_PY%" (
    echo Creating .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create .venv. Make sure Python 3.11+ is on PATH.
        pause
        exit /b 1
    )
)

rem Require discord.py 2.7+ with davey (DAVE), plus kanalizer for EN reading.
rem Import-only checks miss old discord.py installs, so assert versions too.
"%VENV_PY%" -B -c "import discord, nacl, davey, kanalizer; from discord.voice_state import has_dave; assert has_dave; v=tuple(int(x) for x in discord.__version__.split('.')[:2]); assert v>=(2,7)" 2>nul
if errorlevel 1 (
    echo Installing / upgrading dependencies into .venv ...
    rem --no-compile avoids creating .pyc files in site-packages
    "%VENV_PY%" -m pip install -U --no-compile -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies. Check your network and requirements.txt.
        pause
        exit /b 1
    )
)

"%VENV_PY%" -B main.py %*
pause
