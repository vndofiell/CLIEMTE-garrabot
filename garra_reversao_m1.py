# ═══════════════════════════════════════════════════════════════════════════════
# 🦅 GARRA REVERSÃO M1 — WICK EXHAUSTION ENGINE
# Entrada CALL/PUT apenas na VIRADA da vela M1 (vela fechada = confirmação)
#
# Módulos:
#   WickAnalyzer          → ratio pavio/corpo/range
#   ExhaustionDetector    → sequência + distância + pressão
#   ZoneDetector          → suporte/resistência + contagem de testes
#   CandlePatternDetector → martelo, engolfo, estrela, shooting star…
#   PressureAnalyzer      → placar comprador/vendedor das últimas N velas
#   ConfirmationEngine    → fechamento + direção + dominância
#   MarketRegime          → TENDÊNCIA / LATERAL / TRANSIÇÃO
#   ScoreEngine           → 0-100 pontos com bloqueio por contradição
#   MemoryZoneEngine      → histórico de zonas por ativo (win/loss)
#   Decision              → CALL | PUT | AGUARDAR + JSON completo
# ═══════════════════════════════════════════════════════════════════════════════
import os
import json
import time
import math

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_VAULT_FILE = os.path.join(_BASE_DIR, "garra_m1_vault.json")
_CFG_FILE   = os.path.join(_BASE_DIR, "garra_m1_config.json")
_ZONA_FILE  = os.path.join(_BASE_DIR, "garra_m1_zonas.json")

