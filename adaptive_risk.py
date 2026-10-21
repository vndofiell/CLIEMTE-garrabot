# -*- coding: utf-8 -*-
"""
ADAPTIVE RISK ENGINE — SEC-RIA

Substituição compatível com o main.py atual do BOT GARRA.

Este arquivo mantém a API esperada pelo main.py:
    AdaptiveConfig
    AdaptiveRiskEngine
    set_mode()
    iniciar()
    resetar()
    calcular_stake()
    registrar_resultado()
    status()

SEC-RIA não é Martingale: uma perda não causa duplicação automática.
A próxima stake depende de perda acumulada, payout, expectativa,
qualidade do sinal, drawdown, sequência e limites da banca.

IMPORTANTE:
- Nenhum gerenciamento garante recuperação ou lucro.
- Use primeiro em DEMO/backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Any, Dict, Optional
import math
import threading
import time


# ============================================================
# CONFIGURAÇÃO
# ============================================================

@dataclass
class AdaptiveConfig:
    modo: str = "DESLIGADO"

    stake_min: float = 0.35
    stake_max: float = 10.00

    # Risco máximo por entrada.
    risco_max_pct: float = 0.03

    # Sequência defensiva/bloqueio.
    max_losses_seguidos: int = 3
    bloquear_apos_losses: int = 5

    # Drawdown relativo ao pico da banca.
    drawdown_defensivo: float = 0.05
    drawdown_bloqueio: float = 0.10

    # Histórico recente.
    janela_resultados: int = 20

    # Fatores usados pela lógica adaptativa.
    reducao_loss: float = 0.80
    reducao_drawdown: float = 0.70
    aumento_win: float = 1.05
    recovery_max_pct: float = 0.30

    # Scores mantidos para compatibilidade com a interface atual.
    score_min_operar: float = 40.0
    score_defensivo: float = 60.0

    # Cooldown em segundos depois de bloqueio/pausa.
    cooldown_segundos: int = 60


@dataclass
class AdaptiveState:
    saldo_inicial: float = 0.0
    saldo_atual: float = 0.0
    pico_saldo: float = 0.0

    perda_acumulada: float = 0.0
    lucro_ciclo: float = 0.0

    wins: int = 0
    losses: int = 0
    operacoes: int = 0

    wins_seguidos: int = 0
    losses_seguidos: int = 0

    stake_anterior: float = 0.0
    ultima_decisao: str = "INICIALIZANDO"
    ultimo_motivo: str = ""

    recuperacoes: int = 0
    recuperacoes_concluidas: int = 0
    recuperacoes_abandonadas: int = 0

    bloqueado_ate: float = 0.0


class AdaptiveRiskEngine:
    """Motor SEC-RIA compatível com as rotas existentes do main.py."""

    MODOS_VALIDOS = {
        "DESLIGADO",
        "NORMAL",
        "MODERADO",
        "INTELIGENTE",
        "DEFENSIVO",
        "SEC-RIA",
        "RIA",
    }

    def __init__(self, config: Optional[AdaptiveConfig] = None):
        self.config = config or AdaptiveConfig()
        self.state = AdaptiveState()
        self.historico = deque(maxlen=max(20, int(self.config.janela_resultados)))
        self._lock = threading.RLock()

    # ========================================================
    # UTILITÁRIOS
    # ========================================================

    @staticmethod
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            x = float(v)
            if math.isfinite(x):
                return x
        except Exception:
            pass
        return default

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    @staticmethod
    def _stake(v: float) -> float:
        return round(max(0.0, v) + 1e-10, 2)

    def _normalizar_modo(self, modo: Any) -> str:
        s = str(modo or "DESLIGADO").strip().upper()
        aliases = {
            "RECOVERY INTELIGENTE ADAPTATIVO": "SEC-RIA",
            "RECOVERY_INTELIGENTE_ADAPTATIVO": "SEC-RIA",
            "RECOVERY INTELIGENTE": "SEC-RIA",
            "RECOVERY_ADAPTATIVO": "SEC-RIA",
            "ADAPTATIVO": "INTELIGENTE",
        }
        return aliases.get(s, s)

    # ========================================================
    # MODO / CICLO
    # ========================================================

    def set_mode(self, modo: str):
        with self._lock:
            modo_n = self._normalizar_modo(modo)
            if modo_n not in self.MODOS_VALIDOS:
                modo_n = "INTELIGENTE"
            self.config.modo = modo_n

    def iniciar(self, saldo: float):
        with self._lock:
            saldo = max(0.0, self._num(saldo))
            self.state = AdaptiveState(
                saldo_inicial=saldo,
                saldo_atual=saldo,
                pico_saldo=saldo,
            )
            self.historico.clear()

    def resetar(self, saldo: Optional[float] = None):
        with self._lock:
            if saldo is None:
                saldo = self.state.saldo_inicial
            self.iniciar(float(saldo))

    # ========================================================
    # MÉTRICAS
    # ========================================================

    def _janela(self):
        return list(self.historico)

    def winrate_recente(self) -> float:
        dados = self._janela()
        if not dados:
            return 0.50
        return sum(1 for x in dados if x.get("resultado") == "WIN") / len(dados)

    def _winrate_curto(self, n: int = 10) -> float:
        dados = self._janela()[-n:]
        if not dados:
            return 0.50
        return sum(1 for x in dados if x.get("resultado") == "WIN") / len(dados)

    def _drawdown(self, saldo: Optional[float] = None) -> float:
        saldo = self.state.saldo_atual if saldo is None else self._num(saldo)
        pico = self.state.pico_saldo
        if pico <= 0:
            return 0.0
        return self._clamp((pico - saldo) / pico, 0.0, 1.0)

    def _payout_liquido(self, payout: float) -> float:
        """Normaliza payout para lucro líquido por $1 apostado."""
        p = max(0.0, self._num(payout, 0.80))
        # O main.py documenta payout como 0.80/0.85.
        # Também aceitamos 80/85 para evitar erro de integração.
        if p > 3.0:
            p /= 100.0
        return p

    def _edge(self, payout: float, winrate: float) -> Dict[str, float]:
        p = self._payout_liquido(payout)
        wr = self._clamp(winrate, 0.0, 1.0)
        br = 1.0 / (1.0 + p) if p > 0 else 1.0
        expectativa = wr * p - (1.0 - wr)
        margem = wr - br
        return {
            "payout": p,
            "winrate": wr,
            "breakeven": br,
            "expectativa": expectativa,
            "margem": margem,
        }

    def _qualidade(self, qualidade_sinal: float, volatilidade: float, regime: str) -> float:
        sinal = self._clamp(self._num(qualidade_sinal, 50.0), 0.0, 100.0)
        vol = self._num(volatilidade, 50.0)
        # A rota atual documenta volatilidade como 0-100.
        if vol <= 1.0:
            vol *= 100.0
        vol = self._clamp(vol, 0.0, 100.0)

        wr_curto = self._winrate_curto(10) * 100.0
        wr_longo = self.winrate_recente() * 100.0

        score = (
            sinal * 0.50
            + wr_curto * 0.30
            + wr_longo * 0.20
        )

        score -= min(25.0, self.state.losses_seguidos * 7.0)
        score += min(8.0, self.state.wins_seguidos * 2.0)
        score -= max(0.0, vol - 55.0) * 0.25

        reg = str(regime or "").upper()
        if reg in {"ALTA_VOLATILIDADE", "ALTA_VOL", "INSTAVEL", "RUIM"}:
            score -= 10.0
        elif reg in {"LATERAL", "TENDENCIA", "NORMAL"}:
            score += 2.0

        score -= self._drawdown() * 35.0
        return self._clamp(score, 0.0, 100.0)

    # ========================================================
    # CÁLCULO DA STAKE
    # ========================================================

    def calcular_stake(
        self,
        stake_base: float,
        gerenciamento: str = "fixa",
        gale: int = 0,
        qualidade_sinal: float = 50.0,
        payout: float = 0.80,
        volatilidade: float = 50.0,
        regime: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Calcula a stake real do SEC-RIA.

        Compatível com a chamada atual do main.py.
        """
        with self._lock:
            base = max(0.0, self._num(stake_base))
            gale = max(0, int(gale))
            saldo = self.state.saldo_atual

            if saldo <= 0:
                # O motor ainda pode operar com stake_base, mas não cria
                # uma falsa recuperação sem conhecer a banca.
                limite_banca = self.config.stake_max
            else:
                limite_banca = saldo * self.config.risco_max_pct

            limite = min(self.config.stake_max, limite_banca)

            if limite <= 0:
                return self._decisao(False, 0.0, "Limite de risco da banca é zero.")

            # DESLIGADO: preserva comportamento de stake base.
            if self.config.modo == "DESLIGADO":
                stake = min(base, limite)
                return self._decisao(True, self._stake(stake), "Motor adaptativo desligado.")

            # Cooldown após bloqueio.
            if time.time() < self.state.bloqueado_ate:
                return self._decisao(False, 0.0, "Cooldown de proteção ativo.")

            drawdown = self._drawdown()
            wr = self.winrate_recente()
            edge = self._edge(payout, wr)
            qualidade = self._qualidade(qualidade_sinal, volatilidade, regime)

            # ------------------------------------------------------------
            # BLOQUEIOS DUROS
            # ------------------------------------------------------------
            if drawdown >= self.config.drawdown_bloqueio:
                self.state.bloqueado_ate = time.time() + self.config.cooldown_segundos
                self.state.recuperacoes_abandonadas += 1
                return self._decisao(False, 0.0, "Drawdown de bloqueio atingido.")

            if self.state.losses_seguidos >= self.config.bloquear_apos_losses:
                self.state.bloqueado_ate = time.time() + self.config.cooldown_segundos
                self.state.recuperacoes_abandonadas += 1
                return self._decisao(False, 0.0, "Sequência máxima de losses atingida.")

            # ------------------------------------------------------------
            # OPERAÇÃO NORMAL — SEM DÍVIDA
            # ------------------------------------------------------------
            if self.state.perda_acumulada <= 0.000001:
                if drawdown >= self.config.drawdown_defensivo:
                    stake = min(base * self.config.reducao_drawdown, limite)
                    stake = self._stake(stake)
                    return self._decisao(True, stake, "Modo DEFENSIVO por drawdown.")

                # Qualidade muito baixa: não aumenta exposição.
                if qualidade < self.config.score_min_operar:
                    stake = min(base * 0.75, limite)
                    return self._decisao(True, self._stake(stake), "Sinal abaixo do score ideal; stake reduzida.")

                stake = min(base, limite)
                return self._decisao(True, self._stake(stake), "Operação normal.")

            # ------------------------------------------------------------
            # RECUPERAÇÃO
            # ------------------------------------------------------------
            # Nunca tenta recuperar tudo automaticamente.
            perda = self.state.perda_acumulada

            # Se expectativa não é positiva, não faz sentido aumentar risco.
            if edge["expectativa"] <= 0 or edge["margem"] < 0.01:
                stake = min(base * 0.70, limite)
                self.state.recuperacoes_abandonadas += 1
                return self._decisao(
                    True,
                    self._stake(stake),
                    "Recuperação bloqueada: expectativa/margem insuficiente."
                )

            # Meta adaptativa: começa em 30% da perda e diminui com risco.
            pct = self.config.recovery_max_pct
            pct = self._clamp(pct, 0.05, 0.40)

            if self.state.losses_seguidos >= 1:
                pct *= 0.90
            if self.state.losses_seguidos >= 2:
                pct *= 0.80
            if self.state.losses_seguidos >= 3:
                pct *= 0.65

            vol = self._num(volatilidade, 50.0)
            if vol <= 1.0:
                vol *= 100.0
            if vol >= 75:
                pct *= 0.55
            elif vol >= 60:
                pct *= 0.75

            if drawdown >= self.config.drawdown_defensivo:
                pct *= self.config.reducao_drawdown

            if qualidade >= 80:
                fator_qualidade = 1.00
            elif qualidade >= 70:
                fator_qualidade = 0.90
            elif qualidade >= 60:
                fator_qualidade = 0.78
            elif qualidade >= 50:
                fator_qualidade = 0.65
            else:
                fator_qualidade = 0.50

            # Recupera apenas uma fração da perda nesta entrada.
            meta = perda * pct
            stake_teorica = meta / edge["payout"] if edge["payout"] > 0 else float("inf")
            stake = stake_teorica * fator_qualidade

            # Proteção anti-Martingale: nunca passa de 2x a base.
            # Também respeita 3% da banca e stake_max.
            teto_progressao = base * 2.0
            stake = min(stake, teto_progressao, limite)

            # Em drawdown defensivo, reduz ainda mais.
            if drawdown >= self.config.drawdown_defensivo:
                stake = min(stake, base * 0.70)

            # Se a stake calculada ficou abaixo da mínima, só sobe para a mínima
            # se isso continuar dentro do limite de risco.
            if stake > 0 and stake < self.config.stake_min:
                if self.config.stake_min <= limite:
                    stake = self.config.stake_min
                else:
                    return self._decisao(False, 0.0, "Stake mínima ultrapassa o risco permitido.")

            stake = self._stake(stake)

            if stake <= 0:
                return self._decisao(False, 0.0, "Não existe stake segura para recuperação.")

            self.state.recuperacoes += 1
            return self._decisao(
                True,
                stake,
                "RECUPERAÇÃO ADAPTATIVA calculada por perda + payout + expectativa + risco."
            )

    # ========================================================
    # REGISTRO DO RESULTADO
    # ========================================================

    def registrar_resultado(
        self,
        resultado: str,
        lucro: float,
        saldo: float,
        gale: int = 0,
        **kwargs,
    ):
        with self._lock:
            res = str(resultado or "").upper().strip()
            if res not in {"WIN", "LOSS"}:
                return

            lucro = self._num(lucro)
            saldo = max(0.0, self._num(saldo))

            # Primeira atualização de saldo pode inicializar o motor.
            if self.state.saldo_inicial <= 0 and saldo > 0:
                self.state.saldo_inicial = saldo
                self.state.pico_saldo = saldo

            self.state.saldo_atual = saldo
            self.state.pico_saldo = max(self.state.pico_saldo, saldo)
            self.state.operacoes += 1
            self.state.lucro_ciclo += lucro

            if res == "WIN":
                self.state.wins += 1
                self.state.wins_seguidos += 1
                self.state.losses_seguidos = 0

                # O lucro real primeiro paga a dívida de recuperação.
                if lucro > 0:
                    antes = self.state.perda_acumulada
                    self.state.perda_acumulada = max(0.0, antes - lucro)

                    if antes > 0 and self.state.perda_acumulada <= 0.000001:
                        self.state.recuperacoes_concluidas += 1

            else:
                self.state.losses += 1
                self.state.losses_seguidos += 1
                self.state.wins_seguidos = 0

                # LOSS aumenta a dívida real pelo prejuízo da operação.
                self.state.perda_acumulada += abs(lucro)

            self.historico.append({
                "resultado": res,
                "lucro": lucro,
                "saldo": saldo,
                "gale": int(gale),
                "timestamp": time.time(),
            })

            self.state.stake_anterior = self.state.stake_anterior or self.config.stake_min

            # Proteção adicional após drawdown crítico.
            if self._drawdown() >= self.config.drawdown_bloqueio:
                self.state.bloqueado_ate = time.time() + self.config.cooldown_segundos

    # ========================================================
    # DECISÃO / STATUS
    # ========================================================

    def _decisao(self, permitir: bool, stake: float, motivo: str) -> Dict[str, Any]:
        modo = self.config.modo
        if stake > 0:
            self.state.stake_anterior = stake

        if not permitir:
            self.state.ultima_decisao = "PAUSAR"
        elif "RECUPERAÇÃO" in motivo or "RECUPERACAO" in motivo:
            self.state.ultima_decisao = "RECUPERAR"
        elif "DEFENSIVO" in motivo:
            self.state.ultima_decisao = "DEFESA"
        else:
            self.state.ultima_decisao = "OPERAR"

        self.state.ultimo_motivo = motivo

        return {
            "ok": True,
            "permitir": bool(permitir),
            "stake": self._stake(stake),
            "score": 0.0,
            "modo": modo,
            "fator": 1.0,
            "motivo": motivo,
            "drawdown": self._drawdown() * 100.0,
            "losses_seguidos": self.state.losses_seguidos,
            "perda_acumulada": round(self.state.perda_acumulada, 2),
            "lucro_ciclo": round(self.state.lucro_ciclo, 2),
            "decisao": self.state.ultima_decisao,
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            dd = self._drawdown() * 100.0
            wr = self.winrate_recente() * 100.0
            return {
                "modo": self.config.modo,
                "saldo_inicial": round(self.state.saldo_inicial, 2),
                "saldo_atual": round(self.state.saldo_atual, 2),
                "pico_saldo": round(self.state.pico_saldo, 2),
                "perda_acumulada": round(self.state.perda_acumulada, 2),
                "lucro_ciclo": round(self.state.lucro_ciclo, 2),
                "wins": self.state.wins,
                "losses": self.state.losses,
                "operacoes": self.state.operacoes,
                "winrate_recente": wr,
                "losses_seguidos": self.state.losses_seguidos,
                "wins_seguidos": self.state.wins_seguidos,
                "drawdown_pct": dd,
                "stake_anterior": round(self.state.stake_anterior, 2),
                "recuperacoes": self.state.recuperacoes,
                "recuperacoes_concluidas": self.state.recuperacoes_concluidas,
                "recuperacoes_abandonadas": self.state.recuperacoes_abandonadas,
                "bloqueado": time.time() < self.state.bloqueado_ate,
                "ultima_decisao": self.state.ultima_decisao,
                "ultimo_motivo": self.state.ultimo_motivo,
            }
