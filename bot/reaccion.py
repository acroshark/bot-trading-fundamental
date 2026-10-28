"""Reacción de la cripto en cada apertura de bolsa (contrato 2.14, mensaje «cripto_aperturas»). Solo mide: no opera ni
predice.

Al usuario no le importa el índice de cada bolsa, sino cómo se mueven BTC, ETH, SOL y XRP cuando abre. Para cada bolsa de
regions.MARKETS (las mismas claves que la web):
- el estado de su sesión: «en_curso» las 4 primeras horas desde la apertura (la ventana de reacción de la cripto),
  «terminada» después y hasta la siguiente apertura, y «por_abrir» si no abrió en las últimas 24 h o hoy no tiene sesión;
- lo que lleva cada moneda desde la apertura (en curso) o lo que hizo en sus 4 h (terminada): rendimiento, rango
  (máximo − mínimo sobre la apertura), máximo, mínimo y la curva (≤ 60 puntos); y la anterior apertura completa. Un
  resultado «a las 4 h» (terminada y «last») solo sale si las velas llegan hasta 5 min antes de las 4 h, como en la
  historia: con un hueco de velas la moneda no sale (nunca un valor a medias) y «aviso» lo explica;
- «hist»: lo que hizo la cripto en esa apertura en los últimos 3 años como mucho (sube a las 4 h, rendimiento y rango
  medios, rango ≥ 2 % a las 4 h y a las 8 h), con su propia hora de apertura.
Y «chain»: si una apertura deja la cripto en positivo a las 4 h, ¿sube en la siguiente? Medido SIN mirar el futuro (la
siguiente es la primera que empieza cuando ya se sabe ese resultado): NO predice, y así se dice en «nota».

- Horarios: regions (hora local con zoneinfo: cambio de hora incluido) y apertura.market_open_on (fines de semana y
  festivos de 2026-2027); en la historia, además, los festivos que delatan los índices diarios
  (backtest_aperturas.index_data y _trades_on). São Paulo el miércoles de ceniza abre a las 13:00: en vivo se usa esa
  hora y en la historia esa sesión no cuenta (como el laboratorio). Dubái no es una ventana del motor (window null) y no
  tiene calendario de festivos: su historia cuenta todos los días laborables.
- Las cifras de la historia y del encadenamiento reproducen exactamente las de los scripts de referencia del 28-10-2026
  (oscilacion.py y encadenamiento.py, fuera del repositorio; docs/05, sección 18): mismas aperturas
  (backtest_aperturas.build_openings, ventanas del laboratorio y sin sesiones especiales), mismas reglas de cobertura y
  el solape tratado igual.
- Velas de 1 min en vivo: las que ya tiene el bot (la vigilancia en memoria más la caché de HIST_DIR/1m, con
  aprendizaje.CacheBars); este módulo no pide nada a Binance.
- hist y chain se calculan en un hilo al arrancar y una vez al día desde HIST_DIR/1m y se guardan en
  BACKTEST_DIR/reaccion_aperturas.json (con menos de 24 h se reutilizan al arrancar).
- Cualquier fallo queda en el registro y el mensaje sale sin esa parte: nunca tumba el bot."""
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import apertura, config, regions
from . import backtest_aperturas as bta
from .models import now_ms

