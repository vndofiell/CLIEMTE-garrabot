# ═══════════════════════════════════════════════════════════════════════════════
# TRADING PRO MULTI-AI — Orquestrador de 5 IAs com rotação de líder
# ═══════════════════════════════════════════════════════════════════════════════
#
# Arquitetura:
#   AI_01_TENDENCIA   → EMA 20/50/200 + RSI + slope
#   AI_02_PRICE_ACTION→ corpo/pavio/padrão/confirmação
#   AI_03_ESTATISTICA → memória broker/ativo/hora/direção
#   AI_04_REGIME_RISCO→ veto: lateral/volatilidade/drawdown
#   AI_05_SUPERVISOR  → consenso final (threshold configurável)
#
# Fluxo de perda:
#   LOSS → bloqueia reentrada → espera nova vela M1 → troca IA líder → reanálise
#
# Integração com peças existentes:
#   - AdaptiveRiskEngine   (adaptive_risk.py)
#   - MemoryTimeEngine     (memory_time_engine.py)
#   - GarraTrendProEngine  (main.py — passado via injeção)
#   - memory_vault.json    (histórico de operações)
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import math
import os
import time
import threading
from typing import Optional

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_BASE_DIR, "trading_pro_config.json")
_VAULT_FILE  = os.path.join(_BASE_DIR, "trading_pro_vault.json")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÃO PADRÃO
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_DEFAULT: dict = {
    "timeframe":                  "M1",
    "min_consensus":              60,    # score mínimo do Supervisor para OPERAR
    "min_lead_confidence":        55,    # confiança mínima da IA líder
    "max_consecutive_losses":     3,     # losses seguidos antes de forçar rotação
    "cooldown_candles_after_loss":1,     # velas de espera após loss
    "rotate_on_loss":             True,  # troca IA líder após loss
    "reset_leader_after_wins":    2,     # wins seguidos para voltar à AI_01
    "require_new_candle":         True,  # exige nova vela M1 entre operações
    "min_candles":                5,     # mínimo de velas no histórico (relaxado para entrar mais rápido)
    "veto_lateral_adx":           10,    # ADX abaixo disso → veto lateral (relaxado)
    "veto_max_drawdown_pct":      0.15,  # drawdown acima disso → veto risco
    "veto_max_losses_seguidos":   5,     # losses seguidos → veto
}

