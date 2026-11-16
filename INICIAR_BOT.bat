@echo off
title GARRABOT - Local
color 0A

echo.
echo  =============================================
echo    GARRABOT - INICIANDO LOCALMENTE
echo  =============================================
echo.

set PYTHON=C:\Users\vando\AppData\Local\Programs\Python\Python313\python.exe
set PASTA=C:\Users\vando\OneDrive\Desktop\CLIEMTE-garrabot-main
set NGROK=%PASTA%\ngrok.exe
set DOMINIO=paraphrastically-gatherable-deloras.ngrok-free.dev
set PORT=5000
set PYTHON_VENV=%PASTA%\venv\Scripts\python.exe

:: ================================================================
::  MENU - POS RIT
:: ================================================================
echo.
echo  ============================================
echo    CONFIGURACAO - POS RIT
echo  ============================================
echo.
echo  O bot so entra na conta real apos uma
echo  sequencia de WIN ou LOSS consecutivos.
echo.
echo  [1] DESATIVADO  - entra normalmente
echo  [2] Pos LOSS    - aguarda N losses seguidos
echo  [3] Pos WIN     - aguarda N wins seguidos
echo.
set /p POSRIT_OPCAO=  Escolha (1, 2 ou 3): 

if "%POSRIT_OPCAO%"=="2" goto MENU_LOSS
if "%POSRIT_OPCAO%"=="3" goto MENU_WIN
goto POSRIT_OFF

:MENU_LOSS
echo.
set /p POSRIT_QTD=  Quantos LOSSES consecutivos para entrar (ex: 5): 
set POSRIT_ATIVO=true
set POSRIT_TIPO=loss
goto POSRIT_OK

:MENU_WIN
echo.
set /p POSRIT_QTD=  Quantos WINS consecutivos para entrar (ex: 3): 
set POSRIT_ATIVO=true
set POSRIT_TIPO=win
goto POSRIT_OK

:POSRIT_OFF
set POSRIT_ATIVO=false
set POSRIT_TIPO=loss
set POSRIT_QTD=3
set POSRIT_RECUP=true
echo.
echo  Pos RIT: DESATIVADO
goto POSRIT_CONFIRMAR

:POSRIT_OK
echo.
if "%POSRIT_TIPO%"=="loss" echo  Pos RIT: ATIVO - entrando apos %POSRIT_QTD% LOSSES consecutivos
if "%POSRIT_TIPO%"=="win"  echo  Pos RIT: ATIVO - entrando apos %POSRIT_QTD% WINS  consecutivos
echo.
echo  ============================================
echo    SE A ENTRADA REAL DER LOSS:
echo  ============================================
echo.
echo  [1] Entrar em cada tick ate ganhar (sem perder nenhum tick)
echo  [2] Reiniciar a contagem do zero
echo.
set /p POSRIT_RECUP_OPCAO=  Escolha (1 ou 2):
if "%POSRIT_RECUP_OPCAO%"=="2" (
    set POSRIT_RECUP=false
    echo.
    echo  Apos loss: reinicia contagem do zero.
) else (
    set POSRIT_RECUP=true
    echo.
    echo  Apos loss: entra em cada tick ate WIN.
)

:POSRIT_CONFIRMAR
echo.
echo  ============================================
echo.
set /p CONFIRMAR=  Confirmar e iniciar? (S/N):
if /i "%CONFIRMAR%"=="N" goto MENU_POS_RIT_REINICIAR
goto AMBIENTE

:MENU_POS_RIT_REINICIAR
cls
goto MENU_POS_RIT_LABEL

:MENU_POS_RIT_LABEL
echo.
echo  ============================================
echo    CONFIGURACAO - POS RIT
echo  ============================================
echo.
echo  [1] DESATIVADO  - entra normalmente
echo  [2] Pos LOSS    - aguarda N losses seguidos
echo  [3] Pos WIN     - aguarda N wins seguidos
echo.
set /p POSRIT_OPCAO=  Escolha (1, 2 ou 3):
if "%POSRIT_OPCAO%"=="2" goto MENU_LOSS
if "%POSRIT_OPCAO%"=="3" goto MENU_WIN
goto POSRIT_OFF

:: ================================================================
::  AMBIENTE
:: ================================================================
:AMBIENTE
echo.
echo  Verificando ambiente...

