"""Réplica de 3 años del preset SEIS4R («6 diarias 4R: stop 2 %, objetivo 4R, hasta 7 días») con el propio motor,
operación a operación frente a la variante «F0 stop 2 % · objetivo 4R · tope 7d» de
BACKTEST_DIR/controles/salidas_motor.py (la n.º 1 del ranking de estrategias). SOLO LECTURA de los datos: no toca bot.db
ni el bot en marcha; nada opera dinero real.

Qué hace:
1. Carga las velas de 1 min de HIST_DIR/1m (las mismas que salidas_motor.py, desde septiembre de 2023) y el funding de
   HIST_DIR/funding.
2. Entradas: en cada apertura del laboratorio (28-10-2023 → 20-10-2026, sin la sesión especial de São Paulo) pasa las
   velas de 1 min, en orden, a apertura.ImpulseRun con los parámetros del preset (apertura.SEIS4R, sin noticias: la
   historia no las tiene y sin grupos fiables el veto no actúa). Comprueba que son las mismas que las de «6 diarias»
   (SEIS con las noticias como etiqueta), que son las que usó salidas_motor.py.
3. El motor: cada entrada pasa por el libro paper en miniatura de salidas_motor.py y replica_ruptura3.py (una posición
   por moneda, ≤ 6 entradas y 3 % de riesgo por día de Madrid, pausa por pérdida del día, 0,5 % de riesgo por operación
   y los controles del paper) con el plan de apertura.impulse_plan y la gestión de apertura.simulate_plan, con los costes
   de los backtests (comisiones.costes_de_backtest: 0,10 % por lado también en los perpetuos y 2 pb) y el funding real de
   Binance en los cortos.
4. La referencia: la variante de salidas_motor.py rehecha aquí con SU código (la salida vectorizada de simular_todo, rama
   F0, y su libro con su comprobación del plan: el de «6 diarias» con el stop y el objetivo de la variante), comprobada
   contra salidas_motor.json (operaciones, R con y sin funding, ganadoras, salidas, horas, largos y cortos, dentro y
   fuera, los vetos del libro y los días con el paper detenido). Después, operación a operación: moneda, lado, señal,
   entrada, salida, motivo, resultado, funding y tamaño.
5. Antes y después de costes (las mismas operaciones sin comisión, sin deslizamiento y sin funding) y los perfiles de
   costes de controles/costes/bruto_neto.py: sin costes, backtest, OKX EU con X-Perps (los de hoy del paper, con las
   mismas operaciones) y OKX EU sin X-Perps (contado 0,35 % y sin cortos: el libro rehecho solo con las compras, con la
   regla del 3 % neto y el tamaño de ese perfil).
6. Escribe BACKTEST_DIR/seis4r_resumen.json (el resumen para la web con la forma de seis2_resumen.json; en «fuera» y
   «dentro», además, «antes_de_costes» y «perfiles», con las claves de bot/ranking.PERFILES_TEXTO) y
   BACKTEST_DIR/controles/replica_seis4r.json y .txt (la comparación, los vetos, los perfiles y las operaciones). Si el
   motor no reproduce la referencia, o la referencia no reproduce salidas_motor.json, NO escribe el resumen (sale con 1).
Uso: M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe -X utf8 scripts\\replica_seis4r.py
"""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import apertura as ap, backtest_aperturas as bt, comisiones, config, paper  # noqa: E402

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
MAD = ZoneInfo("Europe/Madrid")
T, O, HI, LO, C, V = 0, 1, 2, 3, 4, 5
FROM, TO = date(2023, 10, 28), date(2026, 10, 20)                     # las aperturas de salidas_motor.py
SPLIT = int(datetime(2025, 8, 16, tzinfo=MAD).timestamp() * 1000)     # corte dentro/fuera (como seis2_resumen.json)
COINS = list(config.COINS)
SLIP = paper.COST_SLIP_BPS / 1e4
FEE = config.FEE_PCT / 100
RISK = float(config.RISK_PER_TRADE_PCT)
# --preset seis4r (por defecto): «6 diarias 4R», stop 2 %; --preset seis4r3: la misma horquilla con stop 3 % (29-10-2026)
_argp = argparse.ArgumentParser(description="Réplica del motor de «6 diarias 4R» (seis4r) o de su variante con stop 3 % "
                                            "(seis4r3) frente a su variante de salidas_motor.py")
_argp.add_argument("--preset", choices=("seis4r", "seis4r3"), default="seis4r")
PRESET = _argp.parse_args().preset
_CONF = {"seis4r": (ap.SEIS4R, "F0 stop 2 % · objetivo 4R · tope 7d", 2.0, "seis4r_resumen.json", "replica_seis4r"),
         "seis4r3": (ap.SEIS4R3, "F0 stop 3 % · objetivo 4R · tope 7d", 3.0, "seis4r3_resumen.json", "replica_seis4r3")}
_PARAMS, VAR, SL, _RES, _DET = _CONF[PRESET]                         # VAR: la variante de salidas_motor.py
K, CAP_H = 4.0, 168
MAXH = 720                    # la ventana de salidas_motor.py (30 días: el tope más largo de su rejilla)
CTRL = config.BACKTEST_DIR / "controles"
OUT_RES = config.BACKTEST_DIR / _RES
OUT_DET = CTRL / f"{_DET}.json"
LOGF = CTRL / f"{_DET}.txt"
W_STOP, W_OBJ, W_TIEMPO, W_FIN = 0, 1, 4, 6                           # los motivos de salidas_motor.py (rama F0)
WHY_REF = {W_STOP: "stop", W_OBJ: "objetivo", W_TIEMPO: "tiempo", W_FIN: "fin de datos"}
WHY_MOTOR = {"inicial": "stop", "objetivo": "objetivo", "tiempo": "tiempo", "fin de datos": "fin de datos"}
assert config.USER_RULES, "las reglas del usuario tienen que estar activas (BOT_USER_RULES distinto de 0)"
assert RISK == 0.5 and config.FEE_PCT == 0.10 and paper.COST_SLIP_BPS == 2.0
comisiones.reiniciar()
assert comisiones.activo() == comisiones.CON_XPERPS, comisiones.activo()
P, _notes = ap.impulse_params(_PARAMS)                                 # el preset tal cual (veto de noticias fiables)
assert not _notes and ap.preset_of(P) == PRESET, _notes
for _k, _v in (("measure_min", 15), ("min_move_pct", 0.0), ("entry", "t:15"), ("rvol_min", None), ("vol_filter", "todos"),
               ("btc_filter", "ninguno"), ("news_mode", "fiables"), ("sl_pct", SL), ("target_r", K),
               ("max_hold_h", float(CAP_H)), ("tp1_frac", 0.0), ("lock_r", None), ("max_day_entries", 6),
               ("max_day_risk_pct", 3.0), ("long_instrument", "spot"), ("short_instrument", "perp")):
    assert P[_k] == _v, (_k, P[_k])
