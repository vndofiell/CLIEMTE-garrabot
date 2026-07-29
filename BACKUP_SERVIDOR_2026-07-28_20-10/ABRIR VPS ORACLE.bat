@echo off
setlocal enabledelayedexpansion

:MENU
cls
color 0A
echo.
echo  =============================================
echo    BOT GARRA ^|^| VPS ORACLE 158.101.108.207
echo  =============================================
echo.
echo  [1] Abrir terminal SSH (livre)
echo  [2] Deploy completo  ^(git pull + pm2 restart^)
echo  [3] Ver status PM2
echo  [4] Ver logs ao vivo  ^(Ctrl+C para sair^)
echo  [5] Reiniciar garrabot
echo  [6] Parar garrabot
echo  [7] Iniciar garrabot
echo  [8] Ver uso de CPU / RAM / Disco
echo  [9] Ver ultimas 50 linhas de log
echo  [0] Sair
echo.
set /p "op=  Escolha uma opcao: "

if "%op%"=="1" goto SSH_LIVRE
if "%op%"=="2" goto DEPLOY
if "%op%"=="3" goto STATUS
if "%op%"=="4" goto LOGS_VIVO
if "%op%"=="5" goto RESTART
if "%op%"=="6" goto STOP
if "%op%"=="7" goto START
if "%op%"=="8" goto RECURSOS
if "%op%"=="9" goto LOGS_TAIL
if "%op%"=="0" goto FIM
goto MENU

:: ─────────────────────────────────────────────
:SSH_LIVRE
cls
color 0A
echo.
echo  Abrindo terminal SSH livre...
echo  (Digite 'exit' para voltar ao menu)
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207
goto VOLTAR

:: ─────────────────────────────────────────────
:DEPLOY
cls
color 0E
echo.
echo  Executando deploy...
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "cd ~/CLIEMTE-garrabot && git pull && pm2 restart garrabot && pm2 status"
goto VOLTAR

:: ─────────────────────────────────────────────
:STATUS
cls
color 0B
echo.
echo  Status PM2:
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "pm2 status"
goto VOLTAR

:: ─────────────────────────────────────────────
:LOGS_VIVO
cls
color 07
echo.
echo  Logs ao vivo - pressione Ctrl+C para parar...
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "pm2 logs garrabot"
goto VOLTAR

:: ─────────────────────────────────────────────
:RESTART
cls
color 0E
echo.
echo  Reiniciando garrabot...
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "pm2 restart garrabot && pm2 status"
goto VOLTAR

:: ─────────────────────────────────────────────
:STOP
cls
color 0C
echo.
echo  Parando garrabot...
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "pm2 stop garrabot && pm2 status"
goto VOLTAR

:: ─────────────────────────────────────────────
:START
cls
color 0A
echo.
echo  Iniciando garrabot...
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "pm2 start garrabot && pm2 status"
goto VOLTAR

:: ─────────────────────────────────────────────
:RECURSOS
cls
color 0B
echo.
echo  Uso de recursos da VPS:
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "echo '--- CPU e RAM ---' && free -h && echo '' && echo '--- Disco ---' && df -h / && echo '' && echo '--- PM2 ---' && pm2 status"
goto VOLTAR

:: ─────────────────────────────────────────────
:LOGS_TAIL
cls
color 07
echo.
echo  Ultimas 50 linhas de log:
echo.
ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "pm2 logs garrabot --lines 50 --nostream"
goto VOLTAR

:: ─────────────────────────────────────────────
:VOLTAR
echo.
echo  -----------------------------------------
echo  Pressione qualquer tecla para voltar ao menu...
pause >nul
goto MENU

:: ─────────────────────────────────────────────
:FIM
cls
echo.
echo  Ate logo!
echo.
timeout /t 2 >nul
exit
