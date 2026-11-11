@echo off
title vibe-tracker - push
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
git config pull.rebase false
if exist vibe-tracker-update.zip (
  echo === Unpacking vibe-tracker-update.zip ===
  tar -xf vibe-tracker-update.zip
  if errorlevel 1 (
    echo.
    echo !!! Could not unpack the zip - nothing was pushed. Tell Claude. !!!
    pause
    exit /b 1
  )
  del vibe-tracker-update.zip
)
if exist workflows_new (
  if not exist .github\workflows mkdir .github\workflows
  move /y workflows_new\*.yml .github\workflows\ >nul
  rmdir /s /q workflows_new
)
if exist dashboard\pages rmdir /s /q dashboard\pages
git add -A
git commit -m "legacy platforms, activity index, platform totals, AI games view"
echo === Merging what is already on GitHub ===
git pull --no-edit -X ours origin main
echo === Pushing ===
git push origin main
echo.
echo === DONE - you can close this window ===
pause