PS = ap.impulse_params({**ap.SEIS, "news_mode": "etiqueta"})[0]        # el P de salidas_motor.py (entradas y plan)
t0 = time.time()
CTRL.mkdir(parents=True, exist_ok=True)
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


def _n(x: float, k: int = 2) -> str:
    return ap._n(x, k)


def _sg(x: float, k: int = 3) -> str:
    return ap._sg(x, k)


log(f"réplica de {PRESET.upper()} ({ap.PRESETS[PRESET]['nombre']}) · reglas del usuario activas · riesgo {RISK} % · "
    f"{config.FEE_PCT} % por lado · {paper.COST_SLIP_BPS} pb · avg_hold_h del preset {P['avg_hold_h']} h")

# ================================================================ datos (como salidas_motor.py: desde septiembre de 2023)
A = {}
for c in COINS:
    paths = []
    for ym in months("2023-09", "2026-10"):
        z, part = bt.zip_path(bt.SYMS[c], ym), bt.part_path(bt.SYMS[c], ym)
        paths.append(z if z.exists() else part)
    s = bt.load_series([p for p in paths if p.exists()])
    A[c] = {"t": np.frombuffer(s.t, dtype=np.int64).copy(), "o": np.frombuffer(s.cols[0]).copy(),
            "h": np.frombuffer(s.cols[1]).copy(), "l": np.frombuffer(s.cols[2]).copy(),
            "c": np.frombuffer(s.cols[3]).copy(), "v": np.frombuffer(s.cols[4]).copy()}
    del s
FUND = {}
for c in COINS:
    raw = json.loads((config.HIST_DIR / "funding" / f"{c}USDT.json").read_text(encoding="utf-8"))
    rows = raw.get("rows") or raw.get("series") if isinstance(raw, dict) else raw
    FUND[c] = (np.array([int(r[0]) for r in rows], dtype=np.int64), np.array([float(r[1]) for r in rows]))
log(f"velas en {time.time() - t0:.0f} s: " + ", ".join(f"{c} {len(A[c]['t'])} hasta {fecha(int(A[c]['t'][-1]))}"
                                                     for c in COINS))
OPS = sorted((o for o in bt.build_openings(FROM, TO, bt.index_data())
              if o["id"] in ap.LAB_WINDOWS and not ap.special_session(o)), key=lambda o: (o["open_ms"], o["id"]))
JORN = {"dentro": len({o["day"] for o in OPS if o["open_ms"] < SPLIT}),
        "fuera": len({o["day"] for o in OPS if o["open_ms"] >= SPLIT})}
NAT = {"dentro": (date(2025, 8, 16) - date.fromisoformat(OPS[0]["day"])).days,
       "fuera": (TO + timedelta(days=1) - date(2025, 8, 16)).days}
log(f"{len(OPS)} aperturas {OPS[0]['day']} → {OPS[-1]['day']} · jornadas dentro {JORN['dentro']}, fuera "
    f"{JORN['fuera']} · días naturales dentro {NAT['dentro']}, fuera {NAT['fuera']}")


def barras(c: str, a_ms: int, b_ms: int, j0: int | None = None) -> list[list]:
    x = A[c]
    i = int(np.searchsorted(x["t"], a_ms)) if j0 is None else j0
    j = int(np.searchsorted(x["t"], b_ms))
    return [[t_, o_, h_, l_, c_, v_, 0.0, 0.0] for t_, o_, h_, l_, c_, v_ in
            zip(x["t"][i:j].tolist(), x["o"][i:j].tolist(), x["h"][i:j].tolist(), x["l"][i:j].tolist(),
                x["c"][i:j].tolist(), x["v"][i:j].tolist())]


def tasa_en(coin: str, ms: int) -> float | None:
    """Última tasa de funding publicada antes de ms, en % (la que lee el plan, como salidas_motor.py)."""
    ft, fr = FUND[coin]
    j = int(np.searchsorted(ft, ms, "left")) - 1
    return float(fr[j] * 100) if j >= 0 else None


def funding_de(coin: str, side: str, a: int, b: int, th: int = 0) -> list[tuple[int, float]]:
    """Liquidaciones del funding en (a, b] en % del nominal de entrada."""
    ft, fr = FUND[coin]
    i, j = int(np.searchsorted(ft, a, "right")), int(np.searchsorted(ft, b, "right"))
    return [(int(ft[k]), paper.funding_pay(side, 100.0, float(fr[k]) * 100) * (0.5 if th and ft[k] > th else 1.0))
            for k in range(i, j)]


def midnight(ms: int) -> int:
    dd = datetime.fromtimestamp(ms / 1000, MAD).date()
    return int(datetime(dd.year, dd.month, dd.day, tzinfo=MAD).timestamp() * 1000)


def W_PERP(i: str) -> float:
    return 1 / paper.PERP_MAX_LEVERAGE if i == "perp" else 1.0


# ================================================================ el paper en miniatura (el de salidas_motor.py)
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


# ================================================================ entradas: apertura.ImpulseRun con el preset y con SEIS
CTX = {"btc_dir": 0, "atr_pct": None, "prev_react": None}


def entradas(params: dict) -> list[dict]:
    out = []
    for k, o in enumerate(OPS):
        om = o["open_ms"]
        for ci, coin in enumerate(COINS):
            run = ap.ImpulseRun(coin, om, params, dict(CTX), None, [])
            act = None
            for b in barras(coin, om, om + 122 * MIN):
                act = run.feed(b)
                if act or run.done:
                    break
            if act and act.get("action") == "enter":
                out.append({"k": k, "e": k * 4 + ci, "coin": coin, "t": int(act["t"]), "d": act["dir"],
                            "dia": dia_mad(act["t"]), "win": o["id"], "open_ms": om})
    return out