log = logging.getLogger("reaccion")

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
COINS = list(config.COINS)
REACT_H = 4                 # ventana de reacción de la cripto tras cada apertura
LONG_H = 8                  # el rango también a 8 h
RANGE_PCT = 2.0             # «oscila más de un 2 %»
RECENT_H = 24               # «por_abrir» si la última apertura tiene más de 24 h
OPEN_TOL_MS = 5 * MIN       # la primera vela tiene que abrir como mucho 5 min después de la apertura
COVER_PER_H = 50            # oscilacion.py: al menos 50 velas por hora para medir el rango
CHAIN_MIN_GAP_MS = 30 * MIN  # encadenamiento.py: dos aperturas a menos de 30 min no dan tiempo a saber nada
SPARK_MAX = 60
TICK_S = 60
LOOKBACK_D = 12             # velas en vivo hacia atrás (la última completa tras la Semana Dorada china)
SCAN_D = 16                 # días que se miran hacia atrás y hacia delante para encontrar sesiones
HIST_FILE = "reaccion_aperturas.json"
HIST_MAX_AGE_MS = DAY
HIST_YEARS = 3              # regla del usuario: como mucho 3 años de historia
HIST_RETRY_S = 3600         # tras un fallo del cálculo de la historia, se reintenta a la hora
VERSION = 2                 # 2: nota del solape corregida (30 a 90 min según la época del año)
ERR_LOG_S = 300
# Ventana del motor de cada bolsa (la de apertura.openings_on cuando abren a la vez); Dubái solo es referencia.
WINDOW_OF = {"tokio": "tokio-seul", "seul": "tokio-seul", "shanghai": "china", "hongkong": "china",
             "frankfurt": "europa", "londres": "europa", "madrid": "europa", "nuevayork": "nuevayork",
             "saopaulo": "saopaulo", "dubai": None}
NOTA_DUBAI = "Sin calendario de festivos de Dubái: la historia cuenta todos los días laborables."


def _r(x, d: int = 3):
    return None if x is None else round(float(x), d)


def _pc(x: float) -> str:
    return f"{x * 100:.1f}".replace(".", ",") + " %"


# ---------------------------------------------------------------- horarios (puros)

def local_day(key: str, ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(regions.MARKETS[key].tz)).date()


def especial(key: str, day: date) -> bool:
    """Sesión con horario especial (São Paulo el miércoles de ceniza abre a las 13:00)."""
    return apertura.special_session({"id": key, "day": day.isoformat()})


def sesion(key: str, day: date, idx: dict | None = None) -> bool:
    """¿La bolsa abre ese día (fecha local)? Lunes a viernes sin festivo (apertura.market_open_on) y, con idx (índices
    diarios de backtest_aperturas.index_data), sin los festivos que delata la historia."""
    if not apertura.market_open_on(key, day):
        return False
    return idx is None or bta._trades_on(idx, key, day)


def open_ms_on(key: str, day: date) -> int:
    """Apertura en ms UTC de la sesión de ese día local (con su cambio de hora; São Paulo especial a las 13:00)."""
    m = regions.MARKETS[key]
    if especial(key, day):
        return int(datetime(day.year, day.month, day.day, 13, 0, tzinfo=ZoneInfo(m.tz)).timestamp() * 1000)
    return int(regions.open_utc_on(m, day).timestamp() * 1000)


def ventana_de(key: str, day: date) -> str | None:
    """Ventana del motor en la que abre la bolsa ese día (apertura.openings_on); None para Dubái."""
    if key not in apertura.TRADED:
        return None
    for o in apertura.openings_on(day):
        if key in o["markets"]:
            return o["id"]
    return WINDOW_OF.get(key)


def sesiones(key: str, now: int, idx: dict | None = None) -> tuple[list[int], int | None]:
    """(aperturas ya pasadas de los últimos SCAN_D días, de la más reciente a la más antigua; siguiente apertura)."""
    d0 = local_day(key, now)
    past, nxt = [], None
    for k in range(SCAN_D):
        d = d0 - timedelta(days=k)
        if sesion(key, d, idx):
            t = open_ms_on(key, d)
            if t <= now:
                past.append(t)
    for k in range(SCAN_D):
        d = d0 + timedelta(days=k)
        if sesion(key, d, idx):
            t = open_ms_on(key, d)
            if t > now:
                nxt = t
                break
    return sorted(past, reverse=True), nxt


def estado_sesion(key: str, now: int, last: int | None, idx: dict | None = None) -> str:
    """«en_curso» de 0 a 4 h desde la apertura; «terminada» pasadas 4 h y hasta la siguiente apertura; «por_abrir» si no
    abrió en las últimas 24 h o si hoy (fecha local) no hay sesión."""
    if last is not None and 0 <= now - last < REACT_H * H:
        return "en_curso"
    if last is not None and now - last < RECENT_H * H and sesion(key, local_day(key, now), idx):
        return "terminada"
    return "por_abrir"


