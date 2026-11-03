@echo off
title vibe-tracker - first scrape run
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
echo === Secrets present? ===
gh secret list
echo.
echo === Triggering the scrape workflow ===
gh workflow run scrape.yml
timeout /t 8 >nul
echo.
echo === Recent runs ===
gh run list --limit 5
echo.
echo === Watching the latest scrape run (Ctrl+C to stop watching; the run continues on GitHub) ===
for /f "tokens=1" %%i in ('gh run list --workflow=scrape.yml --limit 1 --json databaseId --jq ".[0].databaseId"') do set RUNID=%%i
gh run watch %RUNID% --exit-status
if errorlevel 1 (
  echo.
  echo === Run FAILED - last 80 log lines: ===
  gh run view %RUNID% --log-failed | more
)
echo.
pause