CANDS = entradas(P)
CANDS_SEIS = entradas(PS)
MISMAS = [(x["coin"], x["t"], x["d"]) for x in CANDS] == [(x["coin"], x["t"], x["d"]) for x in CANDS_SEIS]
log(f"{len(CANDS)} entradas candidatas del motor con SEIS4R ({len(CANDS_SEIS)} con SEIS como salidas_motor.py: "
    f"{'las mismas' if MISMAS else 'DISTINTAS'}) · {time.time() - t0:.0f} s")
for x in CANDS:
    a = A[x["coin"]]
    x["j"] = j = int(np.searchsorted(a["t"], x["t"]))
    x["ok"] = j < len(a["t"])
    if x["ok"]:
        x["E"] = float(a["o"][j] * (1 + x["d"] * SLIP))
        x["t_fill"] = int(a["t"][j])
    x["inst"] = ap.impulse_instrument(x["d"], P)


# ================================================================ la referencia: salidas_motor.py, rama F0 de la variante
def salida_ref(x: dict) -> tuple[int, float, int] | None:
    """salidas_motor.simular_todo para «F0 stop 2 % · objetivo 4R · tope 7d» (vectorizada): (salida, ret %, motivo).
    Orden pesimista: el stop antes que el objetivo en la misma vela, y los dos antes que el cierre por tiempo."""
    if not x["ok"]:
        return None
    ss = np.searchsorted
    a, j, d, E = A[x["coin"]], x["j"], x["d"], x["E"]
    jm = int(ss(a["t"], a["t"][j] + MAXH * H + 3 * H))
    tt = a["t"][j:jm]
    if d > 0:
        F, Ad, Op, Cl = a["h"][j:jm], a["l"][j:jm], a["o"][j:jm], a["c"][j:jm]
    else:
        F, Ad, Op, Cl = -a["l"][j:jm], -a["h"][j:jm], -a["o"][j:jm], -a["c"][j:jm]
    n = len(tt)
    cmF = np.maximum.accumulate(F)
    cmNA = np.maximum.accumulate(-Ad)
    ke = int(ss(tt, tt[0] + CAP_H * H - MIN, "left"))
    ex_fin = d * Cl[-1] * (1 - d * SLIP)

    def ret_of(ex: float) -> float:
        return (d * (ex / E - 1) - FEE * (1 + ex / E)) * 100

    r_ = E * SL / 100
    stop = E - d * r_
    ks = int(ss(cmNA, -(d * stop), "left"))
    tp = E + d * r_ * K
    kt = int(ss(cmF, d * tp, "left"))
    if ks < n and ks <= kt and ks <= ke:
        k, ex, why = ks, d * min(d * stop, Op[ks]) * (1 - d * SLIP), W_STOP
    elif kt < n and kt <= ke:
        k, ex, why = kt, tp, W_OBJ
    elif ke < n:
        k, ex, why = ke, d * Cl[ke] * (1 - d * SLIP), W_TIEMPO
    else:
        k, ex, why = n - 1, ex_fin, W_FIN
    return int(tt[k]) + MIN, float(ret_of(ex)), why


for x in CANDS:
    x["ref"] = salida_ref(x)
log(f"salidas de la referencia para {len(CANDS)} candidatos · {time.time() - t0:.0f} s")


def libro_ref() -> tuple[list[dict], Counter, Cartera]:
    """book() de salidas_motor.py con la variante: el plan, el de «6 diarias» con stop 2 % y objetivo 4R."""
    cart, out, no = Cartera(), [], Counter()
    ledger: dict[str, list] = defaultdict(list)
    open_pos: list[dict] = []
    loss_long = paper.stop_loss_frac("long", SL, paper.SPREAD_FALLBACK_BPS / 2 + paper.COST_SLIP_BPS)
    loss_short = paper.stop_loss_frac("short", SL, paper.SPREAD_FALLBACK_BPS / 2 + paper.COST_SLIP_BPS)
    pp = dict(PS, sl_pct=SL, target_r=K)
    for x in CANDS:
        t, coin, d = x["t"], x["coin"], x["d"]
        side = "long" if d > 0 else "short"
        open_pos = [p for p in open_pos if p["tx"] > t]
        if cart.halt_check(t, config.MAX_DAILY_LOSS_PCT):
            no["paper detenido por la pérdida del día"] += 1
            continue
        if any(p["coin"] == coin for p in open_pos):
            no["moneda ocupada"] += 1
            continue
        if x["ref"] is None:
            no["fin de datos"] += 1
            continue
        if not ap.impulse_plan(d, x["E"], pp, tasa_en(coin, t), x["inst"])["ok"]:
            no["plan"] += 1
            continue
        eq = cart.eq_at(t)
        day = ledger[x["dia"]]
        limit = ap.day_limits(len(day), sum(r for r, _ in day) / eq * 100, RISK, PS)
        if limit:
            no["tope de 6 del día" if "entradas" in limit else "riesgo del día"] += 1
            continue
        N = min(eq * RISK / 100 / (loss_long if d > 0 else loss_short), eq * paper.MAX_NOTIONAL_PCT / 100)
        inst = x["inst"]
        expo = sum(p["N"] * W_PERP(p["inst"]) for p in open_pos) + N * W_PERP(inst)
        perp = sum(p["N"] for p in open_pos if p["inst"] == "perp") + (N if inst == "perp" else 0.0)
        why_no = ("4 posiciones abiertas" if len(open_pos) >= config.MAX_POSITIONS else
                  "exposición > 100 %" if expo > eq * paper.MAX_EXPOSURE_PCT / 100 + 1e-6 else
                  "perpetuos > 2 × capital" if inst == "perp" and perp > eq * paper.PERP_MAX_LEVERAGE + 1e-6 else None)
        if why_no:
            no[why_no] += 1
            continue
        ext, ret, why = x["ref"]
        fl = funding_de(coin, side, x["t_fill"], ext) if inst == "perp" else []
        f_pct = sum(v for _, v in fl)
        p = {"coin": coin, "d": d, "te": x["t_fill"], "tx": ext, "E": x["E"], "N": N, "inst": inst, "th": 0, "rh": 0.0,
             "fee_open": N * FEE, "pnl": N * (ret + f_pct) / 100, "fund": fl}
        cart.live.append(p)
        open_pos.append(p)
        day.append((N * SL / 100, x["e"]))
        out.append({"coin": coin, "d": d, "t": t, "t_fill": x["t_fill"], "dia": x["dia"], "win": x["win"], "ext": ext,
                    "ret": ret, "R": ret / SL, "fund_pct": f_pct, "R_f": (ret + f_pct) / SL, "why": WHY_REF[why],
                    "N": N, "inst": inst})
    return out, no, cart


