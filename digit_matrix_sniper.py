# -*- coding: utf-8 -*-
"""
DIGIT MATRIX SNIPER PRO
=======================
Módulo independente para integrar ao GARRABOT / Digit Sniper PRO.

Recursos:
- Layout Matrix em tempo real via /digit-matrix
- Análise DIGITUNDER x DIGITOVER
- Frequência por dígito
- Janelas 10/20/30/50/100
- Exaustão / concentração / repetição
- Score 0..100
- Confirmação multi-janela
- Filtro de volatilidade
- Histórico local de sinais/resultados
- Configuração de stake/gerenciamento/stops
- Conta secundária isolada no estado da estratégia
- Endpoint para receber ticks do seu WebSocket atual
- Endpoint opcional para executar ordem através de callback do bot

IMPORTANTE:
Este módulo NÃO promete win rate. O score é um filtro interno.
Teste primeiro em DEMO.
"""

from __future__ import annotations

import math
import os
import json
import time
import threading
from collections import Counter, deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional, Any

try:
    from flask import jsonify, request, render_template_string
except Exception:
    jsonify = request = render_template_string = None


# ============================================================
# CONFIGURAÇÃO
# ============================================================

DEFAULT_CONFIG = {
    "nome": "DIGIT MATRIX SNIPER PRO",
    "ativo": "AUTO",
    "janela_principal": 50,
    "janelas_confirmacao": [10, 20, 50],
    "score_minimo": 70,
    "under_barreira": 6,
    "over_barreira": 3,
    "gatilho_minimo": 3,
    "gatilho_maximo": 8,
    "usar_frequencia": True,
    "usar_exaustao": True,
    "usar_repeticao": True,
    "usar_distribuicao": True,
    "usar_volatilidade": True,
    "usar_historico": True,
    "usar_volume_proxy": True,
    "entrada_usd": 0.35,
    "take_profit_usd": 10.0,
    "stop_loss_usd": 100.0,
    "gerenciamento": "Masaniello",
    "gale_maximo": 2,
    "conta_principal": True,
    "conta_secundaria": False,
    "auto_entrada": False,
    "cooldown_segundos": 3,
    "mercados": [
        "R_10", "R_25", "R_50", "R_75", "R_100",
        "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V"
    ],
}

ALLOWED_UNDER = set(range(1, 8))
ALLOWED_OVER  = set(range(3, 9))


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(v)))

def _safe_int(v, default=0):
    try:   return int(v)
    except: return default

def _safe_float(v, default=0.0):
    try:   return float(v)
    except: return default

def _normalize_digits(values):
    out = []
    for x in values or []:
        try:
            d = int(x)
            if 0 <= d <= 9:
                out.append(d)
        except Exception:
            pass
    return out

def _pct(part, total):
    return (part / total * 100.0) if total else 0.0

def _mean(values):
    return sum(values) / len(values) if values else 0.0

def _stdev(values):
    if len(values) < 2: return 0.0
    m = _mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))

def _entropy(counter, total):
    if total <= 0: return 0.0
    h = 0.0
    for n in counter.values():
        if n:
            p = n / total
            h -= p * math.log(p, 2)
    return _clamp(h / math.log(10, 2) * 100)

def _longest_run(values, predicate):
    best = cur = 0
    for v in values:
        if predicate(v): cur += 1; best = max(best, cur)
        else: cur = 0
    return best

def _same_digit_run(values):
    if not values: return 0
    best = cur = 1
    for i in range(1, len(values)):
        if values[i] == values[i - 1]: cur += 1; best = max(best, cur)
        else: cur = 1
    return best

def _barrier_ok(contract, barrier):
    contract = str(contract).upper()
    b = _safe_int(barrier, -1)
    if contract == "DIGITUNDER": return b in ALLOWED_UNDER
    if contract == "DIGITOVER":  return b in ALLOWED_OVER
    return False

def _favorable(digits, contract, barrier):
    if not digits: return 0
    b = int(barrier)
    if contract == "DIGITUNDER": return sum(d < b for d in digits)
    return sum(d > b for d in digits)

def _target_prob_from_uniform(contract, barrier):
    b = int(barrier)
    if contract == "DIGITUNDER": return b * 10.0
    return (9 - b) * 10.0

def _regime(digits):
    if len(digits) < 10: return "DADOS_INSUFICIENTES"
    c = Counter(digits)
    h = _entropy(c, len(digits))
    repeat = _same_digit_run(digits)
    max_share = max(c.values()) / len(digits)
    if repeat >= 4 or max_share >= 0.30: return "CONCENTRADO"
    if h >= 92: return "DISTRIBUIDO"
    return "MISTO"

