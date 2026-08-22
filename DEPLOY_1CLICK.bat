@echo off
title Deploy Garrabot - GitHub + Oracle
color 0A
echo.
echo  =============================================
echo    GARRABOT — COMMIT + DEPLOY COMPLETO
echo  =============================================
echo.

:: ── [1/3] Commit no GitHub ─────────────────────────────────────────────────
echo  [1/3] Enviando alteracoes para o GitHub...
echo.
git add -A
git diff --cached --quiet
if %errorlevel% == 0 (
    echo  Nenhuma alteracao nova para commitar. Continuando deploy...
) else (
    set /p MSG="  Digite a mensagem do commit (ou Enter para mensagem automatica): "
    if "%MSG%"=="" set MSG=deploy: atualizacao automatica
    git commit -m "%MSG%"
    git push origin main
    echo  GitHub atualizado com sucesso!
)
echo.

:: ── [2/3] Deploy no Oracle ─────────────────────────────────────────────────
echo  [2/3] Conectando no Oracle e atualizando servidor...
echo.
ssh -i "%~dp0ssh-key-2026-07-26.key" -o StrictHostKeyChecking=no ubuntu@158.101.108.207 "cd ~/CLIEMTE-garrabot && git fetch origin && git restore --source=origin/main --staged --worktree $(git ls-files) && pm2 restart garrabot && pm2 status"
echo.

:: ── [3/3] Concluido ────────────────────────────────────────────────────────
echo  =============================================
echo    TUDO PRONTO!
echo    - GitHub: atualizado
echo    - Oracle: reiniciado e online
echo  =============================================
echo.
pause