with comisiones.costes_de_backtest():   # como salidas_motor.py (antes de bot/comisiones.py): 0,10 % en todo
    REF, REF_NO, REF_CART = libro_ref()
log(f"referencia (salidas_motor.py): {len(REF)} operaciones · vetos {dict(REF_NO)} · {len(REF_CART.halt_days)} días con "
    f"el paper detenido · {time.time() - t0:.0f} s")


# ================================================================ el motor: impulse_plan + simulate_plan (como el paper)
SIMC: dict = {}


def simular(coin: str, j: int, d: int, plan: dict, hh: float, fee: float | None = None,
            slip: float | None = None) -> dict:
    """apertura.simulate_plan desde la vela j (entrada a su apertura con el deslizamiento), como replica_ruptura3.py."""
    slip = SLIP if slip is None else slip
    key = (coin, j, d, plan["stopPct"], plan["tp1R"], plan["tp2R"], plan["tp1Frac"], plan["lockR"], hh, fee, slip,
           comisiones.activo())
    if key in SIMC:
        return SIMC[key]
    x = A[coin]
    t0_ = int(x["t"][j])
    end = t0_ + int(hh * H)
    full = end + 3 * H
    for lim in sorted({min(full, t0_ + 27 * H), full}):
        bars = barras(coin, t0_, lim, j0=j)
        rr = ap.simulate_plan(bars, 0, d, plan, end, fee=fee, slip=slip)
        if rr["why"] != "fin de datos" or lim >= full:
            break
    out = {"E": bars[0][O] * (1 + d * slip), "t_fill": t0_, "ext": int(rr["t"]), "ret": float(rr["ret"]),
           "why": rr["why"], "partial": bool(rr["partial"]), "th": 0, "rh": 0.0, "locked": bool(rr["locked"])}
    SIMC[key] = out
    return out


def comision_pct(r: dict, plan: dict, d: int, inst: str, perf: dict) -> float:
    """Comisión (% del nominal) de una operación simulada SIN comisión (r["ret"] = bruto con el deslizamiento), como
    controles/costes/bruto_neto.py: la entrada paga tomador; la salida, creador si es el objetivo (orden límite) y
    tomador si es el stop, el cierre por tiempo o el fin de datos. Lo negociado sale del bruto."""
    mk, tk = perf[inst]
    t_fin = 1.0 + d * r["ret"] / 100   # precio de salida / precio de entrada (sin parcial)
    return 100 * (tk + (mk if r["why"] == "objetivo" else tk) * t_fin)


def _perfil(pid: str) -> dict:
    p = comisiones.PERFILES[pid]
    out = {"spot": (p["spot"]["creador"] / 100, p["spot"]["tomador"] / 100)}
    if p.get("perp"):
        out["perp"] = (p["perp"]["creador"] / 100, p["perp"]["tomador"] / 100)
    return out


PERF = {"backtest": {"spot": (FEE, FEE), "perp": (FEE, FEE)},
        "okx_eu_con_xperps": _perfil(comisiones.CON_XPERPS), "okx_eu_sin_xperps": _perfil(comisiones.SIN_XPERPS)}


class Book:
    """Una posición por moneda, topes del día de Madrid, pausa por pérdida del día y controles del paper (4 posiciones,
    exposición ≤ 100 %, perpetuos ≤ 2 × capital), como el libro de salidas_motor.py y replica_ruptura3.Book. perf None:
    la comisión plana del backtest dentro de simulate_plan; con un perfil: se simula sin comisión y se cobra la del
    perfil (comision_pct), también en la marca a mercado (como bruto_neto.Book)."""

    def __init__(self, params: dict, perf: dict | None = None):
        self.params, self.perf = params, perf
        self.cart = Cartera()
        self.open: list[dict] = []
        self.trades: list[dict] = []
        self.ledger: dict[str, list] = defaultdict(list)
        self.no = Counter()

    def entrar(self, x: dict) -> str:
        coin, d, t = x["coin"], x["d"], x["t"]
        a = A[coin]
        side = "long" if d > 0 else "short"
        self.open = [p for p in self.open if p["tx"] > t]
        if self.cart.halt_check(t, config.MAX_DAILY_LOSS_PCT):
            self.no["paper detenido por la pérdida del día"] += 1
            return "tope"
        if any(p["coin"] == coin for p in self.open):
            self.no["moneda ocupada"] += 1
            return "ocupada"
        j = int(np.searchsorted(a["t"], t))
        if j >= len(a["t"]):
            self.no["fin de datos"] += 1
            return "datos"
        inst = ap.impulse_instrument(d, self.params)
        E = float(a["o"][j] * (1 + d * SLIP))
        plan = ap.impulse_plan(d, E, self.params, tasa_en(coin, t), inst)
        if not plan["ok"]:
            self.no["plan"] += 1
            return "plan"
        eq = self.cart.eq_at(t)
        day = self.ledger[dia_mad(t)]
        limit = ap.day_limits(len(day), sum(day) / eq * 100, RISK, self.params)
        if limit:
            self.no["tope de 6 del día" if "entradas" in limit else "riesgo del día"] += 1
            return "tope"
        sl = float(plan["stopPct"])
        loss = paper.stop_loss_frac(side, sl, paper.SPREAD_FALLBACK_BPS / 2 + paper.COST_SLIP_BPS)
        N = min(eq * RISK / 100 / loss, eq * paper.MAX_NOTIONAL_PCT / 100)
        expo = sum(p["N"] * W_PERP(p["inst"]) for p in self.open) + N * W_PERP(inst)
        perp = sum(p["N"] for p in self.open if p["inst"] == "perp") + (N if inst == "perp" else 0.0)
        why_no = ("4 posiciones abiertas" if len(self.open) >= config.MAX_POSITIONS else
                  "exposición > 100 %" if expo > eq * paper.MAX_EXPOSURE_PCT / 100 + 1e-6 else
                  "perpetuos > 2 × capital" if inst == "perp" and perp > eq * paper.PERP_MAX_LEVERAGE + 1e-6 else None)
        if why_no:
            self.no[why_no] += 1
            return "tope"
        hh = float(self.params["max_hold_h"])
        if self.perf is None:
            r = simular(coin, j, d, plan, hh)
            ret, fee_open = r["ret"], N * FEE
        else:
            r = simular(coin, j, d, plan, hh, fee=0.0)
            ret, fee_open = r["ret"] - comision_pct(r, plan, d, inst, self.perf), N * self.perf[inst][1]
        fl = funding_de(coin, side, r["t_fill"], r["ext"]) if inst == "perp" else []
        f_pct = sum(v for _, v in fl)
        p = {"coin": coin, "d": d, "te": r["t_fill"], "tx": r["ext"], "E": r["E"], "N": N, "inst": inst, "th": 0,
             "rh": 0.0, "fee_open": fee_open, "pnl": N * (ret + f_pct) / 100, "fund": fl}
        self.cart.live.append(p)
        self.open.append(p)
        day.append(N * sl / 100)
        self.trades.append({"coin": coin, "d": d, "t": t, "t_fill": r["t_fill"], "dia": dia_mad(t), "win": x["win"],
                            "ext": r["ext"], "ret": ret, "R": ret / sl, "fund_pct": f_pct, "R_f": (ret + f_pct) / sl,
                            "why": WHY_MOTOR.get(r["why"], r["why"]), "N": N, "inst": inst, "sl": sl, "j": j,
                            "plan": plan, "hh": hh, "sim": r})
        return "entra"


