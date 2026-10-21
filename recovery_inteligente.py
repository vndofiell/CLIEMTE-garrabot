# -*- coding: utf-8 -*-
# ============================================================
# SEC-RIA — RECOVERY INTELIGENTE ADAPTATIVO
# ============================================================

from collections import deque
from dataclasses import dataclass
import math
import time


@dataclass
class RIAConfig:
    # ---------------- RISCO ----------------
    stake_min: float = 0.35
    stake_max: float = 10.00

    # Máximo de uma entrada em relação à banca
    risco_max_banca: float = 0.025       # 2,5%

    # Exposição máxima acumulada durante recuperação
    exposicao_max_banca: float = 0.06    # 6%

    # ---------------- RECUPERAÇÃO ----------------
    # Percentual inicial da perda que será buscado
    recuperacao_base: float = 0.30

    # Nunca tentar recuperar tudo de uma vez
    recuperacao_max: float = 0.45

    # ---------------- PROTEÇÃO ----------------
    max_loss_seguidos: int = 4
    max_loss_recuperacao: int = 3

    drawdown_defesa: float = 0.04
    drawdown_pausa: float = 0.08

    # ---------------- ANÁLISE ----------------
    janela_curta: int = 10
    janela_media: int = 20
    janela_longa: int = 50

    confianca_minima: float = 55.0

    # Não entrar em recuperação sem margem matemática
    margem_minima: float = 0.015

    # Intervalo mínimo entre decisões
    intervalo_minimo: float = 0.0


