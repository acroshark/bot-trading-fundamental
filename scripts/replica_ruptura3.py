"""Réplica de 3 años del preset RUPTURA3 («Ruptura V3: rango de apertura con tendencia, volumen y régimen») con el
propio motor, operación a operación frente a la medida prerregistrada. SOLO LECTURA de los datos: no toca bot.db ni el
bot en marcha; nada opera dinero real.

Qué hace:
1. Carga las velas de 1 min de HIST_DIR/1m (las mismas que BACKTEST_DIR/controles/selectiva/medir_selectiva.py), los
   cierres diarios de HIST_DIR/cripto y el funding de HIST_DIR/funding.
2. En cada apertura del laboratorio (28-10-2023 → 20-10-2026, sin festivos ni la sesión especial de São Paulo) calcula
   el contexto con las funciones del motor (bot/ruptura3.py: régimen cripto con las 4 monedas, tendencia diaria y de 4 h)
   y pasa las velas de 1 min, en orden, a apertura.RupturaRun con los parámetros del preset (apertura.RUPTURA3). Las
   velas de 4 h salen de las de 1 min, como en la medida (en vivo son las de Binance; el diagnóstico «4h_en_vivo» mide
   la diferencia con solo las 1.000 últimas, lo que pide el motor).
3. Ordena los disparos (ruptura3.orden), aplica el tope por apertura y pasa cada uno por un libro paper en miniatura
   (el de medir_selectiva.py: una posición por moneda, límites del día de Madrid, pausa por pérdida del día y controles
   del paper) con el plan de apertura.impulse_plan y la gestión de apertura.simulate_plan, con los costes de los
   backtests (comisiones.costes_de_backtest: 0,10 % por lado y 2 pb) y el funding real en los cortos.
4. Compara cada operación con la lista de medir_selectiva.json (Ruptura V3, salida A) y escribe:
   - BACKTEST_DIR/ruptura3_resumen.json: el resumen para la web, con la forma de seis2_resumen.json (más, dentro de
     «fuera» y «dentro», «antes_de_costes»: el R de las mismas operaciones sin comisiones ni deslizamiento ni funding y
     cuánto se lleva cada coste). Su nota dice también lo que la cifra principal no dice: que la puerta prerregistrada
     no pasa y el puesto de V3 entre las configuraciones medidas fuera de muestra, que lo de fuera sale de los cortos,
     lo que dan sin X-Perps y con los costes de hoy (de controles/costes/bruto_neto.json, si está) y el régimen con los
     últimos cierres diarios guardados (si hoy no operaría).
   - BACKTEST_DIR/controles/selectiva/replica_ruptura3.json y .txt: la comparación, el embudo, el diagnóstico de 4 h y,
     con las mismas cuentas, «6 diarias v2» antes y después de costes (comprueba además que este libro la reproduce).
Uso: M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe -X utf8 scripts\\replica_ruptura3.py [--sin-seis2] [--sin-4h]
"""
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import apertura as ap, backtest_aperturas as bt, comisiones, config, paper, ruptura3 as r3  # noqa: E402

SIN_SEIS2 = "--sin-seis2" in sys.argv
SIN_4H = "--sin-4h" in sys.argv
MIN, H, DAY = 60_000, 3_600_000, 86_400_000
M5, H4 = 5 * MIN, 4 * H
MAD = ZoneInfo("Europe/Madrid")
T, O, HI, LO, C, V = 0, 1, 2, 3, 4, 5
FROM, TO = date(2023, 10, 28), date(2026, 10, 20)
SPLIT = int(datetime(2025, 8, 16, tzinfo=MAD).timestamp() * 1000)      # corte dentro/fuera (como seis2_resumen.json)
COINS = list(config.COINS)
SLIP = paper.COST_SLIP_BPS / 1e4
FEE = config.FEE_PCT / 100
RISK = float(config.RISK_PER_TRADE_PCT)
SEL = config.BACKTEST_DIR / "controles" / "selectiva"
OUT_RES = config.BACKTEST_DIR / "ruptura3_resumen.json"
OUT_DET = SEL / "replica_ruptura3.json"
LOGF = SEL / "replica_ruptura3.txt"
assert config.USER_RULES, "las reglas del usuario tienen que estar activas (BOT_USER_RULES distinto de 0)"
assert RISK == 0.5 and config.FEE_PCT == 0.10
P = ap.impulse_params(ap.RUPTURA3)[0]
assert ap.preset_of(P) == "ruptura3" and P["modelo"] == "ruptura3"
P2 = ap.impulse_params(ap.SEIS2)[0]
t0 = time.time()
_fh = open(LOGF, "w", encoding="utf-8")


def log(*a) -> None:
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    _fh.write(s + "\n")
    _fh.flush()


