@echo off
chcp 65001 >nul
title GPX Route Builder

:: Porta nella cartella dello script
cd /d "%~dp0"

echo ======================================
echo   GPX Route Builder
echo ======================================
echo.

:: ── 1. Docker Desktop ──────────────────────────────────────────────────────────

docker info >nul 2>&1
if %errorlevel% neq 0 (
    set "DOCKER_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
    if not exist "%DOCKER_EXE%" (
        echo ERRORE: Docker Desktop non trovato.
        echo Scaricalo da: https://www.docker.com/products/docker-desktop/
        echo.
        pause
        exit /b 1
    )

    echo Avvio Docker Desktop...
    start "" "%DOCKER_EXE%"

    echo Attendo che Docker sia pronto
    set ELAPSED=0
    :waitdocker
        timeout /t 3 /nobreak >nul
        set /a ELAPSED+=3
        <nul set /p "=  %ELAPSED%s..."
        docker info >nul 2>&1
        if %errorlevel% neq 0 (
            if %ELAPSED% GEQ 120 (
                echo.
                echo.
                echo ERRORE: Docker non risponde dopo 2 minuti.
                echo Apri Docker Desktop manualmente e riprova.
                pause
                exit /b 1
            )
            goto waitdocker
        )
    echo.
    echo Docker pronto.
)

echo.
echo Controllo aggiornamenti...
docker compose pull

echo.
echo Avvio GPX Route Builder...
echo (il browser si apre automaticamente non appena l'app e' pronta)
echo Per fermare l'applicazione: premi Ctrl+C in questa finestra.
echo.

:: ── 2. Apri il browser quando Streamlit risponde ───────────────────────────────

start /b powershell -WindowStyle Hidden -Command ^
    "do { Start-Sleep 2 } until (try { (Invoke-WebRequest 'http://localhost:8501/_stcore/health' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false }); Start-Sleep 1; Start-Process 'http://localhost:8501'"

:: ── 3. docker compose up (foreground — i log restano visibili) ────────────────

docker compose up
