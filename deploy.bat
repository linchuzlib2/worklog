@echo off
setlocal
cd /d %~dp0

set REPO_SSH=git@github.com:linchuzlib2/worklog.git
set DEFAULT_BRANCH=main
set SSH_KEY=%~dp0.ssh\id_ed25519

echo === [1/5] Checking git ===
where git >nul 2>nul
if errorlevel 1 (
  echo ERROR: git not found in PATH. Install Git for Windows from https://git-scm.com/download/win
  exit /b 1
)

if not exist "%SSH_KEY%" (
  echo ERROR: SSH key not found at %SSH_KEY%
  echo Run: ssh-keygen -t ed25519 -C "linchuzlib2@users.noreply.github.com" -f "%SSH_KEY%" -N ""
  exit /b 1
)

REM Configure git to use this SSH key and disable host key checking (first push)
set GIT_SSH_COMMAND=ssh -i "%SSH_KEY%" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new

REM Set local git identity if not already set
git config user.name >nul 2>nul
if errorlevel 1 git config user.name "linchuzlib2"
git config user.email >nul 2>nul
if errorlevel 1 git config user.email "linchuzlib2@users.noreply.github.com"

echo.
echo === [2/5] Initializing repo ===
if not exist .git (
  git init -b %DEFAULT_BRANCH%
  if errorlevel 1 git init ^&^& git symbolic-ref HEAD refs/heads/%DEFAULT_BRANCH%
)

REM Ensure remote origin exists and points to SSH URL
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin %REPO_SSH%
) else (
  for /f "delims=" %%u in ('git remote get-url origin') do set CURRENT_URL=%%u
  if not "%CURRENT_URL%"=="%REPO_SSH%" git remote set-url origin %REPO_SSH%
)

echo.
echo === [3/5] Staging and committing ===
git add -A
git diff --cached --quiet
if %errorlevel%==0 (
  echo No changes to commit.
) else (
  git commit -m "Update worklog app %date% %time%"
)

echo.
echo === [4/5] Fetching remote (verifies SSH auth) ===
git fetch origin %DEFAULT_BRANCH%
if errorlevel 1 (
  echo.
  echo Fetch failed. Likely the SSH public key has not been added to GitHub yet.
  echo Public key:
  type "%SSH_KEY%.pub"
  echo.
  echo Add it at: https://github.com/settings/ssh/new
  echo Then re-run this script.
  exit /b 1
)

REM If remote branch exists, soft-reset local to it (keep our changes as new commit on top)
git rev-parse --verify refs/remotes/origin/%DEFAULT_BRANCH% >nul 2>nul
if not errorlevel 1 (
  git reset --soft refs/remotes/origin/%DEFAULT_BRANCH%
  git add -A
  git diff --cached --quiet
  if not errorlevel 1 (
    echo No changes ahead of remote.
  ) else (
    git commit -m "Update worklog app %date% %time%"
  )
)

echo.
echo === [5/5] Pushing to GitHub via SSH ===
git push -u origin %DEFAULT_BRANCH%
if errorlevel 1 (
  echo Push failed. See message above.
  exit /b 1
)

echo.
echo Push complete. Render will auto-deploy if connected to this repo.
endlocal