def _extract_digits_from_tick_payload(payload):
    if isinstance(payload, (list, tuple, deque)):
        if all(isinstance(x, (int, float, str)) for x in payload):
            return _normalize_digits(payload)
        result = []
        for x in payload:
            if isinstance(x, dict):
                if "digit" in x:      result.extend(_normalize_digits([x["digit"]]))
                elif "last_digit" in x: result.extend(_normalize_digits([x["last_digit"]]))
                elif "quote" in x:
                    s = str(x["quote"])
                    if "." in s: result.extend(_normalize_digits([s.replace(".", "")[-1]]))
        return result
    if isinstance(payload, dict):
        for key in ("digits", "ultimos_digitos", "last_digits"):
            if key in payload: return _normalize_digits(payload[key])
        for key in ("data", "ticks"):
            if key in payload: return _extract_digits_from_tick_payload(payload[key])
        if "digit" in payload:      return _normalize_digits([payload["digit"]])
        if "last_digit" in payload: return _normalize_digits([payload["last_digit"]])
    return []


@dataclass
class Candidate:
    contract: str
    barrier: int
    score: float
    confidence: float
    favorable_pct: float
    base_probability: float
    frequency_score: float
    exhaustion_score: float
    repetition_score: float
    distribution_score: float
    volatility_score: float
    history_score: float
    volume_score: float
    trigger: int
    reason: list

    def to_dict(self):
        return asdict(self)


