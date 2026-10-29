"""El libro del laboratorio vivo (docs/07, apartado 7): la gestión verificada del paper.

Es el libro de BACKTEST_DIR/controles/selectiva/medir_selectiva.py (réplica D de replica_seis.py y salidas_motor.py)
portado con su misma lógica: Cartera (cuenta con marca a mercado y pausa por pérdida del día), simular
(apertura.simulate_plan desde la vela de entrada) y Book.entrar (una posición por moneda, también la mitad que queda;
topes del día de Madrid; controles del paper). Lo que cambia está marcado con ⚑:
- ⚑ Comisiones reales por perfil (bot/comisiones.py): simulate_plan cobra la de TOMADOR del instrumento en todos los
  lados y después se devuelve la diferencia tomador − creador de lo que sale con orden límite (la parcial y el
  objetivo), sobre su nominal negociado. Es lo que cobra el paper. Sin X-Perps no hay cortos (veto «sin_cortos»).
- ⚑ Una operación que acaba «fin de datos» ocupa su moneda pero no cuenta como resultado (como el laboratorio).
- ⚑ Stop por señal (estructural) subido a 1,5 % y vetado por encima del tope de la salida («stop_lejano»).
- ⚑ Una señal vale como mucho hasta la primera vela tras t: si esa vela llega más de 15 min tarde (hueco), no entra.
El canario (canario.py) comprueba que este libro reproduce seis2_resumen.json antes de medir nada."""
import math
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .. import apertura as ap
from .. import comisiones, config, paper
from . import ajustes
from .salidas import Salida

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
T_, O_, HI_, LO_, C_ = 0, 1, 2, 3, 4
SLIP = paper.COST_SLIP_BPS / 1e4
RISK = float(config.RISK_PER_TRADE_PCT)
HUECO_MAX_MS = 15 * MIN
STOP_EXTRA_PB = {"BTC": 5.0, "ETH": 5.0, "SOL": 20.0, "XRP": 20.0}   # estrés de stops (libro USDC de OKX EU)
PERFIL = comisiones.CON_XPERPS
ESTRESES = ("desliz_x2_y_stops", "entrada_1_min_tarde", "sin_xperps", "objetivo_superado_2pb", "contrario")


def dia_mad(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ajustes.MAD).date().isoformat()


def midnight(ms: int) -> int:
    dd = datetime.fromtimestamp(ms / 1000, ajustes.MAD).date()
    return int(datetime(dd.year, dd.month, dd.day, tzinfo=ajustes.MAD).timestamp() * 1000)


@contextmanager
def perfil_activo(perfil: str = PERFIL, reglas: bool = True):
    """El perfil de comisiones y las reglas del usuario dentro del bloque (estado del proceso: la tarea es un proceso
    aparte, así que no afecta al bot). «backtest:0.10» = comisiones.costes_de_backtest(0.10) (el canario)."""
    viejo_reglas = config.USER_RULES
    config.USER_RULES = bool(reglas)
    try:
        if perfil.startswith("backtest"):
            fee = float(perfil.split(":")[1]) if ":" in perfil else None
            with comisiones.costes_de_backtest(fee):
                yield
        else:
            viejo = comisiones._estado.get("elegido")
            ok, why = comisiones.elegir(perfil)
            if not ok:
                raise ValueError(why)
            try:
                yield
            finally:
                comisiones._estado["elegido"] = viejo
    finally:
        config.USER_RULES = viejo_reglas


def tasas(inst: str) -> tuple[float, float]:
    """(tomador, creador) en fracción del perfil activo."""
    t = comisiones.tasas(inst)
    return float(t["tomador"]) / 100, float(t["creador"]) / 100


def W_PERP(i: str) -> float:
    return 1 / paper.PERP_MAX_LEVERAGE if i == "perp" else 1.0