def correr(cands: list[dict], params: dict, perf: dict | None = None) -> Book:
    book = Book(params, perf)
    for x in cands:
        book.entrar(x)
    return book


with comisiones.costes_de_backtest():
    BK = correr(CANDS, P)
ROWS = BK.trades
log(f"motor (SEIS4R): {len(ROWS)} operaciones · vetos {dict(BK.no)} · {len(BK.cart.halt_days)} días con el paper "
    f"detenido · {len(SIMC)} simulaciones · {time.time() - t0:.0f} s")


# ================================================================ comparación: referencia frente a salidas_motor.json
def stats(rows: list[dict], seg: str) -> dict:
    """Las cifras de salidas_motor.stats que se comparan con salidas_motor.json."""
    if not rows:
        return {"n": 0}
    R = np.array([r["R"] for r in rows])
    Rf = np.array([r["R_f"] for r in rows])
    w = Counter(r["why"] for r in rows)
    lg, sh = [r["R_f"] for r in rows if r["d"] > 0], [r["R_f"] for r in rows if r["d"] < 0]
    return {"n": len(rows), "op_dia": round(len(rows) / JORN[seg], 3), "R_medio": round(float(R.mean()), 4),
            "R_medio_funding": round(float(Rf.mean()), 4), "gana_pct": round(float((Rf > 0).mean() * 100), 2),
            "salidas": dict(sorted(w.items(), key=lambda kv: -kv[1])), "suma_R": round(float(Rf.sum()), 2),
            "horas_media": round(float(np.mean([(r["ext"] - r["t_fill"]) / H for r in rows])), 1),
            "largos": {"n": len(lg), "R_medio_funding": round(float(np.mean(lg)), 4) if lg else None},
            "cortos": {"n": len(sh), "R_medio_funding": round(float(np.mean(sh)), 4) if sh else None}}


def seg_de(t: int) -> str:
    return "dentro" if t < SPLIT else "fuera"


SM = json.loads((CTRL / "salidas_motor.json").read_text(encoding="utf-8"))
SMV = next(r for r in SM["todas"] if r["id"] == VAR)
KEYS = ("n", "op_dia", "R_medio", "R_medio_funding", "gana_pct", "salidas", "suma_R", "horas_media", "largos", "cortos")
REF_VS_JSON = {}
for seg in ("dentro", "fuera"):
    mine = stats([r for r in REF if seg_de(r["t"]) == seg], seg)
    REF_VS_JSON[seg] = {k: {"json": SMV[seg].get(k), "referencia": mine.get(k), "igual": SMV[seg].get(k) == mine.get(k)}
                        for k in KEYS}
REF_VS_JSON["vetos"] = {"json": SMV.get("vetos"), "referencia": dict(REF_NO),
                        "igual": SMV.get("vetos") == dict(REF_NO)}
REF_VS_JSON["dias_detenido"] = {"json": SMV.get("dias_detenido"), "referencia": len(REF_CART.halt_days),
                                "igual": SMV.get("dias_detenido") == len(REF_CART.halt_days)}
REF_OK = all(v["igual"] for seg in ("dentro", "fuera") for v in REF_VS_JSON[seg].values()) \
    and REF_VS_JSON["vetos"]["igual"] and REF_VS_JSON["dias_detenido"]["igual"]
log(f"REFERENCIA frente a salidas_motor.json ({VAR}): {'IGUAL' if REF_OK else 'DISTINTA'}")
for seg in ("dentro", "fuera"):
    for k, v in REF_VS_JSON[seg].items():
        if not v["igual"]:
            log(f"   {seg} {k}: json {v['json']} · referencia {v['referencia']}")
for k in ("vetos", "dias_detenido"):
    if not REF_VS_JSON[k]["igual"]:
        log(f"   {k}: json {REF_VS_JSON[k]['json']} · referencia {REF_VS_JSON[k]['referencia']}")


# ================================================================ comparación: el motor frente a la referencia, una a una
def clave(r: dict) -> tuple:
    return (r["coin"], r["d"], r["t"], r["t_fill"], r["ext"], r["why"])


def iguales(a: dict, b: dict) -> bool:
    return (clave(a) == clave(b) and abs(a["ret"] - b["ret"]) < 1e-9 and abs(a["fund_pct"] - b["fund_pct"]) < 1e-9
            and abs(a["N"] - b["N"]) <= 1e-9 * max(1.0, abs(b["N"])) and a["inst"] == b["inst"])


def compacta(r: dict) -> dict:
    return {"moneda": r["coin"], "lado": "largo" if r["d"] > 0 else "corto", "apertura": r["win"],
            "entrada": fecha(r["t_fill"]), "salida": fecha(r["ext"]), "motivo": r["why"], "R": round(r["R"], 4),
            "R_funding": round(r["R_f"], 4), "muestra": seg_de(r["t"])}


