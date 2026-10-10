"""Estratégia educativa de fluxo OTC para Quotex.
Recebe candles OHLC FECHADOS e devolve CALL/PUT/NO_TRADE.
Não envia ordens nem promete rentabilidade.
"""
from statistics import median


def _num(c, *keys):
    for k in keys:
        if k in c:
            try:
                return float(c[k])
            except (TypeError, ValueError):
                pass
    raise ValueError(f"Candle sem campo numérico: {keys[0]}")


def _normalizar(candles):
    out = []
    for c in candles:
        if not isinstance(c, dict):
            raise ValueError("Cada candle deve ser um objeto com open/high/low/close.")
        o  = _num(c, "open",  "o")
        h  = _num(c, "high",  "h")
        l  = _num(c, "low",   "l")
        cl = _num(c, "close", "c")
        if h < max(o, cl, l) or l > min(o, cl, h):
            raise ValueError("OHLC inválido: máxima/mínima inconsistente.")
        out.append({"open": o, "high": h, "low": l, "close": cl})
    return out


def _ema(vals, period):
    if len(vals) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    e = sum(vals[:period]) / period
    result = [None] * (period - 1) + [e]
    for v in vals[period:]:
        e = alpha * v + (1.0 - alpha) * e
        result.append(e)
    return result


def _rsi(vals, period=14):
    if len(vals) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = ((period - 1) * ag + gains[i]) / period
        al = ((period - 1) * al + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _atr(candles, period=14):
    trs = []
    for i, c in enumerate(candles):
        prev = candles[i - 1]["close"] if i else c["close"]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - prev), abs(c["low"] - prev)))
    if len(trs) < period:
        return None, []
    smooth = sum(trs[:period]) / period
    arr = [None] * (period - 1) + [smooth]
    for tr in trs[period:]:
        smooth = ((period - 1) * smooth + tr) / period
        arr.append(smooth)
    return arr[-1], arr


def _adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None, None, None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up   = cur["high"] - prev["high"]
        down = prev["low"]  - cur["low"]
        plus_dm.append(up   if up   > down and up   > 0 else 0.0)
        minus_dm.append(down if down > up   and down > 0 else 0.0)
        trs.append(max(cur["high"] - cur["low"],
                       abs(cur["high"] - prev["close"]),
                       abs(cur["low"]  - prev["close"])))

    def wilder(arr):
        s = sum(arr[:period])
        out = [None] * (period - 1) + [s]
        for v in arr[period:]:
            s = s - s / period + v
            out.append(s)
        return out

    tr_s, p_s, m_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dx = []
    last_pdi = last_mdi = None
    for tr, p, m in zip(tr_s, p_s, m_s):
        if tr is None or tr <= 0:
            dx.append(None)
            continue
        pdi, mdi = 100 * p / tr, 100 * m / tr
        last_pdi, last_mdi = pdi, mdi
        den = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / den if den else 0.0)
    valid = [x for x in dx if x is not None]
    if len(valid) < period:
        return None, last_pdi, last_mdi
    adx = sum(valid[:period]) / period
    for x in valid[period:]:
        adx = ((period - 1) * adx + x) / period
    return adx, last_pdi, last_mdi


