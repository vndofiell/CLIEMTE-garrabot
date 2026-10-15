@echo off
title BOT GARRA — PC Local + Ngrok Fixo
color 0A
echo.
echo  =============================================
echo    BOT GARRA ^|^| INICIANDO NO PC + NGROK
echo    Render: https://bot-garra-uv2r.onrender.com
echo  =============================================
echo.

set PYTHON=C:\Users\vando\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\python.exe
set NGROK=C:\Users\vando\OneDrive\Desktop\BOT GARRA SERVIDOR1\BOT GARRA SERVIDOR\ngrok.exe
set PASTA=C:\Users\vando\OneDrive\Desktop\BOT GARRA SERVIDOR1\BOT GARRA SERVIDOR
set DOMINIO=paraphrastically-gatherable-deloras.ngrok-free.dev
set RENDER=https://bot-garra-uv2r.onrender.com

:: Mata processos anteriores
echo  Encerrando processos anteriores...
taskkill /F /IM ngrok.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000.*LISTENING" 2^>nul') do taskkill /PID %%a /F >nul 2>&1
timeout /t 2 /nobreak >nul

:: Tenta iniciar ngrok com dominio fixo
echo  Tentando iniciar ngrok com dominio fixo...
start "" /B "%NGROK%" http --domain=%DOMINIO% 5000 >> ngrok_log.txt 2>&1
echo  Aguardando ngrok subir...
timeout /t 8 /nobreak >nul

:: Confirma que o ngrok esta respondendo antes de iniciar o bot
:CHECK_NGROK
curl -s http://localhost:4040/api/tunnels | findstr "public_url" >nul 2>&1
if errorlevel 1 (
    echo  Aguardando ngrok...
    timeout /t 3 /nobreak >nul
    goto CHECK_NGROK
)
echo  Ngrok detectado! Iniciando bot...

:: Inicia bot local
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%PASTA%"
start "" /B "%PYTHON%" -u main.py > bot_log.txt 2>&1
timeout /t 5 /nobreak >nul

:: Abre o Render (sempre disponível) e o ngrok se estiver ok
echo  Abrindo navegador...
start "" "%RENDER%"

echo.
echo  =============================================
echo    RENDER (sempre online):
echo    %RENDER%
echo.
echo    LOCAL (se ngrok ok):
echo    https://%DOMINIO%/
echo  =============================================
echo.
pause
