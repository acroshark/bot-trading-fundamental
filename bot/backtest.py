"""Backtest de estrategias con los datos de bot/history.py. Escribe config.BACKTEST_DIR/resultados.json (contrato 3.1).

Familias: encadenamiento entre bolsas, bolsa → cripto, ventanas cripto (las de cripto también con stop dinámico),
noticias del propio bot y flujo minorista. Todas las rentabilidades descuentan config.FEE_PCT por lado.

Veredictos: «funciona» queda para las estrategias operables que, tras comisiones, son significativas con la corrección
de Benjamini-Hochberg por comparaciones múltiples y se mantienen fuera de muestra. Lo que no se puede operar (relaciones
de cierre a cierre o sin la información a tiempo) como mucho llega a «solo_correlacion».

Uso: python -m bot.backtest [--offline]"""
import argparse
import asyncio
import bisect
import json
import logging
import math
import os
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from . import config, history, regions
from .models import now_ms

log = logging.getLogger("backtest")

H = 3600_000
YEAR_MS = 365.25 * 86400_000
OOS_SHARE = 0.30            # el último 30 % de las operaciones queda fuera de muestra
EQUITY_POINTS = 200
TRAIL_MULT = 1.5            # stop dinámico = 1,5 veces el rango medio horario de las 24 h previas
TRAIL_MAX_H = 8             # con stop dinámico la operación dura como mucho una sesión
NEWS_MIN_SCORE = 25         # |dirección| / 100 · magnitud (0-100): umbral de «efecto alto»
STALE_OPEN_MAX = 0.20       # si más del 20 % de las aperturas repiten el cierre anterior, el índice no da apertura
CLOSE_MARGIN_MS = 15 * 60_000   # un cierre se conoce 15 min después (Hong Kong lo fija en subasta hasta las 16:10)
FDR_Q = 0.05                # Benjamini-Hochberg: como mucho un 5 % de falsos positivos entre las que pasan
RERUN_S = 20 * 3600
RETRY_S = 2 * 3600          # si faltó alguna fuente o falló el cálculo, se reintenta antes
NEWS_KEEP_MS = history.HOURLY_DAYS * 86400_000   # noticias que aún caben en las velas 1h
YAHOO_OPEN = "índice Yahoo (sesgo optimista)"
ASIA = ["tokio", "seul", "shanghai", "hongkong"]
WEST = ["frankfurt", "londres", "madrid", "nuevayork", "saopaulo"]
VERDICT_ORDER = {"funciona": 0, "dudosa": 1, "solo_correlacion": 2, "no_funciona": 3, "pocos_datos": 4}
VERDICT_TXT = {"funciona": "funciona", "dudosa": "dudosa", "solo_correlacion": "solo correlación",
               "no_funciona": "no funciona", "pocos_datos": "pocos datos"}
NY = ZoneInfo("America/New_York")
M = regions.MARKETS