class Datos:
    """Las velas en bruto y el funding de un Mercado, como A y FUND de medir_selectiva.py."""

    def __init__(self, m):
        self.m = m
        self.A = {}
        for c in m.monedas:
            v = m.velas(c)
            self.A[c] = {"t": np.asarray(v.t), "o": np.asarray(v.o), "h": np.asarray(v.h), "l": np.asarray(v.l),
                         "c": np.asarray(v.c), "v": np.asarray(v.v)}
        self.FUND = {c: m.funding(c) for c in m.monedas}
        self._atr: dict = {}

    def barras(self, c: str, a_ms: int, b_ms: int, j0: int | None = None) -> list[list]:
        x = self.A[c]
        i = int(np.searchsorted(x["t"], a_ms)) if j0 is None else j0
        j = int(np.searchsorted(x["t"], b_ms))
        return [[t_, o_, h_, l_, c_, v_, 0.0, 0.0] for t_, o_, h_, l_, c_, v_ in
                zip(x["t"][i:j].tolist(), x["o"][i:j].tolist(), x["h"][i:j].tolist(), x["l"][i:j].tolist(),
                    x["c"][i:j].tolist(), x["v"][i:j].tolist())]

    def tasa_en(self, coin: str, ms: int) -> float | None:
        ft, fr = self.FUND[coin]
        j = int(np.searchsorted(ft, ms, "left")) - 1
        return float(fr[j] * 100) if j >= 0 else None

    def funding_de(self, coin: str, side: str, a: int, b: int, th: int = 0) -> list[tuple[int, float]]:
        """Liquidaciones del funding en (a, b] en % del nominal de entrada (tras la parcial, la mitad)."""
        ft, fr = self.FUND[coin]
        i, j = int(np.searchsorted(ft, a, "right")), int(np.searchsorted(ft, b, "right"))
        return [(int(ft[k]), paper.funding_pay(side, 100.0, float(fr[k]) * 100) * (0.5 if th and ft[k] > th else 1.0))
                for k in range(i, j)]

    def atr_h_pct(self, coin: str, t: int) -> float | None:
        """ATR(14) de 1 h en % al decidir (para el seguimiento trail_atr de la salida C)."""
        if coin not in self._atr:
            from . import indicadores as ind
            b = self.m.barras(coin, 60)
            self._atr[coin] = (np.asarray(b.t_cierre), ind.atr_pct(b.h, b.l, b.c, 14))
        tc, a = self._atr[coin]
        k = int(np.searchsorted(tc, t, "right")) - 1
        return float(a[k]) if k >= 0 and np.isfinite(a[k]) else None


class Cartera:
    """Cuenta del paper con marca a mercado (pausa por pérdida del día), como salidas_motor.py, con la mitad que queda
    tras la parcial (Cartera de medir_selectiva.py)."""

    def __init__(self, datos: Datos, eq0: float = 100.0):
        self.D = datos
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
            x = self.D.A[p["coin"]]
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


@dataclass
class Medida:
    operaciones: list = field(default_factory=list)   # las que cuentan (salida conocida)
    abiertas: list = field(default_factory=list)      # «fin de datos»: ocupan su moneda y no cuentan
    vetos: Counter = field(default_factory=Counter)
    halt_days: list = field(default_factory=list)
    perfil: str = PERFIL
    eq0: float = 100.0

    def cuenta(self) -> dict:
        """Capital al cierre de cada día de Madrid (por la fecha de salida de las operaciones)."""
        e = self.eq0
        out = {}
        for r in sorted(self.operaciones, key=lambda r: (r["ext"], r["t"])):
            e += r["N"] * (r["ret"] + r["fund_pct"]) / 100
            out[dia_mad(r["ext"])] = e
        return out