# Ordem de rotação das IAs líderes
_ORDEM_LIDERES = [
    "AI_01_TENDENCIA",
    "AI_02_PRICE_ACTION",
    "AI_03_ESTATISTICA",
    "AI_04_REGIME_RISCO",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS TÉCNICOS
# ─────────────────────────────────────────────────────────────────────────────

def _ema(closes: list, periodo: int) -> float:
    if len(closes) < periodo:
        return float("nan")
    k = 2.0 / (periodo + 1)
    v = sum(closes[:periodo]) / periodo
    for c in closes[periodo:]:
        v = c * k + v * (1 - k)
    return v


def _rsi(closes: list, periodo: int = 14) -> float:
    if len(closes) < periodo + 1:
        return 50.0
    ganhos, perdas = [], []
    for i in range(1, periodo + 1):
        d = closes[-periodo - 1 + i] - closes[-periodo - 1 + i - 1]
        (ganhos if d > 0 else perdas).append(abs(d))
    mg = sum(ganhos) / periodo if ganhos else 0
    mp = sum(perdas) / periodo if perdas else 1e-9
    rs = mg / mp
    return round(100 - 100 / (1 + rs), 2)


def _slope(closes: list, janela: int = 5) -> float:
    """Inclinação linear normalizada (regressão OLS de 1 grau)."""
    slc = closes[-janela:] if len(closes) >= janela else closes
    n = len(slc)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(slc) / n
    num = sum((i - x_mean) * (slc[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return (num / den) if den > 1e-12 else 0.0


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


def _adx(velas: list, periodo: int = 14) -> float:
    """ADX simplificado."""
    if len(velas) < periodo + 2:
        return 25.0
    tr_list, dm_pos, dm_neg = [], [], []
    for i in range(1, len(velas)):
        h, l, ph, pl = (velas[i]["maxima"], velas[i]["minima"],
                        velas[i-1]["maxima"], velas[i-1]["minima"])
        pc = velas[i-1]["fechamento"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        dmp = max(h - ph, 0) if (h - ph) > (pl - l) else 0
        dmn = max(pl - l, 0) if (pl - l) > (h - ph) else 0
        tr_list.append(tr); dm_pos.append(dmp); dm_neg.append(dmn)
    slc = -periodo
    atr_v = sum(tr_list[slc:]) / periodo
    if atr_v < 1e-9:
        return 25.0
    dip = (sum(dm_pos[slc:]) / periodo) / atr_v * 100
    dim = (sum(dm_neg[slc:]) / periodo) / atr_v * 100
    dx  = abs(dip - dim) / (dip + dim + 1e-9) * 100
    return round(dx, 1)


# ─────────────────────────────────────────────────────────────────────────────
# AI 01 — TENDÊNCIA
# ─────────────────────────────────────────────────────────────────────────────

class AI01Tendencia:
    """
    Analisa EMA 20/50/200, RSI e inclinação do preço.
    Retorna: { direcao, confianca, detalhes }
    """

    def analisar(self, velas: list) -> dict:
        if len(velas) < 20:
            return self._neutro("Histórico insuficiente")

        closes = [v["fechamento"] for v in velas]
        ema20  = _ema(closes, 20)
        ema50  = _ema(closes, 50) if len(closes) >= 50 else float("nan")
        ema200 = _ema(closes, 200) if len(closes) >= 200 else float("nan")
        rsi    = _rsi(closes, 14)
        slope  = _slope(closes, 5)

        alta  = ema20 > (ema50 if not math.isnan(ema50) else ema20 - 1)
        baixa = ema20 < (ema50 if not math.isnan(ema50) else ema20 + 1)

        if not math.isnan(ema200):
            alta  = alta  and ema50 > ema200
            baixa = baixa and ema50 < ema200

        score = 0
        if alta:
            score += 40
            if rsi > 50: score += 20
            if rsi > 60: score += 10
            if slope > 0: score += 15
            direcao = "CALL"
        elif baixa:
            score += 40
            if rsi < 50: score += 20
            if rsi < 40: score += 10
            if slope < 0: score += 15
            direcao = "PUT"
        else:
            return self._neutro("EMAs sem alinhamento")

        # Penalidade RSI extremo (sobrecompra/sobrevenda)
        if rsi > 75 and direcao == "CALL": score -= 15
        if rsi < 25 and direcao == "PUT":  score -= 15

        confianca = min(score, 100)
        return {
            "direcao":   direcao,
            "confianca": confianca,
            "detalhes":  {
                "ema20":  round(ema20, 6),
                "ema50":  round(ema50, 6) if not math.isnan(ema50)  else None,
                "ema200": round(ema200, 6) if not math.isnan(ema200) else None,
                "rsi":    rsi,
                "slope":  round(slope, 8),
            },
        }

    @staticmethod
    def _neutro(motivo: str) -> dict:
        return {"direcao": "NEUTRO", "confianca": 0, "detalhes": {"motivo": motivo}}


# ─────────────────────────────────────────────────────────────────────────────
# AI 02 — PRICE ACTION
# ─────────────────────────────────────────────────────────────────────────────

class AI02PriceAction:
    """
    Analisa padrão de velas: corpo, pavio, engolfo, martelo, etc.
    Só analisa a vela FECHADA (velas[-2]) — nunca a vela em formação.
    """

    def analisar(self, velas: list) -> dict:
        if len(velas) < 3:
            return self._neutro("Velas insuficientes")

        vela  = velas[-2]   # última FECHADA
        prev  = velas[-3]

        ab, fc = vela["abertura"],   vela["fechamento"]
        mx, mn = vela["maxima"],     vela["minima"]
        rng    = mx - mn if mx != mn else 1e-9
        corpo  = abs(fc - ab)
        pav_s  = mx - max(ab, fc)
        pav_i  = min(ab, fc) - mn
        bullish = fc > ab
        bearish = fc < ab

        score_call = 0
        score_put  = 0
        padroes    = []

        # Doji — indiferente
        if corpo / rng < 0.10:
            return self._neutro("DOJI — indecisão")

        # Corpo dominante (Marubozu)
        if corpo / rng > 0.75:
            if bullish: score_call += 30; padroes.append("MARUBOZU_ALTA")
            else:       score_put  += 30; padroes.append("MARUBOZU_BAIXA")

        # Pavio inferior dominante (martelo/pin bar CALL)
        m_inf = pav_i / corpo if corpo > 1e-9 else 0
        m_sup = pav_s / corpo if corpo > 1e-9 else 0
        if m_inf >= 2.0:
            score_call += 25
            padroes.append("MARTELO" if bullish else "PIN_BAR_CALL")
        if m_sup >= 2.0:
            score_put += 25
            padroes.append("SHOOTING_STAR" if bearish else "PIN_BAR_PUT")

        # Engolfo de alta
        if (bullish and prev["fechamento"] < prev["abertura"]
                and ab <= prev["fechamento"] and fc >= prev["abertura"]):
            score_call += 30; padroes.append("ENGOLFO_ALTA")

        # Engolfo de baixa
        if (bearish and prev["fechamento"] > prev["abertura"]
                and ab >= prev["fechamento"] and fc <= prev["abertura"]):
            score_put += 30; padroes.append("ENGOLFO_BAIXA")

        # Confirmação de fechamento
        if bullish and (fc - mn) / rng > 0.65:
            score_call += 15
        if bearish and (mx - fc) / rng > 0.65:
            score_put += 15

        if score_call >= score_put and score_call > 0:
            return {
                "direcao":   "CALL",
                "confianca": min(score_call, 100),
                "detalhes":  {"padroes": padroes, "score_call": score_call, "score_put": score_put},
            }
        if score_put > score_call:
            return {
                "direcao":   "PUT",
                "confianca": min(score_put, 100),
                "detalhes":  {"padroes": padroes, "score_call": score_call, "score_put": score_put},
            }
        return self._neutro("Sem padrão definido")

    @staticmethod
    def _neutro(motivo: str) -> dict:
        return {"direcao": "NEUTRO", "confianca": 0, "detalhes": {"motivo": motivo}}


# ─────────────────────────────────────────────────────────────────────────────
# AI 03 — ESTATÍSTICA
# ─────────────────────────────────────────────────────────────────────────────

class AI03Estatistica:
    """
    Consulta o vault de operações passadas para o mesmo
    broker / ativo / hora / direção e retorna win rate histórico.
    Não inventa confiança — retorna NEUTRO se amostra < 5.
    """

    def analisar(self, broker: str, ativo: str, hora: str, direcao: str) -> dict:
        vault = self._carregar()
        filtro = [
            op for op in vault
            if (op.get("broker", "").upper() == broker.upper()
                and op.get("ativo",   "").upper() == ativo.upper()
                and op.get("hora",    "") == hora
                and op.get("direcao", "").upper() == direcao.upper())
        ]
        total = len(filtro)
        if total < 5:
            return {
                "direcao":   direcao,
                "confianca": 0,
                "detalhes":  {"motivo": f"Amostra insuficiente ({total}/5)", "total": total},
            }
        wins = sum(1 for op in filtro if op.get("resultado", "").upper() == "WIN")
        wr   = wins / total
        # Mapeia win rate para confiança: 50% = 0, 100% = 100
        confianca = max(0, min(100, round((wr - 0.50) * 200)))
        return {
            "direcao":   direcao if wr >= 0.50 else ("PUT" if direcao == "CALL" else "CALL"),
            "confianca": confianca,
            "detalhes":  {"total": total, "wins": wins, "win_rate": round(wr * 100, 1)},
        }

    @staticmethod
    def _carregar() -> list:
        try:
            if os.path.exists(_VAULT_FILE):
                with open(_VAULT_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []


# ─────────────────────────────────────────────────────────────────────────────
# AI 04 — REGIME / RISCO  (IA de VETO)
# ─────────────────────────────────────────────────────────────────────────────

class AI04RegimeRisco:
    """
    IA de veto duro.
    Retorna: { aprovado: bool, motivo_veto: str|None, regime: str }
    """

    def analisar(self, velas: list, losses_seguidos: int,
                 drawdown_pct: float, cfg: dict) -> dict:

        if len(velas) < 10:
            return {"aprovado": False, "motivo_veto": "Histórico insuficiente", "regime": "INDEFINIDO"}

        # ADX — mercado lateral?
        adx = _adx(velas, 14)
        if adx < cfg.get("veto_lateral_adx", 20):
            return {"aprovado": False, "motivo_veto": f"Mercado lateral (ADX={adx:.1f})", "regime": "LATERAL"}

        # Drawdown excessivo?
        if drawdown_pct >= cfg.get("veto_max_drawdown_pct", 0.08):
            return {"aprovado": False,
                    "motivo_veto": f"Drawdown excessivo ({drawdown_pct*100:.1f}%)",
                    "regime": "RISCO_ALTO"}

        # Losses seguidos demais?
        if losses_seguidos >= cfg.get("veto_max_losses_seguidos", 4):
            return {"aprovado": False,
                    "motivo_veto": f"Sequência de {losses_seguidos} losses consecutivos",
                    "regime": "SEQUENCIA_LOSS"}

        # Volatilidade exagerada (ATR > 3x média histórica)
        atr_atual = _atr(velas[-5:],  5)  if len(velas) >= 6  else 0
        atr_media = _atr(velas[-50:], 14) if len(velas) >= 51 else 0
        if atr_media > 0 and atr_atual > atr_media * 3:
            return {"aprovado": False,
                    "motivo_veto": f"Volatilidade extrema (ATR={atr_atual:.6f} > 3×média)",
                    "regime": "VOLATIL"}

        # Define regime
        closes = [v["fechamento"] for v in velas]
        ema20  = _ema(closes, 20)
        ema50  = _ema(closes, 50) if len(closes) >= 50 else ema20
        if ema20 > ema50:
            regime = "TENDENCIA_ALTA"
        elif ema20 < ema50:
            regime = "TENDENCIA_BAIXA"
        else:
            regime = "LATERAL"

        return {"aprovado": True, "motivo_veto": None, "regime": regime, "adx": adx}


# ─────────────────────────────────────────────────────────────────────────────
# AI 05 — SUPERVISOR (consenso final)
# ─────────────────────────────────────────────────────────────────────────────

class AI05Supervisor:
    """
    Recebe os votos das 4 IAs e calcula o consenso ponderado.
    Veto da AI04 → resultado imediato AGUARDAR (independente das outras).
    """

    # Pesos por IA (somam 100)
    PESOS = {
        "AI_01_TENDENCIA":    30,
        "AI_02_PRICE_ACTION": 30,
        "AI_03_ESTATISTICA":  20,
        "AI_04_REGIME_RISCO": 20,
    }

    def decidir(self, votos: dict, cfg: dict) -> dict:
        """
        votos = {
            "AI_01_TENDENCIA":    { direcao, confianca },
            "AI_02_PRICE_ACTION": { direcao, confianca },
            "AI_03_ESTATISTICA":  { direcao, confianca },
            "AI_04_REGIME_RISCO": { aprovado, motivo_veto, regime },
        }
        """
        # Veto duro da AI04
        if not votos["AI_04_REGIME_RISCO"]["aprovado"]:
            return {
                "operar":  False,
                "direcao": "AGUARDAR",
                "score":   0,
                "motivo":  f"VETO AI04: {votos['AI_04_REGIME_RISCO']['motivo_veto']}",
                "votos":   votos,
            }

        min_consensus      = cfg.get("min_consensus", 78)
        min_lead_confidence = cfg.get("min_lead_confidence", 72)

        # Contagem ponderada por direção
        score_call = 0
        score_put  = 0
        for ia, peso in self.PESOS.items():
            if ia == "AI_04_REGIME_RISCO":
                continue
            voto = votos.get(ia, {})
            conf = voto.get("confianca", 0)
            dir_ = voto.get("direcao", "NEUTRO")
            if dir_ == "CALL":
                score_call += conf * peso / 100
            elif dir_ == "PUT":
                score_put  += conf * peso / 100

        score_total = max(score_call, score_put)
        direcao     = "CALL" if score_call >= score_put else "PUT"

        if score_total < min_consensus:
            return {
                "operar":  False,
                "direcao": "AGUARDAR",
                "score":   round(score_total, 1),
                "motivo":  f"Consenso insuficiente ({score_total:.1f} < {min_consensus})",
                "votos":   votos,
            }

        # Conflito: as duas direções com diferença muito pequena (<10)
        if abs(score_call - score_put) < 10:
            return {
                "operar":  False,
                "direcao": "AGUARDAR",
                "score":   round(score_total, 1),
                "motivo":  f"Conflito CALL={score_call:.0f} PUT={score_put:.0f}",
                "votos":   votos,
            }

        return {
            "operar":       True,
            "direcao":      direcao,
            "score":        round(score_total, 1),
            "score_call":   round(score_call, 1),
            "score_put":    round(score_put, 1),
            "min_consensus": min_consensus,
            "motivo":       "Consenso aprovado",
            "votos":        votos,
            "regime":       votos["AI_04_REGIME_RISCO"].get("regime", "—"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# ORQUESTRADOR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

class TradingProOrchestrator:
    """
    Coordena as 5 IAs, gerencia o estado entre velas e executa a rotação
    de líder após loss.

    Uso:
        orc = TradingProOrchestrator()
        resultado = orc.avaliar(velas, broker="QUOTEX", ativo="EURUSD_OTC")
        orc.registrar_resultado("WIN", lucro=0.85)
    """

    def __init__(self):
        self.cfg          = self._carregar_config()
        self._lock        = threading.Lock()

        # Estado de rotação
        self._idx_lider       = 0      # índice em _ORDEM_LIDERES
        self._losses_seguidos = 0
        self._wins_seguidos   = 0
        self._vela_ultima_op  = None   # timestamp da última vela operada
        self._aguardando_vela = False  # True após loss até nova vela
        self._ultima_direcao  = None

        # IAs instanciadas
        self._ai01 = AI01Tendencia()
        self._ai02 = AI02PriceAction()
        self._ai03 = AI03Estatistica()
        self._ai04 = AI04RegimeRisco()
        self._ai05 = AI05Supervisor()

    # ── Configuração ──────────────────────────────────────────────────────────
    def _carregar_config(self) -> dict:
        base = dict(CONFIG_DEFAULT)
        try:
            if os.path.exists(_CONFIG_FILE):
                with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                    base.update(json.load(f))
        except Exception:
            pass
        return base

    def salvar_config(self, cfg: dict):
        self.cfg = {**CONFIG_DEFAULT, **cfg}
        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── IA líder atual ────────────────────────────────────────────────────────
    @property
    def lider_atual(self) -> str:
        return _ORDEM_LIDERES[self._idx_lider % len(_ORDEM_LIDERES)]

    def _rotar_lider(self):
        self._idx_lider = (self._idx_lider + 1) % len(_ORDEM_LIDERES)

    def _resetar_lider(self):
        self._idx_lider = 0

    # ── Avaliação principal ───────────────────────────────────────────────────
    def avaliar(self, velas: list, broker: str = "DERIV",
                ativo: str = "", drawdown_pct: float = 0.0) -> dict:
        """
        Parâmetros:
          velas         : list[dict] — OHLC fechadas {abertura, fechamento, maxima, minima}
          broker        : "QUOTEX" | "DERIV"
          ativo         : símbolo do ativo
          drawdown_pct  : drawdown atual da sessão (0.0 a 1.0)

        Retorna:
          { operar, direcao, score, lider, motivo, votos, ... }
        """
        with self._lock:
            min_c = self.cfg.get("min_candles", 30)
            if len(velas) < min_c:
                return self._aguardar(f"Histórico insuficiente ({len(velas)}/{min_c})")

            # Bloqueio pós-loss: exige nova vela
            if self._aguardando_vela and self.cfg.get("require_new_candle", True):
                ts_atual = velas[-1].get("timestamp", 0)
                if ts_atual == self._vela_ultima_op:
                    return self._aguardar("Aguardando nova vela M1 após loss")
                # Nova vela chegou — libera
                self._aguardando_vela = False

            # Hora para AI03
            import datetime as _dt
            try:
                from zoneinfo import ZoneInfo
                hora = _dt.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%H")
            except Exception:
                hora = _dt.datetime.utcnow().strftime("%H")

            # ── Rodar as 4 IAs ───────────────────────────────────────────────
            v01 = self._ai01.analisar(velas)
            v02 = self._ai02.analisar(velas)
            # AI03 usa a direção sugerida pelo líder como referência
            dir_ref = v01.get("direcao") or v02.get("direcao") or "CALL"
            v03 = self._ai03.analisar(broker, ativo, hora, dir_ref)
            v04 = self._ai04.analisar(velas, self._losses_seguidos, drawdown_pct, self.cfg)

            votos = {
                "AI_01_TENDENCIA":    v01,
                "AI_02_PRICE_ACTION": v02,
                "AI_03_ESTATISTICA":  v03,
                "AI_04_REGIME_RISCO": v04,
            }

            # ── Supervisor decide ─────────────────────────────────────────────
            decisao = self._ai05.decidir(votos, self.cfg)
            decisao["lider"] = self.lider_atual

            # Verifica confiança mínima do líder
            if decisao["operar"]:
                voto_lider = votos.get(self.lider_atual, {})
                conf_lider = voto_lider.get("confianca", 0)
                min_lc     = self.cfg.get("min_lead_confidence", 72)
                if conf_lider < min_lc:
                    decisao["operar"]  = False
                    decisao["direcao"] = "AGUARDAR"
                    decisao["motivo"]  = (
                        f"Líder {self.lider_atual} com confiança insuficiente "
                        f"({conf_lider} < {min_lc})"
                    )

            if decisao["operar"]:
                self._ultima_direcao = decisao["direcao"]
                self._vela_ultima_op = velas[-1].get("timestamp", time.time())

            return decisao

    # ── Registrar resultado ───────────────────────────────────────────────────
    def registrar_resultado(self, resultado: str, lucro: float = 0.0,
                            broker: str = "DERIV", ativo: str = "",
                            score: float = 0.0):
        """
        resultado : "WIN" | "LOSS"
        Atualiza contadores e decide rotação de líder.
        """
        with self._lock:
            res = resultado.upper()

            # Persiste no vault
            import datetime as _dt
            try:
                from zoneinfo import ZoneInfo
                hora = _dt.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%H")
            except Exception:
                hora = _dt.datetime.utcnow().strftime("%H")

            entrada = {
                "broker":    broker,
                "ativo":     ativo,
                "direcao":   self._ultima_direcao or "",
                "leader":    self.lider_atual,
                "resultado": res,
                "lucro":     lucro,
                "score":     score,
                "hora":      hora,
                "timestamp": time.time(),
            }
            self._salvar_vault(entrada)

            if res == "WIN":
                self._losses_seguidos = 0
                self._wins_seguidos  += 1
                # Volta para AI_01 após N wins seguidos
                if (self.cfg.get("reset_leader_after_wins", 2) > 0
                        and self._wins_seguidos >= self.cfg["reset_leader_after_wins"]
                        and self._idx_lider != 0):
                    self._resetar_lider()
                    self._wins_seguidos = 0

            else:  # LOSS
                self._wins_seguidos   = 0
                self._losses_seguidos += 1
                self._aguardando_vela = self.cfg.get("require_new_candle", True)

                # Rotaciona líder se configurado
                if self.cfg.get("rotate_on_loss", True):
                    self._rotar_lider()

    # ── Vault ─────────────────────────────────────────────────────────────────
    def _salvar_vault(self, entrada: dict):
        try:
            vault = []
            if os.path.exists(_VAULT_FILE):
                with open(_VAULT_FILE, "r", encoding="utf-8") as f:
                    vault = json.load(f)
        except Exception:
            vault = []
        vault.append(entrada)
        try:
            with open(_VAULT_FILE, "w", encoding="utf-8") as f:
                json.dump(vault[-3000:], f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── Estado atual ──────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            vault = []
            try:
                if os.path.exists(_VAULT_FILE):
                    with open(_VAULT_FILE, "r", encoding="utf-8") as f:
                        vault = json.load(f)
            except Exception:
                pass
            total = len(vault)
            wins  = sum(1 for op in vault if op.get("resultado") == "WIN")
            return {
                "lider":             self.lider_atual,
                "idx_lider":         self._idx_lider,
                "losses_seguidos":   self._losses_seguidos,
                "wins_seguidos":     self._wins_seguidos,
                "aguardando_vela":   self._aguardando_vela,
                "total_operacoes":   total,
                "wins":              wins,
                "losses":            total - wins,
                "win_rate":          round(wins / total * 100, 1) if total > 0 else 0.0,
                "config":            self.cfg,
            }

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _aguardar(motivo: str) -> dict:
        return {
            "operar":  False,
            "direcao": "AGUARDAR",
            "score":   0,
            "motivo":  motivo,
            "votos":   {},
            "lider":   None,
        }


# ── Instância global ──────────────────────────────────────────────────────────
_orchestrator = TradingProOrchestrator()


def get_orchestrator() -> TradingProOrchestrator:
    """Retorna a instância singleton do orquestrador."""
    return _orchestrator
