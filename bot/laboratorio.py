"""Laboratorio de estrategias de apertura: prueba ~2.000 reglas inventadas que encadenan noticia o bolsas previas →
tendencia en la apertura → momento, volumen y volatilidad → entrada, stop y objetivo, con las velas de 1 min de Binance
de config.HIST_DIR/1m, y separa lo que aguanta de lo que es suerte.

Reglas del usuario, en TODA estrategia (las comprueban las pruebas):
- Beneficio/riesgo de 2, 2,5 o 3 (nunca menos de 2:1) y stop de 1,5, 2, 2,5 o 3 % (nunca menos de 1,5 %).
- Horquilla pura: sin break-even ni stop dinámico; o toca el objetivo o el stop. El cierre por tiempo (24 h, la misma
  apertura del día siguiente o 7 días de seguridad) rompe el «2R o −1R» y se cuenta aparte.
- Se opera en las aperturas reales (con festivos) de Tokio-Seúl, China, Europa, São Paulo y Nueva York, siguiendo a las
  bolsas que cerraron antes o la reacción de los primeros minutos.

Sin mirar al futuro: cada dato se calcula con lo que había antes de decidir (cierres de bolsa con el margen de
backtest.known(); velas cerradas antes de la vela de entrada). Supuesto pesimista dentro de la vela (si toca stop y
objetivo en la misma vela, cuenta el stop; en la vela de una entrada límite, el extremo favorable no cuenta).
Costes: 0,10 % de comisión por lado, 2 pb de deslizamiento por lado y, en las salidas por stop, 5 pb más en BTC y ETH y
20 pb más en SOL y XRP (libro de OKX EU en USDC, menos profundo). El objetivo solo se cobra si el precio lo supera en
2 pb (un simple toque puede no llenar la orden límite). R = resultado neto / stop.

Contra la suerte: corte cronológico 60/40. Base emparejada: los MISMOS eventos de la estrategia (todos sus filtros) con
la dirección al azar por apertura (las 4 monedas se mueven juntas), pasando por sus confirmaciones y su entrada, una vez
con la misma cuota de largos que la estrategia (separa la habilidad de la exposición a la tendencia del periodo) y otra
al 50 %; hay que batir el percentil 95 de las dos. Permutación por bloques: el sentido de cada apertura se reparte entre
las aperturas de la misma ventana (se guarda también el p por evento; no aplica a las entradas «pb», cuya dirección la
fija el propio disparador). Benjamini-Hochberg al 10 % con p de error típico HAC (Newey-West, 7 días); Sharpe
deflactado con el T de cada estrategia y el número efectivo de pruebas (Li y Ji, por correlación de las series
diarias); vecinos de la rejilla positivos dentro y fuera; positiva en cada año fuera de muestra.
«Prometedora»: positiva dentro y fuera, bate a sus dos bases emparejadas, sigue positiva fuera sin su mejor mes, opera
en bastantes semanas distintas y no es negativa en ningún año de la muestra. «Robusta»: además pasa todo lo demás. Las
dos son solo candidatas: el periodo lo eligió el usuario con una pasada anterior a la vista, así que el único fuera de
muestra virgen es el paper desde la fecha de congelación (se guarda la lista con fecha).

Uso: python -m bot.laboratorio [--desde 2023-11] [--procesos 6] [--max 2000] [--salida laboratorio.json]
Solo lee la caché (no descarga nada) y escribe en config.BACKTEST_DIR. No toca aperturas.json ni la recomendación que
carga el motor en vivo."""
import argparse
import hashlib
import heapq
import json
import logging
import math
import os
import random
import statistics
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import apertura, backtest, config, history, paper, regions
from . import backtest_aperturas as bt
from .models import now_ms

log = logging.getLogger("laboratorio")

VERSION = 2
MIN, H, DAY = 60_000, 3_600_000, 86_400_000
YEAR_MS = 365.25 * DAY
MAD = ZoneInfo("Europe/Madrid")
COINS = list(config.COINS)

# Costes
FEE = config.FEE_PCT / 100
SLIP = 2.0 / 1e4
STOP_EXTRA = 5.0 / 1e4                       # extra en los stops de BTC y ETH (y el que se usa sin moneda)
STOP_EXTRA_ALT = 20.0 / 1e4                  # extra en los stops de SOL y XRP (escenario central de 15-30 pb)
ALT_COINS = ("SOL", "XRP")
TP_THROUGH = 2.0 / 1e4                       # el objetivo se cobra si el precio lo supera en 2 pb (no basta tocarlo)
FEE_PERP = 0.05 / 100                        # comisión por lado de un perpetuo (OKX, sin verificar para X-Perps EU)

# Horquillas (reglas del usuario) y vecinos de estabilidad (3,5 solo para los vecinos: también cumplen las reglas)
SL_GRID = (1.5, 2.0, 2.5, 3.0)
R_GRID = (2.0, 2.5, 3.0)
SL_ALL = SL_GRID + (3.5,)
R_ALL = R_GRID + (3.5,)
ADV_LV = SL_ALL
FAV_LV = tuple(sorted({round(s * r, 4) for s in SL_ALL for r in R_ALL}))
DURS = ("7d", "24h", "sig")
DUR_TEXT = {"7d": "sin tope de tiempo (7 días de seguridad)", "24h": "como mucho 24 h",
            "sig": "hasta la misma apertura del día siguiente"}
HMAX = 7 * 1440              # 7 días en velas de 1 min
BLOCK = 60                   # bloques de 1 h para el primer paso
NBH = HMAX // BLOCK + 2
BIG = 2 ** 30                # «no llega» en los tiempos de primer paso
CHUNK = 4000

# Entradas y características de la apertura
RANGES = (5, 15, 30, 60)
TFS = (1, 5)
BRK_END_MIN = 120            # la ruptura tiene que confirmarse antes de +120 min
RETEST_MIN = 60              # el retesteo caduca 60 min después de la ruptura
TIMES = (15, 30, 60)
IMP_X = (0.5, 1.0, 1.5)
IMP_N = (15, 30, 60)
PB_N = (15, 30)              # retroceso y reanudación tras los primeros N min
PB_END_MIN = 120
PB_FRAC = 0.5
PRE_N = (15, 60, 240)
PRE_BARS, POST_BARS = 241, 240
COVERAGE = 0.9
GAP_RUN = 15                 # un hueco de ≥ 15 min seguidos sin velas invalida la operación que lo cruza
BREAK_BPS, BREAK_FRAC = 2.0, 0.05   # margen de ruptura como el motor (2 pb o el 5 % del rango)
RVOL_SESS = 5
ATR_N, ATR_LOOK_H = 14, 720
MACRO_BEFORE_MIN, MACRO_AFTER_MIN, MACRO_REACT_MIN = 240, 60, 15
CHAIN_24H = 24 * H
CHAIN_MAX_AGE = 4 * DAY
NONE_T = np.iinfo(np.int64).max

# Evaluación (criterios fijados antes de la pasada del 26-10-2026; se guardan con la lista congelada)
IS_SHARE = 0.60
MIN_TRADES = 30
MIN_WEEKS_OOS = 20           # semanas distintas con operaciones fuera de muestra (hay ~62)
MIN_WEEKS_ALL = 60           # semanas distintas con operaciones en todo el periodo (hay ~156)
MIN_YEAR_IS = 15             # un año de la muestra con al menos tantas operaciones no puede salir negativo
FDR_Q = 0.10
N_NULL = 200
N_PERM = 1000
P_PERM = 0.05
DSR_MIN = 0.95
NULL_Q = 0.95
HAC_LAGS = 7                 # Newey-West con 7 días de retardo (las operaciones de 7 días se solapan)
N_BOOT = 2000                # remuestreo de meses fuera de muestra
MAX_ACCOUNT = 10
MAX_PERM_CANDS = 120
WF_MIN_TRAIN_Q = 2           # selección trimestral en avance: al menos 2 trimestres de entrenamiento
SOURCE_COVER_MIN = 0.5       # una fuente opcional que cubre menos de la mitad del periodo no se prueba
SEED = 20261026
EULER = 0.5772156649015329
PROTECTED = {"aperturas.json", "aperturas-v1.json", "resultados.json"}
COMPARE_DEFAULT = "laboratorio-desde-2021-01.json"   # pasada anterior con las mismas estrategias (solo referencia)

ASIA_M = ("tokio", "seul", "shanghai", "hongkong")
EUROPE_M = ("frankfurt", "londres", "madrid")
WIN_IDS = ("tokio-seul", "china", "europa", "saopaulo", "nuevayork")
WIN_NAME = {"tokio-seul": "Tokio y Seúl", "china": "Shanghái y Hong Kong", "europa": "Europa", "saopaulo": "São Paulo",
            "nuevayork": "Nueva York"}
WIN_SETS = {"todas": WIN_IDS, "nuevayork": ("nuevayork",), "europa": ("europa",), "asia": ("tokio-seul", "china"),
            "america": ("saopaulo", "nuevayork")}
WIN_TEXT = {"todas": "todas las aperturas", "nuevayork": "solo Nueva York", "europa": "solo Europa",
            "asia": "solo Asia (Tokio-Seúl y China)", "america": "solo América (São Paulo y Nueva York)"}
COIN_SETS = {"todas": tuple(COINS), "BTC+ETH": ("BTC", "ETH"), "SOL+XRP": ("SOL", "XRP"), "BTC": ("BTC",),
             "ETH": ("ETH",), "SOL": ("SOL",), "XRP": ("XRP",)}

KEYS = ([("brk", R, tf) for R in RANGES for tf in TFS] + [("ret", R, tf) for R in RANGES for tf in TFS]
        + [("t", N) for N in TIMES] + [("imp", X, N) for X in IMP_X for N in IMP_N] + [("pb", N) for N in PB_N])
KEY_IDX = {k: i for i, k in enumerate(KEYS)}
NK = len(KEYS)


# ---------------------------------------------------------------- textos

def _num(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _sg(x: float, d: int = 2) -> str:
    return ("+" if x >= 0 else "") + _num(x, d)


def _pc(x: float, d: int = 1) -> str:
    return _num(x * 100, d) + " %"


def _g(x: float) -> str:
    """Número corto en español: 1,5 · 2 · 0,25."""
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


# ---------------------------------------------------------------- estadística (sin scipy)

def _betacf(a: float, b: float, x: float) -> float:
    fpmin, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > fpmin else fpmin)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > fpmin else fpmin)
        c = 1.0 + aa / c if abs(c) > fpmin else 1.0 + aa / fpmin
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > fpmin else fpmin)
        c = 1.0 + aa / c if abs(c) > fpmin else 1.0 + aa / fpmin
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Beta incompleta regularizada I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    bt_ = math.exp(lbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt_ * _betacf(a, b, x) / a
    return 1.0 - bt_ * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T ≥ t) con T de Student de df grados de libertad (p unilateral)."""
    if not (df > 0) or not math.isfinite(t):
        return 0.0 if t == math.inf else 1.0 if t == -math.inf else float("nan")
    x = df / (df + t * t)
    p = 0.5 * betainc(df / 2.0, 0.5, x)
    return p if t > 0 else 1.0 - p


def bh_qvalues(ps: list[float]) -> list[float]:
    """q-valores de Benjamini-Hochberg (los mismos que backtest.bh_qvalues; NaN cuenta como 1)."""
    return backtest.bh_qvalues([1.0 if (p is None or not math.isfinite(p)) else float(p) for p in ps])


def expected_max_z(n_trials: float) -> float:
    """Máximo esperado de n_trials normales estándar independientes (aproximación de Bailey y López de Prado)."""
    if n_trials <= 1:
        return 0.0
    nd = statistics.NormalDist()
    return (1 - EULER) * nd.inv_cdf(1 - 1 / n_trials) + EULER * nd.inv_cdf(1 - 1 / (n_trials * math.e))


def deflated_sharpe(sr: float, T: int, skew: float, kurt: float, sr_var: float | None, n_trials: float) -> float:
    """Sharpe deflactado (Bailey y López de Prado, 2014): probabilidad de que el Sharpe verdadero supere al máximo
    esperado por azar entre n_trials pruebas. Con sr_var se usa esa varianza de los Sharpe (la original, común a
    todas); con sr_var None, la varianza nula de ESTA estrategia con su propio T, (1 − asim·SR + (curt − 1)/4·SR²)/(T − 1),
    para no castigar a las que tienen más datos. sr, skew y kurt (no excedente: normal = 3) son del mismo periodo que T."""
    if T < 3 or not math.isfinite(sr):
        return float("nan")
    nd = statistics.NormalDist()
    den = max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-12)
    var0 = den / (T - 1) if sr_var is None else sr_var
    sr0 = math.sqrt(var0) * expected_max_z(n_trials) if var0 > 0 else 0.0
    z = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(den)
    return nd.cdf(z)


def hac_t(x, lags: int = HAC_LAGS) -> float:
    """t de la media con error típico de Newey-West (núcleo de Bartlett): corrige la autocorrelación de la serie diaria
    (operaciones de varios días que se solapan)."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return 0.0
    z = x - x.mean()
    s = float((z * z).sum()) / n
    for k in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - k / (lags + 1)) * float((z[k:] * z[:-k]).sum()) / n
    return float(x.mean()) / math.sqrt(s / n) if s > 0 else 0.0


def effective_trials(series: np.ndarray) -> float:
    """Número efectivo de pruebas independientes (Li y Ji, 2005) a partir de las series diarias de las estrategias
    (filas): suma de f(λ) = [λ ≥ 1] + (λ − ⌊λ⌋) sobre los autovalores de su matriz de correlación."""
    X = np.asarray(series, float)
    if X.ndim != 2 or X.shape[0] < 2 or X.shape[1] < 3:
        return float(max(1, X.shape[0] if X.ndim == 2 else 1))
    sd = X.std(1)
    X = X[sd > 0]
    if len(X) < 2:
        return 1.0
    Z = (X - X.mean(1, keepdims=True)) / X.std(1, keepdims=True)
    G = (Z.T @ Z) / Z.shape[1]          # mismos autovalores no nulos que la correlación entre estrategias
    lam = np.clip(np.linalg.eigvalsh(G), 0, None)
    return float(max(1.0, ((lam >= 1) + (lam - np.floor(lam))).sum()))


def moments(x: np.ndarray) -> tuple[float, float, float, float]:
    """Media, desviación (poblacional), asimetría y curtosis (no excedente)."""
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else 0.0), 0.0, 0.0, 3.0
    m = float(x.mean())
    sd = float(x.std())
    if sd <= 0:
        return m, 0.0, 0.0, 3.0
    z = (x - m) / sd
    return m, sd, float((z ** 3).mean()), float((z ** 4).mean())


def _q(xs, p: float) -> float:
    xs = np.asarray(xs, float)
    return float(np.quantile(xs, p)) if len(xs) else float("nan")


# ---------------------------------------------------------------- velas de 1 min en numpy (solo caché)

def month_files(coin: str, desde: str | None = None, folder: Path | None = None) -> list[Path]:
    """Archivos mensuales de la caché de backtest_aperturas para una moneda (el zip manda sobre la parte de la API)."""
    folder = Path(folder) if folder else bt.dir_1m()
    sym = bt.SYMS[coin]
    zips, apis = {}, {}
    if not folder.exists():
        return []
    for p in folder.iterdir():
        name = p.name
        if not name.startswith(sym + "-1m-"):
            continue
        rest = name[len(sym) + 4:]
        ym = rest[:7]
        if len(ym) != 7 or ym[4] != "-" or not (ym[:4] + ym[5:]).isdigit() or (desde and ym < desde):
            continue
        if rest == ym + ".zip":
            zips[ym] = p
        elif rest == ym + "-api.json":
            apis[ym] = p
    by = {**apis, **zips}
    return [by[k] for k in sorted(by)]