def fecha(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%d-%m-%Y %H:%M")


def dia_mad(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def months(a: str, b: str) -> list[str]:
    y, m, out = int(a[:4]), int(a[5:]), []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


log(f"réplica de RUPTURA3 · reglas del usuario activas · riesgo {RISK} % · {config.FEE_PCT} % por lado · "
    f"{paper.COST_SLIP_BPS} pb · perfil de las comisiones del bot (no se usa aquí): {comisiones.activo()}")

# ================================================================ datos
A = {}
for c in COINS:
    paths = []
    for ym in months("2023-06", "2026-10"):
        z, part = bt.zip_path(bt.SYMS[c], ym), bt.part_path(bt.SYMS[c], ym)
        paths.append(z if z.exists() else part)
    s = bt.load_series([p for p in paths if p.exists()])
    A[c] = {"t": np.frombuffer(s.t, dtype=np.int64).copy(), "o": np.frombuffer(s.cols[0]).copy(),
            "h": np.frombuffer(s.cols[1]).copy(), "l": np.frombuffer(s.cols[2]).copy(),
            "c": np.frombuffer(s.cols[3]).copy(), "v": np.frombuffer(s.cols[4]).copy()}
    del s
FUND, FROWS = {}, {}
for c in COINS:
    raw = json.loads((config.HIST_DIR / "funding" / f"{c}USDT.json").read_text(encoding="utf-8"))
    rows = raw.get("rows") or raw.get("series") if isinstance(raw, dict) else raw
    FROWS[c] = [[int(r[0]), float(r[1])] for r in rows]
    FUND[c] = (np.array([r[0] for r in FROWS[c]], dtype=np.int64), np.array([r[1] for r in FROWS[c]]))
DD1 = {}
for c in COINS:
    rows = json.loads((config.HIST_DIR / "cripto" / f"{c}_1d.json").read_text(encoding="utf-8"))["rows"]
    DD1[c] = {"t": np.array([int(r[0]) for r in rows], dtype=np.int64), "c": np.array([float(r[4]) for r in rows])}
log(f"velas en {time.time() - t0:.0f} s: " + ", ".join(f"{c} {len(A[c]['t'])} hasta {fecha(int(A[c]['t'][-1]))}"
                                                     for c in COINS))
OPS = sorted((o for o in bt.build_openings(FROM, TO, bt.index_data())
              if o["id"] in ap.LAB_WINDOWS and not ap.special_session(o)), key=lambda o: (o["open_ms"], o["id"]))
JORN = {"dentro": len({o["day"] for o in OPS if o["open_ms"] < SPLIT}),
        "fuera": len({o["day"] for o in OPS if o["open_ms"] >= SPLIT})}
log(f"{len(OPS)} aperturas {OPS[0]['day']} → {OPS[-1]['day']} · jornadas dentro {JORN['dentro']}, fuera {JORN['fuera']}")


def barras(c: str, a_ms: int, b_ms: int, j0: int | None = None) -> list[list]:
    x = A[c]
    i = int(np.searchsorted(x["t"], a_ms)) if j0 is None else j0
    j = int(np.searchsorted(x["t"], b_ms))
    return [[t_, o_, h_, l_, c_, v_, 0.0, 0.0] for t_, o_, h_, l_, c_, v_ in
            zip(x["t"][i:j].tolist(), x["o"][i:j].tolist(), x["h"][i:j].tolist(), x["l"][i:j].tolist(),
                x["c"][i:j].tolist(), x["v"][i:j].tolist())]


def tasa_en(coin: str, ms: int) -> float | None:
    """Última tasa de funding publicada antes de ms, en % (la que lee el plan, como medir_selectiva.py)."""
    ft, fr = FUND[coin]
    j = int(np.searchsorted(ft, ms, "left")) - 1
    return float(fr[j] * 100) if j >= 0 else None


def funding_de(coin: str, side: str, a: int, b: int, th: int = 0) -> list[tuple[int, float]]:
    ft, fr = FUND[coin]
    i, j = int(np.searchsorted(ft, a, "right")), int(np.searchsorted(ft, b, "right"))
    return [(int(ft[k]), paper.funding_pay(side, 100.0, float(fr[k]) * 100) * (0.5 if th and ft[k] > th else 1.0))
            for k in range(i, j)]


def midnight(ms: int) -> int:
    dd = datetime.fromtimestamp(ms / 1000, MAD).date()
    return int(datetime(dd.year, dd.month, dd.day, tzinfo=MAD).timestamp() * 1000)


# ================================================================ el paper en miniatura (el libro de medir_selectiva.py)
class Cartera:
    def __init__(self, eq0: float = 100.0):
        self.eq0, self.realized, self.live, self.day, self.base, self.checked = eq0, 0.0, [], None, None, None
        self.halted, self.halt_days = False, []

    def eq_grid(self, g: np.ndarray) -> np.ndarray:
        a = int(g[0])
        keep = []
        for p in self.live:
            if p["tx"] <= a:
                self.realized += p["pnl"]
            else:
                keep.append(p)
        self.live = keep
        tot = np.full(len(g), self.eq0 + self.realized)
        for p in self.live:
            if p["te"] > g[-1]:
                continue
            x = A[p["coin"]]
            k = np.clip(np.searchsorted(x["t"], g - MIN, "right") - 1, 0, len(x["t"]) - 1)
            mv = p["d"] * (x["c"][k] / p["E"] - 1)
            if p["th"]:
                aft = g >= p["th"]
                up = p["N"] * np.where(aft, 0.5 * mv + p["rh"] / 100, mv) - p["fee_open"]
            else:
                up = p["N"] * mv - p["fee_open"]
            if p["fund"]:
                fm = np.array([m for m, _ in p["fund"]], dtype=np.int64)
                fc = np.cumsum([v for _, v in p["fund"]]) * p["N"] / 100
                jj = np.searchsorted(fm, g, "right")
                up = up + np.where(jj > 0, fc[np.maximum(jj - 1, 0)], 0.0)
            tot += np.where(g >= p["tx"], p["pnl"], np.where(g >= p["te"], up, 0.0))
        return tot

    def eq_at(self, t: int) -> float:
        return float(self.eq_grid(np.array([t], dtype=np.int64))[0])

    def halt_check(self, t: int, max_loss: float) -> bool:
        ds = midnight(t)
        if self.day != ds:
            self.day, self.halted, self.checked = ds, False, ds
            self.base = self.eq_at(ds)
        if not self.halted and t > self.checked:
            g = np.arange(self.checked + MIN, t + 1, MIN, dtype=np.int64)
            eq = self.eq_grid(g)
            if (self.base - eq.min()) / self.base * 100 >= max_loss - 1e-12:
                self.halted = True
                self.halt_days.append(dia_mad(ds))
            self.checked = t
        return self.halted


SIMC: dict = {}


def simular(coin: str, j: int, d: int, plan: dict, hh: float, ha: float | None, fee: float | None = None,
            slip: float | None = None) -> dict:
    """apertura.simulate_plan desde la vela j (entrada a su apertura con el deslizamiento), como medir_selectiva.py."""
    slip = SLIP if slip is None else slip
    key = (coin, j, d, plan["stopPct"], plan["tp1R"], plan["tp2R"], plan["tp1Frac"], plan["lockR"], plan["lockFloorR"],
           plan["lockTrailR"], hh, ha, fee, slip)
    if key in SIMC:
        return SIMC[key]
    x = A[coin]
    t0_ = int(x["t"][j])
    end = t0_ + int(hh * H)
    end_act = t0_ + int(ha * H) if ha else None
    full = max(end, end_act or 0) + 3 * H
    for lim in sorted({min(full, t0_ + 27 * H), full}):
        bars = barras(coin, t0_, lim, j0=j)
        rr = ap.simulate_plan(bars, 0, d, plan, end, fee=fee, slip=slip, end_active_ms=end_act)
        if rr["why"] != "fin de datos" or lim >= full:
            break
    E = bars[0][O] * (1 + d * slip)
    th, rh = 0, 0.0
    if rr["partial"] and rr.get("partial_t"):
        frac = float(plan["tp1Frac"])
        tp1 = E * (1 + d * plan["stopPct"] * plan["tp1R"] / 100)
        th = int(rr["partial_t"])
        rh = (frac * d * (tp1 / E - 1) - FEE * frac * tp1 / E) * 100
    out = {"E": E, "t_fill": t0_, "ext": int(rr["t"]), "ret": float(rr["ret"]), "why": rr["why"],
           "partial": bool(rr["partial"]), "th": th, "rh": rh, "locked": bool(rr["locked"])}
    SIMC[key] = out
    return out


def W_PERP(i: str) -> float:
    return 1 / paper.PERP_MAX_LEVERAGE if i == "perp" else 1.0


class Book:
    """Una posición por moneda, topes del día de Madrid, pausa por pérdida del día y controles del paper (4 posiciones,
    exposición ≤ 100 %, perpetuos ≤ 2 × capital), como medir_selectiva.Book. plan(d, E, sl, rate, inst) → (plan, 24, 168)."""

    def __init__(self, params: dict):
        self.params = params
        self.cart = Cartera()
        self.open: list[dict] = []
        self.trades: list[dict] = []
        self.ledger: dict[str, list] = defaultdict(list)
        self.no = Counter()

    def entrar(self, coin: str, d: int, t: int, sl: float, cap: tuple | None = None) -> tuple[str, str | None]:
        x = A[coin]
        side = "long" if d > 0 else "short"
        self.open = [p for p in self.open if p["tx"] > t]
        if self.cart.halt_check(t, config.MAX_DAILY_LOSS_PCT):
            self.no["paper detenido por la pérdida del día"] += 1
            return "tope", "perdida_dia"
        if any(p["coin"] == coin for p in self.open):
            self.no["moneda ocupada"] += 1
            return "ocupada", "moneda_ocupada"
        j = int(np.searchsorted(x["t"], t))
        if j >= len(x["t"]):
            self.no["sin velas"] += 1
            return "datos", "sin_velas"
        inst = ap.impulse_instrument(d, self.params)
        rate = tasa_en(coin, t)
        E = float(x["o"][j] * (1 + d * SLIP))
        pp = {**self.params, "sl_pct": sl}
        plan = ap.impulse_plan(d, E, pp, rate, inst)
        if not plan["ok"]:
            self.no["plan (regla 2R / 3 % neto)"] += 1
            return "plan", "reglas"
        eq = self.cart.eq_at(t)
        day = self.ledger[dia_mad(t)]
        limit = ap.day_limits(len(day), sum(day) / eq * 100, RISK, self.params)
        if limit:
            k = "tope de 6 del día" if "entradas" in limit else "riesgo del día (3 %)"
            self.no[k] += 1
            return "tope", ap._limit_code(limit)
        if cap is not None and cap[1] is not None and cap[0] >= cap[1]:
            self.no[f"tope de {cap[1]} por apertura"] += 1
            return "tope", "tope_apertura"
        loss = paper.stop_loss_frac(side, sl, paper.SPREAD_FALLBACK_BPS / 2 + paper.COST_SLIP_BPS)
        N = min(eq * RISK / 100 / loss, eq * paper.MAX_NOTIONAL_PCT / 100)

        def rem(p: dict) -> float:
            return 0.5 if p["th"] and p["th"] <= t else 1.0

        expo = sum(p["N"] * rem(p) * W_PERP(p["inst"]) for p in self.open) + N * W_PERP(inst)
        perp = sum(p["N"] * rem(p) for p in self.open if p["inst"] == "perp") + (N if inst == "perp" else 0.0)
        why_no = ("4 posiciones abiertas" if len(self.open) >= config.MAX_POSITIONS else
                  "exposición > 100 %" if expo > eq * paper.MAX_EXPOSURE_PCT / 100 + 1e-6 else
                  "perpetuos > 2 × capital" if inst == "perp" and perp > eq * paper.PERP_MAX_LEVERAGE + 1e-6 else None)
        if why_no:
            self.no[why_no] += 1
            return "tope", "max_posiciones" if why_no.startswith("4") else "exposicion"
        hh, ha = float(self.params["max_hold_h"]), self.params.get("max_hold_active_h")
        r = simular(coin, j, d, plan, hh, ha)
        fl = funding_de(coin, side, r["t_fill"], r["ext"], r["th"]) if inst == "perp" else []
        f_pct = sum(v for _, v in fl)
        p = {"coin": coin, "d": d, "te": r["t_fill"], "tx": r["ext"], "E": r["E"], "N": N, "inst": inst, "th": r["th"],
             "rh": r["rh"], "fee_open": N * FEE, "pnl": N * (r["ret"] + f_pct) / 100, "fund": fl}
        self.cart.live.append(p)
        self.open.append(p)
        day.append(N * sl / 100)
        self.trades.append({"coin": coin, "d": d, "t": t, "t_fill": r["t_fill"], "dia": dia_mad(t), "ext": r["ext"],
                            "ret": r["ret"], "R": r["ret"] / sl, "fund_pct": f_pct, "R_f": (r["ret"] + f_pct) / sl,
                            "why": r["why"], "partial": r["partial"], "th": r["th"], "locked": r["locked"], "N": N,
                            "inst": inst, "sl": sl, "j": j, "plan": plan, "hh": hh, "ha": ha})
        return "entra", None


def compacta(r: dict) -> dict:
    """La forma de las operaciones de medir_selectiva.json (compacta)."""
    return {"moneda": r["coin"], "lado": "largo" if r["d"] > 0 else "corto", "apertura": r.get("win"),
            "entrada": fecha(r["t_fill"]), "salida": fecha(r["ext"]), "stop_pct": round(r["sl"], 3),
            "R": round(r["R_f"], 3), "motivo": r["why"] + (" (mitad en 3R antes)" if r["partial"] and r["why"] !=
                                                           "objetivo" else ""),
            "muestra": "dentro" if r["t"] < SPLIT else "fuera"}


# ================================================================ contexto de cada apertura (las funciones del motor)
B4 = {}
for c in COINS:
    a = A[c]
    k4 = a["t"] // H4
    uq, st = np.unique(k4, return_index=True)
    en = np.r_[st[1:], len(k4)]
    h4_, l4_, c4_ = np.maximum.reduceat(a["h"], st), np.minimum.reduceat(a["l"], st), a["c"][en - 1]
    B4[c] = {"start": uq * H4, "n": en - st, "h": h4_, "l": l4_, "s": r3.series_4h(h4_, l4_, c4_)}


def t4_de(c: str, T0: int) -> dict | None:
    """Tendencia de 4 h con la serie entera de velas de 4 h hechas con las de 1 min (como la medida)."""
    b = B4[c]
    i = int(np.searchsorted(b["start"], T0 - H4, "right")) - 1
    if i < 0 or b["n"][i] < r3.MIN_1M_4H:
        return None
    return r3.tendencia_4h_en(b["s"], i)


def t4_vivo(c: str, T0: int) -> dict | None:
    """Lo que vería el motor en vivo: las 1.000 últimas velas de 4 h cerradas (ruptura3.tendencia_4h)."""
    b = B4[c]
    i = int(np.searchsorted(b["start"], T0 - H4, "right"))
    k = max(0, i - 1000)
    bars = [[int(b["start"][x]), 0.0, float(b["h"][x]), float(b["l"][x]), float(b["s"]["c"][x])] for x in range(k, i)]
    return r3.tendencia_4h(bars, T0)


_reg: dict = {}


def contexto(T0: int) -> tuple[dict, dict]:
    nn = {c: int(np.searchsorted(DD1[c]["t"] + DAY, T0, "right")) for c in COINS}
    f8 = {c: r3.funding_8h(FROWS[c], T0) for c in COINS}
    key = (tuple(nn.values()), tuple(f8.values()))
    if key not in _reg:
        _reg[key] = r3.regimen({c: DD1[c]["c"][max(0, nn[c] - r3.DIAS_REGIMEN):nn[c]].tolist() for c in COINS}, f8)
    return _reg[key], nn


EMB = Counter()
ZON = {"dentro": Counter(), "fuera": Counter()}
CANDS = []
DIAG4 = Counter()
for n, o in enumerate(OPS):
    T0 = o["open_ms"]
    reg, nn = contexto(T0)
    ZON["dentro" if T0 < SPLIT else "fuera"][reg["zona"]] += 1
    for c in COINS:
        t4 = t4_de(c, T0)
        rp = {"zona": reg["zona"], "puntos": reg["puntos"], "d_dia": r3.tendencia_diaria(DD1[c]["c"][:nn[c]]),
              "t4": t4}
        if not SIN_4H and t4 is not None:
            tv = t4_vivo(c, T0)
            if tv is None:
                DIAG4["sin_tendencia_en_vivo"] += 1
            else:
                same = (tv["d"] == t4["d"] and tv["di"] == t4["di"] and (tv["adx"] >= r3.ADX_MIN) == (t4["adx"] >= r3.ADX_MIN))
                DIAG4["igual_decision" if same else "distinta_decision"] += 1
                DIAG4["max_dif_adx_x1e9"] = max(DIAG4["max_dif_adx_x1e9"], int(abs(tv["adx"] - t4["adx"]) * 1e9))
        EMB["moneda×apertura"] += 1
        run = ap.RupturaRun(c, T0, P, ctx={"rp": rp, "btc_dir": 0, "atr_pct": None, "prev_react": None})
        act = None
        for b in barras(c, T0 - r3.PRE5 * M5, T0 + (P["entry_end_min"] + 5) * MIN):
            act = run.feed(b)
            if act or run.done:
                break
        if act and act.get("action") == "enter":
            EMB["entra"] += 1
            CANDS.append({"n_o": n, "T0": T0, "win": o["id"], "coin": c, "d": act["dir"], "t": act["t"],
                          "s": act["stop_pct"], "s_struct": act["s_struct"], "adx": act["adx"], "ext": act["ext"]})
        else:
            EMB[(run.decs[-1]["motivo_codigo"] if run.decs else "sin_decision") if run.done else "sin_terminar"] += 1
log(f"contexto y disparos en {time.time() - t0:.0f} s · zonas {json.dumps({k: dict(v) for k, v in ZON.items()})} · "
    f"embudo {dict(EMB)} · 4 h en vivo {dict(DIAG4)}")


def correr(cands: list[dict], params: dict, tope) -> tuple[Book, Counter]:
    book, ent_o, diag = Book(params), Counter(), Counter()
    for x in sorted(cands, key=r3.orden):
        st, code = book.entrar(x["coin"], x["d"], x["t"], x["s"], cap=(ent_o[x["n_o"]], tope))
        diag[st if st != "tope" else "tope: " + code] += 1
        if st == "entra":
            ent_o[x["n_o"]] += 1
            book.trades[-1].update(n_o=x["n_o"], win=x["win"])
    return book, diag


with comisiones.costes_de_backtest():
    BK, DIAG = correr(CANDS, P, P["rp_max_apertura"])
ROWS = BK.trades
log(f"Ruptura V3: {len(ROWS)} operaciones · {dict(DIAG)} · libro {dict(BK.no)} · {time.time() - t0:.0f} s")

# ================================================================ comparación con medir_selectiva.json
MEDIDA = json.loads((SEL / "medir_selectiva.json").read_text(encoding="utf-8"))
REF = MEDIDA["estrategias"]["ruptura"]["variantes"]["V3"]["salida_A"]
ref_ops = REF["operaciones"]
mine = [compacta(r) for r in sorted(ROWS, key=lambda r: r["t"])]
k_ref = Counter(json.dumps(x, sort_keys=True, ensure_ascii=False) for x in ref_ops)
k_mine = Counter(json.dumps(x, sort_keys=True, ensure_ascii=False) for x in mine)
solo_ref = list((k_ref - k_mine).elements())
solo_mia = list((k_mine - k_ref).elements())
iguales = sum((k_ref & k_mine).values())
COMP = {"medida": str(SEL / "medir_selectiva.json"), "operaciones_medida": len(ref_ops), "operaciones_replica": len(mine),
        "iguales": iguales, "solo_en_la_medida": [json.loads(x) for x in solo_ref],
        "solo_en_la_replica": [json.loads(x) for x in solo_mia],
        "mismo_orden": mine == ref_ops,
        "senales_medida": MEDIDA["estrategias"]["ruptura"]["variantes"]["V3"]["senales"], "senales_replica": len(CANDS),
        "embudo_medida": MEDIDA["estrategias"]["ruptura"]["variantes"]["V3"]["embudo_moneda_apertura"],
        "embudo_replica": dict(EMB), "vetos_libro_medida": REF["vetos_libro"], "vetos_libro_replica": dict(BK.no)}
COMP["reproduce"] = bool(not solo_ref and not solo_mia and len(mine) == len(ref_ops))
log(f"COMPARACIÓN con medir_selectiva.json: {iguales} de {len(ref_ops)} operaciones iguales (moneda, lado, apertura, "
    f"entrada, salida, stop, R con funding, motivo y muestra) · solo en la medida {len(solo_ref)} · solo en la réplica "
    f"{len(solo_mia)} · señales {len(CANDS)} (medida {COMP['senales_medida']}) → "
    f"{'REPRODUCE' if COMP['reproduce'] else 'NO REPRODUCE'}")
for x in COMP["solo_en_la_medida"][:20]:
    log("   solo en la medida:", json.dumps(x, ensure_ascii=False))
for x in COMP["solo_en_la_replica"][:20]:
    log("   solo en la réplica:", json.dumps(x, ensure_ascii=False))


# ================================================================ antes y después de costes (mismas operaciones)
def costes(rows: list[dict]) -> list[dict]:
    """Cada operación otra vez con el mismo plan: sin comisión ni deslizamiento (bruto), solo con deslizamiento y con
    los dos (neto, la de verdad); el funding aparte. En R de la operación (R = su stop)."""
    out = []
    for r in rows:
        plan, j, d = r["plan"], r["j"], r["d"]
        b0 = simular(r["coin"], j, d, plan, r["hh"], r["ha"], fee=0.0, slip=0.0)
        b1 = simular(r["coin"], j, d, plan, r["hh"], r["ha"], fee=0.0, slip=SLIP)
        out.append({"t": r["t"], "bruto": b0["ret"] / r["sl"], "desliz": b1["ret"] / r["sl"],
                    "neto": r["ret"] / r["sl"], "neto_f": r["R_f"], "sl": r["sl"]})
    return out


def bloque_costes(cs: list[dict], jornadas: int) -> dict | None:
    if not cs:
        return None
    m = lambda k: float(np.mean([x[k] for x in cs]))   # noqa: E731
    br, de, ne, nf = m("bruto"), m("desliz"), m("neto"), m("neto_f")
    n = len(cs)
    return {"n": n, "R_bruto": round(br, 4), "deslizamiento_R": round(de - br, 4), "comisiones_R": round(ne - de, 4),
            "funding_R": round(nf - ne, 4), "R_neto": round(nf, 4), "costes_R": round(nf - br, 4),
            "gana_bruto_pct": round(100 * sum(1 for x in cs if x["bruto"] > 0) / n, 1),
            "gana_neto_pct": round(100 * sum(1 for x in cs if x["neto_f"] > 0) / n, 1),
            "stop_medio_pct": round(float(np.mean([x["sl"] for x in cs])), 3),
            "cuenta_jornada_bruto_pct": round(br * RISK * n / jornadas, 4),
            "cuenta_jornada_costes_pct": round((nf - br) * RISK * n / jornadas, 4),
            "cuenta_jornada_neto_pct": round(nf * RISK * n / jornadas, 4),
            "op_jornada": round(n / jornadas, 3)}


with comisiones.costes_de_backtest():
    CS = costes(ROWS)
COSTES = {seg: bloque_costes([x for x in CS if (x["t"] < SPLIT) == (seg == "dentro")], JORN[seg])
          for seg in ("dentro", "fuera")}
log(f"Ruptura V3 antes y después de costes: {json.dumps(COSTES, ensure_ascii=False)}")

SEIS2 = None
if not SIN_SEIS2:   # «6 diarias v2» con el mismo libro: las entradas de ImpulseRun (SEIS, noticias como etiqueta)
    PS = ap.impulse_params({**ap.SEIS, "news_mode": "etiqueta"})[0]
    c2 = []
    for n, o in enumerate(OPS):
        om = o["open_ms"]
        for c in COINS:
            run = ap.ImpulseRun(c, om, PS, {"btc_dir": 0, "atr_pct": None, "prev_react": None}, None, [])
            act = None
            for b in barras(c, om, om + 122 * MIN):
                act = run.feed(b)
                if act or run.done:
                    break
            if act and act.get("action") == "enter":
                c2.append({"n_o": n, "coin": c, "t": int(act["t"]), "d": act["dir"], "s": 2.0, "win": o["id"],
                           "adx": 0.0, "ext": 0.0})
    with comisiones.costes_de_backtest():
        bk2, dg2 = correr(c2, P2, None)
        cs2 = costes(bk2.trades)
    r2 = bk2.trades
    cif = {seg: {"n": len(v), "R": round(float(np.mean([r["R"] for r in v])), 4),
                 "R_funding": round(float(np.mean([r["R_f"] for r in v])), 4)}
           for seg, v in (("dentro", [r for r in r2 if r["t"] < SPLIT]), ("fuera", [r for r in r2 if r["t"] >= SPLIT]))}
    ref2 = MEDIDA["verificacion"]["seis2"]["con_el_plan_real_de_SEIS2"]
    SEIS2 = {"cifras": cif, "referencia_medir_selectiva": {k: ref2[k] for k in ("dentro_n", "dentro_R", "fuera_n", "fuera_R")},
             "reproduce": (cif["dentro"]["n"] == ref2["dentro_n"] and abs(cif["dentro"]["R"] - ref2["dentro_R"]) < 6e-5
                           and cif["fuera"]["n"] == ref2["fuera_n"] and abs(cif["fuera"]["R"] - ref2["fuera_R"]) < 6e-5),
             "antes_de_costes": {seg: bloque_costes([x for x in cs2 if (x["t"] < SPLIT) == (seg == "dentro")], JORN[seg])
                                 for seg in ("dentro", "fuera")},
             "libro": dict(bk2.no)}
    log(f"«6 diarias v2» con este libro: {json.dumps(cif)} (medir_selectiva, plan real de SEIS2: "
        f"{json.dumps(SEIS2['referencia_medir_selectiva'])}) → {'reproduce' if SEIS2['reproduce'] else 'NO reproduce'} · "
        f"antes y después de costes {json.dumps(SEIS2['antes_de_costes'], ensure_ascii=False)} · {time.time() - t0:.0f} s")


# ================================================================ resumen para la web (forma de seis2_resumen.json)
def _curva(rows: list[dict]) -> list[list]:
    acc, out = 0.0, []
    for r in sorted(rows, key=lambda r: (r["ext"], r["t"])):
        acc += r["R"]
        out.append([int(r["ext"]), round(acc, 3)])
    return out


def _salida(r: dict) -> str:
    w = r["why"]
    if w == "inicial":
        return "stop"
    if w == "tiempo" and r["locked"]:
        return "tiempo (activada)"
    return w


def _bloque(rows: list[dict], seg: str) -> dict:
    rs = [r["R"] for r in rows]
    lg_ = [r["R"] for r in rows if r["d"] > 0]
    sh_ = [r["R"] for r in rows if r["d"] < 0]
    mR = lambda v: float(np.mean(v))   # noqa: E731
    return {"n": len(rs), "mean_R": round(mR(rs), 4) if rs else None,
            "win_rate": round(sum(x > 0 for x in rs) / len(rs), 4) if rs else None,
            "salidas": dict(Counter(_salida(r) for r in rows)),
            "horas_media": round(mR([(r["ext"] - r["t_fill"]) / H for r in rows]), 1) if rows else None,
            "largos": {"n": len(lg_), "mean_R": round(mR(lg_), 4) if lg_ else None},
            "cortos": {"n": len(sh_), "mean_R": round(mR(sh_), 4) if sh_ else None},
            "desde": int(min(r["t_fill"] for r in rows)) if rows else None,
            "hasta": int(max(r["ext"] for r in rows)) if rows else None, "curva_R": _curva(rows),
            "op_jornada": round(len(rs) / JORN[seg], 3), "stop_medio_pct": round(mR([r["sl"] for r in rows]), 3)
            if rows else None, "antes_de_costes": COSTES[seg]}


dentro = [r for r in ROWS if r["t"] < SPLIT]
fuera = [r for r in ROWS if r["t"] >= SPLIT]
f_out, f_in = _bloque(fuera, "fuera"), _bloque(dentro, "dentro")
f_out.update(paper_R=f_out["mean_R"],
             paper_R_funding=round(float(np.mean([r["R_f"] for r in fuera])), 4) if fuera else None)
f_in["paper_R_funding"] = round(float(np.mean([r["R_f"] for r in dentro])), 4) if dentro else None
_FUERA = ("estrategia", "pb_frac", "brk_tf", "target_pct", "min_strength", "session_max_min", "min_move_pct", "lab")
params = {k: P[k] for k in ap.IMP_STATUS_KEYS if k in P and k not in _FUERA}
params.update(estrategia="impulso", preset="ruptura3")
cd, cf = COSTES["dentro"] or {}, COSTES["fuera"] or {}


# ================================================================ lo que la cifra principal no dice (revisión del 29-10-2026)
def configuraciones_fuera() -> list[tuple[float, str]]:
    """(R fuera de muestra con funding, nombre) de cada variante × salida que midió medir_selectiva.py (sin los
    controles ni las salidas repetidas)."""
    out = []
    for est, e in MEDIDA["estrategias"].items():
        for vn, v in (e.get("variantes") or {}).items():
            for sal, b in v.items():
                f = b.get("fuera") if sal.startswith("salida_") and isinstance(b, dict) else None
                if isinstance(f, dict) and isinstance(f.get("R_medio_funding", f.get("R_medio")), (int, float)):
                    out.append((float(f.get("R_medio_funding", f.get("R_medio"))), f"{est} {vn} {sal[7:]}"))
    return sorted(out)


def regimen_hoy() -> dict:
    """El régimen (bot/ruptura3.py) con los últimos cierres diarios y el funding guardados, el de una apertura de hoy, y
    desde qué día está en esa zona (un punto al día, a las 8:00 UTC, con los cierres de ese momento)."""
    fin = int(min(DD1[c]["t"][-1] for c in COINS)) + DAY      # cuando cerró la última vela diaria de las 4
    dias = []
    for k in range(120):
        reg_k, _ = contexto(fin - k * DAY + 8 * H)
        dias.append((fin - k * DAY + 8 * H, reg_k["zona"], reg_k["puntos"]))
    desde = dias[0][0]
    for t_k, z_k, _ in dias[1:]:
        if z_k != dias[0][1]:
            break
        desde = t_k
    return {"zona": dias[0][1], "puntos": dias[0][2], "ultimo_cierre": dia_mad(fin - DAY), "desde": dia_mad(desde),
            "zonas_60_dias": dict(Counter(z for _, z, _ in dias[:60]))}


CRIT = MEDIDA.get("criterios_fijados", {}).get("ruptura") or {}
CONF = configuraciones_fuera()
_v3 = next((r_ for r_, n_ in CONF if n_ == "ruptura V3 A"), None)
RANGO = 1 + sum(1 for r_, _ in CONF if r_ > _v3 + 1e-12) if _v3 is not None else None   # puesto de V3 (1 = la mejor)
sh_f = [r for r in fuera if r["d"] < 0]
lg_3 = [r["R_f"] for r in ROWS if r["d"] > 0]
lg_in, lg_out = [r["R_f"] for r in dentro if r["d"] > 0], [r["R_f"] for r in fuera if r["d"] > 0]
HOY = regimen_hoy()
BN = config.BACKTEST_DIR / "controles" / "costes" / "bruto_neto.json"
try:   # el estudio de costes (bruto, costes de hoy del paper y sin X-Perps), si está
    _bn = json.loads(BN.read_text(encoding="utf-8"))["estrategias"]["ruptura_v3"]["casos"]
    SXP = {seg: _bn["sin_xperps"][seg] for seg in ("dentro", "fuera")}
    HOYC = {seg: _bn["paper_hoy"][seg] for seg in ("dentro", "fuera")}
except (OSError, ValueError, KeyError, TypeError):
    SXP = HOYC = None
log(f"puerta prerregistrada: {CRIT.get('veredicto')} · V3 es la {RANGO}.ª de {len(CONF)} configuraciones fuera de "
    f"muestra ({CONF[0][0]:+.3f} a {CONF[-1][0]:+.3f} R) · régimen de hoy {json.dumps(HOY, ensure_ascii=False)} · "
    f"bruto_neto.json {'leído' if SXP else 'no está'}")
_mR = lambda v: float(np.mean(v)) if v else 0.0   # noqa: E731
nota_honesta = (
    (f"La puerta fijada antes de medir (diseno_ruptura.md, sección 11, sobre V1) dio «{CRIT['veredicto']}» (V1: "
     f"{ap._sg(CRIT['R_dentro'], 2)} R dentro y {ap._sg(CRIT['R_fuera'], 2)} R fuera, por debajo del p95 de su base al "
     f"azar, {ap._sg(CRIT['base_p95_fuera'], 2)}) y el diseño no asciende otra variante tras ver los datos: V3 sale de "
     f"la regla de elección del script (máximo R dentro entre las de ≥ 150 operaciones dentro) y es la "
     + ("mejor" if RANGO == 1 else f"{RANGO}.ª") + f" fuera de muestra de las {len(CONF)} configuraciones medidas "
     f"(de {ap._sg(CONF[0][0], 2)} a {ap._sg(CONF[-1][0], 2)} R), así que su cifra de fuera es optimista. Se prueba en "
     f"paper por decisión del usuario, no porque pasara su puerta. " if CRIT.get("veredicto") and RANGO else "")
    + f"El resultado de fuera de muestra sale de los cortos (en RISK_OFF): sus {len(sh_f)} cortos dan "
      f"{ap._sg(_mR([r['R_f'] for r in sh_f]), 3)} R por operación ({ap._n(sum(r['R_f'] for r in sh_f), 1)} de los "
      f"{ap._n(sum(r['R_f'] for r in fuera), 1)} R); los "
      f"{len(lg_3)} largos de los 3 años dan {ap._sg(_mR(lg_3), 3)} R (dentro {ap._sg(_mR(lg_in), 3)} en {len(lg_in)}, "
      f"fuera {ap._sg(_mR(lg_out), 3)} en {len(lg_out)}). Sin cuenta X-Perps no hay cortos"
    + (f": con 0,35 % por lado sus compras dan {ap._sg(SXP['dentro']['R_medio'], 3)} R dentro y "
       f"{ap._sg(SXP['fuera']['R_medio'], 3)} R fuera (controles/costes/bruto_neto.json). " if SXP else ". ")
    + f"Con los cierres diarios guardados hasta el {datetime.fromisoformat(HOY['ultimo_cierre']).strftime('%d-%m-%Y')}, "
      f"el régimen está en {HOY['zona']} ({ap._n(HOY['puntos'], 1)} puntos) desde el "
      f"{datetime.fromisoformat(HOY['desde']).strftime('%d-%m-%Y')}"
    + (": así no mira ninguna moneda (en RISK_ON solo compraría, el lado sin resultado). " if HOY["zona"] not in
       ("RISK_ON", "RISK_OFF") else (": solo compras, el lado sin resultado. " if HOY["zona"] == "RISK_ON" else
                                     ": solo ventas en perpetuo (hacen falta X-Perps). "))
    + (f"Con los costes de hoy del paper (OKX EU con X-Perps): {ap._sg(HOYC['dentro']['R_medio'], 3)} R dentro y "
       f"{ap._sg(HOYC['fuera']['R_medio'], 3)} R fuera. " if HOYC else ""))
nota = (f"Réplica del motor con el preset RUPTURA3 («Ruptura V3»): apertura.RupturaRun con el contexto de "
        f"bot/ruptura3.py (régimen cripto de la skill crypto-regime-analyzer con las 4 monedas: largos solo en RISK_ON y "
        f"cortos solo en RISK_OFF; tendencia diaria y de 4 h y ADX(14) de 4 h ≥ 25), rango de los 15 primeros min, "
        f"disparo al cierre de 5 min fuera del rango con volumen ≥ 1,5 × el del rango y rango verdadero ≥ 1,5 × ATR(14) "
        f"de 5 min hasta +240 min, stop máx(2 %, el otro lado del rango + 0,10 %) hasta el 3 %, como mucho 2 por "
        f"apertura, y la salida A de «6 diarias v2» con R = ese stop (apertura.simulate_plan), una posición por moneda, "
        f"≤ 6 entradas y 3 % de riesgo por día y los controles del paper, {ap._n(config.FEE_PCT)} % de comisión por lado "
        f"y {ap._n(paper.COST_SLIP_BPS, 0)} pb de deslizamiento. "
        + (f"Coincide operación a operación con BACKTEST_DIR/controles/selectiva/medir_selectiva.py (las {iguales} "
           f"operaciones). " if COMP["reproduce"] else
           f"NO coincide del todo con medir_selectiva.py: {iguales} de {len(ref_ops)} operaciones iguales (ver "
           f"controles/selectiva/replica_ruptura3.json). ")
        + f"mean_R y paper_R van sin funding; paper_R_funding, con el funding real. La historia no tiene noticias: el "
          f"veto de noticias corroboradas no actúa. Fuera de muestra {ap._sg(f_out['mean_R'], 3)} R en {f_out['n']} "
          f"operaciones ({ap._n(f_out['op_jornada'], 2)} por jornada), pero dentro {ap._sg(f_in['mean_R'], 3)} R en "
          f"{f_in['n']}: el signo cambia con el periodo (dentro casi todo largos, fuera casi todo cortos) y frente a su "
          f"base con el sentido al azar p = {ap._n(REF['base_emparejada']['fuera']['p_valor'], 2)} (medir_selectiva.py): NO "
          f"es una ventaja demostrada. "
        + nota_honesta
        + f"Antes de costes las mismas operaciones dan {ap._sg(cd.get('R_bruto') or 0, 3)} R dentro y "
          f"{ap._sg(cf.get('R_bruto') or 0, 3)} R fuera; los costes se llevan {ap._n(-(cd.get('costes_R') or 0), 3)} y "
          f"{ap._n(-(cf.get('costes_R') or 0), 3)} R por operación (con stops de ≈ 2 %, 0,24 % de ida y vuelta son "
          f"≈ 0,12 R). Corte dentro/fuera el {datetime.fromtimestamp(SPLIT / 1000, MAD).strftime('%d-%m-%Y')}, como "
          f"seis2_resumen.json.")
resumen = {"generated": int(time.time() * 1000), "split": SPLIT, "sl_pct": P["sl_pct"], "nota": nota, "fuera": f_out,
           "dentro": f_in, "nombre": ap.PRESETS["ruptura3"]["nombre"], "params": params}
ref6 = json.loads((config.BACKTEST_DIR / "seis2_resumen.json").read_text(encoding="utf-8"))
assert set(resumen) == set(ref6), (set(resumen), set(ref6))
assert set(ref6["fuera"]) <= set(resumen["fuera"]) and set(ref6["dentro"]) - {"paper_R_funding"} <= set(resumen["dentro"])
OUT_RES.write_text(json.dumps(resumen, ensure_ascii=False), encoding="utf-8")
log(f"resumen guardado en {OUT_RES}: fuera {f_out['n']} op {f_out['mean_R']:+.4f} R (con funding "
    f"{f_out['paper_R_funding']:+.4f}) · dentro {f_in['n']} op {f_in['mean_R']:+.4f} R")
det = {"generado": datetime.now(MAD).isoformat(timespec="seconds"), "comparacion": COMP, "diagnostico_4h_en_vivo": dict(DIAG4),
       "zonas_regimen": {k: dict(v) for k, v in ZON.items()}, "diagnostico_libro": dict(DIAG),
       "antes_de_costes": COSTES, "seis2_mismo_libro": SEIS2, "jornadas": JORN,
       "lo_que_no_dice_la_cifra": {   # revisión del 29-10-2026: la puerta, de dónde sale lo de fuera y el régimen de hoy
           "puerta_prerregistrada_ruptura": CRIT, "criterio_de_eleccion": MEDIDA.get("eleccion", {}).get("criterio"),
           "puesto_fuera_de_V3": RANGO, "configuraciones_medidas": len(CONF),
           "R_fuera_de_las_configuraciones": [round(CONF[0][0], 4), round(CONF[-1][0], 4)] if CONF else None,
           "cortos_fuera": {"n": len(sh_f), "R_medio": round(_mR([r["R_f"] for r in sh_f]), 4),
                            "R_total": round(sum(r["R_f"] for r in sh_f), 3),
                            "R_total_fuera": round(sum(r["R_f"] for r in fuera), 3)},
           "largos": {"tres_anos": {"n": len(lg_3), "R_medio": round(_mR(lg_3), 4)},
                      "dentro": {"n": len(lg_in), "R_medio": round(_mR(lg_in), 4)},
                      "fuera": {"n": len(lg_out), "R_medio": round(_mR(lg_out), 4)}},
           "sin_xperps_bruto_neto": SXP, "costes_de_hoy_bruto_neto": HOYC, "regimen_hoy": HOY},
       "operaciones": mine}
OUT_DET.write_text(json.dumps(det, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
log(f"detalle en {OUT_DET} · hecho en {time.time() - t0:.0f} s")
