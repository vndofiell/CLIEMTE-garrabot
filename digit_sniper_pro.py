# digit_sniper_pro.py
# Módulo para integrar ao GarraBot (Flask).
#
# No seu arquivo principal:
#   from digit_sniper_pro import register_digit_sniper
#   register_digit_sniper(app, _buscar_ticks_ws_sync)
#
# Abra: http://127.0.0.1:5000/digit-sniper
#
# A tela é ANALÍTICA/PAPER por padrão. Ela não dispara uma compra real.

from __future__ import annotations

import math
import time
from collections import Counter
from typing import Callable, Any

from flask import jsonify, request

DEFAULT_ASSETS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

MIN_SCORE = 82
WINDOW = 50
TRIGGER_MIN = 4
TRIGGER_MAX = 6
UNDER_BARRIERS = range(3, 8)
OVER_BARRIERS = range(3, 7)


def _last_digit(value: Any) -> int | None:
    """Extrai o último dígito significativo da parte decimal do preço."""
    try:
        # Converte para string preservando a precisão original
        s = str(value).strip()
        # Se já é string de preço (ex: "1234.56"), usa direto
        # Se é float, converte sem zero-padding extra
        if isinstance(value, float):
            # Usa repr para evitar zeros extras: 1234.5 → "1234.5", não "1234.50000"
            s = repr(value)
        # Remove notação científica se houver
        if 'e' in s or 'E' in s:
            s = f"{float(s):.10f}".rstrip('0')
        # Pega o último dígito da parte decimal (ou do inteiro se sem ponto)
        if '.' in s:
            decimal = s.split('.')[-1].rstrip('0') or '0'
            return int(decimal[-1])
        else:
            return int(s[-1])
    except Exception:
        return None


def _digits_from_ticks(ticks: list[Any]) -> list[int]:
    out = []
    for tick in ticks:
        d = _last_digit(tick)
        if d is not None and 0 <= d <= 9:
            out.append(d)
    return out


def _entropy(digits: list[int]) -> float:
    if not digits:
        return 0.0
    c = Counter(digits)
    n = len(digits)
    h = -sum((v / n) * math.log2(v / n) for v in c.values())
    return h / math.log2(10)


def _streak_same_side(digits: list[int], tipo: str, barrier: int) -> int:
    """Conta a sequência recente CONTRÁRIA ao lado apostado."""
    if not digits:
        return 0

    def favorable(d: int) -> bool:
        return d < barrier if tipo == "DIGITUNDER" else d > barrier

    count = 0
    for d in reversed(digits):
        if favorable(d):
            break
        count += 1
    return count


def _side_rate(digits: list[int], tipo: str, barrier: int) -> float:
    if not digits:
        return 0.0
    if tipo == "DIGITUNDER":
        return sum(d < barrier for d in digits) / len(digits)
    return sum(d > barrier for d in digits) / len(digits)


def _score_candidate(
    digits: list[int],
    tipo: str,
    barrier: int,
    payout: float,
    min_trigger: int = TRIGGER_MIN,
) -> dict:
    if len(digits) < 20:
        return {
            "score": 0, "tipo": tipo, "barreira": barrier, "gatilho": 0,
            "taxa20": 0, "taxa50": 0, "payout": payout,
            "status": "NÃO OPERAR", "entrada": False,
            "motivo": "Aguardando pelo menos 20 ticks",
        }

    w20 = digits[-20:]
    w50 = digits[-50:]
    rate20 = _side_rate(w20, tipo, barrier)
    rate50 = _side_rate(w50, tipo, barrier)
    trigger = _streak_same_side(w20, tipo, barrier)
    ent = _entropy(w50)

    # Frequência 30%, consistência 20%, gatilho 25%,
    # payout 15%, estabilidade 10%.
    freq_score = min(100.0, rate20 * 100.0)
    consistency = 100.0 - min(100.0, abs(rate20 - rate50) * 250.0)

    if min_trigger <= trigger <= TRIGGER_MAX:
        trigger_score = 100.0
    elif trigger == min_trigger - 1:
        trigger_score = 65.0
    elif trigger > TRIGGER_MAX:
        trigger_score = 35.0
    else:
        trigger_score = max(0.0, trigger * 18.0)

    payout_score = max(0.0, min(100.0, ((payout - 0.70) / 0.30) * 100.0))
    stability_score = max(
        0.0, min(100.0, (1.0 - abs(ent - 0.82) / 0.82) * 100.0)
    )

    score = (
        freq_score * 0.30
        + consistency * 0.20
        + trigger_score * 0.25
        + payout_score * 0.15
        + stability_score * 0.10
    )

    reasons = []
    if payout < 0.70:
        reasons.append("payout abaixo do mínimo")
    if trigger < min_trigger:
        reasons.append(f"gatilho {trigger}/{min_trigger}")
    if trigger > TRIGGER_MAX:
        reasons.append("gatilho excessivo")
    if rate20 < 0.55:
        reasons.append("frequência observada insuficiente")
    if abs(rate20 - rate50) > 0.15:
        reasons.append("janelas divergentes")

    entrada = score >= MIN_SCORE and not reasons

    if entrada:
        status = "ENTRADA LIBERADA"
    elif score >= 70:
        status = "AGUARDANDO CONFIRMAÇÃO"
    else:
        status = "NÃO OPERAR"

    return {
        "score": round(score, 1),
        "tipo": tipo,
        "barreira": barrier,
        "gatilho": trigger,
        "taxa20": round(rate20 * 100, 1),
        "taxa50": round(rate50 * 100, 1),
        "payout": round(payout, 4),
        "entropia": round(ent, 3),
        "status": status,
        "entrada": entrada,
        "motivo": "; ".join(reasons) if reasons else "Todos os filtros aprovados",
    }


