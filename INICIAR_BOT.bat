@echo off
title BOT GARRA — PC Local + Ngrok
color 0A
echo.
echo  =============================================
echo    BOT GARRA ^|^| INICIANDO NO PC + NGROK
echo    Pasta: BOT GARRA SERVIDOR351
echo  =============================================
echo.

set PYTHON=C:\Users\vando\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\python.exe
set PASTA=C:\Users\vando\OneDrive\Desktop\BOT GARRA SERVIDOR351
set NGROK=%PASTA%\ngrok.exe
set DOMINIO=paraphrastically-gatherable-deloras.ngrok-free.dev

:: Mata processos anteriores
echo  Encerrando processos anteriores...
taskkill /F /IM ngrok.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000.*LISTENING" 2^>nul') do taskkill /PID %%a /F >nul 2>&1
timeout /t 2 /nobreak >nul

:: Inicia ngrok com dominio fixo
echo  Iniciando ngrok com dominio fixo...
cd /d "%PASTA%"
start "" /B "%NGROK%" http --domain=%DOMINIO% 5000 >> ngrok_log.txt 2>&1
echo  Aguardando ngrok subir (10s)...
timeout /t 10 /nobreak >nul

:: Confirma que ngrok esta respondendo
:CHECK_NGROK
curl -s http://localhost:4040/api/tunnels | findstr "public_url" >nul 2>&1
if errorlevel 1 (
    echo  Aguardando ngrok...
    timeout /t 3 /nobreak >nul
    goto CHECK_NGROK
)
echo  ✅ Ngrok ativo! Iniciando bot...

:: Inicia bot local (stdout e stderr aparecem direto no terminal)
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%PASTA%"
"%PYTHON%" -u main.py 2>&1

pause
