"""
GARRA AI CORE — HMA (Hybrid Market Analyzer)
Motor híbrido de decisão multi-timeframe para o BOT GARRA.

Módulos:
  1. TickAnalyzer       — análise tick a tick
  2. MultiTimeframe     — janelas configuráveis (10, 25, 50, 100, 250, 500)
  3. PatternRegime      — detecção de regime de mercado
  4. DecisionEngine     — consenso ponderado por tamanho de janela + limiar adaptativo
  5. RiskGate           — segunda camada: penaliza losses seguidos, veta instabilidade

Melhorias v2:
  - Peso de cada janela proporcional ao log2(janela) — janelas grandes pesam mais
  - CALL/PUT AUTO usa regressão linear real (slope dos últimos 25 ticks)
  - Limiar de conflito adaptativo: aumenta quando confiança total é baixa
  - historico_recente (losses seguidos) eleva limiar de confiança no Risk Gate
  - LATERALIZACAO penaliza CALL/PUT (mas não DIGIT)
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
    xm = (n - 1) / 2.0          # média dos índices
    ym = sum(valores) / n
    num = sum((xi[i] - xm) * (valores[i] - ym) for i in range(n))
    den = sum((xi[i] - xm) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


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
        n_over5  = sum(1 for d in recentes if d > 5)
        n_under5 = sum(1 for d in recentes if d < 5)
        pct_over5  = n_over5  / len(recentes)
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
    """
    Gera sinais para cada janela de ticks configurada.

    Melhoria v2: CALL/PUT AUTO usa regressão linear real (slope dos últimos
    min(25, janela) ticks) em vez de comparação de médias por metade.
    Isso elimina falsos sinais em mercados com movimento concentrado no início
    ou no fim da janela.
    """

    JANELAS_PADRAO = [10, 25, 50, 100, 250, 500]

    def _sinal_janela(self, ticks: List[float], janela: int, contrato: str) -> dict:
        """Retorna sinal (PUT/CALL/OVER/UNDER/NOTR) e confiança para uma janela."""
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

        # ── Slope LONGO: regressão sobre toda a janela ────────────────────────
        slope_longo = _slope_linear(slice_t)
        media_slice = sum(slice_t) / n
        slope_longo_norm = slope_longo / max(abs(media_slice), 0.0001)

        # ── Slope CURTO: últimos min(15, janela//3) ticks — tendência recente ─
        slope_curto_n = max(5, min(15, janela // 3))
        slope_curto_ticks = slice_t[-slope_curto_n:]
        slope_curto = _slope_linear(slope_curto_ticks)
        media_curto = sum(slope_curto_ticks) / len(slope_curto_ticks)
        slope_curto_norm = slope_curto / max(abs(media_curto), 0.0001)

        # Direção confirmada somente quando slope curto E longo apontam o mesmo lado
        tendencia_alta_longa = slope_longo > 0
        tendencia_alta_curta = slope_curto > 0
        tendencia_confirmada = (tendencia_alta_longa == tendencia_alta_curta)
        tendencia_alta = tendencia_alta_curta   # usamos o curto como sinal principal

        # ── Volatilidade normalizada da janela completa ────────────────────────
        variancia = sum((t - media_slice) ** 2 for t in slice_t) / n
        vol = math.sqrt(variancia) / max(abs(media_slice), 0.0001)

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
            # ── Slope duplo: curto + longo devem concordar ────────────────────
            # force proporcional à MÉDIA dos slopes curto e longo — evita inflação
            # quando apenas um dos dois é forte.
            force_c = min(1.0, abs(slope_curto_norm) * 3000)
            force_l = min(1.0, abs(slope_longo_norm) * 3000)
            force   = (force_c + force_l) / 2.0   # média — mais conservador

            if not tendencia_confirmada:
                # Slopes divergem — confiança cai para zona de NO_TRADE
                confianca = 45.0
                direcao   = "NOTR"
            else:
                # Confiança parte de 55 (força zero) e vai até 90 (força máxima)
                # Muito mais honesto que o antigo 72..95 fixo
                confianca = 55.0 + force * 35.0
                direcao   = "CALL" if tendencia_alta else "PUT"

        elif contrato_u == "CALL":
            force_c = min(1.0, abs(slope_curto_norm) * 3000)
            force_l = min(1.0, abs(slope_longo_norm) * 3000)
            force   = (force_c + force_l) / 2.0
            if not tendencia_confirmada:
                confianca = 45.0
                direcao   = "NOTR"
            elif tendencia_alta:
                confianca = 55.0 + force * 35.0
                direcao   = "CALL"
            else:
                confianca = 40.0 - force * 10.0
                direcao   = "PUT"

        elif contrato_u == "PUT":
            force_c = min(1.0, abs(slope_curto_norm) * 3000)
            force_l = min(1.0, abs(slope_longo_norm) * 3000)
            force   = (force_c + force_l) / 2.0
            if not tendencia_confirmada:
                confianca = 45.0
                direcao   = "NOTR"
            elif not tendencia_alta:
                confianca = 55.0 + force * 35.0
                direcao   = "PUT"
            else:
                confianca = 40.0 - force * 10.0
                direcao   = "CALL"

        else:
            # ── AUTO: avalia todas as 6 direções e escolhe a mais forte ──
            force_c  = min(1.0, abs(slope_curto_norm) * 3000)
            force_l  = min(1.0, abs(slope_longo_norm) * 3000)
            force_cp = (force_c + force_l) / 2.0 * 0.45
            if tendencia_confirmada:
                score_call = 0.50 + force_cp if tendencia_alta else 0.50 - force_cp
            else:
                score_call = 0.50   # sem confirmação → neutro
            score_put = 1.0 - score_call
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
            direcao   = melhor if confianca > 53 else "NOTR"

        confianca = round(min(99, max(0, confianca)), 1)

        # ── Penalização por alta volatilidade ─────────────────────────────────
        if vol > 0.008:
            confianca *= 0.85   # penaliza mais cedo e mais forte
        elif vol > 0.005:
            confianca *= 0.93

        return {
            "janela":              janela,
            "direcao":             direcao,
            "confianca":           round(confianca, 1),
            "pct_pares":           round(pct_pares, 3),
            "pct_over5":           round(pct_over5, 3),
            "volatilidade":        round(vol, 5),
            "tendencia_alta":      tendencia_alta,
            "tendencia_confirmada": tendencia_confirmada,
            "slope_curto_norm":    round(slope_curto_norm, 8),
            "slope_longo_norm":    round(slope_longo_norm, 8),
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

        # Tendência via regressão linear (agora usa o utilitário compartilhado)
        slope = _slope_linear(janela50)

        # Volatilidade
        ym = sum(janela50) / n
        variancia = sum((t - ym) ** 2 for t in janela50) / n
        vol = math.sqrt(variancia)
        vol_norm = vol / max(abs(ym), 0.0001)

        # Amplitude da janela
        amplitude = max(janela50) - min(janela50)

        # Detecção de regime
        regime = "SEM_VANTAGEM"
        score  = 50

        if abs(slope) > 0.0001 and vol_norm < 0.005:
            regime = "TENDENCIA"
            score  = 75 + min(20, abs(slope) * 100000)
        elif abs(slope) < 0.00005 and vol_norm < 0.003:
            regime = "LATERALIZACAO"
            score  = 60
        elif vol_norm > 0.01:
            regime = "INSTABILIDADE"
            score  = 30   # penaliza
        elif vol_norm > 0.007:
            regime = "EXPANSAO"
            score  = 55
        elif vol_norm < 0.002:
            regime = "COMPRESSAO"
            score  = 65
        elif abs(slope) > 0.00005 and vol_norm > 0.004:
            # Alta volatilidade + slope → aceleração ou reversão
            slope_rec = _slope_linear(janela50[-10:])
            if slope * slope_rec < 0:
                regime = "REVERSAO"
                score  = 68
            else:
                regime = "ACELERACAO"
                score  = 62

        vol_label = "alta" if vol_norm > 0.007 else ("média" if vol_norm > 0.003 else "baixa")

        return {
            "regime":      regime,
            "score":       round(score, 1),
            "slope":       round(slope, 8),
            "volatilidade": vol_label,
            "vol_norm":    round(vol_norm, 6),
            "amplitude":   round(amplitude, 6),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DECISION ENGINE — Consenso ponderado por tamanho de janela
# ═══════════════════════════════════════════════════════════════════════════════

class DecisionEngine:
    """
    Consolida os votos das janelas em uma decisão final.

    Melhorias v2:
      - Peso de cada janela = log2(janela) em vez de peso uniforme.
        Janela 500 ticks pesa ~8.97×, janela 10 ticks pesa ~3.32×.
        Isso reduz o impacto do ruído das janelas curtas.
      - Limiar de conflito adaptativo:
        · Base: 35% (como antes)
        · Sobe para 40% se confiança total < 70% (mercado incerto)
        · Sobe para 45% se confiança total < 60% (mercado muito incerto)
      - Penalização de CALL/PUT em regime LATERALIZACAO:
        DIGIT não é penalizado (lateralização é neutra para dígitos).
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

        # ── Agrupa votos por direção com peso = log2(janela) ──────────────────
        votos: Dict[str, float] = {}
        contagem_dir: Dict[str, int] = {}   # quantas janelas votaram em cada direção
        detalhes = []
        total_janelas_validas = 0

        for janela, sinal in sinais_mtf.items():
            dir_j  = sinal["direcao"]
            conf_j = sinal["confianca"]
            peso_j = math.log2(max(2, int(janela)))
            if dir_j == "NOTR" or conf_j < 50:
                detalhes.append(f"J{janela}(w{peso_j:.1f}): NOTR ({conf_j:.0f}%)")
                continue
            total_janelas_validas += 1
            voto_ponderado = conf_j * peso_j
            votos[dir_j] = votos.get(dir_j, 0) + voto_ponderado
            contagem_dir[dir_j] = contagem_dir.get(dir_j, 0) + 1
            detalhes.append(f"J{janela}(w{peso_j:.1f}): {dir_j} {conf_j:.0f}%")

        if not votos:
            return {"decisao": "NO_TRADE", "confianca": 0, "motivo": "todas as janelas neutras"}

        melhor_dir  = max(votos, key=votos.get)
        total_peso  = sum(votos.values())
        consenso_pct = votos[melhor_dir] / total_peso * 100

        # ── Mínimo de janelas concordantes ────────────────────────────────────
        # Exige que pelo menos 2 janelas votem na mesma direção — evita entradas
        # baseadas em 1 única janela que inflou a confiança.
        janelas_concordando = contagem_dir.get(melhor_dir, 0)
        if janelas_concordando < 2:
            return {
                "decisao": "NO_TRADE",
                "confianca": round(consenso_pct, 1),
                "motivo": f"apenas {janelas_concordando} janela confirma {melhor_dir} — mínimo 2",
                "detalhes": detalhes,
                "votos": votos,
            }

        # ── Limiar de conflito mais rigoroso ──────────────────────────────────
        # 2ª direção com >25% já é conflito — antes era 35%.
        # Quando temos poucas janelas o limiar cai ainda mais.
        if total_janelas_validas <= 2:
            limiar_conflito = 20   # poucas janelas → qualquer oposição veta
        elif consenso_pct < 60:
            limiar_conflito = 25
        elif consenso_pct < 75:
            limiar_conflito = 30
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
        regime_nome  = regime.get("regime", "SEM_VANTAGEM")
        regime_score = regime.get("score", 50)
        penalidade_regime = 0

        if regime_nome == "INSTABILIDADE":
            penalidade_regime = 20
        elif regime_nome == "SEM_VANTAGEM":
            penalidade_regime = 10
        elif regime_nome == "EXPANSAO":
            penalidade_regime = 5
        elif regime_nome == "LATERALIZACAO" and melhor_dir in self._DIRECIONAIS:
            # Lateralização é armadilha para CALL/PUT mas neutra para DIGIT
            penalidade_regime = 15

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

        # ── Reforço/veto por sinais de tick ───────────────────────────────────
        if tick_info.get("ok"):
            ultima_seq = tick_info.get("ultima_seq", 0)
            if ultima_seq >= 6:
                confianca_final = min(confianca_final * 1.05, 99)

        return {
            "decisao":      melhor_dir,
            "confianca":    round(confianca_final, 1),
            "consenso_pct": round(consenso_pct, 1),
            "regime":       regime_nome,
            "conflito":     False,
            "motivo":       "consenso multi-timeframe ponderado",
            "detalhes":     detalhes,
            "votos":        votos,
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
    ) -> dict:

        motivos_veto = []

        # Veto 1: regime extremamente instável mesmo com confiança alta
        if regime.get("regime") == "INSTABILIDADE" and regime.get("vol_norm", 0) > 0.015:
            motivos_veto.append("volatilidade extrema — mercado instável")

        # Veto 2: payout muito baixo
        if payout < 0.80:
            motivos_veto.append(f"payout {payout:.2f} < 0.80 — edge insuficiente")

        # Veto 3: confiança insuficiente após ajuste por losses seguidos
        losses_seq = int(historico_recente) if historico_recente else 0
        # +5pp após 3 losses seguidos, +10pp após 5+
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

        aprovado = len(motivos_veto) == 0

        return {
            "aprovado":         aprovado,
            "motivos_veto":     motivos_veto,
            "stake_ok":         banca <= 0 or stake / banca <= 0.05,
            "payout_ok":        payout >= 0.80,
            "ajuste_losses":    ajuste_losses,
            "limiar_efetivo":   limiar_efetivo,
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
    historico_recente = None,   # int (losses seguidos) ou list (formato antigo)
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
    # Aceita int (losses seguidos vindos do JS) ou None
    if historico_recente is None:
        historico_recente = 0
    elif isinstance(historico_recente, list):
        historico_recente = 0   # formato antigo ignorado

    # Módulo 1 — Tick Analyzer
    tick_info = TickAnalyzer().analisar(ticks)

    # Módulo 2 — Multi-Timeframe (com slope real e pesos)
    mtf_resultado = MultiTimeframeAnalyzer().analisar(ticks, janelas, contrato)

    # Módulo 3 — Regime
    regime = PatternRegimeEngine().classificar(ticks)

    # Módulo 4 — Decisão (pesos por janela + limiar adaptativo + penalidade lateral)
    decisao = DecisionEngine().decidir(mtf_resultado, regime, tick_info, confianca_minima)

    # Módulo 5 — Risk Gate (com ajuste por losses seguidos)
    rg = RiskGate().validar(decisao, regime, banca, stake, payout,
                            historico_recente, confianca_minima)

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