def analyze_digits(
    digits: list[int],
    payout: float,
    min_score: int = MIN_SCORE,
) -> dict:
    digits = [int(x) for x in digits if 0 <= int(x) <= 9][-WINDOW:]

    candidates = []
    for b in UNDER_BARRIERS:
        candidates.append(_score_candidate(digits, "DIGITUNDER", b, payout))
    for b in OVER_BARRIERS:
        candidates.append(_score_candidate(digits, "DIGITOVER", b, payout))

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0] if candidates else None
    counts = Counter(digits)

    return {
        "ok": True,
        "digitos": digits,
        "frequencias": {str(i): counts.get(i, 0) for i in range(10)},
        "melhor": best or {
            "score": 0,
            "status": "NÃO OPERAR",
            "motivo": "Dados insuficientes",
        },
        "ranking": candidates[:8],
        "janela": len(digits),
        "timestamp": time.strftime("%H:%M:%S"),
    }


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GarraBot — Dígitos Sniper PRO</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#071019;color:#eaf2f8;font-family:Arial,sans-serif}
.wrap{max-width:1250px;margin:auto;padding:18px}
.top{display:flex;justify-content:space-between;gap:12px;align-items:center}
h1{margin:0;font-size:25px}.sub{color:#8ea5b7;margin-top:5px}
.card{background:#0d1823;border:1px solid #203243;border-radius:14px;padding:15px;margin-top:14px;box-shadow:0 8px 25px #0005}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:end}
label{font-size:12px;color:#9db0c0;display:block;margin-bottom:5px}
select,input,button{height:40px;border-radius:9px;border:1px solid #304658;background:#101f2c;color:#fff;padding:0 12px}
button{cursor:pointer;font-weight:bold}.primary{background:#0b7d53;border-color:#159b6c}
.grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px}
.score{font-size:52px;font-weight:800;text-align:center}
.status{text-align:center;font-size:18px;font-weight:bold;padding:10px;border-radius:10px;background:#162635}
.green{color:#38e39c}.yellow{color:#ffd166}.red{color:#ff6474}
.digits{display:flex;gap:5px;flex-wrap:wrap}
.d{width:34px;height:34px;border-radius:8px;display:grid;place-items:center;background:#162737;font-weight:bold}
.bars{display:grid;grid-template-columns:repeat(10,1fr);gap:6px;align-items:end;height:130px}
.bar{background:#24516b;border-radius:5px 5px 0 0;min-height:3px;position:relative}
.bar span{position:absolute;bottom:-19px;left:0;width:100%;text-align:center;font-size:11px}
table{width:100%;border-collapse:collapse}
td,th{padding:8px;border-bottom:1px solid #1d2d3a;text-align:left;font-size:13px}
.badge{padding:4px 8px;border-radius:8px;background:#162635}
.live{color:#38e39c}.muted{color:#8499aa}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
<div class="top">
<div>
<h1>🎯 GarraBot — DÍGITOS SNIPER PRO</h1>
<div class="sub">Scanner estatístico + exaustão + payout + filtro de risco</div>
</div>
<div class="badge live">● MONITORANDO</div>
</div>
<div class="card">
<div class="controls">
<div>
<label>ATIVO</label>
<select id="asset">
<option>R_10</option>
<option>R_25</option>
<option>R_50</option>
<option>R_75</option>
<option selected>R_100</option>
<option>1HZ10V</option>
<option>1HZ25V</option>
<option>1HZ50V</option>
<option>1HZ75V</option>
<option>1HZ100V</option>
</select>
</div>
<div>
<label>PAYOUT (ex.: 0.85)</label>
<input id="payout" type="number" step="0.01" value="0.85" min="0" max="1">
</div>
<div>
<label>ATUALIZAÇÃO</label>
<select id="interval">
<option value="1500">1,5s</option>
<option value="3000" selected>3s</option>
<option value="5000">5s</option>
</select>
</div>
<button class="primary" onclick="scan()">🔎 ANALISAR AGORA</button>
<button onclick="toggleAuto()" id="autoBtn">▶ AUTO</button>
</div>
</div>
<div class="grid">
<div class="card">
<h3>Últimos dígitos</h3>
<div id="digits" class="digits">
</div>
<h3>Distribuição</h3>
<div id="bars" class="bars">
</div>
</div>
<div class="card">
<h3>Score</h3>
<div id="score" class="score">—</div>
<div id="status" class="status">Aguardando dados</div>
<p id="reason" class="muted">
</p>
</div>
<div class="card">
<h3>Sinal</h3>
<div id="signal" style="font-size:27px;font-weight:800">—</div>
<p>Barreira: <b id="barrier">—</b>
</p>
<p>Gatilho: <b id="trigger">—</b>
</p>
<p>Taxa 20: <b id="rate20">—</b>
</p>
<p>Taxa 50: <b id="rate50">—</b>
</p>
<p>Payout: <b id="pay">—</b>
</p>
</div>
</div>
<div class="card">
<h3>Ranking de candidatos</h3>
<table>
<thead>
<tr>
<th>Tipo</th>
<th>Barreira</th>
<th>Score</th>
<th>Gatilho</th>
<th>20 ticks</th>
<th>50 ticks</th>
<th>Status</th>
</tr>
</thead>
<tbody id="rank">
</tbody>
</table>
</div>
</div>
<script>
let timer=null;
async function scan(){
  const asset=document.getElementById('asset').value;
  const payout=parseFloat(document.getElementById('payout').value)||0;
  try{
    const r=await fetch('/digit-sniper/analisar',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({asset,payout})
    });
    const j=await r.json();
    if(!r.ok) throw new Error(j.erro||'Falha na análise');
    render(j);
  }catch(e){
    document.getElementById('reason').textContent='Erro: '+e;
  }
}
function render(j){
 const ds=j.digitos||[], best=j.melhor||{};
 document.getElementById('digits').innerHTML=ds.map(
   x=>`<div class="d">${x}</div>`).join('');
 const f=j.frequencias||{};
 const mx=Math.max(1,...Object.values(f));
 document.getElementById('bars').innerHTML=Array.from({length:10},(_,i)=>
   `<div class="bar" style="height:${Math.max(3,(f[i]||0)/mx*100)}px">
<span>${i}</span>
</div>`).join('');
 document.getElementById('score').textContent=best.score??'—';
 const st=document.getElementById('status');
 st.textContent=best.status||'—';
 st.className='status '+((best.entrada)?'green':
   (best.score>=70?'yellow':'red'));
 document.getElementById('reason').textContent=best.motivo||'';
 document.getElementById('signal').textContent=best.tipo||'—';
 document.getElementById('barrier').textContent=best.barreira??'—';
 document.getElementById('trigger').textContent=best.gatilho??'—';
 document.getElementById('rate20').textContent=(best.taxa20??'—')+'%';
 document.getElementById('rate50').textContent=(best.taxa50??'—')+'%';
 document.getElementById('pay').textContent=best.payout??'—';
 document.getElementById('rank').innerHTML=(j.ranking||[]).map(x=>
   `<tr>
<td>${x.tipo}</td>
<td>${x.barreira}</td>
<td>${x.score}</td>
<td>${x.gatilho}</td>
<td>${x.taxa20}%</td>
<td>${x.taxa50}%</td>
<td>${x.status}</td>
</tr>`).join('');
}
function toggleAuto(){
 const b=document.getElementById('autoBtn');
 if(timer){clearInterval(timer);timer=null;b.textContent='▶ AUTO';return}
 scan();
 timer=setInterval(scan,parseInt(document.getElementById('interval').value));
 b.textContent='⏹ PARAR';
}
scan();
</script>
</body>
</html>"""


def register_digit_sniper(
    app,
    tick_fetcher: Callable[..., list[Any]],
    *,
    default_payout: float = 0.85,
    min_score: int = MIN_SCORE,
):
    """Registra a tela e os endpoints no Flask existente."""

    @app.get("/digit-sniper")
    def digit_sniper_page():
        return HTML

    @app.post("/digit-sniper/analisar")
    def digit_sniper_analisar():
        data = request.get_json(silent=True) or {}
        asset = str(data.get("asset", "R_100")).strip().upper()
        payout = float(data.get("payout", default_payout))

        if asset not in DEFAULT_ASSETS:
            return jsonify({"ok": False, "erro": "Ativo não permitido."}), 400

        try:
            ticks = tick_fetcher(asset, count=WINDOW)
        except TypeError:
            ticks = tick_fetcher(asset)

        if not ticks:
            return jsonify({
                "ok": False,
                "erro": f"Não foi possível obter ticks de {asset}.",
            }), 503

        cleaned = []
        for t in ticks:
            if isinstance(t, dict):
                t = t.get("quote", t.get("price", t.get("value")))
            cleaned.append(t)

        result = analyze_digits(_digits_from_ticks(cleaned), payout, min_score)
        result["ativo"] = asset
        return jsonify(result)

    return app