# ── Configuração padrão ───────────────────────────────────────────────────────
CFG_DEFAULT = {
    "score_minimo":              60,
    "janela_entrada_segundos":   2,
    "janela_exaustao":           7,
    "janela_zona":               20,
    "janela_pressao":            10,
    "pavio_moderado_mult":       1.2,
    "pavio_forte_mult":          2.0,
    "pavio_extremo_mult":        2.5,
    "pavio_ratio_minimo":        0.25,
    "corpo_dominante_mult":      1.5,
    "testes_zona_relevante":     1,
    "testes_zona_forte":         2,
    "usar_anti_doji":            True,
    "bloquear_lateral":          False,
    "bloquear_falso_rompimento": False,
    "atr_periodo":               14,
    "modo_mercado":              "automatico",
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS ATR / ESTRUTURA
# ─────────────────────────────────────────────────────────────────────────────

def _atr(velas: list, periodo: int = 14) -> float:
    slc = velas[-(periodo + 1):]
    if len(slc) < 2:
        return 0.0
    trs = []
    for i in range(1, len(slc)):
        hl = slc[i]["maxima"] - slc[i]["minima"]
        hc = abs(slc[i]["maxima"] - slc[i - 1]["fechamento"])
        lc = abs(slc[i]["minima"] - slc[i - 1]["fechamento"])
        trs.append(max(hl, hc, lc))
    return sum(trs) / len(trs) if trs else 0.0


def _ema(closes: list, periodo: int) -> float:
    if len(closes) < periodo:
        return float("nan")
    k = 2.0 / (periodo + 1)
    v = sum(closes[:periodo]) / periodo
    for c in closes[periodo:]:
        v = c * k + v * (1 - k)
    return v


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 1 — WickAnalyzer
# Calcula ratios pavio superior/inferior/corpo para cada vela
# ─────────────────────────────────────────────────────────────────────────────

def analisar_pavio(vela: dict) -> dict:
    ab   = vela["abertura"]
    fc   = vela["fechamento"]
    mx   = vela["maxima"]
    mn   = vela["minima"]
    rng  = mx - mn if mx != mn else 1e-9
    corpo       = abs(fc - ab)
    pav_sup     = mx - max(ab, fc)
    pav_inf     = min(ab, fc) - mn
    r_sup  = pav_sup / rng
    r_inf  = pav_inf / rng
    r_corp = corpo  / rng
    # multiplicadores relativo ao corpo
    m_sup  = (pav_sup / corpo) if corpo > 1e-9 else 0
    m_inf  = (pav_inf / corpo) if corpo > 1e-9 else 0
    return {
        "range":   rng,
        "corpo":   corpo,
        "pav_sup": pav_sup,
        "pav_inf": pav_inf,
        "r_sup":   round(r_sup,  3),
        "r_inf":   round(r_inf,  3),
        "r_corp":  round(r_corp, 3),
        "m_sup":   round(m_sup,  2),   # pavio_sup / corpo
        "m_inf":   round(m_inf,  2),   # pavio_inf / corpo
        "bullish": fc > ab,
        "bearish": fc < ab,
        "doji":    r_corp < 0.10,
    }


def classificar_pavio(pw: dict, cfg: dict) -> dict:
    """Retorna nível de rejeição superior e inferior: NENHUM/MODERADO/FORTE/EXTREMO."""
    p_mod = float(cfg.get("pavio_moderado_mult", 1.5))
    p_for = float(cfg.get("pavio_forte_mult",    2.0))
    p_ext = float(cfg.get("pavio_extremo_mult",  2.5))
    r_min = float(cfg.get("pavio_ratio_minimo",  0.40))

    def _nivel(mult, ratio):
        if mult >= p_ext and ratio >= r_min:  return "EXTREMO"
        if mult >= p_for and ratio >= r_min:  return "FORTE"
        if mult >= p_mod and ratio >= 0.25:   return "MODERADO"
        return "NENHUM"

    return {
        "rej_sup": _nivel(pw["m_sup"], pw["r_sup"]),
        "rej_inf": _nivel(pw["m_inf"], pw["r_inf"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 2 — ExhaustionDetector
# Analisa as últimas N velas para detectar exaustão compradora ou vendedora
# ─────────────────────────────────────────────────────────────────────────────

def detectar_exaustao(velas: list, janela: int = 7) -> dict:
    slc = velas[-janela:] if len(velas) >= janela else velas
    if len(slc) < 3:
        return {"exaustao": "NENHUMA", "direcao": None, "score": 0}

    altas  = sum(1 for v in slc if v["fechamento"] > v["abertura"])
    baixas = sum(1 for v in slc if v["fechamento"] < v["abertura"])
    total  = len(slc)

    corpos = [abs(v["fechamento"] - v["abertura"]) for v in slc]
    media_corpo = sum(corpos) / len(corpos) if corpos else 1e-9

    # Distância percorrida (fechamento primeiro → último)
    dist = slc[-1]["fechamento"] - slc[0]["abertura"]

    # Enfraquecimento: últimos corpos menores que os primeiros?
    if len(corpos) >= 4:
        media_ini = sum(corpos[:len(corpos)//2]) / (len(corpos)//2)
        media_fim = sum(corpos[len(corpos)//2:]) / max(1, total - len(corpos)//2)
        enfraquecendo = media_fim < media_ini * 0.85
    else:
        enfraquecendo = False

    # Pavio dominante na última vela
    pw_ult = analisar_pavio(slc[-1])
    pav_sup_dom = pw_ult["m_sup"] >= 1.5
    pav_inf_dom = pw_ult["m_inf"] >= 1.5

    score  = 0
    direcao = None

    # Exaustão de ALTA (PUT)
    if altas >= total * 0.6 and dist > 0:
        score += min(15, int(altas / total * 15))
        if enfraquecendo: score += 5
        if pav_sup_dom:   score += 10
        direcao = "PUT"

    # Exaustão de BAIXA (CALL)
    elif baixas >= total * 0.6 and dist < 0:
        score += min(15, int(baixas / total * 15))
        if enfraquecendo: score += 5
        if pav_inf_dom:   score += 10
        direcao = "CALL"

    nivel = "FORTE" if score >= 20 else "MODERADA" if score >= 10 else "NENHUMA"
    return {
        "exaustao":    nivel,
        "direcao":     direcao,
        "score":       score,
        "altas":       altas,
        "baixas":      baixas,
        "enfraquecendo": enfraquecendo,
        "pav_sup_dom": pav_sup_dom,
        "pav_inf_dom": pav_inf_dom,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 3 — ZoneDetector
# Detecta zonas de suporte/resistência e conta os testes
# ─────────────────────────────────────────────────────────────────────────────

def detectar_zona(velas: list, janela: int = 20, atr_val: float = 0.0) -> dict:
    hist = velas[-janela - 1: -1] if len(velas) >= janela + 1 else velas[:-1]
    atual = velas[-1]
    if not hist:
        return {"zona": "INDEFINIDA", "nivel": 0, "testes": 0, "tipo": None}

    maximas = [v["maxima"]    for v in hist]
    minimas = [v["minima"]    for v in hist]
    r_max   = max(maximas)
    r_min   = min(minimas)
    tolerancia = atr_val * 0.5 if atr_val > 0 else (r_max - r_min) * 0.03

    fc = atual["fechamento"]

    # Conta rejeições próximas ao nível de resistência
    testes_r = sum(
        1 for v in hist
        if abs(v["maxima"] - r_max) < tolerancia
    )
    # Conta rejeições próximas ao suporte
    testes_s = sum(
        1 for v in hist
        if abs(v["minima"] - r_min) < tolerancia
    )

    perto_r = abs(fc - r_max) < tolerancia * 2
    perto_s = abs(fc - r_min) < tolerancia * 2

    if perto_r and fc < r_max:
        return {
            "zona":   "RESISTENCIA",
            "nivel":  round(r_max, 6),
            "testes": testes_r,
            "tipo":   "PUT",
        }
    if perto_s and fc > r_min:
        return {
            "zona":   "SUPORTE",
            "nivel":  round(r_min, 6),
            "testes": testes_s,
            "tipo":   "CALL",
        }

    return {"zona": "MEIO", "nivel": 0, "testes": 0, "tipo": None}


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 4 — PressureAnalyzer
# Score de pressão compradora/vendedora das últimas N velas
# ─────────────────────────────────────────────────────────────────────────────

def analisar_pressao(velas: list, janela: int = 10) -> dict:
    slc = velas[-janela:] if len(velas) >= janela else velas
    score_c = 0
    score_v = 0
    for i, v in enumerate(slc):
        ab, fc, mx, mn = v["abertura"], v["fechamento"], v["maxima"], v["minima"]
        rng = mx - mn if mx != mn else 1e-9
        corpo = abs(fc - ab)
        bullish = fc > ab
        bearish = fc < ab
        # Vela de alta
        if bullish:
            score_c += 2
            if (fc - mn) / rng > 0.7:   score_c += 2   # fechou perto da máx
            if corpo / rng > 0.5:        score_c += 1   # corpo grande
        if bearish:
            score_v += 2
            if (mx - fc) / rng > 0.7:   score_v += 2
            if corpo / rng > 0.5:        score_v += 1
        # Máxima/mínima crescente
        if i > 0:
            prev = slc[i - 1]
            if mx > prev["maxima"]:   score_c += 1
            if mn > prev["minima"]:   score_c += 1
            if mx < prev["maxima"]:   score_v += 1
            if mn < prev["minima"]:   score_v += 1

    # Rejeição na última vela
    pw = analisar_pavio(slc[-1])
    if pw["m_sup"] >= 1.5:   score_v += 3
    if pw["m_inf"] >= 1.5:   score_c += 3

    dominancia = "CALL" if score_c > score_v else "PUT" if score_v > score_c else "NEUTRA"
    return {
        "score_c": score_c,
        "score_v": score_v,
        "dominancia": dominancia,
        "delta": score_c - score_v,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 5 — CandlePatternDetector
# Detecta padrões de reversão na vela fechada
# ─────────────────────────────────────────────────────────────────────────────

def detectar_padrao(vela: dict, vela_ant: dict | None, vela_ant2: dict | None = None) -> dict:
    pw   = analisar_pavio(vela)
    ab   = vela["abertura"]
    fc   = vela["fechamento"]

    padrao    = "NENHUM"
    direcao   = None

    # Anti-padrão: doji duplo
    if pw["doji"]:
        return {"padrao": "DOJI", "direcao": None, "forca": 0}

    # ── CALL (reversão de baixa) ──────────────────────────────────────────────
    # Martelo: pavio inferior dominante + corpo bullish + pavio sup pequeno
    if pw["m_inf"] >= 2.0 and pw["bullish"] and pw["r_sup"] < 0.15:
        padrao  = "MARTELO"
        direcao = "CALL"
    # Pin bar CALL: pavio inferior mesmo em vela bearish
    elif pw["m_inf"] >= 2.5 and pw["r_sup"] < 0.20:
        padrao  = "PIN_BAR_CALL"
        direcao = "CALL"
    # Engolfo de alta
    elif vela_ant and fc > ab and vela_ant["fechamento"] < vela_ant["abertura"]:
        if ab <= vela_ant["fechamento"] and fc >= vela_ant["abertura"]:
            padrao  = "ENGOLFO_ALTA"
            direcao = "CALL"
    # Morning Star (3 velas)
    elif vela_ant and vela_ant2:
        pw2 = analisar_pavio(vela_ant)
        if (vela_ant2["fechamento"] < vela_ant2["abertura"]   # 1ª: bearish
                and pw2["doji"]                                # 2ª: doji
                and fc > ab):                                  # 3ª: bullish
            padrao  = "MORNING_STAR"
            direcao = "CALL"

    # ── PUT (reversão de alta) ────────────────────────────────────────────────
    elif pw["m_sup"] >= 2.0 and pw["bearish"] and pw["r_inf"] < 0.15:
        padrao  = "SHOOTING_STAR"
        direcao = "PUT"
    elif pw["m_sup"] >= 2.5 and pw["r_inf"] < 0.20:
        padrao  = "PIN_BAR_PUT"
        direcao = "PUT"
    elif vela_ant and fc < ab and vela_ant["fechamento"] > vela_ant["abertura"]:
        if ab >= vela_ant["fechamento"] and fc <= vela_ant["abertura"]:
            padrao  = "ENGOLFO_BAIXA"
            direcao = "PUT"
    elif vela_ant and vela_ant2:
        pw2 = analisar_pavio(vela_ant)
        if (vela_ant2["fechamento"] > vela_ant2["abertura"]
                and pw2["doji"]
                and fc < ab):
            padrao  = "EVENING_STAR"
            direcao = "PUT"

    # Força do padrão
    forca_map = {
        "MARTELO": 10, "SHOOTING_STAR": 10,
        "ENGOLFO_ALTA": 10, "ENGOLFO_BAIXA": 10,
        "PIN_BAR_CALL": 8, "PIN_BAR_PUT": 8,
        "MORNING_STAR": 8, "EVENING_STAR": 8,
        "NENHUM": 0, "DOJI": 0,
    }
    return {
        "padrao":  padrao,
        "direcao": direcao,
        "forca":   forca_map.get(padrao, 5),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 6 — ConfirmationEngine
# Verifica se o fechamento confirma a direção esperada
# ─────────────────────────────────────────────────────────────────────────────

def confirmar_entrada(vela: dict, direcao_esperada: str) -> dict:
    ab, fc = vela["abertura"], vela["fechamento"]
    mx, mn = vela["maxima"],   vela["minima"]
    rng    = mx - mn if mx != mn else 1e-9

    fechou_call = fc > ab
    fechou_put  = fc < ab

    # Fechamento no terço superior (call) ou inferior (put)
    topo_tercio = mn + rng * 0.65
    bot_tercio  = mn + rng * 0.35
    call_forte  = fc >= topo_tercio
    put_forte   = fc <= bot_tercio

    if direcao_esperada == "CALL":
        confirmado = fechou_call
        forca      = 10 if (confirmado and call_forte) else 5 if confirmado else 0
    elif direcao_esperada == "PUT":
        confirmado = fechou_put
        forca      = 10 if (confirmado and put_forte) else 5 if confirmado else 0
    else:
        confirmado, forca = False, 0

    return {"confirmado": confirmado, "forca": forca}


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 7 — MarketRegime
# TENDÊNCIA_ALTA / TENDÊNCIA_BAIXA / LATERAL / TRANSIÇÃO
# ─────────────────────────────────────────────────────────────────────────────

def detectar_regime(velas: list) -> dict:
    if len(velas) < 10:
        return {"regime": "INDEFINIDO", "contexto_score": 0}
    closes = [v["fechamento"] for v in velas]
    ema20  = _ema(closes, 20) if len(closes) >= 20 else float("nan")
    ema50  = _ema(closes, 50) if len(closes) >= 50 else float("nan")

    # Estrutura de máximas/mínimas das últimas 10 velas
    ult10  = velas[-10:]
    maxs   = [v["maxima"] for v in ult10]
    mins   = [v["minima"] for v in ult10]
    hh = maxs[-1] > maxs[-3] > maxs[-5]
    hl = mins[-1] > mins[-3] > mins[-5]
    lh = maxs[-1] < maxs[-3] < maxs[-5]
    ll = mins[-1] < mins[-3] < mins[-5]

    # Range da estrutura
    rng_rel = (max(maxs) - min(mins)) / (closes[-1] or 1)
    lateral = rng_rel < 0.003   # range muito pequeno = lateral

    if lateral:
        return {"regime": "LATERAL", "contexto_score": 0, "ema20": ema20, "ema50": ema50}

    if hh and hl:
        regime = "TENDENCIA_ALTA"
        cs     = 5
    elif lh and ll:
        regime = "TENDENCIA_BAIXA"
        cs     = 5
    elif (hh and ll) or (lh and hl):
        regime = "TRANSICAO"
        cs     = 2
    else:
        regime = "LATERAL"
        cs     = 0

    return {"regime": regime, "contexto_score": cs, "ema20": ema20, "ema50": ema50}


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 8 — CandleDominance (vela dominante por tamanho)
# ─────────────────────────────────────────────────────────────────────────────

def detectar_dominancia_candle(velas: list) -> dict:
    if len(velas) < 11:
        return {"dominante": False, "mult": 1.0, "nivel": "NORMAL"}
    corpos    = [abs(v["fechamento"] - v["abertura"]) for v in velas[-11:-1]]
    media     = sum(corpos) / len(corpos) if corpos else 1e-9
    atual     = abs(velas[-1]["fechamento"] - velas[-1]["abertura"])
    mult      = (atual / media) if media > 1e-9 else 1.0
    nivel     = "EXTREMO" if mult >= 2.5 else "MUITO_FORTE" if mult >= 2.0 else "FORTE" if mult >= 1.5 else "NORMAL"
    return {"dominante": mult >= 1.5, "mult": round(mult, 2), "nivel": nivel}


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 9 — MemoryZoneEngine
# Memória histórica das zonas por ativo (wins/losses por zona)
# ─────────────────────────────────────────────────────────────────────────────

class MemoryZoneEngine:
    def __init__(self):
        self._dados = self._carregar()

    def _carregar(self) -> dict:
        if os.path.exists(_ZONA_FILE):
            try:
                with open(_ZONA_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _salvar(self):
        try:
            with open(_ZONA_FILE, "w", encoding="utf-8") as f:
                json.dump(self._dados, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _chave(self, ativo: str, nivel: float, tipo: str) -> str:
        nivel_arred = round(nivel, 4)
        return f"{ativo}|{tipo}|{nivel_arred}"

    def consultar(self, ativo: str, nivel: float, tipo: str) -> dict:
        k = self._chave(ativo, nivel, tipo)
        return self._dados.get(k, {"testes": 0, "wins": 0, "losses": 0})

    def registrar(self, ativo: str, nivel: float, tipo: str, resultado: str):
        k = self._chave(ativo, nivel, tipo)
        if k not in self._dados:
            self._dados[k] = {"testes": 0, "wins": 0, "losses": 0}
        self._dados[k]["testes"] += 1
        if resultado.upper() == "WIN":
            self._dados[k]["wins"]   += 1
        else:
            self._dados[k]["losses"] += 1
        self._salvar()

    def bonus_score(self, ativo: str, nivel: float, tipo: str) -> int:
        d = self.consultar(ativo, nivel, tipo)
        total = d["wins"] + d["losses"]
        if total < 3:
            return 0
        wr = d["wins"] / total
        if wr >= 0.70:  return 5
        if wr >= 0.55:  return 2
        return 0


_mze = MemoryZoneEngine()


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 10 — ScoreEngine + Filtro anti-sinal
# Tabela de pontuação e bloqueios
# ─────────────────────────────────────────────────────────────────────────────
#
# Módulo                 | Máx CALL | Máx PUT
# ────────────────────────────────────────────
# Zona forte             |   20     |   20
# Pavio dominante        |   20     |   20
# Exaustão               |   15     |   15
# Pressão (pressão)      |   10     |   10
# Candlestick            |   10     |   10
# Dominância de candle   |   10     |   10
# Confirmação            |   10     |   10
# Tendência/contexto     |    5     |    5
# Total                  |  100     |  100
# ─────────────────────────────────────────────────────────────────────────────

SCORE_MAX = 100


def _calcular_scores(
    zona:     dict, rej_sup: str, rej_inf: str,
    exaustao: dict, pressao: dict,
    padrao:   dict, dominancia: dict,
    conf_c:   dict, conf_p:   dict,
    regime:   dict, cfg:      dict,
) -> tuple:
    sc = 0   # score CALL
    sp = 0   # score PUT
    mc = []  # motivos CALL
    mp = []  # motivos PUT

    # ── ZONA (máx 20) ────────────────────────────────────────────────────────
    testes  = zona.get("testes", 0)
    t_rel   = int(cfg.get("testes_zona_relevante", 2))
    t_for   = int(cfg.get("testes_zona_forte",     3))

    if zona["tipo"] == "CALL":
        pts = 10 if testes >= 1 else 0
        if testes >= t_rel: pts = 15
        if testes >= t_for: pts = 20
        sc += pts
        mc.append(f"suporte ({testes} testes) +{pts}")
    elif zona["tipo"] == "PUT":
        pts = 10 if testes >= 1 else 0
        if testes >= t_rel: pts = 15
        if testes >= t_for: pts = 20
        sp += pts
        mp.append(f"resistência ({testes} testes) +{pts}")

    # ── PAVIO DOMINANTE (máx 20) ──────────────────────────────────────────────
    _pav_pts = {"EXTREMO": 20, "FORTE": 15, "MODERADO": 8, "NENHUM": 0}
    pts_ri   = _pav_pts.get(rej_inf, 0)
    pts_rs   = _pav_pts.get(rej_sup, 0)
    if pts_ri > 0:
        sc += pts_ri
        mc.append(f"pavio inferior {rej_inf} +{pts_ri}")
    if pts_rs > 0:
        sp += pts_rs
        mp.append(f"pavio superior {rej_sup} +{pts_rs}")

    # ── EXAUSTÃO (máx 15) ─────────────────────────────────────────────────────
    ex_score = min(15, exaustao.get("score", 0))
    if exaustao.get("direcao") == "CALL":
        sc += ex_score
        mc.append(f"exaustão vendedora +{ex_score}")
    elif exaustao.get("direcao") == "PUT":
        sp += ex_score
        mp.append(f"exaustão compradora +{ex_score}")

    # ── PRESSÃO (máx 10) ──────────────────────────────────────────────────────
    delta = abs(pressao.get("delta", 0))
    dom   = pressao.get("dominancia")
    pts_p = min(10, delta)
    if dom == "CALL":
        sc += pts_p
        mc.append(f"pressão compradora +{pts_p}")
    elif dom == "PUT":
        sp += pts_p
        mp.append(f"pressão vendedora +{pts_p}")

    # ── CANDLESTICK (máx 10) ──────────────────────────────────────────────────
    forca_pad = min(10, padrao.get("forca", 0))
    if padrao.get("direcao") == "CALL":
        sc += forca_pad
        mc.append(f"{padrao['padrao']} +{forca_pad}")
    elif padrao.get("direcao") == "PUT":
        sp += forca_pad
        mp.append(f"{padrao['padrao']} +{forca_pad}")

    # ── DOMINÂNCIA DE CANDLE (máx 10) ────────────────────────────────────────
    _dom_pts = {"EXTREMO": 10, "MUITO_FORTE": 8, "FORTE": 5, "NORMAL": 0}
    dom_pts  = _dom_pts.get(dominancia.get("nivel", "NORMAL"), 0)
    # A dominância confirma a direção do último candle
    if dom_pts > 0:
        sc += dom_pts; mc.append(f"candle dominante ({dominancia['nivel']}) +{dom_pts}")
        sp += dom_pts; mp.append(f"candle dominante ({dominancia['nivel']}) +{dom_pts}")

    # ── CONFIRMAÇÃO (máx 10) ──────────────────────────────────────────────────
    sc += conf_c.get("forca", 0)
    if conf_c.get("forca", 0) > 0: mc.append(f"confirmação CALL +{conf_c['forca']}")
    sp += conf_p.get("forca", 0)
    if conf_p.get("forca", 0) > 0: mp.append(f"confirmação PUT +{conf_p['forca']}")

    # ── CONTEXTO / TENDÊNCIA (máx 5) ─────────────────────────────────────────
    reg = regime.get("regime", "")
    cs  = regime.get("contexto_score", 0)
    if reg == "TENDENCIA_ALTA":
        sc += cs; mc.append(f"tendência de alta +{cs}")
    elif reg == "TENDENCIA_BAIXA":
        sp += cs; mp.append(f"tendência de baixa +{cs}")
    elif reg == "TRANSICAO":
        sc += cs; mc.append(f"transição +{cs}")
        sp += cs; mp.append(f"transição +{cs}")

    # ── BÔNUS memória de zona ─────────────────────────────────────────────────
    # (adicionado pelo motor principal com ativo disponível)

    return min(sc, SCORE_MAX), min(sp, SCORE_MAX), mc, mp


def _filtro_anti_sinal(pw: dict, zona: dict, exaustao: dict,
                       regime: dict, cfg: dict) -> str | None:
    """
    Retorna motivo de bloqueio (str) ou None se não há bloqueio.
    """
    # Doji absoluto
    if pw["doji"] and cfg.get("usar_anti_doji", True):
        return "DOJI bloqueado"
    # Pavios dos dois lados (indecisão)
    if pw["r_sup"] > 0.30 and pw["r_inf"] > 0.30:
        return "Pavios duplos — indecisão"
    # Zona indefinida e sem exaustão
    if zona["tipo"] is None and exaustao["exaustao"] == "NENHUMA":
        return "Sem zona + sem exaustão"
    # Mercado lateral E bloquear_lateral ligado
    if regime["regime"] == "LATERAL" and cfg.get("bloquear_lateral", False):
        return "Mercado lateral"
    # Transição com score baixo — deixa o ScoreEngine decidir (não bloqueia aqui)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR PRINCIPAL — GarraReversaoM1Engine
# ─────────────────────────────────────────────────────────────────────────────

class GarraReversaoM1Engine:
    """
    Motor GARRA REVERSÃO M1 — WICK EXHAUSTION.
    Analisa a vela FECHADA e autoriza CALL / PUT / AGUARDAR.
    """

    def __init__(self):
        self.historico = self._carregar_vault()
        self.config    = self._carregar_config()

    # ── Persistência ─────────────────────────────────────────────────────────
    def _carregar_vault(self) -> list:
        if os.path.exists(_VAULT_FILE):
            try:
                with open(_VAULT_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _salvar_vault(self):
        try:
            with open(_VAULT_FILE, "w", encoding="utf-8") as f:
                json.dump(self.historico[-2000:], f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _carregar_config(self) -> dict:
        if os.path.exists(_CFG_FILE):
            try:
                with open(_CFG_FILE, "r", encoding="utf-8") as f:
                    return {**CFG_DEFAULT, **json.load(f)}
            except Exception:
                pass
        return dict(CFG_DEFAULT)

    def salvar_config(self, cfg: dict):
        self.config = {**CFG_DEFAULT, **cfg}
        try:
            with open(_CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def registrar_resultado(self, dados: dict):
        dados["timestamp"] = time.time()
        self.historico.append(dados)
        self._salvar_vault()
        # Alimenta memória de zonas
        if dados.get("zona_nivel") and dados.get("zona_tipo") and dados.get("ativo"):
            _mze.registrar(
                ativo     = dados["ativo"],
                nivel     = float(dados["zona_nivel"]),
                tipo      = dados["zona_tipo"],
                resultado = str(dados.get("resultado", "LOSS")),
            )

    # ── Avaliação principal ───────────────────────────────────────────────────
    def avaliar(self, dados: dict) -> dict:
        """
        Parâmetros esperados em `dados`:
          velas         : list[dict]  — OHLC fechadas
                          cada vela: {abertura, fechamento, maxima, minima}
          ts_vela_atual : float       — timestamp da vela que acabou de abrir
          ts_agora      : float       — timestamp atual
          ativo         : str         — ativo (para memória de zonas)
          cfg_override  : dict        — sobrescreve config (opcional)
        """
        cfg    = {**self.config, **(dados.get("cfg_override") or {})}
        velas  = dados.get("velas", [])
        ts_now = float(dados.get("ts_agora",      time.time()))
        ts_vel = float(dados.get("ts_vela_atual", ts_now))
        ativo  = str(dados.get("ativo", ""))
        janela = int(cfg.get("janela_entrada_segundos", 2))
        score_min = int(cfg.get("score_minimo", 75))

        if len(velas) < 20:
            return self._aguardar(
                f"Histórico insuficiente ({len(velas)}/20)", 0, 0, cfg
            )

        # Janela de entrada — rejeita apenas se o ts_vela for do futuro distante
        # (indica dado incorreto). Aceita qualquer momento dentro da vela M1.
        # ts_vel = quando a vela atual abriu; ts_now - ts_vel = segundos da vela
        # Para M1 (60s) isso vai de 0 a ~60s — não bloqueamos aqui.
        # Bloqueamos só se o timestamp vier claramente errado (> 3 velas atrás).
        periodo_seg = 60  # M1
        if (ts_now - ts_vel) > periodo_seg * 3:
            return self._aguardar(
                f"Fora do ciclo ({ts_now - ts_vel:.0f}s)", 0, 0, cfg
            )

        # Velas fechadas para análise
        vela_fc  = velas[-2]            # última fechada (a que analisa)
        vela_ant = velas[-3] if len(velas) >= 3 else None
        vela_ant2= velas[-4] if len(velas) >= 4 else None

        atr_val = _atr(velas[:-1], int(cfg.get("atr_periodo", 14)))

        # ── Módulo 1: Pavio ──────────────────────────────────────────────────
        pw       = analisar_pavio(vela_fc)
        rej_cls  = classificar_pavio(pw, cfg)
        rej_sup  = rej_cls["rej_sup"]
        rej_inf  = rej_cls["rej_inf"]

        # ── Módulo 2: Exaustão ───────────────────────────────────────────────
        janela_ex = int(cfg.get("janela_exaustao", 7))
        exaustao  = detectar_exaustao(velas[:-1], janela_ex)

        # ── Módulo 3: Zona ───────────────────────────────────────────────────
        janela_z  = int(cfg.get("janela_zona", 20))
        zona      = detectar_zona(velas[:-1], janela_z, atr_val)

        # ── Módulo 4: Pressão ────────────────────────────────────────────────
        janela_p  = int(cfg.get("janela_pressao", 10))
        pressao   = analisar_pressao(velas[:-1], janela_p)

        # ── Módulo 5: Candlestick ────────────────────────────────────────────
        padrao    = detectar_padrao(vela_fc, vela_ant, vela_ant2)

        # ── Módulo 6: Confirmação ────────────────────────────────────────────
        conf_c  = confirmar_entrada(vela_fc, "CALL")
        conf_p  = confirmar_entrada(vela_fc, "PUT")

        # ── Módulo 7: Regime ─────────────────────────────────────────────────
        regime  = detectar_regime(velas[:-1])

        # ── Módulo 8: Dominância de candle ───────────────────────────────────
        dom_candle = detectar_dominancia_candle(velas[:-1])

        # ── Filtro anti-sinal ────────────────────────────────────────────────
        bloqueio = _filtro_anti_sinal(pw, zona, exaustao, regime, cfg)
        if bloqueio:
            return self._aguardar(bloqueio, 0, 0, cfg)

        # ── ScoreEngine ──────────────────────────────────────────────────────
        sc, sp, mc, mp = _calcular_scores(
            zona, rej_sup, rej_inf, exaustao, pressao,
            padrao, dom_candle, conf_c, conf_p, regime, cfg,
        )

        # Bônus memória de zonas
        if zona["nivel"] > 0 and ativo:
            bonus = _mze.bonus_score(ativo, zona["nivel"], zona.get("zona", ""))
            if zona["tipo"] == "CALL":
                sc += bonus
                if bonus: mc.append(f"memória zona +{bonus}")
            elif zona["tipo"] == "PUT":
                sp += bonus
                if bonus: mp.append(f"memória zona +{bonus}")

        sc = min(sc, SCORE_MAX)
        sp = min(sp, SCORE_MAX)

        # ── Decisão ──────────────────────────────────────────────────────────
        direcao, score_final, motivos = None, 0, []

        # Conflito forte: ambos acima do mínimo mas diferença pequena → AGUARDAR
        if sc >= score_min and sp >= score_min and abs(sc - sp) < 10:
            return self._aguardar(
                f"Conflito CALL={sc} PUT={sp} — diferença insuficiente",
                sc, sp, cfg,
            )

        if sc >= score_min and sc > sp:
            direcao, score_final, motivos = "CALL", sc, mc
        elif sp >= score_min and sp > sc:
            direcao, score_final, motivos = "PUT", sp, mp

        if not direcao:
            return {
                "operar":       False,
                "direcao":      "AGUARDAR",
                "score_call":   sc,
                "score_put":    sp,
                "score_minimo": score_min,
                "motivo":       "Confluência insuficiente",
                "detalhes": {
                    "pavio":    pw,
                    "zona":     zona,
                    "exaustao": exaustao,
                    "regime":   regime,
                    "padrao":   padrao,
                },
            }

        confianca_label = (
            "ALTA_CONFLUENCIA" if score_final >= 90 else
            "FORTE"            if score_final >= 80 else
            "MODERADO"         if score_final >= 70 else
            "FRACO"
        )

        return {
            "operar":         True,
            "direcao":        direcao,
            "score":          score_final,
            "score_call":     sc,
            "score_put":      sp,
            "score_minimo":   score_min,
            "motivos":        motivos,
            "confianca":      confianca_label,
            "tipo_entrada":   "VIRADA_VELA",
            "ts_vela":        ts_vel,
            "janela_segundos": janela,
            "zona":           zona.get("zona"),
            "zona_nivel":     zona.get("nivel"),
            "zona_tipo":      zona.get("tipo"),
            "zona_testes":    zona.get("testes"),
            "pavio":          f"INF_{rej_inf}" if direcao == "CALL" else f"SUP_{rej_sup}",
            "exaustao":       exaustao["exaustao"] != "NENHUMA",
            "exaustao_nivel": exaustao["exaustao"],
            "candlestick":    padrao["padrao"],
            "regime":         regime["regime"],
            "mercado":        "OTC" if "_otc" in ativo.lower() else "OPEN",
            "detalhes": {
                "pavio":      pw,
                "zona":       zona,
                "exaustao":   exaustao,
                "pressao":    pressao,
                "padrao":     padrao,
                "dominancia": dom_candle,
                "regime":     regime,
            },
        }

    def _aguardar(self, motivo: str, sc: int, sp: int, cfg: dict) -> dict:
        return {
            "operar":       False,
            "direcao":      "AGUARDAR",
            "score_call":   sc,
            "score_put":    sp,
            "score_minimo": int(cfg.get("score_minimo", 75)),
            "motivo":       motivo,
            "detalhes":     {},
        }

    def estatisticas(self) -> dict:
        total  = len(self.historico)
        if total == 0:
            return {"total": 0, "wins": 0, "losses": 0, "wr": 0.0}
        wins   = sum(1 for op in self.historico if str(op.get("resultado","")).upper() == "WIN")
        losses = total - wins
        return {
            "total":    total,
            "wins":     wins,
            "losses":   losses,
            "wr":       round(wins / total * 100, 1),
            "ultimas_10": self.historico[-10:],
        }


# Instância global
_engine = GarraReversaoM1Engine()


def get_engine() -> GarraReversaoM1Engine:
    return _engine
