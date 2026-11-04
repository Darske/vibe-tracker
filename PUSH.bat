@echo off
title vibe-tracker - push
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
git config pull.rebase false
git add -A
git commit -m "coverage: deeper lists; lifetime + daily plays in dashboard"
echo === Merging what is already on GitHub ===
git pull --no-edit -X ours origin main
echo === Pushing ===
git push origin main
echo.
echo === DONE - you can close this window ===
pause
