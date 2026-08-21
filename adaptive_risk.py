# =============================================================================
# ADAPTIVE RISK ENGINE — Motor de Risco Adaptativo para o BOT GARRA
# =============================================================================
# Versão : 1.0.0
# Autor  : BOT GARRA ELITE
# Data   : 2025
#
# Descrição:
#   Motor de gestão de risco inteligente que opera em quatro modos:
#     DESLIGADO  — passa stake sem alterar (comportamento original)
#     MODERADO   — ajustes suaves de ±5-20% com base em drawdown e sequências
#     INTELIGENTE— ajustes dinâmicos baseados em score multicamada
#     DEFENSIVO  — proteção agressiva, pode bloquear entradas
#
# Integração:
#   from adaptive_risk import AdaptiveRiskEngine, AdaptiveConfig, adaptive_stake
# =============================================================================

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================

@dataclass
class AdaptiveConfig:
    """
    Parâmetros de configuração do motor adaptativo.
    Todos os valores possuem defaults conservadores prontos para uso.
    """

    # ── Modo operacional ──────────────────────────────────────────────────────
    modo: str = "DESLIGADO"           # DESLIGADO | MODERADO | INTELIGENTE | DEFENSIVO

    # ── Limites de stake ──────────────────────────────────────────────────────
    stake_min: float = 0.35           # Stake mínima permitida (USD)
    stake_max: float = 10.00          # Stake máxima permitida (USD)

    # ── Risco de banca ────────────────────────────────────────────────────────
    risco_max_pct: float = 0.03       # Exposição máxima por operação (3% da banca)

    # ── Sequências de loss ───────────────────────────────────────────────────
    max_losses_seguidos: int = 3      # Losses consecutivos antes de reduzir stake
    bloquear_apos_losses: int = 5     # Losses consecutivos para bloquear entradas

    # ── Drawdown ──────────────────────────────────────────────────────────────
    drawdown_defensivo: float = 0.05  # DD a partir do qual entra em modo defensivo (5%)
    drawdown_bloqueio: float = 0.10   # DD que bloqueia entradas completamente (10%)

    # ── Janela de análise ─────────────────────────────────────────────────────
    janela_resultados: int = 20       # Últimas N operações para calcular métricas

    # ── Fatores de ajuste ─────────────────────────────────────────────────────
    reducao_loss: float = 0.80        # Fator de redução após loss seguido (80%)
    reducao_drawdown: float = 0.70    # Fator de redução quando DD alto (70%)
    aumento_win: float = 1.05         # Fator de aumento após sequência boa (5%)

    # ── Loss Recovery ─────────────────────────────────────────────────────────
    recovery_max_pct: float = 0.30    # Exposição máxima no recovery (30% da banca)

    # ── Score de qualidade de sinal ───────────────────────────────────────────
    score_min_operar: float = 40.0    # Score abaixo desse valor bloqueia entrada
    score_defensivo: float = 60.0     # Score abaixo aplica redução de stake

    # ── Cooldown após bloqueio ────────────────────────────────────────────────
    cooldown_segundos: int = 60       # Segundos de cooldown após bloqueio por losses


# =============================================================================
# ESTADO INTERNO
# =============================================================================

@dataclass
class _AdaptiveState:
    """Estado interno mutável do motor. Não deve ser manipulado externamente."""

    saldo_inicial: float = 0.0
    saldo_atual: float = 0.0
    saldo_pico: float = 0.0           # Pico histórico de banca (para drawdown)

    wins: int = 0
    losses: int = 0
    wins_seguidos: int = 0
    losses_seguidos: int = 0

    operacoes: int = 0
    gale_atual: int = 0

    bloqueado_ate: float = 0.0        # Timestamp Unix até quando está em cooldown
    bloqueado_motivo: str = ""

    resultados: Deque[dict] = field(
        default_factory=lambda: deque(maxlen=200)
    )                                 # Histórico de resultados para análise


# =============================================================================
# MOTOR PRINCIPAL
# =============================================================================

