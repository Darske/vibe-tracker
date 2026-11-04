@echo off
title vibe-tracker - push
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
git config pull.rebase false
if exist workflows_new (
  if not exist .github\workflows mkdir .github\workflows
  move /y workflows_new\*.yml .github\workflows\ >nul
  rmdir /s /q workflows_new
)
if exist dashboard\pages rmdir /s /q dashboard\pages
git add -A
git commit -m "home: daily plays toggle + clickable platforms; platform page: total plays view"
echo === Merging what is already on GitHub ===
git pull --no-edit -X ours origin main
echo === Pushing ===
git push origin main
echo.
echo === DONE - you can close this window ===
pause