if not exist "%PASTA%\.env" (
    copy "%PASTA%\.env.example" "%PASTA%\.env" >nul
    echo  .env criado.
)

if not exist "%PYTHON_VENV%" (
    echo  Criando venv...
    "%PYTHON%" -m venv "%PASTA%\venv"
    echo  Instalando dependencias...
    "%PYTHON_VENV%" -m pip install -r "%PASTA%\requirements-local.txt" --quiet
)

echo  OK - Ambiente pronto.
echo.

echo  Encerrando processos anteriores...
taskkill /F /IM ngrok.exe >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "0.0.0.0:%PORT%" 2^>nul') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo  Iniciando ngrok...
start "NGROK" /MIN "%NGROK%" http --url=%DOMINIO% %PORT%
timeout /t 8 /nobreak >nul

echo  Verificando ngrok...
set TENTATIVAS=0
:CHECK_NGROK
set /a TENTATIVAS=%TENTATIVAS%+1
curl -s http://localhost:4040/api/tunnels 2>nul | findstr "public_url" >nul 2>&1
if not errorlevel 1 goto NGROK_OK
if %TENTATIVAS% LSS 5 (
    timeout /t 3 /nobreak >nul
    goto CHECK_NGROK
)
echo  AVISO: ngrok sem resposta, continuando sem tunnel...
goto INICIAR

:NGROK_OK
echo  Ngrok ativo! https://%DOMINIO%

:INICIAR
echo.
echo  Iniciando bot...
echo  =============================================
echo.

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%PASTA%"

:: Injeta a config do Pos RIT no bot via arquivo temporario
:: O main.py le posrit_startup.json ao iniciar (se existir) e aplica
echo {"ativo":%POSRIT_ATIVO%,"tipo":"%POSRIT_TIPO%","qtd":%POSRIT_QTD%,"recuperarAposLoss":%POSRIT_RECUP%} > "%PASTA%\posrit_startup.json"

:: Inicia o bot em background para poder abrir o navegador logo depois
start "GARRABOT-SERVER" /B "%PYTHON_VENV%" -u main.py > "%PASTA%\bot_log.txt" 2>&1

echo  Aguardando bot subir...
:WAIT_BOT
timeout /t 2 /nobreak >nul
curl -s http://localhost:%PORT%/ping 2>nul | findstr "online" >nul 2>&1
if errorlevel 1 goto WAIT_BOT

echo  Bot online!
echo.

:: Aplica config do Pos RIT via API do bot
echo  Aplicando configuracao Pos RIT...
curl -s -X POST http://localhost:%PORT%/posrit-config ^
    -H "Content-Type: application/json" ^
    -d "{\"ativo\":%POSRIT_ATIVO%,\"tipo\":\"%POSRIT_TIPO%\",\"qtd\":%POSRIT_QTD%,\"recuperarAposLoss\":%POSRIT_RECUP%}" >nul 2>&1
echo  Configuracao aplicada.

echo.
echo  Abrindo navegador com cache limpo...

:: Abre o Chrome com cache desabilitado apontando para o bot local
start "" "chrome.exe" --disable-cache --new-window "http://localhost:%PORT%"

echo.
echo  =============================================
echo  Bot rodando em: http://localhost:%PORT%
echo  URL publica:    https://%DOMINIO%
echo.
if "%POSRIT_ATIVO%"=="true" (
    if "%POSRIT_TIPO%"=="loss" echo  Pos RIT:        ATIVO - %POSRIT_QTD% LOSSES seguidos
    if "%POSRIT_TIPO%"=="win"  echo  Pos RIT:        ATIVO - %POSRIT_QTD% WINS  seguidos
    if "%POSRIT_RECUP%"=="true" echo  Apos loss:      Entra em cada tick ate WIN
    if "%POSRIT_RECUP%"=="false" echo  Apos loss:      Reinicia contagem do zero
) else (
    echo  Pos RIT:        DESATIVADO
)
echo.
echo  Para VER OS LOGS do bot, rode:
echo    type "%PASTA%\bot_log.txt"
echo  Ou acompanhe o arquivo bot_log.txt
echo.
echo  Pressione qualquer tecla para ENCERRAR o bot.
echo  =============================================
pause >nul

echo  Encerrando bot...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM ngrok.exe >nul 2>&1
del "%PASTA%\posrit_startup.json" >nul 2>&1
echo  Encerrado.
pause