# ---------------------------------------------------------------- medida en vivo (pura)

def medida(a, open_ms: int, end_ms: int, spark: bool = True, completa: bool = False) -> dict | None:
    """Velas de 1 min [t, o, h, l, c, …] que abren en [open_ms, end_ms) → rendimiento, rango, máximo y mínimo en %
    sobre la apertura (la de la primera vela, que tiene que abrir como mucho 5 min después) y la curva (≤ 60 puntos:
    [ms del cierre, % desde la apertura], empezando en [open_ms, 0]). None sin velas. completa: además, la última vela
    tiene que abrir como mucho 5 min antes de end_ms (como _ret en la historia) y hacen falta al menos COVER_PER_H velas
    por hora, como _rango_pct (un hueco en medio daría un rango a medias): un resultado «a las 4 h» de verdad."""
    if a is None or not len(a):
        return None
    a = np.asarray(a, float)
    t = a[:, 0]
    i, j = int(np.searchsorted(t, open_ms, "left")), int(np.searchsorted(t, end_ms, "left"))
    if j <= i or t[i] - open_ms > OPEN_TOL_MS:
        return None
    if completa and (end_ms - t[j - 1] > OPEN_TOL_MS or j - i < (end_ms - open_ms) / H * COVER_PER_H):
        return None
    seg = a[i:j]
    ref = float(seg[0, 1])
    if not ref > 0:
        return None
    c = seg[:, 4]
    hi, lo = float(seg[:, 2].max()), float(seg[:, 3].min())
    out = {"ret_pct": _r((c[-1] / ref - 1) * 100), "range_pct": _r((hi - lo) / ref * 100),
           "high_pct": _r((hi / ref - 1) * 100), "low_pct": _r((lo / ref - 1) * 100)}
    if spark:
        n = len(seg)
        pick = np.unique(np.linspace(0, n - 1, min(n, SPARK_MAX - 1)).round().astype(int))
        out["spark"] = [[int(open_ms), 0.0]] + [[int(seg[k, 0]) + MIN, _r((c[k] / ref - 1) * 100, 2)] for k in pick]
    return out


