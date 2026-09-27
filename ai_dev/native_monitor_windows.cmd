@echo off
chcp 65001 >nul
if /I "%MEGAPROG_MONITOR_KIND%"=="overview" (
    title MegaProg - ALL PROJECTS
) else (
    title MegaProg - THIS TASK
)
"%MEGAPROG_PY%" -u "%MEGAPROG_MONITOR_RUNNER%"
exit /b %ERRORLEVEL%
