@echo off
title vibe-tracker - push
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
if exist workflows_new\*.yml (
  move /y workflows_new\*.yml .github\workflows\
  rmdir /s /q workflows_new
)
git config pull.rebase false
git add -A
git commit -m "ccu poll + likes per 1k plays"
echo === Merging what is already on GitHub ===
git pull --no-edit -X ours origin main
echo === Pushing ===
git push origin main
echo.
echo === DONE - you can close this window ===
pause
