# ══════════════════════════════════════════════════════════════════════════════
# MEMORY TIME ENGINE 7D — Motor de Memória Temporal com Aprendizado Contínuo
# ══════════════════════════════════════════════════════════════════════════════
#
# Arquitetura:
#   1. APRENDIZADO (0–7 dias): coleta sem bloquear
#   2. ANALISANDO  (cálculo inicial dos slots)
#   3. ATIVO       (bloqueia / libera por horário + regime + maré)
#
# Arquivos:
#   memory_time_engine.json  — slots de horário agregados
#   memory_time_raw.json     — experiências brutas (máx 5000)
#
# Integração:
#   from memory_time_engine import MemoryTimeEngine
#   mte = MemoryTimeEngine()
#   decisao = mte.pode_operar(estrategia, ativo, regime, confianca)
#   mte.registrar(resultado, estrategia, ativo, regime, confianca, virtual)
# ══════════════════════════════════════════════════════════════════════════════

import json
import os
import time
import threading
import datetime as _dt
from typing import Optional

# ── Constantes ────────────────────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_AGG_FILE   = os.path.join(_BASE_DIR, "memory_time_engine.json")
_RAW_FILE   = os.path.join(_BASE_DIR, "memory_time_raw.json")

DIAS_MINIMOS        = 7        # dias de coleta antes de ativar bloqueios
SLOT_MINUTOS        = 5        # granularidade dos slots (minutos)
MIN_OPS_POR_SLOT    = 5        # mínimo de operações para classificar o slot
WR_BOM              = 62.0     # win rate (%) para classificar como OPERAR
WR_RUIM             = 45.0     # win rate (%) para classificar como BLOQUEAR
MAX_RAW             = 5000     # máximo de experiências brutas salvas
PESO_REAL           = 1.00     # peso de operações reais
PESO_VIRTUAL        = 0.35     # peso de operações virtuais
MARE_JANELA         = 8        # últimas N operações para calcular maré
MARE_WINS_LIBERAR   = 3        # wins consecutivos para sair da maré ruim
MARE_LOSSES_BLOQUEAR= 4        # losses consecutivos para detectar maré ruim

# Pesos decrescentes por dia de antiguidade (hoje=1.00, 7 dias atrás=0.30)
PESOS_DIA = {0: 1.00, 1: 0.90, 2: 0.80, 3: 0.70,
             4: 0.60, 5: 0.50, 6: 0.40, 7: 0.30}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _hora_brt() -> _dt.datetime:
    """Retorna datetime atual no fuso de Brasília (UTC-3)."""
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return _dt.datetime.utcnow() - _dt.timedelta(hours=3)