class Libro:
    """Book.entrar de medir_selectiva.py con la salida de la variante y los ⚑ del módulo."""

    def __init__(self, datos: Datos, salida: Salida, eq0: float = 100.0, slip: float | None = None,
                 stop_extra: bool = False, retraso: int = 0, objetivo_2pb: bool = False):
        self.D = datos
        self.salida = salida
        self.cart = Cartera(datos, eq0)
        self.open: list[dict] = []
        self.trades: list[dict] = []
        self.abiertas: list[dict] = []
        self.ledger: dict[str, list] = defaultdict(list)
        self.no = Counter()
        self.slip = SLIP if slip is None else slip
        self.stop_extra = stop_extra
        self.retraso = retraso
        self.obj2 = objetivo_2pb
        self.seis2 = dict(ap.SEIS2)
        self.simc: dict = {}

    # ---------------------------------------------------------------- simulación de una operación
    def simular(self, coin: str, j: int, d: int, plan: dict, hh: float, ha: float | None, fin_ms: int = 0,
                slip: float | None = None) -> dict:
        """apertura.simulate_plan desde la vela j (entrada a su apertura con el deslizamiento), con las comisiones del
        perfil activo (tomador y la devolución del creador en la parcial y el objetivo)."""
        slip = self.slip if slip is None else slip
        key = (coin, j, d, plan.get("stopPct"), plan.get("tp1R"), plan.get("tp2R"), plan.get("tp1Frac"),
               plan.get("lockR"), plan.get("lockFloorR"), plan.get("lockTrailR"), hh, ha, fin_ms, slip, self.obj2)
        if key in self.simc:
            return self.simc[key]
        x = self.D.A[coin]
        t0_ = int(x["t"][j])
        end = t0_ + int(hh * H)
        if fin_ms and fin_ms < end:   # el tope propio de la señal acota la parte sin activar (el cerrojo sigue el suyo)
            end = max(t0_ + MIN, int(fin_ms))
        end_act = t0_ + int(ha * H) if ha else None
        full = max(end, end_act or 0) + 3 * H
        inst = ap.impulse_instrument(d, self.seis2)
        tom, cre = tasas(inst)
        pl = dict(plan)
        sp = float(pl["stopPct"])
        if self.obj2:   # el objetivo y la parcial solo se cobran si el precio los supera en 2 pb
            for k in ("tp1R", "tp2R"):
                if pl.get(k):
                    pl[k] = float(pl[k]) + 0.02 / sp
        rr, bars = None, None
        for lim in sorted({min(full, t0_ + 27 * H), full}):
            bars = self.D.barras(coin, t0_, lim, j0=j)
            rr = ap.simulate_plan(bars, 0, d, pl, end, fee=tom, slip=slip, end_active_ms=end_act)
            if rr["why"] != "fin de datos" or lim >= full:
                break
        E = bars[0][O_] * (1 + d * slip)
        ru = paper.exit_rules(pl)
        tp1 = E * (1 + d * sp * ru["tp1R"] / 100) if ru["tp1_frac"] and ru["tp1R"] else None
        tp2 = E * (1 + d * sp * ru["tp2R"] / 100) if ru["tp2R"] else None
        dfee = tom - cre
        refund = 0.0
        th, rh = 0, 0.0
        parcial = bool(rr["partial"] and rr.get("partial_t"))
        if parcial:
            frac = float(ru["tp1_frac"])
            th = int(rr["partial_t"])
            refund += dfee * frac * tp1 / E
            rh = (frac * d * (tp1 / E - 1) - cre * frac * tp1 / E) * 100
            if self.obj2:   # se cobra al precio del objetivo original (2 pb peor que el disparo)
                rh -= frac * 0.02
        ret = float(rr["ret"])
        if rr["why"] == "objetivo":
            rem = (1 - float(ru["tp1_frac"])) if parcial else 1.0
            px = tp2 if tp2 is not None else tp1
            if px is not None:
                refund += dfee * rem * px / E
            if self.obj2:
                ret -= rem * 0.02
        if self.obj2 and parcial:
            ret -= float(ru["tp1_frac"]) * 0.02
        ret += refund * 100
        out = {"E": E, "t_fill": t0_, "ext": int(rr["t"]), "ret": ret, "why": rr["why"], "first": rr["first"],
               "partial": bool(rr["partial"]), "th": th, "rh": rh, "locked": bool(rr["locked"]),
               "mfe": float(rr["mfe"]), "mae": float(rr["mae"]), "fee_t": tom, "fee_m": cre, "refund": refund * 100}
        self.simc[key] = out
        return out

    # ---------------------------------------------------------------- una entrada
    def ocupada(self, coin: str, t: int) -> bool:
        return any(p["coin"] == coin and p["tx"] > t for p in self.open)

    def entrar(self, coin: str, d: int, t: int, sl_in: float, objetivo_r: float | None = None, fin_ms: int = 0,
               meta: dict | None = None) -> tuple[str, str | None, dict | None]:
        """(estado, motivo, operación): estado = entra | plan | tope | ocupada | datos | stop | cortos."""
        x = self.D.A[coin]
        side = "long" if d > 0 else "short"
        self.open = [p for p in self.open if p["tx"] > t]
        if self.cart.halt_check(t, config.MAX_DAILY_LOSS_PCT):
            self.no["paper detenido por la pérdida del día"] += 1
            return "tope", "paper detenido por la pérdida del día", None
        if any(p["coin"] == coin for p in self.open):
            self.no["moneda ocupada"] += 1
            return "ocupada", "moneda ocupada", None
        j = int(np.searchsorted(x["t"], t)) + self.retraso
        if j >= len(x["t"]):
            self.no["sin velas"] += 1
            return "datos", "sin velas", None
        if int(x["t"][j]) - t > HUECO_MAX_MS + self.retraso * MIN:
            self.no["sin velas (hueco tras la señal)"] += 1
            return "datos", "sin velas (hueco tras la señal)", None
        info = self.salida.tipo == "INFO"
        if not info:
            sl = max(config.RULE_MIN_STOP_PCT, float(sl_in))
            if sl > self.salida.sl_max + 1e-9:
                self.no["stop_lejano"] += 1
                return "stop", "stop_lejano", None
        else:
            sl = float(sl_in)
        inst = ap.impulse_instrument(d, self.seis2)
        if inst == "perp" and not comisiones.hay_cortos():
            self.no["sin_cortos"] += 1
            return "cortos", "sin_cortos", None
        rate = self.D.tasa_en(coin, t)
        E = float(x["o"][j] * (1 + d * self.slip))
        atr_h = self.D.atr_h_pct(coin, t) if self.salida.s.get("trail_atr") else None
        plan, hh, ha, SL = self.salida.plan(d, E, sl, rate, inst, objetivo_r, atr_h)
        if not plan["ok"]:
            self.no["plan (regla 2R / 3 % neto)"] += 1
            return "plan", plan.get("why"), None
        eq = self.cart.eq_at(t)
        day = self.ledger[dia_mad(t)]
        limit = ap.day_limits(len(day), sum(day) / eq * 100, RISK, self.seis2)
        if limit:
            k = "tope de 6 del día" if "entradas" in limit else "riesgo del día (3 %)"
            self.no[k] += 1
            return "tope", k, None
        tom, _ = tasas(inst)
        loss = paper.stop_loss_frac(side, SL, paper.SPREAD_FALLBACK_BPS / 2 + paper.COST_SLIP_BPS, fee_pct=tom * 100)
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
            return "tope", why_no, None
        r = self.simular(coin, j, d, plan, hh, ha, fin_ms=fin_ms)
        ret = r["ret"]
        if self.stop_extra and r["first"] == "stop":
            ret -= STOP_EXTRA_PB.get(coin, 20.0) / 100 * (0.5 if r["partial"] else 1.0)
        fl = self.D.funding_de(coin, side, r["t_fill"], r["ext"], r["th"]) if inst == "perp" else []
        f_pct = sum(v for _, v in fl)
        p = {"coin": coin, "d": d, "te": r["t_fill"], "tx": r["ext"], "E": r["E"], "N": N, "inst": inst, "th": r["th"],
             "rh": r["rh"], "fee_open": N * tom, "pnl": N * (ret + f_pct) / 100, "fund": fl}
        self.cart.live.append(p)
        self.open.append(p)
        day.append(N * SL / 100)
        tr = {"coin": coin, "d": d, "t": t, "t_fill": r["t_fill"], "dia": dia_mad(t), "ext": r["ext"], "ret": ret,
              "R": ret / SL, "fund_pct": f_pct, "R_f": (ret + f_pct) / SL, "why": r["why"], "first": r["first"],
              "partial": r["partial"], "th": r["th"], "locked": r["locked"], "N": N, "eq": eq, "inst": inst, "sl": SL,
              "sl_in": float(sl_in), "E": r["E"], "j": j, "mfe": r["mfe"], "mae": r["mae"], "rate": rate,
              "objetivo_r": objetivo_r, "fin_ms": int(fin_ms or 0), "refund": r["refund"], **(meta or {})}
        if r["why"] == "fin de datos":   # ⚑ ocupa su moneda y no cuenta
            self.abiertas.append(tr)
        else:
            self.trades.append(tr)
        return "entra", None, tr


