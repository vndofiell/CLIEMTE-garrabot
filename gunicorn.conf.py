"""
Gunicorn config — Bot Garra (Render)
Usa --preload para carregar o módulo 1x só, evitando múltiplos threads de polling.
post_fork reinicia o thread de polling dentro do worker correto.
"""
import os
import threading

bind         = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers      = 1
threads      = 4
timeout      = 120
worker_class = "gthread"
preload_app  = True   # carrega o módulo 1x no master — sem múltiplos pollings

def post_fork(server, worker):
    """
    Executado no processo FILHO após o fork.
    Threads do processo pai morreram — reinicia o polling aqui.
    """
    pid = os.getpid()
    server.log.info(f"[Gunicorn post_fork] Worker PID={pid} — reiniciando thread Telegram...")
    try:
        # Remove lockfile antigo do processo pai (se existir)
        import main as _m
        lf_path = getattr(_m, "_TG_LOCKFILE", None)
        if lf_path and os.path.exists(lf_path):
            try:
                os.remove(lf_path)
            except Exception:
                pass
        # Força reinício do polling neste worker
        fn = getattr(_m, "_tg_garantir_controller", None)
        if fn:
            fn()
            server.log.info(f"[Gunicorn post_fork] Thread Telegram OK no PID={pid}")
        else:
            server.log.warning(f"[Gunicorn post_fork] _tg_garantir_controller não encontrado!")
    except Exception as e:
        server.log.error(f"[Gunicorn post_fork] Erro: {e}")
