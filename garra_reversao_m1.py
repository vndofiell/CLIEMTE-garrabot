# ═══════════════════════════════════════════════════════════════════════════════
# GARRA REVERSÃO M1 PRO — Motor de Análise
# Entrada CALL/PUT somente na virada da vela M1
# Price Action + Estrutura + EMA + RSI + MACD + ADX + ATR + S/R + Bollinger
# ═══════════════════════════════════════════════════════════════════════════════
import os
import json
import time
import math

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_VAULT_FILE = os.path.join(_BASE_DIR, "garra_m1_vault.json")
_CFG_FILE   = os.path.join(_BASE_DIR, "garra_m1_config.json")

# ── Configuração padrão ───────────────────────────────────────────────────────
CFG_DEFAULT = {
    "ativo":                     "R_10",
    "mercado":                   "automatico",
    "duracao":                   1,
    "duracao_unidade":           "m",
    "score_minimo":              85,
    "janela_entrada_segundos":   2,
    "ema_rapida":                20,
    "ema_media":                 50,
    "ema_lenta":                 200,
    "rsi_periodo":               14,
    "rsi_call":                  52,
    "rsi_put":                   48,
    "adx_minimo":                20,
    "atr_periodo":               14,
    "usar_price_action":         True,
    "usar_estrutura":            True,
    "usar_ema":                  True,
    "usar_rsi":                  True,
    "usar_macd":                 True,
    "usar_adx":                  True,
    "usar_atr":                  True,
    "usar_sr":                   True,
    "usar_bollinger":            True,
    "usar_anti_doji":            True,
    "bloquear_lateral":          True,
    "bloquear_falso_rompimento": True,
    "stake":                     0.35,
    "stop_win":                  10.0,
    "stop_loss":                 5.0,
    "gerenciamento":             "flat",
    "mte_ativo":                 True,
    "modo_demo":                 True,
}


# ── Helpers de indicadores ────────────────────────────────────────────────────

def _ema(closes: list, periodo: int) -> float:
    """EMA exponencial clássica."""
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
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - periodo, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    gains  /= periodo
    losses /= periodo
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + gains / losses)


def _macd(closes: list, fast=12, slow=26, signal=9) -> tuple:
    """Retorna (macd_line, signal_line, histogram)."""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    kf = 2.0 / (fast   + 1)
    ks = 2.0 / (slow   + 1)
    kg = 2.0 / (signal + 1)
    ef = sum(closes[:fast]) / fast
    es = sum(closes[:slow]) / slow
    for i in range(fast, len(closes)):
        ef = closes[i] * kf + ef * (1 - kf)
    for i in range(slow, len(closes)):
        es = closes[i] * ks + es * (1 - ks)
    macd_line = ef - es
    # série de macd_line (últimos signal+1 pontos — aproximação)
    macd_series = []
    ef2 = sum(closes[:fast]) / fast
    es2 = sum(closes[:slow]) / slow
    kf2 = 2.0 / (fast + 1)
    ks2 = 2.0 / (slow + 1)
    for i in range(1, len(closes)):
        ef2 = closes[i] * kf2 + ef2 * (1 - kf2)
        if i >= slow - 1:
            es2 = closes[i] * ks2 + es2 * (1 - ks2)
            macd_series.append(ef2 - es2)
    if len(macd_series) < signal:
        sig_line = macd_line
    else:
        sig_line = sum(macd_series[-signal:]) / signal
        for v in macd_series[-signal:]:
            sig_line = v * kg + sig_line * (1 - kg)
    return macd_line, sig_line, macd_line - sig_line


def _atr(velas: list, periodo: int = 14) -> float:
    slc = velas[-periodo - 1:]
    if len(slc) < 2:
        return 0.0
    trs = []
    for i in range(1, len(slc)):
        hl = slc[i]["maxima"]  - slc[i]["minima"]
        hc = abs(slc[i]["maxima"]  - slc[i - 1]["fechamento"])
        lc = abs(slc[i]["minima"]  - slc[i - 1]["fechamento"])
        trs.append(max(hl, hc, lc))
    return sum(trs) / len(trs)