def _sl_de(s_pct: float, salida: Salida) -> float | None:
    """Stop de la operación: el fijo de la salida si lo tiene; con «estructural», el de la señal."""
    if salida.tipo == "INFO":
        return 1.5
    if salida.sl_fijo is not None:
        return salida.sl_fijo
    return float(s_pct) if math.isfinite(s_pct) else None


def medir(m, s, salida: Salida | dict, perfil: str = PERFIL, reglas: bool = True, desde_ms: int | None = None,
          hasta_ms: int | None = None, estres: str | None = None, eq0: float = 100.0, datos: Datos | None = None,
          ventanas: tuple | None = None) -> Medida:
    """Las señales s pasan por el libro con la salida de la variante. estres: desliz_x2_y_stops | entrada_1_min_tarde |
    sin_xperps | objetivo_superado_2pb (la misma secuencia de señales con el cambio; contrario va en r_opuesto)."""
    from .familias.base import Senales   # noqa: F401  (tipo)
    sal = salida if isinstance(salida, Salida) else Salida(salida)
    if estres == "sin_xperps":
        perfil = comisiones.SIN_XPERPS
    D = datos or Datos(m)
    med = Medida(perfil=perfil, eq0=eq0)
    with perfil_activo(perfil, reglas):
        lb = Libro(D, sal, eq0=eq0, slip=2 * SLIP if estres == "desliz_x2_y_stops" else None,
                   stop_extra=estres == "desliz_x2_y_stops", retraso=1 if estres == "entrada_1_min_tarde" else 0,
                   objetivo_2pb=estres == "objetivo_superado_2pb")
        ss = s.entre(desde_ms, hasta_ms) if (desde_ms is not None or hasta_ms is not None) else s
        mon = m.monedas
        for i in range(len(ss)):
            sl = _sl_de(float(ss.stop_pct[i]), sal)
            if sl is None:
                lb.no["sin stop (salida con stop estructural y señal sin stop)"] += 1
                continue
            ob = float(ss.objetivo_r[i]) if np.isfinite(ss.objetivo_r[i]) else None
            w = int(ss.ventana[i])
            meta = {"clave": int(ss.clave[i]), "ventana": (ventanas[w] if ventanas and 0 <= w < len(ventanas) else w),
                    "senal": i}
            lb.entrar(mon[int(ss.moneda[i])], int(ss.dir[i]), int(ss.t[i]), sl, ob, int(ss.fin_ms[i]), meta)
        med.operaciones, med.abiertas, med.vetos = lb.trades, lb.abiertas, lb.no
        med.halt_days = list(lb.cart.halt_days)
    return med


