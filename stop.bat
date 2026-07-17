@echo off
chcp 65001 >NUL
cd /d "%~dp0"
title AI Chat - Stop

REM ---- Read ports (defaults 8001 / 3001); override via dev.env ----
set BACKEND_PORT=8001
set FRONTEND_PORT=3001
if exist "%~dp0dev.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0dev.env") do (
    if "%%a"=="BACKEND_PORT" set "BACKEND_PORT=%%b"
    if "%%a"=="FRONTEND_PORT" set "FRONTEND_PORT=%%b"
  )
)

echo Stopping app processes - backend :%BACKEND_PORT%, frontend :%FRONTEND_PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :%BACKEND_PORT% ^| findstr LISTENING') do taskkill /F /T /PID %%p >NUL 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :%FRONTEND_PORT% ^| findstr LISTENING') do taskkill /F /T /PID %%p >NUL 2>&1

echo Stopping databases - postgres, qdrant ...
docker compose -f docker-compose.dev.yml down >NUL 2>&1
docker compose down >NUL 2>&1

echo.
echo Stopped. Data volumes are kept - run start.bat again anytime.
pause
