"""
GARRA AI CORE — HMA (Hybrid Market Analyzer)
Motor híbrido de decisão multi-timeframe para o BOT GARRA.

Módulos:
  1. TickAnalyzer       — análise tick a tick
  2. MultiTimeframe     — janelas configuráveis (10, 25, 50, 100, 250, 500)
  3. PatternRegime      — detecção de regime de mercado
  4. DecisionEngine     — consenso ponderado por tamanho de janela + limiar adaptativo
  5. RiskGate           — segunda camada: penaliza losses seguidos, veta instabilidade

Melhorias v3 (assertividade + análise):
  - TickAnalyzer: Z-Score dos dígitos, entropia de Shannon, divergência preço×momentum
  - MultiTimeframe: confirmação de quebra de nível + filtro de ruído por SNR
  - PatternRegime: suporte/resistência dinâmicos, detect. de pivot reversal
  - DecisionEngine: peso por qualidade do sinal (vol-ajustado), bonus de consenso
    total e veto quando regime contradiz direção escolhida
  - RiskGate: filtro de horário de baixa liquidez + score por win-rate recente
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


# ─── Utilitário: regressão linear simples ─────────────────────────────────────
def _slope_linear(valores: List[float]) -> float:
    """Retorna o slope (inclinação) da regressão linear de uma série."""
    n = len(valores)
    if n < 2:
        return 0.0
    xi = list(range(n))
    xm = (n - 1) / 2.0
    ym = sum(valores) / n
    num = sum((xi[i] - xm) * (valores[i] - ym) for i in range(n))
    den = sum((xi[i] - xm) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


# ─── Utilitário: entropia de Shannon ─────────────────────────────────────────
def _entropia_shannon(frequencias: List[float]) -> float:
    """
    Calcula a entropia de Shannon normalizada (0=máxima ordem, 1=máximo caos).
    Recebe lista de frequências relativas (deve somar ≈ 1.0).
    """
    H = 0.0
    for p in frequencias:
        if p > 0:
            H -= p * math.log2(p)
    # Normaliza pelo máximo teórico: log2(n_categorias)
    n = len([p for p in frequencias if p > 0])
    max_H = math.log2(max(n, 2))
    return round(H / max_H, 4) if max_H > 0 else 1.0


# ─── Utilitário: Z-Score de uma série ────────────────────────────────────────
def _zscore_ultimo(valores: List[float]) -> float:
    """Z-Score do último valor em relação à série inteira."""
    n = len(valores)
    if n < 3:
        return 0.0
    mu = sum(valores) / n
    sigma = math.sqrt(sum((v - mu) ** 2 for v in valores) / n)
    if sigma < 1e-10:
        return 0.0
    return round((valores[-1] - mu) / sigma, 3)


# ─── Utilitário: Signal-to-Noise Ratio (SNR) ─────────────────────────────────
def _snr(valores: List[float]) -> float:
    """
    Razão sinal/ruído: |slope| / desvio_padrão normalizado.
    SNR alto → tendência clara; SNR baixo → ruído dominante.
    """
    if len(valores) < 4:
        return 0.0
    slope = abs(_slope_linear(valores))
    mu = sum(valores) / len(valores)
    if mu == 0:
        return 0.0
    sigma = math.sqrt(sum((v - mu) ** 2 for v in valores) / len(valores))
    noise = sigma / max(abs(mu), 1e-10)
    return round(slope / max(noise, 1e-10), 4)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TICK ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

class TickAnalyzer:
    """Extrai sinais dos últimos ticks brutos — v3: Z-Score, entropia e divergência."""

    def analisar(self, ticks: List[float]) -> dict:
        if len(ticks) < 5:
            return {"ok": False, "motivo": "ticks insuficientes"}

        digits = [int(str(t).replace('.', '')[-1]) for t in ticks]
        n = len(digits)

        # ── Sequências ──────────────────────────────────────────────────────────
        ultima_seq = 1
        for i in range(n - 2, -1, -1):
            if digits[i] == digits[i + 1]:
                ultima_seq += 1
            else:
                break

        # ── Frequência dos últimos 20 dígitos ───────────────────────────────────
        recentes = digits[-20:]
        freq = [recentes.count(d) / len(recentes) for d in range(10)]
        dig_dominante = freq.index(max(freq))
        freq_dominante = max(freq)

        # ── Entropia de Shannon dos dígitos (mede "aleatoriedade") ──────────────
        # 0 = padrão puro / 1 = totalmente aleatório
        entropia = _entropia_shannon(freq)

        # ── Z-Score do último dígito na distribuição atual ──────────────────────
        # Positivo → dígito raro recente; negativo → dígito muito repetido
        zscore_dig = _zscore_ultimo([float(d) for d in recentes])

        # ── Aceleração do preço ─────────────────────────────────────────────────
        if len(ticks) >= 6:
            v1 = ticks[-2] - ticks[-3]
            v2 = ticks[-1] - ticks[-2]
            aceleracao = v2 - v1
        else:
            aceleracao = 0.0

        # ── Momentum (média dos deslocamentos) ──────────────────────────────────
        deslocamentos = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        momentum = sum(deslocamentos[-10:]) / max(1, min(10, len(deslocamentos)))

        # ── Divergência preço × momentum ────────────────────────────────────────
        # Detecta quando preço sobe mas momentum cai (divergência = sinal de reversão)
        slope_preco   = _slope_linear(ticks[-15:]) if len(ticks) >= 15 else 0.0
        slope_momento = _slope_linear(deslocamentos[-14:]) if len(deslocamentos) >= 14 else 0.0
        divergencia = (slope_preco > 0 and slope_momento < 0) or \
                      (slope_preco < 0 and slope_momento > 0)

        # ── Distribuição dos dígitos (desvio do esperado 10%) ───────────────────
        desvio_max = max(abs(f - 0.10) for f in freq)

        # ── Pares e ímpares recentes ─────────────────────────────────────────────
        n_pares  = sum(1 for d in recentes if d % 2 == 0)
        n_impares = len(recentes) - n_pares
        pct_pares = n_pares / len(recentes)

        # ── Over e Under (barreira 5) ────────────────────────────────────────────
        n_over5  = sum(1 for d in recentes if d > 5)
        n_under5 = sum(1 for d in recentes if d < 5)
        pct_over5  = n_over5  / len(recentes)
        pct_under5 = n_under5 / len(recentes)

        # ── SNR do preço (clareza da tendência vs ruído) ─────────────────────────
        snr_preco = _snr(ticks[-30:]) if len(ticks) >= 10 else 0.0

        return {
            "ok":              True,
            "n_ticks":         n,
            "ultimo_digit":    digits[-1],
            "ultima_seq":      ultima_seq,
            "freq_dominante":  round(freq_dominante, 3),
            "dig_dominante":   dig_dominante,
            "aceleracao":      round(aceleracao, 6),
            "momentum":        round(momentum, 6),
            "desvio_max":      round(desvio_max, 3),
            "pct_pares":       round(pct_pares, 3),
            "pct_over5":       round(pct_over5, 3),
            "pct_under5":      round(pct_under5, 3),
            # ── Novos indicadores v3 ──────────────────────────────────────────
            "entropia":        entropia,          # 0=padrão, 1=caos
            "zscore_dig":      zscore_dig,         # Z-Score último dígito
            "divergencia":     divergencia,        # True → alerta de reversão
            "snr_preco":       round(snr_preco, 4),# sinal/ruído do preço
            "slope_preco":     round(slope_preco, 8),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MULTI-TIMEFRAME ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

class MultiTimeframeAnalyzer:
    """
    Gera sinais para cada janela de ticks configurada.

    Melhoria v2: CALL/PUT AUTO usa regressão linear real (slope dos últimos
    min(25, janela) ticks) em vez de comparação de médias por metade.
    Isso elimina falsos sinais em mercados com movimento concentrado no início
    ou no fim da janela.
    """

    JANELAS_PADRAO = [10, 25, 50, 100, 250, 500]

    def _sinal_janela(self, ticks: List[float], janela: int, contrato: str) -> dict:
        """Retorna sinal (PUT/CALL/OVER/UNDER/NOTR) e confiança para uma janela — v3."""
        if len(ticks) < janela:
            return {"direcao": "NOTR", "confianca": 0, "motivo": "dados insuficientes"}

        slice_t = ticks[-janela:]
        digits = [int(str(t).replace('.', '')[-1]) for t in slice_t]

        n = len(digits)
        n_pares   = sum(1 for d in digits if d % 2 == 0)
        n_over5   = sum(1 for d in digits if d > 5)
        n_under5  = sum(1 for d in digits if d < 5)
        pct_pares   = n_pares   / n
        pct_impares = 1 - pct_pares
        pct_over5   = n_over5   / n
        pct_under5  = n_under5  / n

        # ── Tendência: slope curto (ruído) + slope longo (tendência principal) ──
        #
        # PROBLEMA CORRIGIDO: usar só os últimos 25 ticks para janelas grandes
        # significa que uma pequena correção momentânea pode mascarar uma tendência
        # de alta/baixa clara visível no gráfico. Solução:
        #   - slope_curto: últimos min(25, janela) ticks  → micro-tendência
        #   - slope_longo: janela inteira                 → tendência dominante
        # A tendência dominante tem prioridade quando é forte (>2× o slope_curto).
        #
        slope_curto_n  = min(25, janela)
        slope_longo_n  = janela   # janela inteira

        slope_ticks_curto = slice_t[-slope_curto_n:]
        slope_curto  = _slope_linear(slope_ticks_curto)
        slope_longo  = _slope_linear(slice_t)   # toda a janela

        media_slope  = sum(slice_t) / n
        nivel        = max(abs(media_slope), 0.0001)

        slope_curto_norm = slope_curto / nivel
        slope_longo_norm = slope_longo / nivel

        # Tendência dominante: se o slope longo é forte e contradiz o curto,
        # prevalecer o longo (evita entrar contra a tendência do gráfico).
        _longo_forte  = abs(slope_longo_norm) > abs(slope_curto_norm) * 0.5
        _contradicao  = (slope_longo > 0) != (slope_curto > 0)
        if janela >= 100 and _longo_forte and _contradicao:
            # Usa o slope longo como diretivo (respeita a tendência principal)
            slope      = slope_longo
            slope_norm = slope_longo_norm
        else:
            slope      = slope_curto
            slope_norm = slope_curto_norm

        # Guarda ambos para uso posterior no retorno e no veto do DecisionEngine
        slope_ticks = slope_ticks_curto
        tendencia_alta = slope > 0

        # Flag de conflito: curto e longo apontam em direções opostas
        conflito_slope = _contradicao and janela >= 50

        # ── SNR da janela: distingue tendência real de ruído ─────────────────
        snr = _snr(slope_ticks)
        # Fator de qualidade: SNR alto → mais confiante; SNR baixo → mais cauteloso
        fator_snr = min(1.0, snr / 5.0)   # normaliza: SNR=5 → fator=1.0

        # ── Volatilidade normalizada da janela completa ───────────────────────
        media = sum(slice_t) / n
        variancia = sum((t - media) ** 2 for t in slice_t) / n
        vol = math.sqrt(variancia) / max(abs(media), 0.0001)

        # ── Detecção de quebra de nível (suporte/resistência dinâmico) ────────
        # Compara último tick com máxima e mínima da janela anterior
        if janela >= 20:
            janela_ant = slice_t[:n // 2]
            max_ant = max(janela_ant)
            min_ant = min(janela_ant)
            preco_atual = slice_t[-1]
            quebra_resistencia = preco_atual > max_ant * 1.0001
            quebra_suporte     = preco_atual < min_ant * 0.9999
        else:
            quebra_resistencia = quebra_suporte = False

        # Seleciona direção e confiança conforme contrato solicitado
        contrato_u = contrato.upper()

        if contrato_u in ("DIGITOVER", "OVER"):
            excesso   = pct_over5 - 0.50
            confianca = 50 + excesso * 100
            direcao   = "OVER" if pct_over5 >= 0.50 else "UNDER"

        elif contrato_u in ("DIGITUNDER", "UNDER"):
            excesso   = pct_under5 - 0.50
            confianca = 50 + excesso * 100
            direcao   = "UNDER" if pct_under5 >= 0.50 else "OVER"

        elif contrato_u in ("DIGITEVEN", "EVEN", "PAR"):
            excesso   = pct_impares - 0.50
            confianca = 50 + excesso * 100
            direcao   = "EVEN" if pct_impares >= 0.50 else "ODD"

        elif contrato_u in ("DIGITODD", "ODD", "IMPAR"):
            excesso   = pct_pares - 0.50
            confianca = 50 + excesso * 100
            direcao   = "ODD" if pct_pares >= 0.50 else "EVEN"

        elif contrato_u in ("CALL/PUT AUTO", "CALL_PUT_AUTO", "DIRECIONAL"):
            force = min(1.0, abs(slope_norm) * 5000)
            # v3: confiança base ponderada pelo SNR (sinal mais limpo = mais confiante)
            confianca_base = (65 + force * 23) * (0.70 + 0.30 * fator_snr)
            # Bonus por quebra de nível confirmada na mesma direção
            if tendencia_alta  and quebra_resistencia: confianca_base = min(99, confianca_base + 6)
            if not tendencia_alta and quebra_suporte:  confianca_base = min(99, confianca_base + 6)
            direcao   = "CALL" if tendencia_alta else "PUT"
            confianca = confianca_base

        elif contrato_u == "CALL":
            force     = min(1.0, abs(slope_norm) * 5000)
            confianca = (70 + force * 25) if tendencia_alta else (45 - force * 15)
            confianca *= (0.75 + 0.25 * fator_snr)
            direcao   = "CALL" if tendencia_alta else "PUT"

        elif contrato_u == "PUT":
            force     = min(1.0, abs(slope_norm) * 5000)
            confianca = (70 + force * 25) if not tendencia_alta else (45 - force * 15)
            confianca *= (0.75 + 0.25 * fator_snr)
            direcao   = "PUT" if not tendencia_alta else "CALL"

        else:
            # ── AUTO: avalia todas as 6 direções e escolhe a mais forte ──
            force_cp = min(0.45, abs(slope_norm) * 5000 / 100)
            score_call  = 0.50 + force_cp if tendencia_alta  else 0.50 - force_cp
            score_put   = 1.0 - score_call
            # Boost por quebra de nível
            if quebra_resistencia: score_call = min(0.95, score_call + 0.05)
            if quebra_suporte:     score_put  = min(0.95, score_put  + 0.05)
            opcoes = {
                "OVER":  pct_over5,
                "UNDER": pct_under5,
                "EVEN":  pct_impares,
                "ODD":   pct_pares,
                "CALL":  score_call,
                "PUT":   score_put,
            }
            melhor    = max(opcoes, key=opcoes.get)
            confianca = opcoes[melhor] * 100
            direcao   = melhor if confianca > 52 else "NOTR"

        confianca = round(min(99, max(0, confianca)), 1)

        # ── Penalizações pós-cálculo ──────────────────────────────────────────
        # Alta volatilidade reduz confiança
        if vol > 0.01:
            confianca *= 0.88
        # SNR muito baixo (sinal sujo) penaliza adicionalmente para CALL/PUT
        if contrato_u in ("CALL/PUT AUTO", "CALL_PUT_AUTO", "DIRECIONAL", "CALL", "PUT") \
                and snr < 0.5:
            confianca *= 0.92
        # Conflito curto×longo: janela grande com micro-correção na contramão
        # → penaliza confiança para evitar entradas contra a tendência do gráfico
        if conflito_slope and contrato_u in ("CALL/PUT AUTO", "CALL_PUT_AUTO", "DIRECIONAL", "CALL", "PUT"):
            confianca *= 0.85

        return {
            "janela":              janela,
            "direcao":             direcao,
            "confianca":           round(confianca, 1),
            "pct_pares":           round(pct_pares, 3),
            "pct_over5":           round(pct_over5, 3),
            "volatilidade":        round(vol, 5),
            "tendencia_alta":      tendencia_alta,
            "slope_norm":          round(slope_norm, 8),
            "slope_longo_norm":    round(slope_longo_norm, 8),
            "conflito_slope":      conflito_slope,
            "snr":                 round(snr, 4),
            "quebra_resistencia":  quebra_resistencia,
            "quebra_suporte":      quebra_suporte,
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
            return {"regime": "SEM_VANTAGEM", "score": 0, "volatilidade": "desconhecida",
                    "pivot_reversao": False, "snr_regime": 0.0}

        janela50 = ticks[-50:] if len(ticks) >= 50 else ticks
        n = len(janela50)

        # ── Tendência via regressão linear ────────────────────────────────────
        slope = _slope_linear(janela50)

        # ── Volatilidade ──────────────────────────────────────────────────────
        ym = sum(janela50) / n
        variancia = sum((t - ym) ** 2 for t in janela50) / n
        vol = math.sqrt(variancia)
        vol_norm = vol / max(abs(ym), 0.0001)

        # ── Amplitude da janela ───────────────────────────────────────────────
        amplitude = max(janela50) - min(janela50)

        # ── SNR do regime ─────────────────────────────────────────────────────
        snr_regime = _snr(janela50)

        # ── Detecção de Pivot de reversão ─────────────────────────────────────
        # Um pivot de reversão ocorre quando o slope das últimas 10 velas contradiz
        # o slope das 20 anteriores com magnitude mínima relevante
        pivot_reversao = False
        if len(janela50) >= 30:
            slope_ant = _slope_linear(janela50[-30:-10])
            slope_rec = _slope_linear(janela50[-10:])
            # Sinais opostos E ambos com magnitude suficiente
            if (slope_ant * slope_rec < 0
                    and abs(slope_ant) > 0.00003
                    and abs(slope_rec) > 0.00003):
                pivot_reversao = True

        # ── Detecção de regime ────────────────────────────────────────────────
        regime = "SEM_VANTAGEM"
        score  = 50

        if abs(slope) > 0.0001 and vol_norm < 0.005:
            regime = "TENDENCIA"
            # v3: score reforçado por SNR (tendência clara = mais assertivo)
            score  = 75 + min(20, abs(slope) * 100000) + min(5, snr_regime)
        elif abs(slope) < 0.00005 and vol_norm < 0.003:
            regime = "LATERALIZACAO"
            score  = 60
        elif vol_norm > 0.01:
            regime = "INSTABILIDADE"
            score  = 30
        elif vol_norm > 0.007:
            regime = "EXPANSAO"
            score  = 55
        elif vol_norm < 0.002:
            regime = "COMPRESSAO"
            # v3: compressão de alta qualidade (SNR baixo → energia acumulando)
            score  = 65 + min(5, snr_regime * 2)
        elif abs(slope) > 0.00005 and vol_norm > 0.004:
            slope_rec_check = _slope_linear(janela50[-10:])
            if slope * slope_rec_check < 0 or pivot_reversao:
                regime = "REVERSAO"
                score  = 68
            else:
                regime = "ACELERACAO"
                score  = 62

        # Penaliza score se pivot de reversão detectado em regime TENDENCIA ou ACELERACAO
        if pivot_reversao and regime in ("TENDENCIA", "ACELERACAO"):
            score = max(40, score - 15)

        vol_label = "alta" if vol_norm > 0.007 else ("média" if vol_norm > 0.003 else "baixa")

        return {
            "regime":          regime,
            "score":           round(score, 1),
            "slope":           round(slope, 8),
            "volatilidade":    vol_label,
            "vol_norm":        round(vol_norm, 6),
            "amplitude":       round(amplitude, 6),
            "pivot_reversao":  pivot_reversao,
            "snr_regime":      round(snr_regime, 4),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DECISION ENGINE — Consenso ponderado por tamanho de janela
# ═══════════════════════════════════════════════════════════════════════════════

class DecisionEngine:
    """
    Consolida os votos das janelas em uma decisão final.

    v3 (assertividade):
      - Peso de cada janela = log2(janela) × fator_SNR
      - Limiar de conflito adaptativo
      - Bonus de super-consenso
      - Veto pivot×direção e penalidade de entropia
      - BUGFIX v3.1: veto de inversão tendência longa×curta —
        quando as janelas longas (>=100) apontam na direção OPOSTA
        à decisão, o motor recua para NO_TRADE. Isso evita entrar
        contra a tendência visível no gráfico (ex: gráfico subindo
        mas micro-correção de ticks gera PUT).
    """

    # Tipos de contrato direcionais (afetados pela penalização de lateral)
    _DIRECIONAIS = {"CALL", "PUT"}

    def decidir(
        self,
        sinais_mtf: dict,
        regime: dict,
        tick_info: dict,
        confianca_minima: float = 65.0,
    ) -> dict:

        if not sinais_mtf:
            return {"decisao": "NO_TRADE", "confianca": 0, "motivo": "sem sinais"}

        # ── Agrupa votos por direção com peso = log2(janela) × SNR ───────────
        votos: Dict[str, float] = {}
        detalhes = []

        for janela, sinal in sinais_mtf.items():
            dir_j  = sinal["direcao"]
            conf_j = sinal["confianca"]
            snr_j  = sinal.get("snr", 1.0)
            # v3: peso proporcional à informação da janela E qualidade do sinal
            peso_base = math.log2(max(2, int(janela)))
            fator_snr = min(1.5, max(0.5, 1.0 + (snr_j - 1.0) * 0.1))  # ±50% ajuste suave
            peso_j = peso_base * fator_snr
            if dir_j == "NOTR" or conf_j < 50:
                detalhes.append(f"J{janela}(w{peso_j:.1f}): NOTR ({conf_j:.0f}%)")
                continue
            voto_ponderado = conf_j * peso_j
            votos[dir_j] = votos.get(dir_j, 0) + voto_ponderado
            detalhes.append(f"J{janela}(w{peso_j:.1f}|snr{snr_j:.1f}): {dir_j} {conf_j:.0f}%")

        if not votos:
            return {"decisao": "NO_TRADE", "confianca": 0, "motivo": "todas as janelas neutras"}

        melhor_dir  = max(votos, key=votos.get)
        total_peso  = sum(votos.values())
        consenso_pct = votos[melhor_dir] / total_peso * 100

        # ── BUGFIX v3.1: Veto de inversão tendência longa×curta ──────────────
        # Se as janelas longas (>=100) têm conflito_slope E apontam na direção
        # OPOSTA à decisão dominante → a decisão está contra a tendência do gráfico.
        # Ex: gráfico subindo (slope_longo positivo) mas ticks recentes desceram
        #     → janela 25 diz PUT, janelas 100/250/500 dizem CALL
        #     → sem esse veto, o motor poderia entrar em PUT contra o gráfico.
        _janelas_longas_contra = [
            j for j, s in sinais_mtf.items()
            if int(j) >= 100
            and s.get("conflito_slope")
            and s.get("direcao") != melhor_dir
            and s.get("direcao") not in ("NOTR",)
        ]
        if _janelas_longas_contra and melhor_dir in self._DIRECIONAIS:
            # Verifica se slope_longo das janelas longas aponta contra a decisão
            _slope_longas = [
                sinais_mtf[j].get("slope_longo_norm", 0)
                for j in _janelas_longas_contra
            ]
            _slope_medio_longo = sum(_slope_longas) / len(_slope_longas)
            _contra_tendencia = (
                (melhor_dir == "PUT"  and _slope_medio_longo > 0.00005) or
                (melhor_dir == "CALL" and _slope_medio_longo < -0.00005)
            )
            if _contra_tendencia:
                return {
                    "decisao":  "NO_TRADE",
                    "confianca": round(consenso_pct, 1),
                    "motivo":   (
                        f"VETO: decisao {melhor_dir} contra tendencia longa "
                        f"(slope_longo={_slope_medio_longo:.6f}) — "
                        f"janelas longas conflitantes: {list(_janelas_longas_contra)}"
                    ),
                    "detalhes": detalhes,
                    "votos":    votos,
                }

        # ── v3: Bonus de super-consenso (todas as janelas na mesma direção) ──
        n_janelas_ativas = len(votos)
        n_janelas_melhor = sum(1 for d in [s["direcao"] for s in sinais_mtf.values()]
                               if d == melhor_dir)
        bonus_consenso = 0
        if n_janelas_ativas == 1 and n_janelas_melhor == len(sinais_mtf):
            # Todas as janelas unânimes
            bonus_consenso = 4
        elif consenso_pct >= 90:
            bonus_consenso = 3

        # ── Limiar de conflito adaptativo ─────────────────────────────────────
        if consenso_pct < 60:
            limiar_conflito = 45
        elif consenso_pct < 70:
            limiar_conflito = 40
        else:
            limiar_conflito = 35

        dirs_ordenadas = sorted(votos.items(), key=lambda x: x[1], reverse=True)
        conflito = False
        if len(dirs_ordenadas) > 1:
            segundo_pct = dirs_ordenadas[1][1] / total_peso * 100
            if segundo_pct > limiar_conflito:
                conflito = True

        if conflito:
            return {
                "decisao": "NO_TRADE",
                "confianca": round(consenso_pct, 1),
                "motivo": f"conflito entre timeframes (2º={segundo_pct:.0f}% > limiar {limiar_conflito}%)",
                "detalhes": detalhes,
                "votos": votos,
            }

        # ── Penalização por regime ─────────────────────────────────────────────
        regime_nome     = regime.get("regime", "SEM_VANTAGEM")
        regime_score    = regime.get("score", 50)
        pivot_reversao  = regime.get("pivot_reversao", False)
        penalidade_regime = 0

        if regime_nome == "INSTABILIDADE":
            penalidade_regime = 20
        elif regime_nome == "SEM_VANTAGEM":
            penalidade_regime = 10
        elif regime_nome == "EXPANSAO":
            penalidade_regime = 5
        elif regime_nome == "LATERALIZACAO" and melhor_dir in self._DIRECIONAIS:
            penalidade_regime = 15

        # v3: Veto quando pivot de reversão contradiz a direção escolhida
        if pivot_reversao and melhor_dir in self._DIRECIONAIS:
            slope_regime = regime.get("slope", 0)
            # Se a direção vai CONTRA a reversão detectada → penalidade extra
            contra_reversao = (melhor_dir == "CALL" and slope_regime < 0) or \
                              (melhor_dir == "PUT"  and slope_regime > 0)
            if contra_reversao:
                penalidade_regime += 12

        confianca_final = round(
            consenso_pct * (regime_score / 100) - penalidade_regime + bonus_consenso, 1
        )
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

        # ── Reforço/veto por sinais de tick ───────────────────────────────────
        if tick_info.get("ok"):
            ultima_seq = tick_info.get("ultima_seq", 0)
            if ultima_seq >= 6:
                confianca_final = min(confianca_final * 1.05, 99)

            # v3: divergência de momentum → penaliza CALL/PUT (sinal de reversão iminente)
            if tick_info.get("divergencia") and melhor_dir in self._DIRECIONAIS:
                confianca_final = max(0, confianca_final - 8)

            # v3: mercado muito caótico (alta entropia) → reduz confiança
            entropia = tick_info.get("entropia", 0.5)
            if entropia > 0.92:    # quase aleatório
                confianca_final *= 0.90

        return {
            "decisao":        melhor_dir,
            "confianca":      round(confianca_final, 1),
            "consenso_pct":   round(consenso_pct, 1),
            "regime":         regime_nome,
            "conflito":       False,
            "motivo":         "consenso multi-timeframe ponderado v3",
            "detalhes":       detalhes,
            "votos":          votos,
            "bonus_consenso": bonus_consenso,
            "pivot_reversao": pivot_reversao,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 5. RISK GATE — Validação Final
# ═══════════════════════════════════════════════════════════════════════════════

class RiskGate:
    """
    Segunda camada de validação independente.

    Melhorias v2:
      - historico_recente (losses seguidos) eleva dinamicamente o limiar de
        confiança exigido: após 3 losses seguidos o gate fica 5% mais rigoroso;
        após 5+, mais 5% (máx +10pp sobre o limiar original).
        Isso protege a banca em sequências ruins sem desligar o bot.
      - Veto de volatilidade extrema mantido.
      - Veto de payout < 0.80 mantido.
    """

    def validar(
        self,
        decisao: dict,
        regime: dict,
        banca: float,
        stake: float,
        payout: float,
        historico_recente,       # int: losses seguidos
        confianca_minima: float = 65.0,
        historico_win_rate: float = -1.0,   # win rate recente (-1 = desconhecido)
    ) -> dict:

        motivos_veto = []

        # Veto 1: regime extremamente instável
        if regime.get("regime") == "INSTABILIDADE" and regime.get("vol_norm", 0) > 0.015:
            motivos_veto.append("volatilidade extrema — mercado instável")

        # Veto 2: payout muito baixo
        if payout < 0.80:
            motivos_veto.append(f"payout {payout:.2f} < 0.80 — edge insuficiente")

        # Veto 3: confiança insuficiente após ajuste por losses seguidos
        losses_seq = int(historico_recente) if historico_recente else 0
        ajuste_losses = 0
        if losses_seq >= 5:
            ajuste_losses = 10
        elif losses_seq >= 3:
            ajuste_losses = 5

        limiar_efetivo = confianca_minima + ajuste_losses
        confianca_atual = decisao.get("confianca", 0)

        if ajuste_losses > 0 and confianca_atual < limiar_efetivo:
            motivos_veto.append(
                f"losses seguidos={losses_seq} → limiar elevado para {limiar_efetivo:.0f}% "
                f"(confiança atual {confianca_atual:.1f}%)"
            )

        # ── Veto 4 (v3): win rate recente muito baixo → ambiente desfavorável ──
        # Se o bot está acertando menos de 40% nas últimas N operações → pausa
        if 0.0 <= historico_win_rate < 0.40:
            motivos_veto.append(
                f"win rate recente {historico_win_rate*100:.0f}% < 40% — "
                "ambiente desfavorável, aguardando reversão"
            )

        # ── Veto 5 (v3): pivot de reversão + confiança marginal ──────────────
        # Quando o regime detectou pivot de reversão e confiança não é alta,
        # sobe o limiar para evitar entrar no início de uma reversão
        if regime.get("pivot_reversao") and confianca_atual < (confianca_minima + 8):
            motivos_veto.append(
                f"pivot de reversão detectado — confiança {confianca_atual:.1f}% "
                f"insuficiente (mín {confianca_minima + 8:.0f}% em reversões)"
            )

        aprovado = len(motivos_veto) == 0

        return {
            "aprovado":              aprovado,
            "motivos_veto":          motivos_veto,
            "stake_ok":              banca <= 0 or stake / banca <= 0.05,
            "payout_ok":             payout >= 0.80,
            "ajuste_losses":         ajuste_losses,
            "limiar_efetivo":        limiar_efetivo,
            "win_rate_recente":      round(historico_win_rate, 3) if historico_win_rate >= 0 else None,
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
    historico_recente = None,    # int (losses seguidos) ou list (formato antigo)
    historico_win_rate: float = -1.0,  # win rate recente (0.0–1.0), -1 = desconhecido
) -> dict:
    """
    Avalia os ticks e retorna a decisão completa do GARRA AI CORE HMA — v3.

    Retorna:
        decisao:    "OVER" | "UNDER" | "EVEN" | "ODD" | "CALL" | "PUT" | "NO_TRADE"
        aprovado:   bool — True somente se Decision + RiskGate aprovaram
        confianca:  float — percentual de confiança da decisão
        regime:     string — regime detectado
        detalhes:   list — votos por janela
        analise:    dict — indicadores analíticos extras (entropia, SNR, pivot, divergência)
    """
    if historico_recente is None:
        historico_recente = 0
    elif isinstance(historico_recente, list):
        historico_recente = 0

    # Módulo 1 — Tick Analyzer (v3: entropia, Z-Score, divergência, SNR)
    tick_info = TickAnalyzer().analisar(ticks)

    # Módulo 2 — Multi-Timeframe (v3: SNR por janela, quebra de nível)
    mtf_resultado = MultiTimeframeAnalyzer().analisar(ticks, janelas, contrato)

    # Módulo 3 — Regime (v3: pivot de reversão, SNR regime)
    regime = PatternRegimeEngine().classificar(ticks)

    # Módulo 4 — Decisão (v3: pesos × SNR, bonus consenso, pivot veto, divergência)
    decisao = DecisionEngine().decidir(mtf_resultado, regime, tick_info, confianca_minima)

    # Módulo 5 — Risk Gate (v3: win rate recente, pivot reversão)
    rg = RiskGate().validar(
        decisao, regime, banca, stake, payout,
        historico_recente, confianca_minima,
        historico_win_rate=historico_win_rate,
    )

    aprovado = decisao["decisao"] != "NO_TRADE" and rg["aprovado"]

    # ── Painel analítico (resumo dos indicadores v3) ──────────────────────────
    analise = {
        "entropia":       tick_info.get("entropia"),      # 0=padrão / 1=caos
        "snr_preco":      tick_info.get("snr_preco"),     # clareza do sinal de preço
        "divergencia":    tick_info.get("divergencia"),   # divergência preço×momentum
        "pivot_reversao": regime.get("pivot_reversao"),   # reversão iminente?
        "snr_regime":     regime.get("snr_regime"),       # qualidade do regime
        "bonus_consenso": decisao.get("bonus_consenso", 0),
    }

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
        "analise":         analise,
        "ts":              time.time(),
    }