by_ref = {clave(r)[:3]: r for r in REF}
by_mot = {clave(r)[:3]: r for r in ROWS}
IGUALES = sum(1 for k, r in by_mot.items() if k in by_ref and iguales(r, by_ref[k]))
SOLO_REF = [compacta(by_ref[k]) for k in by_ref if k not in by_mot]
SOLO_MOT = [compacta(by_mot[k]) for k in by_mot if k not in by_ref]
DIST = [{"motor": compacta(by_mot[k]), "referencia": compacta(by_ref[k]),
         "dif_ret": by_mot[k]["ret"] - by_ref[k]["ret"], "dif_N": by_mot[k]["N"] - by_ref[k]["N"]}
        for k in by_mot if k in by_ref and not iguales(by_mot[k], by_ref[k])]
MOT_OK = (len(ROWS) == len(REF) == IGUALES and not SOLO_REF and not SOLO_MOT and not DIST
          and dict(BK.no) == dict(REF_NO) and len(BK.cart.halt_days) == len(REF_CART.halt_days))
COMP = {"variante": VAR, "operaciones_referencia": len(REF), "operaciones_motor": len(ROWS), "iguales": IGUALES,
        "solo_en_la_referencia": SOLO_REF[:50], "solo_en_el_motor": SOLO_MOT[:50], "distintas": DIST[:50],
        "n_solo_en_la_referencia": len(SOLO_REF), "n_solo_en_el_motor": len(SOLO_MOT), "n_distintas": len(DIST),
        "vetos_referencia": dict(REF_NO), "vetos_motor": dict(BK.no),
        "dias_detenido": {"referencia": len(REF_CART.halt_days), "motor": len(BK.cart.halt_days)},
        "entradas_iguales_que_seis": MISMAS, "candidatos": len(CANDS),
        "referencia_frente_a_salidas_motor_json": REF_VS_JSON, "referencia_reproduce_el_json": REF_OK,
        "motor_reproduce_la_referencia": MOT_OK}
COMP["reproduce"] = bool(REF_OK and MOT_OK and MISMAS)
log(f"COMPARACIÓN del motor con la referencia: {IGUALES} de {len(REF)} operaciones iguales (moneda, lado, señal, "
    f"entrada, salida, motivo, resultado, funding, tamaño e instrumento) · solo en la referencia {len(SOLO_REF)} · solo "
    f"en el motor {len(SOLO_MOT)} · distintas {len(DIST)} → {'REPRODUCE' if COMP['reproduce'] else 'NO REPRODUCE'}")
for x in (SOLO_REF[:10] + SOLO_MOT[:10] + DIST[:10]):
    log("   ", json.dumps(x, ensure_ascii=False, default=float))


# ================================================================ antes y después de costes, y los perfiles de costes
def lados(r: dict) -> dict:
    """La misma operación sin comisión ni deslizamiento (bruto) y sin comisión con el deslizamiento (con_desliz), y la
    comisión de cada perfil sobre lo negociado (comision_pct). Comprueba que la del backtest da su resultado."""
    b0 = simular(r["coin"], r["j"], r["d"], r["plan"], r["hh"], fee=0.0, slip=0.0)
    b1 = simular(r["coin"], r["j"], r["d"], r["plan"], r["hh"], fee=0.0, slip=SLIP)
    com = {k: comision_pct(b1, r["plan"], r["d"], r["inst"], PERF[k]) for k in ("backtest", "okx_eu_con_xperps")}
    dif = abs(b1["ret"] - com["backtest"] - r["ret"])
    assert dif < 1e-9, (r["coin"], r["t"], dif)
    return {"bruto": b0["ret"], "con_desliz": b1["ret"], "com": com, "dif": dif}


with comisiones.costes_de_backtest():
    for r in ROWS:
        r["lados"] = lados(r)
log(f"antes de costes: {len(ROWS)} operaciones simuladas otra vez sin comisión y sin deslizamiento · "
    f"{time.time() - t0:.0f} s")


def bloque_costes(rows: list[dict], seg: str) -> dict | None:
    """Como replica_ruptura3.bloque_costes: bruto, deslizamiento, comisiones y funding en R de la operación."""
    if not rows:
        return None
    m = lambda v: float(np.mean(v))   # noqa: E731
    br = m([r["lados"]["bruto"] / r["sl"] for r in rows])
    de = m([r["lados"]["con_desliz"] / r["sl"] for r in rows])
    ne = m([r["ret"] / r["sl"] for r in rows])
    nf = m([r["R_f"] for r in rows])
    n, jor = len(rows), JORN[seg]
    return {"n": n, "R_bruto": round(br, 4), "deslizamiento_R": round(de - br, 4), "comisiones_R": round(ne - de, 4),
            "funding_R": round(nf - ne, 4), "R_neto": round(nf, 4), "costes_R": round(nf - br, 4),
            "gana_bruto_pct": round(100 * sum(1 for r in rows if r["lados"]["bruto"] > 0) / n, 1),
            "gana_neto_pct": round(100 * sum(1 for r in rows if r["R_f"] > 0) / n, 1),
            "stop_medio_pct": round(m([r["sl"] for r in rows]), 3),
            "cuenta_jornada_bruto_pct": round(br * RISK * n / jor, 4),
            "cuenta_jornada_costes_pct": round((nf - br) * RISK * n / jor, 4),
            "cuenta_jornada_neto_pct": round(nf * RISK * n / jor, 4), "op_jornada": round(n / jor, 3)}


def perfil_de(Rs: list[float], seg: str) -> dict:
    """Las cifras de un perfil de costes en un tramo, con las claves de bot/ranking (perfiles)."""
    a = np.array(Rs, dtype=float)
    n = len(a)
    if not n:
        return {"n": 0, "R": None, "gana_pct": None, "op_dia": 0.0, "pct_dia": 0.0, "pct_dia_natural": 0.0,
                "R_total": 0.0}
    R = float(a.mean())
    return {"n": n, "R": round(R, 4), "gana_pct": round(float((a > 0).mean() * 100), 1),
            "op_dia": round(n / JORN[seg], 3), "pct_dia": round(R * RISK * n / JORN[seg], 4),
            "pct_dia_natural": round(R * RISK * n / NAT[seg], 4), "R_total": round(float(a.sum()), 2)}


# sin X-Perps: el libro rehecho solo con las compras, con la regla del 3 % neto y el tamaño de ese perfil (0,35 %)
comisiones.elegir(comisiones.SIN_XPERPS)    # solo en este proceso: el bot no se toca
assert not comisiones.hay_cortos() and abs(paper.cost_rt_pct("spot") - 0.74) < 1e-9
BK_SX = correr([x for x in CANDS if x["d"] > 0], P, PERF["okx_eu_sin_xperps"])
for r in BK_SX.trades:
    b0 = simular(r["coin"], r["j"], r["d"], r["plan"], r["hh"], fee=0.0, slip=0.0)
    r["lados"] = {"bruto": b0["ret"], "con_desliz": r["sim"]["ret"], "com": {}, "dif": 0.0}
