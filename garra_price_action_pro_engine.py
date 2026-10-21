from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from math import isfinite


@dataclass
class ConfigEstrategia:
    ema_rapida: int = 9
    ema_media: int = 21
    ema_tendencia: int = 200

    rsi_periodo: int = 14
    rsi_compra_min: float = 52.0
    rsi_compra_max: float = 68.0
    rsi_venda_min: float = 32.0
    rsi_venda_max: float = 48.0

    macd_rapido: int = 12
    macd_lento: int = 26
    macd_sinal: int = 9

    volume_periodo: int = 20
    volume_minimo: float = 0.80

    atr_periodo: int = 14
    periodo_suporte_resistencia: int = 30

    pontuacao_minima: int = 7
    pontuacao_diferenca: int = 2
    corpo_minimo: float = 0.12
    permitir_contra_tendencia: bool = False


def _validar_velas(velas: List[Dict[str, Any]]) -> List[Dict[str, float]]:
    resultado = []

    for vela in velas:
        try:
            item = {
                "time": float(vela.get("time", 0)),
                "open": float(vela["open"]),
                "high": float(vela["high"]),
                "low": float(vela["low"]),
                "close": float(vela["close"]),
                "volume": float(vela.get("volume", 0) or 0),
            }
        except (TypeError, ValueError, KeyError):
            continue

        valores = [
            item["open"], item["high"], item["low"],
            item["close"], item["volume"]
        ]

        if not all(isfinite(v) for v in valores):
            continue

        if item["high"] < item["low"]:
            continue

        if item["high"] < max(item["open"], item["close"]):
            continue

        if item["low"] > min(item["open"], item["close"]):
            continue

        resultado.append(item)

    resultado.sort(key=lambda x: x["time"])
    return resultado


def calcular_ema(valores: List[float], periodo: int) -> List[Optional[float]]:
    saida = [None] * len(valores)

    if periodo <= 0 or len(valores) < periodo:
        return saida

    inicial = sum(valores[:periodo]) / periodo
    saida[periodo - 1] = inicial
    multiplicador = 2 / (periodo + 1)
    anterior = inicial

    for i in range(periodo, len(valores)):
        anterior = (
            valores[i] * multiplicador
            + anterior * (1 - multiplicador)
        )
        saida[i] = anterior

    return saida


def calcular_rsi(
    fechamentos: List[float],
    periodo: int = 14
) -> List[Optional[float]]:
    tamanho = len(fechamentos)
    saida = [None] * tamanho

    if tamanho <= periodo or periodo <= 0:
        return saida

    ganhos = []
    perdas = []

    for i in range(1, tamanho):
        diferenca = fechamentos[i] - fechamentos[i - 1]
        ganhos.append(max(diferenca, 0.0))
        perdas.append(max(-diferenca, 0.0))

    media_ganho = sum(ganhos[:periodo]) / periodo
    media_perda = sum(perdas[:periodo]) / periodo

    def valor_rsi(g, p):
        if p == 0 and g == 0:
            return 50.0
        if p == 0:
            return 100.0
        if g == 0:
            return 0.0
        return 100 - (100 / (1 + g / p))

    saida[periodo] = valor_rsi(media_ganho, media_perda)

    for i in range(periodo + 1, tamanho):
        indice = i - 1
        media_ganho = (
            media_ganho * (periodo - 1) + ganhos[indice]
        ) / periodo
        media_perda = (
            media_perda * (periodo - 1) + perdas[indice]
        ) / periodo

        saida[i] = valor_rsi(media_ganho, media_perda)

    return saida


def calcular_macd(
    fechamentos: List[float],
    rapido: int = 12,
    lento: int = 26,
    periodo_sinal: int = 9
) -> Dict[str, List[Optional[float]]]:
    ema_rapida = calcular_ema(fechamentos, rapido)
    ema_lenta = calcular_ema(fechamentos, lento)

    linha_macd = [
        (
            ema_rapida[i] - ema_lenta[i]
            if ema_rapida[i] is not None
            and ema_lenta[i] is not None
            else None
        )
        for i in range(len(fechamentos))
    ]

    indices_validos = [
        i for i, valor in enumerate(linha_macd)
        if valor is not None
    ]

    valores_validos = [linha_macd[i] for i in indices_validos]
    sinais_validos = calcular_ema(valores_validos, periodo_sinal)

    linha_sinal = [None] * len(fechamentos)

    for posicao, indice in enumerate(indices_validos):
        linha_sinal[indice] = sinais_validos[posicao]

    histograma = [
        (
            linha_macd[i] - linha_sinal[i]
            if linha_macd[i] is not None
            and linha_sinal[i] is not None
            else None
        )
        for i in range(len(fechamentos))
    ]

    return {
        "macd": linha_macd,
        "sinal": linha_sinal,
        "histograma": histograma,
    }


