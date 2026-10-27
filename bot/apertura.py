"""Motor de aperturas: opera BTC, ETH, SOL y XRP en las aperturas de las bolsas. Dos modos (OpeningEngine, pick_strategy):

Modo «impulso» (por defecto en vivo, la operativa del usuario; ImpulseRun y funciones *impulse*): no se adivina la
dirección, se mide el impulso de los primeros 15 min (30 o 60) y, si pasa de ±0,2 %, se sigue con las mismas reglas que
la familia «Movimiento de los primeros minutos» del laboratorio: entrada a +30 min (o en la reanudación tras retroceder
la mitad, o en la ruptura del rango) con volumen relativo ≥ 1,5 desde la apertura y solo a favor de la tendencia diaria
de BTC, en todas las aperturas y monedas. Largos al contado y cortos como perpetuos (funding en el paper). Las noticias y
la apertura anterior respaldan y miden (news_mode y etiquetas). Salida: horquilla pura de 3 % y 3R sin tope de tiempo
(7 días de seguridad), sin break-even, stop dinámico ni parciales, y solo si el objetivo deja ≥ 3 % neto de costes.
Preset IMPULSO: la prometedora más sólida del laboratorio, que NO es una ventaja demostrada (se mide en paper).

Modo «ruptura» (v2, sin cambios): la ruptura del rango de apertura solo a favor de la señal de las noticias.
Reglas (las mismas en vivo y en bot/backtest_aperturas.py, que llama a estas funciones):
1. Señal por moneda y bolsa: plan del estratega, noticias triadas y predicciones. Sin señal no se opera en vivo.
2. Rango: máximo y mínimo de los primeros N minutos desde la apertura (velas de 1 min de Binance al contado).
3. Entrada: el cierre de una vela de 1 min rompe el rango (más un margen) en la dirección de la señal, con volumen
   relativo (rvol) suficiente y presión compradora o vendedora a favor.
4. Stop inicial: el otro lado del rango o k×ATR, el más cercano que no sea ridículo, entre un mínimo y un máximo en %.
5. Stop dinámico: el del paper trading. Sigue al máximo favorable a una distancia que baja desde la del stop inicial
   hasta m×ATR según avanza la operación, con break-even a +1 R (stopPct y trailPct del ticket).
6. Reentradas: tras salir, rango nuevo de 5 min y nueva ruptura a favor, si sigue habiendo volumen y señal; como mucho 3.
7. Fin de la sesión: volumen agotado tras operar, 4 h desde la apertura, sin ruptura en 90 min, menos de 30 min por
   delante para una entrada nueva o dos pérdidas seguidas.
8. Una sola posición por moneda: si ya hay una abierta o en curso (otra ventana o la fase 2), no se entra.

v2 (parámetros nuevos; los valores de PARAMS reproducen la v1 y el preset V2 activa la disciplina del paquete
day-trader-skill): confirmación al cierre de velas de 5 min, stop estructural (medio del rango, otro lado o k×ATR de
5 min) con suelo de costes, objetivo fijo (2R, 3R o +3 % neto), parcial y break-even opcionales, puerta R:R desde la
entrada real, bandera MOVING, límites diarios globales y puerta de probabilidades con las sesiones anteriores.
En vivo, sin params, se cargan los parámetros que eligió el backtest de 24 meses (aperturas.json); sin una
recomendación válida, el preset V2 (sin puerta), nunca la v1.
"""
import asyncio
import bisect
import inspect
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from . import config, paper, regions
from .models import now_ms

log = logging.getLogger("apertura")
MAD = ZoneInfo("Europe/Madrid")

MIN = 60_000
H = 3600_000
KLINES = "https://data-api.binance.vision/api/v3/klines"
PAIRS = {c: f"{c}USDT" for c in config.COINS}
SRC = "Motor de aperturas"
MODE = "Ruptura de apertura"          # uno de claude_agents.MODES
STRATEGY = "motor-aperturas"
ENTRY_TYPE = "Ruptura confirmada al cierre de 1 min (a mercado)"
T, O, HI, LO, C, V, QV, TB = range(8)  # vela: [open_ms, o, h, l, c, vol_base, vol_quote, taker_buy_base]

# Bolsas que abren a la vez forman una sola ventana. Dubái solo es referencia: no tiene ventana.
GROUPS = {frozenset({"tokio", "seul"}): ("tokio-seul", "Tokio y Seúl", "tokio"),
          frozenset({"shanghai", "hongkong"}): ("china", "Shanghái y Hong Kong", "hongkong"),
          frozenset({"frankfurt", "londres", "madrid"}): ("europa", "Europa", "londres")}
TRADED = [k for k in regions.MARKETS if k != "dubai"]

PARAMS = {
    "range_min": 15,            # minutos del rango de apertura
    "rvol_min": 1.5,            # rvol de 5 min mínimo para entrar
    "rvol_lookback": 5,
    "rvol_sessions": 5,         # sesiones anteriores con las que se compara el volumen
    "exhaust_rvol": 0.8,        # rvol de 15 min por debajo de esto = volumen agotado
    "rearm_rvol": 1.0,          # rvol de 15 min mínimo para buscar una reentrada
    "break_bps": 2.0,           # margen de ruptura: el mayor de 2 pb del precio
    "break_frac": 0.05,         # ... o el 5 % de la altura del rango
    "buy_edge": 0.03,           # compras agresivas ≥ 53 % para largos (≤ 47 % para cortos)
    "buy_n": 5,
    "atr_n": 14,
    "stop_atr": 2.0,            # stop inicial alternativo: k×ATR de 1 min
    "stop_min_pct": 0.3,        # un stop más cercano es ridículo (las comisiones de ida y vuelta ya son 0,2 %)
    "stop_max_pct": 3.0,
    "trail_atr": 6.0,           # stop dinámico: máximo favorable − m×ATR de 1 min
    "trail_min_pct": 0.2,
    "trail_max_pct": 5.0,
    "session_max_min": 240,     # la sesión acaba 4 h después de la apertura
    "first_entry_max_min": 90,  # sin ruptura en este tiempo no se entra
    "last_entry_min": 30,       # con menos de esto por delante no se abre otra operación
    "rearm_range_min": 5,
    "max_reentries": 3,
    "max_losses_row": 2,
    "reject_wait_min": 5,
    "max_rejects": 3,
    "lead_min": 15,             # se arma 15 min antes de la apertura
    "min_strength": 0.2,        # fuerza mínima de la señal
    "news_hours": 6,
    "news_half_life_h": 2.0,
    # v2: con estos valores se comporta como la v1
    "confirm_tf": 1,            # confirmación al cierre de velas de 1 o de 5 min (solo cerradas)
    "stop_mode": "cercano",     # cercano (v1: rango o ATR, el más cercano; salidas de la v1) | or_mid | or_far | atr5
    "stop_atr5": 1.5,           # k del modo atr5 (ATR de velas de 5 min)
    "max_cost_r": None,         # suelo del stop: coste de ida y vuelta ≤ esta parte de R (0,25 → stop ≥ ~1 %)
    "target_r": None,           # objetivo fijo en R (2, 3) ...
    "target_pct": None,         # ... o en % neto (3); los dos None = sin objetivo
    "tp1_r": 1.0,               # salida parcial: en tp1_r R ...
    "tp1_frac": 0.0,            # ... esta parte de la posición (0 = sin parcial)
    "be_r": 1.0,                # break-even (cubriendo costes) a +be_r R; None = nunca
    "trail_start_r": None,      # stop dinámico desde +trail_start_r R; None = desde la entrada
    "trail_tf": 1,              # ATR del stop dinámico en velas de 1 o de 5 min (trail_atr 0 = sin stop dinámico)
    "min_rr": 1.0,              # puerta R:R (solo con objetivo): (objetivo − entrada real) ≥ min_rr × riesgo y ≥ 3 × coste
    "moving_pct": None,         # bandera MOVING: no entrar si ya se movió ≥ esto en las moving_h horas previas
    "moving_h": 4,
    "max_day_entries": None,    # tope de entradas por día de Madrid (todas las ventanas)
    "max_day_risk_pct": None,   # riesgo comprometido en stop por día de Madrid (ganen o pierdan), en % del capital
    "gate": False,              # puerta de probabilidades con las sesiones anteriores de la ventana
    "gate_pp": 5.0,             # P(objetivo primero) ≥ punto muerto 1/(1+R) + gate_pp puntos
    "gate_min_n": 20,           # rupturas pasadas que tocaron objetivo o stop; con menos, la puerta no se cumple
    "gate_sessions": 60,        # sesiones anteriores que se miran
}
# Preset v2: disciplina del paquete day-trader-skill (stop estructural fijo, objetivo 2R, sin break-even ni stop
# dinámico, R:R desde la entrada real, una entrada por moneda y ventana y límites diarios). Es la referencia «preset
# V2» del backtest, que pierde con costes fuera de muestra (24 meses hasta oct. 2026, encadenamiento: R medio −0,23
# en 363 operaciones): no es una ventaja.
# Puerta de probabilidades APAGADA: con 60 sesiones de historia hay de media 3,7 rupturas decididas por ventana y
# moneda (hacen falta 20), así que no dejaría entrar nunca; con 250 solo pasa el 0,3 % de las rupturas.
V2 = {"range_min": 30, "confirm_tf": 5, "stop_mode": "or_mid", "max_cost_r": 0.25, "stop_max_pct": 3.0,
      "target_r": 2.0, "target_pct": None, "tp1_frac": 0.0, "be_r": None, "trail_atr": 0.0, "trail_start_r": None,
      "trail_tf": 5, "min_rr": 1.0, "max_reentries": 0, "moving_pct": 3.0, "moving_h": 4, "max_day_entries": 3,
      "max_day_risk_pct": 1.5, "gate": False, "gate_pp": 5.0, "gate_min_n": 20, "gate_sessions": 60}
REC_MIN_MONTHS = 24             # una recomendación del backtest con menos historia (pasada corta o de prueba) no se carga
MAX_FILL_DELAY_MS = 60_000      # el paper no ejecuta un ticket del motor aprobado más tarde (veto o aprobación a mano)
M5 = 5 * MIN
COVERAGE = 0.9                  # una sesión pasada con menos del 90 % de sus velas no cuenta en la puerta
COIN_FLIP_PP, STRONG_PP, MANY_NEITHER = 5.0, 15.0, 0.40   # veredicto de la puerta (como la Time Machine del paquete)
W_PLAN, W_NEWS, W_PRED = 0.35, 0.40, 0.25   # peso de cada fuente en la señal (lo que falta cuenta como neutral)
CONF_W = {"baja": 0.5, "media": 0.8, "alta": 1.0}
CRIPTO_TIPOS = {"exchange", "seguridad", "on-chain", "red", "regulatorio", "institucional"}
STATES = ("esperando", "midiendo_rango", "armada", "en_posicion", "rearmada", "terminada")

TICK_S = 5
PUBLISH_MIN_S = 3
LIVE_LAG_MS = 90_000        # una vela cerrada hace más de esto ya no sirve para entrar
PRE_MIN = 80                # velas de antes de la apertura (rvol sin historia y ATR)
SUBMIT_GRACE_MS = 20_000    # tras este plazo sin rastro en el paper, la orden se da por bloqueada
KEEP_MS = 8 * H             # ventanas terminadas que siguen en el estado
ACTIVA_CHECK_S = 60         # cada cuánto se mira si cambió estrategia_activa.json (evolución)

# Festivos de cada bolsa: calendario mínimo de 2026 y 2027 con los seguros (regions solo mira el día de la semana).
# Una ventana no se opera si todas sus bolsas tienen festivo. Si aquí falta uno, ese día se opera como uno normal.
_HOL = {
    "nuevayork": "2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 "
                 "2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 "
                 "2027-11-25 2027-12-24",
    "londres": "2026-01-01 2026-04-03 2026-04-06 2026-05-04 2026-05-25 2026-08-31 2026-12-25 2026-12-28 2027-01-01 "
               "2027-03-26 2027-03-29 2027-05-03 2027-05-31 2027-08-30 2027-12-27 2027-12-28",
    "frankfurt": "2026-01-01 2026-04-03 2026-04-06 2026-05-01 2026-12-24 2026-12-25 2026-12-31 2027-01-01 2027-03-26 "
                 "2027-03-29 2027-12-24 2027-12-31",
    "madrid": "2026-01-01 2026-04-03 2026-04-06 2026-05-01 2026-12-25 2027-01-01 2027-03-26 2027-03-29",
    "saopaulo": "2026-01-01 2026-02-16 2026-02-17 2026-04-03 2026-04-21 2026-05-01 2026-06-04 2026-09-07 2026-10-12 "
                "2026-11-02 2026-11-20 2026-12-24 2026-12-25 2026-12-31 2027-01-01 2027-02-08 2027-02-09 2027-03-26 "
                "2027-04-21 2027-05-27 2027-09-07 2027-10-12 2027-11-02 2027-11-15 2027-12-24 2027-12-31",
    "tokio": "2026-01-01 2026-01-02 2026-01-12 2026-02-11 2026-02-23 2026-03-20 2026-04-29 2026-05-04 2026-05-05 "
             "2026-05-06 2026-07-20 2026-08-11 2026-09-21 2026-09-22 2026-09-23 2026-10-12 2026-11-03 2026-11-23 "
             "2026-12-31 2027-01-01 2027-01-11 2027-02-11 2027-02-23 2027-04-29 2027-05-03 2027-05-04 2027-05-05 "
             "2027-07-19 2027-08-11 2027-09-20 2027-10-11 2027-11-03 2027-11-23 2027-12-31",
    "seul": "2026-01-01 2026-02-16 2026-02-17 2026-02-18 2026-05-01 2026-05-05 2026-09-24 2026-09-25 2026-10-09 "
            "2026-12-25 2026-12-31 2027-01-01 2027-05-05 2027-12-31",
    "shanghai": "2026-01-01 2026-01-02 2026-02-16 2026-02-17 2026-02-18 2026-02-19 2026-02-20 2026-02-23 2026-04-06 "
                "2026-05-01 2026-05-04 2026-05-05 2026-06-19 2026-09-25 2026-10-01 2026-10-02 2026-10-05 2026-10-06 "
                "2026-10-07 2027-01-01 2027-10-01",
    "hongkong": "2026-01-01 2026-02-17 2026-02-18 2026-02-19 2026-04-03 2026-04-06 2026-04-07 2026-05-01 2026-06-19 "
                "2026-07-01 2026-10-01 2026-12-25 2027-01-01 2027-03-26 2027-03-29 2027-07-01 2027-10-01",
}
HOLIDAYS = {k: frozenset(v.split()) for k, v in _HOL.items()}


# ---------------------------------------------------------------- textos

def _n(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _sg(x: float, d: int = 2) -> str:
    return ("+" if x >= 0 else "") + _n(x, d)


def _px(x: float) -> str:
    return _n(x, 2 if abs(x) >= 100 else 4)


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%H:%M")


def _cut(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _f(x, default=None):
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _maybe(x):
    return await x if inspect.isawaitable(x) else x


def _params(params: dict | None) -> dict:
    """Parámetros completos: los de PARAMS con los cambios de params (sin copiar si ya vienen completos)."""
    if not params:
        return PARAMS
    return params if len(params) >= len(PARAMS) and all(k in params for k in PARAMS) else {**PARAMS, **params}


# ---------------------------------------------------------------- velas e indicadores (funciones puras)

def to_bars(raw: list, now: int | None = None) -> list[list]:
    """Velas de la API de Binance → [open_ms, o, h, l, c, vol_base, vol_quote, taker_buy_base]. Sin la vela en curso."""
    out = []
    for k in raw or []:
        if now is not None and int(k[6]) >= now:
            continue
        out.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]), float(k[9])])
    return out


def _t0(b) -> int:
    return b[T]


def bar_index(bars: list, t: int) -> int:
    """Primera vela que abre en t o después."""
    return bisect.bisect_left(bars, t, key=_t0)


def slot_volume(bars: list, a: int, b: int) -> float:
    """Volumen (en moneda) de las velas que abren en [a, b)."""
    i, j = bar_index(bars, a), bar_index(bars, b)
    return sum(bars[k][V] for k in range(i, j))


def opening_range(bars: list, open_ms: int, minutes: int) -> dict | None:
    """Máximo, mínimo y volumen de las velas de los primeros `minutes` minutos desde open_ms."""
    i, j = bar_index(bars, open_ms), bar_index(bars, open_ms + minutes * MIN)
    if j <= i:
        return None
    seg = bars[i:j]
    return {"hi": max(b[HI] for b in seg), "lo": min(b[LO] for b in seg), "vol": sum(b[V] for b in seg), "n": j - i}


def prior_volumes(vol_sum, open_ms: int, prev_opens: list[int], t: int, lookback: int) -> list[float]:
    """Volumen del mismo tramo (relativo a su apertura) en las sesiones anteriores. vol_sum(a, b) suma [a, b)."""
    off = t - open_ms
    return [vol_sum(po + off - lookback * MIN, po + off) for po in prev_opens]


def rvol(bars: list, t: int, lookback: int = 15, prior: list[float] | None = None) -> float | None:
    """Volumen de los `lookback` minutos anteriores a t frente al mismo tramo en las sesiones anteriores (prior; la
    mediana, para que un día de pánico no infle la referencia). Sin al menos dos sesiones con datos, frente a la media
    de los 60 min previos."""
    cur = slot_volume(bars, t - lookback * MIN, t)
    ref = sorted(v for v in (prior or []) if v > 0)
    if len(ref) >= 2:
        h = len(ref) // 2
        base = ref[h] if len(ref) % 2 else (ref[h - 1] + ref[h]) / 2
    else:
        base = slot_volume(bars, t - (lookback + 60) * MIN, t - lookback * MIN) / 60 * lookback
    return cur / base if base > 0 else None


def atr(bars: list, n: int = 14, end: int | None = None) -> float | None:
    """ATR simple de las últimas n velas hasta bars[end - 1] (en precio)."""
    end = len(bars) if end is None else end
    start = max(1, end - n)
    if end - start < 1:
        return None
    s = 0.0
    for k in range(start, end):
        h, l, pc = bars[k][HI], bars[k][LO], bars[k - 1][C]
        s += max(h - l, abs(h - pc), abs(l - pc))
    return s / (end - start)


def buy_ratio(bars: list, n: int = 5, end: int | None = None) -> float | None:
    """Parte del volumen de las últimas n velas que fueron compras agresivas (taker buy)."""
    end = len(bars) if end is None else end
    seg = bars[max(0, end - n):end]
    v = sum(b[V] for b in seg)
    return sum(b[TB] for b in seg) / v if v > 0 else None


def bars5(bars: list, end: int, n: int) -> list[list]:
    """Las n últimas velas de 5 min (rejilla UTC) ya cerradas al cierre de bars[end − 1], agregadas desde las de 1 min.
    La vela de 5 min en curso no cuenta."""
    if end <= 0:
        return []
    lim = (bars[end - 1][T] + MIN) // M5 * M5   # fin de la última vela de 5 min cerrada
    k = end - 1
    while k >= 0 and bars[k][T] >= lim:
        k -= 1
    out = []
    while k >= 0 and len(out) < n:
        b0 = bars[k][T] // M5 * M5
        j = k
        while j >= 0 and bars[j][T] >= b0:
            j -= 1
        seg = bars[j + 1:k + 1]
        out.append([b0, seg[0][O], max(b[HI] for b in seg), min(b[LO] for b in seg), seg[-1][C],
                    sum(b[V] for b in seg), sum(b[QV] for b in seg), sum(b[TB] for b in seg)])
        k = j
    return out[::-1]


