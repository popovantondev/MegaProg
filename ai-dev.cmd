@echo off
setlocal
set "PYTHONUTF8=1"
chcp 65001 >nul
pushd "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m ai_dev %*
) else (
  python -m ai_dev %*
)
set "exit_code=%errorlevel%"
popd
endlocal & exit /b %exit_code%