def calcular_atr(
    velas: List[Dict[str, float]],
    periodo: int = 14
) -> List[Optional[float]]:
    tamanho = len(velas)
    saida = [None] * tamanho

    if tamanho < periodo or periodo <= 0:
        return saida

    ranges = []

    for i, vela in enumerate(velas):
        if i == 0:
            verdadeiro_range = vela["high"] - vela["low"]
        else:
            fechamento_anterior = velas[i - 1]["close"]
            verdadeiro_range = max(
                vela["high"] - vela["low"],
                abs(vela["high"] - fechamento_anterior),
                abs(vela["low"] - fechamento_anterior),
            )

        ranges.append(verdadeiro_range)

    if tamanho < periodo:
        return saida

    anterior = sum(ranges[:periodo]) / periodo
    saida[periodo - 1] = anterior

    for i in range(periodo, tamanho):
        anterior = (
            anterior * (periodo - 1) + ranges[i]
        ) / periodo
        saida[i] = anterior

    return saida


def analisar_padrao_candle(
    vela: Dict[str, float],
    anterior: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    abertura = vela["open"]
    fechamento = vela["close"]
    maxima = vela["high"]
    minima = vela["low"]

    amplitude = maxima - minima

    if amplitude <= 0:
        return {"padrao": "DOJI", "direcao": 0, "forca": 0.0, "doji": True}

    corpo = abs(fechamento - abertura)
    sombra_superior = maxima - max(abertura, fechamento)
    sombra_inferior = min(abertura, fechamento) - minima
    proporcao_corpo = corpo / amplitude

    if proporcao_corpo <= 0.10:
        return {"padrao": "DOJI", "direcao": 0, "forca": 0.0, "doji": True}

    bullish = fechamento > abertura
    bearish = fechamento < abertura

    if (
        anterior is not None
        and anterior["close"] < anterior["open"]
        and bullish
        and abertura <= anterior["close"]
        and fechamento >= anterior["open"]
    ):
        return {"padrao": "ENGOLFO_ALTISTA", "direcao": 1, "forca": 1.0, "doji": False}

    if (
        anterior is not None
        and anterior["close"] > anterior["open"]
        and bearish
        and abertura >= anterior["close"]
        and fechamento <= anterior["open"]
    ):
        return {"padrao": "ENGOLFO_BAIXISTA", "direcao": -1, "forca": 1.0, "doji": False}

    if (
        sombra_inferior >= corpo * 2
        and sombra_superior <= corpo * 0.8
        and proporcao_corpo <= 0.45
    ):
        return {
            "padrao": "PINBAR_ALTISTA",
            "direcao": 1,
            "forca": min(1.0, sombra_inferior / amplitude),
            "doji": False,
        }

    if (
        sombra_superior >= corpo * 2
        and sombra_inferior <= corpo * 0.8
        and proporcao_corpo <= 0.45
    ):
        return {
            "padrao": "PINBAR_BAIXISTA",
            "direcao": -1,
            "forca": min(1.0, sombra_superior / amplitude),
            "doji": False,
        }

    if bullish and proporcao_corpo >= 0.65:
        return {"padrao": "MARTELO_ALTISTA", "direcao": 1, "forca": proporcao_corpo, "doji": False}

    if bearish and proporcao_corpo >= 0.65:
        return {"padrao": "MARTELO_BAIXISTA", "direcao": -1, "forca": proporcao_corpo, "doji": False}

    return {
        "padrao": "CANDLE_ALTISTA" if bullish else "CANDLE_BAIXISTA",
        "direcao": 1 if bullish else -1,
        "forca": proporcao_corpo,
        "doji": False,
    }


def calcular_suporte_resistencia(
    velas: List[Dict[str, float]],
    periodo: int = 30
) -> Dict[str, Optional[float]]:
    recentes = velas[-periodo:] if velas else []

    if not recentes:
        return {"suporte": None, "resistencia": None}

    return {
        "suporte": min(v["low"] for v in recentes),
        "resistencia": max(v["high"] for v in recentes),
    }


def calcular_volume_relativo(
    volumes: List[float],
    periodo: int = 20
) -> List[Optional[float]]:
    saida = [None] * len(volumes)

    for i in range(len(volumes)):
        inicio = max(0, i - periodo)
        historico = volumes[inicio:i]

        if not historico:
            continue

        media = sum(historico) / len(historico)

        if media > 0:
            saida[i] = volumes[i] / media
        else:
            saida[i] = 0.0

    return saida


def avaliar_garra_price_action_pro(
    dados_velas: List[Dict[str, Any]],
    config: Optional[ConfigEstrategia] = None,
) -> Dict[str, Any]:
    cfg = config or ConfigEstrategia()
    velas = _validar_velas(dados_velas)

    minimo = max(
        cfg.ema_tendencia + 5,
        cfg.macd_lento + cfg.macd_sinal + 5,
        cfg.rsi_periodo + 5,
        cfg.volume_periodo + 5,
        cfg.atr_periodo + 5,
    )

    if len(velas) < minimo:
        return {
            "sinal": "AGUARDAR",
            "motivo": f"Dados insuficientes: necessário pelo menos {minimo} velas.",
            "pontuacao_call": 0,
            "pontuacao_put": 0,
            "confianca": 0.0,
            "indicadores": {},
            "padrao_candle": None,
        }

    fechamentos = [v["close"] for v in velas]
    volumes = [v["volume"] for v in velas]

    ema9   = calcular_ema(fechamentos, cfg.ema_rapida)
    ema21  = calcular_ema(fechamentos, cfg.ema_media)
    ema200 = calcular_ema(fechamentos, cfg.ema_tendencia)
    rsi    = calcular_rsi(fechamentos, cfg.rsi_periodo)

    macd_data = calcular_macd(fechamentos, cfg.macd_rapido, cfg.macd_lento, cfg.macd_sinal)
    atr_data  = calcular_atr(velas, cfg.atr_periodo)
    vol_rel   = calcular_volume_relativo(volumes, cfg.volume_periodo)

    i            = len(velas) - 1
    vela         = velas[i]
    vela_anterior = velas[i - 1]
    candle       = analisar_padrao_candle(vela, vela_anterior)

    valores = {
        "ema9":            ema9[i],
        "ema21":           ema21[i],
        "ema200":          ema200[i],
        "rsi":             rsi[i],
        "macd":            macd_data["macd"][i],
        "macd_sinal":      macd_data["sinal"][i],
        "macd_histograma": macd_data["histograma"][i],
        "atr":             atr_data[i],
        "volume_relativo": vol_rel[i],
    }

    if any(v is None for v in valores.values()):
        return {
            "sinal": "AGUARDAR",
            "motivo": "Indicadores ainda não estão disponíveis.",
            "pontuacao_call": 0,
            "pontuacao_put": 0,
            "confianca": 0.0,
            "indicadores": valores,
            "padrao_candle": candle["padrao"],
        }

    preco          = vela["close"]
    pontuacao_call = 0
    pontuacao_put  = 0
    motivos_call   = []
    motivos_put    = []

    tendencia_alta  = preco > valores["ema200"]
    tendencia_baixa = preco < valores["ema200"]

    if tendencia_alta:
        pontuacao_call += 2
        motivos_call.append("Preço acima da EMA 200")
    if tendencia_baixa:
        pontuacao_put += 2
        motivos_put.append("Preço abaixo da EMA 200")

    if valores["ema9"] > valores["ema21"]:
        pontuacao_call += 2
        motivos_call.append("EMA 9 acima da EMA 21")
    elif valores["ema9"] < valores["ema21"]:
        pontuacao_put += 2
        motivos_put.append("EMA 9 abaixo da EMA 21")

    valor_rsi = valores["rsi"]
    if cfg.rsi_compra_min <= valor_rsi <= cfg.rsi_compra_max:
        pontuacao_call += 1
        motivos_call.append("RSI favorece compra")
    elif cfg.rsi_venda_min <= valor_rsi <= cfg.rsi_venda_max:
        pontuacao_put += 1
        motivos_put.append("RSI favorece venda")

    hist_atual    = macd_data["histograma"][i]
    hist_anterior = macd_data["histograma"][i - 1]

    if valores["macd"] > valores["macd_sinal"] and hist_atual is not None and hist_atual > 0:
        pontuacao_call += 2
        motivos_call.append("MACD altista")
    elif valores["macd"] < valores["macd_sinal"] and hist_atual is not None and hist_atual < 0:
        pontuacao_put += 2
        motivos_put.append("MACD baixista")

    if hist_atual is not None and hist_anterior is not None:
        if hist_atual > hist_anterior:
            pontuacao_call += 1
            motivos_call.append("Histograma MACD crescendo")
        elif hist_atual < hist_anterior:
            pontuacao_put += 1
            motivos_put.append("Histograma MACD diminuindo")

    volume = valores["volume_relativo"]
    if volume is not None and volume >= cfg.volume_minimo:
        if vela["close"] > vela["open"]:
            pontuacao_call += 1
            motivos_call.append("Volume confirma candle altista")
        elif vela["close"] < vela["open"]:
            pontuacao_put += 1
            motivos_put.append("Volume confirma candle baixista")

    if candle["direcao"] > 0:
        pontuacao_call += 2 if candle["forca"] >= 0.55 else 1
        motivos_call.append(candle["padrao"])
    elif candle["direcao"] < 0:
        pontuacao_put += 2 if candle["forca"] >= 0.55 else 1
        motivos_put.append(candle["padrao"])

    sr             = calcular_suporte_resistencia(velas[:-1], cfg.periodo_suporte_resistencia)
    suporte        = sr["suporte"]
    resistencia    = sr["resistencia"]
    amplitude_atr  = valores["atr"]

    dist_suporte    = (preco - suporte)        if suporte     is not None else None
    dist_resistencia = (resistencia - preco)   if resistencia is not None else None

    if dist_suporte    is not None and amplitude_atr > 0 and dist_suporte    <= amplitude_atr * 0.35 and candle["direcao"] > 0:
        pontuacao_call += 1
        motivos_call.append("Reação próxima do suporte")
    if dist_resistencia is not None and amplitude_atr > 0 and dist_resistencia <= amplitude_atr * 0.35 and candle["direcao"] < 0:
        pontuacao_put += 1
        motivos_put.append("Reação próxima da resistência")

    amplitude_candle = vela["high"] - vela["low"]
    corpo_candle     = abs(vela["close"] - vela["open"])
    bloqueios        = []

    if candle["doji"]:
        bloqueios.append("Doji: direção indefinida")

    if amplitude_candle > 0 and (corpo_candle / amplitude_candle) < cfg.corpo_minimo:
        bloqueios.append("Corpo do candle muito pequeno")

    if not cfg.permitir_contra_tendencia and tendencia_alta  and pontuacao_put  > pontuacao_call:
        bloqueios.append("PUT contra a tendência principal")
    if not cfg.permitir_contra_tendencia and tendencia_baixa and pontuacao_call > pontuacao_put:
        bloqueios.append("CALL contra a tendência principal")

    if volume is None or volume < cfg.volume_minimo:
        bloqueios.append("Volume relativo abaixo do mínimo")

    diferenca        = abs(pontuacao_call - pontuacao_put)
    maior_pontuacao  = max(pontuacao_call, pontuacao_put)

    if bloqueios:
        sinal  = "AGUARDAR"
        motivo = "; ".join(bloqueios)
    elif pontuacao_call >= cfg.pontuacao_minima and pontuacao_call > pontuacao_put and diferenca >= cfg.pontuacao_diferenca:
        sinal  = "CALL"
        motivo = "; ".join(motivos_call)
    elif pontuacao_put >= cfg.pontuacao_minima and pontuacao_put > pontuacao_call and diferenca >= cfg.pontuacao_diferenca:
        sinal  = "PUT"
        motivo = "; ".join(motivos_put)
    else:
        sinal  = "AGUARDAR"
        motivo = "Confluência insuficiente entre os indicadores"

    confianca = min(100.0, round((maior_pontuacao / 12) * 100, 2))

    return {
        "sinal":                    sinal,
        "motivo":                   motivo,
        "confianca":                confianca,
        "pontuacao_call":           pontuacao_call,
        "pontuacao_put":            pontuacao_put,
        "timestamp":                vela["time"],
        "preco_entrada_referencia": preco,
        "padrao_candle":            candle["padrao"],
        "indicadores": {
            **valores,
            "suporte":         suporte,
            "resistencia":     resistencia,
            "tendencia":       "ALTA" if tendencia_alta else "BAIXA" if tendencia_baixa else "LATERAL",
            "volume_relativo": volume,
            "atr":             amplitude_atr,
        },
        "motivos_call": motivos_call,
        "motivos_put":  motivos_put,
        "bloqueios":    bloqueios,
    }
