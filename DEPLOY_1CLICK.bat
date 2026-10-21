@echo off
title Deploy Garrabot - GitHub + Render
color 0A
echo.
echo  =============================================
echo    GARRABOT - COMMIT + DEPLOY RENDER
echo  =============================================
echo.

:: ── [1/2] Commit no GitHub ─────────────────────────────────────────────────
echo  [1/2] Enviando alteracoes para o GitHub...
echo.
cd /d "%~dp0"
git add -A
git diff --cached --quiet
if %errorlevel% == 0 (
    echo  Nenhuma alteracao nova para commitar. Continuando deploy...
) else (
    set /p MSG="  Mensagem do commit (Enter = automatica): "
    if "%MSG%"=="" set MSG=deploy: atualizacao automatica
    git commit -m "%MSG%"
    git push origin main
    echo  GitHub atualizado! O Render vai detectar e iniciar o deploy automaticamente.
)
echo.

:: ── [2/2] Concluido ────────────────────────────────────────────────────────
echo  =============================================
echo    TUDO PRONTO!
echo    - GitHub: atualizado
echo    - Render: deploy iniciado automaticamente
echo      (acompanhe em: https://dashboard.render.com)
echo  =============================================
echo.
pause