def parse_zip(path) -> np.ndarray:
    """Zip mensual de Binance → filas [t_ms, o, h, l, c, vol, vol_quote] (microsegundos pasados a ms)."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    txt = raw.decode("ascii", "ignore").replace("\r", "")
    if txt and not txt[0].isdigit():
        txt = txt.split("\n", 1)[1] if "\n" in txt else ""
    txt = txt.strip()
    if not txt:
        return np.empty((0, 7))
    try:
        a = np.fromstring(txt.replace("\n", ","), sep=",")
    except ValueError:
        a = np.empty(1)
    if a.size % 12 or a.size < 12 * txt.count("\n"):   # formato inesperado: fila a fila
        rows = [r for r in (bt.parse_csv_line(x) for x in txt.split("\n")) if r]
        return np.array([r[:7] for r in rows], float).reshape(-1, 7)
    a = a.reshape(-1, 12)
    t = np.where(a[:, 0] > 1e14, np.floor(a[:, 0] / 1000), a[:, 0])
    return np.column_stack([t, a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], a[:, 7]])


def parse_api(path) -> np.ndarray:
    rows = (history._read(Path(path)) or {}).get("rows") or []
    if not rows:
        return np.empty((0, 7))
    return np.array([r[:7] for r in rows], float)


def dense_from_rows(a: np.ndarray) -> dict | None:
    """Filas [t, o, h, l, c, v, qv] → serie en rejilla de 1 min sin huecos: las velas que faltan repiten el último
    cierre con volumen 0 y valid = False."""
    if a is None or not len(a):
        return None
    a = a[(a[:, 1] > 0) & (a[:, 2] > 0) & (a[:, 3] > 0) & (a[:, 4] > 0)]
    if not len(a):
        return None
    a = a[np.argsort(a[:, 0], kind="stable")]
    t = (a[:, 0].astype(np.int64) // MIN) * MIN
    keep = np.concatenate(([True], np.diff(t) > 0))
    a, t = a[keep], t[keep]
    t0 = int(t[0])
    n = int((t[-1] - t0) // MIN) + 1
    k = (t - t0) // MIN
    valid = np.zeros(n, bool)
    valid[k] = True
    last = np.maximum.accumulate(np.where(valid, np.arange(n), 0))
    c = np.zeros(n)
    c[k] = a[:, 4]
    c = c[last]
    o, h, l = c.copy(), c.copy(), c.copy()
    o[k], h[k], l[k] = a[:, 1], a[:, 2], a[:, 3]
    v, qv = np.zeros(n), np.zeros(n)
    v[k], qv[k] = a[:, 5], a[:, 6]
    return {"t0": t0, "n": n, "o": o, "h": h, "l": l, "c": c, "v": v, "qv": qv, "valid": valid}


def load_dense(paths) -> dict | None:
    parts = []
    for p in paths:
        p = Path(p)
        try:
            a = parse_zip(p) if p.suffix == ".zip" else parse_api(p)
        except Exception as e:   # un archivo dañado no para el resto
            log.warning("Velas ilegibles %s: %s", p, e)
            continue
        if len(a):
            parts.append(a)
    return dense_from_rows(np.concatenate(parts)) if parts else None


# ---------------------------------------------------------------- aperturas y bolsas previas (sin mirar al futuro)

def opening_meta(openings: list[dict]) -> dict:
    """Arrays por apertura: hora, ventana, la misma apertura anterior y siguiente, las dos aperturas anteriores (de
    cualquier ventana) y las 5 sesiones anteriores de la bolsa principal (para el rvol)."""
    no = len(openings)
    om = np.array([o["open_ms"] for o in openings], np.int64)
    wid = np.array([WIN_IDS.index(o["id"]) for o in openings], np.int64)
    nxt, prev_same = np.full(no, -1, np.int64), np.full(no, -1, np.int64)
    prev, prev2 = np.full(no, -1, np.int64), np.full(no, -1, np.int64)
    last_by: dict[int, int] = {}
    for i in range(no):
        w = int(wid[i])
        if w in last_by:
            prev_same[i] = om[last_by[w]]
            nxt[last_by[w]] = om[i]
        last_by[w] = i
        k = i - 1
        while k >= 0 and om[k] >= om[i]:
            k -= 1
        if k >= 0:
            prev[i] = om[k]
            k2 = k - 1
            while k2 >= 0 and om[k2] >= om[k]:
                k2 -= 1
            if k2 >= 0:
                prev2[i] = om[k2]
    ps = np.full((no, RVOL_SESS), -1, np.int64)
    for i, o in enumerate(openings):
        xs = apertura.prev_session_opens(o["lead"], o["open_ms"], RVOL_SESS)
        ps[i, :len(xs)] = xs
    mday = [datetime.fromtimestamp(t / 1000, MAD).date().isoformat() for t in om]
    wd = np.array([date.fromisoformat(o["day"]).weekday() for o in openings], np.int64)
    return {"open_ms": om, "wid": wid, "next_same_ms": nxt, "prev_same_ms": prev_same, "prev_ms": prev,
            "prev2_ms": prev2, "prev_sess": ps, "mday": mday, "weekday": wd}


def chain_table(openings: list[dict], idx: dict) -> dict[str, np.ndarray]:
    """Bolsas previas para cada apertura, solo con sesiones ya cerradas al abrir (backtest.known, 15 min de margen):
    última bolsa cerrada (c_ult, %), cuántas cerraron en las 24 h previas (c_n24) y la suma de sus signos (c_sum24) y
    su media (c_mean24), Nueva York (c_ny), Asia (c_asia) y Europa (c_eu); la sesión anterior de las bolsas de la
    propia ventana (own_prev) y la racha de la bolsa principal (own_streak, +k o −k sesiones seguidas)."""
    no = len(openings)
    keys = ("c_ult", "c_n24", "c_sum24", "c_mean24", "c_ny", "c_asia", "c_eu", "own_prev", "own_streak")
    out = {k: np.full(no, np.nan) for k in keys}
    dates = {k: sorted(x["close"]) for k, x in idx.items()}
    for i, o in enumerate(openings):
        om, lead, day = o["open_ms"], o["lead"], date.fromisoformat(o["day"])
        sess, own, lead_iso = [], [], None
        for k, x in idx.items():
            m = regions.MARKETS[k]
            d = datetime.fromtimestamp(om / 1000, ZoneInfo(m.tz)).date()
            for _ in range(8):
                if regions.is_trading_day(m, d) and backtest.known(k, d, lead, day):
                    iso = d.isoformat()
                    if iso > x["last"] or iso < x["first"]:
                        break   # pudo cerrar y no tenemos el dato: no se usa
                    if iso in x["close"]:
                        cl, pc = x["close"][iso]
                        cms = backtest._close_ms(k, d)
                        if om - cms <= CHAIN_MAX_AGE and pc:
                            rec = (cms, (cl / pc - 1) * 100, k)
                            (own if k in o["markets"] else sess).append(rec)
                            if k == lead:
                                lead_iso = iso
                        break
                d -= timedelta(days=1)
        if sess:
            out["c_ult"][i] = max(sess)[1]
            s24 = [x for x in sess if om - x[0] <= CHAIN_24H]
            out["c_n24"][i] = len(s24)
            out["c_sum24"][i] = sum((x[1] > 0) - (x[1] < 0) for x in s24)
            if s24:
                out["c_mean24"][i] = sum(x[1] for x in s24) / len(s24)
            ny = [x[1] for x in sess if x[2] == "nuevayork"]
            if ny:
                out["c_ny"][i] = ny[0]
            for key, group in (("c_asia", ASIA_M), ("c_eu", EUROPE_M)):
                g = [x[1] for x in s24 if x[2] in group]
                if g:
                    out[key][i] = sum(g) / len(g)
        if own:
            out["own_prev"][i] = sum(x[1] for x in own) / len(own)
        if lead_iso is not None:
            ds, cl = dates[lead], idx[lead]["close"]
            j = ds.index(lead_iso)
            sgn0, k = None, 0
            while j >= 0 and k < 10:
                c_, p_ = cl[ds[j]]
                s = (c_ > p_) - (c_ < p_)
                if s == 0 or (sgn0 is not None and s != sgn0):
                    break
                sgn0, k, j = s, k + 1, j - 1
            out["own_streak"][i] = (sgn0 or 0) * k
    return out


# ---------------------------------------------------------------- fuentes opcionales (las descarga otro agente)

def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("Archivo ilegible %s: %s", path, e)
        return None


def load_fng() -> dict[str, float] | None:
    """HIST_DIR/sentimiento/fng.json: [["AAAA-MM-DD", valor], ...]."""
    raw = _read_json(config.HIST_DIR / "sentimiento" / "fng.json")
    out = {}
    for r in raw or []:
        try:
            out[str(r[0])[:10]] = float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


def load_funding(coin: str) -> np.ndarray | None:
    """HIST_DIR/funding/<SIM>.json: [[ms, tasa], ...] (se prueba BTCUSDT, BTC, BTC-USDT-SWAP y BTCUSDC)."""
    for sim in (f"{coin}USDT", coin, f"{coin}-USDT-SWAP", f"{coin}USDC", f"{coin}-USDC-SWAP"):
        raw = _read_json(config.HIST_DIR / "funding" / f"{sim}.json")
        if isinstance(raw, dict):
            raw = raw.get("rows") or raw.get("series")
        if raw:
            try:
                a = np.array([[float(r[0]), float(r[1])] for r in raw], float)
            except (TypeError, ValueError, IndexError):
                continue
            if len(a):
                return a[np.argsort(a[:, 0])]
    return None


def load_macro(with_names: bool = False):
    """HIST_DIR/macro/eventos.json: [{name, date, t_ms}, ...] → horas de publicación ordenadas (ms); con with_names,
    también {ms: "CPI" | "NFP" | "FOMC" | "CPI+NFP"}."""
    raw = _read_json(config.HIST_DIR / "macro" / "eventos.json")
    if isinstance(raw, dict):
        raw = raw.get("events") or raw.get("rows")
    names: dict[int, set] = {}
    for e in raw or []:
        try:
            names.setdefault(int(e["t_ms"]), set()).add(str(e.get("name") or "?"))
        except (TypeError, ValueError, KeyError):
            continue
    ts = np.array(sorted(names), np.int64) if names else None
    if with_names:
        return ts, {t: "+".join(sorted(v)) for t, v in names.items()}
    return ts


def macro_in_windows(om: np.ndarray, macro_t: np.ndarray | None, names: dict[int, str]) -> dict[str, int]:
    """Cuántas aperturas tienen cada tipo de dato macro en su franja (de 4 h antes a 1 h después, el último publicado):
    es lo que de verdad prueba la familia macro."""
    out: dict[str, int] = {}
    if macro_t is None or not len(macro_t):
        return out
    pos = np.searchsorted(macro_t, om + MACRO_AFTER_MIN * MIN, side="right") - 1
    for i, p in enumerate(pos):
        if p >= 0 and macro_t[p] >= om[i] - MACRO_BEFORE_MIN * MIN:
            k = names.get(int(macro_t[p]), "?")
            out[k] = out.get(k, 0) + 1
    for k in sorted({x for v in names.values() for x in v.split("+")}):
        out.setdefault(k, 0)
    return dict(sorted(out.items()))


def _res_ms(res) -> int:
    """Resolución de GDELT («15min», «1h», «1d»…) en ms; sin dato, 1 día (lo más prudente)."""
    s = str(res or "").lower().strip()
    num = "".join(ch for ch in s if ch.isdigit()) or "1"
    k = int(num)
    if "min" in s:
        return k * MIN
    if "h" in s:
        return k * H
    if "d" in s or "day" in s:
        return k * DAY
    return DAY


def gdelt_lag(step: int) -> int:
    """Retraso de publicación de GDELT tras cerrar un tramo: 30 min con tramos de menos de 1 h y 1 h con tramos horarios
    o diarios (además, GDELT revisa sus series hacia atrás: lo descargado hoy no es exactamente lo que se veía)."""
    return 30 * MIN if step < H else H


def load_gdelt() -> list[dict] | None:
    """HIST_DIR/gdelt/<slug>.json: {query, resolution, series [[ms, tono, vol], ...]}. Una sola resolución por
    consulta (cripto.json y cripto-1h.json son la misma consulta): la que cubre más tiempo y, a igualdad, la más fina."""
    folder = config.HIST_DIR / "gdelt"
    if not folder.exists():
        return None
    by_query: dict[str, dict] = {}
    for p in sorted(folder.glob("*.json")):
        raw = _read_json(p)
        if not isinstance(raw, dict) or not raw.get("series"):
            continue
        try:
            a = np.array([[float(r[0]), float(r[1])] for r in raw["series"] if r[1] is not None], float)
        except (TypeError, ValueError, IndexError):
            continue
        if len(a) > 10:
            a = a[np.argsort(a[:, 0])]
            step = _res_ms(raw.get("resolution"))
            s = {"slug": p.stem, "query": raw.get("query"), "step": step, "lag": gdelt_lag(step),
                 "t": a[:, 0].astype(np.int64), "tone": a[:, 1]}
            q = str(raw.get("query") or p.stem.split("-")[0])
            old = by_query.get(q)
            span = lambda x: (int(x["t"][-1] - x["t"][0]), -x["step"])
            if old is None or span(s) > span(old):
                by_query[q] = s
    return [by_query[k] for k in sorted(by_query)] or None


def fng_features(om: np.ndarray, fng: dict | None) -> dict[str, np.ndarray]:
    """Miedo y codicia conocido al abrir: el del día UTC de (apertura − 1 h) y su cambio a 1, 3 y 7 días."""
    no = len(om)
    out = {k: np.full(no, np.nan) for k in ("fng", "fng_d1", "fng_d3", "fng_d7")}
    if not fng:
        return out
    for i, t in enumerate(om):
        d = datetime.fromtimestamp((t - H) / 1000, timezone.utc).date()
        v = fng.get(d.isoformat())
        if v is None:
            continue
        out["fng"][i] = v
        for k in (1, 3, 7):
            w = fng.get((d - timedelta(days=k)).isoformat())
            if w is not None:
                out[f"fng_d{k}"][i] = v - w
    return out


def funding_features(om: np.ndarray, fund: np.ndarray | None) -> np.ndarray:
    """Percentil (0-1) de la última tasa de funding ya publicada frente a las de los 30 días previos."""
    out = np.full(len(om), np.nan)
    if fund is None or not len(fund):
        return out
    ft, fr = fund[:, 0], fund[:, 1]
    for i, t in enumerate(om):
        k = int(np.searchsorted(ft, t, side="right")) - 1
        if k < 0:
            continue
        a = int(np.searchsorted(ft, t - 30 * DAY, side="right"))
        w = fr[a:k]
        if len(w) >= 20:
            out[i] = float((w < fr[k]).mean())
    return out


def gdelt_features(om: np.ndarray, series: list[dict] | None) -> np.ndarray:
    """Tono de las noticias (GDELT): media de las últimas 24 h ya publicadas frente a su media y desviación de los 30
    días previos (z), promediado entre las consultas que haya."""
    out = np.full(len(om), np.nan)
    if not series:
        return out
    zs = np.full((len(series), len(om)), np.nan)
    for s_i, s in enumerate(series):
        known = s["t"] + s["step"] + s.get("lag", gdelt_lag(s["step"]))   # al cerrar el tramo y publicarse
        for i, t in enumerate(om):
            k = int(np.searchsorted(known, t, side="right"))
            a24 = int(np.searchsorted(known, t - DAY, side="right"))
            a30 = int(np.searchsorted(known, t - 30 * DAY, side="right"))
            if k - a30 < 10 or k - a24 < 1:
                continue
            base = s["tone"][a30:k]
            sd = base.std()
            if sd > 0:
                zs[s_i, i] = (s["tone"][a24:k].mean() - base.mean()) / sd
    with np.errstate(all="ignore"):
        cnt = np.isfinite(zs).sum(0)
        tot = np.nansum(zs, 0)
    out[cnt > 0] = tot[cnt > 0] / cnt[cnt > 0]
    return out


def btc_trend(om: np.ndarray, closes: dict[int, float]) -> dict[str, np.ndarray]:
    """Tendencia diaria de BTC al abrir: cierre del último día UTC completo frente a sus medias de 20 y 50 días."""
    out = {k: np.full(len(om), np.nan) for k in ("btc_c", "btc_ma20", "btc_ma50")}
    if not closes:
        return out
    d0, d1 = min(closes), max(closes)
    cl = np.full(d1 - d0 + 1, np.nan)
    for d, v in closes.items():
        cl[d - d0] = v
    ok = np.isfinite(cl)
    last = np.maximum.accumulate(np.where(ok, np.arange(len(cl)), 0))
    cl = cl[last]
    cs = np.concatenate(([0.0], np.cumsum(cl)))
    yd = om // DAY - 1 - d0
    good = (yd >= 49) & (yd < len(cl))
    y = np.clip(yd, 0, len(cl) - 1)
    out["btc_c"] = np.where(good, cl[y], np.nan)
    out["btc_ma20"] = np.where(good, (cs[y + 1] - cs[np.clip(y - 19, 0, None)]) / 20, np.nan)
    out["btc_ma50"] = np.where(good, (cs[y + 1] - cs[np.clip(y - 49, 0, None)]) / 50, np.nan)
    return out


# ---------------------------------------------------------------- primer paso (numpy)

def block_max(x: np.ndarray) -> np.ndarray:
    nb = -(-len(x) // BLOCK)
    pad = np.full(nb * BLOCK, -np.inf)
    pad[:len(x)] = x
    return pad.reshape(nb, BLOCK).max(1)


def first_cross(x: np.ndarray, bx: np.ndarray, s: np.ndarray, scale: np.ndarray, thr, end: np.ndarray) -> np.ndarray:
    """Primer índice k en [s, end) con x[k]·scale ≥ thr, por fila y nivel (thr: (L,) o (m, L)); −1 si no llega.
    Primero el tramo hasta el final del bloque de s, luego el máximo acumulado de los bloques de 1 h siguientes y,
    en el bloque donde se cruza, la vela exacta."""
    s = np.asarray(s, np.int64)
    m, n = len(s), len(x)
    thr = np.broadcast_to(np.asarray(thr, float), (m, np.asarray(thr).shape[-1]))
    L = thr.shape[1]
    if m == 0:
        return np.full((0, L), -1, np.int64)
    end = np.asarray(end, np.int64)
    ar = np.arange(BLOCK)
    a = np.minimum((s // BLOCK + 1) * BLOCK, end)
    idx = s[:, None] + ar
    vals = np.where(idx < a[:, None], x[np.minimum(idx, n - 1)] * scale[:, None], -np.inf)
    run = np.maximum.accumulate(vals, axis=1)
    pos = (run[:, :, None] < thr[:, None, :]).sum(axis=1)       # el máximo acumulado no baja: es la posición
    hit = pos < (a - s)[:, None]
    out = np.where(hit, s[:, None] + pos, -1)
    b1 = s // BLOCK + 1
    bidx = b1[:, None] + np.arange(NBH)
    nbk = len(bx)
    inside = (bidx < nbk) & (bidx * BLOCK < end[:, None])
    bv = np.where(inside, bx[np.minimum(bidx, nbk - 1)] * scale[:, None], -np.inf)
    brun = np.maximum.accumulate(bv, axis=1)
    bpos = (brun[:, :, None] < thr[:, None, :]).sum(axis=1)
    r, q = np.nonzero(~hit & (bpos < NBH))
    if len(r):
        bf = b1[r] + bpos[r, q]
        kidx = bf[:, None] * BLOCK + ar
        vv = x[np.minimum(kidx, n - 1)] * scale[r][:, None] >= thr[r, q][:, None]
        k = bf * BLOCK + vv.argmax(1)
        ok = vv.any(1) & (k < end[r]) & (k < n)
        out[r[ok], q[ok]] = k[ok]
    return out


def hole_runs(valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Tramos de ≥ GAP_RUN velas seguidas que faltan: (inicios, finales exclusivos)."""
    d = np.diff(np.concatenate(([0], (~valid).astype(np.int8), [0])))
    st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    keep = en - st >= GAP_RUN
    return st[keep], en[keep]