comisiones.reiniciar()
log(f"sin X-Perps (solo compras, 0,35 % por lado): {len(BK_SX.trades)} operaciones · vetos {dict(BK_SX.no)} · "
    f"{time.time() - t0:.0f} s")


def perfiles(seg: str) -> dict:
    rows = [r for r in ROWS if seg_de(r["t"]) == seg]
    sx = [r for r in BK_SX.trades if seg_de(r["t"]) == seg]
    return {"sin_costes": perfil_de([r["lados"]["bruto"] / r["sl"] for r in rows], seg),
            "backtest": perfil_de([r["R_f"] for r in rows], seg),
            "okx_eu_con_xperps": perfil_de([(r["lados"]["con_desliz"] - r["lados"]["com"]["okx_eu_con_xperps"]
                                             + r["fund_pct"]) / r["sl"] for r in rows], seg),
            "okx_eu_sin_xperps": perfil_de([r["R"] for r in sx], seg)}


COSTES = {seg: bloque_costes([r for r in ROWS if seg_de(r["t"]) == seg], seg) for seg in ("dentro", "fuera")}
PERFILES = {seg: perfiles(seg) for seg in ("dentro", "fuera")}
log(f"antes y después de costes: {json.dumps(COSTES, ensure_ascii=False)}")
log(f"perfiles de costes: {json.dumps(PERFILES, ensure_ascii=False)}")

# ================================================================ resumen para la web (forma de seis2_resumen.json)
HORAS = {seg: float(np.mean([(r["ext"] - r["t_fill"]) / H for r in ROWS if seg_de(r["t"]) == seg]))
         for seg in ("dentro", "fuera")}
HORAS["tres_anos"] = float(np.mean([(r["ext"] - r["t_fill"]) / H for r in ROWS]))
log(f"duración media: dentro {HORAS['dentro']:.2f} h · fuera {HORAS['fuera']:.2f} h · 3 años {HORAS['tres_anos']:.2f} h "
    f"(avg_hold_h del preset: {P['avg_hold_h']} h)")


def _curva(rows: list[dict]) -> list[list]:
    acc, out = 0.0, []
    for r in sorted(rows, key=lambda r: (r["ext"], r["t"])):
        acc += r["R"]
        out.append([int(r["ext"]), round(acc, 3)])
    return out


def _bloque(rows: list[dict], seg: str) -> dict:
    rs = [r["R"] for r in rows]
    lg_ = [r["R"] for r in rows if r["d"] > 0]
    sh_ = [r["R"] for r in rows if r["d"] < 0]
    mR = lambda v: float(np.mean(v))   # noqa: E731
    return {"n": len(rs), "mean_R": round(mR(rs), 4) if rs else None,
            "win_rate": round(sum(x > 0 for x in rs) / len(rs), 4) if rs else None,
            "salidas": dict(Counter(r["why"] for r in rows)),
            "horas_media": round(mR([(r["ext"] - r["t_fill"]) / H for r in rows]), 1) if rows else None,
            "largos": {"n": len(lg_), "mean_R": round(mR(lg_), 4) if lg_ else None},
            "cortos": {"n": len(sh_), "mean_R": round(mR(sh_), 4) if sh_ else None},
            "desde": int(min(r["t_fill"] for r in rows)) if rows else None,
            "hasta": int(max(r["ext"] for r in rows)) if rows else None, "curva_R": _curva(rows),
            "op_jornada": round(len(rs) / JORN[seg], 3), "stop_medio_pct": round(mR([r["sl"] for r in rows]), 3)
            if rows else None, "antes_de_costes": COSTES[seg], "perfiles": PERFILES[seg]}


dentro = [r for r in ROWS if r["t"] < SPLIT]
fuera = [r for r in ROWS if r["t"] >= SPLIT]
f_out, f_in = _bloque(fuera, "fuera"), _bloque(dentro, "dentro")
f_out.update(paper_R=f_out["mean_R"],
             paper_R_funding=round(float(np.mean([r["R_f"] for r in fuera])), 4) if fuera else None)
f_in["paper_R_funding"] = round(float(np.mean([r["R_f"] for r in dentro])), 4) if dentro else None
_FUERA = ("estrategia", "pb_frac", "brk_tf", "entry_end_min", "target_pct", "min_strength", "session_max_min", "modelo",
          "rp_max_apertura", "rp_stop_max_pct")
params = {k: P[k] for k in ap.IMP_STATUS_KEYS if k in P and k not in _FUERA}
params.update(estrategia="impulso", preset=PRESET)
cd, cf = COSTES["dentro"] or {}, COSTES["fuera"] or {}
pd_, pf_ = PERFILES["dentro"], PERFILES["fuera"]
lg_f, sh_f = [r["R_f"] for r in fuera if r["d"] > 0], [r["R_f"] for r in fuera if r["d"] < 0]
lg_d, sh_d = [r["R_f"] for r in dentro if r["d"] > 0], [r["R_f"] for r in dentro if r["d"] < 0]
_mR = lambda v: float(np.mean(v)) if v else 0.0   # noqa: E731
# su puesto entre las salidas de salidas_motor.py por % de la cuenta por jornada fuera de muestra (el orden del ranking)
PUESTO = 1 + sum(1 for r in SM["todas"] if (r.get("fuera") or {}).get("R_medio_funding") is not None
                 and r["fuera"]["R_medio_funding"] * r["fuera"]["op_dia"]
                 > SMV["fuera"]["R_medio_funding"] * SMV["fuera"]["op_dia"] + 1e-12)
