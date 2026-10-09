# ═══════════════════════════════════════════════════════════════════════════════
# POCKET OPTION CONNECTOR — Módulo de conexão com a corretora Pocket Option
# ═══════════════════════════════════════════════════════════════════════════════
#
# Fluxo principal:
#   1. pocket_capturar_ssid(email, senha) → login HTTP automático, extrai SSID
#   2. pocket_conectar(ssid, is_demo)     → conecta via WebSocket
#   3. pocket_operar(ativo, direcao, valor, duracao) → executa CALL/PUT
#   4. pocket_resultado(order_id)         → verifica WIN/LOSS por poll
#
# Captura automática de SSID (sem Selenium):
#   Estratégia 1: curl_cffi (impersonate Chrome) — mais confiável
#   Estratégia 2: requests padrão — fallback
#   Em ambos os casos faz login em pocketoption.com e extrai o token
#   da sessão WebSocket que vem embutido na página /cabinet
#
# Padrão idêntico ao quotex_connector.py:
#   - Estado global protegido por threading.Lock
#   - Loop asyncio dedicado em thread separada
#   - Reconexão automática em background
#   - API de operação compatível com o frontend existente
# ═══════════════════════════════════════════════════════════════════════════════

import threading
import time
import asyncio
import json
import os
import re
import traceback
import concurrent.futures

# ── Caminho do arquivo de configuração ────────────────────────────────────────
_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_PO_CFG_FILE = os.path.join(_BASE_DIR, "pocket_option_config.json")

# ── Estado global da conexão ──────────────────────────────────────────────────
_PO_STATE: dict = {
    "status":       "desconectado",   # desconectado | conectando | conectado | erro
    "ssid":         "",               # SSID de autenticação
    "is_demo":      True,             # True = demo, False = real
    "saldo":        0.0,
    "moeda":        "USD",
    "client":       None,             # instância AsyncPocketOptionClient
    "ts_conectado": 0,
    "erro":         "",
    "loop":         None,             # event loop da thread de conexão
    "thread":       None,             # thread que roda o loop
}
_PO_LOCK = threading.Lock()

# ── Estado global da captura de SSID ─────────────────────────────────────────
_PO_SSID_STATE: dict = {
    "status": "idle",      # idle | capturando | capturado | otp_necessario | erro
    "ssid":   "",
    "erro":   "",
    "ts":     0,
}
_PO_SSID_LOCK = threading.Lock()

# Cache de resultados de operações: {order_id: {"win": bool, "lucro": float, ...}}
_PO_RESULTADOS: dict = {}
_PO_RESULTADOS_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTÊNCIA DE CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════════════════════

