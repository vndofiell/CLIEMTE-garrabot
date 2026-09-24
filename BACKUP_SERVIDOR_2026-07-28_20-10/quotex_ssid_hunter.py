# ═══════════════════════════════════════════════════════════════════════════════
# QUOTEX SSID HUNTER — Captura automática do cookie SSID via Chrome
# ═══════════════════════════════════════════════════════════════════════════════
import os
import threading
import time

_HUNTER_STATE: dict = {
    "status":    "idle",   # idle | abrindo | aguardando_login | capturado | erro
    "ssid":      "",
    "erro":      "",
    "driver":    None,
    "ts_inicio": 0,
}
_HUNTER_LOCK = threading.Lock()

_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "quotex_chrome_profile")


def ssid_hunter_status() -> dict:
    with _HUNTER_LOCK:
        return {
            "status":    _HUNTER_STATE["status"],
            "ssid":      _HUNTER_STATE["ssid"],
            "erro":      _HUNTER_STATE["erro"],
            "ts_inicio": _HUNTER_STATE["ts_inicio"],
        }


def ssid_hunter_parar():
    with _HUNTER_LOCK:
        driver = _HUNTER_STATE.get("driver")
        _HUNTER_STATE["driver"] = None
        _HUNTER_STATE["status"] = "idle"
        _HUNTER_STATE["ssid"]   = ""
    if driver:
        try:
            driver.quit()
        except Exception:
            pass


def _todos_cookies(driver) -> dict:
    """Retorna dict {nome: valor} de todos os cookies do domínio atual."""
    try:
        return {c["name"]: c["value"] for c in driver.get_cookies()}
    except Exception:
        return {}


def _hunter_thread():
    with _HUNTER_LOCK:
        _HUNTER_STATE["status"]    = "abrindo"
        _HUNTER_STATE["ssid"]      = ""
        _HUNTER_STATE["erro"]      = ""
        _HUNTER_STATE["ts_inicio"] = time.time()

    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        opts = Options()
        os.makedirs(_PROFILE_DIR, exist_ok=True)
        opts.add_argument(f"--user-data-dir={_PROFILE_DIR}")
        opts.add_argument("--profile-directory=Default")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1100,700")
        opts.add_argument("--window-position=100,50")

        service = Service()
        driver  = webdriver.Chrome(service=service, options=opts)
        driver.execute_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        with _HUNTER_LOCK:
            _HUNTER_STATE["driver"] = driver
            _HUNTER_STATE["status"] = "aguardando_login"

        # ── Abre página de login ──────────────────────────────────────────────
        driver.get("https://qxbroker.com/pt/sign-in")
        time.sleep(2)

        print("[SSID Hunter] 🌐 Chrome aberto — aguardando login do usuário...")

        timeout = 300
        inicio  = time.time()

        while time.time() - inicio < timeout:
            with _HUNTER_LOCK:
                if _HUNTER_STATE["status"] == "idle":
                    print("[SSID Hunter] ⛔ Cancelado.")
                    return

            try:
                url_atual = driver.current_url.lower()
            except Exception:
                break

            # ── Sucesso: usuário foi redirecionado para /trade após login ─────
            if "/trade" in url_atual:
                time.sleep(2)  # aguarda a página carregar window.settings

                token = ""

                # 1ª tentativa: window.settings.token (injetado pela Quotex no /trade)
                try:
                    token = driver.execute_script(
                        "return (window.settings && window.settings.token) ? window.settings.token : '';"
                    ) or ""
                    if token:
                        print(f"[SSID Hunter] 🎯 Token via window.settings: {token[:12]}...")
                except Exception:
                    pass

                # 2ª tentativa: localStorage
                if not token:
                    try:
                        token = driver.execute_script(
                            "return localStorage.getItem('token')"
                            "|| localStorage.getItem('ssid')"
                            "|| localStorage.getItem('authToken') || '';"
                        ) or ""
                        if token:
                            print(f"[SSID Hunter] 🗄️ Token via localStorage: {token[:12]}...")
                    except Exception:
                        pass

                # 3ª tentativa: cookie do documento
                if not token:
                    try:
                        cookies = _todos_cookies(driver)
                        token = cookies.get("token") or cookies.get("ssid") or ""
                        if token:
                            print(f"[SSID Hunter] 🍪 Token via cookie: {token[:12]}...")
                    except Exception:
                        pass

                if token and len(token) > 8:
                    print(f"[SSID Hunter] ✅ Token capturado! len={len(token)}")
                    with _HUNTER_LOCK:
                        _HUNTER_STATE["ssid"]   = token
                        _HUNTER_STATE["status"] = "capturado"
                    time.sleep(2)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    with _HUNTER_LOCK:
                        _HUNTER_STATE["driver"] = None
                    return
                else:
                    # Página ainda carregando — aguarda e tenta de novo
                    time.sleep(1)
                    continue

            time.sleep(1)

        raise TimeoutError("Login não detectado em 5 minutos.")

    except Exception as e:
        msg = str(e)
        print(f"[SSID Hunter] ❌ Erro: {msg}")
        with _HUNTER_LOCK:
            _HUNTER_STATE["status"] = "erro"
            _HUNTER_STATE["erro"]   = msg
            _HUNTER_STATE["driver"] = None
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def ssid_hunter_iniciar() -> dict:
    with _HUNTER_LOCK:
        st = _HUNTER_STATE["status"]
        if st in ("abrindo", "aguardando_login"):
            return {"ok": True, "status": st, "msg": "Já em andamento."}
    ssid_hunter_parar()
    t = threading.Thread(target=_hunter_thread, daemon=True, name="ssid-hunter")
    t.start()
    return {"ok": True, "status": "abrindo",
            "msg": "Chrome abrindo — faça login na Quotex e o SSID será capturado automaticamente."}
