"""
Configuração do Gunicorn para o Bot Garra no Render.
Usa post_fork hook para iniciar threads daemon DEPOIS do fork do worker,
garantindo que o polling do Telegram funcione corretamente.
"""
import threading
import os

# ── Configurações de servidor ────────────────────────────────────────────────
bind        = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers     = 1
threads     = 4
timeout     = 120
worker_class = "gthread"
# SEM --preload: módulo carregado em cada worker, threads sobrevivem ao fork

def post_fork(server, worker):
    """Executado em cada worker APÓS o fork — momento seguro para iniciar threads."""
    pid = os.getpid()
    server.log.info(f"[Gunicorn] Worker {pid} iniciado. Iniciando thread Telegram...")
    try:
        import main as _app_module
        fn = getattr(_app_module, "_tg_garantir_controller", None)
        if fn:
            fn()
            server.log.info(f"[Gunicorn] Thread Telegram iniciado no worker {pid}")
        else:
            server.log.warning(f"[Gunicorn] _tg_garantir_controller não encontrado no worker {pid}")
    except Exception as e:
        server.log.error(f"[Gunicorn] Erro ao iniciar thread Telegram: {e}")
