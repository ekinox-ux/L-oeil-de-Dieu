@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "BACKEND_PORT=%BACKEND_PORT%"
if not defined BACKEND_PORT set "BACKEND_PORT=8500"
set "FRONTEND_PORT=%FRONTEND_PORT%"
if not defined FRONTEND_PORT set "FRONTEND_PORT=3000"
set "VENV_PATH=%VENV_PATH%"
if not defined VENV_PATH set "VENV_PATH=%SCRIPT_DIR%venv"

set "PYTHON_EXE=%VENV_PATH%\Scripts\python.exe"
set "PID_DIR=%TEMP%\oeil_pids"
set "BACKEND_PID_FILE=%PID_DIR%\backend.pid"
set "FRONTEND_PID_FILE=%PID_DIR%\frontend.pid"
set "BOT_PID_FILE=%PID_DIR%\bot.pid"
set "BACKEND_LOG_FILE=%SCRIPT_DIR%backend\uvicorn.log"
set "BACKEND_ERR_LOG_FILE=%SCRIPT_DIR%backend\uvicorn.err.log"
set "SUPERVISOR_LOG_FILE=%PID_DIR%\supervisor.log"
set "STOP_SCRIPT=%SCRIPT_DIR%stop_project.ps1"

set "PS_GET_BACKEND_READY=try { $h = Invoke-RestMethod -Uri http://127.0.0.1:%BACKEND_PORT%/api/health -TimeoutSec 2; if ($h.indexes_loaded -gt 0) { exit 0 } else { exit 1 } } catch { exit 1 }"
set "PS_GET_BACKEND_UP=try { $null = Invoke-RestMethod -Uri http://127.0.0.1:%BACKEND_PORT%/api/health -TimeoutSec 2; exit 0 } catch { exit 1 }"
set "PS_HAS_BACKEND_LISTENER=$listenerPid = Get-NetTCPConnection -LocalPort %BACKEND_PORT% -State Listen -ErrorAction SilentlyContinue ^| Select-Object -First 1 -ExpandProperty OwningProcess; if ($listenerPid) { exit 0 } else { exit 1 }"
set "PS_BACKEND_PROCESS_RUNNING=if (-not (Test-Path -LiteralPath '%BACKEND_PID_FILE%')) { exit 1 }; $trackedPid = Get-Content -LiteralPath '%BACKEND_PID_FILE%' -ErrorAction SilentlyContinue ^| Select-Object -First 1; if ($trackedPid -and (Get-Process -Id ([int]$trackedPid) -ErrorAction SilentlyContinue)) { exit 0 } else { exit 1 }"
set "PS_BOT_PROCESS_RUNNING=if (-not (Test-Path -LiteralPath '%BOT_PID_FILE%')) { exit 1 }; $trackedPid = Get-Content -LiteralPath '%BOT_PID_FILE%' -ErrorAction SilentlyContinue ^| Select-Object -First 1; if ($trackedPid -and (Get-Process -Id ([int]$trackedPid) -ErrorAction SilentlyContinue)) { exit 0 } else { exit 1 }"

rem -- Detection collision port backend (lancee apres l'arret de nos propres process) --
rem N'ecrit rien si le port est libre; sinon affiche PID, nom et commandline du detenteur.
set "PS_CHECK_PORT_FOREIGN=$listenerPid = Get-NetTCPConnection -LocalPort %BACKEND_PORT% -State Listen -ErrorAction SilentlyContinue ^| Select-Object -First 1 -ExpandProperty OwningProcess; if (-not $listenerPid) { exit 0 }; $proc = Get-CimInstance Win32_Process -Filter \"ProcessId=$listenerPid\" -ErrorAction SilentlyContinue; $name = if ($proc) { $proc.Name } else { 'inconnu' }; $cmd = if ($proc) { $proc.CommandLine } else { '' }; Write-Host \"PID=$listenerPid nom=$name cmd=$cmd\"; exit 1"
set "PS_CHECK_BACKEND_BIND=$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, %BACKEND_PORT%); $available = $false; try { $listener.Start(); $available = $true } catch {} finally { try { $listener.Stop() } catch {} }; if ($available) { exit 0 } else { exit 1 }"
set "PS_CHECK_FRONTEND_PORT=if (Get-NetTCPConnection -LocalPort %FRONTEND_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"

if not exist "%PYTHON_EXE%" (
    echo [ERREUR] Python du venv introuvable: %PYTHON_EXE%
    echo Creez-le avec: powershell -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1"
    exit /b 1
)

"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le venv est invalide ou n'utilise pas Python 3.14.
    echo Reconstruction sure: powershell -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1" -Recreate
    exit /b 1
)

if not exist "%PID_DIR%" mkdir "%PID_DIR%"
> "%SUPERVISOR_LOG_FILE%" echo [%date% %time%] Supervisor start

rem -- Nettoyage des zombies d'anciennes sessions --
echo Nettoyage des process orphelins...
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_SCRIPT%" -PythonExe "%PYTHON_EXE%" -PidDir "%PID_DIR%" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Impossible d'arreter tous les anciens process du projet.
    echo Consultez les fichiers PID dans %PID_DIR%.
    exit /b 1
)
call :log Nettoyage pre-demarrage effectue

