@echo off
title vibe-tracker - push update
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"
cd /d "%USERPROFILE%\Documents\vibe-tracker"
git config pull.rebase false
git add -A
git commit -m "coverage + lifetime/daily plays" >/dev/null 2>&1
echo === Merging what is already on GitHub ===
git pull --no-edit -X ours origin main
if errorlevel 1 (
  echo Merge needs help - keeping local versions of conflicting files
  git checkout --ours . && git add -A && git commit --no-edit -m "merge origin/main"
)
echo === Pushing ===
git push origin main
echo.
echo === DONE - you can close this window ===
pause
