@echo off
title BOT GARRA — Iniciando...
color 0A
echo.
echo  =============================================
echo    BOT GARRA — SERVIDOR LOCAL
echo    Arquivo: main.py (raiz)
echo  =============================================
echo.

:: Muda para a pasta correta (raiz do projeto)
cd /d "%~dp0"

:: Verifica se python existe
where python >nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=python
) else (
    :: Usa o caminho completo do Python instalado
    set PYTHON=C:\Users\vando\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\python.exe
)

echo  Iniciando main.py da RAIZ (pasta correta)...
echo  Pressione CTRL+C para parar.
echo.

"%PYTHON%" main.py

pause
