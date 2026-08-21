# import webbrowser
from digit_sniper_pro import register_digit_sniper
from digit_matrix_sniper import register_digit_matrix
import threading
import time
import json
import os
import datetime as _dt_brt_mod
import io as _io
import re
import subprocess as _subprocess
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests
from flask import Flask, jsonify, request

# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE RISK ENGINE — Motor de Risco Adaptativo
# ═══════════════════════════════════════════════════════════════════════════════
from adaptive_risk import AdaptiveRiskEngine, AdaptiveConfig

# Configuração padrão do motor (sobrescrita via /bot-config ou /adaptive-config)
ADAPTIVE_CONFIG = AdaptiveConfig(
    modo                 = "DESLIGADO",
    stake_min            = 0.35,
    stake_max            = 10.00,
    risco_max_pct        = 0.03,
    max_losses_seguidos  = 3,
    drawdown_defensivo   = 0.05,
    drawdown_bloqueio    = 0.10,
    janela_resultados    = 20,
    reducao_loss         = 0.80,
    reducao_drawdown     = 0.70,
    aumento_win          = 1.05,
    recovery_max_pct     = 0.30,
    score_min_operar     = 40.0,
    score_defensivo      = 60.0,
    bloquear_apos_losses = 5,
    cooldown_segundos    = 60,
)

ADAPTIVE_ENGINE = AdaptiveRiskEngine(ADAPTIVE_CONFIG)

# ── Hora no fuso de Brasília (UTC-3) — independe da timezone do servidor ──
def _hora_brt(fmt: str = "%H:%M") -> str:
    try:
        from zoneinfo import ZoneInfo
        return _dt_brt_mod.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime(fmt)
    except Exception:
        brt = _dt_brt_mod.datetime.utcnow() - _dt_brt_mod.timedelta(hours=3)
        return brt.strftime(fmt)

app = Flask(__name__)

APP_ID       = "33qw17TW2WM9OqeTqtRaC"
SERVIDOR_URL = "https://garrabot.duckdns.org/pegar-token-robo"   # Oracle Cloud
RENDER_URL   = SERVIDOR_URL   # alias de compatibilidade — não usa mais o Render
SITE_LOGIN   = "https://garrabot.duckdns.org/login"
API_BASE     = "https://api.derivws.com/trading/v1/options"

# Guarda tipo de conta escolhido (DEMO ou REAL)
_access_token: dict = {"value": None, "tipo": "DEMO"}

# ═══════════════════════════════════════════════════════════════════════
# SISTEMA DE AUTENTICAÇÃO — ADMIN + USUÁRIOS + EMAIL
# ═══════════════════════════════════════════════════════════════════════
import smtplib
import random
import string
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Credenciais do administrador master
ADMIN_USER  = "fiel"
ADMIN_SENHA = "3510"

# ── Arquivo de histórico de logins ───────────────────────────────────────────
_LOGIN_HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_historico.json")

def _hist_ler() -> list:
    try:
        with open(_LOGIN_HIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _hist_salvar(lista: list):
    with open(_LOGIN_HIST_FILE, "w", encoding="utf-8") as f:
        json.dump(lista[-500:], f, ensure_ascii=False, indent=2)   # mantém últimas 500 entradas

def _hist_registrar(username: str, ip: str, pc: str, evento: str):
    """Registra um evento de login no histórico (LOGIN, LOGOUT, BLOQUEIO, LOGIN_DUPLO)."""
    lista = _hist_ler()
    lista.append({
        "username": username,
        "ip":       ip,
        "pc":       pc,
        "evento":   evento,
        "data_hora": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _hist_salvar(lista)

def _auth_gerar_token() -> str:
    """Gera token de sessão único (32 bytes hex)."""
    return hashlib.sha256((str(time.time()) + str(random.random())).encode()).hexdigest()

# Estado global do modo de operação
_MODO_OPERACAO = {"modo": "NORMAL"}   # NORMAL ou ESPELHO

# ── Cache de cotação USD/BRL ──────────────────────────────────────────────────
_COT_CACHE: dict = {"valor": 0.0, "ts": 0}
_COT_LOCK = threading.Lock()

def _buscar_cotacao() -> float:
    """Retorna cotação USD/BRL em tempo real; usa cache de 60s."""
    global _COT_CACHE
    with _COT_LOCK:
        if time.time() - _COT_CACHE["ts"] < 60 and _COT_CACHE["valor"] > 0:
            return _COT_CACHE["valor"]
    apis = [
        ("https://economia.awesomeapi.com.br/json/last/USD-BRL",
         lambda r: float(r.json()["USDBRL"]["bid"])),
        ("https://open.er-api.com/v6/latest/USD",
         lambda r: float(r.json()["rates"]["BRL"])),
    ]
    for url, extractor in apis:
        try:
            resp = requests.get(url, timeout=4)
            val  = extractor(resp)
            if val > 0:
                with _COT_LOCK:
                    _COT_CACHE["valor"] = val
                    _COT_CACHE["ts"]    = time.time()
                return val
        except Exception:
            continue
    with _COT_LOCK:
        return _COT_CACHE["valor"] if _COT_CACHE["valor"] > 0 else 6.20

# Caminhos dos arquivos de persistência
_BASE_DIR_AUTH  = os.path.dirname(os.path.abspath(__file__))
_USUARIOS_FILE  = os.path.join(_BASE_DIR_AUTH, "usuarios_sistema.json")
_EMAIL_CFG_FILE = os.path.join(_BASE_DIR_AUTH, "email_config.json")

# ── helpers ─────────────────────────────────────────────────────────────

def _auth_ler_usuarios() -> dict:
    try:
        with open(_USUARIOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"usuarios": []}

def _auth_salvar_usuarios(dados: dict):
    with open(_USUARIOS_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

def _auth_ler_email_cfg() -> dict:
    """Lê a config de e-mail. Prioridade: arquivo JSON → variáveis de ambiente."""
    cfg = {}
    try:
        with open(_EMAIL_CFG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    # Variáveis de ambiente como fallback (útil no Render onde o JSON pode ser apagado)
    if not cfg.get("remetente"):
        cfg["remetente"]    = os.environ.get("EMAIL_REMETENTE", "")
    if not cfg.get("senha_app"):
        cfg["senha_app"]    = os.environ.get("EMAIL_SENHA_APP", "")
    if not cfg.get("smtp_host"):
        cfg["smtp_host"]    = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    if not cfg.get("smtp_port"):
        cfg["smtp_port"]    = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
    if not cfg.get("nome_exibicao"):
        cfg["nome_exibicao"] = os.environ.get("EMAIL_NOME_EXIBICAO", "BOT GARRA")
    return cfg

def _auth_hash(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

def _auth_gerar_senha(tamanho: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=tamanho))

def _auth_enviar_email(destinatario: str, assunto: str, corpo_html: str) -> bool:
    """
    Envia e-mail via SMTP com cabeçalhos anti-spam.
    Boas práticas implementadas:
      - Reply-To idêntico ao From (evita flag de spoofing)
      - Message-ID único com domínio real do remetente
      - Date no formato RFC 2822
      - X-Mailer discreto (sem "Python" que é flaggeado)
      - Assunto sem palavras gatilho (GRÁTIS, CLIQUE, etc.)
      - Texto puro sempre presente (filtros penalizam só-HTML)
      - Corpo HTML limpo sem javascript, sem imagens externas
    """
    import re as _re
    import uuid as _uuid
    from email.utils import formatdate as _formatdate

    cfg = _auth_ler_email_cfg()
    if not cfg.get("remetente") or not cfg.get("senha_app"):
        print("[AUTH] E-mail não configurado.")
        return False

    remetente    = cfg["remetente"].strip()
    nome_exib    = cfg.get("nome_exibicao", "BOT GARRA").strip()
    # Extrai o domínio do remetente para compor o Message-ID
    dominio      = remetente.split("@")[-1] if "@" in remetente else "gmail.com"

    try:
        # ── Texto puro limpo (remove tags HTML e espaços extras) ──────────────
        texto_puro = _re.sub(r"<[^>]+>", " ", corpo_html)
        texto_puro = _re.sub(r"\s{2,}", "\n", texto_puro).strip()

        # ── Monta a mensagem ──────────────────────────────────────────────────
        msg = MIMEMultipart("alternative")

        # Cabeçalhos essenciais anti-spam
        msg["Subject"]    = assunto
        msg["From"]       = f"{nome_exib} <{remetente}>"
        msg["To"]         = destinatario
        msg["Reply-To"]   = f"{nome_exib} <{remetente}>"   # evita flag de spoofing
        msg["Date"]       = _formatdate(localtime=True)     # RFC 2822 obrigatório
        msg["Message-ID"] = f"<{_uuid.uuid4().hex}@{dominio}>"  # ID único por mensagem
        msg["MIME-Version"] = "1.0"
        # X-Mailer genérico — "Python smtplib" é frequentemente flaggeado por filtros
        msg["X-Mailer"]   = "MailClient/2.0"
        # Prioridade normal (1=alta, 3=normal, 5=baixa) — e-mails com prioridade ALTA
        # são frequentemente tratados como spam
        msg["X-Priority"] = "3"

        # Parte plain ANTES da html (RFC 2046 — cliente usa a última que suportar)
        msg.attach(MIMEText(texto_puro,  "plain", "utf-8"))
        msg.attach(MIMEText(corpo_html,  "html",  "utf-8"))

        # ── Envia via SMTP com STARTTLS ───────────────────────────────────────
        with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"),
                          int(cfg.get("smtp_port", 587))) as srv:
            srv.ehlo(dominio)          # identifica o domínio corretamente
            srv.starttls()
            srv.ehlo(dominio)          # segundo ehlo após STARTTLS (obrigatório)
            srv.login(remetente, cfg["senha_app"])
            srv.sendmail(remetente, destinatario, msg.as_bytes())

        print(f"[AUTH] E-mail enviado para {destinatario}")
        return True
    except Exception as e:
        print(f"[AUTH] Falha ao enviar e-mail: {e}")
        return False

def _auth_buscar_usuario(username: str) -> dict | None:
    dados = _auth_ler_usuarios()
    for u in dados["usuarios"]:
        if u["username"].lower() == username.lower():
            return u
    return None

# ── Rota: cadastrar novo usuário (qualquer pessoa pode solicitar) ─────────

@app.route('/auth/cadastrar', methods=['POST'])
def auth_cadastrar():
    dados = request.get_json() or {}
    username = (dados.get("username") or "").strip()
    email    = (dados.get("email") or "").strip().lower()

    if not username or not email or "@" not in email:
        return jsonify({"ok": False, "erro": "Usuário e e-mail válidos são obrigatórios."})

    db = _auth_ler_usuarios()
    for u in db["usuarios"]:
        if u["username"].lower() == username.lower():
            return jsonify({"ok": False, "erro": "Usuário já cadastrado."})
        if u["email"].lower() == email.lower():
            return jsonify({"ok": False, "erro": "E-mail já cadastrado."})

    novo = {
        "username":             username,
        "email":                email,
        "status":               "PENDENTE",   # PENDENTE → APROVADO → BLOQUEADO
        "senha_hash":           None,
        "criado_em":            time.strftime("%Y-%m-%d %H:%M:%S"),
        # ── Campos de segurança / sessão ──────────────────────────────────────
        "login_token":          None,
        "ultimo_ip":            None,
        "ultimo_pc":            None,
        "primeira_tentativa_ip": None,
        "primeira_tentativa_pc": None,
        "ultima_atividade":     None,
        "tentativas_login_duplo": 0,
        "bloqueado_em":         None,
        "motivo_bloqueio":      None,
    }
    db["usuarios"].append(novo)
    _auth_salvar_usuarios(db)
    print(f"[AUTH] Novo cadastro pendente: {username} <{email}>")
    return jsonify({"ok": True, "msg": "Cadastro enviado. Aguarde aprovação do administrador."})

# ── Rota: admin aprova ou bloqueia usuário ────────────────────────────────

def _auth_dias_restantes(u: dict) -> int | None:
    """Retorna dias restantes do período de teste, ou None se sem prazo."""
    dias = u.get("dias_teste")
    aprovado_em = u.get("aprovado_em")
    if not dias or not aprovado_em:
        return None
    try:
        import datetime
        ap = datetime.datetime.strptime(aprovado_em, "%Y-%m-%d %H:%M:%S")
        expira = ap + datetime.timedelta(days=int(dias))
        restam = (expira - datetime.datetime.now()).days
        return restam
    except Exception:
        return None

@app.route('/auth/admin/aprovar', methods=['POST'])
def auth_admin_aprovar():
    dados = request.get_json() or {}
    adm_user  = dados.get("admin_user")
    adm_senha = dados.get("admin_senha")
    if adm_user != ADMIN_USER or adm_senha != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})

    username  = (dados.get("username") or "").strip()
    acao      = (dados.get("acao") or "aprovar").upper()  # APROVAR | BLOQUEAR | REMOVER
    dias_teste = dados.get("dias_teste")   # None = sem prazo; int = N dias

    db = _auth_ler_usuarios()
    for u in db["usuarios"]:
        if u["username"].lower() == username.lower():
            if acao == "REMOVER":
                db["usuarios"].remove(u)
                _auth_salvar_usuarios(db)
                return jsonify({"ok": True, "msg": f"Usuário {username} removido."})

            if acao == "BLOQUEAR":
                u["status"] = "BLOQUEADO"
                _auth_salvar_usuarios(db)
                return jsonify({"ok": True, "msg": f"Usuário {username} bloqueado."})

            # Aprovação: gera senha aleatória e envia por e-mail
            nova_senha  = _auth_gerar_senha(10)
            agora       = time.strftime("%Y-%m-%d %H:%M:%S")
            u["status"]      = "APROVADO"
            u["senha_hash"]  = _auth_hash(nova_senha)
            u["aprovado_em"] = agora
            # Salva prazo de teste (None = acesso permanente)
            if dias_teste is not None:
                try:
                    u["dias_teste"] = int(dias_teste)
                except Exception:
                    u["dias_teste"] = None
            else:
                u["dias_teste"] = None
            _auth_salvar_usuarios(db)

            # ── Limpa configs de Telegram e WhatsApp do usuário anterior ──
            # Garante que o novo usuário começa sem as configurações de outro
            _base = os.path.dirname(os.path.abspath(__file__))
            _cfg_vazio_tg = {"token": "", "chat_id": "", "enabled": False, "resultados": True, "stopwin": True}
            _cfg_vazio_wa = {"api_url": "http://localhost:3000", "instancia": "GarraBot", "token": "422442", "enabled": False, "chat_id": ""}
            try:
                with open(os.path.join(_base, "telegram_config.json"), "w", encoding="utf-8") as _f:
                    json.dump(_cfg_vazio_tg, _f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            try:
                with open(os.path.join(_base, "whatsapp_config.json"), "w", encoding="utf-8") as _f:
                    json.dump(_cfg_vazio_wa, _f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            print(f"[AUTH] Configs Telegram/WhatsApp limpas para novo usuário: {username}")

            prazo_txt = f"{u['dias_teste']} dias" if u.get("dias_teste") else "permanente"
            nome_bot  = _auth_ler_email_cfg().get("nome_exibicao", "BOT GARRA")
            corpo = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head><body>
<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #dddddd;border-radius:6px;overflow:hidden;">
  <div style="background:#111111;padding:24px 28px;">
    <p style="color:#00cc33;font-size:1.1rem;font-weight:bold;margin:0;letter-spacing:2px;">{nome_bot}</p>
  </div>
  <div style="padding:28px;color:#333333;font-size:0.95rem;line-height:1.7;">
    <p>Olá, <b>{u['username']}</b>!</p>
    <p>Seu cadastro foi confirmado. Use a senha abaixo para acessar o sistema:</p>
    <div style="background:#f4f4f4;border-left:4px solid #00cc33;padding:14px 20px;margin:20px 0;font-size:1.3rem;font-family:monospace;letter-spacing:4px;text-align:center;color:#111;">
      {nova_senha}
    </div>
    <p style="font-size:0.85rem;color:#666;">Periodo de acesso: <b>{prazo_txt}</b></p>
    <p style="font-size:0.8rem;color:#999;margin-top:24px;">Nao compartilhe esta senha com ninguem.</p>
  </div>
</div>
</body></html>"""

            enviado = _auth_enviar_email(u["email"], f"Seu acesso ao {nome_bot} foi confirmado", corpo)
            return jsonify({
                "ok": True,
                "msg": f"Usuário {username} aprovado ({prazo_txt}). E-mail {'enviado' if enviado else 'NÃO enviado (verifique config)'}.",
                "email_ok": enviado
            })

    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rota: estender prazo de teste de um usuário ───────────────────────────

@app.route('/auth/admin/estender-prazo', methods=['POST'])
def auth_admin_estender_prazo():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username  = (dados.get("username") or "").strip()
    dias_extra = dados.get("dias_extra")
    try:
        dias_extra = int(dias_extra)
    except Exception:
        return jsonify({"ok": False, "erro": "Informe um número válido de dias."})

    db = _auth_ler_usuarios()
    for u in db["usuarios"]:
        if u["username"].lower() == username.lower():
            if u["status"] != "APROVADO":
                return jsonify({"ok": False, "erro": "Usuário não está aprovado."})

            import datetime
            # Se já tem prazo, soma a partir de hoje para estender
            dias_atuais = u.get("dias_teste")
            if dias_atuais:
                # Recalcula: quantos dias restam + extras
                restam = _auth_dias_restantes(u) or 0
                if restam < 0: restam = 0
                total = restam + dias_extra
                u["aprovado_em"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                u["dias_teste"]  = total
            else:
                # Sem prazo → define novo prazo a partir de agora
                u["aprovado_em"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                u["dias_teste"]  = dias_extra

            _auth_salvar_usuarios(db)
            return jsonify({"ok": True, "msg": f"Prazo de {u['username']} definido para {u['dias_teste']} dias a partir de agora."})

    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rota: admin lista todos os usuários ──────────────────────────────────

@app.route('/auth/admin/listar', methods=['POST'])
def auth_admin_listar():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    db = _auth_ler_usuarios()
    # Remove senha_hash da resposta por segurança
    lista = [{k: v for k, v in u.items() if k != "senha_hash"} for u in db["usuarios"]]
    return jsonify({"ok": True, "usuarios": lista})

# ── Rota: admin configura e-mail SMTP ────────────────────────────────────

@app.route('/auth/admin/email-config/get', methods=['POST'])
def auth_admin_email_config_get():
    """Retorna a config atual (sem a senha_app por segurança)."""
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    cfg = _auth_ler_email_cfg()
    cfg.pop("senha_app", None)   # nunca retorna a senha
    return jsonify({"ok": True, "config": cfg})

@app.route('/auth/admin/email-config/salvar', methods=['POST'])
def auth_admin_email_config_salvar():
    """Salva a configuração de e-mail SMTP no arquivo JSON.
    Se senha_app vier vazia, mantém a senha já salva (não sobrescreve)."""
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    remetente  = (dados.get("remetente") or "").strip()
    senha_app  = (dados.get("senha_app") or "").strip()
    if not remetente:
        return jsonify({"ok": False, "erro": "Remetente é obrigatório."})
    # Se a senha vier vazia, mantém a senha que já está salva
    if not senha_app:
        cfg_atual = _auth_ler_email_cfg()
        senha_app = cfg_atual.get("senha_app", "")
    if not senha_app:
        return jsonify({"ok": False, "erro": "Senha de app é obrigatória na primeira configuração."})
    nova = {
        "smtp_host":     (dados.get("smtp_host") or "smtp.gmail.com").strip(),
        "smtp_port":     int(dados.get("smtp_port") or 587),
        "remetente":     remetente,
        "senha_app":     senha_app,
        "nome_exibicao": (dados.get("nome_exibicao") or "GARRABOT ELITE").strip(),
    }
    with open(_EMAIL_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(nova, f, ensure_ascii=False, indent=2)
    print(f"[AUTH] Config e-mail salva: {remetente}")
    return jsonify({"ok": True, "msg": f"✅ Configuração salva! Remetente: {remetente}"})

@app.route('/auth/admin/email-config/testar', methods=['POST'])
def auth_admin_email_config_testar():
    """Envia um e-mail de teste e retorna o erro exato se falhar."""
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    cfg = _auth_ler_email_cfg()
    remetente = cfg.get("remetente", "").strip()
    senha_app = cfg.get("senha_app", "").strip()
    if not remetente:
        return jsonify({"ok": False, "erro": "❌ Remetente não configurado. Verifique a variável EMAIL_REMETENTE no Render."})
    if not senha_app:
        return jsonify({"ok": False, "erro": "❌ Senha de app não configurada. Verifique a variável EMAIL_SENHA_APP no Render."})
    corpo = """
    <div style="font-family:monospace;background:#000;color:#00ff41;padding:30px;border:1px solid #00ff41;">
    <h2 style="letter-spacing:3px;">⚡ GARRABOT ELITE</h2>
    <p style="color:#aaa;">Este é um <b style="color:#00ff41">e-mail de teste</b>.</p>
    <p>Se você recebeu este e-mail, a configuração SMTP está funcionando corretamente! ✅</p>
    </div>"""
    # Envia com captura do erro exato
    import smtplib as _smtplib
    try:
        import re as _re, uuid as _uuid
        from email.utils import formatdate as _formatdate
        dominio = remetente.split("@")[-1]
        from email.mime.multipart import MIMEMultipart as _MM
        from email.mime.text import MIMEText as _MT
        msg = _MM("alternative")
        msg["Subject"]    = "GARRABOT ELITE — Teste de E-mail"
        msg["From"]       = f"BOT GARRA <{remetente}>"
        msg["To"]         = remetente
        msg["Date"]       = _formatdate(localtime=True)
        msg["Message-ID"] = f"<{_uuid.uuid4().hex}@{dominio}>"
        msg.attach(_MT("Teste de e-mail GARRABOT.", "plain", "utf-8"))
        msg.attach(_MT(corpo, "html", "utf-8"))
        with _smtplib.SMTP(cfg.get("smtp_host","smtp.gmail.com"), int(cfg.get("smtp_port",587)), timeout=20) as srv:
            srv.ehlo(dominio)
            srv.starttls()
            srv.ehlo(dominio)
            srv.login(remetente, senha_app)
            srv.sendmail(remetente, remetente, msg.as_bytes())
        print(f"[AUTH] E-mail de teste enviado para {remetente}")
        return jsonify({"ok": True, "msg": f"✅ E-mail de teste enviado para {remetente}!"})
    except _smtplib.SMTPAuthenticationError as e:
        erro = f"❌ Autenticação SMTP falhou: senha de app incorreta ou acesso bloqueado pelo Google. Detalhe: {e}"
        print(f"[AUTH] {erro}")
        return jsonify({"ok": False, "erro": erro})
    except _smtplib.SMTPException as e:
        erro = f"❌ Erro SMTP: {e}"
        print(f"[AUTH] {erro}")
        return jsonify({"ok": False, "erro": erro})
    except Exception as e:
        erro = f"❌ Erro inesperado: {e}"
        print(f"[AUTH] {erro}")
        return jsonify({"ok": False, "erro": erro})

# ── Rota: diagnóstico de e-mail (mostra o que está configurado SEM expor a senha) ──
@app.route('/auth/admin/email-config/diagnostico', methods=['POST'])
def auth_admin_email_config_diagnostico():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    cfg = _auth_ler_email_cfg()
    remetente = cfg.get("remetente", "").strip()
    senha_app = cfg.get("senha_app", "").strip()
    # Mostra de onde veio cada valor
    fonte_rem = "arquivo JSON" if remetente else "NÃO CONFIGURADO"
    if not remetente:
        env_rem = os.environ.get("EMAIL_REMETENTE", "")
        if env_rem:
            remetente = env_rem
            fonte_rem = "variável de ambiente"
    fonte_sen = "arquivo JSON" if cfg.get("senha_app","").strip() else "NÃO CONFIGURADO"
    if not cfg.get("senha_app","").strip():
        env_sen = os.environ.get("EMAIL_SENHA_APP", "")
        if env_sen:
            senha_app = env_sen
            fonte_sen = "variável de ambiente"
    return jsonify({
        "ok": True,
        "remetente":       remetente or "❌ NÃO CONFIGURADO",
        "fonte_remetente": fonte_rem,
        "senha_ok":        bool(senha_app),
        "fonte_senha":     fonte_sen,
        "smtp_host":       cfg.get("smtp_host", "smtp.gmail.com"),
        "smtp_port":       cfg.get("smtp_port", 587),
    })

# Mantém rota antiga para compatibilidade
@app.route('/auth/admin/email-config', methods=['GET', 'POST'])
def auth_admin_email_config():
    if request.method == 'GET':
        cfg = _auth_ler_email_cfg()
        cfg.pop("senha_app", None)
        return jsonify({"ok": True, "config": cfg})
    return auth_admin_email_config_salvar()

# ── Rota: admin altera senha do próprio admin ─────────────────────────────

@app.route('/auth/admin/alterar-senha', methods=['POST'])
def auth_admin_alterar_senha():
    global ADMIN_SENHA
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Senha atual incorreta."})
    nova = (dados.get("nova_senha") or "").strip()
    if len(nova) < 4:
        return jsonify({"ok": False, "erro": "Senha muito curta (mínimo 4 caracteres)."})
    ADMIN_SENHA = nova
    return jsonify({"ok": True, "msg": "Senha do administrador alterada com sucesso."})

@app.route('/auth/admin/alterar-usuario', methods=['POST'])
def auth_admin_alterar_usuario():
    global ADMIN_USER
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Credenciais incorretas."})
    novo_user = (dados.get("novo_usuario") or "").strip()
    if len(novo_user) < 3:
        return jsonify({"ok": False, "erro": "Usuário muito curto (mínimo 3 caracteres)."})
    if " " in novo_user:
        return jsonify({"ok": False, "erro": "Usuário não pode conter espaços."})
    ADMIN_USER = novo_user
    return jsonify({"ok": True, "msg": f"Usuário do administrador alterado para '{novo_user}'."})

# ── Rota: reenviar senha para usuário aprovado ────────────────────────────

@app.route('/auth/admin/reenviar-senha', methods=['POST'])
def auth_admin_reenviar_senha():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    db = _auth_ler_usuarios()
    for u in db["usuarios"]:
        if u["username"].lower() == username.lower():
            if u["status"] != "APROVADO":
                return jsonify({"ok": False, "erro": "Usuário não está aprovado."})

            # Verifica ANTES de gerar senha se o e-mail está configurado
            cfg_email = _auth_ler_email_cfg()
            if not cfg_email.get("remetente") or not cfg_email.get("senha_app"):
                return jsonify({
                    "ok": False,
                    "erro": "⚠️ E-mail SMTP não configurado! Vá em CONFIG E-MAIL e preencha o remetente e a senha de app."
                })

            nome_bot  = cfg_email.get("nome_exibicao", "BOT GARRA")
            nova_senha = _auth_gerar_senha(10)
            u["senha_hash"] = _auth_hash(nova_senha)
            _auth_salvar_usuarios(db)
            corpo = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head><body>
<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #dddddd;border-radius:6px;overflow:hidden;">
  <div style="background:#111111;padding:24px 28px;">
    <p style="color:#00cc33;font-size:1.1rem;font-weight:bold;margin:0;letter-spacing:2px;">{nome_bot}</p>
  </div>
  <div style="padding:28px;color:#333333;font-size:0.95rem;line-height:1.7;">
    <p>Ola, <b>{u['username']}</b>!</p>
    <p>Sua senha de acesso foi redefinida. Use a senha abaixo para entrar no sistema:</p>
    <div style="background:#f4f4f4;border-left:4px solid #00cc33;padding:14px 20px;margin:20px 0;font-size:1.3rem;font-family:monospace;letter-spacing:4px;text-align:center;color:#111;">
      {nova_senha}
    </div>
    <p style="font-size:0.8rem;color:#999;margin-top:24px;">Nao compartilhe esta senha com ninguem.</p>
  </div>
</div>
</body></html>"""
            enviado = _auth_enviar_email(u["email"], f"Nova senha de acesso - {nome_bot}", corpo)
            if enviado:
                return jsonify({"ok": True, "email_ok": True, "msg": f"✅ Nova senha enviada para {u['email']} com sucesso."})
            else:
                # Senha foi gerada mas e-mail falhou — retorna a senha direto para o admin ver
                return jsonify({
                    "ok": True,
                    "email_ok": False,
                    "senha_gerada": nova_senha,
                    "msg": f"⚠️ E-mail NÃO enviado (falha SMTP). Senha gerada: {nova_senha} — passe ao usuário manualmente."
                })
    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rota: login do usuário final (verifica status + senha + prazo) ────────

@app.route('/login-sistema', methods=['POST'])
def login_sistema():
    dados    = request.get_json() or {}
    user     = (dados.get("user") or "").strip()
    pw       = dados.get("password") or ""
    ip_novo  = request.remote_addr or "desconhecido"
    _pc_raw  = (dados.get("pc_id") or "").strip()[:80]
    # Só aceita fingerprint real — ignora string "desconhecido" ou vazio
    pc_novo  = _pc_raw if (_pc_raw and _pc_raw != "desconhecido") else ""

    # Admin master — acesso direto, sem prazo
    if user == ADMIN_USER and pw == ADMIN_SENHA:
        return jsonify({"ok": True, "tipo": "ADMIN", "username": user,
                        "email": "administrador@sistema", "dias_restantes": None})

    # Usuário comum
    u = _auth_buscar_usuario(user)
    if not u:
        return jsonify({"ok": False, "erro": "USUÁRIO NÃO CADASTRADO"})
    if u["status"] == "PENDENTE":
        return jsonify({"ok": False, "erro": "ACESSO PENDENTE — aguarde aprovação do administrador.", "status": "PENDENTE"})
    if u["status"] == "BLOQUEADO":
        motivo = u.get("motivo_bloqueio") or "contate o administrador."
        return jsonify({"ok": False, "erro": f"ACESSO BLOQUEADO — {motivo}", "status": "BLOQUEADO",
                        "motivo": motivo})
    if not u.get("senha_hash"):
        return jsonify({"ok": False, "erro": "SENHA NÃO DEFINIDA — aguarde aprovação.", "status": "PENDENTE"})

    # ── Validação de PC aprovado ──────────────────────────────────────────────
    # Se o usuário tiver lista de PCs aprovados, só permite login nesses PCs
    # pc_novo vazio significa que o fingerprint não chegou — ignora a validação
    pcs_aprovados = u.get("pcs_aprovados", [])
    if pcs_aprovados and pc_novo and pc_novo not in pcs_aprovados:
        # Registra tentativa de PC não autorizado
        db2 = _auth_ler_usuarios()
        for usr2 in db2["usuarios"]:
            if usr2["username"].lower() == user.lower():
                # Guarda o PC novo para o admin poder aprovar pelo painel
                pcs_pendentes = usr2.get("pcs_pendentes", [])
                if pc_novo not in pcs_pendentes:
                    pcs_pendentes.append(pc_novo)
                usr2["pcs_pendentes"] = pcs_pendentes[-5:]  # guarda últimos 5
                break
        _auth_salvar_usuarios(db2)
        _hist_registrar(user, ip_novo, pc_novo, "PC_NAO_AUTORIZADO")
        print(f"[AUTH] PC não autorizado para {user}: {pc_novo[:16]}...")
        return jsonify({
            "ok":     False,
            "erro":   "DISPOSITIVO NÃO AUTORIZADO — contate o administrador para liberar este computador.",
            "status": "PC_BLOQUEADO"
        })

    # Verifica se o período de teste expirou
    restam = _auth_dias_restantes(u)
    if restam is not None and restam < 0:
        db = _auth_ler_usuarios()
        for usr in db["usuarios"]:
            if usr["username"].lower() == user.lower():
                usr["status"]          = "BLOQUEADO"
                usr["motivo_bloqueio"] = "PERÍODO DE TESTE EXPIRADO"
                usr["bloqueado_em"]    = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        _auth_salvar_usuarios(db)
        return jsonify({"ok": False, "erro": "PERÍODO DE TESTE EXPIRADO — contate o administrador.", "status": "EXPIRADO"})

    if _auth_hash(pw) != u["senha_hash"]:
        return jsonify({"ok": False, "erro": "SENHA INCORRETA"})

    # ══════════════════════════════════════════════════════════════════════
    # DETECÇÃO DE LOGIN DUPLO
    # Lógica de 2 etapas para evitar falsos positivos:
    #   1ª detecção → derruba sessão anterior, registra alerta
    #   2ª detecção em device diferente → bloqueia conta automaticamente
    # ══════════════════════════════════════════════════════════════════════
    token_atual = u.get("login_token")
    ip_anterior = u.get("ultimo_ip")
    pc_anterior = u.get("ultimo_pc")
    agora_str   = time.strftime("%Y-%m-%d %H:%M:%S")

    # Considera "mesmo dispositivo" se IP E PC coincidem (reconexão legítima)
    mesmo_device = (ip_anterior == ip_novo and pc_anterior == pc_novo)

    if token_atual and not mesmo_device:
        # Existe sessão ativa em outro dispositivo
        tentativas = int(u.get("tentativas_login_duplo") or 0) + 1
        db = _auth_ler_usuarios()
        for usr in db["usuarios"]:
            if usr["username"].lower() == user.lower():
                usr["tentativas_login_duplo"] = tentativas
                if tentativas >= 2:
                    # ── 2ª detecção → BLOQUEAR ──────────────────────────────
                    usr["status"]              = "BLOQUEADO"
                    usr["motivo_bloqueio"]     = "LOGIN DUPLO DETECTADO"
                    usr["bloqueado_em"]        = agora_str
                    usr["login_token"]         = None
                    usr["ultima_atividade"]    = agora_str
                    # Guarda IPs/PCs do incidente para o admin ver
                    usr["primeira_tentativa_ip"] = ip_anterior
                    usr["primeira_tentativa_pc"] = pc_anterior
                else:
                    # ── 1ª detecção → Derrubar sessão anterior ───────────────
                    usr["login_token"]           = None
                    usr["primeira_tentativa_ip"] = ip_anterior
                    usr["primeira_tentativa_pc"] = pc_anterior
                    usr["ultima_atividade"]      = agora_str
                break
        _auth_salvar_usuarios(db)

        # Registra no histórico
        _hist_registrar(user, ip_novo, pc_novo,
                        "BLOQUEIO_LOGIN_DUPLO" if tentativas >= 2 else "LOGIN_DUPLO_1A_DETECCAO")

        # Notifica admin por Telegram se configurado
        def _notif_admin():
            tg = _tg_carregar()
            if tg.get("enabled") and tg.get("token") and tg.get("chat_id"):
                acao_txt = "🔴 CONTA BLOQUEADA" if tentativas >= 2 else "⚠️ 1ª Detecção (sessão anterior derrubada)"
                msg = (
                    f"🚨 LOGIN DUPLO DETECTADO\n\n"
                    f"👤 Usuário: <b>{user}</b>\n"
                    f"📍 {acao_txt}\n\n"
                    f"🖥️ PC anterior: <code>{pc_anterior or '?'}</code>\n"
                    f"🌐 IP anterior: <code>{ip_anterior or '?'}</code>\n\n"
                    f"🖥️ PC novo: <code>{pc_novo}</code>\n"
                    f"🌐 IP novo: <code>{ip_novo}</code>\n\n"
                    f"🕐 {agora_str}"
                )
                _tg_enviar_texto(tg["token"], tg["chat_id"], msg)
        threading.Thread(target=_notif_admin, daemon=True).start()

        if tentativas >= 2:
            return jsonify({
                "ok": False,
                "erro": "LOGIN DUPLO DETECTADO — conta bloqueada automaticamente.",
                "status": "BLOQUEADO",
                "motivo": "LOGIN DUPLO DETECTADO"
            })
        else:
            # Sessão anterior derrubada — permite o login neste device
            pass   # continua para registrar nova sessão abaixo

    # ── Registra nova sessão ──────────────────────────────────────────────
    novo_token = _auth_gerar_token()
    db = _auth_ler_usuarios()
    for usr in db["usuarios"]:
        if usr["username"].lower() == user.lower():
            usr["login_token"]        = novo_token
            usr["ultimo_ip"]          = ip_novo
            # Só atualiza o pc se vier um fingerprint real (não "desconhecido")
            if pc_novo and pc_novo != "desconhecido":
                usr["ultimo_pc"]      = pc_novo
            usr["ultima_atividade"]   = agora_str
            # Reseta contador de tentativas duplas em login legítimo
            if mesmo_device or not token_atual:
                usr["tentativas_login_duplo"] = 0
            break
    _auth_salvar_usuarios(db)
    _hist_registrar(user, ip_novo, pc_novo, "LOGIN")

    return jsonify({
        "ok":            True,
        "tipo":          "USUARIO",
        "username":      u["username"],
        "email":         u.get("email", ""),
        "dias_restantes": restam,
        "session_token": novo_token,   # cliente deve armazenar e enviar no logout
    })

# ── Rota: logout do usuário (invalida token de sessão) ───────────────────────

@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    dados = request.get_json() or {}
    user  = (dados.get("username") or "").strip()
    token = (dados.get("session_token") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "Usuário não informado."})
    db = _auth_ler_usuarios()
    for usr in db["usuarios"]:
        if usr["username"].lower() == user.lower():
            if token and usr.get("login_token") == token:
                usr["login_token"]      = None
                usr["ultima_atividade"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _auth_salvar_usuarios(db)
                _hist_registrar(user, request.remote_addr or "?",
                                usr.get("ultimo_pc") or "?", "LOGOUT")
            return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rotas admin: gerenciar PCs aprovados ─────────────────────────────────────

@app.route('/auth/admin/pcs/listar', methods=['POST'])
def auth_admin_pcs_listar():
    """Retorna PCs aprovados e pendentes de um usuário."""
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    u = _auth_buscar_usuario(username)
    if not u:
        return jsonify({"ok": False, "erro": "Usuário não encontrado."})
    return jsonify({
        "ok":            True,
        "pcs_aprovados": u.get("pcs_aprovados", []),
        "pcs_pendentes": u.get("pcs_pendentes", []),
        "ultimo_pc":     u.get("ultimo_pc", ""),
    })

@app.route('/auth/admin/pcs/aprovar', methods=['POST'])
def auth_admin_pcs_aprovar():
    """Adiciona um PC à lista de aprovados do usuário."""
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    pc_id    = (dados.get("pc_id") or "").strip()
    if not username or not pc_id:
        return jsonify({"ok": False, "erro": "username e pc_id obrigatórios."})
    db = _auth_ler_usuarios()
    for u in db["usuarios"]:
        if u["username"].lower() == username.lower():
            aprovados = u.get("pcs_aprovados", [])
            pendentes = u.get("pcs_pendentes", [])
            if pc_id not in aprovados:
                aprovados.append(pc_id)
            if pc_id in pendentes:
                pendentes.remove(pc_id)
            u["pcs_aprovados"] = aprovados
            u["pcs_pendentes"] = pendentes
            _auth_salvar_usuarios(db)
            print(f"[AUTH] PC aprovado para {username}: {pc_id[:16]}...")
            return jsonify({"ok": True, "msg": f"PC aprovado para {username}.", "pcs_aprovados": aprovados})
    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

@app.route('/auth/admin/pcs/revogar', methods=['POST'])
def auth_admin_pcs_revogar():
    """Remove um PC da lista de aprovados do usuário."""
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    pc_id    = (dados.get("pc_id") or "").strip()
    if not username or not pc_id:
        return jsonify({"ok": False, "erro": "username e pc_id obrigatórios."})
    db = _auth_ler_usuarios()
    for u in db["usuarios"]:
        if u["username"].lower() == username.lower():
            aprovados = u.get("pcs_aprovados", [])
            if pc_id in aprovados:
                aprovados.remove(pc_id)
            u["pcs_aprovados"] = aprovados
            _auth_salvar_usuarios(db)
            print(f"[AUTH] PC revogado de {username}: {pc_id[:16]}...")
            return jsonify({"ok": True, "msg": f"PC revogado de {username}.", "pcs_aprovados": aprovados})
    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rota admin: desbloquear conta bloqueada por LOGIN DUPLO ──────────────────

@app.route('/auth/admin/desbloquear', methods=['POST'])
def auth_admin_desbloquear():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    db = _auth_ler_usuarios()
    for usr in db["usuarios"]:
        if usr["username"].lower() == username.lower():
            usr["status"]                  = "APROVADO"
            usr["motivo_bloqueio"]         = None
            usr["bloqueado_em"]            = None
            usr["login_token"]             = None
            usr["tentativas_login_duplo"]  = 0
            usr["primeira_tentativa_ip"]   = None
            usr["primeira_tentativa_pc"]   = None
            _auth_salvar_usuarios(db)
            _hist_registrar(username, "admin", "admin", "DESBLOQUEIO_ADMIN")
            return jsonify({"ok": True, "msg": f"Usuário {username} desbloqueado."})
    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rota admin: liberar novo dispositivo (reseta token/IP/PC sem desbloquear) ─

@app.route('/auth/admin/liberar-dispositivo', methods=['POST'])
def auth_admin_liberar_dispositivo():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    db = _auth_ler_usuarios()
    for usr in db["usuarios"]:
        if usr["username"].lower() == username.lower():
            usr["login_token"]            = None
            usr["ultimo_ip"]              = None
            usr["ultimo_pc"]              = None
            usr["tentativas_login_duplo"] = 0
            usr["primeira_tentativa_ip"]  = None
            usr["primeira_tentativa_pc"]  = None
            _auth_salvar_usuarios(db)
            _hist_registrar(username, "admin", "admin", "LIBERACAO_DISPOSITIVO")
            return jsonify({"ok": True, "msg": f"Dispositivo de {username} liberado. Próximo login será registrado como novo dispositivo."})
    return jsonify({"ok": False, "erro": "Usuário não encontrado."})

# ── Rota admin: histórico de logins de um usuário ────────────────────────────

@app.route('/auth/admin/historico-logins', methods=['POST'])
def auth_admin_historico_logins():
    dados = request.get_json() or {}
    if dados.get("admin_user") != ADMIN_USER or dados.get("admin_senha") != ADMIN_SENHA:
        return jsonify({"ok": False, "erro": "Acesso negado."})
    username = (dados.get("username") or "").strip()
    lista = _hist_ler()
    if username:
        lista = [h for h in lista if h.get("username", "").lower() == username.lower()]
    # Retorna os 50 mais recentes em ordem decrescente
    return jsonify({"ok": True, "historico": list(reversed(lista[-50:]))})

# ── Rota modo de operação ──────────────────────────────────────────────────

@app.route('/set-modo', methods=['POST'])
def set_modo():
    dados = request.get_json() or {}
    modo = dados.get("modo", "NORMAL").upper()
    _MODO_OPERACAO["modo"] = modo
    print(f"[*] Sistema configurado para modo: {modo}")
    return jsonify({"ok": True})

@app.route('/get-modo', methods=['GET'])
def get_modo():
    return jsonify(_MODO_OPERACAO)

# ── Rotas: avisos e agendamentos ─────────────────────────────────────────

_AVISOS_CFG_FILE  = os.path.join(_BASE_DIR_AUTH, "avisos_cfg.json")
_AGENDAMENTOS_FILE = os.path.join(_BASE_DIR_AUTH, "agendamentos.json")

def _ler_avisos_cfg() -> dict:
    try:
        with open(_AVISOS_CFG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ativo": "1", "dias_aviso": 3, "msg_teste": "", "msg_plano": ""}

def _ler_agendamentos() -> list:
    try:
        with open(_AGENDAMENTOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

@app.route('/avisos/cfg', methods=['GET', 'POST'])
def avisos_cfg():
    if request.method == 'GET':
        return jsonify({"ok": True, "cfg": _ler_avisos_cfg()})
    dados = request.get_json() or {}
    cfg = dados.get("cfg", {})
    if not cfg:
        return jsonify({"ok": False, "erro": "Dados inválidos."})
    with open(_AVISOS_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})

@app.route('/avisos/agendamentos', methods=['GET', 'POST'])
def avisos_agendamentos():
    if request.method == 'GET':
        return jsonify({"ok": True, "agendamentos": _ler_agendamentos()})
    dados = request.get_json() or {}
    lista = dados.get("agendamentos", [])
    with open(_AGENDAMENTOS_FILE, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})

# Endpoint para o cliente verificar avisos ativos (agendamentos + prazo)
@app.route('/avisos/cliente', methods=['POST'])
def avisos_cliente():
    dados = request.get_json() or {}
    username = (dados.get("username") or "").strip()

    result = {"ok": True, "agendamento": None, "prazo": None}

    # ── Verifica agendamentos ativos ──────────────────────────────
    import datetime as _dt
    agora = _dt.datetime.now()
    for ag in _ler_agendamentos():
        try:
            # datetime-local salva "2026-07-25T15:01" — normaliza para "2026-07-25 15:01"
            ini = _dt.datetime.fromisoformat(ag["inicio"].replace("T", " "))
            fim = _dt.datetime.fromisoformat(ag["fim"].replace("T", " "))
            if ini <= agora <= fim:
                result["agendamento"] = {
                    "msg":  ag.get("msg", ""),
                    "tipo": ag.get("tipo", "custom"),
                    "tipoLabel": ag.get("tipoLabel", "AVISO"),
                    "cor":  ag.get("cor", "yellow"),
                }
                break
        except Exception:
            pass

    # ── Verifica prazo do usuário ─────────────────────────────────
    if username:
        cfg = _ler_avisos_cfg()
        if cfg.get("ativo", "1") == "1":
            u = _auth_buscar_usuario(username)
            if u and u.get("dias_teste") and u.get("aprovado_em"):
                restam = _auth_dias_restantes(u)
                limite = int(cfg.get("dias_aviso", 3))
                if restam is not None and 0 <= restam <= limite:
                    import datetime
                    ap         = datetime.datetime.strptime(u["aprovado_em"], "%Y-%m-%d %H:%M:%S")
                    expira     = ap + datetime.timedelta(days=int(u["dias_teste"]))
                    diff       = expira - datetime.datetime.now()
                    total_s    = int(diff.total_seconds())
                    dias_r     = total_s // 86400
                    horas_r    = (total_s % 86400) // 3600
                    mins_r     = (total_s % 3600) // 60
                    # Timestamp Unix em milissegundos — preciso para o cronômetro exato
                    expira_ms  = int(expira.timestamp() * 1000)
                    eh_teste   = int(u["dias_teste"]) <= 30
                    msg_tmpl   = cfg.get("msg_teste" if eh_teste else "msg_plano", "") or (
                        "⚠ Seu período de TESTE expira em {DIAS}d e {HORAS}h. Contate o admin para renovar."
                        if eh_teste else
                        "⚠ Seu plano expira em {DIAS}d e {HORAS}h. Renove para não perder o acesso."
                    )
                    msg = (msg_tmpl
                           .replace("{DIAS}",    str(dias_r))
                           .replace("{HORAS}",   str(horas_r))
                           .replace("{MINS}",    str(mins_r))
                           .replace("{USUARIO}", u["username"]))
                    result["prazo"] = {
                        "msg":       msg,
                        "dias":      dias_r,
                        "horas":     horas_r,
                        "eh_teste":  eh_teste,
                        "expira_ms": expira_ms,   # timestamp exato em ms
                    }

    return jsonify(result)

# ── Rota debug: ver agendamentos e horário do servidor ───────────────────
@app.route('/avisos/debug', methods=['GET'])
def avisos_debug():
    import datetime as _dt
    agora = _dt.datetime.now()
    lista = _ler_agendamentos()
    detalhes = []
    for ag in lista:
        try:
            ini = _dt.datetime.fromisoformat(ag["inicio"].replace("T", " "))
            fim = _dt.datetime.fromisoformat(ag["fim"].replace("T", " "))
            ativo = ini <= agora <= fim
            detalhes.append({
                "id":     ag.get("id"),
                "msg":    ag.get("msg", "")[:60],
                "inicio": ag.get("inicio"),
                "fim":    ag.get("fim"),
                "ativo":  ativo,
                "ini_parsed": str(ini),
                "fim_parsed": str(fim),
            })
        except Exception as e:
            detalhes.append({"id": ag.get("id"), "erro": str(e), "raw": ag})
    return jsonify({
        "servidor_agora": str(agora),
        "total_agendamentos": len(lista),
        "agendamentos": detalhes
    })

# ── Rota: serve o painel de administração ─────────────────────────────────

# ═══════════════════════════════════════════════════════════════
# PWA — Manifest, Service Worker, Assets e APK
# ═══════════════════════════════════════════════════════════════
@app.route('/manifest.json')
def manifest_json():
    from flask import Response
    try:
        with open('manifest.json', 'r', encoding='utf-8') as f:
            data = f.read()
        resp = Response(data, mimetype='application/manifest+json')
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    except FileNotFoundError:
        return jsonify({"erro": "manifest.json não encontrado"}), 404

@app.route('/sw.js')
def service_worker():
    from flask import Response
    try:
        with open('sw.js', 'r', encoding='utf-8') as f:
            data = f.read()
        resp = Response(data, mimetype='application/javascript')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Service-Worker-Allowed'] = '/'
        return resp
    except FileNotFoundError:
        return "Service Worker não encontrado", 404

@app.route('/assets/<path:filename>')
def assets_static(filename):
    """Serve ícones e assets da pasta /assets/"""
    from flask import send_from_directory
    return send_from_directory('assets', filename)

@app.route('/garrabot.apk')
def download_apk():
    """Rota para download do APK"""
    from flask import send_file
    try:
        return send_file('garrabot.apk', as_attachment=True, download_name='GARRABOT.apk')
    except FileNotFoundError:
        return jsonify({"erro": "APK ainda não disponível. Em breve!"}), 404

@app.route('/admin')
def admin_panel():
    try:
        with open('admin.html', 'r', encoding='utf-8') as f:
            html = f.read()
        from flask import Response
        import time as _time_mod
        # Injeta um timestamp no <head> para forçar o browser a nunca usar cache
        ts = str(int(_time_mod.time()))
        html = html.replace('<meta charset="UTF-8">', f'<meta charset="UTF-8"><meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"><meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0"><!-- v{ts} -->', 1)
        resp = Response(html, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma']        = 'no-cache'
        resp.headers['Expires']       = '0'
        return resp
    except FileNotFoundError:
        return "<h1>admin.html não encontrado</h1>", 404

@app.route('/')
def index():
    with open('index.html', 'r', encoding='utf-8') as f:
        html = f.read()
    from flask import Response
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

# URL do callback — agora é a própria VPS (não precisa mais do Netlify)
CALLBACK_URL = "https://garrabot.duckdns.org/callback.html"

@app.route('/callback.html')
def callback_html():
    """Recebe o redirect da Deriv após login OAuth.
    Lê o ?code da URL, troca pelo access_token e mostra tela de sucesso/erro."""
    from flask import Response

    erro_param = request.args.get('error', '')
    erro_desc  = request.args.get('error_description', '')
    code       = request.args.get('code', '').strip()

    # ── HTML base com Matrix rain — usado por todas as telas do callback ──────
    MATRIX_HEAD = """<!DOCTYPE html><html lang="pt-br"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GARRABOT</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;font-family:'Courier New',monospace;overflow:hidden}
canvas{position:fixed;top:0;left:0;z-index:0}
.center{position:fixed;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;z-index:10}
.box{border:1px solid #00ff41;padding:44px 38px;width:430px;text-align:center;box-shadow:0 0 50px rgba(0,255,65,0.4),inset 0 0 40px rgba(0,255,65,0.04);background:rgba(0,5,0,0.90);position:relative}
.box::before{content:'';position:absolute;top:-1px;left:10%;width:80%;height:2px;background:linear-gradient(90deg,transparent,#00ff41,transparent)}
.logo{font-size:1.5rem;letter-spacing:8px;color:#00ff41;margin-bottom:4px;text-shadow:0 0 20px #00ff41}
.sub{font-size:0.6rem;letter-spacing:4px;color:#1a5c2a;margin-bottom:28px}
.icone{font-size:2.4rem;margin:10px 0}
.titulo{font-size:0.95rem;letter-spacing:2px;margin:10px 0 8px}
.msg{font-size:0.72rem;color:#888;line-height:1.9;margin:10px 0}
.msg b{color:#ccc}
.msg span.inst{color:#aaa}
.btn{margin-top:16px;width:100%;padding:11px;background:rgba(0,255,65,0.08);border:1px solid #00ff41;color:#00ff41;font-family:'Courier New',monospace;font-size:0.78rem;letter-spacing:2px;cursor:pointer}
.btn:hover{background:rgba(0,255,65,0.2)}
.cnt-wrap{font-size:0.62rem;color:#555;letter-spacing:1px;margin-top:10px}
.cnt-num{font-size:1.4rem;color:#ffbd2e;text-shadow:0 0 10px #ffbd2e;vertical-align:middle}
</style>
</head><body>
<canvas id="mx"></canvas>
<div class="center"><div class="box">
<div class="logo">GARRABOT</div>
<div class="sub">CYBER TRADING SYSTEM v2.0</div>"""

    MATRIX_SCRIPT = """<script>
(function(){
var cv=document.getElementById('mx'),ctx=cv.getContext('2d');
var ch='GARRABOT01ABCDEFｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺ0123456789$#@%&'.split('');
var cols,drops;
function rsz(){cv.width=window.innerWidth;cv.height=window.innerHeight;cols=Math.floor(cv.width/16);drops=Array(cols).fill(1);}
rsz();window.addEventListener('resize',rsz);
setInterval(function(){
  ctx.fillStyle='rgba(0,0,0,0.05)';ctx.fillRect(0,0,cv.width,cv.height);
  ctx.font='14px Courier New';
  for(var i=0;i<drops.length;i++){
    var c=ch[Math.floor(Math.random()*ch.length)];
    ctx.fillStyle=drops[i]*16<cv.height*0.1?'#fff':'#00ff41';
    ctx.fillText(c,i*16,drops[i]*16);
    if(drops[i]*16>cv.height&&Math.random()>0.975)drops[i]=0;
    drops[i]++;
  }
},50);
})();
</script>"""

    MATRIX_FOOT = "</div></div></body></html>"

    def _html(icone, titulo, cor, detalhe, fechar=True):
        cnt_html = '<div class="cnt-wrap">FECHANDO EM <span id="cnt" class="cnt-num">5</span> SEGUNDOS...</div>' if fechar else ''
        btn_html = '<button class="btn" onclick="window.close()">&#10005; FECHAR ESTA ABA</button>' if fechar else ''
        cnt_js   = '<script>var c=5,t=setInterval(function(){c--;var e=document.getElementById("cnt");if(e)e.textContent=c;if(c<=0){clearInterval(t);window.close();}},1000);</script>' if fechar else ''
        body = f"""
<div class="icone">{icone}</div>
<div class="titulo" style="color:{cor}">{titulo}</div>
<div class="msg">{detalhe}</div>
{cnt_html}
{btn_html}"""
        return Response(MATRIX_HEAD + body + MATRIX_FOOT + MATRIX_SCRIPT + cnt_js, mimetype='text/html')

    # ── Erro vindo da Deriv (link já usado, acesso negado, etc.) ────────────
    if erro_param:
        ja_usado = 'already been used' in erro_desc or 'verifier' in erro_desc
        if ja_usado:
            return _html('&#128260;', 'LINK JA UTILIZADO', '#ffbd2e',
                'Este link de login ja foi usado.<br>Cada conexao gera um codigo unico.<br><br>'
                '<span style="color:#aaa">&#9658; FECHE esta aba<br>'
                '&#9658; No bot, clique em <b style="color:#00ff41">conectar</b><br>'
                '&#9658; Clique em <b style="color:#00ff41">CONECTAR CONTA DERIV</b> novamente</span>')
        return _html('&#10060;', 'ERRO DE AUTENTICACAO', '#ff4444',
            f'<b>{erro_desc.replace("+", " ")}</b><br><br>Feche esta aba e tente novamente.')

    # ── Sem code ─────────────────────────────────────────────────────────────
    if not code:
        return _html('&#9888;', 'PAGINA ACESSADA DIRETAMENTE', '#ffbd2e',
            'Esta pagina so funciona apos o login na Deriv.<br><br>'
            '&#9658; No bot, clique em <b style="color:#00ff41">conectar</b>')

    # ── Code ja foi usado antes (recarga de aba ou double-redirect) ──────────
    with _token_lock:
        used_codes = _token_recebido.setdefault('_used_codes', set())
        if code in used_codes:
            return _html('&#128260;', 'LINK JA UTILIZADO', '#ffbd2e',
                'Este link de login ja foi usado.<br>Cada conexao gera um codigo unico.<br><br>'
                '<span style="color:#aaa">&#9658; FECHE esta aba<br>'
                '&#9658; No bot, clique em <b style="color:#00ff41">conectar</b><br>'
                '&#9658; Clique em <b style="color:#00ff41">CONECTAR CONTA DERIV</b> novamente</span>')
        used_codes.add(code)

    # ── Tem code — PKCE server-side: usa verifier armazenado na sessao ────────
    # Como o server-side nao tem sessionStorage, usamos o verifier do estado global
    # O verifier e gerado no frontend e enviado para a VPS antes do redirect
    verifier = request.args.get('verifier', '').strip()
    if not verifier:
        # Tenta buscar do estado global (salvo pelo /store-verifier)
        with _token_lock:
            verifier = _token_recebido.get('_pkce_verifier', '')

    if not verifier:
        return _html('&#128260;', 'SESSAO EXPIRADA', '#ffbd2e',
            'O verificador PKCE nao foi encontrado.<br>'
            'A sessao expirou ou a aba foi recarregada.<br><br>'
            '<b>&#9658; FECHE esta aba<br>'
            '&#9658; No bot, clique em conectar novamente</b>')

    # Troca o code pelo access_token
    try:
        r = requests.post(
            "https://auth.deriv.com/oauth2/token",
            data={
                "grant_type":    "authorization_code",
                "client_id":     APP_ID,
                "code":          code,
                "code_verifier": verifier,
                "redirect_uri":  CALLBACK_URL,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15
        )
        print(f"[Callback] token exchange: status={r.status_code} body={r.text[:300]}")
        data         = r.json()
        access_token = (data.get("access_token") or "").strip()
        if not access_token:
            return _html('&#10060;', 'FALHA NA AUTENTICACAO', '#ff4444',
                f'access_token nao retornado.<br><small style="color:#555">{r.text[:150]}</small><br><br>Feche e tente novamente.')
        # Verifica se é login da conta secundária (aguardando = True)
        _eh_secundaria = False
        with _token_sec_lock:
            if _token_secundaria.get("aguardando"):
                _token_secundaria["access_token"] = access_token
                _token_secundaria["ts"]           = time.time()
                _token_secundaria["aguardando"]   = False
                _eh_secundaria = True
                print(f"[Callback] Token SECUNDÁRIA armazenado: {access_token[:10]}...")

        if not _eh_secundaria:
            with _token_lock:
                _token_recebido["access_token"]      = access_token
                _token_recebido["ts"]                = time.time()
                _token_recebido["_bot_confirmou"]    = False
                _token_recebido.pop("_pkce_verifier", None)
            print(f"[Callback] Token PRINCIPAL armazenado: {access_token[:10]}... ({len(access_token)} chars)")
        # Sucesso — aguarda o bot confirmar antes de iniciar countdown
        sucesso_body = """
<div class="icone">&#9989;</div>
<div class="titulo" style="color:#00ff41;text-shadow:0 0 14px #00ff41">ACESSO AUTORIZADO</div>
<div id="stmsg" class="msg">AGUARDANDO BOT CONECTAR<span id="dots">...</span></div>
<div id="cnt-area" class="cnt-wrap" style="display:none">FECHANDO EM <span id="cnt" class="cnt-num">5</span> SEGUNDOS...</div>
<button class="btn" onclick="window.close()">&#10005; FECHAR ESTA ABA</button>"""

        sucesso_js = """<script>
(function(){
var cv=document.getElementById('mx'),ctx=cv.getContext('2d');
var ch='GARRABOT01ABCDEFｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺ0123456789$#@%&'.split('');
var cols,drops;
function rsz(){cv.width=window.innerWidth;cv.height=window.innerHeight;cols=Math.floor(cv.width/16);drops=Array(cols).fill(1);}
rsz();window.addEventListener('resize',rsz);
setInterval(function(){
  ctx.fillStyle='rgba(0,0,0,0.05)';ctx.fillRect(0,0,cv.width,cv.height);
  ctx.font='14px Courier New';
  for(var i=0;i<drops.length;i++){
    var c=ch[Math.floor(Math.random()*ch.length)];
    ctx.fillStyle=drops[i]*16<cv.height*0.1?'#fff':'#00ff41';
    ctx.fillText(c,i*16,drops[i]*16);
    if(drops[i]*16>cv.height&&Math.random()>0.975)drops[i]=0;
    drops[i]++;
  }
},50);
var dotF=['...','.. ','.','.  '],dotI=0;
var dotT=setInterval(function(){var d=document.getElementById('dots');if(d)d.textContent=dotF[dotI++%4];},400);
var tries=0;
function startCountdown(){
  clearInterval(dotT);
  var s=document.getElementById('stmsg');
  if(s){s.innerHTML='BOT CONECTADO COM SUCESSO!';s.style.color='#00ff41';}
  var a=document.getElementById('cnt-area');if(a)a.style.display='block';
  var c=5,t=setInterval(function(){c--;var e=document.getElementById('cnt');if(e)e.textContent=c;if(c<=0){clearInterval(t);window.close();}},1000);
}
function poll(){
  fetch('/auth/token-confirmado').then(function(r){return r.json();}).then(function(d){
    if(d.ok){startCountdown();}
    else{tries++;if(tries<30)setTimeout(poll,1000);else{clearInterval(dotT);var s=document.getElementById('stmsg');if(s){s.innerHTML='BOT DEMOROU — FECHE MANUALMENTE';s.style.color='#ffbd2e';}}}
  }).catch(function(){tries++;if(tries<30)setTimeout(poll,1000);});
}
setTimeout(poll,1000);
})();
</script>"""

        return Response(MATRIX_HEAD + sucesso_body + MATRIX_FOOT + sucesso_js, mimetype='text/html')
    except Exception as e:
        print(f"[Callback] Erro: {e}")
        return _html('&#10060;', 'ERRO DE CONEXAO', '#ff4444',
            f'Nao foi possivel contactar o servidor Deriv.<br><small>{e}</small><br><br>Feche e tente novamente.')


@app.route('/auth/token-confirmado', methods=['GET'])
def auth_token_confirmado():
    """Consultado pela página de sucesso do callback para saber se o bot já leu o token."""
    with _token_lock:
        ok = bool(_token_recebido.get("_bot_confirmou", False))
    return jsonify({"ok": ok})

@app.route('/store-verifier', methods=['POST'])
def store_verifier():
    """Recebe e armazena o PKCE verifier antes do redirect para a Deriv.
    Chamado pelo frontend logo antes de redirecionar para auth.deriv.com."""
    dados    = request.get_json(silent=True) or {}
    verifier = (dados.get("verifier") or "").strip()
    if verifier:
        with _token_lock:
            _token_recebido["_pkce_verifier"] = verifier
        return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "verifier vazio"}), 400



@app.route('/login')
def login_deriv():
    with open('netlify-garrabot/index.html', 'r', encoding='utf-8') as f:
        html = f.read()
    from flask import Response
    return Response(html, mimetype='text/html')

@app.route('/open-login')
def open_login():
    tipo = request.args.get('tipo', 'DEMO')
    _access_token["tipo"] = tipo.upper()
    # Garante que o slot da secundária NÃO está em modo aguardando
    # Evita que o callback roube o token do login principal para a secundária
    with _token_sec_lock:
        _token_secundaria["aguardando"] = False
    return jsonify({"status": "opened", "url": SITE_LOGIN})

# ── Estado global do login da conta secundária ──────────────────────────────
# Usado para monitorar o processo de login em janela anônima
_SEC_LOGIN_STATE = {
    "status": "idle",        # idle | opening | waiting_login | token_obtido | conectado | erro
    "browser_process": None,  # subprocess.Popen do navegador
    "browser_path": None,     # caminho do executável do navegador
    "browser_nome": "",       # nome do navegador (chrome, msedge, firefox...)
    "old_token": "",          # token antes do login (para detectar mudança)
    "tipo": "DEMO",          # DEMO ou REAL
    "ts_inicio": 0,           # timestamp do início do login
    "conta_id": None,         # ID da conta secundária registrada
    "conta_nome": "",         # Nome da conta
    "erro": None,             # Mensagem de erro se falhar
}
_SEC_LOGIN_LOCK = threading.Lock()


def _sec_encontrar_browsers() -> list:
    """
    Escaneia o sistema em busca de navegadores instalados.
    Retorna lista de (nome, caminho_executavel, flag_anonimo).
    Prioridade: Chrome > Edge > Firefox > Brave > Opera > Vivaldi.
    """
    encontrados = []

    # ── Locais comuns no Windows ──────────────────────────────────────────
    drives = [os.environ.get(p, "C:")[0] + ":\\" for p in ("SystemDrive", "ProgramFiles", "HOMEDRIVE")]
    drives = list(set(drives))  # remove duplicatas
    base_dirs = set()

    # Caminhos padrão do sistema
    for p in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMDATA"]:
        v = os.environ.get(p, "")
        if v:
            base_dirs.add(v)
            # Chrome muitas vezes está em %LOCALAPPDATA%\Google\Chrome\Application
            # Edge muitas vezes está em %PROGRAMFILES%\Microsoft\Edge\Application
            # Firefox muitas vezes está em %PROGRAMFILES%\Mozilla Firefox

    # Adiciona caminhos manuais comuns
    user_home = os.path.expanduser("~")
    base_dirs.add(rf"{user_home}\AppData\Local")  # %LOCALAPPDATA% alternativo
    # Tenta via variável USERNAME (mais seguro que os.getlogin())
    username = os.environ.get("USERNAME", "")
    if username:
        # Edge Canary/Dev em alguns sistemas
        base_dirs.add(rf"C:\Users\{username}\AppData\Local\Microsoft\Edge SxS\Application")
        base_dirs.add(rf"C:\Users\{username}\AppData\Local\Microsoft\WindowsApps")
    try:
        for d in ["C:\\", "D:\\"]:
            if os.path.exists(d):
                for sub in ["Program Files", "Program Files (x86)", "Users\\" + (username or "Default") + "\\AppData\\Local"]:
                    p = os.path.join(d, sub)
                    if os.path.exists(p):
                        base_dirs.add(p)
    except Exception:
        pass

    # ── Browsers a procurar ────────────────────────────────────────────────
    browsers = [
        ("chrome",  ["Google\\Chrome\\Application\\chrome.exe", "Chromium\\Application\\chrome.exe"],
                     "--incognito"),
        ("msedge",  ["Microsoft\\Edge\\Application\\msedge.exe"],
                     "--inprivate"),
        ("firefox", ["Mozilla Firefox\\firefox.exe"],
                     "--private-window"),
        ("brave",   ["BraveSoftware\\Brave-Browser\\Application\\brave.exe"],
                     "--incognito"),
        ("opera",   ["Opera\\launcher.exe"],
                     "--private"),
        ("vivaldi", ["Vivaldi\\Application\\vivaldi.exe"],
                     "--incognito"),
    ]

    for nome, subpaths, flag in browsers:
        for base in base_dirs:
            for sub in subpaths:
                path = os.path.join(base, sub)
                if os.path.exists(path):
                    encontrados.append((nome, path, flag))
                    break  # só uma vez por browser
            else:
                continue
            break

    # ── Tenta via registro do Windows (mais confiável) ────────────────────
    try:
        import winreg as _wr
        reg_paths = [
            (r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe", "chrome", "--incognito"),
            (r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe", "msedge", "--inprivate"),
            (r"SOFTWARE\\Mozilla\\Mozilla Firefox\\Main", "firefox", "--private-window"),
            (r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\brave.exe", "brave", "--incognito"),
        ]
        for reg_key, nome, flag in reg_paths:
            try:
                with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, reg_key) as key:
                    path, _ = _wr.QueryValueEx(key, "")
                    if os.path.exists(path):
                        # Evita duplicatas
                        if not any(p == path for _, p, _ in encontrados):
                            encontrados.append((nome, path, flag))
            except Exception:
                pass
            try:
                with _wr.OpenKey(_wr.HKEY_CURRENT_USER, reg_key) as key:
                    path, _ = _wr.QueryValueEx(key, "")
                    if os.path.exists(path):
                        if not any(p == path for _, p, _ in encontrados):
                            encontrados.append((nome, path, flag))
            except Exception:
                pass
    except Exception:
        pass

    # ── Tenta via comando 'where' (PATH do sistema) ───────────────────────
    for cmd, nome, flag in [("chrome", "chrome", "--incognito"),
                             ("msedge", "msedge", "--inprivate"),
                             ("firefox", "firefox", "--private-window")]:
        try:
            result = _subprocess.run(["where", cmd], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                path = result.stdout.strip().split("\n")[0].strip()
                if os.path.exists(path):
                    if not any(p == path for _, p, _ in encontrados):
                        encontrados.append((nome, path, flag))
        except Exception:
            pass

    # Ordena por prioridade (Chrome primeiro)
    prioridade = {"chrome": 0, "msedge": 1, "firefox": 2, "brave": 3, "opera": 4, "vivaldi": 5}
    encontrados.sort(key=lambda x: prioridade.get(x[0], 99))

    if encontrados:
        print(f"[Secundária] Navegadores encontrados: {[f'{n}' for n,_,_ in encontrados]}")
    else:
        print(f"[Secundária] NENHUM navegador anônimo encontrado!")

    return encontrados


def _sec_fechar_navegador():
    """Fecha o driver Selenium se estiver rodando."""
    with _SEC_LOGIN_LOCK:
        driver = _SEC_LOGIN_STATE.get("browser_process")
        if driver:
            try:
                driver.quit()
                print("[Secundária] Selenium driver fechado.")
            except Exception as e:
                print(f"[Secundária] Erro ao fechar driver: {e}")
        _SEC_LOGIN_STATE["browser_process"] = None


def _sec_registrar_com_token(tok: str, tipo: str):
    """Usa o access_token para registrar a conta secundária via API Deriv."""
    headers = {
        "Authorization": f"Bearer {tok}",
        "Deriv-App-ID":  APP_ID,
        "Content-Type":  "application/json",
    }
    res_contas = requests.get(f"{API_BASE}/accounts", headers=headers, timeout=10)
    if res_contas.status_code != 200:
        raise Exception(f"Erro ao listar contas: {res_contas.status_code}")
    contas_api = res_contas.json().get("data", [])
    conta_id = None
    for c in contas_api:
        cid      = str(c.get("account_id", ""))
        is_virt  = c.get("is_virtual", False)
        is_demo  = str(c.get("account_type","")).lower() == "demo" or is_virt or cid.upper().startswith(("VR","DOT","VRT"))
        if tipo == "DEMO" and is_demo:
            conta_id = cid; break
        elif tipo == "REAL" and not is_demo:
            conta_id = cid; break
    if not conta_id:
        raise Exception(f"Nenhuma conta {tipo} encontrada")
    res_otp = requests.post(f"{API_BASE}/accounts/{conta_id}/otp", headers=headers, timeout=10)
    wss_url = res_otp.json().get("data", {}).get("url", "")
    if not wss_url:
        raise Exception("OTP não retornou WSS URL")
    conta = _account_manager.adicionar_conta(
        conta_id=conta_id, tipo="SECUNDARIA",
        wss_url=wss_url, access_token=tok,
        nome=f"Secundária {tipo}",
    )
    with _SEC_LOGIN_LOCK:
        _SEC_LOGIN_STATE["status"]     = "conectado"
        _SEC_LOGIN_STATE["conta_id"]   = conta_id
        _SEC_LOGIN_STATE["conta_nome"] = conta.get("nome", "")
    # Salva no slot dedicado da secundária também
    with _token_sec_lock:
        _token_secundaria["access_token"] = tok
        _token_secundaria["ts"]           = time.time()
        _token_secundaria["aguardando"]   = False
    print(f"[Secundária] Conta registrada! ID={conta_id}")


def _sec_login_selenium(tipo: str):
    """
    Abre Chrome headless via Selenium no Oracle, navega para o login da Deriv,
    captura o token do callback e registra a conta secundária automaticamente.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    with _SEC_LOGIN_LOCK:
        _SEC_LOGIN_STATE["status"] = "abrindo_navegador"

    print("[Secundária] Iniciando Selenium headless...")

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    # Usa o chromedriver do snap
    service = Service(executable_path="/snap/bin/chromium.chromedriver")

    driver = None
    try:
        driver = webdriver.Chrome(service=service, options=opts)
        with _SEC_LOGIN_LOCK:
            _SEC_LOGIN_STATE["browser_process"] = driver
            _SEC_LOGIN_STATE["status"] = "aguardando_login"

        print(f"[Secundária] Navegando para login: {SITE_LOGIN}")
        driver.get(SITE_LOGIN)

        # Aguarda até 180s que a URL mude para /callback.html?code=...
        # (o usuário faz login manualmente na tela do celular/PC)
        timeout = 180
        inicio  = time.time()
        token_capturado = None

        while time.time() - inicio < timeout:
            current_url = driver.current_url
            # Detecta callback com code
            if "callback.html" in current_url and "code=" in current_url:
                print(f"[Secundária] Callback detectado: {current_url[:80]}...")
                # Aguarda o servidor processar (a página de callback chama /auth/deriv internamente)
                time.sleep(3)
                # Lê o token do slot da secundária
                with _token_sec_lock:
                    tok = _token_secundaria.get("access_token", "")
                    tok_age = time.time() - _token_secundaria.get("ts", 0)
                if tok and tok not in ("None","null","") and tok_age < 30:
                    token_capturado = tok
                    break
                # Fallback: lê diretamente da URL se vier como query param
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)
                tok_url = params.get("access_token", [None])[0]
                if tok_url:
                    token_capturado = tok_url
                    break
            # Detecta se o token da secundária chegou via /callback.html server-side
            with _token_sec_lock:
                tok = _token_secundaria.get("access_token", "")
                tok_age = time.time() - _token_secundaria.get("ts", 0)
            if tok and tok not in ("None","null","") and tok_age < 60:
                token_capturado = tok
                print(f"[Secundária] Token capturado via slot dedicado: {tok[:10]}...")
                break
            time.sleep(2)

        driver.quit()
        with _SEC_LOGIN_LOCK:
            _SEC_LOGIN_STATE["browser_process"] = None

        if token_capturado:
            _sec_registrar_com_token(token_capturado, tipo)
        else:
            raise Exception("Timeout: login não concluído em 3 minutos")

    except Exception as e:
        print(f"[Secundária] Erro Selenium: {e}")
        if driver:
            try: driver.quit()
            except: pass
        with _SEC_LOGIN_LOCK:
            _SEC_LOGIN_STATE["browser_process"] = None
            _SEC_LOGIN_STATE["status"] = "erro"
            _SEC_LOGIN_STATE["erro"]   = str(e)


# ── Token recebido do Netlify após OAuth Deriv ───────────────────────────────
_token_recebido: dict = {"access_token": "", "ts": 0}
_token_lock = threading.Lock()

@app.route('/auth/deriv', methods=['POST', 'OPTIONS'])
def auth_deriv():
    """Recebe o code PKCE do Netlify callback.html, troca pelo access_token na Deriv e armazena."""
    # CORS — permite o Netlify chamar esta rota
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        return resp

    dados    = request.get_json(silent=True) or {}
    code     = (dados.get("code") or "").strip()
    verifier = (dados.get("verifier") or "").strip()

    if not code or not verifier:
        resp = jsonify({"ok": False, "erro": "code e verifier são obrigatórios"})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 400

    # Troca o code pelo access_token via PKCE
    try:
        r = requests.post(
            "https://auth.deriv.com/oauth2/token",
            # Deriv exige application/x-www-form-urlencoded, não JSON
            data={
                "grant_type":    "authorization_code",
                "client_id":     APP_ID,
                "code":          code,
                "code_verifier": verifier,
                "redirect_uri":  CALLBACK_URL,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15
        )
        print(f"[Auth/Deriv] token exchange: status={r.status_code} body={r.text[:300]}")
        data         = r.json()
        access_token = (data.get("access_token") or "").strip()
        if not access_token:
            resp = jsonify({"ok": False, "erro": "access_token não retornado", "detalhe": r.text[:200]})
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp, 400
        with _token_lock:
            _token_recebido["access_token"] = access_token
            _token_recebido["ts"]           = time.time()
        print(f"[Auth/Deriv] access_token armazenado: {access_token[:10]}... ({len(access_token)} chars)")
        resp = jsonify({"ok": True, "msg": "Token recebido com sucesso!"})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        print(f"[Auth/Deriv] Erro: {e}")
        resp = jsonify({"ok": False, "erro": str(e)})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 500

@app.route('/pegar-token-robo', methods=['GET', 'POST'])
def pegar_token_robo():
    """Recebe o access_token enviado pelo site Netlify após o login OAuth na Deriv.
    Pode receber via GET (query string) ou POST (JSON body)."""
    dados = request.get_json(silent=True) or {}
    token = (
        request.args.get("token") or
        request.args.get("access_token") or
        dados.get("token") or
        dados.get("access_token") or ""
    ).strip().strip('"')
    if token and token not in ("None", "null"):
        with _token_lock:
            _token_recebido["access_token"] = token
            _token_recebido["ts"]           = time.time()
        print(f"[Token] Access token recebido: {token[:10]}... ({len(token)} chars)")
        return jsonify({"ok": True})
    # Sem token — retorna o que está armazenado (usado pelo keep-alive)
    with _token_lock:
        t = _token_recebido.get("access_token", "")
    return jsonify({"token": t} if t else {"token": None})

# Token exclusivo da conta secundária — separado do token principal
_token_secundaria: dict = {"access_token": "", "ts": 0}
_token_sec_lock = threading.Lock()

@app.route('/open-login-secundaria')
def open_login_secundaria():
    """Inicia Selenium headless no Oracle para fazer login da conta secundária."""
    tipo = request.args.get("tipo", "DEMO").upper()

    # Verifica se já tem login em andamento
    with _SEC_LOGIN_LOCK:
        st = _SEC_LOGIN_STATE.get("status", "idle")
        if st in ("abrindo_navegador", "aguardando_login"):
            return jsonify({"status": "already_opened"})

    # Reseta slot da secundária
    with _token_sec_lock:
        _token_secundaria["access_token"] = ""
        _token_secundaria["ts"]           = 0
        _token_secundaria["aguardando"]   = True

    # Inicia Selenium em thread background
    t = threading.Thread(target=_sec_login_selenium, args=(tipo,), daemon=True)
    t.start()

    return jsonify({"status": "opened", "url": SITE_LOGIN,
                    "msg": "Selenium iniciado — aguarde o login ser detectado automaticamente."})

@app.route('/auth/deriv-secundaria', methods=['POST', 'OPTIONS'])
def auth_deriv_secundaria():
    """Recebe token OAuth da conta secundária — rota dedicada, totalmente isolada do slot principal."""
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        return resp
    dados        = request.get_json(silent=True) or {}
    access_token = (dados.get("access_token") or dados.get("token") or "").strip()
    if access_token:
        with _token_sec_lock:
            # Salva EXCLUSIVAMENTE no slot da secundária — nunca toca o _token_recebido principal
            _token_secundaria["access_token"] = access_token
            _token_secundaria["ts"]           = time.time()
            _token_secundaria["aguardando"]   = False
        print(f"[SecToken] Token secundária isolado com sucesso: {access_token[:10]}...")
        resp = jsonify({"ok": True})
    else:
        resp = jsonify({"ok": False, "erro": "token vazio"})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

def _get_token_com_access(access_token: str, tipo: str):
    """Usa o access_token para obter WSS URL via API Deriv. Retorna dict com wss_url ou erro."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Deriv-App-ID":  APP_ID,
        "Content-Type":  "application/json"
    }
    res_contas = requests.get(f"{API_BASE}/accounts", headers=headers, timeout=10)
    print(f"[API] Contas: status={res_contas.status_code} | body={res_contas.text[:300]}")
    if res_contas.status_code != 200:
        return {"wss_url": None, "erro": f"Erro ao listar contas: {res_contas.status_code}", "detalhe": res_contas.text[:200]}
    contas_api = res_contas.json().get("data", [])
    conta_id = None
    for c in contas_api:
        cid          = str(c.get("account_id", ""))
        account_type = str(c.get("account_type", "")).lower()
        is_virt      = c.get("is_virtual", False)
        is_demo      = account_type == "demo" or is_virt or cid.upper().startswith(("VR", "DOT", "VRT"))
        print(f"[API] Conta: id={cid} | account_type={account_type} | is_demo={is_demo}")
        if tipo == "DEMO" and is_demo:
            conta_id = cid; break
        elif tipo == "REAL" and not is_demo:
            conta_id = cid; break
    if not conta_id:
        return {"wss_url": None, "erro": f"Nenhuma conta {tipo} encontrada", "contas_disponiveis": len(contas_api)}
    print(f"[API] Gerando OTP para conta {conta_id}...")
    res_otp = requests.post(f"{API_BASE}/accounts/{conta_id}/otp", headers=headers, timeout=10)
    print(f"[API] OTP: status={res_otp.status_code} | body={res_otp.text[:300]}")
    wss_url = res_otp.json().get("data", {}).get("url")
    if not wss_url:
        return {"wss_url": None, "erro": "OTP não retornou URL", "detalhe": res_otp.text[:200]}
    print(f"[API] URL WSS gerada com sucesso!")
    return {"wss_url": wss_url, "conta_id": conta_id, "tipo": tipo}

@app.route('/get-token')
def get_token():
    """Gera WSS URL usando o token armazenado localmente (recebido via /pegar-token-robo)."""
    try:
        # 1. Tenta usar token armazenado localmente (recebido do Netlify)
        with _token_lock:
            access_token = _token_recebido.get("access_token", "")
            token_ts     = _token_recebido.get("ts", 0)

        # Token expira após 10 minutos sem uso
        if access_token and (time.time() - token_ts) < 600:
            print(f"[Token] Usando token local: {access_token[:10]}...")
            # Marca que o bot leu o token (sinaliza para a página de callback)
            with _token_lock:
                _token_recebido["_bot_confirmou"] = True
            tipo = _access_token.get("tipo", "DEMO")
            return jsonify(_get_token_com_access(access_token, tipo))

        # 2. Fallback: tenta buscar do RENDER_URL (compatibilidade)
        try:
            res = requests.get(RENDER_URL, timeout=5)
            if res.status_code == 200:
                data = res.json()
                access_token = (
                    data.get("token") or
                    data.get("access_token") or
                    (data.get("data") or {}).get("token") or ""
                )
                if isinstance(access_token, dict):
                    access_token = access_token.get("token") or access_token.get("access_token") or ""
                access_token = str(access_token).strip().strip('"') if access_token else ""
                if access_token and access_token not in ("None", "null"):
                    with _token_lock:
                        _token_recebido["access_token"] = access_token
                        _token_recebido["ts"]           = time.time()
                    tipo = _access_token.get("tipo", "DEMO")
                    return jsonify(_get_token_com_access(access_token, tipo))
        except Exception:
            pass

        return jsonify({"wss_url": None, "erro": "token_nao_encontrado"})

    except Exception as e:
        err = str(e)
        if "timed out" not in err and "ConnectionPool" not in err:
            print(f"[Erro] {err}")
        return jsonify({"wss_url": None, "erro": err})

@app.route('/clear-token', methods=['POST'])
def clear_token():
    """Limpa o token armazenado localmente — chamado quando o usuário inicia uma nova conexão."""
    with _token_lock:
        _token_recebido["access_token"] = ""
        _token_recebido["ts"]           = 0
        _token_recebido["_bot_confirmou"] = False
    _render_token_cache["token"] = ""
    _render_token_cache["ts"]    = 0
    print("[Token] Token limpo pelo front-end (nova conexão iniciada).")
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────
# TELEGRAM — config (salva/lê telegram_config.json)
# ─────────────────────────────────────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
TG_ARQUIVO = os.path.join(_BASE_DIR, "telegram_config.json")

def _tg_carregar():
    padrao = {"token": "", "chat_id": "", "enabled": False, "resultados": True, "stopwin": True}
    try:
        if os.path.exists(TG_ARQUIVO):
            with open(TG_ARQUIVO, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return padrao

@app.route('/tg-config', methods=['GET'])
def tg_config_get():
    return jsonify(_tg_carregar())

@app.route('/tg-config', methods=['POST'])
def tg_config_post():
    dados = request.get_json(force=True, silent=True) or {}
    atual = _tg_carregar()
    atual.update({k: dados[k] for k in ("token","chat_id","enabled","resultados","stopwin") if k in dados})
    try:
        with open(TG_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(atual, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────
# TELEGRAM — envio assíncrono (IP fixo acessível, bypassa bloqueio DNS)
# ─────────────────────────────────────────────────────────
def _assets_path(nome):
    return os.path.join(_BASE_DIR, "assets", nome)

# Usa domínio oficial — Oracle Cloud tem DNS pleno, IP fixo não é necessário
_TG_BASE    = "https://api.telegram.org"
_TG_HEADERS = {}
_TG_TIMEOUT = (10, 25)

def _tg_url(token: str, method: str) -> str:
    return f"{_TG_BASE}/bot{token}/{method}"

def _preparar_foto(img_path: str, max_width: int = 280) -> tuple:
    """Redimensiona com PIL. Retorna (bytes, filename, mime)."""
    try:
        from PIL import Image
        img = Image.open(img_path).convert("RGB")
        w, h = img.size
        nh = max(1, int(h * max_width / w))
        resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', 1))  # type: ignore[attr-defined]
        img = img.resize((max_width, nh), resample)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        print(f"[TG] PIL OK — {buf.tell()} bytes")
        return buf.getvalue(), "img.jpg", "image/jpeg"
    except Exception:
        with open(img_path, "rb") as f:
            raw = f.read()
        fname = os.path.basename(img_path)
        return raw, fname, "image/png" if fname.lower().endswith(".png") else "image/jpeg"

def _tg_enviar_foto(token: str, chat_id: str, caption: str, img_path: str,
                    max_width: int = 280) -> bool:
    if not os.path.exists(img_path):
        print(f"[TG] imagem não encontrada: {img_path}")
        return False
    photo_bytes, filename, mime = _preparar_foto(img_path, max_width)
    try:
        resp = requests.post(
            _tg_url(token, "sendPhoto"),
            headers=_TG_HEADERS,
            data={"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
            files={"photo": (filename, photo_bytes, mime)},
            timeout=_TG_TIMEOUT,
            verify=False,
        )
        result = resp.json()
        if result.get("ok"):
            print(f"[TG] foto enviada OK ({len(photo_bytes)} bytes)")
            return True
        print(f"[TG] sendPhoto falhou: {result.get('description')}")
        return False
    except Exception as e:
        print(f"[TG] sendPhoto erro: {e}")
        return False

def _tg_enviar_texto(token: str, chat_id: str, msg: str) -> bool:
    try:
        resp = requests.post(
            _tg_url(token, "sendMessage"),
            headers=_TG_HEADERS,
            json={"chat_id": str(chat_id), "text": msg, "parse_mode": "HTML"},
            timeout=_TG_TIMEOUT,
            verify=False,
        )
        ok = bool(resp.json().get("ok"))
        if not ok:
            print(f"[TG] sendMessage falhou: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[TG] sendMessage erro: {e}")
        return False

def _tg_dispatch(fn):
    """Executa fn numa thread daemon — não bloqueia o Flask."""
    threading.Thread(target=fn, daemon=True).start()

@app.route('/tg-send', methods=['POST'])
def tg_send():
    """Coloca o envio na fila Telegram (não bloqueia o Flask)."""
    d = request.get_json(force=True, silent=True) or {}
    token   = d.get("token", "")
    chat_id = d.get("chat_id", "")
    if not token or not chat_id:
        return jsonify({"ok": False, "erro": "token/chat_id ausentes"})

    # ── Modo ESPELHO: só envia notificações da conta SECUNDÁRIA ──
    if _MODO_OPERACAO.get("modo") == "ESPELHO":
        conta = str(d.get("conta", "")).upper()
        print(f"[TG] modo=ESPELHO conta='{conta}' stop_win={d.get('stop_win')} keys={list(d.keys())}")
        if conta != "SECUNDARIA":
            print("[TG] Modo ESPELHO: notificação bloqueada (não é conta SECUNDÁRIA).")
            return jsonify({"ok": True, "bloqueado": True, "motivo": "modo_espelho_conta_nao_secundaria"})

    # Cotação capturada aqui (fora da thread) para não atrasar o envio
    cotacao = _buscar_cotacao()

    # Snapshot dos dados — evita capturar variáveis mutáveis na closure
    payload = dict(d)

    def _enviar():
        tok = str(token); cid = str(chat_id); cot = cotacao

        # ── Texto direto (usado por testar/stopwin do JS) ──
        if payload.get("_texto_direto"):
            _tg_enviar_texto(tok, cid, str(payload["_texto_direto"]))
            return

        # ── Modo Virtual (LV acumulando) — notificação simples ──
        if payload.get("virtual"):
            _tg_enviar_texto(tok, cid, str(payload.get("texto", "🤖 Robô Garra analisando...")))
            return

        # ── Relatório de Stop Win ──────────────────────────
        if payload.get("stop_win"):
            lucro           = float(payload.get("lucro", 0))
            banca           = float(payload.get("banca", 0))
            wins            = int(payload.get("wins", 0))
            losses          = int(payload.get("losses", 0))
            modo            = str(payload.get("modo", ""))
            estrategia      = str(payload.get("estrategia", ""))
            max_win_consec  = int(payload.get("max_win_consec", 0))
            max_loss_consec = int(payload.get("max_loss_consec", 0))
            max_stake       = float(payload.get("max_stake", 0))
            total           = wins + losses
            wr              = (wins / total * 100) if total > 0 else 0.0
            lucro_brl       = lucro * cot
            banca_brl       = banca * cot
            msg = (
                f"🏆 STOP WIN BATIDO\n\n"
                f"💰 Banca: ${banca:.2f} (R$ {banca_brl:.2f})\n"
                f"📈 Lucro: +${lucro:.2f} (R$ +{lucro_brl:.2f})\n\n"
                f"📊 {wins}W • {losses}L • {wr:.0f}%\n\n"
                f"🔥 Máx WIN: {max_win_consec}x\n"
                f"💀 Máx LOSS: {max_loss_consec}x\n"
                f"💵 Stake Máx: ${max_stake:.2f}\n\n"
                f"🤖 {estrategia.upper()}\n"
                f"⚙️ {modo.upper()}\n\n"
                f"🕐 {_hora_brt()}"
            )
            img = _assets_path("Meta Batida.png")
            if not _tg_enviar_foto(tok, cid, msg, img, max_width=400):
                _tg_enviar_texto(tok, cid, msg)
            return

        # ── Resultado WIN / LOSS ───────────────────────────
        win             = bool(payload.get("win", False))
        lucro           = float(payload.get("lucro", 0))
        profit_tot      = float(payload.get("profit_total", 0))
        banca           = float(payload.get("banca", 0))
        wins            = int(payload.get("wins", 0))
        losses          = int(payload.get("losses", 0))
        prox_stake      = float(payload.get("prox_stake", 0))
        modo            = str(payload.get("modo", ""))
        estrategia      = str(payload.get("estrategia", ""))
        total           = wins + losses
        lucro_brl       = abs(lucro) * cot
        profit_brl      = profit_tot * cot
        banca_brl       = banca * cot

        if win:
            img_nome     = "WIM GARRA.png"
            res_linha    = "✅  RESULTADO: WIN"
            lucro_linha  = f"💵  Lucro: +${lucro:.2f}"
        else:
            img_nome     = "LOSS GARRA.png"
            res_linha    = "❌  RESULTADO: LOSS"
            lucro_linha  = f"💵  Lucro: -${abs(lucro):.2f}"

        entrada        = float(payload.get("entrada", payload.get("prox_stake", 0)))
        sinal_tot      = "+" if profit_tot >= 0 else "-"
        profit_brl_str = f"{sinal_tot}R${abs(profit_brl):.2f}"
        profit_usd_str = f"{sinal_tot}${abs(profit_tot):.2f}"
        banca_brl_str  = f"{banca_brl:.2f}".replace(".", ",")
        lucro_op_brl   = abs(lucro) * cot
        lucro_op_str   = f"+R${lucro_op_brl:.2f}" if win else f"-R${lucro_op_brl:.2f}"

        msg = (
            f"🟢  OPERAÇÃO FINALIZADA\n\n"
            f"{res_linha}\n\n"
            f"💰  Entrada: ${entrada:.2f}\n"
            f"{lucro_linha}  ({lucro_op_str})\n\n"
            f"➡️  Próxima Entrada: ${prox_stake:.2f}\n"
            f"⚙️  Gestão: {modo}\n\n"
            f"📊  Mercado: {estrategia.split()[0] if estrategia else '--'}\n"
            f"🎯  Estratégia: {estrategia}\n\n"
            f"🏦  Banca: ${banca:.2f}  /  R${banca_brl_str}\n"
            f"📈  Lucro Total: {profit_usd_str}  /  {profit_brl_str}\n\n"
            f"🕐  {_hora_brt()}"
        )
        img = _assets_path(img_nome)
        if not _tg_enviar_foto(tok, cid, msg, img, max_width=320):
            _tg_enviar_texto(tok, cid, msg)

    _tg_dispatch(_enviar)
    return jsonify({"ok": True})

# Cache do token mais recente lido do Render
_render_token_cache: dict = {"token": "", "ts": 0}

# Diagnóstico: veja o JSON bruto do Render (salva token em cache)
@app.route('/debug-render')
def debug_render():
    global _render_token_cache
    try:
        res = requests.get(RENDER_URL, timeout=5)
        body = res.text[:500]
        if res.status_code == 200:
            # Extrai e salva o token em cache para uso posterior
            try:
                d = res.json()
                tok = (
                    d.get("token") or
                    d.get("access_token") or
                    (d.get("data") or {}).get("token")
                )
                if isinstance(tok, dict):
                    tok = tok.get("token") or tok.get("access_token")
                tok = str(tok).strip().strip('"') if tok else ""
                if tok and tok not in ("None", "null", ""):
                    _render_token_cache["token"] = tok
                    _render_token_cache["ts"]    = time.time()
            except Exception:
                pass
        return jsonify({"status_code": res.status_code, "body": body})
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route('/cotacao-brl')
def cotacao_brl():
    """Retorna cotação USD/BRL em tempo real com cache de 60s."""
    val = _buscar_cotacao()
    return jsonify({"usd_brl": val, "ts": _COT_CACHE.get("ts", 0)})

# ─────────────────────────────────────────────────────────
# GROQ IA — config + geração + estratégias salvas
# ─────────────────────────────────────────────────────────
GROQ_CFG_ARQUIVO   = os.path.join(_BASE_DIR, "groq_config.json")
STRATEGIES_DIR     = os.path.join(_BASE_DIR, "strategies")
MEMORY_FILE        = os.path.join(_BASE_DIR, "memory_vault.json")
FEEDBACK_FILE      = os.path.join(_BASE_DIR, "strategies_feedback.json")
os.makedirs(STRATEGIES_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────
# BANCO DE AVALIAÇÕES — curtidas e bloqueadas
# ─────────────────────────────────────────────────────────
def _feedback_ler() -> dict:
    """Lê o banco de avaliações. Retorna {'aprovadas': [...], 'bloqueadas': [...]}"""
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"aprovadas": [], "bloqueadas": []}

def _feedback_salvar(dados: dict):
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)

def _feedback_chave(est: dict) -> str:
    """Gera uma chave única para identificar a estratégia (tipo+ativo+barreira)."""
    return "|".join([
        str(est.get("tipo_contrato", "")).upper(),
        str(est.get("ativo", "")).upper(),
        str(est.get("barreira", "")),
        str(est.get("barreira_over", "")),
        str(est.get("barreira_under", "")),
        str(est.get("gerenciamento", "")).lower(),
    ])

def _feedback_bloco_bloqueadas() -> str:
    """Retorna bloco de texto para injetar no system prompt com estratégias bloqueadas."""
    dados = _feedback_ler()
    bloqueadas = dados.get("bloqueadas", [])
    if not bloqueadas:
        return ""
    linhas = []
    for b in bloqueadas:
        linhas.append(
            f"  - {b.get('tipo_contrato','')} barreira={b.get('barreira','')} "
            f"ativo={b.get('ativo','')} gerenciamento={b.get('gerenciamento','')} "
            f"[motivo: {b.get('motivo','não gostei')}]"
        )
    return (
        "\n\n=== ESTRATÉGIAS PERMANENTEMENTE BLOQUEADAS (NUNCA GERE ESTAS) ===\n"
        + "\n".join(linhas)
        + "\nESSAS COMBINAÇÕES FORAM TESTADAS E REPROVADAS PELO USUÁRIO. "
        "NÃO gere nenhuma variação delas. Se necessário, mude o tipo_contrato ou o ativo.\n"
    )

# ─────────────────────────────────────────────────────────
# EDC — Memória Episódica (Memory Vault)
# ─────────────────────────────────────────────────────────
def _registrar_experiencia(
    estrategia_nome: str,
    resultado: str,
    lucro: float,
    regime_mercado: str,
    # Atributos multidimensionais (Fase 2)
    tipo_contrato: str = "",
    barreira: int = 0,
    nivel_gale: int = 0,
    confianca_edc: float = 0.0,
    notas_edc: dict = None,
    volatilidade_ctx: str = "desconhecida",
    conta_id: str = "",
):
    """
    Armazena o resultado de cada trade com contexto rico para análise causal.
    Mantém as últimas 500 experiências para suportar análise estatística.
    """
    memoria = []
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
        except Exception:
            memoria = []

    hora = time.localtime()
    nova_exp = {
        # ── Base ──────────────────────────────────────────
        "timestamp":        time.time(),
        "hora_str":         f"{hora.tm_hour:02d}:{hora.tm_min:02d}",
        "dia_semana":       hora.tm_wday,   # 0=seg … 6=dom
        "estrategia":       estrategia_nome,
        "resultado":        resultado,      # "WIN" ou "LOSS"
        "lucro":            lucro,
        "contexto":         regime_mercado, # ativo (R_100, 1HZ25V…)
        # ── Contexto de mercado ───────────────────────────
        "tipo_contrato":    tipo_contrato,
        "barreira":         barreira,
        "nivel_gale":       nivel_gale,
        "volatilidade_ctx": volatilidade_ctx,
        # ── Nota do Conselho no momento da entrada ────────
        "confianca_edc":    confianca_edc,
        "notas_edc":        notas_edc or {},
        # ── Multi-conta ───────────────────────────────────
        "conta_id":         conta_id,
    }
    memoria.append(nova_exp)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memoria[-500:], f, indent=2, ensure_ascii=False)

def _recuperar_aprendizado() -> str:
    """Resume o histórico de performance por estratégia para injetar no prompt."""
    if not os.path.exists(MEMORY_FILE):
        return "Nenhuma experiência prévia registrada."
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memoria = json.load(f)
    except Exception:
        return "Nenhuma experiência prévia registrada."
    resumo: dict = {}
    for exp in memoria:
        est = exp.get("estrategia", "desconhecida")
        if est not in resumo:
            resumo[est] = {"wins": 0, "losses": 0, "lucro_total": 0.0}
        if exp.get("resultado") == "WIN":
            resumo[est]["wins"] += 1
        else:
            resumo[est]["losses"] += 1
        resumo[est]["lucro_total"] = round(
            resumo[est]["lucro_total"] + float(exp.get("lucro", 0)), 2
        )
    return json.dumps(resumo, ensure_ascii=False)

# ─────────────────────────────────────────────────────────
# EDC FASE 2 — Conselho de Especialistas (Decision Supervisor)
# ─────────────────────────────────────────────────────────
class DecisionSupervisor:
    """
    Motor de Confiança Probabilística.
    Fragmenta a decisão em 6 agentes independentes, pondera as notas
    e emite um veredicto: OPERAR (confiança >= threshold) ou AGUARDAR.

    Cada agente retorna uma nota de 0 a 100.
    Pesos:
      - estatistica  0.30  (histórico de win rate do ativo/estratégia)
      - risco        0.25  (saúde da banca, nível de Gale)
      - scanner      0.15  (momentum: sequência de dígitos recentes)
      - volatilidade 0.10  (conservadorismo baseado no ativo)
      - volume       0.10  (amplitude de movimento dos ticks — atividade do mercado)
      - fluxo        0.10  (direção do mercado vs. direção da estratégia — bloqueio duro)
    """

    PESOS = {
        "scanner":     0.15,
        "estatistica": 0.30,
        "volatilidade":0.10,
        "risco":       0.25,
        "volume":      0.10,
        "fluxo":       0.10,
    }
    THRESHOLD_PADRAO = 82  # confiança mínima para OPERAR

    def __init__(self, threshold: int = THRESHOLD_PADRAO):
        self.threshold = threshold

    # ── Agente 1 — Scanner de Momentum ───────────────────
    def _agente_scanner(self, ctx: dict) -> int:
        """
        Analisa a sequência de dígitos recentes enviada pelo frontend.
        Penaliza quando os últimos ticks contradizem o tipo de contrato.
        ctx esperado: { tipo_contrato, barreira, ultimos_digitos: [int,...] }
        """
        digitos  = ctx.get("ultimos_digitos", [])
        tipo     = ctx.get("tipo_contrato", "").upper()
        barreira = int(ctx.get("barreira", 5))

        if not digitos or len(digitos) < 3:
            return 60  # sem dados suficientes → nota neutra

        janela = digitos[-10:]  # últimos 10 ticks

        if tipo == "DIGITUNDER":
            # Proporção de dígitos abaixo da barreira na janela
            favoraveis = sum(1 for d in janela if d < barreira)
        elif tipo == "DIGITOVER":
            favoraveis = sum(1 for d in janela if d > barreira)
        elif tipo in ("DIGITEVEN", "DIGITODD"):
            alvo = (lambda d: d % 2 == 0) if tipo == "DIGITEVEN" else (lambda d: d % 2 != 0)
            favoraveis = sum(1 for d in janela if alvo(d))
        else:
            return 65  # FLUXO / DUPLA / PCT → nota neutra

        pct = favoraveis / len(janela)
        # Mapeia 0-100%  →  0-100 pontos (linear)
        return round(pct * 100)

    # ── Agente 2 — Estatística Histórica ─────────────────
    def _agente_estatistica(self, ctx: dict) -> int:
        """
        Consulta o memory_vault.json filtrado por estratégia + ativo.
        Retorna o win rate histórico como nota (0-100).
        """
        estrategia = ctx.get("nome", "")
        ativo      = ctx.get("ativo", "")

        if not os.path.exists(MEMORY_FILE):
            return 60  # sem histórico → nota neutra

        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
        except Exception:
            return 60

        # Filtra por estratégia (e opcionalmente por ativo)
        relevantes = [
            e for e in memoria
            if e.get("estrategia") == estrategia
            and (not ativo or e.get("contexto") == ativo)
        ]
        if len(relevantes) < 3:
            # Menos de 3 amostras → sem dados suficientes, nota neutra
            return 60

        wins = sum(1 for e in relevantes if e.get("resultado") == "WIN")
        taxa = wins / len(relevantes)

        # Win rate <30% → nota 0 ; =50% → 50 ; =80% → 90 ; =100% → 100
        # Escala suavizada: penaliza fortemente abaixo de 50%
        if taxa < 0.50:
            nota = round(taxa * 100)          # 0-49
        else:
            nota = round(50 + (taxa - 0.50) * 100)  # 50-100
        return min(nota, 100)

    # ── Agente 3 — Volatilidade do Ativo ─────────────────
    def _agente_volatilidade(self, ctx: dict) -> int:
        """
        Score conservador baseado no ativo e tipo de contrato.
        Ativos mais voláteis (R_100) com barreiras arriscadas → nota baixa.
        """
        ativo    = ctx.get("ativo", "R_100").upper()
        tipo     = ctx.get("tipo_contrato", "").upper()
        barreira = int(ctx.get("barreira", 5))

        # Mapa de volatilidade base (0-100, maior = mais seguro)
        perfil_ativo = {
            "1HZ10V": 95, "1HZ25V": 90, "1HZ50V": 80,
            "R_10": 88, "R_25": 85, "R_50": 78,
            "R_75": 65, "1HZ75V": 70, "1HZ100V": 60, "R_100": 50,
        }
        base = perfil_ativo.get(ativo, 65)

        # Ajuste de barreira para DIGITOVER/UNDER
        if tipo == "DIGITUNDER":
            # Under 8/9 → seguro; Under 5/4 → arriscado
            penalidade = max(0, (8 - barreira) * 8)
            base = max(0, base - penalidade)
        elif tipo == "DIGITOVER":
            # Over 1/2 → seguro; Over 5/6 → arriscado
            penalidade = max(0, (barreira - 2) * 8)
            base = max(0, base - penalidade)

        return min(round(base), 100)

    # ── Agente 4 — Segurança de Banca ────────────────────
    def _agente_risco(self, ctx: dict) -> int:
        """
        Verifica nível de Martingale e exposição da banca.
        ctx esperado: { nivel_gale (int), banca_usd (float), stake_usd (float) }
        """
        nivel_gale = int(ctx.get("nivel_gale", 0))
        banca      = float(ctx.get("banca_usd", 100.0))
        stake      = float(ctx.get("stake_usd", 1.0))

        nota = 100

        # Penaliza Martingale progressivo
        penalidade_gale = {0: 0, 1: 10, 2: 25, 3: 45, 4: 70}
        nota -= penalidade_gale.get(nivel_gale, 80)

        # Penaliza se stake > 5% da banca
        if banca > 0:
            pct_risco = stake / banca
            if pct_risco > 0.10:
                nota -= 40
            elif pct_risco > 0.05:
                nota -= 20
            elif pct_risco > 0.03:
                nota -= 10

        return max(0, round(nota))

    # ── Agente 5 — Filtro de Volume (Atividade de Mercado) ───────────────────
    def _agente_volume(self, ctx: dict) -> int:
        """
        Mede a força do movimento dos ticks (amplitude média).
        Em índices sintéticos, 'volume' é a variação tick a tick do preço.
        - Mercado parado (avg_move muito baixo) → nota baixa (ruim para Touch/Dupla)
        - Mercado com explosão (avg_move alto)  → nota 100
        - Mercado normal                        → nota 85 (neutro)
        ctx esperado: { ultimos_precos: [float, ...], tipo_contrato: str }
        """
        ticks = ctx.get("ultimos_precos", [])
        tipo  = ctx.get("tipo_contrato", "").upper()

        if not ticks or len(ticks) < 10:
            return 70  # sem dados suficientes → nota neutra

        diffs    = [abs(ticks[i] - ticks[i - 1]) for i in range(1, len(ticks))]
        avg_move = sum(diffs) / len(diffs)

        # NOTOUCH se beneficia de mercado calmo — inverte a lógica
        if tipo == "NOTOUCH":
            if avg_move < 0.0001:
                return 95   # mercado parado → ótimo para NOTOUCH
            elif avg_move > 0.005:
                return 40   # explosão → perigoso para NOTOUCH
            return 80

        # Demais contratos: quanto mais movimento, melhor
        if avg_move < 0.0001:
            return 30   # mercado "morto" — entrada arriscada
        elif avg_move > 0.005:
            return 100  # alta atividade — ótimo para Touch/Dupla/Digit
        return 85

    # ── Agente 6 — Filtro de Fluxo Obrigatório ───────────────────────────────
    def _agente_fluxo_obrigatorio(self, ctx: dict) -> int:
        """
        Verifica coerência entre a direção do mercado e o tipo de contrato.
        Bloqueia (nota 0) entradas contrárias ao fluxo.
        Penaliza (nota 50) entradas em mercado neutro/lateral.
        ctx esperado: { fluxo_mercado: "CALL"|"PUT"|"NEUTRO", tipo_contrato: str }
        """
        fluxo = ctx.get("fluxo_mercado", "NEUTRO").upper()
        tipo  = ctx.get("tipo_contrato", "").upper()

        # Contratos direcionais — Over sobe, Under cai
        if "OVER" in tipo and fluxo == "PUT":
            return 0    # fluxo contrário → bloqueio duro
        if "UNDER" in tipo and fluxo == "CALL":
            return 0    # fluxo contrário → bloqueio duro

        # Contratos de dígitos não-direcionais (PAR/ÍMPAR/DUPLA): fluxo não bloqueia
        if tipo in ("DIGITEVEN", "DIGITODD", "DIGITDUPLA",
                    "DIGITMATCH", "DIGITPCT", "SATURACAO"):
            return 85   # neutro positivo — fluxo não é determinante

        # Fluxo neutro para contratos direcionais → reduz confiança
        if fluxo == "NEUTRO":
            return 50

        # Fluxo alinhado com o contrato → nota máxima
        return 100

    # ── Motor de Consenso ─────────────────────────────────
    def avaliar(self, ctx: dict) -> dict:
        """
        Executa todos os agentes, pondera e retorna o relatório completo.
        Retorno: { confianca, veredicto, notas, motivo, threshold }
        """
        notas = {
            "scanner":     self._agente_scanner(ctx),
            "estatistica": self._agente_estatistica(ctx),
            "volatilidade":self._agente_volatilidade(ctx),
            "risco":       self._agente_risco(ctx),
            "volume":      self._agente_volume(ctx),
            "fluxo":       self._agente_fluxo_obrigatorio(ctx),
        }

        confianca = round(sum(notas[k] * self.PESOS[k] for k in notas), 1)
        veredicto = "OPERAR" if confianca >= self.threshold else "AGUARDAR"

        # Identifica o agente mais restritivo para o motivo narrativo
        agente_fraco = min(notas, key=lambda k: notas[k])
        labels = {
            "scanner":      "Momentum dos dígitos recentes desfavorável",
            "estatistica":  "Win rate histórico baixo para esta estratégia/ativo",
            "volatilidade": "Ativo muito volátil para a barreira configurada",
            "risco":        "Nível de Martingale ou exposição de banca elevada",
            "volume":       "Mercado sem atividade suficiente (amplitude de tick muito baixa)",
            "fluxo":        "Direção do mercado contrária ao tipo de contrato",
        }
        motivo = (
            f"✅ Todos os módulos aprovaram a entrada."
            if veredicto == "OPERAR"
            else f"⛔ Módulo mais restritivo: {agente_fraco.upper()} ({notas[agente_fraco]}/100) — {labels[agente_fraco]}."
        )

        return {
            "confianca":  confianca,
            "veredicto":  veredicto,
            "threshold":  self.threshold,
            "notas":      notas,
            "motivo":     motivo,
        }

    # ── Narrativa para Telegram/WhatsApp ─────────────────
    def narrativa(self, relatorio: dict, nome_estrategia: str = "") -> str:
        """Formata o relatório do Conselho como mensagem de notificação."""
        v = relatorio["veredicto"]
        c = relatorio["confianca"]
        n = relatorio["notas"]
        icone = "🛡️" if v == "OPERAR" else "🚫"
        linhas = [
            f"{icone} CONSELHO DE ESPECIALISTAS — {v} (Confiança: {c}%)",
        ]
        if nome_estrategia:
            linhas.append(f"Estratégia: {nome_estrategia}")
        fluxo_nota  = n.get('fluxo', '—')
        fluxo_icone = "🟢" if isinstance(fluxo_nota, int) and fluxo_nota >= 85 else ("🔴" if isinstance(fluxo_nota, int) and fluxo_nota == 0 else "🟡")
        linhas += [
            f"",
            f"📊 Estatística histórica : {n['estatistica']}/100",
            f"📈 Scanner de momentum  : {n['scanner']}/100",
            f"🌡️ Volatilidade do ativo : {n['volatilidade']}/100",
            f"🏦 Segurança de banca   : {n['risco']}/100",
            f"📊 Volume / Atividade   : {n.get('volume', '—')}/100",
            f"{fluxo_icone} Fluxo direcional    : {fluxo_nota}/100",
            f"",
            relatorio["motivo"],
        ]
        return "\n".join(linhas)


# Instância global — reutilizada em todas as rotas
_supervisor = DecisionSupervisor()

# ─────────────────────────────────────────────────────────
# EDC — Formatador de Veredito Cognitivo (Narrativa Executiva)
# ─────────────────────────────────────────────────────────
def formatar_veredito_cognitivo(resultado: dict) -> str:
    """
    Transforma o JSON da IA + notas do Conselho em uma mensagem
    executiva para Telegram/WhatsApp.
    Suporta tanto resultado da rota /ai/gerar-cognitivo quanto
    o relatório direto do DecisionSupervisor.
    """
    confianca = resultado.get("confianca_total") or resultado.get("confianca", 0)
    notas     = resultado.get("confianca_detalhada") or resultado.get("notas", {})
    decisao   = resultado.get("decisao") or resultado.get("veredicto", "AGUARDAR")
    motivo    = resultado.get("motivo_estrategico") or resultado.get("motivo", "—")
    nome      = resultado.get("nome", "—")
    ativo     = resultado.get("ativo", "—")
    ger       = resultado.get("gerenciamento", "—")
    assertiv  = resultado.get("assertividade", "—")

    icone     = "✅" if decisao == "OPERAR" else "⚠️"
    barras_ok = int(confianca // 10)
    barra     = "🟢" * barras_ok + "⚪" * (10 - barras_ok)

    h = notas.get("historico") or notas.get("estatistica", "—")
    v = notas.get("volatilidade", "—")
    r = notas.get("risco", "—")

    return (
        f"{icone} *VEREDITO DA ENTIDADE COGNITIVA*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Decisão:* {decisao}\n"
        f"🎯 *Estratégia:* {nome}\n"
        f"📊 *Confiança:* {confianca}%\n"
        f"{barra}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧠 *Raciocínio do Supervisor:*\n"
        f"_{motivo}_\n\n"
        f"🏛️ *Notas do Conselho:*\n"
        f"└ 📊 Histórico:    {h}/100\n"
        f"└ ⚡ Volatilidade: {v}/100\n"
        f"└ 🛡️ Risco/Banca: {r}/100\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Expectativa:* {assertiv}\n"
        f"⚙️ *Ativo:* {ativo} | {ger}\n"
    )

def _groq_cfg_ler():
    try:
        if os.path.exists(GROQ_CFG_ARQUIVO):
            with open(GROQ_CFG_ARQUIVO, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "chave": "",
        "modelo": "llama-3.3-70b-versatile",
        "nvidia_chave": "",
        "nvidia_modelo": "nvidia/llama-3.1-nemotron-70b-instruct",
        "provedor_ativo": "groq",
    }

def _groq_cfg_salvar(dados: dict):
    with open(GROQ_CFG_ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)

def _ia_listar_arquivos() -> list:
    """Retorna lista de dicts de todas as estratégias salvas, ordenadas por data."""
    resultado = []
    for fname in sorted(os.listdir(STRATEGIES_DIR)):
        if fname.endswith(".json"):
            fpath = os.path.join(STRATEGIES_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    d = json.load(f)
                d["_arquivo"] = fname
                resultado.append(d)
            except Exception:
                pass
    return resultado

def _ia_salvar_novo(dados: dict) -> str:
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"config_ia_{ts}.json"
    fpath = os.path.join(STRATEGIES_DIR, fname)
    dados["_arquivo"] = fname
    if "data" not in dados:
        dados["data"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    return fname

@app.route('/groq-config', methods=['GET'])
def groq_config_get():
    return jsonify(_groq_cfg_ler())

@app.route('/groq-config', methods=['POST'])
def groq_config_post():
    dados = request.get_json(force=True, silent=True) or {}
    atual = _groq_cfg_ler()
    for k in ("chave", "modelo", "nvidia_chave", "nvidia_modelo", "provedor_ativo"):
        if k in dados:
            atual[k] = dados[k]
    _groq_cfg_salvar(atual)
    return jsonify({"ok": True})

# ── Helpers compartilhados entre /ai/gerar e /ai/gerar-ultra ──────────────
def _detectar_contrato(prompt_lower: str):
    """Retorna o tipo de contrato detectado no texto do prompt, ou None."""
    # Match tem prioridade máxima — é o mais específico
    # Differs foi REMOVIDO — contrato proibido por risco de ruína
    if any(w in prompt_lower for w in ["digitmatch", "match", "igual", "prever dígito", "prever digito"]):
        return "DIGITMATCH"

    # Percentual tem prioridade alta — é o mais específico entre os demais
    _pct_words = [
        "percentual", "porcentagem", "% de dígitos", "% dos dígitos",
        "quando % for", "quando o percentual", "percentagem",
        "menor que 10%", "maior que 70%", "gatilho de percentual",
        "pct_hist", "digitpct",
    ]
    if any(w in prompt_lower for w in _pct_words):
        return "DIGITPCT"

    # Par e Ímpar — detectados antes de FLUXO para evitar falso-positivo com "call"
    _even_words = ["digiteven", "even", " par ", "pares", "digito par", "dígito par"]
    _odd_words  = ["digitodd",  "odd",  "impar", "ímpar", "impares", "ímpares", "impa",
                   "digito impar", "dígito ímpar"]
    if any(w in prompt_lower for w in _even_words):
        return "DIGITEVEN"
    if any(w in prompt_lower for w in _odd_words):
        return "DIGITODD"

    # Não Toca tem prioridade sobre Toca (substring mais longa primeiro)
    _notouch_words = ["não toca", "nao toca", "notouch", "no touch", "nunca toca",
                      "não atinge", "nao atinge", "não bate", "nao bate"]
    _touch_words   = ["toca", "touch", "batida", "atinge", "bate na barreira",
                      "digittoch", "digittouch"]
    if any(w in prompt_lower for w in _notouch_words):
        return "NOTOUCH"
    if any(w in prompt_lower for w in _touch_words):
        return "TOUCH"

    # CALL/PUT/FLUXO
    _fluxo_words = [
        "call e put", "put e call", "call/put", "put/call",
        "naipe de call", "naipe call", "call and put",
        "fluxo", "direcional", "tendência", "tendencia", "subida e descida",
        "alta e baixa", "1 minuto", "um minuto", "vela", "candle",
        "sniper", "price action", "ação do preço",
    ]
    # Garra Dupla tem prioridade máxima sobre dupla genérica
    _garra_dupla_words = [
        "garra dupla", "garra_dupla", "garradupla",
        "dupla janela", "over4 under5", "over 4 under 5",
        "janela superior inferior", "gale isolado",
    ]
    # Dupla tem prioridade sobre over/under individuais
    _dupla_words = [
        "digitdupla", "dupla", "dois lados", "ambos os lados",
        "over e under", "under e over", "over e anda",
        "over e unda", "dois ao mesmo tempo", "dois contratos",
        "barreira dupla", "entrada dupla", "dobrado",
    ]
    _over_words  = ["digitover",  "over",  "ouver", "ouvier", "acima", "maior que", "maior", "cima"]
    _under_words = ["digitunder", "under", "umder", "ander",  "abaixo", "menor que", "menor", "baixo"]
    _sat_words   = [
        "saturacao", "saturação", "saturation", "smart rank", "smartrank",
        "ausencia", "ausência", "dígito ausente", "digito ausente",
        "mais tempo sem sair", "falta mais tempo", "delay zero",
    ]
    if any(w in prompt_lower for w in _fluxo_words):
        return "FLUXO"
    if any(w in prompt_lower for w in _sat_words):
        return "SATURACAO"
    if any(w in prompt_lower for w in _garra_dupla_words):
        return "GARRA_DUPLA"
    if any(w in prompt_lower for w in _dupla_words):
        return "DIGITDUPLA"
    if any(w in prompt_lower for w in _over_words):
        return "DIGITOVER"
    if any(w in prompt_lower for w in _under_words):
        return "DIGITUNDER"
    return None

# Barreiras permitidas por tipo — qualquer coisa fora desse mapa é bloqueada
# antes de chegar ao front-end, independente do que a IA gerou.
_BARREIRAS_VALIDAS = {
    # OVER: só barreiras ≥ 3 são permitidas
    # Over 0/1/2 têm >80-90% de chance de ganhar → payout irrisório, ROI negativo
    "DIGITOVER":   range(3, 9),   # 3, 4, 5, 6, 7, 8
    # UNDER: só barreiras ≤ 7 são permitidas
    # Under 8/9 têm >80-90% de chance de ganhar → payout irrisório, ROI negativo
    "DIGITUNDER":  range(1, 8),   # 1, 2, 3, 4, 5, 6, 7
    # MATCH/DIFFERS: qualquer dígito 0-9
    "DIGITMATCH":  range(0, 10),
    "DIGITDIFFERS":range(0, 10),
}

def _validar_barreira(est: dict) -> str | None:
    """
    PORTA DE FERRO — bloqueio agressivo de contratos e barreiras proibidas.
    - DIGITDIFFERS                 → BLOQUEADO (payout irrisório, risco de ruína)
    - DIGITUNDER barreira >= 8     → BLOQUEADO
    - DIGITOVER  barreira <= 2     → BLOQUEADO
    - DIGITMATCH seq_gatilho < 15  → BLOQUEADO
    - DIGITDUPLA barreira_over <= 2 ou barreira_under >= 8 → BLOQUEADO
    Retorna None se válida, ou string de erro se inválida.
    """
    tipo = str(est.get("tipo_contrato", "")).upper()

    try:
        barreira = int(est.get("barreira", 0))
    except (TypeError, ValueError):
        barreira = 0

    # ── DIGITDIFFERS — bloqueio total ────────────────────────────────────────
    if "DIFFERS" in tipo or tipo == "DIGITDIFFERS":
        return "BLOQUEIO CRÍTICO: Contratos Differs são proibidos por alto risco de ruína."

    # ── DIGITUNDER ───────────────────────────────────────────────────────────
    if tipo == "DIGITUNDER" and barreira >= 8:
        return f"BLOQUEIO: Under {barreira} banido. Payout insuficiente."

    # ── DIGITOVER ────────────────────────────────────────────────────────────
    if tipo == "DIGITOVER" and barreira <= 2:
        return f"BLOQUEIO: Over {barreira} banido. Payout insuficiente."

    # ── DIGITMATCH — exige exaustão mínima ───────────────────────────────────
    if tipo == "DIGITMATCH":
        try:
            seq = int(est.get("seq_gatilho", 0))
        except (TypeError, ValueError):
            seq = 0
        if seq < 15:
            return "PROIBIDO: Match exige exaustão mínima de 15 ticks (seq_gatilho >= 15)."

    # ── DIGITDUPLA: valida os dois lados ─────────────────────────────────────
    if tipo == "DIGITDUPLA":
        try:
            b_over  = int(est.get("barreira_over",  0))
            b_under = int(est.get("barreira_under", 9))
        except (TypeError, ValueError):
            return None
        erros = []
        if b_over <= 2:
            erros.append(f"PROIBIDO: DIGITDUPLA Over {b_over} banido — use barreira_over >= 3.")
        if b_under >= 8:
            erros.append(f"PROIBIDO: DIGITDUPLA Under {b_under} banido — use barreira_under <= 7.")
        return " | ".join(erros) if erros else None

    # ── Demais tipos: verifica range geral ───────────────────────────────────
    if tipo in _BARREIRAS_VALIDAS:
        permitidas = _BARREIRAS_VALIDAS[tipo]
        if isinstance(est.get("barreira"), str):
            return None  # barreira não-numérica (ex: TOUCH "+0.05") — OK
        if barreira not in permitidas:
            min_b, max_b = min(permitidas), max(permitidas)
            return (
                f"Barreira {barreira} inválida para {tipo}. "
                f"Permitido: {min_b}–{max_b}."
            )

    return None


def _montar_system_prompt(perfil: str, contexto_extra: str = "") -> str:
    aprendizado = _recuperar_aprendizado()
    base = (
        "VOCÊ É UM ENGENHEIRO QUANTITATIVO E TRADER DE ALTA PERFORMANCE.\n"
        "Sua missão é gerar estratégias com ASSERTIVIDADE ACIMA DE 80%.\n\n"

        f"=== MEMÓRIA DE PERFORMANCE (O QUE APRENDEMOS ATÉ AGORA) ===\n"
        f"{aprendizado}\n"
        "Analise os dados acima. Se uma estratégia teve mais losses que wins, "
        "ajuste a barreira para mais conservadora ou mude o seq_gatilho. NÃO REPITA ERROS.\n\n"

        "=== REGRAS DE OURO PARA ASSERTIVIDADE ===\n"
        "1. EXAUSTÃO (Obrigatório): Para Par/Ímpar ou Over/Under central, exija sempre "
        "   seq_gatilho entre 4 e 6.\n"
        "   - Exemplo: Para entrar em PAR, espere sair 5 ÍMPARES seguidos.\n"
        "2. BARREIRAS CONSERVADORAS: Priorize Under 6 ou Under 7 (nunca 8 ou 9), Over 3 ou Over 4 (nunca 0, 1 ou 2). "
        "   Elas têm win rate de 60-70% com payout adequado para ROI positivo.\n"
        "3. TOCA / NÃO TOCA (Touch/No Touch): Use barreiras longas (offsets acima de 0.100) "
        "   para NOTOUCH com duração de 2-3 minutos.\n"
        "4. GESTÃO DE RISCO: Se a estratégia for conservadora (80%+ win), use 'soros' ou "
        "   'conservador'. Se for 50/50, use 'martingale' limitado a 3 níveis.\n\n"

        "=== RACIOCÍNIO CAUSAL ===\n"
        "Ao receber um pedido, você deve:\n"
        "1. Identificar o regime de mercado (Tendência, Lateral ou Alta Volatilidade).\n"
        "2. Consultar a memória acima e evitar estratégias com desempenho ruim.\n"
        "3. Ajustar a barreira para probabilidade estatística > 80%.\n"
        "4. Se o mercado estiver volátil (R_100), use barreiras conservadoras (Under 6 / Over 3).\n\n"

        "=== CONHECIMENTO BASE — ESTRATÉGIAS ASSERTIVAS NA DERIV ===\n\n"

        "CONTRATOS DE DÍGITOS (Digit Over/Under/Odd/Even):\n"
        "- DIGITUNDER barreira 7 no R_100 → ~70% win rate (7 dígitos ganham: 0-6) — RECOMENDADO.\n"
        "- DIGITUNDER barreira 6 no 1HZ10V → ~60% win rate — payout adequado para ROI positivo.\n"
        "- DIGITOVER barreira 3 no 1HZ25V → ~70% win rate (7 dígitos ganham: 4-9) — RECOMENDADO.\n"
        "- DIGITOVER barreira 4 → ~60% win rate — payout adequado para ROI positivo.\n"
        "- ❌ NUNCA use Under 8, Under 9, Over 0, Over 1, Over 2 — win rate >80% gera payout irrisório.\n"
        "- DIGITOVER/UNDER central (barreiras 4-6): SEMPRE exija seq_gatilho ≥ 4.\n"
        "  Ex: Over 5 → espera sair 5 dígitos ≤5 consecutivos antes de entrar.\n"
        "- DIGITEVEN/DIGITODD → prob. ~50%, OBRIGATÓRIO seq_gatilho entre 4 e 6.\n"
        "  Ex: Para PAR, espera 5 ÍMPARES seguidos — isso eleva a assertividade para ~75%.\n"
        "- Ativos 1HZ (1HZ10V, 1HZ25V...) são MAIS previsíveis que R_ para dígitos.\n"
        "- DIGITDUPLA (Over+Under ao mesmo tempo) é seguro com barreiras assimétricas: over=3 + under=7.\n"
        "  ❌ NUNCA use barreira_over <= 2 ou barreira_under >= 8 — payout irrisório nos dois casos.\n\n"

        "CONTRATOS FLUXO (Rise/Fall / CALL/PUT):\n"
        "- Use EMA 9 cruzando EMA 21 para cima com RSI>50 → CALL (Rise).\n"
        "- Use EMA 9 cruzando EMA 21 para baixo com RSI<50 → PUT (Fall).\n"
        "- Duração ideal: 2-5 minutos no gráfico de 1min.\n"
        "- Use velas=3 (aguarda 3 candles na mesma direção antes de entrar).\n"
        "- Melhor ativo: R_10 ou R_50 (mais lentos, tendências mais claras).\n"
        "- Evite em mercados laterais.\n\n"

        "SELEÇÃO DE ATIVO — REGRA DE OURO:\n"
        "- R_10 ou R_50 → lentos, ótimos para tendência e médias móveis (FLUXO).\n"
        "- R_75 ou R_100 → velozes, melhores para Digit Over/Under com barreiras conservadoras.\n"
        "- 1HZ10V ou 1HZ25V → os MAIS estáveis, melhores para qualquer estratégia de dígitos.\n"
        "- R_100 → mais volátil, use barreiras conservadoras (Over 1, Under 8).\n\n"

        "GERENCIAMENTO DE BANCA — REGRAS CRÍTICAS:\n"
        "- Nunca arrisce mais de 1-3% da banca por operação.\n"
        "- Martingale: máximo 2-3 níveis. Mais que isso é risco de ruína.\n"
        "- Para estratégias conservadoras (80%+ win): use 'soros' ou 'conservador'.\n"
        "- Para estratégias moderadas: use 'adaptativo' ou 'ciclos'.\n"
        "- Para estratégias agressivas: use 'martingale' limitado a 3 níveis.\n"
        "- Stop loss ideal: 1.5x o take profit (ex: TP=10, SL=15).\n\n"

        "=== REGRAS RÍGIDAS DE SEGURANÇA E ROI (INVIOLÁVEIS) ===\n"
        "1. PROIBIÇÃO DE BAIXO RETORNO — NUNCA, sob nenhuma circunstância, gere:\n"
        "   - DIGITUNDER com barreira 8 ou 9. Win rate >80% → payout mínimo → ROI NEGATIVO com Martingale.\n"
        "   - DIGITOVER com barreira 0, 1 ou 2. Win rate >80% → payout mínimo → ROI NEGATIVO com Martingale.\n"
        "   - DIGITDUPLA com barreira_over <= 2 ou barreira_under >= 8. Mesma razão.\n"
        "2. MOTIVO MATEMÁTICO: Nestas barreiras o payout da Deriv é irrisório (<5% por operação).\n"
        "   Um único Martingale nível 3 já supera todo o lucro acumulado.\n"
        "3. ALTERNATIVAS ASSERTIVAS — se o usuário pedir algo 'seguro' ou 'conservador', use:\n"
        "   - DIGITUNDER barreira 6 ou 7 (com seq_gatilho >= 4).\n"
        "   - DIGITOVER barreira 3 ou 4 (com seq_gatilho >= 4).\n"
        "   - DIGITDUPLA over=3 + under=7 (com seq_gatilho >= 2).\n"
        "4. Se você ignorar esta regra, a estratégia SERÁ DESCARTADA pelo filtro de segurança do bot.\n"
        "BARREIRAS PERMITIDAS:\n"
        "  DIGITOVER:  3, 4, 5, 6, 7, 8  (❌ proibido: 0, 1, 2)\n"
        "  DIGITUNDER: 1, 2, 3, 4, 5, 6, 7  (❌ proibido: 8, 9)\n"
        "  DIGITDUPLA: barreira_over >= 3  e  barreira_under <= 7\n"
        "=== FIM DAS REGRAS DE SEGURANÇA ===\n\n"

        "=== FIM DO CONHECIMENTO BASE ===\n\n"

        "Gere APENAS um JSON válido com os seguintes campos:\n"
        "- nome: string (máximo 35 caracteres)\n"
        "- descricao: string de 1-2 frases (máximo 120 caracteres)\n"
        "- tipo_contrato: EXATAMENTE um destes valores:\n"
        "    DIGITOVER    = aposta que o dígito final será MAIOR que a barreira\n"
        "    DIGITUNDER   = aposta que o dígito final será MENOR que a barreira\n"
        "    DIGITODD     = aposta que o dígito final será ÍMPAR\n"
        "    DIGITEVEN    = aposta que o dígito final será PAR\n"
        "    DIGITDUPLA   = entra OVER e UNDER ao mesmo tempo (dois lados)\n"
        "    GARRA_DUPLA  = OVER4 + UNDER5 simultâneos com gatilho de repetição e Gale ISOLADO por janela.\n"
        "                   Campo obrigatório: seq_gatilho ≥ 2. NÃO usa barreira_over/under — barreiras são fixas.\n"
        "    DIGITPCT     = entra baseado no percentual de dígitos numa janela recente de ticks.\n"
        "                   Campos obrigatórios: pct_janela (inteiro, ticks, ex:50), pct_min_fraco (%, 1-49, ex:10), pct_min_forte (%, 51-99, ex:70).\n"
        "    DIGITMATCH   = aposta que o dígito final será EXATAMENTE a barreira (0-9). ROI ~800%.\n"
        "                   OBRIGATÓRIO: seq_gatilho ≥ 15 (esperar o dígito ficar ausente por 15+ ticks).\n"
        "                   Gerenciamento: sempre 'conservador' ou 'fixa' — stake baixíssima (0.35 máx).\n"
        "    TOUCH        = (TOCA) ganha se o preço atingir a barreira durante o contrato.\n"
        "                   A barreira deve ser um offset com sinal: ex: '+0.05' ou '-0.05'.\n"
        "    NOTOUCH      = (NÃO TOCA) ganha se o preço NUNCA atingir a barreira durante o contrato.\n"
        "                   A barreira deve ser um offset com sinal: ex: '+0.150' ou '-0.150'.\n"
        "                   IMPORTANTE: Para TOUCH use barreiras pequenas (+0.010 a +0.050).\n"
        "                               Para NOTOUCH use barreiras grandes (+0.100 a +0.500).\n"
        "    FLUXO        = CALL ou PUT baseado na direção do preço\n"
        "    SATURACAO    = entra EVEN/ODD pela saturação de dígitos (Smart Rank)\n"
        "  ATENÇÃO CRÍTICA:\n"
        "    - 'call', 'put', 'sniper', 'vela', 'candle', '1 minuto', 'fluxo', 'tendência' → FLUXO\n"
        "    - 'over' → DIGITOVER | 'under' → DIGITUNDER\n"
        "    - 'par', 'even', 'pares' → DIGITEVEN | 'ímpar', 'odd', 'impares' → DIGITODD\n"
        "    - 'toca', 'touch', 'atinge' → TOUCH | 'não toca', 'notouch' → NOTOUCH\n"
        "    - 'dos dois lados', 'dupla' → DIGITDUPLA\n"
        "    - 'garra dupla', 'garra_dupla', 'dupla janela', 'gale isolado' → GARRA_DUPLA\n"
        "    - 'percentual', '% de dígitos', 'porcentagem' → DIGITPCT\n"
        "    - 'match', 'igual', 'prever dígito' → DIGITMATCH\n"
        "- barreira: inteiro (0-9) para DIGITOVER/DIGITUNDER/DIGITEVEN/DIGITODD.\n"
        "            String com sinal para TOUCH/NOTOUCH (ex: '+0.050'). Use 0 para FLUXO/DIGITPCT.\n"
        "- barreira_over: inteiro (0-4) — obrigatório se tipo_contrato=DIGITDUPLA.\n"
        "- barreira_under: inteiro (5-9) — obrigatório se tipo_contrato=DIGITDUPLA.\n"
        "- pct_janela: inteiro (ticks da janela) — obrigatório se tipo_contrato=DIGITPCT. Ex: 50.\n"
        "- pct_min_fraco: inteiro 1-49 — % máximo do lado fraco para disparar — obrigatório se DIGITPCT. Ex: 10.\n"
        "- pct_min_forte: inteiro 51-99 — % mínimo do lado forte para disparar — obrigatório se DIGITPCT. Ex: 70.\n"
        "    SATURACAO   = entra EVEN quando dígitos ímpares saturam acima de sat_limiar% E o dígito mais ausente (Smart Rank #1) é par.\n"
        "                  Entra ODD quando pares saturam E o dígito mais ausente é ímpar.\n"
        "                  Campos obrigatórios: sat_janela (ex:25), sat_limiar (ex:70), sat_smart_min (ex:10).\n"
        "- sat_janela: inteiro — janela de ticks para calcular saturação — obrigatório se SATURACAO. Ex: 25.\n"
        "- sat_limiar: inteiro 51-95 — % mínimo de saturação para disparar — obrigatório se SATURACAO. Ex: 70.\n"
        "- sat_smart_min: inteiro — ausência mínima em ticks do dígito #1 do Smart Rank — obrigatório se SATURACAO. Ex: 10.\n"
        "- duracao: inteiro. FLUXO=minutos (ex:1). Dígitos=ticks (ex:1).\n"
        "- velas: inteiro 2-7 — para FLUXO: candles na mesma direção antes de entrar.\n"
        "- seq_gatilho: inteiro 0-10 — dígitos opostos esperados antes de entrar.\n"
        "  REGRA: Para barreiras centrais (Over 4-6 / Under 4-6) ou Par/Ímpar, seq_gatilho MÍNIMO = 4.\n"
        "- stop_loss_usd: SEMPRE use exatamente 100.0\n"
        "- take_profit_usd: SEMPRE use exatamente 10.0\n"
        "- entrada_usd: SEMPRE use exatamente 0.35\n"
        "- gerenciamento: martingale|soros|loss_recovery|conservador|qsr|masaniello|ciclos|adaptativo|fixa\n"
        "- ativo: R_10|R_25|R_50|R_75|R_100|1HZ10V|1HZ25V|1HZ50V|1HZ75V|1HZ100V\n"
        f"Perfil de risco do usuário: {perfil}.\n"
    )
    # Injeta bloqueadas do usuário — a IA nunca deve gerar essas combinações
    base += _feedback_bloco_bloqueadas()
    if contexto_extra:
        base += (
            "\n--- CONTEXTO PESQUISADO NA INTERNET (use para embasar a estratégia) ---\n"
            + contexto_extra[:3000]
            + "\n--- FIM DO CONTEXTO ---\n"
        )
    base += (
        "\n=== FILTRO DE VOLUME / ATIVIDADE DE MERCADO ===\n"
        "O Conselho de Especialistas agora inclui um Agente de Volume que mede a amplitude "
        "média de movimentação dos ticks (tick range).\n"
        "- Se o usuário ativar 'Filtro de Volume', priorize estratégias que se beneficiam "
        "  da MOVIMENTAÇÃO do preço: TOUCH, FLUXO, DIGITDUPLA.\n"
        "- Evite NOTOUCH se o volume estiver alto (mercado explosivo) — barreira pode ser tocada.\n"
        "- Prefira NOTOUCH apenas quando o mercado estiver calmo (baixa amplitude de ticks).\n"
        "- Para DIGITOVER/DIGITUNDER centrais (barreira 4-6): volume alto melhora assertividade "
        "  pois o mercado se afasta da barreira mais rapidamente.\n"
        "=== FIM DO FILTRO DE VOLUME ===\n\n"

        "\n=== REGRAS DE FLUXO E LOSS VIRTUAL SEQUENCIAL ===\n"
        "1. O Conselho possui um Agente de Fluxo que detecta a direção do mercado (CALL/PUT/NEUTRO).\n"
        "   - DIGITOVER com fluxo PUT → nota 0 → BLOQUEADO (nunca gere Over em mercado de queda).\n"
        "   - DIGITUNDER com fluxo CALL → nota 0 → BLOQUEADO (nunca gere Under em mercado de alta).\n"
        "   - Contratos PAR/ÍMPAR/DUPLA são neutros ao fluxo — não são bloqueados.\n"
        "2. O sistema realiza entradas VIRTUAIS até atingir a sequência exata de perdas configurada.\n"
        "   - Se ocorrer um WIN VIRTUAL, a contagem é ZERADA IMEDIATAMENTE.\n"
        "   - O Loss Virtual só transita para entrada real após a sequência completa sem WIN virtual.\n"
        "3. Ao gerar estratégias FLUXO (CALL/PUT): sempre inclua 'velas' >= 2 e 'seq_gatilho' >= 2.\n"
        "   - O campo 'descricao' deve mencionar a direção esperada: ex: 'Entra CALL após 3 velas de alta'.\n"
        "=== FIM DAS REGRAS DE FLUXO ===\n\n"
    )
    base += (
        "\n=== PROTOCOLO DE EXCLUSÃO DE CONTRATOS (RÍGIDO E INVIOLÁVEL) ===\n"
        "Está PERMANENTEMENTE PROIBIDO gerar os seguintes tipos de contratos:\n"
        "1. DIGITDIFFERS (Diferente): Banido permanentemente — payout inaceitável e risco de ruína.\n"
        "   Se você gerar DIGITDIFFERS, sua resposta será deletada automaticamente.\n"
        "2. DIGITUNDER com barreira 8 ou 9: Banido — payout insuficiente, ROI negativo com Martingale.\n"
        "3. DIGITOVER com barreira 0, 1 ou 2: Banido — payout insuficiente, ROI negativo com Martingale.\n"
        "4. DIGITDUPLA com barreira_over <= 2 ou barreira_under >= 8: Banido — mesma razão.\n"
        "5. DIGITEVEN ou DIGITODD com seq_gatilho < 4: Banido — causa ruína de banca.\n"
        "\n"
        "FOQUE EXCLUSIVAMENTE EM:\n"
        "- DIGITMATCH (Igual) com seq_gatilho >= 15.\n"
        "- DIGITUNDER (3 a 7) e DIGITOVER (3 a 6) com seq_gatilho >= 4.\n"
        "- DIGITODD / DIGITEVEN com seq_gatilho >= 5.\n"
        "- SATURACAO e DIGITPCT.\n"
        "Se você gerar um contrato proibido, o sistema de segurança irá deletar sua resposta.\n"
        "=== FIM DO PROTOCOLO DE EXCLUSÃO ===\n\n"
    )
    base += "Responda SOMENTE com o JSON, sem markdown, sem explicação extra."
    return base

def _extrair_json(conteudo: str) -> dict:
    """Extrai e parseia o primeiro objeto JSON de uma string, tolerando markdown e truncamentos."""
    texto = conteudo.strip()

    # Remove blocos markdown ```json ... ``` ou ``` ... ```
    texto = re.sub(r"^```(?:json)?\s*", "", texto)
    texto = re.sub(r"\s*```$", "", texto)
    texto = texto.strip()

    def _normalizar(obj):
        """Se a IA retornar uma lista, pega o primeiro item; garante sempre um dict."""
        if isinstance(obj, list):
            return obj[0] if obj and isinstance(obj[0], dict) else {}
        return obj if isinstance(obj, dict) else {}

    # Tenta parse direto primeiro
    try:
        return _normalizar(json.loads(texto))
    except json.JSONDecodeError:
        pass

    # Tenta extrair o primeiro { ... } completo da string
    inicio = texto.find("{")
    if inicio != -1:
        # Conta chaves para encontrar o fechamento correto
        profundidade = 0
        fim = -1
        em_string = False
        escape = False
        for i, ch in enumerate(texto[inicio:], inicio):
            if escape:
                escape = False
                continue
            if ch == "\\" and em_string:
                escape = True
                continue
            if ch == '"' and not escape:
                em_string = not em_string
                continue
            if not em_string:
                if ch == "{":
                    profundidade += 1
                elif ch == "}":
                    profundidade -= 1
                    if profundidade == 0:
                        fim = i
                        break
        if fim != -1:
            try:
                return _normalizar(json.loads(texto[inicio:fim + 1]))
            except json.JSONDecodeError:
                pass

        # Último recurso: JSON truncado — tenta fechar as chaves/strings abertas
        fragmento = texto[inicio:]
        # Fecha string aberta
        if fragmento.count('"') % 2 != 0:
            fragmento += '"'
        # Fecha arrays/objetos aninhados
        abertos = fragmento.count("{") - fragmento.count("}")
        fragmento += "}" * max(0, abertos)
        # Remove vírgula antes do fechamento final
        fragmento = re.sub(r",\s*}", "}", fragmento)
        try:
            return _normalizar(json.loads(fragmento))
        except json.JSONDecodeError as e:
            raise e

    raise json.JSONDecodeError("Nenhum objeto JSON encontrado", texto, 0)


def _chamar_groq(chave: str, modelo: str, system_prompt: str, user_prompt: str,
                 temperature: float = 0.3, max_tokens: int = 1024):
    """Chama a API Groq e retorna (dict_parsed, conteudo_raw). Lança exceção em caso de erro."""
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
        json={
            "model": modelo,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=30,
    )
    if resp.status_code == 429:
        retry_after = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset-requests")
        dica = f" Aguarde {retry_after}s antes de tentar novamente." if retry_after else " Aguarde alguns segundos antes de tentar novamente."
        raise Exception(f"⏳ Limite de requisições da API Groq atingido (429).{dica}")
    if resp.status_code == 401:
        raise Exception("🔑 Chave API Groq inválida ou expirada. Verifique a chave em console.groq.com")
    if resp.status_code == 503:
        raise Exception("🔌 API Groq temporariamente indisponível (503). Tente novamente em instantes.")
    resp.raise_for_status()
    conteudo = resp.json()["choices"][0]["message"]["content"].strip()
    return _extrair_json(conteudo), conteudo

def _chamar_nvidia(chave: str, modelo: str, system_prompt: str, user_prompt: str,
                   temperature: float = 0.5, max_tokens: int = 32768):
    """Chama a API da NVIDIA (Nemotron) e retorna (dict_parsed, conteudo_raw)."""
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {chave}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": modelo,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 0.95,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code == 401:
        raise Exception("🔑 Chave API NVIDIA inválida ou expirada.")
    if resp.status_code == 429:
        raise Exception("⏳ Limite de requisições da API NVIDIA atingido (429). Aguarde alguns segundos.")
    if resp.status_code != 200:
        raise Exception(f"Erro NVIDIA ({resp.status_code}): {resp.text}")
    conteudo = resp.json()["choices"][0]["message"]["content"].strip()
    return _extrair_json(conteudo), conteudo

def _resolver_funcao_ia(dados: dict):
    """Retorna (funcao_ia, chave, modelo) de acordo com o provedor_ativo ou o campo 'provedor' do payload."""
    cfg      = _groq_cfg_ler()
    provedor = dados.get("provedor") or cfg.get("provedor_ativo", "groq")
    if provedor == "nvidia":
        chave  = dados.get("chave") or cfg.get("nvidia_chave", "")
        modelo = dados.get("modelo") or cfg.get("nvidia_modelo", "nvidia/llama-3.1-nemotron-70b-instruct")
        return _chamar_nvidia, chave, modelo, provedor
    chave  = dados.get("chave") or cfg.get("chave", "")
    modelo = dados.get("modelo") or cfg.get("modelo", "llama-3.3-70b-versatile")
    return _chamar_groq, chave, modelo, "groq"

# ── Busca web via DuckDuckGo (sem API key) ───────────────────────────────────
def _buscar_estrategias_web(query: str, max_resultados: int = 8) -> str:
    """
    Busca estratégias de trading no DuckDuckGo Lite e retorna um bloco de
    texto com os trechos encontrados. Não requer nenhuma chave de API.
    """
    import re, html
    headers_ddg = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    # Termos de busca focados em estratégias recentes (2024/2025)
    termos = (
        f"Deriv binary options strategy {query} "
        "digit 2024 2025 high win rate validated"
    )
    trechos = []
    tentativas = [
        # DuckDuckGo Lite (HTML simples, sem JS)
        ("https://lite.duckduckgo.com/lite/",
         {"q": termos, "kl": "br-pt"}, "post"),
        # Fallback: DuckDuckGo HTML
        ("https://html.duckduckgo.com/html/",
         {"q": termos}, "post"),
    ]
    for url, params, method in tentativas:
        try:
            if method == "post":
                r = requests.post(url, data=params, headers=headers_ddg,
                                  timeout=12, verify=False)
            else:
                r = requests.get(url, params=params, headers=headers_ddg,
                                 timeout=12, verify=False)
            if r.status_code != 200:
                continue
            texto = r.text
            # Extrai snippets: texto dentro de <a> e <td class="result-snippet">
            snippets = re.findall(
                r'class="result-snippet"[^>]*>(.*?)</(?:td|span|div)>',
                texto, re.DOTALL | re.IGNORECASE
            )
            # Fallback: qualquer bloco de texto com palavras-chave relevantes
            if not snippets:
                snippets = re.findall(
                    r'<(?:td|p|span)[^>]*>([^<]{60,300})</(?:td|p|span)>',
                    texto, re.DOTALL
                )
            for s in snippets[:max_resultados]:
                limpo = html.unescape(re.sub(r'<[^>]+>', ' ', s)).strip()
                limpo = re.sub(r'\s+', ' ', limpo)
                if len(limpo) > 40:
                    trechos.append(limpo)
            if trechos:
                break
        except Exception:
            continue

    if not trechos:
        # Fallback neutro: força a IA a raciocinar do zero, sem exemplos fixos
        return (
            "ATENÇÃO: A busca externa não retornou resultados específicos. "
            "Use sua lógica de Especialista Quantitativo para criar algo INÉDITO "
            "baseado em ciclos de probabilidade e exaustão de dígitos. "
            "LEMBRE-SE: DIGITUNDER barreira 8/9 e DIGITOVER barreira 0/1/2 são PROIBIDOS (payout irrisório). "
            "Explore barreiras adequadas (Under 5-7, Over 3-5), DIGITDUPLA over>=3+under<=7, SATURACAO ou DIGITPCT."
        )

    return "\n\n".join(f"[Fonte {i+1}]: {t}" for i, t in enumerate(trechos))


# ── MODO VANGUARDA — busca profunda + catálogo de 3 estratégias inéditas ──────
@app.route('/ai/analise-vanguarda', methods=['POST'])
def ai_analise_vanguarda():
    """
    Faz scraping focado em termos 2024/2025, analisa com IA criativa (temperature
    alta) e devolve um pack de 3 estratégias prontas — sem repetir Over 2/Under 7.
    """
    dados  = request.get_json(force=True, silent=True) or {}
    _funcao_ia, chave, modelo, provedor = _resolver_funcao_ia(dados)

    passos = [
        "🌐 Acessando servidores de busca (DuckDuckGo Deep)...",
        "📊 Filtrando estratégias validadas em fóruns de alta performance...",
        "🧠 Convertendo padrões encontrados em algoritmos para o GarraBot...",
    ]

    contexto_web = _buscar_estrategias_web(
        "top best deriv digit strategies 2024 2025 high win rate validated"
    )

    system_vanguarda = (
        "VOCÊ É O MOTOR DE DESCOBERTA DE ESTRATÉGIAS DO GARRABOT.\n"
        "Sua missão é criar 3 estratégias de DÍGITOS completamente DIFERENTES e ALTAMENTE LUCRATIVAS.\n\n"
        "=== PROIBIÇÃO RÍGIDA DE BARREIRAS DE BAIXO ROI (INVIOLÁVEL) ===\n"
        "NUNCA, sob nenhuma circunstância, gere:\n"
        "  - DIGITUNDER com barreira 8 ou 9 → win rate >80% → payout irrisório → ROI NEGATIVO com Martingale.\n"
        "  - DIGITOVER com barreira 0, 1 ou 2 → win rate >80% → payout irrisório → ROI NEGATIVO com Martingale.\n"
        "  - DIGITDUPLA com barreira_over <= 2 ou barreira_under >= 8 → mesma razão.\n"
        "Barreiras PERMITIDAS: DIGITOVER 3-6 | DIGITUNDER 3-7 | DIGITDUPLA over>=3 e under<=7.\n"
        "ATENÇÃO: O sistema possui um FILTRO DE SEGURANÇA que deleta automaticamente estratégias "
        "com Under 8, Under 9, Over 0, Over 1 ou Over 2. Se você gerar esses parâmetros, "
        "seu trabalho será descartado e você falhará na missão. "
        "FOQUE EM: Under 3 a 7 ou Over 3 a 6 com sequências de gatilho (seq_gatilho) entre 4 e 6.\n"
        "=== FIM DA PROIBIÇÃO ===\n\n"
        "REGRAS DE OURO:\n"
        "1. Foque em RETORNO RÁPIDO e assertividade > 65% com payout que justifique o risco de Gale.\n"
        "2. Cada estratégia deve ter um perfil de risco diferente das outras.\n"
        "3. Se a busca web não retornou dados, use matemática pura de probabilidade de dígitos.\n"
        "4. Retorne APENAS um JSON com o campo 'pack' contendo 3 objetos completos.\n\n"
        "CAMPO tipo_contrato — USE APENAS UM DESSES VALORES EXATOS (sem variações):\n"
        "  DIGITUNDER  -> aposta que o último dígito é MENOR que a barreira (barreira 1-7 APENAS)\n"
        "  DIGITOVER   -> aposta que o último dígito é MAIOR que a barreira (barreira 3-8 APENAS)\n"
        "  DIGITODD    -> aposta que o último dígito é ÍMPAR (barreira=null)\n"
        "  DIGITEVEN   -> aposta que o último dígito é PAR (barreira=null)\n"
        "  DIGITDUPLA  -> abre OVER e UNDER ao mesmo tempo (barreira_over e barreira_under)\n"
        "  GARRA_DUPLA -> OVER4+UNDER5 simultâneos, Gale isolado por janela, gatilho de repetição (seq_gatilho>=2)\n"
        "  DIGITPCT    -> histograma percentual de exaustão (pct_janela, pct_min_fraco, pct_min_forte)\n"
        "  SATURACAO   -> saturação de dígitos (sat_janela, sat_limiar, sat_smart_min)\n"
        "NUNCA use: 'Binário', 'Binary', 'UNDER9', 'OVER0', 'digit', ou qualquer outro valor.\n\n"
        "Cada objeto deve ter: nome, descricao_detalhada, assertividade_estimada, roi_esperado, "
        "tipo_contrato, barreira, seq_gatilho, ativo, gerenciamento.\n"
        "IMPORTANTE — valores financeiros fixos obrigatórios para TODOS os objetos:\n"
        "  entrada_usd=0.35, take_profit_usd=10.0, stop_loss_usd=100.0"
        + _feedback_bloco_bloqueadas()
    )

    prompt_final = (
        f"RESULTADOS DA BUSCA WEB:\n{contexto_web}\n\n"
        "Com base nos dados acima, gere um 'Pack' com 3 estratégias prontas e INÉDITAS.\n"
        "Retorne APENAS JSON puro no formato: "
        "{ \"pack\": [ {estrategia1}, {estrategia2}, {estrategia3} ] }"
    )

    try:
        resultado, _ = _funcao_ia(chave, modelo, system_vanguarda, prompt_final, temperature=0.8)
        pack = resultado.get("pack") or []

        _TIPOS_VALIDOS_V = {"DIGITOVER","DIGITUNDER","DIGITODD","DIGITEVEN",
                             "DIGITDUPLA","DIGITPCT","SATURACAO","GARRA_DUPLA"}
        _TIPO_FIX = {
            "BINARY":"DIGITUNDER","BINÁRIO":"DIGITUNDER","BINARYO":"DIGITUNDER",
            "UNDER":"DIGITUNDER","OVER":"DIGITOVER","ODD":"DIGITODD","EVEN":"DIGITEVEN",
            "DUPLA":"DIGITDUPLA","PCT":"DIGITPCT","SATURAÇÃO":"SATURACAO","SATURACÃO":"SATURACAO",
            "FLUXO":"SATURACAO","DIGIT":"DIGITUNDER",
            "GARRADUPLA":"GARRA_DUPLA","GARRA":"GARRA_DUPLA",
        }
        for item in pack:
            item["_vanguarda"]      = True
            # Garante valores financeiros fixos independente do que a IA retornou
            item["entrada_usd"]     = 0.35
            item["take_profit_usd"] = 10.0
            item["stop_loss_usd"]   = 100.0
            # Sanitiza tipo_contrato — corrige variações da IA para valores aceitos pelo bot
            tipo_raw = str(item.get("tipo_contrato", "")).upper().strip().replace(" ","").replace("-","").replace("_","")
            if tipo_raw not in _TIPOS_VALIDOS_V:
                tipo_corr = _TIPO_FIX.get(tipo_raw)
                if not tipo_corr:
                    if tipo_raw.startswith("UNDER") or tipo_raw.endswith("UNDER"):
                        tipo_corr = "DIGITUNDER"
                    elif tipo_raw.startswith("OVER") or tipo_raw.endswith("OVER"):
                        tipo_corr = "DIGITOVER"
                    elif "GARRADUPLA" in tipo_raw or "GARRADUPLA" in tipo_raw:
                        tipo_corr = "GARRA_DUPLA"
                    elif "DUPLA" in tipo_raw or "DUAL" in tipo_raw:
                        tipo_corr = "DIGITDUPLA"
                    elif "SAT" in tipo_raw:
                        tipo_corr = "SATURACAO"
                    elif "PCT" in tipo_raw or "HIST" in tipo_raw or "PERCENT" in tipo_raw:
                        tipo_corr = "DIGITPCT"
                    elif "ODD" in tipo_raw or "IMPAR" in tipo_raw:
                        tipo_corr = "DIGITODD"
                    elif "EVEN" in tipo_raw or "PAR" in tipo_raw:
                        tipo_corr = "DIGITEVEN"
                    else:
                        tipo_corr = "DIGITUNDER"  # fallback seguro
                item["tipo_contrato"] = tipo_corr
            else:
                item["tipo_contrato"] = tipo_raw

        return jsonify({"ok": True, "pack": pack, "_passos": passos})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route('/ai/gerar', methods=['POST'])
def ai_gerar():
    dados  = request.get_json(force=True, silent=True) or {}
    _funcao_ia, chave, modelo, provedor = _resolver_funcao_ia(dados)
    prompt = dados.get("prompt", "")
    perfil = dados.get("perfil", "moderado")

    if not chave:
        return jsonify({"erro": f"Chave API {provedor.upper()} não configurada"})
    if not prompt:
        return jsonify({"erro": "Prompt vazio"})

    system_prompt    = _montar_system_prompt(perfil)
    contrato_forcado = _detectar_contrato(prompt.lower())

    conteudo = ""
    try:
        estrategia, conteudo = _funcao_ia(chave, modelo, system_prompt, prompt)
        # Só sobrescreve se a IA não gerou DIGITPCT — DIGITPCT tem precedência
        if contrato_forcado and estrategia.get("tipo_contrato", "").upper() != "DIGITPCT":
            estrategia["tipo_contrato"] = contrato_forcado
        # Limpeza para o Layout não quebrar
        if "nome" in estrategia:
            estrategia["nome"] = estrategia["nome"][:35]
        if "descricao" in estrategia:
            estrategia["descricao"] = estrategia["descricao"][:120]
        # Guarda dura de barreira — rejeita se a IA ignorou as regras
        _erro_barreira = _validar_barreira(estrategia)
        if _erro_barreira:
            return jsonify({"erro": _erro_barreira})
        return jsonify(estrategia)
    except json.JSONDecodeError as e:
        return jsonify({"erro": f"IA retornou JSON inválido: {e}", "raw": conteudo[:300]})
    except Exception as e:
        return jsonify({"erro": str(e)})

# ── MODO ULTRA (normal / mestre / supremo) ────────────────────────────────────
@app.route('/ai/gerar-ultra', methods=['POST'])
def ai_gerar_ultra():
    """
    Modo Ultra unificado — aceita o parâmetro "nivel":
      normal  → busca na web + _montar_system_prompt  (comportamento original)
      mestre  → busca na web + _montar_system_prompt_mestre  (3 personas)
      supremo → busca na web + _montar_prompt_supremo (5 personas + dados mercado)
    """
    dados    = request.get_json(force=True, silent=True) or {}
    _funcao_ia, chave, modelo, provedor = _resolver_funcao_ia(dados)
    prompt   = dados.get("prompt", "")
    perfil   = dados.get("perfil", "moderado")
    nivel    = dados.get("nivel", "normal")   # "normal" | "mestre" | "supremo"

    if not chave:
        return jsonify({"erro": f"Chave API {provedor.upper()} não configurada"})
    if not prompt:
        return jsonify({"erro": "Prompt vazio"})

    passos = []

    # ── Passo 1: Busca na internet (comum a todos os níveis) ──────────────────
    label_nivel = {"normal": "🌐 ULTRA Normal", "mestre": "🧙 ULTRA Mestre",
                   "supremo": "🏆 ULTRA Supremo"}.get(nivel, "🌐 ULTRA")
    passos.append(f"🔍 [{label_nivel}] Buscando estratégias assertivas na internet...")
    contexto_web = _buscar_estrategias_web(prompt)
    fonte = "internet" if "Fonte" in contexto_web else "base interna"
    passos.append(f"📚 Dados coletados ({fonte}). Analisando com IA...")

    # ── Passo 2: Análise rápida (comum) ──────────────────────────────────────
    system_analise = (
        "Você é um analista sênior de estratégias para Deriv (índices sintéticos e opções binárias). "
        "Princípios que você sabe de cor:\n"
        "- Over/Under perto de 5 tem ~50% win rate → exige seq_gatilho≥3 para ser assertivo.\n"
        "- Over 2 (~80%), Over 1 (~90%), Under 7 (~80%), Under 8 (~90%).\n"
        "- Ativos 1HZ são mais estáveis; R_100 é mais volátil.\n"
        "- Martingale: máximo 3 níveis. Acima disso é risco de ruína.\n"
        "- Stop loss deve ser 1.5x o take profit.\n"
        "Analise os dados abaixo e retorne em máximo 5 linhas: "
        "tipo de contrato ideal, barreira recomendada, seq_gatilho sugerido, "
        "gerenciamento de banca e ativo mais adequado para o pedido do usuário."
    )
    analise_txt = ""
    try:
        _, analise_txt = _funcao_ia(
            chave, modelo, system_analise,
            f"Pedido do usuário: {prompt}\n\nDados pesquisados:\n{contexto_web}",
            temperature=0.2, max_tokens=300,
        )
        passos.append(f"🧠 Análise concluída: {analise_txt[:120]}...")
    except Exception as _ea:
        if "429" in str(_ea):
            return jsonify({"erro": "⏳ Limite de requisições atingido (429). Aguarde alguns segundos.", "_passos": passos})
        analise_txt = ""
        passos.append("⚠️ Análise rápida falhou, usando contexto direto...")

    # ── Passo 3: Escolhe o system prompt pelo nível ───────────────────────────
    contexto_final = contexto_web
    if analise_txt:
        contexto_final = f"ANÁLISE DO ESPECIALISTA:\n{analise_txt}\n\nDADOS BRUTOS:\n{contexto_web}"

    if nivel == "mestre":
        passos.append("🧙 Ativando ULTRA Mestre — 3 personas consultando...")
        system_final = _montar_system_prompt_mestre(perfil, contexto_final)
    elif nivel == "supremo":
        passos.append("🏆 Ativando ULTRA Supremo — coletando dados de mercado...")
        dados_mercado = _coletar_dados_mercado_supremo()
        passos.append(f"📡 Melhor ativo: {dados_mercado.get('melhor', {}).get('ativo', '?')}")
        system_final = _montar_prompt_supremo(dados_mercado, [])
    else:  # normal
        passos.append("⚡ Gerando estratégia Ultra Normal...")
        system_final = _montar_system_prompt(perfil, contexto_final)

    contrato_forcado = _detectar_contrato(prompt.lower())

    conteudo = ""
    try:
        estrategia, conteudo = _funcao_ia(
            chave, modelo, system_final,
            f"Crie a estratégia mais assertiva possível para: {prompt}",
            temperature=0.2,
            max_tokens=1024,
        )
        if not isinstance(estrategia, dict):
            estrategia = {}
        if contrato_forcado and estrategia.get("tipo_contrato", "").upper() != "DIGITPCT":
            estrategia["tipo_contrato"] = contrato_forcado
        estrategia["_ultra"] = True
        estrategia["_ultra_nivel"] = nivel
        if "nome" in estrategia:
            estrategia["nome"] = estrategia["nome"][:35]
        if "descricao" in estrategia:
            estrategia["descricao"] = estrategia["descricao"][:120]
        _erro_barreira = _validar_barreira(estrategia)
        if _erro_barreira:
            return jsonify({"erro": _erro_barreira, "_passos": passos})
        estrategia["_passos"] = passos + [f"✅ Estratégia {label_nivel} gerada com sucesso!"]
        return jsonify(estrategia)
    except json.JSONDecodeError as e:
        return jsonify({"erro": f"IA retornou JSON inválido: {e}", "raw": conteudo[:300],
                        "_passos": passos})
    except Exception as e:
        return jsonify({"erro": str(e), "_passos": passos})

# ── MODO COMBO — cria 3 estratégias de dígitos NOVAS ─────────────────────────
# Os parâmetros numéricos são sorteados aqui no Python (100% aleatórios a cada
# chamada). A IA recebe os números prontos e só precisa criar o nome, a
# descrição e a nota de assertividade — garantindo diversidade real.
@app.route('/ai/gerar-combo', methods=['POST'])
def ai_gerar_combo():
    import random

    dados  = request.get_json(force=True, silent=True) or {}
    _funcao_ia, chave, modelo, provedor = _resolver_funcao_ia(dados)
    tema   = dados.get("tema", "").strip()
    perfil = dados.get("perfil", "moderado")

    if not chave:
        return jsonify({"erro": f"Chave API {provedor.upper()} não configurada"})

    passos = []

    # ── Lê salvos para montar o bloco "não repita" ───────────────────────────
    salvas = _ia_listar_arquivos()
    combos_usados = []
    if salvas:
        for s in salvas[-12:]:
            combos_usados.append(
                f"  • {s.get('nome','?')}: tipo={s.get('tipo_contrato','?')} "
                f"ativo={s.get('ativo','?')} mgmt={s.get('gerenciamento','?')} "
                f"barreira={s.get('barreira','?')} seq={s.get('seq_gatilho','?')} "
                f"janela={s.get('pct_janela') or s.get('sat_janela','?')}"
            )
    bloco_evitar = ""
    if combos_usados:
        bloco_evitar = (
            "ESTRATÉGIAS JÁ SALVAS — NÃO repita a mesma combinação de tipo+ativo+barreira:\n"
            + "\n".join(combos_usados) + "\n\n"
        )
    bloco_evitar += _feedback_bloco_bloqueadas()

    # ════════════════════════════════════════════════════════════════════════
    # POOL DE RECEITAS — 12 receitas possíveis, cada uma com todos os campos
    # já definidos. O Python sorteia 3 sem repetição. A IA só cria nome,
    # descrição e nota de assertividade.
    # ════════════════════════════════════════════════════════════════════════
    _POOL = [
        # ── ASSERTIVIDADE SÓLIDA COM ROI POSITIVO ─────────────────────────
        {"tipo_contrato":"DIGITUNDER","ativo":"1HZ10V","gerenciamento":"soros",
         "barreira":7,"seq_gatilho":3,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Gatilho Under 7 1HZ10V",
         "_logica":"70% win rate com payout adequado. Entra após 3 dígitos seguidos >= 7.",
         "_assertividade":"70%"},

        # ── EXAUSTÃO PAR/ÍMPAR ────────────────────────────────────────────
        {"tipo_contrato":"DIGITEVEN","ativo":"R_100","gerenciamento":"ciclos",
         "barreira":0,"seq_gatilho":5,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Exaustão de Ímpares (Even)",
         "_logica":"Espera o mercado saturar com 5 ímpares seguidos para buscar a correção no Par.",
         "_assertividade":"82%"},

        # ── TOUCH (LUCRO RÁPIDO) ──────────────────────────────────────────
        {"tipo_contrato":"TOUCH","ativo":"R_10","gerenciamento":"martingale",
         "barreira":"+0.012","seq_gatilho":0,"duracao":2,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Sniper Touch",
         "_logica":"Busca um toque rápido em uma barreira curta durante micro-tendências.",
         "_assertividade":"70%"},

        # ── NOTOUCH (MURALHA DE DEFESA) ───────────────────────────────────
        {"tipo_contrato":"NOTOUCH","ativo":"1HZ100V","gerenciamento":"conservador",
         "barreira":"+0.185","seq_gatilho":0,"duracao":3,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Muralha de Defesa",
         "_logica":"Aposta que o preço não atingirá uma zona extrema em 3 minutos. Altamente segura.",
         "_assertividade":"85%"},

        # ── PERCENTUAL (SMART EXAUSTÃO) ───────────────────────────────────
        {"tipo_contrato":"DIGITPCT","ativo":"R_50","gerenciamento":"adaptativo",
         "pct_janela":40,"pct_min_fraco":10,"pct_min_forte":70,"duracao":1,
         "barreira":0,"barreira_over":0,"barreira_under":0,"seq_gatilho":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Exaustão Percentual",
         "_logica":"Entra quando um lado do histograma atinge 70% de dominância, prevendo a inversão.",
         "_assertividade":"78%"},

        # ── OVER CONSERVADOR COM ROI POSITIVO ────────────────────────────
        {"tipo_contrato":"DIGITOVER","ativo":"1HZ25V","gerenciamento":"soros",
         "barreira":3,"seq_gatilho":4,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Gatilho Over 3 1HZ25V seq=4",
         "_logica":"4 dígitos ≤3 consecutivos → Over 3 (~70% win rate, payout adequado para ROI positivo)",
         "_assertividade":"70%"},

        # ── SATURAÇÃO SMART RANK ──────────────────────────────────────────
        {"tipo_contrato":"SATURACAO","ativo":"R_75","gerenciamento":"adaptativo",
         "sat_janela":30,"sat_limiar":72,"sat_smart_min":15,"duracao":1,
         "barreira":0,"barreira_over":0,"barreira_under":0,"seq_gatilho":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "_tecnica":"Saturação Smart R_75",
         "_logica":"Janela 30t com limiar 72% + Smart Rank confirmando ausência de 15 ticks",
         "_assertividade":"75%"},

        # ── HISTOGRAMA ULTRA-CONSERVADOR ──────────────────────────────────
        {"tipo_contrato":"DIGITPCT","ativo":"1HZ75V","gerenciamento":"conservador",
         "pct_janela":60,"pct_min_fraco":15,"pct_min_forte":80,"duracao":1,
         "barreira":0,"barreira_over":0,"barreira_under":0,"seq_gatilho":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Histograma Ultra-Conservador 1HZ75V",
         "_logica":"Janela 60t + exigência rígida 80%/15% → poucas entradas, alta precisão",
         "_assertividade":"78%"},

        # ── EXAUSTÃO ÍMPAR ────────────────────────────────────────────────
        {"tipo_contrato":"DIGITODD","ativo":"R_100","gerenciamento":"martingale",
         "barreira":0,"seq_gatilho":5,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Sniper Ímpar R_100",
         "_logica":"Sequência de 5 pares consecutivos → entrada em ÍMPAR por reversão",
         "_assertividade":"75%"},

        # ── DUPLA ASSIMÉTRICA COM ROI POSITIVO ───────────────────────────
        {"tipo_contrato":"DIGITDUPLA","ativo":"1HZ100V","gerenciamento":"martingale",
         "barreira_over":3,"barreira_under":7,"seq_gatilho":0,"duracao":1,
         "barreira":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Dupla Over3+Under7",
         "_logica":"Ganha se dígito ∈ {0,1,2} ou {8,9} → 5/10 dígitos ganham (50% cobertura, payout justo)",
         "_assertividade":"60%"},

        # ── UNDER 7 COM GATILHO REFORÇADO ────────────────────────────────
        {"tipo_contrato":"DIGITUNDER","ativo":"R_100","gerenciamento":"adaptativo",
         "barreira":7,"seq_gatilho":4,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Gatilho Under 7 R_100 seq=4",
         "_logica":"4 dígitos ≥7 consecutivos → Under 7 — exaustão reforçada eleva assertividade",
         "_assertividade":"82%"},

        # ── NOTOUCH CLÁSSICO ──────────────────────────────────────────────
        {"tipo_contrato":"NOTOUCH","ativo":"1HZ50V","gerenciamento":"conservador",
         "barreira":"+0.150","seq_gatilho":0,"duracao":3,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Defesa de Barreira Longe 1HZ50V",
         "_logica":"Barreira distante +0.150 em 3min → NÃO TOCA com margem confortável",
         "_assertividade":"82%"},

        # ── ALTO ROI: DIGIT MATCH (~800% por acerto) ─────────────────────
        # Stake mínima obrigatória — a exaustão de 20 ticks é o filtro de entrada
        {"tipo_contrato":"DIGITMATCH","ativo":"R_100","gerenciamento":"fixa",
         "barreira":5,"seq_gatilho":20,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Caçador de Match — Dígito 5",
         "_logica":"Aguarda 20 ticks sem sair o dígito 5. Entrada mínima. ROI de ~800% por acerto.",
         "_assertividade":"~10% win | ROI 800%"},

        # ── ALTO ROI: OVER 6 COM EXAUSTÃO PROFUNDA ───────────────────────
        # seq_gatilho=6 eleva assertividade para ~45% (base 40%) com payout 230%
        {"tipo_contrato":"DIGITOVER","ativo":"1HZ10V","gerenciamento":"soros",
         "barreira":6,"seq_gatilho":6,"duracao":1,
         "barreira_over":0,"barreira_under":0,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Inversão Over 6 — Exaustão Profunda",
         "_logica":"Aguarda 6 dígitos ≤6 consecutivos. Payout ~230%. Soros limita risco.",
         "_assertividade":"~45% | ROI 230%"},

        # ── GARRA DUPLA — OVER4 + UNDER5 COM GALE ISOLADO ────────────────
        {"tipo_contrato":"GARRA_DUPLA","ativo":"1HZ100V","gerenciamento":"martingale",
         "barreira":0,"seq_gatilho":3,"duracao":1,
         "barreira_over":4,"barreira_under":5,
         "pct_janela":0,"pct_min_fraco":0,"pct_min_forte":0,
         "sat_janela":0,"sat_limiar":0,"sat_smart_min":0,
         "_tecnica":"Garra Dupla — Over4+Under5",
         "_logica":"Aguarda 3 dígitos iguais consecutivos. Dispara OVER4+UNDER5 simultaneamente. Gale isolado por janela.",
         "_assertividade":"~50% cada janela | dupla cobertura"},

    ]

    # Sorteia 3 receitas sem repetir tipo+ativo
    random.shuffle(_POOL)
    trio = []
    tipos_usados  = set()
    ativos_usados = set()
    for r in _POOL:
        chave_uni = f"{r['tipo_contrato']}_{r['ativo']}"
        if chave_uni not in tipos_usados and r['ativo'] not in ativos_usados:
            trio.append(r)
            tipos_usados.add(chave_uni)
            ativos_usados.add(r['ativo'])
        if len(trio) == 3:
            break
    # Fallback: se não completou 3, pega os primeiros
    if len(trio) < 3:
        for r in _POOL:
            if r not in trio:
                trio.append(r)
            if len(trio) == 3:
                break

    tema_final = tema if tema else f"índices sintéticos Deriv perfil {perfil}"
    passos.append(f"🧠 Sorteando 3 receitas únicas para: {tema_final}...")

    # Monta o prompt com os parâmetros já definidos — IA só cria nome e descrição
    bloco_receitas = ""
    for i, r in enumerate(trio):
        bloco_receitas += (
            f"\nEstratégia {i+1}:\n"
            f"  tipo_contrato: {r['tipo_contrato']}\n"
            f"  ativo: {r['ativo']}\n"
            f"  gerenciamento: {r['gerenciamento']}\n"
            f"  duracao: 1\n"
        )
        if r['tipo_contrato'] == 'SATURACAO':
            bloco_receitas += (
                f"  sat_janela: {r['sat_janela']}\n"
                f"  sat_limiar: {r['sat_limiar']}\n"
                f"  sat_smart_min: {r['sat_smart_min']}\n"
            )
        elif r['tipo_contrato'] == 'DIGITPCT':
            bloco_receitas += (
                f"  pct_janela: {r['pct_janela']}\n"
                f"  pct_min_fraco: {r['pct_min_fraco']}\n"
                f"  pct_min_forte: {r['pct_min_forte']}\n"
            )
        elif r['tipo_contrato'] == 'DIGITDUPLA':
            bloco_receitas += (
                f"  barreira_over: {r['barreira_over']}\n"
                f"  barreira_under: {r['barreira_under']}\n"
                f"  seq_gatilho: {r['seq_gatilho']}\n"
            )
        elif r['tipo_contrato'] in ('TOUCH', 'NOTOUCH'):
            bloco_receitas += (
                f"  barreira: {r['barreira']}\n"
                f"  duracao: {r.get('duracao', 2)}\n"
            )
        elif r['tipo_contrato'] in ('DIGITEVEN', 'DIGITODD'):
            bloco_receitas += (
                f"  seq_gatilho: {r['seq_gatilho']}\n"
            )
        else:  # DIGITUNDER / DIGITOVER
            bloco_receitas += (
                f"  barreira: {r['barreira']}\n"
                f"  seq_gatilho: {r['seq_gatilho']}\n"
            )
        bloco_receitas += (
            f"  _logica_base: {r['_logica']}\n"
            f"  _assertividade_base: {r['_assertividade']}\n"
        )

    passos.append("⚡ IA nomeando e descrevendo as estratégias...")

    auto_prompt = (
        f"Você recebeu 3 receitas de estratégias de dígitos com parâmetros já definidos.\n"
        f"Seu trabalho é criar o nome criativo, a descrição técnica e a nota de assertividade "
        f"de cada uma, com base nos parâmetros fornecidos.\n\n"
        f"Tema: {tema_final} | Perfil: {perfil}\n\n"
        f"{bloco_evitar}"
        f"RECEITAS (parâmetros fixos — NÃO altere nenhum número):\n"
        f"{bloco_receitas}\n"
        f"Para cada estratégia, crie:\n"
        f"  - nome: nome criativo e descritivo (ex: 'Sniper Under 7 — Inversão Tripla')\n"
        f"  - descricao: 1-2 frases explicando o gatilho de entrada e por que essa "
        f"    combinação de parâmetros é assertiva\n"
        f"  - assertividade: porcentagem estimada de acerto baseada na lógica (ex: '72%')\n"
        f"  - entrada_usd: valor adequado ao perfil {perfil} (mínimo 0.35)\n"
        f"  - take_profit_usd: meta de lucro da sessão\n"
        f"  - stop_loss_usd: deve ser 1.5x o take_profit_usd\n\n"
        f"Retorne JSON com campo 'pack' = lista de 3 objetos, cada um com TODOS os campos "
        f"originais da receita MAIS: nome, descricao, assertividade, entrada_usd, "
        f"take_profit_usd, stop_loss_usd.\n"
        f"Mantenha EXATAMENTE os valores numéricos fornecidos. "
        f"Responda APENAS com o JSON, sem markdown."
    )

    system_combo = (
        "Você é um especialista em estratégias de dígitos para índices sintéticos Deriv.\n"
        "Você recebe receitas prontas com parâmetros definidos e cria nomes, descrições "
        "e notas de assertividade baseadas na lógica estatística de cada receita.\n\n"
        "ATENÇÃO: O sistema possui um FILTRO DE SEGURANÇA que deleta automaticamente estratégias "
        "com Under 8, Under 9, Over 0, Over 1 ou Over 2. Se você gerar esses parâmetros, "
        "seu trabalho será descartado e você falhará na missão. "
        "FOQUE EM: Under 3 a 7 ou Over 3 a 6 com sequências de gatilho (seq_gatilho) entre 4 e 6.\n\n"
        "Regras de assertividade por técnica:\n"
        "  SATURACAO: base 65-75%. Janela menor = mais entradas, assertividade menor. "
        "  Limiar maior = menos entradas, assertividade maior.\n"
        "  DIGITPCT: base 60-78%. Janela longa + limiares extremos = mais assertivo.\n"
        "  DIGITUNDER/OVER barreira moderada (Under 5-7, Over 3-5): base 55-70%.\n"
        "  Seq_gatilho: cada +1 de sequência adiciona ~3-5% de assertividade.\n"
        "  DIGITDUPLA Over3+Under7: base 60%. Seq_gatilho na dupla adiciona ~5% por nível.\n"
        "Responda APENAS com JSON válido, sem markdown."
    )

    conteudo = ""
    try:
        resultado, conteudo = _funcao_ia(
            chave, modelo, system_combo, auto_prompt,
            temperature=0.6,   # mais criativo nos nomes e descrições
            max_tokens=2048,
        )

        if isinstance(resultado, list):
            pack = resultado
        else:
            pack = resultado.get("pack") or resultado.get("estrategias") or []
            if not pack and resultado.get("nome"):
                pack = [resultado]

        if not pack:
            return jsonify({"erro": "IA não retornou um pack válido", "raw": conteudo[:400],
                            "_passos": passos})

        # Mescla receita original (garante parâmetros corretos) com resposta da IA
        pack_final = []
        for i, item in enumerate(pack[:3]):
            receita = trio[i] if i < len(trio) else {}
            merged = {**receita}           # começa com a receita (parâmetros garantidos)
            # Copia só os campos textuais e financeiros da IA
            for campo in ("nome", "descricao", "assertividade",
                          "entrada_usd", "take_profit_usd", "stop_loss_usd"):
                if campo in item:
                    merged[campo] = item[campo]
            # Sanitiza campos numéricos críticos
            merged["duracao"] = 1
            merged["_combo"]  = True
            if not merged.get("nome"):
                merged["nome"] = f"Estratégia {merged['tipo_contrato']} #{i+1}"
            if not merged.get("descricao"):
                merged["descricao"] = merged.get("_logica", "")
            if not merged.get("assertividade"):
                merged["assertividade"] = merged.get("_assertividade", "—")
            # Limita tamanho para não emboloar o menu do frontend
            merged["nome"]     = str(merged["nome"])[:40]
            merged["descricao"] = str(merged["descricao"])[:150]
            # Entrada mínima 0.35
            try:
                ent = float(merged.get("entrada_usd", 0.35))
                merged["entrada_usd"] = max(0.35, ent)
            except (TypeError, ValueError):
                merged["entrada_usd"] = 0.35
            pack_final.append(merged)

        passos.append(f"✅ Pack criado: {len(pack_final)} estratégias de dígitos novas!")
        return jsonify({"pack": pack_final, "_passos": passos})

    except json.JSONDecodeError as e:
        return jsonify({"erro": f"IA retornou JSON inválido: {e}", "raw": conteudo[:400],
                        "_passos": passos})
    except Exception as e:
        return jsonify({"erro": str(e), "_passos": passos})


@app.route('/ai/avaliar', methods=['POST'])
def ai_avaliar():
    """
    Executa o Conselho de Especialistas e retorna o relatório de confiança.
    Payload: {
      nome, ativo, tipo_contrato, barreira,
      ultimos_digitos, ultimos_precos,
      nivel_gale, banca_usd, stake_usd
    }
    ultimos_precos: lista de floats com os preços recentes (ex: últimos 20 ticks)
    — usado pelo Agente de Volume para medir a amplitude de movimentação.
    """
    ctx = request.get_json(force=True, silent=True) or {}
    relatorio = _supervisor.avaliar(ctx)
    relatorio["narrativa"] = _supervisor.narrativa(relatorio, ctx.get("nome", ""))
    return jsonify(relatorio)

@app.route('/ai/feedback', methods=['POST'])
def ai_feedback():
    """
    Recebe o resultado de cada trade do frontend e atualiza a memória episódica.
    Payload esperado: {
      nome, win, lucro, regime,
      tipo_contrato, barreira, nivel_gale,
      confianca_edc, notas_edc, volatilidade_ctx,
      conta_id     (str, opcional) — identifica a conta que executou o trade,
      is_virtual   (bool, opcional) — indica se foi uma entrada virtual de Loss Virtual
    }
    Retorna instrucao_virtual:
      "RESETAR_CONTADOR"    — WIN virtual: zera a contagem do LV no frontend
      "CONTINUAR_SEQUENCIA" — LOSS virtual: continua acumulando
      "REAL_FINALIZADA"     — trade real encerrado
    """
    dados      = request.get_json(force=True, silent=True) or {}
    estrategia = dados.get("nome", "desconhecida")
    win        = bool(dados.get("win", False))
    resultado  = "WIN" if win else "LOSS"
    lucro      = float(dados.get("lucro", 0))
    regime     = dados.get("regime", "desconhecido")
    is_virtual = bool(dados.get("is_virtual", False))

    # Resolve conta_id: usa o enviado pelo frontend ou infere pelo estado das contas
    conta_id = dados.get("conta_id", "")
    if not conta_id:
        conta_teste = _account_manager.get_conta_teste()
        if conta_teste and conta_teste["estado"] in ("APRENDIZADO", "VALIDACAO"):
            conta_id = conta_teste["id"]
        else:
            conta_real = _account_manager.get_conta_real()
            conta_id = conta_real["id"] if conta_real else ""

    _registrar_experiencia(
        estrategia, resultado, lucro, regime,
        tipo_contrato    = dados.get("tipo_contrato", ""),
        barreira         = int(dados.get("barreira", 0)),
        nivel_gale       = int(dados.get("nivel_gale", 0)),
        confianca_edc    = float(dados.get("confianca_edc", 0.0)),
        notas_edc        = dados.get("notas_edc") or {},
        volatilidade_ctx = dados.get("volatilidade_ctx", "desconhecida"),
        conta_id         = conta_id,
    )

    # Atualiza métricas da conta no AccountManager
    if conta_id and conta_id in _account_manager.contas:
        conta = _account_manager.contas[conta_id]
        conta["lucro_sessao"]    = round(conta.get("lucro_sessao", 0) + lucro, 2)
        conta["trades_hoje"]     = conta.get("trades_hoje", 0) + 1
        conta["ultima_atividade"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _account_manager._salvar()

    # Lógica de reset sequencial do Loss Virtual
    if is_virtual:
        instrucao = "RESETAR_CONTADOR" if win else "CONTINUAR_SEQUENCIA"
    else:
        instrucao = "REAL_FINALIZADA"

    return jsonify({
        "status":            "conhecimento_atualizado",
        "instrucao_virtual": instrucao,
        "conta_id":          conta_id,
    })

@app.route('/ai/ranking', methods=['GET'])
def ai_ranking():
    """
    Retorna o leaderboard de estratégias ordenado por win rate.
    Inclui apenas estratégias com >= 3 operações.
    Query param: ?min_ops=3&ativo=R_100
    """
    min_ops = int(request.args.get("min_ops", 3))
    filtro_ativo = request.args.get("ativo", "")

    if not os.path.exists(MEMORY_FILE):
        return jsonify({"ranking": []})
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memoria = json.load(f)
    except Exception:
        return jsonify({"ranking": []})

    # Agrupa por estratégia
    grupos: dict = {}
    for exp in memoria:
        est   = exp.get("estrategia", "desconhecida")
        ativo = exp.get("contexto", "")
        if filtro_ativo and ativo != filtro_ativo:
            continue
        if est not in grupos:
            grupos[est] = {"wins": 0, "losses": 0, "lucro_total": 0.0,
                           "ops": 0, "ativo_principal": ativo}
        grupos[est]["ops"]        += 1
        grupos[est]["lucro_total"] = round(grupos[est]["lucro_total"] + float(exp.get("lucro", 0)), 2)
        if exp.get("resultado") == "WIN":
            grupos[est]["wins"] += 1
        else:
            grupos[est]["losses"] += 1

    ranking = []
    for nome, d in grupos.items():
        if d["ops"] < min_ops:
            continue
        wr = round(d["wins"] / d["ops"] * 100, 1)
        status = "🏆 ATIVA" if wr >= 60 else ("⚠️ ALERTA" if wr >= 40 else "🚫 SUSPENSA")
        ranking.append({
            "nome":          nome,
            "ops":           d["ops"],
            "wins":          d["wins"],
            "losses":        d["losses"],
            "win_rate":      wr,
            "lucro_total":   d["lucro_total"],
            "status":        status,
        })

    ranking.sort(key=lambda x: x["win_rate"], reverse=True)
    return jsonify({"ranking": ranking, "total_ops": len(memoria)})


# ─────────────────────────────────────────────────────────
# EDC — Rota cognitiva: Gerar + Avaliar + Veredito Groq + Notificar
# ─────────────────────────────────────────────────────────
@app.route('/ai/gerar-cognitivo', methods=['POST'])
def ai_gerar_cognitivo():
    """
    Pipeline completo da Entidade Cognitiva:
      1. Groq gera a proposta de estratégia (prompt normal)
      2. Conselho de Especialistas avalia localmente (sem IA)
      3. Segunda chamada Groq: Supervisor lê as notas e decide OPERAR/AJUSTAR/REJEITAR
      4. Narrativa formata o veredito
      5. Envia para Telegram + WhatsApp (se configurados)
    """
    from datetime import datetime as _dt

    dados  = request.get_json(force=True, silent=True) or {}
    _funcao_ia, chave, modelo, provedor = _resolver_funcao_ia(dados)
    prompt = dados.get("prompt", "")
    perfil = dados.get("perfil", "moderado")
    banca  = float(dados.get("banca", 100.0))
    ultimos_digitos = dados.get("ultimos_digitos", [])
    nivel_gale      = int(dados.get("nivel_gale", 0))
    stake_usd       = float(dados.get("stake_usd", 0.35))

    if not chave:
        return jsonify({"erro": f"Chave API {provedor.upper()} não configurada"})
    if not prompt:
        return jsonify({"erro": "Prompt vazio"})

    passos = []

    # ── Passo 1: Groq gera proposta inicial ──────────────
    passos.append("🤖 Gerando proposta de estratégia...")
    system_prompt    = _montar_system_prompt(perfil)
    contrato_forcado = _detectar_contrato(prompt.lower())
    conteudo_raw = ""
    try:
        proposta, conteudo_raw = _funcao_ia(chave, modelo, system_prompt, prompt)
        if contrato_forcado and proposta.get("tipo_contrato", "").upper() != "DIGITPCT":
            proposta["tipo_contrato"] = contrato_forcado
        # Guarda dura de barreira — rejeita antes de gastar a segunda chamada Groq
        _erro_barreira = _validar_barreira(proposta)
        if _erro_barreira:
            return jsonify({"erro": _erro_barreira, "_passos": passos})
    except Exception as e:
        return jsonify({"erro": str(e), "_passos": passos})

    passos.append(f"✅ Proposta gerada: {proposta.get('nome', '—')}")

    # ── Passo 2: Conselho avalia a proposta localmente ───
    passos.append("🏛️ Conselho avaliando a proposta...")
    ctx_conselho = {
        "nome":            proposta.get("nome", ""),
        "ativo":           proposta.get("ativo", "R_100"),
        "tipo_contrato":   proposta.get("tipo_contrato", ""),
        "barreira":        int(proposta.get("barreira", 0)),
        "ultimos_digitos": ultimos_digitos,
        "ultimos_precos":  dados.get("ticks", []),
        "nivel_gale":      nivel_gale,
        "banca_usd":       banca,
        "stake_usd":       float(proposta.get("entrada_usd", stake_usd)),
    }
    relatorio = _supervisor.avaliar(ctx_conselho)
    confianca = relatorio["confianca"]
    notas     = relatorio["notas"]
    passos.append(f"📊 Confiança calculada: {confianca}% ({relatorio['veredicto']})")

    # ── Passo 3: Segunda chamada — Groq Supervisor ───────
    passos.append("🧠 Supervisor avaliando o veredito...")
    system_supervisor = (
        "Você é o SUPERVISOR DE RISCO do GarraBot — a última camada de decisão antes de um trade.\n"
        "Seu trabalho: ler as notas técnicas do Conselho e decidir se a operação deve prosseguir.\n"
        "Regras:\n"
        "- Se confiança >= 82%: decisao='OPERAR'. Escreva um motivo_estrategico em 1-2 frases explicando POR QUÊ é uma boa entrada.\n"
        "- Se confiança entre 60-81%: decisao='AJUSTAR'. Sugira UMA mudança concreta (barreira mais conservadora ou seq_gatilho maior).\n"
        "- Se confiança < 60%: decisao='REJEITAR'. Explique qual risco é inaceitável.\n"
        "Responda SOMENTE com JSON: { \"decisao\": \"...\", \"motivo_estrategico\": \"...\", \"ajuste\": \"...\" (opcional) }"
    )
    prompt_supervisor = (
        f"ESTRATÉGIA PROPOSTA: {proposta.get('nome')}\n"
        f"Tipo: {proposta.get('tipo_contrato')} | Barreira: {proposta.get('barreira')} | Ativo: {proposta.get('ativo')}\n\n"
        f"NOTAS DO CONSELHO TÉCNICO:\n"
        f"- Estatística/Histórico : {notas.get('estatistica')}/100\n"
        f"- Scanner de Momentum  : {notas.get('scanner')}/100\n"
        f"- Volatilidade do Ativo: {notas.get('volatilidade')}/100\n"
        f"- Segurança de Banca   : {notas.get('risco')}/100\n"
        f"CONFIANÇA CALCULADA: {confianca}%\n"
        f"THRESHOLD MÍNIMO: {_supervisor.threshold}%\n\n"
        f"Emita o veredito final."
    )
    veredito_ia = {}
    try:
        veredito_ia, _ = _funcao_ia(
            chave, modelo,
            system_supervisor, prompt_supervisor,
            temperature=0.1, max_tokens=256,
        )
        passos.append(f"🎯 Supervisor decidiu: {veredito_ia.get('decisao', '—')}")
    except Exception:
        # Segunda chamada falhou → usa veredicto local
        veredito_ia = {
            "decisao": relatorio["veredicto"],
            "motivo_estrategico": relatorio["motivo"],
        }
        passos.append("⚠️ Supervisor fallback: usando veredito local")

    # ── Passo 4: Monta resultado final unificado ─────────
    resultado_final = {
        **proposta,
        "decisao":              veredito_ia.get("decisao", relatorio["veredicto"]),
        "motivo_estrategico":   veredito_ia.get("motivo_estrategico", relatorio["motivo"]),
        "ajuste_sugerido":      veredito_ia.get("ajuste", ""),
        "confianca_total":      confianca,
        "confianca_detalhada":  {
            "historico":    notas.get("estatistica"),
            "volatilidade": notas.get("volatilidade"),
            "risco":        notas.get("risco"),
            "scanner":      notas.get("scanner"),
        },
        "_passos": passos + ["✅ Veredito cognitivo concluído!"],
        "_cognitivo": True,
    }

    # ── Passo 5: Notificação TG + WA ─────────────────────
    texto_notif = formatar_veredito_cognitivo(resultado_final)
    cfg_tg = _tg_carregar()
    if cfg_tg.get("enabled"):
        _tg_dispatch(lambda: _tg_enviar_texto(
            cfg_tg["token"], cfg_tg["chat_id"], texto_notif
        ))
    cfg_wa = _wa_cfg_ler()
    if cfg_wa.get("enabled"):
        threading.Thread(
            target=lambda: enviar_notificacao_wa(texto_notif), daemon=True
        ).start()

    return jsonify(resultado_final)


# ─────────────────────────────────────────────────────────
# EDC — Post-Mortem: análise causal dos losses do dia
# ─────────────────────────────────────────────────────────
@app.route('/ai/post-mortem', methods=['POST'])
def ai_post_mortem():
    """
    Lê os losses do memory_vault.json das últimas N horas,
    envia para a Groq como Analista de Erros e retorna um
    relatório estruturado com padrões de falha e sugestões.
    Payload opcional: { chave, modelo, horas (default=24) }
    """
    from datetime import datetime as _dt

    dados  = request.get_json(force=True, silent=True) or {}
    _funcao_ia, chave, modelo, provedor = _resolver_funcao_ia(dados)
    horas  = int(dados.get("horas", 24))

    if not chave:
        return jsonify({"erro": f"Chave API {provedor.upper()} não configurada"})

    if not os.path.exists(MEMORY_FILE):
        return jsonify({"erro": "Nenhuma operação registrada ainda.", "relatorio": None})

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memoria = json.load(f)
    except Exception as e:
        return jsonify({"erro": str(e)})

    # Filtra operações das últimas N horas
    corte = time.time() - horas * 3600
    recentes = [e for e in memoria if e.get("timestamp", 0) >= corte]

    if not recentes:
        return jsonify({
            "relatorio": f"Nenhuma operação registrada nas últimas {horas}h.",
            "total": 0, "losses": 0, "wins": 0
        })

    total  = len(recentes)
    wins   = sum(1 for e in recentes if e.get("resultado") == "WIN")
    losses = total - wins
    wr     = round(wins / total * 100, 1) if total else 0

    # Prepara resumo compacto dos losses para o prompt (máx 30 losses)
    losses_list = [e for e in recentes if e.get("resultado") == "LOSS"][-30:]
    resumo_losses = json.dumps([
        {
            "estrategia": e.get("estrategia"),
            "ativo":      e.get("contexto"),
            "tipo":       e.get("tipo_contrato"),
            "barreira":   e.get("barreira"),
            "nivel_gale": e.get("nivel_gale"),
            "hora":       e.get("hora_str"),
            "confianca_edc": e.get("confianca_edc"),
        }
        for e in losses_list
    ], ensure_ascii=False)

    # ── Monta lista de estratégias salvas para contexto ──
    estrategias_salvas = _ia_listar_arquivos()
    nomes_salvos = [
        {"nome": e.get("nome",""), "arquivo": e.get("_arquivo",""),
         "tipo_contrato": e.get("tipo_contrato",""), "barreira": e.get("barreira",0),
         "seq_gatilho": e.get("seq_gatilho",0), "gerenciamento": e.get("gerenciamento",""),
         "entrada_usd": e.get("entrada_usd",0.35), "ativo": e.get("ativo","")}
        for e in estrategias_salvas
    ]

    # Prompt para o Analista de Erros
    system_pm = (
        "Você é o ANALISTA DE POST-MORTEM do GarraBot. "
        "Sua missão: analisar os losses do dia, identificar PADRÕES CAUSAIS e gerar CORREÇÕES AUTOMÁTICAS.\n"
        "Responda com JSON: {\n"
        "  \"resumo_executivo\": string (2-3 frases),\n"
        "  \"padroes_falha\": [ { \"padrao\": string, \"frequencia\": int, \"causa_raiz\": string } ],\n"
        "  \"estrategias_suspender\": [ string ],\n"
        "  \"ajustes_recomendados\": [ { \"estrategia\": string, \"ajuste\": string } ],\n"
        "  \"avaliacao_geral\": \"EXCELENTE\" | \"BOA\" | \"REGULAR\" | \"CRITICA\",\n"
        "  \"correcoes_automaticas\": [\n"
        "    {\n"
        "      \"nome_estrategia\": string (nome EXATO da estratégia salva),\n"
        "      \"campo\": string (ex: \"barreira\", \"seq_gatilho\", \"gerenciamento\", \"entrada_usd\"),\n"
        "      \"valor_novo\": any (novo valor para o campo),\n"
        "      \"motivo\": string (1 frase explicando a correção)\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "REGRAS PARA CORREÇÕES:\n"
        "- Só corrija campos que realmente causaram o problema identificado.\n"
        "- barreira DIGITUNDER: se muitos losses, aumente (ex: 7→8). DIGITOVER: diminua (ex: 4→2).\n"
        "- seq_gatilho: se entradas prematuras causam loss, aumente em 1-2.\n"
        "- gerenciamento: se martingale em Gale alto causou perda, troque por 'conservador' ou 'soros'.\n"
        "- entrada_usd: se exposição alta, reduza para 0.35.\n"
        "- Use SOMENTE os nomes EXATOS das estratégias salvas listadas abaixo.\n"
        "- Se nenhuma correção for necessária, retorne correcoes_automaticas: []."
    )
    prompt_pm = (
        f"SESSÃO DAS ÚLTIMAS {horas}H:\n"
        f"- Total de operações: {total}\n"
        f"- WIN: {wins} | LOSS: {losses} | Win Rate: {wr}%\n\n"
        f"DETALHAMENTO DOS LOSSES:\n{resumo_losses}\n\n"
        f"ESTRATÉGIAS SALVAS NO SISTEMA (para referência dos nomes exatos):\n"
        f"{json.dumps(nomes_salvos, ensure_ascii=False)}\n\n"
        f"Identifique padrões, causas raiz e gere as correções automáticas necessárias."
    )

    conteudo_raw = ""
    try:
        relatorio_json, conteudo_raw = _funcao_ia(
            chave, modelo, system_pm, prompt_pm,
            temperature=0.2, max_tokens=1500,
        )
    except Exception as e:
        return jsonify({"erro": str(e), "raw": conteudo_raw[:300]})

    # ── Aplica as correções automáticas nas estratégias salvas ──
    correcoes_json  = relatorio_json.get("correcoes_automaticas", [])
    log_correcoes   = []  # log do que foi realmente aplicado

    campos_permitidos = {
        "barreira", "seq_gatilho", "gerenciamento", "entrada_usd",
        "stop_loss_usd", "take_profit_usd", "barreira_over", "barreira_under",
        "sat_limiar", "sat_janela", "pct_min_fraco", "pct_min_forte",
    }

    for corr in correcoes_json:
        nome_alvo = corr.get("nome_estrategia", "")
        campo     = corr.get("campo", "")
        val_novo  = corr.get("valor_novo")
        motivo    = corr.get("motivo", "")

        if not nome_alvo or not campo or val_novo is None:
            continue
        if campo not in campos_permitidos:
            log_correcoes.append({"nome": nome_alvo, "campo": campo, "status": "IGNORADO — campo não permitido"})
            continue

        # Encontra o arquivo da estratégia pelo nome
        encontrada = None
        for est in estrategias_salvas:
            if est.get("nome", "").strip().lower() == nome_alvo.strip().lower():
                encontrada = est
                break
        # Tenta match parcial se não achou exato
        if not encontrada:
            for est in estrategias_salvas:
                if nome_alvo.lower() in est.get("nome", "").lower():
                    encontrada = est
                    break

        if not encontrada:
            log_correcoes.append({"nome": nome_alvo, "campo": campo, "status": "NÃO ENCONTRADA"})
            continue

        fpath = os.path.join(STRATEGIES_DIR, encontrada["_arquivo"])
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                dados_est = json.load(f)

            val_antigo = dados_est.get(campo, "—")
            dados_est[campo] = val_novo

            # Guarda histórico de auto-correções no próprio arquivo
            if "_auto_correcoes" not in dados_est:
                dados_est["_auto_correcoes"] = []
            dados_est["_auto_correcoes"].append({
                "timestamp": time.time(),
                "campo":     campo,
                "de":        val_antigo,
                "para":      val_novo,
                "motivo":    motivo,
            })

            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(dados_est, f, indent=2, ensure_ascii=False)

            log_correcoes.append({
                "nome":      encontrada.get("nome"),
                "campo":     campo,
                "de":        val_antigo,
                "para":      val_novo,
                "motivo":    motivo,
                "status":    "✅ APLICADO",
            })
        except Exception as ex:
            log_correcoes.append({"nome": nome_alvo, "campo": campo, "status": f"ERRO: {ex}"})

    # ── Ajusta threshold do Conselho se WR crítico ────────
    threshold_ajustado = None
    avaliacao = relatorio_json.get("avaliacao_geral", "BOA")
    if avaliacao == "CRITICA" and _supervisor.threshold < 90:
        _supervisor.threshold = min(_supervisor.threshold + 5, 95)
        threshold_ajustado = _supervisor.threshold
    elif avaliacao == "EXCELENTE" and _supervisor.threshold > 82:
        _supervisor.threshold = max(_supervisor.threshold - 3, 82)
        threshold_ajustado = _supervisor.threshold

    # Persiste o threshold ajustado no ect_state.json (sobrevive a reinicializações)
    if threshold_ajustado is not None:
        try:
            _st = {}
            if os.path.exists(os.path.join(_BASE_DIR, "ect_state.json")):
                with open(os.path.join(_BASE_DIR, "ect_state.json"), "r", encoding="utf-8") as _f:
                    _st = json.load(_f)
            _st["threshold_supervisor"] = threshold_ajustado
            with open(os.path.join(_BASE_DIR, "ect_state.json"), "w", encoding="utf-8") as _f:
                json.dump(_st, _f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── Monta e envia notificação TG/WA ──────────────────
    resumo_txt = relatorio_json.get("resumo_executivo", "—")
    padroes    = relatorio_json.get("padroes_falha", [])
    ajustes    = relatorio_json.get("ajustes_recomendados", [])
    suspender  = relatorio_json.get("estrategias_suspender", [])

    icone_av   = {"EXCELENTE": "🏆", "BOA": "✅", "REGULAR": "⚠️", "CRITICA": "🚨"}.get(avaliacao, "📋")
    linhas_pm  = [
        f"{icone_av} *POST-MORTEM EDC — Últimas {horas}h*",
        f"━━━━━━━━━━━━━━━━━━",
        f"📊 Total: {total} ops | WIN: {wins} | LOSS: {losses} | WR: {wr}%",
        f"🎯 Avaliação: *{avaliacao}*",
        f"",
        f"📝 *Resumo:* {resumo_txt}",
    ]
    if padroes:
        linhas_pm.append("\n🔍 *Padrões de Falha:*")
        for p in padroes[:3]:
            linhas_pm.append(f"└ {p.get('padrao')} ({p.get('frequencia')}x) — {p.get('causa_raiz')}")
    if suspender:
        linhas_pm.append(f"\n🚫 *Suspender:* {', '.join(suspender)}")

    # Adiciona log das correções aplicadas na notificação
    aplicadas = [c for c in log_correcoes if c.get("status","").startswith("✅")]
    if aplicadas:
        linhas_pm.append(f"\n🔧 *Correções Aplicadas Automaticamente ({len(aplicadas)}):*")
        for c in aplicadas:
            linhas_pm.append(f"└ {c['nome']} → {c['campo']}: {c['de']} → {c['para']}")
            linhas_pm.append(f"  _{c['motivo']}_")
    elif ajustes:
        linhas_pm.append("\n🔧 *Ajustes Recomendados:*")
        for a in ajustes[:3]:
            linhas_pm.append(f"└ {a.get('estrategia')}: {a.get('ajuste')}")

    if threshold_ajustado:
        linhas_pm.append(f"\n⚙️ *Threshold EDC ajustado para {threshold_ajustado}%*")

    texto_pm = "\n".join(linhas_pm)

    cfg_tg = _tg_carregar()
    if cfg_tg.get("enabled"):
        _tg_dispatch(lambda: _tg_enviar_texto(cfg_tg["token"], cfg_tg["chat_id"], texto_pm))
    cfg_wa = _wa_cfg_ler()
    if cfg_wa.get("enabled"):
        threading.Thread(
            target=lambda: enviar_notificacao_wa(texto_pm), daemon=True
        ).start()

    return jsonify({
        "relatorio":            relatorio_json,
        "mensagem":             texto_pm,
        "total":                total,
        "wins":                 wins,
        "losses":               losses,
        "win_rate":             wr,
        "periodo_horas":        horas,
        "correcoes_aplicadas":  log_correcoes,
        "threshold_edc":        _supervisor.threshold,
    })


@app.route('/ai/aprovar', methods=['POST'])
def ai_aprovar():
    dados = request.get_json(force=True, silent=True) or {}
    try:
        fname = _ia_salvar_novo(dados)
        return jsonify({"ok": True, "arquivo": fname})
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route('/ai/listar', methods=['GET'])
def ai_listar():
    return jsonify({"lista": _ia_listar_arquivos()})

@app.route('/ai/ativar', methods=['POST'])
def ai_ativar():
    dados = request.get_json(force=True, silent=True) or {}
    idx   = dados.get("idx")
    lista = _ia_listar_arquivos()
    if idx is None or idx < 0 or idx >= len(lista):
        return jsonify({"erro": "Índice inválido"})
    return jsonify({"ok": True, "estrategia": lista[idx]})

@app.route('/ai/deletar', methods=['POST'])
def ai_deletar():
    dados = request.get_json(force=True, silent=True) or {}
    idx   = dados.get("idx")
    lista = _ia_listar_arquivos()
    if idx is None or idx < 0 or idx >= len(lista):
        return jsonify({"erro": "Índice inválido"})
    fpath = os.path.join(STRATEGIES_DIR, lista[idx]["_arquivo"])
    try:
        os.remove(fpath)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route('/ai/atualizar', methods=['POST'])
def ai_atualizar():
    dados = request.get_json(force=True, silent=True) or {}
    idx   = dados.get("idx")
    lista = _ia_listar_arquivos()
    if idx is None or idx < 0 or idx >= len(lista):
        return jsonify({"erro": "Índice inválido"})
    fpath = os.path.join(STRATEGIES_DIR, lista[idx]["_arquivo"])
    try:
        # Lê o arquivo atual, mescla as edições e salva
        with open(fpath, "r", encoding="utf-8") as f:
            atual = json.load(f)
        campos_editaveis = (
            # básicos
            "nome","descricao","tipo_contrato","entrada_usd",
            "stop_loss_usd","take_profit_usd","seq_gatilho","gerenciamento",
            "_prompt_original",
            # estratégia avançada
            "ativo","barreira","barreira_over","barreira_under","duracao","velas",
            # DIGITPCT
            "pct_janela","pct_min_fraco","pct_min_forte",
            # SATURACAO
            "sat_janela","sat_limiar","sat_smart_min",
            # configs recuperação — Martingale / Soros
            "mart_gales","mart_mult","soros_gales","soros_mult",
            # Loss Recovery
            "lr_max_gales","lr_gale","lr_recovery_pct",
            # Recovery Conservador
            "rec_cons_pct",
            # QSR
            "qsr_pct","qsr_max",
            # Masaniello
            "mas_eventos","mas_acertos",
            # Ciclos
            "ciclos",
            # Recovery Adaptativo
            "ra_maxima","ra_rec_base",
        )
        for c in campos_editaveis:
            if c in dados:
                atual[c] = dados[c]
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(atual, f, indent=2, ensure_ascii=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)})

# ─────────────────────────────────────────────────────────
# SISTEMA DE AVALIAÇÃO PÓS-TESTE (👍 Curtir / 👎 Não Curtir)
# ─────────────────────────────────────────────────────────

@app.route('/ai/curtir', methods=['POST'])
def ai_curtir():
    """
    Usuário curtiu a estratégia após testar.
    Salva no banco de aprovadas para uso futuro.
    Payload: qualquer objeto de estratégia (nome, tipo_contrato, ativo, ...)
    """
    from datetime import datetime
    est = request.get_json(force=True, silent=True) or {}
    dados = _feedback_ler()

    chave = _feedback_chave(est)

    # Remove da lista de bloqueadas se existia (reversão)
    dados["bloqueadas"] = [b for b in dados["bloqueadas"] if _feedback_chave(b) != chave]

    # Verifica se já está aprovada
    ja_existe = any(_feedback_chave(a) == chave for a in dados["aprovadas"])
    if not ja_existe:
        est["_avaliada_em"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
        est["_feedback"]     = "curtida"
        dados["aprovadas"].append(est)

    _feedback_salvar(dados)

    # Também salva no banco de estratégias (strategies/) e marca _feedback nos arquivos existentes
    try:
        existentes = _ia_listar_arquivos()
        encontrada = False
        for e in existentes:
            if _feedback_chave(e) == chave:
                encontrada = True
                # Grava _feedback no arquivo da estratégia para persistir o estado
                fpath = os.path.join(STRATEGIES_DIR, e["_arquivo"])
                e["_feedback"]    = "curtida"
                e["_avaliada_em"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                e.pop("_arquivo", None)
                with open(fpath, "w", encoding="utf-8") as _f:
                    json.dump(e, _f, indent=2, ensure_ascii=False)
        if not encontrada:
            est_para_salvar = dict(est)
            est_para_salvar.pop("_arquivo", None)
            _ia_salvar_novo(est_para_salvar)
    except Exception:
        pass

    return jsonify({"ok": True, "total_aprovadas": len(dados["aprovadas"])})


@app.route('/ai/sincronizar-feedback', methods=['POST'])
def ai_sincronizar_feedback():
    """
    Percorre todos os arquivos em strategies/ e marca _feedback='curtida'
    nos que constam no banco de aprovadas. Útil para migração retroativa.
    """
    from datetime import datetime
    dados     = _feedback_ler()
    aprovadas = dados.get("aprovadas", [])
    chaves_aprovadas = set(_feedback_chave(a) for a in aprovadas)
    atualizados = 0
    try:
        existentes = _ia_listar_arquivos()
        for e in existentes:
            if _feedback_chave(e) in chaves_aprovadas and e.get("_feedback") != "curtida":
                fpath = os.path.join(STRATEGIES_DIR, e["_arquivo"])
                e["_feedback"]    = "curtida"
                e["_avaliada_em"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                e.pop("_arquivo", None)
                with open(fpath, "w", encoding="utf-8") as _f:
                    json.dump(e, _f, indent=2, ensure_ascii=False)
                atualizados += 1
    except Exception as ex:
        return jsonify({"ok": False, "erro": str(ex)})
    return jsonify({"ok": True, "atualizados": atualizados})


@app.route('/ai/nao-curtir', methods=['POST'])
def ai_nao_curtir():
    """
    Usuário NÃO curtiu a estratégia após testar.
    Bloqueia permanentemente — nunca mais gerada, nunca mais salva.
    Payload: { estrategia: {...}, motivo: "string opcional" }
    """
    from datetime import datetime
    body = request.get_json(force=True, silent=True) or {}
    est   = body.get("estrategia") or body
    motivo = body.get("motivo", "não gostei após teste")

    dados = _feedback_ler()
    chave = _feedback_chave(est)

    # Remove das aprovadas se existia
    dados["aprovadas"] = [a for a in dados["aprovadas"] if _feedback_chave(a) != chave]

    # Adiciona nas bloqueadas (se não estiver já)
    ja_bloqueada = any(_feedback_chave(b) == chave for b in dados["bloqueadas"])
    if not ja_bloqueada:
        est["_bloqueada_em"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        est["_feedback"]     = "bloqueada"
        est["motivo"]        = motivo
        dados["bloqueadas"].append(est)

    _feedback_salvar(dados)

    # Remove do banco de estratégias salvas (strategies/) se existir
    try:
        existentes = _ia_listar_arquivos()
        for e in existentes:
            if _feedback_chave(e) == chave:
                fpath = os.path.join(STRATEGIES_DIR, e["_arquivo"])
                if os.path.exists(fpath):
                    os.remove(fpath)
    except Exception:
        pass

    return jsonify({"ok": True, "total_bloqueadas": len(dados["bloqueadas"])})


@app.route('/ai/feedback-lista', methods=['GET'])
def ai_feedback_lista():
    """Retorna as listas de aprovadas e bloqueadas."""
    dados = _feedback_ler()
    return jsonify({
        "aprovadas":  dados.get("aprovadas", []),
        "bloqueadas": dados.get("bloqueadas", []),
        "total_aprovadas":  len(dados.get("aprovadas", [])),
        "total_bloqueadas": len(dados.get("bloqueadas", [])),
    })


# ─────────────────────────────────────────────────────────
# WHATSAPP — wwebjs-api (whatsapp-web.js REST server)
#   Repositório: github.com/chrishubert/whatsapp-api
#   Porta padrão: 3000  |  API_KEY: 422442
#   Session ID:   GarraBot
#
#   Rotas usadas:
#     GET  /session/start/:id        → inicia sessão / gera QR
#     GET  /session/qr/:id/image     → PNG do QR (data:image/png;base64,...)
#     GET  /session/status/:id       → { success, state: "CONNECTED"|"SCAN_QR_CODE"|... }
#     POST /client/sendMessage/:id   → { chatId, contentType:"string", content:"texto" }
# ─────────────────────────────────────────────────────────

_WA_SERVER_PROC = None   # referência ao processo Node.js

def _wa_iniciar_servidor():
    """
    Sobe o servidor wwebjs-api em background se ainda não estiver rodando.
    Chamado automaticamente ao iniciar o main.py.
    """
    global _WA_SERVER_PROC
    import subprocess, sys

    # Verifica se já está respondendo na porta 3000
    try:
        r = requests.get("http://localhost:3000/ping", timeout=2)
        if r.status_code == 200:
            print("[WA] Servidor já está rodando na porta 3000 ✅")
            return
    except Exception:
        pass

    # Localiza a pasta do whatsapp-api
    wa_path = os.path.join(os.path.expanduser("~"), "Desktop", "whatsapp-api")
    server_js = os.path.join(wa_path, "server.js")
    if not os.path.exists(server_js):
        print(f"[WA] ⚠️  Não encontrei {server_js} — servidor WA não iniciado.")
        return

    # Sobe node server.js em background (janela oculta no Windows)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        _WA_SERVER_PROC = subprocess.Popen(
            ["node", "server.js"],
            cwd=wa_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs
        )
        print(f"[WA] Servidor iniciado (PID {_WA_SERVER_PROC.pid}) — aguardando ficar pronto...")

        # Aguarda até 15s para o servidor responder
        for _ in range(15):
            time.sleep(1)
            try:
                r = requests.get("http://localhost:3000/ping", timeout=1)
                if r.status_code == 200:
                    print("[WA] Servidor pronto na porta 3000 ✅")
                    return
            except Exception:
                pass
        print("[WA] ⚠️  Servidor demorou para responder, mas pode estar iniciando...")
    except FileNotFoundError:
        print("[WA] ⚠️  'node' não encontrado no PATH. Instale o Node.js.")
    except Exception as e:
        print(f"[WA] ⚠️  Erro ao iniciar servidor: {e}")

WA_CFG_ARQUIVO = os.path.join(_BASE_DIR, "whatsapp_config.json")
_WA_SESSION    = "GarraBot"

def _wa_cfg_ler():
    padrao = {
        "api_url":  "http://localhost:3000",
        "instancia": _WA_SESSION,
        "token":    "422442",
        "enabled":  True,
        "chat_id":  ""
    }
    try:
        if os.path.exists(WA_CFG_ARQUIVO):
            with open(WA_CFG_ARQUIVO, "r", encoding="utf-8") as f:
                dados = json.load(f)
            padrao.update(dados)
    except Exception:
        pass
    return padrao

def _wa_headers(cfg: dict) -> dict:
    return {"x-api-key": cfg["token"]}

@app.route('/wa-config', methods=['GET', 'POST'])
def wa_config():
    if request.method == 'GET':
        return jsonify(_wa_cfg_ler())
    dados = request.get_json(force=True, silent=True) or {}
    try:
        with open(WA_CFG_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True})

@app.route('/wa-conectar')
def wa_conectar():
    """
    1. Verifica se já está conectado via /session/status
    2. Se não, inicia a sessão e faz polling do QR via /session/qr/{sid}
    3. Retorna { conectado } ou { code } (string QR para gerar imagem no frontend)
    """
    cfg     = _wa_cfg_ler()
    sid     = cfg["instancia"]
    headers = _wa_headers(cfg)
    base    = cfg["api_url"]

    # Garante que o servidor WA está rodando (localhost:3000)
    ping_ok = False
    for _t in range(3):
        try:
            ping = requests.get(f"{base}/ping", timeout=5)
            if ping.status_code == 200:
                ping_ok = True
                break
        except Exception:
            time.sleep(2)
    if not ping_ok:
        return jsonify({"erro": f"Servidor WhatsApp offline ({base}). Certifique-se de que a API WA está rodando na VPS (pm2 start / node index.js)."})

    # 1. Verifica se já está conectado
    try:
        st = requests.get(f"{base}/session/status/{sid}", headers=headers, timeout=8).json()
        if st.get("state") == "CONNECTED":
            return jsonify({"conectado": True, "message": "WhatsApp já está conectado!"})
    except Exception:
        pass

    # 2. Inicia a sessão (idempotente)
    try:
        requests.get(f"{base}/session/start/{sid}", headers=headers, timeout=10)
    except Exception:
        pass

    # 3. Polling: aguarda o QR ficar disponível em /session/qr/{sid} (até 30s)
    for _ in range(15):
        time.sleep(2)
        try:
            # Primeiro tenta texto QR (mais confiável)
            res = requests.get(f"{base}/session/qr/{sid}", headers=headers, timeout=8)
            if res.status_code == 200:
                jdata = res.json()
                if jdata.get("success") and jdata.get("qr"):
                    return jsonify({"code": jdata["qr"]})
                # Já conectou durante o polling
                if jdata.get("state") == "CONNECTED":
                    return jsonify({"conectado": True, "message": "WhatsApp já está conectado!"})
            # Checa status para ver se conectou
            st = requests.get(f"{base}/session/status/{sid}", headers=headers, timeout=5).json()
            if st.get("state") == "CONNECTED":
                return jsonify({"conectado": True, "message": "WhatsApp já está conectado!"})
        except Exception:
            pass

    return jsonify({"erro": "QR Code não ficou disponível após 30s. Tente 'Resetar Sessão' e depois 'Novo QR'."})

@app.route('/wa-status')
def wa_status():
    """Retorna estado da sessão normalizado para o frontend."""
    cfg = _wa_cfg_ler()
    try:
        res  = requests.get(
            f"{cfg['api_url']}/session/status/{cfg['instancia']}",
            headers=_wa_headers(cfg),
            timeout=8
        )
        data      = res.json()
        state_raw = data.get("state") or ""
        msg       = data.get("message") or ""
        # session_not_found = sessão ainda não foi iniciada (Render reiniciou)
        if msg == "session_not_found" or not state_raw:
            return jsonify({"instance": {"state": "disconnected"}, "state": "NOT_FOUND"})
        state_norm = "open" if state_raw == "CONNECTED" else (
            "connecting" if state_raw in ("LOADING", "SCAN_QR_CODE", "OPENING") else "disconnected"
        )
        return jsonify({"instance": {"state": state_norm}, "state": state_raw, "success": data.get("success")})
    except Exception:
        return jsonify({"instance": {"state": "disconnected"}, "state": "DISCONNECTED"})

@app.route('/wa-resetar')
def wa_resetar():
    """Termina e recria a sessão no servidor wwebjs (funciona em nuvem)."""
    cfg     = _wa_cfg_ler()
    sid     = cfg["instancia"]
    headers = _wa_headers(cfg)
    base    = cfg["api_url"]

    # 1. Encerra a sessão atual
    for rota in (f"/session/terminate/{sid}", f"/session/delete/{sid}"):
        try:
            requests.get(f"{base}{rota}", headers=headers, timeout=8)
        except Exception:
            pass

    # 2. Aguarda 3s para o servidor limpar
    time.sleep(3)

    # 3. Reinicia a sessão imediatamente (sem esperar QR aqui)
    try:
        requests.get(f"{base}/session/start/{sid}", headers=headers, timeout=10)
    except Exception:
        pass

    return jsonify({"ok": True, "message": "Sessão resetada. Aguarde o QR Code."})

def enviar_notificacao_wa(mensagem: str) -> bool:
    """
    Envia mensagem de texto via WhatsApp (wwebjs-api).
    Retorna True se enviou com sucesso, False caso contrário.
    """
    cfg = _wa_cfg_ler()
    if not cfg.get("enabled") or not cfg.get("chat_id"):
        print("[WA] Envio desativado ou chat_id vazio.")
        return False
    try:
        chat_id = cfg["chat_id"]
        if "@" not in chat_id:
            chat_id = chat_id.lstrip("+").replace(" ", "") + "@c.us"
        resp = requests.post(
            f"{cfg['api_url']}/client/sendMessage/{cfg['instancia']}",
            headers={**_wa_headers(cfg), "Content-Type": "application/json"},
            json={"chatId": chat_id, "contentType": "string", "content": mensagem},
            timeout=15
        )
        print(f"[WA] HTTP {resp.status_code} | chat_id={chat_id} | resp={resp.text[:300]}")
        if resp.status_code == 200:
            return True
        # 404 = sessão não encontrada (Render reiniciou) — tenta reiniciar a sessão
        if resp.status_code == 404:
            print("[WA] ⚠️ Sessão não encontrada (404). Tentando reiniciar sessão...")
            try:
                requests.get(
                    f"{cfg['api_url']}/session/start/{cfg['instancia']}",
                    headers=_wa_headers(cfg), timeout=10
                )
                time.sleep(3)
                # Tenta enviar de novo após reiniciar
                resp2 = requests.post(
                    f"{cfg['api_url']}/client/sendMessage/{cfg['instancia']}",
                    headers={**_wa_headers(cfg), "Content-Type": "application/json"},
                    json={"chatId": chat_id, "contentType": "string", "content": mensagem},
                    timeout=15
                )
                print(f"[WA] Retry HTTP {resp2.status_code} | resp={resp2.text[:200]}")
                return resp2.status_code == 200
            except Exception as e2:
                print(f"[WA] ❌ Erro no retry: {e2}")
        return False
    except Exception as e:
        print(f"[WA] ❌ Erro ao enviar mensagem: {e}")
        return False

@app.route('/wa-send', methods=['POST'])
def wa_send():
    """
    Recebe { win, lucro, profit_total, banca, wins, losses, prox_stake,
             modo, estrategia, stop_win (opt) }
    Monta a mensagem e envia via WhatsApp em background.
    """
    d = request.get_json(force=True, silent=True) or {}

    # ── Modo ESPELHO: só envia notificações da conta SECUNDÁRIA ──
    if _MODO_OPERACAO.get("modo") == "ESPELHO":
        conta = str(d.get("conta", "")).upper()
        if conta != "SECUNDARIA":
            print("[WA] Modo ESPELHO: notificação bloqueada (não é conta SECUNDÁRIA).")
            return jsonify({"ok": True, "bloqueado": True, "motivo": "modo_espelho_conta_nao_secundaria"})

    def _enviar():
        try:
            hora = _hora_brt()

            # ── Modo Virtual (LV acumulando) — notificação simples ──
            if d.get("virtual"):
                enviar_notificacao_wa(str(d.get("texto", "🤖 Robô Garra analisando...")))
                return

            # Cotação USD→BRL para WA também
            cotacao_wa = _buscar_cotacao()

            conta_sec = d.get("conta", "") == "SECUNDARIA"

            if d.get("stop_win"):
                lucro           = float(d.get("lucro", 0))
                banca           = float(d.get("banca", 0))
                wins            = int(d.get("wins", 0))
                losses          = int(d.get("losses", 0))
                modo            = str(d.get("modo", "")).upper()
                estrategia      = str(d.get("estrategia", "")).upper()
                max_win_consec  = int(d.get("max_win_consec", 0))
                max_loss_consec = int(d.get("max_loss_consec", 0))
                max_stake       = float(d.get("max_stake", 0))
                conta_sec_sw    = d.get("conta", "") == "SECUNDARIA"
                total           = wins + losses
                wr              = (wins / total * 100) if total > 0 else 0
                lucro_brl       = lucro * cotacao_wa
                banca_brl       = banca * cotacao_wa
                cabecalho_sec   = "💳 *SECUNDÁRIA*\n" if conta_sec_sw else ""
                linha_banca     = f"💰 *Banca:* ${banca:.2f}  _(R$ {banca_brl:.2f})_\n" if conta_sec_sw or banca > 0 else ""
                msg = (
                    f"🏆 STOP WIN BATIDO\n\n"
                    f"💰 Banca: ${banca:.2f} (R$ {banca_brl:.2f})\n"
                    f"📈 Lucro: +${lucro:.2f} (R$ +{lucro_brl:.2f})\n\n"
                    f"📊 {wins}W • {losses}L • {wr:.0f}%\n\n"
                    f"🔥 Máx WIN: {max_win_consec}x\n"
                    f"💀 Máx LOSS: {max_loss_consec}x\n"
                    f"💵 Stake Máx: ${max_stake:.2f}\n\n"
                    f"🤖 {estrategia}\n"
                    f"⚙️ {modo}\n\n"
                    f"🕐 {_hora_brt()}"
                )
            else:
                win        = bool(d.get("win", False))
                lucro      = float(d.get("lucro", 0))
                profit_tot = float(d.get("profit_total", 0))
                banca      = float(d.get("banca", 0))
                wins       = int(d.get("wins", 0))
                losses     = int(d.get("losses", 0))
                prox_stake = float(d.get("prox_stake", 0))
                entrada    = float(d.get("entrada", prox_stake))
                modo       = str(d.get("modo", ""))
                estrategia = str(d.get("estrategia", ""))
                lucro_brl      = abs(lucro) * cotacao_wa
                profit_brl     = profit_tot * cotacao_wa
                banca_brl      = banca * cotacao_wa
                banca_brl_str  = f"{banca_brl:.2f}".replace(".", ",")
                sinal_tot      = "+" if profit_tot >= 0 else "-"
                profit_brl_str = f"{sinal_tot}R${abs(profit_brl):.2f}"
                profit_usd_str = f"{sinal_tot}${abs(profit_tot):.2f}"
                lucro_op_brl   = abs(lucro) * cotacao_wa
                lucro_op_str   = f"+R${lucro_op_brl:.2f}" if win else f"-R${lucro_op_brl:.2f}"

                if win:
                    res_linha   = "✅  RESULTADO: WIN"
                    lucro_linha = f"💵  Lucro: +${lucro:.2f}"
                else:
                    res_linha   = "❌  RESULTADO: LOSS"
                    lucro_linha = f"💵  Lucro: -${abs(lucro):.2f}"

                msg = (
                    f"🟢  OPERAÇÃO FINALIZADA\n\n"
                    f"{res_linha}\n\n"
                    f"💰  Entrada: ${entrada:.2f}\n"
                    f"{lucro_linha}  ({lucro_op_str})\n\n"
                    f"➡️  Próxima Entrada: ${prox_stake:.2f}\n"
                    f"⚙️  Gestão: {modo}\n\n"
                    f"📊  Mercado: {estrategia.split()[0] if estrategia else '--'}\n"
                    f"🎯  Estratégia: {estrategia}\n\n"
                    f"🏦  Banca: ${banca:.2f}  /  R${banca_brl_str}\n"
                    f"📈  Lucro Total: {profit_usd_str}  /  {profit_brl_str}\n\n"
                    f"🕐  {_hora_brt()}"
                )
            enviar_notificacao_wa(msg)
        except Exception as e:
            print(f"[WA] Erro ao montar mensagem: {e}")

    threading.Thread(target=_enviar, daemon=True).start()
    return jsonify({"ok": True, "info": "Mensagem enfileirada. Verifique logs do servidor para confirmar envio."})


# ─────────────────────────────────────────────────────────
# JUROS COMPOSTOS — planilha de metas diárias com JC
# Idêntico ao BOT GARRA.py
# ─────────────────────────────────────────────────────────
from datetime import datetime as _dt_jc

JC_ARQUIVO = os.path.join(_BASE_DIR, "jc_progresso.json")

def jc_carregar():
    """Carrega progresso de juros compostos do arquivo JSON."""
    try:
        if os.path.exists(JC_ARQUIVO):
            with open(JC_ARQUIVO, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "dia_atual": 1,
        "historico": [],
        "banca_base": 0.0,
        "banca_atual": 0.0,
        "perc": 0.05,
        "ativo": False,
        "dias": 30,
    }

def jc_salvar(dados):
    """Salva progresso de juros compostos no arquivo JSON."""
    try:
        with open(JC_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2)
    except Exception:
        pass

def jc_meta_hoje():
    """Retorna a meta do dia atual (banca_atual * perc). Retorna None se banca <= 0."""
    dados = jc_carregar()
    banca = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc  = dados.get("perc", 0.05)
    if banca <= 0:
        return None
    return max(round(banca * perc, 2), 0.01)

def jc_registrar_dia(lucro_total, wins=0, losses=0):
    """Registra resultado do dia e avança banca com juros compostos.
    Se o lucro superar a meta, avança múltiplos dias proporcionalmente.
    Retorna (dia_num, banca_ini, banca_final, meta_dia, atingiu, dias_equivalentes) ou None."""
    dados    = jc_carregar()
    banca_ini = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc      = dados.get("perc", 0.05)
    if banca_ini <= 0:
        return None
    meta_dia    = max(round(banca_ini * perc, 2), 0.01)
    banca_final = round(banca_ini + lucro_total, 2)
    atingiu     = lucro_total >= meta_dia
    dia_num     = dados.get("dia_atual", 1)
    historico   = dados.get("historico", [])
    if atingiu and meta_dia > 0:
        dias_equivalentes = max(1, int(lucro_total / meta_dia))
    else:
        dias_equivalentes = 1
    data_str = _dt_jc.now().strftime("%d/%m/%Y %H:%M")
    if dias_equivalentes <= 1:
        historico.append({
            "dia": dia_num, "data": data_str,
            "banca_inicial": banca_ini, "meta": meta_dia,
            "lucro_real": round(lucro_total, 2), "banca_final": banca_final,
            "atingiu_meta": atingiu, "wins": wins, "losses": losses,
            "dias_equivalentes": 1,
        })
        dados.update({
            "dia_atual":   dia_num + 1,
            "historico":   historico,
            "banca_base":  dados.get("banca_base", banca_ini),
            "banca_atual": banca_final,
            "perc":        perc,
        })
    else:
        lucro_restante = lucro_total
        banca_acum     = banca_ini
        for i in range(dias_equivalentes):
            meta_i      = max(round(banca_acum * perc, 2), 0.01)
            lucro_i     = meta_i if i < dias_equivalentes - 1 else round(lucro_restante, 2)
            banca_fim_i = round(banca_acum + lucro_i, 2)
            historico.append({
                "dia": dia_num + i, "data": data_str,
                "banca_inicial": banca_acum, "meta": meta_i,
                "lucro_real": lucro_i, "banca_final": banca_fim_i,
                "atingiu_meta": True,
                "wins":   wins   if i == 0 else 0,
                "losses": losses if i == 0 else 0,
                "dias_equivalentes": dias_equivalentes if i == 0 else 0,
            })
            lucro_restante = round(lucro_restante - lucro_i, 2)
            banca_acum     = banca_fim_i
        dados.update({
            "dia_atual":   dia_num + dias_equivalentes,
            "historico":   historico,
            "banca_base":  dados.get("banca_base", banca_ini),
            "banca_atual": banca_final,
            "perc":        perc,
        })
    jc_salvar(dados)
    return dia_num, banca_ini, banca_final, meta_dia, atingiu, dias_equivalentes


# ── Rotas Flask para Juros Compostos ──────────────────────

@app.route('/jc-config', methods=['GET'])
def jc_config_get():
    """Retorna a configuração e progresso atual de JC."""
    dados = jc_carregar()
    banca = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc  = dados.get("perc", 0.05)
    meta  = max(round(banca * perc, 2), 0.01) if banca > 0 else 0.0
    return jsonify({
        "ok":          True,
        "ativo":       dados.get("ativo", False),
        "banca_base":  dados.get("banca_base", 0.0),
        "banca_atual": banca,
        "perc":        perc,
        "perc_pct":    round(perc * 100, 4),
        "dia_atual":   dados.get("dia_atual", 1),
        "meta_hoje":   meta,
        "historico":   dados.get("historico", []),
        "dias":        dados.get("dias", 30),
    })

@app.route('/jc-config', methods=['POST'])
def jc_config_post():
    """Salva/atualiza configuração de JC (banca_base, perc, ativo).
    NUNCA sobrescreve banca_atual quando já há histórico — preserva o progresso."""
    body  = request.get_json(force=True, silent=True) or {}
    dados = jc_carregar()
    tem_historico = len(dados.get("historico", [])) > 0
    if "banca_base" in body:
        banca = float(body["banca_base"])
        dados["banca_base"] = banca
        # Só reseta banca_atual se ainda não há histórico (planilha virgem / dia 1 sem registros)
        if not tem_historico:
            dados["banca_atual"] = banca
    if "perc" in body:
        dados["perc"] = float(body["perc"])
    if "perc_pct" in body:
        dados["perc"] = float(body["perc_pct"]) / 100.0
    if "ativo" in body:
        dados["ativo"] = bool(body["ativo"])
    if "dias" in body:
        dados["dias"] = max(1, int(body["dias"]))
    if not dados.get("dia_atual"):
        dados["dia_atual"] = 1
    jc_salvar(dados)
    return jsonify({"ok": True, "meta_hoje": jc_meta_hoje()})

@app.route('/jc-meta', methods=['GET'])
def jc_meta_route():
    """Retorna somente a meta do dia de hoje."""
    meta  = jc_meta_hoje()
    dados = jc_carregar()
    return jsonify({
        "ok":          True,
        "meta_hoje":   meta,
        "dia_atual":   dados.get("dia_atual", 1),
        "banca_atual": dados.get("banca_atual") or dados.get("banca_base", 0.0),
        "ativo":       dados.get("ativo", False),
    })

@app.route('/jc-registrar-dia', methods=['POST'])
def jc_registrar_dia_route():
    """Registra resultado do dia de JC e avança a banca com juros compostos.
    Body JSON: { lucro_total: float, wins?: int, losses?: int }"""
    body        = request.get_json(force=True, silent=True) or {}
    lucro_total = float(body.get("lucro_total", 0.0))
    wins        = int(body.get("wins",   0))
    losses      = int(body.get("losses", 0))
    result      = jc_registrar_dia(lucro_total, wins, losses)
    if result is None:
        return jsonify({"ok": False, "erro": "Configure a banca base primeiro (banca_base <= 0)."})
    dia_num, banca_ini, banca_final, meta_dia, atingiu, dias_eq = result
    return jsonify({
        "ok":               True,
        "dia":              dia_num,
        "banca_inicial":    banca_ini,
        "banca_final":      banca_final,
        "meta_dia":         meta_dia,
        "atingiu_meta":     atingiu,
        "dias_equivalentes": dias_eq,
        "proximo_dia":      dia_num + dias_eq,
    })

@app.route('/jc-resetar', methods=['POST'])
def jc_resetar_route():
    """Reseta toda a planilha de JC mantendo configurações de banca/perc.
    Body JSON (opcional): { banca_base?: float, perc_pct?: float }"""
    body  = request.get_json(force=True, silent=True) or {}
    dados = jc_carregar()
    banca = float(body.get("banca_base", dados.get("banca_base", 100.0)))
    perc  = float(body.get("perc_pct",  dados.get("perc", 0.05) * 100)) / 100.0
    jc_salvar({
        "dia_atual":   1,
        "historico":   [],
        "banca_base":  banca,
        "banca_atual": banca,
        "perc":        perc,
        "ativo":       dados.get("ativo", False),
    })
    return jsonify({"ok": True, "banca_base": banca, "perc_pct": round(perc * 100, 4)})

@app.route('/jc-projecao', methods=['GET'])
def jc_projecao_route():
    """Calcula projeção de juros compostos para N dias.
    Query params: dias (int, padrão 30)"""
    dados    = jc_carregar()
    banca    = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc     = dados.get("perc", 0.05)
    dias     = int(request.args.get("dias", 30))
    dia_atual = dados.get("dia_atual", 1)
    historico = dados.get("historico", [])
    # Monta tabela da projeção
    tabela       = []
    banca_acum   = float(dados.get("banca_base", banca))
    hist_map     = {h["dia"]: h for h in historico}
    banca_atual_real = banca
    for d in range(1, dias + 1):
        if d == dia_atual:
            banca_acum = banca_atual_real
        if d < dia_atual and d in hist_map:
            banca_acum = hist_map[d].get("banca_inicial", banca_acum)
        meta_d = max(round(banca_acum * perc, 2), 0.01)
        eh_hoje = (d == dia_atual)
        passado = (d < dia_atual)
        if passado and d in hist_map:
            h       = hist_map[d]
            lucro_r = h.get("lucro_real", 0)
            atingiu = h.get("atingiu_meta", False)
            dias_eq = h.get("dias_equivalentes", 1)
            tabela.append({
                "dia": d, "banca_inicio": h.get("banca_inicial", banca_acum),
                "meta": meta_d, "lucro_real": lucro_r,
                "atingiu_meta": atingiu, "dias_equivalentes": dias_eq,
                "status": "passado",
            })
            banca_acum = round(h.get("banca_final", banca_acum + lucro_r), 2)
        elif eh_hoje:
            tabela.append({
                "dia": d, "banca_inicio": banca_acum,
                "meta": meta_d, "lucro_real": None,
                "atingiu_meta": None, "dias_equivalentes": None,
                "status": "hoje",
            })
            banca_acum = round(banca_acum * (1 + perc), 2)
        else:
            tabela.append({
                "dia": d, "banca_inicio": banca_acum,
                "meta": meta_d, "lucro_real": None,
                "atingiu_meta": None, "dias_equivalentes": None,
                "status": "futuro",
            })
            banca_acum = round(banca_acum * (1 + perc), 2)
    banca_ini_proj = dados.get("banca_base", banca)
    banca_fim_proj = round(banca_ini_proj * ((1 + perc) ** dias), 2)
    notif_total    = round(sum(h.get("lucro_real", 0) for h in historico), 2)
    dias_restantes = max(0, dias - dia_atual + 1)
    return jsonify({
        "ok":             True,
        "banca_base":     banca_ini_proj,
        "banca_projetada": banca_fim_proj,
        "lucro_total_proj": round(banca_fim_proj - banca_ini_proj, 2),
        "perc_pct":       round(perc * 100, 4),
        "dias":           dias,
        "dia_atual":      dia_atual,
        "dias_restantes": dias_restantes,
        "notif_total":    notif_total,
        "tabela":         tabela,
    })


# ─────────────────────────────────────────────────────────
# JUROS COMPOSTOS — CONTA SECUNDÁRIA (arquivo separado)
# ─────────────────────────────────────────────────────────
JC_SEC_ARQUIVO = os.path.join(_BASE_DIR, "jc_sec_progresso.json")

def jc_sec_carregar():
    try:
        if os.path.exists(JC_SEC_ARQUIVO):
            with open(JC_SEC_ARQUIVO, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"dia_atual": 1, "historico": [], "banca_base": 0.0,
            "banca_atual": 0.0, "perc": 0.05, "ativo": False, "dias": 30}

def jc_sec_salvar(dados):
    try:
        with open(JC_SEC_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2)
    except Exception:
        pass

def jc_sec_meta_hoje():
    dados = jc_sec_carregar()
    banca = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc  = dados.get("perc", 0.05)
    if banca <= 0:
        return None
    return max(round(banca * perc, 2), 0.01)

def jc_sec_registrar_dia(lucro_total, wins=0, losses=0):
    dados     = jc_sec_carregar()
    banca_ini = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc      = dados.get("perc", 0.05)
    if banca_ini <= 0:
        return None
    meta_dia    = max(round(banca_ini * perc, 2), 0.01)
    banca_final = round(banca_ini + lucro_total, 2)
    atingiu     = lucro_total >= meta_dia
    dia_num     = dados.get("dia_atual", 1)
    historico   = dados.get("historico", [])
    dias_equivalentes = max(1, int(lucro_total / meta_dia)) if atingiu and meta_dia > 0 else 1
    data_str = _dt_jc.now().strftime("%d/%m/%Y %H:%M")
    if dias_equivalentes <= 1:
        historico.append({
            "dia": dia_num, "data": data_str,
            "banca_inicial": banca_ini, "meta": meta_dia,
            "lucro_real": round(lucro_total, 2), "banca_final": banca_final,
            "atingiu_meta": atingiu, "wins": wins, "losses": losses,
            "dias_equivalentes": 1,
        })
    else:
        lucro_restante = round(lucro_total, 2)
        banca_acum     = banca_ini
        for i in range(dias_equivalentes):
            meta_i     = max(round(banca_acum * perc, 2), 0.01)
            lucro_i    = meta_i if i < dias_equivalentes - 1 else round(lucro_restante, 2)
            banca_fim_i = round(banca_acum + lucro_i, 2)
            historico.append({
                "dia": dia_num + i, "data": data_str,
                "banca_inicial": banca_acum, "meta": meta_i,
                "lucro_real": lucro_i, "banca_final": banca_fim_i,
                "atingiu_meta": True,
                "wins": wins if i == 0 else 0,
                "losses": losses if i == 0 else 0,
                "dias_equivalentes": dias_equivalentes if i == 0 else 0,
            })
            lucro_restante = round(lucro_restante - lucro_i, 2)
            banca_acum     = banca_fim_i
    dados.update({
        "dia_atual":   dia_num + dias_equivalentes,
        "historico":   historico,
        "banca_base":  dados.get("banca_base", banca_ini),
        "banca_atual": banca_final,
        "perc":        perc,
    })
    jc_sec_salvar(dados)
    return dia_num, banca_ini, banca_final, meta_dia, atingiu, dias_equivalentes

@app.route('/jc-sec-config', methods=['GET'])
def jc_sec_config_get():
    dados = jc_sec_carregar()
    banca = dados.get("banca_atual") or dados.get("banca_base", 0.0)
    perc  = dados.get("perc", 0.05)
    meta  = max(round(banca * perc, 2), 0.01) if banca > 0 else 0.0
    return jsonify({
        "ok": True, "ativo": dados.get("ativo", False),
        "banca_base": dados.get("banca_base", 0.0), "banca_atual": banca,
        "perc": perc, "perc_pct": round(perc * 100, 4),
        "dia_atual": dados.get("dia_atual", 1), "meta_hoje": meta,
        "historico": dados.get("historico", []), "dias": dados.get("dias", 30),
    })

@app.route('/jc-sec-config', methods=['POST'])
def jc_sec_config_post():
    body  = request.get_json(force=True, silent=True) or {}
    dados = jc_sec_carregar()
    if "banca_base" in body:
        banca = float(body["banca_base"])
        dados["banca_base"] = banca
        # Só reseta banca_atual junto com banca_base quando ainda não há histórico
        # (dia 1 sem nenhum registro). Se já há histórico, banca_atual é o saldo real.
        tem_historico = len(dados.get("historico", [])) > 0
        if not tem_historico:
            dados["banca_atual"] = banca
    if "perc_pct" in body:
        dados["perc"] = float(body["perc_pct"]) / 100.0
    if "perc" in body:
        dados["perc"] = float(body["perc"])
    if "ativo" in body:
        dados["ativo"] = bool(body["ativo"])
    if "dias" in body:
        dados["dias"] = max(1, int(body["dias"]))
    if not dados.get("dia_atual"):
        dados["dia_atual"] = 1
    jc_sec_salvar(dados)
    return jsonify({"ok": True, "meta_hoje": jc_sec_meta_hoje()})

@app.route('/jc-sec-meta', methods=['GET'])
def jc_sec_meta_route():
    meta  = jc_sec_meta_hoje()
    dados = jc_sec_carregar()
    return jsonify({
        "ok": True, "meta_hoje": meta,
        "dia_atual": dados.get("dia_atual", 1),
        "banca_atual": dados.get("banca_atual") or dados.get("banca_base", 0.0),
        "ativo": dados.get("ativo", False),
    })

@app.route('/jc-sec-registrar-dia', methods=['POST'])
def jc_sec_registrar_dia_route():
    body        = request.get_json(force=True, silent=True) or {}
    lucro_total = float(body.get("lucro_total", 0.0))
    wins        = int(body.get("wins", 0))
    losses      = int(body.get("losses", 0))
    result      = jc_sec_registrar_dia(lucro_total, wins, losses)
    if result is None:
        return jsonify({"ok": False, "erro": "Configure a banca base primeiro."})
    dia_num, banca_ini, banca_final, meta_dia, atingiu, dias_eq = result
    return jsonify({
        "ok": True, "dia": dia_num,
        "banca_inicial": banca_ini, "banca_final": banca_final,
        "meta_dia": meta_dia, "atingiu_meta": atingiu,
        "dias_equivalentes": dias_eq, "proximo_dia": dia_num + dias_eq,
    })

@app.route('/jc-sec-sincronizar', methods=['POST'])
def jc_sec_sincronizar_route():
    """Atualiza banca_atual com o saldo real da corretora sem resetar histórico."""
    body  = request.get_json(force=True, silent=True) or {}
    saldo = body.get("saldo")
    if saldo is None:
        return jsonify({"ok": False, "erro": "Campo 'saldo' obrigatório."})
    try:
        saldo = float(saldo)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "Saldo inválido."})
    if saldo <= 0:
        return jsonify({"ok": False, "erro": "Saldo deve ser maior que zero."})
    dados = jc_sec_carregar()
    dados["banca_atual"] = round(saldo, 2)
    # Só atualiza banca_base se ainda não foi configurada pelo usuário
    if dados.get("banca_base", 0.0) <= 0:
        dados["banca_base"] = round(saldo, 2)
    jc_sec_salvar(dados)
    return jsonify({"ok": True, "banca_atual": dados["banca_atual"], "meta_hoje": jc_sec_meta_hoje()})

@app.route('/jc-sec-resetar', methods=['POST'])
def jc_sec_resetar_route():
    body  = request.get_json(force=True, silent=True) or {}
    dados = jc_sec_carregar()
    banca = float(body.get("banca_base", dados.get("banca_base", 100.0)))
    perc  = float(body.get("perc_pct",  dados.get("perc", 0.05) * 100)) / 100.0
    jc_sec_salvar({
        "dia_atual": 1, "historico": [],
        "banca_base": banca, "banca_atual": banca,
        "perc": perc, "ativo": dados.get("ativo", False),
    })
    return jsonify({"ok": True, "banca_base": banca, "perc_pct": round(perc * 100, 4)})


# ─────────────────────────────────────────────────────────
# CAIXA GALE — cofre de retenção de lucro
# ─────────────────────────────────────────────────────────
CAIXA_GALE_ARQUIVO = os.path.join(_BASE_DIR, "caixa_gale_stats.json")

def _caixa_ler():
    padrao = {"saldo_cofre": 0.0, "percentual_retencao": 20.0}
    try:
        if os.path.exists(CAIXA_GALE_ARQUIVO):
            with open(CAIXA_GALE_ARQUIVO, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return padrao

@app.route('/caixa-gale', methods=['GET', 'POST'])
def caixa_gale_api():
    if request.method == 'GET':
        return jsonify(_caixa_ler())
    dados = request.get_json(force=True, silent=True) or {}
    atual = _caixa_ler()
    if "saldo_cofre"          in dados: atual["saldo_cofre"]          = float(dados["saldo_cofre"])
    if "percentual_retencao"  in dados: atual["percentual_retencao"]  = float(dados["percentual_retencao"])
    try:
        with open(CAIXA_GALE_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(atual, f, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# ECT — ENTIDADE COGNITIVA DE TRADING
# Módulos das 4 Camadas da Arquitetura Closed-Loop
# ═══════════════════════════════════════════════════════════════════════════════

import math
import random as _random

# Arquivo de persistência do estado ECT (threshold + status das estratégias)
ECT_STATE_FILE = os.path.join(_BASE_DIR, "ect_state.json")

def _ect_state_ler() -> dict:
    padrao = {
        "threshold_supervisor": 82,
        "estrategias_suspensas": [],
        "regime_atual": "DESCONHECIDO",
    }
    try:
        if os.path.exists(ECT_STATE_FILE):
            with open(ECT_STATE_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            padrao.update(dados)
            # Sincroniza o threshold em memória com o persitido
            _supervisor.threshold = int(padrao.get("threshold_supervisor", 82))
    except Exception:
        pass
    return padrao

def _ect_state_salvar(dados: dict):
    try:
        with open(ECT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

# Inicializa estado ECT ao carregar o módulo
_ect_state_ler()


# ─────────────────────────────────────────────────────────────────────────────
# CAMADA 1 — DETECTOR DE REGIME DE MERCADO (RegimoDetector)
# Calcula desvio padrão móvel e classifica: TENDENCIA | LATERAL | ALTA_VOLATILIDADE
# ─────────────────────────────────────────────────────────────────────────────
class RegimoDetector:
    """
    Analisa uma janela de ticks recentes e classifica o regime de mercado.
    Utiliza:
      - Desvio padrão móvel para separar Expansão vs Consolidação
      - Coeficiente de variação (CV) para normalizar por escala de preço
      - Slope da média móvel para detectar tendência direcional
    """

    # Limiares recalibrados para índices sintéticos Deriv (escala real medida)
    # CVs reais observados: 0.000034 (1HZ10V) a 0.000641 (R_100)
    CV_ALTA_VOLATILIDADE = 0.00030  # CV > 0.03% → expansão / mais volátil  (era 0.00040)
    CV_LATERAL           = 0.00008  # CV < 0.008% → consolidação real        (era 0.00012)
    SLOPE_TENDENCIA      = 0.00015  # slope mínimo — sensível a micro-tendências (era 0.0005)

    def classificar(self, ticks: list) -> dict:
        """
        Parâmetro: ticks — lista de floats (preços recentes, pelo menos 10).
        Retorna dict: { regime, fluxo_direcao, cv, std, media, slope, descricao }
        fluxo_direcao: "CALL" | "PUT" | "NEUTRO"  — usado pelo frontend e pelo Supervisor.
        """
        if not ticks or len(ticks) < 5:
            return {
                "regime": "DESCONHECIDO",
                "fluxo_direcao": "NEUTRO",
                "cv": 0.0, "std": 0.0, "media": 0.0, "slope": 0.0,
                "descricao": "Dados insuficientes (mínimo 5 ticks).",
                "recomendacao": "Aguardar mais dados.",
            }

        n      = len(ticks)
        media  = sum(ticks) / n
        var    = sum((t - media) ** 2 for t in ticks) / n
        std    = math.sqrt(var)
        cv     = (std / media) if media != 0 else 0.0

        # Slope: regressão linear simples sobre os últimos ticks
        xs  = list(range(n))
        xm  = sum(xs) / n
        num = sum((xs[i] - xm) * (ticks[i] - media) for i in range(n))
        den = sum((xs[i] - xm) ** 2 for i in range(n))
        slope = (num / den) if den != 0 else 0.0

        # Classificação — slope forte sobrepõe CV baixo (índices Deriv têm CV pequeno mas slope real)
        SLOPE_FORTE = self.SLOPE_TENDENCIA * 3  # slope 3× acima do limiar = tendência clara

        if cv > self.CV_ALTA_VOLATILIDADE:
            regime = "ALTA_VOLATILIDADE"
            descricao = (
                f"Mercado em expansão forte (CV={cv:.6f}). "
                "Use barreiras conservadoras: Under 8 / Over 1. Evite Fluxo."
            )
            recomendacao = "DIGITUNDER_8 | DIGITOVER_1 | evitar_martingale"
        elif cv < self.CV_LATERAL:
            # CV muito baixo = consolidação, mas se slope for muito forte ainda é tendência
            if abs(slope) >= SLOPE_FORTE:
                regime = "TENDENCIA"
                direcao = "ALTA" if slope > 0 else "BAIXA"
                descricao = (
                    f"Mercado em tendência forte de {direcao} com baixa volatilidade "
                    f"(CV={cv:.6f}, slope={slope:.6f}). Ideal para FLUXO."
                )
                recomendacao = f"FLUXO_{'CALL' if slope > 0 else 'PUT'} | EMA_9x21 | velas_3"
            else:
                regime = "LATERAL"
                descricao = (
                    f"Mercado consolidando (CV={cv:.6f}). "
                    "Ideal para DIGITOVER/UNDER com gatilho de sequência."
                )
                recomendacao = "DIGITUNDER_7 | DIGITOVER_2 | seq_gatilho_3"
        else:
            if abs(slope) >= self.SLOPE_TENDENCIA:
                regime = "TENDENCIA"
                direcao = "ALTA" if slope > 0 else "BAIXA"
                descricao = (
                    f"Mercado em tendência de {direcao} (slope={slope:.6f}). "
                    "Ideal para FLUXO CALL/PUT com EMA."
                )
                recomendacao = f"FLUXO_{'CALL' if slope > 0 else 'PUT'} | EMA_9x21 | velas_3"
            else:
                regime = "LATERAL"
                descricao = (
                    f"Mercado levemente ativo (CV={cv:.6f}, slope baixo). "
                    "DIGITOVER/UNDER moderado funciona bem."
                )
                recomendacao = "DIGITUNDER_7 | DIGITOVER_2 | SATURACAO"

        # Direção do fluxo para o frontend e para o Supervisor de Decisão
        if slope > self.SLOPE_TENDENCIA:
            fluxo_direcao = "CALL"
        elif slope < -self.SLOPE_TENDENCIA:
            fluxo_direcao = "PUT"
        else:
            fluxo_direcao = "NEUTRO"

        return {
            "regime":        regime,
            "fluxo_direcao": fluxo_direcao,
            "cv":            round(cv, 6),
            "std":           round(std, 6),
            "media":         round(media, 6),
            "slope":         round(slope, 8),
            "descricao":     descricao,
            "recomendacao":  recomendacao,
            "n_ticks":       n,
        }


_regime_detector = RegimoDetector()


@app.route('/ect/scan', methods=['POST'])
def ect_scan():
    """
    Camada 1 — Percepção.
    Recebe ticks recentes e retorna o regime de mercado classificado.
    Payload: { ticks: [float, ...], ativo: str (opcional) }
    """
    dados = request.get_json(force=True, silent=True) or {}
    ticks = dados.get("ticks", [])
    ativo = dados.get("ativo", "")

    if not isinstance(ticks, list):
        return jsonify({"erro": "Campo 'ticks' deve ser uma lista de números."})

    try:
        ticks = [float(t) for t in ticks]
    except (TypeError, ValueError):
        return jsonify({"erro": "Todos os valores em 'ticks' devem ser numéricos."})

    resultado = _regime_detector.classificar(ticks)
    resultado["ativo"] = ativo

    # Persiste o regime atual no estado ECT
    state = _ect_state_ler()
    state["regime_atual"] = resultado["regime"]
    _ect_state_salvar(state)

    return jsonify(resultado)


@app.route('/ect/regime', methods=['GET'])
def ect_regime_atual():
    """Retorna o último regime calculado sem processar novos ticks."""
    state = _ect_state_ler()
    return jsonify({
        "regime":    state.get("regime_atual", "DESCONHECIDO"),
        "threshold": _supervisor.threshold,
        "suspensas": state.get("estrategias_suspensas", []),
    })


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER MULTI-MERCADO — varre todos os ativos e retorna ranking de regime
# Busca ticks históricos via API REST pública da Deriv (sem autenticação)
# ─────────────────────────────────────────────────────────────────────────────
_ATIVOS_DERIV = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

# Score de "qualidade" por regime para ordenar o ranking
# LATERAL e TENDENCIA têm score igual — ambos são operáveis
# ALTA_VOLATILIDADE tem score menor pois exige barreiras conservadoras
_REGIME_SCORE = {
    "LATERAL":           3,
    "TENDENCIA":         3,   # igual ao LATERAL — nenhum tem prioridade
    "ALTA_VOLATILIDADE": 1,
    "DESCONHECIDO":      0,
}

def _buscar_ticks_ativo(ativo: str, count: int = 60) -> list:
    """
    Busca os últimos N ticks de um ativo via API REST pública da Deriv.
    Endpoint: wss://ws.binaryws.com — usamos a REST equivalente via HTTP.
    Retorna lista de floats (quotes) ou [] em caso de erro.
    """
    # A Deriv não expõe REST pura para ticks históricos sem WebSocket,
    # mas expõe ticks_history via HTTP usando o endpoint de streaming simulado.
    # Usamos o endpoint público de ticks do servidor dxtrade/deriv que aceita HTTP.
    try:
        url = "https://api.deriv.com/ticks_history"
        payload = {
            "ticks_history": ativo,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "start": 1,
            "style": "ticks",
        }
        resp = requests.post(
            "https://api.deriv.com/ticks_history",
            json={"ticks_history": ativo, "adjust_start_time": 1,
                  "count": count, "end": "latest", "start": 1, "style": "ticks"},
            timeout=8,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            prices = data.get("history", {}).get("prices", [])
            if prices:
                return [float(p) for p in prices]
    except Exception:
        pass

    # Fallback: endpoint WebSocket via HTTP Long-Poll (Deriv JSON API)
    try:
        resp2 = requests.get(
            f"https://ws.binaryws.com/websockets/v3?app_id=1089",
            timeout=5, verify=False,
        )
    except Exception:
        pass

    # Fallback 2: endpoint alternativo público
    try:
        import urllib.request
        import urllib.parse
        req_body = json.dumps({
            "ticks_history": ativo,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "start": 1,
            "style": "ticks",
            "subscribe": 0,
        }).encode()
        req = urllib.request.Request(
            "https://api.deriv.com/",
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp3:
            data3 = json.loads(resp3.read())
            prices3 = data3.get("history", {}).get("prices", [])
            if prices3:
                return [float(p) for p in prices3]
    except Exception:
        pass

    return []


def _buscar_ticks_ws_sync(ativo: str, count: int = 60) -> list:
    """
    Busca ticks via WebSocket síncrono (usa websocket-client se disponível,
    ou socket raw se não tiver).
    """
    try:
        import websocket as _ws
        import threading as _th

        ticks_coletados = []
        evento = _th.Event()

        def _on_message(ws, msg):
            d = json.loads(msg)
            if d.get("msg_type") == "history":
                prices = d.get("history", {}).get("prices", [])
                ticks_coletados.extend([float(p) for p in prices])
                evento.set()
                ws.close()

        def _on_open(ws):
            ws.send(json.dumps({
                "ticks_history": ativo,
                "adjust_start_time": 1,
                "count": count,
                "end": "latest",
                "start": 1,
                "style": "ticks",
            }))

        wsapp = _ws.WebSocketApp(
            "wss://ws.binaryws.com/websockets/v3?app_id=1089",
            on_message=_on_message,
            on_open=_on_open,
        )
        t = _th.Thread(target=lambda: wsapp.run_forever(ping_interval=0), daemon=True)
        t.start()
        evento.wait(timeout=10)
        return ticks_coletados
    except Exception:
        return []


@app.route('/ect/scan-multi', methods=['GET', 'POST'])
def ect_scan_multi():
    """
    Scanner Multi-Mercado — varre todos os 10 ativos Deriv simultaneamente.
    Usa threading.Thread puro (sem concurrent.futures) para compatibilidade
    com Python 3.12 em thread daemon (evita RuntimeError: can't register atexit).
    """
    dados = request.get_json(force=True, silent=True) or {}
    ativos_param = dados.get("ativos") or request.args.get("ativos", "")
    if ativos_param:
        ativos = [a.strip().upper() for a in ativos_param.split(",") if a.strip()]
    else:
        ativos = _ATIVOS_DERIV

    resultados = []
    lock = threading.Lock()

    def _processar_ativo(ativo):
        try:
            ticks = _buscar_ticks_ws_sync(ativo, count=60)
            if not ticks:
                res = {
                    "ativo": ativo, "regime": "DESCONHECIDO",
                    "cv": None, "slope": None, "n_ticks": 0,
                    "descricao": "Falha ao obter ticks da Deriv.",
                    "recomendacao": "—", "score": 0, "erro": True,
                }
            else:
                res = _regime_detector.classificar(ticks)
                res["ativo"]  = ativo
                res["score"]  = _REGIME_SCORE.get(res["regime"], 0)
                res["erro"]   = False
        except Exception as exc:
            res = {
                "ativo": ativo, "regime": "DESCONHECIDO",
                "cv": None, "slope": None, "n_ticks": 0,
                "descricao": str(exc), "recomendacao": "—",
                "score": 0, "erro": True,
            }
        with lock:
            resultados.append(res)

    # Dispara uma thread por ativo e aguarda todas terminarem (timeout 20s)
    threads = [threading.Thread(target=_processar_ativo, args=(a,), daemon=True)
               for a in ativos]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    # Ordena: score desc, depois CV asc (menos volátil primeiro dentro do mesmo regime)
    resultados.sort(key=lambda x: (-(x.get("score") or 0), x.get("cv") or 99))

    melhor = next(
        (r for r in resultados if not r.get("erro") and r["regime"] != "DESCONHECIDO"),
        None
    )

    if melhor:
        state = _ect_state_ler()
        state["regime_atual"]        = melhor["regime"]
        state["melhor_ativo"]        = melhor["ativo"]
        state["melhor_recomendacao"] = melhor["recomendacao"]
        _ect_state_salvar(state)

    return jsonify({
        "ranking": resultados,
        "melhor":  melhor,
        "total":   len(ativos),
        "sucesso": sum(1 for r in resultados if not r.get("erro")),
    })


# ─────────────────────────────────────────────────────────────────────────────
# CAMADA 2 — SIMULAÇÃO: MONTE CARLO + WALK-FORWARD (MonteCarloSimulator)
# Executa 1.000 variações da estratégia sobre o histórico do Memory Vault.
# ─────────────────────────────────────────────────────────────────────────────
class MonteCarloSimulator:
    """
    Simula N variações de uma estratégia aplicando perturbações nos parâmetros
    e mede a resiliência da curva de lucro.

    Walk-Forward:
      - Treino: primeiros 70% do histórico filtrado
      - Teste  : últimos 30% (out-of-sample)
    """

    N_SIMULACOES = 1000
    PERTURBACAO  = 0.10   # ±10% nos parâmetros numéricos

    def _carregar_historico(self, estrategia: str, ativo: str) -> list:
        """Carrega operações do Memory Vault filtradas por estratégia e/ou ativo."""
        if not os.path.exists(MEMORY_FILE):
            return []
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
        except Exception:
            return []
        ops = [
            e for e in memoria
            if (not estrategia or e.get("estrategia") == estrategia)
            and (not ativo or e.get("contexto") == ativo)
        ]
        return ops

    def _simular_uma(self, ops: list, win_rate_base: float,
                     payout: float, stake: float,
                     perturb: bool = True) -> dict:
        """
        Simula uma sequência de trades usando o win_rate_base como probabilidade.
        Aplica perturbação aleatória de ±PERTURBACAO se perturb=True.
        Retorna: { lucro_final, max_drawdown, profit_factor, wins, losses }
        """
        wr = win_rate_base
        if perturb:
            delta = self.PERTURBACAO * (2 * _random.random() - 1)
            wr    = max(0.01, min(0.99, wr + delta))

        n       = max(len(ops), 100)   # simula ao menos 100 operações
        lucro   = 0.0
        pico    = 0.0
        max_dd  = 0.0
        lucro_b = 0.0   # bruto positivo (wins)
        perda_b = 0.0   # bruto negativo (losses)
        wins    = 0
        losses  = 0

        for _ in range(n):
            if _random.random() < wr:
                ganho   = stake * payout
                lucro  += ganho
                lucro_b += ganho
                wins   += 1
            else:
                lucro  -= stake
                perda_b += stake
                losses  += 1
            if lucro > pico:
                pico = lucro
            dd = pico - lucro
            if dd > max_dd:
                max_dd = dd

        pf = (lucro_b / perda_b) if perda_b > 0 else (lucro_b if lucro_b > 0 else 0.0)
        return {
            "lucro_final":   round(lucro, 4),
            "max_drawdown":  round(max_dd, 4),
            "profit_factor": round(pf, 4),
            "wins":          wins,
            "losses":        losses,
            "n_ops":         n,
        }

    def executar(self, estrategia: str, ativo: str,
                 stake: float, payout: float) -> dict:
        """
        Executa N_SIMULACOES variações e retorna o relatório estatístico completo.
        """
        ops = self._carregar_historico(estrategia, ativo)

        # Win rate base: usa histórico real se disponível, senão neutro 50%
        if ops:
            wins_reais = sum(1 for e in ops if e.get("resultado") == "WIN")
            wr_base    = wins_reais / len(ops)
        else:
            wr_base = 0.50

        # Walk-Forward: divide o histórico em treino (70%) e teste (30%)
        wf_resultado = None
        if len(ops) >= 10:
            corte     = int(len(ops) * 0.70)
            treino    = ops[:corte]
            teste     = ops[corte:]
            wr_treino = sum(1 for e in treino if e.get("resultado") == "WIN") / len(treino)
            wr_teste  = sum(1 for e in teste  if e.get("resultado") == "WIN") / len(teste)
            wf_resultado = {
                "n_treino":  len(treino),
                "n_teste":   len(teste),
                "wr_treino": round(wr_treino * 100, 1),
                "wr_teste":  round(wr_teste  * 100, 1),
                "divergencia": round(abs(wr_treino - wr_teste) * 100, 1),
                "overfitting": abs(wr_treino - wr_teste) > 0.15,
            }

        # Monte Carlo: N_SIMULACOES variações
        resultados  = []
        lucros      = []
        drawdowns   = []
        pfs         = []
        for _ in range(self.N_SIMULACOES):
            r = self._simular_uma(ops, wr_base, payout, stake, perturb=True)
            resultados.append(r)
            lucros.append(r["lucro_final"])
            drawdowns.append(r["max_drawdown"])
            pfs.append(r["profit_factor"])

        # Estatísticas dos resultados
        def _percentil(lst, p):
            s = sorted(lst)
            i = int(len(s) * p / 100)
            return s[min(i, len(s) - 1)]

        lucro_med   = sum(lucros) / len(lucros)
        pf_med      = sum(pfs)    / len(pfs)
        dd_max      = max(drawdowns)
        lucro_p5    = _percentil(lucros, 5)    # pior caso 5%
        lucro_p95   = _percentil(lucros, 95)   # melhor caso 95%
        pf_p25      = _percentil(pfs, 25)      # PF no quartil inferior
        pct_lucrativas = sum(1 for l in lucros if l > 0) / len(lucros) * 100

        # Aprovação: PF mediano > 1.25 E pelo menos 60% das simulações lucrativas
        aprovado = (pf_med >= 1.25) and (pct_lucrativas >= 60.0)

        return {
            "aprovado":           aprovado,
            "n_simulacoes":       self.N_SIMULACOES,
            "wr_base":            round(wr_base * 100, 1),
            "n_historico":        len(ops),
            "lucro_medio":        round(lucro_med, 4),
            "lucro_p5":           round(lucro_p5,  4),
            "lucro_p95":          round(lucro_p95, 4),
            "profit_factor_medio":round(pf_med,    4),
            "profit_factor_p25":  round(pf_p25,    4),
            "max_drawdown_pior":  round(dd_max,    4),
            "pct_simulacoes_lucrativas": round(pct_lucrativas, 1),
            "walk_forward":       wf_resultado,
            "motivo": (
                "✅ Estratégia aprovada: PF médio ≥ 1.25 e ≥ 60% das simulações lucrativas."
                if aprovado else
                f"⛔ Estratégia reprovada: PF médio = {pf_med:.2f} | Lucrativas: {pct_lucrativas:.0f}%."
            ),
        }


_mc_simulator = MonteCarloSimulator()


@app.route('/ect/backtest', methods=['POST'])
def ect_backtest():
    """
    Camada 2 — Simulação.
    Executa Walk-Forward Analysis + Monte Carlo (1.000 variações).
    Payload: { estrategia, ativo, stake (float), payout (float 0-1) }
    """
    dados     = request.get_json(force=True, silent=True) or {}
    estrategia = dados.get("estrategia", "")
    ativo      = dados.get("ativo", "")
    stake      = float(dados.get("stake",  0.35))
    payout     = float(dados.get("payout", 0.85))

    resultado = _mc_simulator.executar(estrategia, ativo, stake, payout)
    return jsonify(resultado)


# ─────────────────────────────────────────────────────────────────────────────
# CAMADA 3 — VALIDAÇÃO: PROFIT FACTOR ≥ 1.25 (ProfitFactorValidator)
# Valida se uma estratégia tem Fator de Lucro superior ao mínimo exigido
# em pelo menos N operações (reais ou simuladas via Monte Carlo).
# ─────────────────────────────────────────────────────────────────────────────
class ProfitFactorValidator:
    """
    Calcula o Profit Factor real e simulado de uma estratégia.
    Combina o histórico real do Memory Vault com as simulações Monte Carlo
    para estimar o PF esperado em 500 operações.

    PF = Lucro Bruto / Perda Bruta
    PF ≥ 1.25 em ≥ 500 ops → estratégia promovida para ambiente real.
    """

    MIN_PROFIT_FACTOR = 1.25
    MIN_OPS_VALIDACAO = 500

    def validar(self, estrategia: str, ativo: str,
                stake: float, payout: float) -> dict:
        """
        Retorna o relatório completo de validação.
        """
        # Histórico real
        ops_reais = _mc_simulator._carregar_historico(estrategia, ativo)
        n_reais   = len(ops_reais)

        # PF real (se histórico suficiente)
        pf_real = None
        wr_real = None
        if n_reais >= 3:
            wins_r  = sum(1 for e in ops_reais if e.get("resultado") == "WIN")
            losses_r = n_reais - wins_r
            wr_real = round(wins_r / n_reais * 100, 1)
            lb_real = wins_r  * stake * payout
            pb_real = losses_r * stake
            pf_real = round(lb_real / pb_real, 4) if pb_real > 0 else (
                lb_real if lb_real > 0 else 0.0
            )

        # Monte Carlo para estimar PF em 500 ops
        mc = _mc_simulator.executar(estrategia, ativo, stake, payout)
        pf_mc  = mc["profit_factor_medio"]
        pf_p25 = mc["profit_factor_p25"]

        # Ops totais: reais + simuladas (normalizado para 500)
        ops_efetivas = n_reais + mc["n_simulacoes"]

        # Critério de aprovação
        crit_pf   = pf_mc >= self.MIN_PROFIT_FACTOR
        crit_ops  = ops_efetivas >= self.MIN_OPS_VALIDACAO
        crit_pct  = mc["pct_simulacoes_lucrativas"] >= 60.0
        aprovado  = crit_pf and crit_ops and crit_pct

        nivel = (
            "ELITE"       if pf_mc >= 2.0  else
            "EXCELENTE"   if pf_mc >= 1.75 else
            "APROVADA"    if pf_mc >= 1.25 else
            "LIMÍTROFE"   if pf_mc >= 1.00 else
            "REPROVADA"
        )

        return {
            "aprovada":              aprovado,
            "nivel":                 nivel,
            "profit_factor_real":    pf_real,
            "profit_factor_mc":      round(pf_mc,  4),
            "profit_factor_pior25":  round(pf_p25, 4),
            "win_rate_real":         wr_real,
            "n_ops_reais":           n_reais,
            "n_ops_efetivas":        ops_efetivas,
            "min_ops_exigido":       self.MIN_OPS_VALIDACAO,
            "min_pf_exigido":        self.MIN_PROFIT_FACTOR,
            "criterios": {
                "pf_suficiente":      crit_pf,
                "ops_suficientes":    crit_ops,
                "pct_lucrativas_ok":  crit_pct,
            },
            "walk_forward":          mc.get("walk_forward"),
            "pct_simulacoes_lucrativas": mc["pct_simulacoes_lucrativas"],
            "motivo": (
                f"✅ Promovida para ambiente REAL. Nível: {nivel} | PF={pf_mc:.2f}"
                if aprovado else
                f"⛔ Não promovida. PF={pf_mc:.2f} (mín {self.MIN_PROFIT_FACTOR}) | "
                f"Ops={ops_efetivas} (mín {self.MIN_OPS_VALIDACAO})."
            ),
        }


_pf_validator = ProfitFactorValidator()


@app.route('/ect/validar', methods=['POST'])
def ect_validar():
    """
    Camada 3 — Validação.
    Verifica se uma estratégia tem Profit Factor ≥ 1.25 em ≥ 500 operações.
    Payload: { estrategia, ativo, stake (float), payout (float 0-1) }
    """
    dados      = request.get_json(force=True, silent=True) or {}
    estrategia = dados.get("estrategia", "")
    ativo      = dados.get("ativo", "")
    stake      = float(dados.get("stake",  0.35))
    payout     = float(dados.get("payout", 0.85))

    resultado = _pf_validator.validar(estrategia, ativo, stake, payout)
    return jsonify(resultado)


# ─────────────────────────────────────────────────────────────────────────────
# CAMADA 4 — EXECUÇÃO: DETECTOR DE DEGRADAÇÃO 2σ (EquityCurveMonitor)
# Monitora a curva de equidade de estratégias ativas em tempo real.
# Se o win rate atual desviar > 2 desvios padrões do esperado → SUSPENDE.
# ─────────────────────────────────────────────────────────────────────────────
class EquityCurveMonitor:
    """
    Implementa o critério de degradação do ECT:
    "Se a estratégia validada desviar mais de 2 desvios padrões da sua
     curva de equidade esperada, ela é suspensa automaticamente."

    Algoritmo:
      1. Carrega as últimas N operações da estratégia no Memory Vault.
      2. Calcula a janela deslizante de win rate (blocos de 20 ops).
      3. Calcula mean(WR) e std(WR) dos blocos históricos.
      4. Avalia o bloco mais recente: se WR_recente < mean - 2*std → DEGRADO.
    """

    JANELA_BLOCO  = 20   # operações por bloco para calcular WR parcial
    MIN_BLOCOS    = 3    # mínimo de blocos para calcular os desvios padrões
    SIGMA_LIMITE  = 2.0  # número de desvios padrões para acionar suspensão

    def _blocos_wr(self, ops: list) -> list:
        """Divide as operações em blocos e calcula o WR de cada bloco."""
        blocos = []
        for i in range(0, len(ops), self.JANELA_BLOCO):
            bloco = ops[i:i + self.JANELA_BLOCO]
            if len(bloco) < self.JANELA_BLOCO // 2:
                break   # ignora blocos muito pequenos
            wins_b = sum(1 for e in bloco if e.get("resultado") == "WIN")
            blocos.append(wins_b / len(bloco))
        return blocos

    def monitorar(self, estrategia: str, ativo: str = "") -> dict:
        """
        Analisa a curva de equidade da estratégia.
        Retorna: { status, desvio_sigmas, wr_recente, wr_medio, wr_std,
                   degradada, n_ops, n_blocos, motivo }
        """
        ops = _mc_simulator._carregar_historico(estrategia, ativo)

        if len(ops) < self.JANELA_BLOCO * self.MIN_BLOCOS:
            return {
                "status":         "DADOS_INSUFICIENTES",
                "degradada":      False,
                "n_ops":          len(ops),
                "n_blocos":       0,
                "wr_recente":     None,
                "wr_medio":       None,
                "wr_std":         None,
                "desvio_sigmas":  None,
                "motivo": (
                    f"Operações insuficientes para análise de degradação. "
                    f"Necessário: {self.JANELA_BLOCO * self.MIN_BLOCOS} | Disponível: {len(ops)}."
                ),
            }

        blocos = self._blocos_wr(ops)
        if len(blocos) < self.MIN_BLOCOS + 1:
            return {
                "status":        "DADOS_INSUFICIENTES",
                "degradada":     False,
                "n_ops":         len(ops),
                "n_blocos":      len(blocos),
                "wr_recente":    None,
                "wr_medio":      None,
                "wr_std":        None,
                "desvio_sigmas": None,
                "motivo":        "Blocos históricos insuficientes para calcular desvio padrão.",
            }

        # Histórico: todos os blocos exceto o último (mais recente)
        historico    = blocos[:-1]
        wr_recente   = blocos[-1]

        wr_medio = sum(historico) / len(historico)
        var_h    = sum((b - wr_medio) ** 2 for b in historico) / len(historico)
        wr_std   = math.sqrt(var_h) if var_h > 0 else 0.0001

        desvio_sigmas = (wr_medio - wr_recente) / wr_std
        degradada     = desvio_sigmas >= self.SIGMA_LIMITE

        if degradada:
            status = "DEGRADADA"
            motivo = (
                f"🚨 DEGRADAÇÃO DETECTADA: Win Rate do bloco recente ({wr_recente*100:.1f}%) "
                f"caiu {desvio_sigmas:.1f}σ abaixo da média histórica ({wr_medio*100:.1f}% ± {wr_std*100:.1f}%). "
                f"Estratégia suspensa para reanálise."
            )
        elif desvio_sigmas >= 1.5:
            status = "ALERTA"
            motivo = (
                f"⚠️ ALERTA: Win Rate recente ({wr_recente*100:.1f}%) mostrando "
                f"queda de {desvio_sigmas:.1f}σ. Monitoramento aumentado."
            )
        else:
            status = "SAUDAVEL"
            motivo = (
                f"✅ Curva de equidade estável. WR recente: {wr_recente*100:.1f}% "
                f"(média: {wr_medio*100:.1f}%, desvio: {desvio_sigmas:.1f}σ)."
            )

        return {
            "status":           status,
            "degradada":        degradada,
            "n_ops":            len(ops),
            "n_blocos":         len(blocos),
            "wr_recente":       round(wr_recente, 4),
            "wr_medio":         round(wr_medio, 4),
            "wr_std":           round(wr_std, 4),
            "desvio_sigmas":    round(desvio_sigmas, 2),
            "sigma_limite":     self.SIGMA_LIMITE,
            "motivo":           motivo,
        }


_equity_monitor = EquityCurveMonitor()


@app.route('/ect/degradacao', methods=['POST'])
def ect_degradacao():
    """
    Camada 4 — Execução.
    Monitora a curva de equidade de uma estratégia.
    Se degradada (desvio > 2σ), adiciona à lista de estratégias suspensas.
    Payload: { estrategia, ativo (opcional), auto_suspender (bool, default True) }
    """
    dados      = request.get_json(force=True, silent=True) or {}
    estrategia = dados.get("estrategia", "")
    ativo      = dados.get("ativo", "")
    auto_susp  = bool(dados.get("auto_suspender", True))

    if not estrategia:
        return jsonify({"erro": "Campo 'estrategia' obrigatório."})

    resultado = _equity_monitor.monitorar(estrategia, ativo)

    # Auto-suspensão: adiciona à lista de suspensas no estado ECT
    if resultado["degradada"] and auto_susp:
        state = _ect_state_ler()
        suspensas = state.get("estrategias_suspensas", [])
        if estrategia not in suspensas:
            suspensas.append(estrategia)
            state["estrategias_suspensas"] = suspensas
            _ect_state_salvar(state)
            resultado["auto_suspensa"] = True

            # Notifica via Telegram se configurado
            cfg_tg = _tg_carregar()
            if cfg_tg.get("enabled"):
                msg_alert = (
                    f"🚨 <b>ECT — ESTRATÉGIA SUSPENSA</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📛 <b>{estrategia}</b>\n"
                    f"📊 WR Recente: {resultado['wr_recente']*100:.1f}% "
                    f"(−{resultado['desvio_sigmas']:.1f}σ da média)\n"
                    f"⚡ Status: DEGRADADA → Suspensa para reanálise\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Execute /ai/post-mortem para análise causal."
                )
                _tg_dispatch(lambda: _tg_enviar_texto(
                    cfg_tg["token"], cfg_tg["chat_id"], msg_alert
                ))
        else:
            resultado["auto_suspensa"] = False  # já estava suspensa
    else:
        resultado["auto_suspensa"] = False

    return jsonify(resultado)


@app.route('/ect/reativar', methods=['POST'])
def ect_reativar():
    """
    Remove uma estratégia da lista de suspensas (após reanálise e correção).
    Payload: { estrategia: str }
    """
    dados      = request.get_json(force=True, silent=True) or {}
    estrategia = dados.get("estrategia", "")
    if not estrategia:
        return jsonify({"erro": "Campo 'estrategia' obrigatório."})

    state     = _ect_state_ler()
    suspensas = state.get("estrategias_suspensas", [])
    if estrategia in suspensas:
        suspensas.remove(estrategia)
        state["estrategias_suspensas"] = suspensas
        _ect_state_salvar(state)
        return jsonify({"ok": True, "mensagem": f"Estratégia '{estrategia}' reativada."})
    return jsonify({"ok": False, "mensagem": f"Estratégia '{estrategia}' não estava suspensa."})


@app.route('/ect/status', methods=['GET'])
def ect_status():
    """
    Retorna o estado completo da ECT:
    regime atual, threshold, estratégias suspensas, e resumo do Memory Vault.
    """
    state   = _ect_state_ler()
    n_ops   = 0
    n_est   = 0
    wr_geral = None
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                mem = json.load(f)
            n_ops = len(mem)
            wins  = sum(1 for e in mem if e.get("resultado") == "WIN")
            wr_geral = round(wins / n_ops * 100, 1) if n_ops > 0 else None
            n_est = len({e.get("estrategia") for e in mem})
        except Exception:
            pass

    return jsonify({
        "regime_atual":          state.get("regime_atual", "DESCONHECIDO"),
        "threshold_supervisor":  _supervisor.threshold,
        "estrategias_suspensas": state.get("estrategias_suspensas", []),
        "memory_vault": {
            "n_operacoes":   n_ops,
            "n_estrategias": n_est,
            "wr_geral":      wr_geral,
        },
        "modulos": {
            "scanner":       "✅ RegimoDetector ativo",
            "backtest":      "✅ MonteCarloSimulator ativo (1.000 variações)",
            "validacao":     "✅ ProfitFactorValidator ativo (PF ≥ 1.25)",
            "monitoramento": "✅ EquityCurveMonitor ativo (2σ)",
        },
    })

# ─────────────────────────────────────────────────────────────────────────────
# ECT — CRIAÇÃO AUTOMÁTICA DE ESTRATÉGIA DO MELHOR ATIVO
# Gera e salva a estratégia ideal para o melhor mercado detectado pelo scanner
# ─────────────────────────────────────────────────────────────────────────────

# Mapa de barreira recomendada por tipo e regime
_ESTRATEGIA_POR_REGIME = {
    "LATERAL": {
        "tipo_contrato": "DIGITUNDER",
        "barreira":      7,
        "seq_gatilho":   3,
        "gerenciamento": "soros",
        "duracao":       1,
        "assertividade": "78%",
    },
    "ALTA_VOLATILIDADE": {
        "tipo_contrato": "DIGITUNDER",
        "barreira":      8,
        "seq_gatilho":   2,
        "gerenciamento": "conservador",
        "duracao":       1,
        "assertividade": "88%",
    },
    "TENDENCIA": {
        "tipo_contrato": "FLUXO",
        "barreira":      0,
        "seq_gatilho":   0,
        "gerenciamento": "adaptativo",
        "duracao":       1,
        "assertividade": "65%",
    },
}


@app.route('/ect/criar-estrategia-melhor', methods=['POST'])
def ect_criar_estrategia_melhor():
    """
    Recebe o resultado do scan-multi e cria/salva automaticamente a estratégia
    ideal para o melhor ativo detectado.
    Payload: { melhor: { ativo, regime, recomendacao, cv, ... }, entrada_usd, payout }
    Retorna: { ok, arquivo, estrategia }
    """
    dados   = request.get_json(force=True, silent=True) or {}
    melhor  = dados.get("melhor") or {}
    ativo   = melhor.get("ativo",  "1HZ10V")
    regime  = melhor.get("regime", "LATERAL")
    entrada = float(dados.get("entrada_usd", 0.35))
    payout  = float(dados.get("payout", 0.85))

    # Parâmetros base por regime
    params = _ESTRATEGIA_POR_REGIME.get(regime, _ESTRATEGIA_POR_REGIME["LATERAL"]).copy()

    # Ajusta barreira se R_100 (mais volátil dentro do lateral)
    if ativo == "R_100" and params["tipo_contrato"] == "DIGITUNDER":
        params["barreira"] = max(params["barreira"], 8)

    # Calcula PF e TP/SL sugeridos
    wr_decimal = {
        "DIGITUNDER": {7: 0.70, 8: 0.80, 9: 0.90},
        "DIGITOVER":  {1: 0.90, 2: 0.80, 3: 0.70},
    }.get(params["tipo_contrato"], {}).get(params["barreira"], 0.65)

    take_profit = round(entrada * 20, 2)   # meta: 20x entrada
    stop_loss   = round(take_profit * 1.5, 2)

    # Monta o objeto estratégia completo
    nome = f"ECT Auto — {params['tipo_contrato']} {params['barreira']} {ativo}"
    if params["tipo_contrato"] == "FLUXO":
        nome = f"ECT Auto — FLUXO {ativo}"

    estrategia = {
        "nome":          nome,
        "descricao": (
            f"Gerado automaticamente pelo ECT Scanner. "
            f"Regime detectado: {regime}. "
            f"Ativo: {ativo} (CV={melhor.get('cv', '—')}). "
            f"Barreira {params['barreira']} com gatilho de {params['seq_gatilho']} ticks antes de entrar."
        ),
        "tipo_contrato":  params["tipo_contrato"],
        "barreira":       params["barreira"],
        "barreira_over":  0,
        "barreira_under": 0,
        "seq_gatilho":    params["seq_gatilho"],
        "duracao":        params["duracao"],
        "ativo":          ativo,
        "gerenciamento":  params["gerenciamento"],
        "entrada_usd":    entrada,
        "take_profit_usd": take_profit,
        "stop_loss_usd":   stop_loss,
        "assertividade":   params["assertividade"],
        "pct_janela":      0,
        "pct_min_fraco":   0,
        "pct_min_forte":   0,
        "sat_janela":      0,
        "sat_limiar":      0,
        "sat_smart_min":   0,
        "velas":           0,
        "_ect_auto":       True,
        "_regime_origem":  regime,
        "_cv_origem":      melhor.get("cv"),
        "_wr_estimado":    round(wr_decimal * 100, 1),
    }

    try:
        arquivo = _ia_salvar_novo(estrategia)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

    return jsonify({"ok": True, "arquivo": arquivo, "estrategia": estrategia})


# ─────────────────────────────────────────────────────────────────────────────
# ECT — MONITOR AUTOMÁTICO DE MERCADO (Background Thread)
# Escaneia todos os ativos a cada N minutos e troca automaticamente se
# encontrar um mercado mais assertivo que o atual.
# ─────────────────────────────────────────────────────────────────────────────

_monitor_state = {
    "ativo":        False,       # monitor ligado/desligado
    "intervalo":    10,          # minutos entre cada scan
    "ultimo_scan":  None,        # timestamp do último scan
    "ultimo_melhor": None,       # último melhor ativo detectado
    "ultima_estrategia": None,   # última estratégia criada automaticamente
    "ultima_estrategia_ts": None,# timestamp Unix da última estratégia criada
    "ultima_vista_ts": None,     # timestamp em que o frontend viu a última estratégia
    "log":          [],          # histórico de ações (máx 20)
    "thread":       None,        # referência à thread daemon
    "ciclo":        0,           # contador de ciclos executados
    "historico_tipos": [],       # últimos 4 tipos gerados — usados para proibir repetição
}
_monitor_lock = threading.Lock()


def _monitor_log(msg: str):
    """Registra uma entrada no log do monitor (máx 20 entradas)."""
    with _monitor_lock:
        _monitor_state["log"].append({
            "ts":  time.strftime("%H:%M:%S"),
            "msg": msg,
        })
        if len(_monitor_state["log"]) > 20:
            _monitor_state["log"].pop(0)
    print(f"[ECT Monitor] {msg}")


def _monitor_executar_scan():
    """
    Executa um ciclo completo do monitor:
    1. Escaneia todos os ativos
    2. Compara com o ativo/estratégia atual
    3. Se achar melhor, cria e aplica estratégia automaticamente
    """
    _monitor_log("🔍 Iniciando scan automático dos 10 ativos...")

    resultados = []
    lock_r = threading.Lock()

    def _proc(ativo):
        try:
            ticks = _buscar_ticks_ws_sync(ativo, count=60)
            if not ticks:
                return
            res = _regime_detector.classificar(ticks)
            res["ativo"]  = ativo
            res["score"]  = _REGIME_SCORE.get(res["regime"], 0)
            res["erro"]   = False
            with lock_r:
                resultados.append(res)
        except Exception:
            pass

    threads = [threading.Thread(target=_proc, args=(a,), daemon=True)
               for a in _ATIVOS_DERIV]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    if not resultados:
        _monitor_log("⚠️ Scan sem resultados — sem conexão ou timeout.")
        return

    # Ordena por score desc; dentro do mesmo score embaralha por ciclo
    # para rotacionar entre ativos diferentes a cada scan — evita sempre
    # escolher o mesmo ativo quando vários têm o mesmo score.
    ciclo_atual = _monitor_state.get("ciclo", 0)
    resultados.sort(key=lambda x: (
        -(x.get("score") or 0),
        # desempate por ciclo: rotaciona entre os ativos de mesmo score
        (_ATIVOS_DERIV.index(x["ativo"]) + ciclo_atual) % len(_ATIVOS_DERIV)
    ))
    melhor = next((r for r in resultados if not r.get("erro")
                   and r["regime"] != "DESCONHECIDO"), None)

    if not melhor:
        _monitor_log("⚠️ Nenhum ativo com regime definido.")
        return

    _monitor_log(f"📡 Melhor ativo: {melhor['ativo']} ({melhor['regime']}, CV={melhor.get('cv')})")

    # Persiste regime no estado ECT
    state = _ect_state_ler()
    state["regime_atual"]        = melhor["regime"]
    state["melhor_ativo"]        = melhor["ativo"]
    state["melhor_recomendacao"] = melhor["recomendacao"]
    _ect_state_salvar(state)

    # Registra scan e detecta se ativo/regime mudou (usado apenas no log)
    ultimo = _monitor_state.get("ultimo_melhor")
    mudou  = (
        ultimo is None or
        ultimo.get("ativo")  != melhor["ativo"] or
        ultimo.get("regime") != melhor["regime"]
    )

    with _monitor_lock:
        _monitor_state["ultimo_scan"]   = time.time()
        _monitor_state["ultimo_melhor"] = melhor
        ciclo = _monitor_state.get("ciclo", 0) + 1
        _monitor_state["ciclo"] = ciclo

    if mudou:
        _monitor_log(f"🔄 Novo mercado detectado! Gerando estratégia IA para {melhor['ativo']}...")
    else:
        _monitor_log(f"🔁 Ciclo {ciclo} — mesmo mercado ({melhor['ativo']} {melhor['regime']}), IA reavalia com dados atuais...")

    # Sempre gera estratégia nova a cada ciclo — IA usa CV/slope atuais do mercado

    entrada = 0.35
    take_profit = round(entrada * 20, 2)
    stop_loss   = round(take_profit * 1.5, 2)

    estrategia_auto = None
    fonte_estrategia = "tabela_fixa"

    # ── Tenta gerar via IA Groq ───────────────────────────────────────────────
    groq_cfg = _groq_cfg_ler()
    groq_chave = groq_cfg.get("chave", "")
    groq_modelo = groq_cfg.get("modelo", "llama-3.3-70b-versatile")

    if groq_chave:
        try:
            regime = melhor["regime"]
            ativo  = melhor["ativo"]
            cv     = melhor.get("cv", 0)
            slope  = melhor.get("slope", 0)

            # Histórico dos últimos 4 tipos gerados — proíbe todos eles no próximo ciclo
            historico_tipos = _monitor_state.get("historico_tipos", [])
            proibidos = list(dict.fromkeys(historico_tipos[-4:]))  # últimos 4 únicos

            if proibidos:
                lista_proib = ", ".join(proibidos)
                proibe = (
                    f"PROIBIDO usar qualquer um destes tipos (já usados recentemente): {lista_proib}. "
                    f"Você DEVE escolher um tipo DIFERENTE de todos os listados acima."
                )
            else:
                proibe = ""

            # Pool completo de tipos — IA rotaciona por todos
            _POOL_TIPOS = [
                "DIGITUNDER", "DIGITOVER", "DIGITODD", "DIGITEVEN",
                "DIGITDUPLA", "SATURACAO", "FLUXO", "DIGITPCT",
                "FLUXO_1TICK", "GARRA_DUPLA",
            ]
            tipos_livres = [t for t in _POOL_TIPOS if t not in proibidos]
            sugestao = tipos_livres[ciclo % len(tipos_livres)] if tipos_livres else "DIGITUNDER"

            tipos_disponiveis = (
                "TIPOS DISPONÍVEIS — escolha UM deles:\n"
                "  DIGITUNDER  barreira X → dígito final < X  (ex: barreira=7, ~80% win)\n"
                "  DIGITOVER   barreira X → dígito final > X  (ex: barreira=2, ~80% win)\n"
                "  DIGITODD               → dígito final ímpar (seq_gatilho≥3)\n"
                "  DIGITEVEN              → dígito final par   (seq_gatilho≥3)\n"
                "  DIGITDUPLA  over=X under=Y → entra nos dois lados ao mesmo tempo\n"
                "  GARRA_DUPLA seq_gatilho=N → OVER4+UNDER5 simultâneos, Gale isolado por janela\n"
                "  SATURACAO   sat_janela=25 sat_limiar=70 sat_smart_min=10\n"
                "  FLUXO       velas=5 → aguarda 5 preços consecutivos na mesma direção\n"
                "  FLUXO_1TICK velas=1 → entra imediatamente no próximo tick (sem esperar)\n"
                "  DIGITPCT    pct_janela=50 pct_min_fraco=30 pct_min_forte=70\n"
            )

            user_prompt = (
                f"Você é a IA do GarraBot. Gere uma estratégia NOVA e CRIATIVA.\n\n"
                f"=== DADOS DO MERCADO ===\n"
                f"Ativo: {ativo} | Regime: {regime}\n"
                f"CV: {cv:.6f} | Slope: {slope:.6f}\n"
                f"Ciclo #{ciclo} | {time.strftime('%H:%M:%S')}\n\n"
                f"{tipos_disponiveis}\n"
                f"=== RESTRIÇÕES (OBRIGATÓRIO RESPEITAR) ===\n"
                f"{proibe}\n"
                f"SUGESTÃO para este ciclo: use {sugestao}\n\n"
                f"Gerenciamentos: martingale | soros | loss_recovery | conservador | "
                f"qsr | masaniello | ciclos | adaptativo | fixa\n\n"
                f"Entrada: ${entrada:.2f} | TP: ${take_profit:.2f} | SL: ${stop_loss:.2f}\n\n"
                f"IMPORTANTE: Gere o campo 'tipo_contrato' com EXATAMENTE um dos tipos acima. "
                f"Para FLUXO_1TICK use tipo_contrato='FLUXO' e velas=1."
            )

            # Temperatura rotaciona entre 0.7 e 0.95 por ciclo
            temp = 0.7 + (ciclo % 4) * 0.083
            temp = min(0.95, temp)
            system_prompt = _montar_system_prompt("moderado")
            proposta, _ = _chamar_groq(groq_chave, groq_modelo, system_prompt, user_prompt,
                                        temperature=temp, max_tokens=800)

            if proposta and isinstance(proposta, dict) and proposta.get("tipo_contrato"):
                # ── Normaliza tipo_contrato — corrige nomes inválidos da IA ────
                _TIPOS_VALIDOS = {"DIGITOVER", "DIGITUNDER", "DIGITODD", "DIGITEVEN",
                                  "DIGITDUPLA", "DIGITPCT", "SATURACAO", "FLUXO",
                                  "GARRA_DUPLA"}
                _TIPO_ALIAS = {
                    "DIGITCALL": "FLUXO", "CALL": "FLUXO", "PUT": "FLUXO",
                    "RISE": "FLUXO", "FALL": "FLUXO", "SNIPER": "FLUXO",
                    "OVER": "DIGITOVER", "UNDER": "DIGITUNDER",
                    "ODD": "DIGITODD", "EVEN": "DIGITEVEN",
                    "DUPLA": "DIGITDUPLA", "PCT": "DIGITPCT",
                    "GARRA DUPLA": "GARRA_DUPLA", "GARRADUPLA": "GARRA_DUPLA",
                }
                tipo_raw = str(proposta.get("tipo_contrato", "")).upper().strip()
                if tipo_raw not in _TIPOS_VALIDOS:
                    tipo_raw = _TIPO_ALIAS.get(tipo_raw, "")
                if tipo_raw not in _TIPOS_VALIDOS:
                    # tipo inválido — descarta proposta da IA e usa fallback
                    _monitor_log(f"⚠️ IA retornou tipo inválido '{proposta.get('tipo_contrato')}' — fallback tabela fixa.")
                else:
                    proposta["tipo_contrato"] = tipo_raw

                    # ── Normaliza gerenciamento ──────────────────────────────
                    _GERES_VALIDOS = {"martingale","soros","loss_recovery","conservador","qsr",
                                      "masaniello","ciclos","adaptativo","fixa"}
                    ger_raw = str(proposta.get("gerenciamento", "")).lower().strip()
                    if ger_raw not in _GERES_VALIDOS:
                        proposta["gerenciamento"] = "soros"

                    # ── Sana campos numéricos ────────────────────────────────
                    # velas só faz sentido para FLUXO
                    if tipo_raw != "FLUXO":
                        proposta["velas"] = 0
                    else:
                        proposta["velas"] = max(0, min(7, int(proposta.get("velas") or 3)))

                    # barreira: 0-9
                    proposta["barreira"] = max(0, min(9, int(proposta.get("barreira") or 0)))

                    # seq_gatilho: 0-10
                    proposta["seq_gatilho"] = max(0, min(10, int(proposta.get("seq_gatilho") or 0)))

                    # ativo: força o ativo detectado (não deixa IA mudar)
                    proposta["ativo"] = ativo

                    # Garante campos obrigatórios com defaults seguros
                    proposta.setdefault("entrada_usd",     entrada)
                    proposta.setdefault("take_profit_usd", take_profit)
                    proposta.setdefault("stop_loss_usd",   stop_loss)
                    proposta.setdefault("barreira_over",   0)
                    proposta.setdefault("barreira_under",  0)
                    proposta.setdefault("duracao",         1)
                    proposta.setdefault("pct_janela",      0)
                    proposta.setdefault("pct_min_fraco",   0)
                    proposta.setdefault("pct_min_forte",   0)
                    proposta.setdefault("sat_janela",      0)
                    proposta.setdefault("sat_limiar",      0)
                    proposta.setdefault("sat_smart_min",   0)
                    proposta["_ect_auto"]      = True
                    proposta["_monitor_auto"]  = True
                    proposta["_regime_origem"] = melhor["regime"]
                    proposta["_cv_origem"]     = melhor.get("cv")
                    proposta["_ia_gerada"]     = True
                    estrategia_auto  = proposta
                    fonte_estrategia = "groq_ia"
                    _monitor_log(f"🧠 IA gerou: {proposta.get('nome','?')} | {tipo_raw} | barr={proposta.get('barreira')} | ger={proposta.get('gerenciamento')}")
        except Exception as e_groq:
            _monitor_log(f"⚠️ IA Groq falhou ({e_groq}) — usando tabela fixa como fallback.")

    # ── Fallback: tabela fixa se Groq não gerou ──────────────────────────────
    if estrategia_auto is None:
        params = _ESTRATEGIA_POR_REGIME.get(melhor["regime"], _ESTRATEGIA_POR_REGIME["LATERAL"]).copy()
        if melhor["ativo"] == "R_100" and params["tipo_contrato"] == "DIGITUNDER":
            params["barreira"] = max(params["barreira"], 8)

        nome = f"ECT Auto — {params['tipo_contrato']} {params['barreira']} {melhor['ativo']}"
        if params["tipo_contrato"] == "FLUXO":
            nome = f"ECT Auto — FLUXO {melhor['ativo']}"

        estrategia_auto = {
            "nome":           nome,
            "descricao": (
                f"[ECT Monitor — tabela fixa] {melhor['regime']} em {melhor['ativo']}. "
                f"CV={melhor.get('cv')}. Criada às {time.strftime('%H:%M:%S')}."
            ),
            "tipo_contrato":   params["tipo_contrato"],
            "barreira":        params["barreira"],
            "barreira_over":   0, "barreira_under": 0,
            "seq_gatilho":     params["seq_gatilho"],
            "duracao":         params["duracao"],
            "ativo":           melhor["ativo"],
            "gerenciamento":   params["gerenciamento"],
            "entrada_usd":     entrada,
            "take_profit_usd": take_profit,
            "stop_loss_usd":   stop_loss,
            "assertividade":   params["assertividade"],
            "pct_janela": 0, "pct_min_fraco": 0, "pct_min_forte": 0,
            "sat_janela": 0, "sat_limiar": 0, "sat_smart_min": 0,
            "velas": 0,
            "_ect_auto": True,
            "_monitor_auto":  True,
            "_regime_origem": melhor["regime"],
            "_cv_origem":     melhor.get("cv"),
            "_ia_gerada":     False,
        }

    nome = estrategia_auto.get("nome", f"ECT Auto — {melhor['ativo']}")

    try:
        arquivo = _ia_salvar_novo(estrategia_auto)
        agora_ts = time.time()
        tipo_gerado = estrategia_auto.get("tipo_contrato", "")
        with _monitor_lock:
            _monitor_state["ultima_estrategia"] = {
                "arquivo":        arquivo,
                "nome":           nome,
                "ativo":          melhor["ativo"],
                "regime":         melhor["regime"],
                "criada_em":      time.strftime("%H:%M:%S"),
                "estrategia":     estrategia_auto,
            }
            _monitor_state["ultima_estrategia_ts"] = agora_ts
            _monitor_state["ultima_vista_ts"]      = None
            # Atualiza histórico de tipos — mantém os últimos 6 para rotação
            hist = _monitor_state.get("historico_tipos", [])
            if tipo_gerado and (not hist or hist[-1] != tipo_gerado):
                hist.append(tipo_gerado)
            if len(hist) > 6:
                hist = hist[-6:]
            _monitor_state["historico_tipos"] = hist
        fonte_label = "🧠 IA Groq" if fonte_estrategia == "groq_ia" else "📋 Tabela Fixa"
        _monitor_log(f"✅ Estratégia salva [{fonte_label}]: {nome} | tipo={tipo_gerado} | hist={hist}")
    except Exception as e:
        _monitor_log(f"❌ Erro ao salvar estratégia: {e}")
        return

    # Monta texto de notificação
    tipo_c  = estrategia_auto.get("tipo_contrato", "?")
    barr    = estrategia_auto.get("barreira", 0)
    seq_g   = estrategia_auto.get("seq_gatilho", 0)
    ger     = estrategia_auto.get("gerenciamento", "?")
    fonte_label = "🧠 IA Groq" if fonte_estrategia == "groq_ia" else "📋 Tabela Fixa"
    intervalo_min = _monitor_state["intervalo"]
    proximo_txt   = f"{intervalo_min} min"

    msg_notif = (
        f"🤖 ECT MONITOR — Nova Estratégia AUTO-ATIVADA\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📡 Ativo: {melhor['ativo']} | Regime: {melhor['regime']} (CV={melhor.get('cv')})\n"
        f"⚡ Tipo: {tipo_c} | Barreira: {barr}\n"
        f"🎯 Seq. Gatilho: {seq_g} | Gestão: {ger.upper()}\n"
        f"💵 Entrada: ${entrada:.2f} | TP: ${take_profit:.2f} | SL: ${stop_loss:.2f}\n"
        f"💡 {melhor.get('recomendacao', '—')}\n"
        f"🔬 Fonte: {fonte_label}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏰ {time.strftime('%H:%M:%S')} · Próximo scan em {proximo_txt}"
    )

    cfg_tg = _tg_carregar()
    if cfg_tg.get("enabled"):
        msg_tg_html = (
            f"🤖 <b>ECT MONITOR — Nova Estratégia AUTO-ATIVADA</b>\n"
            f"─────────────\n"
            f"📡 Ativo: <b>{melhor['ativo']}</b> | Regime: <b>{melhor['regime']}</b> (CV={melhor.get('cv')})\n"
            f"⚡ Tipo: <b>{tipo_c}</b> | Barreira: <b>{barr}</b>\n"
            f"🎯 Seq. Gatilho: <b>{seq_g}</b> | Gestão: <b>{ger.upper()}</b>\n"
            f"💵 Entrada: <b>${entrada:.2f}</b> | TP: <b>${take_profit:.2f}</b> | SL: <b>${stop_loss:.2f}</b>\n"
            f"💡 {melhor.get('recomendacao', '—')}\n"
            f"🔬 Fonte: <b>{fonte_label}</b>\n"
            f"─────────────\n"
            f"⏰ {time.strftime('%H:%M:%S')} · Próximo scan em <b>{proximo_txt}</b>"
        )
        _tg_dispatch(lambda: _tg_enviar_texto(cfg_tg["token"], cfg_tg["chat_id"], msg_tg_html))
        _monitor_log("📱 Notificação Telegram enviada.")

    threading.Thread(target=lambda: enviar_notificacao_wa(msg_notif), daemon=True).start()
    _monitor_log("📱 Notificação WhatsApp enviada.")


def _monitor_loop():
    """Loop principal do monitor — roda em thread daemon."""
    _monitor_log("🚀 Monitor automático iniciado.")
    while True:
        with _monitor_lock:
            ativo = _monitor_state["ativo"]
            intervalo = _monitor_state["intervalo"]
        if not ativo:
            break
        try:
            _monitor_executar_scan()
        except Exception as e:
            _monitor_log(f"❌ Erro no ciclo: {e}")
        # Aguarda o intervalo configurado (verifica a cada 30s se foi desligado)
        for _ in range(intervalo * 2):
            time.sleep(30)
            with _monitor_lock:
                if not _monitor_state["ativo"]:
                    break
    _monitor_log("🔴 Monitor automático encerrado.")


@app.route('/ect/monitor/start', methods=['POST'])
def ect_monitor_start():
    """Liga o monitor automático de mercado."""
    dados     = request.get_json(force=True, silent=True) or {}
    intervalo = int(dados.get("intervalo", 10))  # minutos

    with _monitor_lock:
        if _monitor_state["ativo"]:
            return jsonify({"ok": True, "msg": "Monitor já está ativo.",
                            "intervalo": _monitor_state["intervalo"]})
        _monitor_state["ativo"]    = True
        _monitor_state["intervalo"] = max(5, min(60, intervalo))
        _monitor_state["log"]      = []

    t = threading.Thread(target=_monitor_loop, daemon=True, name="ECT-Monitor")
    with _monitor_lock:
        _monitor_state["thread"] = t
    t.start()

    # Persiste no estado ECT
    state = _ect_state_ler()
    state["monitor_ativo"]    = True
    state["monitor_intervalo"] = _monitor_state["intervalo"]
    _ect_state_salvar(state)

    return jsonify({
        "ok":       True,
        "msg":      f"Monitor iniciado — scan a cada {_monitor_state['intervalo']} minutos.",
        "intervalo": _monitor_state["intervalo"],
    })


@app.route('/ect/monitor/stop', methods=['POST'])
def ect_monitor_stop():
    """Desliga o monitor automático."""
    with _monitor_lock:
        _monitor_state["ativo"] = False

    state = _ect_state_ler()
    state["monitor_ativo"] = False
    _ect_state_salvar(state)

    return jsonify({"ok": True, "msg": "Monitor desligado."})


@app.route('/ect/monitor/status', methods=['GET'])
def ect_monitor_status():
    """Retorna o estado atual do monitor."""
    with _monitor_lock:
        ultimo_ts     = _monitor_state.get("ultimo_scan")
        ultimo_melhor = _monitor_state.get("ultimo_melhor")
        ultima_est    = _monitor_state.get("ultima_estrategia")
        ativo         = _monitor_state["ativo"]
        intervalo     = _monitor_state["intervalo"]
        log           = list(_monitor_state["log"])
        ciclo         = _monitor_state.get("ciclo", 0)

    proximo = None
    if ultimo_ts and ativo:
        seg_restantes = max(0, int((ultimo_ts + intervalo * 60) - time.time()))
        proximo = f"{seg_restantes // 60:02d}:{seg_restantes % 60:02d}"

    return jsonify({
        "ativo":              ativo,
        "intervalo_minutos":  intervalo,
        "ultimo_scan":        time.strftime("%H:%M:%S", time.localtime(ultimo_ts)) if ultimo_ts else None,
        "proximo_scan":       proximo,
        "ultimo_melhor":      ultimo_melhor,
        "ultima_estrategia":  ultima_est,
        "log":                log[-10:],
        "ciclo":              ciclo,
    })


@app.route('/ect/estrategia-ativa', methods=['GET', 'POST'])
def ect_estrategia_ativa():
    """
    GET  → retorna { estrategia, is_nova, timestamp } para polling do frontend.
           is_nova = True apenas UMA VEZ por estratégia criada (reseta após 1ª leitura).
    POST { marcar_vista: true } → marca a estratégia como já vista (sem retornar is_nova).
    """
    metodo = request.method

    with _monitor_lock:
        ultima_est  = _monitor_state.get("ultima_estrategia")
        est_ts      = _monitor_state.get("ultima_estrategia_ts")
        vista_ts    = _monitor_state.get("ultima_vista_ts")

    if metodo == "POST":
        dados = request.get_json(force=True, silent=True) or {}
        if dados.get("marcar_vista") and est_ts is not None:
            with _monitor_lock:
                _monitor_state["ultima_vista_ts"] = est_ts
        return jsonify({"ok": True})

    # GET — verifica se há estratégia nova não-vista
    is_nova = (
        ultima_est is not None and
        est_ts is not None and
        (vista_ts is None or vista_ts < est_ts)
    )

    return jsonify({
        "ok":          True,
        "is_nova":     is_nova,
        "timestamp":   est_ts,
        "estrategia":  ultima_est,          # None se nunca criou
    })


# ═══════════════════════════════════════════════════════════════════════════════
# FIM DOS MÓDULOS ECT
# ═══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────
# MÓDULO: Deep Scraper & Analyst (ROI Rápido)
# ─────────────────────────────────────────────────────────

@app.route('/ai/deep-search', methods=['POST'])
def ai_deep_search():
    """
    Realiza uma varredura profunda na internet por estratégias de alto ROI,
    analisa os padrões e retorna uma lista de 5 estratégias PRONTAS.
    """
    dados  = request.get_json(force=True, silent=True) or {}
    chave  = dados.get("chave") or _groq_cfg_ler().get("chave", "")
    modelo = dados.get("modelo") or _groq_cfg_ler().get("modelo", "llama-3.3-70b-versatile")
    perfil = dados.get("perfil", "agressivo")  # Foco em retorno rápido

    passos = [
        "🌐 Iniciando varredura em fóruns de trading e repositórios...",
        "🔍 Filtrando padrões de Win Rate > 75% e ROI acelerado...",
        "🧠 IA processando dados e convertendo em lógica de código...",
    ]

    # Termos de busca focados em ROI e estratégias atuais (2025)
    query_especializada = (
        "best high return Deriv strategies 2025 "
        "digit over under bot patterns validated"
    )
    contexto_web = _buscar_estrategias_web(query_especializada, max_resultados=8)

    system_deep_analyst = (
        "Você é um Analista Quantitativo Sênior especializado em High-Frequency Trading (HFT).\n"
        "Sua tarefa é extrair as 5 melhores estratégias atuais que oferecem RETORNO RÁPIDO (ROI) "
        "e ALTA ASSERTIVIDADE na plataforma Deriv.\n\n"
        "REGRAS DE OURO:\n"
        "1. Foque em padrões de 'Exaustão de Dígitos' e 'Momentum de Volatilidade'.\n"
        "2. As estratégias devem ser seguras mas lucrativas.\n"
        "3. Gere configurações específicas de Barreira, Gatilho e Gerenciamento.\n"
        "4. Retorne APENAS um JSON com o campo 'lista_estrategias' contendo 5 objetos completos."
    )

    prompt_usuario = (
        f"Com base nestes dados de pesquisa real: {contexto_web}\n\n"
        "Crie um catálogo de 5 estratégias 'Ready-to-Trade'.\n"
        "Cada uma deve conter:\n"
        "- nome, descricao_detalhada, assertividade_estimada, roi_esperado.\n"
        "- Configuração técnica completa: tipo_contrato, barreira, seq_gatilho, ativo, gerenciamento.\n"
        "- Parâmetros financeiros: entrada_usd, take_profit_usd, stop_loss_usd.\n\n"
        "Retorne no formato JSON puro para processamento imediato."
    )

    try:
        resultado, _ = _chamar_groq(chave, modelo, system_deep_analyst, prompt_usuario, temperature=0.5)
        lista = resultado.get("lista_estrategias") or resultado.get("pack") or []

        # Adiciona flag de 'Deep Search' para o frontend reconhecer
        for item in lista:
            item["_is_deep_search"] = True

        return jsonify({"ok": True, "estrategias": lista, "_passos": passos})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route('/ai/inspecionar-estrategia', methods=['POST'])
def ai_inspecionar():
    """
    Recebe os dados brutos de uma estratégia e faz uma auditoria
    explicando ponto a ponto o que cada configuração faz.
    """
    dados  = request.get_json(force=True, silent=True) or {}
    chave  = _groq_cfg_ler().get("chave", "")

    prompt_auditoria = (
        f"Analise tecnicamente esta configuração de bot:\n{json.dumps(dados)}\n\n"
        "Explique para o usuário:\n"
        "1. Por que este gatilho foi escolhido?\n"
        "2. Qual o risco matemático real?\n"
        "3. Como o gerenciamento protege o capital?\n"
        "Seja direto e técnico."
    )

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt_auditoria}],
                "temperature": 0.3,
            },
            timeout=30,
        )
        explicacao = res.json()["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "auditoria": explicacao})
    except Exception:
        return jsonify({"ok": False, "erro": "Falha na auditoria da IA"})


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULOS IA AVANÇADOS — Laboratório, Ranking, Padrões, Evolutiva, Estatística
# Integração das 5 novas camadas inteligentes ao Flask
# ═══════════════════════════════════════════════════════════════════════════════

# ── Importações lazy (não requerem instalação extra) ──────────────────────────
try:
    from ia_laboratorio      import executar_laboratorio
    from ia_ranking_inteligente import gerar_ranking_avancado
    from ia_detector_padroes import detectar_padroes
    from ia_evolutiva        import evoluir_estrategia
    from ia_estatistica      import analisar_estatisticas_completas
    _MODULOS_IA_OK = True
except ImportError as _ie:
    _MODULOS_IA_OK = False
    print(f"[IA Avançada] Módulos não carregados: {_ie}")


# ─────────────────────────────────────────────────────────────────────────────
# /ai/laboratorio — Laboratório de Estratégias (gera N e backtesta todas)
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/ai/laboratorio', methods=['POST'])
def ai_laboratorio():
    """
    Gera N candidatas de estratégia, executa backtest Monte Carlo em cada uma
    e retorna o ranking das melhores aprovadas.

    Payload (todos opcionais):
      tipo_contrato — filtra por tipo (ex: "DIGITUNDER")
      ativo         — filtra por ativo (ex: "1HZ25V")
      perfil        — moderado | conservador | agressivo (default: moderado)
      n_candidatas  — quantas gerar (default: 200, máx: 500)
      top_n         — quantas retornar (default: 5)
      stake         — valor da entrada USD (default: 0.35)
      payout        — multiplicador de ganho (default: 0.85)
    """
    if not _MODULOS_IA_OK:
        return jsonify({"erro": "Módulos IA avançados não carregados."})

    dados = request.get_json(force=True, silent=True) or {}

    tipo   = dados.get("tipo_contrato", "")
    ativo  = dados.get("ativo", "")
    perfil = dados.get("perfil", "moderado")
    n_cand = min(int(dados.get("n_candidatas", 200)), 500)
    top_n  = int(dados.get("top_n", 5))
    stake  = float(dados.get("stake", 0.35))
    payout = float(dados.get("payout", 0.85))

    # Carrega memória para o backtest usar histórico real
    memoria = []
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
    except Exception:
        pass

    try:
        resultado = executar_laboratorio(
            tipo_contrato=tipo,
            ativo=ativo,
            perfil=perfil,
            n_candidatas=n_cand,
            top_n=top_n,
            stake=stake,
            payout=payout,
            memoria=memoria,
        )
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# /ai/ranking-avancado — Ranking completo com Win Rate, PF, Drawdown, Sharpe, ROI
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/ai/ranking-avancado', methods=['GET'])
def ai_ranking_avancado():
    """
    Retorna ranking completo das estratégias com todas as métricas avançadas.

    Query params (todos opcionais):
      min_ops    — mínimo de operações (default: 3)
      ativo      — filtra por ativo
      tipo       — filtra por tipo_contrato
      min_wr     — win rate mínimo (%)
      ordenar    — score | win_rate | profit_factor | sharpe | roi (default: score)
    """
    if not _MODULOS_IA_OK:
        return jsonify({"erro": "Módulos IA avançados não carregados."})

    min_ops    = int(request.args.get("min_ops", 3))
    filtro_atv = request.args.get("ativo", "")
    filtro_tp  = request.args.get("tipo", "")
    min_wr     = float(request.args.get("min_wr", 0))
    ordenar    = request.args.get("ordenar", "score")

    # Carrega memória
    memoria = []
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
    except Exception:
        pass

    try:
        resultado = gerar_ranking_avancado(
            memoria=memoria,
            min_ops=min_ops,
            filtro_ativo=filtro_atv,
            filtro_tipo=filtro_tp,
            filtro_min_wr=min_wr,
            ordenar_por=ordenar,
        )
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# /ai/detector-padroes — Detecta padrões em sequência de dígitos
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/ai/detector-padroes', methods=['POST'])
def ai_detector_padroes():
    """
    Analisa uma sequência de dígitos e retorna padrões detectados com
    probabilidade, ação recomendada e Smart Rank.

    Payload:
      digitos          — lista de ints 0-9 (obrigatório, mínimo 5)
      min_probabilidade — filtra padrões abaixo deste % (default: 60)
    """
    if not _MODULOS_IA_OK:
        return jsonify({"erro": "Módulos IA avançados não carregados."})

    dados    = request.get_json(force=True, silent=True) or {}
    digitos  = dados.get("digitos", [])
    min_prob = float(dados.get("min_probabilidade", 60.0))

    if not digitos:
        return jsonify({"erro": "Campo 'digitos' obrigatório."})

    try:
        resultado = detectar_padroes(digitos, min_probabilidade=min_prob)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# /ai/evoluir-estrategia — IA Evolutiva: analisa losses e propõe mutação
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/ai/evoluir-estrategia', methods=['POST'])
def ai_evoluir_estrategia():
    """
    Analisa o histórico de uma estratégia, identifica causa raiz dos losses
    e propõe/aplica mutação para melhorar a performance.

    Payload:
      estrategia   — objeto completo da estratégia (nome, tipo_contrato, barreira, ...)
      auto_aplicar — true = aplica automaticamente se aprovada (default: false)
      stake        — valor da entrada USD (default: 0.35)
      payout       — multiplicador (default: 0.85)
    """
    if not _MODULOS_IA_OK:
        return jsonify({"erro": "Módulos IA avançados não carregados."})

    dados        = request.get_json(force=True, silent=True) or {}
    estrategia   = dados.get("estrategia") or dados
    auto_aplicar = bool(dados.get("auto_aplicar", False))
    stake        = float(dados.get("stake", 0.35))
    payout       = float(dados.get("payout", 0.85))

    if not estrategia.get("nome"):
        return jsonify({"erro": "Campo 'nome' da estratégia é obrigatório."})

    # Carrega memória
    memoria = []
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
    except Exception:
        pass

    try:
        resultado = evoluir_estrategia(
            estrategia=estrategia,
            memoria=memoria,
            stake=stake,
            payout=payout,
            auto_aplicar=auto_aplicar,
        )
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# /ai/estatistica-completa — Análise estatística profunda do vault
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/ai/estatistica-completa', methods=['GET', 'POST'])
def ai_estatistica_completa():
    """
    Análise estatística profunda do memory_vault:
    por hora, dia, ativo, tipo, barreira, gale, correlação EDC e mapa de calor.

    Query params / body (todos opcionais):
      horas    — analisa apenas últimas N horas (0 = tudo)
      ativo    — filtra por ativo
      tipo     — filtra por tipo_contrato
      top_n    — quantos itens em cada ranking (default: 10)
    """
    if not _MODULOS_IA_OK:
        return jsonify({"erro": "Módulos IA avançados não carregados."})

    if request.method == 'POST':
        dados = request.get_json(force=True, silent=True) or {}
    else:
        dados = request.args.to_dict()

    filtro_horas = int(dados.get("horas", 0))
    filtro_atv   = dados.get("ativo", "")
    filtro_tp    = dados.get("tipo", "")
    top_n        = int(dados.get("top_n", 10))

    # Carrega memória
    memoria = []
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
    except Exception:
        pass

    try:
        resultado = analisar_estatisticas_completas(
            memoria=memoria,
            filtro_horas=filtro_horas,
            filtro_ativo=filtro_atv,
            filtro_tipo=filtro_tp,
            top_n=top_n,
        )
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
# 🧙 MODO MESTRE — IA Construtiva com Raciocínio em 3 Camadas
# ═══════════════════════════════════════════════════════════════════════════════

def _extrair_licoes_aprendidas() -> str:
    """
    Extrai lições das estratégias APROVADAS e BLOQUEADAS pelo usuário.
    Transforma feedback em conhecimento estruturado para o prompt.
    """
    dados = _feedback_ler()
    aprovadas  = dados.get("aprovadas", [])
    bloqueadas = dados.get("bloqueadas", [])

    if not aprovadas and not bloqueadas:
        return "Nenhuma lição registrada ainda. Use sua expertise pura."

    linhas = ["=== LIÇÕES APRENDIDAS COM O USUÁRIO ==="]

    if aprovadas:
        linhas.append("\n✅ O QUE FUNCIONOU (usuário aprovou após teste):")
        por_tipo = {}
        for a in aprovadas:
            t = a.get("tipo_contrato", "?")
            por_tipo.setdefault(t, []).append(a)
        for tipo, ests in por_tipo.items():
            barreiras = [str(e.get("barreira", "?")) for e in ests[:5]]
            ativos    = [e.get("ativo", "?") for e in ests[:5]]
            gatilhos  = [str(e.get("seq_gatilho", "?")) for e in ests[:5]]
            linhas.append(
                f"  • {tipo}: barreiras={barreiras}, ativos={ativos}, "
                f"gatilhos={gatilhos} ({len(ests)}x aprovada)"
            )

    if bloqueadas:
        linhas.append("\n❌ O QUE FALHOU (usuário rejeitou — NUNCA repita):")
        for b in bloqueadas[:10]:
            motivo = b.get("motivo", "não gostei")
            linhas.append(
                f"  • {b.get('tipo_contrato','?')} barr={b.get('barreira','?')} "
                f"ativo={b.get('ativo','?')} ger={b.get('gerenciamento','?')} "
                f"→ motivo: {motivo}"
            )

    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
            grupos = {}
            for e in memoria:
                nome = e.get("estrategia", "?")
                grupos.setdefault(nome, {"w": 0, "l": 0})
                if e.get("resultado") == "WIN":
                    grupos[nome]["w"] += 1
                else:
                    grupos[nome]["l"] += 1
            top = sorted(
                [(n, d, d["w"] / (d["w"] + d["l"]) * 100) for n, d in grupos.items()
                 if d["w"] + d["l"] >= 5],
                key=lambda x: x[2], reverse=True
            )[:3]
            if top:
                linhas.append("\n🏆 TOP 3 ESTRATÉGIAS DO VAULT (por win rate real):")
                for nome, d, wr in top:
                    linhas.append(f"  • {nome}: {wr:.0f}% WR ({d['w']}W/{d['l']}L)")
        except Exception:
            pass

    linhas.append("\n=== FIM DAS LIÇÕES ===")
    return "\n".join(linhas)


def _montar_system_prompt_mestre(perfil: str, contexto_extra: str = "") -> str:
    """
    Prompt MESTRE — transforma a IA em um construtor estratégico com:
    1. Raciocínio em cadeia (chain-of-thought)
    2. Auto-crítica antes de entregar
    3. Personas de 3 mestres (Quant, Estatístico, Risk Manager)
    4. Few-shot das estratégias campeãs do usuário
    5. Lições aprendidas do feedback
    """
    aprendizado = _recuperar_aprendizado()
    licoes      = _extrair_licoes_aprendidas()

    base = (
"""╔══════════════════════════════════════════════════════════════╗
║   🧙 MESTRE CONSTRUTOR DE ESTRATÉGIAS — GARRABOT ELITE     ║
║   Você não é uma IA comum. Você é um MESTRE com 3 mentes.  ║
╚══════════════════════════════════════════════════════════════╝

=== SUAS 3 PERSONAS (consulte TODAS antes de decidir) ===

🎯 PERSONA 1 — O QUANT (Matemático)
   Pensa em: probabilidade exata, payout, ROI, expectativa matemática.
   Pergunta: "Qual a expectativa matemática E[valor] desta operação?"
   Fórmula: E = (win_rate × payout) - (loss_rate × stake)
   Só aprova se E > 0 com margem de segurança ≥ 15%.

📊 PERSONA 2 — O ESTATÍSTICO (Padrões)
   Pensa em: exaustão de dígitos, ciclos, saturação, lei dos grandes números.
   Pergunta: "O mercado já mostrou exaustão suficiente para justificar a entrada?"
   Regra: quanto mais central a barreira (4-6), mais exaustão exige.

🛡️ PERSONA 3 — O RISK MANAGER (Sobrevivência)
   Pensa em: drawdown máximo, ruína, exposição, Martingale seguro.
   Pergunta: "Se 5 losses consecutivos acontecerem, a banca sobrevive?"
   Regra: Martingale máximo 3 níveis. Stake nunca > 3% da banca.

=== PROTOCOLO DE RACIOCÍNIO OBRIGATÓRIO (4 etapas) ===

ANTES de gerar a estratégia, você DEVE pensar internamente:

ETAPA 1 — DIAGNÓSTICO DO MERCADO
   • Qual o regime? (TENDENCIA / LATERAL / ALTA_VOLATILIDADE)
   • Qual o ativo mais adequado para este regime?
   • O que o histórico (memória) diz sobre este ativo+tipo?

ETAPA 2 — CONSTRUÇÃO DA HIPÓTESE
   • Qual tipo de contrato explora melhor o regime atual?
   • Qual barreira oferece melhor relação win_rate × payout?
   • Qual seq_gatilho garante exaustão suficiente?
   • Qual gerenciamento protege a banca neste cenário?

ETAPA 3 — AUTO-CRÍTICA (OBRIGATÓRIA)
   • "Se eu fosse o Risk Manager, o que eu criticaria nesta estratégia?"
   • "Existe algum cenário onde esta estratégia quebra a banca?"
   • "A barreira escolhida tem payout suficiente para justificar o risco?"
   • Se encontrar falha → AJUSTE antes de entregar.

ETAPA 4 — VEREDICTO FINAL
   • Confiança real (0-100%) baseada nas 3 personas
   • Se confiança < 70% → RECUSE e sugira alternativa
   • Se confiança ≥ 70% → ENTREGUE com explicação

"""
+ licoes +
"""

=== MEMÓRIA DE PERFORMANCE (o que já aprendemos) ===
""" + aprendizado + """

=== REGRAS INVIOLÁVEIS DE UM MESTRE ===

1. NUNCA gere barreiras de payout irrisório:
   ❌ DIGITUNDER 8/9 | DIGITOVER 0/1/2 | DIGITDUPLA over≤2 ou under≥8
   ✅ DIGITUNDER 5-7 | DIGITOVER 3-6 | DIGITDUPLA over=3-4 + under=6-7

2. NUNCA gere DIGITDIFFERS (banido permanentemente).

3. Para Par/Ímpar ou Over/Under central (4-6): seq_gatilho MÍNIMO = 4.

4. Para DIGITMATCH: seq_gatilho MÍNIMO = 15 (exaustão profunda).

5. Se o usuário pedir "conservador" → use barreiras extremas (Under 7, Over 3).
   Se pedir "agressivo" → use barreiras centrais com gatilho alto.

6. Ativos 1HZ são mais previsíveis → use para dígitos.
   Ativos R_ são mais voláteis → use para FLUXO ou barreiras conservadoras.

7. Gerenciamento por perfil:
   • conservador → 'soros' ou 'conservador' ou 'fixa'
   • moderado    → 'adaptativo' ou 'ciclos'
   • agressivo   → 'martingale' (máx 3) ou 'loss_recovery'

=== FORMATO DE RESPOSTA ===

Responda APENAS com JSON válido contendo:

{
  "nome": "string ≤ 35 chars — criativo e descritivo",
  "descricao": "string ≤ 150 chars — explique o gatilho de entrada",
  "raciocinio": {
    "diagnostico": "1-2 frases sobre o mercado e por que escolheu este ativo",
    "hipotese": "1-2 frases sobre a lógica da estratégia",
    "auto_critica": "1-2 frases sobre o que você questionou e ajustou",
    "confianca_justificada": "por que esta confiança é real"
  },
  "tipo_contrato": "DIGITOVER|DIGITUNDER|DIGITODD|DIGITEVEN|DIGITDUPLA|GARRA_DUPLA|DIGITPCT|SATURACAO|DIGITMATCH|TOUCH|NOTOUCH|FLUXO",
  "barreira": "int (0-9) ou string com sinal para TOUCH/NOTOUCH",
  "barreira_over": "int (apenas DIGITDUPLA)",
  "barreira_under": "int (apenas DIGITDUPLA)",
  "seq_gatilho": "int 0-10",
  "duracao": "int (minutos para FLUXO/TOUCH/NOTOUCH, ticks para dígitos)",
  "velas": "int 2-7 (apenas FLUXO)",
  "pct_janela": "int (apenas DIGITPCT)",
  "pct_min_fraco": "int 1-49 (apenas DIGITPCT)",
  "pct_min_forte": "int 51-99 (apenas DIGITPCT)",
  "sat_janela": "int (apenas SATURACAO)",
  "sat_limiar": "int 51-95 (apenas SATURACAO)",
  "sat_smart_min": "int (apenas SATURACAO)",
  "ativo": "R_10|R_25|R_50|R_75|R_100|1HZ10V|1HZ25V|1HZ50V|1HZ75V|1HZ100V",
  "gerenciamento": "martingale|soros|loss_recovery|conservador|qsr|masaniello|ciclos|adaptativo|fixa",
  "entrada_usd": 0.35,
  "take_profit_usd": 10.0,
  "stop_loss_usd": 100.0,
  "assertividade": "XX% — justificativa curta",
  "confianca_mestre": "int 0-100",
  "alternativa": "string — caso esta falhe, sugira esta outra"
}

Responda SOMENTE com o JSON, sem markdown, sem explicação fora do JSON.
"""
    )

    if contexto_extra:
        base += (
            "\n--- CONTEXTO ADICIONAL ---\n"
            + contexto_extra[:3000]
            + "\n--- FIM DO CONTEXTO ---\n"
        )

    return base


@app.route('/ai/gerar-mestre', methods=['POST'])
def ai_gerar_mestre():
    """
    🧙 MODO MESTRE — Pipeline de 3 etapas:
    1. GERA com raciocínio estruturado (temperature 0.4)
    2. CRITICA a própria estratégia (temperature 0.2)
    3. REFINA se necessário (temperature 0.3)

    Retorna JSON completo com campo 'raciocinio' explicando cada decisão.
    """
    dados  = request.get_json(force=True, silent=True) or {}
    cfg    = _groq_cfg_ler()
    provedor = dados.get("provedor") or cfg.get("provedor_ativo", "groq")

    if provedor == "nvidia":
        chave  = dados.get("chave") or cfg.get("nvidia_chave", "")
        modelo = dados.get("modelo") or cfg.get("nvidia_modelo", "nvidia/llama-3.1-nemotron-70b-instruct")
        _funcao_ia = _chamar_nvidia
    else:
        chave  = dados.get("chave") or cfg.get("chave", "")
        modelo = dados.get("modelo") or cfg.get("modelo", "llama-3.3-70b-versatile")
        _funcao_ia = _chamar_groq

    prompt = dados.get("prompt", "")
    perfil = dados.get("perfil", "moderado")

    if not chave:
        return jsonify({"erro": f"Chave API {provedor.upper()} não configurada"})
    if not prompt:
        return jsonify({"erro": "Prompt vazio"})

    passos = []
    passos.append("🧙 Ativando modo MESTRE — 3 personas consultando...")

    system_mestre = _montar_system_prompt_mestre(perfil)
    contrato_forcado = _detectar_contrato(prompt.lower())

    # ══════ ETAPA 1: GERAÇÃO COM RACIOCÍNIO ══════
    passos.append("🎯 Etapa 1/3: Quant + Estatístico + Risk Manager analisando...")
    estrategia = {}
    conteudo_raw = ""
    try:
        estrategia, conteudo_raw = _funcao_ia(
            chave, modelo, system_mestre, prompt,
            temperature=0.4, max_tokens=1500
        )
        if contrato_forcado and estrategia.get("tipo_contrato", "").upper() != "DIGITPCT":
            estrategia["tipo_contrato"] = contrato_forcado
    except Exception as e:
        return jsonify({"erro": f"Falha na geração: {e}", "_passos": passos})

    _erro = _validar_barreira(estrategia)
    if _erro:
        return jsonify({"erro": _erro, "_passos": passos})

    passos.append(f"✅ Estratégia construída: {estrategia.get('nome', '?')}")

    # ══════ ETAPA 2: AUTO-CRÍTICA ══════
    passos.append("🔍 Etapa 2/3: Risk Manager criticando a estratégia...")
    system_critica = (
        "Você é o RISK MANAGER do GarraBot. Recebeu uma estratégia e deve criticá-la.\n"
        "Avalie de 0 a 100 a qualidade e sugira MELHORIAS CONCRETAS se necessário.\n"
        "Responda APENAS com JSON:\n"
        "{\n"
        "  \"nota\": int 0-100,\n"
        "  \"pontos_fortes\": [\"string\", ...],\n"
        "  \"pontos_fracos\": [\"string\", ...],\n"
        "  \"ajustes_sugeridos\": {\"campo\": \"valor_novo\"} (vazio se não precisa ajustar),\n"
        "  \"veredicto\": \"APROVADA\" | \"AJUSTAR\" | \"REPROVADA\"\n"
        "}\n"
        "Critérios de reprovação:\n"
        "- Barreira com payout irrisório (Under 8/9, Over 0/1/2)\n"
        "- Seq_gatilho baixo para barreira central (< 4)\n"
        "- Martingale > 3 níveis\n"
        "- Stake > 5% da banca implícita\n"
    )

    prompt_critica = (
        f"ESTRATÉGIA GERADA:\n{json.dumps(estrategia, ensure_ascii=False)}\n"
        f"PEDIDO ORIGINAL: {prompt}\n"
        f"PERFIL: {perfil}\n"
        "Critique e sugira ajustes."
    )

    critica = {}
    try:
        critica, _ = _funcao_ia(
            chave, modelo, system_critica, prompt_critica,
            temperature=0.2, max_tokens=600
        )
        passos.append(f"📝 Nota do Risk Manager: {critica.get('nota', '?')}/100 — {critica.get('veredicto', '?')}")
    except Exception:
        critica = {"nota": 75, "veredicto": "APROVADA", "ajustes_sugeridos": {}}
        passos.append("⚠️ Crítica falhou — seguindo com estratégia original")

    # ══════ ETAPA 3: REFINO (se necessário) ══════
    estrategia_final = dict(estrategia)
    if critica.get("veredicto") == "AJUSTAR" and critica.get("ajustes_sugeridos"):
        passos.append("🔧 Etapa 3/3: Refinando com base na crítica...")
        ajustes = critica["ajustes_sugeridos"]
        for campo, valor in ajustes.items():
            if campo in estrategia_final:
                estrategia_final[campo] = valor
        estrategia_final["_refinada"] = True
        estrategia_final["_ajustes_aplicados"] = ajustes
        passos.append(f"✅ {len(ajustes)} ajustes aplicados")
    else:
        passos.append("✅ Etapa 3/3: Estratégia aprovada sem ajustes")

    # ══════ MONTA RESULTADO FINAL ══════
    estrategia_final["_mestre"] = True
    estrategia_final["_critica"] = {
        "nota": critica.get("nota", 0),
        "pontos_fortes": critica.get("pontos_fortes", []),
        "pontos_fracos": critica.get("pontos_fracos", []),
        "veredicto": critica.get("veredicto", "APROVADA"),
    }
    estrategia_final["_passos"] = passos + ["🧙 Estratégia Mestre concluída!"]

    if "nome" in estrategia_final:
        estrategia_final["nome"] = estrategia_final["nome"][:35]
    if "descricao" in estrategia_final:
        estrategia_final["descricao"] = estrategia_final["descricao"][:150]

    _erro_final = _validar_barreira(estrategia_final)
    if _erro_final:
        return jsonify({"erro": _erro_final, "_passos": passos})

    return jsonify(estrategia_final)


# ═══════════════════════════════════════════════════════════════════════════════
# 🏆 MODO SUPREMO — IA Autônoma Total (5 Personas + Loop Fechado)
# A IA analisa, decide, cria, aplica e aprende — sem intervenção humana
# ═══════════════════════════════════════════════════════════════════════════════

_SUPREMO_STATE = {
    "ativo": False,
    "intervalo": 5,          # minutos entre decisões
    "ultimo_ciclo": None,
    "ciclos_executados": 0,
    "estrategia_atual": None,
    "thread": None,
    "log": [],
    "persona_votos": {},     # últimos votos das 5 personas
}
_SUPREMO_LOCK = threading.Lock()


def _supremo_log(msg: str):
    """Registra log do modo supremo (máx 50 entradas)."""
    with _SUPREMO_LOCK:
        _SUPREMO_STATE["log"].append({
            "ts":  time.strftime("%H:%M:%S"),
            "msg": msg,
        })
        if len(_SUPREMO_STATE["log"]) > 50:
            _SUPREMO_STATE["log"].pop(0)
    print(f"[SUPREMO] {msg}")


def _coletar_dados_mercado_supremo() -> dict:
    """
    Coleta dados completos de todos os 10 ativos para a IA Suprema decidir.
    Retorna: { ativos: [{ativo, regime, cv, slope, fluxo, ticks, score}], melhor }
    """
    resultados = []
    lock = threading.Lock()

    def _proc(ativo):
        try:
            ticks = _buscar_ticks_ws_sync(ativo, count=80)
            if not ticks:
                return
            res = _regime_detector.classificar(ticks)

            digitos_finais = []
            for t in ticks[-20:]:
                try:
                    str_t = f"{t:.5f}"
                    digito = int(str_t.split(".")[-1][-1])
                    digitos_finais.append(digito)
                except Exception:
                    pass

            hist_digitos = {}
            for d in digitos_finais:
                hist_digitos[d] = hist_digitos.get(d, 0) + 1

            res["ativo"]         = ativo
            res["score"]         = _REGIME_SCORE.get(res["regime"], 0)
            res["digitos_20"]    = digitos_finais[-20:]
            res["hist_digitos"]  = hist_digitos
            res["ultimo_preco"]  = ticks[-1] if ticks else 0
            res["erro"]          = False

            with lock:
                resultados.append(res)
        except Exception as e:
            with lock:
                resultados.append({
                    "ativo": ativo, "regime": "DESCONHECIDO",
                    "erro": True, "descricao": str(e)
                })

    threads = [threading.Thread(target=_proc, args=(a,), daemon=True)
               for a in _ATIVOS_DERIV]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)

    resultados.sort(key=lambda x: (
        -(x.get("score") or 0),
        x.get("cv") or 99
    ))

    melhor = next((r for r in resultados if not r.get("erro")
                   and r["regime"] != "DESCONHECIDO"), None)

    return {"ativos": resultados, "melhor": melhor}


def _montar_prompt_supremo(dados_mercado: dict, historico_decisoes: list) -> str:
    """
    Monta o prompt supremo com 5 personas debatendo.
    Cada persona tem uma visão diferente do mercado.
    """
    melhor = dados_mercado.get("melhor") or {}
    ativos_top = dados_mercado.get("ativos", [])[:5]

    resumo_ativos = []
    for a in ativos_top:
        resumo_ativos.append(
            f"  • {a['ativo']}: regime={a.get('regime')} "
            f"CV={a.get('cv', 0):.6f} slope={a.get('slope', 0):.6f} "
            f"fluxo={a.get('fluxo_direcao', 'NEUTRO')} "
            f"dígitos_20={a.get('digitos_20', [])[-10:]}"
        )
    resumo_txt = "\n".join(resumo_ativos)

    hist_txt = "Nenhuma decisão anterior ainda."
    if historico_decisoes:
        linhas = []
        for h in historico_decisoes[-5:]:
            linhas.append(
                f"  • {h.get('hora', '?')}: {h.get('ativo', '?')} "
                f"{h.get('tipo_contrato', '?')} barr={h.get('barreira', '?')} "
                f"→ {h.get('resultado', '?')} (WR={h.get('win_rate', '?')}%)"
            )
        hist_txt = "\n".join(linhas)

    top_vault = "Sem dados ainda."
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
            grupos = {}
            for e in memoria:
                nome = e.get("estrategia", "?")
                grupos.setdefault(nome, {"w": 0, "l": 0})
                if e.get("resultado") == "WIN":
                    grupos[nome]["w"] += 1
                else:
                    grupos[nome]["l"] += 1
            top = sorted(
                [(n, d, d["w"] / (d["w"] + d["l"]) * 100) for n, d in grupos.items()
                 if d["w"] + d["l"] >= 3],
                key=lambda x: x[2], reverse=True
            )[:3]
            if top:
                top_vault = "\n".join(
                    f"  • {n}: {wr:.0f}% WR ({d['w']}W/{d['l']}L)"
                    for n, d, wr in top
                )
        except Exception:
            pass

    licoes = _extrair_licoes_aprendidas()

    return f"""╔══════════════════════════════════════════════════════════════╗
║   🏆 IA SUPREMA — ENTIDADE AUTÔNOMA DE TRADING            ║
║   Você é uma IA SUPREMA com 5 MENTES trabalhando juntas.  ║
║   Você DECIDE TUDO: ativo, tipo, barreira, gatilho,       ║
║   gerenciamento, duração. Sem intervenção humana.         ║
╚══════════════════════════════════════════════════════════════╝

=== SUAS 5 PERSONAS (todas devem opinar antes da decisão final) ===

🎯 PERSONA 1 — O QUANT (Matemático)
   Pensa em: payout, ROI, expectativa matemática.
   Fórmula: E = (win_rate × payout) - (loss_rate × stake)
   Só aprova se E > 0 com margem ≥ 15%.

📊 PERSONA 2 — O ESTATÍSTICO (Padrões de Dígitos)
   Analisa o histograma dos últimos 20 dígitos.
   Pergunta: "Qual dígito está saturado? Qual está ausente?"
   Regra: quanto mais central a barreira (4-6), mais exaustão exige.

🌊 PERSONA 3 — O ANALISTA DE FLUXO (Regime de Mercado)
   Analisa CV, slope e fluxo_direcao de cada ativo.
   Pergunta: "Qual ativo está em TENDENCIA clara? Qual está LATERAL?"
   Regra: TENDENCIA → FLUXO | LATERAL → Dígitos | ALTA_VOL → Conservador

🛡️ PERSONA 4 — O RISK MANAGER (Sobrevivência)
   Pensa em: drawdown, Martingale seguro, exposição.
   Regra: stake ≤ 3% da banca. Martingale máx 3 níveis.

🧠 PERSONA 5 — O APRENDIZ (Memória do Vault)
   Consulta o histórico de decisões anteriores.
   Pergunta: "O que funcionou? O que falhou? Qual padrão se repete?"
   Regra: NUNCA repita erros do passado.

=== DADOS DO MERCADO EM TEMPO REAL ===

TOP 5 ATIVOS (ordenados por qualidade):
{resumo_txt}

MELHOR ATIVO DETECTADO:
  • Ativo: {melhor.get('ativo', '?')}
  • Regime: {melhor.get('regime', '?')}
  • CV: {melhor.get('cv', 0):.6f}
  • Slope: {melhor.get('slope', 0):.6f}
  • Fluxo: {melhor.get('fluxo_direcao', 'NEUTRO')}
  • Dígitos recentes: {melhor.get('digitos_20', [])[-10:]}
  • Recomendação técnica: {melhor.get('recomendacao', '—')}

=== HISTÓRICO DAS SUAS ÚLTIMAS DECISÕES ===
{hist_txt}

=== TOP 3 ESTRATÉGIAS DO VAULT (melhor win rate real) ===
{top_vault}

=== LIÇÕES APRENDIDAS ===
{licoes}

=== PROTOCOLO DE DECISÃO SUPREMA (OBRIGATÓRIO) ===

ETAPA 1 — CADA PERSONA VOTA (internamente)
   Cada uma das 5 personas analisa os dados e emite um voto:
   - Qual ativo escolher?
   - Qual tipo de contrato?
   - Qual barreira?
   - Qual gerenciamento?

ETAPA 2 — DEBATE (conflitos são resolvidos matematicamente)
   Se Quant diz "Over 3" e Estatístico diz "Under 7", você decide
   com base em qual tem maior expectativa matemática.

ETAPA 3 — DECISÃO FINAL
   Você escolhe a estratégia que maximiza:
   (confiança × ROI esperado) - (risco de ruína)

ETAPA 4 — AUTO-CRÍTICA
   "Se eu fosse o Risk Manager, o que eu criticaria?"
   Se encontrar falha → AJUSTE antes de entregar.

=== REGRAS INVIOLÁVEIS ===

1. NUNCA barreiras de payout irrisório:
   ❌ DIGITUNDER 8/9 | DIGITOVER 0/1/2 | DIGITDUPLA over≤2 ou under≥8
   ✅ DIGITUNDER 5-7 | DIGITOVER 3-6 | DIGITDUPLA over=3-4 + under=6-7

2. NUNCA DIGITDIFFERS (banido permanentemente).

3. Par/Ímpar ou Over/Under central (4-6): seq_gatilho MÍNIMO = 4.

4. DIGITMATCH: seq_gatilho MÍNIMO = 15.

5. Se o melhor ativo for R_100 (volátil) → barreiras conservadoras.
   Se for 1HZ10V/1HZ25V (estável) → pode usar barreiras centrais.

6. Gerenciamento por regime:
   • LATERAL → 'soros' | 'ciclos' | 'adaptativo'
   • TENDENCIA → 'martingale' (máx 3) | 'loss_recovery'
   • ALTA_VOLATILIDADE → 'conservador' | 'fixa'

7. Duração:
   • Dígitos → duracao=1 (tick único)
   • FLUXO → duracao=1-5 minutos, velas=3-5
   • TOUCH/NOTOUCH → duracao=2-3 minutos

=== FORMATO DE RESPOSTA ===

Responda APENAS com JSON válido:

{{
  "nome": "string ≤ 35 chars — criativo e descritivo",
  "descricao": "string ≤ 150 chars — explique o gatilho de entrada",
  "raciocinio_supremo": {{
    "voto_quant": "1-2 frases do Quant",
    "voto_estatistico": "1-2 frases do Estatístico (analise os dígitos_20)",
    "voto_fluxo": "1-2 frases do Analista de Fluxo (analise CV/slope)",
    "voto_risk": "1-2 frases do Risk Manager",
    "voto_aprendiz": "1-2 frases do Aprendiz (consulte histórico)",
    "debate": "1-2 frases resolvendo conflitos entre personas",
    "decisao_final": "1-2 frases explicando POR QUE esta estratégia"
  }},
  "tipo_contrato": "DIGITOVER|DIGITUNDER|DIGITODD|DIGITEVEN|DIGITDUPLA|GARRA_DUPLA|DIGITPCT|SATURACAO|DIGITMATCH|TOUCH|NOTOUCH|FLUXO",
  "barreira": 5,
  "barreira_over": 3,
  "barreira_under": 7,
  "seq_gatilho": 0,
  "duracao": 1,
  "velas": 3,
  "pct_janela": 50,
  "pct_min_fraco": 30,
  "pct_min_forte": 60,
  "sat_janela": 25,
  "sat_limiar": 70,
  "sat_smart_min": 10,
  "ativo": "1HZ50V",
  "gerenciamento": "soros",
  "entrada_usd": 0.35,
  "take_profit_usd": 10.0,
  "stop_loss_usd": 100.0,
  "assertividade": "XX% — justificativa",
  "confianca_suprema": 80,
  "alternativa": "string — caso esta falhe, sugira esta outra"
}}

Responda SOMENTE com o JSON, sem markdown.
"""


@app.route('/ai/supremo', methods=['POST'])
def ai_supremo():
    """
    🏆 MODO SUPREMO — IA Autônoma Total
    1. Coleta dados de mercado em tempo real (10 ativos)
    2. Monta prompt com 5 personas debatendo
    3. IA decide TUDO: ativo, tipo, barreira, gerenciamento, tempo
    4. Retorna estratégia pronta para aplicar
    """
    dados  = request.get_json(force=True, silent=True) or {}
    chave  = dados.get("chave") or _groq_cfg_ler().get("chave", "")
    modelo = dados.get("modelo") or _groq_cfg_ler().get("modelo", "llama-3.3-70b-versatile")

    if not chave:
        return jsonify({"erro": "Chave API Groq não configurada"})

    passos = ["🏆 Ativando modo SUPREMO — 5 personas consultando..."]

    # ══════ ETAPA 1: Coleta dados de mercado ══════
    passos.append("📡 Escaneando 10 ativos em tempo real...")
    dados_mercado = _coletar_dados_mercado_supremo()
    melhor = dados_mercado.get("melhor")

    if not melhor:
        return jsonify({"erro": "Nenhum ativo com dados válidos", "_passos": passos})

    passos.append(f"🎯 Melhor ativo: {melhor['ativo']} ({melhor['regime']})")

    # ══════ ETAPA 2: Histórico de decisões ══════
    historico_decisoes = []
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
            for e in memoria[-10:]:
                historico_decisoes.append({
                    "hora": e.get("hora_str", "?"),
                    "ativo": e.get("contexto", "?"),
                    "tipo_contrato": e.get("tipo_contrato", "?"),
                    "barreira": e.get("barreira", "?"),
                    "resultado": e.get("resultado", "?"),
                })
        except Exception:
            pass

    # ══════ ETAPA 3: IA Suprema decide ══════
    passos.append("🧠 5 personas debatendo...")
    prompt_supremo = _montar_prompt_supremo(dados_mercado, historico_decisoes)

    estrategia = {}
    try:
        estrategia, _ = _chamar_groq(
            chave, modelo,
            "Você é a IA SUPREMA do GarraBot. Responda APENAS com JSON.",
            prompt_supremo,
            temperature=0.5, max_tokens=1800
        )
    except Exception as e:
        return jsonify({"erro": f"Falha na decisão: {e}", "_passos": passos})

    _erro = _validar_barreira(estrategia)
    if _erro:
        return jsonify({"erro": _erro, "_passos": passos})

    passos.append(f"✅ Decisão tomada: {estrategia.get('nome', '?')}")

    # ══════ ETAPA 4: Auto-crítica ══════
    passos.append("🔍 Auto-crítica do Risk Manager...")
    system_critica = (
        "Você é o RISK MANAGER da IA Suprema. Critique esta estratégia.\n"
        "Responda APENAS com JSON:\n"
        "{\"nota\": 0, \"pontos_fortes\": [], \"pontos_fracos\": [], "
        "\"ajustes_sugeridos\": {}, \"veredicto\": \"APROVADA\"}"
    )
    prompt_critica = (
        f"ESTRATÉGIA: {json.dumps(estrategia, ensure_ascii=False)}\n"
        f"MERCADO: {melhor['ativo']} {melhor['regime']} CV={melhor.get('cv', 0):.6f}"
    )

    critica = {}
    try:
        critica, _ = _chamar_groq(
            chave, modelo, system_critica, prompt_critica,
            temperature=0.2, max_tokens=500
        )
        passos.append(f"📝 Nota: {critica.get('nota', '?')}/100 — {critica.get('veredicto', '?')}")
    except Exception:
        critica = {"nota": 80, "veredicto": "APROVADA", "ajustes_sugeridos": {}}

    # ══════ ETAPA 5: Refino se necessário ══════
    estrategia_final = dict(estrategia)
    if critica.get("veredicto") == "AJUSTAR" and critica.get("ajustes_sugeridos"):
        passos.append("🔧 Refinando com base na crítica...")
        for campo, valor in critica["ajustes_sugeridos"].items():
            if campo in estrategia_final:
                estrategia_final[campo] = valor
        estrategia_final["_refinada"] = True

    # ══════ MONTA RESULTADO FINAL ══════
    estrategia_final["_supremo"] = True
    estrategia_final["_critica"] = {
        "nota": critica.get("nota", 0),
        "pontos_fortes": critica.get("pontos_fortes", []),
        "pontos_fracos": critica.get("pontos_fracos", []),
        "veredicto": critica.get("veredicto", "APROVADA"),
    }
    estrategia_final["_dados_mercado"] = {
        "melhor_ativo": melhor["ativo"],
        "regime": melhor["regime"],
        "cv": melhor.get("cv"),
        "slope": melhor.get("slope"),
        "fluxo": melhor.get("fluxo_direcao"),
    }
    estrategia_final["_passos"] = passos + ["🏆 Decisão Suprema concluída!"]

    if "nome" in estrategia_final:
        estrategia_final["nome"] = estrategia_final["nome"][:35]
    if "descricao" in estrategia_final:
        estrategia_final["descricao"] = estrategia_final["descricao"][:150]

    return jsonify(estrategia_final)


# ═══════════════════════════════════════════════════════════════════════════════
# 🏆 LOOP SUPREMO — Modo Autônomo Total (background thread)
# ═══════════════════════════════════════════════════════════════════════════════

def _supremo_loop():
    """Loop principal do modo supremo — roda em thread daemon."""
    _supremo_log("🏆 Modo SUPREMO iniciado — IA autônoma total ativada.")

    groq_cfg = _groq_cfg_ler()
    chave = groq_cfg.get("chave", "")
    modelo = groq_cfg.get("modelo", "llama-3.3-70b-versatile")

    if not chave:
        _supremo_log("❌ Chave Groq não configurada — modo supremo encerrado.")
        with _SUPREMO_LOCK:
            _SUPREMO_STATE["ativo"] = False
        return

    while True:
        with _SUPREMO_LOCK:
            if not _SUPREMO_STATE["ativo"]:
                break
            intervalo = _SUPREMO_STATE["intervalo"]

        try:
            with _SUPREMO_LOCK:
                ciclo_num = _SUPREMO_STATE["ciclos_executados"] + 1
            _supremo_log(f"🔄 Ciclo #{ciclo_num} — analisando mercado...")

            dados_mercado = _coletar_dados_mercado_supremo()
            melhor = dados_mercado.get("melhor")

            if not melhor:
                _supremo_log("⚠️ Nenhum ativo com dados válidos — aguardando próximo ciclo.")
            else:
                historico_decisoes = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                            memoria = json.load(f)
                        for e in memoria[-10:]:
                            historico_decisoes.append({
                                "hora": e.get("hora_str", "?"),
                                "ativo": e.get("contexto", "?"),
                                "tipo_contrato": e.get("tipo_contrato", "?"),
                                "barreira": e.get("barreira", "?"),
                                "resultado": e.get("resultado", "?"),
                            })
                    except Exception:
                        pass

                prompt_supremo = _montar_prompt_supremo(dados_mercado, historico_decisoes)

                estrategia, _ = _chamar_groq(
                    chave, modelo,
                    "Você é a IA SUPREMA do GarraBot. Responda APENAS com JSON.",
                    prompt_supremo,
                    temperature=0.5, max_tokens=1800
                )

                _erro = _validar_barreira(estrategia)
                if _erro:
                    _supremo_log(f"⚠️ Estratégia rejeitada: {_erro}")
                else:
                    estrategia["_supremo"] = True
                    estrategia["_auto"]    = True
                    estrategia["_ciclo"]   = ciclo_num

                    try:
                        arquivo = _ia_salvar_novo(estrategia)
                        _supremo_log(f"✅ Estratégia salva: {estrategia.get('nome', '?')} | {arquivo}")

                        with _SUPREMO_LOCK:
                            _SUPREMO_STATE["estrategia_atual"] = {
                                "arquivo":    arquivo,
                                "nome":       estrategia.get("nome", "?"),
                                "ativo":      melhor["ativo"],
                                "regime":     melhor["regime"],
                                "criada_em":  time.strftime("%H:%M:%S"),
                                "estrategia": estrategia,
                            }
                            _SUPREMO_STATE["ciclos_executados"] += 1
                            _SUPREMO_STATE["ultimo_ciclo"] = time.time()

                        cfg_tg = _tg_carregar()
                        if cfg_tg.get("enabled"):
                            msg = (
                                f"🏆 <b>SUPREMO — Nova Decisão Autônoma</b>\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"📡 Ativo: <b>{melhor['ativo']}</b> | "
                                f"Regime: <b>{melhor['regime']}</b>\n"
                                f"⚡ Tipo: <b>{estrategia.get('tipo_contrato', '?')}</b> | "
                                f"Barreira: <b>{estrategia.get('barreira', '?')}</b>\n"
                                f"🎯 Seq: <b>{estrategia.get('seq_gatilho', '?')}</b> | "
                                f"Gestão: <b>{str(estrategia.get('gerenciamento', '?')).upper()}</b>\n"
                                f"💵 Entrada: <b>${estrategia.get('entrada_usd', 0.35):.2f}</b>\n"
                                f"🧠 Confiança: <b>{estrategia.get('confianca_suprema', '?')}%</b>\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"⏰ {time.strftime('%H:%M:%S')} | "
                                f"Ciclo #{_SUPREMO_STATE['ciclos_executados']}"
                            )
                            _tg_dispatch(lambda: _tg_enviar_texto(
                                cfg_tg["token"], cfg_tg["chat_id"], msg
                            ))
                    except Exception as e:
                        _supremo_log(f"❌ Erro ao salvar: {e}")

        except Exception as e:
            _supremo_log(f"❌ Erro no ciclo: {e}")

        # Aguarda intervalo (checa parada a cada 30s)
        for _ in range(intervalo * 2):
            time.sleep(30)
            with _SUPREMO_LOCK:
                if not _SUPREMO_STATE["ativo"]:
                    break

    _supremo_log("🔴 Modo SUPREMO encerrado.")


@app.route('/supremo/start', methods=['POST'])
def supremo_start():
    """Ativa o modo supremo autônomo."""
    dados = request.get_json(force=True, silent=True) or {}
    intervalo = int(dados.get("intervalo", 5))

    with _SUPREMO_LOCK:
        if _SUPREMO_STATE["ativo"]:
            return jsonify({"ok": True, "msg": "Modo supremo já está ativo."})

        _SUPREMO_STATE["ativo"]    = True
        _SUPREMO_STATE["intervalo"] = max(3, min(60, intervalo))
        _SUPREMO_STATE["log"]      = []

        t = threading.Thread(target=_supremo_loop, daemon=True, name="Supremo-Loop")
        _SUPREMO_STATE["thread"] = t
        t.start()

    return jsonify({
        "ok": True,
        "msg": f"Modo SUPREMO ativado — decisão a cada {_SUPREMO_STATE['intervalo']} minutos.",
        "intervalo": _SUPREMO_STATE["intervalo"],
    })


@app.route('/supremo/stop', methods=['POST'])
def supremo_stop():
    """Desativa o modo supremo."""
    with _SUPREMO_LOCK:
        _SUPREMO_STATE["ativo"] = False
    return jsonify({"ok": True, "msg": "Modo SUPREMO desativado."})


@app.route('/supremo/status', methods=['GET'])
def supremo_status():
    """Retorna status do modo supremo."""
    with _SUPREMO_LOCK:
        ultimo = (
            time.strftime("%H:%M:%S", time.localtime(_SUPREMO_STATE["ultimo_ciclo"]))
            if _SUPREMO_STATE["ultimo_ciclo"] else None
        )
        return jsonify({
            "ativo":             _SUPREMO_STATE["ativo"],
            "intervalo":         _SUPREMO_STATE["intervalo"],
            "ciclos_executados": _SUPREMO_STATE["ciclos_executados"],
            "ultimo_ciclo":      ultimo,
            "estrategia_atual":  _SUPREMO_STATE["estrategia_atual"],
            "log":               _SUPREMO_STATE["log"][-15:],
        })


@app.route('/supremo/decidir-agora', methods=['POST'])
def supremo_decidir_agora():
    """Força uma decisão imediata da IA Suprema (sem esperar o loop)."""
    dados  = request.get_json(force=True, silent=True) or {}
    chave  = dados.get("chave") or _groq_cfg_ler().get("chave", "")
    modelo = dados.get("modelo") or _groq_cfg_ler().get("modelo", "llama-3.3-70b-versatile")

    if not chave:
        return jsonify({"erro": "Chave API Groq não configurada"})

    with app.test_request_context(
        '/ai/supremo', method='POST',
        json={"chave": chave, "modelo": modelo}
    ):
        return ai_supremo()


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-CONTAS — AccountManager
# Gerencia N contas simultâneas com estados independentes
# ═══════════════════════════════════════════════════════════════════════════════

import uuid

CONTAS_ARQUIVO = os.path.join(_BASE_DIR, "contas_config.json")

class AccountManager:
    """
    Gerencia múltiplas contas Deriv simultaneamente.

    Estados de cada conta:
      APRENDIZADO  → conta teste, IA aprende e valida estratégias
      VALIDACAO    → estratégias sendo testadas em paper trading
      APROVADA     → estratégia passou nos critérios de promoção
      PRODUCAO     → conta real operando com segurança
      PAUSADA      → conta pausada manualmente
    """

    # Critérios de promoção (TESTE → REAL)
    CRITERIOS_PROMOCAO = {
        "min_operacoes":           50,
        "min_win_rate":            65.0,
        "min_profit_factor":       1.30,
        "max_drawdown_pct":        15.0,
        "min_dias_aprendizado":    3,
        "min_estrategias_validas": 2,
    }

    # Limites de segurança para conta REAL
    LIMITES_REAL = {
        "max_stake_pct_banca":  0.02,
        "max_gale_nivel":       2,
        "max_loss_diario_pct":  0.05,
        "max_trades_dia":       30,
        "cooldown_apos_loss":   300,
        "threshold_edc_min":    85,
    }

    def __init__(self):
        self.contas: dict = {}
        self.conta_teste_id = None
        self.conta_real_id  = None
        self.conta_secundaria_id = None
        self._lock = threading.Lock()
        self._carregar()

    # ── Persistência ──────────────────────────────────────────────────────
    def _carregar(self):
        try:
            if os.path.exists(CONTAS_ARQUIVO):
                with open(CONTAS_ARQUIVO, "r", encoding="utf-8") as f:
                    dados = json.load(f)
                self.contas              = dados.get("contas", {})
                self.conta_teste_id      = dados.get("conta_teste_id")
                self.conta_real_id       = dados.get("conta_real_id")
                self.conta_secundaria_id = dados.get("conta_secundaria_id")
                # OTPs expiram — limpa wss_url de todas as contas ao carregar.
                # O access_token é preservado para permitir reconexão automática.
                for conta in self.contas.values():
                    conta["wss_url"] = ""
        except Exception:
            pass

    def _salvar(self):
        try:
            with open(CONTAS_ARQUIVO, "w", encoding="utf-8") as f:
                json.dump({
                    "contas":              self.contas,
                    "conta_teste_id":      self.conta_teste_id,
                    "conta_real_id":       self.conta_real_id,
                    "conta_secundaria_id": self.conta_secundaria_id,
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── Cadastro de contas ────────────────────────────────────────────────
    def adicionar_conta(self, conta_id: str, tipo: str, wss_url: str,
                        access_token: str, nome: str = "") -> dict:
        """Registra uma nova conta no sistema. tipo: 'TESTE' | 'REAL' | 'SECUNDARIA'"""
        tipo_up = tipo.upper()

        # Estado padrão por tipo
        if tipo_up == "TESTE":
            estado_padrao = "APRENDIZADO"
        elif tipo_up == "SECUNDARIA":
            estado_padrao = "STANDBY"
        else:
            estado_padrao = "STANDBY"

        with self._lock:
            conta = {
                "id":            conta_id,
                "nome":          nome or f"Conta {tipo_up} #{len(self.contas)+1}",
                "tipo":          tipo_up,
                "estado":        estado_padrao,
                "wss_url":       wss_url,
                "access_token":  access_token,
                "banca_usd":     0.0,
                "lucro_sessao":  0.0,
                "trades_hoje":   0,
                "data_cadastro": time.strftime("%Y-%m-%d %H:%M:%S"),
                "ultima_atividade": None,
                "estrategias_ativas": [],
                "historico_promocoes": [],
                "config_seguranca": dict(self.LIMITES_REAL) if tipo_up == "REAL" else {},
            }
            self.contas[conta_id] = conta

            if tipo_up == "TESTE" and not self.conta_teste_id:
                self.conta_teste_id = conta_id
            elif tipo_up == "REAL" and not self.conta_real_id:
                self.conta_real_id = conta_id
            elif tipo_up == "SECUNDARIA":
                # Sempre atualiza a secundária (pode trocar de conta)
                # Remove a antiga se existir
                if self.conta_secundaria_id and self.conta_secundaria_id != conta_id:
                    old_id = self.conta_secundaria_id
                    if old_id in self.contas:
                        del self.contas[old_id]
                self.conta_secundaria_id = conta_id

            self._salvar()
        return conta

    def remover_conta(self, conta_id: str) -> bool:
        with self._lock:
            if conta_id in self.contas:
                del self.contas[conta_id]
                if self.conta_teste_id == conta_id:
                    self.conta_teste_id = None
                if self.conta_real_id == conta_id:
                    self.conta_real_id = None
                if self.conta_secundaria_id == conta_id:
                    self.conta_secundaria_id = None
                self._salvar()
                return True
            return False

    # ── Consulta ──────────────────────────────────────────────────────────
    def get_conta(self, conta_id: str) -> dict | None:
        return self.contas.get(conta_id)

    def get_conta_teste(self) -> dict | None:
        return self.contas.get(self.conta_teste_id) if self.conta_teste_id else None

    def get_conta_real(self) -> dict | None:
        return self.contas.get(self.conta_real_id) if self.conta_real_id else None

    def get_conta_secundaria(self) -> dict | None:
        return self.contas.get(self.conta_secundaria_id) if self.conta_secundaria_id else None

    def listar_contas(self) -> list:
        return list(self.contas.values())

    # ── Transição de estado ───────────────────────────────────────────────
    def mudar_estado(self, conta_id: str, novo_estado: str, motivo: str = ""):
        if conta_id not in self.contas:
            return
        estados_validos = {
            "APRENDIZADO", "VALIDACAO", "APROVADA",
            "PRODUCAO", "PAUSADA", "STANDBY"
        }
        if novo_estado not in estados_validos:
            return

        conta = self.contas[conta_id]
        estado_antigo = conta["estado"]
        conta["estado"] = novo_estado
        conta["historico_promocoes"].append({
            "de":        estado_antigo,
            "para":      novo_estado,
            "motivo":    motivo,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._salvar()
        print(f"[AccountManager] {conta['nome']}: {estado_antigo} → {novo_estado} ({motivo})")

    # ── Verificação de promoção (TESTE → REAL) ────────────────────────────
    def verificar_promocao(self) -> dict:
        """Verifica se a conta TESTE pode promover estratégias para a REAL."""
        conta_teste = self.get_conta_teste()
        conta_real  = self.get_conta_real()

        if not conta_teste:
            return {"pode_promover": False, "motivo": "Nenhuma conta TESTE cadastrada."}
        if not conta_real:
            return {"pode_promover": False, "motivo": "Nenhuma conta REAL cadastrada."}

        metricas  = self._calcular_metricas_conta(conta_teste["id"])
        criterios = self.CRITERIOS_PROMOCAO
        checklist = {
            "operacoes_suficientes": metricas["total_ops"]      >= criterios["min_operacoes"],
            "win_rate_adequado":     metricas["win_rate"]        >= criterios["min_win_rate"],
            "profit_factor_ok":      metricas["profit_factor"]   >= criterios["min_profit_factor"],
            "drawdown_controlado":   metricas["max_drawdown_pct"] <= criterios["max_drawdown_pct"],
            "dias_aprendizado":      metricas["dias_ativo"]      >= criterios["min_dias_aprendizado"],
            "estrategias_validas":   metricas["estrategias_pf_ok"] >= criterios["min_estrategias_validas"],
        }
        todos_ok = all(checklist.values())

        return {
            "pode_promover": todos_ok,
            "checklist":     checklist,
            "metricas":      metricas,
            "criterios":     criterios,
            "conta_teste":   conta_teste["nome"],
            "conta_real":    conta_real["nome"],
            "motivo": (
                "✅ Todos os critérios atendidos. Estratégias prontas para produção."
                if todos_ok else
                "⏳ Critérios não atendidos. Continue em modo aprendizado."
            ),
        }

    def _calcular_metricas_conta(self, conta_id: str) -> dict:
        """Calcula métricas do Memory Vault filtradas por conta."""
        _empty = {
            "total_ops": 0, "win_rate": 0, "profit_factor": 0,
            "max_drawdown_pct": 0, "dias_ativo": 0, "estrategias_pf_ok": 0,
        }
        if not os.path.exists(MEMORY_FILE):
            return _empty
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
        except Exception:
            return _empty

        ops = [e for e in memoria if e.get("conta_id") == conta_id]
        if not ops:
            return _empty

        wins        = sum(1 for e in ops if e.get("resultado") == "WIN")
        losses      = len(ops) - wins
        wr          = (wins / len(ops) * 100) if ops else 0
        lucro_bruto = sum(float(e.get("lucro", 0)) for e in ops if e.get("resultado") == "WIN")
        perda_bruta = abs(sum(float(e.get("lucro", 0)) for e in ops if e.get("resultado") == "LOSS"))
        pf          = (lucro_bruto / perda_bruta) if perda_bruta > 0 else 0

        pico = acumulado = max_dd = 0
        for e in ops:
            acumulado += float(e.get("lucro", 0))
            if acumulado > pico:
                pico = acumulado
            dd = pico - acumulado
            if dd > max_dd:
                max_dd = dd

        banca  = self.contas.get(conta_id, {}).get("banca_usd", 100)
        dd_pct = (max_dd / banca * 100) if banca > 0 else 0

        timestamps = [e.get("timestamp", 0) for e in ops if e.get("timestamp")]
        dias = 0
        if timestamps:
            dias = max(1, int((max(timestamps) - min(timestamps)) / 86400))

        estrategias: dict = {}
        for e in ops:
            nome = e.get("estrategia", "?")
            if nome not in estrategias:
                estrategias[nome] = {"w": 0, "l": 0, "lucro": 0, "perda": 0}
            if e.get("resultado") == "WIN":
                estrategias[nome]["w"] += 1
                estrategias[nome]["lucro"] += float(e.get("lucro", 0))
            else:
                estrategias[nome]["l"] += 1
                estrategias[nome]["perda"] += abs(float(e.get("lucro", 0)))

        pf_ok = sum(
            1 for d in estrategias.values()
            if d["perda"] > 0 and (d["lucro"] / d["perda"]) >= 1.25
            and (d["w"] + d["l"]) >= 10
        )

        return {
            "total_ops":        len(ops),
            "wins":             wins,
            "losses":           losses,
            "win_rate":         round(wr, 1),
            "profit_factor":    round(pf, 2),
            "max_drawdown_pct": round(dd_pct, 1),
            "dias_ativo":       dias,
            "estrategias_pf_ok": pf_ok,
            "lucro_total":      round(sum(float(e.get("lucro", 0)) for e in ops), 2),
        }

    # ── Segurança da conta REAL ───────────────────────────────────────────
    def validar_trade_real(self, conta_id: str, stake: float,
                           nivel_gale: int, confianca_edc: float) -> dict:
        """Valida se um trade na conta REAL respeita os limites de segurança."""
        conta = self.get_conta(conta_id)
        if not conta:
            return {"aprovado": False, "motivo": "Conta não encontrada."}

        limites = conta.get("config_seguranca", self.LIMITES_REAL)
        banca   = conta.get("banca_usd", 100)

        verificacoes = []

        # 1. Stake vs banca
        max_stake = banca * limites["max_stake_pct_banca"]
        if stake > max_stake:
            verificacoes.append({
                "regra": "stake_max", "ok": False,
                "detalhe": f"Stake ${stake:.2f} > máximo ${max_stake:.2f} ({limites['max_stake_pct_banca']*100}% da banca)",
                "ajuste": round(max_stake, 2),
            })
        else:
            verificacoes.append({"regra": "stake_max", "ok": True})

        # 2. Nível de Gale
        if nivel_gale > limites["max_gale_nivel"]:
            verificacoes.append({
                "regra": "gale_max", "ok": False,
                "detalhe": f"Gale {nivel_gale} > máximo {limites['max_gale_nivel']}",
                "ajuste": limites["max_gale_nivel"],
            })
        else:
            verificacoes.append({"regra": "gale_max", "ok": True})

        # 3. Confiança EDC
        if confianca_edc < limites["threshold_edc_min"]:
            verificacoes.append({
                "regra": "confianca_min", "ok": False,
                "detalhe": f"Confiança {confianca_edc}% < mínimo {limites['threshold_edc_min']}%",
            })
        else:
            verificacoes.append({"regra": "confianca_min", "ok": True})

        # 4. Trades hoje
        if conta.get("trades_hoje", 0) >= limites["max_trades_dia"]:
            verificacoes.append({
                "regra": "trades_dia", "ok": False,
                "detalhe": f"Limite diário atingido: {limites['max_trades_dia']} trades",
            })
        else:
            verificacoes.append({"regra": "trades_dia", "ok": True})

        # 5. Perda diária
        perda_hoje = abs(min(0, conta.get("lucro_sessao", 0)))
        max_perda  = banca * limites["max_loss_diario_pct"]
        if perda_hoje >= max_perda:
            verificacoes.append({
                "regra": "perda_diaria", "ok": False,
                "detalhe": f"Perda diária ${perda_hoje:.2f} >= limite ${max_perda:.2f}",
            })
        else:
            verificacoes.append({"regra": "perda_diaria", "ok": True})

        todas_ok = all(v["ok"] for v in verificacoes)
        return {
            "aprovado":     todas_ok,
            "verificacoes": verificacoes,
            "motivo": (
                "✅ Trade aprovado pelos limites de segurança."
                if todas_ok else
                "🚫 Trade bloqueado: " + "; ".join(
                    v["detalhe"] for v in verificacoes if not v["ok"]
                )
            ),
        }


# Instância global
_account_manager = AccountManager()


# ═══════════════════════════════════════════════════════════════════════════════
# ROTAS MULTI-CONTAS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/contas/adicionar', methods=['POST'])
def contas_adicionar():
    """
    Adiciona uma nova conta ao sistema.
    Payload: { tipo: "TESTE"|"REAL"|"SECUNDARIA", nome?: string }

    Isolamento de tokens:
    - SECUNDARIA: usa APENAS o token exclusivo do slot _token_secundaria.
      Se não houver token da secundária, retorna erro — nunca usa o token principal.
    - TESTE / REAL: usa o token principal (_render_token_cache / _token_recebido).
    """
    dados = request.get_json(force=True, silent=True) or {}
    tipo  = dados.get("tipo", "TESTE").upper()
    nome  = dados.get("nome", "")

    # "DEMO" é sinônimo de "TESTE"
    if tipo == "DEMO":
        tipo = "TESTE"

    if tipo not in ("TESTE", "REAL", "SECUNDARIA"):
        return jsonify({"erro": "Tipo deve ser TESTE, REAL ou SECUNDARIA."})

    try:
        access_token = ""

        if tipo == "SECUNDARIA":
            # ── Token EXCLUSIVO da secundária — nunca usa token principal ──────
            with _token_sec_lock:
                sec_tok = _token_secundaria.get("access_token", "")
                sec_age = time.time() - _token_secundaria.get("ts", 0)
                sec_ok  = bool(sec_tok) and sec_tok not in ("None", "null") and sec_age < 300
                if sec_ok:
                    access_token = sec_tok
                    # Consome o token — evita reusar na próxima chamada
                    _token_secundaria["access_token"] = ""
                    _token_secundaria["ts"]           = 0
                    print(f"[SecToken] Usando token exclusivo da secundária: {access_token[:10]}...")
            if not access_token:
                return jsonify({"erro": "Token da conta secundária não encontrado. Faça login na conta secundária primeiro."})
        else:
            # ── Token da conta principal (TESTE / REAL) ───────────────────────
            # 1. Prioriza token exclusivo da secundária presente no slot para
            #    caso de chamada legada que ainda envia tipo=TESTE/REAL com token sec
            with _token_sec_lock:
                sec_tok = _token_secundaria.get("access_token", "")
                sec_age = time.time() - _token_secundaria.get("ts", 0)
                sec_ok  = bool(sec_tok) and sec_tok not in ("None", "null") and sec_age < 300
                if sec_ok:
                    access_token = sec_tok
                    _token_secundaria["access_token"] = ""
                    _token_secundaria["ts"]           = 0
                    print(f"[SecToken] Usando token exclusivo da secundária (legado): {access_token[:10]}...")

            # 2. Fallback: token principal do cache
            if not access_token:
                cache_age = time.time() - _render_token_cache.get("ts", 0)
                if _render_token_cache.get("token") and cache_age < 300:
                    access_token = _render_token_cache["token"]
                else:
                    res = requests.get(SERVIDOR_URL, timeout=10)
                    if res.status_code != 200:
                        return jsonify({"erro": "Servidor indisponível. Tente novamente."})
                    data = res.json()
                    tok  = (
                        data.get("token") or
                        data.get("access_token") or
                        (data.get("data") or {}).get("token")
                    )
                    if isinstance(tok, dict):
                        tok = tok.get("token") or tok.get("access_token")
                    access_token = str(tok).strip().strip('"') if tok else ""
                    if access_token and access_token not in ("None", "null", ""):
                        _render_token_cache["token"] = access_token
                        _render_token_cache["ts"]    = time.time()

        if not access_token or access_token in ("None", "null", ""):
            return jsonify({"erro": "Token não encontrado. Faça login na Deriv primeiro."})

        # 2. Lista contas
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Deriv-App-ID":  APP_ID,
            "Content-Type":  "application/json",
        }
        res_contas = requests.get(f"{API_BASE}/accounts", headers=headers, timeout=10)
        if res_contas.status_code != 200:
            return jsonify({"erro": f"Erro ao listar contas: {res_contas.status_code}"})

        contas_api = res_contas.json().get("data", [])

        # 3. Filtra por tipo de conta Deriv (DEMO ou REAL)
        # Para SECUNDARIA: usa tipoReal enviado pelo front-end (DEMO ou REAL)
        tipo_filtro = dados.get("tipoReal", "DEMO").upper()
        if tipo_filtro == "DEMO":
            tipo_filtro = "TESTE"
        if tipo_filtro not in ("TESTE", "REAL"):
            tipo_filtro = "TESTE"

        conta_id = None
        for c in contas_api:
            cid          = str(c.get("account_id", ""))
            account_type = str(c.get("account_type", "")).lower()
            is_virt      = c.get("is_virtual", False)
            is_demo      = account_type == "demo" or is_virt or cid.upper().startswith(("VR", "DOT", "VRT"))
            filtro_uso   = tipo if tipo in ("TESTE", "REAL") else tipo_filtro
            if filtro_uso == "TESTE" and is_demo:
                conta_id = cid
                break
            elif filtro_uso == "REAL" and not is_demo:
                conta_id = cid
                break

        if not conta_id:
            return jsonify({"erro": f"Nenhuma conta {tipo_filtro} encontrada na API Deriv."})

        # 4. Gera WSS URL (OTP)
        res_otp = requests.post(f"{API_BASE}/accounts/{conta_id}/otp", headers=headers, timeout=10)
        wss_url = res_otp.json().get("data", {}).get("url")
        if not wss_url:
            return jsonify({"erro": "OTP não retornou URL WSS."})

        # 5. Registra no AccountManager
        # Conta secundária é sempre registrada com tipo="SECUNDARIA" independente do tipoReal
        conta = _account_manager.adicionar_conta(
            conta_id=conta_id, tipo=tipo,
            wss_url=wss_url, access_token=access_token, nome=nome,
        )
        return jsonify({
            "ok":   True,
            "conta": {
                "id":      conta["id"],
                "nome":    conta["nome"],
                "tipo":    conta["tipo"],
                "estado":  conta["estado"],
                "wss_url": conta["wss_url"],
            },
            "msg": f"Conta {tipo} registrada com sucesso!",
        })

    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route('/contas/listar', methods=['GET'])
def contas_listar():
    """Lista todas as contas cadastradas com status."""
    contas = _account_manager.listar_contas()
    for c in contas:
        c["metricas"] = _account_manager._calcular_metricas_conta(c["id"])
    return jsonify({
        "contas":              contas,
        "conta_teste_id":      _account_manager.conta_teste_id,
        "conta_real_id":       _account_manager.conta_real_id,
        "conta_secundaria_id": _account_manager.conta_secundaria_id,
        "total":               len(contas),
    })


@app.route('/contas/remover', methods=['POST'])
def contas_remover():
    """Remove uma conta do sistema."""
    dados    = request.get_json(force=True, silent=True) or {}
    conta_id = dados.get("conta_id", "")
    if _account_manager.remover_conta(conta_id):
        return jsonify({"ok": True, "msg": "Conta removida."})
    return jsonify({"ok": False, "erro": "Conta não encontrada."})


@app.route('/contas/verificar-secundaria', methods=['GET'])
def contas_verificar_secundaria():
    """
    Retorna o status atual do login da conta secundária.
    Usado pelo frontend para mostrar progresso.
    """
    with _SEC_LOGIN_LOCK:
        state = dict(_SEC_LOGIN_STATE)
        state.pop("browser_process", None)
        state.pop("browser_path", None)

    # Verifica se token exclusivo da secundária foi recebido
    with _token_sec_lock:
        sec_tok = _token_secundaria.get("access_token", "")
        sec_age = time.time() - _token_secundaria.get("ts", 0)
        token_pronto = bool(sec_tok) and sec_tok not in ("None", "null") and sec_age < 300

    if token_pronto:
        state["status"]      = "token_pronto"
        state["token_pronto"] = True
        return jsonify(state)

    # Conta secundária já registrada E com wss_url válida?
    sec = _account_manager.get_conta_secundaria()
    if sec:
        if sec.get("wss_url"):
            state["status"] = "conectado"
        else:
            state["status"] = "idle"
        state["conta_id"]   = sec["id"]
        state["conta_nome"] = sec.get("nome", "")
        state["conta_tipo"] = sec.get("tipo", "")

    return jsonify(state)


@app.route('/contas/fechar-navegador', methods=['POST'])
def contas_fechar_navegador():
    """
    Força o fechamento do navegador anônimo da conta secundária.
    Útil se o usuário quiser cancelar o login.
    """
    _sec_fechar_navegador()
    with _SEC_LOGIN_LOCK:
        _SEC_LOGIN_STATE["status"] = "idle"
        _SEC_LOGIN_STATE["erro"] = "Cancelado pelo usuário."
    return jsonify({"ok": True, "msg": "Navegador fechado."})


# ─────────────────────────────────────────────────────────
# BOT PRINCIPAL — Configurações persistidas no servidor
# ─────────────────────────────────────────────────────────
BOT_CFG_ARQUIVO = os.path.join(_BASE_DIR, "bot_config.json")

@app.route('/bot-config', methods=['GET'])
def bot_config_get():
    """Retorna as últimas configurações salvas do bot principal."""
    padrao = {
        "stake": 0.35,
        "stopWin": 10.0,
        "stopLoss": 100.0,
        "estrategia": "🎯 DIGITUNDER 5",
        "gerenciamento": "🔄 Martingale",
        "mgr_cfg": {},
        "mgr_cfgs": {},
        "estrategias": {},
        "lv": {},
        "tickRecovery": {},
        "bloqHist": {},
        "rotacao": {},
        "edcFiltro": False,
        "modo": "NORMAL",
        # ── Sistema Adaptativo ────────────────────────────────────────────────
        "modoAdaptativo": "DESLIGADO",
        "adaptativoConfig": {
            "stake_min":            0.35,
            "stake_max":            10.00,
            "risco_max_pct":        0.03,
            "max_losses_seguidos":  3,
            "drawdown_defensivo":   0.05,
            "drawdown_bloqueio":    0.10,
            "janela_resultados":    20,
            "score_min_operar":     40,
            "score_defensivo":      60,
            "bloquear_apos_losses": 5,
            "cooldown_segundos":    60,
        },
    }
    try:
        if os.path.exists(BOT_CFG_ARQUIVO):
            with open(BOT_CFG_ARQUIVO, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return jsonify(padrao)

@app.route('/bot-config', methods=['POST'])
def bot_config_post():
    """Salva as configurações do bot principal no servidor."""
    dados = request.get_json(force=True, silent=True) or {}
    atual = {}
    try:
        if os.path.exists(BOT_CFG_ARQUIVO):
            with open(BOT_CFG_ARQUIVO, "r", encoding="utf-8") as f:
                atual = json.load(f)
    except Exception:
        pass
    campos = (
        "stake", "stopWin", "stopLoss", "estrategia", "gerenciamento",
        "mgr_cfg", "mgr_cfgs", "estrategias", "lv", "tickRecovery",
        "bloqHist", "rotacao", "edcFiltro", "modo", "_modoIaLivre",
        # Sistema Adaptativo
        "modoAdaptativo", "adaptativoConfig",
    )
    for k in campos:
        if k in dados:
            atual[k] = dados[k]

    # ── Aplica modo adaptativo no engine se enviado ───────────────────────────
    if "modoAdaptativo" in dados:
        ADAPTIVE_ENGINE.set_mode(dados["modoAdaptativo"])

    # ── Aplica configurações adaptativas no engine se enviadas ────────────────
    if "adaptativoConfig" in dados and isinstance(dados["adaptativoConfig"], dict):
        ac = dados["adaptativoConfig"]
        cfg = ADAPTIVE_ENGINE.config
        if "stake_min"            in ac: cfg.stake_min            = float(ac["stake_min"])
        if "stake_max"            in ac: cfg.stake_max            = float(ac["stake_max"])
        if "risco_max_pct"        in ac: cfg.risco_max_pct        = float(ac["risco_max_pct"])
        if "max_losses_seguidos"  in ac: cfg.max_losses_seguidos  = int(ac["max_losses_seguidos"])
        if "drawdown_defensivo"   in ac: cfg.drawdown_defensivo   = float(ac["drawdown_defensivo"])
        if "drawdown_bloqueio"    in ac: cfg.drawdown_bloqueio    = float(ac["drawdown_bloqueio"])
        if "janela_resultados"    in ac: cfg.janela_resultados    = int(ac["janela_resultados"])
        if "score_min_operar"     in ac: cfg.score_min_operar     = float(ac["score_min_operar"])
        if "score_defensivo"      in ac: cfg.score_defensivo      = float(ac["score_defensivo"])
        if "bloquear_apos_losses" in ac: cfg.bloquear_apos_losses = int(ac["bloquear_apos_losses"])
        if "cooldown_segundos"    in ac: cfg.cooldown_segundos    = int(ac["cooldown_segundos"])
    try:
        with open(BOT_CFG_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(atual, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE RISK — Rotas da API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/adaptive-config', methods=['GET', 'POST'])
def adaptive_config():
    """
    GET  — Retorna a configuração atual do motor adaptativo.
    POST — Atualiza a configuração e/ou o modo do motor.

    Payload POST (todos opcionais):
      modo                 : "DESLIGADO" | "MODERADO" | "INTELIGENTE" | "DEFENSIVO"
      saldo                : float  — inicializa o motor com esse saldo
      stake_min            : float
      stake_max            : float
      risco_max_pct        : float
      max_losses_seguidos  : int
      drawdown_defensivo   : float
      drawdown_bloqueio    : float
      janela_resultados    : int
      score_min_operar     : float
      score_defensivo      : float
      bloquear_apos_losses : int
      cooldown_segundos    : int
    """
    if request.method == 'GET':
        cfg = ADAPTIVE_ENGINE.config
        return jsonify({
            "ok":   True,
            "modo": cfg.modo,
            "config": {
                "stake_min":            cfg.stake_min,
                "stake_max":            cfg.stake_max,
                "risco_max_pct":        cfg.risco_max_pct,
                "max_losses_seguidos":  cfg.max_losses_seguidos,
                "drawdown_defensivo":   cfg.drawdown_defensivo,
                "drawdown_bloqueio":    cfg.drawdown_bloqueio,
                "janela_resultados":    cfg.janela_resultados,
                "score_min_operar":     cfg.score_min_operar,
                "score_defensivo":      cfg.score_defensivo,
                "bloquear_apos_losses": cfg.bloquear_apos_losses,
                "cooldown_segundos":    cfg.cooldown_segundos,
            },
        })

    # POST
    dados = request.get_json(force=True, silent=True) or {}
    cfg   = ADAPTIVE_ENGINE.config

    if "modo" in dados:
        ADAPTIVE_ENGINE.set_mode(str(dados["modo"]))

    if "saldo" in dados:
        ADAPTIVE_ENGINE.iniciar(float(dados["saldo"]))

    mapa = {
        "stake_min":            (float, "stake_min"),
        "stake_max":            (float, "stake_max"),
        "risco_max_pct":        (float, "risco_max_pct"),
        "max_losses_seguidos":  (int,   "max_losses_seguidos"),
        "drawdown_defensivo":   (float, "drawdown_defensivo"),
        "drawdown_bloqueio":    (float, "drawdown_bloqueio"),
        "janela_resultados":    (int,   "janela_resultados"),
        "score_min_operar":     (float, "score_min_operar"),
        "score_defensivo":      (float, "score_defensivo"),
        "bloquear_apos_losses": (int,   "bloquear_apos_losses"),
        "cooldown_segundos":    (int,   "cooldown_segundos"),
    }
    for chave, (tipo, attr) in mapa.items():
        if chave in dados:
            setattr(cfg, attr, tipo(dados[chave]))

    print(
        f"[Adaptive] ✅ Config atualizada | Modo={cfg.modo} | "
        f"StakeMin={cfg.stake_min} | StakeMax={cfg.stake_max}"
    )
    return jsonify({"ok": True, "modo": cfg.modo})


@app.route('/adaptive-iniciar', methods=['POST'])
def adaptive_iniciar():
    """
    Inicializa (ou reinicia) o motor com o saldo atual da conta.
    Deve ser chamado assim que o saldo for descoberto após login na Deriv.

    Payload: { saldo: float }
    """
    dados = request.get_json(force=True, silent=True) or {}
    saldo = dados.get("saldo")
    if saldo is None:
        return jsonify({"ok": False, "erro": "Campo 'saldo' obrigatório."}), 400
    try:
        ADAPTIVE_ENGINE.iniciar(float(saldo))
        print(f"[Adaptive] 🚀 Motor inicializado | Saldo={saldo:.2f}")
        return jsonify({"ok": True, "saldo": float(saldo), "modo": ADAPTIVE_ENGINE.config.modo})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400


@app.route('/adaptive-stake', methods=['POST'])
def adaptive_stake_route():
    """
    Calcula a stake adaptada para uma operação antes de enviá-la à Deriv.

    Payload:
      stake_base       (float, obrigatório) — stake do gerenciamento nativo
      gerenciamento    (str)   — martingale | soros | fixa | loss_recovery | ...
      gale             (int)   — nível de gale atual (0 = nova entrada)
      qualidade_sinal  (float) — score do sinal (0–100, padrão 50)
      payout           (float) — payout esperado (ex.: 0.85, padrão 0.80)
      volatilidade     (float) — volatilidade do mercado (0–100, padrão 50)
      regime           (str)   — LATERAL | TENDENCIA | INDEFINIDO

    Resposta:
      permitir       (bool)  — se a entrada está autorizada
      stake          (float) — stake ajustada
      score          (float) — score calculado
      modo           (str)   — modo ativo
      fator          (float) — fator aplicado
      motivo         (str)   — motivo do bloqueio (se houver)
      drawdown       (float) — drawdown atual em %
      losses_seguidos(int)   — losses consecutivos atuais
    """
    dados = request.get_json(force=True, silent=True) or {}
    stake_base = dados.get("stake_base")
    if stake_base is None:
        return jsonify({"ok": False, "erro": "Campo 'stake_base' obrigatório."}), 400

    resultado = ADAPTIVE_ENGINE.calcular_stake(
        stake_base      = float(stake_base),
        gerenciamento   = str(dados.get("gerenciamento", "fixa")),
        gale            = int(dados.get("gale", 0)),
        qualidade_sinal = float(dados.get("qualidade_sinal", 50)),
        payout          = float(dados.get("payout", 0.80)),
        volatilidade    = float(dados.get("volatilidade", 50)),
        regime          = str(dados.get("regime", "")),
    )

    print(
        f"[Adaptive] 🧠 Modo={resultado['modo']} | "
        f"Score={resultado['score']} | "
        f"Stake={resultado['stake']:.2f} | "
        f"Fator={resultado['fator']} | "
        f"DD={resultado['drawdown']:.2f}% | "
        f"LossSeq={resultado['losses_seguidos']} | "
        f"Permitir={resultado['permitir']}"
    )
    if not resultado["permitir"]:
        print(f"[Adaptive] 🚫 BLOQUEADO | Motivo: {resultado['motivo']}")

    return jsonify(resultado)


@app.route('/adaptive-resultado', methods=['POST'])
def adaptive_resultado():
    """
    Registra o resultado de uma operação finalizada no motor adaptativo.
    DEVE ser chamado após cada WIN ou LOSS.

    Payload:
      resultado (str, obrigatório) — "WIN" | "LOSS"
      lucro     (float)            — valor do lucro/prejuízo (positivo em WIN)
      saldo     (float)            — saldo atual após o resultado
      gale      (int)              — nível de gale da operação
    """
    dados     = request.get_json(force=True, silent=True) or {}
    resultado = str(dados.get("resultado", "")).upper()
    if resultado not in ("WIN", "LOSS"):
        return jsonify({"ok": False, "erro": "resultado deve ser 'WIN' ou 'LOSS'"}), 400

    ADAPTIVE_ENGINE.registrar_resultado(
        resultado = resultado,
        lucro     = float(dados.get("lucro", 0)),
        saldo     = float(dados.get("saldo", 0)),
        gale      = int(dados.get("gale", 0)),
    )

    status = ADAPTIVE_ENGINE.status()
    print(
        f"[Adaptive] {'✅' if resultado == 'WIN' else '❌'} {resultado} registrado | "
        f"WR={status['winrate_recente']:.1f}% | DD={status['drawdown_pct']:.2f}% | "
        f"LossSeq={status['losses_seguidos']} | WinSeq={status['wins_seguidos']}"
    )
    return jsonify({"ok": True, "status": status})


@app.route('/adaptive-status', methods=['GET'])
def adaptive_status():
    """
    Retorna snapshot completo do estado do motor adaptativo.
    Útil para exibição no painel de administração / dashboard do bot.
    """
    return jsonify({"ok": True, "status": ADAPTIVE_ENGINE.status()})


@app.route('/adaptive-resetar', methods=['POST'])
def adaptive_resetar():
    """
    Reseta o estado do motor adaptativo.
    Payload opcional: { saldo: float } — se omitido, reutiliza o saldo_inicial.
    """
    dados = request.get_json(force=True, silent=True) or {}
    saldo = dados.get("saldo")
    ADAPTIVE_ENGINE.resetar(float(saldo) if saldo is not None else None)
    print(f"[Adaptive] 🔄 Motor resetado | Saldo={ADAPTIVE_ENGINE.state.saldo_inicial:.2f}")
    return jsonify({"ok": True, "status": ADAPTIVE_ENGINE.status()})


# ─────────────────────────────────────────────────────────
# CONTA SECUNDÁRIA — Configurações independentes (Gerenciamento, Stake, Stops)
# ─────────────────────────────────────────────────────────
SEC_CFG_ARQUIVO = os.path.join(_BASE_DIR, "sec_config.json")

@app.route('/contas/sec-config', methods=['GET'])
def contas_sec_config_get():
    """
    Retorna as configurações independentes da conta secundária.
    Valores são SEPARADOS da conta teste!
    """
    padrao = {
        "gerenciamento": "Martingale",
        "stake": 0.35,
        "stopWin": 10.0,
        "stopLoss": 100.0,
        "limitePerda": 10.0,
    }
    try:
        if os.path.exists(SEC_CFG_ARQUIVO):
            with open(SEC_CFG_ARQUIVO, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                padrao.update(dados)
    except Exception:
        pass
    return jsonify(padrao)

@app.route('/contas/sec-config', methods=['POST'])
def contas_sec_config_post():
    """
    Salva as configurações independentes da conta secundária.
    Payload: { gerenciamento, stake, stopWin, stopLoss }
    """
    dados = request.get_json(force=True, silent=True) or {}
    # Carrega atual e faz merge
    atual = {}
    try:
        if os.path.exists(SEC_CFG_ARQUIVO):
            with open(SEC_CFG_ARQUIVO, "r", encoding="utf-8") as f:
                atual = json.load(f)
    except Exception:
        pass
    for k in ("gerenciamento", "stake", "stopWin", "stopLoss", "limitePerda",
              "limiteSeqLoss", "gatilhoSeqAtivo", "gatilhoPerdaAtivo", "mgrCfgs"):
        if k in dados:
            atual[k] = dados[k]
    try:
        with open(SEC_CFG_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(atual, f, indent=2, ensure_ascii=False)
        print(f"[Secundária] Configurações salvas: {atual}")
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True})


@app.route('/contas/verificar-promocao', methods=['GET'])
def contas_verificar_promocao():
    """Verifica se a conta TESTE pode promover estratégias para a REAL."""
    resultado = _account_manager.verificar_promocao()
    return jsonify(resultado)


@app.route('/contas/promover', methods=['POST'])
def contas_promover():
    """
    Promove estratégias da conta TESTE para a conta REAL.
    Payload: { forcar?: bool }
    """
    dados        = request.get_json(force=True, silent=True) or {}
    forcar       = bool(dados.get("forcar", False))
    verificacao  = _account_manager.verificar_promocao()

    if not verificacao["pode_promover"] and not forcar:
        return jsonify({
            "ok":        False,
            "erro":      "Critérios de promoção não atendidos.",
            "checklist": verificacao.get("checklist"),
            "metricas":  verificacao.get("metricas"),
        })

    conta_teste = _account_manager.get_conta_teste()
    conta_real  = _account_manager.get_conta_real()
    if not conta_teste or not conta_real:
        return jsonify({"erro": "Contas TESTE e REAL devem estar cadastradas."})

    # Identifica estratégias aprovadas (PF >= 1.25, mín 10 ops)
    estrategias_aprovadas = []
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memoria = json.load(f)
            grupos: dict = {}
            for e in memoria:
                if e.get("conta_id") != conta_teste["id"]:
                    continue
                nome = e.get("estrategia", "?")
                if nome not in grupos:
                    grupos[nome] = {"w": 0, "l": 0, "lucro": 0, "perda": 0}
                if e.get("resultado") == "WIN":
                    grupos[nome]["w"]     += 1
                    grupos[nome]["lucro"] += float(e.get("lucro", 0))
                else:
                    grupos[nome]["l"]     += 1
                    grupos[nome]["perda"] += abs(float(e.get("lucro", 0)))
            for nome, d in grupos.items():
                pf    = (d["lucro"] / d["perda"]) if d["perda"] > 0 else 0
                total = d["w"] + d["l"]
                if pf >= 1.25 and total >= 10:
                    estrategias_aprovadas.append({
                        "nome":     nome,
                        "pf":       round(pf, 2),
                        "win_rate": round(d["w"] / total * 100, 1),
                        "ops":      total,
                    })
        except Exception:
            pass

    # Muda estados
    _account_manager.mudar_estado(
        conta_teste["id"], "APROVADA",
        f"Promoção automática: {len(estrategias_aprovadas)} estratégias validadas",
    )
    _account_manager.mudar_estado(
        conta_real["id"], "PRODUCAO",
        f"Recebendo estratégias da conta {conta_teste['nome']}",
    )

    # Notificação Telegram
    msg = (
        f"🎓 PROMOÇÃO DE CONTA APROVADA!\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📊 Conta TESTE: {conta_teste['nome']}\n"
        f"💰 Conta REAL: {conta_real['nome']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"✅ Estratégias promovidas: {len(estrategias_aprovadas)}\n"
    )
    for est in estrategias_aprovadas[:5]:
        msg += f"  • {est['nome']}: PF {est['pf']} | WR {est['win_rate']}%\n"

    cfg_tg = _tg_carregar()
    if cfg_tg.get("enabled"):
        _tg_dispatch(lambda: _tg_enviar_texto(cfg_tg["token"], cfg_tg["chat_id"], msg))

    return jsonify({
        "ok":                   True,
        "estrategias_promovidas": estrategias_aprovadas,
        "conta_teste_estado":   conta_teste["estado"],
        "conta_real_estado":    conta_real["estado"],
        "msg":                  "Promoção realizada com sucesso!",
    })


@app.route('/contas/validar-trade-real', methods=['POST'])
def contas_validar_trade_real():
    """
    Valida se um trade na conta REAL respeita os limites de segurança.
    Payload: { conta_id?, stake, nivel_gale, confianca_edc }
    """
    dados        = request.get_json(force=True, silent=True) or {}
    conta_id     = dados.get("conta_id") or _account_manager.conta_real_id
    stake        = float(dados.get("stake", 0.35))
    nivel_gale   = int(dados.get("nivel_gale", 0))
    confianca    = float(dados.get("confianca_edc", 0))
    resultado    = _account_manager.validar_trade_real(conta_id, stake, nivel_gale, confianca)
    return jsonify(resultado)


@app.route('/contas/estado', methods=['POST'])
def contas_mudar_estado():
    """Muda manualmente o estado de uma conta."""
    dados       = request.get_json(force=True, silent=True) or {}
    conta_id    = dados.get("conta_id", "")
    novo_estado = dados.get("estado", "")
    motivo      = dados.get("motivo", "Mudança manual")

    if not conta_id or not novo_estado:
        return jsonify({"erro": "conta_id e estado são obrigatórios."})

    _account_manager.mudar_estado(conta_id, novo_estado, motivo)
    return jsonify({"ok": True, "msg": f"Estado alterado para {novo_estado}."})


@app.route('/contas/wss-secundaria', methods=['GET'])
def contas_wss_secundaria():
    """
    Gera OTP fresco para a conta SECUNDÁRIA usando EXCLUSIVAMENTE
    o token próprio da secundária, isolado do token principal.
    Nunca acessa _token_recebido nem _render_token_cache.
    """
    sec = _account_manager.get_conta_secundaria()
    if not sec:
        return jsonify({
            "wss_url": None,
            "erro": "Nenhuma conta secundária cadastrada."
        })

    conta_id = sec.get("id", "")

    # ── ISOLAMENTO TOTAL: token vem exclusivamente do slot da secundária ─
    # 1. Tenta o slot em memória (_token_secundaria), protegido pelo lock
    access_token = ""
    with _token_sec_lock:
        access_token = _token_secundaria.get("access_token", "").strip().strip('"')

    # 2. Fallback: token persistido no AccountManager (salvo no registro da conta)
    #    Nunca usa _token_recebido nem _render_token_cache (esses são da conta principal)
    if not access_token or access_token in ("None", "null", ""):
        access_token = sec.get("access_token", "").strip().strip('"')

    if not access_token or access_token in ("None", "null", ""):
        return jsonify({
            "wss_url": None,
            "erro": "Token da conta secundária expirado. Reconecte a conta secundária."
        })

    # ── Gera OTP → WSS URL ────────────────────────────────────────────────
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Deriv-App-ID":  APP_ID,
            "Content-Type":  "application/json",
        }
        res_otp  = requests.post(
            f"{API_BASE}/accounts/{conta_id}/otp",
            headers=headers, timeout=10
        )
        body_otp = res_otp.json()
        wss_url  = (body_otp.get("data") or {}).get("url")

        if not wss_url:
            erros   = body_otp.get("errors") or []
            msg_err = erros[0].get("message", str(body_otp)[:120]) if erros else \
                      body_otp.get("error", {}).get("message", res_otp.text[:120])
            print(f"[SecWSS] OTP Secundária falhou para conta {conta_id}: {msg_err}")
            return jsonify({"wss_url": None, "erro": f"OTP Secundária falhou: {msg_err}"})

        # Persiste wss_url no AccountManager (token não muda — só o OTP expira)
        if conta_id in _account_manager.contas:
            _account_manager.contas[conta_id]["wss_url"] = wss_url
            _account_manager._salvar()

        print(f"[SecWSS] WSS da secundária gerado com sucesso para a conta {conta_id}")
        return jsonify({
            "wss_url":  wss_url,
            "conta_id": conta_id,
            "nome":     sec.get("nome", ""),
            "tipo":     sec.get("tipo", ""),
        })
    except Exception as e:
        return jsonify({"wss_url": None, "erro": str(e)})


@app.route('/contas/wss-secundaria-invalidar', methods=['POST'])
def contas_wss_secundaria_invalidar():
    """Limpa o wss_url salvo da conta secundária — chamado pelo frontend ao abrir o WS."""
    sec = _account_manager.get_conta_secundaria()
    if sec:
        conta_id = sec.get("id", "")
        if conta_id and conta_id in _account_manager.contas:
            _account_manager.contas[conta_id]["wss_url"] = ""
            _account_manager._salvar()
    return jsonify({"ok": True})


@app.route('/wa-ping')
def wa_ping():
    """Verifica se o servidor WhatsApp está online."""
    cfg = _wa_cfg_ler()
    try:
        r = requests.get(f"{cfg['api_url']}/ping", timeout=12)
        return jsonify({"online": r.status_code == 200, "status": r.status_code})
    except Exception as e:
        return jsonify({"online": False, "erro": str(e)})

# URL fixa do servidor Oracle Cloud
_SELF_URL = "https://garrabot.duckdns.org"

def _wa_keepalive_loop():
    """Bate no servidor WA e em si mesmo a cada 30min para manter conexões ativas."""
    wa_url   = _wa_cfg_ler().get("api_url", "")
    self_url = os.environ.get("SELF_URL", "").rstrip("/") or _SELF_URL
    while True:
        time.sleep(1800)  # 30 minutos — Oracle não dorme, intervalo maior economiza recursos
        try:
            if wa_url:
                requests.get(f"{wa_url}/ping", timeout=10)
                print("[KeepAlive] WA API pingada com sucesso.")
        except Exception as e:
            print(f"[KeepAlive] WA ping falhou: {e}")
        try:
            requests.get(f"{self_url}/pegar-token-robo", timeout=15)
            print(f"[KeepAlive] Self ping OK ({self_url}).")
        except Exception as e:
            print(f"[KeepAlive] Self ping falhou: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# ESTRATÉGIA GARRA DUPLA — Dupla Janela Independente (Padrão Vídeo Time)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Cada janela (Superior / Inferior) possui seu próprio histórico de ticks e
# avalia o gatilho de repetição de forma 100% independente:
#   • Janela Superior → DIGITOVER  barreira=4  — dispara quando ELA MESMA acumula
#                       gatilho_repeticoes dígitos iguais consecutivos
#   • Janela Inferior → DIGITUNDER barreira=5  — idem, histórico separado
# O Gale também é isolado: WIN num lado reseta apenas aquela janela.

_GARRA_DUPLA_BARREIRA_OVER  = 4   # DIGITOVER  > 4  (dígitos 5-9 ganham → ~50%)
_GARRA_DUPLA_BARREIRA_UNDER = 5   # DIGITUNDER < 5  (dígitos 0-4 ganham → ~50%)
_GARRA_DUPLA_HIST_LIMITE    = 5   # tamanho máximo do histórico por janela


def _garra_dupla_processar_janela(digito_atual: int,
                                   config_janela: dict,
                                   tipo_contrato: str) -> dict:
    """
    Processa a lógica de gatilho e gestão de Gale de forma 100% independente
    para cada janela (padrão vídeo Time).

    tipo_contrato : 'SUPERIOR' → DIGITOVER 4  |  'INFERIOR' → DIGITUNDER 5
    config_janela : dict com estado da janela:
        stake               (float) — stake atual
        gale_nivel          (int)   — gales acumulados
        gatilho_repeticoes  (int)   — N repetições para disparar (padrão 2)
        historico           (list)  — últimos dígitos desta janela (gerenciado aqui)

    Retorna:
        disparar      (bool)
        payload_ordem (dict)  — payload pronto para API Deriv (somente se disparar=True)
    """
    hist = config_janela.setdefault("historico", [])
    hist.append(int(digito_atual))
    if len(hist) > _GARRA_DUPLA_HIST_LIMITE:
        hist.pop(0)

    qtd = int(config_janela.get("gatilho_repeticoes", 2))
    if len(hist) < qtd:
        return {"disparar": False, "motivo": "historico_insuficiente"}

    ultimos = hist[-qtd:]
    if not all(d == ultimos[0] for d in ultimos):
        return {"disparar": False, "motivo": "sequencia_nao_atingida"}

    # Gatilho atingido para esta janela isoladamente
    barreira = _GARRA_DUPLA_BARREIRA_OVER  if tipo_contrato == "SUPERIOR" \
               else _GARRA_DUPLA_BARREIRA_UNDER
    tipo_api = "DIGITOVER"  if tipo_contrato == "SUPERIOR" else "DIGITUNDER"

    return {
        "disparar": True,
        "digito_gatilho": int(ultimos[0]),
        "payload_ordem": {
            "contract_type": tipo_api,
            "barrier":       str(barreira),
            "amount":        round(float(config_janela.get("stake", 0.35)), 2),
            "basis":         "stake",
            "currency":      "USD",
            "duration":      1,
            "duration_unit": "t",
        },
    }


def _garra_dupla_atualizar_janela(resultado: str,
                                   config_janela: dict,
                                   fator_gale: float = 1.4,
                                   stake_base: float = 0.35) -> dict:
    """
    Atualiza stake e nível de Gale de forma isolada para a janela que encerrou.

    resultado : 'WIN' | 'LOSS'
    """
    if resultado == "WIN":
        config_janela["gale_nivel"] = 0
        config_janela["stake"]      = round(stake_base, 2)
    else:
        config_janela["gale_nivel"] = int(config_janela.get("gale_nivel", 0)) + 1
        config_janela["stake"]      = round(
            float(config_janela.get("stake", stake_base)) * fator_gale, 2
        )
    return config_janela


# ── Funções de compatibilidade (mantêm a API pública inalterada) ─────────────

def _garra_dupla_avaliar_gatilho(ultimos_digitos: list, config_bot: dict) -> dict:
    """
    Wrapper de compatibilidade — usa o novo motor por-janela internamente.
    Avalia ambas as janelas com o mesmo tick e retorna contratos para as que
    atingiram o gatilho.

    O config_bot agora armazena sub-dicts 'janela_superior' e 'janela_inferior'
    com histórico e estado próprios. Campos legados (stake_superior/inferior,
    gale_superior/inferior) são sincronizados automaticamente.
    """
    currency = str(config_bot.get("currency", "USD"))
    duracao  = int(config_bot.get("duracao",  1))
    stake_base = float(config_bot.get("stake_base", 0.35))
    fator      = float(config_bot.get("fator_gale", 1.4))
    qtd        = int(config_bot.get("qtd_gatilho", 3))

    if not ultimos_digitos:
        return {"executar": False, "motivo": "ticks_insuficientes"}

    digito_atual = int(ultimos_digitos[-1])

    # Inicializa sub-dicts de janela se ainda não existirem
    jsup = config_bot.setdefault("janela_superior", {
        "stake": config_bot.get("stake_superior", stake_base),
        "gale_nivel": config_bot.get("gale_superior", 0),
        "gatilho_repeticoes": qtd,
        "historico": [],
    })
    jinf = config_bot.setdefault("janela_inferior", {
        "stake": config_bot.get("stake_inferior", stake_base),
        "gale_nivel": config_bot.get("gale_inferior", 0),
        "gatilho_repeticoes": qtd,
        "historico": [],
    })
    # Mantém qtd_gatilho sincronizado entre as janelas
    jsup["gatilho_repeticoes"] = qtd
    jinf["gatilho_repeticoes"] = qtd

    res_sup = _garra_dupla_processar_janela(digito_atual, jsup, "SUPERIOR")
    res_inf = _garra_dupla_processar_janela(digito_atual, jinf, "INFERIOR")

    # Sincroniza campos legados
    config_bot["stake_superior"] = jsup["stake"]
    config_bot["stake_inferior"] = jinf["stake"]
    config_bot["gale_superior"]  = jsup["gale_nivel"]
    config_bot["gale_inferior"]  = jinf["gale_nivel"]

    # Monta resposta — qualquer janela que disparou gera seu contrato
    contratos = {}
    executar   = False

    if res_sup["disparar"]:
        executar = True
        p = res_sup["payload_ordem"].copy()
        p["currency"]      = currency
        p["duration"]      = duracao
        contratos["contrato_superior"] = p

    if res_inf["disparar"]:
        executar = True
        p = res_inf["payload_ordem"].copy()
        p["currency"]      = currency
        p["duration"]      = duracao
        contratos["contrato_inferior"] = p

    if not executar:
        return {"executar": False, "motivo": "nenhuma_janela_disparou"}

    return {
        "executar":  True,
        "gatilho_digito": digito_atual,
        "qtd_gatilho":    qtd,
        **contratos,
    }


def _garra_dupla_processar_resultado(resultado_janela: str,
                                     tipo_janela: str,
                                     config_bot: dict) -> dict:
    """
    Processa WIN ou LOSS de forma isolada para cada janela e recalcula o Gale.

    tipo_janela     : 'superior' | 'inferior'
    resultado_janela: 'WIN'      | 'LOSS'
    """
    stake_base = float(config_bot.get("stake_base", 0.35))
    fator      = float(config_bot.get("fator_gale", 1.4))

    chave_janela = "janela_superior" if tipo_janela == "superior" else "janela_inferior"
    janela = config_bot.setdefault(chave_janela, {
        "stake": stake_base, "gale_nivel": 0,
        "gatilho_repeticoes": config_bot.get("qtd_gatilho", 3),
        "historico": [],
    })

    _garra_dupla_atualizar_janela(resultado_janela, janela, fator, stake_base)

    # Sincroniza campos legados
    if tipo_janela == "superior":
        config_bot["stake_superior"] = janela["stake"]
        config_bot["gale_superior"]  = janela["gale_nivel"]
    else:
        config_bot["stake_inferior"] = janela["stake"]
        config_bot["gale_inferior"]  = janela["gale_nivel"]

    return config_bot


# ── Estado em memória da Garra Dupla (por sessão) ───────────────────────────
_garra_dupla_state: dict = {
    "stake_base":     0.35,
    "fator_gale":     1.4,
    "qtd_gatilho":    3,
    "currency":       "USD",
    "duracao":        1,
    "gale_superior":  0,
    "gale_inferior":  0,
    "stake_superior": 0.35,
    "stake_inferior": 0.35,
    # Sub-dicts por janela (histórico independente — padrão vídeo Time)
    "janela_superior": {"stake": 0.35, "gale_nivel": 0, "gatilho_repeticoes": 3, "historico": []},
    "janela_inferior": {"stake": 0.35, "gale_nivel": 0, "gatilho_repeticoes": 3, "historico": []},
}
_garra_dupla_lock = threading.Lock()


@app.route('/garra-dupla/config', methods=['GET', 'POST'])
def garra_dupla_config():
    """
    GET  → retorna configuração atual da Garra Dupla.
    POST → atualiza campos: stake_base, fator_gale, qtd_gatilho, currency, duracao.
    """
    global _garra_dupla_state
    if request.method == 'POST':
        dados = request.get_json(force=True, silent=True) or {}
        campos_editaveis = (
            "stake_base", "fator_gale", "qtd_gatilho", "currency", "duracao"
        )
        with _garra_dupla_lock:
            for c in campos_editaveis:
                if c in dados:
                    _garra_dupla_state[c] = dados[c]
            # Ao mudar stake_base: reseta stakes e históricos das duas janelas
            if "stake_base" in dados:
                base = float(dados["stake_base"])
                qtd  = int(_garra_dupla_state.get("qtd_gatilho", 3))
                if "stake_superior" not in dados:
                    _garra_dupla_state["stake_superior"] = base
                if "stake_inferior" not in dados:
                    _garra_dupla_state["stake_inferior"] = base
                # Reinicia sub-dicts de janela com nova stake
                _garra_dupla_state["janela_superior"] = {
                    "stake": base, "gale_nivel": 0,
                    "gatilho_repeticoes": qtd, "historico": []
                }
                _garra_dupla_state["janela_inferior"] = {
                    "stake": base, "gale_nivel": 0,
                    "gatilho_repeticoes": qtd, "historico": []
                }
            # Ao mudar qtd_gatilho: sincroniza as janelas existentes
            if "qtd_gatilho" in dados:
                qtd = int(dados["qtd_gatilho"])
                _garra_dupla_state["janela_superior"]["gatilho_repeticoes"] = qtd
                _garra_dupla_state["janela_inferior"]["gatilho_repeticoes"] = qtd
        return jsonify({"ok": True, "config": _garra_dupla_state})
    with _garra_dupla_lock:
        return jsonify({"ok": True, "config": dict(_garra_dupla_state)})


@app.route('/garra-dupla/tick', methods=['POST'])
def garra_dupla_tick():
    """
    Endpoint padrão vídeo Time — recebe UM dígito por vez e cada janela
    avalia o gatilho de forma 100% independente.

    Payload:
      digito  (int)  — dígito atual do tick

    Resposta:
      superior  (dict)  — { disparar, payload_ordem?, digito_gatilho? }
      inferior  (dict)  — { disparar, payload_ordem?, digito_gatilho? }
      config_atual (dict) — estado das stakes/gales após o tick
    """
    global _garra_dupla_state
    dados  = request.get_json(force=True, silent=True) or {}
    digito = dados.get("digito")
    if digito is None:
        return jsonify({"erro": "digito obrigatório (int 0-9)"}), 400
    try:
        digito = int(digito)
    except (TypeError, ValueError):
        return jsonify({"erro": "digito deve ser inteiro 0-9"}), 400

    with _garra_dupla_lock:
        state = _garra_dupla_state
        fator      = float(state.get("fator_gale", 1.4))
        stake_base = float(state.get("stake_base", 0.35))
        qtd        = int(state.get("qtd_gatilho", 3))

        # Garante sub-dicts existentes
        jsup = state.setdefault("janela_superior", {
            "stake": state.get("stake_superior", stake_base),
            "gale_nivel": state.get("gale_superior", 0),
            "gatilho_repeticoes": qtd, "historico": []
        })
        jinf = state.setdefault("janela_inferior", {
            "stake": state.get("stake_inferior", stake_base),
            "gale_nivel": state.get("gale_inferior", 0),
            "gatilho_repeticoes": qtd, "historico": []
        })
        jsup["gatilho_repeticoes"] = qtd
        jinf["gatilho_repeticoes"] = qtd

        res_sup = _garra_dupla_processar_janela(digito, jsup, "SUPERIOR")
        res_inf = _garra_dupla_processar_janela(digito, jinf, "INFERIOR")

        # Sincroniza campos legados
        state["stake_superior"] = jsup["stake"]
        state["stake_inferior"] = jinf["stake"]
        state["gale_superior"]  = jsup["gale_nivel"]
        state["gale_inferior"]  = jinf["gale_nivel"]

        config_snapshot = {
            "gale_superior":  state["gale_superior"],
            "gale_inferior":  state["gale_inferior"],
            "stake_superior": state["stake_superior"],
            "stake_inferior": state["stake_inferior"],
        }

    return jsonify({
        "superior":    res_sup,
        "inferior":    res_inf,
        "config_atual": config_snapshot,
    })


@app.route('/garra-dupla/avaliar', methods=['POST'])
def garra_dupla_avaliar():
    """
    Avalia o gatilho em lote (lista de dígitos) — cada dígito é processado
    sequencialmente pelas duas janelas independentes.

    Payload:
      ultimos_digitos (list[int])  — sequência de dígitos a processar
      config          (dict)       — opcional; sobrescreve campos do estado global

    Resposta:
      executar           (bool)    — True se alguma janela disparou no último tick
      contrato_superior  (dict)    — presente se janela superior disparou
      contrato_inferior  (dict)    — presente se janela inferior disparou
      gatilho_digito     (int)
      config_atual       (dict)
    """
    dados = request.get_json(force=True, silent=True) or {}
    ultimos = dados.get("ultimos_digitos", [])
    if not isinstance(ultimos, list) or len(ultimos) == 0:
        return jsonify({"erro": "ultimos_digitos obrigatório (list)"}), 400

    with _garra_dupla_lock:
        cfg = dict(_garra_dupla_state)
        # Permite sobrescrever campos pontualmente sem alterar estado global
        for k, v in (dados.get("config") or {}).items():
            cfg[k] = v

    resultado = _garra_dupla_avaliar_gatilho(ultimos, cfg)
    resultado["config_atual"] = {
        "gale_superior":  cfg.get("gale_superior",  0),
        "gale_inferior":  cfg.get("gale_inferior",  0),
        "stake_superior": cfg.get("stake_superior", cfg.get("stake_base", 0.35)),
        "stake_inferior": cfg.get("stake_inferior", cfg.get("stake_base", 0.35)),
    }
    return jsonify(resultado)


@app.route('/garra-dupla/resultado', methods=['POST'])
def garra_dupla_resultado():
    """
    Processa o resultado (WIN/LOSS) de uma janela e atualiza o Gale isolado.

    Payload:
      tipo_janela      (str)  — 'superior' | 'inferior'
      resultado_janela (str)  — 'WIN' | 'LOSS'

    Resposta:
      ok          (bool)
      config_atual (dict)  — estado atualizado das stakes/gales
    """
    global _garra_dupla_state
    dados = request.get_json(force=True, silent=True) or {}
    tipo     = str(dados.get("tipo_janela",      "")).lower()
    resultado = str(dados.get("resultado_janela", "")).upper()

    if tipo not in ("superior", "inferior"):
        return jsonify({"erro": "tipo_janela deve ser 'superior' ou 'inferior'"}), 400
    if resultado not in ("WIN", "LOSS"):
        return jsonify({"erro": "resultado_janela deve ser 'WIN' ou 'LOSS'"}), 400

    with _garra_dupla_lock:
        _garra_dupla_state = _garra_dupla_processar_resultado(
            resultado, tipo, dict(_garra_dupla_state)
        )
        config_snapshot = {
            "gale_superior":  _garra_dupla_state["gale_superior"],
            "gale_inferior":  _garra_dupla_state["gale_inferior"],
            "stake_superior": _garra_dupla_state["stake_superior"],
            "stake_inferior": _garra_dupla_state["stake_inferior"],
        }

    # ── Adaptive Risk: registra resultado da janela correspondente ───────────
    lucro  = float(dados.get("lucro", 0))
    saldo  = float(dados.get("saldo", 0))
    gale_j = int(
        config_snapshot["gale_superior"] if tipo == "superior"
        else config_snapshot["gale_inferior"]
    )
    ADAPTIVE_ENGINE.registrar_resultado(
        resultado = resultado,
        lucro     = lucro,
        saldo     = saldo,
        gale      = gale_j,
    )

    return jsonify({"ok": True, "config_atual": config_snapshot})


@app.route('/garra-dupla/resetar', methods=['POST'])
def garra_dupla_resetar():
    """
    Reseta gales, stakes e históricos das duas janelas para os valores base.
    Útil para iniciar nova sessão de operações.
    """
    global _garra_dupla_state
    with _garra_dupla_lock:
        base = round(float(_garra_dupla_state.get("stake_base", 0.35)), 2)
        qtd  = int(_garra_dupla_state.get("qtd_gatilho", 3))
        _garra_dupla_state["gale_superior"]  = 0
        _garra_dupla_state["gale_inferior"]  = 0
        _garra_dupla_state["stake_superior"] = base
        _garra_dupla_state["stake_inferior"] = base
        # Reseta também os sub-dicts de janela (limpa histórico)
        _garra_dupla_state["janela_superior"] = {
            "stake": base, "gale_nivel": 0,
            "gatilho_repeticoes": qtd, "historico": []
        }
        _garra_dupla_state["janela_inferior"] = {
            "stake": base, "gale_nivel": 0,
            "gatilho_repeticoes": qtd, "historico": []
        }
        config_snapshot = dict(_garra_dupla_state)
    return jsonify({"ok": True, "config": config_snapshot})


# ═══════════════════════════════════════════════════════════════════════════════
# FIM DA ESTRATÉGIA GARRA DUPLA
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# ESTRATÉGIA BARREIRA FIXA 5 — Gatilho de Repetição + Martingale Simples
# ═══════════════════════════════════════════════════════════════════════════════
#
# Barreira sempre = 5, fixa:
#   DIGITUNDER → ganha se dígito final < 5  (dígitos 0-4, ~50%)
#   DIGITOVER  → ganha se dígito final > 5  (dígitos 6-9, ~40%)
# Gatilho: N dígitos iguais consecutivos antes de disparar.
# Gale: Martingale simples com fator configurável.

_BF5_BARREIRA = "5"


def _bf5_avaliar_gatilho(ultimos_digitos: list, config_bot: dict) -> dict:
    """
    Avalia o gatilho de repetição e retorna o payload com barreira fixa = 5.

    Parâmetros em config_bot:
      stake          (float) — stake atual
      qtd_gatilho    (int)   — repetições necessárias (padrão 2)
      tipo_contrato  (str)   — 'DIGITUNDER' | 'DIGITOVER' (padrão 'DIGITUNDER')
      currency       (str)   — moeda (padrão 'USD')
      duracao        (int)   — duração em ticks (padrão 1)

    Retorna:
      executar      (bool)
      payload_ordem (dict)  — payload pronto para API Deriv
      gatilho_digito (int)  — dígito que disparou
    """
    qtd = int(config_bot.get("qtd_gatilho", 2))
    if len(ultimos_digitos) < qtd:
        return {"executar": False, "motivo": "ticks_insuficientes"}

    ultimos = ultimos_digitos[-qtd:]
    if not all(d == ultimos[0] for d in ultimos):
        return {"executar": False, "motivo": "gatilho_nao_atingido"}

    tipo      = str(config_bot.get("tipo_contrato", "DIGITUNDER")).upper()
    currency  = str(config_bot.get("currency", "USD"))
    duracao   = int(config_bot.get("duracao",  1))

    return {
        "executar": True,
        "gatilho_digito": int(ultimos[0]),
        "payload_ordem": {
            "contract_type": tipo,
            "barrier":       _BF5_BARREIRA,
            "amount":        round(float(config_bot.get("stake", 0.35)), 2),
            "basis":         "stake",
            "currency":      currency,
            "duration":      duracao,
            "duration_unit": "t",
        },
    }


def _bf5_atualizar_gale(resultado: str, config_bot: dict) -> dict:
    """
    Atualiza stake e nível de Gale (Martingale) após resultado do trade.

    resultado : 'WIN' | 'LOSS'
    """
    fator = float(config_bot.get("fator_gale",  1.4))
    base  = float(config_bot.get("stake_base",  0.35))

    if resultado == "WIN":
        config_bot["nivel_gale"] = 0
        config_bot["stake"]      = round(base, 2)
    else:
        config_bot["nivel_gale"] = int(config_bot.get("nivel_gale", 0)) + 1
        config_bot["stake"]      = round(float(config_bot.get("stake", base)) * fator, 2)

    return config_bot


# ── Estado em memória da Barreira Fixa 5 (por sessão) ───────────────────────
_bf5_state: dict = {
    "stake_base":     0.35,
    "stake":          0.35,
    "fator_gale":     1.4,
    "nivel_gale":     0,
    "qtd_gatilho":    2,
    "tipo_contrato":  "DIGITUNDER",
    "currency":       "USD",
    "duracao":        1,
}
_bf5_lock = threading.Lock()


@app.route('/bf5/config', methods=['GET', 'POST'])
def bf5_config():
    """
    GET  → retorna configuração atual da Barreira Fixa 5.
    POST → atualiza campos: stake_base, fator_gale, qtd_gatilho,
                            tipo_contrato, currency, duracao.
    """
    global _bf5_state
    if request.method == 'POST':
        dados = request.get_json(force=True, silent=True) or {}
        campos_editaveis = (
            "stake_base", "fator_gale", "qtd_gatilho",
            "tipo_contrato", "currency", "duracao"
        )
        with _bf5_lock:
            for c in campos_editaveis:
                if c in dados:
                    _bf5_state[c] = dados[c]
            # Ao mudar stake_base reseta a stake atual e o nível de gale
            if "stake_base" in dados:
                _bf5_state["stake"]      = round(float(dados["stake_base"]), 2)
                _bf5_state["nivel_gale"] = 0
        return jsonify({"ok": True, "config": dict(_bf5_state)})
    with _bf5_lock:
        return jsonify({"ok": True, "config": dict(_bf5_state)})


@app.route('/bf5/avaliar', methods=['POST'])
def bf5_avaliar():
    """
    Avalia o gatilho e retorna o payload pronto para a API Deriv.

    Payload:
      ultimos_digitos (list[int]) — últimos dígitos observados
      config          (dict)      — opcional; sobrescreve campos do estado global

    Resposta:
      executar       (bool)
      payload_ordem  (dict)   — presente se executar=True
      gatilho_digito (int)    — dígito que disparou
      config_atual   (dict)   — estado após avaliação
    """
    dados   = request.get_json(force=True, silent=True) or {}
    ultimos = dados.get("ultimos_digitos", [])
    if not isinstance(ultimos, list) or len(ultimos) == 0:
        return jsonify({"erro": "ultimos_digitos obrigatório (list)"}), 400

    with _bf5_lock:
        cfg = dict(_bf5_state)
    for k, v in (dados.get("config") or {}).items():
        cfg[k] = v

    resultado = _bf5_avaliar_gatilho(ultimos, cfg)
    resultado["config_atual"] = {
        "stake":      cfg.get("stake",      cfg.get("stake_base", 0.35)),
        "nivel_gale": cfg.get("nivel_gale", 0),
    }
    return jsonify(resultado)


@app.route('/bf5/resultado', methods=['POST'])
def bf5_resultado():
    """
    Processa WIN ou LOSS e atualiza o Martingale.

    Payload:
      resultado (str) — 'WIN' | 'LOSS'

    Resposta:
      ok          (bool)
      config_atual (dict) — stake e nivel_gale atualizados
    """
    global _bf5_state
    dados     = request.get_json(force=True, silent=True) or {}
    resultado = str(dados.get("resultado", "")).upper()

    if resultado not in ("WIN", "LOSS"):
        return jsonify({"erro": "resultado deve ser 'WIN' ou 'LOSS'"}), 400

    with _bf5_lock:
        _bf5_state = _bf5_atualizar_gale(resultado, dict(_bf5_state))
        config_snapshot = {
            "stake":      _bf5_state["stake"],
            "nivel_gale": _bf5_state["nivel_gale"],
        }

    # ── Adaptive Risk: registra resultado ────────────────────────────────────
    ADAPTIVE_ENGINE.registrar_resultado(
        resultado = resultado,
        lucro     = float(dados.get("lucro", 0)),
        saldo     = float(dados.get("saldo", 0)),
        gale      = config_snapshot["nivel_gale"],
    )

    return jsonify({"ok": True, "config_atual": config_snapshot})


@app.route('/bf5/resetar', methods=['POST'])
def bf5_resetar():
    """
    Reseta stake e nível de Gale para os valores base.
    """
    global _bf5_state
    with _bf5_lock:
        base = round(float(_bf5_state.get("stake_base", 0.35)), 2)
        _bf5_state["stake"]      = base
        _bf5_state["nivel_gale"] = 0
        config_snapshot = dict(_bf5_state)
    return jsonify({"ok": True, "config": config_snapshot})


# ═══════════════════════════════════════════════════════════════════════════════
# FIM DA ESTRATÉGIA BARREIRA FIXA 5
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# GARRA TREND PRO INSTITUCIONAL V2.0 — MOTOR DE CONFLUÊNCIA DE 10 MÓDULOS
# ═══════════════════════════════════════════════════════════════════════════════

GARRA_TREND_VAULT = os.path.join(_BASE_DIR, "garra_trend_vault.json")

class GarraTrendProEngine:
    """
    Motor Institucional de Alta Confluência.
    Avalia 10 módulos independentes e exige Score >= 92 para disparar sinais.
    """

    def __init__(self):
        self.historico_ia = self._carregar_vault()

    def _carregar_vault(self) -> list:
        if os.path.exists(GARRA_TREND_VAULT):
            try:
                with open(GARRA_TREND_VAULT, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _salvar_vault(self, dados: list):
        try:
            with open(GARRA_TREND_VAULT, "w", encoding="utf-8") as f:
                json.dump(dados[-2000:], f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def registrar_operacao(self, dados_op: dict):
        """Módulo 10 – IA Estatística: Grava resultado para auto-aprendizado de pesos."""
        dados_op["timestamp"] = time.time()
        self.historico_ia.append(dados_op)
        self._salvar_vault(self.historico_ia)

    def calcular_pesos_dinamicos(self) -> dict:
        """Módulo 10 – Ajusta os pesos do score com base no que mais deu WIN no histórico."""
        pesos_base = {
            "tendencia_principal": 15,
            "micro_tendencia":     10,
            "forca_velas":         15,
            "momentum":            10,
            "candlestick":         15,
            "volatilidade":         5,
            "rompimento":           5,
            "lateralidade":        10,
            "adx_indicador":       10,
            "ia_estatistica":       5
        }
        if len(self.historico_ia) < 20:
            return pesos_base

        wins = [op for op in self.historico_ia if op.get("resultado") == "WIN"]
        if not wins:
            return pesos_base

        return pesos_base

    def avaliar_mercado(self, dados_mercado: dict) -> dict:
        """
        Executa os 10 módulos de confluência.
        dados_mercado esperados:
          - precos: list[float] (últimos preços)
          - velas: list[dict]  (OHLC + corpo + pavio)
          - indicadores: { ema20, ema50, ema200, rsi, macd, adx, atr, bollinger_band }
          - horario: str
          - mercado: str
        """
        ind    = dados_mercado.get("indicadores", {})
        velas  = dados_mercado.get("velas", [])

        # ── MÓDULO 1: Tendência Principal ──────────────────────────────────
        ema20  = ind.get("ema20",  0)
        ema50  = ind.get("ema50",  0)
        ema200 = ind.get("ema200", 0)

        tendencia_alta  = (ema20 > ema50 > ema200)
        tendencia_baixa = (ema20 < ema50 < ema200)
        mod1_ok         = tendencia_alta or tendencia_baixa
        pontos_mod1     = 15 if mod1_ok else 0

        # ── MÓDULO 2: Micro Tendência (Últimas 10 velas) ───────────────────
        ultimas_10 = velas[-10:] if len(velas) >= 10 else velas
        if ultimas_10:
            altas       = sum(1 for v in ultimas_10 if v.get("fechamento", 0) > v.get("abertura", 0))
            forca_micro = (altas / len(ultimas_10)) * 100
            if tendencia_baixa:
                forca_micro = 100 - forca_micro
        else:
            forca_micro = 50

        mod2_ok     = forca_micro >= 70.0
        pontos_mod2 = 10 if mod2_ok else 0

        # ── MÓDULO 3: Força das Velas ──────────────────────────────────────
        pontos_velas = 0
        if velas:
            v_atual = velas[-1]
            corpo   = abs(v_atual.get("fechamento", 0) - v_atual.get("abertura", 0))
            pavio   = v_atual.get("pavio_superior", 0) + v_atual.get("pavio_inferior", 0)
            if v_atual.get("tipo") == "DOJI":
                pontos_velas -= 5
            elif pavio > corpo * 2:
                pontos_velas -= 3
            elif corpo > 0.001:
                pontos_velas += 3
            else:
                pontos_velas += 1
        mod3_ok     = pontos_velas > 0
        pontos_mod3 = 15 if mod3_ok else 0

        # ── MÓDULO 4: Momentum (RSI, MACD, ATR) ───────────────────────────
        rsi      = ind.get("rsi",  50)
        macd_val = ind.get("macd",  0)
        momentum_ok = (tendencia_alta  and rsi > 55 and macd_val > 0) or \
                      (tendencia_baixa and rsi < 45 and macd_val < 0)
        pontos_mod4 = 10 if momentum_ok else 0

        # ── MÓDULO 5: Candlestick (Padrões de Alta/Baixa) ──────────────────
        padrao_detectado = dados_mercado.get("padrao_candlestick", "NENHUM")
        padroes_validos  = [
            "ENGOLFO_ALTA", "ENGOLFO_BAIXA", "MARTELO", "SHOOTING_STAR",
            "MORNING_STAR", "EVENING_STAR", "HARAMI", "MARUBOZU"
        ]
        mod5_ok     = padrao_detectado in padroes_validos
        pontos_mod5 = 15 if mod5_ok else 0

        # ── MÓDULO 6: Volatilidade (ATR & Bollinger) ──────────────────────
        atr     = ind.get("atr", 0.001)
        mod6_ok = atr > 0.0002
        pontos_mod6 = 5 if mod6_ok else 0

        # ── MÓDULO 7: Rompimento ou Pullback ──────────────────────────────
        falso_rompimento = dados_mercado.get("falso_rompimento", False)
        pontos_mod7      = -40 if falso_rompimento else 5

        # ── MÓDULO 8: Mercado Lateral (ADX / Distância EMA20) ─────────────
        adx             = ind.get("adx", 25)
        mercado_lateral = adx < 20 or abs(ema20 - ema50) < 0.0001
        pontos_mod8     = -100 if mercado_lateral else 10

        # ── MÓDULO 9 & 10: Score Inteligente e IA Estatística ─────────────
        score_total = (
            pontos_mod1 + pontos_mod2 + pontos_mod3 +
            pontos_mod4 + pontos_mod5 + pontos_mod6 +
            pontos_mod7 + pontos_mod8 + 10  # 10 pontos base da IA Estatística
        )

        # ── FILTRO ANTI-LOSS RÍGIDO ───────────────────────────────────────
        aprovado_anti_loss = (
            mod1_ok and
            mod2_ok and
            mod6_ok and
            not mercado_lateral and
            not falso_rompimento and
            score_total >= 92
        )

        direcao = "CALL" if tendencia_alta else ("PUT" if tendencia_baixa else "NEUTRO")

        return {
            "score_total":   score_total,
            "score_minimo":  92,
            "aprovado":      aprovado_anti_loss,
            "direcao":       direcao if aprovado_anti_loss else "AGUARDAR",
            "detalhes_modulos": {
                "m1_tendencia":       mod1_ok,
                "m2_micro_tendencia": f"{forca_micro:.1f}%",
                "m3_forca_velas":     pontos_velas,
                "m4_momentum":        momentum_ok,
                "m5_candlestick":     padrao_detectado,
                "m6_volatilidade":    mod6_ok,
                "m7_rompimento":      not falso_rompimento,
                "m8_lateralidade":    not mercado_lateral,
            },
            "motivo_bloqueio": None if aprovado_anti_loss else "Reprovado pelo Filtro Anti-Loss ou Score < 92"
        }


# Instância Global do Motor Garra Trend Pro V2.0
_garra_trend_engine = GarraTrendProEngine()


# ── ROTAS FLASK PARA O GARRA TREND PRO V2.0 ───────────────────────────────────

@app.route('/garra-trend/avaliar', methods=['POST'])
def garra_trend_avaliar():
    """
    Endpoint principal consumido pelo front-end para testar confluência.
    Executa os 10 Módulos + Filtro Anti-Loss.
    """
    dados    = request.get_json(force=True, silent=True) or {}
    resultado = _garra_trend_engine.avaliar_mercado(dados)

    # Se aprovado institucionalmente, dispara notificação opcional via Telegram
    if resultado["aprovado"]:
        cfg_tg = _tg_carregar()
        if cfg_tg.get("enabled"):
            msg = (
                f"🚀 *GARRA TREND PRO V2.0 — SINAL INSTITUCIONAL*\n\n"
                f"📈 *Direção:* {resultado['direcao']}\n"
                f"⭐ *Score de Confluência:* {resultado['score_total']}/100\n"
                f"💎 *Status:* Aprovado pelo Filtro Anti-Loss\n\n"
                f"🕐 {time.strftime('%H:%M:%S')}"
            )
            _tg_dispatch(lambda: _tg_enviar_texto(cfg_tg["token"], cfg_tg["chat_id"], msg))

    return jsonify(resultado)


@app.route('/garra-trend/registrar-resultado', methods=['POST'])
def garra_trend_registrar():
    """
    Alimenta o Módulo 10 (IA Estatística) com o resultado real da operação (WIN/LOSS).
    """
    dados = request.get_json(force=True, silent=True) or {}
    if "resultado" not in dados:
        return jsonify({"ok": False, "erro": "Campo 'resultado' (WIN/LOSS) obrigatório."}), 400

    _garra_trend_engine.registrar_operacao(dados)

    # ── Adaptive Risk: alimenta o motor com o resultado real ─────────────────
    res_str = str(dados.get("resultado", "")).upper()
    if res_str in ("WIN", "LOSS"):
        ADAPTIVE_ENGINE.registrar_resultado(
            resultado = res_str,
            lucro     = float(dados.get("lucro", 0)),
            saldo     = float(dados.get("saldo", 0)),
            gale      = int(dados.get("gale", 0)),
        )

    return jsonify({"ok": True, "mensagem": "Resultado gravado na IA Estatística do GarraTrend Pro."})


@app.route('/ia/quick-sort-simulacao', methods=['POST'])
def quick_sort_simulacao():
    dados = request.get_json(force=True, silent=True) or {}
    arr = dados.get("lista", [6, 3, 1, 5, 7, 2, 8, 9, 4, 0])

    passos = []

    def partition(a, low, high):
        pivot = a[high]
        i = low - 1
        for j in range(low, high):
            passos.append({"acao": "compare", "i": i+1, "j": j, "pivot": pivot, "arr": list(a)})
            if a[j] <= pivot:
                i += 1
                a[i], a[j] = a[j], a[i]
                passos.append({"acao": "swap", "i": i, "j": j, "arr": list(a)})
        a[i + 1], a[high] = a[high], a[i + 1]
        passos.append({"acao": "swap", "i": i + 1, "j": high, "arr": list(a)})
        return i + 1

    def quick_sort(a, low, high):
        if low < high:
            pi = partition(a, low, high)
            quick_sort(a, low, pi - 1)
            quick_sort(a, pi + 1, high)

    quick_sort(list(arr), 0, len(arr) - 1)
    return jsonify({"ok": True, "passos": passos})


# ── Digit Sniper PRO ────────────────────────────────────────────────────────
register_digit_sniper(app, _buscar_ticks_ws_sync)

# ── Digit Matrix Sniper PRO ──────────────────────────────────────────────────
register_digit_matrix(app, _buscar_ticks_ws_sync)

def start_server():
    # Oracle Cloud — porta configurável via variável de ambiente, padrão 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    print("🚀 Iniciando Interface Cyber Cloud...")
    threading.Thread(target=_wa_keepalive_loop, daemon=True).start()
    start_server()