def mercado(key: str, now: int, bars: dict, hist: dict | None = None, idx: dict | None = None) -> dict:
    """Entrada de un mercado en «cripto_aperturas» (contrato 2.14) con las velas de cada moneda."""
    past, nxt = sesiones(key, now, idx)
    last = past[0] if past else None
    st = estado_sesion(key, now, last, idx)
    shown = last if st in ("en_curso", "terminada") else None
    coins, avisos = None, []
    if shown is not None:
        if st == "en_curso":
            got = {c: medida(bars.get(c), shown, now + 1) for c in COINS}
        else:
            got, faltan = _bloque_4h(bars, shown, True)
            if faltan:
                avisos.append(f"Faltan velas de {', '.join(faltan)} para completar las 4 h de esta apertura: no se "
                              f"enseña un resultado a medias.")
        coins = {c: v for c, v in got.items() if v} or None
    prev = next((t for t in past if t != shown and t + REACT_H * H <= now), None)
    last_blk = None
    if prev is not None:
        got, faltan = _bloque_4h(bars, prev, False)
        if faltan:
            avisos.append(f"Faltan velas de {', '.join(faltan)} para completar las 4 h de la apertura anterior.")
        last_blk = {"open_ms": prev, "coins": {c: {"ret_4h_pct": v["ret_pct"], "range_4h_pct": v["range_pct"]}
                                               for c, v in got.items() if v} or None}
    ref = shown or nxt or last or now
    out = {"name": regions.MARKETS[key].name, "window": ventana_de(key, local_day(key, ref)), "state": st,
           "open_ms": last, "next_open_ms": nxt,
           "minutes_since_open": int((now - last) // MIN) if shown is not None else None,
           "coins": coins, "last": last_blk, "hist": (hist or {}).get(key)}
    if avisos:
        out["aviso"] = " ".join(avisos)
    return out


def _bloque_4h(bars: dict, t0: int, spark: bool) -> tuple[dict, list[str]]:
    """Las 4 h desde t0 de cada moneda, solo si están completas; y las monedas con velas que no llegan a las 4 h."""
    got, faltan = {}, []
    for c in COINS:
        v = medida(bars.get(c), t0, t0 + REACT_H * H, spark=spark, completa=True)
        if v:
            got[c] = v
        elif medida(bars.get(c), t0, t0 + REACT_H * H, spark=False) is not None:
            faltan.append(c)
    return got, faltan


def mensaje(now: int, bars: dict, hist_res: dict | None = None, idx: dict | None = None) -> dict:
    """Mensaje «cripto_aperturas» entero. hist_res: lo de calcular_hist (o del archivo), con markets, chain y total."""
    hm = (hist_res or {}).get("markets") or {}
    markets = {}
    for k in regions.MARKETS:
        try:
            markets[k] = mercado(k, now, bars, hm, idx)
        except Exception:
            log.exception("Reacción: no pude medir %s", k)
    return {"type": "cripto_aperturas", "ts": now, "coins": COINS, "markets": markets,
            "chain": (hist_res or {}).get("chain"), "total": (hist_res or {}).get("total"),
            "hist_generated": (hist_res or {}).get("generated")}


# ---------------------------------------------------------------- historia (pura con numpy)

def _ret(t, o, c, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rendimiento de la apertura de la vela de a al cierre de la última antes de b, como encadenamiento.ret: la primera
    vela abre como mucho 5 min después de a y la última como mucho 5 min antes de b; si no, NaN."""
    out = np.full(len(a), np.nan)
    n = len(t)
    if not n or not len(a):
        return out
    i = np.searchsorted(t, a, "left")
    j = np.searchsorted(t, b, "left") - 1
    ii, jj = np.minimum(i, n - 1), np.clip(j, 0, n - 1)
    ok = (i < n) & (j > i) & (t[ii] - a <= OPEN_TOL_MS) & (b - t[jj] <= OPEN_TOL_MS)
    out[ok] = c[jj[ok]] / o[ii[ok]] - 1
    return out


def _rango_pct(t, o, h, l, a: np.ndarray, hours: int) -> np.ndarray:
    """(máximo − mínimo) / apertura en % de las velas que abren en [a, a + hours), como oscilacion.py (al menos 50
    velas por hora; si no, NaN)."""
    out = np.full(len(a), np.nan)
    if not len(t) or not len(a):
        return out
    i = np.searchsorted(t, a, "left")
    j = np.searchsorted(t, a + hours * H, "left")
    for k in np.flatnonzero(j - i >= hours * COVER_PER_H):
        x, y = int(i[k]), int(j[k])
        out[k] = (h[x:y].max() - l[x:y].min()) / o[x] * 100
    return out


def lista_aperturas(start: date, end: date, idx: dict | None) -> tuple[dict[str, list[int]], list[dict]]:
    """Aperturas de cada bolsa con su propia hora (sin sesiones especiales) y las de las ventanas del laboratorio
    (backtest_aperturas.build_openings, como los scripts de referencia), ordenadas por (open_ms, id)."""
    per: dict[str, list[int]] = {k: [] for k in regions.MARKETS}
    d = start
    while d <= end:
        for k in regions.MARKETS:
            if sesion(k, d, idx) and not especial(k, d):
                per[k].append(open_ms_on(k, d))
        d += timedelta(days=1)
    ops = [o for o in bta.build_openings(start, end, idx or {})
           if o["id"] in apertura.LAB_WINDOWS and not apertura.special_session(o)]
    ops.sort(key=lambda o: (o["open_ms"], o["id"]))
    return per, ops


def metricas(series, opens: np.ndarray, extra_a: np.ndarray, extra_b: np.ndarray) -> dict:
    """Para una moneda: rendimiento a 4 h, rango a 4 y 8 h de cada apertura y rendimiento de los tramos extra."""
    t, o, h, l, c = series
    return {"ret": _ret(t, o, c, opens, opens + REACT_H * H), "r4": _rango_pct(t, o, h, l, opens, REACT_H),
            "r8": _rango_pct(t, o, h, l, opens, LONG_H), "extra": _ret(t, o, c, extra_a, extra_b)}


def _bloque(ret: np.ndarray, r4: np.ndarray, r8: np.ndarray) -> dict | None:
    m, m4, m8 = ~np.isnan(ret), ~np.isnan(r4), ~np.isnan(r8)
    if not m.any():
        return None
    return {"n": int(m.sum()), "up_rate": _r((ret[m] > 0).mean(), 4), "mean_ret_pct": _r(ret[m].mean() * 100),
            "mean_range_pct": _r(r4[m4].mean()) if m4.any() else None,
            "range_ge2_rate": _r((r4[m4] >= RANGE_PCT).mean(), 4) if m4.any() else None,
            "range_ge2_8h_rate": _r((r8[m8] >= RANGE_PCT).mean(), 4) if m8.any() else None}


def _hist_de(sel: np.ndarray, opens: np.ndarray, M: dict, key: str | None = None) -> dict | None:
    """Historia de un conjunto de aperturas (índices sel en opens) con las métricas por moneda M."""
    if not len(sel):
        return None
    coins = {c: b for c in COINS if c in M and (b := _bloque(M[c]["ret"][sel], M[c]["r4"][sel], M[c]["r8"][sel]))}
    if not coins:
        return None
    have = np.zeros(len(sel), bool)
    any4, any8 = np.zeros(len(sel), bool), np.zeros(len(sel), bool)
    cov4, cov8 = np.zeros(len(sel), bool), np.zeros(len(sel), bool)
    for c in coins:
        have |= ~np.isnan(M[c]["ret"][sel])
        r4, r8 = M[c]["r4"][sel], M[c]["r8"][sel]
        cov4 |= ~np.isnan(r4)
        cov8 |= ~np.isnan(r8)
        any4 |= np.nan_to_num(r4, nan=-1) >= RANGE_PCT
        any8 |= np.nan_to_num(r8, nan=-1) >= RANGE_PCT
    first = int(opens[sel][have].min()) if have.any() else int(opens[sel].min())
    out = {"n": int(have.sum()),
           "desde": local_day(key, first).isoformat() if key else
           datetime.fromtimestamp(first / 1000, timezone.utc).date().isoformat(),
           "coins": coins,
           "any_range_ge2_rate": _r(any4[cov4].mean(), 4) if cov4.any() else None,
           "any_range_ge2_8h_rate": _r(any8[cov8].mean(), 4) if cov8.any() else None}
    if key == "dubai":
        out["nota"] = NOTA_DUBAI
    return out


def _tasas(pairs: list[tuple[float, float]]) -> dict | None:
    """(anterior, siguiente) → tras subida, tras bajada (o nula) y base de la siguiente, como encadenamiento.py."""
    if not pairs:
        return None
    a = np.array([p[0] for p in pairs])
    b = np.array([p[1] for p in pairs]) > 0
    up = a > 0
    n_up, n_down = int(up.sum()), int((~up).sum())
    return {"after_up_rate": _r(b[up].mean(), 4) if n_up else None,
            "after_down_rate": _r(b[~up].mean(), 4) if n_down else None,
            "base_rate": _r(b.mean(), 4), "n": len(pairs), "n_up": n_up, "n_down": n_down}


def encadenamiento(ops: list[dict], R: dict[str, np.ndarray], P: dict[str, np.ndarray]) -> dict | None:
    """chain del contrato: lo que hace la cripto en la siguiente apertura tras una que la dejó en positivo a las 4 h,
    con la siguiente que empieza cuando ya se sabe ese resultado (≥ 4 h después: sin mirar el futuro). R: rendimiento a
    4 h de cada apertura por moneda; P: lo que llevaba cada apertura cuando abrió la siguiente (NaN si abren a menos de
    30 min). Las cuatro monedas juntas. Además, «parcial» (con lo que llevaba la anterior) y «tal_cual» (la siguiente
    sin más, que se solapa: São Paulo y Nueva York abren con entre 30 y 90 min de diferencia según la época del año)."""
    if not ops:
        return None
    opens = np.array([o["open_ms"] for o in ops], np.int64)
    nxt = np.searchsorted(opens, opens + REACT_H * H, "left")
    operable, parcial, naive = [], [], []
    for c in COINS:
        r, p = R.get(c), P.get(c)
        if r is None:
            continue
        for k in range(len(ops)):
            a = r[k]
            if k + 1 < len(ops) and not np.isnan(a) and not np.isnan(r[k + 1]):
                naive.append((a, r[k + 1]))
            if p is not None and k + 1 < len(ops) and not np.isnan(p[k]) and not np.isnan(r[k + 1]):
                parcial.append((p[k], r[k + 1]))
            nk = int(nxt[k])
            if nk < len(ops) and not np.isnan(a) and not np.isnan(r[nk]):
                operable.append((a, r[nk]))
    main = _tasas(operable)
    if not main:
        return None
    par, nai = _tasas(parcial), _tasas(naive)
    nota = (f"Tras una apertura que deja la cripto en positivo a las 4 h, la siguiente apertura (la primera que empieza "
            f"cuando ya se sabe ese resultado) sube el {_pc(main['after_up_rate'] or 0)} de las veces, frente al "
            f"{_pc(main['base_rate'])} de todas: no predice. ")
    if par and par["after_up_rate"] is not None:
        nota += (f"Con lo que llevaba la anterior al abrir la siguiente, igual: {_pc(par['after_up_rate'])} "
                 f"(base {_pc(par['base_rate'])}). ")
    if nai and nai["after_up_rate"] is not None:
        nota += (f"El {_pc(nai['after_up_rate'])} que sale al mirar la siguiente apertura tal cual es un artefacto del "
                 f"solape (São Paulo y Nueva York abren con entre 30 y 90 min de diferencia según la época del año y sus "
                 f"ventanas de 4 h se solapan): no es una señal.")
    return {**main, "nota": nota.strip(), "parcial": par, "tal_cual": nai}


def calcular_hist(loader, start: date, end: date, idx: dict | None = None, clock=now_ms) -> dict:
    """Historia de cada bolsa (con su propia hora), el total de las ventanas del laboratorio y el encadenamiento.
    loader(moneda) → (t, o, h, l, c) en arrays de numpy ordenados por t (velas de 1 min), o None. Se procesa una moneda
    detrás de otra para no tener las cuatro en memoria a la vez."""
    per, ops = lista_aperturas(start, end, idx)
    allo = sorted({t for v in per.values() for t in v} | {o["open_ms"] for o in ops})
    opens = np.array(allo, np.int64)
    pos = {t: i for i, t in enumerate(allo)}
    wo = np.array([o["open_ms"] for o in ops], np.int64)
    # Tramo de lo que llevaba cada apertura al abrir la siguiente (encadenamiento.py): NaN si abren a menos de 30 min.
    ea = wo[:-1] if len(wo) > 1 else np.array([], np.int64)
    eb = np.minimum(wo[1:], wo[:-1] + REACT_H * H) if len(wo) > 1 else np.array([], np.int64)
    gap_ok = (wo[1:] - wo[:-1] >= CHAIN_MIN_GAP_MS) if len(wo) > 1 else np.array([], bool)
    M, R, P = {}, {}, {}
    for c in COINS:
        s = loader(c)
        if s is None or not len(s[0]):
            log.warning("Reacción: sin velas de %s en la caché de 1 min: su historia queda vacía", c)
            continue
        M[c] = metricas(s, opens, ea, eb)
        del s
        R[c] = M[c]["ret"][[pos[t] for t in wo]] if len(wo) else np.array([])
        p = np.full(len(wo), np.nan)
        if len(wo) > 1:
            p[:-1] = np.where(gap_ok, M[c]["extra"], np.nan)
        P[c] = p
    markets = {}
    for k, v in per.items():
        sel = np.array([pos[t] for t in v], int)
        markets[k] = _hist_de(sel, opens, M, k) if M else None
    total = _hist_de(np.array([pos[t] for t in wo], int), opens, M) if M and len(wo) else None
    return {"version": VERSION, "generated": clock(), "desde": start.isoformat(), "hasta": end.isoformat(),
            "markets": markets, "total": total, "chain": encadenamiento(ops, R, P) if R else None}


def cargar_moneda(coin: str, desde: date, folder=None):
    """Velas de 1 min de la caché (zip mensual o parte de la API, como backtest_aperturas y la vigilancia) desde el mes
    de «desde» → (t, o, h, l, c) ordenadas y sin minutos repetidos (manda la primera, como Series.add). Solo lectura."""
    from . import laboratorio as L   # perezoso: es un módulo grande
    parts = []
    for p in L.month_files(coin, f"{desde.year:04d}-{desde.month:02d}", folder):
        try:
            a = L.parse_zip(p) if p.suffix == ".zip" else L.parse_api(p)
        except Exception as e:   # un archivo dañado no para el resto
            log.warning("Reacción: velas ilegibles %s: %s", p, e)
            continue
        if len(a):
            parts.append(np.ascontiguousarray(a[:, :5], float))
        time.sleep(0)   # cede el GIL al bucle de eventos entre archivo y archivo
    if not parts:
        return None
    a = np.concatenate(parts)
    del parts
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate(([True], np.diff(a[:, 0]) > 0))]
    return a[:, 0].astype(np.int64), a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy(), a[:, 4].copy()


def periodo(today: date) -> tuple[date, date]:
    """Los últimos 3 años como mucho, hasta ayer."""
    try:
        start = today.replace(year=today.year - HIST_YEARS)
    except ValueError:   # 29 de febrero
        start = today.replace(year=today.year - HIST_YEARS, day=28)
    return start, today - timedelta(days=1)


def leer_archivo(path: Path) -> dict | None:
    try:
        res = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("Reacción: %s ilegible (%s): se recalcula", path, e)
        return None
    if not isinstance(res, dict) or res.get("version") != VERSION or not isinstance(res.get("markets"), dict):
        return None
    return res


def guardar_archivo(path: Path, res: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- módulo

class Reaccion:
    """Publica «cripto_aperturas» cada minuto. bars(moneda, desde_ms, hasta_ms) → velas de 1 min [t, o, h, l, c, …]
    (aprendizaje.CacheBars.get con las de la vigilancia); loader(moneda, desde) → series para la historia (por defecto
    la caché de HIST_DIR/1m); index_loader() → índices diarios para los festivos de la historia."""

    def __init__(self, bus, bars=None, clock=now_ms, loader=None, index_loader=None, path=None, folder=None):
        self.bus, self.bars, self.clock = bus, bars, clock
        self.loader = loader or (lambda c, desde: cargar_moneda(c, desde, folder))
        self.index_loader = index_loader or bta.index_data
        self._path = Path(path) if path else None
        self.hist: dict | None = None
        self.msg: dict | None = None
        self.err: str | None = None
        self._task: asyncio.Task | None = None
        self._fail_at = float("-inf")
        self._err_at: dict[str, float] = {}
        try:
            self.hist = leer_archivo(self.path())
        except Exception:
            log.exception("Reacción: no pude leer la historia guardada")
        if self.hist:
            log.info("Reacción: historia de %s a %s cargada de %s", self.hist.get("desde"), self.hist.get("hasta"),
                     self.path())

    def path(self) -> Path:
        return self._path or Path(config.BACKTEST_DIR) / HIST_FILE

    def _log_err(self, key: str, msg: str, *args, exc: bool = False) -> None:
        t = time.monotonic()
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_S:
            self._err_at[key] = t
            (log.exception if exc else log.warning)(msg, *args)

    # ---- historia

    def hist_due(self, now: int) -> bool:
        gen = (self.hist or {}).get("generated")
        return not isinstance(gen, (int, float)) or now - gen >= HIST_MAX_AGE_MS

    def compute_hist(self, now: int | None = None) -> dict:
        """En un hilo: calcula la historia con las velas de la caché y la guarda (BACKTEST_DIR/reaccion_aperturas.json)."""
        now = self.clock() if now is None else now
        t0 = time.monotonic()
        start, end = periodo(datetime.fromtimestamp(now / 1000, timezone.utc).date())
        try:
            idx = self.index_loader() or {}
        except Exception as e:
            log.warning("Reacción: sin índices diarios para los festivos de la historia (%s): se cuentan los laborables", e)
            idx = {}
        res = calcular_hist(lambda c: self.loader(c, start), start, end, idx, clock=lambda: now)
        if not res.get("total"):
            raise RuntimeError("sin velas de 1 min en la caché para la historia")
        try:   # un fallo al guardar (disco, bloqueo de Windows) no tira los ~50 s de cálculo: se usa igual
            guardar_archivo(self.path(), res)
        except Exception as e:
            log.warning("Reacción: no pude guardar la historia en %s (%s): se usa igual y se guarda en el próximo cálculo",
                        self.path(), e)
        ch = res.get("chain") or {}
        log.info("Reacción: historia de %d aperturas (%s → %s) en %.0f s; tras subida la siguiente sube el %s (base %s)",
                 res["total"]["n"], res["desde"], res["hasta"], time.monotonic() - t0,
                 ch.get("after_up_rate"), ch.get("base_rate"))
        return res

    def _hist_done(self, task: asyncio.Task) -> None:
        self._task = None
        if task.cancelled():
            return
        e = task.exception()
        if e is not None:
            self._fail_at = time.monotonic()
            self.err = str(e)[:200] or type(e).__name__
            log.warning("Reacción: la historia no se pudo calcular (%s); el mensaje sale sin ella y se reintenta en 1 h",
                        self.err)
            return
        self.hist, self.err = task.result(), None

    def ensure_hist(self, now: int) -> None:
        """Lanza el cálculo de la historia en un hilo si toca (al arrancar sin archivo reciente y una vez al día)."""
        if self._task is not None or not self.hist_due(now) or time.monotonic() - self._fail_at < HIST_RETRY_S:
            return
        self._task = asyncio.get_running_loop().create_task(asyncio.to_thread(self.compute_hist, now))
        self._task.add_done_callback(self._hist_done)

    # ---- en vivo

    def _bars_now(self, now: int) -> dict:
        out = {}
        if self.bars is None:
            return out
        for c in COINS:
            try:
                a = self.bars(c, now - LOOKBACK_D * DAY, now + MIN)
                out[c] = np.asarray(a, float) if a is not None and len(a) else None
            except Exception as e:
                self._log_err("bars:" + c, "Reacción: sin velas de %s: %s", c, e)
                out[c] = None
        return out

    def build(self, now: int, bars: dict) -> dict:
        return mensaje(now, bars, self.hist)

    async def step(self, now: int | None = None) -> dict:
        now = self.clock() if now is None else int(now)
        self.ensure_hist(now)
        bars = await asyncio.to_thread(self._bars_now, now)   # CacheBars puede leer del disco
        self.msg = self.bus.publish(self.build(now, bars))
        return self.msg

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log_err("ronda", "Reacción: fallo en la ronda; sigue en la próxima", exc=True)
            await asyncio.sleep(TICK_S)

    def view(self) -> dict:
        """Para GET /api/cripto_aperturas: el último mensaje (o, antes de la primera ronda, sin mercados)."""
        return self.msg or {"type": "cripto_aperturas", "ts": None, "estado": "arrancando", "coins": COINS,
                            "markets": {}, "chain": (self.hist or {}).get("chain"), "total": (self.hist or {}).get("total")}


def apagado() -> dict:
    """GET /api/cripto_aperturas sin el módulo."""
    return {"type": "cripto_aperturas", "ts": None, "estado": "apagado", "coins": COINS, "markets": {}, "chain": None}
