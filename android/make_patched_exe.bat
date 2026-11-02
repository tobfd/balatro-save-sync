@echo off
rem Double-click helper: asks for the balatro-mobile-maker folder and builds the patched Balatro.exe.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0make_patched_exe.py" %*
) else (
  python "%~dp0make_patched_exe.py" %*
)
pause
