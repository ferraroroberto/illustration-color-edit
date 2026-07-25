@echo off
REM Launch the SVG color-to-grayscale Streamlit app on Windows.
REM Uses the project's local .venv if present; otherwise falls back to system Python.

setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    echo [warn] No .venv found in %CD%. Falling back to system python.
    set "PYEXE=python"
)

REM Port comes from .streamlit\config.toml (8503) unless overridden via %*.
"%PYEXE%" -m streamlit run app\app.py %*

REM Keep the window open on failure so the reason stays readable. Streamlit
REM writes "Port 8503 is not available" and exits immediately when the port is
REM taken; without this the window closes before anyone can read it (issue #42).
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo [error] Streamlit exited with code %RC%. See the message above.
    pause
)

endlocal