# ---------- Textos ----------
def _num(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _signed(x: float, d: int = 1) -> str:
    return ("+" if x >= 0 else "") + _num(x, d)


def _p(p: float) -> str:
    return "< 0,001" if p < 0.001 else _num(p, 3)


# ---------- Estadística ----------
def binom_sf(k: int, n: int, p: float) -> float:
    """P(X ≥ k) con X ~ Binomial(n, p): p-valor unilateral de acertar k veces o más por puro azar."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq, lc = math.log(p), math.log1p(-p), math.lgamma(n + 1)
    terms = [lc - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq for i in range(k, n + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def norm_sf(z: float) -> float:
    """P(Z ≥ z) con Z normal estándar."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def bh_qvalues(ps: list[float]) -> list[float]:
    """q-valores de Benjamini-Hochberg, en el mismo orden que ps: la menor tasa de falsos positivos con la que cada
    prueba pasaría."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q, low = [1.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        low = min(low, ps[i] * m / rank)
        q[i] = min(1.0, low)
    return q


def _downsample(points: list, k: int = EQUITY_POINTS) -> list:
    if len(points) <= k:
        return points
    step = (len(points) - 1) / (k - 1)
    return [points[round(i * step)] for i in range(k)]


def _pool_base(pool: list[tuple[int, object, bool]]):
    """base_after para evaluate: con el universo de casos (ms, clave, acierta), tasa base de cada clave desde el corte."""
    def after(cut: int) -> dict:
        agg: dict = {}
        for t, key, ok in pool:
            if t >= cut:
                a = agg.setdefault(key, [0, 0])
                a[0] += ok
                a[1] += 1
        return {key: h / n for key, (h, n) in agg.items()}
    return after


def evaluate(trades: list[tuple], span_years: float | None = None, base_after=None) -> dict:
    """Métricas del contrato 3.1. Cada operación es (ms, retorno bruto % en el sentido de la operación, probabilidad base
    de que ese sentido acierte[, clave de esa base]). Acierto = retorno bruto > 0; el resto va neto de comisiones.
    base_after(corte_ms) → {clave: base} da la tasa base del propio tramo fuera de muestra (si no, la de toda la muestra).
    p_net es el p-valor unilateral de que la ganancia media neta sea mayor que cero."""
    trades = sorted(trades, key=lambda t: t[0])
    n = len(trades)
    oos0 = {"n": 0, "hit_rate": 0.0, "avg_ret_pct": 0.0, "base_rate": 0.0}
    if not n:
        return {"n": 0, "hit_rate": 0.0, "base_rate": 0.0, "lift_pp": 0.0, "p_value": 1.0, "avg_ret_pct": 0.0,
                "total_ret_pct": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0,
                "trades_per_year": 0.0, "oos": oos0, "equity": [], "p_net": 1.0}
    fee = 2 * config.FEE_PCT
    hits = [t[1] > 0 for t in trades]
    net = [t[1] - fee for t in trades]
    base = sum(t[2] for t in trades) / n
    hit = sum(hits) / n
    eq, peak, dd, curve = 100.0, 100.0, 0.0, [[trades[0][0], 100.0]]
    for t, r in zip(trades, net):
        eq *= 1 + r / 100
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
        curve.append([t[0], round(eq, 3)])
    gains, losses = sum(r for r in net if r > 0), -sum(r for r in net if r < 0)
    pf = gains / losses if losses > 0 else (99.99 if gains > 0 else 0.0)
    years = span_years or (trades[-1][0] - trades[0][0]) / YEAR_MS
    tpy = n / max(years, 1 / 365.25)
    avg = sum(net) / n
    sd = statistics.pstdev(net) if n > 1 else 0.0
    se = statistics.stdev(net) / math.sqrt(n) if n > 1 else 0.0
    p_net = norm_sf(avg / se) if se > 0 else (0.0 if avg > 0 else 1.0)
    k = int(round(n * (1 - OOS_SHARE)))
    o_tr, o_net = trades[k:], net[k:]
    oos = oos0
    if o_tr:
        bm = base_after(o_tr[0][0]) if base_after else {}
        o_base = sum(bm.get(t[3] if len(t) > 3 else None, t[2]) for t in o_tr) / len(o_tr)
        oos = {"n": len(o_tr), "hit_rate": round(sum(hits[k:]) / len(o_tr), 4),
               "avg_ret_pct": round(sum(o_net) / len(o_net), 4), "base_rate": round(o_base, 4)}
    return {"n": n, "hit_rate": round(hit, 4), "base_rate": round(base, 4), "lift_pp": round((hit - base) * 100, 2),
            "p_value": float(f"{binom_sf(sum(hits), n, base):.3g}"), "avg_ret_pct": round(avg, 4),
            "total_ret_pct": round((eq / 100 - 1) * 100, 2), "win_rate": round(sum(1 for r in net if r > 0) / n, 4),
            "profit_factor": round(pf, 3), "max_dd_pct": round(dd, 2),
            "sharpe": round(avg / sd * math.sqrt(tpy), 3) if sd > 0 else 0.0, "trades_per_year": round(tpy, 1),
            "oos": oos, "equity": _downsample(curve), "p_net": float(f"{p_net:.3g}")}


def _tradable(s: dict) -> bool:
    """Operable de verdad: con la información a tiempo y sin ser una relación de cierre a cierre."""
    return bool(s.get("actionable", True)) and (s.get("params") or {}).get("tipo") != "relacion"


def _why_untradable(m: dict) -> str:
    """Por qué una relación firme no se puede operar (abre el verdict_why de «solo_correlacion»)."""
    prm = m.get("params") or {}
    a, b = prm.get("a"), prm.get("b")
    if prm.get("tipo") != "relacion" or a not in M or b not in M:
        return "Hay correlación, pero la información no llega a tiempo para operar."
    A, B = M[a].name, M[b].name
    if prm.get("cierre") == "antes":
        return (f"{A} y {B} se mueven juntas, pero de cierre a cierre: la apertura de {B} ya recoge el cierre de {A}, "
                "así que no se puede operar tal cual; lo que cuenta es la versión operable, de apertura a cierre.")
    if prm.get("cierre") == "a_la_vez":
        return f"{A} y {B} se mueven juntas, pero ambas cierran casi a la vez: no da tiempo a operar."
    return f"{A} y {B} se mueven juntas, pero {A} aún no ha cerrado cuando abre {B}: no da tiempo a operar."


def verdict(m: dict) -> tuple[str, str]:
    """Reglas del contrato 3.1, más estrictas:
    - Operables: para «funciona» además tienen que ganar dinero neto de comisiones (también fuera de muestra), con una
      ganancia neta significativa (params.p_net) y pasar la corrección de Benjamini-Hochberg (params.q_value < FDR_Q).
      Fuera de muestra el acierto tiene que superar la base global y la de ese mismo tramo.
    - Lo que no se puede operar (relaciones de cierre a cierre o sin la información a tiempo) nunca es «funciona»:
      si la relación es firme queda en «solo_correlacion».
    Sin q_value (estrategia evaluada suelta, fuera de compute) no hay corrección."""
    n, hr, base, lift, p = m["n"], m["hit_rate"], m["base_rate"], m["lift_pp"], m["p_value"]
    prm = m.get("params") or {}
    q, p_net = prm.get("q_value"), prm.get("p_net")
    tradable = _tradable(m)
    note = "" if tradable else (" Solo informativa: es una relación de cierre a cierre, no una estrategia operable."
                                if prm.get("tipo") == "relacion" else
                                " Solo informativa: la información no llega a tiempo para operar.")
    if n < 100:
        return "pocos_datos", (f"{'Aún no hay casos' if not n else f'Solo {n} casos'}: hacen falta al menos 100 para "
                               "opinar y 200 para darla por buena." + note)
    head = (f"Acierta el {_num(hr * 100)} % frente a una base del {_num(base * 100)} % "
            f"({_signed(lift)} pp, p {'' if p < 0.001 else '= '}{_p(p)}, n = {n})")
    if p >= 0.05 or lift < 2:
        return "no_funciona", head + ": sin ventaja clara frente al azar." + note
    oos = m["oos"]
    o_hit, o_base = oos["hit_rate"], max(base, oos.get("base_rate") or 0.0)
    fails = []
    if n < 200:
        fails.append("tiene menos de 200 casos")
    if o_hit <= o_base:
        fails.append(f"fuera de muestra no se mantiene ({_num(o_hit * 100)} % frente a una base del {_num(o_base * 100)} %)")
    if tradable:
        if m["avg_ret_pct"] <= 0:
            fails.append(f"no cubre las comisiones ({_num(m['avg_ret_pct'], 3)} % neto por operación)")
        elif oos["avg_ret_pct"] <= 0:
            fails.append("fuera de muestra pierde dinero tras comisiones")
        elif p_net is not None and p_net >= 0.05:
            fails.append(f"la ganancia neta no es significativa (p = {_p(p_net)})")
    if q is not None and q >= FDR_Q:
        fails.append(f"no supera la corrección por comparaciones múltiples (q = {_p(q)} con "
                     f"{prm.get('n_pruebas', '?')} pruebas)")
    if fails:
        return "dudosa", head + ", pero " + " y ".join(fails) + "." + note
    if not tradable:
        return "solo_correlacion", (_why_untradable(m) + " " + head +
                                    f"; se mantiene fuera de muestra ({_num(o_hit * 100)} %).")
    return "funciona", (head + f"; se mantiene fuera de muestra ({_num(o_hit * 100)} %) y gana "
                        f"{_num(m['avg_ret_pct'], 3)} % neto por operación"
                        + (f" (q {'' if q < 0.001 else '= '}{_p(q)} con {prm.get('n_pruebas', '?')} pruebas)"
                           if q is not None else "") + ".")


def judge(s: dict) -> dict:
    """(Re)calcula verdict y verdict_why a partir de las métricas guardadas en la estrategia."""
    s["verdict"], s["verdict_why"] = verdict(s)
    if s["params"].get("apertura") == YAHOO_OPEN and s["verdict"] in ("funciona", "dudosa"):
        s["verdict_why"] += (" Ojo: medida con la apertura del índice en Yahoo, que puede ir retrasada y exagerar la "
                             "ventaja; confirmar con el futuro o un ETF antes de operar.")
    return s


def make_strategy(sid: str, family: str, name: str, description: str, trades: list, *, actionable: bool = True,
                  span_years: float | None = None, params: dict | None = None, base_after=None, **extra) -> dict:
    m = evaluate(trades, span_years, base_after)
    oos, eq, p_net = m.pop("oos"), m.pop("equity"), m.pop("p_net")
    s = {"id": sid, "family": family, "name": name, "description": description, **m, "actionable": actionable,
         "oos": oos, "verdict": "", "verdict_why": "", "equity": eq, "params": {**(params or {}), "p_net": p_net},
         **extra}
    return judge(s)


def _test_p(s: dict) -> float:
    """p-valor que entra en la corrección: en las operables, el peor entre el del acierto y el de la ganancia neta."""
    p = s["p_value"]
    return max(p, s["params"].get("p_net", 1.0)) if _tradable(s) else p


def finalize(strategies: list[dict]) -> None:
    """Corrección de Benjamini-Hochberg (aparte las operables y las solo informativas, con las de 100 casos o más, que
    son las que se juzgan), veredicto final y enlace de cada relación con su versión operable."""
    for group in ([s for s in strategies if _tradable(s)], [s for s in strategies if not _tradable(s)]):
        tested = [s for s in group if s["n"] >= 100]
        for s, q in zip(tested, bh_qvalues([_test_p(s) for s in tested])):
            s["params"]["q_value"] = float(f"{q:.3g}")
            s["params"]["n_pruebas"] = len(tested)
    for s in strategies:
        judge(s)
    _link_relations(strategies)


def _counts(strategies: list[dict]) -> dict:
    return {v: sum(1 for s in strategies if s["verdict"] == v) for v in VERDICT_ORDER}


def rank_key(s: dict) -> tuple:
    """Orden de resultados.json: veredicto, primero lo operable de verdad, luego lo que se conoce a tiempo, p-valor."""
    return VERDICT_ORDER[s["verdict"]], not _tradable(s), not s["actionable"], s["p_value"], -s["lift_pp"]


# ---------- Índices: sesiones alineadas ----------
@dataclass(frozen=True)
class Day:
    open: float | None
    close: float
    cc: float | None      # % de cierre a cierre sobre la sesión anterior de la serie
    oc: float | None      # % de apertura a cierre; None si la apertura no es fiable


def stale_open_share(rows: list[list]) -> float:
    """Proporción de sesiones cuya apertura es prácticamente el cierre anterior (menos de 0,01 % de diferencia)."""
    prev, same, n = None, 0, 0
    for r in rows:
        o, c = r[1], r[4]
        if prev and o:
            n += 1
            same += abs(o / prev - 1) < 0.0001
        prev = c
    return same / n if n else 0.0


def prepare_index(rows: list[list]) -> dict[date, Day]:
    """Filas de history.load_index → sesiones por fecha local. Una apertura que repite el cierre anterior, a cero o
    absurda no se usa para operar (Yahoo la rellena así cuando no la tiene). Si eso pasa en más del 20 % de las
    sesiones (FTSE 100 e Ibovespa en Yahoo), la apertura de ese índice no sirve ningún día."""
    out: dict[date, Day] = {}
    prev = None
    usable = stale_open_share(rows) <= STALE_OPEN_MAX
    for d, o, h, l, c in (r[:5] for r in rows):
        cc = (c / prev - 1) * 100 if prev else None
        if cc is not None and abs(cc) > 25:
            cc = None
        ok = usable and bool(o) and abs(o / c - 1) < 0.2 and o != prev and not (o == c and h == l)
        out[date.fromisoformat(d)] = Day(o if ok else None, c, cc, (c / o - 1) * 100 if ok else None)
        prev = c
    return out


@lru_cache(maxsize=None)
def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def _open_ms(key: str, d: date) -> int:
    return int(regions.open_utc_on(M[key], d).timestamp() * 1000)


@lru_cache(maxsize=None)
def _close_ms(key: str, d: date) -> int:
    return int(regions.close_utc_on(M[key], d).timestamp() * 1000)


def _clear_caches() -> None:
    """Las cachés de fechas solo hacen falta durante un cálculo: en el proceso 24/7 no se quedan llenas."""
    for f in (prev_weekday, _open_ms, _close_ms):
        f.cache_clear()


def known(a: str, a_day: date, b: str, b_day: date) -> bool:
    """¿La sesión a_day de A ya ha cerrado cuando abre B el día b_day? Con CLOSE_MARGIN_MS de margen: un cierre en el
    mismo instante de la apertura (Shanghái y Londres en verano) o que aún sale de la subasta de cierre (Hong Kong,
    hasta las 16:10) no se puede usar."""
    return _close_ms(a, a_day) + CLOSE_MARGIN_MS <= _open_ms(b, b_day)


def align(a: dict[date, Day], b: dict[date, Day], lag: int = 0) -> list[tuple[date, Day, Day]]:
    """Sesiones de B con la de A del mismo día (lag 0) o del día laborable anterior (lag 1, p. ej. Nueva York → Tokio).
    Si alguna de las dos no cotizó ese día, se salta."""
    out = []
    for d in sorted(b):
        ad = d if lag == 0 else prev_weekday(d)
        if ad in a and a[ad].cc is not None and b[d].cc is not None:
            out.append((d, a[ad], b[d]))
    return out


def _leg_change(idx: dict, leg: tuple[str, int], b: str, d: date) -> float | None:
    a, lag = leg
    ad = d if lag == 0 else prev_weekday(d)
    day = idx[a].get(ad)
    if day is None or day.cc is None or not known(a, ad, b, d):
        return None
    return day.cc


def _years(d0: date, d1: date) -> float:
    return max((d1 - d0).days / 365.25, 1 / 365.25)


# ---------- Familia 1: encadenamiento entre bolsas ----------
def _leg_id(leg) -> str:
    return leg[0] if leg[1] == 0 else f"{leg[0]}_ayer"


def _leg_label(leg, alone: bool) -> str:
    name = M[leg[0]].name
    return name if leg[1] == 0 else (f"{name} cerró la sesión anterior" if alone else f"{name} (sesión anterior)")


def _relation(a: str, b: str, pairs: list, up: bool, today: date) -> dict:
    """Relación de cierre a cierre del mismo día. Informativa: nunca es operable tal cual (veredicto como mucho
    «solo_correlacion»). params.cierre dice cuándo se conoce A respecto a B en la fecha de cálculo."""
    sign = 1 if up else -1
    word = "positivo" if up else "negativo"
    actionable = known(a, today, b, today)
    together = abs(_close_ms(a, today) - _close_ms(b, today)) <= H
    when = "antes" if actionable else "a_la_vez" if together else "despues"
    base = sum(1 for _, x, _ in pairs if x * sign > 0) / len(pairs) if pairs else 0.0
    trades = [(_close_ms(b, d), sign * x, base) for y, x, d in pairs if y * sign > 0]
    pool = [(_close_ms(b, d), None, x * sign > 0) for _, x, d in pairs]
    span = _years(pairs[0][2], pairs[-1][2]) if pairs else None
    why = (f"{M[a].name} cierra antes de que abra {M[b].name}: la versión operable es comprar o vender en la apertura."
           if actionable else f"{M[a].name} cierra después de que abra {M[b].name}, así que no sirve para anticipar su apertura.")
    return make_strategy(
        f"encadenamiento-{a}-{b}-{'sube' if up else 'baja'}", "encadenamiento",
        f"Si {M[a].name} cierra en {word}, {M[b].name} cierra en {word}",
        f"Relación de cierre a cierre entre {M[a].index_name} y {M[b].index_name} el mismo día. {why} "
        "Su rentabilidad es hipotética: no se puede comprar al cierre anterior.",
        trades, actionable=actionable, span_years=span, base_after=_pool_base(pool),
        params={"tipo": "relacion", "a": a, "b": b, "lag": 0, "direccion": "sube" if up else "baja", "cierre": when},
        market=b, coin=None, window=f"Sesión de {M[b].name}", side="long" if up else "short")


def _operable(idx: dict, legs: list, b: str, up: bool) -> dict | None:
    """Comprar (o vender) B en su apertura si todas las patas cerraron en positivo (o negativo) y cerrar al cierre de B.
    Solo cuentan los días en que todas las patas ya habían cerrado al abrir B."""
    sign = 1 if up else -1
    total, universe = 0, []
    for d in sorted(idx[b]):
        db = idx[b][d]
        if db.oc is None:
            continue
        total += 1
        vals = [_leg_change(idx, leg, b, d) for leg in legs]
        if any(v is None for v in vals):
            continue
        universe.append((d, all(v * sign > 0 for v in vals), db.oc))
    if not universe:
        return None
    p_dir = sum(1 for _, _, oc in universe if oc * sign > 0) / len(universe)
    trades = [(_open_ms(b, d), sign * oc, p_dir) for d, s, oc in universe if s]
    pool = [(_open_ms(b, d), None, oc * sign > 0) for d, _, oc in universe]
    word = "positivo" if up else "negativo"
    verb = "comprar" if up else "vender"
    alone = len(legs) == 1
    cond = (f"{_leg_label(legs[0], True)} en {word}" if alone and legs[0][1] else
            f"{_leg_label(legs[0], True)} cierra en {word}" if alone else
            f"{' y '.join(_leg_label(l, False) for l in legs)} cierran en {word}")
    share = len(universe) / total if total else 0.0
    return make_strategy(
        f"encadenamiento-{'+'.join(_leg_id(l) for l in legs)}-{b}-{'compra' if up else 'venta'}", "encadenamiento",
        f"Si {cond}, {verb} {M[b].name} en su apertura y cerrar al cierre",
        f"Entrada en la apertura del {M[b].index_name} y salida a su cierre, solo los días en que la información ya se "
        f"conocía ({_num(share * 100, 0)} % de las sesiones). Comisión {_num(config.FEE_PCT, 2)} % por lado. "
        "La apertura es la primera cotización diaria del índice en Yahoo: puede ir retrasada (valores que aún no han "
        "abierto) y exagerar la ventaja de las señales nocturnas; confirmarla con el futuro o un ETF antes de operar.",
        trades, span_years=_years(universe[0][0], universe[-1][0]), base_after=_pool_base(pool),
        params={"tipo": "operable", "legs": [list(l) for l in legs], "b": b, "direccion": "compra" if up else "venta",
                "share_operable": round(share, 3), "apertura": YAHOO_OPEN},
        market=b, coin=None, window=f"Sesión de {M[b].name}", side="long" if up else "short")


def _single_legs(idx: dict, b: str) -> list[tuple[str, int]]:
    """Patas sueltas para B: la sesión del mismo día de A si alguna vez cierra antes de que abra B, y la del día
    anterior si la mayoría de los días A no ha cerrado aún (Nueva York → Tokio, Fráncfort → Nueva York…)."""
    legs = []
    for a in idx:
        if a == b:
            continue
        days = [d for d, _, _ in align(idx[a], idx[b], 0)]
        share = sum(known(a, d, b, d) for d in days) / len(days) if days else 0.0
        if share > 0:
            legs.append((a, 0))
        if share < 0.5:
            legs.append((a, 1))
    return legs


def _combo_legs(singles: list, b: str) -> list[tuple[str, int]]:
    """Para combinar de dos en dos: las del mismo día más Nueva York de la víspera (o todo Occidente para Asia)."""
    lag1 = WEST if b in ASIA else ["nuevayork"]
    return [l for l in singles if l[1] == 0 or (l[0] in lag1 and l[0] != b)]


def _link_relations(strategies: list[dict]) -> None:
    """Cada relación de cierre a cierre enlaza con su versión operable (apertura a cierre) para no confundirlas."""
    by_id = {s["id"]: s for s in strategies}
    for s in strategies:
        p = s["params"]
        if p.get("tipo") != "relacion":
            continue
        side = "compra" if p["direccion"] == "sube" else "venta"
        for cand in (f"encadenamiento-{p['a']}-{p['b']}-{side}", f"encadenamiento-{p['a']}_ayer-{p['b']}-{side}"):
            op = by_id.get(cand)
            if op:
                p["operable_id"] = cand
                s["verdict_why"] += (f" Versión operable ({'mismo día' if '_ayer' not in cand else 'sesión anterior'}, "
                                     f"de apertura a cierre): acierta el {_num(op['hit_rate'] * 100)} % frente al "
                                     f"{_num(op['base_rate'] * 100)} %, {_signed(op['avg_ret_pct'], 3)} % neto por "
                                     f"operación, {VERDICT_TXT[op['verdict']]}.")
                break


def chain_strategies(idx: dict[str, dict[date, Day]], today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    keys = [k for k in M if idx.get(k)]
    out = []
    for a in keys:
        for b in keys:
            if a != b:
                pairs = [(da.cc, db.cc, d) for d, da, db in align(idx[a], idx[b], 0)]
                for up in (True, False):
                    out.append(_relation(a, b, pairs, up, today))
    for b in keys:
        singles = _single_legs({k: idx[k] for k in keys}, b)
        groups = [[l] for l in singles] + [list(c) for c in combinations(_combo_legs(singles, b), 2)]
        for legs in groups:
            for up in (True, False):
                s = _operable(idx, legs, b, up)
                if s:
                    out.append(s)
    _link_relations(out)
    return out


def leadlag_matrix(idx: dict[str, dict[date, Day]], today: date | None = None) -> dict:
    """Matriz del contrato 3.1 (mismo día, de cierre a cierre) y, aparte, la de A del día anterior → B («lag1»)."""
    today = today or datetime.now(timezone.utc).date()
    keys = [k for k in M if idx.get(k)]

    def block(lag: int) -> dict:
        pu, pd, n, ru, rd = ([[0.0] * len(keys) for _ in keys] for _ in range(5))
        for i, a in enumerate(keys):
            for j, b in enumerate(keys):
                pairs = [(da.cc, db.cc) for _, da, db in align(idx[a], idx[b], lag)]
                n[i][j] = len(pairs)
                ups = [y for x, y in pairs if x > 0]
                downs = [y for x, y in pairs if x < 0]
                pu[i][j] = round(sum(1 for y in ups if y > 0) / len(ups), 4) if ups else 0.0
                pd[i][j] = round(sum(1 for y in downs if y < 0) / len(downs), 4) if downs else 0.0
                ru[i][j] = round(sum(ups) / len(ups), 4) if ups else 0.0
                rd[i][j] = round(sum(downs) / len(downs), 4) if downs else 0.0
        return {"p_up_given_up": pu, "p_down_given_down": pd, "n": n, "avg_ret_up_pct": ru, "avg_ret_down_pct": rd}

    same = block(0)
    base = []
    for b in keys:
        cc = [d.cc for d in idx[b].values() if d.cc is not None]
        base.append(round(sum(1 for x in cc if x > 0) / len(cc), 4) if cc else 0.0)
    return {"markets": keys, "names": [M[k].name for k in keys], "p_up_given_up": same["p_up_given_up"],
            "p_down_given_down": same["p_down_given_down"], "base_up": base, "n": same["n"],
            "actionable": [[a != b and known(a, today, b, today) for b in keys] for a in keys],
            "avg_ret_up_pct": same["avg_ret_up_pct"], "avg_ret_down_pct": same["avg_ret_down_pct"],
            "date": today.isoformat(), "lag1": block(1)}


# ---------- Velas 1h de cripto ----------
class Candles:
    """Velas 1h de una moneda. Entradas y salidas en horas en punto: entrar en la vela i es comprar a su apertura;
    salir k horas después es vender al cierre de la vela i+k-1."""

    def __init__(self, rows: list[list]):
        self.t = [int(r[0]) for r in rows]
        self.o = [float(r[1]) for r in rows]
        self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]
        self.c = [float(r[4]) for r in rows]
        self.idx = {t: i for i, t in enumerate(self.t)}
        self._trail: dict = {}
        self._base: dict = {}
        self._s1, self._s2 = [0.0], [0.0]    # sumas acumuladas del rendimiento horario (para la desviación)
        for o, c in zip(self.o, self.c):
            r = (c / o - 1) * 100
            self._s1.append(self._s1[-1] + r)
            self._s2.append(self._s2[-1] + r * r)

    def at(self, ts: int) -> int | None:
        return self.idx.get(ts)

    def contig(self, i: int, j: int) -> bool:
        return 0 <= i <= j < len(self.t) and self.t[j] - self.t[i] == (j - i) * H

    def fixed(self, i: int | None, k: int, d: int) -> float | None:
        """Retorno bruto % de entrar en la vela i y salir k horas después, en el sentido d (+1 largo, −1 corto)."""
        if i is None or not self.contig(i, i + k - 1):
            return None
        return d * (self.c[i + k - 1] / self.o[i] - 1) * 100

    def before(self, i: int | None, k: int) -> float | None:
        """Movimiento % de las k horas previas a la vela i."""
        if i is None or not self.contig(i - k, i - 1):
            return None
        return (self.c[i - 1] / self.o[i - k] - 1) * 100

    def avg_range(self, i: int, look: int = 24) -> float | None:
        if not self.contig(i - look, i - 1):
            return None
        return sum((self.h[j] - self.l[j]) / self.c[j] * 100 for j in range(i - look, i)) / look

    def sigma(self, i: int, look: int = 720) -> float | None:
        """Desviación del rendimiento horario en las `look` horas previas (30 días)."""
        if i < look:
            return None
        s1, s2 = self._s1[i] - self._s1[i - look], self._s2[i] - self._s2[i - look]
        var = s2 / look - (s1 / look) ** 2
        return math.sqrt(var) if var > 0 else None

    def trail(self, i: int | None, kmax: int, d: int, dist_pct: float) -> float | None:
        """Retorno bruto % con stop dinámico a dist_pct del mejor precio desde la entrada; como mucho kmax horas.
        En cada vela se mira primero el stop que venía de antes y luego se actualiza el máximo (o mínimo)."""
        if i is None or not self.contig(i, i + kmax - 1):
            return None
        entry, best, f = self.o[i], self.o[i], dist_pct / 100
        for j in range(i, i + kmax):
            if d > 0:
                stop = best * (1 - f)
                if self.o[j] <= stop:
                    return (self.o[j] / entry - 1) * 100
                if self.l[j] <= stop:
                    return (stop / entry - 1) * 100
                best = max(best, self.h[j])
            else:
                stop = best * (1 + f)
                if self.o[j] >= stop:
                    return -(self.o[j] / entry - 1) * 100
                if self.h[j] >= stop:
                    return -(stop / entry - 1) * 100
                best = min(best, self.l[j])
        return d * (self.c[i + kmax - 1] / entry - 1) * 100

    def trail_auto(self, i: int | None, d: int, kmax: int = TRAIL_MAX_H) -> float | None:
        key = (i, d, kmax)
        if key not in self._trail:
            rng = self.avg_range(i) if i is not None else None
            self._trail[key] = self.trail(i, kmax, d, TRAIL_MULT * rng) if rng else None
        return self._trail[key]

    def base_rates(self, k: int, since: int | None = None) -> tuple[float, float]:
        """Probabilidad de subir y de bajar en k horas desde una hora cualquiera (tasa base para noticias); con `since`,
        solo desde esa hora (tramo fuera de muestra)."""
        key = (k, since)
        if key not in self._base:
            i0 = bisect.bisect_left(self.t, since) if since is not None else 0
            g = [x for x in (self.fixed(i, k, 1) for i in range(i0, len(self.t))) if x is not None]
            self._base[key] = (sum(1 for x in g if x > 0) / len(g), sum(1 for x in g if x < 0) / len(g)) if g else (0.5, 0.5)
        return self._base[key]

    def days(self) -> list[date]:
        if not self.t:
            return []
        d0 = datetime.fromtimestamp(self.t[0] / 1000, timezone.utc).date()
        d1 = datetime.fromtimestamp(self.t[-1] / 1000, timezone.utc).date()
        return [d0 + timedelta(days=k) for k in range((d1 - d0).days + 1)]


def window_start_ms(kind: str, d: date) -> int:
    """Inicio de cada ventana en UTC (con su horario de verano), redondeado a la hora en punto anterior porque las
    velas son de 1 h: la apertura de Nueva York (09:30) entra a las 09:00 de allí."""
    if kind in ("tokio", "londres", "nuevayork"):
        dt = regions.open_utc_on(M[kind], d)
    elif kind == "macro":
        dt = datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY)
    else:   # cierre diario de cripto
        dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) // H * H


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def _crypto(cd: Candles, slots: list, rule, sid: str, family: str, name: str, description: str, **extra) -> dict:
    """slots: (vela de referencia, sentido de la señal, vela de entrada) para cada caso del universo. La tasa base es la
    proporción de casos del universo en que esa regla, entrando en la vela de referencia, acierta en ese sentido."""
    ups = [(cd.t[i], g) for i, g in ((i, rule(i, 1)) for i, _, _ in slots) if g is not None]
    downs = [(cd.t[i], g) for i, g in ((i, rule(i, -1)) for i, _, _ in slots) if g is not None]
    p_up = sum(1 for _, g in ups if g > 0) / len(ups) if ups else 0.5
    p_down = sum(1 for _, g in downs if g > 0) / len(downs) if downs else 0.5
    pool = [(t, 1, g > 0) for t, g in ups] + [(t, -1, g > 0) for t, g in downs]
    trades = []
    for _, d, ie in slots:
        if d and ie is not None:
            g = rule(ie, d)
            if g is not None:
                trades.append((cd.t[ie], g, p_up if d > 0 else p_down, d))
    span = (cd.t[slots[-1][0]] - cd.t[slots[0][0]]) / YEAR_MS if slots else None
    return make_strategy(sid, family, name, description, trades, span_years=span or None,
                         base_after=_pool_base(pool), **extra)


def _rules(cd: Candles, hold: int) -> list[tuple[str, str, object, dict]]:
    fixed = lambda i, d: cd.fixed(i, hold, d)
    trail = lambda i, d: cd.trail_auto(i, d)
    return [("", "", fixed, {"salida": f"{hold} h"}),
            ("-trailing", " con stop dinámico", trail,
             {"salida": "stop dinámico", "stop": "trailing", "trail_mult": TRAIL_MULT, "max_hold_h": TRAIL_MAX_H})]


# ---------- Familia 2: bolsa → cripto ----------
def asia_signal(idx: dict, d: date, t_ms: int, which: str) -> int | None:
    """Signo del cierre de una bolsa asiática ya conocido en t_ms (con CLOSE_MARGIN_MS de margen), o del bloque «asia»:
    al menos 3 bolsas cerradas y 3 o más en el mismo sentido (si no, 0). None si no hay dato."""
    def one(k: str) -> int | None:
        day = idx.get(k, {}).get(d)
        if day is None or day.cc is None or _close_ms(k, d) + CLOSE_MARGIN_MS > t_ms:
            return None
        return _sign(day.cc)

    if which != "asia":
        return one(which)
    signs = [s for s in map(one, ASIA) if s is not None]
    if len(signs) < 3:
        return None
    return 1 if signs.count(1) >= 3 else -1 if signs.count(-1) >= 3 else 0


def stock_to_crypto(idx: dict, candles: dict[str, Candles], daily: dict[str, list] | None = None) -> list[dict]:
    """Cierre de las bolsas asiáticas → cripto en las 4 h desde la apertura de Londres y de Nueva York (velas 1h), y
    cierre de las occidentales → vela diaria siguiente (histórico 1d completo)."""
    out = []
    sigs = [k for k in ASIA if idx.get(k)] + (["asia"] if sum(bool(idx.get(k)) for k in ASIA) >= 3 else [])
    for coin, cd in candles.items():
        if not cd.t:
            continue
        for wkey, wname in (("londres", "Londres"), ("nuevayork", "Nueva York")):
            for sig in sigs:
                slots = []
                for d in cd.days():
                    if d.weekday() >= 5:
                        continue
                    i = cd.at(window_start_ms(wkey, d))
                    if cd.fixed(i, 4, 1) is None:
                        continue
                    s = asia_signal(idx, d, cd.t[i], sig)
                    if s is not None:
                        slots.append((i, s, i))
                if not slots:
                    continue
                who = "Asia (3 bolsas o más)" if sig == "asia" else M[sig].name
                for up in (True, False):
                    want = 1 if up else -1
                    sl = [(i, s if s == want else 0, ie) for i, s, ie in slots]
                    for suf, label, rule, extra in _rules(cd, 4):
                        out.append(_crypto(
                            cd, sl, rule, f"bolsa_a_cripto-{sig}-{coin.lower()}-{wkey}-{'compra' if up else 'venta'}{suf}",
                            "bolsa_a_cripto",
                            f"Si {who} cierra en {'positivo' if up else 'negativo'}, {'comprar' if up else 'vender'} {coin} "
                            f"en la apertura de {wname}{label}",
                            f"Entrada en la hora de apertura de {wname} y salida "
                            f"{'4 h después' if not suf else 'con stop dinámico de 1,5 veces el rango medio horario (máximo 8 h)'}; "
                            "solo cuenta la bolsa si ya había cerrado (con 15 min de margen). Velas 1h de Binance.",
                            params={"senal": sig, "ventana": wkey, "direccion": "compra" if up else "venta", **extra},
                            market=wkey, coin=coin, window=f"Apertura de {wname}", side="long" if up else "short"))
    # Bolsas occidentales de la víspera → vela diaria de cripto (00:00 UTC), con todo el histórico 1d.
    for coin, rows in (daily or {}).items():
        dm = {datetime.fromtimestamp(r[0] / 1000, timezone.utc).date(): (r[0], r[1], r[4]) for r in rows}
        for a in WEST:
            if not idx.get(a):
                continue
            slots = []
            for d, day in sorted(idx[a].items()):
                nd = dm.get(d + timedelta(days=1))
                if day.cc is not None and nd:
                    slots.append((nd[0], _sign(day.cc), (nd[2] / nd[1] - 1) * 100))
            if not slots:
                continue
            for up in (True, False):
                sign = 1 if up else -1
                p = sum(1 for _, _, r in slots if r * sign > 0) / len(slots)
                trades = [(t, sign * r, p) for t, s, r in slots if s == sign]
                pool = [(t, None, r * sign > 0) for t, _, r in slots]
                out.append(make_strategy(
                    f"bolsa_a_cripto-{a}-{coin.lower()}-dia_siguiente-{'compra' if up else 'venta'}", "bolsa_a_cripto",
                    f"Si {M[a].name} cierra en {'positivo' if up else 'negativo'}, {'comprar' if up else 'vender'} {coin} "
                    "a las 00:00 UTC y cerrar 24 h después",
                    f"Vela diaria de Binance que empieza tras el cierre de {M[a].index_name} (todo el histórico diario).",
                    trades, span_years=(slots[-1][0] - slots[0][0]) / YEAR_MS, base_after=_pool_base(pool),
                    params={"senal": a, "ventana": "dia_siguiente", "direccion": "compra" if up else "venta", "salida": "24 h"},
                    market=None, coin=coin, window="Día siguiente (00:00 UTC)", side="long" if up else "short"))
    return out


# ---------- Familias 3 y 4: ventanas cripto, con y sin stop dinámico ----------
CRYPTO_WINDOWS = [("tokio", "Apertura de Tokio", "tokio"), ("londres", "Apertura de Londres", "londres"),
                  ("macro", "Datos macro de EE. UU. (08:30 de Nueva York)", "nuevayork"),
                  ("nuevayork", "Apertura de Nueva York", "nuevayork"), ("cierre", "Cierre diario (00:00 UTC)", None)]
WINDOW_KINDS = {
    "tendencia": ("seguir la tendencia de las 4 h previas durante 2 h",
                  "Si las 4 h previas subieron, compra; si bajaron, vende. Salida a las 2 h."),
    "ruptura": ("ruptura de la primera hora",
                "Si en las 3 h siguientes una vela cierra por encima del máximo (o por debajo del mínimo) de la primera "
                "hora, entra en ese sentido y sale 2 h después."),
    "contra": ("contra-tendencia tras un movimiento de más de 2 desviaciones",
               "Si las 4 h previas se movieron más de 2 desviaciones (30 días de historia), va en contra durante 2 h."),
}


def crypto_windows(idx: dict, candles: dict[str, Candles]) -> list[dict]:
    """Tendencia previa, ruptura de la primera hora y contra-tendencia en cada ventana; cada una con salida fija a 2 h
    y con stop dinámico. Tokio, Londres y Nueva York solo cuentan los días en que esa bolsa cotiza."""
    out = []
    for coin, cd in candles.items():
        if not cd.t:
            continue
        for wkey, wname, mk in CRYPTO_WINDOWS:
            tdays = set(idx[mk]) if mk and idx.get(mk) else None
            slots = {"tendencia": [], "ruptura": [], "contra": []}
            for d in cd.days():
                if mk and (d not in tdays if tdays is not None else d.weekday() >= 5):
                    continue
                i = cd.at(window_start_ms(wkey, d))
                if i is None:
                    continue
                pre = cd.before(i, 4)
                if pre is not None and cd.fixed(i, 2, 1) is not None:
                    slots["tendencia"].append((i, _sign(pre), i))
                    sg = cd.sigma(i)
                    if sg:
                        big = abs(pre) > 2 * sg * 2     # desviación de 4 h = horaria · √4
                        slots["contra"].append((i, -_sign(pre) if big else 0, i))
                if cd.fixed(i + 1, 2, 1) is not None:
                    s, ie = 0, None
                    for j in (i + 1, i + 2, i + 3):
                        if not cd.contig(i, j):
                            break
                        if cd.c[j] > cd.h[i]:
                            s, ie = 1, j + 1
                            break
                        if cd.c[j] < cd.l[i]:
                            s, ie = -1, j + 1
                            break
                    slots["ruptura"].append((i + 1, s, ie))
            for kind, sl in slots.items():
                if not sl:
                    continue
                label, desc = WINDOW_KINDS[kind]
                for suf, tlabel, rule, extra in _rules(cd, 2):
                    out.append(_crypto(
                        cd, sl, rule, f"ventana_cripto-{wkey}-{kind}-{coin.lower()}{suf}", "ventana_cripto",
                        f"{coin} · {wname}: {label}{tlabel}",
                        desc + (" Con stop dinámico de 1,5 veces el rango medio horario (máximo 8 h) en vez de salida fija."
                                if suf else "") + " Velas 1h de Binance.",
                        params={"ventana": wkey, "tipo": kind, **extra},
                        market=mk, coin=coin, window=wname, side="mixto"))
    return out


# ---------- Familia 5: noticias del propio bot ----------
def news_direction(tr: dict) -> tuple[int, float]:
    """Sentido y fuerza (|dirección| / 100 · magnitud) del triaje v2; con el triaje antiguo se aproxima con el
    sentimiento y la importancia."""
    rep = tr.get("repercusion")
    if isinstance(rep, dict) and rep.get("direccion") is not None:
        dv, mag = float(rep["direccion"]), float(rep.get("magnitud") or 0)
    else:
        dv = {"alcista": 50.0, "bajista": -50.0}.get(tr.get("sentimiento"), 0.0)
        mag = float(tr.get("importancia") or 0) * 10
    return _sign(dv), abs(dv) / 100 * mag


def news_observations(evs: list[dict], seen: set | None = None) -> list[list]:
    """Eventos triaged → [hora en punto de entrada, moneda, sentido, historia], una por historia y moneda. Un evento
    malformado (dirección no numérica, formato de otro productor…) se descarta sin parar los demás."""
    seen = set() if seen is None else seen
    obs = []
    for e in evs:
        try:
            tr = (e.get("patch") or {}).get("triage") or {}
            d, score = news_direction(tr)
            if not d or score < NEWS_MIN_SCORE:
                continue
            rep = tr.get("repercusion") if isinstance(tr.get("repercusion"), dict) else {}
            coins = [c for c in (rep.get("activos") or []) if c in config.COINS] or \
                    [c for c in (tr.get("monedas") or e.get("coins") or []) if c in config.COINS]
            t = -(-int(e["ts"]) // H) * H
            key = e.get("story") or e.get("id")
        except Exception as ex:
            log.debug("Noticia descartada en el backtest (%s): %s", str(e)[:80], ex)
            continue
        for c in coins:
            if (key, c) not in seen:
                seen.add((key, c))
                obs.append([t, c, d, key])
    return obs


def news_obs_path() -> Path:
    return config.BACKTEST_DIR / "noticias_obs.json"


def _arr(x) -> list:
    """Lista JSON devuelta por json_extract (o nada)."""
    try:
        v = json.loads(x) if isinstance(x, str) else x
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def read_triaged(db_path, since_ts: int) -> list[dict]:
    """Eventos triaged posteriores a since_ts con solo los campos que usa el backtest. Abre su propia conexión, así
    que sirve desde un hilo sin tocar la del bot."""
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        con.execute("PRAGMA query_only = ON")
        rows = con.execute(
            "SELECT ts, story, id, json_extract(body, '$.patch.triage.repercusion.direccion'), "
            "json_extract(body, '$.patch.triage.repercusion.magnitud'), "
            "json_extract(body, '$.patch.triage.repercusion.activos'), json_extract(body, '$.patch.triage.sentimiento'), "
            "json_extract(body, '$.patch.triage.importancia'), json_extract(body, '$.patch.triage.monedas'), "
            "json_extract(body, '$.coins') FROM events WHERE kind = 'triaged' AND ts > ? ORDER BY ts",
            (int(since_ts),)).fetchall()
    finally:
        con.close()
    out = []
    for ts, story, eid, dv, mag, act, sent, imp, mon, coins in rows:
        tr = {"sentimiento": sent, "importancia": imp, "monedas": _arr(mon)}
        if dv is not None or mag is not None or act is not None:
            tr["repercusion"] = {"direccion": dv, "magnitud": mag, "activos": _arr(act)}
        out.append({"ts": ts, "story": story, "id": eid, "coins": _arr(coins), "patch": {"triage": tr}})
    return out


def update_news_obs(db_path, now: int | None = None) -> list[list]:
    """Acumula en BACKTEST_DIR/noticias_obs.json las observaciones de las noticias nuevas desde la última lectura, para
    no releer la base entera en cada recálculo. Devuelve todas las vigentes (las que aún caben en las velas 1h)."""
    now = now or now_ms()
    floor = now - NEWS_KEEP_MS
    path = news_obs_path()
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cache = {}
    except Exception as e:
        log.warning("noticias_obs.json ilegible, se rehace: %s", e)
        cache = {}
    obs = [o for o in cache.get("obs") or [] if isinstance(o, list) and len(o) == 4 and o[0] >= floor]
    last = int(cache.get("last_ts") or 0)
    if db_path and Path(db_path).exists():
        try:
            evs = read_triaged(db_path, max(last - H, floor))   # 1 h de solape por si entró algo con hora repetida
        except sqlite3.Error as e:
            log.warning("Noticias del backtest: no se pudo leer %s: %s", db_path, e)
            evs = []
        obs += news_observations(evs, {(o[3], o[1]) for o in obs})
        last = max([last] + [int(e["ts"]) for e in evs])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_ts": last, "obs": obs}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return obs


def _db_path(store) -> Path | None:
    """Ruta de bot.db a partir de una ruta o de un Store. Llamar desde el hilo del bot (usa su conexión)."""
    if store is None or isinstance(store, list):
        return None
    if isinstance(store, (str, os.PathLike)):
        return Path(store)
    try:
        row = store.db.execute("PRAGMA database_list").fetchone()
        return Path(row[2]) if row and row[2] else None
    except Exception as e:
        log.warning("Noticias del backtest: no sé dónde está la base de datos: %s", e)
        return None


def news_backtest(store, candles: dict[str, Candles] | None = None) -> list[dict]:
    """Noticias triadas con efecto alto → retorno a 1 h y 4 h de las monedas afectadas, entrando en la primera hora en
    punto tras el triaje. Se llena con el tiempo. `store` es la lista de eventos ya leída, un Store o la ruta de bot.db
    (en los dos últimos casos se lee de forma incremental). Sin operaciones solapadas en la misma moneda."""
    if store is None:
        obs = []
    elif isinstance(store, list):
        obs = news_observations(store)
    else:
        obs = update_news_obs(_db_path(store))
    obs = sorted(obs, key=lambda o: o[0])
    if candles is None:
        candles = {c: Candles(history.load_klines(c, "1h")) for c in config.COINS}
    out = []
    for k in (1, 4):
        trades, busy = [], {}
        for t, c, d, _ in obs:
            cd = candles.get(c)
            if cd is None or t < busy.get(c, 0):
                continue
            g = cd.fixed(cd.at(t), k, d)
            if g is None:
                continue
            busy[c] = t + k * H
            p_up, p_down = cd.base_rates(k)
            trades.append((t, g, p_up if d > 0 else p_down, (c, d)))

        def after(cut: int, k=k) -> dict:
            return {(c, d): cd.base_rates(k, cut)[0 if d > 0 else 1] for c, cd in candles.items() for d in (1, -1)}
        out.append(make_strategy(
            f"noticias-{k}h", "noticias", f"Noticias de efecto alto: seguir su dirección durante {k} h",
            f"Noticias triadas por el bot con |dirección| · magnitud ≥ {NEWS_MIN_SCORE}: compra si son alcistas y vende "
            f"si son bajistas, en cada moneda afectada, desde la hora en punto siguiente y durante {k} h, sin solapar "
            "operaciones en la misma moneda. Se llena con el tiempo: con menos de 30 casos no hay ni primera lectura.",
            trades, base_after=after,
            params={"min_score": NEWS_MIN_SCORE, "horizonte_h": k, "noticias": len({o[3] for o in obs})},
            market=None, coin=None, window=f"{k} h tras la noticia", side="mixto"))
    return out


# ---------- Familia 6: flujo minorista ----------
def flow_strategies(ls: dict[str, list], candles: dict[str, Candles], look: int = 168, min_hist: int = 72) -> list[dict]:
    """Ratio largo/corto horario en extremos (percentil ≥ 90 o ≤ 10 de los 7 días previos, como en market.py) →
    4 h siguientes, siguiendo al minorista o yendo en su contra. Sin operaciones solapadas."""
    out = []
    for coin, rows in ls.items():
        cd = candles.get(coin)
        if not cd or not cd.t or len(rows) <= min_hist:
            continue
        vals = [r[1] for r in rows]
        base_slots = []
        for k, (ts, v) in enumerate(rows):
            hist = vals[max(0, k - look):k]
            if len(hist) < min_hist:
                continue
            pct = sum(1 for x in hist if x <= v) / len(hist) * 100
            i = cd.at(ts // H * H + H)
            if cd.fixed(i, 4, 1) is not None:
                base_slots.append((i, 1 if pct >= 90 else -1 if pct <= 10 else 0))
        for version, mult, label in (("seguir", 1, "seguirlo"), ("contraria", -1, "ir en su contra")):
            sl, busy = [], -1
            for i, ext in base_slots:
                d = ext * mult
                if d and cd.t[i] >= busy:
                    sl.append((i, d, i))
                    busy = cd.t[i] + 4 * H
                else:
                    sl.append((i, 0, None))
            if sl:
                out.append(_crypto(
                    cd, sl, lambda i, d, cd=cd: cd.fixed(i, 4, d), f"flujo-{coin.lower()}-{version}", "flujo",
                    f"{coin} · Minorista en extremo: {label} durante 4 h",
                    "Ratio de cuentas largo/corto de Binance (futuros) por encima del percentil 90 o por debajo del 10 de "
                    "los 7 días previos. Entrada una hora después del dato. Binance solo guarda 30 días: la muestra crece "
                    "con cada descarga.",
                    params={"version": version, "percentiles": [10, 90], "ventana_pct_h": look, "salida": "4 h"},
                    market=None, coin=coin, window="Extremo de largos/cortos", side="mixto"))
    return out


# ---------- Conjunto, guardado y CLI ----------
def compute(store=None, today: date | None = None) -> dict:
    """Calcula todas las familias con la caché de history. Cada familia va aparte: si una falla, las demás siguen.
    `store`: None, lista de eventos, ruta de bot.db o Store (para noticias; véase news_backtest)."""
    today = today or datetime.now(timezone.utc).date()
    try:
        raw = {k: history.load_index(k) for k, m in M.items() if m.index}
        idx = {k: prepare_index(rows) for k, rows in raw.items() if rows}
        stale = [k for k, rows in raw.items() if rows and stale_open_share(rows) > STALE_OPEN_MAX]
        candles = {c: Candles(history.load_klines(c, "1h")) for c in config.COINS}
        daily = {c: history.load_klines(c, "1d") for c in config.COINS}
        ls = {c: history.load_ls(c) for c in config.COINS}
        jobs = [("encadenamiento", lambda: chain_strategies(idx, today)),
                ("bolsa_a_cripto", lambda: stock_to_crypto(idx, candles, daily)),
                ("ventana_cripto", lambda: crypto_windows(idx, candles)),
                ("flujo", lambda: flow_strategies(ls, candles)),
                ("noticias", lambda: news_backtest(store if store is not None else [], candles))]
        strategies, errors = [], []
        for fam, job in jobs:
            try:
                strategies += job()
            except Exception as e:
                log.exception("Familia %s falló", fam)
                errors.append(f"{fam}: {e}")
        finalize(strategies)
        strategies.sort(key=rank_key)
        leadlag = leadlag_matrix(idx, today) if idx else {"markets": [], "p_up_given_up": [], "p_down_given_down": [],
                                                          "base_up": [], "n": [], "actionable": []}
        data = history.summary()
        data["open_unreliable"] = stale     # índices sin apertura fiable: solo sirven como señal y en las relaciones
        return {"generated": now_ms(), "fee_pct": config.FEE_PCT, "fdr_q": FDR_Q,
                "close_margin_min": CLOSE_MARGIN_MS // 60_000, "data": data, "strategies": strategies,
                "leadlag": leadlag, "counts": _counts(strategies),
                "counts_operables": _counts([s for s in strategies if _tradable(s)]), "errors": errors}
    finally:
        _clear_caches()


def results_path() -> Path:
    return config.BACKTEST_DIR / "resultados.json"


def _mtime() -> float | None:
    try:
        return results_path().stat().st_mtime
    except OSError:
        return None


def save(res: dict) -> None:
    path = results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def load_results() -> dict | None:
    try:
        return json.loads(results_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("resultados.json ilegible: %s", e)
        return None


_cache: dict = {"mtime": None, "res": None}


def load_results_cached() -> dict | None:
    """load_results() que solo relee resultados.json (varios MB) cuando cambia. Para la API: llamarla con
    asyncio.to_thread."""
    mt = _mtime()
    if mt is None:
        return None
    if mt != _cache["mtime"]:
        _cache["res"], _cache["mtime"] = load_results(), mt
    return _cache["res"]


def filter_results(res: dict | None, family: str | None = None, verdict_: str | None = None, sid: str | None = None,
                   curves: bool = True) -> dict:
    """Vista para GET /api/backtest: filtra por familia, veredicto o id y, si curves es False, quita las curvas."""
    res = res or {"generated": 0, "data": {}, "strategies": [], "leadlag": {}}
    st = [s for s in res.get("strategies") or [] if (not family or s["family"] == family)
          and (not verdict_ or s["verdict"] == verdict_) and (not sid or s["id"] == sid)]
    if not curves:
        st = [{**s, "equity": []} for s in st]
    return {**res, "strategies": st}


def _compute_and_save(src, errors: list[str]) -> dict:
    res = compute(src)
    res["errors"] = errors + res["errors"]
    save(res)
    return res


async def run(session: aiohttp.ClientSession | None = None, store=None, offline: bool = False) -> dict:
    """Refresca la historia (salvo offline) y calcula y guarda resultados.json en un hilo. `store` puede ser el Store del
    bot, la ruta de bot.db o una lista de eventos: la base se lee en el hilo con su propia conexión y solo lo nuevo."""
    errors = []
    if not offline:
        if session is None:
            async with aiohttp.ClientSession(headers={"User-Agent": config.USER_AGENT}) as s:
                errors = (await history.ensure_history(s))["errors"]
        else:
            errors = (await history.ensure_history(session))["errors"]
    src = store if store is None or isinstance(store, (list, str, os.PathLike)) else _db_path(store)
    return await asyncio.to_thread(_compute_and_save, src, errors)


def summary_message(res: dict, top: int = 5) -> dict:
    """Resumen corto para el bus (el detalle sale por GET /api/backtest)."""
    st = res.get("strategies") or []
    best = [s for s in st if _tradable(s) and s["verdict"] in ("funciona", "dudosa")][:top]
    return {"type": "backtest", "generated": res.get("generated", 0), "n": len(st), "counts": _counts(st),
            "counts_operables": _counts([s for s in st if _tradable(s)]),
            "best": [{k: s.get(k) for k in ("id", "family", "name", "n", "hit_rate", "base_rate", "lift_pp", "avg_ret_pct",
                                            "verdict")} for s in best]}


class Backtester:
    """Tarea del bot. Publica el último resumen guardado al arrancar (y si otro proceso regenera resultados.json) y,
    cuando toca (20 h después del último cálculo, o 2 h si faltó alguna fuente o falló), refresca la historia, calcula y
    guarda en un hilo y publica un resumen «backtest». Mira cada hora; un fallo solo se registra."""
    START_DELAY_S = 30
    CHECK_S = 3600

    def __init__(self, bus, session: aiohttp.ClientSession):
        self.bus = bus
        self.s = session
        self.running = False
        self.next_ms = 0          # cuándo toca recalcular (0: en cuanto se pueda)
        self._mtime = None        # versión de resultados.json ya publicada

    def _schedule(self, res: dict) -> None:
        wait = RETRY_S if res.get("errors") else RERUN_S
        self.next_ms = int(res.get("generated") or 0) + wait * 1000

    async def step(self) -> None:
        mt = _mtime()
        if mt is not None and mt != self._mtime:
            prev = await asyncio.to_thread(load_results)
            self._mtime = mt
            if prev:
                self._schedule(prev)
                self.bus.publish(summary_message(prev))
        if now_ms() >= self.next_ms:
            self.running = True
            res = await run(self.s, getattr(self.bus, "store", None))
            self._mtime = _mtime()
            self._schedule(res)
            self.bus.publish(summary_message(res))

    async def run(self) -> None:
        await asyncio.sleep(self.START_DELAY_S)   # deja arrancar antes lo urgente (noticias y precios)
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Backtest: %s", e)
                self.next_ms = now_ms() + RETRY_S * 1000
            finally:
                self.running = False
            await asyncio.sleep(self.CHECK_S)


def best_and_worst(res: dict, k: int = 10) -> tuple[list[dict], list[dict]]:
    """Mejores: operables por veredicto y Sharpe. Peores: con 100 casos o más, el lift más negativo."""
    act = [s for s in res["strategies"] if _tradable(s) and s["n"] >= 100]
    best = sorted(act, key=lambda s: (VERDICT_ORDER[s["verdict"]], -s["sharpe"]))[:k]
    worst = sorted(act, key=lambda s: (s["lift_pp"], s["avg_ret_pct"]))[:k]
    return best, worst


def _print_summary(res: dict) -> None:
    st = res["strategies"]
    c, co = _counts(st), _counts([s for s in st if _tradable(s)])
    print(f"\nBacktest: {len(st)} estrategias · " + " · ".join(f"{VERDICT_TXT[v]} {c[v]}" for v in VERDICT_ORDER))
    print(f"Operables: {sum(co.values())} · " + " · ".join(f"{VERDICT_TXT[v]} {co[v]}" for v in VERDICT_ORDER
                                                          if v != "solo_correlacion"))
    fams: dict[str, int] = {}
    for s in st:
        fams[s["family"]] = fams.get(s["family"], 0) + 1
    print("Por familia: " + ", ".join(f"{f} {n}" for f, n in fams.items()))
    d = res["data"]
    print(f"Datos: {len(d['indices'])} índices, {len(d['coins'])} monedas (1d), {len(d['coins_1h'])} (1h), "
          f"{len(d['flow'])} con ratio L/S")
    if d.get("open_unreliable"):
        print("Sin apertura fiable en Yahoo (solo señal y relaciones): " + ", ".join(M[k].index_name for k in d["open_unreliable"]))
    for e in res.get("errors", []):
        print("  Error:", e)
    best, worst = best_and_worst(res)
    line = lambda s: (f"  {s['verdict']:<11} n={s['n']:<5} acierto {_num(s['hit_rate'] * 100)} % base "
                      f"{_num(s['base_rate'] * 100)} % lift {_signed(s['lift_pp'])} pp p={s['p_value']:.3g} "
                      f"q={s['params'].get('q_value', 1):.3g} {_signed(s['avg_ret_pct'], 3)} %/op  {s['name']}  [{s['id']}]")
    print("\n10 mejores (operables):")
    for s in best:
        print(line(s))
    print("\n10 peores (operables, n ≥ 100):")
    for s in worst:
        print(line(s))
    print(f"\nGuardado en {results_path()}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m bot.backtest", description="Backtest de estrategias del bot")
    ap.add_argument("--offline", action="store_true", help="usa solo la caché de históricos, sin descargar")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = config.DB_PATH if config.DB_PATH.exists() else None   # noticias: se lee la base con su propia conexión
    res = asyncio.run(run(store=db, offline=args.offline))
    _print_summary(res)


if __name__ == "__main__":
    main()