def pocket_cfg_carregar() -> dict:
    """Lê a configuração salva em disco."""
    padrao = {"email": "", "senha": "", "ssid": "", "is_demo": True}
    try:
        if os.path.exists(_PO_CFG_FILE):
            with open(_PO_CFG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return padrao


def pocket_cfg_salvar(email: str = "", senha: str = "",
                      ssid: str = "", is_demo: bool = True) -> None:
    """Persiste a configuração em disco."""
    try:
        # Lê o que já existe para não sobrescrever campos não fornecidos
        atual = pocket_cfg_carregar()
        if email:
            atual["email"]   = email
        if senha:
            atual["senha"]   = senha
        if ssid:
            atual["ssid"]    = ssid
        atual["is_demo"] = is_demo
        with open(_PO_CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(atual, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[PocketOption] ⚠️ Erro ao salvar config: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CAPTURA AUTOMÁTICA DO SSID — Login HTTP (sem Selenium)
# ═══════════════════════════════════════════════════════════════════════════════
#
# A Pocket Option usa:
#   1. POST /login  (form ou JSON) → seta cookie de sessão
#   2. O cookie "io" ou "_session" contém o SSID para o WebSocket
#   3. A página /cabinet/user-settings tem o campo "token" no HTML
#
# ═══════════════════════════════════════════════════════════════════════════════

_PO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# URLs de login que a PocketOption usa (tenta em ordem)
_PO_LOGIN_URLS = [
    "https://pocketoption.com/en/login/",
    "https://pocketoption.com/pt/login/",
    "https://po.trade/en/login/",
]
_PO_CABINET_URLS = [
    "https://pocketoption.com/en/cabinet/",
    "https://pocketoption.com/pt/cabinet/",
]


def _po_extrair_ssid_do_html(html: str) -> str:
    """
    Extrai o token/SSID de HTML da Pocket Option.
    Tenta múltiplos padrões em ordem de confiabilidade.
    """
    padroes = [
        # Padrão principal: objeto JS com chave "session"
        r'["\']session["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        # Variantes de nome
        r'["\']token["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        r'["\']user_token["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        r'["\']auth_token["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        r'["\']ssid["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        # Formato completo do SSID WebSocket embutido na página
        r'42\[.auth.,\{[^}]*session["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        # data-attributes
        r'data-session=["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        r'data-token=["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        # window.settings
        r'window\.settings\s*=\s*\{[^}]*["\']session["\']\s*:\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        # Variável JS simples
        r'var\s+session\s*=\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
        r'let\s+session\s*=\s*["\']([a-zA-Z0-9%_\-\.~]{20,})["\']',
    ]
    for pat in padroes:
        m = re.search(pat, html)
        if m:
            tok = m.group(1)
            # Remove URL-encoding se necessário
            try:
                import urllib.parse
                tok = urllib.parse.unquote(tok)
            except Exception:
                pass
            if len(tok) >= 20:
                return tok
    return ""


def _po_extrair_ssid_dos_cookies(session_obj) -> str:
    """
    Extrai o SSID do cookie de sessão da Pocket Option.
    O cookie pode se chamar 'io', '_session', 'PHPSESSID', 'po_session', etc.
    """
    # Nomes conhecidos do cookie de sessão da PocketOption
    nomes_cookie = ["io", "po_session", "session", "_session", "PHPSESSID",
                    "pocket_session", "laravel_session", "remember_web"]
    try:
        jar = session_obj.cookies
        # Tenta pelo nome
        for nome in nomes_cookie:
            val = jar.get(nome)
            if val and len(val) >= 20:
                return val
        # Pega qualquer cookie que pareça um token (>= 20 chars alfanuméricos)
        for c in jar:
            v = c.value if hasattr(c, 'value') else str(c)
            if v and len(v) >= 32 and re.match(r'^[a-zA-Z0-9%_\-\.~]+$', v):
                return v
    except Exception:
        pass
    return ""


def _po_montar_ssid_completo(session: str, is_demo: bool, uid: int = 0) -> str:
    """
    Monta o SSID no formato completo que a API aceita:
      42["auth",{"session":"...","isDemo":1,"uid":0,"platform":2}]
    """
    return json.dumps(
        ["auth", {
            "session":  session,
            "isDemo":   1 if is_demo else 0,
            "uid":      uid,
            "platform": 2,
        }],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _po_tentar_login_json(s, email: str, senha: str, login_url: str) -> tuple:
    """
    Tenta login via API JSON (modo preferido da Pocket Option).
    Retorna (ok, session_token, uid, erro).
    """
    # Endpoint de API JSON
    api_endpoints = [
        login_url.replace("/login/", "/api/v1/login"),
        login_url.replace("/login/", "/api/login"),
        "https://pocketoption.com/api/v1/login",
        "https://pocketoption.com/api/login",
    ]
    for ep in api_endpoints:
        try:
            r = s.post(
                ep,
                json={"email": email, "password": senha},
                headers={
                    "Content-Type": "application/json",
                    "Accept":       "application/json",
                    "Referer":      login_url,
                },
                timeout=15,
            )
            if r.status_code in (200, 201):
                j = r.json() if hasattr(r, 'json') else {}
                tok = (j.get("session") or j.get("token") or j.get("ssid")
                       or (j.get("data") or {}).get("session", "")
                       or (j.get("data") or {}).get("token", ""))
                uid = int(j.get("uid") or j.get("user_id") or
                          (j.get("data") or {}).get("uid", 0) or 0)
                if tok and len(tok) >= 20:
                    return True, tok, uid, ""
        except Exception:
            continue
    return False, "", 0, "api_json_falhou"


def _po_capturar_curl_cffi(email: str, senha: str) -> tuple:
    """
    Faz login na Pocket Option usando curl_cffi (impersonate Chrome).
    Retorna (ok, session_token, uid, erro).

    Estratégias em ordem:
      1. API JSON  (/api/v1/login)
      2. Form POST (/login/)
      3. Cookie de sessão após login
      4. HTML do /cabinet
    """
    try:
        from curl_cffi import requests as _creqs

        s = _creqs.Session(impersonate="chrome124")
        s.headers.update({
            "User-Agent":      _PO_UA,
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin":          "https://pocketoption.com",
        })

        uid = 0

        # ── Tenta todos os URLs de login ─────────────────────────────────────
        login_url_usado = None
        for login_url in _PO_LOGIN_URLS:
            try:
                # 1. Carrega a página para pegar cookies e CSRF
                csrf = ""
                try:
                    pg  = s.get(login_url, timeout=12)
                    for pat_csrf in [r'name="_token"\s+value="([^"]+)"',
                                     r'"_token"\s*:\s*"([^"]+)"',
                                     r'csrf[_-]token["\s]*[=:]["\s]*([a-zA-Z0-9+/=]{20,})'
                                    ]:
                        mc = re.search(pat_csrf, pg.text)
                        if mc:
                            csrf = mc.group(1)
                            break
                except Exception:
                    pass

                # 2. Estratégia A — API JSON
                ok_j, tok_j, uid_j, _ = _po_tentar_login_json(s, email, senha, login_url)
                if ok_j and tok_j:
                    return True, tok_j, uid_j, ""

                # 3. Estratégia B — Form POST (application/x-www-form-urlencoded)
                payload = {"email": email, "password": senha, "remember": "1"}
                if csrf:
                    payload["_token"] = csrf

                resp = s.post(
                    login_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer":      login_url,
                    },
                    timeout=20,
                    allow_redirects=True,
                )

                tl = resp.text.lower()
                if "two-factor" in tl or "2fa" in tl or resp.status_code == 422:
                    return False, "", 0, "OTP_REQUIRED"

                # Tenta JSON da resposta
                try:
                    j = resp.json()
                    tok = (j.get("session") or j.get("token") or j.get("ssid")
                           or (j.get("data") or {}).get("session", ""))
                    uid = int(j.get("uid") or j.get("user_id") or 0)
                    if tok and len(tok) >= 20:
                        return True, tok, uid, ""
                except Exception:
                    pass

                login_url_usado = login_url
                break  # avança para extração pós-login

            except Exception:
                continue

        if not login_url_usado:
            return False, "", 0, "Falha ao carregar página de login."

        # ── 4. Cookie de sessão após login ────────────────────────────────────
        tok_cookie = _po_extrair_ssid_dos_cookies(s)
        if tok_cookie:
            print(f"[PocketOption] 🍪 SSID extraído do cookie! len={len(tok_cookie)}")
            return True, tok_cookie, uid, ""

        # ── 5. HTML do /cabinet ───────────────────────────────────────────────
        for cab_url in _PO_CABINET_URLS:
            try:
                cab = s.get(cab_url, timeout=15)
                tok = _po_extrair_ssid_do_html(cab.text)
                if tok:
                    m_uid = re.search(r'["\']uid["\']\s*:\s*(\d+)', cab.text)
                    if m_uid:
                        uid = int(m_uid.group(1))
                    return True, tok, uid, ""
                # Tenta extrair cookie após carregar /cabinet
                tok_cookie = _po_extrair_ssid_dos_cookies(s)
                if tok_cookie:
                    return True, tok_cookie, uid, ""
            except Exception:
                continue

        # ── 6. Último fallback: página de trade ───────────────────────────────
        try:
            trade = s.get("https://pocketoption.com/en/cabinet/demo-quick-high-low/", timeout=15)
            tok = _po_extrair_ssid_do_html(trade.text)
            if tok:
                return True, tok, uid, ""
            tok_cookie = _po_extrair_ssid_dos_cookies(s)
            if tok_cookie:
                return True, tok_cookie, uid, ""
        except Exception:
            pass

        # Debug: loga os cookies disponíveis para diagnóstico
        try:
            cookies_nomes = [c.name for c in s.cookies] if hasattr(s.cookies, '__iter__') else list(s.cookies.keys())
            print(f"[PocketOption] 🔍 Cookies disponíveis: {cookies_nomes}")
        except Exception:
            pass

        return False, "", 0, "Token não encontrado. Tente colar o SSID manualmente."

    except ImportError:
        return False, "", 0, "curl_cffi_indisponivel"
    except Exception as e:
        return False, "", 0, str(e)


def _po_capturar_requests(email: str, senha: str) -> tuple:
    """
    Fallback com requests padrão (sem impersonate).
    Retorna (ok, session_token, uid, erro).
    """
    try:
        import requests as _req

        s = _req.Session()
        s.headers.update({
            "User-Agent":      _PO_UA,
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Origin":          "https://pocketoption.com",
        })

        uid = 0
        for login_url in _PO_LOGIN_URLS:
            try:
                # CSRF
                csrf = ""
                pg = s.get(login_url, timeout=12)
                for pat_csrf in [r'name="_token"\s+value="([^"]+)"',
                                  r'"_token"\s*:\s*"([^"]+)"']:
                    mc = re.search(pat_csrf, pg.text)
                    if mc:
                        csrf = mc.group(1)
                        break

                # API JSON primeiro
                ok_j, tok_j, uid_j, _ = _po_tentar_login_json(s, email, senha, login_url)
                if ok_j and tok_j:
                    return True, tok_j, uid_j, ""

                # Form POST
                payload = {"email": email, "password": senha, "remember": "1"}
                if csrf:
                    payload["_token"] = csrf
                resp = s.post(
                    login_url, data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Referer": login_url},
                    timeout=20, allow_redirects=True,
                )
                tl = resp.text.lower()
                if "two-factor" in tl or "2fa" in tl or resp.status_code == 422:
                    return False, "", 0, "OTP_REQUIRED"
                break
            except Exception:
                continue

        # Cookie
        tok_c = _po_extrair_ssid_dos_cookies(s)
        if tok_c:
            return True, tok_c, uid, ""

        # HTML cabinet
        for cab_url in _PO_CABINET_URLS:
            try:
                cab = s.get(cab_url, timeout=15)
                tok = _po_extrair_ssid_do_html(cab.text)
                if tok:
                    return True, tok, uid, ""
                tok_c = _po_extrair_ssid_dos_cookies(s)
                if tok_c:
                    return True, tok_c, uid, ""
            except Exception:
                continue

        return False, "", 0, "Token não encontrado. Tente colar o SSID manualmente."

    except Exception as e:
        return False, "", 0, str(e)


def _po_atualizar_ssid_state(ssid: str, status: str = "capturado", erro: str = ""):
    """Atualiza o estado global de captura de SSID."""
    with _PO_SSID_LOCK:
        _PO_SSID_STATE.update({
            "status": status,
            "ssid":   ssid,
            "erro":   erro,
            "ts":     time.time(),
        })


def pocket_ssid_status() -> dict:
    """Retorna o estado atual da captura de SSID."""
    with _PO_SSID_LOCK:
        return dict(_PO_SSID_STATE)


def pocket_ssid_definir(ssid: str):
    """Permite que fontes externas (frontend) registrem um SSID capturado."""
    _po_atualizar_ssid_state(ssid, status="capturado", erro="")
    # Salva em disco
    cfg = pocket_cfg_carregar()
    cfg["ssid"] = ssid
    pocket_cfg_salvar(ssid=ssid, is_demo=cfg.get("is_demo", True))


def pocket_capturar_ssid(email: str, senha: str) -> dict:
    """
    Captura o SSID da Pocket Option de forma automática usando múltiplas
    estratégias em cascata (sem Selenium):

      1. curl_cffi (impersonate Chrome) — mais confiável, evita bloqueios
      2. requests padrão — fallback simples

    Retorna dict com { ok, ssid, metodo, erro }.
    Atualiza _PO_SSID_STATE com o resultado.
    """
    _po_atualizar_ssid_state("", status="capturando", erro="")
    print(f"[PocketOption] 🔐 Iniciando captura SSID para {email}...")

    is_demo = pocket_cfg_carregar().get("is_demo", True)

    # ── Estratégia 1: curl_cffi ──────────────────────────────────────────────
    ok, session, uid, erro = _po_capturar_curl_cffi(email, senha)
    if ok and session:
        ssid = _po_montar_ssid_completo(session, is_demo, uid)
        print(f"[PocketOption] ✅ SSID capturado via curl_cffi! session_len={len(session)}")
        _po_atualizar_ssid_state(ssid, status="capturado")
        pocket_cfg_salvar(email=email, senha=senha, ssid=ssid, is_demo=is_demo)
        return {"ok": True, "ssid": ssid, "metodo": "curl_cffi"}

    if erro == "OTP_REQUIRED":
        _po_atualizar_ssid_state("", status="otp_necessario", erro="OTP_REQUIRED")
        return {"ok": False, "ssid": "", "erro": "OTP_REQUIRED", "otp": True}

    print(f"[PocketOption] ⚠️ curl_cffi falhou ({erro}). Tentando requests...")

    # ── Estratégia 2: requests padrão ────────────────────────────────────────
    ok2, session2, uid2, erro2 = _po_capturar_requests(email, senha)
    if ok2 and session2:
        ssid2 = _po_montar_ssid_completo(session2, is_demo, uid2)
        print(f"[PocketOption] ✅ SSID capturado via requests! session_len={len(session2)}")
        _po_atualizar_ssid_state(ssid2, status="capturado")
        pocket_cfg_salvar(email=email, senha=senha, ssid=ssid2, is_demo=is_demo)
        return {"ok": True, "ssid": ssid2, "metodo": "requests"}

    if erro2 == "OTP_REQUIRED":
        _po_atualizar_ssid_state("", status="otp_necessario", erro="OTP_REQUIRED")
        return {"ok": False, "ssid": "", "erro": "OTP_REQUIRED", "otp": True}

    # ── Todas as estratégias falharam ────────────────────────────────────────
    motivo_final = erro2 or erro or "Todas as estratégias de captura falharam."
    print(f"[PocketOption] ❌ Captura SSID falhou: {motivo_final}")
    _po_atualizar_ssid_state("", status="erro", erro=motivo_final)
    return {"ok": False, "ssid": "", "erro": motivo_final}


# ═══════════════════════════════════════════════════════════════════════════════
# IMPORTAÇÃO DA API
# ═══════════════════════════════════════════════════════════════════════════════

try:
    import sys as _sys
    _api_path = os.path.join(_BASE_DIR, "PocketOptionAPI-main")
    if _api_path not in _sys.path:
        _sys.path.insert(0, _api_path)
    from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection
    _PO_API_DISPONIVEL = True
    print("[PocketOption] ✅ pocketoptionapi_async carregado com sucesso.")
except ImportError as _e:
    _PO_API_DISPONIVEL = False
    print(f"[PocketOption] ⚠️ pocketoptionapi_async não disponível: {_e}")


# ═══════════════════════════════════════════════════════════════════════════════
# LOOP ASYNCIO DEDICADO
# ═══════════════════════════════════════════════════════════════════════════════

def _po_loop_thread(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _po_get_loop() -> asyncio.AbstractEventLoop:
    """Retorna (e cria se necessário) o loop asyncio dedicado."""
    with _PO_LOCK:
        loop   = _PO_STATE.get("loop")
        thread = _PO_STATE.get("thread")
        if loop is None or not loop.is_running():
            loop   = asyncio.new_event_loop()
            thread = threading.Thread(target=_po_loop_thread, args=(loop,), daemon=True)
            thread.start()
            _PO_STATE["loop"]   = loop
            _PO_STATE["thread"] = thread
    return loop


def _run_async(coro, timeout: float = 15.0):
    loop = _po_get_loop()
    fut  = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


# ═══════════════════════════════════════════════════════════════════════════════
# CONEXÃO PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

def pocket_conectado() -> bool:
    with _PO_LOCK:
        return _PO_STATE["status"] == "conectado" and _PO_STATE["client"] is not None


def pocket_status() -> dict:
    with _PO_LOCK:
        return {
            "status":       _PO_STATE["status"],
            "ssid_ok":      bool(_PO_STATE["ssid"]),
            "is_demo":      _PO_STATE["is_demo"],
            "saldo":        _PO_STATE["saldo"],
            "moeda":        _PO_STATE["moeda"],
            "ts_conectado": _PO_STATE["ts_conectado"],
            "erro":         _PO_STATE["erro"],
        }


async def _conectar_async(ssid: str, is_demo: bool):
    try:
        with _PO_LOCK:
            _PO_STATE["status"] = "conectando"
            _PO_STATE["erro"]   = ""

        client = AsyncPocketOptionClient(
            ssid=ssid,
            is_demo=is_demo,
            enable_logging=False,
            auto_reconnect=True,
        )
        ok = await client.connect()

        if not ok:
            with _PO_LOCK:
                _PO_STATE["status"] = "erro"
                _PO_STATE["erro"]   = "Conexão recusada pela Pocket Option."
            return

        try:
            balance = await client.get_balance()
            saldo   = float(balance.balance)
            moeda   = str(balance.currency)
        except Exception:
            saldo, moeda = 0.0, "USD"

        with _PO_LOCK:
            _PO_STATE["status"]       = "conectado"
            _PO_STATE["client"]       = client
            _PO_STATE["saldo"]        = saldo
            _PO_STATE["moeda"]        = moeda
            _PO_STATE["ts_conectado"] = time.time()
            _PO_STATE["erro"]         = ""

        tipo = "DEMO" if is_demo else "REAL"
        print(f"[PocketOption] ✅ Conectado ({tipo}) | Saldo: {moeda} {saldo:.2f}")

    except Exception as e:
        msg = str(e)
        with _PO_LOCK:
            _PO_STATE["status"] = "erro"
            _PO_STATE["erro"]   = msg
        print(f"[PocketOption] ❌ Erro na conexão: {msg}")
        traceback.print_exc()


def _pocket_desconectar_sync():
    with _PO_LOCK:
        client = _PO_STATE.get("client")
        loop   = _PO_STATE.get("loop")
        _PO_STATE["client"] = None
        _PO_STATE["status"] = "desconectado"

    if client and loop and loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            fut.result(timeout=5.0)
        except Exception:
            pass


def pocket_desconectar() -> dict:
    _pocket_desconectar_sync()
    print("[PocketOption] 🔌 Desconectado.")
    return {"ok": True, "msg": "Desconectado da Pocket Option."}


def pocket_conectar(ssid: str = "", is_demo: bool = True,
                    email: str = "", senha: str = "") -> dict:
    """
    Conecta à Pocket Option.

    Se ssid for fornecido: conecta diretamente.
    Se ssid for vazio mas email+senha forem fornecidos:
        captura o SSID automaticamente e conecta.

    Retorna dict com ok, status, saldo ou erro.
    """
    if not _PO_API_DISPONIVEL:
        return {"ok": False, "erro": "pocketoptionapi_async não instalado."}

    ssid  = (ssid  or "").strip()
    email = (email or "").strip()
    senha = (senha or "").strip()

    # ── Auto-captura se SSID não fornecido ───────────────────────────────────
    if not ssid and email and senha:
        print("[PocketOption] 🔄 SSID não fornecido — capturando automaticamente...")
        resultado_ssid = pocket_capturar_ssid(email, senha)
        if resultado_ssid.get("ok") and resultado_ssid.get("ssid"):
            ssid = resultado_ssid["ssid"]
            print(f"[PocketOption] ✅ SSID capturado automaticamente.")
        elif resultado_ssid.get("otp"):
            return {"ok": False, "otp": True, "erro": "OTP necessário."}
        else:
            return {"ok": False, "erro": resultado_ssid.get("erro", "Falha na captura do SSID.")}

    if not ssid:
        return {"ok": False, "erro": "SSID ou email+senha são obrigatórios."}

    # Desconecta sessão anterior
    _pocket_desconectar_sync()

    # Persiste
    pocket_cfg_salvar(email=email, senha=senha, ssid=ssid, is_demo=is_demo)

    with _PO_LOCK:
        _PO_STATE["ssid"]    = ssid
        _PO_STATE["is_demo"] = is_demo

    try:
        _run_async(_conectar_async(ssid, is_demo), timeout=20.0)
    except concurrent.futures.TimeoutError:
        with _PO_LOCK:
            _PO_STATE["status"] = "erro"
            _PO_STATE["erro"]   = "Timeout ao conectar (20s)."
        return {"ok": False, "erro": "Timeout ao conectar."}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    with _PO_LOCK:
        ok    = _PO_STATE["status"] == "conectado"
        saldo = _PO_STATE["saldo"]
        erro  = _PO_STATE["erro"]

    return {
        "ok":     ok,
        "status": "conectado" if ok else "erro",
        "saldo":  saldo,
        "erro":   "" if ok else erro,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SALDO
# ═══════════════════════════════════════════════════════════════════════════════

def pocket_get_saldo() -> dict:
    if not pocket_conectado():
        return {"ok": False, "erro": "Não conectado à Pocket Option."}
    with _PO_LOCK:
        client = _PO_STATE["client"]
    try:
        balance = _run_async(client.get_balance(), timeout=10.0)
        saldo   = float(balance.balance)
        moeda   = str(balance.currency)
        with _PO_LOCK:
            _PO_STATE["saldo"] = saldo
            _PO_STATE["moeda"] = moeda
        return {"ok": True, "saldo": saldo, "moeda": moeda}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# OPERAÇÕES
# ═══════════════════════════════════════════════════════════════════════════════

def pocket_operar(ativo: str, direcao: str, valor: float, duracao: int = 60) -> dict:
    """
    Executa uma operação binária na Pocket Option.
    direcao: "call" | "put"
    """
    if not _PO_API_DISPONIVEL:
        return {"ok": False, "erro": "pocketoptionapi_async não instalado."}
    if not pocket_conectado():
        return {"ok": False, "erro": "Não conectado à Pocket Option."}

    ativo   = (ativo   or "").strip()
    direcao = (direcao or "").strip().lower()
    duracao = max(5, int(duracao))

    if not ativo:
        return {"ok": False, "erro": "Campo 'ativo' obrigatório."}
    if direcao not in ("call", "put"):
        return {"ok": False, "erro": "Campo 'direcao' deve ser 'call' ou 'put'."}
    if valor <= 0:
        return {"ok": False, "erro": "Campo 'valor' deve ser maior que zero."}

    with _PO_LOCK:
        client = _PO_STATE["client"]

    direction = OrderDirection.CALL if direcao == "call" else OrderDirection.PUT

    try:
        order = _run_async(
            client.place_order(
                asset=ativo,
                amount=float(valor),
                direction=direction,
                duration=duracao,
            ),
            timeout=15.0,
        )
        order_id = str(order.order_id)
        print(f"[PocketOption] 🎯 Operação | ID={order_id} | {ativo} {direcao.upper()} ${valor:.2f} {duracao}s")
        return {"ok": True, "id": order_id, "order_id": order_id,
                "ativo": ativo, "direcao": direcao, "valor": valor, "duracao": duracao}
    except Exception as e:
        msg = str(e)
        print(f"[PocketOption] ❌ Erro ao operar: {msg}")
        return {"ok": False, "erro": msg}


async def _verificar_resultado_async(client, order_id: str, duracao_s: int):
    await asyncio.sleep(duracao_s + 3)
    try:
        return await client.check_win(order_id)
    except Exception:
        return None


def pocket_resultado_iniciar(order_id: str, duracao_s: int = 60):
    """Inicia verificação do resultado em background."""
    if not pocket_conectado():
        return
    with _PO_LOCK:
        client = _PO_STATE["client"]

    def _bg():
        try:
            result = _run_async(
                _verificar_resultado_async(client, order_id, duracao_s),
                timeout=duracao_s + 15.0,
            )
            if result is not None:
                lucro = float(getattr(result, "profit", None) or
                              (result.get("profit") if isinstance(result, dict) else 0) or 0.0)
                win   = lucro > 0
                with _PO_RESULTADOS_LOCK:
                    _PO_RESULTADOS[order_id] = {
                        "ok": True, "order_id": order_id,
                        "win": win, "lucro": lucro,
                        "status": "win" if win else "lose",
                    }
                print(f"[PocketOption] {'✅ WIN' if win else '❌ LOSE'} | ID={order_id} | lucro={lucro:.2f}")
        except Exception as e:
            with _PO_RESULTADOS_LOCK:
                _PO_RESULTADOS[order_id] = {"ok": False, "order_id": order_id, "erro": str(e)}
            print(f"[PocketOption] ⚠️ Erro ao verificar resultado {order_id}: {e}")

    threading.Thread(target=_bg, daemon=True).start()


def pocket_resultado(order_id: str) -> dict:
    """Poll de resultado. Retorna {"ok": False, "pendente": True} enquanto aguarda."""
    with _PO_RESULTADOS_LOCK:
        res = _PO_RESULTADOS.get(order_id)
    if res is None:
        return {"ok": False, "pendente": True, "order_id": order_id}
    return res
