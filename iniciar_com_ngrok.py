"""
INICIADOR DO BOT GARRA COM NGROK
- Inicia o bot Flask
- Inicia o ngrok
- Pega a URL pública e mostra na tela
- Fica monitorando e reinicia se cair
"""
import subprocess
import time
import requests
import sys
import os
import signal

PYTHON   = sys.executable
PASTA    = os.path.dirname(os.path.abspath(__file__))
NGROK    = os.path.join(PASTA, "ngrok.exe")
BOT_LOG  = os.path.join(PASTA, "bot_log.txt")
BOT_ERR  = os.path.join(PASTA, "bot_err.txt")

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"]       = "1"

bot_proc   = None
ngrok_proc = None

def iniciar_bot():
    global bot_proc
    print("[BOT] Iniciando Flask...")
    bot_proc = subprocess.Popen(
        [PYTHON, "-u", "main.py"],
        cwd=PASTA,
        stdout=open(BOT_LOG, "w", encoding="utf-8"),
        stderr=open(BOT_ERR, "w", encoding="utf-8"),
    )
    print(f"[BOT] PID: {bot_proc.pid}")

def iniciar_ngrok():
    global ngrok_proc
    print("[NGROK] Iniciando túnel na porta 5000...")
    ngrok_proc = subprocess.Popen(
        [NGROK, "http", "5000"],
        cwd=PASTA,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[NGROK] PID: {ngrok_proc.pid}")

def pegar_url():
    for _ in range(15):
        try:
            r = requests.get("http://localhost:4040/api/tunnels", timeout=3)
            tunnels = r.json().get("tunnels", [])
            for t in tunnels:
                if t.get("proto") == "https":
                    return t["public_url"]
        except Exception:
            pass
        time.sleep(1)
    return None

def encerrar(sig=None, frame=None):
    print("\n[ENCERRAR] Parando bot e ngrok...")
    if bot_proc:   bot_proc.terminate()
    if ngrok_proc: ngrok_proc.terminate()
    sys.exit(0)

signal.signal(signal.SIGINT,  encerrar)
signal.signal(signal.SIGTERM, encerrar)

# ── Inicia tudo ──────────────────────────────────────────────────────────────
iniciar_bot()
time.sleep(4)  # aguarda Flask subir

iniciar_ngrok()
time.sleep(3)  # aguarda ngrok conectar

url = pegar_url()
if url:
    print(f"\n{'='*60}")
    print(f"  BOT GARRA ONLINE!")
    print(f"  Acesse: {url}/login")
    print(f"{'='*60}\n")
else:
    print("[ERRO] Não foi possível obter URL do ngrok")

# ── Watchdog: reinicia se bot ou ngrok caírem ────────────────────────────────
print("[WATCHDOG] Monitorando... (Ctrl+C para parar)\n")
while True:
    time.sleep(10)

    # Verifica bot
    if bot_proc and bot_proc.poll() is not None:
        print("[WATCHDOG] Bot caiu! Reiniciando...")
        iniciar_bot()
        time.sleep(4)

    # Verifica ngrok
    if ngrok_proc and ngrok_proc.poll() is not None:
        print("[WATCHDOG] Ngrok caiu! Reiniciando...")
        iniciar_ngrok()
        time.sleep(3)
        nova_url = pegar_url()
        if nova_url:
            print(f"\n[WATCHDOG] Nova URL: {nova_url}/login\n")