class DigitMatrixEngine:

    def __init__(self, config=None, state_dir=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)

        self.state_dir    = Path(state_dir or os.path.dirname(os.path.abspath(__file__)))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.state_dir / "digit_matrix_history.json"
        self.config_file  = self.state_dir / "digit_matrix_config.json"

        self._lock            = threading.RLock()
        self.digits_by_asset  = {}
        self.last_analysis    = {}
        self.last_signal      = {}
        self.last_trade_ts    = {}
        self.stats = {"signals": 0, "entries": 0, "wins": 0, "losses": 0, "blocked": 0}

        self._load_config()
        self.history = self._load_history()

    def _load_config(self):
        try:
            if self.config_file.exists():
                saved = json.loads(self.config_file.read_text(encoding="utf-8"))
                if isinstance(saved, dict): self.config.update(saved)
        except Exception: pass

    def save_config(self):
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.config_file)

    def _load_history(self):
        try:
            if self.history_file.exists():
                x = json.loads(self.history_file.read_text(encoding="utf-8"))
                return x if isinstance(x, list) else []
        except Exception: pass
        return []

    def _save_history(self):
        data = self.history[-2000:]
        tmp  = self.history_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.history_file)

    def feed_ticks(self, asset, digits):
        digits = _normalize_digits(digits)
        if not digits: return
        with self._lock:
            q = self.digits_by_asset.setdefault(asset, deque(maxlen=500))
            q.extend(digits)

    def get_digits(self, asset, limit=None):
        with self._lock:
            vals = list(self.digits_by_asset.get(asset, []))
        return vals[-int(limit):] if limit else vals

    # ── filtros ──────────────────────────────────────────────────────────

    def _frequency_score(self, digits, contract, barrier):
        if not digits: return 50.0
        favorable  = _favorable(digits, contract, barrier)
        observed   = _pct(favorable, len(digits))
        theoretical = _target_prob_from_uniform(contract, barrier)
        edge = observed - theoretical
        return _clamp(50 + edge * 2.5)

    def _exhaustion_score(self, digits, contract, barrier):
        if len(digits) < 4: return 50.0
        b    = int(barrier)
        pred = (lambda d: d >= b) if contract == "DIGITUNDER" else (lambda d: d <= b)
        run  = _longest_run(digits, pred)
        if run >= self.config["gatilho_minimo"]:
            return _clamp(55 + (run - self.config["gatilho_minimo"]) * 10)
        return _clamp(45 + run * 3)

    def _repetition_score(self, digits, contract, barrier):
        if not digits: return 50.0
        run  = _same_digit_run(digits)
        last = digits[-1]
        target = (last < barrier if contract == "DIGITUNDER" else last > barrier)
        if run >= 4: return 82.0 if not target else 72.0
        if run >= 2: return 62.0
        return 50.0

    def _distribution_score(self, digits, contract, barrier):
        if len(digits) < 10: return 50.0
        c = Counter(digits)
        entropy   = _entropy(c, len(digits))
        max_share = max(c.values()) / len(digits)
        if max_share >= 0.40: return 42.0
        return _clamp(55 + (100 - entropy) * 0.20)

    def _volatility_score(self, digits):
        if len(digits) < 10: return 60.0
        moves = [abs(digits[i] - digits[i - 1]) for i in range(1, len(digits))]
        avg   = _mean(moves)
        if avg >= 5: return 35.0
        if avg >= 4: return 48.0
        if avg >= 3: return 62.0
        return 78.0

    def _volume_proxy_score(self, digits, contract, barrier):
        if len(digits) < 10: return 50.0
        moves = [abs(digits[i] - digits[i - 1]) for i in range(1, len(digits))]
        avg   = _mean(moves)
        if 1.5 <= avg <= 4.0: return 78.0
        if avg < 1.0:         return 55.0
        return 45.0

    def _history_score(self, asset, contract, barrier):
        rows = [
            x for x in self.history[-500:]
            if x.get("asset") == asset
            and x.get("contract") == contract
            and int(x.get("barrier", -1)) == int(barrier)
            and x.get("result") in ("WIN", "LOSS")
        ]
        if len(rows) < 5: return 55.0
        wins = sum(x["result"] == "WIN" for x in rows)
        return _clamp(wins / len(rows) * 100)

    def _candidate(self, asset, digits, contract, barrier):
        if not _barrier_ok(contract, barrier): return None

        windows = [int(x) for x in self.config.get("janelas_confirmacao", [10, 20, 50]) if int(x) > 0]
        freq_scores = []; exhaust_scores = []; repeat_scores = []
        dist_scores = []; vol_scores    = []; volume_scores = []
        reasons = []

        for w in windows:
            if len(digits) < w: continue
            d = digits[-w:]
            freq_scores.append(self._frequency_score(d, contract, barrier))
            exhaust_scores.append(self._exhaustion_score(d, contract, barrier))
            repeat_scores.append(self._repetition_score(d, contract, barrier))
            dist_scores.append(self._distribution_score(d, contract, barrier))
            vol_scores.append(self._volatility_score(d))
            volume_scores.append(self._volume_proxy_score(d, contract, barrier))

        if not freq_scores: return None

        frequency    = _mean(freq_scores)
        exhaustion   = _mean(exhaust_scores)
        repetition   = _mean(repeat_scores)
        distribution = _mean(dist_scores)
        volatility   = _mean(vol_scores)
        volume       = _mean(volume_scores)

        main           = digits[-min(len(digits), int(self.config["janela_principal"])):]
        favorable_pct  = _pct(_favorable(main, contract, barrier), len(main))
        base_probability = _target_prob_from_uniform(contract, barrier)

        trigger = max(
            _longest_run(main,
                (lambda d: d >= barrier) if contract == "DIGITUNDER" else (lambda d: d <= barrier)),
            _same_digit_run(main)
        )

        history = self._history_score(asset, contract, barrier)

        parts = []
        if self.config.get("usar_frequencia",    True): parts.append((frequency,    0.26))
        if self.config.get("usar_exaustao",       True): parts.append((exhaustion,    0.20))
        if self.config.get("usar_repeticao",      True): parts.append((repetition,    0.12))
        if self.config.get("usar_distribuicao",   True): parts.append((distribution,  0.10))
        if self.config.get("usar_volatilidade",   True): parts.append((volatility,    0.12))
        if self.config.get("usar_historico",      True): parts.append((history,       0.10))
        if self.config.get("usar_volume_proxy",   True): parts.append((volume,        0.10))

        total_weight = sum(w for _, w in parts) or 1
        score = sum(v * w for v, w in parts) / total_weight

        spread = max([frequency, exhaustion, repetition, distribution, volatility, history, volume]) - \
                 min([frequency, exhaustion, repetition, distribution, volatility, history, volume])
        if spread > 50: score -= 8

        if 4 <= barrier <= 6 and trigger < int(self.config["gatilho_minimo"]):
            score -= 12
            reasons.append(f"gatilho insuficiente ({trigger} < {self.config['gatilho_minimo']})")
        elif trigger >= int(self.config["gatilho_minimo"]):
            reasons.append(f"gatilho confirmado: {trigger}")

        if volatility < 35:
            score -= 10
            reasons.append("volatilidade alta")

        if favorable_pct > base_probability + 8:
            reasons.append(f"frequência favorável acima do teórico ({favorable_pct:.1f}% vs {base_probability:.1f}%)")
        elif favorable_pct < base_probability - 8:
            reasons.append(f"frequência favorável abaixo do teórico ({favorable_pct:.1f}% vs {base_probability:.1f}%)")

        if history >= 65:  reasons.append(f"histórico favorável: {history:.1f}%")
        elif history <= 45: reasons.append(f"histórico fraco: {history:.1f}%")

        score = _clamp(score)

        return Candidate(
            contract=contract, barrier=int(barrier),
            score=round(score, 2), confidence=round(score, 2),
            favorable_pct=round(favorable_pct, 2), base_probability=round(base_probability, 2),
            frequency_score=round(frequency, 2), exhaustion_score=round(exhaustion, 2),
            repetition_score=round(repetition, 2), distribution_score=round(distribution, 2),
            volatility_score=round(volatility, 2), history_score=round(history, 2),
            volume_score=round(volume, 2), trigger=int(trigger), reason=reasons,
        )

    # ── análise principal ────────────────────────────────────────────────

    def analyze(self, asset, digits=None):
        if digits is None: digits = self.get_digits(asset)
        digits = _normalize_digits(digits)

        if len(digits) < 10:
            return {"ok": False, "status": "APRENDENDO", "asset": asset,
                    "message": f"Ticks insuficientes: {len(digits)}/10", "digits": digits}

        under = self._candidate(asset, digits, "DIGITUNDER", int(self.config["under_barreira"]))
        over  = self._candidate(asset, digits, "DIGITOVER",  int(self.config["over_barreira"]))

        candidates = sorted([x for x in (under, over) if x is not None], key=lambda x: x.score, reverse=True)
        best   = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None

        threshold = float(self.config["score_minimo"])
        status = "BLOQUEADO"; signal = None
        reason = "Nenhuma oportunidade válida."

        if best:
            margin = best.score - (second.score if second else 0)
            if best.score >= threshold:
                if second and abs(margin) < 5:
                    reason = "UNDER/OVER muito próximos — sem vantagem suficiente."
                else:
                    status = "SINAL"; signal = best.to_dict()
                    reason = f"{best.contract} {best.barrier} com score {best.score:.1f}/100."
            else:
                reason = f"Melhor score {best.score:.1f} abaixo do mínimo {threshold:.1f}."

        result = {
            "ok": True, "status": status, "asset": asset,
            "timestamp": time.time(), "digits": digits[-100:],
            "matrix": self.matrix(asset, digits),
            "regime": _regime(digits[-50:]),
            "under": under.to_dict() if under else None,
            "over":  over.to_dict()  if over  else None,
            "best": signal, "reason": reason,
            "config": self.public_config(), "stats": dict(self.stats),
        }

        with self._lock:
            self.last_analysis[asset] = result
            if signal:
                self.last_signal[asset] = signal
                self.stats["signals"] += 1
            else:
                self.stats["blocked"] += 1

        return result

    def matrix(self, asset, digits=None):
        digits  = _normalize_digits(digits if digits is not None else self.get_digits(asset))
        total   = len(digits); last20 = digits[-20:]; last10 = digits[-10:]
        rows = []
        for d in range(10):
            total_count = digits.count(d); count20 = last20.count(d); count10 = last10.count(d)
            rows.append({
                "digit": d, "frequency": round(_pct(total_count, total), 2),
                "count": total_count, "last20": count20, "last10": count10,
                "last_seen": (len(digits) - 1 - max(
                    (i for i, x in enumerate(reversed(digits)) if x == d), default=len(digits)
                )) if d in digits else None,
                "recent": ("HOT" if count10 >= 3 else "COLD" if count10 == 0 else "NORMAL"),
            })
        return rows

    def public_config(self):
        return dict(self.config)

    def register_result(self, asset, contract, barrier, result, metadata=None):
        result = str(result).upper()
        if result not in ("WIN", "LOSS"): raise ValueError("result deve ser WIN ou LOSS")
        row = {"ts": time.time(), "asset": asset, "contract": str(contract).upper(),
               "barrier": int(barrier), "result": result}
        if metadata: row["metadata"] = metadata
        with self._lock:
            self.history.append(row)
            if result == "WIN": self.stats["wins"] += 1
            else:               self.stats["losses"] += 1
            self._save_history()

    def can_enter(self, asset):
        return (time.time() - self.last_trade_ts.get(asset, 0)) >= float(self.config["cooldown_segundos"])

    def mark_entry(self, asset):
        self.last_trade_ts[asset] = time.time()
        self.stats["entries"] += 1


