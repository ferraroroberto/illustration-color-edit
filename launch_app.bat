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

"%PYEXE%" -m streamlit run app\app.py %*

endlocal
