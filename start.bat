@echo off
chcp 65001 >NUL
cd /d "%~dp0"

echo ============================================================
echo   AI Chat Platform - Start   (single window)
echo   backend + frontend on host; databases in docker
echo ============================================================
echo.

REM ---- Read ports (defaults 8001 / 3001); override via dev.env ----
set BACKEND_PORT=8001
set FRONTEND_PORT=3001
if exist "%~dp0dev.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0dev.env") do (
    if "%%a"=="BACKEND_PORT" set "BACKEND_PORT=%%b"
    if "%%a"=="FRONTEND_PORT" set "FRONTEND_PORT=%%b"
  )
)
REM Inherited by the frontend child process below.
set "NEXT_PUBLIC_API_BASE_URL=http://localhost:%BACKEND_PORT%"

REM ---- 0. Docker running? ----
docker info >NUL 2>&1
if errorlevel 1 (
    echo [X] Docker is not running. Start Docker Desktop, then run this again.
    echo     The app runs natively but the databases still use docker.
    pause
    exit /b 1
)

REM ---- 1. Stop the full docker app stack if running (frees docker app ports). ----
echo [1/5] Stopping any running app containers ...
docker compose down >NUL 2>&1

REM ---- 2. Start databases (postgres + qdrant). Never needs rebuild. ----
echo [2/5] Starting databases: postgres, qdrant ...
docker compose -f docker-compose.dev.yml up -d
if errorlevel 1 (
    echo [X] Failed to start databases. Check docker-compose.dev.yml.
    pause
    exit /b 1
)

REM ---- 3. Prepare backend venv + frontend deps in THIS shell ----
REM        (so the start /b children inherit the environment).
echo [3/5] Preparing backend venv + frontend deps ...
pushd backend
if not exist ".venv\Scripts\python.exe" (
    echo   creating backend venv and installing deps - one-time, about 1 min...
    python -m venv .venv
    call ".venv\Scripts\activate.bat"
    python -m pip install -q --upgrade pip
    pip install -q -r requirements.txt
) else (
    call ".venv\Scripts\activate.bat"
)
popd
pushd frontend
if not exist "node_modules\next" (
    echo   installing frontend deps - one-time, about 1 min...
    call npm install
)
popd

REM ---- 4. Free the app ports: kill any stale backend/frontend processes ----
REM        left over from a previous run (avoids EADDRINUSE on restart).
echo [4/5] Freeing app ports %BACKEND_PORT% and %FRONTEND_PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :%BACKEND_PORT% ^| findstr LISTENING') do taskkill /F /T /PID %%p >NUL 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :%FRONTEND_PORT% ^| findstr LISTENING') do taskkill /F /T /PID %%p >NUL 2>&1

REM ---- 5. Launch backend + frontend in THIS window (no new windows). ----
echo [5/5] Launching backend port %BACKEND_PORT% and frontend port %FRONTEND_PORT% ...
echo.
echo   Backend :  http://localhost:%BACKEND_PORT%   docs at /docs
echo   Frontend:  http://localhost:%FRONTEND_PORT%
echo   Login   :  admin@example.com / changeme123
echo.
echo   --- logs from both services appear below, interleaved ---
echo.

start "ai-backend" /b cmd /c "cd /d %~dp0backend && uvicorn app.main:app --reload --host 0.0.0.0 --port %BACKEND_PORT%"
start "ai-frontend" /b cmd /c "cd /d %~dp0frontend && npx next dev -p %FRONTEND_PORT%"

REM ---- Wait for both to come up ----
echo waiting for services ...
set b=0
:be
set /a b+=1
curl -fs -m 2 http://localhost:%BACKEND_PORT%/health >NUL 2>&1 && goto be_ok
if %b% geq 40 goto be_slow
ping 127.0.0.1 -n 3 >NUL
goto be
:be_ok
echo [ok] backend up.
goto fe
:be_slow
echo [!] backend not responding yet - watch the logs below.
:fe
set f=0
:fe_l
set /a f+=1
REM Frontend readiness = port listening (next dev's first compile is slow,
REM so HTTP 200 is unreliable; a listening check is fast and correct).
netstat -ano | findstr ":%FRONTEND_PORT% " | findstr LISTENING >NUL 2>&1 && goto fe_ok
if %f% geq 40 goto fe_slow
ping 127.0.0.1 -n 3 >NUL
goto fe_l
:fe_ok
echo [ok] frontend up.
goto waitall
:fe_slow
echo [!] frontend not responding yet - watch the logs below.
:waitall
echo.
echo ============================================================
echo   Both services are running in THIS window.
echo   Open http://localhost:%FRONTEND_PORT%
echo   Press any key here to STOP the app. Databases keep running.
echo   To stop everything including databases, run stop.bat
echo ============================================================
pause >NUL

REM ---- Cleanup: stop the app processes by port (DBs left running). ----
echo.
echo Stopping app processes ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :%BACKEND_PORT% ^| findstr LISTENING') do taskkill /F /T /PID %%p >NUL 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :%FRONTEND_PORT% ^| findstr LISTENING') do taskkill /F /T /PID %%p >NUL 2>&1
echo App stopped. Run stop.bat to stop the databases too.
pause