def _adx_proxy(closes: list, velas: list) -> float:
    """Proxy de ADX: |EMA20 - EMA50| / ATR * 10."""
    e20 = _ema(closes, 20)
    e50 = _ema(closes, 50)
    atr = _atr(velas) or 1e-9
    if math.isnan(e20) or math.isnan(e50):
        return 0.0
    return min(100.0, abs(e20 - e50) / atr * 10.0)


def _bollinger(closes: list, periodo: int = 20, desvios: float = 2.0) -> dict:
    if len(closes) < periodo:
        return {"upper": 0, "middle": 0, "lower": 0, "pct_b": 0.5}
    slc = closes[-periodo:]
    mid = sum(slc) / periodo
    std = math.sqrt(sum((c - mid) ** 2 for c in slc) / periodo)
    upper = mid + desvios * std
    lower = mid - desvios * std
    last  = closes[-1]
    pct_b = (last - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    return {"upper": upper, "middle": mid, "lower": lower, "pct_b": pct_b}


def _estrutura(velas: list) -> str:
    """Detecta HH/HL (ALTA), LH/LL (BAIXA) ou LATERAL."""
    if len(velas) < 6:
        return "INDEFINIDA"
    maximas = [v["maxima"]    for v in velas[-6:]]
    minimas = [v["minima"]    for v in velas[-6:]]
    hh = maximas[-1] > maximas[-3] > maximas[-5]
    hl = minimas[-1] > minimas[-3] > minimas[-5]
    lh = maximas[-1] < maximas[-3] < maximas[-5]
    ll = minimas[-1] < minimas[-3] < minimas[-5]
    if hh and hl:
        return "ALTA"
    if lh and ll:
        return "BAIXA"
    return "LATERAL"


def _detectar_candle(vela: dict, vela_ant: dict | None) -> dict:
    ab = vela["abertura"]
    fc = vela["fechamento"]
    mx = vela["maxima"]
    mn = vela["minima"]
    corpo       = abs(fc - ab)
    rng         = mx - mn if mx != mn else 1e-9
    pav_sup     = mx - max(ab, fc)
    pav_inf     = min(ab, fc) - mn
    pct_corpo   = corpo / rng
    bullish     = fc > ab
    bearish     = fc < ab
    doji        = pct_corpo < 0.10
    pin_bar_call = pav_inf >= corpo * 2 and bullish
    pin_bar_put  = pav_sup >= corpo * 2 and bearish
    marubozu     = pct_corpo >= 0.75
    resultado = {
        "doji":                 doji,
        "bullish":              bullish,
        "bearish":              bearish,
        "marubozu":             marubozu,
        "pin_bar_call":         pin_bar_call,
        "pin_bar_put":          pin_bar_put,
        "pct_corpo":            pct_corpo,
        "bullish_confirmation": False,
        "bearish_confirmation": False,
    }
    # Engolfo
    if vela_ant:
        ab2 = vela_ant["abertura"]
        fc2 = vela_ant["fechamento"]
        if bullish and fc > ab2 and ab < fc2:
            resultado["bullish_confirmation"] = True
        if bearish and fc < ab2 and ab > fc2:
            resultado["bearish_confirmation"] = True
    # Força simples
    if not resultado["bullish_confirmation"] and (marubozu or pin_bar_call):
        resultado["bullish_confirmation"] = bullish
    if not resultado["bearish_confirmation"] and (marubozu or pin_bar_put):
        resultado["bearish_confirmation"] = bearish
    return resultado


def _suporte_resistencia(velas: list, janela: int = 20) -> dict:
    """Detecta rejeição de S/R nas últimas `janela` velas."""
    if len(velas) < janela + 1:
        return {"rejeicao_suporte": False, "rejeicao_resistencia": False}
    hist     = velas[-janela - 1: -1]
    atual    = velas[-1]
    maximas  = [v["maxima"] for v in hist]
    minimas  = [v["minima"] for v in hist]
    r_level  = max(maximas)
    s_level  = min(minimas)
    atr_val  = _atr(velas) or 1e-9
    perto_r  = abs(atual["fechamento"] - r_level) < atr_val * 0.5
    perto_s  = abs(atual["fechamento"] - s_level) < atr_val * 0.5
    # Rejeição: vela fechou longe mas tocou o nível
    rej_s = perto_s and atual["fechamento"] > s_level
    rej_r = perto_r and atual["fechamento"] < r_level
    return {"rejeicao_suporte": rej_s, "rejeicao_resistencia": rej_r,
            "nivel_suporte": s_level, "nivel_resistencia": r_level}


def _falso_rompimento(velas: list, janela: int = 10) -> bool:
    """Detecta falso rompimento: fechou além do range mas voltou."""
    if len(velas) < janela + 2:
        return False
    hist    = velas[-janela - 2: -2]
    pen     = velas[-2]  # penúltima
    atual   = velas[-1]
    r_max   = max(v["maxima"] for v in hist)
    r_min   = min(v["minima"] for v in hist)
    # Penúltima rompeu para cima mas atual fechou abaixo
    if pen["fechamento"] > r_max and atual["fechamento"] < r_max:
        return True
    # Penúltima rompeu para baixo mas atual fechou acima
    if pen["fechamento"] < r_min and atual["fechamento"] > r_min:
        return True
    return False


# ── Motor principal ───────────────────────────────────────────────────────────

class GarraReversaoM1Engine:
    """
    Motor da estratégia GARRA REVERSÃO M1 PRO.
    Analisa a vela FECHADA e decide CALL / PUT / AGUARDAR.
    A entrada só é autorizada nos primeiros `janela_entrada_segundos`
    após a abertura da nova vela — regra aplicada pelo front-end.
    """

    def __init__(self):
        self.historico  = self._carregar_vault()
        self.config     = self._carregar_config()

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
                    cfg = json.load(f)
                    return {**CFG_DEFAULT, **cfg}
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

    # ── Avaliação principal ───────────────────────────────────────────────────
    def avaliar(self, dados: dict) -> dict:
        """
        Parâmetros esperados em `dados`:
          velas          : list[dict]  — OHLC das velas M1 fechadas
                           cada vela: {abertura, fechamento, maxima, minima, timestamp}
          ts_vela_atual  : float       — timestamp unix da vela que acabou de abrir
          ts_agora       : float       — timestamp unix atual
          cfg_override   : dict        — sobrescreve config temporariamente (opcional)
          mte_resultado  : dict        — resposta do MTE (opcional)
        """
        cfg    = {**self.config, **(dados.get("cfg_override") or {})}
        velas  = dados.get("velas", [])
        ts_now = float(dados.get("ts_agora",      time.time()))
        ts_vela= float(dados.get("ts_vela_atual", ts_now))
        janela = int(cfg.get("janela_entrada_segundos", 2))
        score_min = int(cfg.get("score_minimo", 85))

        # Mínimo de velas para calcular os indicadores (EMA200 precisa de 200+)
        # Com menos de 200 velas a EMA200 fica imprecisa mas os outros indicadores funcionam.
        # Aceitamos a partir de 50 velas para não bloquear logo após o carregamento via API.
        if len(velas) < 50:
            return self._aguardar(
                f"Histórico insuficiente ({len(velas)}/50 velas)",
                score_call=0, score_put=0, cfg=cfg
            )

        # Vela fechada (confirmação) e vela atual (para checar virada)
        anterior = velas[-2]
        # atual    = velas[-1]  # timestamp da vela recém-aberta

        # ── JANELA DE ENTRADA ─────────────────────────────────────────────
        # O controle da janela de entrada (N segundos após virada) é feito
        # inteiramente no front-end. O servidor apenas analisa e retorna o score.
        # Mantemos a verificação apenas para rejeitar chamadas muito atrasadas
        # (> 55s significa que é do ciclo anterior — bug de timing).
        seg_desde_abertura = ts_now - ts_vela
        if seg_desde_abertura > 55:
            return self._aguardar(
                f"Chamada fora do ciclo ({seg_desde_abertura:.0f}s)",
                0, 0, cfg
            )

        closes = [v["fechamento"] for v in velas]
        score_call, score_put = 0, 0
        motivos_call, motivos_put = [], []
        detalhes = {}

        # ── 1. ESTRUTURA (+15) ────────────────────────────────────────────
        if cfg.get("usar_estrutura", True):
            est = _estrutura(velas[:-1])  # usa apenas velas fechadas
            detalhes["estrutura"] = est
            if est == "ALTA":
                score_call += 15
                motivos_call.append("estrutura HH/HL")
            elif est == "BAIXA":
                score_put  += 15
                motivos_put.append("estrutura LH/LL")
            elif est == "LATERAL" and cfg.get("bloquear_lateral", True):
                return self._aguardar("Mercado lateral", score_call, score_put, cfg,
                                      detalhes=detalhes)

        # ── 2. EMA (+30 total: 10+10+10) ─────────────────────────────────
        if cfg.get("usar_ema", True):
            ema20  = _ema(closes, int(cfg.get("ema_rapida", 20)))
            ema50  = _ema(closes, int(cfg.get("ema_media",  50)))
            ema200 = _ema(closes, int(cfg.get("ema_lenta",  200)))
            detalhes.update({"ema20": ema20, "ema50": ema50, "ema200": ema200})
            if not (math.isnan(ema20) or math.isnan(ema50) or math.isnan(ema200)):
                if ema20 > ema50:
                    score_call += 10; motivos_call.append("EMA20 > EMA50")
                else:
                    score_put  += 10; motivos_put.append("EMA20 < EMA50")
                if ema50 > ema200:
                    score_call += 10; motivos_call.append("EMA50 > EMA200")
                else:
                    score_put  += 10; motivos_put.append("EMA50 < EMA200")
                # alinhamento completo vale mais
                if ema20 > ema50 > ema200:
                    score_call += 10; motivos_call.append("alinhamento EMA completo")
                elif ema20 < ema50 < ema200:
                    score_put  += 10; motivos_put.append("alinhamento EMA completo")

        # ── 3. RSI (+10) ──────────────────────────────────────────────────
        if cfg.get("usar_rsi", True):
            rsi_val = _rsi(closes, int(cfg.get("rsi_periodo", 14)))
            detalhes["rsi"] = round(rsi_val, 2)
            rsi_call_thr = float(cfg.get("rsi_call", 52))
            rsi_put_thr  = float(cfg.get("rsi_put",  48))
            if rsi_val > rsi_call_thr:
                score_call += 10; motivos_call.append(f"RSI {rsi_val:.1f} > {rsi_call_thr}")
            if rsi_val < rsi_put_thr:
                score_put  += 10; motivos_put.append(f"RSI {rsi_val:.1f} < {rsi_put_thr}")

        # ── 4. MACD (+10) ─────────────────────────────────────────────────
        if cfg.get("usar_macd", True):
            macd_line, sig_line, hist_val = _macd(closes)
            detalhes["macd"] = round(macd_line, 6)
            detalhes["macd_hist"] = round(hist_val, 6)
            if macd_line > 0 and hist_val > 0:
                score_call += 10; motivos_call.append("MACD positivo")
            elif macd_line < 0 and hist_val < 0:
                score_put  += 10; motivos_put.append("MACD negativo")

        # ── 5. ADX (+5, bloqueio se < mínimo) ────────────────────────────
        if cfg.get("usar_adx", True):
            adx_val = _adx_proxy(closes, velas[:-1])
            adx_min = float(cfg.get("adx_minimo", 20))
            detalhes["adx"] = round(adx_val, 2)
            if adx_val < adx_min:
                return self._aguardar(
                    f"ADX insuficiente ({adx_val:.1f} < {adx_min})",
                    score_call, score_put, cfg, detalhes=detalhes
                )
            score_call += 5; score_put += 5  # força presente para os dois
            motivos_call.append(f"ADX {adx_val:.1f}")
            motivos_put.append(f"ADX {adx_val:.1f}")

        # ── 6. ATR (+5) ───────────────────────────────────────────────────
        if cfg.get("usar_atr", True):
            atr_val = _atr(velas[:-1], int(cfg.get("atr_periodo", 14)))
            detalhes["atr"] = round(atr_val, 6)
            if atr_val > 0:
                score_call += 5; score_put += 5
                motivos_call.append(f"ATR ok ({atr_val:.5f})")
                motivos_put.append(f"ATR ok ({atr_val:.5f})")

        # ── 7. PRICE ACTION — vela anterior (+10) ─────────────────────────
        if cfg.get("usar_price_action", True):
            vela_ant2 = velas[-3] if len(velas) >= 3 else None
            candle    = _detectar_candle(anterior, vela_ant2)
            detalhes["candle"] = {k: v for k, v in candle.items() if isinstance(v, bool)}
            # Anti-Doji absoluto
            if candle["doji"] and cfg.get("usar_anti_doji", True):
                return self._aguardar("Doji bloqueado", score_call, score_put, cfg,
                                      detalhes=detalhes)
            if candle["bullish_confirmation"]:
                score_call += 10; motivos_call.append("confirmação PA CALL")
            if candle["bearish_confirmation"]:
                score_put  += 10; motivos_put.append("confirmação PA PUT")

        # ── 8. SUPORTE / RESISTÊNCIA (+10) ───────────────────────────────
        if cfg.get("usar_sr", True):
            sr = _suporte_resistencia(velas[:-1])
            detalhes["sr"] = sr
            if sr["rejeicao_suporte"]:
                score_call += 10; motivos_call.append("rejeição de suporte")
            if sr["rejeicao_resistencia"]:
                score_put  += 10; motivos_put.append("rejeição de resistência")

        # ── 9. BOLLINGER (+5) ─────────────────────────────────────────────
        if cfg.get("usar_bollinger", True):
            bb = _bollinger(closes[:-1])
            detalhes["bollinger_pct_b"] = round(bb["pct_b"], 3)
            if bb["pct_b"] < 0.20:
                score_call += 5; motivos_call.append("BB baixo → call")
            elif bb["pct_b"] > 0.80:
                score_put  += 5; motivos_put.append("BB alto → put")

        # ── 10. FALSO ROMPIMENTO (bloqueio absoluto) ──────────────────────
        if cfg.get("bloquear_falso_rompimento", True):
            if _falso_rompimento(velas[:-1]):
                return self._aguardar("Falso rompimento detectado",
                                      score_call, score_put, cfg, detalhes=detalhes)

        # ── DECISÃO ───────────────────────────────────────────────────────
        direcao, score_final, motivos = None, 0, []
        if score_call >= score_min and score_call > score_put:
            direcao, score_final, motivos = "CALL", score_call, motivos_call
        elif score_put >= score_min and score_put > score_call:
            direcao, score_final, motivos = "PUT", score_put, motivos_put

        if not direcao:
            return {
                "operar":      False,
                "direcao":     "AGUARDAR",
                "score_call":  score_call,
                "score_put":   score_put,
                "score_minimo": score_min,
                "motivo":      "Confluência insuficiente",
                "detalhes":    detalhes,
                "cfg":         {k: cfg[k] for k in ("ativo","duracao","duracao_unidade","stake")},
            }

        return {
            "operar":            True,
            "direcao":           direcao,
            "score":             score_final,
            "score_call":        score_call,
            "score_put":         score_put,
            "score_minimo":      score_min,
            "motivos":           motivos,
            "detalhes":          detalhes,
            "tipo_entrada":      "VIRADA_VELA",
            "ts_vela":           ts_vela,
            "janela_segundos":   janela,
            "cfg": {
                "ativo":          cfg.get("ativo", "R_10"),
                "duracao":        cfg.get("duracao", 1),
                "duracao_unidade":cfg.get("duracao_unidade", "m"),
                "stake":          cfg.get("stake", 0.35),
                "gerenciamento":  cfg.get("gerenciamento", "flat"),
            },
        }

    def _aguardar(self, motivo: str, score_call: int, score_put: int,
                  cfg: dict, detalhes: dict = None) -> dict:
        return {
            "operar":    False,
            "direcao":   "AGUARDAR",
            "score_call": score_call,
            "score_put":  score_put,
            "score_minimo": int(cfg.get("score_minimo", 85)),
            "motivo":    motivo,
            "detalhes":  detalhes or {},
            "cfg": {k: cfg.get(k) for k in ("ativo","duracao","duracao_unidade","stake")},
        }

    # ── Estatísticas ─────────────────────────────────────────────────────────
    def estatisticas(self) -> dict:
        total = len(self.historico)
        if total == 0:
            return {"total": 0, "wins": 0, "losses": 0, "wr": 0.0}
        wins   = sum(1 for op in self.historico if str(op.get("resultado","")).upper() == "WIN")
        losses = total - wins
        return {
            "total":   total,
            "wins":    wins,
            "losses":  losses,
            "wr":      round(wins / total * 100, 1),
            "ultimas_10": self.historico[-10:],
        }


# Instância global
_engine = GarraReversaoM1Engine()


def get_engine() -> GarraReversaoM1Engine:
    return _engine