def features(bars: list, i: int, open_ms: int, vol_sum=None, prev_opens: list[int] | None = None,
             params: dict | None = None) -> dict:
    """Indicadores al cierre de bars[i]: rvol de 5 y 15 min, presión compradora y ATR (y el de 5 min si el stop o el
    stop dinámico lo usan). Solo miran hacia atrás."""
    p = _params(params)
    t = bars[i][T] + MIN
    vs = vol_sum or (lambda a, b: slot_volume(bars, a, b))
    lb = p["rvol_lookback"]
    pr = prior_volumes(vs, open_ms, prev_opens, t, lb) if prev_opens else None
    pr15 = prior_volumes(vs, open_ms, prev_opens, t, 15) if prev_opens else None
    out = {"rvol": rvol(bars, t, lb, pr), "rvol15": rvol(bars, t, 15, pr15),
           "buy_ratio": buy_ratio(bars, p["buy_n"], i + 1), "atr": atr(bars, p["atr_n"], i + 1)}
    if p.get("stop_mode") == "atr5" or (p.get("trail_tf") == 5 and p.get("trail_atr")):
        out["atr5"] = atr(bars5(bars, i + 1, p["atr_n"] + 1), p["atr_n"])
    return out


# ---------------------------------------------------------------- entrada y stops (funciones puras)

def confirm(signal_dir: int, bar: list, rng: dict | None, rvol_v: float | None, buy_ratio_v: float | None,
            params: dict | None = None) -> dict:
    """¿El cierre de esta vela de 1 min confirma la entrada? Rompe el rango (más un margen) en la dirección de la señal,
    con rvol ≥ umbral y presión a favor. Con dir 0 solo se mira «ruptura libre» (cualquier lado) si params["free"]."""
    p = _params(params)
    if not rng:
        return {"ok": False, "dir": 0, "why": "Aún no hay rango"}
    if signal_dir == 0 and not p.get("free"):
        return {"ok": False, "dir": 0, "why": "Sin señal de las noticias: no se opera"}
    c, hi, lo = bar[C], rng["hi"], rng["lo"]
    margin = max(c * p["break_bps"] / 1e4, (hi - lo) * p["break_frac"])
    up, dn = c > hi + margin, c < lo - margin
    allowed = (1, -1) if signal_dir == 0 else (signal_dir,)
    d = 1 if up and 1 in allowed else -1 if dn and -1 in allowed else 0
    if not d:
        if up or dn:
            return {"ok": False, "dir": 0, "why": f"Rompe el rango {'al alza' if up else 'a la baja'}, en contra de la "
                                                  "señal: no se entra"}
        return {"ok": False, "dir": 0, "why": f"Sin ruptura: cierre {_px(c)} dentro del rango {_px(lo)}–{_px(hi)} "
                                              f"(margen {_px(margin)})"}
    side = "al alza" if d > 0 else "a la baja"
    edge = "máximo" if d > 0 else "mínimo"
    if rvol_v is None or rvol_v < p["rvol_min"]:
        rv = "sin dato" if rvol_v is None else _n(rvol_v)
        return {"ok": False, "dir": d, "why": f"Ruptura {side} sin volumen: rvol {rv} < {_n(p['rvol_min'])}"}
    need = 0.5 + p["buy_edge"] if d > 0 else 0.5 - p["buy_edge"]
    if buy_ratio_v is None or (d > 0 and buy_ratio_v < need) or (d < 0 and buy_ratio_v > need):
        br = "sin dato" if buy_ratio_v is None else f"{_n(buy_ratio_v * 100, 0)} %"
        who = "compradora" if d > 0 else "vendedora"
        return {"ok": False, "dir": d, "why": f"Ruptura {side} con volumen pero sin presión {who}: compras {br}"}
    press = (f"compras {_n(buy_ratio_v * 100, 0)} %" if d > 0 else f"ventas {_n((1 - buy_ratio_v) * 100, 0)} %")
    return {"ok": True, "dir": d, "price": c,
            "why": f"Cierre de {p.get('confirm_tf') or 1} min {_px(c)} rompe el {edge} del rango "
                   f"({_px(hi if d > 0 else lo)}) con rvol {_n(rvol_v)} y {press}"}


def stop_floor_pct(params: dict | None = None) -> float:
    """Stop mínimo en %: stop_min_pct y, con max_cost_r, el que deja el coste de ida y vuelta en esa parte de R."""
    p = _params(params)
    k = p.get("max_cost_r")
    return max(p["stop_min_pct"], paper.cost_rt_pct() / k if k else 0.0)


def initial_stop(d: int, entry: float, rng: dict | None, atr_v: float | None, params: dict | None = None,
                 atr5_v: float | None = None) -> dict:
    """Stop inicial según stop_mode, entre el suelo (stop_floor_pct) y stop_max_pct.
    - cercano (v1): el otro lado del rango o k×ATR de 1 min, el más cercano que no baje del suelo.
    - or_mid: el medio del rango; or_far: el otro lado del rango; atr5: stop_atr5 × ATR de 5 min."""
    p = _params(params)
    lo_pct, hi_pct = stop_floor_pct(p), p["stop_max_pct"]
    mode = p.get("stop_mode") or "cercano"
    if mode == "cercano":
        cands = []
        if rng:
            dist = entry - rng["lo"] if d > 0 else rng["hi"] - entry
            if dist > 0:
                cands.append((dist / entry * 100, "rango"))
        if atr_v:
            cands.append((p["stop_atr"] * atr_v / entry * 100, "atr"))
        ok = [c for c in cands if c[0] >= lo_pct]
        pct, kind = min(ok) if ok else (lo_pct, "minimo")
    else:
        dist = None
        if mode in ("or_mid", "or_far") and rng:
            lvl = (rng["hi"] + rng["lo"]) / 2 if mode == "or_mid" else (rng["lo"] if d > 0 else rng["hi"])
            dist = d * (entry - lvl)
        elif mode == "atr5" and atr5_v:
            dist = p["stop_atr5"] * atr5_v
        pct, kind = (dist / entry * 100, mode) if dist and dist > 0 else (lo_pct, "minimo")
        if pct < lo_pct:
            pct, kind = lo_pct, "minimo"
    if pct > hi_pct:
        pct, kind = hi_pct, "maximo"
    pct = round(pct, 3)
    near = "rango y ATR quedaban demasiado cerca" if mode == "cercano" else "el stop estructural quedaba más cerca"
    texts = {"rango": "en el otro lado del rango", "atr": f"a {_n(p['stop_atr'], 1)} ATR",
             "or_mid": "en el medio del rango", "or_far": "en el otro lado del rango",
             "atr5": f"a {_n(p['stop_atr5'], 1)} ATR de 5 min",
             "minimo": f"en el mínimo de {_n(lo_pct)} % ({near})", "maximo": f"en el máximo de {_n(hi_pct)} %"}
    return {"price": entry * (1 - d * pct / 100), "pct": pct, "kind": kind,
            "why": f"Stop inicial {_n(pct)} % {texts[kind]}"}


def trail_pct(entry: float, atr_v: float | None, stop_pct: float, params: dict | None = None) -> float | None:
    """Distancia del stop dinámico en %: m×ATR (de 1 o de 5 min, según trail_tf), entre trail_min_pct y trail_max_pct.
    None si trail_atr es 0 (sin stop dinámico)."""
    p = _params(params)
    if not p["trail_atr"]:
        return None
    t = p["trail_atr"] * atr_v / entry * 100 if atr_v else stop_pct
    return round(max(p["trail_min_pct"], min(p["trail_max_pct"], t)), 3)


def dynamic_stop(d: int, entry: float, stop_pct: float, trail: float | None, best: float, stop: float, be: bool,
                 be_r: float | None = 1.0, be_pct: float = 0.0,
                 trail_start_r: float | None = None) -> tuple[float, bool, str | None]:
    """Stop tras un nuevo extremo favorable `best`: el mismo cálculo que el paper (paper.stop_update). Por defecto, el
    de la v1: break-even a +1 R en la entrada y stop dinámico desde el extremo a max(trail, stop_pct − ganancia %).
    Devuelve (stop, be, tipo del último movimiento o None)."""
    return paper.stop_update(d, entry, stop_pct, trail, best, stop, be, be_r, be_pct, trail_start_r)


def target_price(d: int, ref: float, stop_pct: float, params: dict | None = None) -> float | None:
    """Objetivo final desde ref: target_pct % neto (más el coste de ida y vuelta) o target_r × riesgo. None = sin objetivo."""
    p = _params(params)
    if p.get("target_pct"):
        return ref * (1 + d * (p["target_pct"] + paper.cost_rt_pct()) / 100)
    if p.get("target_r"):
        return ref * (1 + d * p["target_r"] * stop_pct / 100)
    return None


def entry_plan(d: int, entry: float, act: dict, params: dict | None = None) -> dict:
    """Plan de salidas desde la entrada real (el precio de ahora en vivo; la apertura de la vela siguiente en el
    backtest). Stop y objetivo se quedan en los precios calculados al cierre que confirma (act["stop_px"],
    act["target_px"]); el stop no baja del suelo ni pasa del techo. Puerta R:R, solo con objetivo:
    (objetivo − entrada) ≥ min_rr × riesgo y ≥ 3 × coste de ida y vuelta. Una entrada que ya ha cruzado el stop no
    se hace (la ruptura ha fallado).
    Devuelve los campos del ticket (stopPct, trailPct, tp1R, tp1Frac, tp2R, beR, trailStartR), rr, ok y why. Con el
    stop de la v1 (cercano) no lleva beR: el paper gestiona entonces las salidas de la v1 (break-even en la entrada)."""
    p = _params(params)
    risk = d * (entry - act["stop_px"]) / entry * 100
    stop = round(min(p["stop_max_pct"], max(stop_floor_pct(p), risk)), 3)
    out = {"stopPct": stop, "trailPct": act.get("trailPct"), "tp1R": p["tp1_r"], "tp1Frac": p["tp1_frac"],
           "tp2R": None, "beR": p["be_r"], "trailStartR": p["trail_start_r"], "rr": None, "ok": True,
           "why": f"Sin objetivo fijo · stop {_n(stop)} %"}
    if (p.get("stop_mode") or "cercano") == "cercano":
        del out["beR"]
    if risk <= 0:
        out.update(ok=False, why=f"El precio ({_px(entry)}) ya ha cruzado el stop ({_px(act['stop_px'])}): la ruptura "
                                 "ha fallado")
        return out
    tgt = act.get("target_px")
    if tgt is None:
        return out
    reward = d * (tgt - entry) / entry * 100
    rr = reward / stop
    cost = paper.cost_rt_pct()
    out.update(tp2R=round(rr, 3), rr=round(rr, 3))
    if rr < p["min_rr"] - 1e-9:
        out.update(ok=False, why=f"R:R {_n(rr)} < {_n(p['min_rr'])} desde la entrada real {_px(entry)}: "
                                 "el movimiento ya ha ocurrido")
    elif reward < 3 * cost - 1e-9:
        out.update(ok=False, why=f"Recorrido hasta el objetivo {_n(reward)} % < 3 × coste de ida y vuelta "
                                 f"({_n(3 * cost)} %)")
    else:
        out["why"] = (f"R:R {_n(rr)} desde {_px(entry)}: objetivo {_px(tgt)} (+{_n(reward)} %), stop {_n(stop)} %")
    return out


def moving_flag(bars: list, open_ms: int, params: dict | None = None) -> dict | None:
    """Bandera MOVING: cambio entre la apertura de hace moving_h horas y el último cierre antes de la apertura de la
    ventana. Si llega a moving_pct (en cualquier sentido), el movimiento ya ha ocurrido. None si está desactivada."""
    p = _params(params)
    if not p.get("moving_pct"):
        return None
    h = p["moving_h"]
    i = bar_index(bars, open_ms)
    ref_t = open_ms - int(h * H)
    k = bar_index(bars, ref_t)
    if i == 0 or k >= i or bars[k][T] > ref_t + 30 * MIN:
        return {"moving": False, "pct": None, "hours": h,
                "why": f"Sin velas de las {_n(h, 0)} h previas: no se comprueba si ya se movió"}
    pct = (bars[i - 1][C] / bars[k][O] - 1) * 100
    mv = abs(pct) >= p["moving_pct"]
    why = (f"El movimiento ya ha ocurrido: {_sg(pct)} % en las {_n(h, 0)} h previas (≥ {_n(p['moving_pct'], 1)} %): "
           "no se entra" if mv else f"{_sg(pct)} % en las {_n(h, 0)} h previas (< {_n(p['moving_pct'], 1)} %)")
    return {"moving": mv, "pct": round(pct, 3), "hours": h, "why": why}


def day_limits(entries: int, risk_pct: float, new_risk: float, params: dict | None = None) -> str | None:
    """Límites diarios globales (día de Madrid, todas las ventanas): tope de entradas y presupuesto de riesgo
    comprometido (suma del riesgo en stop de las entradas del día, ganen o pierdan). Motivo para no entrar o None."""
    p = _params(params)
    cap, budget = p.get("max_day_entries"), p.get("max_day_risk_pct")
    if cap is not None and entries >= cap:
        return f"Tope diario de {cap} entradas alcanzado (todas las ventanas, día de Madrid)"
    if budget is not None and risk_pct + new_risk > budget + 1e-9:
        return (f"Presupuesto de riesgo del día agotado: {_n(risk_pct)} % comprometido + {_n(new_risk)} % > "
                f"{_n(budget)} %")
    return None


# ---------------------------------------------------------------- una moneda en una apertura (sin red)

