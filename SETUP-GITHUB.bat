@echo off
title vibe-tracker - GitHub setup
echo === vibe-tracker: GitHub setup ===
cd /d "%USERPROFILE%\Documents\vibe-tracker" || (echo Folder not found & pause & exit /b 1)

where git >nul 2>&1 || (
  echo Installing Git...
  winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
)
where gh >nul 2>&1 || (
  echo Installing GitHub CLI...
  winget install --id GitHub.cli -e --silent --accept-package-agreements --accept-source-agreements
)
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;%LOCALAPPDATA%\Programs\Git\cmd"

if not exist .github\workflows mkdir .github\workflows
if exist workflows_to_install (
  move /y workflows_to_install\*.yml .github\workflows\ >nul
  rmdir /s /q workflows_to_install
)

git config user.name >nul 2>&1 || git config --global user.name "Giorgio"
git config user.email >nul 2>&1 || git config --global user.email "angelesgiorgio01@gmail.com"
if not exist .git git init -b main
git add -A
git commit -m "vibe-tracker: milestones 1-2 (poller, 9 adapters, Health page)" >nul 2>&1 || echo (nothing new to commit)

gh auth status >nul 2>&1
if errorlevel 1 (
  echo.
  echo === GitHub login needed: press Enter when asked, then type the one-time code into the browser ===
  gh auth login --hostname github.com --git-protocol https --web
)

for /f %%u in ('gh api user -q .login') do set GHUSER=%%u
echo GitHub user: %GHUSER%

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  gh repo create vibe-tracker --public --source . --remote origin --push || (
    echo Repo may already exist - adding remote and pushing...
    git remote add origin https://github.com/%GHUSER%/vibe-tracker.git
    git push -u origin main
  )
) else (
  git push -u origin main
)

echo.
echo === DONE: https://github.com/%GHUSER%/vibe-tracker ===
echo You can close this window.
pause