def reestres(m, ops: list[dict], salida: Salida | dict, tipo: str, perfil: str = PERFIL, reglas: bool = True,
             datos: Datos | None = None) -> list[dict]:
    """Diagnóstico sobre las MISMAS operaciones, sin volver a pasar por el libro (medir_selectiva.estres): cada una se
    vuelve a simular con el cambio. Devuelve [{t, dia, coin, d, R_f}]."""
    sal = salida if isinstance(salida, Salida) else Salida(salida)
    if tipo == "sin_xperps":
        perfil = comisiones.SIN_XPERPS
    D = datos or Datos(m)
    out = []
    with perfil_activo(perfil, reglas):
        lb = Libro(D, sal, slip=2 * SLIP if tipo == "desliz_x2_y_stops" else None,
                   objetivo_2pb=tipo == "objetivo_superado_2pb")
        for tr in ops:
            coin, d, j, sl = tr["coin"], tr["d"], tr["j"], tr["sl_in"]
            if tipo == "contrario":
                d = -d
            if tipo == "entrada_1_min_tarde":
                j += 1
            inst = ap.impulse_instrument(d, lb.seis2)
            if inst == "perp" and not comisiones.hay_cortos():
                continue
            x = D.A[coin]
            if j >= len(x["t"]):
                continue
            slip = lb.slip
            E = float(x["o"][j] * (1 + d * slip))
            sl_u = max(config.RULE_MIN_STOP_PCT, sl) if sal.tipo != "INFO" else sl
            atr_h = D.atr_h_pct(coin, tr["t"]) if sal.s.get("trail_atr") else None
            plan, hh, ha, SL = sal.plan(d, E, sl_u, tr["rate"], inst, tr.get("objetivo_r"), atr_h)
            if not plan["ok"]:
                out.append({"t": tr["t"], "dia": tr["dia"], "coin": coin, "d": d, "R_f": None, "plan": False})
                continue
            r = lb.simular(coin, j, d, plan, hh, ha, fin_ms=tr.get("fin_ms") or 0)
            if r["why"] == "fin de datos":
                continue
            ret = r["ret"]
            if tipo == "desliz_x2_y_stops" and r["first"] == "stop":
                ret -= STOP_EXTRA_PB.get(coin, 20.0) / 100 * (0.5 if r["partial"] else 1.0)
            fl = D.funding_de(coin, "long" if d > 0 else "short", r["t_fill"], r["ext"], r["th"]) if inst == "perp" else []
            out.append({"t": tr["t"], "dia": tr["dia"], "coin": coin, "d": d, "R_f": (ret + sum(v for _, v in fl)) / SL,
                        "plan": True})
    return out


def compacta(ops: list[dict], corte: int | None = None) -> list[dict]:
    """Operaciones para la web (las 200 últimas fuera, todas las de avance)."""
    f = ajustes.fecha_mad
    return [{"moneda": r["coin"], "lado": "largo" if r["d"] > 0 else "corto", "entrada": f(r["t_fill"]),
             "salida": f(r["ext"]), "t": int(r["t"]), "t_salida": int(r["ext"]), "stop_pct": round(r["sl"], 3),
             "R": round(r["R_f"], 3), "motivo": r["why"] + (" (mitad en 3R antes)" if r["partial"] and r["why"] != "objetivo"
                                                            else ""),
             **({"muestra": "dentro" if r["t"] < corte else "fuera"} if corte else {})}
            for r in sorted(ops, key=lambda r: r["t"])]
