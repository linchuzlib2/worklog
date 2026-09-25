@echo off
setlocal
cd /d %~dp0

echo === [1/3] Checking git status ===
git status --porcelain
if not exist .git (
  echo Initializing git repository...
  git init
)

echo.
echo === [2/3] Committing changes ===
git add -A
git diff --cached --quiet
if %errorlevel%==0 (
  echo No changes to commit.
) else (
  git commit -m "Update worklog app %date% %time%"
)

echo.
echo === [3/3] Pushing to GitHub ===
git push origin HEAD

echo.
echo Push complete. Render will auto-deploy if connected to this repo.
endlocal
