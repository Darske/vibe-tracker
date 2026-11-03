@echo off
title vibe-tracker - push update
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
git pull --rebase origin main
git add -A
git commit -m "Milestone 3 dashboard pages + batched DB writes"
git push origin main
echo.
echo === DONE - you can close this window ===
pause