# ============================================================
# UI MATRIX
# ============================================================

MATRIX_HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DIGIT MATRIX SNIPER PRO</title>
<style>
:root{
 --bg:#020605;--panel:#06110d;--line:#0b4b35;
 --green:#00ff88;--green2:#00b86b;--text:#b7ffd9;
 --red:#ff4f6d;--yellow:#ffd166;--cyan:#39e6ff;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at top,#072016 0,#020605 48%,#000 100%);
 color:var(--text);font-family:Consolas,Monaco,monospace;min-height:100vh}
.header{padding:16px 20px;border-bottom:1px solid var(--line);
 display:flex;justify-content:space-between;gap:15px;align-items:center;
 background:#020806cc;position:sticky;top:0;z-index:10;backdrop-filter:blur(8px)}
.title{font-size:20px;font-weight:900;letter-spacing:2px;color:var(--green)}
.badge{padding:6px 10px;border:1px solid var(--line);border-radius:5px}
.wrap{padding:15px;max-width:1500px;margin:auto}
.grid{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:12px}
.panel{border:1px solid var(--line);background:linear-gradient(180deg,#06130e,#020806);
 box-shadow:0 0 20px #00ff8810;min-width:0}
.panel h3{margin:0;padding:10px 12px;border-bottom:1px solid var(--line);
 font-size:12px;color:var(--green);letter-spacing:1px}
.pad{padding:12px}
.digits{display:grid;grid-template-columns:repeat(10,1fr);gap:5px;margin-top:10px}
.digit{position:relative;text-align:center;padding:13px 2px;border:1px solid #0a4935;
 background:#020a07;color:#69ffb1;font-size:22px;font-weight:900;transition:.18s;overflow:hidden}
.digit.flash{transform:scale(1.08);box-shadow:0 0 20px #00ff8899}
.bar{height:8px;background:#03150e;border:1px solid #0a4935;margin:7px 0 13px;overflow:hidden}
.fill{height:100%;transition:.35s;background:var(--green)}
.metric{display:flex;justify-content:space-between;border-bottom:1px dotted #0b4935;
 padding:7px 0;font-size:12px}
.metric b{color:#fff}
.signal{margin-top:10px;padding:16px;text-align:center;border:1px solid var(--green);
 background:#00ff8810;box-shadow:0 0 28px #00ff8820}
.signal.blocked{border-color:#56303a;background:#ff4f6d08}
.signal .big{font-size:26px;color:var(--green);font-weight:900}
.signal.blocked .big{color:var(--red)}
.score{font-size:38px;font-weight:900;margin:8px 0}
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{border-bottom:1px solid #0a392b;padding:7px;text-align:center}
th{color:#54d99b}
.hot{color:#ff5e75}.cold{color:#39e6ff}.normal{color:#b7ffd9}
.controls{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
input,select,button{width:100%;background:#020806;color:#b7ffd9;border:1px solid #0b4b35;padding:8px;font:inherit}
button{cursor:pointer;color:#00ff88;font-weight:bold}
pre{margin:0;max-height:260px;overflow:auto;white-space:pre-wrap;color:#7bffba;font-size:10px}
@media(max-width:1050px){.grid{grid-template-columns:1fr 1fr}.controls{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.grid{grid-template-columns:1fr}.digits{grid-template-columns:repeat(5,1fr)}}
</style>
</head>
<body>
<div class="header">
 <div class="title">🧬 DIGIT MATRIX SNIPER PRO</div>
 <div class="badge" id="status">CONECTANDO...</div>
</div>
<div class="wrap">
 <div class="panel" style="margin-bottom:12px">
  <h3>⚙ CONFIGURAÇÃO</h3>
  <div class="pad controls">
   <select id="asset">
    <option>AUTO</option><option>R_10</option><option>R_25</option><option>R_50</option>
    <option>R_75</option><option>R_100</option><option>1HZ10V</option><option>1HZ25V</option>
    <option>1HZ50V</option><option>1HZ75V</option><option>1HZ100V</option>
   </select>
   <input id="score" type="number" min="50" max="100" value="82" placeholder="Score mínimo">
   <input id="under" type="number" min="1" max="7" value="6" placeholder="Under barreira">
   <input id="over"  type="number" min="3" max="8" value="3" placeholder="Over barreira">
  </div>
 </div>
 <div class="grid">
  <div class="panel"><h3>🔢 STREAM / MATRIX</h3><div class="pad">
   <div id="stream" style="font-size:12px;color:#58d99a;min-height:18px"></div>
   <div class="digits" id="digits"></div>
  </div></div>
  <div class="panel"><h3>🎯 UNDER × OVER</h3><div class="pad">
   <div class="metric"><span>DIGITUNDER</span><b id="us">--</b></div>
   <div class="bar"><div class="fill" id="ub" style="width:0"></div></div>
   <div class="metric"><span>DIGITOVER</span><b id="os">--</b></div>
   <div class="bar"><div class="fill" id="ob" style="width:0"></div></div>
   <div class="signal blocked" id="signal">
    <div class="big" id="decision">AGUARDANDO</div>
    <div class="score" id="confidence">--</div>
    <div id="reason">Coletando dados...</div>
   </div>
  </div></div>
  <div class="panel"><h3>🧠 FILTROS MATRIX</h3><div class="pad" id="filters"></div></div>
  <div class="panel" style="grid-column:1/-1"><h3>📊 MAPA DOS 10 DÍGITOS</h3><div class="pad">
   <table><thead><tr><th>Dígito</th><th>Freq.</th><th>Total</th><th>10T</th><th>20T</th><th>Estado</th></tr></thead>
   <tbody id="matrix"></tbody></table>
  </div></div>
  <div class="panel"><h3>📋 LOG MATRIX</h3><div class="pad"><pre id="log"></pre></div></div>
  <div class="panel"><h3>📈 ESTATÍSTICAS</h3><div class="pad" id="stats"></div></div>
  <div class="panel"><h3>ℹ️ REGRAS</h3><div class="pad" style="font-size:11px;line-height:1.7">
   Score é filtro interno, não garantia.<br>Over 0/1/2 bloqueado.<br>Under 8/9 bloqueado.<br>
   Under/Over central exige gatilho.<br>Sem confirmação suficiente = sem entrada.<br>
   Teste em DEMO antes de usar conta real.
  </div></div>
 </div>
</div>
<script>
let logs=[];
function addLog(s){logs.unshift(new Date().toLocaleTimeString()+"  "+s);logs=logs.slice(0,80);document.getElementById("log").textContent=logs.join("\n")}
function render(r){
 const d=r.digits||[];
 document.getElementById("status").textContent=(r.status||"—")+" | "+(r.asset||"—");
 document.getElementById("stream").textContent=d.slice(-30).join(" ");
 document.getElementById("digits").innerHTML=(r.matrix||[]).map(x=>{
  const c=x.recent==="HOT"?"hot":x.recent==="COLD"?"cold":"normal";
  return `<div class="digit ${c}">${x.digit}<small>${x.frequency}%</small></div>`
 }).join("");
 const u=r.under,o=r.over;
 document.getElementById("us").textContent=u?`${u.score}/100`:"—";
 document.getElementById("os").textContent=o?`${o.score}/100`:"—";
 document.getElementById("ub").style.width=(u?u.score:0)+"%";
 document.getElementById("ob").style.width=(o?o.score:0)+"%";
 const s=document.getElementById("signal"),best=r.best;
 if(best){
  s.classList.remove("blocked");
  document.getElementById("decision").textContent=best.contract+" "+best.barrier;
  document.getElementById("confidence").textContent=best.score+"/100";
  document.getElementById("reason").textContent=(r.reason||"")+" | gatilho "+best.trigger;
 }else{
  s.classList.add("blocked");
  document.getElementById("decision").textContent="SEM ENTRADA";
  document.getElementById("confidence").textContent=(r.under?.score??r.over?.score??"--")+"/100";
  document.getElementById("reason").textContent=r.reason||"Aguardando";
 }
 const b=best||u||o;
 document.getElementById("filters").innerHTML=b?[
  ["Frequência",b.frequency_score],["Exaustão",b.exhaustion_score],
  ["Repetição",b.repetition_score],["Distribuição",b.distribution_score],
  ["Volatilidade",b.volatility_score],["Histórico",b.history_score],["Atividade",b.volume_score]
 ].map(a=>`<div class="metric"><span>${a[0]}</span><b>${Number(a[1]).toFixed(1)}</b></div>`).join(""):"<div>Coletando...</div>";
 document.getElementById("matrix").innerHTML=(r.matrix||[]).map(x=>{
  const c=x.recent==="HOT"?"hot":x.recent==="COLD"?"cold":"normal";
  return `<tr><td>${x.digit}</td><td>${x.frequency}%</td><td>${x.count}</td><td>${x.last10}</td><td>${x.last20}</td><td class="${c}">${x.recent}</td></tr>`
 }).join("");
 const st=r.stats||{};
 document.getElementById("stats").innerHTML=[
  ["Sinais",st.signals||0],["Entradas",st.entries||0],["Wins",st.wins||0],
  ["Losses",st.losses||0],["Bloqueios",st.blocked||0],["Regime",r.regime||"—"]
 ].map(a=>`<div class="metric"><span>${a[0]}</span><b>${a[1]}</b></div>`).join("");
 if(best) addLog("🎯 "+best.contract+" "+best.barrier+" | score "+best.score);
 else addLog("🚫 BLOQUEADO | "+(r.reason||""));
}
async function update(){
 try{
  const r=await fetch("/api/digit-matrix/state?asset="+encodeURIComponent(document.getElementById("asset").value));
  const data=await r.json();
  if(data.ok) render(data.analysis); else addLog("⚠ "+(data.error||"sem resposta"));
 }catch(e){addLog("❌ "+e.message)}
}
async function save(){
 try{
  const p={score_minimo:Number(document.getElementById("score").value),under_barreira:Number(document.getElementById("under").value),over_barreira:Number(document.getElementById("over").value)};
  const r=await fetch("/api/digit-matrix/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)});
  const x=await r.json(); addLog(x.ok?"⚙ configuração salva":"❌ "+x.error);
 }catch(e){addLog("❌ "+e.message)}
}
["score","under","over"].forEach(id=>document.getElementById(id).addEventListener("change",save));
document.getElementById("asset").addEventListener("change",update);
setInterval(update,800);
update();
</script>
</body>
</html>
"""


# ============================================================
# REGISTRO NO FLASK
# ============================================================

def register_digit_matrix(
    app,
    buscar_ticks_ws_sync: Optional[Callable[..., Any]] = None,
    executar_ordem: Optional[Callable[[dict], Any]] = None,
    config: Optional[dict] = None,
    state_dir: Optional[str] = None,
):
    """
    Integra o Digit Matrix Sniper PRO ao Flask existente.

    Uso no main.py:
        from digit_matrix_sniper import register_digit_matrix
        register_digit_matrix(app, _buscar_ticks_ws_sync)
    """
    engine = DigitMatrixEngine(config=config, state_dir=state_dir)
    app.extensions["digit_matrix_engine"] = engine

    @app.route("/digit-matrix", methods=["GET"])
    def digit_matrix_page():
        return render_template_string(MATRIX_HTML)

    @app.route("/api/digit-matrix/config", methods=["GET", "POST"])
    def digit_matrix_config():
        if request.method == "GET":
            return jsonify({"ok": True, "config": engine.public_config()})
        data = request.get_json(silent=True) or {}
        if "score_minimo" in data:
            data["score_minimo"] = int(_clamp(_safe_float(data["score_minimo"], 70), 50, 100))
        if "under_barreira" in data:
            b = _safe_int(data["under_barreira"], 6)
            if b not in ALLOWED_UNDER:
                return jsonify({"ok": False, "error": "Under inválido. Use 1..7."}), 400
            data["under_barreira"] = b
        if "over_barreira" in data:
            b = _safe_int(data["over_barreira"], 3)
            if b not in ALLOWED_OVER:
                return jsonify({"ok": False, "error": "Over inválido. Use 3..8."}), 400
            data["over_barreira"] = b
        # Campos extras (ativo, intervalo) — salva diretamente sem validação rígida
        if "ativo" in data and isinstance(data["ativo"], str):
            engine.config["ativo"] = data["ativo"].strip() or "R_100"
        if "intervalo_ms" in data:
            engine.config["intervalo_ms"] = max(500, int(_safe_int(data["intervalo_ms"], 1500)))
        allowed = set(DEFAULT_CONFIG) | {"ativo", "intervalo_ms"}
        for k, v in data.items():
            if k in allowed: engine.config[k] = v
        engine.save_config()
        return jsonify({"ok": True, "config": engine.public_config()})

    @app.route("/api/digit-matrix/ticks", methods=["POST"])
    def digit_matrix_ticks():
        data   = request.get_json(silent=True) or {}
        asset  = str(data.get("asset") or "").strip()
        if not asset:
            return jsonify({"ok": False, "error": "asset obrigatório"}), 400
        digits = _extract_digits_from_tick_payload(data)
        if not digits:
            return jsonify({"ok": False, "error": "nenhum dígito válido"}), 400
        engine.feed_ticks(asset, digits)
        return jsonify({"ok": True, "analysis": engine.analyze(asset)})

    @app.route("/api/digit-matrix/state", methods=["GET"])
    def digit_matrix_state():
        requested = request.args.get("asset", "AUTO").upper()
        asset = requested
        if asset == "AUTO":
            counts = {a: len(v) for a, v in engine.digits_by_asset.items()}
            asset  = max(counts, key=counts.get) if counts else engine.config.get("mercados", ["R_100"])[0]

        digits = engine.get_digits(asset)
        if len(digits) < 10 and buscar_ticks_ws_sync:
            try:
                try:    payload = buscar_ticks_ws_sync(asset, 100)
                except TypeError: payload = buscar_ticks_ws_sync(asset)
                incoming = _extract_digits_from_tick_payload(payload)
                if incoming:
                    engine.feed_ticks(asset, incoming)
                    digits = engine.get_digits(asset)
            except Exception:
                pass

        return jsonify({"ok": True, "analysis": engine.analyze(asset, digits)})

    @app.route("/api/digit-matrix/analisar", methods=["POST"])
    def digit_matrix_analisar():
        data   = request.get_json(silent=True) or {}
        asset  = str(data.get("asset") or engine.config.get("ativo") or "R_100")
        digits = _extract_digits_from_tick_payload(data)
        if digits: engine.feed_ticks(asset, digits)
        return jsonify({"ok": True, "analysis": engine.analyze(asset)})

    @app.route("/api/digit-matrix/sinal", methods=["POST"])
    def digit_matrix_signal():
        data   = request.get_json(silent=True) or {}
        asset  = str(data.get("asset") or "R_100")
        digits = _extract_digits_from_tick_payload(data)
        if digits: engine.feed_ticks(asset, digits)
        analysis = engine.analyze(asset)

        if analysis.get("status") != "SINAL":
            return jsonify({"ok": True, "executed": False, "analysis": analysis})

        best = analysis["best"]
        if not engine.can_enter(asset):
            return jsonify({"ok": True, "executed": False, "reason": "cooldown", "analysis": analysis})

        order = {
            "asset": asset, "contract_type": best["contract"], "barrier": best["barrier"],
            "amount": float(engine.config["entrada_usd"]), "duration": 1, "duration_unit": "t",
            "strategy": engine.config["nome"], "score": best["score"], "trigger": best["trigger"],
        }

        if not engine.config.get("auto_entrada", False):
            return jsonify({"ok": True, "executed": False, "reason": "auto_entrada_desativada",
                            "order": order, "analysis": analysis})

        if executar_ordem is None:
            return jsonify({"ok": True, "executed": False, "reason": "executar_ordem_nao_configurado",
                            "order": order, "analysis": analysis})

        try:
            engine.mark_entry(asset)
            result = executar_ordem(order)
            return jsonify({"ok": True, "executed": True, "order": order, "result": result, "analysis": analysis})
        except Exception as exc:
            return jsonify({"ok": False, "executed": False, "error": str(exc), "order": order}), 500

    @app.route("/api/digit-matrix/result", methods=["POST"])
    def digit_matrix_result():
        data     = request.get_json(silent=True) or {}
        asset    = str(data.get("asset") or "")
        contract = str(data.get("contract_type") or "")
        barrier  = _safe_int(data.get("barrier"), -1)
        result   = str(data.get("result") or "").upper()
        if not asset or not contract or barrier < 0:
            return jsonify({"ok": False, "error": "asset, contract_type e barrier são obrigatórios"}), 400
        if result not in ("WIN", "LOSS"):
            return jsonify({"ok": False, "error": "result deve ser WIN ou LOSS"}), 400
        engine.register_result(asset, contract, barrier, result, metadata=data.get("metadata"))
        return jsonify({"ok": True, "stats": dict(engine.stats)})

    @app.route("/api/digit-matrix/reset", methods=["POST"])
    def digit_matrix_reset():
        with engine._lock:
            engine.digits_by_asset.clear()
            engine.last_analysis.clear()
            engine.last_signal.clear()
        return jsonify({"ok": True})

    print("✅ DIGIT MATRIX SNIPER PRO registrado.")
    print("🌐 Interface: /digit-matrix")
    print("📡 API ticks: /api/digit-matrix/ticks")
    print("🎯 API sinal: /api/digit-matrix/sinal")

    return engine


# Alias
register = register_digit_matrix
