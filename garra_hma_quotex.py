"""
GARRA HMA QUOTEX — Motor de Análise OHLC para Quotex
=====================================================
Motor dedicado para mercados forex/OTC da Quotex.
Usa exclusivamente dados de velas OHLC (open, high, low, close).

Módulos:
  1. TrendEngine     — EMA cruzamento + slope de regressão linear
  2. CandleEngine    — padrão de corpo + sombra das velas
  3. MomentumEngine  — RSI simplificado + aceleração de preço
  4. RegimeEngine    — ATR + compressão/expansão de volatilidade
  5. DecisionEngine  — consenso ponderado dos 4 módulos
  6. RiskGate        — payout, losses sequenciais, volatilidade extrema

Entrada esperada: list[dict] com chaves {open, high, low, close, time}
                  mínimo 15 velas, ideal 50–100 velas M1.
"""

import math
from typing import List, Dict, Optional


# ══════════════════════════════════════════════════════════════════════════════
# Utilitários
# ══════════════════════════════════════════════════════════════════════════════

def _ema(values: List[float], period: int) -> List[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _slope(values: List[float]) -> float:
    """Regressão linear — retorna slope normalizado pelo preço médio."""
    n = len(values)
    if n < 3:
        return 0.0
    xm = (n - 1) / 2.0
    ym = sum(values) / n
    num = sum((i - xm) * (values[i] - ym) for i in range(n))
    den = sum((i - xm) ** 2 for i in range(n))
    slope = (num / den) if den != 0 else 0.0
    return slope / max(abs(ym), 1e-10)


def _atr(candles: List[dict], period: int = 14) -> float:
    """Average True Range normalizado pelo preço."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:]
    atr_val = sum(window) / len(window)
    price = candles[-1]["close"]
    return atr_val / max(abs(price), 1e-10)


# ══════════════════════════════════════════════════════════════════════════════
# 1. TREND ENGINE — EMA cruzamento + slope
# ══════════════════════════════════════════════════════════════════════════════

class TrendEngine:
    """
    Detecta tendência via:
    - Cruzamento EMA9 × EMA21
    - Slope da EMA9 nos últimos 8 períodos
    - Posição do close em relação à EMA21
    """

    def analisar(self, closes: List[float]) -> dict:
        if len(closes) < 22:
            return {"direcao": "NEUTRO", "forca": 0.0, "motivo": "dados insuficientes"}

        ema9  = _ema(closes, 9)
        ema21 = _ema(closes, 21)

        # Alinha os dois arrays (ema21 é mais curto)
        offset = len(ema9) - len(ema21)
        ema9_al  = ema9[offset:]
        ema21_al = ema21

        if len(ema9_al) < 3:
            return {"direcao": "NEUTRO", "forca": 0.0, "motivo": "ema insuficiente"}

        # Cruzamento: posição atual e anterior
        cruzou_acima = ema9_al[-1] > ema21_al[-1] and ema9_al[-2] <= ema21_al[-2]
        cruzou_abaixo = ema9_al[-1] < ema21_al[-1] and ema9_al[-2] >= ema21_al[-2]

        # Posição atual
        acima = ema9_al[-1] > ema21_al[-1]

        # Slope da EMA9 nos últimos 8 períodos
        slope_val = _slope(ema9_al[-8:])

        # Distância relativa entre EMAs
        dist = (ema9_al[-1] - ema21_al[-1]) / max(abs(ema21_al[-1]), 1e-10)

        # Força: combinação de slope + distância relativa
        forca = min(1.0, (abs(slope_val) * 5000 + abs(dist) * 200))

        if acima and slope_val > 0:
            direcao = "CALL"
            bonus = 0.15 if cruzou_acima else 0.0
        elif not acima and slope_val < 0:
            direcao = "PUT"
            bonus = 0.15 if cruzou_abaixo else 0.0
        else:
            direcao = "NEUTRO"
            bonus = 0.0

        forca = min(1.0, forca + bonus)

        return {
            "direcao":     direcao,
            "forca":       round(forca, 4),
            "slope":       round(slope_val, 8),
            "ema9":        round(ema9_al[-1], 6),
            "ema21":       round(ema21_al[-1], 6),
            "cruzamento":  cruzou_acima or cruzou_abaixo,
            "motivo":      f"EMA9{'>' if acima else '<'}EMA21 slope={'↑' if slope_val > 0 else '↓'}",
        }


# ══════════════════════════════════════════════════════════════════════════════
# 2. CANDLE ENGINE — Padrão de corpo e sombra
# ══════════════════════════════════════════════════════════════════════════════

class CandleEngine:
    """
    Analisa os últimos 5 candles:
    - Razão corpo/sombra
    - Sequência de candles do mesmo tipo
    - Pressão compradora vs vendedora
    - Padrões: engolfo, martelo, estrela cadente
    """

    def analisar(self, candles: List[dict]) -> dict:
        if len(candles) < 5:
            return {"direcao": "NEUTRO", "forca": 0.0, "motivo": "poucos candles"}

        ultimos = candles[-5:]

        alta  = sum(1 for c in ultimos if c["close"] > c["open"])
        baixa = sum(1 for c in ultimos if c["close"] < c["open"])

        # Proporção de alta vs baixa
        total = alta + baixa
        if total == 0:
            return {"direcao": "NEUTRO", "forca": 0.0, "motivo": "doji sequence"}

        prop_alta  = alta / total
        prop_baixa = baixa / total

        # Tamanho médio do corpo (relativo ao range)
        corpos = []
        for c in ultimos:
            rng = c["high"] - c["low"]
            corpo = abs(c["close"] - c["open"])
            corpos.append(corpo / max(rng, 1e-10))
        corpo_medio = sum(corpos) / len(corpos)

        # Último candle: padrão especial
        ult = candles[-1]
        corpo_ult = abs(ult["close"] - ult["open"])
        sombra_inf = ult["open"] if ult["close"] > ult["open"] else ult["close"]
        sombra_sup = ult["high"] - (ult["close"] if ult["close"] > ult["open"] else ult["open"])
        sombra_inf_sz = sombra_inf - ult["low"]
        range_ult = ult["high"] - ult["low"]

        # Martelo (sombra inferior grande = reversão para alta)
        martelo = (sombra_inf_sz > corpo_ult * 2 and
                   ult["close"] > ult["open"] and
                   range_ult > 0)
        # Estrela cadente (sombra superior grande = reversão para baixa)
        estrela = (sombra_sup > corpo_ult * 2 and
                   ult["close"] < ult["open"] and
                   range_ult > 0)

        # Engolfo (último candle engole o anterior)
        ante = candles[-2]
        engolfo_alta = (ult["close"] > ult["open"] and
                        ult["open"] < ante["close"] and
                        ult["close"] > ante["open"])
        engolfo_baixa = (ult["close"] < ult["open"] and
                         ult["open"] > ante["close"] and
                         ult["close"] < ante["open"])

        # Decisão
        if prop_alta > 0.6 or martelo or engolfo_alta:
            direcao = "CALL"
            forca = prop_alta * corpo_medio + (0.2 if martelo or engolfo_alta else 0)
        elif prop_baixa > 0.6 or estrela or engolfo_baixa:
            direcao = "PUT"
            forca = prop_baixa * corpo_medio + (0.2 if estrela or engolfo_baixa else 0)
        else:
            direcao = "NEUTRO"
            forca = 0.0

        forca = min(1.0, forca)

        padrao = ("martelo" if martelo else
                  "estrela_cadente" if estrela else
                  "engolfo_alta" if engolfo_alta else
                  "engolfo_baixa" if engolfo_baixa else
                  "sequencia")

        return {
            "direcao":      direcao,
            "forca":        round(forca, 4),
            "prop_alta":    round(prop_alta, 3),
            "prop_baixa":   round(prop_baixa, 3),
            "corpo_medio":  round(corpo_medio, 3),
            "padrao":       padrao,
            "motivo":       f"{alta}↑/{baixa}↓ últimos 5 | padrão:{padrao}",
        }


# ══════════════════════════════════════════════════════════════════════════════
# 3. MOMENTUM ENGINE — RSI + aceleração de preço
# ══════════════════════════════════════════════════════════════════════════════

class MomentumEngine:
    """
    RSI(14) simplificado + aceleração do slope de closes.
    RSI > 55 → pressão compradora.
    RSI < 45 → pressão vendedora.
    """

    def analisar(self, closes: List[float]) -> dict:
        if len(closes) < 15:
            return {"direcao": "NEUTRO", "forca": 0.0, "rsi": 50.0, "motivo": "dados insuficientes"}

        # RSI(14)
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in diffs[-14:]]
        losses = [max(-d, 0) for d in diffs[-14:]]
        ag = sum(gains) / 14
        al = sum(losses) / 14
        rsi = 100 - (100 / (1 + ag / al)) if al > 0 else (100 if ag > 0 else 50)

        # Aceleração: slope dos últimos 5 vs slope dos 5 anteriores
        slope_rec  = _slope(closes[-5:])
        slope_ante = _slope(closes[-10:-5]) if len(closes) >= 10 else slope_rec
        aceleracao = slope_rec - slope_ante

        # Força do momentum
        rsi_dist = abs(rsi - 50) / 50.0   # 0..1
        acel_norm = min(1.0, abs(aceleracao) * 10000)
        forca = min(1.0, rsi_dist * 0.6 + acel_norm * 0.4)

        if rsi > 55 and slope_rec > 0:
            direcao = "CALL"
        elif rsi < 45 and slope_rec < 0:
            direcao = "PUT"
        else:
            direcao = "NEUTRO"

        return {
            "direcao":    direcao,
            "forca":      round(forca, 4),
            "rsi":        round(rsi, 1),
            "slope_rec":  round(slope_rec, 8),
            "aceleracao": round(aceleracao, 8),
            "motivo":     f"RSI={rsi:.1f} slope={'↑' if slope_rec > 0 else '↓'}",
        }


# ══════════════════════════════════════════════════════════════════════════════
# 4. REGIME ENGINE — ATR + compressão/expansão
# ══════════════════════════════════════════════════════════════════════════════

class RegimeEngine:
    """
    Classifica o regime de mercado usando ATR normalizado:
    - TENDENCIA      : ATR baixo + slope forte
    - EXPANSAO       : ATR crescendo
    - COMPRESSAO     : ATR muito baixo (consolidação)
    - INSTABILIDADE  : ATR muito alto (evitar entrada)
    """

    def analisar(self, candles: List[dict]) -> dict:
        if len(candles) < 15:
            return {"regime": "DESCONHECIDO", "atr_norm": 0.0, "penalidade": 0}

        atr_norm = _atr(candles, 14)

        # Slope dos closes dos últimos 20
        closes = [c["close"] for c in candles[-20:]]
        slope_val = _slope(closes)

        if atr_norm > 0.008:
            regime = "INSTABILIDADE"
            penalidade = 20
        elif atr_norm > 0.004:
            regime = "EXPANSAO"
            penalidade = 5
        elif atr_norm < 0.001:
            regime = "COMPRESSAO"
            penalidade = 3
        elif abs(slope_val) > 0.00005 and atr_norm < 0.004:
            regime = "TENDENCIA"
            penalidade = -5   # bônus
        else:
            regime = "LATERAL"
            penalidade = 8

        return {
            "regime":     regime,
            "atr_norm":   round(atr_norm, 6),
            "slope":      round(slope_val, 8),
            "penalidade": penalidade,
            "motivo":     f"ATR={atr_norm:.5f} regime={regime}",
        }


# ══════════════════════════════════════════════════════════════════════════════
# 5. DECISION ENGINE — consenso ponderado dos 4 módulos
# ══════════════════════════════════════════════════════════════════════════════

class DecisionEngine:
    """
    Pesos dos módulos:
      TrendEngine    40% — tendência é o sinal mais confiável
      CandleEngine   30% — padrões de vela confirmam
      MomentumEngine 30% — RSI + aceleração filtram divergências
    """

    PESOS = {"trend": 0.40, "candle": 0.30, "momentum": 0.30}

    def decidir(
        self,
        trend: dict,
        candle: dict,
        momentum: dict,
        regime: dict,
        confianca_minima: float = 75.0,
    ) -> dict:

        votos: Dict[str, float] = {}
        detalhes = []

        for nome, modulo, peso in [
            ("trend",    trend,    self.PESOS["trend"]),
            ("candle",   candle,   self.PESOS["candle"]),
            ("momentum", momentum, self.PESOS["momentum"]),
        ]:
            d = modulo["direcao"]
            f = modulo["forca"]
            detalhes.append(f"{nome}:{d}({f:.2f})")
            if d in ("CALL", "PUT"):
                votos[d] = votos.get(d, 0) + peso * f

        if not votos:
            return {
                "aprovado": False, "decisao": "NO_TRADE",
                "confianca": 0.0,
                "motivo": "nenhum módulo gerou sinal direcional",
                "detalhes": detalhes,
            }

        melhor = max(votos, key=votos.get)
        total  = sum(votos.values())
        pct_melhor = votos[melhor] / total * 100 if total > 0 else 0

        # Conflito: segundo voto muito próximo
        dirs = sorted(votos.items(), key=lambda x: x[1], reverse=True)
        if len(dirs) > 1:
            pct_segundo = dirs[1][1] / total * 100
            if pct_segundo > 38:
                return {
                    "aprovado": False, "decisao": "NO_TRADE",
                    "confianca": round(pct_melhor, 1),
                    "motivo": f"conflito entre módulos ({pct_segundo:.0f}% divergente)",
                    "detalhes": detalhes,
                }

        # Score base: força ponderada → escala 0–99
        score_bruto = votos[melhor] * 100    # já é 0..100 (pesos somam 1 * forca ≤ 1)
        score_base  = min(99.0, 50 + score_bruto * 50)

        # Penalidade/bônus de regime
        penalidade = regime.get("penalidade", 0)
        confianca  = round(max(0, min(99, score_base - penalidade)), 1)

        if confianca < confianca_minima:
            return {
                "aprovado": False, "decisao": "NO_TRADE",
                "confianca": confianca,
                "motivo": (
                    f"confiança {confianca:.1f}% abaixo do mínimo {confianca_minima:.1f}%"
                    f" | regime:{regime.get('regime','?')}"
                ),
                "detalhes": detalhes,
            }

        return {
            "aprovado":  True,
            "decisao":   melhor,
            "confianca": confianca,
            "motivo":    f"consenso OHLC | regime:{regime.get('regime','?')}",
            "detalhes":  detalhes,
            "votos":     votos,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 6. RISK GATE
# ══════════════════════════════════════════════════════════════════════════════

class RiskGate:
    def validar(
        self,
        decisao: dict,
        regime: dict,
        payout: float,
        losses_seq: int,
        confianca_minima: float,
    ) -> dict:

        vetos = []

        if payout < 0.75:
            vetos.append(f"payout {payout:.0%} < 75% — edge insuficiente")

        if regime.get("regime") == "INSTABILIDADE" and regime.get("atr_norm", 0) > 0.01:
            vetos.append("volatilidade extrema — ATR muito alto")

        # Eleva limiar após losses seguidos
        ajuste = 0
        if losses_seq >= 5:
            ajuste = 10
        elif losses_seq >= 3:
            ajuste = 5

        limiar = confianca_minima + ajuste
        if ajuste > 0 and decisao.get("confianca", 0) < limiar:
            vetos.append(
                f"{losses_seq} losses seguidos → limiar elevado para {limiar:.0f}% "
                f"(atual {decisao.get('confianca', 0):.1f}%)"
            )

        return {
            "aprovado":       len(vetos) == 0,
            "vetos":          vetos,
            "limiar_efetivo": limiar,
            "ajuste_losses":  ajuste,
        }


# ══════════════════════════════════════════════════════════════════════════════
# FUNÇÃO PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def quotex_hma_avaliar(
    candles: List[dict],
    confianca_minima: float = 75.0,
    payout: float = 0.85,
    losses_seq: int = 0,
) -> dict:
    """
    Avalia uma lista de candles OHLC e retorna a decisão do motor.

    Parâmetros:
        candles         — list[{open, high, low, close, time}], mín. 15, ideal 50+
        confianca_minima — threshold mínimo para aprovar (padrão 75)
        payout          — payout decimal ex. 0.85 = 85%
        losses_seq      — número de losses seguidos (para Risk Gate)

    Retorna dict com:
        aprovado, decisao, confianca, motivo, regime, detalhes, risk_gate
    """
    # Valida estrutura mínima
    candles_ok = [
        c for c in candles
        if isinstance(c, dict)
        and c.get("open", 0) > 0
        and c.get("close", 0) > 0
        and c.get("high", 0) > 0
        and c.get("low", 0) > 0
    ]
    candles_ok.sort(key=lambda c: c.get("time", 0))

    if len(candles_ok) < 15:
        return {
            "aprovado": False, "decisao": "NO_TRADE",
            "confianca": 0.0,
            "motivo": f"dados insuficientes: {len(candles_ok)} velas (mín. 15)",
            "regime": "DESCONHECIDO",
            "detalhes": [],
            "risk_gate": {"aprovado": False, "vetos": ["dados insuficientes"]},
        }

    closes = [c["close"] for c in candles_ok]

    # Módulos
    trend    = TrendEngine().analisar(closes)
    candle   = CandleEngine().analisar(candles_ok)
    momentum = MomentumEngine().analisar(closes)
    regime   = RegimeEngine().analisar(candles_ok)

    # Decisão
    decisao = DecisionEngine().decidir(trend, candle, momentum, regime, confianca_minima)

    # Risk Gate
    rg = RiskGate().validar(decisao, regime, payout, losses_seq, confianca_minima)

    aprovado = decisao["aprovado"] and rg["aprovado"]

    return {
        "aprovado":   aprovado,
        "decisao":    decisao["decisao"],
        "confianca":  decisao["confianca"],
        "motivo":     decisao["motivo"] if aprovado else (
            " | ".join(rg["vetos"]) if rg["vetos"] else decisao["motivo"]
        ),
        "regime":     regime["regime"],
        "atr_norm":   regime["atr_norm"],
        "detalhes":   decisao.get("detalhes", []),
        "modulos": {
            "trend":    trend,
            "candle":   candle,
            "momentum": momentum,
            "regime":   regime,
        },
        "risk_gate":  rg,
    }
