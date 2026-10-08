# ═══════════════════════════════════════════════════════════════════════════════
# QUOTEX CONNECTOR — Módulo de conexão com a corretora Quotex
# ═══════════════════════════════════════════════════════════════════════════════
#
# Fluxo principal:
#   1. quotex_capturar_ssid(email, senha) → faz login HTTP e extrai o SSID
#   2. quotex_conectar(email, senha, tipo_conta, ssid) → conecta via WebSocket
#
# A captura automática do SSID usa curl_cffi (impersonate Chrome) e fallback
# para requests padrão. Sem Selenium — funciona em servidores headless.
#
# Padrão idêntico ao módulo Deriv do main.py:
#   - Estado global protegido por threading.Lock
#   - Reconexão automática em background
#   - API de operação (call/put) compatível com o frontend existente
# ═══════════════════════════════════════════════════════════════════════════════

import threading
import time
import asyncio
import json
import os
import re
import traceback

# ── Monkey-patch pyquotex: garante que offset=None nunca cause timedelta crash ──
try:
    import pyquotex.expiration as _qx_exp
    _orig_get_next_timeframe = _qx_exp.get_next_timeframe
    _orig_get_server_timer   = _qx_exp.get_server_timer

    def _safe_get_next_timeframe(timestamp, time_zone, timeframe, open_time=None):
        return _orig_get_next_timeframe(timestamp, time_zone or 0, timeframe, open_time)

    def _safe_get_server_timer(time_offset_seconds):
        return _orig_get_server_timer(time_offset_seconds or 0)

    _qx_exp.get_next_timeframe = _safe_get_next_timeframe
    _qx_exp.get_server_timer   = _safe_get_server_timer

    # Também patch no api.py para offset nunca ficar None
    import pyquotex.api as _qx_api
    _orig_profile_setter = _qx_api.QuotexAPI.profile if hasattr(_qx_api, 'QuotexAPI') else None
except Exception:
    pass

# ── Estado global da conexão ──────────────────────────────────────────────────
_QUOTEX_STATE: dict = {
    "status":           "desconectado",   # desconectado | conectando | conectado | erro
    "email":            "",
    "senha":            "",
    "tipo_conta":       "DEMO",           # DEMO | REAL
    "saldo":            0.0,
    "moeda":            "USD",
    "client":           None,             # instância QuotexAPI
    "ts_conectado":     0,
    "erro":             "",
    "loop":             None,             # event loop da thread de conexão
    "_falhas_balance":  0,                # contador de falhas consecutivas em get_balance
}
_QUOTEX_LOCK = threading.Lock()

# ── Estado global da captura SSID ─────────────────────────────────────────────
_SSID_CAPTURE_STATE: dict = {
    "status":  "idle",      # idle | capturando | capturado | erro
    "ssid":    "",
    "erro":    "",
    "ts":      0,
}
_SSID_CAPTURE_LOCK = threading.Lock()