rem -- Verification collision port backend apres nettoyage --
rem Nos process viennent d'etre arretes : tout detenteur restant est etranger.
set "PORT_HOLDER="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "%PS_CHECK_PORT_FOREIGN%"`) do set "PORT_HOLDER=%%I"
if defined PORT_HOLDER (
    echo.
    echo [ERREUR] Port %BACKEND_PORT% deja utilise par un process etranger : !PORT_HOLDER!
    echo Liberez le port ou relancez avec BACKEND_PORT=autre_port
    call :log Abort: port %BACKEND_PORT% occupe par !PORT_HOLDER!
    exit /b 1
)
set "PORT_HOLDER="

powershell -NoProfile -Command "%PS_CHECK_BACKEND_BIND%" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Port backend %BACKEND_PORT% indisponible ou reserve par Windows.
    echo Choisissez un port hors des plages exclues de Windows.
    call :log Abort: port backend %BACKEND_PORT% indisponible ou reserve
    exit /b 1
)

powershell -NoProfile -Command "%PS_CHECK_FRONTEND_PORT%" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Port frontend %FRONTEND_PORT% deja utilise.
    call :log Abort: port frontend %FRONTEND_PORT% occupe
    exit /b 1
)

rem -- Rotation des logs backend (garde les 3 dernieres sessions) --
call :rotate_file "%BACKEND_LOG_FILE%"
call :rotate_file "%BACKEND_ERR_LOG_FILE%"
call :log Logs backend rotates

echo [1/3] Demarrage du backend
call :start_backend
if errorlevel 1 (
    echo [ERREUR] Impossible de lancer le processus backend.
    call :log Echec du lancement du processus backend
    goto startup_failed
)

echo Attente du backend...
set "ATTEMPTS=0"
set "BACKEND_READY=0"
set "BOT_STARTED=0"

:wait_backend_up
set /a ATTEMPTS+=1
if %ATTEMPTS% gtr 15 (
    echo [ERREUR] Backend non joignable apres 15 tentatives.
    call :log Backend non joignable apres 15 tentatives
    goto startup_failed
)
powershell -NoProfile -Command "%PS_GET_BACKEND_UP%" >nul 2>&1
if not errorlevel 1 (
    echo Backend joignable.
    call :log Backend joignable
    goto backend_up_done
)
powershell -NoProfile -Command "%PS_BACKEND_PROCESS_RUNNING%" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le processus backend s'est arrete pendant le demarrage.
    if exist "%BACKEND_ERR_LOG_FILE%" (
        echo.
        echo --- Dernieres erreurs Uvicorn ---
        powershell -NoProfile -Command "Get-Content -LiteralPath '%BACKEND_ERR_LOG_FILE%' -Tail 20"
        echo --- Fin des erreurs Uvicorn ---
    )
    call :log Processus backend arrete pendant le demarrage
    goto startup_failed
)
timeout /t 1 /nobreak >nul
goto wait_backend_up

:backend_up_done
echo [2/3] Demarrage du frontend
powershell -NoProfile -Command "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList '%SCRIPT_DIR%serve_frontend.py', '--port', '%FRONTEND_PORT%', '--directory', '%SCRIPT_DIR%frontend' -WorkingDirectory '%SCRIPT_DIR%frontend' -WindowStyle Normal -PassThru; Set-Content -Path '%FRONTEND_PID_FILE%' -Value $p.Id"
call :log Frontend lance

rem Verifier si un index est deja pret (possible si le backend etait deja lance)
powershell -NoProfile -Command "%PS_GET_BACKEND_READY%" >nul 2>&1
if not errorlevel 1 (
    echo Index pret. Demarrage du bot.
    set "BACKEND_READY=1"
    goto start_bot
)

echo [3/3] Index en chargement, bot en attente...
call :log Index en chargement, bot en attente
goto main_loop

:start_bot
echo [3/3] Demarrage du bot
call :start_bot_process
set "BOT_STARTED=1"
call :log Bot lance

:main_loop
echo.
echo Frontend : http://127.0.0.1:%FRONTEND_PORT%
echo API      : http://127.0.0.1:%BACKEND_PORT%/docs
echo.
echo Appuyez sur Q pour tout fermer.
set "BACKEND_RESTART_COOLDOWN=60"
set "BOT_RESTART_COOLDOWN=60"

:wait_loop
call :watch_backend
call :watch_bot
if "%BOT_STARTED%"=="0" (
    powershell -NoProfile -Command "%PS_GET_BACKEND_READY%" >nul 2>&1
    if not errorlevel 1 (
        echo.
        echo Index pret. Demarrage automatique du bot...
        call :start_bot_process
        set "BOT_STARTED=1"
        set "BACKEND_READY=1"
        call :log Bot lance automatiquement apres disponibilite index
    )
)
choice /C QN /N /T 1 /D N /M "" >nul
if errorlevel 2 goto wait_loop
if errorlevel 1 goto shutdown
goto wait_loop

:startup_failed
echo.
echo Demarrage annule. Le frontend et le bot n'ont pas ete lances.
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_SCRIPT%" -PythonExe "%PYTHON_EXE%" -PidDir "%PID_DIR%" >nul 2>&1
call :log Demarrage annule et process nettoyes
goto end_error

:shutdown
echo Fermeture...
call :log Arret demande par l utilisateur

rem Arret des seuls PID suivis et verifies comme appartenant a ce projet.
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_SCRIPT%" -PythonExe "%PYTHON_EXE%" -PidDir "%PID_DIR%" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Certains process du projet n'ont pas pu etre arretes.
    call :log Echec du nettoyage de certains process
    goto end_error
)

rem Verification finale terminee.
rem Aucun balayage global: les autres processus Python ne sont jamais touches.
call :log Tous les process du projet arretes

echo Termine.
goto end

:watch_backend
if defined BACKEND_RESTART_COOLDOWN (
    if %BACKEND_RESTART_COOLDOWN% gtr 0 set /a BACKEND_RESTART_COOLDOWN-=1
)
curl --connect-timeout 2 --max-time 3 -sf http://127.0.0.1:%BACKEND_PORT%/api/health >nul 2>&1
if not errorlevel 1 (
    set "BACKEND_READY=1"
    goto :eof
)
if defined BACKEND_RESTART_COOLDOWN if %BACKEND_RESTART_COOLDOWN% gtr 0 goto :eof
rem Verifier si le process backend est toujours vivant (meme sans listener)
rem Si le PID existe et tourne, il est probablement en train de charger les index
if exist "%BACKEND_PID_FILE%" (
    for /f "usebackq delims=" %%p in ("%BACKEND_PID_FILE%") do (
        powershell -NoProfile -Command "if (Get-Process -Id %%p -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
        if not errorlevel 1 (
            goto :eof
        )
    )
)
rem Verifier aussi par port, le PID file peut etre absent
powershell -NoProfile -Command "%PS_HAS_BACKEND_LISTENER%" >nul 2>&1
if not errorlevel 1 goto :eof
echo.
echo [ATTENTION] Backend mort (process absent et pas de listener sur %BACKEND_PORT%). Relance...
call :log Backend mort - relance automatique
set "BACKEND_READY=0"
rem Les logs du backend mort contiennent la cause du crash : on les archive
rem avant que le nouveau process ne les ecrase.
call :rotate_file "%BACKEND_LOG_FILE%"
call :rotate_file "%BACKEND_ERR_LOG_FILE%"
call :start_backend
set "BACKEND_RESTART_COOLDOWN=60"
goto :eof

:watch_bot
rem Relance le bot s'il s'est arrete (crash, coupure reseau), avec un delai
rem d'environ 60 tours de boucle entre deux relances.
if not "%BOT_STARTED%"=="1" goto :eof
if %BOT_RESTART_COOLDOWN% gtr 0 (
    set /a BOT_RESTART_COOLDOWN-=1
    goto :eof
)
powershell -NoProfile -Command "%PS_BOT_PROCESS_RUNNING%" >nul 2>&1
if not errorlevel 1 goto :eof
echo.
echo [ATTENTION] Bot arrete. Relance...
call :log Bot arrete - relance automatique
call :start_bot_process
set "BOT_RESTART_COOLDOWN=60"
goto :eof

:start_bot_process
powershell -NoProfile -Command "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList '%SCRIPT_DIR%bot\bot.py' -WorkingDirectory '%SCRIPT_DIR%bot' -WindowStyle Normal -PassThru; Set-Content -Path '%BOT_PID_FILE%' -Value $p.Id"
goto :eof

:start_backend
call :log Demarrage du backend
powershell -NoProfile -Command "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList '-m', 'uvicorn', 'main:app', '--port', '%BACKEND_PORT%', '--app-dir', '%SCRIPT_DIR%backend' -WorkingDirectory '%SCRIPT_DIR%backend' -RedirectStandardOutput '%BACKEND_LOG_FILE%' -RedirectStandardError '%BACKEND_ERR_LOG_FILE%' -WindowStyle Hidden -PassThru; Set-Content -Path '%BACKEND_PID_FILE%' -Value $p.Id"
if errorlevel 1 exit /b 1
exit /b 0

:log
echo [%date% %time%] %*>> "%SUPERVISOR_LOG_FILE%"
goto :eof

:rotate_file
rem Rotate %1 -> %1.1 -> %1.2 -> %1.3 (oldest discarded).
set "F=%~1"
if exist "%F%.3" del /q "%F%.3" >nul 2>&1
if exist "%F%.2" move /y "%F%.2" "%F%.3" >nul 2>&1
if exist "%F%.1" move /y "%F%.1" "%F%.2" >nul 2>&1
if exist "%F%" move /y "%F%" "%F%.1" >nul 2>&1
set "F="
goto :eof

:end
endlocal
exit /b 0

:end_error
endlocal
exit /b 1