class RecoveryInteligente:

    def __init__(self, config=None):
        self.cfg = config or RIAConfig()

        self.historico = deque(
            maxlen=self.cfg.janela_longa
        )

        self.perda_acumulada = 0.0
        self.lucro_ciclo = 0.0

        self.banca_inicial = 0.0
        self.banca_atual = 0.0
        self.pico_banca = 0.0

        self.loss_seguidos = 0
        self.win_seguidos = 0

        self.stake_anterior = 0.0
        self.ultima_decisao = None

        self.exposicao_ciclo = 0.0

        self.operacoes = 0
        self.wins = 0
        self.losses = 0

        self.ultima_decisao_time = 0.0

    # ============================================================
    # UTILITÁRIOS
    # ============================================================

    def _num(self, valor, padrao=0.0):
        try:
            valor = float(valor)
            if math.isfinite(valor):
                return valor
        except Exception:
            pass
        return padrao

    def _limitar(self, valor, minimo, maximo):
        return max(minimo, min(maximo, valor))

    def _arredondar(self, valor):
        return round(max(0.0, valor) + 1e-10, 2)

    # ============================================================
    # RESET
    # ============================================================

    def resetar(self, banca):
        banca = max(0.0, self._num(banca))
        self.historico.clear()
        self.perda_acumulada = 0.0
        self.lucro_ciclo = 0.0
        self.banca_inicial = banca
        self.banca_atual = banca
        self.pico_banca = banca
        self.loss_seguidos = 0
        self.win_seguidos = 0
        self.stake_anterior = 0.0
        self.exposicao_ciclo = 0.0
        self.operacoes = 0
        self.wins = 0
        self.losses = 0
        self.ultima_decisao = None

    # ============================================================
    # DRAWDOWN
    # ============================================================

    def calcular_drawdown(self, banca):
        banca = self._num(banca)
        if self.pico_banca <= 0:
            return 0.0
        return self._limitar(
            (self.pico_banca - banca) / self.pico_banca,
            0.0, 1.0
        )

    # ============================================================
    # WIN RATE
    # ============================================================

    def winrate(self, quantidade=None):
        if not self.historico:
            return 0.50
        dados = list(self.historico)
        if quantidade:
            dados = dados[-quantidade:]
        if not dados:
            return 0.50
        wins = sum(1 for r in dados if r["win"])
        return wins / len(dados)

    # ============================================================
    # EXPECTATIVA MATEMÁTICA
    # ============================================================

    def expectativa(self, payout, winrate):
        payout  = max(0.0, self._num(payout))
        winrate = self._limitar(self._num(winrate, 0.50), 0.0, 1.0)
        lossrate = 1.0 - winrate
        exp = (winrate * payout) - lossrate
        breakeven = (1.0 / (1.0 + payout)) if payout > 0 else 1.0
        margem = winrate - breakeven
        return {
            "expectativa": exp,
            "breakeven":   breakeven,
            "margem":      margem,
            "winrate":     winrate,
            "payout":      payout,
        }

    # ============================================================
    # QUALIDADE DO MOMENTO
    # ============================================================

    def calcular_qualidade(self, confianca=50.0, volatilidade=0.0, regime="NORMAL"):
        confianca    = self._limitar(self._num(confianca, 50.0), 0.0, 100.0)
        volatilidade = self._limitar(self._num(volatilidade), 0.0, 1.0)

        curta = self.winrate(self.cfg.janela_curta) * 100
        media = self.winrate(self.cfg.janela_media) * 100
        longa = self.winrate(self.cfg.janela_longa) * 100

        score = (
            confianca * 0.40
            + curta   * 0.30
            + media   * 0.20
            + longa   * 0.10
        )

        score -= self.loss_seguidos * 7
        score += min(8, self.win_seguidos * 2)
        score -= volatilidade * 20

        regime = str(regime or "NORMAL").upper()
        if regime in ("ALTA_VOL", "ALTA_VOLATILIDADE", "INSTAVEL", "RUIM"):
            score -= 10
        elif regime in ("NORMAL", "LATERAL", "TENDENCIA"):
            score += 2

        return self._limitar(score, 0, 100)

    # ============================================================
    # DECISÃO PRINCIPAL
    # ============================================================

    def calcular_stake(
        self,
        banca,
        stake_base,
        payout,
        confianca=50.0,
        volatilidade=0.0,
        regime="NORMAL",
        winrate_externo=None,
        exposicao_atual=0.0,
    ):
        agora = time.time()

        banca          = max(0.0, self._num(banca))
        stake_base     = max(0.0, self._num(stake_base))
        payout         = max(0.0, self._num(payout))
        exposicao_atual = max(0.0, self._num(exposicao_atual))

        if banca <= 0:
            return self._resultado("PAUSAR", 0.0, "Banca inválida.")
        if stake_base <= 0:
            return self._resultado("PAUSAR", 0.0, "Stake base inválida.")

        # Inicialização
        if self.banca_inicial <= 0:
            self.banca_inicial = banca
            self.pico_banca    = banca
        self.banca_atual = banca
        if banca > self.pico_banca:
            self.pico_banca = banca

        drawdown = self.calcular_drawdown(banca)

        wr = (
            self._limitar(self._num(winrate_externo, self.winrate()), 0.0, 1.0)
            if winrate_externo is not None
            else self.winrate()
        )

        edge      = self.expectativa(payout, wr)
        qualidade = self.calcular_qualidade(confianca, volatilidade, regime)

        limite_por_banca     = banca * self.cfg.risco_max_banca
        limite_exposicao     = banca * self.cfg.exposicao_max_banca
        exposicao_disponivel = max(0.0, limite_exposicao - exposicao_atual)
        limite_stake = min(self.cfg.stake_max, limite_por_banca, exposicao_disponivel)

        # ---- BLOQUEIOS ----
        if drawdown >= self.cfg.drawdown_pausa:
            return self._resultado("PAUSAR", 0.0, "Drawdown máximo atingido.")
        if self.loss_seguidos >= self.cfg.max_loss_seguidos:
            return self._resultado("PAUSAR", 0.0, "Sequência máxima de losses.")
        if limite_stake <= 0:
            return self._resultado("PAUSAR", 0.0, "Limite de exposição atingido.")

        # ---- SEM PREJUÍZO ----
        if self.perda_acumulada <= 0:
            if drawdown >= self.cfg.drawdown_defesa:
                stake = min(stake_base * 0.70, limite_stake)
                return self._resultado("DEFESA", self._arredondar(stake), "Banca em drawdown defensivo.")
            stake = min(stake_base, limite_stake)
            return self._resultado("OPERAR", self._arredondar(stake), "Operação normal.")

        # ---- RECUPERAÇÃO ----
        if edge["expectativa"] <= 0:
            stake = min(stake_base * 0.70, limite_stake)
            return self._resultado("DEFESA", self._arredondar(stake), "Expectativa matemática insuficiente.")
        if edge["margem"] < self.cfg.margem_minima:
            stake = min(stake_base * 0.75, limite_stake)
            return self._resultado("DEFESA", self._arredondar(stake), "Margem matemática insuficiente.")
        if confianca < self.cfg.confianca_minima:
            stake = min(stake_base * 0.70, limite_stake)
            return self._resultado("DEFESA", self._arredondar(stake), "Confiança insuficiente.")

        # Percentual adaptativo
        percentual = self.cfg.recuperacao_base
        if   self.loss_seguidos == 1: percentual *= 0.90
        elif self.loss_seguidos == 2: percentual *= 0.75
        elif self.loss_seguidos >= 3: percentual *= 0.55
        if   volatilidade >= 0.75: percentual *= 0.55
        elif volatilidade >= 0.55: percentual *= 0.75
        if drawdown >= self.cfg.drawdown_defesa: percentual *= 0.60
        percentual = self._limitar(percentual, 0.05, self.cfg.recuperacao_max)

        meta         = self.perda_acumulada * percentual
        stake_teorica = meta / payout

        # Fator de qualidade
        if   qualidade >= 80: fator = 1.00
        elif qualidade >= 70: fator = 0.90
        elif qualidade >= 60: fator = 0.78
        elif qualidade >= 55: fator = 0.65
        else:                 fator = 0.50
        if drawdown    >= self.cfg.drawdown_defesa: fator *= 0.70
        if volatilidade >= 0.70:                    fator *= 0.65

        stake = stake_teorica * fator

        # Teto: máximo 2× stake base (não vira Martingale)
        teto  = min(limite_stake, stake_base * 2.0)
        stake = min(stake, teto)

        if qualidade < 55:
            stake = min(stake, stake_base * 0.70)
            acao  = "DEFESA"
        else:
            acao  = "RECUPERAR"

        # Mínimo
        if stake < self.cfg.stake_min:
            if self.cfg.stake_min <= limite_stake:
                stake = self.cfg.stake_min
            else:
                return self._resultado("PAUSAR", 0.0, "Stake mínima ultrapassaria o limite de risco.")

        stake = self._arredondar(stake)

        resultado = self._resultado(acao, stake, (
            "Recuperação calculada por perda acumulada, "
            "payout, expectativa, qualidade, drawdown e risco."
        ))
        resultado.update({
            "perda_acumulada":        round(self.perda_acumulada, 2),
            "meta_recuperacao":       round(meta, 2),
            "percentual_recuperacao": round(percentual * 100, 2),
            "expectativa":            round(edge["expectativa"], 6),
            "breakeven":              round(edge["breakeven"] * 100, 2),
            "margem":                 round(edge["margem"] * 100, 2),
            "winrate":                round(wr * 100, 2),
            "qualidade":              round(qualidade, 2),
            "drawdown":               round(drawdown * 100, 2),
            "loss_seguidos":          self.loss_seguidos,
            "payout":                 payout,
        })

        self.ultima_decisao      = resultado
        self.stake_anterior      = stake
        self.ultima_decisao_time = agora
        return resultado

    # ============================================================
    # REGISTRO DO RESULTADO
    # ============================================================

    def registrar_resultado(self, win, lucro, saldo_atual, stake=None):
        lucro       = self._num(lucro)
        saldo_atual = max(0.0, self._num(saldo_atual))
        stake       = self._num(stake if stake is not None else self.stake_anterior)

        self.banca_atual = saldo_atual
        if saldo_atual > self.pico_banca:
            self.pico_banca = saldo_atual

        self.operacoes += 1

        if win:
            self.wins += 1
            self.win_seguidos  += 1
            self.loss_seguidos  = 0
            self.lucro_ciclo   += lucro
            if lucro > 0:
                recuperado             = min(lucro, self.perda_acumulada)
                self.perda_acumulada  -= recuperado
                self.perda_acumulada   = max(0.0, self.perda_acumulada)
        else:
            self.losses += 1
            self.loss_seguidos += 1
            self.win_seguidos   = 0
            self.lucro_ciclo   += lucro
            self.perda_acumulada += abs(lucro)

        self.historico.append({
            "win":       bool(win),
            "lucro":     lucro,
            "stake":     stake,
            "saldo":     saldo_atual,
            "timestamp": time.time(),
        })

        return self.status()

    # ============================================================
    # RESULTADO PADRÃO
    # ============================================================

    def _resultado(self, acao, stake, motivo):
        resultado = {
            "acao":             acao,
            "stake":            self._arredondar(stake),
            "motivo":           motivo,
            "perda_acumulada":  round(self.perda_acumulada, 2),
            "loss_seguidos":    self.loss_seguidos,
            "win_seguidos":     self.win_seguidos,
        }
        self.ultima_decisao = resultado
        return resultado

    # ============================================================
    # STATUS
    # ============================================================

    def status(self):
        drawdown = self.calcular_drawdown(self.banca_atual)
        return {
            "modo":             "SEC-RIA",
            "banca":            round(self.banca_atual, 2),
            "perda_acumulada":  round(self.perda_acumulada, 2),
            "lucro_ciclo":      round(self.lucro_ciclo, 2),
            "wins":             self.wins,
            "losses":           self.losses,
            "loss_seguidos":    self.loss_seguidos,
            "win_seguidos":     self.win_seguidos,
            "operacoes":        self.operacoes,
            "winrate":          round(self.winrate() * 100, 2),
            "drawdown":         round(drawdown * 100, 2),
            "stake_anterior":   round(self.stake_anterior, 2),
            "ultima_decisao":   self.ultima_decisao,
        }


# ============================================================
# INSTÂNCIA GLOBAL
# ============================================================

SEC_RIA = RecoveryInteligente()


# ============================================================
# FUNÇÕES SIMPLES PARA USAR DIRETO NO BOT
# ============================================================

def ria_calcular_stake(
    banca,
    stake_base,
    payout,
    confianca=70.0,
    volatilidade=0.20,
    regime="NORMAL",
    winrate=None,
    exposicao=0.0,
):
    return SEC_RIA.calcular_stake(
        banca=banca,
        stake_base=stake_base,
        payout=payout,
        confianca=confianca,
        volatilidade=volatilidade,
        regime=regime,
        winrate_externo=winrate,
        exposicao_atual=exposicao,
    )


def ria_registrar_resultado(win, lucro, saldo, stake=None):
    return SEC_RIA.registrar_resultado(
        win=win,
        lucro=lucro,
        saldo_atual=saldo,
        stake=stake,
    )
