@echo off
title Deploy Garrabot - 1 Clique
color 0A
echo.
echo  =============================================
echo    ATUALIZANDO GARRABOT NA VPS ORACLE...
echo  =============================================
echo.
echo  [1/3] Conectando na VPS e rodando deploy...
echo.

ssh -i "C:\Users\vando\Downloads\ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "cd ~/CLIEMTE-garrabot && git fetch origin && git reset --hard origin/main && pm2 restart garrabot && pm2 status"

echo.
echo  =============================================
echo    DEPLOY CONCLUIDO COM SUCESSO!
echo  =============================================
echo.
pause