# Arquivo de configuração salva
_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
_QUOTEX_CFG_FILE = os.path.join(_BASE_DIR, "quotex_config.json")


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTÊNCIA DE CREDENCIAIS
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_cfg_carregar() -> dict:
    """Carrega email/senha/tipo_conta/ssid salvos em disco."""
    padrao = {"email": "", "senha": "", "tipo_conta": "DEMO", "ssid": ""}
    try:
        if os.path.exists(_QUOTEX_CFG_FILE):
            with open(_QUOTEX_CFG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return padrao


def quotex_cfg_salvar(email: str, senha: str, tipo_conta: str = "DEMO",
                      ssid: str = "") -> None:
    """Salva credenciais da Quotex em disco."""
    try:
        with open(_QUOTEX_CFG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "email":      email,
                "senha":      senha,
                "tipo_conta": tipo_conta,
                "ssid":       ssid,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# CAPTURA AUTOMÁTICA DO SSID
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_ssid_status() -> dict:
    """Retorna o estado atual da captura de SSID."""
    with _SSID_CAPTURE_LOCK:
        return {
            "status": _SSID_CAPTURE_STATE["status"],
            "ssid":   _SSID_CAPTURE_STATE["ssid"],
            "erro":   _SSID_CAPTURE_STATE["erro"],
            "ts":     _SSID_CAPTURE_STATE["ts"],
        }


def _ssid_extrair_do_html(html: str) -> str:
    """Tenta extrair o token SSID de uma página HTML da Quotex."""
    # 1ª tentativa: window.settings.token (objeto JS embutido na página)
    m = re.search(r'window\.settings\s*=\s*(\{[^<]+?\})\s*;', html)
    if m:
        try:
            settings = json.loads(m.group(1))
            tok = settings.get("token", "")
            if tok and len(tok) >= 16:
                return tok
        except Exception:
            pass

    # 2ª tentativa: regex direto no JSON embutido
    m2 = re.search(r'"token"\s*:\s*"([a-zA-Z0-9_\-\.]{20,})"', html)
    if m2:
        tok = m2.group(1)
        if len(tok) >= 20:
            return tok

    return ""


def _ssid_capturar_curl_cffi(email: str, senha: str,
                               user_ip: str = "") -> tuple[bool, str, str]:
    """
    Faz login na Quotex usando curl_cffi (impersonate Chrome).
    Retorna (ok, ssid, erro).
    """
    try:
        from curl_cffi import requests as _creqs

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

        s = _creqs.Session(impersonate="chrome120")
        s.headers.update({
            "User-Agent":      ua,
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        if user_ip:
            s.headers.update({
                "X-Forwarded-For": user_ip,
                "X-Real-IP":       user_ip,
            })

        # 1. Obtém CSRF token
        csrf = ""
        try:
            pg   = s.get("https://qxbroker.com/pt/sign-in", timeout=15)
            m    = re.search(r'name="_token"\s+value="([^"]+)"', pg.text)
            if m:
                csrf = m.group(1)
        except Exception:
            pass

        # 2. POST de login
        payload = {"email": email, "password": senha}
        if csrf:
            payload["_token"] = csrf

        resp = s.post(
            "https://qxbroker.com/pt/sign-in",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      "https://qxbroker.com/pt/sign-in",
                "Origin":       "https://qxbroker.com",
            },
            timeout=20,
            allow_redirects=True,
        )

        # 3. Detecta OTP
        if ("otp" in resp.text.lower() or "two" in resp.text.lower()
                or resp.status_code == 422):
            return False, "", "OTP_REQUIRED"

        # 4. Tenta extrair token da resposta JSON
        try:
            j = resp.json()
            tok = j.get("token") or j.get("ssid") or (j.get("data") or {}).get("token", "")
            if tok and len(tok) >= 16:
                return True, tok, ""
        except Exception:
            pass

        # 5. Acessa /trade e extrai token do HTML
        try:
            trade = s.get("https://qxbroker.com/pt/trade", timeout=15)
            tok   = _ssid_extrair_do_html(trade.text)
            if tok:
                return True, tok, ""
        except Exception:
            pass

        return False, "", f"Login falhou (HTTP {resp.status_code}). Verifique email/senha."

    except ImportError:
        return False, "", "curl_cffi_indisponivel"
    except Exception as e:
        return False, "", str(e)


def _ssid_capturar_requests(email: str, senha: str) -> tuple[bool, str, str]:
    """
    Fallback: faz login usando requests padrão (sem impersonate).
    Retorna (ok, ssid, erro).
    """
    try:
        import requests as _req

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
        s  = _req.Session()
        s.headers.update({"User-Agent": ua, "Accept-Language": "pt-BR,pt;q=0.9"})

        # CSRF
        csrf = ""
        try:
            pg   = s.get("https://qxbroker.com/pt/sign-in", timeout=15)
            m    = re.search(r'name="_token"\s+value="([^"]+)"', pg.text)
            if m:
                csrf = m.group(1)
        except Exception:
            pass

        payload = {"email": email, "password": senha}
        if csrf:
            payload["_token"] = csrf

        resp = s.post(
            "https://qxbroker.com/pt/sign-in",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      "https://qxbroker.com/pt/sign-in",
                "Origin":       "https://qxbroker.com",
            },
            timeout=20,
            allow_redirects=True,
        )

        if ("otp" in resp.text.lower() or "two" in resp.text.lower()
                or resp.status_code == 422):
            return False, "", "OTP_REQUIRED"

        # Tenta /trade
        trade = s.get("https://qxbroker.com/pt/trade", timeout=15)
        tok   = _ssid_extrair_do_html(trade.text)
        if tok:
            return True, tok, ""

        return False, "", f"Token não encontrado (HTTP {resp.status_code})."

    except Exception as e:
        return False, "", str(e)


def _ssid_capturar_pyquotex(email: str, senha: str,
                              otp_callback=None) -> tuple[bool, str, str]:
    """
    Usa a classe Login do pyquotex para autenticar via HTTP assíncrono.
    Retorna (ok, ssid, erro).
    """
    try:
        from pyquotex.network.login import Login
        from pyquotex.api import QuotexAPI

        # Patch de compatibilidade: curl_cffi.Response não tem reason_phrase
        try:
            from curl_cffi.requests import Response as _CffiResponse
            if not hasattr(_CffiResponse, "reason_phrase"):
                _CffiResponse.reason_phrase = property(
                    lambda self: getattr(self, "reason", str(self.status_code))
                )
        except Exception:
            pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            api = QuotexAPI(
                host="qxbroker.com",
                username=email,
                password=senha,
                lang="pt",
                on_otp_callback=otp_callback,
            )
            login = Login(api=api)
            ok, motivo = await login(username=email, password=senha)

            token = ""
            if ok:
                token = (api.session_data or {}).get("token", "") or ""
                if not token:
                    try:
                        await login.get_profile()
                        token = (api.session_data or {}).get("token", "") or ""
                    except Exception:
                        pass
                if not token:
                    token = getattr(login, "ssid", "") or ""

            try:
                if getattr(login, "_client", None):
                    await login._client.close()
            except Exception:
                pass

            return ok, motivo, token

        ok, motivo, token = loop.run_until_complete(_run())
        loop.close()

        if ok and token:
            return True, token, ""
        elif ok:
            return False, "", "Login OK mas token não encontrado."
        else:
            return False, "", motivo or "Login HTTP falhou."

    except ImportError:
        return False, "", "pyquotex_indisponivel"
    except Exception as e:
        return False, "", str(e)


def quotex_capturar_ssid(email: str, senha: str,
                          otp_callback=None) -> dict:
    """
    Captura o SSID da Quotex de forma automática usando múltiplas estratégias
    em cascata (sem necessidade de Selenium ou interação manual):

      1. curl_cffi (impersonate Chrome) — mais confiável, evita bloqueios
      2. pyquotex Login HTTP assíncrono — biblioteca oficial
      3. requests padrão — fallback simples

    Retorna dict com { ok, ssid, erro }.
    Atualiza _SSID_CAPTURE_STATE com o resultado.
    """
    with _SSID_CAPTURE_LOCK:
        _SSID_CAPTURE_STATE.update({"status": "capturando", "ssid": "", "erro": "", "ts": time.time()})

    print(f"[Quotex] 🔐 Iniciando captura SSID para {email}...")

    # Estratégia 1: curl_cffi
    ok, ssid, erro = _ssid_capturar_curl_cffi(email, senha)
    if ok and ssid:
        print(f"[Quotex] ✅ SSID capturado via curl_cffi! len={len(ssid)}")
        _atualizar_captura(ssid)
        return {"ok": True, "ssid": ssid, "metodo": "curl_cffi"}

    if erro == "OTP_REQUIRED":
        with _SSID_CAPTURE_LOCK:
            _SSID_CAPTURE_STATE.update({"status": "otp_necessario", "erro": "OTP_REQUIRED", "ts": time.time()})
        return {"ok": False, "ssid": "", "erro": "OTP_REQUIRED", "otp": True}

    print(f"[Quotex] ⚠️ curl_cffi falhou ({erro}). Tentando pyquotex...")

    # Estratégia 2: pyquotex Login HTTP
    ok2, ssid2, erro2 = _ssid_capturar_pyquotex(email, senha, otp_callback)
    if ok2 and ssid2:
        print(f"[Quotex] ✅ SSID capturado via pyquotex! len={len(ssid2)}")
        _atualizar_captura(ssid2)
        return {"ok": True, "ssid": ssid2, "metodo": "pyquotex"}

    if erro2 == "OTP_REQUIRED":
        with _SSID_CAPTURE_LOCK:
            _SSID_CAPTURE_STATE.update({"status": "otp_necessario", "erro": "OTP_REQUIRED", "ts": time.time()})
        return {"ok": False, "ssid": "", "erro": "OTP_REQUIRED", "otp": True}

    print(f"[Quotex] ⚠️ pyquotex falhou ({erro2}). Tentando requests...")

    # Estratégia 3: requests padrão
    ok3, ssid3, erro3 = _ssid_capturar_requests(email, senha)
    if ok3 and ssid3:
        print(f"[Quotex] ✅ SSID capturado via requests! len={len(ssid3)}")
        _atualizar_captura(ssid3)
        return {"ok": True, "ssid": ssid3, "metodo": "requests"}

    # Todas as estratégias falharam
    motivo_final = erro3 or erro2 or erro or "Todas as estratégias de captura falharam."
    print(f"[Quotex] ❌ Captura SSID falhou: {motivo_final}")
    with _SSID_CAPTURE_LOCK:
        _SSID_CAPTURE_STATE.update({"status": "erro", "erro": motivo_final, "ts": time.time()})
    return {"ok": False, "ssid": "", "erro": motivo_final}


def _atualizar_captura(ssid: str):
    """Registra SSID capturado no estado global."""
    with _SSID_CAPTURE_LOCK:
        _SSID_CAPTURE_STATE.update({
            "status": "capturado",
            "ssid":   ssid,
            "erro":   "",
            "ts":     time.time(),
        })


def quotex_ssid_definir(ssid: str):
    """Permite que fontes externas (frontend, SSID Hunter) registrem um SSID capturado."""
    _atualizar_captura(ssid)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS DE STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_status() -> dict:
    """Retorna cópia segura do estado atual (sem expor o client object)."""
    with _QUOTEX_LOCK:
        return {
            "status":       _QUOTEX_STATE["status"],
            "email":        _QUOTEX_STATE["email"],
            "tipo_conta":   _QUOTEX_STATE["tipo_conta"],
            "saldo":        _QUOTEX_STATE["saldo"],
            "moeda":        _QUOTEX_STATE["moeda"],
            "ts_conectado": _QUOTEX_STATE["ts_conectado"],
            "erro":         _QUOTEX_STATE["erro"],
        }


def quotex_conectado() -> bool:
    with _QUOTEX_LOCK:
        return (_QUOTEX_STATE["status"] == "conectado"
                and _QUOTEX_STATE["client"] is not None)


# ═══════════════════════════════════════════════════════════════════════════════
# CONEXÃO PRINCIPAL (WebSocket via pyquotex)
# ═══════════════════════════════════════════════════════════════════════════════

def _quotex_conectar_thread(email: str, senha: str, tipo_conta: str,
                             otp_callback=None, ssid: str = "") -> None:
    """
    Executa em thread background.
    Instancia QuotexAPI, autentica e atualiza o estado global.

    Se ssid for fornecido, injeta o token na sessão (pula authenticate).
    Se ssid estiver vazio, tenta capturá-lo automaticamente via HTTP antes
    de abrir o WebSocket.
    """
    with _QUOTEX_LOCK:
        _QUOTEX_STATE["status"]    = "conectando"
        _QUOTEX_STATE["erro"]      = ""
        _QUOTEX_STATE["email"]     = email
        _QUOTEX_STATE["senha"]     = senha
        _QUOTEX_STATE["tipo_conta"] = tipo_conta.upper()

    # ── Auto-captura do SSID se não fornecido ─────────────────────────────────
    if not ssid and email and senha:
        print("[Quotex] 🔄 SSID não fornecido — capturando automaticamente...")
        resultado_ssid = quotex_capturar_ssid(email, senha, otp_callback)
        if resultado_ssid.get("ok") and resultado_ssid.get("ssid"):
            ssid = resultado_ssid["ssid"]
            print(f"[Quotex] ✅ SSID capturado automaticamente. len={len(ssid)}")
        elif resultado_ssid.get("otp"):
            # OTP necessário — informa ao estado e aguarda callback
            with _QUOTEX_LOCK:
                _QUOTEX_STATE["status"] = "aguardando_otp"
                _QUOTEX_STATE["erro"]   = "OTP necessário"
            print("[Quotex] ⚠️ OTP necessário — aguardando código via callback...")
            if otp_callback:
                # Se o callback resolver o OTP, tenta de novo em loop simples
                for _tentativa in range(3):
                    r2 = quotex_capturar_ssid(email, senha, otp_callback)
                    if r2.get("ok") and r2.get("ssid"):
                        ssid = r2["ssid"]
                        break
                    time.sleep(3)
        else:
            print(f"[Quotex] ⚠️ Captura SSID falhou: {resultado_ssid.get('erro')}. "
                  "Tentando conexão direta com email/senha...")

    try:
        from pyquotex.stable_api import Quotex

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        with _QUOTEX_LOCK:
            _QUOTEX_STATE["loop"] = loop

        is_demo = tipo_conta.upper() == "DEMO"

        client = Quotex(
            email=email,
            password=senha,
            lang="pt",
            on_otp_callback=otp_callback,
        )

        # ── Injeta sessão SSID: pula authenticate() completamente ─────────────
        if ssid and ssid.strip():
            try:
                from pyquotex.network.navigator import USER_AGENT_DEFAULT
                _ua = USER_AGENT_DEFAULT
            except ImportError:
                _ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36")

            _ssid = ssid.strip()
            # 1. Persiste em disco (usado por reconexões futuras)
            client.set_session(user_agent=_ua, ssid=_ssid)
            # 2. Atualiza session_data NA MEMÓRIA — sem isso connect() chama authenticate()
            client.session_data["token"]      = _ssid
            client.session_data["user_agent"] = _ua
            print(f"[Quotex] 🔑 Sessão SSID injetada (pula authenticate). len={len(_ssid)}")

        check, reason = loop.run_until_complete(client.connect())

        if not check:
            raise ConnectionError(f"Falha na autenticação Quotex: {reason}")

        # Define tipo de conta (PRACTICE = DEMO / REAL)
        mode = "PRACTICE" if is_demo else "REAL"
        loop.run_until_complete(client.change_account(mode))

        # Obtém saldo inicial
        saldo = loop.run_until_complete(client.get_balance())

        # Sincroniza perfil do servidor (offset de fuso horário).
        # Sem isso, a primeira operação falha com:
        # "unsupported type for timedelta seconds component: NoneType"
        try:
            loop.run_until_complete(client.get_server_time())
        except Exception as _ste:
            print(f"[Quotex] ⚠️ Aviso na sync de tempo: {_ste}")

        # Garante que profile.offset nunca fique None após o login
        try:
            if client.api and client.api.profile and client.api.profile.offset is None:
                client.api.profile.offset = 0
                print("[Quotex] ⚠️ profile.offset era None — definido como 0 (UTC)")
        except Exception:
            pass

        with _QUOTEX_LOCK:
            _QUOTEX_STATE["client"]          = client
            _QUOTEX_STATE["status"]          = "conectado"
            _QUOTEX_STATE["saldo"]           = float(saldo or 0)
            _QUOTEX_STATE["ts_conectado"]    = time.time()
            _QUOTEX_STATE["erro"]            = ""
            _QUOTEX_STATE["_falhas_balance"] = 0

        print(f"[Quotex] ✅ Conectado | conta={tipo_conta} | saldo={saldo:.2f}")

        # Salva credenciais + SSID após conexão bem-sucedida
        quotex_cfg_salvar(email, senha, tipo_conta, ssid)

        # Mantém o loop vivo para operações futuras
        loop.run_forever()

    except ImportError:
        msg = "pyquotex não instalado. Execute: pip install pyquotex"
        print(f"[Quotex] ❌ {msg}")
        with _QUOTEX_LOCK:
            _QUOTEX_STATE["status"] = "erro"
            _QUOTEX_STATE["erro"]   = msg
    except Exception as e:
        msg = str(e)
        print(f"[Quotex] ❌ Erro na conexão: {msg}")
        traceback.print_exc()
        with _QUOTEX_LOCK:
            _QUOTEX_STATE["status"] = "erro"
            _QUOTEX_STATE["erro"]   = msg
            _QUOTEX_STATE["client"] = None

        # ── Reconexão automática ──────────────────────────────────────────────
        time.sleep(15)
        with _QUOTEX_LOCK:
            deve_reconectar = (
                _QUOTEX_STATE["status"] == "erro"
                and email and senha
            )
        if deve_reconectar:
            print("[Quotex] 🔄 Reconectando automaticamente...")
            _quotex_conectar_thread(email, senha, tipo_conta, otp_callback, ssid)


def quotex_conectar(email: str, senha: str, tipo_conta: str = "DEMO",
                    otp_callback=None, ssid: str = "") -> dict:
    """
    Inicia conexão com a Quotex em background.

    Se ssid for fornecido, usa-o diretamente (pula authenticate).
    Se ssid estiver vazio e email+senha forem fornecidos, captura o SSID
    automaticamente antes de abrir o WebSocket.
    """
    quotex_desconectar()

    t = threading.Thread(
        target=_quotex_conectar_thread,
        args=(email, senha, tipo_conta, otp_callback, ssid),
        daemon=True,
        name="quotex-conn",
    )
    t.start()

    msg = ("Capturando SSID e conectando à Quotex..."
           if not ssid else "Conectando à Quotex via SSID...")
    return {"ok": True, "status": "conectando", "msg": msg}


def quotex_desconectar() -> dict:
    """Desconecta e limpa o estado da Quotex."""
    with _QUOTEX_LOCK:
        client = _QUOTEX_STATE.get("client")
        loop   = _QUOTEX_STATE.get("loop")
        _QUOTEX_STATE["client"]       = None
        _QUOTEX_STATE["status"]       = "desconectado"
        _QUOTEX_STATE["ts_conectado"] = 0
        _QUOTEX_STATE["saldo"]        = 0.0
        _QUOTEX_STATE["loop"]         = None

    if loop and loop.is_running():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    if client:
        try:
            client.close()
        except Exception:
            pass

    print("[Quotex] 🔌 Desconectado.")
    return {"ok": True, "status": "desconectado"}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITÁRIOS
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_duracao_alinhada(minutos: int = 1) -> int:
    """
    Calcula a duração em segundos para que a operação expire exatamente
    na virada do N-ésimo minuto a partir de agora.
    """
    segundos_no_minuto  = time.time() % 60
    segundos_ate_virada = 60 - segundos_no_minuto

    if segundos_ate_virada < 3:
        segundos_ate_virada += 60

    duracao_total = int(segundos_ate_virada) + (minutos - 1) * 60
    return max(5, duracao_total)


# ═══════════════════════════════════════════════════════════════════════════════
# SALDO
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_get_saldo() -> dict:
    """
    Consulta saldo atual da conta Quotex.
    Retorna cache quando get_balance() falha.
    """
    with _QUOTEX_LOCK:
        client      = _QUOTEX_STATE.get("client")
        loop        = _QUOTEX_STATE.get("loop")
        tipo        = _QUOTEX_STATE["tipo_conta"]
        saldo_cache = _QUOTEX_STATE["saldo"]

    if not client or not loop:
        if saldo_cache > 0:
            return {"ok": True, "saldo": saldo_cache, "tipo_conta": tipo, "cache": True}
        return {"ok": False, "erro": "Quotex não conectada."}

    try:
        fut   = asyncio.run_coroutine_threadsafe(client.get_balance(), loop)
        saldo = fut.result(timeout=10)
        with _QUOTEX_LOCK:
            _QUOTEX_STATE["saldo"] = float(saldo or 0)
        return {"ok": True, "saldo": float(saldo or 0), "tipo_conta": tipo}
    except Exception as e:
        print(f"[Quotex] ⚠️ get_balance falhou ({e}). Retornando cache: ${saldo_cache:.2f}")
        if saldo_cache > 0:
            return {"ok": True, "saldo": saldo_cache, "tipo_conta": tipo, "cache": True}
        return {"ok": False, "erro": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# ATIVOS
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_get_ativos() -> dict:
    """Retorna lista de ativos disponíveis para operar."""
    with _QUOTEX_LOCK:
        client = _QUOTEX_STATE.get("client")
        loop   = _QUOTEX_STATE.get("loop")

    if not client or not loop:
        return {"ok": False, "erro": "Quotex não conectada.", "ativos": []}

    try:
        fut   = asyncio.run_coroutine_threadsafe(client.get_all_assets(), loop)
        dados = fut.result(timeout=10)
        ativos = []
        if isinstance(dados, dict):
            for nome, payout in dados.items():
                ativos.append({"ativo": nome, "payout": payout})
        elif isinstance(dados, list):
            for item in dados:
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    ativos.append({"ativo": item[0], "payout": item[1] if len(item) > 1 else None})
                else:
                    ativos.append({"ativo": str(item)})
        return {"ok": True, "ativos": ativos, "total": len(ativos)}
    except Exception as e:
        return {"ok": False, "erro": str(e), "ativos": []}


# ═══════════════════════════════════════════════════════════════════════════════
# OPERAÇÕES
# ═══════════════════════════════════════════════════════════════════════════════

def quotex_operar(ativo: str, direcao: str, valor: float, duracao: int) -> dict:
    """
    Executa uma operação binária na Quotex.

    Parâmetros:
        ativo    — ex.: "EURUSD", "EURUSD_otc"
        direcao  — "call" | "put"  (alta | baixa)
        valor    — valor da entrada em USD
        duracao  — duração em segundos (ex.: 60 = 1 min)
    """
    with _QUOTEX_LOCK:
        client = _QUOTEX_STATE.get("client")
        loop   = _QUOTEX_STATE.get("loop")

    if not client or not loop:
        return {"ok": False, "erro": "Quotex não conectada."}

    direcao_norm = direcao.lower().strip()
    if direcao_norm not in ("call", "put"):
        return {"ok": False, "erro": f"Direção inválida: '{direcao}'. Use 'call' ou 'put'."}

    try:
        # Garante offset do servidor carregado antes de operar
        try:
            sync_fut = asyncio.run_coroutine_threadsafe(client.get_server_time(), loop)
            sync_fut.result(timeout=8)
        except Exception:
            pass

        # Garante que profile.offset nunca seja None (causa timedelta NoneType)
        try:
            if client.api and client.api.profile and client.api.profile.offset is None:
                client.api.profile.offset = 0
        except Exception:
            pass

        fut = asyncio.run_coroutine_threadsafe(
            client.buy(amount=valor, asset=ativo, direction=direcao_norm, duration=duracao),
            loop
        )
        resultado = fut.result(timeout=30)

        if isinstance(resultado, (list, tuple)) and len(resultado) >= 2:
            ok, info = bool(resultado[0]), resultado[1]
        else:
            ok, info = bool(resultado), {}

        if not ok:
            return {"ok": False, "erro": "Ordem rejeitada pela Quotex.", "detalhe": str(info)}

        op_id = info.get("id") or info.get("uid") or ""
        print(f"[Quotex] 📈 Operação | ativo={ativo} | dir={direcao_norm} | "
              f"val={valor} | dur={duracao}s | id={op_id}")
        return {
            "ok":      True,
            "id":      op_id,
            "ativo":   ativo,
            "direcao": direcao_norm,
            "valor":   valor,
            "duracao": duracao,
            "info":    info,
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# Cache de resultados já obtidos: op_id -> dict
_RESULTADO_CACHE: dict = {}
_RESULTADO_CACHE_LOCK = threading.Lock()


def quotex_resultado(op_id: str) -> dict:
    """
    Verifica o resultado (win/loss) de uma operação pelo ID.
    NÃO bloqueia — retorna {"ok": False, "pendente": True} se ainda não disponível.
    O background thread preenche _RESULTADO_CACHE quando o resultado chega.
    """
    with _RESULTADO_CACHE_LOCK:
        if op_id in _RESULTADO_CACHE:
            return _RESULTADO_CACHE.pop(op_id)

    return {"ok": False, "pendente": True, "erro": "Aguardando resultado..."}


def _quotex_check_win_bg(op_id: str):
    """Roda em background thread — chama check_win e salva no cache."""
    with _QUOTEX_LOCK:
        client = _QUOTEX_STATE.get("client")
        loop   = _QUOTEX_STATE.get("loop")

    if not client or not loop:
        with _RESULTADO_CACHE_LOCK:
            _RESULTADO_CACHE[op_id] = {"ok": False, "erro": "Quotex não conectada."}
        return

    try:
        fut       = asyncio.run_coroutine_threadsafe(client.check_win(op_id), loop)
        resultado = fut.result(timeout=300)

        if isinstance(resultado, (list, tuple)) and len(resultado) >= 2:
            res, lucro = resultado[0], resultado[1]
        else:
            res, lucro = "desconhecido", float(resultado or 0)

        win = float(lucro or 0) > 0
        with _RESULTADO_CACHE_LOCK:
            _RESULTADO_CACHE[op_id] = {
                "ok": True, "id": op_id, "resultado": res,
                "lucro": float(lucro or 0), "win": win,
            }
    except Exception as e:
        with _RESULTADO_CACHE_LOCK:
            _RESULTADO_CACHE[op_id] = {"ok": False, "erro": str(e)}


def quotex_resultado_iniciar(op_id: str):
    """Dispara o check_win em background. Chame logo após operar."""
    t = threading.Thread(target=_quotex_check_win_bg, args=(op_id,), daemon=True)
    t.start()
