"""
GARRA AI CORE — HMA (Hybrid Market Analyzer)
Motor híbrido de decisão multi-timeframe para o BOT GARRA.

Módulos:
  1. TickAnalyzer       — análise tick a tick
  2. MultiTimeframe     — janelas configuráveis (10, 25, 50, 100, 250, 500)
  3. PatternRegime      — detecção de regime de mercado
  4. DecisionEngine     — consenso e confiança final
  5. RiskGate           — segunda camada de verificação antes da ordem

Rota principal: POST /garra-hma/avaliar
Rota histórico:  POST /garra-hma/historico
"""

import json
import os
import math
import time
import threading
from collections import deque
from typing import List, Dict, Tuple, Optional

# ─── Persistência de histórico ────────────────────────────────────────────────
_HMA_HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garra_hma_historico.json")
_hma_hist_lock = threading.Lock()

def _hma_hist_ler() -> list:
    try:
        with open(_HMA_HIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _hma_hist_salvar(entrada: dict):
    with _hma_hist_lock:
        dados = _hma_hist_ler()
        dados.append(entrada)
        dados = dados[-5000:]   # mantém últimas 5000 operações
        with open(_HMA_HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TICK ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

class TickAnalyzer:
    """Extrai sinais dos últimos ticks brutos."""

    def analisar(self, ticks: List[float]) -> dict:
        if len(ticks) < 5:
            return {"ok": False, "motivo": "ticks insuficientes"}

        digits = [int(str(t).replace('.', '')[-1]) for t in ticks]
        n = len(digits)

        # Sequências
        ultima_seq = 1
        for i in range(n - 2, -1, -1):
            if digits[i] == digits[i + 1]:
                ultima_seq += 1
            else:
                break

        # Frequência dos últimos 20 dígitos
        recentes = digits[-20:]
        freq = [recentes.count(d) / len(recentes) for d in range(10)]
        dig_dominante = freq.index(max(freq))
        freq_dominante = max(freq)

        # Aceleração do preço
        if len(ticks) >= 6:
            v1 = ticks[-2] - ticks[-3]
            v2 = ticks[-1] - ticks[-2]
            aceleracao = v2 - v1
        else:
            aceleracao = 0.0

        # Momentum (média dos deslocamentos)
        deslocamentos = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        momentum = sum(deslocamentos[-10:]) / max(1, min(10, len(deslocamentos)))

        # Distribuição dos dígitos (desvio do esperado 10%)
        desvio_max = max(abs(f - 0.10) for f in freq)

        # Pares e ímpares recentes
        n_pares = sum(1 for d in recentes if d % 2 == 0)
        n_impares = len(recentes) - n_pares
        pct_pares = n_pares / len(recentes)

        # Over e Under (barreira 5)
        n_over5 = sum(1 for d in recentes if d > 5)
        n_under5 = sum(1 for d in recentes if d < 5)
        pct_over5 = n_over5 / len(recentes)
        pct_under5 = n_under5 / len(recentes)

        return {
            "ok": True,
            "n_ticks": n,
            "ultimo_digit": digits[-1],
            "ultima_seq": ultima_seq,
            "freq_dominante": round(freq_dominante, 3),
            "dig_dominante": dig_dominante,
            "aceleracao": round(aceleracao, 6),
            "momentum": round(momentum, 6),
            "desvio_max": round(desvio_max, 3),
            "pct_pares": round(pct_pares, 3),
            "pct_over5": round(pct_over5, 3),
            "pct_under5": round(pct_under5, 3),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MULTI-TIMEFRAME ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

class MultiTimeframeAnalyzer:
    """Gera sinais para cada janela de ticks configurada."""

    JANELAS_PADRAO = [10, 25, 50, 100, 250, 500]

    def _sinal_janela(self, ticks: List[float], janela: int, contrato: str) -> dict:
        """Retorna sinal (PUT/CALL/OVER/UNDER/NOTR) e confiança para uma janela."""
        if len(ticks) < janela:
            return {"direcao": "NOTR", "confianca": 0, "motivo": "dados insuficientes"}

        slice_t = ticks[-janela:]
        digits = [int(str(t).replace('.', '')[-1]) for t in slice_t]

        n = len(digits)
        n_pares = sum(1 for d in digits if d % 2 == 0)
        n_over5 = sum(1 for d in digits if d > 5)
        n_under5 = sum(1 for d in digits if d < 5)
        pct_pares = n_pares / n
        pct_impares = 1 - pct_pares
        pct_over5 = n_over5 / n
        pct_under5 = n_under5 / n

        # Tendência de preço
        metade = n // 2
        media_prim = sum(slice_t[:metade]) / metade
        media_seg = sum(slice_t[metade:]) / (n - metade)
        tendencia_alta = media_seg > media_prim

        # Volatilidade normalizada
        media = sum(slice_t) / n
        variancia = sum((t - media) ** 2 for t in slice_t) / n
        vol = math.sqrt(variancia) / max(abs(media), 0.0001)

        # Seleciona direção e confiança conforme contrato solicitado
        contrato_u = contrato.upper()

        if contrato_u in ("DIGITOVER", "OVER"):
            # Over vence quando último dígito > barreira. Proxy: pct_over5
            excesso = pct_over5 - 0.50   # quanto acima do neutro
            confianca = 50 + excesso * 100
            direcao = "OVER" if pct_over5 >= 0.50 else "UNDER"
        elif contrato_u in ("DIGITUNDER", "UNDER"):
            excesso = pct_under5 - 0.50
            confianca = 50 + excesso * 100
            direcao = "UNDER" if pct_under5 >= 0.50 else "OVER"
        elif contrato_u in ("DIGITEVEN", "EVEN", "PAR"):
            excesso = pct_impares - 0.50   # muitos ímpares → apostar PAR
            confianca = 50 + excesso * 100
            direcao = "EVEN" if pct_impares >= 0.50 else "ODD"
        elif contrato_u in ("DIGITODD", "ODD", "IMPAR"):
            excesso = pct_pares - 0.50
            confianca = 50 + excesso * 100
            direcao = "ODD" if pct_pares >= 0.50 else "EVEN"
        elif contrato_u in ("CALL/PUT AUTO", "CALL_PUT_AUTO", "DIRECIONAL"):
            # Motor escolhe sozinho CALL ou PUT pela tendência de preço
            # Slope + momentum + aceleração combinados
            slope_score = abs(media_seg - media_prim) / max(abs(media_prim), 0.0001)
            confianca_base = min(95, 50 + slope_score * 5000)
            direcao = "CALL" if tendencia_alta else "PUT"
            confianca = confianca_base
        elif contrato_u == "CALL":
            confianca = 60 + 30 * (1 if tendencia_alta else -1)
            direcao = "CALL" if tendencia_alta else "PUT"
        elif contrato_u == "PUT":
            confianca = 60 + 30 * (1 if not tendencia_alta else -1)
            direcao = "PUT" if not tendencia_alta else "CALL"
        else:
            # Automático: avalia OVER/UNDER/EVEN/ODD + CALL/PUT
            # Score de CALL/PUT baseado na tendência de preço
            score_call = 0.50 + min(0.45, abs(media_seg - media_prim) / max(abs(media_prim), 0.0001) * 5) \
                         if tendencia_alta else 0.50 - min(0.45, abs(media_seg - media_prim) / max(abs(media_prim), 0.0001) * 5)
            score_put  = 1.0 - score_call
            opcoes = {
                "OVER":  pct_over5,
                "UNDER": pct_under5,
                "EVEN":  pct_impares,
                "ODD":   pct_pares,
                "CALL":  score_call,
                "PUT":   score_put,
            }
            melhor = max(opcoes, key=opcoes.get)
            confianca = opcoes[melhor] * 100
            direcao = melhor if confianca > 52 else "NOTR"

        confianca = round(min(99, max(0, confianca)), 1)

        # Penalização por alta volatilidade
        if vol > 0.01:
            confianca *= 0.88

        return {
            "janela": janela,
            "direcao": direcao,
            "confianca": round(confianca, 1),
            "pct_pares": round(pct_pares, 3),
            "pct_over5": round(pct_over5, 3),
            "volatilidade": round(vol, 5),
            "tendencia_alta": tendencia_alta,
        }

    def analisar(self, ticks: List[float], janelas: Optional[List[int]], contrato: str) -> dict:
        if not janelas:
            # Seleção automática: só janelas com dados suficientes
            janelas = [j for j in self.JANELAS_PADRAO if len(ticks) >= j]

        resultados = {}
        for j in janelas:
            resultados[j] = self._sinal_janela(ticks, j, contrato)

        return resultados


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PATTERN / REGIME ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class PatternRegimeEngine:
    """Classifica o regime atual do mercado."""

    REGIMES = [
        "TENDENCIA",
        "REVERSAO",
        "LATERALIZACAO",
        "ACELERACAO",
        "COMPRESSAO",
        "EXPANSAO",
        "INSTABILIDADE",
        "SEM_VANTAGEM",
    ]

    def classificar(self, ticks: List[float]) -> dict:
        if len(ticks) < 30:
            return {"regime": "SEM_VANTAGEM", "score": 0, "volatilidade": "desconhecida"}

        janela50 = ticks[-50:] if len(ticks) >= 50 else ticks
        n = len(janela50)

        # Tendência via regressão linear simples
        xi = list(range(n))
        xm = sum(xi) / n
        ym = sum(janela50) / n
        num = sum((xi[i] - xm) * (janela50[i] - ym) for i in range(n))
        den = sum((xi[i] - xm) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0.0

        # Volatilidade
        variancia = sum((t - ym) ** 2 for t in janela50) / n
        vol = math.sqrt(variancia)
        vol_norm = vol / max(abs(ym), 0.0001)

        # Amplitude da janela
        amplitude = max(janela50) - min(janela50)

        # Detecção de regime
        regime = "SEM_VANTAGEM"
        score = 50

        if abs(slope) > 0.0001 and vol_norm < 0.005:
            regime = "TENDENCIA"
            score = 75 + min(20, abs(slope) * 100000)
        elif abs(slope) < 0.00005 and vol_norm < 0.003:
            regime = "LATERALIZACAO"
            score = 60
        elif vol_norm > 0.01:
            regime = "INSTABILIDADE"
            score = 30   # penaliza
        elif vol_norm > 0.007:
            regime = "EXPANSAO"
            score = 55
        elif vol_norm < 0.002:
            regime = "COMPRESSAO"
            score = 65   # mercado comprimido tende a explodir — neutro
        elif abs(slope) > 0.00005 and vol_norm > 0.004:
            # Alta volatilidade + slope → pode ser aceleração ou reversão
            # Checa se últimos 10 ticks foram na direção oposta aos 40 anteriores
            slope_recente_num = sum((xi[-10:][i] - sum(xi[-10:]) / 10) * (janela50[-10:][i] - sum(janela50[-10:]) / 10) for i in range(10))
            slope_recente_den = sum((xi[-10:][i] - sum(xi[-10:]) / 10) ** 2 for i in range(10))
            slope_rec = slope_recente_num / slope_recente_den if slope_recente_den != 0 else 0
            if slope * slope_rec < 0:
                regime = "REVERSAO"
                score = 68
            else:
                regime = "ACELERACAO"
                score = 62

        vol_label = "alta" if vol_norm > 0.007 else ("média" if vol_norm > 0.003 else "baixa")

        return {
            "regime": regime,
            "score": round(score, 1),
            "slope": round(slope, 8),
            "volatilidade": vol_label,
            "vol_norm": round(vol_norm, 6),
            "amplitude": round(amplitude, 6),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DECISION ENGINE — Consenso Multi-Timeframe
# ═══════════════════════════════════════════════════════════════════════════════

class DecisionEngine:
    """Consolida os votos das janelas em uma decisão final com score de confiança."""

    def decidir(
        self,
        sinais_mtf: dict,
        regime: dict,
        tick_info: dict,
        confianca_minima: float = 65.0,
    ) -> dict:

        if not sinais_mtf:
            return {"decisao": "NO_TRADE", "confianca": 0, "motivo": "sem sinais"}

        # Agrupa votos por direção (com peso proporcional à confiança)
        votos: Dict[str, float] = {}
        detalhes = []

        for janela, sinal in sinais_mtf.items():
            dir_j = sinal["direcao"]
            conf_j = sinal["confianca"]
            if dir_j == "NOTR" or conf_j < 50:
                detalhes.append(f"J{janela}: NOTR ({conf_j:.0f}%)")
                continue
            votos[dir_j] = votos.get(dir_j, 0) + conf_j
            detalhes.append(f"J{janela}: {dir_j} {conf_j:.0f}%")

        if not votos:
            return {"decisao": "NO_TRADE", "confianca": 0, "motivo": "todas as janelas neutras"}

        melhor_dir = max(votos, key=votos.get)
        total_peso = sum(votos.values())
        consenso_pct = votos[melhor_dir] / total_peso * 100

        # Verifica conflito: se a segunda direção tiver > 35% do peso → conflito
        dirs_ordenadas = sorted(votos.items(), key=lambda x: x[1], reverse=True)
        conflito = False
        if len(dirs_ordenadas) > 1:
            segundo_pct = dirs_ordenadas[1][1] / total_peso * 100
            if segundo_pct > 35:
                conflito = True

        if conflito:
            return {
                "decisao": "NO_TRADE",
                "confianca": round(consenso_pct, 1),
                "motivo": "conflito entre timeframes",
                "detalhes": detalhes,
                "votos": votos,
            }

        # Penalização por regime instável
        regime_nome = regime.get("regime", "SEM_VANTAGEM")
        regime_score = regime.get("score", 50)
        penalidade_regime = 0

        if regime_nome == "INSTABILIDADE":
            penalidade_regime = 20
        elif regime_nome == "SEM_VANTAGEM":
            penalidade_regime = 10
        elif regime_nome == "EXPANSAO":
            penalidade_regime = 5

        confianca_final = round(consenso_pct * (regime_score / 100) - penalidade_regime, 1)
        confianca_final = max(0, min(99, confianca_final))

        if confianca_final < confianca_minima:
            return {
                "decisao": "NO_TRADE",
                "confianca": confianca_final,
                "motivo": f"confiança {confianca_final:.1f}% abaixo do mínimo {confianca_minima:.1f}%",
                "detalhes": detalhes,
                "votos": votos,
                "regime": regime_nome,
            }

        # Verifica sinais de tick para reforço ou veto
        tick_ok = True
        if tick_info.get("ok"):
            ultima_seq = tick_info.get("ultima_seq", 0)
            if ultima_seq >= 6:
                confianca_final = min(confianca_final * 1.05, 99)   # sequência longa reforça
            if tick_info.get("volatilidade", "baixa") == "alta":
                confianca_final *= 0.90

        return {
            "decisao": melhor_dir,
            "confianca": round(confianca_final, 1),
            "consenso_pct": round(consenso_pct, 1),
            "regime": regime_nome,
            "conflito": False,
            "motivo": "consenso multi-timeframe",
            "detalhes": detalhes,
            "votos": votos,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 5. RISK GATE — Validação Final
# ═══════════════════════════════════════════════════════════════════════════════

class RiskGate:
    """Segunda camada de validação independente. Pode vetar decisões da IA."""

    def validar(
        self,
        decisao: dict,
        regime: dict,
        banca: float,
        stake: float,
        payout: float,
        historico_recente: list,
    ) -> dict:

        motivos_veto = []

        # Veto 1: regime extremamente instável mesmo com confiança alta
        if regime.get("regime") == "INSTABILIDADE" and regime.get("vol_norm", 0) > 0.015:
            motivos_veto.append("volatilidade extrema — mercado instável")

        # Veto 2: risco por operação > 5% da banca
        if banca > 0 and stake / banca > 0.05:
            motivos_veto.append(f"stake {stake:.2f} > 5% da banca {banca:.2f}")

        # Veto 3: payout muito baixo (< 0.80) = edge insuficiente
        if payout < 0.80:
            motivos_veto.append(f"payout {payout:.2f} < 0.80 — edge insuficiente")

        # Veto 4: sequência recente de losses (últimas 5 op = 4+ losses)
        if len(historico_recente) >= 5:
            ultimas5 = historico_recente[-5:]
            losses = sum(1 for r in ultimas5 if r.get("resultado") == "LOSS")
            if losses >= 4:
                motivos_veto.append(f"{losses}/5 últimas operações foram loss — aguardar")

        aprovado = len(motivos_veto) == 0

        return {
            "aprovado": aprovado,
            "motivos_veto": motivos_veto,
            "stake_ok": banca <= 0 or stake / banca <= 0.05,
            "payout_ok": payout >= 0.80,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# FUNÇÃO PRINCIPAL — chamada pela rota Flask
# ═══════════════════════════════════════════════════════════════════════════════

def hma_avaliar(
    ticks: List[float],
    contrato: str = "AUTO",
    janelas: Optional[List[int]] = None,
    confianca_minima: float = 65.0,
    banca: float = 100.0,
    stake: float = 1.0,
    payout: float = 0.85,
    historico_recente: Optional[list] = None,
) -> dict:
    """
    Avalia os ticks e retorna a decisão completa do GARRA AI CORE HMA.

    Retorna:
        decisao:    "OVER" | "UNDER" | "EVEN" | "ODD" | "CALL" | "PUT" | "NO_TRADE"
        aprovado:   bool — True somente se Decision + RiskGate aprovaram
        confianca:  float — percentual de confiança da decisão
        regime:     string — regime detectado
        detalhes:   list — votos por janela
    """
    if historico_recente is None:
        historico_recente = _hma_hist_ler()[-20:]

    # Módulo 1 — Tick Analyzer
    tick_info = TickAnalyzer().analisar(ticks)

    # Módulo 2 — Multi-Timeframe
    mtf_resultado = MultiTimeframeAnalyzer().analisar(ticks, janelas, contrato)

    # Módulo 3 — Regime
    regime = PatternRegimeEngine().classificar(ticks)

    # Módulo 4 — Decisão
    decisao = DecisionEngine().decidir(mtf_resultado, regime, tick_info, confianca_minima)

    # Módulo 5 — Risk Gate
    rg = RiskGate().validar(decisao, regime, banca, stake, payout, historico_recente)

    aprovado = decisao["decisao"] != "NO_TRADE" and rg["aprovado"]

    return {
        "aprovado":        aprovado,
        "decisao":         decisao["decisao"],
        "confianca":       decisao["confianca"],
        "consenso_pct":    decisao.get("consenso_pct", 0),
        "regime":          regime["regime"],
        "regime_score":    regime["score"],
        "volatilidade":    regime["volatilidade"],
        "conflito":        decisao.get("conflito", False),
        "motivo":          decisao.get("motivo", ""),
        "detalhes_mtf":    decisao.get("detalhes", []),
        "votos":           decisao.get("votos", {}),
        "tick_info":       tick_info,
        "risk_gate":       rg,
        "janelas_usadas":  list(mtf_resultado.keys()),
        "ts":              time.time(),
    }