class AdaptiveRiskEngine:
    """
    Motor de risco adaptativo.

    Uso básico:
        engine = AdaptiveRiskEngine(AdaptiveConfig(modo="INTELIGENTE"))
        engine.iniciar(saldo=100.0)

        resultado = engine.calcular_stake(
            stake_base=0.35,
            gerenciamento="martingale",
            gale=1,
            qualidade_sinal=80.0,
            payout=0.85,
            volatilidade=40.0,
            regime="LATERAL",
        )

        # Após a operação terminar:
        engine.registrar_resultado("WIN", lucro=0.297, saldo=100.297, gale=1)
    """

    # Modos válidos (case-insensitive no set_mode)
    _MODOS_VALIDOS = {"DESLIGADO", "MODERADO", "INTELIGENTE", "DEFENSIVO"}

    def __init__(self, config: Optional[AdaptiveConfig] = None):
        self.config = config or AdaptiveConfig()
        self.state  = _AdaptiveState()

    # ==========================================================================
    # INICIALIZAÇÃO
    # ==========================================================================

    def iniciar(self, saldo: float) -> None:
        """
        Inicializa (ou reinicia) o motor com o saldo atual da conta.
        Deve ser chamado assim que o saldo for conhecido (após login na Deriv).
        """
        s = max(float(saldo), 0.01)
        self.state = _AdaptiveState(
            saldo_inicial=s,
            saldo_atual=s,
            saldo_pico=s,
        )

    def set_mode(self, modo: str) -> None:
        """
        Define o modo de operação.
        Aceita: DESLIGADO, MODERADO, INTELIGENTE, DEFENSIVO (case-insensitive).
        """
        m = modo.strip().upper()
        if m in self._MODOS_VALIDOS:
            self.config.modo = m
        else:
            print(f"[AdaptiveRisk] ⚠️  Modo inválido: '{modo}'. Mantendo '{self.config.modo}'.")

    # ==========================================================================
    # CÁLCULO DE STAKE
    # ==========================================================================

    def calcular_stake(
        self,
        stake_base: float,
        gerenciamento: str = "fixa",
        gale: int = 0,
        qualidade_sinal: float = 50.0,
        payout: float = 0.80,
        volatilidade: float = 50.0,
        regime: str = "",
    ) -> dict:
        """
        Calcula a stake adaptada com base no modo e nas métricas de risco.

        Parâmetros
        ----------
        stake_base        : Stake calculada pelo gerenciamento nativo (Martingale, Soros, etc.)
        gerenciamento     : Nome do gerenciamento usado ('martingale', 'soros', 'fixa', ...)
        gale              : Nível de Gale atual (0 = entrada nova)
        qualidade_sinal   : Score de qualidade do sinal (0–100)
        payout            : Payout esperado pelo contrato (ex.: 0.85 = 85%)
        volatilidade      : Volatilidade do mercado (0–100)
        regime            : Regime detectado ('LATERAL', 'TENDENCIA', 'INDEFINIDO', ...)

        Retorna
        -------
        dict com:
          permitir       (bool)  — se a entrada está autorizada
          stake          (float) — stake final após ajuste adaptativo
          score          (float) — score calculado (0–100)
          modo           (str)   — modo ativo
          fator          (float) — fator aplicado sobre a stake_base
          motivo         (str)   — motivo do bloqueio (se houver)
          drawdown       (float) — drawdown atual em %
          losses_seguidos(int)   — losses consecutivos
        """
        cfg   = self.config
        state = self.state
        modo  = cfg.modo

        # Garante tipos corretos
        stake_base      = max(float(stake_base), cfg.stake_min)
        gale            = int(gale)
        qualidade_sinal = float(qualidade_sinal)
        payout          = float(payout)
        volatilidade    = float(volatilidade)

        estado_base = {
            "modo":            modo,
            "score":           qualidade_sinal,
            "fator":           1.0,
            "drawdown":        round(self.drawdown() * 100, 2),
            "losses_seguidos": state.losses_seguidos,
            "wins_seguidos":   state.wins_seguidos,
        }

        # ── MODO DESLIGADO: transparência total ──────────────────────────────
        if modo == "DESLIGADO":
            stake_final = round(
                max(cfg.stake_min, min(stake_base, cfg.stake_max)), 2
            )
            return {
                **estado_base,
                "permitir": True,
                "stake":    stake_final,
                "motivo":   "",
            }

        # ── Verifica bloqueio por cooldown ────────────────────────────────────
        if self.esta_bloqueado():
            restante = max(0, int(state.bloqueado_ate - time.time()))
            return {
                **estado_base,
                "permitir": False,
                "stake":    cfg.stake_min,
                "motivo":   f"Cooldown ativo — aguarde {restante}s ({state.bloqueado_motivo})",
            }

        # ── Calcula score multicamada ─────────────────────────────────────────
        score = self._calcular_score(
            qualidade_sinal=qualidade_sinal,
            payout=payout,
            volatilidade=volatilidade,
            regime=regime,
            gerenciamento=gerenciamento,
            gale=gale,
        )

        # ── Decide se permite entrada ─────────────────────────────────────────
        permitido, motivo_bloqueio = self._verificar_permissao(score, modo)
        if not permitido:
            return {
                **estado_base,
                "score":    round(score, 1),
                "permitir": False,
                "stake":    cfg.stake_min,
                "motivo":   motivo_bloqueio,
            }

        # ── Calcula fator de ajuste de stake ──────────────────────────────────
        fator = self._calcular_fator(score, modo)

        # ── Aplica fator e limites ────────────────────────────────────────────
        stake_ajustada = stake_base * fator

        # Limita pelo risco máximo percentual da banca
        if state.saldo_atual > 0:
            limite_banca = state.saldo_atual * cfg.risco_max_pct
            # Apenas aplica limite de banca para modos não-DESLIGADO
            if modo in ("INTELIGENTE", "DEFENSIVO"):
                stake_ajustada = min(stake_ajustada, limite_banca)
            elif modo == "MODERADO":
                # Moderado: aplica o dobro do limite (menos restritivo)
                stake_ajustada = min(stake_ajustada, limite_banca * 2)

        # Limita ao recovery_max em gerenciamentos de recuperação
        if gerenciamento in ("loss_recovery", "recovery_adaptativo"):
            if state.saldo_atual > 0:
                limite_recovery = state.saldo_atual * cfg.recovery_max_pct
                stake_ajustada = min(stake_ajustada, limite_recovery)

        # Clipa dentro de [stake_min, stake_max]
        stake_final = round(
            max(cfg.stake_min, min(stake_ajustada, cfg.stake_max)), 2
        )

        return {
            **estado_base,
            "score":    round(score, 1),
            "fator":    round(fator, 3),
            "permitir": True,
            "stake":    stake_final,
            "motivo":   "",
        }

    # ==========================================================================
    # SCORE MULTICAMADA
    # ==========================================================================

    def _calcular_score(
        self,
        qualidade_sinal: float,
        payout: float,
        volatilidade: float,
        regime: str,
        gerenciamento: str,
        gale: int,
    ) -> float:
        """
        Calcula um score de 0–100 que representa a qualidade da entrada
        levando em conta múltiplos fatores de risco.
        """
        score = qualidade_sinal  # Base: qualidade do sinal (0–100)

        # ── Ajuste por payout ─────────────────────────────────────────────────
        # Payout ideal ≥ 0.80. Abaixo disso penaliza progressivamente.
        if payout < 0.80:
            score -= (0.80 - payout) * 50          # -50 pts se payout = 0.30
        elif payout >= 0.90:
            score += 5                              # +5 pts para payouts excelentes

        # ── Ajuste por volatilidade ───────────────────────────────────────────
        # Volatilidade moderada (30–60) é ideal. Alta ou muito baixa penaliza.
        if volatilidade > 70:
            score -= (volatilidade - 70) * 0.5     # -5 a -15 pts
        elif volatilidade < 20:
            score -= (20 - volatilidade) * 0.3     # Mercado parado tb é ruim

        # ── Ajuste por regime ─────────────────────────────────────────────────
        r = regime.upper() if regime else ""
        if r == "LATERAL":
            score += 5                              # Regime lateral favorece dígitos
        elif r == "TENDENCIA":
            score -= 5                              # Tendência forte aumenta risco

        # ── Ajuste por sequência de losses ───────────────────────────────────
        ls = self.state.losses_seguidos
        if ls >= 4:
            score -= 30
        elif ls >= 3:
            score -= 20
        elif ls >= 2:
            score -= 10
        elif ls >= 1:
            score -= 5

        # ── Ajuste por drawdown ────────────────────────────────────────────────
        dd = self.drawdown()
        if dd >= 0.08:
            score -= 25
        elif dd >= 0.05:
            score -= 15
        elif dd >= 0.03:
            score -= 8

        # ── Ajuste por nível de Gale ──────────────────────────────────────────
        # Gales altos aumentam o risco exponencialmente
        penalidade_gale = {0: 0, 1: 5, 2: 15, 3: 25, 4: 40}
        score -= penalidade_gale.get(gale, 55)

        # ── Ajuste por gerenciamento ──────────────────────────────────────────
        if gerenciamento in ("loss_recovery", "recovery_adaptativo"):
            score -= 5   # Recovery já carrega risco embutido
        elif gerenciamento in ("soros",):
            score += 3   # Soros é conservador por natureza

        # ── Ajuste por winrate recente ────────────────────────────────────────
        wr_recente = self.winrate_recente()
        if wr_recente < 40.0:
            score -= 15
        elif wr_recente < 50.0:
            score -= 8
        elif wr_recente > 65.0:
            score += 5

        return max(0.0, min(100.0, score))

    # ==========================================================================
    # PERMISSÃO DE ENTRADA
    # ==========================================================================

    def _verificar_permissao(
        self, score: float, modo: str
    ) -> tuple[bool, str]:
        """
        Verifica se a entrada está autorizada com base no score e nas métricas.
        Retorna (permitido: bool, motivo: str).
        """
        cfg   = self.config
        state = self.state

        # ── Bloqueio por losses consecutivos ──────────────────────────────────
        if state.losses_seguidos >= cfg.bloquear_apos_losses:
            self._ativar_cooldown(
                f"{state.losses_seguidos} losses consecutivos"
            )
            return False, (
                f"🚫 {state.losses_seguidos} losses consecutivos — "
                f"cooldown de {cfg.cooldown_segundos}s ativado"
            )

        # ── Bloqueio por drawdown ──────────────────────────────────────────────
        dd = self.drawdown()
        if dd >= cfg.drawdown_bloqueio:
            return False, (
                f"🚫 Drawdown {dd*100:.1f}% ≥ limite de {cfg.drawdown_bloqueio*100:.0f}%"
            )

        # ── Bloqueio por score mínimo (INTELIGENTE e DEFENSIVO) ───────────────
        if modo in ("INTELIGENTE", "DEFENSIVO"):
            if score < cfg.score_min_operar:
                return False, (
                    f"🚫 Score {score:.1f} abaixo do mínimo ({cfg.score_min_operar:.0f})"
                )

        # ── DEFENSIVO: bloqueia com losses_seguidos ≥ max_losses_seguidos ─────
        if modo == "DEFENSIVO":
            if state.losses_seguidos >= cfg.max_losses_seguidos:
                return False, (
                    f"🛡️  Modo DEFENSIVO — {state.losses_seguidos} losses consecutivos"
                )

        return True, ""

    # ==========================================================================
    # CÁLCULO DE FATOR
    # ==========================================================================

    def _calcular_fator(self, score: float, modo: str) -> float:
        """
        Calcula o fator multiplicador da stake com base no score e no modo.
        Retorna um float entre 0.5 e 1.10 (sem explodir a stake).
        """
        cfg   = self.config
        state = self.state
        dd    = self.drawdown()

        fator = 1.0

        if modo == "MODERADO":
            # Ajustes suaves: no máximo ±20%
            if dd >= cfg.drawdown_defensivo:
                fator *= cfg.reducao_drawdown   # reduz 30%
            elif state.losses_seguidos >= cfg.max_losses_seguidos:
                fator *= cfg.reducao_loss        # reduz 20%
            elif state.wins_seguidos >= 3:
                fator *= cfg.aumento_win         # aumenta 5%

        elif modo == "INTELIGENTE":
            # Ajustes baseados no score
            if score >= 80:
                fator *= cfg.aumento_win         # sinal forte → +5%
            elif score >= cfg.score_defensivo:
                fator *= 1.0                     # sinal normal → sem ajuste
            else:
                fator *= cfg.reducao_loss        # sinal fraco → -20%

            # Penalidade extra por drawdown
            if dd >= cfg.drawdown_defensivo:
                fator *= cfg.reducao_drawdown

            # Penalidade por losses seguidos
            if state.losses_seguidos >= 2:
                fator *= (cfg.reducao_loss ** (state.losses_seguidos - 1))

        elif modo == "DEFENSIVO":
            # Sempre reduz quando há qualquer sinal de perigo
            base_defensivo = 0.70
            if score >= 75:
                base_defensivo = 0.85
            elif score >= 60:
                base_defensivo = 0.75

            fator = base_defensivo

            # Redução adicional por drawdown
            if dd >= cfg.drawdown_defensivo:
                fator *= cfg.reducao_drawdown

        # Garante que o fator não extrapole os limites razoáveis
        return round(max(0.50, min(fator, 1.10)), 3)

    # ==========================================================================
    # REGISTRO DE RESULTADOS
    # ==========================================================================

    def registrar_resultado(
        self,
        resultado: str,
        lucro: float = 0.0,
        saldo: float = 0.0,
        gale: int = 0,
    ) -> None:
        """
        Registra o resultado de uma operação finalizada.

        Parâmetros
        ----------
        resultado : 'WIN' | 'LOSS'
        lucro     : Valor do lucro (positivo em WIN, negativo em LOSS)
        saldo     : Saldo atual da conta após o resultado
        gale      : Nível de Gale da operação registrada
        """
        r = resultado.strip().upper()
        if r not in ("WIN", "LOSS"):
            return

        state = self.state
        state.operacoes += 1
        state.gale_atual = int(gale)

        # Atualiza saldo
        if saldo > 0:
            state.saldo_atual = float(saldo)
            if state.saldo_atual > state.saldo_pico:
                state.saldo_pico = state.saldo_atual
        elif lucro != 0:
            state.saldo_atual = max(0.01, state.saldo_atual + float(lucro))
            if state.saldo_atual > state.saldo_pico:
                state.saldo_pico = state.saldo_atual

        # Inicializa pico se ainda não foi feito
        if state.saldo_pico <= 0:
            state.saldo_pico = state.saldo_atual

        # Contadores
        if r == "WIN":
            state.wins += 1
            state.wins_seguidos  += 1
            state.losses_seguidos = 0
            # Reseta cooldown em sequência de wins
            if state.wins_seguidos >= 3 and state.bloqueado_ate > 0:
                state.bloqueado_ate    = 0.0
                state.bloqueado_motivo = ""
        else:
            state.losses += 1
            state.losses_seguidos += 1
            state.wins_seguidos    = 0

        # Histórico circular
        state.resultados.append({
            "resultado": r,
            "lucro":     float(lucro),
            "saldo":     state.saldo_atual,
            "gale":      gale,
            "ts":        time.time(),
        })

    # ==========================================================================
    # COOLDOWN
    # ==========================================================================

    def _ativar_cooldown(self, motivo: str) -> None:
        """Ativa o cooldown de proteção."""
        cfg   = self.config
        state = self.state
        if not self.esta_bloqueado():
            state.bloqueado_ate    = time.time() + cfg.cooldown_segundos
            state.bloqueado_motivo = motivo
            print(
                f"[AdaptiveRisk] 🔒 Cooldown ativado ({cfg.cooldown_segundos}s) | "
                f"Motivo: {motivo}"
            )

    def esta_bloqueado(self) -> bool:
        """Retorna True se o motor está em cooldown e a entrada deve ser bloqueada."""
        state = self.state
        if state.bloqueado_ate > 0 and time.time() < state.bloqueado_ate:
            return True
        # Limpa cooldown expirado
        if state.bloqueado_ate > 0 and time.time() >= state.bloqueado_ate:
            state.bloqueado_ate    = 0.0
            state.bloqueado_motivo = ""
        return False

    # ==========================================================================
    # MÉTRICAS
    # ==========================================================================

    def drawdown(self) -> float:
        """Drawdown atual em decimal (ex.: 0.05 = 5%)."""
        state = self.state
        if state.saldo_pico <= 0:
            return 0.0
        dd = (state.saldo_pico - state.saldo_atual) / state.saldo_pico
        return max(0.0, dd)

    def winrate(self) -> float:
        """Winrate global em % (0–100)."""
        total = self.state.wins + self.state.losses
        if total == 0:
            return 50.0
        return (self.state.wins / total) * 100.0

    def winrate_recente(self) -> float:
        """Winrate das últimas N operações da janela de análise."""
        cfg = self.config
        resultados = list(self.state.resultados)
        recentes   = resultados[-cfg.janela_resultados:]
        if not recentes:
            return 50.0
        wins = sum(1 for r in recentes if r.get("resultado") == "WIN")
        return (wins / len(recentes)) * 100.0

    # ==========================================================================
    # STATUS / DIAGNÓSTICO
    # ==========================================================================

    def status(self) -> dict:
        """
        Retorna um snapshot completo do estado do motor para diagnóstico
        e exibição no painel administrativo.
        """
        state = self.state
        return {
            "modo":              self.config.modo,
            "inicializado":      state.saldo_inicial > 0,
            "saldo_inicial":     round(state.saldo_inicial, 2),
            "saldo_atual":       round(state.saldo_atual, 2),
            "saldo_pico":        round(state.saldo_pico, 2),
            "drawdown_pct":      round(self.drawdown() * 100, 2),
            "wins":              state.wins,
            "losses":            state.losses,
            "wins_seguidos":     state.wins_seguidos,
            "losses_seguidos":   state.losses_seguidos,
            "winrate":           round(self.winrate(), 2),
            "winrate_recente":   round(self.winrate_recente(), 2),
            "operacoes":         state.operacoes,
            "gale_atual":        state.gale_atual,
            "bloqueado":         self.esta_bloqueado(),
            "bloqueado_motivo":  state.bloqueado_motivo if self.esta_bloqueado() else "",
            "cooldown_restante": max(0, int(state.bloqueado_ate - time.time())) if self.esta_bloqueado() else 0,
        }

    # ==========================================================================
    # RESET
    # ==========================================================================

    def resetar(self, saldo: Optional[float] = None) -> None:
        """
        Reseta o estado do motor.
        Se saldo for None, reutiliza o saldo_inicial registrado na última chamada a iniciar().
        """
        if saldo is None:
            saldo = self.state.saldo_inicial
        self.iniciar(float(saldo))


# =============================================================================
# FUNÇÃO SIMPLIFICADA (atalho de integração)
# =============================================================================

def adaptive_stake(
    engine: AdaptiveRiskEngine,
    stake_base: float,
    gerenciamento: str,
    gale: int = 0,
    qualidade_sinal: float = 50.0,
    payout: float = 0.80,
    volatilidade: float = 50.0,
    regime: str = "",
) -> dict:
    """
    Atalho para engine.calcular_stake().
    Conveniente para uso em uma única linha de importação.

    Exemplo:
        from adaptive_risk import adaptive_stake, ADAPTIVE_ENGINE
        resultado = adaptive_stake(ADAPTIVE_ENGINE, stake_base=0.35, gerenciamento="fixa")
    """
    return engine.calcular_stake(
        stake_base=stake_base,
        gerenciamento=gerenciamento,
        gale=gale,
        qualidade_sinal=qualidade_sinal,
        payout=payout,
        volatilidade=volatilidade,
        regime=regime,
    )