class CoinRun:
    """Reglas de una moneda en una apertura: rango, entrada, reentradas y fin de la sesión. No sabe de red ni de
    órdenes: el motor en vivo le pasa las velas y le cuenta las salidas del paper; el backtest simula las salidas."""

    def __init__(self, coin: str, open_ms: int, signal_dir: int, params: dict | None = None, free: bool = False):
        self.p = _params(params)
        self._cp = {**self.p, "free": free}   # parámetros de confirm (sin copiarlos en cada vela)
        self.coin, self.open_ms, self.dir, self.free = coin, open_ms, signal_dir, free
        self.end_ms = open_ms + self.p["session_max_min"] * MIN
        self.state = "midiendo_rango"
        self.rng: dict | None = None
        self.rng_start, self.rng_min = open_ms, self.p["range_min"]
        self.entries = 0
        self.losses_row = 0
        self.rejects = 0
        self.wait_until = 0
        self.resume_ms = 0               # tras una salida, las velas anteriores a esto ya no cuentan
        self.results: list[float] = []   # % neto de cada operación cerrada
        self.last_why = f"Midiendo el rango de los {self.rng_min} primeros minutos"
        self.end_why: str | None = None
        self.feats: dict = {}
        self.moving: dict | None = None   # bandera MOVING ({} si está desactivada), en la primera vela de la sesión
        self._back = "armada"

    @property
    def done(self) -> bool:
        return self.state == "terminada"

    def end(self, why: str) -> dict:
        self.state, self.end_why, self.last_why = "terminada", why, why
        return {"action": "end", "why": why}

    def set_signal(self, d: int) -> None:
        """Señal actualizada (en vivo: en la apertura y antes de cada reentrada). Si deja de acompañar, se acaba."""
        if self.done or self.free or d == self.dir:
            return
        if d and self.entries == 0 and self.state != "en_posicion":
            self.dir = d
            self.last_why = "La señal ha cambiado de sentido antes de entrar: se sigue la nueva"
        elif self.state != "en_posicion":
            self.end("La señal ya no acompaña: no se busca otra entrada")

    def on_bar(self, bars: list, i: int, feats: dict, live_ok: bool = True) -> dict | None:
        """bars[i] es la última vela cerrada y feats sus indicadores. Devuelve {"action": "enter", ...},
        {"action": "end", ...} o None. En posición no hace nada (manda el stop dinámico)."""
        if self.state in ("terminada", "en_posicion"):
            return None
        bar = bars[i]
        if bar[T] < self.resume_ms:   # vela de cuando había posición: ya pasó (en vivo llegan tras la salida)
            return None
        self.feats = feats
        t = bar[T] + MIN
        if self.moving is None and bar[T] >= self.open_ms:   # con las velas previas a la apertura, una vez
            self.moving = moving_flag(bars, self.open_ms, self.p) or {}
            if self.moving.get("moving"):
                return self.end(self.moving["why"])
        if t >= self.end_ms:
            return self.end(f"Fin de la sesión: {_n(self.p['session_max_min'] / 60, 0)} h desde la apertura")
        if self.end_ms - t < self.p["last_entry_min"] * MIN:
            return self.end(f"Quedan menos de {self.p['last_entry_min']} min de sesión: no se abren más operaciones")
        rv15 = feats.get("rvol15")
        if self.entries and rv15 is not None and rv15 < self.p["exhaust_rvol"]:   # tras operar: ¿queda volumen?
            return self.end(f"Volumen agotado: rvol de 15 min {_n(rv15)} < {_n(self.p['exhaust_rvol'])}")
        if self.rng is None:
            if t < self.rng_start + self.rng_min * MIN:
                return None
            rng = opening_range(bars, self.rng_start, self.rng_min)
            if not rng or rng["n"] < max(1, self.rng_min // 2):
                return self.end("Faltan velas para medir el rango")
            self.rng = rng
            if self.state == "midiendo_rango":
                self.state = "armada"
        el = (t - self.open_ms) / MIN
        if self.entries == 0 and el > self.p["first_entry_max_min"]:
            return self.end(f"Sin ruptura confirmada en los primeros {self.p['first_entry_max_min']} min")
        if self.state == "rearmada" and (rv15 is None or rv15 < self.p["rearm_rvol"]):
            rv = "sin dato" if rv15 is None else _n(rv15)
            self.last_why = f"Rearmada, pero el rvol de 15 min ({rv}) no llega a {_n(self.p['rearm_rvol'])}"
            return None
        if t < self.wait_until:
            return None
        tf = self.p.get("confirm_tf") or 1
        if tf > 1 and (t % (tf * MIN) or t - tf * MIN < self.rng_start + self.rng_min * MIN):
            return None   # solo al cierre de una vela de tf min (agregada) que empieza tras el rango
        c = confirm(self.dir, bar, self.rng, feats.get("rvol"), feats.get("buy_ratio"), self._cp)
        self.last_why = c["why"]
        if not c["ok"]:
            return None
        if not live_ok:
            self.last_why = f"Ruptura confirmada en una vela de {tf} min antigua: tarde para entrar"
            return None
        d, px = c["dir"], bar[C]
        st = initial_stop(d, px, self.rng, feats.get("atr"), self.p, feats.get("atr5"))
        tr = trail_pct(px, feats.get("atr5") if self.p.get("trail_tf") == 5 else feats.get("atr"), st["pct"], self.p)
        tgt = target_price(d, px, st["pct"], self.p)
        self._back, self.state = self.state, "en_posicion"
        self.entries += 1
        what = "Entrada" if self.entries == 1 else f"Reentrada {self.entries - 1}"
        exits = f"stop dinámico a {_n(tr)} % del extremo" if tr else "sin stop dinámico"
        if tgt is not None:
            exits += f"; objetivo {_px(tgt)}"
        return {"action": "enter", "dir": d, "price": px, "t": t, "n": self.entries, "stopPct": st["pct"],
                "trailPct": tr, "stop_kind": st["kind"], "rng": dict(self.rng), "stop_px": st["price"],
                "target_px": tgt, "tf": tf, "why": f"{what}: {c['why']}. {st['why']}; {exits}"}

    def on_paused(self, t: int, why: str) -> None:
        """El paper está detenido: se deshace la entrada sin contarla como rechazo y se espera (al reanudar sigue)."""
        if self.state != "en_posicion":
            return
        self.entries -= 1
        self.state = self._back
        self.wait_until = t + self.p["reject_wait_min"] * MIN
        self.last_why = f"En espera: {why}"

    def on_rejected(self, t: int, why: str) -> None:
        """El paper no ejecutó la entrada: se vuelve a esperar una ruptura (tras una pausa)."""
        if self.state != "en_posicion":
            return
        self.entries -= 1
        self.rejects += 1
        self.state = self._back
        self.wait_until = t + self.p["reject_wait_min"] * MIN
        self.last_why = f"Entrada no ejecutada: {why}"
        if self.rejects >= self.p["max_rejects"]:
            self.end(f"{self.rejects} entradas no ejecutadas: se deja la apertura ({why})")

    def on_exit(self, t: int, ret_pct: float, why: str) -> None:
        """La posición se ha cerrado (ret_pct neto). Decide si se rearma para otra entrada o se acaba."""
        if self.state != "en_posicion":
            return
        self.results.append(ret_pct)
        self.losses_row = self.losses_row + 1 if ret_pct < 0 else 0
        done = f"Salida ({why}, {_sg(ret_pct)} %)"
        if self.losses_row >= self.p["max_losses_row"]:
            self.end(f"{done}: {self.losses_row} pérdidas seguidas, se cierra la sesión")
        elif self.entries >= 1 + self.p["max_reentries"]:
            self.end(f"{done}: máximo de {self.p['max_reentries']} reentradas")
        elif t >= self.end_ms - max(self.p["rearm_range_min"], self.p["last_entry_min"]) * MIN:
            self.end(f"{done}: queda poca sesión para otra entrada")
        else:
            self.state, self.rng = "rearmada", None
            self.rng_start, self.rng_min = -(-t // MIN) * MIN, self.p["rearm_range_min"]
            self.resume_ms = self.rng_start
            self.last_why = f"{done}: rearmada, midiendo un rango nuevo de {self.rng_min} min"

    def restore_entry(self, rec: dict | None = None) -> None:
        """Al reiniciar el bot: una entrada de esta sesión que ya estaba hecha en el paper (abierta, en curso o, seguida
        de on_exit, ya cerrada). rec (la operación del paper) solo lo usa el modo impulso."""
        if self.done or self.state == "en_posicion":
            return
        self._back, self.state = self.state, "en_posicion"
        self.entries += 1


# ---------------------------------------------------------------- agenda de aperturas (pura)

def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def group_info(keys) -> tuple[str, str, str]:
    """(id, nombre, bolsa principal) de un grupo de bolsas que abren a la vez."""
    ks = frozenset(keys)
    if ks in GROUPS:
        return GROUPS[ks]
    order = [k for k in TRADED if k in ks]
    return "-".join(order), " y ".join(regions.MARKETS[k].name for k in order), order[0]


def market_open_on(key: str, day: date) -> bool:
    """¿Hay sesión ese día (fecha local)? Lunes a viernes (regions) y sin festivo en el calendario mínimo."""
    return regions.is_trading_day(regions.MARKETS[key], day) and day.isoformat() not in HOLIDAYS.get(key, ())


def openings_on(day: date) -> list[dict]:
    """Aperturas de ese día (fecha local de cada bolsa) agrupadas por instante: [{id, name, markets, lead, open_ms}].
    Lunes a viernes con su cambio de hora (regions). Una ventana con todas sus bolsas en festivo no aparece; si solo
    cierran algunas, sigue con su nombre (las que cierran van en «holiday»)."""
    by: dict[int, list[str]] = {}
    for k in TRADED:
        m = regions.MARKETS[k]
        if regions.is_trading_day(m, day):
            by.setdefault(_ms(regions.open_utc_on(m, day)), []).append(k)
    out = []
    for t, keys in sorted(by.items()):
        markets = [k for k in TRADED if k in keys]
        hol = [k for k in markets if not market_open_on(k, day)]
        if len(hol) == len(markets):
            continue
        gid, name, lead = group_info(keys)
        out.append({"id": gid, "name": name, "markets": markets, "lead": lead, "open_ms": t, "day": day.isoformat(),
                    "holiday": hol})
    return out


def prev_session_opens(lead: str, open_ms: int, n: int = 5, max_days: int = 15) -> list[int]:
    """Aperturas de las n sesiones anteriores de la bolsa (de la más reciente a la más antigua), mirando como mucho
    max_days días atrás."""
    m = regions.MARKETS[lead]
    d = datetime.fromtimestamp(open_ms / 1000, ZoneInfo(m.tz)).date()
    out = []
    for k in range(1, max_days):
        dd = d - timedelta(days=k)
        if market_open_on(lead, dd):
            out.append(_ms(regions.open_utc_on(m, dd)))
            if len(out) >= n:
                break
    return out


# ---------------------------------------------------------------- simulación y puerta de probabilidades (puras)

def pre_min(params: dict | None = None) -> int:
    """Minutos de velas anteriores a la apertura que hacen falta: ATR, rvol sin historia y la bandera MOVING."""
    p = _params(params)
    return max(PRE_MIN, int(p["moving_h"] * 60) + 10 if p.get("moving_pct") else 0)


def pre_coverage_ok(bars: list, open_ms: int, params: dict | None = None) -> bool:
    """¿Hay al menos el 90 % de las velas previas a la apertura que pide pre_min (ATR, rvol y bandera MOVING)? Sin
    ellas la sesión no se juzga con las mismas reglas (MOVING sin comprobar, stop atr5 en el suelo)."""
    pre = pre_min(params)
    return bar_index(bars, open_ms) - bar_index(bars, open_ms - pre * MIN) >= COVERAGE * pre


def simulate_plan(bars: list, j: int, d: int, plan: dict, end_ms: int, fee: float | None = None,
                  slip: float | None = None) -> dict:
    """Operación abierta en la apertura de bars[j] con el plan de salidas de un ticket (stopPct, trailPct, beR,
    trailStartR, tp1R, tp1Frac, tp2R; paper.exit_rules) y gestionada como en el paper. Supuesto pesimista dentro de
    cada vela: primero el stop vigente (a su precio, o a la apertura si la vela ya abre peor); después la parcial y el
    objetivo (órdenes límite, a su precio); después el extremo favorable mueve el stop y, si el contrario toca el stop
    nuevo, sale en él. Costes: fee por lado sobre lo negociado y slip en la entrada y en las salidas a mercado.
    Devuelve {k (vela de salida), t, ret (% neto del nominal), R (ret / stopPct), why, first, mfe (%), mae (%), partial},
    con first = objetivo | stop | ninguno (lo primero que tocó: el objetivo final o un stop; ninguno = salió por tiempo),
    mfe y mae = mejor y peor excursión hasta la salida (en la vela de salida, hasta el objetivo o el stop en que sale)
    y partial = se hizo la salida parcial."""
    fee = config.FEE_PCT / 100 if fee is None else fee
    slip = paper.COST_SLIP_BPS / 1e4 if slip is None else slip
    ru = paper.exit_rules(plan)
    sp = float(plan["stopPct"])
    entry = bars[j][O] * (1 + d * slip)
    r = entry * sp / 100
    stop = entry - d * r
    tp1 = entry + d * r * ru["tp1R"] if ru["tp1_frac"] else None
    tp2 = entry + d * r * ru["tp2R"] if ru["tp2R"] else None
    best, worst, be, kind, part = entry, entry, False, "inicial", False
    rem, gross, turn = 1.0, 0.0, 1.0   # parte abierta, resultado bruto y nominal negociado, en partes del nominal
    k, ex, why, first = len(bars) - 1, None, "fin de datos", "ninguno"
    for k in range(j, len(bars)):
        b = bars[k]
        adv, fav = (b[LO], b[HI]) if d > 0 else (b[HI], b[LO])
        if d * (adv - stop) <= 0:   # 1) el stop vigente, antes que nada (MAE hasta el precio de salida)
            lvl = min(stop, b[O]) if d > 0 else max(stop, b[O])
            if d * (lvl - worst) < 0:
                worst = lvl
            ex, why, first = lvl * (1 - d * slip), kind, "stop"
            break
        w0 = worst
        if d * (adv - worst) < 0:   # pesimista: el extremo contrario antes que la parcial y el objetivo
            worst = adv
        if tp1 is not None and d * (fav - tp1) >= 0:   # 2) la parcial
            q = min(rem, ru["tp1_frac"])
            part = True
            if q >= rem - 1e-12:
                best = tp1 if d * (tp1 - best) > 0 else best
                ex, why, first = tp1, "objetivo", "objetivo"
                break
            gross += q * d * (tp1 / entry - 1)
            turn += q * tp1 / entry
            rem -= q
            tp1 = None
        if tp2 is not None and d * (fav - tp2) >= 0:   # 3) el objetivo (MFE hasta el objetivo)
            best = tp2 if d * (tp2 - best) > 0 else best
            ex, why, first = tp2, "objetivo", "objetivo"
            break
        if d * (fav - best) > 0:
            best = fav
        stop, be, mv = paper.stop_update(d, entry, sp, ru["trail"], best, stop, be, ru["be_r"], ru["be_pct"],
                                         ru["trail_start_r"])
        kind = mv or kind
        if d * (adv - stop) <= 0:   # 4) pesimista: el contrario toca el stop nuevo (MAE hasta ese stop)
            worst = stop if d * (stop - w0) < 0 else w0
            ex, why, first = stop * (1 - d * slip), kind, "stop"
            break
        if b[T] + MIN >= end_ms:   # 5) fin de la ventana
            ex, why = b[C] * (1 - d * slip), "tiempo"
            break
    if ex is None:
        ex = bars[k][C] * (1 - d * slip)
    gross += rem * d * (ex / entry - 1)
    turn += rem * ex / entry
    ret = (gross - fee * turn) * 100
    return {"k": k, "t": bars[k][T] + MIN, "ret": ret, "R": ret / sp, "why": why, "first": first,
            "mfe": d * (best - entry) / entry * 100, "mae": d * (worst - entry) / entry * 100, "partial": part}


def session_case(coin: str, d: int, open_ms: int, bars: list, vol_sum=None, prev_opens: list[int] | None = None,
                 params: dict | None = None, fee: float | None = None, slip: float | None = None) -> dict | None:
    """Primera ruptura de una sesión pasada que cumple la misma regla (CoinRun con dirección d) y su resultado con el
    mismo plan: entrada en la apertura de la vela siguiente, puerta R:R desde ahí y simulate_plan. Si la puerta R:R la
    rechaza, se sigue buscando como en vivo. None si no hubo ruptura válida o faltan velas (de la sesión o de antes:
    pre_coverage_ok, como en el backtest)."""
    p = _params(params)
    slip = paper.COST_SLIP_BPS / 1e4 if slip is None else slip
    i0 = bar_index(bars, open_ms)
    i_end = bar_index(bars, open_ms + p["session_max_min"] * MIN)
    if i0 < 1 or i_end - i0 < COVERAGE * p["session_max_min"] or not pre_coverage_ok(bars, open_ms, p):
        return None
    run = CoinRun(coin, open_ms, d, p)
    for i in range(i0, i_end):
        act = run.on_bar(bars, i, features(bars, i, open_ms, vol_sum, prev_opens, p))
        if run.done:
            return None
        if not act or act["action"] != "enter":
            continue
        if i + 1 >= len(bars):
            return None
        plan = entry_plan(d, bars[i + 1][O] * (1 + d * slip), act, p)
        if not plan["ok"]:
            run.on_rejected(act["t"], plan["why"])
            continue
        res = simulate_plan(bars, i + 1, d, plan, run.end_ms, fee, slip)
        return {"open_ms": open_ms, "t": act["t"], "first": res["first"], "R": round(res["R"], 4),
                "ret": round(res["ret"], 4), "tR": plan["tp2R"], "why": res["why"]}
    return None


def gate_verdict(cases: list[dict], params: dict | None = None, sessions: int | None = None) -> dict:
    """Veredicto de la puerta con las rupturas pasadas (session_case). P(objetivo primero) entre las que tocaron objetivo
    o stop frente al punto muerto 1/(1+R) (R = objetivo medio en R): a menos de 5 puntos, moneda al aire; de 5 a 15, se
    inclina; desde 15, claramente. Se cumple con n ≥ gate_min_n, ventaja ≥ gate_pp puntos y R medio (neto) > 0."""
    p = _params(params)
    n = len(cases)
    hits = sum(c["first"] == "objetivo" for c in cases)
    stops = sum(c["first"] == "stop" for c in cases)
    dec = hits + stops
    avg_r = sum(c["R"] for c in cases) / n if n else None
    trs = [c["tR"] for c in cases if c.get("tR")]
    out = {"n": n, "decided": dec, "hits": hits, "stops": stops, "neither": n - dec, "sessions": sessions,
           "p_target": None, "breakeven": None, "target_R": None, "edge_pp": None,
           "avg_R": round(avg_r, 3) if avg_r is not None else None, "ok": False, "min_n": p["gate_min_n"],
           "gate_pp": p["gate_pp"]}
    if not trs:
        out.update(verdict="sin_objetivo" if n else "pocos_casos",
                   text="Sin objetivo fijo no hay punto muerto: la puerta no se cumple" if n
                   else f"Ninguna ruptura pasada con la misma regla en {sessions or 0} sesiones: la puerta no se cumple")
        return out
    t_r = sum(trs) / len(trs)
    be = 1 / (1 + t_r)
    pt = hits / dec if dec else 0.0
    edge = (pt - be) * 100
    out.update(p_target=round(pt, 4), breakeven=round(be, 4), target_R=round(t_r, 3), edge_pp=round(edge, 2))
    facts = (f"objetivo antes que stop en {_n(pt * 100, 0)} % de {dec} rupturas pasadas (punto muerto {_n(be * 100, 0)} % "
             f"a {_n(t_r, 1)} R), R medio {_sg(avg_r)} con costes")
    if dec < p["gate_min_n"]:
        out.update(verdict="pocos_casos", text=f"Pocos casos ({dec} de {p['gate_min_n']} como mínimo): {facts}; "
                                               "la puerta no se cumple")
        return out
    side = "a favor" if edge > 0 else "en contra"
    if abs(edge) < COIN_FLIP_PP:
        verdict, label = "moneda_al_aire", "Moneda al aire"
    elif abs(edge) < STRONG_PP:
        verdict, label = ("a_favor" if edge > 0 else "en_contra"), f"Se inclina {side}"
    else:
        verdict, label = ("claramente_a_favor" if edge > 0 else "claramente_en_contra"), f"Claramente {side}"
    ok = edge >= p["gate_pp"] and round(avg_r, 4) > 0   # redondeado: un 0 con ruido de coma flotante no es ventaja
    text = f"{label}: {facts} → {'puerta superada' if ok else 'no se entra'}"
    if n and (n - dec) / n > MANY_NEITHER:
        text += f" · ojo: el {_n((n - dec) / n * 100, 0)} % no tocó ni objetivo ni stop"
    out.update(verdict=verdict, text=text, ok=ok)
    return out


def odds_gate(coin: str, d: int, open_ms: int, lead: str, get_bars, vol_sum=None, params: dict | None = None,
              fee: float | None = None, slip: float | None = None) -> dict:
    """Puerta de probabilidades («máquina del tiempo» adaptada) de una moneda en una apertura: con las gate_sessions
    sesiones ANTERIORES de esa ventana (bolsa lead), reúne las rupturas que cumplieron la misma regla y las mide con el
    mismo plan (session_case), y da el veredicto (gate_verdict). get_bars(a, b): velas de 1 min que abren en [a, b);
    vol_sum(a, b): su volumen (None: se suma de las velas de cada sesión). Solo usa datos anteriores a open_ms."""
    p = _params(params)
    opens = prev_session_opens(lead, open_ms, p["gate_sessions"], max_days=2 * p["gate_sessions"] + 30)
    pre = pre_min(p)
    cases = []
    for po in opens:
        bars = get_bars(po - pre * MIN, min(open_ms, po + (p["session_max_min"] + 1) * MIN))
        c = session_case(coin, d, po, bars, vol_sum, prev_session_opens(lead, po, p["rvol_sessions"]), p, fee, slip)
        if c:
            cases.append(c)
    return {**gate_verdict(cases, p, len(opens)), "dir": d}


def load_history(coin: str, months: int = 4, today: date | None = None):
    """Velas de 1 min de los últimos meses que guarda el backtest en config.HIST_DIR/1m (zips mensuales y el mes en
    curso de la API), sin red. Devuelve una backtest_aperturas.Series (slice, vol_sum) o None si no hay archivos."""
    from . import backtest_aperturas as bta   # aquí y no arriba: bta importa este módulo
    today = today or datetime.now(timezone.utc).date()
    mlist, cur = bta.month_list(today, months)
    paths = []
    for ym in mlist + [cur]:
        z, part = bta.zip_path(bta.SYMS[coin], ym), bta.part_path(bta.SYMS[coin], ym)
        if z.exists():
            paths.append(z)
        elif part.exists():
            paths.append(part)
    return bta.load_series(paths) if paths else None


# ---------------------------------------------------------------- señal (pura)

def _market_of(text: str) -> str | None:
    w = (text or "").lower()
    for k, m in regions.MARKETS.items():
        if k in w.replace(" ", "").replace("-", "") or m.name.lower() in w:
            return k
    return None


def _plan(coin: str, markets: list[str], open_ms: int, plans, now: int) -> tuple[float, str] | None:
    """Último plan del estratega (fase 2, entrada programada) para esta ventana: (+1/−1 × confianza, texto)."""
    by_ts: dict[int, list[dict]] = {}
    for e in plans or []:
        if not isinstance(e, dict) or e.get("kind", "strategy") not in ("strategy", "nostrategy"):
            continue
        patch = e.get("patch") or {}
        st = patch.get("strat") if "strat" in patch else e
        if not isinstance(st, dict) or patch.get("src") == SRC or str(e.get("story") or "").startswith("a-"):
            continue
        ts = int(_f(e.get("ts"), 0))
        if ts > now or ts < open_ms - 3 * H:
            continue
        mk = _market_of(st.get("win") or "") or _market_of(e.get("story") or "") or _market_of(e.get("title") or "")
        if mk not in markets:
            continue
        if not st.get("none") and st.get("timing") != "window":
            continue
        win_at = _f(st.get("winAt"))
        if win_at and abs(win_at - open_ms) > 45 * MIN:
            continue
        by_ts.setdefault(ts, []).append(st)
    if not by_ts:
        return None
    last = by_ts[max(by_ts)]
    if any(s.get("none") for s in last):
        return 0.0, "El estratega no ve operación en esta ventana"
    t = next((s for s in last if s.get("coin") == coin), None)
    if not t:
        return 0.0, f"El plan del estratega no incluye {coin}"
    d = 1 if t.get("side") == "long" else -1
    conf = 1.0 if t.get("llm") else 0.7
    who = "Opus" if t.get("llm") else "reglas"
    return d * conf, f"Plan del estratega ({who}): {'compra' if d > 0 else 'venta'} de {coin}"


def _triage(n: dict) -> dict:
    return (n.get("patch") or {}).get("triage") or n.get("triage") or n


def _news(coin: str, markets: list[str], news, now: int, p: dict) -> tuple[float, int, str]:
    """Presión de las noticias triadas: Σ dirección × magnitud × credibilidad × decaimiento × peso de la fuente, en tanh
    (−1 a 1). La credibilidad es «credibilidad_ajustada» si viene (con el peso aprendido del medio, bot/aprendizaje.py);
    el peso de la fuente, «srcWeight» (fuentes_vivas: activa 1, prueba 0,5; silenciada o retirada no cuenta)."""
    total, k, top, top_w = 0.0, 0, "", 0.0
    for n in news or []:
        if not isinstance(n, dict) or n.get("archivada") or n.get("kind") == "lowprio":
            continue
        sw = _f(n.get("srcWeight"), 1.0)
        if n.get("srcState") in ("silenciada", "retirada") or sw <= 0:
            continue
        tr = _triage(n)
        rep = tr.get("repercusion") or n.get("repercusion") or {}
        imp = _f(tr.get("importancia"), _f(n.get("importancia")))
        if imp is not None and imp < config.TRIAGE_MIN:
            continue
        ts = _f(n.get("ts"))
        if ts is None:
            continue
        age_h = (now - ts) / H
        if age_h < 0 or age_h > p["news_hours"]:
            continue
        coins = set(rep.get("activos") or []) | set(tr.get("monedas") or n.get("monedas") or [])
        named = coins & set(config.COINS)
        if named and coin not in named:   # habla de otras monedas: no empuja a esta aunque coincida la bolsa
            continue
        bolsa = bool(set(rep.get("bolsas") or []) & set(markets))
        nivel = (tr.get("alcance") or n.get("alcance") or {}).get("nivel")
        if not (bolsa or coin in named or nivel == "global" or tr.get("tipo") in CRIPTO_TIPOS):
            continue
        cred = _f(n.get("credibilidad_ajustada"), _f(tr.get("credibilidad"), 50.0))
        w = (_f(rep.get("direccion"), 0.0) / 100 * _f(rep.get("magnitud"), 0.0) / 100 * cred / 100
             * 0.5 ** (age_h / p["news_half_life_h"]) * min(1.0, sw))
        if w == 0:
            continue
        total += w
        k += 1
        if abs(w) > top_w:
            top_w, top = abs(w), tr.get("resumen") or n.get("resumen") or n.get("titular") or n.get("title") or ""
    return math.tanh(2 * total), k, top


def _pred(coin: str, markets: list[str], open_ms: int, predictions) -> tuple[float, str] | None:
    """Predicciones: la de la ventana cripto de la moneda (60 %) y las de las bolsas de la ventana (40 %)."""
    items = (predictions or {}).get("items") if isinstance(predictions, dict) else predictions
    crypto, bolsas = None, []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        prob, when = _f(it.get("prob_up")), _f(it.get("when_utc"))
        if prob is None or when is None:
            continue
        w = CONF_W.get(it.get("confidence"), 0.5)
        s = (prob - 0.5) * 2 * w
        if it.get("kind") == "cripto" and it.get("target") == coin:
            if "próxima hora" in str(it.get("label") or "").lower() or "-1h-" in str(it.get("id") or ""):
                continue
            if abs(when - open_ms) <= 2 * MIN:
                crypto = (s, prob, it.get("label") or coin)
        elif it.get("kind") == "bolsa" and it.get("target") in markets and abs(when - open_ms) <= 30 * MIN:
            bolsas.append((s, prob, regions.MARKETS[it["target"]].name))
    if crypto is None and not bolsas:
        return None
    parts, score = [], 0.0
    if bolsas:
        b = sum(x[0] for x in bolsas) / len(bolsas)
        parts.append(", ".join(f"{x[2]} {_n(x[1] * 100, 0)} %" for x in bolsas))
        score = b
    if crypto:
        parts.insert(0, f"{crypto[2]} {_n(crypto[1] * 100, 0)} %")
        score = 0.6 * crypto[0] + 0.4 * score if bolsas else crypto[0]
    return score, "Predicciones de subida: " + "; ".join(parts)


def signal_from(coin: str, markets: list[str], open_ms: int, plans=None, news=None, predictions=None,
                now: int | None = None, params: dict | None = None) -> dict:
    """Dirección y fuerza de la señal de una moneda para la apertura de esas bolsas, con sus factores.
    (a) último plan del estratega para la ventana, (b) noticias triadas recientes de esas bolsas o globales/cripto,
    (c) predicciones de la bolsa o de la ventana cripto. Lo que falta cuenta como neutral."""
    p = _params(params)
    now = open_ms if now is None else now
    drivers, score = [], 0.0
    pl = _plan(coin, markets, open_ms, plans, now)
    if pl:
        w = W_PLAN * pl[0]
        drivers.append({"factor": pl[1], "peso": round(w, 3)})
        score += w
    ns, k, top = _news(coin, markets, news, now, p)
    if k:
        w = W_NEWS * ns
        sense = "a favor de subidas" if ns > 0 else "a favor de bajadas" if ns < 0 else "neutras"
        drivers.append({"factor": f"{k} noticia{'s' if k > 1 else ''} de las últimas {p['news_hours']} h {sense}"
                                  + (f" (la de más peso: «{_cut(top, 90)}»)" if top else ""), "peso": round(w, 3)})
        score += w
    pr = _pred(coin, markets, open_ms, predictions)
    if pr:
        w = W_PRED * pr[0]
        drivers.append({"factor": pr[1], "peso": round(w, 3)})
        score += w
    strength = min(1.0, abs(score))
    d = (1 if score > 0 else -1) if strength >= p["min_strength"] and score else 0
    lead = ", ".join(f"{x['factor']} ({_sg(x['peso'])})" for x in sorted(drivers, key=lambda x: -abs(x["peso"]))[:3])
    if d:
        why = f"Señal {'verde' if d > 0 else 'roja'} en {coin}, fuerza {_n(strength)}: {lead or 'sin factores'}"
    else:
        why = (f"Sin señal clara en {coin}: fuerza {_n(strength)} < {_n(p['min_strength'])}"
               + (f" ({lead})" if lead else " (sin plan, noticias ni predicciones para esta apertura)"))
    return {"dir": d, "strength": round(strength, 3), "score": round(score, 3), "drivers": drivers, "why": why}


SRC_KINDS = ("captured", "corroborated", "srcstate")   # eventos que dicen el medio y el estado de la fuente


def story_sources(store, stories) -> dict[str, dict]:
    """Medio («outlet»), estado y peso de la fuente de cada historia: de su captada y de las subidas de estado
    (confirmación por una fuente mejor o «srcstate»; la última manda). Una consulta por el índice de story; con un store
    sin base (pruebas) o sin datos, {}."""
    db = getattr(store, "db", None)
    ids = list(dict.fromkeys(s for s in stories if s))
    out: dict[str, dict] = {}
    if db is None or not ids:
        return out
    from . import aprendizaje   # perezoso: el aprendizaje importa este módulo
    marks = ",".join("?" * len(SRC_KINDS))
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows = db.execute(f"SELECT story, kind, json_extract(body, '$.patch.srcState'), "
                          f"json_extract(body, '$.patch.srcWeight'), json_extract(body, '$.patch.outlet'), "
                          f"json_extract(body, '$.patch.via'), json_extract(body, '$.patch.url'), "
                          f"json_extract(body, '$.patch.srcId') FROM events WHERE story IN ({','.join('?' * len(part))}) "
                          f"AND kind IN ({marks}) ORDER BY ts, rowid", (*part, *SRC_KINDS)).fetchall()
        for story, kind, estado, peso, outlet, via, url, src_id in rows:
            d = out.setdefault(story, {})
            if kind == "captured" and "outlet" not in d:
                d["outlet"] = aprendizaje.outlet_from_patch({"outlet": outlet, "via": via, "url": url, "srcId": src_id})
            if estado is not None:
                d["srcState"] = estado
            if peso is not None:
                d["srcWeight"] = _f(peso, 1.0)
    return out


def weigh_news(news: list[dict], sources: dict[str, dict]) -> list[dict]:
    """Copia de las triadas con el estado y el peso de su fuente y la credibilidad ajustada por el peso aprendido de su
    medio (aprendizaje.adjusted_credibility; con pocos datos el peso es 1 y no cambia nada)."""
    from . import aprendizaje
    out = []
    for e in news:
        src = sources.get(e.get("story")) or {}
        cred = _triage(e).get("credibilidad")
        extra = {k: src[k] for k in ("srcState", "srcWeight") if k in src}
        adj = aprendizaje.adjusted_credibility(cred, src.get("outlet"))
        if adj is not None:
            extra["credibilidad_ajustada"] = adj
        out.append({**e, **extra, "medio": src.get("outlet")} if extra or src else e)
    return out


def signal_getter(bus, clock=now_ms, params: dict | None = None, cache_s: float = 20.0):
    """get_signal(coin, window) para OpeningEngine: lee del bus los planes del estratega, las noticias triadas y las
    predicciones, y llama a signal_from. Las lecturas de la base se reutilizan durante cache_s. Cada noticia lleva el
    estado y el peso de su fuente y la credibilidad con el peso aprendido de su medio (story_sources, weigh_news): las
    de fuentes silenciadas no cuentan."""
    p = _params(params)
    memo = {"at": -1e18, "plans": [], "news": [], "err_at": -1e18}

    def load(now: int) -> None:
        if time.monotonic() - memo["at"] < cache_s:
            return
        evs = bus.store.events_since(now - int(max(p["news_hours"], 4) * H), ("triaged", "strategy", "nostrategy"))
        memo["plans"] = [e for e in evs if e.get("kind") in ("strategy", "nostrategy")]
        news = [e for e in evs if e.get("kind") == "triaged"]
        try:   # sin pesos (fallo de la base o del aprendizaje) la señal sigue como antes
            news = weigh_news(news, story_sources(bus.store, [e.get("story") for e in news]))
        except Exception:
            if time.monotonic() - memo["err_at"] > 600:
                memo["err_at"] = time.monotonic()
                log.exception("Aperturas: no pude leer el medio y el estado de las noticias; van sin ese peso")
        memo["news"] = news
        memo["at"] = time.monotonic()

    def get_signal(coin: str, window: dict) -> dict:
        now = clock()
        load(now)
        return signal_from(coin, window["markets"], window["open_ms"], memo["plans"], memo["news"],
                           bus.state.get("predictions"), now, p)

    return get_signal


def load_recommendation() -> tuple[dict, str | None]:
    """Parámetros elegidos por el backtest (solo con el 70 % inicial de los datos) en aperturas.json y, si no sirven,
    el motivo. Solo vale la pasada v2 completa (version ≥ 2 y al menos REC_MIN_MONTHS meses)."""
    path = config.BACKTEST_DIR / "aperturas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "No hay recomendación del backtest (falta aperturas.json)"
    except Exception as e:
        return {}, f"No pude leer aperturas.json ({_cut(e, 80)})"
    if not isinstance(data, dict):
        return {}, "aperturas.json no tiene el formato esperado"
    months = _f((data.get("period") or {}).get("months"), 0)
    rec = data.get("recommended")
    if _f(data.get("version"), 0) < 2:
        return {}, "aperturas.json es de la v1 del backtest"
    if months < REC_MIN_MONTHS:
        return {}, f"La recomendación de aperturas.json sale de {_n(months, 0)} meses (< {REC_MIN_MONTHS})"
    if not isinstance(rec, dict) or not rec:
        return {}, "aperturas.json no trae parámetros recomendados"
    return {k: v for k, v in rec.items() if k in PARAMS}, None


def recommended_params() -> dict:
    """Parámetros recomendados por el backtest de 24 meses ({} si no hay una recomendación válida)."""
    return load_recommendation()[0]


# ---------------------------------------------------------------- modo impulso (operativa del usuario, 26-10-2026)
# No se adivina la dirección: se mide el movimiento de los primeros minutos de la apertura y se sigue, con las mismas
# reglas que la familia «Movimiento de los primeros minutos» del laboratorio (fuente ap_mom, entradas t:N, pb y brk,
# confirmación de volumen relativo desde la apertura y filtros de volatilidad y de BTC). Las noticias y la apertura
# anterior son respaldo y medida: filtran (news_mode) y etiquetan cada entrada. Largos al contado y cortos como
# perpetuos (el paper cobra el funding). Salida: horquilla pura (stop y objetivo fijos desde la entrada real; sin
# break-even, stop dinámico ni parciales) con 7 días de seguridad como único tope de tiempo.

STRATEGIES = ("impulso", "ruptura")
MODE_IMP = "Seguir la tendencia"     # uno de claude_agents.MODES
DAY = 24 * H
LAB_WINDOWS = ("tokio-seul", "china", "europa", "saopaulo", "nuevayork")   # ventanas del laboratorio
LAB_WIN_SETS = {"todas": LAB_WINDOWS, "nuevayork": ("nuevayork",), "europa": ("europa",),
                "asia": ("tokio-seul", "china"), "america": ("saopaulo", "nuevayork")}
LAB_COIN_SETS = {"todas": tuple(config.COINS), "BTC+ETH": ("BTC", "ETH"), "SOL+XRP": ("SOL", "XRP"),
                 **{c: (c,) for c in config.COINS}}
ATR_H_N, ATR_LOOK_H = 14, 720        # ATR de 14 h en percentil de las 720 h previas (30 días), como el laboratorio
LAB_BREAK_BPS, LAB_BREAK_FRAC = 2.0, 0.05   # margen de la ruptura del laboratorio (sobre la apertura y el rango)
VOL_FILTERS = ("todos", "no_baja", "alta", "baja")
BTC_FILTERS = ("ninguno", "no_contra", "a_favor")
NEWS_MODES = ("no_contra", "a_favor", "etiqueta")
IMP_STATES = ("esperando_apertura", "midiendo_impulso", "esperando_entrada", "en_posicion", "terminada")
PRIOR_TRIES_IMP = 6                  # intentos de bajar las sesiones anteriores (rvol) antes de darlas por perdidas
INST_TEXT = {"spot": "al contado", "perp": "en perpetuo"}
# Preset IMPULSO: la prometedora más sólida del laboratorio (3 años, 2023-11 → 2026-10, lista congelada el 26-10-2026):
# +0,14 R en 278 operaciones fuera de muestra y permutación p = 0,002, pero NO pasa la corrección por número de pruebas.
# No es una ventaja demostrada: es lo que se mide en paper. Única diferencia, decisión del usuario: 7 días de seguridad en
# vez de cerrar en la misma apertura del día siguiente (el laboratorio lo midió con esto último).
# Comprobado el 27-10-2026 con las velas de HIST_DIR/1m (BACKTEST_DIR/controles/compara_impulso.py): ImpulseRun da las
# mismas 278 entradas fuera de muestra que el laboratorio y, con su salida, las mismas 278 salidas (R medio +0,140).
# Con 7 días de seguridad (sin validar en el laboratorio): 202 operaciones fuera, R medio +0,35 (+0,13 dentro).
LAB_REF = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"
IMPULSO = {
    "estrategia": "impulso",
    "measure_min": 15,          # minutos en que se mide el impulso (15, 30 o 60)
    "min_move_pct": 0.2,        # |cierre a +N min / apertura − 1| mínimo para seguirlo
    "entry": "t:30",            # t:N: a +N min · pb: reanudación tras retroceder pb_frac del impulso · brk: ruptura
    "pb_frac": 0.5,
    "entry_end_min": 120,       # la reanudación (o la ruptura) tiene que confirmarse antes de +120 min
    "brk_tf": 1,                # ruptura: cierre de 1 o de 5 min
    "rvol_min": 1.5,            # volumen desde la apertura hasta la decisión / mediana de las 5 sesiones anteriores
    "vol_filter": "todos",      # todos | no_baja | alta | baja (ATR de 14 h en percentil de 30 días)
    "btc_filter": "a_favor",    # ninguno | no_contra | a_favor (cierre diario de BTC frente a sus medias de 20 y 50 d)
    "news_mode": "no_contra",   # no_contra | a_favor | etiqueta
    "windows": list(LAB_WINDOWS),
    "coins": list(config.COINS),
    "sl_pct": 3.0,
    "target_r": 3.0,            # objetivo en R (9 % con el stop de 3 %)
    "max_hold_h": 168,          # sin tope de tiempo: solo 7 días de seguridad
    "min_net_pct": 3.0,         # regla del usuario: (objetivo − entrada) − coste de ida y vuelta − funding ≥ 3 %
    "avg_hold_h": 65,           # duración media para el funding esperado (≈ 65 h con 7 días de seguridad, réplica fuera)
    "long_instrument": "spot",  # spot | perp
    "short_instrument": "perp",  # en contado USDC no se puede ir corto
    "risk_pct": None,           # riesgo por operación en % del capital (None = config.RISK_PER_TRADE_PCT)
    "max_day_entries": 4,       # límites diarios (día de Madrid, entradas del día): una por moneda como mucho
    "max_day_risk_pct": 2.0,
    "session_max_min": 120,     # fin de la fase de entrada (la posición sigue en el paper)
    "lab": LAB_REF,             # estrategia del laboratorio que reproduce (None si se cambia alguna regla)
}
LAB_KEYS = ("measure_min", "min_move_pct", "entry", "rvol_min", "vol_filter", "btc_filter", "windows", "coins",
            "sl_pct", "target_r")   # con todas como en el preset, se reproduce LAB_REF
IMP_STATUS_KEYS = ("estrategia", "measure_min", "min_move_pct", "entry", "pb_frac", "entry_end_min", "brk_tf", "rvol_min",
                   "vol_filter", "btc_filter", "news_mode", "windows", "coins", "sl_pct", "target_r", "target_pct",
                   "max_hold_h", "min_net_pct", "avg_hold_h", "long_instrument", "short_instrument", "min_strength",
                   "risk_pct", "max_day_entries", "max_day_risk_pct", "session_max_min", "lab")


def entry_kind(entry) -> tuple[str, int | None]:
    """("pb" | "brk" | "t", minutos de t:N) de la entrada; ("", None) si no vale."""
    s = str(entry or "").strip().lower()
    if s in ("pb", "brk"):
        return s, None
    if s.startswith("t:") and s[2:].isdigit():
        return "t", int(s[2:])
    return "", None


def _ids(val, sets: dict, allowed, norm) -> tuple[list, list] | None:
    """(ids válidos en el orden de allowed, descartados) de val: una lista, un grupo de sets, un id suelto o ids
    separados por comas («ETH,SOL»); los grupos valen también dentro de una lista. None si val no es nada de eso."""
    if isinstance(val, str):
        s = val.strip()
        val = [s] if s in sets or norm(s) in sets else re.split(r"[,;+\s]+", s)
    if not isinstance(val, (list, tuple)):
        return None
    got: list = []
    for x in val:
        x = str(x).strip()
        got += sets.get(x) or sets.get(norm(x)) or ([norm(x)] if x else [])
    return [a for a in allowed if a in got], [x for x in dict.fromkeys(got) if x not in allowed]


def impulse_params(over: dict | None = None) -> tuple[dict, list[str]]:
    """Parámetros completos del modo impulso (PARAMS + IMPULSO + cambios) y los avisos. Un valor que no vale vuelve al
    del preset; t:N antes de acabar la medida pasa a t:measure_min; windows y coins admiten lista, grupo del
    laboratorio, id suelto o ids separados por comas. Con las reglas del usuario, stop ≥ 1,5 % y objetivo ≥ 2R (con
    aviso si sube algo); siempre horquilla pura."""
    p = {**PARAMS, **IMPULSO, **{k: v for k, v in (over or {}).items() if k != "estrategia"}, "estrategia": "impulso"}
    notes = []

    def back(k: str, why: str) -> None:
        notes.append(f"{k} = {p[k]!r} no vale ({why}): se usa {IMPULSO[k]!r}")
        p[k] = IMPULSO[k]

    for k, cast in (("measure_min", int), ("entry_end_min", int), ("brk_tf", int), ("min_move_pct", float),
                    ("pb_frac", float), ("sl_pct", float), ("target_r", float), ("max_hold_h", float),
                    ("avg_hold_h", float)):
        try:
            p[k] = cast(p[k])
            if p[k] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            back(k, "tiene que ser un número positivo")
    for k, low in (("rvol_min", 0), ("min_net_pct", 0), ("risk_pct", None), ("max_day_risk_pct", None),
                   ("max_day_entries", 1)):   # None = sin filtro, sin tope o el valor de config; low None = > 0
        if p[k] is None:
            continue
        try:
            p[k] = int(p[k]) if k == "max_day_entries" else float(p[k])
            if p[k] < (low or 0) or (low is None and p[k] <= 0):
                raise ValueError
        except (TypeError, ValueError):
            back(k, "None o un número " + (f"≥ {low}" if low is not None else "> 0"))
    if p["measure_min"] >= p["entry_end_min"]:
        back("measure_min", f"tiene que ser menor que entry_end_min ({p['entry_end_min']})")
    kind, m = entry_kind(p["entry"])
    if not kind:
        back("entry", "pb, brk o t:N")
        kind, m = entry_kind(p["entry"])
    if kind == "t" and m < p["measure_min"]:   # antes de medir no se puede: se entra al medir (t:N ≥ N, como el laboratorio)
        notes.append(f"entry {p['entry']!r} es antes de terminar la medida de {p['measure_min']} min: se entra al "
                     f"medir ('t:{p['measure_min']}')")
        m = p["measure_min"]
    p["entry"] = f"t:{m}" if kind == "t" else kind
    for k, opts in (("vol_filter", VOL_FILTERS), ("btc_filter", BTC_FILTERS), ("news_mode", NEWS_MODES),
                    ("long_instrument", paper.INSTRUMENTS), ("short_instrument", paper.INSTRUMENTS)):
        if p[k] not in opts:
            back(k, " | ".join(opts))
    if p["short_instrument"] == "spot":
        notes.append("Cortos al contado: en OKX EU (USDC) no se pueden ejecutar, solo se miden en paper")
    if p["brk_tf"] not in (1, 5):
        back("brk_tf", "1 o 5")
    for k, sets, allowed, norm, what in (
            ("windows", LAB_WIN_SETS, LAB_WINDOWS, str.lower, "ventanas " + ", ".join(LAB_WINDOWS)),
            ("coins", LAB_COIN_SETS, config.COINS, str.upper, "monedas " + ", ".join(config.COINS))):
        got = _ids(p[k], sets, allowed, norm)
        if got and got[1]:
            notes.append(f"{k}: se descarta {', '.join(map(repr, got[1]))} (no es de las {what})")
        if not got or not got[0]:
            back(k, f"{what} o un grupo del laboratorio: " + ", ".join(sets))
        p[k] = list(got[0]) if got and got[0] else list(IMPULSO[k])
    if config.USER_RULES:   # reglas del usuario para toda operación (con aviso si sube algo)
        for k, low, unit in (("sl_pct", config.RULE_MIN_STOP_PCT, " %"), ("target_r", config.RULE_MIN_RR, " R")):
            if p[k] < low - 1e-12:
                notes.append(f"{k} {_n(p[k])}{unit} sube a {_n(low)}{unit} por la regla del usuario")
                p[k] = float(low)
    p["session_max_min"] = max(p["entry_end_min"], m or 0)
    p.update(be_r=None, trail_atr=0.0, trail_start_r=None, tp1_frac=0.0, target_pct=None, gate=False, moving_pct=None)
    p["lab"] = LAB_REF if all(p[k] == IMPULSO[k] for k in LAB_KEYS) else None
    return p, notes


def _ip(params: dict | None) -> dict:
    """Parámetros del modo impulso ya completos (sin rehacerlos si ya lo están)."""
    if params and params.get("estrategia") == "impulso" and all(k in params for k in IMPULSO) \
            and all(k in params for k in PARAMS):
        return params
    return impulse_params(params)[0]


def impulse_setup(params: dict | None = None) -> tuple[dict, str, str | None]:
    """(parámetros, fuente, aviso) del modo impulso en vivo. Con params, esos (fuente «manual»). Sin ellos, el preset
    IMPULSO («preset_impulso»; ninguna recomendación de backtest lo pisa) con los cambios de la variable de entorno
    BOT_IMPULSO_PARAMS (JSON, p. ej. {"entry": "pb", "news_mode": "etiqueta"}; fuente «preset_impulso+entorno»)."""
    notes = []
    if params is not None:
        over, source = dict(params), "manual"
    else:
        over, source = {}, "preset_impulso"
        raw = os.environ.get("BOT_IMPULSO_PARAMS", "").strip()
        if raw:
            try:
                env = json.loads(raw)
                if not isinstance(env, dict):
                    raise ValueError
            except ValueError:
                notes.append("BOT_IMPULSO_PARAMS no es un objeto JSON válido: se ignora")
            else:
                over.update(env)
                source = "preset_impulso+entorno"
                notes.append("Cambios de BOT_IMPULSO_PARAMS: " + ", ".join(sorted(map(str, env))))
    p, bad = impulse_params(over)
    return p, source, "; ".join(notes + bad) or None


def _easter(y: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano anónimo)."""
    a, b, c = y % 19, y // 100, y % 100
    h = (19 * a + b - b // 4 - (b - (b + 8) // 25 + 1) // 3 + 15) % 30
    l_ = (32 + 2 * (b % 4) + 2 * (c // 4) - h - c % 4) % 7
    m = (a + 11 * h + 22 * l_) // 451
    return date(y, (h + l_ - 7 * m + 114) // 31, (h + l_ - 7 * m + 114) % 31 + 1)


def special_session(o: dict) -> bool:
    """São Paulo el miércoles de ceniza: B3 abre a las 13:00 y no a su hora. El laboratorio no la usa (ni el impulso)."""
    if o["id"] != "saopaulo":
        return False
    d = date.fromisoformat(o["day"])
    return d == _easter(d.year) - timedelta(days=46)


def atr_pct_at(hours: list, open_ms: int, n: int = ATR_H_N, look: int = ATR_LOOK_H) -> dict | None:
    """Régimen de volatilidad al abrir, como el laboratorio: ATR de n horas (rango verdadero / cierre anterior) de la
    última hora cerrada al abrir, en percentil (0-1) de las look horas anteriores. Las horas que faltan repiten el último
    cierre. Solo mira horas cerradas antes de open_ms. None sin historia suficiente."""
    last_h = (open_ms - H) // H * H
    grid: list[list] = []
    for h in sorted((h for h in hours if h[T] <= last_h), key=_t0):
        while grid and grid[-1][0] + H < h[T]:
            c = grid[-1][4]
            grid.append([grid[-1][0] + H, c, c, c, c])
        if not grid or h[T] > grid[-1][0]:
            grid.append([h[T], h[1], h[2], h[3], h[4]])
    while grid and grid[-1][0] < last_h:
        c = grid[-1][4]
        grid.append([grid[-1][0] + H, c, c, c, c])
    kk = len(grid) - 1
    if kk < n - 1 + look:
        return None
    tr = []
    for k in range(kk - look - n + 1, kk + 1):
        hh, hl, hc = grid[k][2], grid[k][3], grid[k][4]
        pc = grid[k - 1][4] if k > 0 else hc
        tr.append(max(hh - hl, abs(hh - pc), abs(hl - pc)) / pc)
    atrs = [sum(tr[j - n + 1:j + 1]) / n for j in range(n - 1, len(tr))]
    cur = atrs[-1]
    return {"pct": sum(x < cur for x in atrs[:-1]) / look, "atr_pct": cur * 100, "hour": grid[kk][0]}


def btc_trend_at(closes: dict, open_ms: int) -> dict | None:
    """Tendencia diaria de BTC al abrir, como el laboratorio: cierre del último día UTC completo frente a sus medias de
    20 y 50 días (+1 por encima de las dos, −1 por debajo de las dos, 0 entre medias). closes: {día UTC (ms // DAY):
    cierre}; los días que faltan repiten el último cierre. None sin 50 días de historia o sin el día de ayer."""
    if not closes:
        return None
    d0, y = min(closes), open_ms // DAY - 1
    if y - d0 < 49 or y > max(closes):
        return None
    cl, last = [], None
    for d in range(d0, y + 1):
        last = closes.get(d, last)
        cl.append(last)
    c, m20, m50 = cl[-1], sum(cl[-20:]) / 20, sum(cl[-50:]) / 50
    return {"dir": 1 if c > m20 and c > m50 else -1 if c < m20 and c < m50 else 0, "close": c, "ma20": m20,
            "ma50": m50, "day": y}


def prev_opening_ms(open_ms: int, windows=LAB_WINDOWS) -> int | None:
    """Apertura anterior (de cualquiera de las ventanas del laboratorio) estrictamente antes de open_ms."""
    d = datetime.fromtimestamp(open_ms / 1000, timezone.utc).date()
    ts = [o["open_ms"] for k in range(-1, 9) for o in openings_on(d - timedelta(days=k))
          if o["id"] in windows and o["open_ms"] < open_ms]
    return max(ts) if ts else None


def react_between(bars: list, a_ms: int, b_ms: int) -> float | None:
    """Retorno (%) de la moneda entre la apertura de la vela de a_ms y el cierre de la anterior a b_ms, como el
    laboratorio: un minuto sin vela vale el último cierre y con más del 10 % de velas sin dato no hay reacción."""
    n = (b_ms - a_ms) // MIN
    if n <= 0:
        return None
    i, j = bar_index(bars, a_ms), bar_index(bars, b_ms)
    if n - (j - i) > (1 - COVERAGE) * n:
        return None
    if i < len(bars) and bars[i][T] == a_ms:
        o = bars[i][O]
    elif i > 0:
        o = bars[i - 1][C]
    else:
        return None
    return (bars[j - 1][C] / o - 1) * 100


def lab_rvol(vol_now: float, prior: list[float]) -> float | None:
    """Volumen relativo del laboratorio al decidir: volumen desde la apertura hasta la vela de decisión frente a la
    mediana del mismo tramo en las sesiones anteriores de la bolsa principal (solo las que tienen volumen). Con menos de
    dos sesiones, None: la confirmación no se cumple."""
    ref = sorted(v for v in prior or [] if v and v > 0)
    if len(ref) < 2:
        return None
    h = len(ref) // 2
    return vol_now / (ref[h] if len(ref) % 2 else (ref[h - 1] + ref[h]) / 2)


def news_label(d: int, sig: dict | None, min_strength: float = PARAMS["min_strength"]) -> str:
    """Las noticias (plan del estratega, noticias triadas y predicciones) frente al impulso: a_favor, en_contra o
    neutral (sin señal clara: fuerza < min_strength)."""
    sig = sig or {}
    nd = sig.get("dir") or 0
    if not nd or _f(sig.get("strength"), 0.0) < min_strength - 1e-12:
        return "neutral"
    return "a_favor" if nd == d else "en_contra"


def impulse_instrument(d: int, params: dict | None = None) -> str:
    """Instrumento de la entrada: el de los largos o el de los cortos (perpetuo por defecto)."""
    p = _ip(params)
    return p["long_instrument"] if d > 0 else p["short_instrument"]


def impulse_tags(d: int, mom: float, ctx: dict | None, sig: dict | None, params: dict | None = None) -> dict:
    """Etiquetas de una entrada: lado e instrumento, noticias, apertura anterior (reacción de la moneda en su primera
    hora, como la familia «Apertura anterior» del laboratorio), tendencia de BTC, volatilidad e impulso."""
    p, ctx = _ip(params), ctx or {}
    pr, btc, vol = ctx.get("prev_react"), ctx.get("btc_dir") or 0, ctx.get("atr_pct")
    return {"lado": "largo" if d > 0 else "corto", "instrumento": impulse_instrument(d, p),
            "noticia": news_label(d, sig, p["min_strength"]),
            "noticia_fuerza": round(_f((sig or {}).get("strength"), 0.0), 3),
            "apertura_anterior": "sin_dato" if not pr else "a_favor" if pr * d > 0 else "en_contra",
            "apertura_anterior_pct": round(pr, 3) if pr is not None else None,
            "btc": "neutral" if not btc else "a_favor" if btc == d else "en_contra",
            "volatilidad_pct": round(vol * 100, 1) if vol is not None else None,
            "impulso_pct": round(mom, 3), "medida_min": p["measure_min"]}


def impulse_filters(d: int, tags: dict, ctx: dict | None, params: dict | None = None) -> str | None:
    """Motivo para no seguir el impulso, o None. Volatilidad y BTC con las reglas del laboratorio (sin dato de
    volatilidad no se opera; sin dato de BTC cuenta como neutral) y las noticias según news_mode."""
    p, v = _ip(params), (ctx or {}).get("atr_pct")
    vf = p["vol_filter"]
    if vf != "todos":
        if v is None:
            return "sin dato de volatilidad (ATR de 14 h en los 30 días previos): no se opera"
        ok = v >= 1 / 3 if vf == "no_baja" else v >= 2 / 3 if vf == "alta" else v < 1 / 3
        if not ok:
            rule = {"no_baja": "≥ 33 %", "alta": "≥ 67 %", "baja": "< 33 %"}[vf]
            return f"volatilidad en el percentil {_n(v * 100, 0)} % de 30 días (hace falta {rule}): no se opera"
    b, bf = tags.get("btc"), p["btc_filter"]
    if bf == "no_contra" and b == "en_contra":
        return "va contra la tendencia diaria de BTC (medias de 20 y 50 días): no se opera"
    if bf == "a_favor" and b != "a_favor":
        return "la tendencia diaria de BTC no acompaña (medias de 20 y 50 días): no se opera"
    nl, nm = tags.get("noticia"), p["news_mode"]
    if nm == "no_contra" and nl == "en_contra":
        return f"las noticias van claramente en contra (fuerza {_n(tags.get('noticia_fuerza') or 0.0)}): no se opera"
    if nm == "a_favor" and nl != "a_favor":
        return "las noticias no van a favor (se exige noticia a favor): no se opera"
    return None


def impulse_strategy(d: int, tags: dict | None) -> str:
    """Nombre de estrategia para el paper (Resultados compara por él): lado y noticia, p. ej. «impulso-largo-noticia-a-
    favor» o «impulso-corto-sin-noticia»."""
    news = {"a_favor": "noticia-a-favor", "en_contra": "noticia-en-contra"}.get((tags or {}).get("noticia"), "sin-noticia")
    return f"impulso-{'largo' if d > 0 else 'corto'}-{news}"


def impulse_entry_type(params: dict | None = None) -> str:
    p = _ip(params)
    kind, m = entry_kind(p["entry"])
    if kind == "pb":
        return f"Reanudación tras retroceder el {_n(p['pb_frac'] * 100, 0)} % del impulso (a mercado)"
    if kind == "t":
        return f"A +{m} min de la apertura, a favor del impulso (a mercado)"
    return f"Ruptura del rango de {p['measure_min']} min a favor del impulso, cierre de {p['brk_tf']} min (a mercado)"


def expected_funding_pct(d: int, rate_pct: float | None, hours: float) -> float:
    """Funding que se espera PAGAR (en % del nominal) en hours horas con la tasa de ahora (% por 8 h): los largos pagan
    la tasa positiva y los cortos la negativa. Lo que se cobraría no se cuenta a favor (prudente)."""
    return max(0.0, d * rate_pct * hours / 8) if rate_pct is not None else 0.0


def impulse_plan(d: int, entry: float, params: dict | None = None, funding_pct: float | None = None,
                 instrument: str = "spot") -> dict:
    """Horquilla pura desde la entrada real: stop a sl_pct % y objetivo a target_r × stop; sin break-even, stop dinámico
    ni parciales. Con las reglas del usuario: stop ≥ 1,5 %, R:R ≥ 2 y (objetivo − entrada) − coste de ida y vuelta −
    funding esperado en la duración media (perpetuos) ≥ min_net_pct (3 %). Siempre: objetivo ≥ 3 × coste."""
    p = _ip(params)
    sl, R = p["sl_pct"], p["target_r"]
    stop, tgt = entry * (1 - d * sl / 100), entry * (1 + d * sl * R / 100)
    rr = (d * (tgt - entry)) / (d * (entry - stop))
    gross, cost = d * (tgt - entry) / entry * 100, paper.cost_rt_pct()
    fund = expected_funding_pct(d, funding_pct, p["avg_hold_h"]) if instrument == "perp" else 0.0
    net = gross - cost - fund
    inst = "perpetuo" if instrument == "perp" else "contado"
    out = {"stopPct": sl, "tp1R": R, "tp2R": R, "tp1Frac": 0.0, "beR": None, "trailPct": None, "trailStartR": None,
           "rr": round(rr, 3), "entry": entry, "stop_px": stop, "target_px": tgt, "instrument": instrument,
           "gross_pct": round(gross, 3), "cost_pct": round(cost, 3), "funding_pct": round(fund, 4),
           "net_pct": round(net, 3), "ok": True,
           "why": f"Horquilla pura desde {_px(entry)} ({inst}): stop {_px(stop)} (−{_n(sl)} %) y objetivo {_px(tgt)} "
                  f"(+{_n(gross)} %, {_n(net)} % neto de costes, R:R {_n(rr)}); sin break-even, stop dinámico ni "
                  "parciales"}
    need = p.get("min_net_pct")
    if config.USER_RULES and (sl < config.RULE_MIN_STOP_PCT - 1e-9 or rr < config.RULE_MIN_RR - 1e-9):
        out.update(ok=False, why=f"Regla del usuario: stop {_n(sl)} % (mínimo {_n(config.RULE_MIN_STOP_PCT)} %) y R:R "
                                 f"{_n(rr)} (mínimo {_n(config.RULE_MIN_RR)})")
    elif config.USER_RULES and need and net < need - 1e-9:
        fd = f" y el funding esperado en {_n(p['avg_hold_h'], 0)} h ({_n(fund, 3)} %)" if fund else ""
        out.update(ok=False, why=f"Regla del usuario: el objetivo da +{_n(gross)} % y, quitando el coste de ida y vuelta "
                                 f"({_n(cost)} %){fd}, quedan {_n(net)} % netos < {_n(need)} %")
    elif gross < 3 * cost - 1e-9:
        out.update(ok=False, why=f"Objetivo +{_n(gross)} % < 3 × coste de ida y vuelta ({_n(3 * cost)} %)")
    return out


class ImpulseRun:
    """Reglas de impulso de una moneda en una apertura, las del laboratorio: mide el movimiento de los primeros
    measure_min minutos (cierre / apertura de la vela de la apertura), lo filtra (volatilidad, BTC y noticias) y entra
    una sola vez a favor: a +N min (t:N), en la reanudación tras retroceder pb_frac del impulso (pb) o en la ruptura del
    rango (brk), si el volumen desde la apertura hasta ese momento es ≥ rvol_min veces el de las sesiones anteriores.
    Fases: midiendo_impulso → esperando_entrada (paso: hora, retroceso, reanudacion o ruptura) → en_posicion → terminada.
    Sin red ni órdenes: el motor le pasa las velas de 1 min cerradas (feed), el contexto (ctx), las noticias (news) y
    las velas de las sesiones anteriores (prior); los minutos sin vela repiten el último cierre (con volumen 0), como la
    rejilla densa del laboratorio."""

    free = False

    def __init__(self, coin: str, open_ms: int, params: dict | None = None, ctx: dict | None = None,
                 news: dict | None = None, prior: list | None = None):
        self.p = _ip(params)
        self.coin, self.open_ms = coin, open_ms
        self.ctx, self.news = ctx, news      # ctx: atr_pct, btc_dir, prev_react (None = aún sin cargar)
        self.prior = prior                   # [(apertura, velas)] de las sesiones anteriores (None = sin cargar)
        self.kind, self.t_min = entry_kind(self.p["entry"])
        self.N, self.E = self.p["measure_min"], self.p["entry_end_min"]
        self.state, self.step = "midiendo_impulso", None
        self.dir, self.mom, self.po, self.hi, self.lo = 0, None, None, None, None
        self.ext = self.level = None
        self.vol, self.rvol = 0.0, None      # volumen desde la apertura y rvol al decidir
        self.entries, self.results, self.tags = 0, [], {}
        self.entry_px = self.stop_px = self.target_px = None
        self.t_next = self.last_c = None
        self.last_why = f"Se mide el impulso de los {self.N} primeros min desde la apertura ({_hm(open_ms)} Madrid)"
        self.end_why: str | None = None
        self.feats: dict = {}
        self.moving = None

    @property
    def done(self) -> bool:
        return self.state == "terminada"

    @property
    def rng(self) -> dict | None:
        return {"hi": self.hi, "lo": self.lo} if self.hi is not None else None

    def end(self, why: str) -> dict:
        self.state, self.step, self.end_why, self.last_why = "terminada", None, why, why
        return {"action": "end", "why": why}

    def set_signal(self, d: int) -> None:   # la dirección la da el impulso; las noticias solo filtran y etiquetan
        return None

    def levels(self) -> dict:
        return {"apertura": self.po, "impulso_pct": round(self.mom, 3) if self.mom is not None else None,
                "maximo": self.hi, "minimo": self.lo, "extremo": self.ext,
                "retroceso": self.level if self.kind == "pb" else None,
                "ruptura": self.level if self.kind == "brk" else None,
                "hora_entrada": self.open_ms + self.t_min * MIN if self.kind == "t" else None,
                "rvol": round(self.rvol, 3) if self.rvol is not None else None, "entrada": self.entry_px,
                "stop": self.stop_px, "objetivo": self.target_px}

    def feed(self, bar: list, live_ok: bool = True) -> dict | None:
        """Una vela de 1 min cerrada (en orden). Devuelve {"action": "enter", ...}, {"action": "end", ...} o None."""
        if self.done or self.state == "en_posicion":
            return None
        if self.t_next is None:
            if bar[T] < self.open_ms:
                return None
            if bar[T] > self.open_ms:   # el laboratorio no opera una apertura sin su primera vela
                return self.end("Falta la vela de la apertura: no se puede medir el impulso")
        elif bar[T] < self.t_next:
            return None
        while self.t_next is not None and self.t_next < bar[T] and not self.done and self.state != "en_posicion":
            c = self.last_c
            act = self._step([self.t_next, c, c, c, c, 0.0, 0.0, 0.0], live_ok)   # minuto sin vela
            if act and act["action"] == "enter":
                return act
        if self.done or self.state == "en_posicion":
            return None
        return self._step(bar, live_ok)

    def _step(self, b: list, live_ok: bool) -> dict | None:
        t, tc, op = b[T], b[T] + MIN, self.open_ms
        self.t_next, self.last_c = t + MIN, b[C]
        self.vol += b[V]
        if self.state == "midiendo_impulso":
            if self.po is None:
                self.po = b[O]
            self.hi = b[HI] if self.hi is None else max(self.hi, b[HI])
            self.lo = b[LO] if self.lo is None else min(self.lo, b[LO])
            if tc < op + self.N * MIN:
                self.last_why = (f"Midiendo el impulso: {_sg((b[C] / self.po - 1) * 100)} % en {(tc - op) // MIN} de "
                                 f"{self.N} min")
                return None
            return self._measured(b, live_ok)
        d, st = self.dir, self.step
        if st == "retroceso":
            if d * ((b[LO] if d > 0 else b[HI]) - self.level) <= 0:
                self.step = "reanudacion"
                self.last_why = (f"Retroceso hasta {_px(self.level)} a las {_hm(tc)}: espero la reanudación (un cierre "
                                 f"por {'encima' if d > 0 else 'debajo'} de {_px(self.ext)} antes de las "
                                 f"{_hm(op + self.E * MIN)})")
        elif st == "reanudacion":
            if d * (b[C] - self.ext) > 0:
                return self._enter(b, live_ok, f"Reanudación: cierre {_px(b[C])} {'por encima' if d > 0 else 'por debajo'}"
                                               f" del {'máximo' if d > 0 else 'mínimo'} del impulso ({_px(self.ext)}) "
                                               f"tras retroceder hasta {_px(self.level)}")
        elif st == "hora":
            if tc >= op + self.t_min * MIN:
                return self._enter(b, live_ok, f"A +{self.t_min} min de la apertura, a favor del impulso de "
                                               f"{_sg(self.mom)} %")
        elif st == "ruptura":
            tf = self.p["brk_tf"]
            if (tf == 1 or (t - op - self.N * MIN) % (5 * MIN) == 4 * MIN) and d * (b[C] - self.level) > 0:
                return self._enter(b, live_ok, f"Ruptura: cierre {_px(b[C])} {'por encima' if d > 0 else 'por debajo'} "
                                               f"de {_px(self.level)} (rango de {self.N} min y margen)")
        if st != "hora" and tc >= op + self.E * MIN:
            what = "reanudación" if self.kind == "pb" else "ruptura"
            return self.end(f"Sin {what} en los primeros {self.E} min: caduca sin entrar")
        return None

    def _measured(self, b: list, live_ok: bool) -> dict | None:
        p, x = self.p, self.p["min_move_pct"]
        self.mom = mom = (b[C] / self.po - 1) * 100
        d = self.dir = 1 if mom >= x else -1 if mom <= -x else 0
        head = f"Impulso de {_sg(mom)} % en {self.N} min"
        if not d:
            return self.end(f"{head}: no llega a ±{_n(x)} %, no se opera")
        self.tags = impulse_tags(d, mom, self.ctx, self.news, p)
        why = impulse_filters(d, self.tags, self.ctx, p)
        if why:
            return self.end(f"{head}, pero {why}")
        side = f"{'compra' if d > 0 else 'venta'} {INST_TEXT[self.tags['instrumento']]}"
        self.ext = self.hi if d > 0 else self.lo
        self.state = "esperando_entrada"
        if self.kind == "pb":
            self.level, self.step = self.ext - p["pb_frac"] * (self.ext - self.po), "retroceso"
            self.last_why = (f"{head} ({side}): espero retroceso hasta {_px(self.level)} (la mitad del movimiento) y "
                             f"luego un cierre por {'encima' if d > 0 else 'debajo'} de {_px(self.ext)}")
        elif self.kind == "t":
            self.step = "hora"
            if self.t_min <= self.N:
                return self._enter(b, live_ok, f"{head}: entrada a +{self.t_min} min, a favor del impulso")
            self.last_why = f"{head} ({side}): entro a +{self.t_min} min ({_hm(self.open_ms + self.t_min * MIN)} Madrid)"
        else:
            mg = max(self.po * LAB_BREAK_BPS / 1e4, LAB_BREAK_FRAC * (self.hi - self.lo))
            self.level, self.step = (self.hi + mg if d > 0 else self.lo - mg), "ruptura"
            self.last_why = (f"{head} ({side}): espero un cierre por {'encima' if d > 0 else 'debajo'} de "
                             f"{_px(self.level)} (ruptura del rango de {self.N} min)")
        if p["rvol_min"]:
            self.last_why += f"; hará falta volumen relativo ≥ {_n(p['rvol_min'])} desde la apertura"
        return None

    def _enter(self, b: list, live_ok: bool, why: str) -> dict | None:
        t_dec = b[T] + MIN
        if self.p["rvol_min"]:   # confirmación del laboratorio: volumen desde la apertura frente a sesiones anteriores
            prior = [slot_volume(bs, po, po + t_dec - self.open_ms) for po, bs in self.prior or []]
            self.rvol = lab_rvol(self.vol, prior)
            if self.rvol is None or self.rvol < self.p["rvol_min"]:
                rv = "sin volumen de referencia de las sesiones anteriores" if self.rvol is None else _n(self.rvol)
                return self.end(f"{why}, pero el volumen relativo desde la apertura ({rv}) no llega a "
                                f"{_n(self.p['rvol_min'])}: no se opera")
            self.tags["rvol"] = round(self.rvol, 3)
            why += f" con volumen relativo {_n(self.rvol)}"
        if not live_ok:
            return self.end(f"{why}, pero en una vela antigua: tarde para entrar (no se persigue el precio)")
        self.state, self.step, self.entries = "en_posicion", None, self.entries + 1
        self.last_why = f"Entrada: {why}"
        return {"action": "enter", "dir": self.dir, "price": b[C], "t": t_dec, "why": why, "rvol": self.rvol,
                "tags": dict(self.tags), "levels": self.levels()}

    def on_paused(self, t: int, why: str) -> None:
        self.on_rejected(t, why)

    def on_rejected(self, t: int, why: str) -> None:
        """El paper no ejecutó la entrada: el laboratorio habría entrado ahí, así que no se busca otra."""
        if self.state != "en_posicion":
            return
        self.entries -= 1
        self.end(f"Entrada no ejecutada: {why}; no se persigue el precio")

    def on_exit(self, t: int, ret_pct: float, why: str) -> None:
        if self.state != "en_posicion":
            return
        self.results.append(ret_pct)
        self.end(f"Salida ({why}, {_sg(ret_pct)} %): una sola entrada por apertura")

    def restore_entry(self, rec: dict | None = None) -> None:
        """Al reiniciar el bot: la entrada de esta apertura ya estaba hecha en el paper. De rec (la posición, la orden
        en curso o la operación cerrada del paper) se recuperan lado, instrumento, etiquetas y niveles."""
        if self.done or self.state == "en_posicion":
            return
        self.state, self.step, self.entries = "en_posicion", None, self.entries + 1
        rec = rec or {}
        if rec.get("side") in ("long", "short"):
            self.dir = 1 if rec["side"] == "long" else -1
        if isinstance(rec.get("tags"), dict):
            self.tags = dict(rec["tags"])
        if rec.get("instrument") in paper.INSTRUMENTS:
            self.tags["instrumento"] = rec["instrument"]
        self.entry_px, self.target_px = _f(rec.get("entry")), _f(rec.get("tp2"))
        self.stop_px = _f(rec.get("sl_initial")) or _f(rec.get("sl"))
        if self.entry_px and self.dir:
            inst = INST_TEXT.get(self.tags.get("instrumento") or impulse_instrument(self.dir, self.p), "")
            lv = "".join(f", {k} {_px(v)}" for k, v in (("stop", self.stop_px), ("objetivo", self.target_px)) if v)
            self.last_why = (f"En posición (paper, recuperada tras reiniciar): {'compra' if self.dir > 0 else 'venta'} "
                             f"{inst} a {_px(self.entry_px)}{lv}")
        else:
            self.last_why = "Entrada enviada al paper (recuperada tras reiniciar): a la espera de ejecutarse"


# ---------------------------------------------------------------- motor en vivo

def entry_type(params: dict | None = None) -> str:
    tf = _params(params).get("confirm_tf") or 1
    return ENTRY_TYPE if tf == 1 else f"Ruptura confirmada al cierre de {tf} min (a mercado)"


def pick_strategy(params: dict | None) -> str:
    """Modo del motor: params["estrategia"]; con params sin ese campo, «ruptura» (los parámetros de siempre); sin params,
    BOT_APERTURAS_ESTRATEGIA y, si no está, «impulso» con las reglas del usuario activas (en vivo) y «ruptura» sin ellas
    (BOT_USER_RULES=0, solo las pruebas de la v1)."""
    if params is not None:
        return params.get("estrategia") if params.get("estrategia") in STRATEGIES else "ruptura"
    env = os.environ.get("BOT_APERTURAS_ESTRATEGIA", "").strip().lower()
    if env in STRATEGIES:
        return env
    return "impulso" if config.USER_RULES else "ruptura"


class OpeningEngine:
    """Vigila las aperturas de las bolsas y entra con submit(ticket, story, headline, strategy, window) del paper
    trading. Publica «openings». Dos modos (pick_strategy):
    - impulso (el de la operativa del usuario): mide el impulso de los primeros minutos de cada apertura en todas las
      monedas y lo sigue con las reglas del laboratorio, largos al contado y cortos en perpetuo; las noticias filtran y
      etiquetan. Sin params: el preset IMPULSO con los cambios de BOT_IMPULSO_PARAMS (impulse_setup).
    - ruptura (v2): arma las monedas con señal 15 min antes, mide el rango con velas de 1 min de Binance y entra en la
      ruptura. history(coin, months) da las velas guardadas para la puerta de probabilidades (por defecto,
      load_history). Sin params: la recomendación del backtest de 24 meses; sin una válida, el preset V2 (con aviso).
    activa_path (modo impulso sin params): estrategia_activa.json de la evolución (bot/evolucion.py). Si existe y cumple
    las reglas del usuario (evolucion.leer_estrategia_activa; si no, el preset), manda sobre el preset y sobre
    BOT_IMPULSO_PARAMS (fuente «evolucion»); con «pausada» no se abren entradas nuevas (las abiertas siguen en el
    paper). Se vuelve a leer si cambia, pero solo cuando no hay ninguna sesión de entrada en curso. Cada ticket lleva la
    versión en tags.estrategia_firma (evolucion.firma_motor) y «openings» la publica en estrategia_cargada."""

    def __init__(self, bus, session: aiohttp.ClientSession | None, get_signal, submit, get_portfolio, clock=now_ms,
                 params: dict | None = None, history=None, activa_path: Path | str | None = None):
        self.bus, self.session = bus, session
        self.get_signal, self.submit, self.get_portfolio = get_signal, submit, get_portfolio
        self.clock = clock
        self.notice: str | None = None   # aviso para la web sobre los parámetros en uso
        self.strategy = pick_strategy(params)
        self.impulso = self.strategy == "impulso"
        # Estrategia de la evolución: solo en modo impulso y sin params explícitos
        self.activa_path = Path(activa_path) if activa_path and self.impulso and params is None else None
        self.activa: dict | None = None
        self.paused, self.firma, self.loaded = False, None, None
        self._activa_mt = self._mtime()
        self._activa_at = time.monotonic()
        self.windows: dict[str, dict] = {}
        if params is not None:
            params = {k: v for k, v in params.items() if k != "estrategia"}
        if self.impulso:
            self._setup_impulso(params, clock())
        elif params is not None:
            self.p, self.source = {**PARAMS, **params}, "manual"
        else:
            rec, why = load_recommendation()
            if rec:
                self.p, self.source = {**PARAMS, **rec}, "backtest"
            else:
                self.p, self.source = {**PARAMS, **V2}, "preset_v2"
                self.notice = (f"{why}: se usa el preset V2 sin puerta (la referencia «preset V2» del backtest; su "
                               "resultado está en el informe), no la v1")
                log.warning("Aperturas: %s", self.notice)
        if config.USER_RULES and not self.impulso:   # reglas del usuario: stop ≥ 1,5 %, ≥ 2R y horquilla pura
            self.p.update({"stop_min_pct": max(self.p["stop_min_pct"], config.RULE_MIN_STOP_PCT),
                           "target_r": max(self.p.get("target_r") or 0, config.RULE_MIN_RR), "target_pct": None,
                           "min_rr": max(self.p["min_rr"], config.RULE_MIN_RR), "be_r": None, "trail_atr": 0.0,
                           "trail_start_r": None, "tp1_frac": 0.0})
            if self.p.get("stop_mode") in (None, "cercano"):
                self.p["stop_mode"] = "or_far"   # el modo v1 usa salidas de la v1 (break-even y dinámico)
        self.history = history or load_history
        self.status: dict | None = None
        self.day: dict = {"date": None, "entries": {}}   # entradas del día de Madrid: {story: riesgo en %}
        self._lag_ms = LIVE_LAG_MS + ((self.p.get("confirm_tf") or 1) - 1) * MIN   # una vela de antigüedad como mucho
        self._key = None
        self._pub_at = 0.0
        self._fails = 0
        self._retry_at = 0
        self._err_at = 0.0

    # ---- estrategia de la evolución (estrategia_activa.json)

    def _mtime(self) -> float | None:
        try:
            return self.activa_path.stat().st_mtime if self.activa_path else None
        except OSError:
            return None

    def _setup_impulso(self, params: dict | None, now: int) -> None:
        """Parámetros del modo impulso: params, o el archivo de la evolución, o el preset (con BOT_IMPULSO_PARAMS).
        Fija la pausa, la versión (firma) y lo que se publica en estrategia_cargada."""
        try:
            from . import evolucion   # perezoso: la evolución importa este módulo
        except Exception:   # sin evolución, el preset de siempre
            log.exception("Aperturas: no pude cargar la evolución; se usa el preset")
            evolucion = None
        notes, self.activa, self.paused = [], None, False
        if params is None and self.activa_path is not None and evolucion is not None:
            self.activa = evolucion.leer_estrategia_activa(self.activa_path)   # None: no hay o no cumple las reglas
            over = evolucion.params_motor(self.activa)
            params = {k: v for k, v in over.items() if k != "estrategia"} if over else None
            self.paused = bool(self.activa and self.activa.get("pausada"))
        self.p, self.source, notice = impulse_setup(params)
        if self.activa is not None and params is not None:
            self.source = "evolucion"
        if self.paused:
            why = _cut((self.activa or {}).get("motivo") or "sin motivo", 200)
            notes.append(f"Campeón pausado por la evolución (solo paper): no se abren entradas nuevas ({why})")
        if notice:
            notes.append(notice)
        self.notice = "; ".join(notes) or None
        if self.notice:
            log.warning("Aperturas (impulso): %s", self.notice)
        try:
            self.firma = evolucion.firma_motor(self.p) if evolucion is not None else None
        except Exception:
            log.exception("Aperturas: no pude calcular la firma de la estrategia")
            self.firma = None
        self.loaded = {"firma": self.firma, "ts": now, "pausada": self.paused, "fuente": self.source,
                       "registro": (self.activa or {}).get("registro")}

    def _session_busy(self, now: int) -> bool:
        """¿Hay alguna apertura armándose o en su sesión de entrada? Entonces no se cambian los parámetros."""
        lead, end = (self.p["lead_min"] + 1) * MIN, (self.p["session_max_min"] + 3) * MIN
        return any(w["open_ms"] - lead <= now <= w["open_ms"] + end for w in self.windows.values() if not w["missed"])

    def _check_activa(self, now: int) -> None:
        """Si estrategia_activa.json cambió (lo escribe la evolución al aplicar o revertir), se carga entre aperturas."""
        if self.activa_path is None or time.monotonic() - self._activa_at < ACTIVA_CHECK_S:
            return
        self._activa_at = time.monotonic()
        mt = self._mtime()
        if mt == self._activa_mt or self._session_busy(now):
            return   # sin cambios, o se espera a que acabe la sesión en curso
        self._activa_mt = mt
        old = self.firma, self.paused
        self._setup_impulso(None, now)
        # Las aperturas aún no armadas se rehacen con los parámetros nuevos (ventanas y monedas pueden cambiar).
        for key in [k for k, w in self.windows.items() if not w["armed"] and not w["missed"]]:
            del self.windows[key]
        self._key = None
        log.info("Aperturas: estrategia de la evolución recargada (firma %s → %s, pausa %s → %s)", old[0], self.firma,
                 old[1], self.paused)

    # ---- agenda

    def _schedule(self, now: int) -> None:
        today = datetime.fromtimestamp(now / 1000, timezone.utc).date()
        for k in (-1, 0, 1, 2):
            for o in openings_on(today + timedelta(days=k)):
                key = f"{o['id']}-{o['day']}"
                if self.impulso and (o["id"] not in self.p["windows"] or special_session(o)):   # como el laboratorio
                    continue
                if key not in self.windows and now - (o["open_ms"] + self.p["session_max_min"] * MIN) < KEEP_MS \
                        and o["open_ms"] - now <= 24 * H:
                    self.windows[key] = {**o, "key": key, "armed": False, "at_open": False, "missed": False,
                                         "signals": {}, "runs": {}, "bars": {}, "prior": {}, "prior_tries": {},
                                         "done_i": {}, "stories": {}, "seq": {}, "why": "", "gate": {}, "plans": {},
                                         "ctx": {},
                                         "prev": prev_session_opens(o["lead"], o["open_ms"], self.p["rvol_sessions"])}
        for key in [k for k, w in self.windows.items()
                    if now - (w["open_ms"] + self.p["session_max_min"] * MIN) > KEEP_MS]:
            del self.windows[key]

    # ---- señal

    async def _signal(self, coin: str, w: dict) -> dict:
        try:
            s = await _maybe(self.get_signal(coin, {"id": w["id"], "name": w["name"], "markets": w["markets"],
                                                    "open_ms": w["open_ms"], "lead": w["lead"]}))
            if isinstance(s, dict) and s.get("dir") in (-1, 0, 1):
                return s
        except Exception as e:
            log.warning("Aperturas: señal de %s en %s no disponible: %s", coin, w["name"], e)
        return {"dir": 0, "strength": 0.0, "drivers": [], "why": "Señal no disponible"}

    async def _arm(self, w: dict, at_open: bool, now: int) -> None:
        """15 min antes (y otra vez en la apertura): señal de cada moneda; se arman las que la tienen. Una moneda que se
        arma con la sesión ya empezada (reinicio del bot) recupera del paper lo que ya se operó en esta apertura.
        En modo impulso se arman todas las monedas de la estrategia: la dirección la dará el impulso."""
        pf = None
        if self.impulso:
            p = self.p
            for coin in p["coins"]:
                w["signals"][coin] = await self._signal(coin, w)
                if coin not in w["runs"]:
                    run = w["runs"][coin] = ImpulseRun(coin, w["open_ms"], p)
                    pf = await self._portfolio() if pf is None else pf
                    self._restore(w, coin, run, pf or {}, now)
            conf = [f"volumen relativo ≥ {_n(p['rvol_min'])}" if p["rvol_min"] else "",
                    {"a_favor": "solo a favor de la tendencia de BTC",
                     "no_contra": "nunca contra la tendencia de BTC"}.get(p["btc_filter"], ""),
                    {"no_contra": "no si las noticias van claramente en contra",
                     "a_favor": "solo con noticias a favor"}.get(p["news_mode"], "noticias solo como etiqueta")]
            w["why"] = (f"Se sigue el impulso de los {p['measure_min']} primeros min si pasa de ±{_n(p['min_move_pct'])} %"
                        f" · {impulse_entry_type(p)} · {' · '.join(x for x in conf if x)} · largos "
                        f"{INST_TEXT[p['long_instrument']]} y cortos {INST_TEXT[p['short_instrument']]} · stop "
                        f"{_n(p['sl_pct'])} % y "
                        f"objetivo {_n(p['target_r'], 1)} R (+{_n(p['sl_pct'] * p['target_r'])} %), sin tope "
                        f"({_n(p['max_hold_h'] / 24, 0)} días de seguridad)")
            w["armed"], w["at_open"] = True, w["at_open"] or at_open
            return
        for coin in config.COINS:
            s = await self._signal(coin, w)
            w["signals"][coin] = s
            run = w["runs"].get(coin)
            if run:
                run.set_signal(s["dir"])
            elif s["dir"]:
                run = w["runs"][coin] = CoinRun(coin, w["open_ms"], s["dir"], self.p)
                pf = await self._portfolio() if pf is None else pf
                self._restore(w, coin, run, pf or {}, now)
        if not w["runs"]:
            w["why"] = "Ninguna moneda con señal clara: no se opera esta apertura"
        w["armed"], w["at_open"] = True, w["at_open"] or at_open

    # ---- cartera del paper

    async def _portfolio(self) -> dict | None:
        try:
            return await _maybe(self.get_portfolio()) or {}
        except Exception as e:
            log.warning("Aperturas: cartera no disponible: %s", e)
            return None

    def _prefix(self, w: dict, coin: str) -> str:
        """Story de las operaciones de una moneda en una apertura: a-<ventana>-<fecha Madrid>-<moneda>-<n>."""
        return f"a-{w['id']}-{datetime.fromtimestamp(w['open_ms'] / 1000, MAD).date().isoformat()}-{coin.lower()}-"

    def _restore(self, w: dict, coin: str, run: CoinRun, pf: dict, now: int) -> None:
        """Rehace lo ya hecho en esta apertura a partir de las stories del paper (cerradas, abiertas y en curso), para
        que un reinicio no repita una story ni olvide las pérdidas seguidas o una posición abierta."""
        pre = self._prefix(w, coin)

        def n_of(story: str) -> int:
            try:
                return int(story[len(pre):])
            except ValueError:
                return 0

        mine = lambda xs: [x for x in xs or [] if isinstance(x, dict) and str(x.get("story") or "").startswith(pre)]
        closed = sorted(mine(pf.get("closed")), key=lambda t: _f(t.get("closed"), 0))
        open_ = mine(pf.get("positions"))
        queued = mine(pf.get("pending")) + mine(pf.get("scheduled"))
        stories = [x["story"] for x in closed + open_ + queued]
        if not stories:
            return
        w["seq"][coin] = max([w["seq"].get(coin, 0)] + [n_of(s) for s in stories])
        for t in closed:
            run.restore_entry(t)
            run.on_exit(int(_f(t.get("closed"), now)), _f(t.get("pnl_pct"), 0.0), t.get("reason") or "cerrada")
            w["stories"][t["story"]] = {"coin": coin, "at": int(_f(t.get("opened"), 0)), "done": True}
        for x in open_ + queued:
            if run.done:
                break
            run.restore_entry(x)   # en modo impulso, con lado, instrumento, etiquetas y niveles de la posición
            is_open = x in open_
            w["stories"][x["story"]] = {"coin": coin, "at": int(_f(x.get("opened"), 0)) or now - SUBMIT_GRACE_MS,
                                        "pending": not is_open, "open": is_open}
        log.info("Aperturas: %s en %s recuperada del paper (%d operaciones, estado %s)", coin, w["name"],
                 len(stories), run.state)

    @staticmethod
    def _coin_busy(pf: dict, coin: str) -> str | None:
        """Posición abierta, pendiente o programada de esa moneda en el paper (de otra ventana o de la fase 2)."""
        for key, what in (("positions", "una posición abierta"), ("pending", "una orden pendiente de aprobar"),
                          ("scheduled", "una entrada programada")):
            for x in pf.get(key) or []:
                if isinstance(x, dict) and x.get("coin") == coin:
                    side = {"long": "compra", "short": "venta"}.get(x.get("side"), "lado sin dato")
                    return f"ya hay {what} en {coin} ({side}, {x.get('story') or 'sin story'})"
        return None

    # ---- datos

    async def fetch_klines(self, coin: str, start: int, limit: int) -> list[list]:
        """Velas de 1 min de Binance al contado (públicas, sin claves), sin la vela en curso."""
        return await self.fetch_interval(coin, "1m", start, limit)

    async def fetch_interval(self, coin: str, interval: str, start: int, limit: int) -> list[list]:
        """Velas de Binance al contado del intervalo dado (1m, 1h, 1d), sin la vela en curso."""
        params = {"symbol": PAIRS[coin], "interval": interval, "startTime": str(start), "limit": str(min(1000, limit))}
        async with self.session.get(KLINES, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return to_bars(await r.json(content_type=None), self.clock())

    async def _context(self, w: dict, coin: str) -> None:
        """Modo impulso, una vez por ventana: tendencia diaria de BTC, volatilidad de la moneda (velas de 1 h) y su
        reacción en la apertura anterior, solo con lo cerrado al abrir. Un fallo de red sale hacia arriba (reintento)."""
        ctx, om = w["ctx"], w["open_ms"]
        if "btc" not in ctx:
            rows = await self.fetch_interval("BTC", "1d", (om // DAY - 80) * DAY, 100)
            ctx["btc"] = btc_trend_at({r[T] // DAY: r[C] for r in rows if r[T] + DAY <= om}, om)
        if coin not in ctx:
            hours = await self.fetch_interval(coin, "1h", om - (ATR_LOOK_H + ATR_H_N + 30) * H, 1000)
            vol = atr_pct_at([h for h in hours if h[T] + H <= om], om)
            prev = prev_opening_ms(om)
            react = None
            if prev is not None:
                bars = await self.fetch_klines(coin, prev - 5 * MIN, 70)
                react = react_between([b for b in bars if b[T] < om], prev, min(prev + 60 * MIN, om))
            ctx[coin] = {"atr_pct": vol["pct"] if vol else None, "atr_h_pct": vol["atr_pct"] if vol else None,
                         "btc_dir": (ctx["btc"] or {}).get("dir", 0), "prev_ms": prev, "prev_react": react}
        run = w["runs"].get(coin)
        if run is not None and run.ctx is None:
            run.ctx = ctx[coin]

    async def _update_bars(self, w: dict, coin: str, now: int) -> None:
        if self.impulso:   # las velas desde la apertura y, para el rvol, las de las sesiones anteriores
            run, tries = w["runs"].get(coin), w["prior_tries"].get(coin, 0)
            if self.p["rvol_min"] and coin not in w["prior"] and tries < PRIOR_TRIES_IMP:   # unos 10 min con la espera
                w["prior_tries"][coin] = tries + 1
                n = self.p["session_max_min"] + 2
                w["prior"][coin] = [(po, await self.fetch_klines(coin, po, n)) for po in w["prev"]]
            if run is not None and run.prior is None and (coin in w["prior"] or tries >= PRIOR_TRIES_IMP):
                run.prior = w["prior"].get(coin) or []   # sin ellas el rvol no se cumple (como el laboratorio)
            bars = w["bars"].setdefault(coin, [])
            start = bars[-1][T] + MIN if bars else w["open_ms"]
            if now >= start + MIN + 2000:
                new = await self.fetch_klines(coin, start, (now - start) // MIN + 2)
                bars.extend(b for b in new if not bars or b[T] > bars[-1][T])
            return
        if coin not in w["prior"] and w["prior_tries"].get(coin, 0) < 3:   # mismo tramo de sesiones anteriores
            w["prior_tries"][coin] = w["prior_tries"].get(coin, 0) + 1
            rows: dict[int, list] = {}
            pre = pre_min(self.p)   # con las velas previas que pide la puerta (MOVING y ATR de 5 min)
            for po in w["prev"]:
                for b in await self.fetch_klines(coin, po - pre * MIN, pre + self.p["session_max_min"] + 10):
                    rows[b[T]] = b
            w["prior"][coin] = [rows[t] for t in sorted(rows)]
        bars = w["bars"].setdefault(coin, [])
        start = bars[-1][T] + MIN if bars else w["open_ms"] - pre_min(self.p) * MIN
        if now < start + MIN + 2000:   # la siguiente vela aún no ha cerrado (2 s de margen para Binance)
            return
        new = await self.fetch_klines(coin, start, (now - start) // MIN + 2)
        bars.extend(b for b in new if not bars or b[T] > bars[-1][T])

    # ---- decisiones

    def _process(self, w: dict, coin: str, now: int) -> list[dict]:
        run, bars = w["runs"][coin], w["bars"].get(coin) or []
        if self.impulso:
            acts, i = [], w["done_i"].get(coin, -1)
            while i + 1 < len(bars) and not run.done and run.state != "en_posicion":
                i += 1
                act = run.feed(bars[i], live_ok=now - (bars[i][T] + MIN) <= self._lag_ms)
                if act and act["action"] == "enter":
                    acts.append(act)
            w["done_i"][coin] = i
            return acts
        prior = w["prior"].get(coin)
        vs = (lambda a, b: slot_volume(prior, a, b)) if prior else None
        acts = []
        i = w["done_i"].get(coin, bar_index(bars, w["open_ms"]) - 1)
        while i + 1 < len(bars) and not run.done and run.state != "en_posicion":
            i += 1
            if bars[i][T] < max(w["open_ms"], run.resume_ms):   # antes de la apertura o de la última salida
                continue
            f = features(bars, i, w["open_ms"], vs, w["prev"] if prior else None, self.p)
            act = run.on_bar(bars, i, f, live_ok=now - (bars[i][T] + MIN) <= self._lag_ms)
            if act and act["action"] == "enter":
                acts.append(act)
        w["done_i"][coin] = i
        return acts

    @staticmethod
    def _risk(sig: dict) -> float:
        """Riesgo del ticket en % del capital: 0,5 × (0,5 + 0,5 × fuerza de la señal), como mínimo 0,05."""
        return max(0.05, round(config.RISK_PER_TRADE_PCT * (0.5 + 0.5 * min(1.0, sig.get("strength") or 0.0)), 2))

    def _ticket(self, w: dict, coin: str, act: dict, sig: dict, plan: dict | None = None) -> dict:
        d = act["dir"]
        plan = plan or entry_plan(d, act["price"], act, self.p)
        end = w["open_ms"] + self.p["session_max_min"] * MIN
        rng = act["rng"]
        inval = (f"{coin} vuelve dentro del rango ({_px(rng['lo'])}–{_px(rng['hi'])}) y pierde el "
                 f"{'mínimo' if d > 0 else 'máximo'}")
        t = {"coin": coin, "side": "long" if d > 0 else "short", "mode": MODE, "risk": self._risk(sig),
             "stopPct": plan["stopPct"], "trailPct": plan["trailPct"], "timing": "now", "win": f"Apertura {w['name']}",
             "winAt": None, "entryType": entry_type(self.p), "tp1R": plan["tp1R"], "tp2R": plan["tp2R"],
             "tp1Frac": plan["tp1Frac"], "trailStartR": plan["trailStartR"],
             "valid": f"hasta las {_hm(end)} Madrid", "inval": _cut(inval, 160), "why": _cut(act["why"], 300),
             "llm": None, "winEnd": end, "market": w["lead"], "src": SRC, "maxAgeMs": MAX_FILL_DELAY_MS,
             "signal": {"dir": sig.get("dir"), "strength": sig.get("strength")}}
        if "beR" in plan:   # sin el campo (stop de la v1), el paper usa las salidas de la v1
            t["beR"] = plan["beR"]
        return t

    def _entry_price(self, coin: str, act: dict, now: int) -> float:
        """Entrada con la que se mide el R:R: la peor entre el cierre que confirma y el precio actual de OKX EU (si es
        de hace menos de 15 s), para que un plan viejo no infravalore el riesgo. Si el precio actual ya ha cruzado el
        stop, ese (y entry_plan rechaza la entrada)."""
        px = act["price"]
        m = (getattr(self.bus, "state", None) or {}).get("market") or {}
        live = _f((m.get("prices") or {}).get(coin))
        upd = _f((m.get("updated") or {}).get(coin))
        if live and (upd is None or now - upd <= 15_000):
            if act["dir"] * (live - act["stop_px"]) <= 0:
                return live
            px = max(px, live) if act["dir"] > 0 else min(px, live)
        return px

    def _day_ledger(self, pf: dict | None, now: int) -> dict:
        """Entradas del motor en el día de Madrid (todas las ventanas) con su riesgo en stop en % del capital. Se rehace
        con la cartera del paper (tras un reinicio y con el riesgo real de cada posición)."""
        today = datetime.fromtimestamp(now / 1000, MAD).date()
        if self.day.get("date") != today.isoformat():
            self.day = {"date": today.isoformat(), "entries": {}}
        start = int(datetime.combine(today, dtime(0), tzinfo=MAD).timestamp() * 1000)
        pf = pf or {}
        eq = _f(pf.get("equity")) or _f(pf.get("start_equity")) or config.PAPER_EQUITY
        for x in (pf.get("positions") or []) + (pf.get("closed") or []):
            s = str((x or {}).get("story") or "") if isinstance(x, dict) else ""
            if s.startswith("a-") and _f(x.get("opened"), 0) >= start:
                r = _f(x.get("riskUsd"))
                if r:
                    self.day["entries"][s] = round(r / eq * 100, 4)
                else:
                    self.day["entries"].setdefault(s, config.RISK_PER_TRADE_PCT)
        return self.day

    async def _gate_ok(self, w: dict, coin: str) -> bool:
        """Puerta de probabilidades de la moneda en esta ventana, una vez por dirección y antes de la primera entrada.
        Si no se cumple (también con pocos casos o sin datos), la moneda se deja esta apertura."""
        run = w["runs"][coin]
        if run.done or run.entries or run.free or not run.dir:
            return not run.done
        g = w["gate"].get(coin)
        if not g or g.get("dir") != run.dir:
            try:
                g = await asyncio.to_thread(self._gate_calc, w, coin, run.dir)
            except Exception as e:
                log.warning("Aperturas: puerta de %s en %s sin datos: %s", coin, w["name"], e)
                g = {"dir": run.dir, "ok": False, "verdict": "sin_datos", "n": 0,
                     "text": f"Sin datos para la puerta de probabilidades ({_cut(e, 80)}): no se entra"}
            w["gate"][coin] = g
        if not g.get("ok"):
            run.end(f"Puerta de probabilidades: {g['text']}")
            return False
        return True

    def _gate_calc(self, w: dict, coin: str, d: int) -> dict:
        """Junta las velas de las sesiones anteriores (las guardadas en HIST_DIR y las que ha descargado el motor) y
        llama a odds_gate. Corre en un hilo: leer los zips tarda unos segundos."""
        p = self.p
        hist = self.history(coin, p["gate_sessions"] // 20 + 2)   # sin caché: unos 2 s y 15-25 MB por moneda
        pre = pre_min(p)
        rows: dict[int, list] = {}
        for po in prev_session_opens(w["lead"], w["open_ms"], p["gate_sessions"] + p["rvol_sessions"],
                                     max_days=2 * p["gate_sessions"] + 40):
            for r in hist.slice(po - pre * MIN, po + (p["session_max_min"] + 1) * MIN) if hist is not None else []:
                rows[r[T]] = r
        for r in w["prior"].get(coin) or []:
            rows.setdefault(r[T], r)
        allb = [rows[t] for t in sorted(rows) if t < w["open_ms"]]
        return odds_gate(coin, d, w["open_ms"], w["lead"], lambda a, b: allb[bar_index(allb, a):bar_index(allb, b)],
                         lambda a, b: slot_volume(allb, a, b), p)

    async def _enter(self, w: dict, coin: str, act: dict, now: int) -> None:
        run = w["runs"][coin]
        sig = w["signals"].get(coin) or {}
        if run.entries > 1:   # antes de cada reentrada se vuelve a mirar la señal
            sig = w["signals"][coin] = await self._signal(coin, w)
            if not run.free and sig["dir"] != run.dir:
                run.on_rejected(now, "la señal ya no acompaña")
                run.end("La señal ya no acompaña: no se busca otra entrada")
                return
        pf = await self._portfolio()
        if pf is None:
            run.on_rejected(now, "sin acceso a la cartera del paper para comprobar las posiciones")
            return
        if pf.get("halted") or (pf.get("limits") or {}).get("halted"):   # detenido a mano o por la pérdida del día
            run.on_paused(now, "el paper está detenido; no se envía nada hasta que se reanude")
            return
        busy = self._coin_busy(pf, coin)
        if busy:   # una sola posición por moneda: ni doble riesgo ni largo y corto a la vez
            run.on_rejected(now, busy)
            return
        plan = entry_plan(act["dir"], self._entry_price(coin, act, now), act, self.p)
        w["plans"][coin] = {k: plan[k] for k in ("stopPct", "tp2R", "rr", "ok", "why")}
        if not plan["ok"]:   # puerta R:R desde la entrada real
            run.on_rejected(now, plan["why"])
            return
        day = self._day_ledger(pf, now)
        limit = day_limits(len(day["entries"]), sum(day["entries"].values()), self._risk(sig), self.p)
        if limit:
            run.on_rejected(now, limit)
            run.end(limit)
            return
        try:
            t = self._ticket(w, coin, act, sig, plan)
            n = w["seq"][coin] = w["seq"].get(coin, 0) + 1
            story = f"{self._prefix(w, coin)}{n}"
            verb = "Compra" if act["dir"] > 0 else "Venta"
            color = "verde" if act["dir"] > 0 else "roja"
            hl = f"Apertura {w['name']} · {verb} {coin} · señal {color} {_n(sig.get('strength') or 0.0)}"
            text = f"{act['why']} · riesgo {_n(t['risk'])} % · cierre como tarde a las {_hm(t['winEnd'])} Madrid"
            self.bus.publish({"type": "event", "ts": now, "kind": "strategy", "agent": "strat", "story": story,
                              "title": f"{verb} {coin} · {MODE}", "coins": [coin], "text": _cut(text, 400),
                              "patch": {"headline": hl, "src": SRC, "tier": "Plan", "t0": now, "coins": [coin],
                                        "strat": t}})
        except Exception as e:   # sin esto la moneda quedaría «en posición» sin orden toda la sesión
            log.exception("Aperturas: no pude preparar la orden de %s en %s: %s", coin, w["name"], e)
            run.on_rejected(now, "fallo interno al preparar la orden")
            return
        try:
            ok = await _maybe(self.submit(t, story, hl, STRATEGY, f"Apertura {w['name']}"))
        except Exception as e:
            log.exception("Aperturas: submit de %s falló: %s", story, e)
            ok = False
        if ok:
            w["stories"][story] = {"coin": coin, "at": now, "pending": False, "open": False}
            self._day_ledger(None, now)["entries"][story] = t["risk"]   # cuenta para los límites del día
            log.info("Aperturas: %s %s en %s (%s)", verb, coin, w["name"], story)
        else:
            run.on_rejected(now, "vetada por el control de riesgo del paper")

    # ---- modo impulso

    async def _news_at_measure(self, w: dict, coin: str, now: int) -> None:
        """Las noticias se vuelven a mirar al medir el impulso: el filtro y la etiqueta son los de ese momento."""
        run = w["runs"][coin]
        if run.state == "midiendo_impulso" and run.news is None and now >= w["open_ms"] + run.N * MIN:
            run.news = w["signals"][coin] = await self._signal(coin, w)

    def _live_price(self, coin: str, now: int) -> float | None:
        """Precio de OKX EU de hace menos de 15 s (None si no hay)."""
        m = (getattr(self.bus, "state", None) or {}).get("market") or {}
        live, upd = _f((m.get("prices") or {}).get(coin)), _f((m.get("updated") or {}).get(coin))
        return live if live and (upd is None or now - upd <= 15_000) else None

    def _funding_now(self, coin: str) -> float | None:
        """Tasa de funding de ahora en % por 8 h (paper.funding_8h de market.flow[moneda]) o None sin dato."""
        m = (getattr(self.bus, "state", None) or {}).get("market") or {}
        return paper.funding_8h((m.get("flow") or {}).get(coin))

    def _risk_imp(self) -> float:
        return float(self.p.get("risk_pct") or config.RISK_PER_TRADE_PCT)

    def _ticket_imp(self, w: dict, coin: str, act: dict, plan: dict, now: int) -> dict:
        """Ticket del modo impulso: inmediato, horquilla pura (tp1R = tp2R, sin parcial, break-even ni dinámico),
        7 días de seguridad desde la entrada, instrumento (contado o perpetuo), estrategia y etiquetas."""
        p, d, tags = self.p, act["dir"], act["tags"]
        end = now + int(p["max_hold_h"] * H)
        days = _n(p["max_hold_h"] / 24, 0)
        sig = w["signals"].get(coin) or {}
        return {"coin": coin, "side": "long" if d > 0 else "short", "mode": MODE_IMP, "risk": self._risk_imp(),
                "stopPct": plan["stopPct"], "trailPct": None, "timing": "now", "win": f"Apertura {w['name']}",
                "winAt": None, "entryType": impulse_entry_type(p), "tp1R": plan["tp2R"], "tp2R": plan["tp2R"],
                "tp1Frac": 0.0, "beR": None, "trailStartR": None,
                "valid": f"sin tope: como mucho {days} días (hasta el "
                         f"{datetime.fromtimestamp(end / 1000, MAD).strftime('%d-%m %H:%M')} Madrid)",
                "inval": _cut(f"Stop {_px(plan['stop_px'])} (−{_n(plan['stopPct'])} %) u objetivo "
                              f"{_px(plan['target_px'])} (+{_n(plan['gross_pct'])} %)", 160),
                "why": _cut(act["why"], 300), "llm": None, "winEnd": end, "holdLabel": f"{days} días de seguridad",
                "maxAgeMs": MAX_FILL_DELAY_MS, "instrument": plan["instrument"], "market": w["lead"], "src": SRC,
                "signal": {"dir": sig.get("dir"), "strength": sig.get("strength")},
                "strategy": impulse_strategy(d, tags), "tags": {**tags, "estrategia_firma": self.firma}}

    async def _enter_imp(self, w: dict, coin: str, act: dict, now: int) -> None:
        """Entrada del modo impulso: cartera, una posición por moneda, horquilla desde el precio real con el filtro del
        3 % neto, límites del día de Madrid y envío al paper con el nombre de estrategia según el lado y la noticia."""
        run = w["runs"][coin]
        pf = await self._portfolio()
        if pf is None:
            run.on_rejected(now, "sin acceso a la cartera del paper para comprobar las posiciones")
            return
        if pf.get("halted") or (pf.get("limits") or {}).get("halted"):
            run.on_paused(now, "el paper está detenido")
            return
        if self.paused:   # la evolución pausó el campeón: se mide la apertura pero no se entra
            run.on_paused(now, "el campeón está pausado por la evolución (solo paper)")
            return
        busy = self._coin_busy(pf, coin)
        if busy:   # una sola posición por moneda (como el laboratorio: la apertura se salta)
            run.on_rejected(now, busy)
            return
        d, tags = act["dir"], act["tags"]
        inst = impulse_instrument(d, self.p)
        entry = self._live_price(coin, now) or act["price"]
        plan = impulse_plan(d, entry, self.p, self._funding_now(coin), inst)
        w["plans"][coin] = {k: plan[k] for k in ("stopPct", "tp2R", "rr", "ok", "why", "instrument", "gross_pct",
                                                 "cost_pct", "funding_pct", "net_pct")}
        w["plans"][coin].update(entry=entry, stop=plan["stop_px"], target=plan["target_px"])
        if not plan["ok"]:
            run.on_rejected(now, plan["why"])
            return
        day = self._day_ledger(pf, now)
        limit = day_limits(len(day["entries"]), sum(day["entries"].values()), self._risk_imp(), self.p)
        if limit:
            run.on_rejected(now, limit)
            return
        try:
            t = self._ticket_imp(w, coin, act, plan, now)
            n = w["seq"][coin] = w["seq"].get(coin, 0) + 1
            story = f"{self._prefix(w, coin)}{n}"
            verb = "Compra" if d > 0 else "Venta"
            news = {"a_favor": "noticia a favor", "en_contra": "noticia en contra"}.get(tags["noticia"], "sin noticia")
            inst_t = "perpetuo" if inst == "perp" else "contado"
            hl = f"Apertura {w['name']} · {verb} {coin} ({inst_t}, paper) · impulso {_sg(tags['impulso_pct'])} % · {news}"
            text = (f"{act['why']} · {plan['why']} · riesgo {_n(t['risk'])} % · apertura anterior "
                    f"{tags['apertura_anterior'].replace('_', ' ')} · BTC {tags['btc'].replace('_', ' ')}")
            self.bus.publish({"type": "event", "ts": now, "kind": "strategy", "agent": "strat", "story": story,
                              "title": f"{verb} {coin} · Impulso de apertura (paper)", "coins": [coin],
                              "text": _cut(text, 400),
                              "patch": {"headline": hl, "src": SRC, "tier": "Plan", "t0": now, "coins": [coin],
                                        "strat": t, "tags": dict(tags)}})
        except Exception as e:
            log.exception("Aperturas: no pude preparar la orden de %s en %s: %s", coin, w["name"], e)
            run.on_rejected(now, "fallo interno al preparar la orden")
            return
        try:
            ok = await _maybe(self.submit(t, story, hl, t["strategy"], f"Apertura {w['name']}"))
        except Exception as e:
            log.exception("Aperturas: submit de %s falló: %s", story, e)
            ok = False
        if ok:
            w["stories"][story] = {"coin": coin, "at": now, "pending": False, "open": False}
            self._day_ledger(None, now)["entries"][story] = t["risk"]
            run.entry_px, run.stop_px, run.target_px = entry, plan["stop_px"], plan["target_px"]
            run.last_why = (f"En posición (paper): {verb.lower()} {INST_TEXT[inst]} a {_px(entry)}, stop "
                            f"{_px(plan['stop_px'])} y objetivo {_px(plan['target_px'])}; sin tope de tiempo "
                            f"({t['holdLabel']})")
            log.info("Aperturas: %s %s en %s (%s, %s)", verb, coin, w["name"], story, t["strategy"])
        else:
            run.on_rejected(now, "vetada por el control de riesgo del paper")

    async def _exits(self, w: dict, now: int) -> None:
        """Lee la cartera del paper: posiciones abiertas, cerradas y órdenes que no llegaron a ejecutarse."""
        open_st = [s for s, x in w["stories"].items() if not x.get("done")]
        if not open_st:
            return
        pf = await self._portfolio()
        if pf is None:
            return
        self._day_ledger(pf, now)
        closed: dict[str, dict] = {}   # la más reciente de cada story
        for t in pf.get("closed") or []:
            if isinstance(t, dict) and (t.get("story") not in closed
                                        or _f(t.get("closed"), 0) > _f(closed[t["story"]].get("closed"), 0)):
                closed[t.get("story")] = t
        positions = {x.get("story") for x in pf.get("positions") or [] if isinstance(x, dict)}
        queued = {x.get("story") for x in (pf.get("pending") or []) + (pf.get("scheduled") or []) if isinstance(x, dict)}
        for s in open_st:
            x = w["stories"][s]
            run = w["runs"][x["coin"]]
            t = closed.get(s)
            opened = _f((t or {}).get("opened"))
            if t and (opened is None or opened >= x["at"] - MIN):   # una cerrada antigua con la misma story no vale
                x["done"] = True
                run.on_exit(int(_f(t.get("closed"), now)), _f(t.get("pnl_pct"), 0.0), t.get("reason") or "cerrada")
                if run.state == "rearmada" and not run.free:
                    s2 = w["signals"][x["coin"]] = await self._signal(x["coin"], w)
                    run.set_signal(s2["dir"])
            elif s in positions:
                x["open"] = True
            elif s in queued:
                x["pending"] = True
            elif now - x["at"] <= SUBMIT_GRACE_MS:
                continue
            elif x["open"]:   # estuvo abierta y salió de la cartera sin aparecer en cerradas
                x["done"] = True
                run.on_exit(now, 0.0, "cerrada (sin detalle en la cartera)")
            else:   # nunca llegó a abrirse: rechazada, caducada o bloqueada al ejecutarse
                x["done"] = True
                self.day["entries"].pop(s, None)   # no cuenta como entrada del día
                run.on_rejected(now, "el paper no llegó a ejecutarla (rechazada, caducada o bloqueada)"
                                if x["pending"] else "el paper no llegó a ejecutarla (bloqueada o rechazada)")

    # ---- ronda

    async def step(self, now: int | None = None) -> None:
        now = self.clock() if now is None else now
        try:
            self._check_activa(now)
        except Exception:   # un archivo raro no para el motor: sigue con lo que tenía
            log.exception("Aperturas: no pude recargar la estrategia de la evolución")
        self._schedule(now)
        for w in sorted(self.windows.values(), key=lambda x: x["open_ms"]):
            try:   # cada ventana aparte: un fallo en una no corta las demás ni la publicación
                await self._window(w, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                if time.monotonic() - self._err_at > 60:
                    self._err_at = time.monotonic()
                    log.exception("Aperturas: fallo en la ventana %s", w.get("key"))
        self._publish(now)

    async def _window(self, w: dict, now: int) -> None:
        if w["missed"] or now < w["open_ms"] - self.p["lead_min"] * MIN:
            return
        end = w["open_ms"] + self.p["session_max_min"] * MIN
        if not w["armed"] and now >= end:   # terminó con el bot parado: no se arma con una señal de después
            w["missed"] = True
            w["why"] = "La sesión de esta apertura pasó con el bot parado: no se siguió"
            return
        if not w["armed"]:
            await self._arm(w, now >= w["open_ms"], now)
        elif not w["at_open"] and now >= w["open_ms"]:
            await self._arm(w, True, now)
        await self._exits(w, now)
        active = [c for c, r in w["runs"].items() if not r.done]
        if now >= end + 2 * MIN:   # sin velas nuevas (corte de datos) la sesión se cierra igual
            for c in active:
                if w["runs"][c].state != "en_posicion":
                    w["runs"][c].end("Fin de la sesión")
            return
        if not active or now < w["open_ms"] + MIN or now < self._retry_at:
            return
        for coin in active:
            try:
                if self.impulso:
                    await self._context(w, coin)
                await self._update_bars(w, coin, now)
                self._fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:   # espera creciente; nunca tumba el bucle
                self._fails += 1
                wait = min(300, 5 * 2 ** min(self._fails, 6))
                self._retry_at = now + wait * 1000
                if time.monotonic() - self._err_at > 60:
                    self._err_at = time.monotonic()
                    log.warning("Aperturas: velas de %s no disponibles (%s); reintento en %d s", coin, e, wait)
                break
            if self.p.get("gate") and not await self._gate_ok(w, coin):
                continue
            if self.impulso:
                await self._news_at_measure(w, coin, now)
            for act in self._process(w, coin, now):
                await (self._enter_imp if self.impulso else self._enter)(w, coin, act, now)

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                if time.monotonic() - self._err_at > 60:
                    self._err_at = time.monotonic()
                    log.exception("Aperturas: fallo en la ronda")
            await asyncio.sleep(TICK_S)

    # ---- estado

    def _wstate(self, w: dict, now: int) -> str:
        runs = list(w["runs"].values())
        if w["missed"]:
            return "terminada"
        if not w["armed"]:
            return "esperando"
        if not runs or all(r.done for r in runs):
            return "terminada"
        if any(r.state == "en_posicion" for r in runs):
            return "en_posicion"
        if now < w["open_ms"]:
            return "armada"
        for s in ("midiendo_rango", "rearmada"):
            if any(r.state == s for r in runs):
                return s
        return "armada"

    def _wstate_imp(self, w: dict, now: int) -> str:
        """Fase de la ventana (IMP_STATES): la más avanzada de sus monedas que siguen vivas."""
        runs = list(w["runs"].values())
        if w["missed"]:
            return "terminada"
        if not w["armed"] or now < w["open_ms"]:
            return "esperando_apertura"
        if not runs or all(r.done for r in runs):
            return "terminada"
        for s in ("en_posicion", "esperando_entrada", "midiendo_impulso"):
            if any(r.state == s for r in runs):
                return s
        return "midiendo_impulso"

    def _coin_imp(self, w: dict, c: str, now: int) -> dict:
        """Estado de una moneda en modo impulso: fase (state, de IMP_STATES), paso de la entrada, por qué en español,
        niveles, etiquetas, contexto y plan, con los campos de la v2 que lee la web."""
        s, run = w["signals"].get(c), w["runs"].get(c)
        news = (run.news if run is not None and run.news is not None else s) or {}
        if run is None:
            state = "terminada" if w["missed"] else "esperando_apertura"
            why = w["why"] if w["missed"] else (f"A las {_hm(w['open_ms'])} Madrid se mide el impulso de los "
                                                 f"{self.p['measure_min']} primeros min")
        else:
            state, why = run.state, run.last_why
            if state == "midiendo_impulso" and now < w["open_ms"]:
                state = "esperando_apertura"
        ctx = (run.ctx if run is not None else None) or w["ctx"].get(c)
        d = run.dir if run is not None else 0
        return {"signal": d, "strength": news.get("strength", 0.0), "why": why, "last_why": why,
                "drivers": news.get("drivers") or [],
                "news": {"dir": news.get("dir", 0), "strength": news.get("strength", 0.0), "why": news.get("why") or "",
                         "label": run.tags.get("noticia") if run is not None and run.tags else None},
                "range": run.rng if run is not None else None,
                "rvol": round(run.rvol, 2) if run is not None and run.rvol is not None else None, "buy_ratio": None,
                "entries": run.entries if run is not None else 0,
                "results": [round(x, 3) for x in run.results] if run is not None else [], "state": state,
                "step": run.step if run is not None else None,
                "instrument": ((run.tags or {}).get("instrumento") or impulse_instrument(d, self.p)) if d else None,
                "gate": None, "moving": None, "plan": w["plans"].get(c),
                "tags": (run.tags or None) if run is not None else None,
                "levels": run.levels() if run is not None and (run.po is not None or run.entry_px) else None,
                "context": {k: ctx.get(k) for k in ("atr_pct", "atr_h_pct", "btc_dir", "prev_ms", "prev_react")}
                if ctx else None}

    def build_status(self, now: int) -> dict:
        if self.impulso:
            day = self._day_ledger(None, now)
            wins = [{"id": w["key"], "name": w["name"], "markets": w["markets"], "open_utc": w["open_ms"],
                     "state": self._wstate_imp(w, now), "why": w["why"],
                     "coins": {c: self._coin_imp(w, c, now) for c in self.p["coins"]}}
                    for w in sorted(self.windows.values(), key=lambda x: x["open_ms"])]
            return {"type": "openings", "ts": now, "strategy": "impulso", "windows": wins,
                    "params": {k: self.p[k] for k in IMP_STATUS_KEYS}, "params_source": self.source,
                    "notice": self.notice, "states": list(IMP_STATES), "paused": self.paused,
                    "estrategia_cargada": self.loaded,
                    "day": {"date": day["date"], "entries": len(day["entries"]),
                            "risk_pct": round(sum(day["entries"].values()), 3),
                            "max_entries": self.p.get("max_day_entries"), "max_risk_pct": self.p.get("max_day_risk_pct")}}
        out = []
        for w in sorted(self.windows.values(), key=lambda x: x["open_ms"]):
            coins = {}
            for c in config.COINS:
                s, run = w["signals"].get(c), w["runs"].get(c)
                f = run.feats if run else {}
                coins[c] = {"signal": (s or {}).get("dir", 0), "strength": (s or {}).get("strength", 0.0),
                            "why": (s or {}).get("why") or ("Se calcula 15 min antes de la apertura"
                                                            if not w["armed"] and not w["missed"] else ""),
                            "drivers": (s or {}).get("drivers") or [],
                            "range": {"hi": run.rng["hi"], "lo": run.rng["lo"]} if run and run.rng else None,
                            "rvol": round(f["rvol"], 2) if f.get("rvol") is not None else None,
                            "buy_ratio": round(f["buy_ratio"], 3) if f.get("buy_ratio") is not None else None,
                            "entries": run.entries if run else 0,
                            "results": [round(x, 3) for x in run.results] if run else [],
                            "state": run.state if run else ("terminada" if w["missed"] else
                                                            "esperando" if not w["armed"] else "sin_senal"),
                            "last_why": run.last_why if run else (w["why"] if w["missed"] else
                                                                  (s or {}).get("why") or ""),
                            # v2: puerta de probabilidades, bandera MOVING y plan de la última entrada
                            "gate": w["gate"].get(c), "moving": (run.moving or None) if run else None,
                            "plan": w["plans"].get(c)}
            out.append({"id": w["key"], "name": w["name"], "markets": w["markets"], "open_utc": w["open_ms"],
                        "state": self._wstate(w, now), "why": w["why"], "coins": coins})
        day = self._day_ledger(None, now)   # a medianoche de Madrid empieza el día nuevo (solo cambia la fecha)
        return {"type": "openings", "ts": now, "strategy": "ruptura", "windows": out,
                "params": {k: self.p[k] for k in ("range_min", "rvol_min", "trail_atr", "min_strength", "confirm_tf",
                                                  "stop_mode", "target_r", "target_pct", "tp1_frac", "be_r",
                                                  "trail_start_r", "min_rr", "moving_pct", "max_day_entries",
                                                  "max_day_risk_pct", "gate", "gate_pp", "gate_min_n")},
                "params_source": self.source, "notice": self.notice,
                "day": {"date": day["date"], "entries": len(day["entries"]),
                        "risk_pct": round(sum(day["entries"].values()), 3),
                        "max_entries": self.p.get("max_day_entries"), "max_risk_pct": self.p.get("max_day_risk_pct")}}

    def _publish(self, now: int) -> None:
        st = self.build_status(now)
        key = json.dumps({k: v for k, v in st.items() if k != "ts"}, sort_keys=True, ensure_ascii=False)
        self.status = st
        if key == self._key or time.monotonic() - self._pub_at < PUBLISH_MIN_S:
            return
        self._key, self._pub_at = key, time.monotonic()
        self.bus.publish(st)

    def view(self) -> dict:
        """Último estado «openings» (para la API)."""
        return self.status or self.build_status(self.clock())