def passages(S: dict, J: np.ndarray, D: np.ndarray, P: np.ndarray, LIM: np.ndarray, NXT: np.ndarray) -> dict:
    """Para cada entrada única (vela J, dirección D, precio P sin deslizamiento, límite LIM, siguiente apertura de la
    misma ventana NXT): velas hasta superar cada nivel favorable (FAV_LV, %) en TP_THROUGH (fav) o solo tocarlo
    (fav_t), velas hasta el nivel adverso (ADV_LV, %), el resultado bruto a favor en el stop (con hueco: si la vela abre
    peor, a la apertura), y para cada duración su largo efectivo, el resultado bruto al cierre y si se acaban los datos.
    En una entrada límite (retesteo) la vela de la entrada solo cuenta para el stop (pesimista)."""
    n, t0 = S["n"], S["t0"]
    o, h, l, c = S["o"], S["h"], S["l"], S["c"]
    m = len(J)
    J = np.asarray(J, np.int64)
    D = np.asarray(D, np.int64)
    E = P * (1 + D * SLIP)
    inv_l = 1.0 / l
    bh, bil = block_max(h), block_max(inv_l)
    end = np.minimum(J + HMAX, n)
    favl, advl = np.array(FAV_LV) / 100, np.array(ADV_LV) / 100
    nf = len(FAV_LV)
    up_thr = np.concatenate(((1 + favl) * (1 + TP_THROUGH), 1 + favl))          # superar el objetivo · tocarlo
    dn_thr = np.concatenate((1 / ((1 - favl) * (1 - TP_THROUGH)), 1 / (1 - favl)))
    fav = np.full((m, nf), BIG, np.int32)
    fav_t = np.full((m, nf), BIG, np.int32)
    adv = np.full((m, len(ADV_LV)), BIG, np.int32)
    adv_g = np.zeros((m, len(ADV_LV)), np.float32)
    for a in range(0, m, CHUNK):
        for side in (1, -1):
            sel = a + np.flatnonzero(D[a:a + CHUNK] == side)
            if not len(sel):
                continue
            j, e, en = J[sel], E[sel], end[sel]
            sf = j + LIM[sel].astype(np.int64)
            if side > 0:
                kf = first_cross(h, bh, sf, 1 / e, up_thr, en)
                ka = first_cross(inv_l, bil, j, e, 1 / (1 - advl), en)
            else:
                kf = first_cross(inv_l, bil, sf, e, dn_thr, en)
                ka = first_cross(h, bh, j, 1 / e, 1 + advl, en)
            kf_all = np.where(kf >= 0, kf - j[:, None], BIG)
            fav[sel], fav_t[sel] = kf_all[:, :nf], kf_all[:, nf:]
            adv[sel] = np.where(ka >= 0, ka - j[:, None], BIG)
            orat = o[np.clip(ka, 0, n - 1)] / e[:, None]
            gap = ka > j[:, None]
            if side > 0:
                g = np.where(gap, np.minimum(1 - advl, orat), 1 - advl) - 1
            else:
                g = 1 - np.where(gap, np.maximum(1 + advl, orat), 1 + advl)
            adv_g[sel] = np.where(ka >= 0, g, -advl)
    tJ = t0 + J * MIN
    lens = np.empty((m, len(DURS)), np.int64)
    lens[:, 0], lens[:, 1] = HMAX, 1440
    lens[:, 2] = np.clip(np.where(NXT > 0, (NXT - tJ) // MIN, HMAX), 1, HMAX)
    avail = (n - J)[:, None]
    dl_end = lens > avail
    eff = np.minimum(lens, avail)
    dl_g = D[:, None] * (c[J[:, None] + eff - 1] / E[:, None] - 1)
    hs, he = hole_runs(S["valid"])
    gap_at = np.full(m, BIG, np.int64)
    if len(hs):   # primer hueco que acaba después de la entrada (si la entrada cae dentro de uno, 0)
        k = np.searchsorted(he, J, side="right")
        kk = np.minimum(k, len(hs) - 1)
        gap_at = np.where(k < len(hs), np.maximum(hs[kk], J) - J, BIG)
    return {"d": D.astype(np.int8), "t": tJ, "px": E, "fav": fav, "fav_t": fav_t, "adv": adv, "adv_g": adv_g,
            "dl_len": eff.astype(np.int32), "dl_g": dl_g.astype(np.float32), "dl_end": dl_end,
            "gap": np.minimum(gap_at, BIG).astype(np.int64)}


# ---------------------------------------------------------------- tabla de eventos de una moneda

def _react(S: dict, cinv: np.ndarray, a_ms: np.ndarray, b_ms: np.ndarray) -> np.ndarray:
    """Retorno (%) de la moneda entre la apertura de la vela de a_ms y el cierre de la anterior a b_ms."""
    t0, n, o, c = S["t0"], S["n"], S["o"], S["c"]
    ia, ib = (a_ms - t0) // MIN, (b_ms - t0) // MIN
    ok = (a_ms > 0) & (ia >= 0) & (ib > ia) & (ib <= n)
    ia_, ib_ = np.clip(ia, 0, n - 1), np.clip(ib, 1, n)
    ok &= (cinv[ib_] - cinv[ia_]) <= (1 - COVERAGE) * np.maximum(ib_ - ia_, 1)
    return np.where(ok, (c[ib_ - 1] / o[ia_] - 1) * 100, np.nan)


def coin_tables(S: dict | None, op: dict, macro_t: np.ndarray | None = None) -> dict:
    """Tabla de eventos (apertura × esta moneda): características antes de decidir, entradas posibles (KEYS × largo y
    corto) con lo que se sabía al decidirlas, y primer paso de cada entrada única."""
    no = len(op["open_ms"])
    shape = (no, NK, 2)
    empty = {"ok": np.zeros(no, bool), "F": {}, "slot_u": np.full(shape, -1, np.int32),
             "slot_t": np.full(shape, NONE_T, np.int64), "slot_dec": np.full(shape, NONE_T, np.int64),
             "slot_mom": np.full(shape, np.nan, np.float32), "slot_vdev": np.full(shape, np.nan, np.float32),
             "slot_rvol": np.full(shape, np.nan, np.float32), "U": None, "daily": {}, "span": [None, None, 0, 0.0]}
    if S is None or S["n"] < PRE_BARS + POST_BARS + 1500:
        return empty
    t0, n = S["t0"], S["n"]
    o, h, l, c, v, qv, valid = (S[k] for k in ("o", "h", "l", "c", "v", "qv", "valid"))
    cv = np.concatenate(([0.0], np.cumsum(v)))
    cqv = np.concatenate(([0.0], np.cumsum(qv)))
    cinv = np.concatenate(([0], np.cumsum(~valid)))
    om = op["open_ms"]
    i0 = (om - t0) // MIN
    ok = (i0 - PRE_BARS >= 0) & (i0 + POST_BARS < n) & (om % MIN == 0)
    ii = np.clip(i0, PRE_BARS, n - POST_BARS - 1)
    bad = cinv[ii + POST_BARS] - cinv[ii - PRE_BARS]
    ok &= (bad <= (1 - COVERAGE) * (PRE_BARS + POST_BARS)) & valid[ii]
    po = o[ii]
    F: dict[str, np.ndarray] = {}
    for N in PRE_N:
        F[f"pre{N}"] = (c[ii - 1] / c[ii - 1 - N] - 1) * 100
    for N in TIMES:
        F[f"mom{N}"] = (c[ii + N - 1] / po - 1) * 100
    F["react_prev"] = _react(S, cinv, op["prev_ms"], np.minimum(op["prev_ms"] + 60 * MIN, om))
    F["react_prev2"] = _react(S, cinv, op["prev2_ms"], np.minimum(op["prev2_ms"] + 60 * MIN, om))
    F["desde_prev"] = _react(S, cinv, op["prev_ms"], om)
    F["react_same"] = _react(S, cinv, op["prev_same_ms"], op["prev_same_ms"] + 60 * MIN)
    # Régimen de volatilidad: ATR de 14 h (en %) de la última hora cerrada, en percentil de los 30 días previos.
    hid = (t0 + np.arange(n, dtype=np.int64) * MIN) // H
    st = np.flatnonzero(np.concatenate(([True], hid[1:] != hid[:-1])))
    en_ = np.concatenate((st[1:], [n]))
    hh, hl, hc = np.maximum.reduceat(h, st), np.minimum.reduceat(l, st), c[en_ - 1]
    ht = hid[st] * H
    pc = np.concatenate(([hc[0]], hc[:-1]))
    tr = np.maximum(hh - hl, np.maximum(np.abs(hh - pc), np.abs(hl - pc))) / pc
    cs = np.concatenate(([0.0], np.cumsum(tr)))
    atr_h = np.full(len(tr), np.nan)
    atr_h[ATR_N - 1:] = (cs[ATR_N:] - cs[:-ATR_N]) / ATR_N
    kk = np.searchsorted(ht, om - H, side="right") - 1
    pct = np.full(no, np.nan)
    for i in np.flatnonzero(ok & (kk >= ATR_N - 1 + ATR_LOOK_H)):
        w = atr_h[kk[i] - ATR_LOOK_H:kk[i]]
        pct[i] = float((w < atr_h[kk[i]]).mean())
    F["atr_pct"] = pct
    F["atr_h"] = np.where(kk >= ATR_N - 1, atr_h[np.clip(kk, 0, len(atr_h) - 1)] * 100, np.nan)
    # Techo (mira al FUTURO a propósito, solo para el informe): cuánto se movió la moneda en las 24 h siguientes.
    up24, dn24 = np.full(no, np.nan), np.full(no, np.nan)
    ar = np.arange(1440)
    for a in range(0, no, 500):
        sl_ = slice(a, a + 500)
        ix = np.minimum(ii[sl_, None] + ar, n - 1)
        up24[sl_] = (h[ix].max(1) / po[sl_] - 1) * 100
        dn24[sl_] = (1 - l[ix].min(1) / po[sl_]) * 100
    F["fut_up24"], F["fut_dn24"] = up24, dn24   # FUTURO: solo para el techo del informe, nunca como señal
    # Dato macro: el último publicado entre 4 h antes y 1 h después de la apertura; reacción de sus 15 primeros min.
    F["macro_known"], F["macro_ret"] = np.full(no, np.nan), np.full(no, np.nan)
    if macro_t is not None and len(macro_t):
        pos = np.searchsorted(macro_t, om + MACRO_AFTER_MIN * MIN, side="right") - 1
        tm = np.where(pos >= 0, macro_t[np.clip(pos, 0, None)], -1)
        good = ok & (pos >= 0) & (tm >= om - MACRO_BEFORE_MIN * MIN)
        it = np.clip((tm - t0) // MIN, 0, n - MACRO_REACT_MIN - 1)
        good &= ((tm - t0) // MIN >= 0) & ((tm - t0) // MIN < n - MACRO_REACT_MIN)
        F["macro_known"] = np.where(good, t0 + (it + MACRO_REACT_MIN) * MIN, np.nan)
        F["macro_ret"] = np.where(good, (c[it + MACRO_REACT_MIN - 1] / o[it] - 1) * 100, np.nan)
    for k in list(F):
        F[k] = np.where(ok, F[k], np.nan)

    # Entradas posibles: vela de entrada, precio, si es límite y vela de decisión.
    sj = np.full(shape, -1, np.int64)
    sp = np.full(shape, np.nan)
    slim = np.zeros(shape, bool)
    sjd = np.full(shape, -1, np.int64)
    rows = np.arange(no)

    def put(key, di, has, j, price, jd, lim=False):
        k = KEY_IDX[key]
        has = has & ok & (j < n - 1)
        sj[has, k, di] = j[has]
        sp[has, k, di] = price[has]
        sjd[has, k, di] = jd[has]
        slim[has, k, di] = lim

    for R in RANGES:
        ix = ii[:, None] + np.arange(R)
        hi, lo = h[ix].max(1), l[ix].min(1)
        mg = np.maximum(po * BREAK_BPS / 1e4, BREAK_FRAC * (hi - lo))
        for tf in TFS:
            if tf == 1:
                cidx = ii[:, None] + R + np.arange(BRK_END_MIN - R)
            else:
                cidx = ii[:, None] + R + 5 * np.arange((BRK_END_MIN - R) // 5) + 4
            cc = c[cidx]
            for di, dsg in ((0, 1), (1, -1)):
                hit = cc > (hi + mg)[:, None] if dsg > 0 else cc < (lo - mg)[:, None]
                has = hit.any(1)
                j = cidx[rows, hit.argmax(1)] + 1           # entrada: apertura de la vela siguiente
                put(("brk", R, tf), di, has, j, o[np.minimum(j, n - 1)], j)
                edge = hi if dsg > 0 else lo                  # retesteo: orden límite en el borde del rango
                rix = np.minimum(j[:, None] + np.arange(RETEST_MIN), n - 1)
                touch = (l[rix] <= edge[:, None]) if dsg > 0 else (h[rix] >= edge[:, None])
                jr = rix[rows, touch.argmax(1)]
                put(("ret", R, tf), di, has & touch.any(1), jr, edge, j, lim=True)
    for N in TIMES:
        j = ii + N
        for di in (0, 1):
            put(("t", N), di, np.ones(no, bool), j, o[j], j)
    for N in IMP_N:
        rel = (c[ii[:, None] + np.arange(N)] / po[:, None] - 1) * 100
        for X in IMP_X:
            for di, dsg in ((0, 1), (1, -1)):
                hit = rel >= X if dsg > 0 else rel <= -X
                j = ii + hit.argmax(1) + 1
                put(("imp", X, N), di, hit.any(1), j, o[np.minimum(j, n - 1)], j)
    for N in PB_N:
        seg = ii[:, None] + np.arange(N)
        ext_hi, ext_lo = h[seg].max(1), l[seg].min(1)
        mN = c[ii + N - 1] / po - 1
        widx = ii[:, None] + N + np.arange(PB_END_MIN - N)
        W = widx.shape[1]
        for di, dsg in ((0, 1), (1, -1)):
            ext = ext_hi if dsg > 0 else ext_lo
            lvl = ext - PB_FRAC * (ext - po)
            pulled = (l[widx] <= lvl[:, None]) if dsg > 0 else (h[widx] >= lvl[:, None])
            p_at = np.where(pulled.any(1), pulled.argmax(1), W)
            res = ((c[widx] > ext[:, None]) if dsg > 0 else (c[widx] < ext[:, None])) & \
                (np.arange(W)[None, :] > p_at[:, None])
            j = widx[rows, res.argmax(1)] + 1
            put(("pb", N), di, (dsg * mN > 0) & res.any(1), j, o[np.minimum(j, n - 1)], j)

    # Lo que se sabía al decidir cada entrada (velas cerradas antes de la vela de decisión).
    has = sj >= 0
    jd = np.clip(np.where(has, sjd, ii[:, None, None]), 1, n)
    iiB = np.broadcast_to(ii[:, None, None], shape)
    mom = (c[jd - 1] / po[:, None, None] - 1) * 100
    vol_now = cv[jd] - cv[iiB]
    with np.errstate(all="ignore"):
        vw = (cqv[jd] - cqv[iiB]) / vol_now
        vdev = (c[jd - 1] / vw - 1) * 100
    ps = op["prev_sess"]
    ip = (ps - t0) // MIN
    pvalid = (ps > 0) & (ip >= 0) & (ip < n)
    off = jd - iiB
    vols = np.full((RVOL_SESS,) + shape, np.nan)
    for s_ in range(RVOL_SESS):
        ipk = np.broadcast_to(ip[:, s_, None, None], shape)
        e_ = ipk + off
        okk = np.broadcast_to(pvalid[:, s_, None, None], shape) & (e_ <= n)
        vk = cv[np.clip(e_, 0, n)] - cv[np.clip(ipk, 0, n)]
        vols[s_] = np.where(okk & (vk > 0), vk, np.nan)
    with np.errstate(all="ignore"):
        cnt = np.isfinite(vols).sum(0)
        med = np.where(cnt >= 2, np.nanmedian(np.where(cnt >= 2, vols, 0.0), axis=0), np.nan)
        rvol = vol_now / med
    slot_dec = np.where(has, t0 + sjd * MIN, NONE_T)
    slot_t = np.where(has, t0 + sj * MIN, NONE_T)

    # Entradas únicas y su primer paso.
    dirs = np.broadcast_to(np.array([1, -1])[None, None, :], shape)
    nxt = np.broadcast_to(op["next_same_ms"][:, None, None], shape)
    keyarr = np.column_stack([sj[has], dirs[has], slim[has], sp[has], nxt[has]]).astype(float)
    slot_u = np.full(shape, -1, np.int32)
    U = None
    if len(keyarr):
        uq, inv = np.unique(keyarr, axis=0, return_inverse=True)
        slot_u[has] = np.asarray(inv).reshape(-1)
        U = passages(S, uq[:, 0].astype(np.int64), uq[:, 1].astype(np.int64), uq[:, 3], uq[:, 2].astype(bool),
                     uq[:, 4].astype(np.int64))
    # Cierres diarios (día UTC completo), para la tendencia de BTC.
    dday = hid // 24
    ends = np.flatnonzero(np.concatenate((dday[1:] != dday[:-1], [True])))
    if len(ends) and (t0 + ends[-1] * MIN) % DAY != DAY - MIN:
        ends = ends[:-1]
    daily = {int(dday[e]): float(c[e]) for e in ends}
    return {"ok": ok, "F": F, "slot_u": slot_u, "slot_t": slot_t, "slot_dec": slot_dec,
            "slot_mom": np.where(has, mom, np.nan).astype(np.float32),
            "slot_vdev": np.where(has, vdev, np.nan).astype(np.float32),
            "slot_rvol": np.where(has, rvol, np.nan).astype(np.float32), "U": U, "daily": daily,
            "span": [t0, t0 + (n - 1) * MIN, int(valid.sum()), float(valid.mean())]}


def coin_job(args: tuple) -> dict:
    """Trabajo de un proceso: carga las velas de una moneda y calcula su tabla de eventos."""
    coin, paths, op, macro_t = args
    t = time.time()
    S = load_dense(paths)
    out = coin_tables(S, op, macro_t)
    out["coin"] = coin
    out["secs"] = round(time.time() - t, 1)
    return out


# ---------------------------------------------------------------- laboratorio (todas las monedas)

def _sgn(x: np.ndarray, thr: float = 0.0) -> np.ndarray:
    x = np.nan_to_num(np.asarray(x, float), nan=0.0)
    if thr > 0:
        return np.where(x >= thr, 1, np.where(x <= -thr, -1, 0)).astype(np.int8)
    return np.sign(x).astype(np.int8)


class Lab:
    """Eventos (apertura × moneda, e = apertura·n_monedas + moneda) con sus características, entradas y primer paso."""

    def __init__(self, meta: dict, feats: dict[str, np.ndarray], tables: dict[str, dict], split_ms: int,
                 coin_feats: dict[str, dict[str, np.ndarray]] | None = None, coins: list[str] | None = None,
                 stop_extra: dict[str, float] | None = None):
        self.coins = list(coins or COINS)
        nc = self.nc = len(self.coins)
        no = self.no = len(meta["open_ms"])
        self.NE = no * nc
        rep = lambda x: np.repeat(np.asarray(x), nc)
        self.open_ms = rep(meta["open_ms"]).astype(np.int64)
        self.wid = rep(meta["wid"])
        self.coin = np.tile(np.arange(nc), no)
        self.weekday = rep(meta["weekday"])
        self.mday = meta["mday"]
        days = sorted(set(self.mday))
        self.days = days
        dpos = {d: k for k, d in enumerate(days)}
        self.day = rep([dpos[d] for d in self.mday]).astype(np.int64)
        self.year = rep([int(d[:4]) for d in self.mday])
        months = sorted({d[:7] for d in self.mday})
        mpos = {m: k for k, m in enumerate(months)}
        self.months = months
        self.month = rep([mpos[d[:7]] for d in self.mday])
        qk = lambda d: f"{d[:4]}-T{(int(d[5:7]) - 1) // 3 + 1}"
        self.quarters = sorted({qk(d) for d in self.mday})
        qpos = {q: k for k, q in enumerate(self.quarters)}
        self.quarter = rep([qpos[qk(d)] for d in self.mday])
        self.week = rep([(date.fromisoformat(d).toordinal() - 1) // 7 for d in self.mday]).astype(np.int64)
        self.split_ms = int(split_ms)
        self.split_day = datetime.fromtimestamp(self.split_ms / 1000, MAD).date().isoformat()
        self.split_idx = sum(1 for d in days if d < self.split_day)
        self.oos = self.open_ms >= self.split_ms
        self.funding: dict[int, np.ndarray] = {}
        self.F = {k: rep(v).astype(float) for k, v in feats.items()}
        empty = coin_tables(None, {"open_ms": meta["open_ms"]})
        tabs = [tables.get(c) or empty for c in self.coins]
        fkeys = sorted({k for t in tabs for k in t["F"]})
        for k in fkeys:
            self.F[k] = np.stack([t["F"].get(k, np.full(no, np.nan)) for t in tabs], axis=1).reshape(-1)
        for c_i, c in enumerate(self.coins):
            for k, x in ((coin_feats or {}).get(c) or {}).items():
                self.F.setdefault(k, np.full(self.NE, np.nan))[c_i::nc] = x
        self.valid = np.stack([t["ok"] for t in tabs], axis=1).reshape(-1)
        st = lambda k: np.stack([t[k] for t in tabs], axis=1).reshape((self.NE, NK, 2))
        self.slot_t, self.slot_dec = st("slot_t"), st("slot_dec")
        self.slot_mom, self.slot_vdev, self.slot_rvol = st("slot_mom"), st("slot_vdev"), st("slot_rvol")
        us, off = [], 0
        slot_u = []
        sx = {c: (STOP_EXTRA_ALT if c in ALT_COINS else STOP_EXTRA) for c in self.coins}
        sx.update(stop_extra or {})
        self.stop_extra = sx
        for c_i, t in enumerate(tabs):
            su = t["slot_u"].astype(np.int64)
            slot_u.append(np.where(su >= 0, su + off, -1))
            if t["U"] is not None:
                u = dict(t["U"])
                u["sx"] = np.full(len(u["d"]), sx[self.coins[c_i]], np.float32)   # extra de stop de esta moneda
                u["c"] = np.full(len(u["d"]), c_i, np.int8)
                if "fav_t" not in u:
                    u["fav_t"] = u["fav"]
                us.append(u)
                off += len(t["U"]["d"])
        self.slot_u = np.stack(slot_u, axis=1).reshape((self.NE, NK, 2))
        self.U = {k: np.concatenate([u[k] for u in us]) for k in us[0]} if us else None
        self.win_mask = {w: np.isin(self.wid, [WIN_IDS.index(x) for x in ws]) for w, ws in WIN_SETS.items()}
        self.coin_mask = {k: np.isin(self.coin, [self.coins.index(c) for c in cs if c in self.coins])
                          for k, cs in COIN_SETS.items()}
        self._src: dict = {}
        self._base: dict = {}
        t_all = meta["open_ms"]
        self.t_from, self.t_to = (int(t_all.min()), int(t_all.max()) + DAY) if len(t_all) else (0, 0)

    # -- dirección (fuente) por evento: +1, −1 o 0 (sin señal); solo con lo conocido al decidir
    def src_dir(self, src: tuple) -> np.ndarray:
        if src in self._src:
            return self._src[src]
        F, kind, p = self.F, src[0], src[1:]
        nan = lambda k: F.get(k, np.full(self.NE, np.nan))
        if kind == "cad_ult":
            d = _sgn(nan("c_ult"), p[0])
        elif kind == "cad_may":
            s = np.nan_to_num(nan("c_sum24"))
            d = np.where(s >= p[0], 1, np.where(s <= -p[0], -1, 0))
        elif kind == "cad_una":
            s, nn = np.nan_to_num(nan("c_sum24")), np.nan_to_num(nan("c_n24"))
            d = np.where((nn >= p[0]) & (np.abs(s) == nn), np.sign(s), 0)
        elif kind == "cad_med":
            d = _sgn(nan("c_mean24"), p[0])
        elif kind == "cad_ny":
            d = _sgn(nan("c_ny"), p[0])
        elif kind == "cad_asia":
            d = _sgn(nan("c_asia"), p[0])
        elif kind == "cad_eu":
            d = _sgn(nan("c_eu"), p[0])
        elif kind == "bol_ayer":
            d = _sgn(nan("own_prev"), p[0])
        elif kind == "bol_racha":
            s = np.nan_to_num(nan("own_streak"))
            d = np.where(np.abs(s) >= p[0], np.sign(s), 0)
        elif kind == "ap_ayer":
            d = _sgn(nan("react_same"), p[0])
        elif kind == "ven_rea":
            d = _sgn(nan("react_prev"), p[0])
        elif kind == "ven_des":
            d = _sgn(nan("desde_prev"), p[0])
        elif kind == "ven_dos":
            a, b = _sgn(nan("react_prev"), p[0]), _sgn(nan("react_prev2"), p[0])
            d = np.where(a == b, a, 0)
        elif kind == "previo":
            d = _sgn(nan(f"pre{p[0]}"), p[1])
        elif kind in ("ruptura", "impulso"):
            k = KEY_IDX[("brk", p[0], p[1]) if kind == "ruptura" else ("imp", p[0], p[1])]
            up, dn = self.slot_dec[:, k, 0], self.slot_dec[:, k, 1]
            d = np.where(up < dn, 1, np.where(dn < up, -1, 0))
        elif kind == "ap_mom":
            d = _sgn(nan(f"mom{p[0]}"), p[1])
        elif kind == "consenso":
            v = (_sgn(nan("c_ult")).astype(int) + _sgn(nan("react_prev")) + _sgn(nan("pre60")) + _sgn(nan("own_prev")))
            d = np.where(v >= p[0], 1, np.where(v <= -p[0], -1, 0))
        elif kind == "consenso2":
            a, b = _sgn(nan("c_ult"), p[0]), _sgn(nan("react_prev"), p[0])
            d = np.where(a == b, a, 0)
        elif kind == "contra_cad":
            d = -_sgn(nan("c_ult"), p[0])
        elif kind == "contra_ven":
            d = -_sgn(nan("react_prev"), p[0])
        elif kind == "contra_previo":
            d = -_sgn(nan(f"pre{p[0]}"), p[1])
        elif kind == "contra_ap":
            d = -_sgn(nan(f"mom{p[0]}"), p[1])
        elif kind == "btc":
            c_, a20, a50 = nan("btc_c"), nan("btc_ma20"), nan("btc_ma50")
            with np.errstate(invalid="ignore"):
                if p[0] == "ma20":
                    d = _sgn(c_ - a20)
                elif p[0] == "ma50":
                    d = _sgn(c_ - a50)
                else:
                    d = np.where((c_ > a20) & (c_ > a50), 1, np.where((c_ < a20) & (c_ < a50), -1, 0))
        elif kind == "fng_contra":
            f = nan("fng")
            with np.errstate(invalid="ignore"):
                d = np.where(f <= p[0], 1, np.where(f >= 100 - p[0], -1, 0))
        elif kind == "fng_cambio":
            d = _sgn(nan(f"fng_d{p[0]}"), p[1])
        elif kind == "fng_nivel":
            f = nan("fng")
            with np.errstate(invalid="ignore"):
                d = np.where(f >= 50 + p[0], 1, np.where(f <= 50 - p[0], -1, 0))
        elif kind == "fund_contra":
            f = nan("fund_pct")
            with np.errstate(invalid="ignore"):
                d = np.where(f >= p[0], -1, np.where(f <= 1 - p[0], 1, 0))
        elif kind == "macro_rea":
            d = _sgn(nan("macro_ret"), p[0])
        elif kind == "macro_contra":
            d = -_sgn(nan("macro_ret"), p[0])
        elif kind == "gdelt":
            d = _sgn(nan("gd_z"), p[0])
        elif kind == "gdelt_contra":
            d = -_sgn(nan("gd_z"), p[0])
        else:
            raise ValueError(f"fuente desconocida {src}")
        d = np.asarray(d, np.int8)
        self._src[src] = d
        return d

    def base_mask(self, st: dict) -> np.ndarray:
        """Filtros que no dependen de la dirección: datos, ventanas, monedas, volatilidad y día."""
        key = (st["win"], st["coins"], st["vol"], st["dia"])
        if key in self._base:
            return self._base[key]
        m = self.valid & self.win_mask[st["win"]] & self.coin_mask[st["coins"]]
        ap = self.F.get("atr_pct", np.full(self.NE, np.nan))
        with np.errstate(invalid="ignore"):
            if st["vol"] == "no_baja":
                m &= ap >= 1 / 3
            elif st["vol"] == "alta":
                m &= ap >= 2 / 3
            elif st["vol"] == "baja":
                m &= ap < 1 / 3
        if st["dia"] == "mar-jue":
            m &= np.isin(self.weekday, (1, 2, 3))
        self._base[key] = m
        return m

    def candidates(self, st: dict, e: np.ndarray, dd: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Entrada de cada evento con su dirección, tras el filtro de BTC, la confirmación y el instante conocido de la
        fuente. Devuelve (eventos, direcciones, entrada única)."""
        if st["btc"] != "ninguno" and len(e):
            c_, a20, a50 = (self.F.get(k, np.full(self.NE, np.nan))[e] for k in ("btc_c", "btc_ma20", "btc_ma50"))
            with np.errstate(invalid="ignore"):
                tr = np.where((c_ > a20) & (c_ > a50), 1, np.where((c_ < a20) & (c_ < a50), -1, 0))
            keep = (dd == tr) if st["btc"] == "a_favor" else (dd * tr >= 0)
            e, dd = e[keep], dd[keep]
        ki = KEY_IDX[st["entry"]]
        di = (dd < 0).astype(np.int64)
        u = self.slot_u[e, ki, di]
        ok = u >= 0
        conf = st["conf"]
        if conf[0] == "momento":
            mo = dd * self.slot_mom[e, ki, di]
            ok &= (mo > 0) if conf[1] <= 0 else (mo >= conf[1])
        elif conf[0] == "vwap":
            ok &= dd * self.slot_vdev[e, ki, di] > 0
        elif conf[0] == "rvol":
            ok &= self.slot_rvol[e, ki, di] >= conf[1]
        elif conf[0] == "ruptura":
            kb = KEY_IDX[("brk", conf[1], conf[2])]
            ok &= self.slot_dec[e, kb, di] <= self.slot_dec[e, ki, di]
        if st["src"][0] in ("macro_rea", "macro_contra"):   # el dato y su reacción, conocidos antes de decidir
            ok &= self.slot_dec[e, ki, di] >= np.nan_to_num(self.F["macro_known"][e], nan=np.inf)
        return e[ok], dd[ok], u[ok]

    def resolve(self, u: np.ndarray, sl: float, r: float, dur: str, touch: bool = False) -> dict:
        """Horquilla pura (SL, TP = R × SL, duración) de cada entrada única, con costes y supuesto pesimista. El objetivo
        exige superar el nivel en TP_THROUGH (con touch, basta tocarlo: solo para la sensibilidad)."""
        U = self.U
        si, ti, di = ADV_LV.index(sl), FAV_LV.index(round(sl * r, 4)), DURS.index(dur)
        tsl = U["adv"][u, si].astype(np.int64)
        ttp = U["fav_t" if touch and "fav_t" in U else "fav"][u, ti].astype(np.int64)
        D = U["dl_len"][u, di].astype(np.int64)
        slh = (tsl < D) & (tsl <= ttp)                 # empate en la misma vela: cuenta el stop
        tph = (ttp < D) & (ttp < tsl)
        g = np.where(slh, U["adv_g"][u, si], np.where(tph, sl * r / 100, U["dl_g"][u, di])).astype(float)
        off = np.where(slh, tsl, np.where(tph, ttp, D - 1))
        d = U["d"][u].astype(float)
        sx = U["sx"][u].astype(float) if "sx" in U else STOP_EXTRA
        ratio = (1 + d * g) * (1 - d * (SLIP + sx * slh))
        ret = (d * (ratio - 1) - FEE * (1 + ratio)) * 100
        why = np.where(slh, 0, np.where(tph, 1, np.where(U["dl_end"][u, di], 3, 2))).astype(np.int8)
        return {"ret": ret, "R": ret / sl, "why": why, "ent": U["t"][u], "ext": U["t"][u] + (off + 1) * MIN,
                "ok": off < U["gap"][u], "px": U["px"][u]}

    def run(self, st: dict, e: np.ndarray, dd: np.ndarray, touch: bool = False) -> dict:
        """Operaciones de una estrategia desde (eventos, direcciones): entrada, horquilla y una posición por moneda. Las
        que siguen abiertas al acabarse los datos (why = 3) ocupan su moneda pero no cuentan como resultado."""
        e, dd, u = self.candidates(st, e, dd)
        if not len(e) or self.U is None:
            return _empty_trades()
        res = self.resolve(u, st["sl"], st["r"], st["dur"], touch)
        ok = res["ok"]
        e, dd = e[ok], dd[ok]
        res = {k: v[ok] for k, v in res.items()}
        keep = no_overlap(self.coin[e], res["ent"], res["ext"], e)
        keep &= res["why"] != 3
        out = {k: v[keep] for k, v in res.items()}
        out["e"], out["dir"] = e[keep], dd[keep]
        order = np.lexsort((out["e"], out["ent"]))
        return {k: v[order] for k, v in out.items()}

    def events(self, st: dict, oos_only: bool = False, side: int = 0) -> tuple[np.ndarray, np.ndarray]:
        """Eventos que mira la estrategia (todos sus filtros que no dependen de la dirección y con señal) y su
        dirección; side = 1 se queda solo con los largos (contado)."""
        d = self.src_dir(st["src"])
        m = self.base_mask(st) & (d != 0)
        if oos_only:
            m = m & self.oos
        if side:
            m = m & (d == side)
        e = np.flatnonzero(m)
        return e, d[e]

    def trades(self, st: dict, oos_only: bool = False, side: int = 0, touch: bool = False) -> dict:
        e, dd = self.events(st, oos_only, side)
        return self.run(st, e, dd, touch)

    def day_range(self, part: str | None) -> tuple[int, int] | None:
        """Índices [desde, hasta) de los días de aperturas de la muestra («is»), fuera («oos») o todo («all»)."""
        if part == "is":
            return 0, self.split_idx
        if part == "oos":
            return self.split_idx, len(self.days)
        if part == "all":
            return 0, len(self.days)
        return None


def _empty_trades() -> dict:
    z = np.zeros(0)
    return {"ret": z, "R": z, "why": z.astype(np.int8), "ent": z.astype(np.int64), "ext": z.astype(np.int64),
            "ok": z.astype(bool), "px": z, "e": z.astype(np.int64), "dir": z.astype(np.int8)}


def no_overlap(coin: np.ndarray, ent: np.ndarray, ext: np.ndarray, prio: np.ndarray) -> np.ndarray:
    """Una posición por moneda a la vez: por orden de entrada, se toma una operación si la anterior de su moneda ya
    salió (una operación que entra justo al salir la otra sí entra)."""
    keep = np.zeros(len(ent), bool)
    for ci in np.unique(coin):
        ix = np.flatnonzero(coin == ci)
        o_ = ix[np.lexsort((prio[ix], ent[ix]))]
        nxt = np.searchsorted(ent[o_], ext[o_], side="left").tolist()
        o_l = o_.tolist()
        i, m = 0, len(o_l)
        while i < m:
            keep[o_l[i]] = True
            i = max(nxt[i], i + 1)
    return keep


# ---------------------------------------------------------------- métricas

def daily_series(lab: Lab, tr: dict, rng_days: tuple[int, int] | None) -> np.ndarray:
    """Suma de R por día de aperturas (día de entrada), con ceros en los días sin operaciones, en [desde, hasta)."""
    day = lab.day[tr["e"]]
    if rng_days is None:
        if not len(day):
            return np.zeros(0)
        rng_days = (int(day.min()), int(day.max()) + 1)
    a, b = rng_days
    inside = (day >= a) & (day < b)
    return np.bincount(day[inside] - a, weights=tr["R"][inside], minlength=max(b - a, 0))


def metrics(lab: Lab, tr: dict, sel: np.ndarray | None = None, span_ms: float | None = None,
            part: str | None = None) -> dict:
    """Métricas de un conjunto de operaciones (R neto = resultado neto / stop). El t y el p van con la serie diaria
    (todas las monedas juntas, ceros en los días sin operaciones) y error típico HAC; t_day_iid es el de antes."""
    if sel is not None:
        tr = {k: v[sel] for k, v in tr.items()}
    n = len(tr["R"])
    if not n:
        return {"trades": 0}
    R, ret, why = tr["R"], tr["ret"], tr["why"]
    e = tr["e"]
    day = lab.day[e]
    dsum = np.bincount(day, weights=R)
    dsum = dsum[np.bincount(day) > 0]
    k = len(dsum)
    md, sd = float(dsum.mean()), float(dsum.std(ddof=1)) if k > 1 else 0.0
    t_iid = md / (sd / math.sqrt(k)) if sd > 0 else 0.0
    ser = daily_series(lab, tr, lab.day_range(part))
    t_day = hac_t(ser) if len(ser) > 2 else t_iid
    sd_t = float(R.std(ddof=1)) if n > 1 else 0.0
    t_tr = float(R.mean()) / (sd_t / math.sqrt(n)) if sd_t > 0 else 0.0
    gain, loss = float(ret[ret > 0].sum()), float(-ret[ret < 0].sum())
    order = np.argsort(tr["ext"], kind="stable")
    cum = np.cumsum(R[order])
    dd = float(np.max(np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:] - cum)) if n else 0.0
    mon = lab.month[e]
    msum = np.bincount(mon, weights=R)
    mcnt = np.bincount(mon)
    mm = msum[mcnt > 0]
    best = int(np.argmax(np.where(mcnt > 0, msum, -np.inf)))
    wo = mon != best
    years = (span_ms or (lab.t_to - lab.t_from)) / YEAR_MS
    tim = why >= 2
    _, _, skew, kurt = moments(dsum)
    sr = md / float(dsum.std()) if k > 1 and dsum.std() > 0 else 0.0
    r4 = lambda x: round(float(x), 4)
    tot = float(R.sum())
    return {"trades": n, "win_rate": r4((ret > 0).mean()), "mean_R": r4(R.mean()), "median_R": r4(np.median(R)),
            "mean_ret_pct": r4(ret.mean()), "total_R": round(tot, 2),
            "profit_factor": round(gain / loss, 3) if loss > 0 else None,
            "t": r4(t_tr), "t_day": r4(t_day), "t_day_iid": r4(t_iid),
            "p_day": r4(t_sf(t_day, k - 1)) if k > 1 else 1.0, "days": k,
            "weeks": int(len(np.unique(lab.week[e]))),
            "max_dd_R": round(dd, 2), "R_per_year": round(tot / years, 2) if years > 0 else None,
            "months": int(len(mm)), "months_pos": r4((mm > 0).mean()) if len(mm) else None,
            "best_month": lab.months[best] if best < len(lab.months) else None,
            "best_month_R": round(float(msum[best]), 2),
            "best_month_share": r4(msum[best] / tot) if tot > 0 else None,
            "mean_R_wo_best_month": r4(R[wo].mean()) if wo.any() else None,
            "mean_hours": round(float(((tr["ext"] - tr["ent"]) / H).mean()), 2),
            "tp_share": r4((why == 1).mean()), "stop_share": r4((why == 0).mean()), "time_share": r4(tim.mean()),
            "time_mean_R": r4(R[tim].mean()) if tim.any() else None,
            "share_ret_ge_3": r4((ret >= 3).mean()), "longs": int((tr["dir"] > 0).sum()),
            "long_share": r4((tr["dir"] > 0).mean()),
            "sr_day": r4(sr), "skew_day": r4(skew), "kurt_day": r4(kurt)}


def by_year(lab: Lab, tr: dict) -> dict:
    """R por año natural (todas las operaciones) y sus partes dentro y fuera de muestra."""
    out = {}
    if not len(tr["R"]):
        return out
    yr = lab.year[tr["e"]]
    oos = lab.oos[tr["e"]]
    for y in np.unique(yr):
        s = yr == y
        so, si = s & oos, s & ~oos
        R = tr["R"][s]
        out[str(int(y))] = {"n": int(s.sum()), "mean_R": round(float(R.mean()), 4), "sum_R": round(float(R.sum()), 2),
                            "win_rate": round(float((tr["ret"][s] > 0).mean()), 4),
                            "is_n": int(si.sum()),
                            "is_mean_R": round(float(tr["R"][si].mean()), 4) if si.any() else None,
                            "oos_n": int(so.sum()),
                            "oos_mean_R": round(float(tr["R"][so].mean()), 4) if so.any() else None}
    return out


def by_quarter(lab: Lab, tr: dict) -> dict:
    """Operaciones y suma de R por trimestre natural («2024-T1»), para la selección trimestral en avance."""
    if not len(tr["R"]):
        return {}
    q = lab.quarter[tr["e"]]
    n = np.bincount(q, minlength=len(lab.quarters))
    s = np.bincount(q, weights=tr["R"], minlength=len(lab.quarters))
    return {lab.quarters[k]: {"n": int(n[k]), "sum_R": round(float(s[k]), 3)} for k in np.flatnonzero(n)}


# ---------------------------------------------------------------- gramática de estrategias

FAMILIES = {
    # familia: (peso, texto, fuente de datos que necesita o None)
    "cadena": (400, "Bolsas previas en cadena", None),
    "misma_bolsa": (170, "Misma bolsa el día anterior", None),
    "ventana_anterior": (260, "Apertura anterior (la siguiente suele coincidir)", None),
    "momento_previo": (140, "Momento cripto antes de la apertura", None),
    "ruptura": (240, "Dirección de la ruptura del rango", None),
    "momento_apertura": (150, "Movimiento de los primeros minutos", None),
    "impulso": (110, "Impulso inicial", None),
    "consenso": (110, "Consenso de varias señales", None),
    "contra": (100, "A la contra (prueba de reversión)", None),
    "tendencia_btc": (60, "Tendencia diaria de BTC", None),
    "miedo_codicia": (70, "Miedo y codicia", "fng"),
    "funding": (50, "Funding extremo", "funding"),
    "macro": (90, "Reacción a un dato macro", "macro"),
    "gdelt": (50, "Tono de las noticias (GDELT)", "gdelt"),
}
SOURCES = {
    "cadena": [("cad_ult", 0.0), ("cad_ult", 0.3), ("cad_ult", 0.7), ("cad_may", 2), ("cad_may", 3), ("cad_una", 2),
               ("cad_una", 3), ("cad_med", 0.3), ("cad_med", 0.6), ("cad_ny", 0.0), ("cad_ny", 0.5), ("cad_ny", 1.0),
               ("cad_asia", 0.0), ("cad_asia", 0.5), ("cad_eu", 0.0), ("cad_eu", 0.5)],
    "misma_bolsa": [("bol_ayer", 0.0), ("bol_ayer", 0.5), ("bol_ayer", 1.0), ("bol_racha", 2), ("bol_racha", 3),
                    ("ap_ayer", 0.0), ("ap_ayer", 0.5)],
    "ventana_anterior": [("ven_rea", 0.0), ("ven_rea", 0.3), ("ven_rea", 0.6), ("ven_des", 0.0), ("ven_des", 0.5),
                         ("ven_des", 1.0), ("ven_dos", 0.0), ("ven_dos", 0.3)],
    "momento_previo": [("previo", 15, 0.0), ("previo", 15, 0.2), ("previo", 60, 0.0), ("previo", 60, 0.3),
                       ("previo", 60, 0.6), ("previo", 240, 0.0), ("previo", 240, 0.6), ("previo", 240, 1.2)],
    "ruptura": [("ruptura", R, tf) for R in RANGES for tf in TFS],
    "momento_apertura": [("ap_mom", N, thr) for N in TIMES for thr in (0.2, 0.5, 1.0)],
    "impulso": [("impulso", X, N) for X in IMP_X for N in IMP_N],
    "consenso": [("consenso", 2), ("consenso", 3), ("consenso", 4), ("consenso2", 0.0), ("consenso2", 0.3)],
    "contra": [("contra_cad", 0.0), ("contra_cad", 0.7), ("contra_ven", 0.3), ("contra_ven", 0.6),
               ("contra_previo", 240, 0.6), ("contra_previo", 240, 1.2), ("contra_ap", 15, 0.5),
               ("contra_ap", 15, 1.0), ("contra_ap", 30, 0.5), ("contra_ap", 30, 1.0)],
    "tendencia_btc": [("btc", "ambas"), ("btc", "ma20"), ("btc", "ma50")],
    "miedo_codicia": [("fng_contra", 25), ("fng_contra", 20), ("fng_cambio", 1, 5), ("fng_cambio", 3, 5),
                      ("fng_cambio", 7, 10), ("fng_nivel", 5), ("fng_nivel", 15)],
    "funding": [("fund_contra", 0.8), ("fund_contra", 0.9), ("fund_contra", 0.95)],
    "macro": [("macro_rea", 0.0), ("macro_rea", 0.2), ("macro_rea", 0.5), ("macro_contra", 0.2),
              ("macro_contra", 0.5)],
    "gdelt": [("gdelt", 1.0), ("gdelt", 1.5), ("gdelt_contra", 1.0), ("gdelt_contra", 1.5)],
}
VOL_W = {"todos": 4, "no_baja": 1.5, "alta": 1, "baja": 0.5}
BTC_W = {"ninguno": 4, "a_favor": 1.5, "no_contra": 1.5}
WIN_W = {"todas": 3, "nuevayork": 2, "europa": 1.5, "asia": 1.5, "america": 1}
COIN_W = {"todas": 3, "BTC+ETH": 1, "SOL+XRP": 1.5, "BTC": 0.5, "ETH": 0.5, "SOL": 0.75, "XRP": 0.5}
DIA_W = {"todos": 6, "mar-jue": 1}
ENTRY_W = {"brk": 1.0, "ret": 0.6, "t": 1.5, "imp": 0.5, "pb": 1.0}


def entries_for(src: tuple) -> list[tuple]:
    kind = src[0]
    if kind == "ruptura":
        return [("brk", src[1], src[2]), ("ret", src[1], src[2])]
    if kind == "impulso":
        return [("imp", src[1], src[2])]
    if kind in ("ap_mom", "contra_ap"):
        out = [("t", M) for M in TIMES if M >= src[1]]
        return out + ([("pb", src[1])] if kind == "ap_mom" and src[1] in PB_N else [])
    return list(KEYS)


def confs_for(src: tuple, entry: tuple) -> list[tuple]:
    out = [("ninguna",), ("vwap",), ("rvol", 1.5), ("rvol", 2.0)]
    if src[0] not in ("ap_mom", "contra_ap", "impulso"):
        out += [("momento", 0.0), ("momento", 0.3)]
    if entry[0] == "t":
        out += [("ruptura", R, tf) for R in RANGES for tf in TFS if R < entry[1]]
    return out


def skey(st: dict) -> str:
    """Clave canónica (e identificador) de una estrategia."""
    f = lambda t: ":".join(_g(x) if isinstance(x, float) else str(x) for x in t)
    return "|".join([f(st["src"]), f(st["conf"]), st["vol"], st["btc"], st["win"], st["coins"], st["dia"],
                     f(st["entry"]), _g(st["sl"]), _g(st["r"]), st["dur"]])


def _wchoice(rng: random.Random, opts: list, weights: list[float]):
    return rng.choices(opts, weights=weights, k=1)[0]


def quotas(families: list[str], max_n: int) -> dict[str, int]:
    w = {f: FAMILIES[f][0] for f in families}
    tot = sum(w.values())
    raw = {f: max_n * x / tot for f, x in w.items()}
    q = {f: int(math.floor(x)) for f, x in raw.items()}
    for f in sorted(raw, key=lambda f: -(raw[f] - q[f]))[:max_n - sum(q.values())]:
        q[f] += 1
    return q


def generate(available: set[str] | None = None, max_n: int = 2000, seed: int = SEED) -> list[dict]:
    """Lista determinista y sin duplicados de estrategias de todas las familias con datos (muestreo estratificado):
    la fuente va por turnos dentro de cada familia, la horquilla y la duración recorren las 36 combinaciones por igual
    y el resto (confirmación, filtros y entrada) se sortea con pesos."""
    available = set(available or ())
    fams = [f for f, (_, _, need) in FAMILIES.items() if need is None or need in available]
    q = quotas(fams, max_n)
    out, seen = [], set()
    for fam in fams:
        rng = random.Random(f"{seed}:{fam}")
        srcs = list(SOURCES[fam])
        rng.shuffle(srcs)
        exits = [(sl, r, du) for sl in SL_GRID for r in R_GRID for du in DURS]
        rng.shuffle(exits)
        got = tries = 0
        while got < q[fam] and tries < q[fam] * 60:
            src = srcs[tries % len(srcs)]
            tries += 1
            ents = entries_for(src)
            entry = _wchoice(rng, ents, [ENTRY_W[x[0]] for x in ents])
            cfs = confs_for(src, entry)
            conf = _wchoice(rng, cfs, [3.0 if c[0] == "ninguna" else 1.0 for c in cfs])
            sl, r, du = exits[got % len(exits)]
            st = {"family": fam, "src": src, "conf": conf,
                  "vol": _wchoice(rng, list(VOL_W), list(VOL_W.values())),
                  "btc": "ninguno" if fam == "tendencia_btc" else _wchoice(rng, list(BTC_W), list(BTC_W.values())),
                  "win": _wchoice(rng, list(WIN_W), list(WIN_W.values())),
                  "coins": _wchoice(rng, list(COIN_W), list(COIN_W.values())),
                  "dia": _wchoice(rng, list(DIA_W), list(DIA_W.values())),
                  "entry": entry, "sl": sl, "r": r, "dur": du}
            k = skey(st)
            if k in seen:
                continue
            seen.add(k)
            st["id"] = k
            st["name"] = describe(st)
            out.append(st)
            got += 1
    return out


def _thr(x, unit: str = " %") -> str:
    return f" (|cambio| ≥ {_g(x)}{unit})" if x else ""


def src_text(src: tuple) -> str:
    k, p = src[0], src[1:]
    T = {
        "cad_ult": lambda: "sigue a la última bolsa que cerró antes de la apertura" + _thr(p[0]),
        "cad_may": lambda: f"sigue a la mayoría de las bolsas cerradas en las 24 h previas (al menos {p[0]} más a un "
                           "lado que al otro)",
        "cad_una": lambda: f"solo si todas las bolsas cerradas en 24 h (al menos {p[0]}) van del mismo lado",
        "cad_med": lambda: f"sigue la media de las bolsas cerradas en 24 h si pasa de ±{_g(p[0])} %",
        "cad_ny": lambda: "sigue a la última sesión de Nueva York" + _thr(p[0]),
        "cad_asia": lambda: "sigue a las bolsas asiáticas cerradas en 24 h" + _thr(p[0]),
        "cad_eu": lambda: "sigue a las bolsas europeas cerradas en 24 h" + _thr(p[0]),
        "bol_ayer": lambda: "sigue a la sesión anterior de las bolsas de la ventana" + _thr(p[0]),
        "bol_racha": lambda: f"sigue la racha de la bolsa ({p[0]} o más sesiones seguidas del mismo signo)",
        "ap_ayer": lambda: "sigue la reacción de la moneda en la primera hora de esta misma apertura el día anterior"
                           + _thr(p[0]),
        "ven_rea": lambda: "sigue la reacción de la moneda en la primera hora de la apertura anterior" + _thr(p[0]),
        "ven_des": lambda: "sigue el movimiento de la moneda desde la apertura anterior hasta esta" + _thr(p[0]),
        "ven_dos": lambda: "solo si coinciden las reacciones de las dos aperturas anteriores" + _thr(p[0]),
        "previo": lambda: f"sigue el movimiento de la moneda en los {p[0]} min previos" + _thr(p[1]),
        "ruptura": lambda: f"hacia donde rompa primero el rango de {p[0]} min (cierre de {p[1]} min)",
        "ap_mom": lambda: f"sigue el movimiento de los primeros {p[0]} min si pasa de ±{_g(p[1])} %",
        "impulso": lambda: f"a favor del primer impulso de ±{_g(p[0])} % en los primeros {p[1]} min",
        "consenso": lambda: f"consenso de cuatro señales (última bolsa, apertura anterior, momento de 60 min y misma "
                            f"bolsa) con al menos {p[0]} de ventaja",
        "consenso2": lambda: "solo si la última bolsa cerrada y la reacción de la apertura anterior coinciden"
                             + _thr(p[0]),
        "contra_cad": lambda: "A LA CONTRA de la última bolsa que cerró" + _thr(p[0]),
        "contra_ven": lambda: "A LA CONTRA de la reacción de la apertura anterior" + _thr(p[0]),
        "contra_previo": lambda: f"A LA CONTRA del movimiento de los {p[0]} min previos" + _thr(p[1]),
        "contra_ap": lambda: f"A LA CONTRA del movimiento de los primeros {p[0]} min si pasa de ±{_g(p[1])} %",
        "btc": lambda: {"ambas": "a favor de la tendencia diaria de BTC (cierre frente a las medias de 20 y 50 días)",
                        "ma20": "a favor de BTC frente a su media de 20 días",
                        "ma50": "a favor de BTC frente a su media de 50 días"}[p[0]],
        "fng_contra": lambda: f"a la contra del miedo y codicia extremo (≤ {p[0]} compra, ≥ {100 - p[0]} vende)",
        "fng_cambio": lambda: f"sigue el cambio del miedo y codicia en {p[0]} días (±{_g(p[1])} puntos)",
        "fng_nivel": lambda: f"sigue el nivel del miedo y codicia (≥ {50 + p[0]} compra, ≤ {50 - p[0]} vende)",
        "fund_contra": lambda: f"a la contra del funding extremo (percentil de 30 días ≥ {_g(p[0] * 100)} vende, "
                               f"≤ {_g(100 - p[0] * 100)} compra)",
        "macro_rea": lambda: "sigue la reacción de los 15 primeros min a un dato macro (4 h antes o en la primera hora)"
                             + _thr(p[0]),
        "macro_contra": lambda: "A LA CONTRA de la reacción de los 15 primeros min a un dato macro" + _thr(p[0]),
        "gdelt": lambda: f"sigue el tono de las noticias (GDELT) si se aparta ±{_g(p[0])} desviaciones de su media",
        "gdelt_contra": lambda: f"A LA CONTRA del tono de las noticias (GDELT) a ±{_g(p[0])} desviaciones",
    }
    return T[k]()


def entry_text(e: tuple) -> str:
    if e[0] == "brk":
        return f"entra al confirmar la ruptura del rango de {e[1]} min (cierre de {e[2]} min)"
    if e[0] == "ret":
        return f"entra en el retesteo del borde del rango de {e[1]} min tras la ruptura (cierre de {e[2]} min)"
    if e[0] == "t":
        return f"entra a +{e[1]} min"
    if e[0] == "imp":
        return f"entra tras un impulso de {_g(e[1])} % a favor en los primeros {e[2]} min"
    return f"entra en la reanudación tras retroceder la mitad del movimiento de los primeros {e[1]} min"


def conf_text(c: tuple) -> str:
    if c[0] == "ninguna":
        return "sin confirmación extra"
    if c[0] == "momento":
        return "si va a favor desde la apertura" + (f" ≥ {_g(c[1])} %" if c[1] else "")
    if c[0] == "vwap":
        return "si el precio está del lado del VWAP"
    if c[0] == "rvol":
        return f"si el volumen relativo ≥ {_g(c[1])}"
    return f"si ya rompió a favor el rango de {c[1]} min (cierre de {c[2]} min)"


def describe(st: dict) -> str:
    filt = [WIN_TEXT[st["win"]], "todas las monedas" if st["coins"] == "todas" else st["coins"].replace("+", " y ")]
    if st["vol"] != "todos":
        filt.append({"no_baja": "sin volatilidad baja", "alta": "solo volatilidad alta",
                     "baja": "solo volatilidad baja"}[st["vol"]])
    if st["btc"] != "ninguno":
        filt.append({"a_favor": "solo a favor de la tendencia de BTC", "no_contra": "nunca contra la tendencia de BTC"}
                    [st["btc"]])
    if st["dia"] != "todos":
        filt.append("solo de martes a jueves")
    return (f"{FAMILIES[st['family']][1]}: {src_text(st['src'])} · {entry_text(st['entry'])} · "
            f"{conf_text(st['conf'])} · {', '.join(filt)} · SL {_g(st['sl'])} % · TP {_g(st['r'])}R "
            f"({_g(st['sl'] * st['r'])} %) · {DUR_TEXT[st['dur']]}")


def rules_ok(st: dict) -> bool:
    """Reglas del usuario: SL ≥ 1,5 %, R ≥ 2 y horquilla pura (sin break-even ni stop dinámico)."""
    return (st["sl"] >= 1.5 and st["r"] >= 2.0 and st["dur"] in DURS
            and not any(k in st for k in ("be_r", "beR", "trail", "trailPct", "trail_atr")))


def neighbors(st: dict) -> list[dict]:
    """Vecinos en la rejilla: SL y R a un paso, el umbral de la fuente a un paso y el de la confirmación."""
    out = []

    def add(**ch):
        x = {**st, **ch}
        if rules_ok(x) and skey(x) != skey(st):
            x["id"] = skey(x)
            out.append(x)

    i = SL_ALL.index(st["sl"])
    for j in (i - 1, i + 1):
        if 0 <= j < len(SL_ALL):
            add(sl=SL_ALL[j])
    i = R_ALL.index(st["r"])
    for j in (i - 1, i + 1):
        if 0 <= j < len(R_ALL):
            add(r=R_ALL[j])
    fam = SOURCES[st["family"]]
    same = [s for s in fam if s[0] == st["src"][0] and s[1:-1] == st["src"][1:-1]]
    if len(same) > 1 and st["src"] in same and st["src"][0] not in ("ruptura", "impulso", "btc"):
        same.sort(key=lambda s: s[-1])
        i = same.index(st["src"])
        for j in (i - 1, i + 1):
            if 0 <= j < len(same):
                add(src=same[j])
    if st["conf"][0] == "momento":
        add(conf=("momento", 0.3 if st["conf"][1] == 0 else 0.0))
    elif st["conf"][0] == "rvol":
        add(conf=("rvol", 2.0 if st["conf"][1] == 1.5 else 1.5))
    return out


# ---------------------------------------------------------------- contra la suerte

def _seed(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def null_pool(lab: Lab, ctx: tuple, reps: int) -> list[np.ndarray]:
    """Base aleatoria GLOBAL fuera de muestra (solo referencia de costes): mismas ventanas, monedas, entrada,
    horquilla y duración con la dirección al azar por apertura (las monedas de una apertura van juntas) y una posición
    por moneda. No aplica los filtros ni la fuente de ninguna estrategia. Devuelve los R de cada repetición."""
    win, coins, entry, sl, r, dur = ctx
    m = lab.valid & lab.win_mask[win] & lab.coin_mask[coins] & lab.oos
    e = np.flatnonzero(m)
    ki = KEY_IDX[entry]
    side = []
    for di in (0, 1):
        u = lab.slot_u[e, ki, di]
        has = u >= 0
        res = lab.resolve(np.where(has, u, 0), sl, r, dur) if lab.U is not None and len(u) else None
        if res is None:
            return [np.zeros(0) for _ in range(reps)]
        side.append((has & res["ok"] & (res["why"] != 3), res["R"], res["ent"], res["ext"]))
    rng = np.random.default_rng(_seed(*ctx))
    op = e // lab.nc
    out = []
    for _ in range(reps):
        pick = rng.integers(0, 2, lab.no).astype(bool)[op]      # un sorteo por apertura
        has = np.where(pick, side[1][0], side[0][0])
        R = np.where(pick, side[1][1], side[0][1])[has]
        ent = np.where(pick, side[1][2], side[0][2])[has]
        ext = np.where(pick, side[1][3], side[0][3])[has]
        keep = no_overlap(lab.coin[e[has]], ent, ext, e[has])
        out.append(R[keep])
    return out


def opening_blocks(lab: Lab, e: np.ndarray, dd: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Agrupa los eventos por apertura: (aperturas, índice de la apertura de cada evento, sentido mayoritario de cada
    apertura (+1 si empate) y patrón de cada evento respecto a ese sentido). dd = patrón × sentido[índice]."""
    op = e // lab.nc
    uo, inv = np.unique(op, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    msum = np.bincount(inv, weights=dd.astype(float), minlength=len(uo))
    mo = np.where(msum >= 0, 1, -1).astype(np.int8)
    rel = (dd * mo[inv]).astype(np.int8)
    return uo, inv, mo, rel


def _mean_R(tr: dict) -> float:
    return float(tr["R"].mean()) if len(tr["R"]) else float("nan")


def _p_emp(vals: np.ndarray, act: float) -> float:
    v = vals[np.isfinite(vals)]
    return round(float((1 + (v >= act).sum()) / (1 + len(v))), 4) if len(v) else 1.0


def matched_base(lab: Lab, st: dict, reps: int) -> dict:
    """Base emparejada fuera de muestra: los MISMOS eventos de la estrategia (todos sus filtros y la selección de la
    fuente) con la dirección al azar por apertura (se conserva el patrón entre monedas de cada apertura) y pasando por su
    filtro de BTC, su confirmación y su entrada (con una entrada que depende de la dirección, la base es «el disparador
    solo»). Dos versiones: con la misma cuota de largos que la estrategia (separa la habilidad de la exposición a la
    tendencia del periodo) y al 50 %."""
    e, dd = lab.events(st, oos_only=True)
    real = lab.run(st, e, dd)
    if len(real["R"]) < 5:
        return {"reps": 0, "n": len(real["R"])}
    act = _mean_R(real)
    uo, inv, mo, rel = opening_blocks(lab, e, dd)
    p_long = float((mo > 0).mean())
    rng = np.random.default_rng(_seed("base", st["id"]))
    out = {"n": len(real["R"]), "actual_mean_R": round(act, 4), "long_share_openings": round(p_long, 4)}
    worst = -np.inf
    for name, p in (("beta", p_long), ("50", 0.5)):
        vals = np.array([_mean_R(lab.run(st, e, (rel * np.where(rng.random(len(uo)) < p, 1, -1)[inv]).astype(np.int8)))
                         for _ in range(reps)])
        v = vals[np.isfinite(vals)]
        if not len(v):
            continue
        p95 = _q(v, NULL_Q)
        worst = max(worst, p95)
        out[name] = {"reps": int(len(v)), "mean": round(float(v.mean()), 4), "p5": round(_q(v, 0.05), 4),
                     "p50": round(_q(v, 0.5), 4), "p95": round(p95, 4), "p": _p_emp(vals, act)}
    out["reps"] = min(out.get("beta", {}).get("reps", 0), out.get("50", {}).get("reps", 0))
    out["p95_max"] = round(worst, 4) if math.isfinite(worst) else None
    out["mean"] = (out.get("beta") or {}).get("mean")          # resumen: la base con su cuota de largos
    out["p95"] = out["p95_max"]
    out["p"] = max(out.get("beta", {}).get("p", 1.0), out.get("50", {}).get("p", 1.0))
    out["beats"] = bool(out["reps"] and math.isfinite(worst) and act > worst)
    return out


def perm_test(lab: Lab, st: dict, n_perm: int) -> dict:
    """Permutación de la señal fuera de muestra por bloques: el sentido de cada apertura (el patrón entre sus monedas se
    conserva) se reparte entre las aperturas de la misma ventana y se vuelve a operar con los mismos filtros y entradas;
    se conserva la cuota de largos. p = (1 + #(R medio ≥ real)) / (1 + n_perm). También el p por evento (la permutación
    de antes, que rompe la coherencia entre monedas y sale demasiado bajo). Con la entrada «pb» la dirección la fija el
    propio disparador (el movimiento de los primeros minutos): la permutación no informa y se marca como no aplicable."""
    e, dd = lab.events(st, oos_only=True)
    real = lab.run(st, e, dd)
    act = _mean_R(real)
    if st["entry"][0] == "pb":
        return {"p": None, "p_event": None, "applicable": False, "n": len(real["R"]), "perms": 0,
                "actual_mean_R": round(act, 4) if math.isfinite(act) else None,
                "why": "la entrada «pb» solo existe en el sentido del movimiento de los primeros minutos: al barajar la "
                       "dirección el disparador la vuelve a poner"}
    if len(real["R"]) < 5:
        return {"p": 1.0, "p_event": 1.0, "n": len(real["R"]), "perms": 0, "applicable": True}
    uo, inv, mo, rel = opening_blocks(lab, e, dd)
    w = lab.wid[uo * lab.nc]
    groups = [np.flatnonzero(w == k) for k in np.unique(w)]
    rng = np.random.default_rng(_seed("perm", st["id"]))
    vb, ve = [], []
    for _ in range(n_perm):
        mp = mo.copy()
        for g in groups:
            mp[g] = mo[g][rng.permutation(len(g))]
        vb.append(_mean_R(lab.run(st, e, (rel * mp[inv]).astype(np.int8))))
        ve.append(_mean_R(lab.run(st, e, rng.permutation(dd))))
    vb, ve = np.array(vb), np.array(ve)
    fin = vb[np.isfinite(vb)]
    return {"p": _p_emp(vb, act), "p_event": _p_emp(ve, act), "applicable": True, "actual_mean_R": round(act, 4),
            "perm_mean_R": round(float(fin.mean()), 4) if len(fin) else None,
            "perm_p95_R": round(_q(fin, 0.95), 4) if len(fin) else None,
            "perms": n_perm, "n": len(real["R"]), "p_min": round(1 / (1 + n_perm), 4)}


def month_bootstrap(lab: Lab, tr: dict, reps: int = N_BOOT, seed: int = 0) -> dict:
    """Remuestreo de MESES fuera de muestra (con reemplazo): qué parte de las historias posibles con esos mismos meses
    da R medio ≤ 0. Mide si el resultado depende de uno o dos episodios."""
    o = lab.oos[tr["e"]]
    if o.sum() < 5:
        return {"months": 0}
    mon = lab.month[tr["e"][o]]
    um, inv = np.unique(mon, return_inverse=True)
    s = np.bincount(inv, weights=tr["R"][o])
    c = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(um), (reps, len(um)))
    means = s[idx].sum(1) / np.maximum(c[idx].sum(1), 1)
    return {"months": int(len(um)), "p_le_0": round(float((means <= 0).mean()), 4),
            "p5": round(_q(means, 0.05), 4), "p50": round(_q(means, 0.5), 4), "p95": round(_q(means, 0.95), 4)}


def short_side(lab: Lab, st: dict, tr: dict) -> dict:
    """Cortos fuera de muestra: en contado USDC no se puede ir corto. R con el modelo del laboratorio (costes de
    contado) y en perpetuo (comisión FEE_PERP por lado y el funding cobrado o pagado en cada liquidación dentro de la
    operación). Quedan marcados como NO ejecutables mientras no esté confirmado el acceso a X-Perps de OKX EU."""
    o = np.flatnonzero(lab.oos[tr["e"]] & (tr["dir"] < 0))
    if not len(o):
        return {"trades": 0, "executable": False}
    fund, have = np.zeros(len(o)), 0
    for i, k in enumerate(o):
        f = lab.funding.get(int(lab.coin[tr["e"][k]]))
        if f is None or not len(f):
            continue
        a, b = np.searchsorted(f[:, 0], [tr["ent"][k], tr["ext"][k]], side="right")
        fund[i] = float(f[a:b, 1].sum()) * 100 / st["sl"]          # el corto cobra el funding positivo
        have += 1
    R = tr["R"][o]
    fee = (FEE - FEE_PERP) * 2 * 100 / st["sl"]
    return {"trades": int(len(o)), "mean_R_spot_model": round(float(R.mean()), 4),
            "mean_R_perp": round(float((R + fee + fund).mean()), 4), "funding_R": round(float(fund.mean()), 4),
            "funding_coverage": round(have / len(o), 4), "executable": False,
            "note": "Sin confirmar el acceso a X-Perps de OKX EU: los cortos no se pueden ejecutar en contado."}


def null_stats(pool: list[np.ndarray], n: int, seed: int) -> dict:
    """Distribución nula del R medio con n operaciones (submuestra de cada repetición al azar)."""
    rng = np.random.default_rng(seed)
    means = []
    for R in pool:
        if not len(R):
            continue
        k = min(n, len(R))
        means.append(float(R[rng.choice(len(R), k, replace=False)].mean()) if k < len(R) else float(R.mean()))
    if not means:
        return {"reps": 0}
    a = np.array(means)
    return {"reps": len(a), "mean": round(float(a.mean()), 4), "p5": round(_q(a, 0.05), 4),
            "p50": round(_q(a, 0.5), 4), "p95": round(_q(a, NULL_Q), 4), "values": a}


# ---------------------------------------------------------------- cuenta diaria

def account(lab: Lab, trs: list[dict], sls: list[float], risk_pct: float, lev: float = 1.0,
            eq0: float = 10_000.0, part: str = "oos", day_from: str | None = None) -> dict:
    """Cuenta de eq0 USDC con las operaciones de una o varias estrategias: una posición por moneda a la vez (entre
    todas), nominal = lev × capital × riesgo / stop, exposición total ≤ lev × capital (si no cabe, se reduce), sin
    reinvertir lo no realizado. El resultado se apunta el día (de Madrid) en que se cierra; si cae en un día sin
    aperturas, en el siguiente con aperturas. Con lev 2 cada operación arriesga el doble: amplifica pérdidas igual que
    ganancias. Si dos operaciones entran a la vez en la misma moneda, manda el orden de la lista (prioridad fija de la
    selección), nunca la hora de salida (sería mirar al futuro)."""
    rows = []
    for prio, (tr, sl) in enumerate(zip(trs, sls)):
        sel = lab.oos[tr["e"]] if part == "oos" else ~lab.oos[tr["e"]] if part == "is" else np.ones(len(tr["e"]), bool)
        for k in np.flatnonzero(sel):
            rows.append((int(tr["ent"][k]), prio, int(k), int(tr["ext"][k]), int(lab.coin[tr["e"][k]]), sl,
                         float(tr["ret"][k])))
    rows.sort(key=lambda x: x[:3])
    rows = [(ent, ext, coin, sl, ret) for ent, _, _, ext, coin, sl, ret in rows]
    if part == "oos":
        days = [d for d in lab.days if d >= lab.split_day]
    elif part == "is":
        days = [d for d in lab.days if d < lab.split_day]
    elif part == "desde":
        days = [d for d in lab.days if d >= (day_from or "")]
    else:
        days = list(lab.days)
    eq, busy, expo = eq0, {}, 0.0
    heap: list = []
    real: list[tuple[int, float]] = []
    skipped = {"moneda_ocupada": 0, "sin_capital": 0}

    def settle(until: int):
        nonlocal eq, expo
        while heap and heap[0][0] <= until:
            ext, coin, notional, pnl = heapq.heappop(heap)
            eq += pnl
            expo -= notional
            busy.pop(coin, None)
            real.append((ext, pnl))

    for ent, ext, coin, sl, ret in rows:
        settle(ent)
        if coin in busy:
            skipped["moneda_ocupada"] += 1
            continue
        want = lev * eq * risk_pct / sl
        notional = min(want, lev * eq - expo)
        if notional < 0.01 * eq:
            skipped["sin_capital"] += 1
            continue
        busy[coin] = ext
        expo += notional
        heapq.heappush(heap, (ext, coin, notional, notional * ret / 100))
    settle(2 ** 62)
    if not days:
        return {"days": 0}
    pnl_day = np.zeros(len(days))
    n_day = np.zeros(len(days), np.int64)
    dz = np.array(days)
    for ext, pnl in real:
        d = datetime.fromtimestamp(ext / 1000, MAD).date().isoformat()
        if d < days[0]:
            continue
        k = min(int(np.searchsorted(dz, d)), len(days) - 1)
        pnl_day[k] += pnl
        n_day[k] += 1
    eq_start = eq0 + np.concatenate(([0.0], np.cumsum(pnl_day)[:-1]))
    pct = pnl_day / eq_start * 100
    st = bt.day_stats([(d, float(x), int(nd), False, False) for d, x, nd in zip(days, pct, n_day)])
    months: dict[str, list] = {}
    for d, x, nd in zip(days, pct, n_day):
        months.setdefault(d[:7], []).append((x, nd))
    st["monthly"] = [{"month": mo, "pct": round((float(np.prod([1 + x / 100 for x, _ in v])) - 1) * 100, 3),
                      "trades": int(sum(nd for _, nd in v))} for mo, v in sorted(months.items())]
    st["final_equity"] = round(eq0 + float(pnl_day.sum()), 2)
    st["skipped"] = skipped
    st["params"] = {"equity": eq0, "risk_pct": risk_pct, "leverage": lev, "part": part}
    return st


# ---------------------------------------------------------------- informe

def trade_rows(lab: Lab, tr: dict, limit: int = 300, part: str = "oos") -> list[dict]:
    sel = np.flatnonzero(lab.oos[tr["e"]] if part == "oos" else np.ones(len(tr["e"]), bool))[-limit:]
    out = []
    WHY = ("stop", "objetivo", "tiempo", "fin de datos")
    for k in sel:
        e = int(tr["e"][k])
        out.append({"date": lab.mday[e // lab.nc], "window": WIN_IDS[int(lab.wid[e])], "coin": lab.coins[int(lab.coin[e])],
                    "side": "long" if tr["dir"][k] > 0 else "short", "entry_t": int(tr["ent"][k]),
                    "exit_t": int(tr["ext"][k]), "entry_px": round(float(tr["px"][k]), 6),
                    "hours": round((int(tr["ext"][k]) - int(tr["ent"][k])) / H, 2), "R": round(float(tr["R"][k]), 3),
                    "ret_pct": round(float(tr["ret"][k]), 4), "why": WHY[int(tr["why"][k])]})
    return out


def _clean(x):
    if isinstance(x, np.ndarray):
        return [_clean(v) for v in x.tolist()]
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _small(m: dict) -> dict:
    return {k: m.get(k) for k in ("trades", "win_rate", "mean_R", "mean_ret_pct", "profit_factor", "t_day", "p_day",
                                  "tp_share", "time_share", "long_share", "weeks", "mean_R_wo_best_month")}


def evaluate_all(lab: Lab, strategies: list[dict], log_every: int = 250) -> list[dict]:
    """Métricas de todas las estrategias: muestra (60 %), fuera de muestra (40 %) y por año."""
    is_span, oos_span = lab.split_ms - lab.t_from, lab.t_to - lab.split_ms
    out = []
    t0 = time.time()
    for k, st in enumerate(strategies):
        tr = lab.trades(st)
        oos = lab.oos[tr["e"]] if len(tr["e"]) else np.zeros(0, bool)
        out.append({"st": st, "is": metrics(lab, tr, ~oos, is_span, "is"), "oos": metrics(lab, tr, oos, oos_span, "oos"),
                    "all_weeks": int(len(np.unique(lab.week[tr["e"]]))) if len(tr["e"]) else 0,
                    "by_year": by_year(lab, tr), "by_quarter": by_quarter(lab, tr),
                    "_daily_oos": daily_series(lab, {k_: v[oos] for k_, v in tr.items()}, lab.day_range("oos"))})
        if log_every and (k + 1) % log_every == 0:
            log.info("Evaluadas %d de %d estrategias (%.0f s)", k + 1, len(strategies), time.time() - t0)
    return out


def _years_is_ok(r: dict) -> bool:
    """Ningún año de la muestra con al menos MIN_YEAR_IS operaciones sale negativo."""
    yrs = [y for y in r["by_year"].values() if (y.get("is_n") or 0) >= MIN_YEAR_IS]
    return all((y.get("is_mean_R") or 0) > 0 for y in yrs)


def classify(lab: Lab, res: list[dict], n_null: int, n_perm: int, n_trials: int | None = None) -> dict:
    """Base emparejada, permutación por bloques, BH (p HAC), Sharpe deflactado (T propio y N efectivo), vecinos dentro
    y fuera, meses y años; clasifica cada estrategia."""
    n_trials = n_trials or len(res)
    ps = [r["oos"].get("p_day", 1.0) if r["oos"].get("trades", 0) >= MIN_TRADES else 1.0 for r in res]
    qs = bh_qvalues(ps)
    enough = [r for r in res if r["oos"].get("trades", 0) >= MIN_TRADES]
    srs = [r["oos"]["sr_day"] for r in enough]
    sr_var = float(np.var(srs)) if len(srs) > 1 else 0.0
    daily = [r.get("_daily_oos") for r in enough if r.get("_daily_oos") is not None and len(r["_daily_oos"])]
    n_eff = effective_trials(np.vstack(daily)) if len(daily) > 1 else float(max(1, len(daily)))
    n_eff = min(n_eff, float(n_trials))
    q_eff = bh_qvalues([min(1.0, p * n_eff / max(1, len(ps))) for p in ps]) if ps else []   # BH con m efectivo
    oos_span = lab.t_to - lab.split_ms

    for r, q, qe in zip(res, qs, q_eff or [1.0] * len(res)):
        o, i = r["oos"], r["is"]
        r["q"] = round(q, 4)
        r["q_eff"] = round(qe, 4)
        r["enough"] = o.get("trades", 0) >= MIN_TRADES
        r["pos_is"] = bool(i.get("trades", 0) >= MIN_TRADES // 2 and i.get("mean_R", -1) > 0)
        r["pos_oos"] = bool(r["enough"] and o["mean_R"] > 0)
        r["dsr"] = deflated_sharpe(o["sr_day"], o["days"], o["skew_day"], o["kurt_day"], None, n_eff) \
            if r["enough"] else float("nan")
        r["weeks_ok"] = bool(r["enough"] and o.get("weeks", 0) >= MIN_WEEKS_OOS
                             and r.get("all_weeks", 0) >= MIN_WEEKS_ALL)
        r["wo_best_ok"] = bool(r["enough"] and (o.get("mean_R_wo_best_month") or -1) > 0)
        r["years_is_ok"] = _years_is_ok(r)
        r["tags"] = []
    # Base emparejada para todas las que tienen datos suficientes (así se sabe cuántas la baten por azar).
    t0 = time.time()
    tested = 0
    for r in res:
        if not r["enough"]:
            continue
        nb = matched_base(lab, r["st"], n_null)
        r["null"] = nb
        r["beats_base"] = bool(nb.get("beats"))
        r["p_null"] = nb.get("p", 1.0)
        tested += 1
    log.info("Base emparejada: %d estrategias (%.0f s)", tested, time.time() - t0)
    for r in res:
        r["promising"] = bool(r["pos_is"] and r["pos_oos"] and r.get("beats_base") and r["weeks_ok"]
                              and r["wo_best_ok"] and r["years_is_ok"])
    # Permutación, vecinos, meses y años para las prometedoras y las mejores fuera de muestra.
    ranked = sorted([r for r in res if r["enough"]], key=lambda r: -r["oos"]["mean_R"])
    cands = [r for r in ranked if r["promising"]][:MAX_PERM_CANDS]
    for r in ranked[:50]:
        if r not in cands and len(cands) < MAX_PERM_CANDS + 50:
            cands.append(r)
    t0 = time.time()
    is_span = lab.split_ms - lab.t_from
    for r in cands:
        st = r["st"]
        r["perm"] = perm_test(lab, st, n_perm)
        nb = []
        for x in neighbors(st):
            tr = lab.trades(x)
            oo = lab.oos[tr["e"]] if len(tr["e"]) else np.zeros(0, bool)
            m = metrics(lab, tr, oo, oos_span, "oos") if len(tr["e"]) else {"trades": 0}
            mi = metrics(lab, tr, ~oo, is_span, "is") if len(tr["e"]) else {"trades": 0}
            nb.append({"id": x["id"], "change": _neighbor_change(st, x), "oos": _small(m), "is": _small(mi)})
        r["neighbors"] = nb
        ev = [x for x in nb if (x["oos"].get("trades") or 0) >= MIN_TRADES // 2]
        ev_is = [x for x in nb if (x["is"].get("trades") or 0) >= MIN_TRADES // 2]
        r["stable"] = bool(ev) and all(x["oos"]["mean_R"] > 0 for x in ev) and all(x["is"]["mean_R"] > 0 for x in ev_is)
        yrs = [y for y in r["by_year"].values() if (y.get("oos_n") or 0) >= 5]
        r["years_ok"] = bool(yrs) and all(y["oos_mean_R"] > 0 for y in yrs)
        r["boot"] = month_bootstrap(lab, lab.trades(st), seed=_seed("boot", st["id"]))
    log.info("Permutación y vecinos: %d estrategias (%.0f s)", len(cands), time.time() - t0)
    for r in res:
        pm = r.get("perm") or {}
        checks = {"positiva_en_muestra": r["pos_is"], "positiva_fuera": r["pos_oos"],
                  "bate_a_la_base": bool(r.get("beats_base")),
                  "fuera_sin_su_mejor_mes": r["wo_best_ok"], "semanas": r["weeks_ok"],
                  "años_de_la_muestra": r["years_is_ok"],
                  "permutacion": bool(pm.get("p") is not None and pm["p"] < P_PERM),
                  "benjamini_hochberg": bool(r["enough"] and r["q"] <= FDR_Q),
                  "sharpe_deflactado": bool(math.isfinite(r["dsr"]) and r["dsr"] >= DSR_MIN),
                  "vecinos": bool(r.get("stable")), "cada_año_fuera": bool(r.get("years_ok"))}
        r["checks"] = checks
        r["class"] = "robusta" if all(checks.values()) else "prometedora" if r["promising"] else "descartada"
    beat = sum(bool(r.get("beats_base")) for r in res)
    return {"sr_var": sr_var, "n_trials": n_trials, "n_eff": round(n_eff, 1), "cands": len(cands),
            "base_tested": tested, "base_beaten": beat,
            "base_tested_pos_is": sum(1 for r in res if r["enough"] and r["pos_is"]),
            "min_q_eff": round(min(q_eff), 4) if q_eff else None}


def _neighbor_change(a: dict, b: dict) -> str:
    for k, txt in (("sl", "SL"), ("r", "R")):
        if a[k] != b[k]:
            return f"{txt} {_g(a[k])} → {_g(b[k])}"
    if a["src"] != b["src"]:
        return f"umbral de la fuente {_g(a['src'][-1])} → {_g(b['src'][-1])}"
    return f"umbral de la confirmación {_g(a['conf'][1])} → {_g(b['conf'][1])}"


def _wf_pick(res: list[dict], y: int, min_n: int) -> list[tuple]:
    """Candidatas para el año y: R medio de los años anteriores con al menos min_n operaciones, de mejor a peor."""
    cand = []
    for r in res:
        n = s = 0
        for yy, x in r["by_year"].items():
            if int(yy) < y:
                n += x["n"]
                s += x["sum_R"]
        if n >= min_n:
            cand.append((s / n, n, r))
    cand.sort(key=lambda c: (-c[0], c[2]["st"]["id"]))
    return cand


def walk_forward(lab: Lab, res: list[dict], top: int = 10, min_n: int = MIN_TRADES) -> list[dict]:
    """Selección en avance por años: cada año se eligen las mejores (R medio de los años anteriores, con al menos
    min_n operaciones) y se mide qué dieron ese año. Es la estimación honesta de «elegir con el laboratorio»."""
    years = sorted({int(y) for y in lab.year})
    out = []
    for y in years[1:]:
        cand = _wf_pick(res, y, min_n)
        pick = cand[:top]
        tests = [(c, c[2]["by_year"].get(str(y))) for c in pick]
        tests = [(c, t) for c, t in tests if t and t["n"]]
        if not tests:
            continue
        n_t = sum(t["n"] for _, t in tests)
        out.append({"year": y, "train_years": [yy for yy in years if yy < y], "min_train_trades": min_n,
                    "candidates": len(cand),
                    "top1": {"name": pick[0][2]["st"]["name"], "id": pick[0][2]["st"]["id"],
                             "train_mean_R": round(pick[0][0], 4), "train_n": pick[0][1],
                             "test": pick[0][2]["by_year"].get(str(y))},
                    "top": len(tests), "test_trades": n_t,
                    "test_mean_R": round(sum(t["sum_R"] for _, t in tests) / n_t, 4),
                    "test_positive": sum(1 for _, t in tests if t["mean_R"] > 0)})
    return out


def _wf_pick_q(res: list[dict], q: str, min_n: int) -> list[tuple]:
    """Candidatas para el trimestre q: R medio de TODOS los trimestres anteriores (ventana creciente)."""
    cand = []
    for r in res:
        n = s = 0
        for qq, x in (r.get("by_quarter") or {}).items():
            if qq < q:
                n += x["n"]
                s += x["sum_R"]
        if n >= min_n:
            cand.append((s / n, n, r))
    cand.sort(key=lambda c: (-c[0], c[2]["st"]["id"]))
    return cand


def walk_forward_q(lab: Lab, res: list[dict], top: int = 10, min_n: int = 100) -> dict:
    """Selección trimestral en avance con ventana creciente: cada trimestre (desde que hay WF_MIN_TRAIN_Q de
    entrenamiento) se eligen las `top` mejores con todo lo anterior y se mide qué dieron en ese trimestre."""
    quarters = list(lab.quarters)
    out, n_t, s_t = [], 0, 0.0
    for q in quarters[WF_MIN_TRAIN_Q:]:
        pick = _wf_pick_q(res, q, min_n)[:top]
        tests = [(c, (c[2].get("by_quarter") or {}).get(q)) for c in pick]
        tests = [(c, t) for c, t in tests if t and t["n"]]
        if not tests:
            continue
        n = sum(t["n"] for _, t in tests)
        s = sum(t["sum_R"] for _, t in tests)
        n_t, s_t = n_t + n, s_t + s
        out.append({"quarter": q, "candidates": len(_wf_pick_q(res, q, min_n)), "top": len(tests), "test_trades": n,
                    "test_mean_R": round(s / n, 4), "test_positive": sum(1 for _, t in tests if t["sum_R"] > 0),
                    "top1": {"id": pick[0][2]["st"]["id"], "train_mean_R": round(pick[0][0], 4), "train_n": pick[0][1]}})
    return {"min_train_trades": min_n, "top": top, "quarters": out, "test_trades": n_t,
            "test_mean_R": round(s_t / n_t, 4) if n_t else None,
            "quarters_positive": sum(1 for x in out if x["test_mean_R"] > 0)}


def walk_forward_account(lab: Lab, res: list[dict], top: int = 10, min_n: int = 100, period: str = "year") -> dict:
    """Cuenta honesta en avance: cada año natural (o cada trimestre, con ventana creciente) se operan solo las `top`
    mejores elegidas con lo anterior (una posición por moneda entre todas; prioridad, el puesto en la selección)."""
    if period == "quarter":
        keys = list(lab.quarters)[WF_MIN_TRAIN_Q:]
        pick_fn = lambda k: _wf_pick_q(res, k, min_n)[:top]
        sel_fn = lambda tr, k: lab.quarter[tr["e"]] == lab.quarters.index(k)
        first_day = lambda k: f"{k[:4]}-{(int(k[-1]) - 1) * 3 + 1:02d}-01"
    else:
        years = sorted({int(y) for y in lab.year})
        keys = years[1:]
        pick_fn = lambda k: _wf_pick(res, k, min_n)[:top]
        sel_fn = lambda tr, k: lab.year[tr["e"]] == k
        first_day = lambda k: f"{k}-01-01"
    if not keys:
        return {}
    trs, sls, picks, cache = [], [], [], {}
    for k in keys:
        chosen = pick_fn(k)
        for _, _, r in chosen:
            sid = r["st"]["id"]
            if sid not in cache:
                cache[sid] = lab.trades(r["st"])
            tr = cache[sid]
            sel = sel_fn(tr, k)
            trs.append({kk: v[sel] for kk, v in tr.items()})
            sls.append(r["st"]["sl"])
        picks.append({"period": k, "ids": [r["st"]["id"] for _, _, r in chosen]})
    started = [p["period"] for p in picks if p["ids"]]
    if not started:
        return {}
    d0 = first_day(started[0])          # desde el primer periodo con alguna elegida
    acc = {f"riesgo_{_g(rk).replace(',', '_')}_lev{int(lv)}": account(lab, trs, sls, rk, lv, part="desde", day_from=d0)
           for rk, lv in ((0.5, 1.0), (1.5, 1.0), (0.5, 2.0), (1.5, 2.0))}
    return {"from": d0, "period": period, "top": top, "min_train_trades": min_n, "picks": picks, **acc}


def family_table(res: list[dict]) -> list[dict]:
    out = []
    for fam, (_, txt, _) in FAMILIES.items():
        xs = [r for r in res if r["st"]["family"] == fam]
        if not xs:
            continue
        en = [r for r in xs if r["enough"]]
        mo = [r["oos"]["mean_R"] for r in en]
        best = max(en, key=lambda r: r["oos"]["mean_R"]) if en else None
        out.append({"family": fam, "name": txt, "tested": len(xs), "enough_trades": len(en),
                    "positive_is": sum(r["pos_is"] for r in xs), "positive_oos": sum(r["pos_oos"] for r in xs),
                    "positive_both": sum(r["pos_is"] and r["pos_oos"] for r in xs),
                    "beat_base": sum(bool(r.get("beats_base")) for r in xs),
                    "promising": sum(r["class"] == "prometedora" for r in xs),
                    "robust": sum(r["class"] == "robusta" for r in xs),
                    "median_oos_R": round(float(np.median(mo)), 4) if mo else None,
                    "share_pos_oos": round(sum(x > 0 for x in mo) / len(mo), 4) if mo else None,
                    "best": {"name": best["st"]["name"], "id": best["st"]["id"], "oos": _small(best["oos"]),
                             "is": _small(best["is"])} if best else None})
    return sorted(out, key=lambda x: -(x["median_oos_R"] if x["median_oos_R"] is not None else -9))


def exit_table(res: list[dict], gbase: list[dict]) -> list[dict]:
    """Por horquilla y duración (¿objetivo de 3 % o stops y objetivos más anchos?): mediana y media del R fuera de
    muestra de las estrategias con datos suficientes, cuántas salen positivas y su base aleatoria."""
    base = {(x["sl"], x["r"], x["dur"]): x for x in gbase}
    out = []
    for sl in SL_GRID:
        for r in R_GRID:
            for du in DURS:
                xs = [x for x in res if x["enough"] and (x["st"]["sl"], x["st"]["r"], x["st"]["dur"]) == (sl, r, du)]
                if not xs:
                    continue
                mo = np.array([x["oos"]["mean_R"] for x in xs])
                out.append({"sl": sl, "r": r, "tp_pct": round(sl * r, 4), "dur": du, "n": len(xs),
                            "median_oos_R": round(float(np.median(mo)), 4), "mean_oos_R": round(float(mo.mean()), 4),
                            "share_pos_oos": round(float((mo > 0).mean()), 4),
                            "mean_ret_pct": round(float(np.mean([x["oos"]["mean_ret_pct"] for x in xs])), 4),
                            "tp_share": round(float(np.mean([x["oos"]["tp_share"] for x in xs])), 4),
                            "time_share": round(float(np.mean([x["oos"]["time_share"] for x in xs])), 4),
                            "random_base_R": (base.get((sl, r, du)) or {}).get("mean_R")})
    return sorted(out, key=lambda x: -x["median_oos_R"])


def ceiling(lab: Lab) -> dict:
    """Techo: cuánto se mueve el mercado tras la apertura y qué daría una horquilla de 1,5 % y 3 % (24 h, entrada a
    +15 min) con la dirección perfecta frente a la dirección al azar."""
    v = np.flatnonzero(lab.valid)
    if not len(v) or lab.U is None:
        return {}
    up, dn = lab.F["fut_up24"][v], lab.F["fut_dn24"][v]
    opn = v // lab.nc
    any3 = np.zeros(lab.no, bool)
    np.logical_or.at(any3, opn, (np.maximum(up, dn) >= 3))
    ki = KEY_IDX[("t", 15)]
    tp = []
    for di in (0, 1):
        u = lab.slot_u[v, ki, di]
        has = u >= 0
        res = lab.resolve(np.where(has, u, 0), 1.5, 2.0, "24h")
        tp.append(has & (res["why"] == 1))
    oracle = tp[0] | tp[1]
    rnd = (tp[0].astype(float) + tp[1]) / 2
    return {"events": int(len(v)), "openings": int(len(np.unique(opn))),
            "share_openings_any_coin_move_ge_3_24h": round(float(any3[np.unique(opn)].mean()), 4),
            "share_events_move_ge_3_24h": round(float((np.maximum(up, dn) >= 3).mean()), 4),
            "bracket_1_5_3_oracle_tp_share": round(float(oracle.mean()), 4),
            "bracket_1_5_3_random_tp_share": round(float(rnd.mean()), 4),
            "bracket_1_5_3_long_tp_share": round(float(tp[0].mean()), 4),
            "bracket_1_5_3_short_tp_share": round(float(tp[1].mean()), 4)}


def global_baseline(lab: Lab, reps: int) -> list[dict]:
    """Base aleatoria de referencia: todas las ventanas y monedas, entrada a +15 min, cada horquilla y duración."""
    out = []
    for sl in SL_GRID:
        for r in R_GRID:
            for du in DURS:
                pool = null_pool(lab, ("todas", "todas", ("t", 15), sl, r, du), reps)
                means = np.array([p.mean() for p in pool if len(p)])
                n = np.array([len(p) for p in pool])
                if not len(means):
                    continue
                out.append({"sl": sl, "r": r, "dur": du, "reps": len(means), "trades_mean": round(float(n.mean()), 1),
                            "mean_R": round(float(means.mean()), 4), "p5_R": round(_q(means, 0.05), 4),
                            "p95_R": round(_q(means, 0.95), 4), "share_reps_positive": round(float((means > 0).mean()), 4)})
    return out


def sensitivity(lab: Lab, st: dict) -> dict:
    """R medio fuera de muestra con supuestos de ejecución distintos: objetivo con un simple toque, stops de SOL y XRP
    con 30 pb extra, y solo largos (lo único ejecutable en contado USDC); y los cortos en perpetuo con funding."""
    oos_span = lab.t_to - lab.split_ms
    mo = lambda tr: _small(metrics(lab, tr, lab.oos[tr["e"]], oos_span, "oos")) if len(tr["e"]) else {"trades": 0}
    out = {"objetivo_con_toque": mo(lab.trades(st, touch=True)), "solo_largos": mo(lab.trades(st, side=1))}
    U = lab.U
    if U is not None and "c" in U:
        alt = np.isin(U["c"], [lab.coins.index(c) for c in ALT_COINS if c in lab.coins])
        old = U["sx"]
        U["sx"] = np.where(alt, np.float32(30 / 1e4), old).astype(np.float32)
        try:
            out["stops_sol_xrp_30pb"] = mo(lab.trades(st))
        finally:
            U["sx"] = old
    out["cortos"] = short_side(lab, st, lab.trades(st))
    return out


def report_entry(r: dict, lab: Lab, with_trades: bool = False) -> dict:
    st = r["st"]
    out = {"id": st["id"], "name": st["name"], "family": st["family"], "class": r["class"],
           "rules": {"sl_pct": st["sl"], "r": st["r"], "tp_pct": round(st["sl"] * st["r"], 4), "dur": st["dur"],
                     "break_even": False, "trailing": False},
           "spec": {k: st[k] for k in ("src", "conf", "vol", "btc", "win", "coins", "dia", "entry")},
           "is": r["is"], "oos": r["oos"], "all_weeks": r.get("all_weeks"), "by_year": r["by_year"],
           "q_bh": r.get("q"), "q_bh_eff": r.get("q_eff"),
           "dsr": None if not math.isfinite(r.get("dsr", float("nan"))) else round(r["dsr"], 4),
           "null": r.get("null"), "p_null": r.get("p_null"), "perm": r.get("perm"), "boot_months": r.get("boot"),
           "neighbors": r.get("neighbors"), "checks": r.get("checks"), "sensitivity": sensitivity(lab, st)}
    if with_trades:
        tr = lab.trades(st)
        out["trades_oos"] = trade_rows(lab, tr)
        out["account_in_sample_selected"] = {
            f"riesgo_{_g(rk).replace(',', '_')}_lev1": account(lab, [tr], [st["sl"]], rk, 1.0) for rk in (0.5, 1.5)}
    return out


def select_for_account(res: list[dict]) -> list[dict]:
    """Robustas y prometedoras para la cuenta: primero las robustas, por R medio fuera de muestra; como mucho
    MAX_ACCOUNT y sin repetir la misma señal y entrada con otra horquilla."""
    good = sorted([r for r in res if r["class"] in ("robusta", "prometedora")],
                  key=lambda r: (r["class"] != "robusta", -r["oos"]["mean_R"]))
    out, seen = [], set()
    for r in good:
        st = r["st"]
        k = (st["src"], st["conf"], st["entry"], st["win"], st["coins"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= MAX_ACCOUNT:
            break
    return out


def _acc_text(a: dict) -> str:
    return (f"media {_sg(a['mean_pct'], 3)} % al día, días ≥ +1 % {_pc(a['share_ge_1'])}, ≥ +3 % "
            f"{_pc(a['share_ge_3'])}, peor día {_sg(a['worst_pct'])} %, caída máxima {_num(a['max_dd_pct'])} % y total "
            f"{_sg(a['total_pct'])} %")


def verdict(rep: dict) -> list[str]:
    S, out = rep["summary"], []
    per, prm = rep["period"], rep["params"]
    sx = prm.get("stop_extra_bps") or {}
    sx_main = _g(sx.get("BTC", STOP_EXTRA * 1e4))
    sx_alt = _g(sx.get("SOL", STOP_EXTRA_ALT * 1e4))
    fz = rep.get("frozen") or {}
    out.append(f"Se probaron {S['tested']} estrategias inventadas que cumplen tus reglas (stop ≥ 1,5 %, objetivo ≥ 2R y "
               f"sin break-even) en {rep['data']['openings']} aperturas reales × {len(COINS)} monedas, de {per['from']} a "
               f"{per['to']} (periodo elegido por ti: desde {per.get('desde') or per['from'][:7]}). Muestra hasta el "
               f"{per['split']} (60 %) y fuera de muestra desde ahí (40 %), con costes: 0,10 % por lado, 2 pb de "
               f"deslizamiento, {sx_main} pb más en los stops de BTC y ETH y {sx_alt} pb en los de SOL y XRP; el objetivo "
               f"solo cuenta si el precio lo supera en {_g(TP_THROUGH * 1e4)} pb.")
    cmp_ = rep.get("compare") or {}
    cper = cmp_.get("period") or {}
    out.append("Aviso de honestidad: el arranque del periodo es un grado de libertad más y se eligió con una pasada "
               "anterior a la vista"
               + (f" (desde {cper.get('from')}, con el corte en {cper.get('split')}), que ya probó las mismas "
                  "estrategias con un fuera de muestra que contiene todo el de ahora" if cper.get("from") else "")
               + ". Las pasadas no son confirmaciones independientes: el único fuera de muestra virgen es el paper desde "
               f"{fz.get('date') or 'hoy'} (la lista y los criterios quedan congelados con esa fecha).")
    if cmp_:
        parts = []
        for key, who in (("current_promising_there", "las prometedoras de hoy"),
                         ("current_top10_there", "las 10 mejores de hoy fuera de muestra")):
            g = cmp_.get(key) or {}
            if g.get("with_is"):
                parts.append(f"{who} ({g['with_is']}) dieron allí una mediana de {_sg(g['median_is_R'], 3)} R en su "
                             f"muestra ({cper.get('from')} a {cper.get('split')}) y {g['positive_is']} salieron "
                             "positivas")
        wf26 = (cmp_.get("wf_min100_by_year") or {}).get("2026")
        acc = cmp_.get("honest_account_0_5") or {}
        if parts:
            out.append(f"Pasada anterior desde {cper.get('from')} (mismas estrategias, código anterior; solo referencia, "
                       "no entra en ninguna cifra de esta pasada): " + "; ".join(parts)
                       + (f"; allí la selección en avance de 2026 dio {_sg(wf26, 3)} R" if wf26 is not None else "")
                       + (f" y la cuenta honesta con 0,5 % de riesgo {_sg(acc['total_pct'])} % en {acc['days']} días"
                          if acc.get("total_pct") is not None else "")
                       + f". Prometedoras en las dos pasadas: {cmp_.get('promising_both', 0)}.")
    rg = (rep["data"].get("regime") or {})
    if rg:
        txt = lambda k: ", ".join(f"{c} {_sg(v[k], 0)} %" for c, v in rg.items() if v.get(k) is not None)
        out.append(f"Régimen: en la muestra {txt('is_pct')}; fuera de muestra {txt('oos_pct')}. Con la tendencia a "
                   "favor de un lado, una estrategia con más cortos (o más largos) gana solo por la tendencia; por eso "
                   "la base emparejada conserva la cuota de largos de cada estrategia.")
    out.append(f"{S['enough_trades']} tienen al menos {MIN_TRADES} operaciones fuera de muestra. Positivas dentro de "
               f"muestra: {S['positive_is']}; fuera: {S['positive_oos']}; en las dos: {S['positive_both']}. Baten a su "
               f"base emparejada (mismos eventos y filtros, dirección al azar por apertura, con su cuota de largos y "
               f"al 50 %): {S['beat_base']} de {S['base_tested']} (por azar, como mucho un 5 %: unas "
               f"{S['expected_by_chance_all']}); de las positivas en muestra, {S['beat_base_pos_is']} de "
               f"{S['base_tested_pos_is']} (por azar unas {S['expected_by_chance']}). Prometedoras (además siguen "
               f"positivas fuera sin su mejor mes, operan en al menos {MIN_WEEKS_OOS} semanas fuera y {MIN_WEEKS_ALL} en "
               f"total y ningún año de la muestra sale negativo): {S['promising']}. Como las {S['tested']} comparten el "
               f"mismo periodo, estos recuentos son una sola observación del régimen, no {S['tested']} pruebas "
               "independientes.")
    if S["robust"]:
        names = "; ".join(f"«{x['name']}» (R medio fuera {_sg(x['oos']['mean_R'], 3)}, {x['oos']['trades']} op.)"
                          for x in rep["robust"][:3])
        out.append(f"{S['robust']} estrategia(s) pasan TODAS las pruebas contra la suerte (base emparejada, permutación "
                   f"por bloques, Benjamini-Hochberg al 10 %, Sharpe deflactado con {_g(rep['stats']['n_eff'])} pruebas "
                   f"efectivas, vecinos dentro y fuera y cada año fuera de muestra): {names}. Aun así son una sola "
                   "historia elegida con el periodo a la vista: solo el paper desde la fecha de congelación dirá algo.")
    else:
        out.append("Ninguna estrategia pasa todas las pruebas contra la suerte (base emparejada, permutación por bloques, "
                   f"Benjamini-Hochberg al 10 %, Sharpe deflactado con {_g(rep['stats']['n_eff'])} pruebas efectivas de "
                   f"{S['tested']}, vecinos de la rejilla dentro y fuera y positiva en cada año fuera de muestra): con "
                   "estos datos no hay ninguna que se pueda dar por buena.")
    # Lo honesto primero: qué habría dado fiarse del laboratorio en tiempo real.
    wfa = rep.get("walk_forward") or {}
    for key, label in (("account_quarter", "cada trimestre se eligen las 10 mejores con todo lo anterior"),
                       ("account", "cada año se eligen las 10 mejores con los años anteriores")):
        wa = wfa.get(key) or {}
        w1, w3 = wa.get("riesgo_0_5_lev1") or {}, wa.get("riesgo_1_5_lev1") or {}
        if w1.get("days"):
            sk = (w3.get("skipped") or {}).get("sin_capital") or 0
            out.append(f"Cuenta HONESTA en avance ({label}, con al menos 100 operaciones; 10.000 USDC, una posición por "
                       f"moneda), desde {wa['from']}, {w1['days']} días: con 0,5 % de riesgo, {_acc_text(w1)}"
                       + (f"; con 1,5 % de riesgo, total {_sg(w3['total_pct'])} % y caída máxima "
                          f"{_num(w3['max_dd_pct'])} %" + (f" (el tope de exposición deja fuera {sk} operaciones por "
                                                           "falta de capital: esa cifra depende mucho de cuáles entran "
                                                           "primero)" if sk > w3.get("trades", 0) / 2 else "")
                          + "." if w3.get("days") else "."))
    wq = wfa.get("quarterly") or {}
    wy = wfa.get("min100") or []
    if wq.get("quarters") or wy:
        parts = [f"{w['year']}: {_sg(w['test_mean_R'], 3)} ({w['test_positive']} de {w['top']} positivas)" for w in wy]
        out.append("Selección en avance, R medio de las elegidas en el periodo siguiente: por años, " + ("; ".join(parts)
                   or "sin datos") + (f"; por trimestres, {_sg(wq['test_mean_R'], 3)} R en {wq['test_trades']} "
                                      f"operaciones ({wq['quarters_positive']} de {len(wq['quarters'])} trimestres "
                                      "positivos)" if wq.get("test_mean_R") is not None else "") + ".")
    if rep["promising"]:
        x = rep["promising"][0]
        fails = [k.replace("_", " ") for k, v in (x.get("checks") or {}).items() if not v]
        se = x.get("sensitivity") or {}
        lo = (se.get("solo_largos") or {})
        tc = (se.get("objetivo_con_toque") or {})
        out.append(f"Prometedoras: {S['promising']} (NO son una ventaja demostrada ni se proponen para operar: el "
                   "criterio no se fijó antes de ver estos datos; quedan congeladas para medirlas en paper). La mejor "
                   f"fuera de muestra es «{x['name']}»: R medio {_sg(x['oos']['mean_R'], 3)} en {x['oos']['trades']} "
                   f"operaciones ({_sg(x['oos']['mean_ret_pct'], 3)} % por operación, {_pc(x['oos']['win_rate'], 0)} "
                   f"ganadoras, {_pc(x['oos'].get('long_share') or 0, 0)} largos), su base emparejada llega a "
                   f"{_sg((x.get('null') or {}).get('p95_max') or 0, 3)} (p95); solo largos "
                   f"{_sg(lo.get('mean_R') or 0, 3)} R en {lo.get('trades') or 0} op.; con un simple toque del objetivo "
                   f"{_sg(tc.get('mean_R') or 0, 3)} R; falla: {', '.join(fails) or 'nada'}."
                   + ("".join(f" Otra: «{y['name']}»: R medio {_sg(y['oos']['mean_R'], 3)} en {y['oos']['trades']} "
                              f"op., {_pc(y['oos'].get('long_share') or 0, 0)} largos, permutación por bloques p "
                              f"{_num((y.get('perm') or {}).get('p') or 1, 3) if (y.get('perm') or {}).get('p') is not None else 'no aplicable'}"
                              f", q {_num(y.get('q_bh') or 1, 3)}; falla: "
                              f"{', '.join(k.replace('_', ' ') for k, v in (y.get('checks') or {}).items() if not v) or 'nada'}."
                              for y in rep["promising"][1:4])))
    if S.get("promising"):
        out.append(f"Cortos: el {_pc(S['short_share_promising'] or 0, 0)} de las operaciones fuera de muestra de las "
                   "prometedoras son cortos, que en contado USDC no se pueden hacer (haría falta X-Perps de OKX EU, sin "
                   f"confirmar). Solo con los largos, {S['promising_long_only_positive']} de {S['promising']} siguen "
                   "positivas fuera de muestra.")
    acc = (rep.get("account") or {}).get("combined") or {}
    a1 = acc.get("riesgo_0_5_lev1") or {}
    if a1.get("days"):
        out.append(f"Cuenta EN MUESTRA (elegidas con estos mismos datos: NO es una previsión ni una prueba; la honesta es "
                   f"la de arriba) con las {len(rep['account']['strategies'])} prometedoras o robustas, 10.000 USDC, "
                   f"fuera de muestra, {a1['days']} días, 0,5 % de riesgo: {_acc_text(a1)}.")
    gb = rep["baseline"]["global"]
    if gb:
        mr = [x["mean_R"] for x in gb]
        out.append(f"Base aleatoria global (solo referencia de costes: dirección al azar por apertura, entrada a +15 min, "
                   f"sin filtros ni fuente): R medio fuera de muestra de {_sg(min(mr), 3)} a {_sg(max(mr), 3)} según la "
                   "horquilla.")
    ce = rep.get("ceiling") or {}
    if ce:
        out.append(f"Tu idea de los +3 %: en el {_pc(ce['share_openings_any_coin_move_ge_3_24h'], 0)} de las aperturas "
                   f"alguna de las 4 monedas llega a moverse ≥ 3 % en las 24 h siguientes, y con la dirección perfecta "
                   f"una horquilla de 1,5 % / 3 % habría cobrado el objetivo en el "
                   f"{_pc(ce['bracket_1_5_3_oracle_tp_share'], 0)} de los casos (por moneda), frente al "
                   f"{_pc(ce['bracket_1_5_3_random_tp_share'], 0)} con dirección al azar. El movimiento existe; lo "
                   "difícil, y lo que miden estas pruebas, es saber de antemano hacia dónde.")
    ex = rep.get("exits") or []
    if ex:
        b = ex[0]
        t3 = [x for x in ex if x["sl"] == 1.5 and x["r"] == 2.0]
        out.append(f"Horquillas (¿3 % de objetivo o más ancho?): la mejor mediana fuera de muestra es SL {_g(b['sl'])} % y "
                   f"TP {_g(b['tp_pct'])} % ({DUR_TEXT[b['dur']]}), con R medio mediano {_sg(b['median_oos_R'], 3)} y "
                   f"{_pc(b['share_pos_oos'], 0)} positivas"
                   + (f"; con tu objetivo de 3 % (SL 1,5 % × 2R) la mediana va de "
                      f"{_sg(min(x['median_oos_R'] for x in t3), 3)} a {_sg(max(x['median_oos_R'] for x in t3), 3)} según "
                      "la duración" if t3 else "")
                   + "." + (" Con dirección al azar todas las horquillas pierden (base global): la horquilla sola no "
                            "crea ventaja; solo cambia cuánto se paga en costes y cuántas operaciones acaban por tiempo."
                            if gb and max(x["mean_R"] for x in gb) < 0 else ""))
    fams = [f for f in rep["families"] if f.get("median_oos_R") is not None]
    if fams:
        b = fams[0]
        share = S["beat_base"] / S["base_tested"] if S.get("base_tested") else 0.0
        out.append(f"Por familias, la que mejor sale fuera de muestra es «{b['name']}» (mediana del R medio "
                   f"{_sg(b['median_oos_R'], 3)}, {_pc(b['share_pos_oos'] or 0, 0)} positivas); la peor, "
                   f"«{fams[-1]['name']}» ({_sg(fams[-1]['median_oos_R'], 3)}). "
                   + (f"Baten a su base más estrategias de las esperables por azar ({_pc(share)} frente a ≤ 5 %): las "
                      "características llevan algo de información en este periodo, pero no es una ventaja de apertura "
                      "demostrada (un solo régimen, el mismo para todas)." if share > 0.08 else
                      f"No baten a su base más estrategias de las esperables por azar ({_pc(share)} frente a ≤ 5 %): "
                      "en este periodo las características no muestran información útil sobre la dirección."))
    mw = (rep["data"].get("sources") or {}).get("macro", {}).get("in_windows") or {}
    if mw and any(r_["family"] == "macro" for r_ in rep.get("all") or []):
        used = ", ".join(f"{k} {v}" for k, v in mw.items() if v)
        out.append(f"Familia macro: aperturas con el dato en su franja (de 4 h antes a 1 h después): {used or 'ninguna'}."
                   + (" El FOMC (14:00 ET) no cae en ninguna franja: la familia macro solo prueba IPC y empleo."
                      if not mw.get("FOMC") else ""))
    out.append(f"Salidas por tiempo: el {_pc(S['time_share_promising'] or 0)} de las operaciones de las prometedoras "
               "se cierra por tiempo, sin tocar ni el objetivo ni el stop; ahí no se cumple el «2R o −1R» y se reporta "
               "aparte (time_share y time_mean_R).")
    return out


def summary_block(res: list[dict], info: dict | None = None) -> dict:
    info = info or {}
    S = {"tested": len(res), "enough_trades": sum(r["enough"] for r in res),
         "positive_is": sum(r["pos_is"] for r in res), "positive_oos": sum(r["pos_oos"] for r in res),
         "positive_both": sum(r["pos_is"] and r["pos_oos"] for r in res),
         "beat_base": sum(bool(r.get("beats_base")) for r in res),
         "beat_base_pos_is": sum(bool(r.get("beats_base")) and r["pos_is"] for r in res),
         "base_tested": info.get("base_tested", sum(r["enough"] for r in res)),
         "base_tested_pos_is": info.get("base_tested_pos_is", sum(r["enough"] and r["pos_is"] for r in res)),
         "promising": sum(r["class"] == "prometedora" for r in res),
         "robust": sum(r["class"] == "robusta" for r in res), "min_trades_oos": MIN_TRADES,
         "min_weeks_oos": MIN_WEEKS_OOS, "min_weeks_all": MIN_WEEKS_ALL}
    # Nulo empírico: cada estrategia tiene su propia base; si ninguna tuviera ventaja, como mucho el 5 % la batiría.
    S["expected_by_chance_all"] = int(round(S["base_tested"] * (1 - NULL_Q)))
    S["expected_by_chance"] = int(round(S["base_tested_pos_is"] * (1 - NULL_Q)))
    pr = [r for r in res if r["class"] in ("prometedora", "robusta")]
    S["time_share_promising"] = round(float(np.mean([r["oos"]["time_share"] for r in pr])), 4) if pr else None
    n_tr = sum(r["oos"]["trades"] for r in pr)
    S["short_share_promising"] = round(1 - sum(r["oos"]["longs"] for r in pr) / n_tr, 4) if n_tr else None
    return S


def summary_text(rep: dict) -> list[str]:
    L = [f"Laboratorio de aperturas · {rep['period']['from']} a {rep['period']['to']} · {rep['summary']['tested']} "
         f"estrategias · fuera de muestra desde {rep['period']['split']}", ""]
    for x in rep["top50"][:10]:
        o = x["oos"]
        L.append(f"  [{x['class']}] R {_sg(o['mean_R'], 3)} ({o['trades']} op., {_pc(o['win_rate'], 0)}, q "
                 f"{_num(x['q_bh'] or 1, 3)}) · {x['name']}")
    L.append("")
    L.extend(rep["verdict"])
    return L


# ---------------------------------------------------------------- otra pasada y lista congelada

def compare_run(path: Path, rep: dict) -> dict | None:
    """Resumen de otra pasada del laboratorio (por ejemplo desde 2021-01) con las mismas estrategias, solo como
    referencia: cómo salieron allí las prometedoras y las mejores de esta pasada, y su selección en avance."""
    other = _read_json(Path(path))
    if not isinstance(other, dict) or not other.get("all"):
        return None
    if (other.get("period") or {}).get("from") == rep["period"].get("from"):
        return None
    idx = {x["id"]: x for x in other["all"]}

    def group(ids: list[str]) -> dict:
        xs = [idx[i] for i in ids if i in idx]
        isr = [x["is"]["mean_R"] for x in xs if (x.get("is") or {}).get("trades")]
        oor = [x["oos"]["mean_R"] for x in xs if (x.get("oos") or {}).get("trades")]
        return {"n": len(ids), "found": len(xs), "with_is": len(isr),
                "median_is_R": round(float(np.median(isr)), 4) if isr else None,
                "positive_is": sum(v > 0 for v in isr), "with_oos": len(oor),
                "median_oos_R": round(float(np.median(oor)), 4) if oor else None,
                "positive_oos": sum(v > 0 for v in oor)}

    prom = [x["id"] for x in rep.get("promising") or []]
    other_prom = [x["id"] for x in other.get("promising") or []]
    wf = {str(w["year"]): w["test_mean_R"] for w in (other.get("walk_forward") or {}).get("min100") or []}
    acc = ((other.get("walk_forward") or {}).get("account") or {}).get("riesgo_0_5_lev1") or {}
    return {"file": Path(path).name, "period": other.get("period"), "version": other.get("version"),
            "same_strategies": len({x["id"] for x in rep.get("all") or []} & set(idx)), "tested": len(idx),
            "promising_other": len(other_prom), "promising_both": len(set(prom) & set(other_prom)),
            "current_promising_there": group(prom), "current_top10_there": group([x["id"] for x in rep["top50"][:10]]),
            "wf_min100_by_year": wf,
            "honest_account_0_5": {k: acc.get(k) for k in ("days", "total_pct", "max_dd_pct", "mean_pct")},
            "note": "Otra pasada con el código anterior (otra base, otros costes y otros criterios) y otro periodo: "
                    "solo referencia; no entra en ninguna cifra de esta pasada."}


def criteria() -> dict:
    """Criterios de esta versión (se guardan con la lista congelada)."""
    return {"version": VERSION, "is_share": IS_SHARE, "min_trades_oos": MIN_TRADES, "min_weeks_oos": MIN_WEEKS_OOS,
            "min_weeks_all": MIN_WEEKS_ALL, "min_year_is_trades": MIN_YEAR_IS, "null_quantile": NULL_Q,
            "p_perm": P_PERM, "fdr_q": FDR_Q, "dsr_min": DSR_MIN, "hac_lags": HAC_LAGS,
            "tp_through_bps": TP_THROUGH * 1e4, "slip_bps": SLIP * 1e4, "fee_pct": config.FEE_PCT,
            "prometedora": "positiva dentro y fuera, bate el p95 de sus dos bases emparejadas, positiva fuera sin su "
                           "mejor mes, semanas mínimas y ningún año de la muestra negativo",
            "robusta": "prometedora + permutación por bloques p < 0,05 + BH q ≤ 0,10 + Sharpe deflactado ≥ 0,95 + "
                       "vecinos positivos dentro y fuera + positiva en cada año fuera"}


def freeze_info(rep: dict, ids: list[str], day: str) -> dict:
    """Lista de estrategias y criterios con fecha: desde aquí, el paper es el único fuera de muestra virgen."""
    ids = sorted(ids)
    try:
        code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    except OSError:
        code = None
    pick = lambda xs: [{"id": x["id"], "name": x["name"], "class": x["class"]} for x in xs]
    return {"date": day, "strategies": len(ids), "strategies_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
            "code_sha256_16": code, "period": rep["period"], "criteria": criteria(),
            "params": {k: rep["params"].get(k) for k in ("stop_extra_bps", "n_null", "n_perm")},
            "robust": pick(rep.get("robust") or []), "promising": pick(rep.get("promising") or []),
            "rule": "Lista y criterios congelados en esta fecha. El único fuera de muestra virgen es el paper desde aquí: "
                    "no se cambian después de ver esos datos. Otra pasada el mismo día queda numerada aparte."}


def write_frozen(rep: dict, stem: str) -> Path | None:
    """Guarda la lista congelada sin pisar otra del mismo día (-2, -3…: así se ve si hubo varias pasadas)."""
    fz = rep.get("frozen")
    if not fz:
        return None
    base = config.BACKTEST_DIR / f"{stem}-congelado-{fz['date']}.json"
    path, k = base, 1
    while path.exists():
        k += 1
        path = base.with_name(f"{base.stem}-{k}.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_clean(fz), ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- principal

def split_point(times: np.ndarray) -> tuple[int, str]:
    """Corte en la medianoche de Madrid tras el 60 % de las aperturas."""
    t60 = int(np.sort(times)[int(len(times) * IS_SHARE)])
    ms = paper.next_midnight_ms(t60 - 1)
    return ms, datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def _pool(jobs: list[tuple], procs: int) -> list[dict]:
    if procs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=min(procs, len(jobs))) as ex:
            return list(ex.map(coin_job, jobs))
    return [coin_job(j) for j in jobs]


def easter(y: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano anónimo)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def ash_wednesday(y: int) -> date:
    """Miércoles de ceniza: B3 abre ese día a las 13:00 de São Paulo, no a las 10:00."""
    return easter(y) - timedelta(days=46)


def special_session(o: dict) -> bool:
    """Aperturas que el calendario general da y que no lo son a esa hora (no se usan)."""
    if o["id"] != "saopaulo":
        return False
    d = date.fromisoformat(o["day"])
    return d == ash_wednesday(d.year)


def regime(tabs: dict[str, dict], t_from: int, split_ms: int, t_to: int) -> dict:
    """Cambio de precio (%) de cada moneda en la muestra y fuera de muestra (cierres diarios UTC)."""
    out = {}
    for c, t in tabs.items():
        dl = t.get("daily") or {}
        if not dl:
            continue
        ks = sorted(dl)
        at = lambda ms: dl[ks[max(0, min(len(ks) - 1, int(np.searchsorted(ks, ms // DAY, side="right")) - 1))]]
        a, s, b = at(t_from), at(split_ms), at(t_to)
        out[c] = {"is_pct": round((s / a - 1) * 100, 1), "oos_pct": round((b / s - 1) * 100, 1)}
    return out


def build_lab(desde: str | None, procs: int, today: date | None = None,
              stop_extra: dict[str, float] | None = None) -> tuple[Lab, dict]:
    """Aperturas reales, bolsas previas, fuentes opcionales y las tablas de cada moneda (en paralelo)."""
    files = {c: month_files(c, desde) for c in COINS}
    if not any(files.values()):
        raise SystemExit(f"No hay velas de 1 min en la caché ({bt.dir_1m()}): descárgalas antes (backtest_aperturas o "
                         "el agente de historia).")
    first = min(Path(p).name.split("-1m-")[1][:7] for fs in files.values() for p in fs[:1])
    start = date.fromisoformat((desde or first) + "-01")
    today = today or datetime.now(timezone.utc).date()
    idx = bt.index_data()
    all_ops = [o for o in bt.build_openings(start, today - timedelta(days=1), idx) if o["id"] in WIN_IDS]
    dropped = [f"{o['id']} {o['day']}" for o in all_ops if special_session(o)]
    openings = [o for o in all_ops if not special_session(o)]
    if len(openings) < 50:
        raise SystemExit("Muy pocas aperturas en el periodo pedido.")
    log.info("%d aperturas de %s a %s (%d sesiones especiales fuera)", len(openings), openings[0]["day"],
             openings[-1]["day"], len(dropped))
    meta = opening_meta(openings)
    feats = chain_table(openings, idx)
    fng, gd = load_fng(), load_gdelt()
    macro, macro_names = load_macro(with_names=True)
    fund = {c: load_funding(c) for c in COINS}
    feats.update(fng_features(meta["open_ms"], fng))
    feats["gd_z"] = gdelt_features(meta["open_ms"], gd)
    coin_feats = {c: {"fund_pct": funding_features(meta["open_ms"], fund[c])} for c in COINS}
    op = {k: meta[k] for k in ("open_ms", "next_same_ms", "prev_same_ms", "prev_ms", "prev2_ms", "prev_sess")}
    jobs = [(c, [str(p) for p in files[c]], op, macro) for c in COINS if files[c]]
    t0 = time.time()
    log.info("Tablas de eventos de %d monedas en %d procesos…", len(jobs), procs)
    tabs = {r["coin"]: r for r in _pool(jobs, procs)}
    log.info("Tablas hechas en %.0f s (%s)", time.time() - t0, ", ".join(f"{c} {r['secs']} s" for c, r in tabs.items()))
    closes = {}
    for r in history.load_klines("BTC", "1d"):
        closes[int(r[0]) // DAY] = float(r[4])
    for d, v in ((tabs.get("BTC") or {}).get("daily") or {}).items():
        closes.setdefault(d, v)
    feats.update(btc_trend(meta["open_ms"], closes))
    split_ms, _ = split_point(meta["open_ms"])
    lab = Lab(meta, feats, tabs, split_ms, coin_feats, stop_extra=stop_extra)
    lab.funding = {i: fund[c] for i, c in enumerate(lab.coins) if fund.get(c) is not None}
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else None
    # Una fuente opcional cuenta si cubre al menos la mitad del periodo (si no, su familia no se prueba y su cupo
    # pasa a las demás: con datos solo al principio no habría nada fuera de muestra).
    t_a, t_b = int(meta["open_ms"].min()), int(meta["open_ms"].max())
    share = lambda a, b: max(0, min(b, t_b) - max(a, t_a)) / max(1, t_b - t_a)
    iso_ms = lambda s_: int(datetime.fromisoformat(s_).replace(tzinfo=timezone.utc).timestamp() * 1000)
    cov = {"fng": share(iso_ms(min(fng)), iso_ms(max(fng)) + DAY) if fng else 0.0,
           "funding": max([share(int(f[0, 0]), int(f[-1, 0])) for f in fund.values() if f is not None] or [0.0]),
           "macro": share(int(macro[0]), int(macro[-1])) if macro is not None else 0.0,
           "gdelt": max([share(int(g["t"][0]), int(g["t"][-1]) + g["step"]) for g in gd] or [0.0]) if gd else 0.0}
    avail = {k for k, v in cov.items() if v >= SOURCE_COVER_MIN}
    cover = {"coins": {c: {"from": day(t["span"][0]), "to": day(t["span"][1]), "bars": t["span"][2],
                           "valid_share": round(t["span"][3], 4), "events_ok": int(t["ok"].sum()),
                           "files": len(files[c])} for c, t in tabs.items()},
             "sources": {"fng": {"present": bool(fng), "from": min(fng) if fng else None, "to": max(fng) if fng else None},
                         "funding": {c: ({"present": True, "from": day(int(f[0, 0])), "to": day(int(f[-1, 0])),
                                          "n": len(f)} if f is not None else {"present": False}) for c, f in fund.items()},
                         "macro": {"present": macro is not None, "n": 0 if macro is None else len(macro),
                                   "from": day(int(macro[0])) if macro is not None else None,
                                   "to": day(int(macro[-1])) if macro is not None else None,
                                   "in_windows": macro_in_windows(meta["open_ms"], macro, macro_names)},
                         "gdelt": {"present": bool(gd), "series": [g["slug"] for g in gd or []],
                                   "note": "Una resolución por consulta; cada tramo se da por conocido al cerrar más "
                                           "30 min (1 h si es horario o diario). GDELT revisa sus series hacia atrás."},
                         "btc_daily": {"days": len(closes)},
                         "coverage_of_period": {k: round(v, 4) for k, v in cov.items()}},
             "openings": len(openings), "openings_dropped": dropped, "available": sorted(avail),
             "families_skipped": [f for f, (_, _, need) in FAMILIES.items() if need and need not in avail],
             "regime": regime(tabs, lab.t_from, split_ms, lab.t_to - DAY)}
    return lab, cover


def run_lab(desde: str | None = None, procs: int = 6, max_n: int = 2000, n_null: int = N_NULL,
            n_perm: int = N_PERM, today: date | None = None, stop_extra: dict[str, float] | None = None,
            compare: str | Path | None = None) -> dict:
    t_all = time.time()
    lab, cover = build_lab(desde, procs, today, stop_extra)
    strategies = generate(set(cover["available"]), max_n)
    log.info("Evaluando %d estrategias…", len(strategies))
    res = evaluate_all(lab, strategies)
    info = classify(lab, res, n_null, n_perm, len(strategies))
    log.info("Base aleatoria global…")
    gbase = global_baseline(lab, max(50, n_null // 2))
    S = summary_block(res, info)
    ranked = sorted([r for r in res if r["enough"]], key=lambda r: -r["oos"]["mean_R"])
    robust = [r for r in ranked if r["class"] == "robusta"]
    prom = [r for r in ranked if r["class"] == "prometedora"]
    sel = select_for_account(res)
    combined = {}
    if sel:
        trs = [lab.trades(r["st"]) for r in sel]
        sls = [r["st"]["sl"] for r in sel]
        combined = {f"riesgo_{_g(rk).replace(',', '_')}_lev1": account(lab, trs, sls, rk, 1.0) for rk in (0.5, 1.5)}
    t_from = datetime.fromtimestamp(lab.t_from / 1000, MAD).date().isoformat()
    t_to = datetime.fromtimestamp((lab.t_to - DAY) / 1000, MAD).date().isoformat()
    sx = {c: round(v * 1e4, 2) for c, v in lab.stop_extra.items()}
    rep = {"generated": now_ms(), "version": VERSION,
           "period": {"from": t_from, "to": t_to, "split": lab.split_day, "is_share": IS_SHARE, "desde": desde},
           "params": {"fee_pct": config.FEE_PCT, "slip_bps": SLIP * 1e4, "stop_extra_bps": sx,
                      "tp_through_bps": TP_THROUGH * 1e4, "fee_perp_pct": FEE_PERP * 100,
                      "sl_grid": SL_GRID, "r_grid": R_GRID, "durations": DUR_TEXT, "max_hours_safety": HMAX / 60,
                      "min_trades_oos": MIN_TRADES, "min_weeks_oos": MIN_WEEKS_OOS, "min_weeks_all": MIN_WEEKS_ALL,
                      "min_year_is_trades": MIN_YEAR_IS, "fdr_q": FDR_Q, "n_null": n_null, "n_perm": n_perm,
                      "p_perm": P_PERM, "dsr_min": DSR_MIN, "null_quantile": NULL_Q, "hac_lags": HAC_LAGS,
                      "n_boot": N_BOOT, "max": max_n,
                      "entries": [list(k) for k in KEYS], "close_margin_min": backtest.CLOSE_MARGIN_MS // MIN,
                      "pessimistic": "si una vela toca stop y objetivo, cuenta el stop; en la vela de una entrada "
                                     "límite solo cuenta el stop; el objetivo exige superarlo en 2 pb",
                      "account": {"equity": 10_000, "risk_pct": [0.5, 1.5], "leverage": [1, 2]}},
           "data": cover, "summary": S,
           "baseline": {"global": gbase,
                        "note": "La base de cada estrategia es la EMPAREJADA (sus mismos eventos y filtros, dirección al "
                                "azar por apertura pasando por su confirmación y su entrada, con su cuota de largos y al "
                                "50 %): «bate a la base» si su R medio fuera de muestra supera el percentil 95 de las "
                                "dos. La global (todas las ventanas y monedas, entrada a +15 min) es solo una "
                                "referencia de costes."},
           "ceiling": ceiling(lab),
           "top50": [report_entry(r, lab) for r in ranked[:50]],
           "robust": [report_entry(r, lab, True) for r in robust],
           "promising": [report_entry(r, lab, True) for r in prom[:40]],
           "account": {"strategies": [r["st"]["id"] for r in sel], "names": [r["st"]["name"] for r in sel],
                       "combined": combined,
                       "warning": "EN MUESTRA: estas estrategias se eligieron con estos mismos datos (positivas fuera y "
                                  "baten a su base), así que su cuenta fuera de muestra está inflada por la selección. "
                                  "La estimación honesta es walk_forward.account_quarter y walk_forward.account.",
                       "note": "Una posición por moneda a la vez entre todas (si dos entran a la vez, manda el puesto "
                               "en la selección); nominal = capital × riesgo / stop; exposición total ≤ 100 %. "
                               "Resultado apuntado el día en que se cierra cada operación."},
           "families": family_table(res),
           "exits": exit_table(res, gbase),
           "walk_forward": {"min30": walk_forward(lab, res, 10, MIN_TRADES), "min100": walk_forward(lab, res, 10, 100),
                            "quarterly": walk_forward_q(lab, res, 10, 100),
                            "quarterly_min30": walk_forward_q(lab, res, 10, MIN_TRADES),
                            "account": walk_forward_account(lab, res, 10, 100),
                            "account_quarter": walk_forward_account(lab, res, 10, 100, "quarter")},
           "all": [{"id": r["st"]["id"], "family": r["st"]["family"], "class": r["class"], "is": _small(r["is"]),
                    "oos": _small(r["oos"]), "q": r.get("q"), "beats_base": bool(r.get("beats_base")),
                    "p_base": r.get("p_null")} for r in res],
           "stats": {"sr_var": info["sr_var"], "n_trials": info["n_trials"], "n_eff": info["n_eff"],
                     "min_q_eff": info["min_q_eff"], "perm_candidates": info["cands"],
                     "base_tested": info["base_tested"], "base_beaten": info["base_beaten"],
                     "secs": round(time.time() - t_all, 1)}}
    pr_entries = rep["robust"] + rep["promising"]
    S["promising_long_only_positive"] = sum(
        1 for x in pr_entries if ((x.get("sensitivity") or {}).get("solo_largos") or {}).get("mean_R", -1) > 0)
    S["promising_touch_positive"] = sum(
        1 for x in pr_entries if ((x.get("sensitivity") or {}).get("objetivo_con_toque") or {}).get("mean_R", -1) > 0)
    S["promising_stops30_positive"] = sum(
        1 for x in pr_entries if ((x.get("sensitivity") or {}).get("stops_sol_xrp_30pb") or {}).get("mean_R", -1) > 0)
    if compare:
        rep["compare"] = compare_run(Path(compare), rep)
    day_iso = (today or datetime.now(MAD).date()).isoformat()
    rep["frozen"] = freeze_info(rep, [s["id"] for s in strategies], day_iso)
    rep["verdict"] = verdict(rep)
    rep["notes"] = [
        "Precios de Binance al contado en USDT como aproximación a OKX EU en USDC (libro solo del EEE, menos "
        "profundo): por eso el objetivo exige superarse en 2 pb y los stops de SOL y XRP llevan 20 pb extra. Falta "
        "comprobar las prometedoras con velas de 1 min de OKX en USDC.",
        "Los cortos se simulan con los costes de contado, pero en contado USDC no se puede ir corto: son NO "
        "ejecutables mientras no esté confirmado X-Perps de OKX EU (ver sensitivity.cortos y sensitivity.solo_largos).",
        "Cada estrategia es una sola combinación de fuente, confirmación, filtros, entrada, horquilla y duración; las "
        "~2.000 se sortean de forma estratificada y determinista (misma lista en cada pasada con los mismos datos).",
        "Sin mirar al futuro: los cierres de bolsa se usan con 15 min de margen (backtest.known), el momento previo y "
        "la reacción de la apertura anterior con velas ya cerradas, el miedo y codicia del día UTC de la apertura − 1 h, "
        "el funding ya liquidado, el dato macro solo tras sus 15 primeros minutos y GDELT al cerrar cada tramo más "
        "su retraso de publicación.",
        "Los horarios supuestos (alternative.me a las 00:00 UTC, sello de liquidación del funding, IPC y empleo de "
        "bls.gov y FOMC de federalreserve.gov) se comprobaron por fuera; el control control_futuro.py solo prueba la "
        "coherencia interna (usa los mismos márgenes que el laboratorio). Las fuentes están en historia_extra.",
        "Entradas: apertura de la vela siguiente a la que confirma (ruptura, impulso, reanudación o +N min); el "
        "retesteo es una orden límite en el borde del rango tras la ruptura (caduca en 60 min).",
        "Una posición por moneda a la vez dentro de cada estrategia (por orden de entrada); las operaciones que siguen "
        "abiertas al acabarse los datos ocupan su moneda pero no cuentan como resultado.",
        "El t y el p van con la suma de R por día de aperturas (las cuatro monedas se mueven juntas), con ceros en los "
        "días sin operaciones y error típico de Newey-West con 7 días de retardo.",
        "Las aperturas de São Paulo del miércoles de ceniza (B3 abre a las 13:00) no se usan.",
        "Una operación que cruza un hueco de datos de 15 min o más se descarta.",
        "El cierre por tiempo rompe el «2R o −1R»: se reporta aparte en time_share y time_mean_R.",
        "Base emparejada, permutación y remuestreo de meses con semillas fijas: los resultados se repiten con los "
        "mismos datos.",
    ]
    return _clean(rep)


def out_path(salida: str | None) -> Path:
    name = Path(salida or "laboratorio.json").name
    if name in PROTECTED:
        raise SystemExit(f"{name} lo usan el motor o la web: elige otro nombre de salida.")
    return config.BACKTEST_DIR / name


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m bot.laboratorio",
                                 description="Laboratorio de ~2.000 estrategias de apertura con control de la suerte")
    ap.add_argument("--desde", default=None, help="primer mes AAAA-MM (por defecto, el más antiguo de la caché)")
    ap.add_argument("--procesos", type=int, default=min(6, os.cpu_count() or 1), help="procesos en paralelo")
    ap.add_argument("--max", type=int, default=2000, help="número de estrategias (por defecto 2000)")
    ap.add_argument("--salida", default="laboratorio.json", help="nombre del archivo en BACKTEST_DIR")
    ap.add_argument("--azar", type=int, default=N_NULL, help="repeticiones de la base emparejada")
    ap.add_argument("--permutaciones", type=int, default=N_PERM, help="permutaciones por estrategia candidata")
    ap.add_argument("--slip-stop", type=float, default=STOP_EXTRA * 1e4, help="pb extra en los stops de BTC y ETH")
    ap.add_argument("--slip-stop-alt", type=float, default=STOP_EXTRA_ALT * 1e4,
                    help="pb extra en los stops de SOL y XRP")
    ap.add_argument("--comparar", default=COMPARE_DEFAULT,
                    help="otra pasada en BACKTEST_DIR para comparar (solo referencia; vacío = ninguna)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = out_path(a.salida)
    sx = {c: (a.slip_stop_alt if c in ALT_COINS else a.slip_stop) / 1e4 for c in COINS}
    cmp_path = config.BACKTEST_DIR / Path(a.comparar).name if a.comparar else None
    if cmp_path is not None and cmp_path.name == path.name:
        cmp_path = None
    rep = run_lab(a.desde, a.procesos, a.max, a.azar, a.permutaciones, stop_extra=sx, compare=cmp_path)
    config.BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    fz = write_frozen(rep, path.stem)
    out = "\n".join(summary_text(rep))
    try:
        print(out)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    print(f"\nGuardado en {path}" + (f" (lista congelada en {fz})" if fz else ""))


if __name__ == "__main__":
    main()
