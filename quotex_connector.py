# ═══════════════════════════════════════════════════════════════════════════════
# QUOTEX CONNECTOR — Módulo de conexão com a corretora Quotex
# ═══════════════════════════════════════════════════════════════════════════════
#
# Usa a biblioteca pyquotex (https://github.com/cleitonleonel/pyquotex)
# para autenticar e operar na Quotex via WebSocket.
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
import traceback

# ── Estado global ──────────────────────────────────────────────────────────────
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

# Arquivo de credenciais salvas
_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
_QUOTEX_CFG_FILE = os.path.join(_BASE_DIR, "quotex_config.json")


# ── Persistência de credenciais ────────────────────────────────────────────────
def quotex_cfg_carregar() -> dict:
    """Carrega email/senha/tipo da Quotex salvos em disco."""
    padrao = {"email": "", "senha": "", "tipo_conta": "DEMO"}
    try:
        if os.path.exists(_QUOTEX_CFG_FILE):
            with open(_QUOTEX_CFG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return padrao


def quotex_cfg_salvar(email: str, senha: str, tipo_conta: str = "DEMO") -> None:
    """Salva credenciais da Quotex em disco (sem criptografia — mesmo padrão do resto do bot)."""
    try:
        with open(_QUOTEX_CFG_FILE, "w", encoding="utf-8") as f:
            json.dump({"email": email, "senha": senha, "tipo_conta": tipo_conta},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── Helpers de status ──────────────────────────────────────────────────────────
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
        return _QUOTEX_STATE["status"] == "conectado" and _QUOTEX_STATE["client"] is not None


# ── Conexão principal ──────────────────────────────────────────────────────────
def _quotex_conectar_thread(email: str, senha: str, tipo_conta: str,
                             otp_callback=None, ssid: str = "") -> None:
    """
    Executa em thread background.
    Instancia QuotexAPI, autentica e atualiza o estado global.

    Se ssid for fornecido, usa set_session() para pular o login por senha
    (evita HTTP 403 por bloqueio de IP/User-Agent da Quotex).
    """
    with _QUOTEX_LOCK:
        _QUOTEX_STATE["status"] = "conectando"
        _QUOTEX_STATE["erro"]   = ""
        _QUOTEX_STATE["email"]  = email
        _QUOTEX_STATE["senha"]  = senha
        _QUOTEX_STATE["tipo_conta"] = tipo_conta.upper()

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

        # ── Modo SSID: injeta sessão do browser — pula login por senha ──
        if ssid and ssid.strip():
            from pyquotex.network.navigator import USER_AGENT_DEFAULT
            _ssid = ssid.strip()
            # 1. Persiste em disco (usado por reconexões futuras)
            client.set_session(user_agent=USER_AGENT_DEFAULT, ssid=_ssid)
            # 2. Atualiza session_data NA MEMÓRIA — sem isso connect() ainda chama authenticate()
            client.session_data["token"]      = _ssid
            client.session_data["user_agent"] = USER_AGENT_DEFAULT
            print(f"[Quotex] 🔑 Usando SSID direto (pula authenticate)")

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
        # porque get_server_time() usa profile.offset que ainda é None.
        try:
            loop.run_until_complete(client.get_server_time())
        except Exception as _ste:
            print(f"[Quotex] ⚠️ Aviso na sync de tempo: {_ste}")

        with _QUOTEX_LOCK:
            _QUOTEX_STATE["client"]       = client
            _QUOTEX_STATE["status"]           = "conectado"
            _QUOTEX_STATE["saldo"]            = float(saldo or 0)
            _QUOTEX_STATE["ts_conectado"]     = time.time()
            _QUOTEX_STATE["erro"]             = ""
            _QUOTEX_STATE["_falhas_balance"]  = 0   # zera contador de falhas

        print(f"[Quotex] ✅ Conectado | conta={tipo_conta} | saldo={saldo:.2f}")

        # Salva credenciais após conexão bem-sucedida
        quotex_cfg_salvar(email, senha, tipo_conta)

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
        # ── Reconexão automática se tinha credenciais salvas ──────────────
        # Aguarda 15s e tenta reconectar automaticamente (evita loop imediato)
        time.sleep(15)
        with _QUOTEX_LOCK:
            # Só reconecta se o status ainda é "erro" (usuário não desconectou manualmente)
            deve_reconectar = _QUOTEX_STATE["status"] == "erro" and email and senha
        if deve_reconectar:
            print(f"[Quotex] 🔄 Reconectando automaticamente em background...")
            _quotex_conectar_thread(email, senha, tipo_conta, otp_callback, ssid)


def quotex_conectar(email: str, senha: str, tipo_conta: str = "DEMO",
                    otp_callback=None, ssid: str = "") -> dict:
    """
    Inicia conexão com a Quotex em background.
    ssid: token SSID capturado do browser (evita HTTP 403).
    """
    quotex_desconectar()

    t = threading.Thread(
        target=_quotex_conectar_thread,
        args=(email, senha, tipo_conta, otp_callback, ssid),
        daemon=True,
        name="quotex-conn",
    )
    t.start()

    return {"ok": True, "status": "conectando", "msg": "Conexão com Quotex iniciada em background."}


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

    # Para o loop de forma segura
    if loop and loop.is_running():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    # Fecha o cliente
    if client:
        try:
            client.close()
        except Exception:
            pass

    print("[Quotex] 🔌 Desconectado.")
    return {"ok": True, "status": "desconectado"}


# ── Duração alinhada à virada do minuto ───────────────────────────────────────
def quotex_duracao_alinhada(minutos: int = 1) -> int:
    """
    Calcula a duração em segundos para que a operação expire exatamente
    na virada do N-ésimo minuto a partir de agora.

    Exemplo com minutos=1 (padrão):
      - Você entra no segundo :40 de uma vela M1
      - Faltam 20s para a virada do minuto
      - A função retorna 20s como duração
      - A operação expira exatamente na virada da próxima vela ✅

    Exemplo com minutos=2:
      - Você entra no segundo :40
      - Faltam 20s + 60s = 80s para a virada de 2 minutos
      - A função retorna 80s ✅

    Parâmetros:
        minutos — número de velas completas que a operação deve durar (padrão: 1)

    Retorna:
        duração em segundos (mínimo 5s para evitar rejeição pela Quotex)
    """
    segundos_no_minuto = time.time() % 60          # posição no minuto atual (0-59)
    segundos_ate_virada = 60 - segundos_no_minuto  # segundos até o próximo minuto

    # Se já estamos muito próximos da virada (< 3s), pula para o minuto seguinte
    # para evitar que a ordem chegue após a virada
    if segundos_ate_virada < 3:
        segundos_ate_virada += 60

    # Adiciona os minutos completos extras além do primeiro
    duracao_total = int(segundos_ate_virada) + (minutos - 1) * 60

    # Garante mínimo de 5 segundos (Quotex rejeita durações muito curtas)
    return max(5, duracao_total)


# ── Saldo ──────────────────────────────────────────────────────────────────────
def quotex_get_saldo() -> dict:
    """
    Consulta saldo atual da conta Quotex.

    Quando get_balance() falha (WS morto / timeout), retorna o último
    valor salvo em cache com ok=True para que o frontend continue
    exibindo o saldo — em vez de travar em '--'.
    """
    with _QUOTEX_LOCK:
        client       = _QUOTEX_STATE.get("client")
        loop         = _QUOTEX_STATE.get("loop")
        tipo         = _QUOTEX_STATE["tipo_conta"]
        saldo_cache  = _QUOTEX_STATE["saldo"]   # último valor conhecido

    if not client or not loop:
        # Sem conector — retorna cache se disponível, senão erro real
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
        # WS morto ou timeout — usa o cache para não travar o frontend
        print(f"[Quotex] ⚠️ get_balance falhou ({e}). Retornando saldo em cache: ${saldo_cache:.2f}")
        if saldo_cache > 0:
            return {"ok": True, "saldo": saldo_cache, "tipo_conta": tipo, "cache": True}
        return {"ok": False, "erro": str(e)}


# ── Ativos disponíveis ─────────────────────────────────────────────────────────
def quotex_get_ativos() -> dict:
    """Retorna lista de ativos disponíveis para operar."""
    with _QUOTEX_LOCK:
        client = _QUOTEX_STATE.get("client")
        loop   = _QUOTEX_STATE.get("loop")

    if not client or not loop:
        return {"ok": False, "erro": "Quotex não conectada.", "ativos": []}

    try:
        fut    = asyncio.run_coroutine_threadsafe(client.get_all_assets(), loop)
        dados  = fut.result(timeout=10)
        # pyquotex.get_all_assets() → dict {nome: payout_str}
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


# ── Executar operação (CALL / PUT) ─────────────────────────────────────────────
def quotex_operar(ativo: str, direcao: str, valor: float, duracao: int) -> dict:
    """
    Executa uma operação binária na Quotex.

    Parâmetros:
        ativo    — ex.: "EURUSD", "EURUSD_otc"
        direcao  — "call" | "put"  (alta | baixa)
        valor    — valor da entrada em USD
        duracao  — duração em segundos (ex.: 60 = 1 min)

    Retorna dict com id da operação e resultado quando disponível.
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
        # Garante que o perfil/offset do servidor está carregado antes de operar.
        # Evita "unsupported type for timedelta seconds component: NoneType"
        # que ocorre quando get_server_time() ainda tem profile.offset=None.
        try:
            sync_fut = asyncio.run_coroutine_threadsafe(client.get_server_time(), loop)
            sync_fut.result(timeout=8)
        except Exception:
            pass  # não bloqueia a operação se a sync falhar

        # buy(amount, asset, direction, duration) → tuple[bool, Any]
        fut    = asyncio.run_coroutine_threadsafe(
            client.buy(amount=valor, asset=ativo, direction=direcao_norm, duration=duracao),
            loop
        )
        resultado = fut.result(timeout=30)
        # resultado é tuple[bool, info]
        if isinstance(resultado, (list, tuple)) and len(resultado) >= 2:
            ok, info = bool(resultado[0]), resultado[1]
        else:
            ok, info = bool(resultado), {}

        if not ok:
            return {"ok": False, "erro": "Ordem rejeitada pela Quotex.", "detalhe": str(info)}

        op_id = info.get("id") or info.get("uid") or ""
        print(f"[Quotex] 📈 Operação enviada | ativo={ativo} | dir={direcao_norm} | val={valor} | dur={duracao}s | id={op_id}")
        return {
            "ok":        True,
            "id":        op_id,
            "ativo":     ativo,
            "direcao":   direcao_norm,
            "valor":     valor,
            "duracao":   duracao,
            "info":      info,
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ── Verificar resultado de operação ───────────────────────────────────────────
def quotex_resultado(op_id: str) -> dict:
    """
    Verifica o resultado (win/loss) de uma operação pelo ID.
    Bloqueia até receber o resultado ou timeout de 5 min.
    """
    with _QUOTEX_LOCK:
        client = _QUOTEX_STATE.get("client")
        loop   = _QUOTEX_STATE.get("loop")

    if not client or not loop:
        return {"ok": False, "erro": "Quotex não conectada."}

    try:
        fut = asyncio.run_coroutine_threadsafe(
            client.check_win(op_id),
            loop
        )
        resultado = fut.result(timeout=310)
        # check_win → tuple[str, float]  (status, profit)
        if isinstance(resultado, (list, tuple)) and len(resultado) >= 2:
            res, lucro = resultado[0], resultado[1]
        else:
            res, lucro = "desconhecido", float(resultado or 0)

        win = float(lucro or 0) > 0
        return {"ok": True, "id": op_id, "resultado": res, "lucro": float(lucro or 0), "win": win}
    except Exception as e:
        return {"ok": False, "erro": str(e)}