def analisar_otc_fluxo(candles, confianca_minima=75):
    """Analisa fluxo de velas + EMA 9/21/50 + RSI14 + ADX14 + ATR14.

    Envie apenas velas fechadas, ordenadas da mais antiga para a mais recente.
    O score é pontuação de confluência, NÃO probabilidade de vitória.
    """
    if not isinstance(candles, list):
        raise ValueError("'candles' precisa ser uma lista OHLC.")
    cs = _normalizar(candles)
    if len(cs) < 60:
        return {"ok": True, "estrategia": "OTC Fluxo EMA Pro", "decisao": "NO_TRADE", "score": 0,
                "motivos": [f"Dados insuficientes: {len(cs)}/60 candles; use pelo menos 60 candles fechados."],
                "indicadores": {}}

    closes = [c["close"] for c in cs]
    e9, e21, e50 = _ema(closes, 9), _ema(closes, 21), _ema(closes, 50)
    rsi = _rsi(closes, 14)
    atr, atr_series = _atr(cs, 14)
    adx, pdi, mdi   = _adx(cs, 14)
    if any(v is None for v in (e9[-1], e21[-1], e50[-1], e50[-4], rsi, atr, adx, pdi, mdi)) or atr <= 0:
        return {"ok": True, "estrategia": "OTC Fluxo EMA Pro", "decisao": "NO_TRADE", "score": 0,
                "motivos": ["Indicadores ainda sem dados suficientes."], "indicadores": {}}

    last  = cs[-1]
    prev  = cs[-2]
    body  = abs(last["close"] - last["open"])
    rng   = max(last["high"] - last["low"], 1e-12)
    upper_wick  = last["high"] - max(last["open"], last["close"])
    lower_wick  = min(last["open"], last["close"]) - last["low"]
    body_ratio  = body / rng
    distance_ema9_atr = abs(last["close"] - e9[-1]) / atr
    atr_recent  = [x for x in atr_series[-20:] if x is not None]
    atr_baseline = median(atr_recent) if atr_recent else atr

    bull_trend = e9[-1] > e21[-1] > e50[-1] and e50[-1] > e50[-4] and pdi > mdi
    bear_trend = e9[-1] < e21[-1] < e50[-1] and e50[-1] < e50[-4] and mdi > pdi

    directions = [1 if c["close"] > c["open"] else (-1 if c["close"] < c["open"] else 0) for c in cs]
    pullback_bull = directions[-3] == -1 and directions[-2] in (-1, 0) and directions[-1] == 1
    pullback_bear = directions[-3] == 1  and directions[-2] in (1,  0) and directions[-1] == -1

    # Confirmação: fecha acima dos 60% do range do candle anterior (CALL)
    #              ou abaixo dos 40% do range do candle anterior (PUT)
    # Mais realista para M1 OTC do que exigir rompimento total da máx/mín.
    prev_rng     = max(prev["high"] - prev["low"], 1e-12)
    confirm_bull = last["close"] > last["open"] and last["close"] >= prev["low"] + prev_rng * 0.60
    confirm_bear = last["close"] < last["open"] and last["close"] <= prev["high"] - prev_rng * 0.60

    wick_ok_bull  = upper_wick <= max(body * 1.5,  atr * 0.25)
    wick_ok_bear  = lower_wick <= max(body * 1.5,  atr * 0.25)
    volatility_ok = 0.40 <= (atr / max(atr_baseline, 1e-12)) <= 2.2
    near_ema      = distance_ema9_atr <= 1.50
    adx_ok        = adx >= 14   # OTC frequentemente tem ADX 14–18 mesmo em tendência

    score_call, score_put = 0, 0
    reasons_call, reasons_put = [], []

    if bull_trend: score_call += 30; reasons_call.append("EMA 9>21>50 + inclinação positiva + DI+ dominante")
    if bear_trend: score_put  += 30; reasons_put.append("EMA 9<21<50 + inclinação negativa + DI- dominante")
    if adx_ok:
        if bull_trend: score_call += 15
        if bear_trend: score_put  += 15
    if rsi is not None and 52 <= rsi <= 68: score_call += 15; reasons_call.append("RSI em faixa de momentum comprador (52–68)")
    if rsi is not None and 32 <= rsi <= 48: score_put  += 15; reasons_put.append("RSI em faixa de momentum vendedor (32–48)")
    if pullback_bull: score_call += 15; reasons_call.append("pullback curto seguido de candle comprador")
    if pullback_bear: score_put  += 15; reasons_put.append("pullback curto seguido de candle vendedor")
    if confirm_bull:  score_call += 15; reasons_call.append("fechamento rompeu a máxima do candle anterior")
    if confirm_bear:  score_put  += 15; reasons_put.append("fechamento rompeu a mínima do candle anterior")
    if near_ema:
        if bull_trend: score_call += 10
        if bear_trend: score_put  += 10
    if body_ratio >= 0.45 and wick_ok_bull and last["close"] > last["open"]: score_call += 10
    if body_ratio >= 0.45 and wick_ok_bear and last["close"] < last["open"]: score_put  += 10

    blocks = []
    if not adx_ok:       blocks.append(f"ADX fraco ({adx:.1f} < 14): possível lateralização")
    if not volatility_ok: blocks.append("ATR fora da faixa recente: volatilidade anormal ou fraca")
    if not near_ema:      blocks.append("Preço esticado em relação à EMA 9; evitar perseguir movimento")
    if body_ratio < 0.25: blocks.append("Candle sem corpo direcional suficiente")
    if upper_wick > max(body * 2.0, atr * 0.35) and last["close"] > last["open"]:
        blocks.append("Pavio superior excessivo contra CALL")
    if lower_wick > max(body * 2.0, atr * 0.35) and last["close"] < last["open"]:
        blocks.append("Pavio inferior excessivo contra PUT")

    decision, score = "NO_TRADE", max(score_call, score_put)
    reasons = []
    if (bull_trend and score_call >= confianca_minima and score_call > score_put
            and adx_ok and volatility_ok and near_ema
            and pullback_bull and confirm_bull and wick_ok_bull):
        decision, score, reasons = "CALL", score_call, reasons_call
    elif (bear_trend and score_put >= confianca_minima and score_put > score_call
            and adx_ok and volatility_ok and near_ema
            and pullback_bear and confirm_bear and wick_ok_bear):
        decision, score, reasons = "PUT", score_put, reasons_put
    else:
        reasons = blocks + ["Confluência incompleta: exige tendência, pullback, retomada confirmada e filtros alinhados."]

    return {
        "ok":               True,
        "estrategia":       "OTC Fluxo EMA Pro",
        "decisao":          decision,
        "score":            int(min(100, score)),
        "confianca_minima": int(confianca_minima),
        "motivos":          reasons,
        "bloqueios":        blocks,
        "indicadores": {
            "ema9":               round(e9[-1],  8),
            "ema21":              round(e21[-1], 8),
            "ema50":              round(e50[-1], 8),
            "rsi14":              round(rsi,  2),
            "adx14":              round(adx,  2),
            "di_plus":            round(pdi,  2),
            "di_minus":           round(mdi,  2),
            "atr14":              round(atr,  8),
            "distancia_ema9_atr": round(distance_ema9_atr, 2),
            "corpo_ratio":        round(body_ratio, 2),
            "pullback_call":      pullback_bull,
            "pullback_put":       pullback_bear,
            "confirmacao_call":   confirm_bull,
            "confirmacao_put":    confirm_bear,
        },
        "aviso": "Score mede confluência técnica, não é probabilidade de vitória. OTC pode mudar de regime; validar em DEMO.",
    }