nota = (
    f"Réplica del motor con el preset {PRESET.upper()} («{ap.PRESETS[PRESET]['nombre']}»): las entradas de «6 diarias» "
    f"(apertura.ImpulseRun, impulso "
    f"de los 15 primeros min, cualquier movimiento ≠ 0; entrada a +15 min) y una horquilla pura gestionada como el paper "
    f"(apertura.simulate_plan: stop {_n(SL, 0)} % y objetivo 4R, +{_n(SL * K, 0)} %; como mucho 7 días; "
    f"{_n(config.FEE_PCT)} % de comisión por "
    f"lado y {_n(paper.COST_SLIP_BPS, 0)} pb de deslizamiento), una posición por moneda, ≤ 6 entradas y 3 % de riesgo "
    f"por día de Madrid y los controles del paper. "
    + (f"Coincide operación a operación con BACKTEST_DIR/controles/salidas_motor.py ({VAR}: las {IGUALES} operaciones) "
       f"y da sus mismas cifras. " if COMP["reproduce"] else
       f"NO coincide del todo con salidas_motor.py ({VAR}): {IGUALES} de {len(REF)} operaciones iguales (ver "
       f"controles/{OUT_DET.name}). ")
    + f"mean_R y paper_R van sin funding; paper_R_funding, con el funding real de Binance. La historia no tiene noticias: "
      f"el veto de noticias fiables no actúa. "
    + ("Era la n.º 1 del ranking de estrategias del 29-10-2026" if PRESET == "seis4r" else
       "La eligió el usuario el 29-10-2026 por ser la que más ganó en 3 años con los costes del paper (sin contar E2) y "
       "ganar en los dos tramos, mirando los dos")
    + f": fuera de muestra "
      f"{_sg(f_out['paper_R_funding'], 3)} R por operación con funding en "
      f"{f_out['n']} operaciones ({_n(f_out['op_jornada'], 2)} por jornada: {_sg(pf_['backtest']['pct_dia'], 3)} % de la "
      f"cuenta por jornada con 0,5 % de riesgo), pero dentro {_sg(f_in['paper_R_funding'], 3)} R en {f_in['n']}. Es la "
    + ("mejor" if PUESTO == 1 else f"{PUESTO}.ª")
    + f" fuera de muestra (en % de la cuenta por jornada) de las {len(SM['todas'])} salidas de una rejilla sobre las "
      f"mismas entradas, elegida después de ver los datos: frágil, NO es una ventaja demostrada. Fuera de muestra gana "
      f"por los cortos "
      f"({len(sh_f)}: {_sg(_mR(sh_f), 3)} R con funding); los {len(lg_f)} largos dan {_sg(_mR(lg_f), 3)} R (dentro, "
      f"largos {_sg(_mR(lg_d), 3)} y cortos {_sg(_mR(sh_d), 3)}). Antes de costes las mismas operaciones dan "
      f"{_sg(cd.get('R_bruto') or 0, 3)} R dentro y {_sg(cf.get('R_bruto') or 0, 3)} R fuera; los costes se llevan "
      f"{_n(-(cd.get('costes_R') or 0), 3)} y {_n(-(cf.get('costes_R') or 0), 3)} R por operación. Con los costes de hoy "
      f"del paper (OKX EU con X-Perps): {_sg(pd_['okx_eu_con_xperps']['R'], 3)} R dentro y "
      f"{_sg(pf_['okx_eu_con_xperps']['R'], 3)} R fuera. Sin cuenta X-Perps (0,35 % por lado y sin cortos) sus "
      f"{pf_['okx_eu_sin_xperps']['n']} compras de fuera dan {_sg(pf_['okx_eu_sin_xperps']['R'] or 0, 3)} R "
      f"({_sg(pd_['okx_eu_sin_xperps']['R'] or 0, 3)} dentro). El cierre por tiempo a los 7 días sale con lo que haya "
      f"(la excepción conocida de la regla). Corte dentro/fuera el "
      f"{datetime.fromtimestamp(SPLIT / 1000, MAD).strftime('%d-%m-%Y')}, como seis2_resumen.json.")
resumen = {"generated": int(time.time() * 1000), "split": SPLIT, "sl_pct": P["sl_pct"], "nota": nota, "fuera": f_out,
           "dentro": f_in, "nombre": ap.PRESETS[PRESET]["nombre"], "params": params}
ref6 = json.loads((config.BACKTEST_DIR / "seis2_resumen.json").read_text(encoding="utf-8"))
assert set(resumen) == set(ref6), (set(resumen), set(ref6))
assert set(ref6["fuera"]) <= set(resumen["fuera"]) and set(ref6["dentro"]) - {"paper_R_funding"} <= set(resumen["dentro"])
det = {"generado": datetime.now(MAD).isoformat(timespec="seconds"), "comparacion": COMP, "jornadas": JORN,
       "dias_naturales": NAT, "antes_de_costes": COSTES, "perfiles": PERFILES,
       "perfiles_texto": {"sin_costes": "comisión 0, sin deslizamiento y sin funding (las mismas operaciones)",
                          "backtest": "0,10 % por lado en todo (también perpetuos), 2 pb y el funding real",
                          "okx_eu_con_xperps": "contado 0,10 % tomador / 0,08 % creador y X-Perps 0,05 % / 0,02 % "
                                               "(creador en el objetivo, tomador en lo demás), 2 pb y funding; las "
                                               "mismas operaciones",
                          "okx_eu_sin_xperps": "contado 0,35 % tomador / 0,20 % creador y sin cortos: el libro rehecho "
                                               "solo con las compras, con la regla del 3 % neto y el tamaño de ese "
                                               "perfil"},
       "vetos_sin_xperps": dict(BK_SX.no), "puesto_entre_las_salidas": {"puesto": PUESTO, "de": len(SM["todas"]),
                                                                        "criterio": "% de la cuenta por jornada fuera "
                                                                                    "de muestra (con funding)"},
       "duracion_media_h": {k: round(v, 2) for k, v in HORAS.items()},
       "avg_hold_h_preset": P["avg_hold_h"], "operaciones": [compacta(r) for r in sorted(ROWS, key=lambda r: r["t"])]}
OUT_DET.write_text(json.dumps(det, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
log(f"detalle en {OUT_DET}")
if not COMP["reproduce"]:
    log(f"NO REPRODUCE: no se escribe el resumen ({OUT_RES.name} se queda como estaba)")
    sys.exit(1)
OUT_RES.write_text(json.dumps(resumen, ensure_ascii=False), encoding="utf-8")
log(f"resumen guardado en {OUT_RES}: fuera {f_out['n']} op {f_out['mean_R']:+.4f} R (con funding "
    f"{f_out['paper_R_funding']:+.4f}) · dentro {f_in['n']} op {f_in['mean_R']:+.4f} R (con funding "
    f"{f_in['paper_R_funding']:+.4f}) · hecho en {time.time() - t0:.0f} s")