def _slot_de(hora: _dt.datetime) -> str:
    """Converte datetime → chave de slot 'HH:MM' (arredondado para SLOT_MINUTOS)."""
    minuto = (hora.minute // SLOT_MINUTOS) * SLOT_MINUTOS
    return f"{hora.hour:02d}:{minuto:02d}"


def _dias_atras(timestamp: float) -> int:
    """Quantos dias atrás foi o timestamp."""
    agora = _hora_brt().timestamp()
    return int((agora - timestamp) / 86400)


def _peso_dia(timestamp: float) -> float:
    d = _dias_atras(timestamp)
    return PESOS_DIA.get(min(d, 7), 0.25)


# ══════════════════════════════════════════════════════════════════════════════
class MemoryTimeEngine:
    """Motor principal de memória temporal."""

    def __init__(self):
        self._lock   = threading.Lock()
        self._raw    = []   # lista de experiências brutas
        self._agg    = {}   # slots agregados: {"HH:MM": {...}}
        self._config = {}   # metadados: estado, dias coletados etc.
        self._mare   = []   # últimas N operações reais para maré
        self._carregar()

    # ── Persistência ─────────────────────────────────────────────────────────
    def _carregar(self):
        # Raw
        try:
            if os.path.exists(_RAW_FILE):
                with open(_RAW_FILE, "r", encoding="utf-8") as f:
                    self._raw = json.load(f)
        except Exception:
            self._raw = []

        # Aggregated + config
        try:
            if os.path.exists(_AGG_FILE):
                with open(_AGG_FILE, "r", encoding="utf-8") as f:
                    dados = json.load(f)
                self._config = dados.get("config", {})
                self._agg    = dados.get("slots", {})
        except Exception:
            self._config = {}
            self._agg    = {}

        # Maré: últimas MARE_JANELA operações reais
        reais = [e for e in self._raw if not e.get("virtual")]
        self._mare = [e["resultado"] for e in reais[-MARE_JANELA:]]

    def _salvar(self):
        """Salva raw e agg em disco (chama dentro de lock)."""
        try:
            with open(_RAW_FILE, "w", encoding="utf-8") as f:
                json.dump(self._raw[-MAX_RAW:], f, ensure_ascii=False)
        except Exception:
            pass
        try:
            with open(_AGG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "config": self._config,
                    "slots":  self._agg,
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── Registro de experiência ───────────────────────────────────────────────
    def registrar(
        self,
        resultado:  str,   # "WIN" | "LOSS"
        estrategia: str  = "",
        ativo:      str  = "",
        regime:     str  = "DESCONHECIDO",
        confianca:  float = 0.0,
        virtual:    bool  = False,
    ):
        """Registra resultado e atualiza slots + maré."""
        agora = _hora_brt()
        slot  = _slot_de(agora)
        r     = resultado.strip().upper()
        if r not in ("WIN", "LOSS"):
            return

        exp = {
            "timestamp":  agora.timestamp(),
            "data":       agora.strftime("%Y-%m-%d"),
            "hora":       agora.strftime("%H:%M:%S"),
            "slot":       slot,
            "dia_semana": agora.weekday(),   # 0=seg … 6=dom
            "estrategia": estrategia,
            "ativo":      ativo,
            "regime":     regime,
            "confianca":  confianca,
            "resultado":  r,
            "virtual":    virtual,
        }

        with self._lock:
            self._raw.append(exp)
            if not virtual:
                self._mare.append(r)
                if len(self._mare) > MARE_JANELA:
                    self._mare = self._mare[-MARE_JANELA:]
            self._recalcular_slot(slot)
            self._atualizar_estado()
            self._salvar()

    # ── Recalcular slot específico ────────────────────────────────────────────
    def _recalcular_slot(self, slot: str):
        """Recalcula agregados do slot usando pesos por idade."""
        agora_ts = _hora_brt().timestamp()
        exps = [e for e in self._raw if e.get("slot") == slot]

        wins_w  = 0.0
        loss_w  = 0.0
        total_w = 0.0
        regimes_w: dict = {}
        ops_real = 0

        for e in exps:
            d  = _dias_atras(e["timestamp"])
            if d > 7:
                continue  # descarta mais antigo que 7 dias
            peso = _peso_dia(e["timestamp"])
            if e.get("virtual"):
                peso *= PESO_VIRTUAL

            if e["resultado"] == "WIN":
                wins_w  += peso
            else:
                loss_w  += peso
            total_w += peso

            if not e.get("virtual"):
                ops_real += 1

            # Por regime
            reg = e.get("regime", "DESCONHECIDO")
            if reg not in regimes_w:
                regimes_w[reg] = {"wins": 0.0, "losses": 0.0}
            if e["resultado"] == "WIN":
                regimes_w[reg]["wins"]   += peso
            else:
                regimes_w[reg]["losses"] += peso

        if total_w == 0 or ops_real < MIN_OPS_POR_SLOT:
            status = "APRENDENDO"
            wr     = 0.0
        else:
            wr = round((wins_w / total_w) * 100, 1)
            if wr >= WR_BOM:
                status = "OPERAR"
            elif wr <= WR_RUIM:
                status = "BLOQUEAR"
            else:
                status = "NEUTRO"

        # Detalhe por regime
        regime_status = {}
        for reg, rv in regimes_w.items():
            total_r = rv["wins"] + rv["losses"]
            if total_r > 0:
                wr_r = round((rv["wins"] / total_r) * 100, 1)
                regime_status[reg] = {
                    "wr":     wr_r,
                    "status": "OPERAR" if wr_r >= WR_BOM else ("BLOQUEAR" if wr_r <= WR_RUIM else "NEUTRO"),
                }

        self._agg[slot] = {
            "operacoes":     len(exps),
            "operacoes_real": ops_real,
            "wins_w":        round(wins_w, 2),
            "losses_w":      round(loss_w, 2),
            "win_rate":      wr,
            "status":        status,
            "regime_status": regime_status,
            "atualizado":    agora_ts,
        }

    # ── Atualizar estado global ───────────────────────────────────────────────
    def _atualizar_estado(self):
        agora = _hora_brt()
        # Dias únicos com operações reais nos últimos 8 dias
        corte = agora.timestamp() - 8 * 86400
        dias_com_op = set(
            e["data"] for e in self._raw
            if not e.get("virtual") and e["timestamp"] >= corte
        )
        dias_coletados = len(dias_com_op)

        if dias_coletados < DIAS_MINIMOS:
            estado = "APRENDIZADO"
        else:
            # Quantos slots têm dados suficientes
            com_dados = sum(1 for s in self._agg.values()
                            if s.get("operacoes_real", 0) >= MIN_OPS_POR_SLOT)
            estado = "ATIVO" if com_dados >= 3 else "ANALISANDO"

        self._config.update({
            "estado":         estado,
            "dias_coletados": dias_coletados,
            "dias_minimos":   DIAS_MINIMOS,
            "total_raw":      len(self._raw),
            "ultimo_update":  agora.isoformat(),
        })

    # ── Consulta: pode operar agora? ──────────────────────────────────────────
    def pode_operar(
        self,
        estrategia: str  = "",
        ativo:      str  = "",
        regime:     str  = "DESCONHECIDO",
        confianca:  float = 0.0,
        enabled:    bool  = True,
    ) -> dict:
        """
        Retorna dict com:
          permitir   bool
          motivo     str
          estado     str   (APRENDIZADO|ANALISANDO|ATIVO)
          mare       str   (BOA|RUIM|NEUTRA)
          slot       str   (HH:MM)
          wr_slot    float
          status_slot str  (OPERAR|BLOQUEAR|NEUTRO|APRENDENDO)
        """
        if not enabled:
            return self._resp(True, "MTE_DESATIVADO")

        with self._lock:
            agora  = _hora_brt()
            slot   = _slot_de(agora)
            estado = self._config.get("estado", "APRENDIZADO")
            mare   = self._avaliar_mare()
            info   = self._agg.get(slot, {})
            wr     = info.get("win_rate", 0.0)
            status = info.get("status", "APRENDENDO")
            reg_st = info.get("regime_status", {})

        # ── APRENDIZADO / ANALISANDO: nunca bloqueia ─────────────────────────
        if estado in ("APRENDIZADO", "ANALISANDO"):
            return self._resp(True, f"MTE_{estado}", estado, mare, slot, wr, status)

        # ── ATIVO: verifica slot ──────────────────────────────────────────────
        # 1. Verifica status do slot por regime específico
        if regime and regime in reg_st:
            st_reg = reg_st[regime].get("status", "NEUTRO")
            wr_reg = reg_st[regime].get("wr", wr)
            if st_reg == "BLOQUEAR":
                return self._resp(
                    False,
                    f"MTE_SLOT_REGIME_RUIM:{slot}:{regime}:WR={wr_reg}%",
                    estado, mare, slot, wr_reg, "BLOQUEAR"
                )

        # 2. Verifica status geral do slot
        if status == "BLOQUEAR":
            return self._resp(False, f"MTE_SLOT_RUIM:{slot}:WR={wr}%",
                              estado, mare, slot, wr, status)

        # 3. Verifica maré
        if mare == "RUIM":
            return self._resp(False, "MTE_MARE_RUIM",
                              estado, mare, slot, wr, status)

        return self._resp(True, f"MTE_LIBERADO:{slot}:WR={wr}%",
                          estado, mare, slot, wr, status)

    def _resp(self, permitir, motivo, estado="APRENDIZADO",
              mare="NEUTRA", slot="", wr=0.0, status="APRENDENDO") -> dict:
        return {
            "permitir":    permitir,
            "motivo":      motivo,
            "estado":      estado,
            "mare":        mare,
            "slot":        slot,
            "wr_slot":     wr,
            "status_slot": status,
            "dias":        self._config.get("dias_coletados", 0),
        }

    # ── Maré ──────────────────────────────────────────────────────────────────
    def _avaliar_mare(self) -> str:
        """Analisa as últimas N operações reais."""
        if len(self._mare) < 3:
            return "NEUTRA"
        ultimas = self._mare[-MARE_JANELA:]
        losses_consec = 0
        for r in reversed(ultimas):
            if r == "LOSS":
                losses_consec += 1
            else:
                break
        if losses_consec >= MARE_LOSSES_BLOQUEAR:
            return "RUIM"
        # Verifica recuperação: últimas wins consecutivas
        wins_consec = 0
        for r in reversed(ultimas):
            if r == "WIN":
                wins_consec += 1
            else:
                break
        if wins_consec >= MARE_WINS_LIBERAR:
            return "BOA"
        return "NEUTRA"

    # ── Reprocessar todos os slots ────────────────────────────────────────────
    def reprocessar(self):
        """Reconstrói todos os slots a partir do raw. Útil após import."""
        with self._lock:
            slots_unicos = set(e.get("slot") for e in self._raw if e.get("slot"))
            for s in slots_unicos:
                self._recalcular_slot(s)
            self._atualizar_estado()
            self._salvar()

    # ── Status público ────────────────────────────────────────────────────────
    def status(self) -> dict:
        """Retorna snapshot completo para a API."""
        with self._lock:
            agora = _hora_brt()
            slot  = _slot_de(agora)
            mare  = self._avaliar_mare()

            # Top 3 melhores e piores slots
            slots_validos = [
                (k, v) for k, v in self._agg.items()
                if v.get("operacoes_real", 0) >= MIN_OPS_POR_SLOT
            ]
            melhores = sorted(slots_validos, key=lambda x: -x[1]["win_rate"])[:3]
            piores   = sorted(slots_validos, key=lambda x:  x[1]["win_rate"])[:3]

            # Próximos slots (próximas 2 horas)
            proximos = []
            for m in range(0, 120, SLOT_MINUTOS):
                fut = agora + _dt.timedelta(minutes=m)
                s   = _slot_de(fut)
                inf = self._agg.get(s, {})
                proximos.append({
                    "slot":    s,
                    "status":  inf.get("status", "APRENDENDO"),
                    "wr":      inf.get("win_rate", 0.0),
                })
            # Remove duplicatas mantendo ordem
            vistos = set()
            proximos_uniq = []
            for p in proximos:
                if p["slot"] not in vistos:
                    vistos.add(p["slot"])
                    proximos_uniq.append(p)

            # Contagens
            n_operar   = sum(1 for _, v in self._agg.items() if v.get("status") == "OPERAR")
            n_bloquear = sum(1 for _, v in self._agg.items() if v.get("status") == "BLOQUEAR")
            n_neutro   = sum(1 for _, v in self._agg.items() if v.get("status") == "NEUTRO")
            n_aprendendo = sum(1 for _, v in self._agg.items() if v.get("status") == "APRENDENDO")

            slot_atual_info = self._agg.get(slot, {})

            return {
                "estado":        self._config.get("estado", "APRENDIZADO"),
                "dias_coletados": self._config.get("dias_coletados", 0),
                "dias_minimos":  DIAS_MINIMOS,
                "total_operacoes": len(self._raw),
                "mare":          mare,
                "mare_historico": self._mare[-MARE_JANELA:],
                "slot_atual": {
                    "slot":    slot,
                    "status":  slot_atual_info.get("status", "APRENDENDO"),
                    "wr":      slot_atual_info.get("win_rate", 0.0),
                    "ops":     slot_atual_info.get("operacoes_real", 0),
                },
                "proximos_slots": proximos_uniq[:8],
                "melhores_slots": [
                    {"slot": k, "wr": v["win_rate"], "ops": v.get("operacoes_real", 0)}
                    for k, v in melhores
                ],
                "piores_slots": [
                    {"slot": k, "wr": v["win_rate"], "ops": v.get("operacoes_real", 0)}
                    for k, v in piores
                ],
                "contagens": {
                    "operar":    n_operar,
                    "bloquear":  n_bloquear,
                    "neutro":    n_neutro,
                    "aprendendo": n_aprendendo,
                },
                "todos_slots": {
                    k: {
                        "wr":     v.get("win_rate", 0.0),
                        "status": v.get("status", "APRENDENDO"),
                        "ops":    v.get("operacoes_real", 0),
                    }
                    for k, v in sorted(self._agg.items())
                },
            }

    def limpar_antigos(self):
        """Remove experiências com mais de 7 dias."""
        corte = _hora_brt().timestamp() - 7 * 86400
        with self._lock:
            antes = len(self._raw)
            self._raw = [e for e in self._raw if e.get("timestamp", 0) >= corte]
            if len(self._raw) < antes:
                self.reprocessar()


# ── Instância singleton ────────────────────────────────────────────────────────
_mte_instance: Optional[MemoryTimeEngine] = None
_mte_lock = threading.Lock()


def get_mte() -> MemoryTimeEngine:
    global _mte_instance
    with _mte_lock:
        if _mte_instance is None:
            _mte_instance = MemoryTimeEngine()
    return _mte_instance


# ── API conveniente ────────────────────────────────────────────────────────────
def mte_pode_operar(
    estrategia: str  = "",
    ativo:      str  = "",
    regime:     str  = "DESCONHECIDO",
    confianca:  float = 0.0,
    enabled:    bool  = True,
) -> dict:
    return get_mte().pode_operar(estrategia, ativo, regime, confianca, enabled)


def mte_registrar(
    resultado:  str,
    estrategia: str  = "",
    ativo:      str  = "",
    regime:     str  = "DESCONHECIDO",
    confianca:  float = 0.0,
    virtual:    bool  = False,
):
    get_mte().registrar(resultado, estrategia, ativo, regime, confianca, virtual)


def mte_status() -> dict:
    return get_mte().status()
